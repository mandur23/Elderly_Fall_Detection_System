"""aiohttp 서버 하나로 edge WebSocket(/ws/device)과 현장 화면·관리자 화면·API(HTTP)를 함께 제공한다.

라즈베리파이가 여러 대 붙을 수 있다. 장치마다 영상 판정·음성 확인·경보가 따로 돈다(events.py).

경로
  GET  /ws/device                 edge 접속 (Authorization: Bearer <DEVICE_TOKEN>, 틀리면 401)
  GET  /kiosk?device=<id>&key=<k> 현장 화면 (라즈베리파이 Chromium이 자기 device와 키로 연다)
  GET  /api/health                서버가 떠 있으면 200 (규약)
  GET  /api/state?device=<id>     그 장치의 상태, 진행 중 이벤트, 문제
  POST /api/clear?device=<id>     화면 '괜찮아요' 버튼 (X-Kiosk-Clear: 1 헤더 필요)
  GET  /api/events?device=<id>    그 장치의 최근 이벤트 기록 (limit=N, 관리자는 device 없이·kind=system도)
       현장 화면 API 셋은 X-Kiosk-Key: <장치별 키>(shared/protocol.kiosk_key) 헤더나 관리자 로그인이 필요하다.
  GET  /admin                     관리자 화면: 모든 장치 상태·이벤트·장치 이상
  GET/POST /admin/login, /admin/logout
  GET  /api/admin/overview        관리자 화면 데이터
  POST /api/admin/clear?device=   관리자 해제 (X-Admin-Action: 1 헤더 필요)
  GET/POST /api/admin/guardians, POST/DELETE /api/admin/guardians/<id>   보호자 계정 관리
  DELETE /api/admin/devices/<id>  연결이 끊긴 장치를 목록에서 지우기
  GET  /g                         보호자 웹 (맡은 장치만, 휴대폰 화면)
  GET/POST /g/login, POST /g/logout
  GET  /api/g/overview            보호자 웹 데이터
  POST /api/g/clear?device=       보호자 해제 (X-Guardian-Action: 1 헤더, 해제 권한이 있는 계정만)

관리자 화면은 admin_password가 있으면 로그인(쿠키)으로, 없으면 서버 PC 자신(127.0.0.1)에서 직접 연 요청만
허락한다. 같은 PC의 터널·프록시(cloudflared 등)를 거쳐 온 요청은 서버 PC 자신으로 보지 않는다.
보호자 웹은 관리자가 만든 보호자 계정으로 로그인한다(server/guardians.py).
자세 추정(PoseFactory)·음성 인식(Recognizer)·알림(Notifier)·안내 음성은 바꿔 끼울 수 있다(테스트용).
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import hmac
import html
import json
import logging
import secrets
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from urllib.parse import quote

from aiohttp import WSMsgType, web

from server.config import SERVER_DIR, Settings
from server.device import DeviceDisconnected, DeviceLink, DeviceRegistry
from server.events import (ALARM, CLEARED_BY_ADMIN, CLEARED_BY_BUTTON, CLEARED_BY_GUARDIAN, SERVER_KEY, SUSPECT,
                           EventService)
from server.fall import FallParams
from server.guardians import (SESSION_SECONDS, GuardianError, Guardians, LoginLimiter, check_password,
                              hash_password)
from server.notifier import Notifier, create_notifier
from server.pose import MediaPipePoseFactory, PoseFactory
from server.speech import Sound, prepare_prompt, siren_pcm
from server.store import Store, now_iso
from server.vision import VideoPipeline
from server.voice import Recognizer, VoskRecognizer
from shared import protocol as P

log = logging.getLogger("server")

STATIC_DIR = SERVER_DIR / "static"
HELLO_TIMEOUT_SECONDS = 10.0
CLEAR_HEADER = "X-Kiosk-Clear"
ADMIN_HEADER = "X-Admin-Action"   # 관리자 화면 스크립트만 붙이는 헤더 (CSRF 방지)
ADMIN_COOKIE = "fall_admin"
ADMIN_LOGIN_FAIL_DELAY = 1.0      # 비밀번호를 빠르게 여러 번 넣어 보지 못하게
ADMIN_SESSION_SECONDS = 30 * 24 * 3600
ADMIN_SECRET_KEY = "admin_session_secret"
# 같은 PC의 터널·역방향 프록시가 붙이는 헤더. 하나라도 있으면 127.0.0.1에서 왔어도 바깥 요청으로 본다.
FORWARD_HEADERS = ("Forwarded", "X-Forwarded-For", "X-Forwarded-Host", "X-Real-IP", "Cf-Connecting-Ip",
                   "True-Client-Ip")
LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")
GUARDIAN_HEADER = "X-Guardian-Action"
GUARDIAN_COOKIE = "fall_guardian"
VIEW_ORDER = {"alarm": 0, "suspect": 1, "disconnected": 2, "check": 3, "normal": 4}
_dumps = functools.partial(json.dumps, ensure_ascii=False)


def _is_loopback(remote: Optional[str]) -> bool:
    return bool(remote) and (remote == "::1" or remote.startswith("127."))


def _host_name(host: Optional[str]) -> str:
    """Host 헤더에서 포트를 뺀 이름. '[::1]:8000' -> '::1'."""
    host = (host or "").strip().lower()
    if host.startswith("["):
        return host[1:host.find("]")] if "]" in host else host
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


def _is_local_request(request: web.Request) -> bool:
    """서버 PC에서 직접 연 요청. 같은 PC의 터널·프록시를 거쳤거나(전달 헤더), 다른 이름으로 들어온
    요청(DNS rebinding)은 아니다."""
    if not _is_loopback(request.remote):
        return False
    if any(h in request.headers for h in FORWARD_HEADERS):
        return False
    return _host_name(request.host) in LOCAL_HOSTS


def _client_ip(request: web.Request) -> str:
    """로그인 제한에 쓸 주소. 같은 PC의 터널·프록시가 넘긴 요청이면 그 프록시가 알려 준 원래 주소를 쓴다
    (그렇지 않으면 바깥 사용자가 모두 127.0.0.1 하나로 보여 한 사람이 모두를 잠글 수 있다)."""
    remote = request.remote or "-"
    if _is_loopback(remote):
        for header in ("Cf-Connecting-Ip", "X-Real-IP", "True-Client-Ip"):
            if request.headers.get(header):
                return request.headers[header].strip()[:64]
        forwarded = request.headers.get("X-Forwarded-For", "")
        if forwarded:
            return forwarded.split(",")[0].strip()[:64]
    return remote


def _same(a: str, b: str) -> bool:
    """시간이 일정한 비교. 쿠키·헤더에 ASCII가 아닌 글자가 와도 오류(500)가 나지 않게 bytes로 비교한다."""
    return hmac.compare_digest(a.encode(), b.encode())


def _is_https(request: web.Request) -> bool:
    """직접 https이거나, 앞단(터널·역방향 프록시)이 https로 받아 넘긴 요청."""
    return request.secure or request.headers.get("X-Forwarded-Proto", "").lower() == "https"


async def _security_headers(request: web.Request, response: web.StreamResponse) -> None:
    # 다른 사이트가 화면을 몰래 틀(frame)에 넣어 해제 버튼을 누르게 하지 못하게 한다.
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")


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
        self._tasks: set = set()
        self.devices = DeviceRegistry()
        self.pipelines: Dict[str, VideoPipeline] = {}
        self.fall_params = FallParams.from_settings(settings)
        self.store: Optional[Store] = None
        self.events: Optional[EventService] = None
        self.started_iso = now_iso()
        self.started_at = time.monotonic()
        self._admin_secret = b""  # 관리자 세션을 비밀번호와 묶는 키 (기록 파일에 두어 재시작해도 로그인 유지)
        self.guardians: Optional[Guardians] = None
        self.login_limiter = LoginLimiter()
        self.admin_limiter = LoginLimiter(by_user=False)

        self.app = web.Application()
        self.app.add_routes([
            web.get(P.WS_PATH, self.ws_device),
            web.get("/", self.index),
            web.get(P.KIOSK_PATH, self.kiosk),
            web.get(P.HEALTH_PATH, self.health),
            web.get("/api/state", self.api_state),
            web.post("/api/clear", self.api_clear),
            web.get("/api/events", self.api_events),
            web.get("/admin", self.admin_page),
            web.get("/admin/login", self.admin_login_page),
            web.post("/admin/login", self.admin_login),
            web.post("/admin/logout", self.admin_logout),
            web.get("/api/admin/overview", self.api_admin_overview),
            web.post("/api/admin/clear", self.api_admin_clear),
            web.get("/api/admin/guardians", self.api_admin_guardians),
            web.post("/api/admin/guardians", self.api_admin_guardian_create),
            web.post("/api/admin/guardians/{gid:\\d+}", self.api_admin_guardian_update),
            web.delete("/api/admin/guardians/{gid:\\d+}", self.api_admin_guardian_delete),
            web.delete("/api/admin/devices/{device_id}", self.api_admin_forget_device),
            web.get("/g", self.guardian_page),
            web.get("/g/login", self.guardian_login_page),
            web.post("/g/login", self.guardian_login),
            web.post("/g/logout", self.guardian_logout),
            web.get("/api/g/overview", self.api_guardian_overview),
            web.post("/api/g/clear", self.api_guardian_clear),
            web.static("/static", STATIC_DIR),
        ])
        self.app.on_response_prepare.append(_security_headers)
        self.app.on_startup.append(self._startup)
        self.app.on_shutdown.append(self._shutdown)
        self.app.on_cleanup.append(self._cleanup)

    # --- 수명 주기 ---
    async def _startup(self, app: web.Application) -> None:
        self.store = Store(self.s.path(self.s.db_path))
        self.guardians = Guardians(self.store)
        secret = self.store.kv_get(ADMIN_SECRET_KEY)
        if secret is None:
            secret = secrets.token_hex(32)
            self.store.kv_set(ADMIN_SECRET_KEY, secret)
        self._admin_secret = bytes.fromhex(secret)
        # 서버를 다시 켜도 전에 접속했던 장치를 기억해, 다시 붙지 않으면 '연결 끊김'으로 보이고 알린다.
        for row in self.store.list_devices():
            self.devices.remember(row["device_id"], row["name"], row["last_seen"])
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
                                   model_problems=self._model_problems, device_problems=self._pipeline_problems)
        await self.events.start()
        for device_id in self.events.cases:  # 장치 기록이 없던 예전 기록 파일의 경보
            if device_id and device_id not in self.devices.known:
                self.devices.remember(device_id, device_id, None)

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
        if not self.recognizer.ok:
            out.append("음성 인식 모델을 불러오지 못했습니다")
        return out

    def _pipeline_problems(self, device_id: str, now: float) -> List[str]:
        pipeline = self.pipelines.get(device_id)
        return pipeline.problems(now) if pipeline is not None else []

    def _reset_detection(self, device_id: str) -> None:
        """그 장치의 이벤트가 끝나면 그 장치의 영상 판정만 다시 시작한다."""
        pipeline = self.pipelines.get(device_id)
        if pipeline is not None:
            pipeline.reset_detection()

    async def _on_suspect(self, device_id: str, features) -> None:
        await self.events.report_suspect(device_id, features)

    # --- edge WebSocket ---
    def _authorized(self, request: web.Request) -> bool:
        token = P.token_from_header(request.headers.get(P.AUTH_HEADER, ""))
        return bool(token) and _same(token, self.s.device_token)

    async def ws_device(self, request: web.Request) -> web.StreamResponse:
        if not self._authorized(request):
            log.warning("토큰이 틀린 장치 접속을 거절했습니다 (%s)", request.remote)
            raise web.HTTPUnauthorized(text="invalid device token")
        # JPEG·PCM은 압축해도 거의 줄지 않으므로 permessage-deflate를 쓰지 않는다(양쪽 CPU·지연 절약).
        ws = web.WebSocketResponse(heartbeat=P.HEARTBEAT_SECONDS, max_msg_size=8 * 1024 * 1024, compress=False)
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
        # 장치가 여러 대일 수 있으므로 라즈베리파이가 보낸 이름을 먼저 쓴다.
        name = str(hello.get("name") or "").strip() or self.s.device_name or device_id
        link = DeviceLink(ws, device_id, name, hello)
        old = self.devices.add(link)
        if old is not None:
            log.warning("[%s] 같은 장치가 다시 접속해 이전 연결을 닫습니다 (4409)", device_id)
            old.mark_closed()
            # 이전 연결이 이미 죽었으면 닫기 응답을 오래 기다릴 수 있으므로 기다리지 않는다.
            self._spawn(old.ws.close(code=P.CLOSE_REPLACED, message=b"replaced by new connection"))
        try:
            self.store.save_device(device_id, name)
            if not self.pose_factory.error:
                pipeline = self.pipelines.get(device_id)
                if pipeline is None:
                    pipeline = VideoPipeline(device_id, self.pose_factory, self.fall_params,
                                             self.s.descent_window_seconds, self._on_suspect,
                                             expected_fps=self.s.video_fps)
                    self.pipelines[device_id] = pipeline
                pipeline.new_session()
                pipeline.start()

            await link.send(P.WELCOME, protocol=P.PROTOCOL_VERSION, video=self.s.video, name=name)
            log.info("[%s] 장치 접속 (%s, %s, 소프트웨어 %s)", device_id, remote, name, hello.get("software", "-"))
            await self.events.on_device_connected(link)
        except BaseException:
            # welcome을 보내는 사이 끊기는 등: 등록을 되돌려 '연결됨'으로 남지 않게 한다.
            self._unregister(link)
            raise
        return link

    def _unregister(self, link: DeviceLink) -> None:
        link.mark_closed()
        if self.devices.remove(link):
            log.warning("[%s] 장치 연결 끊김", link.device_id)
            try:
                self.store.touch_device(link.device_id)
            except Exception:
                log.exception("[%s] 장치 기록 실패", link.device_id)

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _on_binary(self, link: DeviceLink, data: bytes) -> None:
        kind, timestamp, payload = P.unpack(data)
        if link.closed or self.devices.links.get(link.device_id) is not link:
            return  # 새 연결로 바뀐 이전 연결에서 늦게 온 장면·소리 (같은 판정에 섞지 않는다)
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

    # --- HTTP: 현장 화면 ---
    async def index(self, request: web.Request) -> web.StreamResponse:
        raise web.HTTPFound("/admin")

    async def kiosk(self, request: web.Request) -> web.Response:
        page = (STATIC_DIR / "kiosk.html").read_text(encoding="utf-8")
        page = page.replace("{{PROMPT_TEXT}}", html.escape(self.s.prompt_text))
        return web.Response(text=page, content_type="text/html", headers={"Cache-Control": "no-store"})

    async def health(self, request: web.Request) -> web.Response:
        return _json({"ok": True, "protocol": P.PROTOCOL_VERSION,
                                  "state": self.events.state if self.events else "STARTING",
                                  "devices_connected": len(self.devices.links)})

    def _device_ids(self) -> List[str]:
        """한 번이라도 접속한 장치와 경보가 남은 장치 (접속 순서)."""
        return list(dict.fromkeys([*self.devices.known, *self.devices.links, *filter(None, self.events.cases)]))

    def kiosk_url(self, device_id: str) -> str:
        """그 장치의 현장 화면 주소 (장치별 키 포함). 관리자 화면에서만 내보낸다."""
        return (f"{P.KIOSK_PATH}?device={quote(device_id, safe='')}"
                f"&key={P.kiosk_key(self.s.device_token, device_id)}")

    def _kiosk_ok(self, request: web.Request, device_id: Optional[str]) -> bool:
        """현장 화면 API: 그 장치의 키(X-Kiosk-Key)가 맞거나 관리자.

        현장 화면에는 로그인이 없으므로, 같은 공유기나 터널로 서버에 닿는 누구나 경보를 끄거나 다른 방의
        상태를 보지 못하게 장치마다 다른 키를 요구한다. 키는 edge가 토큰으로 계산해 화면 주소에 넣는다."""
        if device_id:
            key = request.headers.get(P.KIOSK_KEY_HEADER, "")
            if key and _same(key, P.kiosk_key(self.s.device_token, device_id)):
                return True
        return self._admin_ok(request)

    def _require_kiosk(self, request: web.Request, device_id: Optional[str]) -> None:
        if not self._kiosk_ok(request, device_id):
            raise web.HTTPUnauthorized(text="kiosk key required")

    def _device_view(self, device_id: str, problems: List[str]) -> str:
        state = self.events.state_of(device_id)
        if state == ALARM:
            return "alarm"
        if state == SUSPECT:
            return "suspect"
        if device_id not in self.devices.links:
            return "disconnected"
        return "check" if problems else "normal"

    def _device_items(self) -> List[Dict]:
        items = []
        for device_id in self._device_ids():
            link = self.devices.links.get(device_id)
            info = self.devices.known.get(device_id, {})
            item = {"device_id": device_id, "name": self.events.device_label(device_id),
                    "connected": link is not None, "since": info.get("since"),
                    "state": self.events.state_of(device_id)}
            if link is not None:
                st = link.status
                item.update({k: st.get(k) for k in ("camera_ok", "mic_ok", "speaker_ok", "fps", "cpu_temp_c",
                                                    "uptime_s")})
                item["software"] = link.hello.get("software")
            pipeline = self.pipelines.get(device_id)
            if pipeline is not None:
                item.update(pipeline.snapshot())
            items.append(item)
        return items

    def state_snapshot(self, device_id: Optional[str] = None) -> Dict:
        """현장 화면용. device_id 장치 하나의 상태. 장치가 한 대뿐이면 device_id를 생략해도 그 장치."""
        now = time.monotonic()
        ids = self._device_ids()
        if device_id is None and len(ids) == 1:
            device_id = ids[0]
        common = {
            "devices": self._device_items(),
            "notifier_enabled": self.notifier.enabled,
            "prompt_source": self._prompt_sound.source if self._prompt_sound else None,
        }
        if device_id is None:
            # 장치가 없거나 여러 대인데 어느 장치인지 모름: 현장 화면은 ?device=로 장치를 정해야 한다.
            # (관리자만 여기 온다. 장치 목록에 각 장치의 현장 화면 주소를 붙인다.)
            common["devices"] = [{**d, "kiosk_url": self.kiosk_url(d["device_id"])} for d in common["devices"]]
            return {**common, "device_id": None, "state": self.events.state,
                    "view": "select" if ids else "disconnected", "event": None,
                    "problems": self.events.problems(now), "location": self.s.device_name,
                    "device_alert_sent": self.events.device_alert_sent}
        problems = self.events.problems(now, device_id)
        # 한 방의 현장 화면에는 다른 방의 정보를 보내지 않는다.
        common["devices"] = [d for d in common["devices"] if d["device_id"] == device_id]
        return {**common, "device_id": device_id, "state": self.events.state_of(device_id),
                "view": self._device_view(device_id, problems), "event": self.events.event_snapshot(device_id),
                "problems": problems, "location": self.events.device_label(device_id),
                "device_alert_sent": self.events.alert_sent_for(device_id)}

    async def api_state(self, request: web.Request) -> web.Response:
        device_id = request.query.get("device") or None
        self._require_kiosk(request, device_id)
        return _json(self.state_snapshot(device_id), headers={"Cache-Control": "no-store"})

    async def api_clear(self, request: web.Request) -> web.Response:
        # 다른 웹 페이지가 몰래 해제 요청을 보내지 못하게(CSRF) 화면 스크립트만 붙이는 헤더를 요구한다.
        if request.headers.get(CLEAR_HEADER) != "1":
            raise web.HTTPForbidden(text=f"{CLEAR_HEADER} header required")
        device_id = request.query.get("device") or None
        self._require_kiosk(request, device_id)
        cleared = await self.events.clear(device_id, CLEARED_BY_BUTTON)
        state = self.events.state_of(device_id) if device_id else self.events.state
        return _json({"cleared": cleared, "state": state})

    async def api_events(self, request: web.Request) -> web.Response:
        try:
            limit = int(request.query.get("limit", "20"))
        except ValueError:
            raise web.HTTPBadRequest(text="limit must be an integer") from None
        device_id = request.query.get("device") or None
        if request.query.get("kind") == "system":
            self._require_admin(request)
            return _json(self.store.recent_system_events(limit))
        self._require_kiosk(request, device_id)  # device 없이(모든 장치)는 관리자만
        return _json(self.store.recent_events(limit, device_id))

    # --- HTTP: 관리자 화면 ---
    def _admin_password_tag(self) -> str:
        """관리자 비밀번호에 묶인 값. 비밀번호를 바꾸면 기존 로그인이 모두 끊긴다."""
        return hmac.new(self._admin_secret, self.s.admin_password.encode(), hashlib.sha256).hexdigest()[:32]

    @staticmethod
    def _token_hash(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def _admin_ok(self, request: web.Request) -> bool:
        if not self.s.admin_password:
            return _is_local_request(request)
        token = request.cookies.get(ADMIN_COOKIE, "")
        if not token:
            return False
        row = self.store.admin_session(self._token_hash(token))
        return (row is not None and row["expires_at"] > time.time()
                and _same(row["password_tag"], self._admin_password_tag()))

    def _admin_denied(self, request: web.Request) -> web.StreamResponse:
        if self.s.admin_password:
            raise web.HTTPFound("/admin/login")
        page = (STATIC_DIR / "admin_denied.html").read_text(encoding="utf-8")
        return web.Response(text=page, content_type="text/html", status=403)

    @staticmethod
    def _page(name: str) -> web.Response:
        page = (STATIC_DIR / name).read_text(encoding="utf-8")
        return web.Response(text=page, content_type="text/html", headers={"Cache-Control": "no-store"})

    async def admin_page(self, request: web.Request) -> web.StreamResponse:
        if not self._admin_ok(request):
            return self._admin_denied(request)
        return self._page("admin.html")

    async def admin_login_page(self, request: web.Request) -> web.StreamResponse:
        if not self.s.admin_password:
            raise web.HTTPFound("/admin")
        return self._page("admin_login.html")

    async def admin_login(self, request: web.Request) -> web.StreamResponse:
        if not self.s.admin_password:
            raise web.HTTPFound("/admin")
        client = _client_ip(request)
        if self.admin_limiter.blocked_for(client, "admin") > 0:
            raise web.HTTPFound("/admin/login?error=locked")
        # 결과를 기다리는 동안 여러 요청을 한꺼번에 보내 제한을 피하지 못하게 먼저 실패로 센다(맞으면 지운다).
        self.admin_limiter.failed(client, "admin")
        form = await request.post()
        password = str(form.get("password", ""))
        if not _same(password, self.s.admin_password):
            log.warning("관리자 로그인 실패 (%s)", client)
            await asyncio.sleep(ADMIN_LOGIN_FAIL_DELAY)
            raise web.HTTPFound("/admin/login?error=1")
        self.admin_limiter.succeeded(client, "admin")
        log.info("관리자 로그인 (%s)", client)
        token = secrets.token_urlsafe(32)
        now = time.time()
        self.store.purge_admin_sessions(now)
        self.store.add_admin_session(self._token_hash(token), now + ADMIN_SESSION_SECONDS, self._admin_password_tag())
        res = web.HTTPFound("/admin")
        res.set_cookie(ADMIN_COOKIE, token, max_age=ADMIN_SESSION_SECONDS, httponly=True, samesite="Strict",
                       secure=_is_https(request), path="/")
        raise res

    async def admin_logout(self, request: web.Request) -> web.StreamResponse:
        token = request.cookies.get(ADMIN_COOKIE, "")
        if token:
            self.store.delete_admin_session(self._token_hash(token))  # 이 로그인은 다시 쓸 수 없다
        res = web.HTTPFound("/admin/login" if self.s.admin_password else "/admin")
        res.del_cookie(ADMIN_COOKIE, path="/")
        raise res

    def admin_overview(self) -> Dict:
        now = time.monotonic()
        subjects = self.events.problems_by_subject(now)
        counts = self.store.counts_since(now_iso(datetime.now() - timedelta(days=1)))
        devices = []
        for item in self._device_items():
            device_id = item["device_id"]
            problems = subjects.get(device_id, [])
            item.update(view=self._device_view(device_id, problems), problems=problems,
                        kiosk_url=self.kiosk_url(device_id),
                        event=self.events.event_snapshot(device_id),
                        alert_sent=self.events.alert_sent_for(device_id),
                        last_24h=counts.get(device_id, {"events": 0, "alarms": 0}))
            devices.append(item)
        devices.sort(key=lambda d: VIEW_ORDER.get(d["view"], 9))
        views = [d["view"] for d in devices]
        return {
            "server": {
                "started_at": self.started_iso,
                "uptime_s": round(now - self.started_at),
                "problems": subjects.get(SERVER_KEY, []),
                "notifier_enabled": self.notifier.enabled,
                "prompt_source": self._prompt_sound.source if self._prompt_sound else None,
                "login": bool(self.s.admin_password),
            },
            "summary": {
                "known": len(devices),
                "connected": sum(d["connected"] for d in devices),
                "alarm": views.count("alarm"),
                "suspect": views.count("suspect"),
                "check": views.count("check") + views.count("disconnected"),
            },
            "devices": devices,
            "events": self.store.recent_events(30),
            "system_events": self.store.recent_system_events(10),
        }

    async def api_admin_overview(self, request: web.Request) -> web.Response:
        if not self._admin_ok(request):
            raise web.HTTPUnauthorized(text="admin login required")
        return _json(self.admin_overview(), headers={"Cache-Control": "no-store"})

    async def api_admin_clear(self, request: web.Request) -> web.Response:
        if not self._admin_ok(request):
            raise web.HTTPUnauthorized(text="admin login required")
        if request.headers.get(ADMIN_HEADER) != "1":
            raise web.HTTPForbidden(text=f"{ADMIN_HEADER} header required")
        device_id = request.query.get("device")
        if not device_id:
            raise web.HTTPBadRequest(text="device is required")
        cleared = await self.events.clear(device_id, CLEARED_BY_ADMIN)
        if cleared:
            log.info("[%s] 관리자 화면에서 해제 (%s)", device_id, request.remote)
        return _json({"cleared": cleared, "state": self.events.state_of(device_id)})

    # --- HTTP: 보호자 계정 관리 (관리자) ---
    def _require_admin(self, request: web.Request, action: bool = False) -> None:
        if not self._admin_ok(request):
            raise web.HTTPUnauthorized(text="admin login required")
        if action and request.headers.get(ADMIN_HEADER) != "1":
            raise web.HTTPForbidden(text=f"{ADMIN_HEADER} header required")

    @staticmethod
    async def _json_body(request: web.Request) -> Dict:
        try:
            body = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise web.HTTPBadRequest(text="JSON body required") from None
        if not isinstance(body, dict):
            raise web.HTTPBadRequest(text="JSON object required")
        return body

    @staticmethod
    async def _hash_new_password(password) -> str:
        """비밀번호 해시는 느리므로(약 0.2초) 다른 스레드에서 만든다(영상·음성 처리가 멈추지 않게)."""
        return await asyncio.to_thread(hash_password, check_password(password))

    async def api_admin_guardians(self, request: web.Request) -> web.Response:
        self._require_admin(request)
        return _json({"guardians": self.guardians.list(), "devices": [
            {"device_id": d["device_id"], "name": d["name"]} for d in self._device_items()]})

    async def api_admin_guardian_create(self, request: web.Request) -> web.Response:
        self._require_admin(request, action=True)
        body = await self._json_body(request)
        try:
            password_hash = await self._hash_new_password(body.get("password"))
            guardian = self.guardians.create(body.get("username"), body.get("name"), devices=body.get("devices", []),
                                             can_clear=body.get("can_clear", True), password_hash=password_hash)
        except GuardianError as e:
            return _json({"error": str(e)}, status=400)
        log.info("보호자 계정 만듦: %s (장치 %s)", guardian["username"], ", ".join(guardian["devices"]) or "-")
        return _json(guardian, status=201)

    async def api_admin_guardian_update(self, request: web.Request) -> web.Response:
        self._require_admin(request, action=True)
        body = await self._json_body(request)
        try:
            password_hash = await self._hash_new_password(body["password"]) if body.get("password") else None
            guardian = self.guardians.update(int(request.match_info["gid"]), name=body.get("name"),
                                             devices=body.get("devices"), can_clear=body.get("can_clear"),
                                             password_hash=password_hash)
        except KeyError:
            raise web.HTTPNotFound(text="no such guardian") from None
        except GuardianError as e:
            return _json({"error": str(e)}, status=400)
        log.info("보호자 계정 바꿈: %s%s", guardian["username"], " (비밀번호 변경)" if body.get("password") else "")
        return _json(guardian)

    async def api_admin_guardian_delete(self, request: web.Request) -> web.Response:
        self._require_admin(request, action=True)
        if not self.guardians.delete(int(request.match_info["gid"])):
            raise web.HTTPNotFound(text="no such guardian")
        return _json({"deleted": True})

    async def api_admin_forget_device(self, request: web.Request) -> web.Response:
        """연결이 끊긴 장치를 목록에서 지운다(치운 라즈베리파이가 계속 '연결 끊김'으로 알리지 않게)."""
        self._require_admin(request, action=True)
        device_id = request.match_info["device_id"]
        if device_id not in self.devices.known:
            raise web.HTTPNotFound(text="no such device")
        if device_id in self.devices.links:
            return _json({"error": "연결된 장치는 지울 수 없습니다"}, status=409)
        if device_id in self.events.cases:
            return _json({"error": "진행 중인 경보·확인이 있는 장치는 지울 수 없습니다"}, status=409)
        self.devices.forget(device_id)
        self.store.delete_device(device_id)
        self.events.forget(device_id)
        pipeline = self.pipelines.pop(device_id, None)
        if pipeline is not None:
            await pipeline.close()
        log.info("[%s] 관리자가 장치를 목록에서 지웠습니다 (%s)", device_id, _client_ip(request))
        return _json({"forgotten": True})

    # --- HTTP: 보호자 웹 ---
    def _guardian(self, request: web.Request) -> Optional[Dict]:
        return self.guardians.from_session(request.cookies.get(GUARDIAN_COOKIE, ""))

    async def guardian_page(self, request: web.Request) -> web.StreamResponse:
        if self._guardian(request) is None:
            raise web.HTTPFound("/g/login")
        return self._page("guardian.html")

    async def guardian_login_page(self, request: web.Request) -> web.StreamResponse:
        return self._page("guardian_login.html")

    async def guardian_login(self, request: web.Request) -> web.StreamResponse:
        form = await request.post()
        username, password = str(form.get("username", ""))[:64], str(form.get("password", ""))[:256]
        client = _client_ip(request)
        if self.login_limiter.blocked_for(client, username) > 0:
            raise web.HTTPFound("/g/login?error=locked")
        # 비밀번호 확인(약 0.2초)을 기다리는 사이 여러 요청을 한꺼번에 보내 제한을 피하지 못하게 먼저 실패로
        # 센다. 맞으면 지운다.
        self.login_limiter.failed(client, username)
        guardian = self.guardians.find(username)
        if not await asyncio.to_thread(Guardians.password_ok, guardian, password):
            log.warning("보호자 로그인 실패: %s (%s)", username[:32], client)
            await asyncio.sleep(ADMIN_LOGIN_FAIL_DELAY)
            raise web.HTTPFound("/g/login?error=1")
        self.guardians.mark_login(guardian)
        self.login_limiter.succeeded(client, username)
        log.info("보호자 로그인: %s (%s)", guardian["username"], client)
        res = web.HTTPFound("/g")
        res.set_cookie(GUARDIAN_COOKIE, self.guardians.session_token(guardian), max_age=SESSION_SECONDS,
                       httponly=True, samesite="Lax", secure=_is_https(request), path="/")
        raise res

    async def guardian_logout(self, request: web.Request) -> web.StreamResponse:
        res = web.HTTPFound("/g/login")
        res.del_cookie(GUARDIAN_COOKIE, path="/")
        raise res

    def guardian_overview(self, guardian: Dict) -> Dict:
        now = time.monotonic()
        subjects = self.events.problems_by_subject(now)
        counts = self.store.counts_since(now_iso(datetime.now() - timedelta(days=1)))
        known = set(self._device_ids())
        devices = []
        for device_id in guardian["devices"]:
            if device_id not in known:
                devices.append({"device_id": device_id, "name": device_id, "connected": False, "since": None,
                                "view": "disconnected", "event": None, "last_24h": {"events": 0, "alarms": 0},
                                "problems": ["아직 한 번도 연결되지 않은 장치입니다"]})
                continue
            problems = subjects.get(device_id, [])
            devices.append({
                "device_id": device_id,
                "name": self.events.device_label(device_id),
                "connected": device_id in self.devices.links,
                "since": self.devices.known.get(device_id, {}).get("since"),
                "view": self._device_view(device_id, problems),
                "event": self.events.event_snapshot(device_id),
                "problems": problems,
                "last_24h": counts.get(device_id, {"events": 0, "alarms": 0}),
            })
        return {
            "guardian": {"name": guardian["name"], "username": guardian["username"],
                         "can_clear": guardian["can_clear"]},
            "devices": devices,
            "events": self.store.events_for_devices(guardian["devices"], 20),
            "server_problems": subjects.get(SERVER_KEY, []),
            "notifier_enabled": self.notifier.enabled,
        }

    async def api_guardian_overview(self, request: web.Request) -> web.Response:
        guardian = self._guardian(request)
        if guardian is None:
            raise web.HTTPUnauthorized(text="login required")
        return _json(self.guardian_overview(guardian), headers={"Cache-Control": "no-store"})

    async def api_guardian_clear(self, request: web.Request) -> web.Response:
        guardian = self._guardian(request)
        if guardian is None:
            raise web.HTTPUnauthorized(text="login required")
        if request.headers.get(GUARDIAN_HEADER) != "1":
            raise web.HTTPForbidden(text=f"{GUARDIAN_HEADER} header required")
        device_id = request.query.get("device", "")
        if device_id not in guardian["devices"] or not guardian["can_clear"]:
            raise web.HTTPForbidden(text="not allowed for this guardian")
        cleared = await self.events.clear(device_id, CLEARED_BY_GUARDIAN + guardian["username"])
        if cleared:
            log.info("[%s] 보호자 웹에서 해제: %s (%s)", device_id, guardian["username"], request.remote)
        return _json({"cleared": cleared, "state": self.events.state_of(device_id)})


def create_app(settings: Settings, **components) -> web.Application:
    return FallServer(settings, **components).app
