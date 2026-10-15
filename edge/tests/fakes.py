"""테스트용 가짜 카메라·마이크."""

import threading
import time
from typing import Optional, Tuple

import numpy as np

from edge.camera import FrameSource
from edge.mic import Microphone
from shared import protocol


class FakeLiveSource(FrameSource):
    """요청 크기와 상관없이 640x480 장면을 30fps로 내는 웹캠 흉내. fail_after장 뒤에는 장면을 내지 않는다."""
    name = "fake"

    def __init__(self, fps: float = 30.0, size: Tuple[int, int] = (640, 480), fail_after: Optional[int] = None):
        self.fps, self.size, self.fail_after = fps, size, fail_after
        self.count = 0
        self.closed = False

    def open(self) -> None:
        self.closed = False

    def read(self):
        time.sleep(1.0 / self.fps)
        if self.fail_after is not None and self.count >= self.fail_after:
            return None
        self.count += 1
        w, h = self.size
        frame = np.zeros((h, w, 3), np.uint8)
        frame[:, : (self.count * 8) % w] = 200  # 장면마다 조금씩 다르게
        return frame

    def close(self) -> None:
        self.closed = True


class FakeMicrophone(Microphone):
    """0.1초마다 3200바이트 조각을 내는 마이크 흉내. 내용은 조각 번호."""

    def __init__(self):
        super().__init__()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.produced = 0

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        period = protocol.AUDIO_CHUNK_SECONDS
        t = time.monotonic()
        while not self._stop.wait(max(0.0, t + period - time.monotonic())):
            t += period
            sink = self._sink
            if self.active and sink is not None:
                self.produced += 1
                sink(bytes([self.produced % 256]) * protocol.AUDIO_CHUNK_BYTES, t - period)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(2.0)

    def status(self):
        return True, ""
