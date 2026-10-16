"""PC 서버 WebSocket 클라이언트 (aiohttp).

접속 -> hello -> welcome(영상 설정 적용) -> 영상·상태 전송, 서버 명령(config, mic, play_*) 처리.
끊기면 재생과 마이크 전송을 멈추고 1·2·5·10초(이후 10초) 간격으로 다시 접속한다. 규약은 shared/protocol.py.
장치 감시는 접속 여부와 상관없이 돌며, 다시 시작해야만 고칠 수 있는 문제면 run()이 FatalError로 끝난다.
welcome의 edge_version이 이 장치(edge/VERSION)와 다르면 영상을 보내기 전에 UpdateRequested로 끝난다
(종료 코드 3 -> systemd가 다시 띄우며 python -m edge.update가 새 코드를 받는다. 판단은 edge/update.py).
"""

import asyncio
import logging
import math
import platform
import socket
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional, Sequence, Set

import aiohttp

from edge import __version__
from edge.camera import Camera
from edge.config import EdgeConfig
from edge.mic import Microphone
from edge.speaker import Speaker
from edge.update import VERSION_FILE, local_version, record_attempt, restart_reason
from shared import protocol

log = logging.getLogger("edge.client")

RETRY_DELAYS = (1.0, 2.0, 5.0, 10.0)
WELCOME_TIMEOUT = 10.0
CAMERA_HANG_EXIT_SECONDS = 60.0   # 카메라 스레드가 이만큼 멈추면(드라이버 정지) 끝내고 systemd가 다시 띄우게 한다
AUDIO_FAIL_EXIT_SECONDS = 60.0    # 마이크·스피커를 이만큼 못 썼고 사운드 카드가 바뀌었으면 끝낸다(PortAudio가 장치를 다시 찾게)
WATCHDOG_SECONDS = 2.0            # 위 문제를 확인하는 간격
AUDIO_QUEUE_CHUNKS = 50           # 보내지 못한 마이크 소리는 5초까지만 쌓는다
MAX_MESSAGE_BYTES = 16 * 1024 * 1024
THERMAL_PATH = Path("/sys/class/thermal/thermal_zone0/temp")

# 보내지 못한 영상이 쌓이지 않게 하는 송신 버퍼 크기 (limit_send_buffers)
NOTSENT_LOWAT_BYTES = 32 * 1024   # 커널에 둘 아직 보내지 못한 데이터 (리눅스 TCP_NOTSENT_LOWAT)
WRITE_BUFFER_HIGH = 16 * 1024     # asyncio 쓰기 버퍼가 이보다 크면 보내기(drain)를 기다린다
FLUSH_POLL_SECONDS = 0.01
# 파이썬에 상수가 없어도 리눅스면 값이 25로 정해져 있다. 다른 OS에서는 설정하지 않는다.
_TCP_NOTSENT_LOWAT = getattr(socket, "TCP_NOTSENT_LOWAT", 25 if sys.platform.startswith("linux") else None)

# 서버가 보낸 영상 설정의 허용 범위
_VIDEO_LIMITS = {"width": (16, 4096), "height": (16, 4096), "fps": (1, 60), "jpeg_quality": (1, 100)}


class FatalError(RuntimeError):
    """다시 접속해도 소용없는 문제. 프로그램을 끝낸다(systemd가 다시 띄운다)."""


class UpdateRequested(FatalError):
    """서버의 edge 버전이 달라 업데이트하려고 끝낸다. main이 종료 코드 3으로 끝내면 systemd가 다시 띄우며
    ExecStartPre(python -m edge.update)가 새 코드를 받는다."""


class _Closed(Exception):
    def __init__(self, message: str, code: Optional[int]):
        super().__init__(message)
        self.code = code


def retry_delay(attempt: int, delays: Sequence[float] = RETRY_DELAYS, close_code: Optional[int] = None) -> float:
    """attempt번째(0부터) 재접속 대기 시간. 다시 접속해도 같은 결과일 문제(토큰 거절 401, 규약 오류 4400,
    같은 device_id 접속 4409)면 가장 긴 간격으로 기다린다."""
    if close_code in (401, protocol.CLOSE_PROTOCOL_ERROR, protocol.CLOSE_REPLACED):
        return delays[-1]
    return delays[min(attempt, len(delays) - 1)]


