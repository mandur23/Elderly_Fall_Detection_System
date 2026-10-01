import unittest

import numpy as np

from l3_perception.features import FeatureExtractor, Keypoints, aspect_ratio, torso_angle


def kp(hip_y, torso=100.0, lying=False):
    """서 있으면 어깨가 엉덩이 위, 누우면 어깨가 엉덩이 왼쪽에 있는 단순 관절."""
    hip = (300.0, hip_y)
    sh = (300.0 - torso, hip_y) if lying else (300.0, hip_y - torso)
    pts = np.array([sh, hip])
    return Keypoints(sh, sh, hip, hip, (*pts.min(axis=0), *pts.max(axis=0)), pts)


class FeatureTest(unittest.TestCase):
    def test_aspect_ratio(self):
        self.assertLess(aspect_ratio((100, 50, 200, 350)), 1.0)
        self.assertGreaterEqual(aspect_ratio((50, 300, 400, 420)), 1.0)

    def test_torso_angle(self):
        self.assertAlmostEqual(torso_angle(np.array([100, 100]), np.array([100, 200])), 0.0)
        self.assertAlmostEqual(torso_angle(np.array([100, 300]), np.array([250, 300])), 90.0)
        self.assertAlmostEqual(torso_angle(np.array([0, 0]), np.array([100, 100])), 45.0)

    def test_descent_velocity_uses_real_time_and_torso_length(self):
        ex = FeatureExtractor(velocity_span=1)
        ex.extract(kp(300), t=0.0)
        f = ex.extract(kp(340), t=0.1)  # 0.1초에 40px = 400px/s, 몸통 100px -> 4.0
        self.assertAlmostEqual(f["descent_velocity"], 4.0, places=2)

        ex.reset()
        ex.extract(kp(300), t=0.0)
        f = ex.extract(kp(340), t=0.4)  # 같은 거리라도 프레임 간격이 길면 느리다
        self.assertAlmostEqual(f["descent_velocity"], 1.0, places=2)

    def test_velocity_is_scale_invariant(self):
        near, far = FeatureExtractor(velocity_span=1), FeatureExtractor(velocity_span=1)
        near.extract(kp(300, torso=200), 0.0)
        far.extract(kp(300, torso=50), 0.0)
        v_near = near.extract(kp(380, torso=200), 0.1)["descent_velocity"]
        v_far = far.extract(kp(320, torso=50), 0.1)["descent_velocity"]
        self.assertAlmostEqual(v_near, v_far, places=2)

    def test_reference_torso_survives_lying(self):
        ex = FeatureExtractor(velocity_span=1)
        ex.extract(kp(300, torso=100), 0.0)
        # 누워서 화면상 몸통이 짧아져도 서 있을 때 길이를 기준으로 쓴다.
        f = ex.extract(kp(340, torso=20, lying=True), 0.1)
        self.assertAlmostEqual(f["descent_velocity"], 4.0, places=2)


if __name__ == "__main__":
    unittest.main()
