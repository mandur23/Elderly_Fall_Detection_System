"""스피커 출력: 서버가 보낸 소리(play_begin -> AUDIO_OUT -> play_end)를 재생한다.

- 재생이 끝나면 on_done(id, stopped=False). 장치 출력 지연까지 기다린 뒤 알린다(안내 음성이 녹음되지 않게).
- loop=true면 play_stop까지 반복한다. 반복 중에 새 play_begin이 오면 반복을 끊고(stopped=True) 새 소리를 재생한다.
- play_stop이면 즉시 멈추고 받은 소리를 버린다. 재생 중이거나 기다리던 id마다 on_done(id, stopped=True).
- 여러 소리는 받은 순서대로 이어서 재생한다.

SoundDeviceSpeaker는 실제 장치, NullSpeaker는 소리를 내지 않고 실시간 속도로 재생한 것처럼 처리한다(시험용).
"""

import asyncio
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

from shared import protocol

log = logging.getLogger("edge.speaker")

RETRY_DELAYS = (1.0, 2.0, 5.0, 10.0)
STALL_SECONDS = 2.0
BYTES_PER_SECOND = protocol.AUDIO_RATE * protocol.AUDIO_SAMPLE_BYTES * protocol.AUDIO_CHANNELS
MAX_CLIP_BYTES = BYTES_PER_SECOND * 300   # 한 소리는 5분까지만 받는다(메모리 보호)

DoneCallback = Callable[[Any, bool], None]


@dataclass
class _Clip:
    id: Any
    loop: bool
    data: bytearray = field(default_factory=bytearray)
    ended: bool = False   # play_end를 받음
    pos: int = 0


class PlaybackBuffer:
    """재생 순서와 남은 소리를 관리한다. 오디오 스레드(read)와 이벤트 루프(나머지)가 함께 쓰므로 잠근다."""

    def __init__(self, max_clip_bytes: int = MAX_CLIP_BYTES):
        self._lock = threading.Lock()
        self._queue: List[_Clip] = []
        self._receiving: Optional[_Clip] = None
        self._max = max_clip_bytes

    def begin(self, clip_id: Any, loop: bool = False) -> List[Any]:
        """새 소리를 받기 시작한다. 반복 중이던 소리는 끊고 그 id 목록을 돌려준다(stopped로 알릴 것)."""
        with self._lock:
            cut = [c.id for c in self._queue if c.loop]
            self._queue = [c for c in self._queue if not c.loop and c.id != clip_id]
            clip = _Clip(clip_id, bool(loop))
            self._queue.append(clip)
            self._receiving = clip
            return cut

    def feed(self, pcm: bytes) -> bool:
        """받는 중인 소리에 PCM을 붙인다. 받는 중인 소리가 없으면 False."""
        with self._lock:
            clip = self._receiving
            if clip is None:
                return False
            if len(pcm) % 2:
                pcm = pcm[:-1]  # 16비트 샘플 경계를 지킨다
            room = self._max - len(clip.data)
            if room <= 0:
                return True
            clip.data += pcm[:room]
            return True

    def end(self, clip_id: Any) -> bool:
        with self._lock:
            clip = next((c for c in self._queue if c.id == clip_id), None)
            if clip is None:
                return False
            clip.ended = True
            if self._receiving is clip:
                self._receiving = None
            return True

    def stop(self) -> List[Any]:
        """모두 멈추고 버린다. 끝나지 않은 id 목록(재생 순서)을 돌려준다."""
        with self._lock:
            ids = [c.id for c in self._queue]
            self._queue.clear()
            self._receiving = None
            return ids

    @property
    def busy(self) -> bool:
        with self._lock:
            return bool(self._queue)

    def read(self, nbytes: int) -> Tuple[bytes, List[Tuple[Any, int]]]:
        """nbytes만큼 꺼낸다(모자라면 무음). 이번에 다 재생한 (id, 끝난 위치 바이트) 목록도 돌려준다."""
        out = bytearray()
        finished: List[Tuple[Any, int]] = []
        with self._lock:
            while len(out) < nbytes and self._queue:
                clip = self._queue[0]
                avail = len(clip.data) - clip.pos
                if avail > 0:
                    take = min(avail, nbytes - len(out))
                    out += clip.data[clip.pos:clip.pos + take]
                    clip.pos += take
                    continue
                if not clip.ended:
                    break  # 아직 받는 중: 무음으로 채우고 기다린다
                if clip.loop and clip.data:
                    clip.pos = 0
                    continue
                self._queue.pop(0)
                finished.append((clip.id, len(out)))
        if len(out) < nbytes:
            out += bytes(nbytes - len(out))
        return bytes(out), finished


