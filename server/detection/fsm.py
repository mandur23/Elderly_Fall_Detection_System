"""낙상 의심 판정 상태 머신.

FSM은 '낙상 의심'까지만 판단한다. 음성 확인, 경보, 해제는 이벤트 서비스(server/events.py)가
맡고, FSM은 SUSPECT가 된 뒤 이벤트가 끝났다고 알려 줄 때(reset)까지 그대로 머문다.
이렇게 해야 같은 낙상으로 이벤트가 여러 번 만들어지지 않는다.

    NORMAL --하강 속도 피크--> FALLING --누운 자세 T1 연속 유지--> SUSPECT --reset()--> NORMAL
                                  |--다시 일어섬 / 시간 초과--> NORMAL

콜백은 두지 않는다. 호출 측이 update() 전후 상태를 비교해 전이를 알아낸다.
"""

from enum import Enum
from typing import Dict, Optional


class FallState(str, Enum):
    NORMAL = "NORMAL"
    FALLING = "FALLING"
    SUSPECT = "SUSPECT"


class FallFSM:
    def __init__(
        self,
        descent_peak: float,
        t1_lying_seconds: float,
        falling_timeout: float,
        angle_lying: float,
        aspect_lying: float,
        angle_standing: float,
        aspect_standing: float,
    ):
        self.descent_peak = descent_peak
        self.t1 = t1_lying_seconds
        self.falling_timeout = falling_timeout
        self.angle_lying = angle_lying
        self.aspect_lying = aspect_lying
        self.angle_standing = angle_standing
        self.aspect_standing = aspect_standing

        self.state = FallState.NORMAL
        self.state_since = 0.0
        self.lying_since: Optional[float] = None
        # SUSPECT로 넘어간 순간의 특징값. 이벤트 기록에 남기기 위해 보관한다.
        self.suspect_features: Optional[Dict[str, float]] = None

    @classmethod
    def from_settings(cls, settings) -> "FallFSM":
        """server.config.Settings의 판정 임계값으로 FSM을 만든다."""
        return cls(
            descent_peak=settings.descent_velocity_peak,
            t1_lying_seconds=settings.t1_lying_seconds,
            falling_timeout=settings.falling_timeout_seconds,
            angle_lying=settings.torso_angle_lying,
            aspect_lying=settings.aspect_ratio_lying,
            angle_standing=settings.torso_angle_standing,
            aspect_standing=settings.aspect_ratio_standing,
        )

    def is_lying(self, f: Dict[str, float]) -> bool:
        return f["torso_angle"] >= self.angle_lying or f["aspect_ratio"] >= self.aspect_lying

    def is_standing(self, f: Dict[str, float]) -> bool:
        return f["torso_angle"] < self.angle_standing and f["aspect_ratio"] < self.aspect_standing

    def _transition(self, new_state: FallState, now: float) -> None:
        if self.state == new_state:
            return
        self.state = new_state
        self.state_since = now
        if new_state != FallState.FALLING:
            self.lying_since = None

    def update(self, features: Optional[Dict[str, float]], now: float) -> FallState:
        """프레임마다 호출한다. features가 None이면 사람이 검출되지 않은 프레임이다.

        사람이 안 보이는 프레임은 '일어섬'으로 보지 않는다. 넘어지면서 가려지는 경우
        정상으로 잘못 복귀하지 않도록 하기 위함이다.
        """
        if self.state == FallState.NORMAL:
            if features is not None and features["descent_velocity"] >= self.descent_peak:
                self._transition(FallState.FALLING, now)
                self.lying_since = now if self.is_lying(features) else None

        elif self.state == FallState.FALLING:
            if features is not None and self.is_standing(features):
                self._transition(FallState.NORMAL, now)
            elif features is not None and self.is_lying(features):
                if self.lying_since is None:
                    self.lying_since = now
                elif now - self.lying_since >= self.t1:
                    self.suspect_features = dict(features)
                    self._transition(FallState.SUSPECT, now)
            else:
                # 누운 자세가 끊기면 T1을 처음부터 다시 잰다.
                self.lying_since = None
                if now - self.state_since >= self.falling_timeout:
                    self._transition(FallState.NORMAL, now)

        # SUSPECT는 이벤트 서비스의 reset() 호출로만 빠져나간다.
        return self.state

    def reset(self, now: float) -> None:
        """이벤트가 끝났을 때(음성 해제, 버튼 해제, 경보 해제) 감시 상태로 복귀한다."""
        self.suspect_features = None
        self._transition(FallState.NORMAL, now)
