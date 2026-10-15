"""마이크 입력: 16kHz mono s16le를 0.1초(3200바이트) 단위로 잘라 첫 샘플의 녹음 시각을 붙인다.

장치는 계속 열어 두되(고장을 바로 알 수 있게), active가 True일 때만 sink로 넘긴다.
서버가 mic enabled=true를 보냈을 때만 active가 된다. 소리는 파일로 저장하지 않는다.
"""

import logging
import threading
import time
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Tuple

from shared import protocol

log = logging.getLogger("edge.mic")

RETRY_DELAYS = (1.0, 2.0, 5.0, 10.0)
STALL_SECONDS = 2.0   # 이 시간 동안 녹음 콜백이 없으면 장치 문제로 보고 다시 연다
BYTES_PER_SECOND = protocol.AUDIO_RATE * protocol.AUDIO_SAMPLE_BYTES * protocol.AUDIO_CHANNELS
SOUND_CARDS_PATH = Path("/proc/asound/cards")

Sink = Callable[[bytes, float], None]


def sound_cards() -> Optional[str]:
    """리눅스(ALSA)가 지금 아는 사운드 카드 목록. 읽을 수 없으면(리눅스가 아님) None.

    PortAudio(sounddevice)는 처음 불러올 때 한 번만 장치를 찾는다. 그 뒤 이 목록이 바뀌었으면(USB 장치를
    뺐다 꽂음) 프로그램을 다시 시작해야 새 장치를 쓸 수 있다. 마이크와 스피커가 함께 쓴다.
    """
    try:
        return SOUND_CARDS_PATH.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


class ChunkAssembler:
    """크기가 제각각인 PCM 조각을 chunk_bytes 단위로 자르고, 각 단위의 첫 샘플 시각을 계산한다."""

    GAP_TOLERANCE = 0.05  # 들어온 시각이 예상과 이만큼 넘게 다르면(녹음이 끊김) 남은 조각을 버리고 새로 시작

    def __init__(self, chunk_bytes: int = protocol.AUDIO_CHUNK_BYTES, bytes_per_second: int = BYTES_PER_SECOND):
        self.chunk_bytes, self.bytes_per_second = chunk_bytes, bytes_per_second
        self._buf = bytearray()
        self._t0 = 0.0  # _buf 첫 바이트의 녹음 시각

    def reset(self) -> None:
        self._buf.clear()

    def push(self, data: bytes, t_first: float) -> List[Tuple[bytes, float]]:
        """data의 첫 샘플 녹음 시각이 t_first. 완성된 (chunk, 첫 샘플 시각) 목록을 돌려준다."""
        if self._buf:
            expected = self._t0 + len(self._buf) / self.bytes_per_second
            if abs(t_first - expected) > self.GAP_TOLERANCE:
                self._buf.clear()
        if not self._buf:
            self._t0 = t_first
        self._buf += data
        out = []
        while len(self._buf) >= self.chunk_bytes:
            out.append((bytes(self._buf[:self.chunk_bytes]), self._t0))
            del self._buf[:self.chunk_bytes]
            self._t0 += self.chunk_bytes / self.bytes_per_second
        return out


class Microphone:
    """active일 때만 sink(pcm, 첫 샘플 시각)를 부른다(오디오 스레드에서)."""

    def __init__(self):
        self._sink: Optional[Sink] = None
        self.active = False

    def set_sink(self, sink: Optional[Sink]) -> None:
        self._sink = sink

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def status(self) -> Tuple[bool, str]:
        raise NotImplementedError

    def failing_for(self) -> float:
        """장치를 쓰지 못한 채 이어진 시간(초). 0이면 정상이거나 해당 없음."""
        return 0.0

    def devices_changed(self) -> bool:
        """장치 목록을 만든 뒤 사운드 카드가 바뀌었는지(다시 시작해야 새 장치를 찾는지)."""
        return False


class DisabledMicrophone(Microphone):
    def status(self) -> Tuple[bool, str]:
        return False, "마이크를 쓰지 않도록 설정되어 있습니다"


