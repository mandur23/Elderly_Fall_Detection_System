"""관절 좌표에서 낙상 판정용 특징(몸통 각도, 종횡비, 하강 속도)을 계산한다.

화면 좌표계: x는 오른쪽, y는 아래쪽으로 증가한다(픽셀).
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from typing import Deque, Dict, Tuple

Point = Tuple[float, float]


@dataclass
class Keypoints:
    left_shoulder: Point
    right_shoulder: Point
    left_hip: Point
    right_hip: Point
    bbox: Tuple[float, float, float, float]  # (xmin, ymin, xmax, ymax). 팔을 뺀 관절로 만든다
    confidence: float = 1.0

    @property
    def shoulder_center(self) -> Point:
        return ((self.left_shoulder[0] + self.right_shoulder[0]) / 2,
                (self.left_shoulder[1] + self.right_shoulder[1]) / 2)

    @property
    def hip_center(self) -> Point:
        return ((self.left_hip[0] + self.right_hip[0]) / 2, (self.left_hip[1] + self.right_hip[1]) / 2)


def aspect_ratio(bbox: Tuple[float, float, float, float]) -> float:
    """영역 가로/세로. 서 있으면 1 미만, 누우면 1 이상이 되는 경향이 있다."""
    xmin, ymin, xmax, ymax = bbox
    return max(1.0, xmax - xmin) / max(1.0, ymax - ymin)


def torso_angle(shoulder: Point, hip: Point) -> float:
    """어깨 중심-엉덩이 중심 선과 수직선 사이의 각도(0~90도)."""
    dx, dy = hip[0] - shoulder[0], hip[1] - shoulder[1]
    norm = math.hypot(dx, dy)
    if norm < 1e-6:
        return 0.0
    return math.degrees(math.acos(min(1.0, abs(dy) / norm)))


class FeatureExtractor:
    """프레임마다 Keypoints를 받아 특징을 계산한다.

    하강 속도는 엉덩이 중심 y의 변화를 몸통 길이로 나눈 값(몸통 길이/초)이다. 프레임 수가 아니라
    velocity_window초 전 기록과 비교한다. 프레임 수로 비교하면 처리 속도에 따라 구간 길이가 달라져
    같은 낙상도 값이 크게 달라졌다(docs/EVALUATION.md). 시간은 edge가 붙인 촬영 시각을 쓴다.
    """

    def __init__(self, velocity_window: float = 0.3, history_seconds: float = 1.5):
        self.velocity_window = velocity_window
        self.history_seconds = history_seconds
        self._history: Deque[Tuple[float, float, float]] = deque()  # (시각, 엉덩이 y, 몸통 길이)

    def reset(self) -> None:
        self._history.clear()

    def _reference_torso_length(self) -> float:
        # 누우면 화면상 몸통 길이가 줄 수 있으므로 최근 최댓값(대개 서 있을 때)을 기준으로 쓴다.
        return max((h[2] for h in self._history), default=1.0)

    def descent_velocity(self, t: float, hip_y: float, torso_len: float) -> float:
        """엉덩이 중심의 하강 속도. 아래로 떨어질수록 큰 양수."""
        self._history.append((t, hip_y, torso_len))
        while self._history and t - self._history[0][0] > self.history_seconds + 1e-6:
            self._history.popleft()
        if len(self._history) < 2:
            return 0.0
        # 구간 시작 시각 이전의 가장 최근 기록. 기록이 그보다 짧으면 가장 오래된 기록.
        t0, y0, _ = self._history[0]
        for ti, yi, _ in reversed(self._history):
            if ti <= t - self.velocity_window + 1e-6:
                t0, y0 = ti, yi
                break
        dt = t - t0
        if dt <= 0:
            return 0.0
        return (hip_y - y0) / dt / max(1.0, self._reference_torso_length())

    def extract(self, kp: Keypoints, t: float) -> Dict[str, float]:
        shoulder, hip = kp.shoulder_center, kp.hip_center
        torso_len = math.hypot(hip[0] - shoulder[0], hip[1] - shoulder[1])
        return {
            "aspect_ratio": round(aspect_ratio(kp.bbox), 3),
            "torso_angle": round(torso_angle(shoulder, hip), 2),
            "descent_velocity": round(self.descent_velocity(t, hip[1], torso_len), 3),
        }
