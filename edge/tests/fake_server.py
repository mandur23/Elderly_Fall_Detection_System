"""시험용 가짜 PC 서버 (aiohttp). 실제 서버 없이 edge를 시험한다.

테스트에서는 FakeServer를 직접 쓰고, PC에서 손으로 시험할 때는 실행한다:
    python -m edge.tests.fake_server --port 8000 --token test-token --demo
--demo는 접속마다 아주 작은 소리 재생 -> playback_done 확인 -> 마이크 2초 켜기 -> 반복 재생과 중단을 해 본다.
받은 영상·소리는 메모리에서 세기만 하고 저장하지 않는다.
"""

import argparse
import asyncio
import logging
import math
import struct
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from aiohttp import WSMsgType, web

from shared import protocol

log = logging.getLogger("fake_server")


def tone(seconds: float, freq: float = 440.0, amplitude: int = 800) -> bytes:
    """16kHz s16le 사인파. 기본 음량은 아주 작다(최대값의 약 2.5%)."""
    n = int(protocol.AUDIO_RATE * seconds)
    return struct.pack(f"<{n}h", *(int(amplitude * math.sin(2 * math.pi * freq * i / protocol.AUDIO_RATE))
                                    for i in range(n)))


class FakeServer:
    def __init__(self, token: str = "test-token", welcome_video: Optional[Dict[str, Any]] = None,
                 host: str = "127.0.0.1", port: int = 0, welcome_protocol: int = protocol.PROTOCOL_VERSION,
                 welcome_extra: Optional[Dict[str, Any]] = None):
        self.token, self.host, self.port = token, host, port
        self.welcome_video = {**protocol.DEFAULT_VIDEO, **(welcome_video or {})}
        self.welcome_protocol = welcome_protocol
        self.welcome_extra = dict(welcome_extra or {})   # welcome에 더 실을 항목 (예: edge_version)
        self.auto_welcome = True
        self.hellos: List[Dict[str, Any]] = []
        self.auth_headers: List[str] = []
        self.auth_failures = 0
        self.connections = 0
        self.frames: List[Tuple[float, float, bytes]] = []   # (촬영 시각, 받은 시각, JPEG)
        self.audio: List[Tuple[float, float, bytes]] = []    # (첫 샘플 시각, 받은 시각, PCM)
        self.statuses: List[Dict[str, Any]] = []
        self.done: List[Tuple[float, Dict[str, Any]]] = []   # (받은 시각, playback_done)
        self.errors: List[Dict[str, Any]] = []
        self.close_codes: List[Optional[int]] = []
        self.ws: Optional[web.WebSocketResponse] = None
        self.on_connect: Optional[Callable[["FakeServer"], Any]] = None
        self._runner: Optional[web.AppRunner] = None

    @property
    def address(self) -> str:
        return f"{self.host}:{self.port}"

    async def start(self) -> None:
        app = web.Application()
        app.router.add_get(protocol.WS_PATH, self._ws_handler)
        app.router.add_get(protocol.HEALTH_PATH, self._health)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, self.port)
        await site.start()
        self.port = self._runner.addresses[0][1]

    async def stop(self) -> None:
        if self.ws is not None and not self.ws.closed:
            await self.ws.close()
        if self._runner is not None:
            await self._runner.cleanup()

    async def _health(self, request: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    async def _ws_handler(self, request: web.Request) -> web.StreamResponse:
        header = request.headers.get(protocol.AUTH_HEADER, "")
        if protocol.token_from_header(header) != self.token:
            self.auth_failures += 1
            raise web.HTTPUnauthorized(text="bad token")
        self.auth_headers.append(header)
        ws = web.WebSocketResponse(heartbeat=protocol.HEARTBEAT_SECONDS, max_msg_size=16 * 1024 * 1024)
        await ws.prepare(request)
        old, self.ws = self.ws, ws
        if old is not None and not old.closed:
            await old.close(code=protocol.CLOSE_REPLACED, message=b"replaced")
        self.connections += 1
        async for msg in ws:
            now = time.monotonic()
            if msg.type == WSMsgType.TEXT:
                data = protocol.decode(msg.data)
                kind = data["type"]
                if kind == protocol.HELLO:
                    self.hellos.append(data)
                    if self.auto_welcome:
                        await ws.send_str(protocol.encode(protocol.WELCOME, protocol=self.welcome_protocol,
                                                          video=self.welcome_video, name="시험 서버",
                                                          **self.welcome_extra))
                    if self.on_connect is not None:
                        asyncio.ensure_future(self.on_connect(self))
                elif kind == protocol.STATUS:
                    self.statuses.append(data)
                elif kind == protocol.PLAYBACK_DONE:
                    self.done.append((now, data))
                elif kind == protocol.ERROR:
                    self.errors.append(data)
            elif msg.type == WSMsgType.BINARY:
                kind, ts, payload = protocol.unpack(msg.data)
                if kind == protocol.VIDEO:
                    self.frames.append((ts, now, payload))
                elif kind == protocol.AUDIO_IN:
                    self.audio.append((ts, now, payload))
        self.close_codes.append(ws.close_code)
        return ws

    async def send(self, type_: str, **fields: Any) -> None:
        await self.ws.send_str(protocol.encode(type_, **fields))

    async def send_audio(self, pcm: bytes, piece: int = 3200) -> None:
        for i in range(0, len(pcm), piece):
            await self.ws.send_bytes(protocol.pack(protocol.AUDIO_OUT, 0.0, pcm[i:i + piece]))

    async def play(self, clip_id: Any, pcm: bytes, loop: bool = False) -> None:
        await self.send(protocol.PLAY_BEGIN, id=clip_id, loop=loop)
        await self.send_audio(pcm)
        await self.send(protocol.PLAY_END, id=clip_id)

    async def drop(self, code: int = 1001) -> None:
        """현재 연결을 서버 쪽에서 끊는다."""
        if self.ws is not None:
            await self.ws.close(code=code)

    @staticmethod
    async def wait_until(predicate: Callable[[], bool], timeout: float = 5.0, interval: float = 0.02) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            await asyncio.sleep(interval)
        return predicate()


# --- 손으로 시험할 때 ---
async def _demo(server: FakeServer) -> None:
    """접속마다 한 번: 짧은 소리 재생 -> 마이크 2초 -> 반복 재생 1초 후 중단."""
    await asyncio.sleep(3.0)
    n_done = len(server.done)
    log.info("데모: 0.5초 재생 (id=demo-tone)")
    await server.play("demo-tone", tone(0.5))
    t = time.monotonic()
    if await server.wait_until(lambda: len(server.done) > n_done, 5.0):
        log.info("데모: playback_done %s (%.2f초 뒤)", server.done[-1][1], time.monotonic() - t)
    n_audio = len(server.audio)
    await server.send(protocol.MIC, enabled=True)
    await asyncio.sleep(2.0)
    await server.send(protocol.MIC, enabled=False)
    chunks = server.audio[n_audio:]
    if chunks:
        pcm = b"".join(c[2] for c in chunks)
        samples = struct.unpack(f"<{len(pcm) // 2}h", pcm)
        rms = math.sqrt(sum(s * s for s in samples) / max(len(samples), 1))
        gaps = [b[0] - a[0] for a, b in zip(chunks, chunks[1:])]
        log.info("데모: 마이크 %d조각 (크기 %s, 시각 간격 평균 %.3f초, RMS %.0f)", len(chunks),
                 sorted({len(c[2]) for c in chunks}), sum(gaps) / max(len(gaps), 1), rms)
    else:
        log.info("데모: 마이크 소리를 받지 못했습니다")
    n_done = len(server.done)
    log.info("데모: 반복 재생 1초 후 play_stop (id=demo-loop)")
    await server.play("demo-loop", tone(0.2, 660.0), loop=True)
    await asyncio.sleep(1.0)
    await server.send(protocol.PLAY_STOP)
    if await server.wait_until(lambda: len(server.done) > n_done, 3.0):
        log.info("데모: playback_done %s", server.done[-1][1])


async def _report(server: FakeServer, interval: float = 2.0) -> None:
    last = 0
    while True:
        await asyncio.sleep(interval)
        frames = server.frames[last:]
        last = len(server.frames)
        status = server.statuses[-1] if server.statuses else {}
        if frames:
            span = frames[-1][0] - frames[0][0]
            fps = (len(frames) - 1) / span if span > 0 else 0.0
            lag = sum(now - ts for ts, now, _ in frames) / len(frames)
            log.info("영상 %d장 (%.1ffps, 평균 지연 %.0fms, 마지막 %d바이트) | 상태 %s", len(frames), fps, lag * 1000,
                     len(frames[-1][2]), {k: v for k, v in status.items() if k != "type"})
        else:
            log.info("영상 없음 | 상태 %s", {k: v for k, v in status.items() if k != "type"})


async def _main(args: argparse.Namespace) -> None:
    server = FakeServer(token=args.token, host=args.host, port=args.port)
    if args.demo:
        server.on_connect = _demo
    await server.start()
    log.info("가짜 서버: ws://%s%s (토큰 %s)", server.address, protocol.WS_PATH, args.token)
    try:
        await _report(server)
    finally:
        await server.stop()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="edge 시험용 가짜 서버")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=protocol.DEFAULT_PORT)
    parser.add_argument("--token", default="test-token")
    parser.add_argument("--demo", action="store_true", help="접속마다 재생·마이크·중단을 시험")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s")
    try:
        asyncio.run(_main(parser.parse_args()))
    except KeyboardInterrupt:
        pass
