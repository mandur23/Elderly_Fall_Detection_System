"""영상 처리: JPEG 디코딩 -> 자세 추정 -> 특징 -> 낙상 판정.

장치마다 VideoPipeline 하나가 작업 스레드 하나로 장면을 차례로 처리한다. 처리가 밀리면 쌓아 두지 않고
가장 최근 장면만 처리한다. 판정 시간은 edge가 붙인 촬영 시각을 쓴다(네트워크 지연이 하강 속도를 흔들지 않게).
장면은 메모리에서만 처리하고 저장하지 않는다.

장면 처리 시간의 대부분은 자세 추정이다. 판정에 필요 없는 장면은 건너뛴다(FrameAnalyzer._skip).
처리가 멈추거나(오류·과부하), 깨진 장면만 오거나, 너무 느려 하강 속도를 제대로 잴 수 없으면 problems()로
장치 문제를 알린다. 영상은 들어오는데 감지가 조용히 멈춘 상태로 남지 않게 하기 위함이다.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Awaitable, Callable, Dict, List, Optional

import numpy as np

from server.fall import FallFSM, FallParams, FallState
from server.features import FeatureExtractor
from server.pose import PoseEstimator, PoseError, PoseFactory

log = logging.getLogger("server.vision")

SuspectCallback = Callable[[str, Optional[Dict[str, float]]], Awaitable[None]]

IDLE_AFTER_SECONDS = 2.0      # 사람이 이만큼 안 보이면(빈 방)
IDLE_INTERVAL_SECONDS = 0.2   # 이 간격으로만 사람을 찾는다 (초당 5장)
STALL_SECONDS = 5.0           # 영상이 들어오는데 이만큼 처리하지 못하면 '분석 멈춤'
DECODE_FAIL_FRAMES = 20       # 연속으로 이만큼 깨진 장면이 오면 문제로 표시
MIN_PROCESSED_FPS = 5.0       # 이보다 느리면 0.3초 하강 속도 구간이 너무 성겨 낙상을 놓친다
FPS_WINDOW_SECONDS = 5.0      # 처리 속도를 재는 구간
SESSION_GAP_SECONDS = 10.0    # 다시 접속했을 때 이보다 오래 끊겼으면 판정을 새로 시작
SUSPECT_RETRY_SECONDS = 1.0   # 낙상 의심을 이벤트로 넘기지 못했을 때 다시 시도하는 간격

_cv2 = None


def _opencv():
    """OpenCV를 처음 쓸 때 한 번 불러온다. 장치마다 작업 스레드가 따로 있으므로 OpenCV 자체 스레드는 끈다
    (장치가 여러 대일 때 코어를 서로 빼앗지 않게)."""
    global _cv2
    if _cv2 is None:
        import cv2
        cv2.setNumThreads(1)
        _cv2 = cv2
    return _cv2


def decode_jpeg(data: bytes) -> Optional[np.ndarray]:
    """JPEG -> RGB 배열. 깨진 장면이면 None."""
    cv2 = _opencv()
    buf = np.frombuffer(data, dtype=np.uint8)
    if hasattr(cv2, "IMREAD_COLOR_RGB"):  # OpenCV 4.10+: 디코딩하면서 바로 RGB (색 변환 복사 없음)
        return cv2.imdecode(buf, cv2.IMREAD_COLOR_RGB)
    bgr = cv2.imdecode(buf, cv2.IMREAD_COLOR)
    if bgr is None:
        return None
    return cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB, dst=bgr)


@dataclass
class FrameResult:
    t: float
    person: Optional[bool]               # None: 자세 추정을 건너뛴 장면
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
        self.analyzed_at: Optional[float] = None  # 마지막으로 자세 추정을 한 장면의 시각
        self.empty_since: Optional[float] = None  # 사람이 계속 안 보이기 시작한 시각

    def _skip(self, t: float) -> bool:
        """자세 추정을 건너뛸 장면인지.

        - 낙상 의심(SUSPECT): 이벤트가 끝나(reset) 다시 감시할 때까지 판정이 바뀌지 않는다. 그동안 음성 확인과
          다른 장치가 CPU를 쓰게 한다.
        - 빈 방: 사람이 없는 장면은 추적 대신 매번 사람 검출을 돌려 사람이 있을 때보다 오히려 느리다(약 24ms 대
          17ms). 정상 상태에서 IDLE_AFTER초 넘게 아무도 안 보이면 IDLE_INTERVAL초마다만 찾고, 보이면 바로 매 장면.
          낙상은 보이던 사람이 내려가는 것으로 잡으므로 빈 방에서 놓칠 것이 없다.
        """
        state = self.fsm.state
        if state == FallState.SUSPECT:
            return True
        return (state == FallState.NORMAL and self.empty_since is not None
                and t - self.empty_since >= IDLE_AFTER_SECONDS
                and t - self.analyzed_at < IDLE_INTERVAL_SECONDS - 1e-6)

    def process(self, jpeg: bytes, t: float) -> Optional[FrameResult]:
        """깨진 장면이면 None."""
        if self._skip(t):
            self.last_t = t
            return FrameResult(t, None, None, self.fsm.state, False)
        rgb = decode_jpeg(jpeg)
        if rgb is None:
            return None
        kp = self.estimator.detect(rgb, t)
        del rgb
        self.last_t = self.analyzed_at = t
        features = None
        if kp is None:
            if self.empty_since is None:
                self.empty_since = t
        else:
            self.empty_since = None
            features = self.last_features = self.extractor.extract(kp, t)
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
        """edge 시계가 바뀌었거나(재부팅) 오래 끊겼다: 판정 기록과 추적 상태를 모두 새로 시작한다."""
        self.reset_detection()
        self.last_t = self.analyzed_at = self.empty_since = None
        self.estimator.reset()


class RateMeter:
    """최근 FPS_WINDOW_SECONDS 동안 초당 횟수. 멈추면 0으로 내려간다."""

    def __init__(self, since: float):
        self.since = since
        self._times: deque = deque()

    def tick(self, now: float) -> None:
        self._times.append(now)

    def reset(self, now: float) -> None:
        self.since = now
        self._times.clear()

    def rate(self, now: float) -> float:
        while self._times and now - self._times[0] > FPS_WINDOW_SECONDS:
            self._times.popleft()
        span = min(FPS_WINDOW_SECONDS, now - self.since)
        return len(self._times) / span if span >= 1.0 else 0.0


class VideoPipeline:
    def __init__(self, device_id: str, factory: PoseFactory, params: FallParams, velocity_window: float,
                 on_suspect: SuspectCallback, expected_fps: float = 10.0):
        self.device_id = device_id
        self.factory = factory
        self.params = params
        self.velocity_window = velocity_window
        self.on_suspect = on_suspect
        # 서버 설정 fps가 낮으면 그 절반 남짓까지는 정상으로 본다.
        self.min_fps = min(MIN_PROCESSED_FPS, 0.6 * expected_fps)
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"pose-{device_id}")
        self._analyzer: Optional[FrameAnalyzer] = None
        self._latest: Optional[tuple] = None
        self._wake = asyncio.Event()
        self._new_session = True
        self._reset_detection = False
        self._task: Optional[asyncio.Task] = None
        self._suspect_pending = False  # 낙상 의심을 이벤트로 아직 넘기지 못함(오류 뒤 다시 시도)
        self._suspect_features: Optional[Dict[str, float]] = None
        self._suspect_retry_at = 0.0
        self.error: Optional[str] = None         # 장면 처리 오류 (다음 장면이 정상이면 지운다)
        self.report_error: Optional[str] = None  # 낙상 의심을 이벤트로 넘기지 못함
        # 상태 표시용
        self.received = self.processed = self.analyzed = self.dropped = 0
        self.decode_errors = self.decode_fail_run = 0
        self.last_frame_at: Optional[float] = None      # 서버 시계(time.monotonic)로 마지막으로 영상을 받은 시각
        self.last_processed_at: Optional[float] = None
        self.session_started_at = time.monotonic()
        self.received_rate = RateMeter(self.session_started_at)
        self.processed_rate = RateMeter(self.session_started_at)
        self.person: Optional[bool] = None
        self.features: Optional[Dict[str, float]] = None
        self.state = FallState.NORMAL

    def start(self) -> None:
        """작업을 시작한다. 이전 작업이 오류로 끝났으면 다시 시작한다."""
        if self._task is not None and self._task.done():
            if not self._task.cancelled() and self._task.exception() is not None:
                log.error("[%s] 영상 처리 작업이 멈춰 있어 다시 시작합니다", self.device_id,
                          exc_info=self._task.exception())
            self._task = None
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name=f"video-{self.device_id}")

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            except Exception:
                log.exception("[%s] 영상 처리 작업 종료 중 오류", self.device_id)
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
        self.received_rate.tick(self.last_frame_at)
        if self._latest is not None:
            self.dropped += 1  # 아직 처리하지 못한 장면은 버리고 최신 장면만 남긴다
        self._latest = (timestamp, jpeg)
        self._wake.set()

    def new_session(self) -> None:
        """장치가 다시 접속했다. 시계가 바뀌었는지는 첫 장면의 시각을 보고 판단한다(_process).

        잠깐 끊겼다 붙은 경우(와이파이·edge 재시작)는 넘어지는 중이던 판정을 이어 간다. edge 시각은
        시스템 전체의 time.monotonic()이라 라즈베리파이가 재부팅할 때만 거꾸로 간다.
        """
        self._latest = None
        self._new_session = True
        self.last_frame_at = None
        self.person = None
        self.features = None
        self.decode_fail_run = 0
        self.session_started_at = time.monotonic()
        self.received_rate.reset(self.session_started_at)
        self.processed_rate.reset(self.session_started_at)

    def reset_detection(self) -> None:
        self._reset_detection = True
        self._suspect_pending = False
        self.report_error = None
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
            new_session, self._new_session = self._new_session, False
            reset_detection, self._reset_detection = self._reset_detection, False
            try:
                result = await loop.run_in_executor(self._executor, self._process, *item,
                                                    new_session, reset_detection)
                self.error = None
            except PoseError as e:
                if self.error != str(e):
                    log.error("[%s] 자세 추정 실패: %s", self.device_id, e)
                self.error = str(e)
                continue
            except Exception as e:
                message = f"{type(e).__name__}: {e}"
                if self.error != message:  # 같은 오류가 장면마다 반복되면 처음 한 번만 자세히 남긴다
                    log.exception("[%s] 장면 처리 중 오류", self.device_id)
                self.error = message
                continue
            if result is None:
                continue
            self._record(result)
            if result.became_suspect:
                log.warning("[%s] 낙상 의심 (t=%.2f) %s", self.device_id, result.t, result.features)
                self._suspect_pending = True
                self._suspect_features = result.features or self._analyzer.last_features
                self._suspect_retry_at = 0.0
            if self._suspect_pending:
                await self._report_suspect()

    async def _report_suspect(self) -> None:
        """낙상 의심을 이벤트 처리로 넘긴다. 실패하면(기록 파일 오류 등) 판정을 SUSPECT로 둔 채 잠시 뒤
        다시 시도한다. 넘어진 사람은 계속 누워 있어 새 하강이 없으므로 여기서 놓치면 다시 감지되지 않는다."""
        now = time.monotonic()
        if now < self._suspect_retry_at:
            return
        try:
            await self.on_suspect(self.device_id, self._suspect_features)
        except Exception:
            log.exception("[%s] 낙상 의심을 이벤트로 넘기지 못했습니다. %.0f초 뒤 다시 시도합니다",
                          self.device_id, SUSPECT_RETRY_SECONDS)
            self.report_error = "낙상 의심을 처리하지 못했습니다 (서버 기록 확인)"
            self._suspect_retry_at = now + SUSPECT_RETRY_SECONDS
            return
        self._suspect_pending = False
        self.report_error = None

    def _process(self, timestamp: float, jpeg: bytes, new_session: bool,
                 reset_detection: bool) -> Optional[FrameResult]:
        if self._analyzer is None:
            self._analyzer = FrameAnalyzer(self.factory.create(), self.params, self.velocity_window)
        if reset_detection:
            self._analyzer.reset_detection()
        last_t = self._analyzer.last_t
        if last_t is not None:
            if new_session and (timestamp <= last_t or timestamp - last_t > SESSION_GAP_SECONDS):
                self._analyzer.reset_session()  # edge 재부팅(시계가 거꾸로) 또는 오래 끊김
            elif timestamp <= last_t:
                return None  # 시각이 거꾸로 가는 장면은 버린다
        result = self._analyzer.process(jpeg, timestamp)
        if result is None:
            self.decode_errors += 1
            self.decode_fail_run += 1
        else:
            self.decode_fail_run = 0
        return result

    def _record(self, result: FrameResult) -> None:
        now = time.monotonic()
        self.processed += 1
        self.last_processed_at = now
        self.processed_rate.tick(now)
        self.state = result.state
        if result.person is not None:  # 건너뛴 장면은 마지막으로 본 값을 그대로 보여 준다
            self.analyzed += 1
            self.person = result.person
            self.features = result.features

    def processed_fps(self, now: Optional[float] = None) -> float:
        return self.processed_rate.rate(time.monotonic() if now is None else now)

    def received_fps(self, now: Optional[float] = None) -> float:
        return self.received_rate.rate(time.monotonic() if now is None else now)

    def problems(self, now: Optional[float] = None) -> List[str]:
        """이 장치의 영상 분석 문제 (화면 표시·보호자 알림용)."""
        now = time.monotonic() if now is None else now
        out = []
        if self.error:
            out.append(f"영상 분석 오류: {self.error}")
        if self.report_error:
            out.append(self.report_error)
        receiving = self.last_frame_at is not None and now - self.last_frame_at <= STALL_SECONDS
        if receiving and not self.error:
            last = self.last_processed_at if self.last_processed_at is not None else self.session_started_at
            if self.decode_fail_run >= DECODE_FAIL_FRAMES:
                out.append("영상이 깨져서 들어옵니다 (카메라·네트워크 확인)")
            elif now - last > STALL_SECONDS:
                out.append("영상 분석이 멈췄습니다")
            elif now - self.session_started_at >= FPS_WINDOW_SECONDS:
                fps, received = self.processed_fps(now), self.received_fps(now)
                if received < self.min_fps:
                    out.append(f"카메라 영상이 느리게 들어옵니다 (초당 {received:.1f}장, 조명·카메라·네트워크 확인)")
                elif fps < self.min_fps:
                    out.append(f"영상 분석이 밀립니다 (초당 {fps:.1f}장 처리, 서버 PC 부하 확인)")
        return out

    def snapshot(self) -> Dict:
        return {
            "detect_state": self.state.value,
            "person_detected": self.person,
            "features": self.features,
            "processed_fps": round(self.processed_fps(), 1),
            "frames_received": self.received,
            "frames_processed": self.processed,
            "frames_analyzed": self.analyzed,
            "frames_dropped": self.dropped,
            "decode_errors": self.decode_errors,
        }
