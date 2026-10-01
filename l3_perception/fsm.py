"""L3 낙상 의심 판정 상태 머신.

L3는 '낙상 의심'까지만 판단한다. 음성 확인, 경보, 해제는 L4가 관리하며,
L3는 SUSPECT를 보고한 뒤 L4가 이벤트를 끝냈다고 알려 줄 때(reset)까지 대기한다.
이렇게 해야 두 프로세스의 상태가 어긋나지 않고 같은 상황에서 알림이 반복되지 않는다.

    NORMAL --하강 속도 피크--> FALLING --누운 자세 T1 연속 유지--> SUSPECT --reset()--> NORMAL
                                  |--다시 일어섬 / 시간 초과--> NORMAL
"""

from enum import Enum
from typing import Callable, Dict, Optional


class FallState(str, Enum):
    NORMAL = "NORMAL"
    FALLING = "FALLING"
    SUSPECT = "SUSPECT"


StateCallback = Callable[[FallState, FallState, Optional[Dict[str, float]]], None]


class FallFSM:
    def __init__(
        self,
        descent_peak: float,
        t1_lying_seconds: float,
        falling_timeout: float,
        angle_lying: float,
        aspect_lying: float,
        angle_standing: float,
        on_state_change: Optional[StateCallback] = None,
    ):
        self.descent_peak = descent_peak
        self.t1 = t1_lying_seconds
        self.falling_timeout = falling_timeout
        self.angle_lying = angle_lying
        self.aspect_lying = aspect_lying
        self.angle_standing = angle_standing
        self.on_state_change = on_state_change

        self.state = FallState.NORMAL
        self.state_since = 0.0
        self.lying_since: Optional[float] = None
        self.suspect_features: Optional[Dict[str, float]] = None

    def is_lying(self, f: Dict[str, float]) -> bool:
        """몸통 각도가 주 기준이다. 종횡비는 몸통도 어느 정도 기울었을 때만 보조로 쓴다.

        실사진 확인 결과, 다리를 벌리고 선 자세는 종횡비가 1을 넘어 종횡비만으로는 누움과 구분되지 않았다.
        """
        angle = f["torso_angle"]
        return angle >= self.angle_lying or (f["aspect_ratio"] >= self.aspect_lying and angle >= self.angle_standing)

    def is_standing(self, f: Dict[str, float]) -> bool:
        return f["torso_angle"] < self.angle_standing

    def _transition(self, new_state: FallState, now: float, features: Optional[Dict[str, float]]) -> None:
        old_state = self.state
        if old_state == new_state:
            return
        self.state = new_state
        self.state_since = now
        if new_state != FallState.FALLING:
            self.lying_since = None
        if self.on_state_change:
            self.on_state_change(old_state, new_state, features)

    def update(self, features: Optional[Dict[str, float]], now: float) -> FallState:
        """프레임마다 호출한다. features가 None이면 사람이 검출되지 않은 프레임이다.

        사람이 안 보이는 프레임은 '일어섬'으로 보지 않는다. 넘어지면서 가려지는 경우
        정상으로 잘못 복귀하지 않도록 하기 위함이다.
        """
        if self.state == FallState.NORMAL:
            if features is not None and features["descent_velocity"] >= self.descent_peak:
                self._transition(FallState.FALLING, now, features)
                self.lying_since = now if self.is_lying(features) else None

        elif self.state == FallState.FALLING:
            if features is not None and self.is_standing(features):
                self._transition(FallState.NORMAL, now, features)
            elif features is not None and self.is_lying(features):
                if self.lying_since is None:
                    self.lying_since = now
                elif now - self.lying_since >= self.t1:
                    self.suspect_features = dict(features)
                    self._transition(FallState.SUSPECT, now, features)
            else:
                # 누운 자세가 끊기면 T1을 처음부터 다시 잰다.
                self.lying_since = None
                if now - self.state_since >= self.falling_timeout:
                    self._transition(FallState.NORMAL, now, features)

        # SUSPECT는 L4의 reset() 호출로만 빠져나간다.
        return self.state

    def reset(self, now: float) -> None:
        """L4가 이벤트를 끝냈을 때(해제 또는 경보 해제) 감시 상태로 복귀한다."""
        self.suspect_features = None
        self._transition(FallState.NORMAL, now, None)
