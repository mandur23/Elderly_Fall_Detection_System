"""aiohttp 서버 하나로 edge WebSocket(/ws/device)과 현장 화면·API(HTTP)를 함께 제공한다.

경로
  GET  /ws/device           edge 접속 (Authorization: Bearer <DEVICE_TOKEN>, 틀리면 401)
  GET  /kiosk               현장 화면 (라즈베리파이 Chromium이 연다)
  GET  /api/health          서버가 떠 있으면 200 (규약)
  GET  /api/state           현재 상태, 진행 중 이벤트, 장치 문제
  POST /api/clear           화면 '괜찮아요' 버튼 (X-Kiosk-Clear: 1 헤더 필요)
  GET  /api/events?limit=N  최근 이벤트 기록 (kind=system이면 장치 이상·복구 기록)

자세 추정(PoseFactory)·음성 인식(Recognizer)·알림(Notifier)·안내 음성은 바꿔 끼울 수 있다(테스트용).
"""

from __future__ import annotations

import asyncio
import functools
import hmac
import html
import json
import logging
import time
from typing import Dict, List, Optional

from aiohttp import WSMsgType, web

from server.config import SERVER_DIR, Settings
from server.device import DeviceDisconnected, DeviceLink, DeviceRegistry
from server.events import ALARM, SUSPECT, EventService
from server.fall import FallParams
from server.notifier import Notifier, create_notifier
from server.pose import MediaPipePoseFactory, PoseFactory
from server.speech import Sound, prepare_prompt, siren_pcm
from server.store import Store
from server.vision import VideoPipeline
from server.voice import Recognizer, VoskRecognizer
from shared import protocol as P

log = logging.getLogger("server")

STATIC_DIR = SERVER_DIR / "static"
HELLO_TIMEOUT_SECONDS = 10.0
CLEAR_HEADER = "X-Kiosk-Clear"
_dumps = functools.partial(json.dumps, ensure_ascii=False)


def _json(data, **kwargs) -> web.Response:
    return web.json_response(data, dumps=_dumps, **kwargs)


