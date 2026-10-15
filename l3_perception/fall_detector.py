"""L3 감지 프로세스: 카메라 -> 자세 추정 -> 특징 계산 -> 낙상 의심 판정 -> L4 보고.

실행: python -m l3_perception.fall_detector
원본 프레임은 메모리에서만 처리하고 저장하거나 L4로 보내지 않는다.
"""

import argparse
import logging
import threading
import time
from typing import Dict, Optional, Tuple, Union

import requests

import config
from common import sdnotify
from l3_perception.camera import Camera, CameraError, create_camera, rotate
from l3_perception.features import FeatureExtractor, Keypoints
from l3_perception.fsm import FallFSM, FallState
from l3_perception.pose_detector import PoseDetector, PoseDetectorError, create_detector

log = logging.getLogger("l3")

CAMERA_RETRY_SECONDS = 5.0
CAMERA_MAX_READ_FAILURES = 10


def create_fsm(on_state_change=None) -> FallFSM:
    return FallFSM(
        descent_peak=config.DESCENT_VELOCITY_PEAK,
        t1_lying_seconds=config.T1_LYING_SECONDS,
        falling_timeout=config.FALLING_TIMEOUT_SECONDS,
        angle_lying=config.TORSO_ANGLE_LYING,
        aspect_lying=config.ASPECT_RATIO_LYING,
        angle_standing=config.TORSO_ANGLE_STANDING,
        on_state_change=on_state_change,
        standing_confirm=config.STANDING_CONFIRM_SECONDS,
        missing_counts_as_lying=config.MISSING_COUNTS_AS_LYING,
    )


def create_extractor() -> FeatureExtractor:
    return FeatureExtractor(velocity_window=config.DESCENT_VELOCITY_WINDOW_SECONDS)


def load_detector(model: str) -> PoseDetector:
    return create_detector(model, config.KEYPOINT_MIN_CONFIDENCE, config.YOLO_MODEL_PATH,
                           config.FRAME_WIDTH, config.FRAME_HEIGHT, config.MEDIAPIPE_MODEL_PATH)


class FrameAnalyzer:
    """프레임 한 장을 자세 추정 -> 특징 -> 상태 머신까지 처리한다.

    실시간 감지와 영상 파일 평가(tools/evaluate.py)가 같은 코드를 쓰도록 분리했다.
    """

    def __init__(self, detector: PoseDetector, on_state_change=None):
        self.detector = detector
        self.extractor = create_extractor()
        self.fsm = create_fsm(on_state_change)

    def process(self, frame, t: float) -> Tuple[Optional[Keypoints], Optional[Dict[str, float]], FallState]:
        kp = self.detector.detect(frame, t)
        features = self.extractor.extract(kp, t) if kp is not None else None
        state = self.fsm.update(features, t)
        return kp, features, state

    def reset(self, t: float) -> None:
        self.fsm.reset(t)
        self.extractor.reset()


class Reporter:
    """L4로 낙상 의심 보고와 하트비트를 보내는 백그라운드 스레드.

    네트워크 호출이 감지 루프를 막지 않도록 분리했다. L4가 꺼져 있으면 보고를
    보관했다가 다음 주기에 다시 보낸다.
    """

    def __init__(self, base_url: str, interval: float, token: str = ""):
        self.base_url = base_url
        self.interval = interval
        self.headers = {"X-Internal-Token": token} if token else {}
        self._lock = threading.Lock()
        self._status: Dict = {}
        self._pending_suspect: Optional[Dict] = None
        self._reported_event_id: Optional[int] = None
        self._reset_requested = False
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="l3-reporter", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def update_status(self, **status) -> None:
        with self._lock:
            self._status.update(status)

    def report_suspect(self, features: Optional[Dict]) -> None:
        with self._lock:
            self._pending_suspect = features or {}
            self._reported_event_id = None
        # 낙상 의심은 다음 하트비트 주기를 기다리지 않고 바로 보낸다.
        threading.Thread(target=self._send_once, daemon=True).start()

    def take_reset(self) -> bool:
        with self._lock:
            requested, self._reset_requested = self._reset_requested, False
            return requested

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            self._send_once()

    def _post(self, path: str, payload: Dict) -> Dict:
        res = requests.post(f"{self.base_url}{path}", json=payload, headers=self.headers, timeout=1.0)
        res.raise_for_status()
        return res.json()

    def _send_once(self) -> None:
        with self._lock:
            pending = self._pending_suspect
            reported = self._reported_event_id
            status = dict(self._status)
        try:
            if pending is not None:
                event_id = self._post("/api/event", {"event_type": "FALL_SUSPECT", "features": pending})["event_id"]
                with self._lock:
                    if self._pending_suspect is pending:
                        self._pending_suspect = None
                        self._reported_event_id = event_id
                reported = event_id
                log.info("L4에 낙상 의심 보고 완료 (event_id=%s)", event_id)

            active_event_id = self._post("/api/heartbeat", status).get("event_id")
            with self._lock:
                # 이 하트비트보다 먼저 보고된 이벤트가 L4에서 끝났으면(해제·경보 해제) 감시를 재개한다.
                if (reported is not None
                        and reported == self._reported_event_id
                        and self._pending_suspect is None
                        and active_event_id != reported):
                    self._reported_event_id = None
                    self._reset_requested = True
        except requests.RequestException as e:
            log.debug("L4 통신 실패: %s", e)


