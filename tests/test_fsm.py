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
                           angle_lying=60, aspect_lying=1.0, angle_standing=40,
                           on_state_change=lambda o, n, f: self.transitions.append(n))

    def test_upright_wide_stance_is_not_lying(self):
        # 실사진(달리는 자세)에서 측정한 값: 몸통 18도, 종횡비 1.07
        self.assertFalse(self.fsm.is_lying({"torso_angle": 18.0, "aspect_ratio": 1.07}))
        self.assertTrue(self.fsm.is_standing({"torso_angle": 18.0, "aspect_ratio": 1.07}))
        # 같은 사진을 90도 돌린 누운 자세: 몸통 72도, 종횡비 1.0
        self.assertTrue(self.fsm.is_lying({"torso_angle": 71.6, "aspect_ratio": 1.0}))
        # 몸통이 45도쯤 기울고 옆으로 넓게 퍼진 자세는 누움으로 본다
        self.assertTrue(self.fsm.is_lying({"torso_angle": 45.0, "aspect_ratio": 1.4}))

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


class FsmFallRulesTest(unittest.TestCase):
    """UR Fall 평가로 추가한 규칙: 일어섬 확인 시간, 하강 후 사라짐을 누움으로 보기."""

    def make(self, **kw):
        return FallFSM(descent_peak=2.0, t1_lying_seconds=2.0, falling_timeout=5.0,
                       angle_lying=60, aspect_lying=1.0, angle_standing=40, **kw)

    def test_upright_moment_at_fall_start_does_not_cancel(self):
        # 쓰러지기 시작한 순간에는 몸통이 아직 서 있다
        fsm = self.make(standing_confirm=0.5)
        fsm.update(PEAK_UPRIGHT, now=0.0)
        fsm.update(STAND, now=0.125)
        fsm.update(LYING, now=0.25)
        self.assertEqual(fsm.state, FallState.FALLING)
        fsm.update(LYING, now=2.25)
        self.assertEqual(fsm.state, FallState.SUSPECT)

    def test_standing_for_confirm_time_returns_normal(self):
        fsm = self.make(standing_confirm=0.5)
        fsm.update(PEAK_UPRIGHT, now=0.0)
        fsm.update(STAND, now=0.25)
        self.assertEqual(fsm.state, FallState.FALLING)
        fsm.update(STAND, now=0.75)
        self.assertEqual(fsm.state, FallState.NORMAL)

    def test_missing_after_descent_counts_as_lying(self):
        fsm = self.make(missing_counts_as_lying=True)
        fsm.update(PEAK_LYING, now=0.0)
        fsm.update(None, now=1.0)  # 화면 아래로 벗어나 자세 추정 실패
        self.assertEqual(fsm.state, FallState.FALLING)
        fsm.update(None, now=2.0)
        self.assertEqual(fsm.state, FallState.SUSPECT)
        self.assertIsNone(fsm.suspect_features)

    def test_missing_without_descent_stays_normal(self):
        fsm = self.make(missing_counts_as_lying=True)
        for t in range(10):  # 화면 밖으로 걸어 나간 경우
            fsm.update(None, now=t)
        self.assertEqual(fsm.state, FallState.NORMAL)

    def test_bent_posture_still_resets_lying_time(self):
        fsm = self.make(missing_counts_as_lying=True)
        fsm.update(PEAK_LYING, now=0.0)
        fsm.update(None, now=1.5)
        fsm.update(BENT, now=1.8)  # 누움도 사라짐도 아님 -> T1 다시 측정
        fsm.update(LYING, now=2.1)
        self.assertEqual(fsm.state, FallState.FALLING)


if __name__ == "__main__":
    unittest.main()