class FallServer:
    def __init__(self, settings: Settings, *, pose_factory: Optional[PoseFactory] = None,
                 recognizer: Optional[Recognizer] = None, notifier: Optional[Notifier] = None,
                 prompt: Optional[Sound] = None):
        if not settings.device_token:
            raise ValueError("device_token(DEVICE_TOKEN)이 설정되지 않았습니다")
        self.s = settings
        self.pose_factory = pose_factory or MediaPipePoseFactory(settings.path(settings.pose_model_path),
                                                                 settings.keypoint_min_confidence)
        self.recognizer = recognizer or VoskRecognizer(settings.path(settings.vosk_model_path), P.AUDIO_RATE,
                                                       settings.voice_grammar)
        self.notifier = notifier or create_notifier(settings)
        self._prompt_sound = prompt
        self._prompt_task: Optional[asyncio.Task] = None
        self.devices = DeviceRegistry()
        self.pipelines: Dict[str, VideoPipeline] = {}
        self.fall_params = FallParams.from_settings(settings)
        self.store: Optional[Store] = None
        self.events: Optional[EventService] = None

        self.app = web.Application()
        self.app.add_routes([
            web.get(P.WS_PATH, self.ws_device),
            web.get("/", self.index),
            web.get(P.KIOSK_PATH, self.kiosk),
            web.get(P.HEALTH_PATH, self.health),
            web.get("/api/state", self.api_state),
            web.post("/api/clear", self.api_clear),
            web.get("/api/events", self.api_events),
            web.static("/static", STATIC_DIR),
        ])
        self.app.on_startup.append(self._startup)
        self.app.on_shutdown.append(self._shutdown)
        self.app.on_cleanup.append(self._cleanup)

    # --- 수명 주기 ---
    async def _startup(self, app: web.Application) -> None:
        self.store = Store(self.s.path(self.s.db_path))
        log.info("자세 추정 모델 불러오는 중: %s", getattr(self.pose_factory, "model_path", self.pose_factory.name))
        await asyncio.to_thread(self.pose_factory.load)
        if self.pose_factory.error:
            log.error("%s", self.pose_factory.error)
        log.info("음성 인식 모델 불러오는 중: %s", getattr(self.recognizer, "model_path", self.recognizer.name))
        await asyncio.to_thread(self.recognizer.load)
        if self._prompt_sound is None:
            # 합성 음성은 몇 초 걸리므로 미리 만들어 둔다(첫 낙상 때 늦지 않게).
            wav = self.s.path(self.s.prompt_wav) if self.s.prompt_wav else None
            self._prompt_task = asyncio.create_task(
                asyncio.to_thread(prepare_prompt, self.s.prompt_text, wav, self.s.tts_voice))
        self.events = EventService(self.s, self.store, self.devices, self.recognizer, self.notifier,
                                   prompt=self._prompt, siren=siren_pcm(), on_finished=self._reset_detection,
                                   model_problems=self._model_problems)
        await self.events.start()

    async def _shutdown(self, app: web.Application) -> None:
        for link in self.devices.connected():
            link.mark_closed()
            await link.ws.close(code=1001, message=b"server shutdown")

    async def _cleanup(self, app: web.Application) -> None:
        if self.events is not None:
            await self.events.stop()
        for pipeline in self.pipelines.values():
            await pipeline.close()
        if self._prompt_task is not None and not self._prompt_task.done():
            self._prompt_task.cancel()
        await self.notifier.close()
        if self.store is not None:
            self.store.close()

    async def _prompt(self) -> Sound:
        if self._prompt_sound is None:
            self._prompt_sound = await asyncio.shield(self._prompt_task)
            log.info("안내 음성 준비 완료 (%s, %.1f초)", self._prompt_sound.source, self._prompt_sound.seconds)
        return self._prompt_sound

    def _model_problems(self) -> List[str]:
        out = []
        if self.pose_factory.error:
            out.append("영상 분석 모델을 불러오지 못했습니다")
        else:
            out.extend(f"영상 분석 오류: {p.error}" for p in self.pipelines.values() if p.error)
        if not self.recognizer.ok:
            out.append("음성 인식 모델을 불러오지 못했습니다")
        return out

    def _reset_detection(self) -> None:
        for pipeline in self.pipelines.values():
            pipeline.reset_detection()

    async def _on_suspect(self, device_id: str, features) -> None:
        await self.events.report_suspect(device_id, features)

    # --- edge WebSocket ---
    def _authorized(self, request: web.Request) -> bool:
        token = P.token_from_header(request.headers.get(P.AUTH_HEADER, ""))
        return bool(token) and hmac.compare_digest(token.encode(), self.s.device_token.encode())

    async def ws_device(self, request: web.Request) -> web.StreamResponse:
        if not self._authorized(request):
            log.warning("토큰이 틀린 장치 접속을 거절했습니다 (%s)", request.remote)
            raise web.HTTPUnauthorized(text="invalid device token")
        ws = web.WebSocketResponse(heartbeat=P.HEARTBEAT_SECONDS, max_msg_size=8 * 1024 * 1024)
        await ws.prepare(request)
        link: Optional[DeviceLink] = None
        try:
            hello = await self._receive_hello(ws)
            link = await self._register(ws, hello, request.remote)
            async for msg in ws:
                if msg.type == WSMsgType.BINARY:
                    self._on_binary(link, msg.data)
                elif msg.type == WSMsgType.TEXT:
                    self._on_text(link, msg.data)
                elif msg.type == WSMsgType.ERROR:
                    log.warning("[%s] WebSocket 오류: %s", link.device_id, ws.exception())
                    break
        except P.ProtocolError as e:
            log.warning("규약 위반으로 연결을 닫습니다 (%s): %s", link.device_id if link else request.remote, e)
            if not ws.closed:
                try:
                    await ws.send_str(P.encode(P.ERROR, message=str(e)))
                except (ConnectionError, RuntimeError):
                    pass
                await ws.close(code=P.CLOSE_PROTOCOL_ERROR, message=str(e)[:100].encode())
        except DeviceDisconnected:
            pass  # welcome 등을 보내는 사이 끊김
        finally:
            if link is not None:
                self._unregister(link)
        return ws

    async def _receive_hello(self, ws: web.WebSocketResponse) -> Dict:
        try:
            msg = await ws.receive(timeout=HELLO_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            raise P.ProtocolError("접속 후 hello가 오지 않았습니다") from None
        if msg.type != WSMsgType.TEXT:
            raise P.ProtocolError("첫 메시지는 hello여야 합니다")
        hello = P.decode(msg.data)
        if hello["type"] != P.HELLO:
            raise P.ProtocolError(f"첫 메시지는 hello여야 합니다 ({hello['type']})")
        if hello["protocol"] != P.PROTOCOL_VERSION:
            raise P.ProtocolError(f"규약 버전이 다릅니다 (edge {hello['protocol']}, 서버 {P.PROTOCOL_VERSION})")
        device_id = hello["device_id"]
        if not isinstance(device_id, str) or not device_id.strip():
            raise P.ProtocolError("device_id가 비어 있습니다")
        audio = hello["audio"] if isinstance(hello["audio"], dict) else {}
        if (audio.get("rate"), audio.get("channels"), audio.get("format")) != (P.AUDIO_RATE, P.AUDIO_CHANNELS, "s16le"):
            raise P.ProtocolError(f"지원하지 않는 소리 형식입니다: {hello['audio']}")
        return hello

    async def _register(self, ws: web.WebSocketResponse, hello: Dict, remote: Optional[str]) -> DeviceLink:
        device_id = hello["device_id"].strip()
        name = self.s.device_name or str(hello.get("name") or device_id)
        link = DeviceLink(ws, device_id, name, hello)
        old = self.devices.add(link)
        if old is not None:
            log.warning("[%s] 같은 장치가 다시 접속해 이전 연결을 닫습니다 (4409)", device_id)
            old.mark_closed()
            # 이전 연결이 이미 죽었으면 닫기 응답을 오래 기다릴 수 있으므로 기다리지 않는다.
            asyncio.create_task(old.ws.close(code=P.CLOSE_REPLACED, message=b"replaced by new connection"))

        if not self.pose_factory.error:
            pipeline = self.pipelines.get(device_id)
            if pipeline is None:
                pipeline = VideoPipeline(device_id, self.pose_factory, self.fall_params,
                                         self.s.descent_window_seconds, self._on_suspect)
                self.pipelines[device_id] = pipeline
            pipeline.new_session()  # edge 시계가 달라질 수 있으므로 판정 기록을 새로 시작한다
            pipeline.start()

        await link.send(P.WELCOME, protocol=P.PROTOCOL_VERSION, video=self.s.video, name=name)
        log.info("[%s] 장치 접속 (%s, %s, 소프트웨어 %s)", device_id, remote, name, hello.get("software", "-"))
        await self.events.on_device_connected(link)
        return link

    def _unregister(self, link: DeviceLink) -> None:
        link.mark_closed()
        if self.devices.remove(link):
            log.warning("[%s] 장치 연결 끊김", link.device_id)

    def _on_binary(self, link: DeviceLink, data: bytes) -> None:
        kind, timestamp, payload = P.unpack(data)
        if kind == P.VIDEO:
            link.last_video_at = time.monotonic()
            pipeline = self.pipelines.get(link.device_id)
            if pipeline is not None:
                pipeline.submit(timestamp, payload)
        elif kind == P.AUDIO_IN:
            link.on_audio(timestamp, payload)
        else:
            raise P.ProtocolError("AUDIO_OUT은 서버만 보냅니다")

    def _on_text(self, link: DeviceLink, data: str) -> None:
        msg = P.decode(data)
        kind = msg["type"]
        if kind not in P.EDGE_TO_SERVER:
            raise P.ProtocolError(f"edge가 보낼 수 없는 메시지입니다: {kind}")
        if kind == P.STATUS:
            link.update_status(msg)
        elif kind == P.PLAYBACK_DONE:
            link.on_playback_done(msg)
        elif kind == P.ERROR:
            log.warning("[%s] edge 오류 보고: %s", link.device_id, msg["message"])
        elif kind == P.HELLO:
            log.warning("[%s] 접속 중 hello를 다시 받아 무시합니다", link.device_id)

    # --- HTTP ---
    async def index(self, request: web.Request) -> web.StreamResponse:
        raise web.HTTPFound(P.KIOSK_PATH)

    async def kiosk(self, request: web.Request) -> web.Response:
        page = (STATIC_DIR / "kiosk.html").read_text(encoding="utf-8")
        page = page.replace("{{PROMPT_TEXT}}", html.escape(self.s.prompt_text))
        return web.Response(text=page, content_type="text/html", headers={"Cache-Control": "no-store"})

    async def health(self, request: web.Request) -> web.Response:
        return _json({"ok": True, "protocol": P.PROTOCOL_VERSION,
                                  "state": self.events.state if self.events else "STARTING",
                                  "devices_connected": len(self.devices.links)})

    def state_snapshot(self) -> Dict:
        now = time.monotonic()
        problems = self.events.problems(now)
        state = self.events.state
        if state == ALARM:
            view = "alarm"
        elif state == SUSPECT:
            view = "suspect"
        elif not self.devices.links:
            view = "disconnected"
        elif problems:
            view = "check"
        else:
            view = "normal"
        devices = []
        for device_id, info in self.devices.known.items():
            link = self.devices.links.get(device_id)
            item = {"device_id": device_id, "name": info["name"], "connected": link is not None,
                    "since": info["since"]}
            if link is not None:
                st = link.status
                item.update({k: st.get(k) for k in ("camera_ok", "mic_ok", "speaker_ok", "fps", "cpu_temp_c")})
            pipeline = self.pipelines.get(device_id)
            if pipeline is not None:
                item.update(pipeline.snapshot())
            devices.append(item)
        return {
            "state": state,
            "view": view,
            "event": self.events.event_snapshot(),
            "problems": problems,
            "devices": devices,
            "location": self.s.device_name,
            "notifier_enabled": self.notifier.enabled,
            "device_alert_sent": self.events.device_alert_sent,
            "prompt_source": self._prompt_sound.source if self._prompt_sound else None,
        }

    async def api_state(self, request: web.Request) -> web.Response:
        return _json(self.state_snapshot(), headers={"Cache-Control": "no-store"})

    async def api_clear(self, request: web.Request) -> web.Response:
        # 다른 웹 페이지가 몰래 해제 요청을 보내지 못하게(CSRF) 화면 스크립트만 붙이는 헤더를 요구한다.
        if request.headers.get(CLEAR_HEADER) != "1":
            raise web.HTTPForbidden(text=f"{CLEAR_HEADER} header required")
        cleared = await self.events.clear_by_button()
        return _json({"cleared": cleared, "state": self.events.state})

    async def api_events(self, request: web.Request) -> web.Response:
        try:
            limit = int(request.query.get("limit", "20"))
        except ValueError:
            raise web.HTTPBadRequest(text="limit must be an integer") from None
        if request.query.get("kind") == "system":
            return _json(self.store.recent_system_events(limit))
        return _json(self.store.recent_events(limit))


def create_app(settings: Settings, **components) -> web.Application:
    return FallServer(settings, **components).app
