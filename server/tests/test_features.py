import unittest

import numpy as np

from server.features import FeatureExtractor, aspect_ratio, torso_angle
from server.pose import ARMS, keypoints_from_landmarks
from server.tests.fakes import lying, standing


class GeometryTest(unittest.TestCase):
    def test_torso_angle(self):
        self.assertAlmostEqual(torso_angle((0, 0), (0, 100)), 0.0)
        self.assertAlmostEqual(torso_angle((0, 0), (100, 0)), 90.0)
        self.assertAlmostEqual(torso_angle((0, 0), (100, 100)), 45.0)
        self.assertAlmostEqual(torso_angle((0, 100), (0, 0)), 0.0)  # 거꾸로여도 0~90도
        self.assertEqual(torso_angle((5, 5), (5, 5)), 0.0)

    def test_aspect_ratio(self):
        self.assertAlmostEqual(aspect_ratio((0, 0, 100, 200)), 0.5)
        self.assertAlmostEqual(aspect_ratio((0, 0, 300, 100)), 3.0)
        self.assertGreater(aspect_ratio((0, 0, 5, 0)), 1.0)  # 높이 0도 나누기 오류 없음

    def test_standing_and_lying_features(self):
        ex = FeatureExtractor()
        f = ex.extract(standing(), 0.0)
        self.assertLess(f["torso_angle"], 40)
        self.assertLess(f["aspect_ratio"], 1)
        f = ex.extract(lying(), 1.0)
        self.assertGreaterEqual(f["torso_angle"], 60)
        self.assertGreaterEqual(f["aspect_ratio"], 1)


class DescentVelocityTest(unittest.TestCase):
    def peak(self, fps: float) -> float:
        """엉덩이가 0.3초 동안 몸통 길이(150px)만큼 내려가는 낙상의 최고 하강 속도."""
        ex = FeatureExtractor(velocity_window=0.3)
        dt, best = 1.0 / fps, 0.0
        for i in range(int(2 * fps)):
            t = i * dt
            hip = 250 + 150 * min(1.0, max(0.0, (t - 1.0) / 0.3))
            best = max(best, ex.extract(standing(hip_y=hip), t)["descent_velocity"])
        return best

    def test_same_value_at_different_fps(self):
        # 프레임 수가 아니라 시간 구간으로 재므로 처리 속도가 달라도 값이 같아야 한다(docs/EVALUATION.md 1번).
        v10, v30 = self.peak(10), self.peak(30)
        self.assertAlmostEqual(v10, 150 / 0.3 / 150, delta=0.15)
        self.assertAlmostEqual(v10, v30, delta=0.15)

    def test_still_is_zero_and_rising_is_negative(self):
        ex = FeatureExtractor()
        for i in range(5):
            self.assertEqual(ex.extract(standing(), i * 0.1)["descent_velocity"], 0.0)
        ex.reset()
        ex.extract(standing(hip_y=300), 0.0)
        self.assertLess(ex.extract(standing(hip_y=200), 0.3)["descent_velocity"], 0)

    def test_reference_torso_length_is_recent_max(self):
        # 누우면 화면상 몸통이 짧아져도 서 있을 때 길이로 나눈다.
        ex = FeatureExtractor()
        ex.extract(standing(hip_y=250), 0.0)
        v = ex.extract(lying(y=400), 0.3)["descent_velocity"]
        self.assertAlmostEqual(v, (400 - 250) / 0.3 / 150, delta=0.01)

    def test_old_history_is_dropped(self):
        ex = FeatureExtractor(history_seconds=1.5)
        ex.extract(standing(hip_y=100), 0.0)
        ex.extract(standing(hip_y=100), 5.0)
        self.assertEqual(len(ex._history), 1)


class LandmarksTest(unittest.TestCase):
    def landmarks(self):
        points = np.zeros((33, 2))
        conf = np.full(33, 0.9)
        points[11], points[12] = (300, 100), (340, 100)  # 어깨
        points[23], points[24] = (305, 250), (335, 250)  # 엉덩이
        points[0] = (320, 60)                            # 코
        points[27], points[28] = (310, 450), (330, 450)  # 발목
        for i in range(1, 33):
            if not points[i].any():
                points[i] = (320, 200)
        return points, conf

    def test_bbox_excludes_arms(self):
        points, conf = self.landmarks()
        points[15], points[16] = (50, 120), (600, 120)  # 팔을 양옆으로 쭉 벌림
        kp = keypoints_from_landmarks(points, conf, 0.5)
        self.assertIsNotNone(kp)
        self.assertGreater(kp.bbox[0], 100)
        self.assertLess(kp.bbox[2], 500)
        self.assertIn(15, ARMS)

    def test_low_confidence_core_point_is_rejected(self):
        points, conf = self.landmarks()
        conf[23] = 0.2
        self.assertIsNone(keypoints_from_landmarks(points, conf, 0.5))


if __name__ == "__main__":
    unittest.main()
