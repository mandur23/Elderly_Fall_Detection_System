import unittest

from l3_perception.fsm import FallFSM, FallState

STAND = {"descent_velocity": 0.0, "torso_angle": 10.0, "aspect_ratio": 0.4}
LYING = {"descent_velocity": 0.0, "torso_angle": 80.0, "aspect_ratio": 1.5}
BENT = {"descent_velocity": 0.0, "torso_angle": 50.0, "aspect_ratio": 0.9}  # 누움도 기립도 아님
PEAK_LYING = dict(LYING, descent_velocity=4.0)
PEAK_UPRIGHT = dict(STAND, descent_velocity=3.0)


class FsmTest(unittest.TestCase):
    def setUp(self):
        self.transitions = []
        self.fsm = FallFSM(descent_peak=2.0, t1_lying_seconds=2.0, falling_timeout=5.0,
                           angle_lying=60, aspect_lying=1.0, angle_standing=40, aspect_standing=0.8,
                           on_state_change=lambda o, n, f: self.transitions.append(n))

    def test_slow_lying_without_peak_stays_normal(self):
        for t in range(10):
            self.fsm.update(LYING, now=t)
        self.assertEqual(self.fsm.state, FallState.NORMAL)

    def test_peak_then_stand_up_returns_normal(self):
        self.fsm.update(PEAK_UPRIGHT, now=0.0)
        self.assertEqual(self.fsm.state, FallState.FALLING)
        self.fsm.update(STAND, now=0.5)
        self.assertEqual(self.fsm.state, FallState.NORMAL)

    def test_peak_then_lying_for_t1_is_suspect(self):
        self.fsm.update(PEAK_LYING, now=0.0)
        self.fsm.update(LYING, now=1.0)
        self.assertEqual(self.fsm.state, FallState.FALLING)
        self.fsm.update(LYING, now=2.0)
        self.assertEqual(self.fsm.state, FallState.SUSPECT)
        self.assertEqual(self.transitions, [FallState.FALLING, FallState.SUSPECT])

    def test_lying_must_be_continuous(self):
        self.fsm.update(PEAK_LYING, now=0.0)
        self.fsm.update(LYING, now=1.5)
        self.fsm.update(BENT, now=1.8)   # 누움이 끊김 -> T1 다시 측정
        self.fsm.update(LYING, now=2.1)
        self.assertEqual(self.fsm.state, FallState.FALLING)
        self.fsm.update(LYING, now=4.2)
        self.assertEqual(self.fsm.state, FallState.SUSPECT)

    def test_missing_person_is_not_standing(self):
        self.fsm.update(PEAK_LYING, now=0.0)
        self.fsm.update(None, now=0.5)
        self.assertEqual(self.fsm.state, FallState.FALLING)

    def test_falling_times_out(self):
        self.fsm.update(PEAK_UPRIGHT, now=0.0)
        self.fsm.update(None, now=4.9)
        self.assertEqual(self.fsm.state, FallState.FALLING)
        self.fsm.update(None, now=5.0)
        self.assertEqual(self.fsm.state, FallState.NORMAL)

    def test_suspect_holds_until_reset(self):
        self.fsm.update(PEAK_LYING, now=0.0)
        self.fsm.update(LYING, now=2.0)
        for t in range(3, 60):  # 오래 지나도, 일어서도 스스로 경보·복귀하지 않는다
            self.fsm.update(STAND if t % 2 else LYING, now=t)
        self.assertEqual(self.fsm.state, FallState.SUSPECT)
        self.fsm.reset(now=60)
        self.assertEqual(self.fsm.state, FallState.NORMAL)
        self.fsm.update(PEAK_LYING, now=61)
        self.assertEqual(self.fsm.state, FallState.FALLING)


if __name__ == "__main__":
    unittest.main()
