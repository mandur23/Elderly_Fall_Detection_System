"""관절 좌표에서 낙상 판정용 기하 특징을 계산한다.

화면 좌표계 기준: x는 오른쪽, y는 아래쪽으로 증가한다.
기기마다 FeatureExtractor를 하나씩 둔다(이전 프레임 기록이 기기별로 달라야 하므로).
"""

from collections import deque
from dataclasses import dataclass
from typing import Deque, Dict, Optional, Tuple

import numpy as np

Point = Tuple[float, float]


@dataclass
class Keypoints:
    left_shoulder: Point
    right_shoulder: Point
    left_hip: Point
    right_hip: Point
    bbox: Tuple[float, float, float, float]  # (xmin, ymin, xmax, ymax)
    all_points: np.ndarray                   # (N, 2) 신뢰도 기준을 통과한 관절 좌표
    confidence: float = 1.0

    @property
    def shoulder_center(self) -> np.ndarray:
        return (np.asarray(self.left_shoulder, dtype=float) + np.asarray(self.right_shoulder, dtype=float)) / 2.0

    @property
    def hip_center(self) -> np.ndarray:
        return (np.asarray(self.left_hip, dtype=float) + np.asarray(self.right_hip, dtype=float)) / 2.0


def aspect_ratio(bbox: Tuple[float, float, float, float]) -> float:
    """바운딩 박스 가로/세로. 서 있으면 1 미만, 누우면 1 이상이 되는 경향이 있다."""
    xmin, ymin, xmax, ymax = bbox
    width = max(1.0, float(xmax - xmin))
    height = max(1.0, float(ymax - ymin))
    return width / height


def torso_angle(shoulder_center: np.ndarray, hip_center: np.ndarray) -> float:
    """어깨 중심-엉덩이 중심 벡터와 수직선 사이의 각도(0~90도)."""
    torso = np.asarray(hip_center, dtype=float) - np.asarray(shoulder_center, dtype=float)
    norm = float(np.linalg.norm(torso))
    if norm < 1e-6:
        return 0.0
    cos = abs(float(torso[1])) / norm
    return float(np.degrees(np.arccos(np.clip(cos, 0.0, 1.0))))


class FeatureExtractor:
    """프레임마다 Keypoints를 받아 특징값을 계산한다.

    하강 속도와 움직임량은 이전 프레임이 필요하므로 최근 기록을 보관한다.
    시간 차는 설정 FPS가 아니라 실제 프레임 시각으로 계산한다
    (휴대폰·Wi-Fi 상태에 따라 프레임 간격이 들쭉날쭉하기 때문).
    """

    def __init__(self, history_len: int = 15, velocity_span: int = 3):
        self.velocity_span = velocity_span
        self._hip_y: Deque[Tuple[float, float]] = deque(maxlen=history_len)   # (시각, y)
        self._torso_len: Deque[float] = deque(maxlen=history_len)
        self._prev_points: Optional[np.ndarray] = None

    def reset(self) -> None:
        self._hip_y.clear()
        self._torso_len.clear()
        self._prev_points = None

    def _reference_torso_length(self) -> float:
        # 누우면 화면상 몸통 길이가 줄 수 있으므로 최근 최댓값(대개 서 있을 때)을 기준으로 쓴다.
        return max(self._torso_len) if self._torso_len else 1.0

    def descent_velocity(self, t: float, hip_y: float, torso_len: float) -> float:
        """엉덩이 중심의 하강 속도(몸통 길이/초). 아래로 떨어질수록 큰 양수."""
        self._hip_y.append((t, hip_y))
        self._torso_len.append(torso_len)
        if len(self._hip_y) < 2:
            return 0.0
        span = min(self.velocity_span, len(self._hip_y) - 1)
        t0, y0 = self._hip_y[-1 - span]
        t1, y1 = self._hip_y[-1]
        dt = t1 - t0
        if dt <= 0:
            return 0.0
        return (y1 - y0) / dt / max(1.0, self._reference_torso_length())

    def motion_quantity(self, points: np.ndarray) -> float:
        """직전 프레임 대비 관절 평균 이동량(몸통 길이 단위). 기록용이며 판정에는 쓰지 않는다."""
        prev, self._prev_points = self._prev_points, points
        if prev is None or prev.shape != points.shape:
            return 0.0
        mean_px = float(np.mean(np.linalg.norm(points - prev, axis=1)))
        return mean_px / max(1.0, self._reference_torso_length())

    def extract(self, kp: Keypoints, t: float) -> Dict[str, float]:
        """특징값 4개를 계산한다. t는 프레임을 받은 실제 시각(초)."""
        shoulder, hip = kp.shoulder_center, kp.hip_center
        torso_len = float(np.linalg.norm(hip - shoulder))
        return {
            "aspect_ratio": round(aspect_ratio(kp.bbox), 3),
            "torso_angle": round(torso_angle(shoulder, hip), 2),
            "descent_velocity": round(self.descent_velocity(t, float(hip[1]), torso_len), 3),
            "motion_quantity": round(self.motion_quantity(np.asarray(kp.all_points, dtype=float)), 3),
        }