def read_cpu_temp() -> Optional[float]:
    """라즈베리파이(리눅스) CPU 온도(°C). 읽을 수 없으면 None."""
    try:
        return int(THERMAL_PATH.read_text().strip()) / 1000.0
    except (OSError, ValueError):
        return None


def clean_video(video: Any) -> Dict[str, Any]:
    """서버가 보낸 영상 설정에서 쓸 수 있는 값만 고른다."""
    if not isinstance(video, dict):
        return {}
    out: Dict[str, Any] = {}
    for key, (low, high) in _VIDEO_LIMITS.items():
        value = video.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        if low <= value <= high:
            out[key] = float(value) if key == "fps" else int(value)
        else:
            log.warning("영상 설정 %s=%r이 허용 범위(%s~%s)를 벗어나 무시합니다", key, value, low, high)
    return out


def _ws_transport(ws) -> Optional[asyncio.Transport]:
    """WebSocket 연결의 asyncio transport. aiohttp가 공개하지 않는 속성이라 찾지 못하면 None."""
    conn = getattr(getattr(ws, "_response", None), "connection", None)
    return getattr(conn, "transport", None)


def limit_send_buffers(ws) -> Optional[asyncio.Transport]:
    """보내지 못한 영상이 송신 버퍼에 쌓이지 않게 한다. 연결의 transport(못 찾으면 None)를 돌려준다.

    약한 Wi-Fi에서는 커널 송신 버퍼(자동으로 수 MB까지 커짐)에 장면이 쌓여 서버가 몇 초 전 영상을 받고,
    ping/pong도 그 뒤에 밀려 연결이 끊긴다. 커널에는 아직 보내지 못한 데이터를 조금만 두고(TCP_NOTSENT_LOWAT),
    asyncio 쓰기 버퍼도 작게 해서, 이전 장면이 아직 나가는 중이면 새 장면을 버리게 한다(_wants_frame).
    리눅스가 아니거나 설정할 수 없으면 그대로 둔다.
    """
    sock = ws.get_extra_info("socket")
    if sock is not None and _TCP_NOTSENT_LOWAT is not None:
        try:
            sock.setsockopt(socket.IPPROTO_TCP, _TCP_NOTSENT_LOWAT, NOTSENT_LOWAT_BYTES)
        except (OSError, ValueError, AttributeError) as e:
            log.debug("TCP_NOTSENT_LOWAT을 설정하지 못했습니다: %s", e)
    transport = _ws_transport(ws)
    if transport is not None:
        try:
            transport.set_write_buffer_limits(high=WRITE_BUFFER_HIGH)
        except (AttributeError, NotImplementedError, ValueError) as e:
            log.debug("쓰기 버퍼 한도를 바꾸지 못했습니다: %s", e)
    return transport


async def _wait_flushed(transport: Optional[asyncio.Transport]) -> None:
    """asyncio 쓰기 버퍼가 빌 때까지(커널로 다 넘어갈 때까지) 기다린다. 연결이 닫히면 바로 끝난다."""
    while transport is not None and not transport.is_closing() and transport.get_write_buffer_size() > 0:
        await asyncio.sleep(FLUSH_POLL_SECONDS)


