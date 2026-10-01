"""L3 감지 프로세스: 카메라 -> 자세 추정 -> 특징 계산 -> 낙상 의심 판정 -> L4 보고.

실행: python -m l3_perception.fall_detector
원본 프레임은 메모리에서만 처리하고 저장하거나 L4로 보내지 않는다.
"""

import argparse
import logging
import threading
import time
from typing import Dict, Optional

import requests

import config
from l3_perception.features import FeatureExtractor
from l3_perception.fsm import FallFSM, FallState
from l3_perception.pose_detector import PoseDetector, PoseDetectorError, create_detector

log = logging.getLogger("l3")

CAMERA_RETRY_SECONDS = 5.0
CAMERA_MAX_READ_FAILURES = 10


class Reporter:
    """L4로 낙상 의심 보고와 하트비트를 보내는 백그라운드 스레드.

    네트워크 호출이 감지 루프를 막지 않도록 분리했다. L4가 꺼져 있으면 보고를
    보관했다가 다음 주기에 다시 보낸다.
    """

    def __init__(self, base_url: str, interval: float):
        self.base_url = base_url
        self.interval = interval
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
        self._wake()

    def take_reset(self) -> bool:
        with self._lock:
            requested, self._reset_requested = self._reset_requested, False
            return requested

    def _wake(self) -> None:
        # 낙상 의심은 다음 하트비트 주기를 기다리지 않고 바로 보낸다.
        threading.Thread(target=self._send_once, daemon=True).start()

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            self._send_once()

    def _send_once(self) -> None:
        with self._lock:
            pending = self._pending_suspect
            reported = self._reported_event_id
            status = dict(self._status)
        try:
            if pending is not None:
                res = requests.post(f"{self.base_url}/api/event",
                                    json={"event_type": "FALL_SUSPECT", "features": pending}, timeout=1.0)
                res.raise_for_status()
                event_id = res.json()["event_id"]
                with self._lock:
                    if self._pending_suspect is pending:
                        self._pending_suspect = None
                        self._reported_event_id = event_id
                reported = event_id
                log.info("L4에 낙상 의심 보고 완료 (event_id=%s)", event_id)

            res = requests.post(f"{self.base_url}/api/heartbeat", json=status, timeout=1.0)
            res.raise_for_status()
            active_event_id = res.json().get("event_id")
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
    def __init__(self, model: str = config.POSE_MODEL, use_camera: bool = True,
                 base_url: str = config.API_BASE_URL):
        self.use_camera = use_camera
        self.running = False
        self.cap = None
        self.reporter = Reporter(base_url, config.HEARTBEAT_INTERVAL_SECONDS)
        self.extractor = FeatureExtractor()
        self.fsm = FallFSM(
            descent_peak=config.DESCENT_VELOCITY_PEAK,
            t1_lying_seconds=config.T1_LYING_SECONDS,
            falling_timeout=config.FALLING_TIMEOUT_SECONDS,
            angle_lying=config.TORSO_ANGLE_LYING,
            aspect_lying=config.ASPECT_RATIO_LYING,
            angle_standing=config.TORSO_ANGLE_STANDING,
            aspect_standing=config.ASPECT_RATIO_STANDING,
            on_state_change=self._on_state_change,
        )

        self.detector: Optional[PoseDetector] = None
        try:
            self.detector = create_detector(model, config.KEYPOINT_MIN_CONFIDENCE, config.YOLO_MODEL_PATH,
                                            config.FRAME_WIDTH, config.FRAME_HEIGHT)
            self.reporter.update_status(detector_ok=True, detector=self.detector.name, detector_error=None)
        except PoseDetectorError as e:
            log.error("자세 추정 모델을 불러오지 못했습니다: %s", e)
            self.reporter.update_status(detector_ok=False, detector=model, detector_error=str(e))
        self.reporter.update_status(camera_ok=not use_camera, l3_state=self.fsm.state.value,
                                    person_detected=False, fps=0.0, features=None)

    def _on_state_change(self, old: FallState, new: FallState, features: Optional[Dict]) -> None:
        log.info("상태 전이 %s -> %s | %s", old.value, new.value, features)
        self.reporter.update_status(l3_state=new.value)
        if new == FallState.SUSPECT:
            self.reporter.report_suspect(features)

    # --- 카메라 ---
    def _open_camera(self, source) -> bool:
        import cv2
        self.cap = cv2.VideoCapture(source)
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, config.FRAME_WIDTH)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, config.FRAME_HEIGHT)
        ok = self.cap.isOpened()
        self.reporter.update_status(camera_ok=ok)
        if ok:
            log.info("카메라 연결됨 (소스: %s)", source)
        else:
            log.error("카메라를 열 수 없습니다 (소스: %s)", source)
        return ok

    def _read_frame(self, source):
        """카메라 프레임을 읽는다. 실패가 계속되면 다시 연결을 시도하고 None을 반환한다."""
        import cv2
        if self.cap is None or not self.cap.isOpened():
            if time.monotonic() - self._last_camera_try < CAMERA_RETRY_SECONDS:
                return None
            self._last_camera_try = time.monotonic()
            if not self._open_camera(source):
                return None
        ok, frame = self.cap.read()
        if not ok:
            self._read_failures += 1
            if self._read_failures >= CAMERA_MAX_READ_FAILURES:
                log.error("카메라 프레임을 계속 읽지 못해 재연결합니다.")
                self.cap.release()
                self.reporter.update_status(camera_ok=False)
                self._read_failures = 0
            return None
        self._read_failures = 0
        self.reporter.update_status(camera_ok=True)
        h, w = frame.shape[:2]
        if (w, h) != (config.FRAME_WIDTH, config.FRAME_HEIGHT):
            frame = cv2.resize(frame, (config.FRAME_WIDTH, config.FRAME_HEIGHT))
        return frame

    # --- 메인 루프 ---
    def run(self, source=config.CAMERA_INDEX) -> None:
        self.running = True
        self._last_camera_try = float("-inf")
        self._read_failures = 0
        self.reporter.start()
        interval = 1.0 / config.TARGET_FPS
        fps = 0.0
        last_tick: Optional[float] = None

        try:
            while self.running:
                tick = time.monotonic()
                now = time.time()

                if self.reporter.take_reset():
                    log.info("L4 이벤트 종료 확인 -> 감시 재개")
                    self.fsm.reset(now)
                    self.extractor.reset()

                frame = self._read_frame(source) if self.use_camera else None
                if self.detector is not None and (frame is not None or not self.use_camera):
                    kp = self.detector.detect(frame)
                    features = self.extractor.extract(kp, now) if kp is not None else None
                    self.fsm.update(features, now)
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
        if self.cap is not None and self.cap.isOpened():
            self.cap.release()
        if self.detector is not None:
            self.detector.close()
        log.info("L3 감지 프로세스 종료")


def main() -> None:
    parser = argparse.ArgumentParser(description="L3 낙상 감지 프로세스")
    parser.add_argument("--model", default=config.POSE_MODEL, choices=["mediapipe", "yolo", "mock"])
    parser.add_argument("--source", default=config.CAMERA_INDEX,
                        help="카메라 번호 또는 시험용 영상 파일 경로")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s][L3] %(message)s")

    source = int(args.source) if str(args.source).isdigit() else args.source
    pipeline = FallDetectionPipeline(model=args.model, use_camera=args.model != "mock")
    try:
        pipeline.run(source)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
