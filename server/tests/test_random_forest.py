"""Random Forest's event transition and per-device history."""
import numpy as np
from server.detection.features import Keypoints
from server.detection.fsm import FallState
from server.detection.random_forest import RandomForestDetector

class Model:
    classes_ = [0, 1]
    def predict_proba(self, values):
        assert np.asarray(values).shape == (1, 42)
        return [[0.1, 0.9]]

def pose():
    return Keypoints((10,20),(20,20),(10,40),(20,40),(0,0,30,80),
                     np.array([[10,20],[20,20],[10,40],[20,40]]), image_width=100,image_height=100)

def test_detection_reset_and_isolation():
    first, second = RandomForestDetector(Model()), RandomForestDetector(Model())
    for i in range(20):
        first.update(pose(), i * 0.2)
    assert first.state == FallState.SUSPECT
    assert second.state == FallState.NORMAL and not second.history
    first.reset(5)
    assert first.state == FallState.NORMAL and first.probability is None

def test_missing_pose_clears_window():
    detector = RandomForestDetector(Model())
    for i in range(5): detector.update(pose(), i * 0.2)
    detector.update(None, 1.0)
    assert not detector.history and not detector.window and detector.probability is None