class FallDetectionPipeline:
    def __init__(self, model: str = config.POSE_MODEL, camera: Optional[Camera] = None,
                 base_url: str = config.API_BASE_URL, token: str = config.INTERNAL_API_TOKEN):
        """camera가 None이면 카메라 없이 동작한다(Mock 모델 데모용)."""
        self.camera = camera
        self.running = False
        self.reporter = Reporter(base_url, config.HEARTBEAT_INTERVAL_SECONDS, token)
        self.analyzer: Optional[FrameAnalyzer] = None
        try:
            detector = load_detector(model)
            self.analyzer = FrameAnalyzer(detector, self._on_state_change)
            self.reporter.update_status(detector_ok=True, detector=detector.name, detector_error=None)
        except PoseDetectorError as e:
            log.error("자세 추정 모델을 불러오지 못했습니다: %s", e)
            self.reporter.update_status(detector_ok=False, detector=model, detector_error=str(e))
        self.reporter.update_status(camera_ok=camera is None, camera=camera.name if camera else None,
                                    l3_state=FallState.NORMAL.value, person_detected=False, fps=0.0,
                                    features=None)

    @property
    def detector(self) -> Optional[PoseDetector]:
        return self.analyzer.detector if self.analyzer else None

    @property
    def fsm(self) -> Optional[FallFSM]:
        return self.analyzer.fsm if self.analyzer else None

    def _on_state_change(self, old: FallState, new: FallState, features: Optional[Dict]) -> None:
        log.info("상태 전이 %s -> %s | %s", old.value, new.value, features)
        self.reporter.update_status(l3_state=new.value)
        if new == FallState.SUSPECT:
            self.reporter.report_suspect(features)

    # --- 카메라 ---
    def _ensure_camera(self) -> bool:
        if self._camera_open:
            return True
        if time.monotonic() - self._last_camera_try < CAMERA_RETRY_SECONDS:
            return False
        self._last_camera_try = time.monotonic()
        try:
            self.camera.open()
        except CameraError as e:
            log.error("%s", e)
            self.reporter.update_status(camera_ok=False, camera_error=str(e))
            return False
        log.info("카메라 연결됨 (%s)", self.camera.name)
        self._camera_open = True
        self._read_failures = 0
        self.reporter.update_status(camera_ok=True, camera_error=None)
        return True

    def _read_frame(self):
        if not self._ensure_camera():
            return None
        frame = self.camera.read()
        if frame is None:
            self._read_failures += 1
            if self._read_failures >= CAMERA_MAX_READ_FAILURES:
                log.error("카메라 프레임을 계속 읽지 못해 다시 연결합니다.")
                self.camera.close()
                self._camera_open = False
                self.reporter.update_status(camera_ok=False, camera_error="프레임 읽기 실패")
            return None
        self._read_failures = 0
        return rotate(frame, config.CAMERA_ROTATION)

    # --- 메인 루프 ---
    def run(self) -> None:
        self.running = True
        self._camera_open = False
        self._last_camera_try = float("-inf")
        self._read_failures = 0
        self.reporter.start()
        sdnotify.ready()
        interval = 1.0 / config.TARGET_FPS
        fps = 0.0
        last_tick: Optional[float] = None

        try:
            while self.running:
                tick = time.monotonic()
                now = time.time()
                # 루프가 멈추면(카메라 드라이버 정지 등) systemd가 재시작하도록 매 반복 알린다.
                sdnotify.watchdog()

                if self.analyzer is not None and self.reporter.take_reset():
                    log.info("L4 이벤트 종료 확인 -> 감시 재개")
                    self.analyzer.reset(now)

                frame = self._read_frame() if self.camera is not None else None
                if self.analyzer is not None and (frame is not None or self.camera is None):
                    kp, features, _ = self.analyzer.process(frame, now)
                    if last_tick is not None and tick > last_tick:
                        inst = 1.0 / (tick - last_tick)
                        fps = 0.9 * fps + 0.1 * inst if fps else inst
                    last_tick = tick
                    self.reporter.update_status(person_detected=kp is not None, features=features,
                                                fps=round(fps, 1))
                del frame  # 원본 프레임은 보관하지 않는다.

                time.sleep(max(0.001, interval - (time.monotonic() - tick)))
        finally:
            self.stop()

    def stop(self) -> None:
        self.running = False
        self.reporter.stop()
        if self.camera is not None:
            self.camera.close()
        if self.detector is not None:
            self.detector.close()
        log.info("L3 감지 프로세스 종료")


def main() -> None:
    parser = argparse.ArgumentParser(description="L3 낙상 감지 프로세스")
    parser.add_argument("--model", default=config.POSE_MODEL, choices=["mediapipe", "yolo", "mock"])
    parser.add_argument("--camera", default=config.CAMERA_BACKEND, choices=["auto", "picamera2", "opencv"])
    parser.add_argument("--source", default=str(config.CAMERA_INDEX),
                        help="카메라 번호 또는 시험용 영상 파일 경로")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s][L3] %(message)s")

    camera = None
    if args.model != "mock":
        source: Union[int, str] = int(args.source) if args.source.isdigit() else args.source
        camera = create_camera(args.camera, source, config.FRAME_WIDTH, config.FRAME_HEIGHT, config.TARGET_FPS)
    pipeline = FallDetectionPipeline(model=args.model, camera=camera)
    try:
        pipeline.run()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