class Speaker:
    """begin/feed/end/stop은 이벤트 루프 스레드에서 부른다. on_done도 이벤트 루프에서 불린다."""

    real = True

    def __init__(self, volume: float = 1.0):
        self.buffer = PlaybackBuffer()
        self.volume = volume
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._on_done: Optional[DoneCallback] = None
        self._pending: Dict[Any, asyncio.TimerHandle] = {}
        self._epoch = 0   # stop()마다 올린다. stop 직전에 끝난 재생의 알림이 늦게 와도 구분하려고
        self._stop_notifies = True

    def attach(self, loop: asyncio.AbstractEventLoop, on_done: Optional[DoneCallback]) -> None:
        self._loop, self._on_done = loop, on_done

    def begin(self, clip_id: Any, loop: bool = False) -> None:
        for cut in self.buffer.begin(clip_id, loop):
            log.info("반복 재생을 끊고 새 소리를 재생합니다 (%s -> %s)", cut, clip_id)
            self._notify(cut, True)

    def feed(self, pcm: bytes) -> None:
        if not self.buffer.feed(pcm):
            log.warning("play_begin 없이 온 소리를 버립니다 (%d바이트)", len(pcm))

    def end(self, clip_id: Any) -> None:
        if not self.buffer.end(clip_id):
            log.warning("모르는 재생 id의 play_end: %r", clip_id)

    def stop(self, notify: bool = True) -> List[Any]:
        """즉시 멈춘다. notify면 끝나지 않은 id마다 on_done(id, True). 멈춘 id 목록을 돌려준다."""
        self._epoch += 1
        self._stop_notifies = notify
        ids = list(self._pending) + self.buffer.stop()
        for handle in self._pending.values():
            handle.cancel()
        self._pending.clear()
        if notify:
            for clip_id in ids:
                self._notify(clip_id, True)
        return ids

    def _notify(self, clip_id: Any, stopped: bool) -> None:
        if self._on_done is not None:
            try:
                self._on_done(clip_id, stopped)
            except Exception:
                log.exception("재생 완료 처리 실패")

    def render(self, nbytes: int, latency: float) -> bytes:
        """오디오 스레드에서 부른다. latency는 이 블록의 첫 샘플이 실제로 들릴 때까지의 시간(초)."""
        epoch = self._epoch
        data, finished = self.buffer.read(nbytes)
        if finished and self._loop is not None:
            for clip_id, offset in finished:
                delay = max(0.0, latency + offset / BYTES_PER_SECOND)
                try:
                    self._loop.call_soon_threadsafe(self._schedule_done, clip_id, delay, epoch)
                except RuntimeError:
                    pass  # 이벤트 루프가 이미 끝남
        if self.volume < 1.0 and data:
            samples = np.frombuffer(data, dtype="<i2").astype(np.float32) * self.volume
            data = samples.astype("<i2").tobytes()
        return data

    def _schedule_done(self, clip_id: Any, delay: float, epoch: int) -> None:
        if epoch != self._epoch:
            # 끝난 직후 stop()으로 장치 출력까지 멈췄다. stop이 모르는 id이므로 여기서 중단으로 알린다.
            if self._stop_notifies:
                self._notify(clip_id, True)
            return
        self._pending[clip_id] = self._loop.call_later(delay, self._fire_done, clip_id)

    def _fire_done(self, clip_id: Any) -> None:
        self._pending.pop(clip_id, None)
        self._notify(clip_id, False)

    def start(self) -> None:
        pass

    def close(self) -> None:
        pass

    def status(self) -> Tuple[bool, str]:
        return True, ""