class SoundDeviceMicrophone(Microphone):
    """sounddevice(PortAudio)로 녹음한다. 장치가 끊기면 1·2·5·10초 간격으로 다시 연다."""

    BLOCK_FRAMES = protocol.AUDIO_CHUNK_BYTES // protocol.AUDIO_SAMPLE_BYTES  # 0.1초

    def __init__(self, device=None, retry_delays: Sequence[float] = RETRY_DELAYS,
                 cards: Callable[[], Optional[str]] = sound_cards):
        super().__init__()
        self.device, self._retry_delays, self._cards = device, tuple(retry_delays), cards
        self._asm = ChunkAssembler()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_callback = 0.0
        self._open = False
        self._error = "마이크를 여는 중입니다"
        self._overflows = 0
        self._fail_since: Optional[float] = None   # 장치를 쓰지 못하기 시작한 시각
        self._cards_at_start: Optional[str] = None  # PortAudio가 장치를 찾을 때의 사운드 카드 목록

    def status(self) -> Tuple[bool, str]:
        ok = self._open and time.monotonic() - self._last_callback < STALL_SECONDS
        return ok, "" if ok else (self._error or "마이크 입력이 멈췄습니다")

    def failing_for(self) -> float:
        since = self._fail_since
        return 0.0 if since is None else time.monotonic() - since

    def devices_changed(self) -> bool:
        before = self._cards_at_start
        now = self._cards() if before is not None else None
        return now is not None and now != before

    def _failing(self) -> None:
        if self._fail_since is None:
            self._fail_since = time.monotonic()

    def start(self) -> None:
        if self._thread is None:
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="mic", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(3.0)
            self._thread = None

    def _callback(self, indata, frames, time_info, status) -> None:
        now = time.monotonic()
        self._last_callback = now
        if status:
            self._overflows += 1
            if self._overflows in (1, 10, 100) or self._overflows % 1000 == 0:
                log.warning("마이크 입력 상태: %s (%d회)", status, self._overflows)
        sink = self._sink
        if not self.active or sink is None:
            self._asm.reset()
            return
        # 첫 샘플이 녹음된 시각. 장치가 시각을 알려 주지 않으면 블록 길이만큼 앞으로 잡는다.
        lag = time_info.currentTime - time_info.inputBufferAdcTime
        if not (time_info.inputBufferAdcTime > 0 and 0.0 <= lag < 1.0):
            lag = frames / protocol.AUDIO_RATE
        for chunk, ts in self._asm.push(bytes(indata), now - lag):
            sink(chunk, ts)

    def _run(self) -> None:
        attempt = 0
        # sounddevice는 처음 불러올 때(바로 아래) 장치를 찾는다. 그때의 사운드 카드 목록을 기억해 둔다.
        self._cards_at_start = self._cards()
        while not self._stop.is_set():
            stream = None
            try:
                import sounddevice as sd
                stream = sd.RawInputStream(
                    samplerate=protocol.AUDIO_RATE, channels=protocol.AUDIO_CHANNELS, dtype="int16",
                    blocksize=self.BLOCK_FRAMES, device=self.device, callback=self._callback)
                stream.start()
            except Exception as e:
                _close_quietly(stream)
                self._error = f"마이크를 열 수 없습니다: {e}"
                self._failing()
                delay = self._retry_delays[min(attempt, len(self._retry_delays) - 1)]
                attempt += 1
                log.warning("%s (%.0f초 뒤 다시 시도)", self._error, delay)
                self._stop.wait(delay)
                continue
            log.info("마이크 열림: %s", _device_name(sd, stream.device))
            opened = self._last_callback = time.monotonic()
            self._open, self._error = True, ""
            try:
                while not self._stop.wait(0.5):
                    if not stream.active:
                        self._error = "마이크 입력이 중단되었습니다"
                        break
                    if time.monotonic() - self._last_callback > STALL_SECONDS:
                        self._error = "마이크 입력이 멈췄습니다"
                        break
                    attempt = 0
                    if self._last_callback > opened:
                        self._fail_since = None   # 소리가 실제로 들어온다
            finally:
                self._open = False
                _close_quietly(stream)
            if not self._stop.is_set():
                self._failing()
                delay = self._retry_delays[min(attempt, len(self._retry_delays) - 1)]
                attempt += 1
                log.warning("%s (%.0f초 뒤 다시 엽니다)", self._error, delay)
                self._stop.wait(delay)


def _device_name(sd, device) -> str:
    try:
        return f"{device} {sd.query_devices(device)['name']}"
    except Exception:
        return str(device)


def _close_quietly(stream) -> None:
    """열다 실패했거나 멈춘 스트림을 닫는다(PortAudio 자원이 남지 않게)."""
    if stream is None:
        return
    try:
        stream.abort()
        stream.close()
    except Exception:
        pass
