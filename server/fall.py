"""낙상 의심 판정 상태 머신.

    NORMAL --하강 속도 기준 이상--> FALLING --누운 자세 lying_seconds 연속--> SUSPECT --reset()--> NORMAL
                                     |--일어선 자세 standing_confirm초 연속 / falling_timeout 초과--> NORMAL

SUSPECT는 음성 확인·경보 처리가 끝나 reset()이 불릴 때까지 유지한다. 같은 낙상으로 이벤트가 반복되지 않게 하기 위함이다.
missing_counts_as_lying이 켜져 있으면 FALLING 중 사람이 안 보이는 프레임도 누운 자세로 센다. 넘어지면서
화면 아래로 벗어나거나 가구 뒤로 가려지는 경우를 놓치지 않기 위함이다(docs/EVALUATION.md, 14 -> 26/30).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Dict, Optional


class FallState(str, Enum):
    NORMAL = "NORMAL"
    FALLING = "FALLING"
    SUSPECT = "SUSPECT"


@dataclass
class FallParams:
    descent_peak: float = 1.5
    lying_seconds: float = 2.0
    falling_timeout: float = 5.0
    angle_lying: float = 60.0
    aspect_lying: float = 1.0
    angle_standing: float = 40.0
    standing_confirm: float = 0.5
    missing_counts_as_lying: bool = True

    @classmethod
    def from_settings(cls, s) -> "FallParams":
        return cls(descent_peak=s.descent_velocity_peak, lying_seconds=s.lying_seconds,
                   falling_timeout=s.falling_timeout_seconds, angle_lying=s.torso_angle_lying,
                   aspect_lying=s.aspect_ratio_lying, angle_standing=s.torso_angle_standing,
                   standing_confirm=s.standing_confirm_seconds,
                   missing_counts_as_lying=s.missing_counts_as_lying)


class FallFSM:
    def __init__(self, params: FallParams):
        self.p = params
        self.state = FallState.NORMAL
        self.state_since = 0.0
        self.lying_since: Optional[float] = None
        self.standing_since: Optional[float] = None
        self.suspect_features: Optional[Dict[str, float]] = None

    def is_lying(self, f: Dict[str, float]) -> bool:
        """몸통 각도가 주 기준이다. 종횡비는 몸통도 어느 정도 기울었을 때만 보조로 쓴다.

        다리를 벌리고 선 자세는 종횡비가 1을 넘어 종횡비만으로는 누움과 구분되지 않는다.
        """
        angle = f["torso_angle"]
        return angle >= self.p.angle_lying or (f["aspect_ratio"] >= self.p.aspect_lying
                                               and angle >= self.p.angle_standing)

    def is_standing(self, f: Dict[str, float]) -> bool:
        return f["torso_angle"] < self.p.angle_standing

    def _to(self, state: FallState, now: float) -> None:
        self.state = state
        self.state_since = now
        self.standing_since = None
        if state != FallState.FALLING:
            self.lying_since = None

    def update(self, features: Optional[Dict[str, float]], now: float) -> FallState:
        """프레임마다 부른다. features가 None이면 사람이 보이지 않는 프레임이다.

        사람이 안 보이는 프레임은 '일어섬'으로 보지 않는다. 넘어지면서 가려졌는데 정상으로 돌아가지 않게 하기 위함이다.
        """
        if self.state == FallState.NORMAL:
            if features is not None and features["descent_velocity"] >= self.p.descent_peak:
                self._to(FallState.FALLING, now)
                self.lying_since = now if self.is_lying(features) else None

        elif self.state == FallState.FALLING:
            if features is not None and self.is_standing(features):
                # 쓰러지기 시작한 순간에는 몸통이 아직 서 있으므로 일어선 자세가 이어져야 정상으로 본다.
                self.lying_since = None
                if self.standing_since is None:
                    self.standing_since = now
                if now - self.standing_since >= self.p.standing_confirm:
                    self._to(FallState.NORMAL, now)
                    return self.state
            else:
                self.standing_since = None
                lying = self.is_lying(features) if features is not None else self.p.missing_counts_as_lying
                if lying:
                    if self.lying_since is None:
                        self.lying_since = now
                    elif now - self.lying_since >= self.p.lying_seconds:
                        self.suspect_features = dict(features) if features is not None else None
                        self._to(FallState.SUSPECT, now)
                    return self.state
                # 누운 자세가 끊기면 누움 시간을 처음부터 다시 잰다.
                self.lying_since = None
            if now - self.state_since >= self.p.falling_timeout:
                self._to(FallState.NORMAL, now)

        # SUSPECT는 reset()으로만 빠져나간다.
        return self.state

    def reset(self) -> None:
        self.suspect_features = None
        self._to(FallState.NORMAL, 0.0)
