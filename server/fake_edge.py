"""라즈베리파이 없이 서버를 시험하는 가짜 edge.

영상 파일을 edge처럼 실시간으로 보내고, 안내 음성은 재생한 척(길이만큼 기다림) playback_done을 보내고,
마이크가 켜지면 지정한 wav 파일(대답)을 0.1초씩 실시간으로 보낸다. 소리는 스피커로 내지 않고 저장하지도 않는다.

    python -m server.fake_edge --token <DEVICE_TOKEN> --video fall.avi --hold 3 --reply safe1.wav
    python -m server.fake_edge --token <DEVICE_TOKEN> --video fall.avi --hold 3            # 대답 없음

--reply를 여러 번 주면 음성 확인 차례마다 하나씩 쓴다. 'none'은 그 차례에 대답하지 않음(작은 잡음만).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional

import aiohttp
import numpy as np

from server.speech import BYTES_PER_SECOND, wav_to_pcm
from shared import protocol as P

log = logging.getLogger("fake_edge")


def noise_chunk(level: int = 30) -> bytes:
    """조용한 방의 작은 잡음. 완전한 0은 서버가 마이크 고장으로 본다."""
    return np.random.randint(-level, level + 1, P.AUDIO_CHUNK_BYTES // 2, dtype="<i2").tobytes()


class FakeEdge:
    def __init__(self, url: str, token: str, device_id: str, video: Optional[Path], hold: float, loop_video: bool,
                 replies: List[Optional[bytes]], speaker_ok: bool = True, mic_ok: bool = True,
                 no_playback_done: bool = False):
        self.url, self.token, self.device_id = url, token, device_id
        self.video, self.hold, self.loop_video = video, hold, loop_video
        self.replies = replies
        self.speaker_ok, self.mic_ok = speaker_ok, mic_ok
        self.no_playback_done = no_playback_done
        self.ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self.video_cfg = dict(P.DEFAULT_VIDEO)
        self._playing: Dict[str, Dict] = {}  # id -> {"pcm": bytearray, "loop": bool, "task": Task}
        self._mic_task: Optional[asyncio.Task] = None
        self._mic_count = 0
        self.started = time.monotonic()
        self.video_done = asyncio.Event()

    async def send(self, type_: str, **fields) -> None:
        await self.ws.send_str(P.encode(type_, **fields))

    async def run(self) -> None:
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(self.url, headers=P.bearer(self.token), heartbeat=P.HEARTBEAT_SECONDS) as ws:
                self.ws = ws
                await self.send(P.HELLO, protocol=P.PROTOCOL_VERSION, device_id=self.device_id, name="가짜 edge",
                                software="fake_edge",
                                video={"width": 640, "height": 480, "fps": 10, "format": "jpeg"},
                                audio={"rate": P.AUDIO_RATE, "channels": 1, "format": "s16le"})
                welcome = P.decode((await ws.receive(timeout=10)).data)
                log.info("welcome: %s", welcome)
                self.video_cfg.update(welcome.get("video", {}))
                tasks = [asyncio.create_task(self._status_loop()), asyncio.create_task(self._video_loop())]
                try:
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            await self._on_text(P.decode(msg.data))
                        elif msg.type == aiohttp.WSMsgType.BINARY:
                            kind, _, payload = P.unpack(msg.data)
                            if kind == P.AUDIO_OUT:
                                for item in self._playing.values():
                                    if item["receiving"]:
                                        item["pcm"] += payload
                finally:
                    for t in tasks:
                        t.cancel()
                log.info("연결 종료 (code=%s)", ws.close_code)

    async def _on_text(self, msg: Dict) -> None:
        kind = msg["type"]
        if kind == P.PLAY_BEGIN:
            self._playing[msg["id"]] = {"pcm": bytearray(), "loop": bool(msg.get("loop")), "receiving": True}
            log.info("재생 시작 %s (loop=%s)", msg["id"], msg.get("loop"))
        elif kind == P.PLAY_END:
            item = self._playing.get(msg["id"])
            if item is not None:
                item["receiving"] = False
                seconds = len(item["pcm"]) / BYTES_PER_SECOND
                log.info("재생할 소리 %.1f초 받음 (%s)", seconds, msg["id"])
                if not item["loop"] and not self.no_playback_done:
                    item["task"] = asyncio.create_task(self._finish_playback(msg["id"], seconds))
        elif kind == P.PLAY_STOP:
            log.info("재생 중단 요청")
            for pid, item in list(self._playing.items()):
                if item.get("task"):
                    item["task"].cancel()
                self._playing.pop(pid, None)
                await self.send(P.PLAYBACK_DONE, id=pid, stopped=True)
        elif kind == P.MIC:
            log.info("마이크 %s", "켜기" if msg["enabled"] else "끄기")
            if msg["enabled"]:
                reply = self.replies[self._mic_count] if self._mic_count < len(self.replies) else None
                self._mic_count += 1
                if self._mic_task is None or self._mic_task.done():
                    self._mic_task = asyncio.create_task(self._mic_loop(reply))
            elif self._mic_task is not None:
                self._mic_task.cancel()
        else:
            log.info("서버 메시지: %s", json.dumps(msg, ensure_ascii=False))

    async def _finish_playback(self, pid: str, seconds: float) -> None:
        await asyncio.sleep(seconds)  # 재생하는 척
        self._playing.pop(pid, None)
        await self.send(P.PLAYBACK_DONE, id=pid)
        log.info("재생 완료 %s", pid)

    async def _mic_loop(self, reply: Optional[bytes]) -> None:
        pcm = bytearray(reply or b"")
        next_at = time.monotonic()
        while True:
            if pcm:
                chunk = bytes(pcm[:P.AUDIO_CHUNK_BYTES]).ljust(P.AUDIO_CHUNK_BYTES, b"\x00")
                del pcm[:P.AUDIO_CHUNK_BYTES]
            else:
                chunk = noise_chunk()
            await self.ws.send_bytes(P.pack(P.AUDIO_IN, time.monotonic(), chunk))
            next_at += P.AUDIO_CHUNK_SECONDS
            await asyncio.sleep(max(0.0, next_at - time.monotonic()))

    async def _status_loop(self) -> None:
        while True:
            await self.send(P.STATUS, camera_ok=True, mic_ok=self.mic_ok, speaker_ok=self.speaker_ok,
                            mic_error=None if self.mic_ok else "가짜 마이크 오류",
                            speaker_error=None if self.speaker_ok else "가짜 스피커 오류",
                            fps=float(self.video_cfg["fps"]), uptime_s=round(time.monotonic() - self.started))
            await asyncio.sleep(P.STATUS_INTERVAL_SECONDS)

    def _frames(self):
        import cv2
        w, h, q = self.video_cfg["width"], self.video_cfg["height"], self.video_cfg["jpeg_quality"]
        fps = self.video_cfg["fps"]
        if self.video is None:
            frame = np.zeros((h, w, 3), np.uint8)
            while True:
                yield cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, q])[1].tobytes()
        while True:
            cap = cv2.VideoCapture(str(self.video))
            if not cap.isOpened():
                raise SystemExit(f"영상을 열 수 없습니다: {self.video}")
            step = max(1.0, (cap.get(cv2.CAP_PROP_FPS) or fps) / fps)
            index, next_pick, last = 0, 0.0, None
            while True:
                ok, frame = cap.read()
                if not ok:
                    break
                if index >= next_pick:
                    next_pick += step
                    last = cv2.imencode(".jpg", cv2.resize(frame, (w, h)), [cv2.IMWRITE_JPEG_QUALITY, q])[1].tobytes()
                    yield last
                index += 1
            cap.release()
            if not self.loop_video:
                break
        if last is not None:
            for _ in range(int(round(self.hold * fps))):  # 넘어진 사람이 계속 누워 있다고 보고 마지막 장면 유지
                yield last

    async def _video_loop(self) -> None:
        interval = 1.0 / self.video_cfg["fps"]
        next_at = time.monotonic()
        for jpeg in self._frames():
            await self.ws.send_bytes(P.pack(P.VIDEO, time.monotonic(), jpeg))
            next_at += interval
            await asyncio.sleep(max(0.0, next_at - time.monotonic()))
        log.info("영상 끝 (상태 보고는 계속)")
        self.video_done.set()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m server.fake_edge", description="가짜 edge (서버 시험용)")
    parser.add_argument("--url", default=f"ws://127.0.0.1:{P.DEFAULT_PORT}{P.WS_PATH}")
    parser.add_argument("--token", required=True)
    parser.add_argument("--device-id", default="fake-edge")
    parser.add_argument("--video", type=Path, help="영상 파일. 없으면 검은 화면")
    parser.add_argument("--hold", type=float, default=0.0, help="영상이 끝난 뒤 마지막 장면을 몇 초 더 보낼지")
    parser.add_argument("--loop", action="store_true", help="영상 반복")
    parser.add_argument("--reply", action="append", default=[],
                        help="마이크가 켜지면 보낼 대답 wav (차례마다 하나, 'none'은 대답 없음)")
    parser.add_argument("--speaker-fail", action="store_true", help="스피커 오류로 보고")
    parser.add_argument("--mic-fail", action="store_true", help="마이크 오류로 보고")
    parser.add_argument("--no-playback-done", action="store_true", help="재생 완료를 보내지 않음(스피커 고장 흉내)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s][fake_edge] %(message)s")
    replies = [None if r.lower() == "none" else wav_to_pcm(Path(r).read_bytes()) for r in args.reply]
    edge = FakeEdge(args.url, args.token, args.device_id, args.video, args.hold, args.loop, replies,
                    speaker_ok=not args.speaker_fail, mic_ok=not args.mic_fail,
                    no_playback_done=args.no_playback_done)
    try:
        asyncio.run(edge.run())
    except KeyboardInterrupt:
        pass
    except aiohttp.WSServerHandshakeError as e:
        print(f"접속 거절: HTTP {e.status} (토큰 확인)", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
