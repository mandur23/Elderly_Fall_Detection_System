"""자세 추정. MediaPipe Pose Landmarker(Tasks API, VIDEO 모드, lite 모델)를 쓴다.

테스트에서는 PoseFactory를 바꿔 끼워 모델 없이도 돌 수 있다. 모델을 불러오지 못하면 PoseError를 던지고,
서버는 '영상 분석 모델을 불러오지 못했습니다'를 장치 문제로 표시한다(아무것도 감지하지 않으면서 정상으로
보이지 않게 하기 위함).
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Sequence

import numpy as np

from server.features import Keypoints

# MediaPipe 랜드마크 번호: 11/12 어깨, 23/24 엉덩이, 13~22 팔(팔꿈치·손목·손가락).
# 팔을 벌리면 영역이 넓어져 서 있어도 누운 것처럼 보이므로 종횡비 계산에서 팔을 뺀다.
L_SHOULDER, R_SHOULDER, L_HIP, R_HIP = 11, 12, 23, 24
ARMS = tuple(range(13, 23))


class PoseError(RuntimeError):
    pass


class PoseEstimator:
    """영상 한 흐름(장치 하나)에 대한 자세 추정기. 같은 스레드에서 차례로 부른다."""

    def detect(self, rgb: np.ndarray, t: float) -> Optional[Keypoints]:
        """rgb: (H, W, 3) uint8. t: 촬영 시각(초). 사람을 못 찾으면 None."""
        raise NotImplementedError

    def reset(self) -> None:
        """새 영상 흐름 시작(다시 접속). 추적 상태와 시각 기록을 지운다."""

    def close(self) -> None:
        pass


class PoseFactory:
    """장치마다 추정기를 만든다. load()는 시작할 때 한 번 불러 모델을 확인한다."""
    name = "base"
    error: Optional[str] = None

    def load(self) -> None:
        pass

    def create(self) -> PoseEstimator:
        raise NotImplementedError


def keypoints_from_landmarks(points: np.ndarray, conf: np.ndarray, min_conf: float,
                             arms: Sequence[int] = ARMS) -> Optional[Keypoints]:
    """어깨·엉덩이 4점이 모두 신뢰도 기준을 넘을 때만 Keypoints를 만든다. points는 픽셀 좌표 (N, 2)."""
    core = [L_SHOULDER, R_SHOULDER, L_HIP, R_HIP]
    if np.any(conf[core] < min_conf):
        return None
    body = np.ones(len(points), dtype=bool)
    body[list(arms)] = False
    body_pts = points[body & (conf >= min_conf)]
    xmin, ymin = body_pts.min(axis=0)
    xmax, ymax = body_pts.max(axis=0)
    return Keypoints(
        left_shoulder=(float(points[L_SHOULDER][0]), float(points[L_SHOULDER][1])),
        right_shoulder=(float(points[R_SHOULDER][0]), float(points[R_SHOULDER][1])),
        left_hip=(float(points[L_HIP][0]), float(points[L_HIP][1])),
        right_hip=(float(points[R_HIP][0]), float(points[R_HIP][1])),
        bbox=(float(xmin), float(ymin), float(xmax), float(ymax)),
        confidence=float(conf[core].mean()),
    )


class MediaPipePoseEstimator(PoseEstimator):
    def __init__(self, model_path: Path, min_conf: float):
        self.model_path = Path(model_path)
        self.min_conf = min_conf
        self._landmarker = None
        self._last_ts_ms = -1
        self._open()

    def _open(self) -> None:
        try:
            import mediapipe as mp
            from mediapipe.tasks.python import BaseOptions, vision
        except ImportError as e:
            raise PoseError("mediapipe 패키지가 설치되지 않았습니다") from e
        if not self.model_path.exists():
            raise PoseError(f"자세 추정 모델 파일이 없습니다: {self.model_path}")
        self._mp = mp
        options = vision.PoseLandmarkerOptions(
            base_options=BaseOptions(model_asset_path=str(self.model_path)),
            running_mode=vision.RunningMode.VIDEO,
            num_poses=1,
            min_pose_detection_confidence=0.5,
            min_pose_presence_confidence=0.5,
            min_tracking_confidence=0.5,
        )
        try:
            self._landmarker = vision.PoseLandmarker.create_from_options(options)
        except Exception as e:  # 모델 파일 손상 등
            raise PoseError(f"자세 추정 모델을 불러오지 못했습니다: {e}") from e
        self._last_ts_ms = -1

    def detect(self, rgb: np.ndarray, t: float) -> Optional[Keypoints]:
        if self._landmarker is None:  # 다시 만들기(reset)가 실패했으면 다음 장면에서 다시 시도한다
            self._open()
        h, w = rgb.shape[:2]
        # VIDEO 모드는 단조 증가하는 시각(ms)이 필요하다.
        ts = max(int(t * 1000), self._last_ts_ms + 1)
        self._last_ts_ms = ts
        image = self._mp.Image(image_format=self._mp.ImageFormat.SRGB, data=np.ascontiguousarray(rgb))
        result = self._landmarker.detect_for_video(image, ts)
        if not result.pose_landmarks:
            return None
        lms = result.pose_landmarks[0]
        points = np.array([[lm.x * w, lm.y * h] for lm in lms])
        conf = np.array([lm.visibility if lm.visibility is not None else 0.0 for lm in lms])
        return keypoints_from_landmarks(points, conf, self.min_conf)

    def reset(self) -> None:
        # 다시 접속하면 edge의 시계가 달라질 수 있어(재부팅 등) 시각이 거꾸로 갈 수 있다. 새로 만든다.
        self.close()
        self._open()

    def close(self) -> None:
        if self._landmarker is not None:
            self._landmarker.close()
            self._landmarker = None


class MediaPipePoseFactory(PoseFactory):
    name = "mediapipe"

    def __init__(self, model_path: Path, min_conf: float):
        self.model_path = Path(model_path)
        self.min_conf = min_conf
        self.error = None

    def load(self) -> None:
        try:
            MediaPipePoseEstimator(self.model_path, self.min_conf).close()
            self.error = None
        except PoseError as e:
            self.error = str(e)

    def create(self) -> PoseEstimator:
        return MediaPipePoseEstimator(self.model_path, self.min_conf)