class NullSpeaker(Speaker):
    """소리를 내지 않고 실시간 속도로 소비한다. PC 시험, 자동 테스트용."""

    real = False
    BLOCK_SECONDS = 0.02

    def __init__(self, volume: float = 1.0):
        super().__init__(volume)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread is None:
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="null-speaker", daemon=True)
            self._thread.start()

    def _run(self) -> None:
        nbytes = int(BYTES_PER_SECOND * self.BLOCK_SECONDS)
        nbytes -= nbytes % 2
        t = time.monotonic()
        while not self._stop.is_set():
            self.render(nbytes, 0.0)
            t += self.BLOCK_SECONDS
            delay = t - time.monotonic()
            if delay > 0:
                self._stop.wait(delay)
            elif delay < -1.0:
                t = time.monotonic()

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(2.0)
            self._thread = None


class SoundDeviceSpeaker(Speaker):
    """sounddevice(PortAudio)로 재생한다. 스트림은 열어 두고 소리가 없을 때는 무음을 낸다.
    장치가 끊기면 1·2·5·10초 간격으로 다시 연다."""

    def __init__(self, device=None, volume: float = 1.0, retry_delays: Sequence[float] = RETRY_DELAYS):
        super().__init__(volume)
        self.device, self._retry_delays = device, tuple(retry_delays)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_callback = 0.0
        self._open = False
        self._error = "스피커를 여는 중입니다"
        self._latency = 0.1
        self._underflows = 0

    def status(self) -> Tuple[bool, str]:
        ok = self._open and time.monotonic() - self._last_callback < STALL_SECONDS
        return ok, "" if ok else (self._error or "스피커 출력이 멈췄습니다")

    def start(self) -> None:
        if self._thread is None:
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="speaker", daemon=True)
            self._thread.start()

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(3.0)
            self._thread = None

    def _callback(self, outdata, frames, time_info, status) -> None:
        self._last_callback = time.monotonic()
        if status:
            self._underflows += 1
            if self._underflows in (1, 10, 100) or self._underflows % 1000 == 0:
                log.warning("스피커 출력 상태: %s (%d회)", status, self._underflows)
        latency = time_info.outputBufferDacTime - time_info.currentTime
        if not (time_info.outputBufferDacTime > 0 and 0.0 <= latency < 2.0):
            latency = self._latency
        outdata[:] = self.render(len(outdata), latency)

    def _run(self) -> None:
        attempt = 0
        while not self._stop.is_set():
            try:
                import sounddevice as sd
                stream = sd.RawOutputStream(
                    samplerate=protocol.AUDIO_RATE, channels=protocol.AUDIO_CHANNELS, dtype="int16",
                    device=self.device, callback=self._callback)
                stream.start()
            except Exception as e:
                self._error = f"스피커를 열 수 없습니다: {e}"
                delay = self._retry_delays[min(attempt, len(self._retry_delays) - 1)]
                attempt += 1
                log.warning("%s (%.0f초 뒤 다시 시도)", self._error, delay)
                self._stop.wait(delay)
                continue
            self._latency = float(stream.latency or 0.1)
            log.info("스피커 열림: %s (지연 %.0fms)", stream.device, self._latency * 1000)
            self._last_callback = time.monotonic()
            self._open, self._error = True, ""
            try:
                while not self._stop.wait(0.5):
                    if not stream.active:
                        self._error = "스피커 출력이 중단되었습니다"
                        break
                    if time.monotonic() - self._last_callback > STALL_SECONDS:
                        self._error = "스피커 출력이 멈췄습니다"
                        break
                    attempt = 0
            finally:
                self._open = False
                try:
                    stream.abort()
                    stream.close()
                except Exception:
                    pass
            if not self._stop.is_set():
                delay = self._retry_delays[min(attempt, len(self._retry_delays) - 1)]
                attempt += 1
                log.warning("%s (%.0f초 뒤 다시 엽니다)", self._error, delay)
                self._stop.wait(delay)