class EdgeClient:
    def __init__(self, config: EdgeConfig, camera: Camera, mic: Microphone, speaker: Speaker, *,
                 retry_delays: Sequence[float] = RETRY_DELAYS,
                 status_interval: float = protocol.STATUS_INTERVAL_SECONDS,
                 heartbeat: float = protocol.HEARTBEAT_SECONDS,
                 welcome_timeout: float = WELCOME_TIMEOUT,
                 camera_hang_exit: float = CAMERA_HANG_EXIT_SECONDS,
                 audio_fail_exit: float = AUDIO_FAIL_EXIT_SECONDS,
                 watchdog_interval: float = WATCHDOG_SECONDS,
                 version_file: Path = VERSION_FILE,
                 attempt_file: Optional[Path] = None):
        self.config, self.camera, self.mic, self.speaker = config, camera, mic, speaker
        self.retry_delays, self.status_interval = tuple(retry_delays), status_interval
        self.heartbeat, self.welcome_timeout, self.camera_hang_exit = heartbeat, welcome_timeout, camera_hang_exit
        self.audio_fail_exit, self.watchdog_interval = audio_fail_exit, watchdog_interval
        self.version_file = version_file    # 서버에서 받아 설치한 버전. 없으면 개발 폴더(업데이트하려고 끝내지 않음)
        self.attempt_file = attempt_file    # 업데이트 시도 기록 (None이면 ~/.cache/fall-edge/update-attempt.json)
        self.connections = 0       # welcome까지 받은 접속 수
        self.frames_sent = 0
        self.frames_dropped = 0    # 이전 장면을 보내는 중이라 버린 장면
        self.audio_sent = 0
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._connected = False    # welcome을 받아 영상을 보내는 중
        self._mic_enabled = False
        self._sending_video = False
        self._pending_frame = None
        self._frame_event: Optional[asyncio.Event] = None
        self._audio_queue: Optional[asyncio.Queue] = None
        self._stopping: Optional[asyncio.Event] = None
        self._fatal: Optional[FatalError] = None   # 감시가 찾은 문제. run()이 끝날 때 던진다
        self._send_tasks: Set[asyncio.Task] = set()  # 보내는 중인 playback_done (끝나기 전에 사라지지 않게)
        self._started = time.monotonic()

    # --- 바깥에서 부르는 것 ---
    def stop(self) -> None:
        """run()을 끝낸다. 이벤트 루프 스레드에서 부른다."""
        if self._stopping is not None:
            self._stopping.set()

    @property
    def connected(self) -> bool:
        return self._connected

    async def run(self) -> None:
        """stop()이 불릴 때까지 접속·재접속을 반복한다. 다시 시작해야만 고칠 수 있는 장치 문제면 FatalError."""
        self._loop = asyncio.get_running_loop()
        self._stopping = asyncio.Event()
        self._fatal = None
        self._frame_event = asyncio.Event()
        self._audio_queue = asyncio.Queue(maxsize=AUDIO_QUEUE_CHUNKS)
        self.camera.set_sink(self._frame_from_thread, self._wants_frame)
        self.mic.set_sink(self._audio_from_thread)
        self.speaker.attach(self._loop, self._on_playback_done)
        # 서버에 접속하지 못하는 동안에도 감시한다(그동안 카메라가 멈춰도 다시 시작되게).
        watchdog = asyncio.create_task(self._watchdog(), name="watchdog")
        attempt = 0
        timeout = aiohttp.ClientTimeout(total=None, connect=10, sock_connect=5)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                while not self._stopping.is_set():
                    welcomed, close_code = False, None
                    try:
                        result = await self._until_stopped(self._session(session))
                        if result is None:
                            break
                        welcomed, close_code = result
                    except aiohttp.WSServerHandshakeError as e:
                        if e.status == 401:
                            log.error("서버가 장치 토큰을 거절했습니다(401). config.toml의 token을 확인하세요")
                            close_code = 401
                        else:
                            log.warning("서버 접속 실패: HTTP %s %s", e.status, e.message)
                    except _Closed as e:
                        log.warning("%s", e)
                        close_code = e.code
                    except (aiohttp.ClientError, OSError, asyncio.TimeoutError) as e:
                        log.warning("서버 연결 문제: %s", str(e) or type(e).__name__)
                    except protocol.ProtocolError as e:
                        log.error("서버와 규약이 맞지 않습니다: %s", e)
                        close_code = protocol.CLOSE_PROTOCOL_ERROR
                    finally:
                        self._disconnected()
                    if self._stopping.is_set():
                        break
                    if welcomed:
                        attempt = 0
                    if close_code == protocol.CLOSE_REPLACED:
                        log.error("같은 device_id(%s)로 다른 장치가 접속해 연결이 끊겼습니다. device_id가 겹치지 않는지 확인하세요",
                                  self.config.device_id)
                    delay = retry_delay(attempt, self.retry_delays, close_code)
                    attempt += 1
                    log.info("%.0f초 뒤 다시 접속합니다", delay)
                    try:
                        await asyncio.wait_for(self._stopping.wait(), delay)
                    except asyncio.TimeoutError:
                        pass
        finally:
            watchdog.cancel()
            await asyncio.gather(watchdog, return_exceptions=True)
            self._disconnected()
            self.camera.set_sink(None, lambda: False)
            self.mic.set_sink(None)
            self.speaker.attach(self._loop, None)
        if self._fatal is not None:
            raise self._fatal

    async def _until_stopped(self, coro):
        """coro를 실행하되 stop()이 불리면 취소하고 None을 돌려준다(접속 시도·welcome 대기 중에도 바로 끝나게)."""
        task = asyncio.ensure_future(coro)
        stopping = asyncio.ensure_future(self._stopping.wait())
        try:
            await asyncio.wait({task, stopping}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            stopping.cancel()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        return None if task.cancelled() else task.result()

    # --- 접속 한 번 ---
    async def _session(self, session: aiohttp.ClientSession):
        """접속해서 끊길 때까지 처리한다. (welcome을 받았는지, 닫기 코드)."""
        url = self.config.ws_url
        log.info("서버에 접속합니다: %s", url)
        # JPEG·PCM은 압축해도 거의 줄지 않고 라즈베리파이 CPU만 쓰므로 permessage-deflate를 쓰지 않는다(compress=0).
        async with session.ws_connect(url, headers=protocol.bearer(self.config.token), heartbeat=self.heartbeat,
                                      max_msg_size=MAX_MESSAGE_BYTES, compress=0) as ws:
            self._ws = ws
            transport = limit_send_buffers(ws)
            await ws.send_str(self._hello())
            welcome = await asyncio.wait_for(self._wait_welcome(ws), self.welcome_timeout)
            if welcome.get("protocol") != protocol.PROTOCOL_VERSION:
                message = f"규약 버전이 다릅니다 (서버 {welcome.get('protocol')!r}, edge {protocol.PROTOCOL_VERSION})"
                await self._send_error(ws, message)
                await ws.close(code=protocol.CLOSE_PROTOCOL_ERROR)
                raise protocol.ProtocolError(message)
            self._apply_video(welcome.get("video"))
            reason = self._update_reason(welcome.get("edge_version"))
            if reason:
                await ws.close(code=aiohttp.WSCloseCode.GOING_AWAY)
                raise UpdateRequested(reason)
            self._connected = True
            self.connections += 1
            log.info("서버 접속됨 (설치 위치: %s, 영상 %s)", welcome.get("name") or self.config.name or "-",
                     _video_text(self.camera.video))
            tasks = [
                asyncio.create_task(self._receive_loop(ws), name="receive"),
                asyncio.create_task(self._video_loop(ws, transport), name="video"),
                asyncio.create_task(self._audio_loop(ws), name="audio"),
                asyncio.create_task(self._status_loop(ws), name="status"),
            ]
            try:
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
            for task in done:
                exc = None if task.cancelled() else task.exception()
                if isinstance(exc, ConnectionResetError):
                    # 닫는 중인 연결에 보내려다 난 오류. 끊김은 아래에서 알린다.
                    log.debug("연결이 닫히는 중이라 보내지 못함(%s): %s", task.get_name(), exc)
                elif exc is not None:
                    log.warning("연결 처리 중 오류(%s): %s", task.get_name(), exc)
        code = ws.close_code
        if not self._stopping.is_set():
            log.warning("서버 연결이 끊겼습니다 (닫기 코드 %s)", code)
        return True, code

    def _update_reason(self, server_version: Any) -> Optional[str]:
        """welcome의 edge_version이 이 장치와 달라 다시 시작해 업데이트해야 하면 그 이유. 없으면 None.

        재생이나 음성 확인 중이면 하지 않는다(접속 직후라 보통은 아님). 업데이트가 계속 실패해도 30분 안에 같은 버전으로
        다시 끝내지 않게 시도 기록을 먼저 남기고, 남기지 못하면 끝내지 않는다.
        """
        reason = restart_reason(server_version, version_file=self.version_file, attempt_file=self.attempt_file)
        if reason is None or self._audio_in_use():
            return None
        if not record_attempt(str(server_version).strip(), "다시 시작 요청", self.attempt_file):
            return None
        return reason

    def _hello(self) -> str:
        video = self.camera.video
        bundle = local_version(self.version_file)
        return protocol.encode(
            protocol.HELLO,
            protocol=protocol.PROTOCOL_VERSION,
            device_id=self.config.device_id,
            name=self.config.name,
            software=(f"fall-edge {__version__}{f' (edge {bundle})' if bundle else ''} "
                      f"(python {platform.python_version()}, {sys.platform})"),
            video={"width": video["width"], "height": video["height"], "fps": video["fps"], "format": "jpeg"},
            audio={"rate": protocol.AUDIO_RATE, "channels": protocol.AUDIO_CHANNELS, "format": "s16le"},
        )

    async def _wait_welcome(self, ws) -> Dict[str, Any]:
        while True:
            msg = await ws.receive()
            if msg.type == aiohttp.WSMsgType.TEXT:
                data = self._decode(msg.data)
                if data is None:
                    continue
                if data["type"] == protocol.WELCOME:
                    return data
                await self._handle(ws, data)
            elif msg.type == aiohttp.WSMsgType.BINARY:
                self._handle_binary(msg.data)
            else:
                raise _Closed(f"welcome 전에 연결이 닫혔습니다 (닫기 코드 {ws.close_code})", ws.close_code)

    async def _receive_loop(self, ws) -> None:
        while True:
            msg = await ws.receive()
            if msg.type == aiohttp.WSMsgType.TEXT:
                data = self._decode(msg.data)
                if data is not None:
                    await self._handle(ws, data)
            elif msg.type == aiohttp.WSMsgType.BINARY:
                self._handle_binary(msg.data)
            elif msg.type == aiohttp.WSMsgType.ERROR:
                log.warning("WebSocket 오류: %s", ws.exception())
                return
            else:  # CLOSE, CLOSING, CLOSED
                return

    # --- 서버 메시지 처리 ---
    def _decode(self, text: str) -> Optional[Dict[str, Any]]:
        try:
            data = protocol.decode(text)
        except protocol.ProtocolError as e:
            log.warning("잘못된 서버 메시지를 무시합니다: %s", e)
            return None
        if data["type"] not in protocol.SERVER_TO_EDGE:
            log.warning("서버가 보낼 수 없는 메시지를 무시합니다: %s", data["type"])
            return None
        return data

    async def _handle(self, ws, msg: Dict[str, Any]) -> None:
        kind = msg["type"]
        if kind in (protocol.WELCOME, protocol.CONFIG):
            self._apply_video(msg.get("video"))
        elif kind == protocol.MIC:
            self._set_mic(bool(msg["enabled"]))
        elif kind == protocol.PLAY_BEGIN:
            clip_id = msg["id"]
            if isinstance(clip_id, bool) or not isinstance(clip_id, (str, int, float)):
                await self._send_error(ws, f"재생 id는 문자열이나 숫자여야 합니다: {clip_id!r}")
                return
            self.speaker.begin(clip_id, bool(msg.get("loop", False)))
            log.info("재생 시작: %r%s", clip_id, " (반복)" if msg.get("loop") else "")
        elif kind == protocol.PLAY_END:
            self.speaker.end(msg["id"])
        elif kind == protocol.PLAY_STOP:
            if not self.speaker.stop(notify=True):
                log.debug("play_stop: 재생 중인 소리가 없습니다")
        elif kind == protocol.ERROR:
            log.warning("서버 오류 메시지: %s", msg["message"])

    def _handle_binary(self, data: bytes) -> None:
        try:
            kind, _, payload = protocol.unpack(data)
        except protocol.ProtocolError as e:
            log.warning("잘못된 바이너리 메시지를 무시합니다: %s", e)
            return
        if kind == protocol.AUDIO_OUT:
            self.speaker.feed(payload)
        else:
            log.warning("서버가 보낼 수 없는 바이너리 메시지를 무시합니다: 0x%02x", kind)

    def _apply_video(self, video: Any) -> None:
        values = clean_video(video)
        if values:
            before = self.camera.video
            self.camera.apply(values)
            after = self.camera.video
            if after != before:
                log.info("영상 설정: %s", _video_text(after))

    def _set_mic(self, enabled: bool) -> None:
        if enabled != self._mic_enabled:
            log.info("마이크 전송 %s", "시작" if enabled else "중지")
        self._mic_enabled = enabled
        self.mic.active = enabled
        if not enabled and self._audio_queue is not None:
            while not self._audio_queue.empty():
                self._audio_queue.get_nowait()

    def _on_playback_done(self, clip_id: Any, stopped: bool) -> None:
        """Speaker가 이벤트 루프에서 부른다."""
        ws = self._ws
        if not self._connected or ws is None or ws.closed:
            return
        fields = {"id": clip_id}
        if stopped:
            fields["stopped"] = True
        log.info("재생 %s: %r", "중단" if stopped else "끝", clip_id)
        task = asyncio.ensure_future(self._send_text(ws, protocol.encode(protocol.PLAYBACK_DONE, **fields)))
        self._send_tasks.add(task)
        task.add_done_callback(self._send_tasks.discard)

    async def _send_text(self, ws, text: str) -> None:
        try:
            await ws.send_str(text)
        except (ConnectionError, RuntimeError, aiohttp.ClientError) as e:
            log.debug("보내지 못함: %s", e)

    async def _send_error(self, ws, message: str) -> None:
        await self._send_text(ws, protocol.encode(protocol.ERROR, message=message))

    # --- 영상 ---
    def _wants_frame(self) -> bool:
        """카메라 스레드에서 부른다. 이전 장면을 보내는 중이면 인코딩하지 않는다."""
        return self._connected and not self._sending_video

    def _frame_from_thread(self, jpeg: bytes, ts: float) -> None:
        try:
            self._loop.call_soon_threadsafe(self._offer_frame, jpeg, ts)
        except RuntimeError:
            pass  # 이벤트 루프가 끝남

    def _offer_frame(self, jpeg: bytes, ts: float) -> None:
        if not self._connected:
            return
        if self._sending_video:
            self.frames_dropped += 1
            return
        self._pending_frame = (jpeg, ts)
        self._frame_event.set()

    async def _video_loop(self, ws, transport: Optional[asyncio.Transport] = None) -> None:
        while True:
            await self._frame_event.wait()
            self._frame_event.clear()
            item, self._pending_frame = self._pending_frame, None
            if item is None:
                continue
            jpeg, ts = item
            self._sending_video = True
            try:
                await ws.send_bytes(protocol.pack(protocol.VIDEO, ts, jpeg))
                # send_bytes는 transport에 넣기만 하면 돌아온다. 장면이 버퍼를 다 떠날 때까지 '보내는 중'으로 두어
                # 그동안 찍힌 장면은 버린다(약한 Wi-Fi에서 오래된 장면이 쌓이지 않게).
                await _wait_flushed(transport)
            finally:
                self._sending_video = False
            self.frames_sent += 1

    # --- 마이크 ---
    def _audio_from_thread(self, pcm: bytes, ts: float) -> None:
        if self._mic_enabled:
            try:
                self._loop.call_soon_threadsafe(self._offer_audio, pcm, ts)
            except RuntimeError:
                pass

    def _offer_audio(self, pcm: bytes, ts: float) -> None:
        if not (self._connected and self._mic_enabled):
            return
        if self._audio_queue.full():
            self._audio_queue.get_nowait()  # 가장 오래된 것을 버린다
        self._audio_queue.put_nowait((pcm, ts))

    async def _audio_loop(self, ws) -> None:
        while True:
            pcm, ts = await self._audio_queue.get()
            if self._mic_enabled:
                await ws.send_bytes(protocol.pack(protocol.AUDIO_IN, ts, pcm))
                self.audio_sent += 1

    # --- 상태 ---
    def status_fields(self, fps: float) -> Dict[str, Any]:
        camera_ok, camera_error = self.camera.status()
        mic_ok, mic_error = self.mic.status()
        speaker_ok, speaker_error = self.speaker.status()
        fields: Dict[str, Any] = {
            "camera_ok": camera_ok, "mic_ok": mic_ok, "speaker_ok": speaker_ok,
            "fps": round(fps, 1), "uptime_s": round(time.monotonic() - self._started),
        }
        for key, error in (("camera_error", camera_error), ("mic_error", mic_error), ("speaker_error", speaker_error)):
            if error:
                fields[key] = error
        temp = read_cpu_temp()
        if temp is not None:
            fields["cpu_temp_c"] = round(temp, 1)
        return fields

    async def _status_loop(self, ws) -> None:
        last_t, last_frames = time.monotonic(), self.frames_sent
        fps = 0.0
        while True:
            await ws.send_str(protocol.encode(protocol.STATUS, **self.status_fields(fps)))
            await asyncio.sleep(self.status_interval)
            now = time.monotonic()
            fps = (self.frames_sent - last_frames) / max(now - last_t, 1e-6)
            last_t, last_frames = now, self.frames_sent

    # --- 장치 감시 ---
    def fatal_problem(self) -> Optional[str]:
        """다시 시작해야만 고칠 수 있는 장치 문제의 설명. 없으면 None."""
        hang = self.camera.stalled_for()
        if math.isinf(hang):
            return "카메라 스레드가 멈췄습니다"
        if hang > self.camera_hang_exit:
            return f"카메라가 {hang:.0f}초 동안 응답하지 않습니다"
        if self._audio_in_use():
            return None   # 음성 확인이나 재생을 끊지 않는다. 끝난 뒤에 다시 본다
        # PortAudio는 시작할 때 한 번만 장치를 찾으므로 USB 마이크·스피커를 다시 꽂으면 다시 시작해야 쓸 수 있다.
        # 사운드 카드가 그대로면 다시 시작해도 같으므로(장치가 없거나 다른 프로그램이 씀) 끝내지 않는다.
        for part, label in ((self.mic, "마이크"), (self.speaker, "스피커")):
            failing = part.failing_for()
            if failing > self.audio_fail_exit and part.devices_changed():
                return f"{label}를 {failing:.0f}초 동안 쓰지 못했고 사운드 카드가 바뀌었습니다. 장치를 다시 찾습니다"
        return None

    def _audio_in_use(self) -> bool:
        """서버가 켠 마이크 전송(음성 확인)이나 재생이 실제로 진행 중인지."""
        listening = self._mic_enabled and self.mic.status()[0]
        playing = self.speaker.buffer.busy and self.speaker.status()[0]
        return listening or playing

    async def _watchdog(self) -> None:
        """접속 여부와 상관없이 fatal_problem()을 본다. 문제가 있으면 run()을 끝낸다(systemd가 다시 띄운다)."""
        while True:
            await asyncio.sleep(self.watchdog_interval)
            try:
                problem = self.fatal_problem()
            except Exception:
                log.exception("장치 감시 중 오류")
                continue
            if problem:
                self._fatal = FatalError(problem)
                self._stopping.set()
                return

    def _disconnected(self) -> None:
        was_connected = self._connected
        self._connected = False
        self._ws = None
        self._set_mic(False)
        self._pending_frame = None
        self._sending_video = False
        stopped = self.speaker.stop(notify=False)
        if was_connected and stopped:
            log.info("연결이 끊겨 재생을 멈췄습니다: %s", stopped)


def _video_text(video: Dict[str, Any]) -> str:
    return f"{video['width']}x{video['height']} {video['fps']:g}fps 품질 {video['jpeg_quality']}"
