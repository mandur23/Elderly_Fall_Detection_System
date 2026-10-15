"""영상 처리: JPEG 디코딩 -> 자세 추정 -> 특징 -> 낙상 판정.

장치마다 VideoPipeline 하나가 작업 스레드 하나로 장면을 차례로 처리한다. 처리가 밀리면 쌓아 두지 않고
가장 최근 장면만 처리한다. 판정 시간은 edge가 붙인 촬영 시각을 쓴다(네트워크 지연이 하강 속도를 흔들지 않게).
장면은 메모리에서만 처리하고 저장하지 않는다.
"""

from __future__ import annotations

import asyncio
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Awaitable, Callable, Dict, Optional

import numpy as np

from server.fall import FallFSM, FallParams, FallState
from server.features import FeatureExtractor
from server.pose import PoseEstimator, PoseError, PoseFactory

log = logging.getLogger("server.vision")

SuspectCallback = Callable[[str, Optional[Dict[str, float]]], Awaitable[None]]


def decode_jpeg(data: bytes) -> Optional[np.ndarray]:
    """JPEG -> RGB 배열. 깨진 장면이면 None."""
    import cv2
    bgr = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
    if bgr is None:
        return None
    return cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)


@dataclass
class FrameResult:
    t: float
    person: bool
    features: Optional[Dict[str, float]]
    state: FallState
    became_suspect: bool


class FrameAnalyzer:
    """장면 한 장을 자세 추정 -> 특징 -> 상태 머신까지 처리한다(동기, 작업 스레드에서 부른다)."""

    def __init__(self, estimator: PoseEstimator, params: FallParams, velocity_window: float):
        self.estimator = estimator
        self.extractor = FeatureExtractor(velocity_window)
        self.fsm = FallFSM(params)
        self.last_t: Optional[float] = None
        self.last_features: Optional[Dict[str, float]] = None

    def process(self, jpeg: bytes, t: float) -> Optional[FrameResult]:
        rgb = decode_jpeg(jpeg)
        if rgb is None:
            return None
        kp = self.estimator.detect(rgb, t)
        del rgb
        self.last_t = t
        features = self.extractor.extract(kp, t) if kp is not None else None
        if features is not None:
            self.last_features = features
        before = self.fsm.state
        state = self.fsm.update(features, t)
        return FrameResult(t, kp is not None, features, state,
                           before != FallState.SUSPECT and state == FallState.SUSPECT)

    def reset_detection(self) -> None:
        """이벤트가 끝나 감시를 다시 시작한다."""
        self.fsm.reset()
        self.extractor.reset()
        self.last_features = None

    def reset_session(self) -> None:
        """다시 접속: edge의 시계가 달라질 수 있으므로 판정 기록과 추적 상태를 모두 새로 시작한다."""
        self.reset_detection()
        self.estimator.reset()
        self.last_t = None


class VideoPipeline:
    def __init__(self, device_id: str, factory: PoseFactory, params: FallParams, velocity_window: float,
                 on_suspect: SuspectCallback):
        self.device_id = device_id
        self.factory = factory
        self.params = params
        self.velocity_window = velocity_window
        self.on_suspect = on_suspect
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"pose-{device_id}")
        self._analyzer: Optional[FrameAnalyzer] = None
        self._latest: Optional[tuple] = None
        self._wake = asyncio.Event()
        self._reset_session = True
        self._reset_detection = False
        self._task: Optional[asyncio.Task] = None
        self.error: Optional[str] = None
        # 상태 표시용
        self.received = 0
        self.processed = 0
        self.dropped = 0
        self.decode_errors = 0
        self.last_frame_at: Optional[float] = None  # 서버 시계(time.monotonic)로 마지막으로 영상을 받은 시각
        self.person: Optional[bool] = None
        self.features: Optional[Dict[str, float]] = None
        self.state = FallState.NORMAL
        self.fps = 0.0
        self._last_done: Optional[float] = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name=f"video-{self.device_id}")

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(self._executor, self._close_analyzer)
        self._executor.shutdown(wait=False)

    def _close_analyzer(self) -> None:
        if self._analyzer is not None:
            self._analyzer.estimator.close()
            self._analyzer = None

    # --- 입력 (이벤트 루프에서 부른다) ---
    def submit(self, timestamp: float, jpeg: bytes) -> None:
        self.received += 1
        self.last_frame_at = time.monotonic()
        if self._latest is not None:
            self.dropped += 1  # 아직 처리하지 못한 장면은 버리고 최신 장면만 남긴다
        self._latest = (timestamp, jpeg)
        self._wake.set()

    def new_session(self) -> None:
        self._latest = None
        self._reset_session = True
        self.last_frame_at = None
        self.person = None
        self.features = None
        self.state = FallState.NORMAL
        self.fps = 0.0
        self._last_done = None

    def reset_detection(self) -> None:
        self._reset_detection = True
        self.state = FallState.NORMAL

    # --- 처리 ---
    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            await self._wake.wait()
            self._wake.clear()
            item, self._latest = self._latest, None
            if item is None:
                continue
            reset_session, self._reset_session = self._reset_session, False
            reset_detection, self._reset_detection = self._reset_detection, False
            try:
                result = await loop.run_in_executor(self._executor, self._process, *item,
                                                    reset_session, reset_detection)
                self.error = None
            except PoseError as e:
                if self.error != str(e):
                    log.error("[%s] 자세 추정 실패: %s", self.device_id, e)
                self.error = str(e)
                continue
            except Exception:
                log.exception("[%s] 장면 처리 중 오류", self.device_id)
                continue
            if result is None:
                continue
            self._record(result)
            if result.became_suspect:
                log.warning("[%s] 낙상 의심 (t=%.2f) %s", self.device_id, result.t, result.features)
                await self.on_suspect(self.device_id, result.features or self._analyzer_last_features())

    def _analyzer_last_features(self) -> Optional[Dict[str, float]]:
        return self._analyzer.last_features if self._analyzer is not None else None

    def _process(self, timestamp: float, jpeg: bytes, reset_session: bool,
                 reset_detection: bool) -> Optional[FrameResult]:
        if self._analyzer is None:
            self._analyzer = FrameAnalyzer(self.factory.create(), self.params, self.velocity_window)
        elif reset_session:
            self._analyzer.reset_session()
        if reset_detection:
            self._analyzer.reset_detection()
        if self._analyzer.last_t is not None and timestamp <= self._analyzer.last_t:
            return None  # 시각이 거꾸로 가는 장면은 버린다
        result = self._analyzer.process(jpeg, timestamp)
        if result is None:
            self.decode_errors += 1
        return result

    def _record(self, result: FrameResult) -> None:
        self.processed += 1
        now = time.monotonic()
        if self._last_done is not None and now > self._last_done:
            inst = 1.0 / (now - self._last_done)
            self.fps = 0.9 * self.fps + 0.1 * inst if self.fps else inst
        self._last_done = now
        self.person = result.person
        self.features = result.features
        self.state = result.state

    def snapshot(self) -> Dict:
        return {
            "detect_state": self.state.value,
            "person_detected": self.person,
            "features": self.features,
            "processed_fps": round(self.fps, 1),
            "frames_received": self.received,
            "frames_processed": self.processed,
            "frames_dropped": self.dropped,
        }
