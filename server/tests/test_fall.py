import unittest

from server.config import Settings
from server.fall import FallFSM, FallParams, FallState


def feat(angle: float, aspect: float = 0.4, v: float = 0.0):
    return {"torso_angle": angle, "aspect_ratio": aspect, "descent_velocity": v}


STAND, LIE, FAST = feat(5), feat(85, 2.0), feat(5, v=3.0)


def run(fsm: FallFSM, frames, t0: float = 0.0, dt: float = 0.1):
    """frames: [(특징 또는 None, 개수)]. 마지막 상태와 다음 시각을 돌려준다."""
    t = t0
    state = fsm.state
    for f, n in frames:
        for _ in range(n):
            state = fsm.update(f, t)
            t += dt
    return state, t


class FallFSMTest(unittest.TestCase):
    def setUp(self):
        self.fsm = FallFSM(FallParams.from_settings(Settings()))

    def test_defaults_are_evaluated_values(self):
        p = self.fsm.p
        self.assertEqual((p.descent_peak, p.lying_seconds, p.standing_confirm, p.missing_counts_as_lying),
                         (1.5, 2.0, 0.5, True))

    def test_fall_then_lying_two_seconds_is_suspect(self):
        state, _ = run(self.fsm, [(STAND, 5), (FAST, 1), (LIE, 20)])  # 누움 1.9초까지
        self.assertEqual(state, FallState.FALLING)
        state, _ = run(self.fsm, [(LIE, 2)], t0=2.6)
        self.assertEqual(state, FallState.SUSPECT)
        self.assertEqual(self.fsm.suspect_features, LIE)

    def test_descent_below_threshold_is_ignored(self):
        state, _ = run(self.fsm, [(feat(5, v=1.4), 1), (LIE, 40)])
        self.assertEqual(state, FallState.NORMAL)

    def test_missing_person_counts_as_lying(self):
        state, _ = run(self.fsm, [(FAST, 1), (None, 22)])
        self.assertEqual(state, FallState.SUSPECT)

    def test_missing_person_not_counted_when_disabled(self):
        fsm = FallFSM(FallParams(missing_counts_as_lying=False))
        state, _ = run(fsm, [(FAST, 1), (None, 30)])
        self.assertNotEqual(state, FallState.SUSPECT)

    def test_standing_up_for_half_second_returns_to_normal(self):
        state, _ = run(self.fsm, [(FAST, 1), (LIE, 5), (STAND, 7)])
        self.assertEqual(state, FallState.NORMAL)

    def test_brief_standing_does_not_cancel_fall(self):
        # 쓰러지기 시작한 순간에는 몸통이 아직 서 있다. 0.5초가 안 되면 정상으로 돌아가지 않는다.
        state, _ = run(self.fsm, [(FAST, 1), (STAND, 3), (LIE, 22)])
        self.assertEqual(state, FallState.SUSPECT)

    def test_lying_interrupted_restarts_timer(self):
        mid = feat(50, 0.5)  # 누움도 일어섬도 아님
        state, _ = run(self.fsm, [(FAST, 1), (LIE, 15), (mid, 1), (LIE, 15)])
        self.assertEqual(state, FallState.FALLING)

    def test_timeout_returns_to_normal(self):
        mid = feat(50, 0.5)
        state, _ = run(self.fsm, [(FAST, 1), (mid, 51)])
        self.assertEqual(state, FallState.NORMAL)

    def test_suspect_stays_until_reset(self):
        run(self.fsm, [(FAST, 1), (LIE, 22)])
        state, _ = run(self.fsm, [(STAND, 20), (FAST, 1)], t0=10)
        self.assertEqual(state, FallState.SUSPECT)
        self.fsm.reset()
        self.assertEqual(self.fsm.state, FallState.NORMAL)
        self.assertIsNone(self.fsm.suspect_features)
        state, _ = run(self.fsm, [(FAST, 1), (LIE, 22)], t0=20)
        self.assertEqual(state, FallState.SUSPECT)

    def test_tilted_with_wide_box_is_lying(self):
        # 몸통이 일어섬 기준(40도) 이상 기울었고 종횡비가 1 이상이면 누운 자세
        self.assertTrue(self.fsm.is_lying(feat(45, 1.2)))
        self.assertFalse(self.fsm.is_lying(feat(30, 1.5)))  # 다리 벌리고 선 자세


if __name__ == "__main__":
    unittest.main()
