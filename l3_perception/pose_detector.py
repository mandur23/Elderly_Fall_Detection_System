"""자세 추정 모델 래퍼. 어떤 모델이든 같은 Keypoints를 반환한다.

모델을 불러오지 못하면 PoseDetectorError를 던진다. 예전처럼 Mock으로 조용히 바꾸면
실제로는 아무것도 감지하지 않으면서 화면에는 '정상'이 표시되기 때문이다.
"""

import logging
import time
from pathlib import Path
from typing import Optional

import numpy as np

from l3_perception.features import Keypoints

log = logging.getLogger("l3.pose")


class PoseDetectorError(RuntimeError):
    pass


class PoseDetector:
    name = "base"

    def detect(self, frame: np.ndarray, t: Optional[float] = None) -> Optional[Keypoints]:
        """t: 프레임 시각(초). 영상 모드 추적을 쓰는 모델에 필요하다."""
        raise NotImplementedError

    def close(self) -> None:
        pass


# 팔 관절(팔꿈치·손목·손가락). 팔을 벌리면 바운딩 박스가 넓어져 서 있어도 '누움'처럼 보이므로
# 종횡비 계산에서 뺀다. (실사진 확인: 달리는 자세에서 팔 포함 시 종횡비 1.51 -> 누움 오판)
MEDIAPIPE_ARMS = tuple(range(13, 23))
COCO_ARMS = (7, 8, 9, 10)


def _keypoints_from(points: np.ndarray, conf: np.ndarray, idx_ls: int, idx_rs: int,
                    idx_lh: int, idx_rh: int, min_conf: float, arms=()) -> Optional[Keypoints]:
    """어깨·엉덩이 4점이 모두 신뢰도 기준을 넘을 때만 Keypoints를 만든다."""
    core = [idx_ls, idx_rs, idx_lh, idx_rh]
    if np.any(conf[core] < min_conf):
        return None
    visible = points[conf >= min_conf]
    body = np.ones(len(points), dtype=bool)
    body[list(arms)] = False
    body_pts = points[body & (conf >= min_conf)]
    xmin, ymin = body_pts.min(axis=0)
    xmax, ymax = body_pts.max(axis=0)
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
    """MediaPipe Pose (Apache-2.0). 랜드마크 11/12 어깨, 23/24 엉덩이.

    모델 파일(.task)이 있으면 Tasks API(PoseLandmarker, 영상 모드)를 쓰고,
    없으면 구버전 solutions API로 대체한다.
    """
    name = "mediapipe"

    def __init__(self, min_conf: float, model_path=None):
        try:
            import mediapipe as mp
        except ImportError as e:
            raise PoseDetectorError("mediapipe 패키지가 설치되지 않았습니다.") from e
        self._mp = mp
        self.min_conf = min_conf
        self._last_ts_ms = -1
        self._landmarker = None
        self._legacy = None
        if model_path is not None and Path(model_path).exists():
            from mediapipe.tasks.python import BaseOptions, vision
            options = vision.PoseLandmarkerOptions(
                base_options=BaseOptions(model_asset_path=str(model_path)),
                running_mode=vision.RunningMode.VIDEO,
                num_poses=1,
                min_pose_detection_confidence=0.5,
                min_pose_presence_confidence=0.5,
                min_tracking_confidence=0.5,
            )
            self._landmarker = vision.PoseLandmarker.create_from_options(options)
        elif hasattr(mp, "solutions"):
            log.warning("MediaPipe 모델 파일이 없어 구버전 API를 사용합니다: %s", model_path)
            self._legacy = mp.solutions.pose.Pose(static_image_mode=False, model_complexity=1,
                                                  min_detection_confidence=0.5)
        else:
            raise PoseDetectorError(f"MediaPipe 모델 파일이 없습니다: {model_path}")

    def detect(self, frame: np.ndarray, t: Optional[float] = None) -> Optional[Keypoints]:
        import cv2
        h, w = frame.shape[:2]
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        if self._landmarker is not None:
            # 영상 모드는 단조 증가하는 타임스탬프(ms)가 필요하다.
            ts = int((t if t is not None else time.time()) * 1000)
            ts = max(ts, self._last_ts_ms + 1)
            self._last_ts_ms = ts
            image = self._mp.Image(image_format=self._mp.ImageFormat.SRGB, data=rgb)
            result = self._landmarker.detect_for_video(image, ts)
            if not result.pose_landmarks:
                return None
            lms = result.pose_landmarks[0]
        else:
            result = self._legacy.process(rgb)
            if not result.pose_landmarks:
                return None
            lms = result.pose_landmarks.landmark
        points = np.array([[lm.x * w, lm.y * h] for lm in lms])
        conf = np.array([lm.visibility for lm in lms])
        return _keypoints_from(points, conf, 11, 12, 23, 24, self.min_conf, MEDIAPIPE_ARMS)

    def close(self) -> None:
        if self._landmarker is not None:
            self._landmarker.close()
        if self._legacy is not None:
            self._legacy.close()


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

    def detect(self, frame: np.ndarray, t: Optional[float] = None) -> Optional[Keypoints]:
        result = self._model(frame, verbose=False)[0]
        if result.keypoints is None or result.boxes is None or len(result.boxes) == 0:
            return None
        best = int(result.boxes.conf.argmax())  # 여러 명이면 가장 확실한 사람 하나만 본다
        points = result.keypoints.xy.cpu().numpy()[best]
        if result.keypoints.conf is not None:
            conf = result.keypoints.conf.cpu().numpy()[best]
        else:
            conf = np.ones(len(points))
        return _keypoints_from(points, conf, 5, 6, 11, 12, self.min_conf, COCO_ARMS)


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

    def detect(self, frame: Optional[np.ndarray], t: Optional[float] = None) -> Optional[Keypoints]:
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
                    width: int, height: int, mediapipe_model_path=None) -> PoseDetector:
    if model == "mediapipe":
        return MediaPipePoseDetector(min_conf, mediapipe_model_path)
    if model == "yolo":
        return YoloPoseDetector(yolo_model_path, min_conf)
    if model == "mock":
        return MockPoseDetector(width, height)
    raise PoseDetectorError(f"알 수 없는 자세 추정 모델: {model}")
