"""접속한 edge 장치: 메시지 보내기, 재생·마이크 제어, 장치 상태.

DeviceLink는 WebSocket 연결 하나다. 같은 device_id가 다시 접속하면 새 DeviceLink가 생기고 이전 것은 닫힌다.
연결이 끊기면 기다리던 재생 완료와 마이크 소리 대기가 모두 DeviceDisconnected로 끝난다(음성 확인 중이면 경보).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional

from aiohttp import web

from server.store import now_iso
from shared import protocol as P

log = logging.getLogger("server.device")

PLAY_CHUNK_BYTES = P.AUDIO_RATE * P.AUDIO_SAMPLE_BYTES // 2  # 0.5초씩 나눠 보낸다


class DeviceDisconnected(Exception):
    pass


def _short(text: Any, limit: int = 80) -> str:
    text = str(text or "").strip()
    return text if len(text) <= limit else text[:limit - 1] + "…"


class DeviceLink:
    def __init__(self, ws: web.WebSocketResponse, device_id: str, name: str, hello: Dict[str, Any]):
        self.ws = ws
        self.device_id = device_id
        self.name = name
        self.hello = hello
        self.connected_at = time.monotonic()
        self.connected_iso = now_iso()
        self.status: Dict[str, Any] = {}
        self.status_at: Optional[float] = None
        self.last_video_at: Optional[float] = None
        self.closed = False
        self._play_lock = asyncio.Lock()
        self._playbacks: Dict[str, asyncio.Future] = {}
        self._play_seq = 0
        self._mic: Optional[asyncio.Queue] = None
        self.mic_on = False

    # --- 보내기 ---
    async def send(self, type_: str, **fields: Any) -> None:
        if self.closed:
            raise DeviceDisconnected(self.device_id)
        try:
            await self.ws.send_str(P.encode(type_, **fields))
        except (ConnectionError, RuntimeError) as e:  # 이미 닫힌 연결
            raise DeviceDisconnected(self.device_id) from e

    async def _send_bytes(self, data: bytes) -> None:
        if self.closed:
            raise DeviceDisconnected(self.device_id)
        try:
            await self.ws.send_bytes(data)
        except (ConnectionError, RuntimeError) as e:
            raise DeviceDisconnected(self.device_id) from e

    # --- 재생 ---
    async def play(self, pcm: bytes, *, loop: bool = False, still_wanted=None) -> Optional[str]:
        """play_begin -> AUDIO_OUT 여러 개 -> play_end. 재생 id를 돌려준다.

        still_wanted가 있으면 보내기 직전에 확인해 False면 보내지 않는다(None). 경보음을 보내려는 사이에
        현장에서 해제한 경우처럼 순서가 엇갈려 소리가 남지 않게 하기 위함이다.
        """
        async with self._play_lock:
            if still_wanted is not None and not still_wanted():
                return None
            self._play_seq += 1
            pid = f"{'loop' if loop else 'play'}-{self._play_seq}"
            if not loop:
                fut = asyncio.get_running_loop().create_future()
                # 기다리는 쪽이 없을 때(취소 등) 끊김 예외가 '처리되지 않음' 경고로 남지 않게 한다.
                fut.add_done_callback(lambda f: f.cancelled() or f.exception())
                self._playbacks[pid] = fut
            await self.send(P.PLAY_BEGIN, id=pid, loop=loop)
            for i in range(0, len(pcm), PLAY_CHUNK_BYTES):
                await self._send_bytes(P.pack(P.AUDIO_OUT, 0.0, pcm[i:i + PLAY_CHUNK_BYTES]))
            await self.send(P.PLAY_END, id=pid)
            return pid

    async def wait_playback(self, pid: str, timeout: float) -> Dict[str, Any]:
        """playback_done을 기다린다. 시간 초과면 asyncio.TimeoutError, 끊기면 DeviceDisconnected."""
        fut = self._playbacks.get(pid)
        if fut is None:
            raise DeviceDisconnected(self.device_id)
        try:
            return await asyncio.wait_for(asyncio.shield(fut), timeout)
        finally:
            if fut.done():
                self._playbacks.pop(pid, None)

    async def stop_playback(self) -> None:
        """play_stop. 연결이 이미 끊겼으면 조용히 넘어간다."""
        try:
            async with self._play_lock:
                await self.send(P.PLAY_STOP)
        except DeviceDisconnected:
            pass

    def on_playback_done(self, msg: Dict[str, Any]) -> None:
        fut = self._playbacks.pop(str(msg["id"]), None)
        if fut is not None and not fut.done():
            fut.set_result(msg)

    # --- 마이크 ---
    async def start_mic(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        self._mic = queue
        self.mic_on = True
        await self.send(P.MIC, enabled=True)
        return queue

    async def stop_mic(self) -> None:
        self._mic = None
        if self.mic_on:
            self.mic_on = False
            try:
                await self.send(P.MIC, enabled=False)
            except DeviceDisconnected:
                pass

    def on_audio(self, timestamp: float, pcm: bytes) -> None:
        if self._mic is not None:
            self._mic.put_nowait(pcm)
        # 마이크를 켜지 않았을 때 온 소리는 버린다(평소 방 안 소리는 처리하지 않음).

    # --- 상태 ---
    def update_status(self, msg: Dict[str, Any]) -> None:
        self.status = {k: v for k, v in msg.items() if k != "type"}
        self.status_at = time.monotonic()

    def voice_device_problem(self) -> Optional[str]:
        """음성 확인에 필요한 마이크·스피커 문제. 없으면 None."""
        if self.status_at is not None:
            if not self.status.get("speaker_ok", True):
                return "스피커 오류: " + _short(self.status.get("speaker_error") or "edge 보고")
            if not self.status.get("mic_ok", True):
                return "마이크 오류: " + _short(self.status.get("mic_error") or "edge 보고")
        return None

    def problems(self, now: float, status_stale: float, video_stale: float) -> List[str]:
        out = []
        status_ok = self.status_at is not None and now - self.status_at <= status_stale
        if not status_ok:
            if self.status_at is not None or now - self.connected_at > status_stale:
                out.append("장치 상태 보고가 끊겼습니다")
        else:
            for key, label in (("camera", "카메라"), ("mic", "마이크"), ("speaker", "스피커")):
                if not self.status.get(f"{key}_ok", True):
                    err = self.status.get(f"{key}_error")
                    out.append(f"{label}를 확인해 주세요" + (f" ({_short(err)})" if err else ""))
        camera_ok = self.status.get("camera_ok", True)
        last_video = self.last_video_at if self.last_video_at is not None else self.connected_at
        if camera_ok and now - last_video > video_stale:
            out.append("영상이 들어오지 않습니다")
        return out

    def mark_closed(self) -> None:
        if self.closed:
            return
        self.closed = True
        for fut in self._playbacks.values():
            if not fut.done():
                fut.set_exception(DeviceDisconnected(self.device_id))
        self._playbacks.clear()
        if self._mic is not None:
            self._mic.put_nowait(None)  # 소리를 기다리던 쪽을 깨운다
            self._mic = None
        self.mic_on = False


class DeviceRegistry:
    def __init__(self):
        self.links: Dict[str, DeviceLink] = {}
        self.known: Dict[str, Dict[str, Any]] = {}  # 한 번이라도 접속한 장치

    def add(self, link: DeviceLink) -> Optional[DeviceLink]:
        """새 연결을 등록하고, 같은 device_id의 이전 연결이 있으면 돌려준다(호출한 쪽이 4409로 닫는다)."""
        old = self.links.get(link.device_id)
        self.links[link.device_id] = link
        self.known[link.device_id] = {"name": link.name, "connected": True, "since": link.connected_iso}
        return old

    def remember(self, device_id: str, name: str, last_seen: Optional[str]) -> None:
        """서버를 다시 켰을 때 전에 접속했던 장치를 '연결 끊김'으로 올려 둔다(기록 파일에서 읽음)."""
        if device_id not in self.links:
            self.known[device_id] = {"name": name, "connected": False, "since": last_seen}

    def forget(self, device_id: str) -> None:
        self.known.pop(device_id, None)

    def remove(self, link: DeviceLink) -> bool:
        """현재 연결이면 지우고 True. 이미 새 연결로 바뀌었으면 False."""
        if self.links.get(link.device_id) is not link:
            return False
        del self.links[link.device_id]
        self.known[link.device_id].update(connected=False, since=now_iso())
        return True

    def get(self, device_id: Optional[str]) -> Optional[DeviceLink]:
        return self.links.get(device_id) if device_id else None

    def connected(self) -> List[DeviceLink]:
        return list(self.links.values())
