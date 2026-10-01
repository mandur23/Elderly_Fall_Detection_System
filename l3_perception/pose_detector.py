"""자세 추정 모델 래퍼. 어떤 모델이든 같은 Keypoints를 반환한다.

모델을 불러오지 못하면 PoseDetectorError를 던진다. 예전처럼 Mock으로 조용히 바꾸면
실제로는 아무것도 감지하지 않으면서 화면에는 '정상'이 표시되기 때문이다.
"""

from typing import Optional

import numpy as np

from l3_perception.features import Keypoints


class PoseDetectorError(RuntimeError):
    pass


class PoseDetector:
    name = "base"

    def detect(self, frame: np.ndarray) -> Optional[Keypoints]:
        raise NotImplementedError

    def close(self) -> None:
        pass


def _keypoints_from(points: np.ndarray, conf: np.ndarray, idx_ls: int, idx_rs: int,
                    idx_lh: int, idx_rh: int, min_conf: float) -> Optional[Keypoints]:
    """어깨·엉덩이 4점이 모두 신뢰도 기준을 넘을 때만 Keypoints를 만든다."""
    core = [idx_ls, idx_rs, idx_lh, idx_rh]
    if np.any(conf[core] < min_conf):
        return None
    visible = points[conf >= min_conf]
    xmin, ymin = visible.min(axis=0)
    xmax, ymax = visible.max(axis=0)
    return Keypoints(
        left_shoulder=tuple(points[idx_ls]),
        right_shoulder=tuple(points[idx_rs]),
        left_hip=tuple(points[idx_lh]),
        right_hip=tuple(points[idx_rh]),
        bbox=(float(xmin), float(ymin), float(xmax), float(ymax)),
        all_points=visible,
        confidence=float(conf[core].mean()),
    )


class MediaPipePoseDetector(PoseDetector):
    """MediaPipe Pose (Apache-2.0). 랜드마크 11/12 어깨, 23/24 엉덩이."""
    name = "mediapipe"

    def __init__(self, min_conf: float):
        try:
            import mediapipe as mp
        except ImportError as e:
            raise PoseDetectorError("mediapipe 패키지가 설치되지 않았습니다.") from e
        self.min_conf = min_conf
        self._pose = mp.solutions.pose.Pose(static_image_mode=False, model_complexity=1,
                                            min_detection_confidence=0.5)

    def detect(self, frame: np.ndarray) -> Optional[Keypoints]:
        import cv2
        h, w = frame.shape[:2]
        results = self._pose.process(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
        if not results.pose_landmarks:
            return None
        lms = results.pose_landmarks.landmark
        points = np.array([[lm.x * w, lm.y * h] for lm in lms])
        conf = np.array([lm.visibility for lm in lms])
        return _keypoints_from(points, conf, 11, 12, 23, 24, self.min_conf)

    def close(self) -> None:
        self._pose.close()


class YoloPoseDetector(PoseDetector):
    """Ultralytics YOLO-Pose (AGPL-3.0). COCO 키포인트 5/6 어깨, 11/12 엉덩이."""
    name = "yolo"

    def __init__(self, model_path: str, min_conf: float):
        try:
            from ultralytics import YOLO
        except ImportError as e:
            raise PoseDetectorError("ultralytics 패키지가 설치되지 않았습니다.") from e
        self.min_conf = min_conf
        self._model = YOLO(model_path)

    def detect(self, frame: np.ndarray) -> Optional[Keypoints]:
        result = self._model(frame, verbose=False)[0]
        if result.keypoints is None or result.boxes is None or len(result.boxes) == 0:
            return None
        best = int(result.boxes.conf.argmax())  # 여러 명이면 가장 확실한 사람 하나만 본다
        points = result.keypoints.xy.cpu().numpy()[best]
        if result.keypoints.conf is not None:
            conf = result.keypoints.conf.cpu().numpy()[best]
        else:
            conf = np.ones(len(points))
        return _keypoints_from(points, conf, 5, 6, 11, 12, self.min_conf)


class MockPoseDetector(PoseDetector):
    """데모·테스트용 가상 사람. 실제 영상은 보지 않는다.

    모드:
      standing - 서 있음
      fall     - 몇 프레임 만에 빠르게 쓰러진 뒤 바닥에 누운 상태 유지
      sit      - 천천히 앉음 (경보가 나면 안 되는 일상 동작)
      stumble  - 빠르게 주저앉았다가 곧바로 일어섬
      absent   - 사람이 화면에 없음
    """
    name = "mock"
    FALL_FRAMES = 3
    SIT_FRAMES = 12

    def __init__(self, width: int = 640, height: int = 480):
        self.w, self.h = width, height
        self.mode = "standing"
        self.step = 0

    def set_mode(self, mode: str) -> None:
        self.mode = mode
        self.step = 0

    def _pose(self, hip_y: float, lying: float) -> Keypoints:
        """엉덩이 높이와 기울기(lying 0=직립, 1=수평)로 가상 관절과 바운딩 박스를 만든다."""
        w, h = self.w, self.h
        a = lying * np.pi / 2
        up = np.array([-np.sin(a), -np.cos(a)])    # 엉덩이 -> 머리 방향
        side = np.array([np.cos(a), -np.sin(a)])   # 몸의 좌우 방향
        hip = np.array([0.5 * w, hip_y])
        shoulder = hip + up * 0.25 * h
        head = hip + up * 0.35 * h
        feet = hip - up * 0.35 * h
        ls, rs = shoulder - side * 0.04 * w, shoulder + side * 0.04 * w
        lh, rh = hip - side * 0.03 * w, hip + side * 0.03 * w
        pts = np.array([head, ls, rs, lh, rh, feet])
        pad = 0.03 * w
        xmin, ymin = pts.min(axis=0) - pad
        xmax, ymax = pts.max(axis=0) + pad
        return Keypoints(tuple(ls), tuple(rs), tuple(lh), tuple(rh),
                         (xmin, ymin, xmax, ymax), pts, 0.95)

    def detect(self, frame: Optional[np.ndarray]) -> Optional[Keypoints]:
        self.step += 1
        stand_hip, floor_hip = 0.55 * self.h, 0.85 * self.h

        if self.mode == "absent":
            return None
        if self.mode == "fall":
            p = min(1.0, self.step / self.FALL_FRAMES)
            return self._pose(stand_hip + p * (floor_hip - stand_hip), p)
        if self.mode == "sit":
            p = min(1.0, self.step / self.SIT_FRAMES)
            return self._pose(stand_hip + p * 0.15 * self.h, 0.0)
        if self.mode == "stumble":
            # 3프레임 동안 빠르게 내려갔다가 3프레임 동안 다시 올라온다.
            p = max(0.0, 1.0 - abs(self.step - 3) / 3) if self.step <= 6 else 0.0
            return self._pose(stand_hip + p * 0.2 * self.h, 0.0)
        return self._pose(stand_hip, 0.0)


def create_detector(model: str, min_conf: float, yolo_model_path: str,
                    width: int, height: int) -> PoseDetector:
    if model == "mediapipe":
        return MediaPipePoseDetector(min_conf)
    if model == "yolo":
        return YoloPoseDetector(yolo_model_path, min_conf)
    if model == "mock":
        return MockPoseDetector(width, height)
    raise PoseDetectorError(f"알 수 없는 자세 추정 모델: {model}")
