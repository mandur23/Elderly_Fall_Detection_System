"""영상 분석 모듈 테스트: 특징 계산, 낙상 FSM, 자세 추정 백엔드."""

import json
import sys
import types

import cv2
import numpy as np
import pytest

from server.config import Settings
from server.detection import (
    FakePoseEstimator,
    FallFSM,
    FallState,
    FeatureExtractor,
    Keypoints,
    PoseEstimatorError,
    YoloPoseEstimator,
    aspect_ratio,
    create_pose_estimator,
    fake_frame,
    torso_angle,
)


# ---------------------------------------------------------------- 특징 계산

def kp(hip_y, torso=100.0, lying=False):
    """서 있으면 어깨가 엉덩이 위, 누우면 어깨가 엉덩이 왼쪽에 있는 단순 관절."""
    hip = (300.0, hip_y)
    sh = (300.0 - torso, hip_y) if lying else (300.0, hip_y - torso)
    pts = np.array([sh, hip])
    return Keypoints(sh, sh, hip, hip, (*pts.min(axis=0), *pts.max(axis=0)), pts)


def test_aspect_ratio():
    assert aspect_ratio((100, 50, 200, 350)) < 1.0      # 세로로 긴 박스 = 서 있음
    assert aspect_ratio((50, 300, 400, 420)) >= 1.0     # 가로로 긴 박스 = 누움


def test_aspect_ratio_degenerate_box_does_not_divide_by_zero():
    assert aspect_ratio((10, 10, 10, 10)) == 1.0


def test_torso_angle():
    assert torso_angle(np.array([100, 100]), np.array([100, 200])) == pytest.approx(0.0)
    assert torso_angle(np.array([100, 300]), np.array([250, 300])) == pytest.approx(90.0)
    assert torso_angle(np.array([0, 0]), np.array([100, 100])) == pytest.approx(45.0)
    # 거꾸로(머리가 아래) 있어도 0~90도 범위
    assert torso_angle(np.array([100, 200]), np.array([100, 100])) == pytest.approx(0.0)


def test_descent_velocity_uses_real_time_and_torso_length():
    ex = FeatureExtractor(velocity_span=1)
    ex.extract(kp(300), t=0.0)
    f = ex.extract(kp(340), t=0.1)  # 0.1초에 40px = 400px/s, 몸통 100px -> 4.0
    assert f["descent_velocity"] == pytest.approx(4.0, abs=0.01)

    ex.reset()
    ex.extract(kp(300), t=0.0)
    f = ex.extract(kp(340), t=0.4)  # 같은 거리라도 프레임 간격이 길면 느리다
    assert f["descent_velocity"] == pytest.approx(1.0, abs=0.01)


def test_first_frame_and_same_timestamp_give_zero_velocity():
    ex = FeatureExtractor(velocity_span=1)
    assert ex.extract(kp(300), t=1.0)["descent_velocity"] == 0.0
    assert ex.extract(kp(400), t=1.0)["descent_velocity"] == 0.0  # dt=0이면 계산하지 않는다


def test_velocity_is_scale_invariant():
    near, far = FeatureExtractor(velocity_span=1), FeatureExtractor(velocity_span=1)
    near.extract(kp(300, torso=200), 0.0)
    far.extract(kp(300, torso=50), 0.0)
    v_near = near.extract(kp(380, torso=200), 0.1)["descent_velocity"]
    v_far = far.extract(kp(320, torso=50), 0.1)["descent_velocity"]
    assert v_near == pytest.approx(v_far, abs=0.01)


def test_reference_torso_survives_lying():
    ex = FeatureExtractor(velocity_span=1)
    ex.extract(kp(300, torso=100), 0.0)
    # 누워서 화면상 몸통이 짧아져도 서 있을 때 길이를 기준으로 쓴다.
    f = ex.extract(kp(340, torso=20, lying=True), 0.1)
    assert f["descent_velocity"] == pytest.approx(4.0, abs=0.01)


def test_velocity_span_looks_back_several_frames():
    ex = FeatureExtractor(velocity_span=3)
    for i, t in enumerate([0.0, 0.1, 0.2, 0.3]):
        f = ex.extract(kp(300 + 10 * i), t)
    # 3프레임 전(0.0초, 300px) -> 지금(0.3초, 330px): 100px/s / 몸통 100px = 1.0
    assert f["descent_velocity"] == pytest.approx(1.0, abs=0.01)


def test_motion_quantity_and_reset():
    ex = FeatureExtractor()
    assert ex.extract(kp(300), 0.0)["motion_quantity"] == 0.0
    # 두 점 모두 10px 이동, 몸통 100px -> 0.1
    assert ex.extract(kp(310), 0.1)["motion_quantity"] == pytest.approx(0.1, abs=0.001)
    ex.reset()
    f = ex.extract(kp(500), 0.2)   # 기록이 지워졌으니 큰 이동도 첫 프레임처럼 0
    assert f["descent_velocity"] == 0.0 and f["motion_quantity"] == 0.0


# ---------------------------------------------------------------- 낙상 FSM

STAND = {"descent_velocity": 0.0, "torso_angle": 10.0, "aspect_ratio": 0.4}
LYING = {"descent_velocity": 0.0, "torso_angle": 80.0, "aspect_ratio": 1.5}
BENT = {"descent_velocity": 0.0, "torso_angle": 50.0, "aspect_ratio": 0.9}  # 누움도 기립도 아님
PEAK_LYING = dict(LYING, descent_velocity=4.0)
PEAK_UPRIGHT = dict(STAND, descent_velocity=3.0)


@pytest.fixture
def fsm():
    return FallFSM(descent_peak=2.0, t1_lying_seconds=2.0, falling_timeout=5.0,
                   angle_lying=60, aspect_lying=1.0, angle_standing=40, aspect_standing=0.8)


def test_slow_lying_without_peak_stays_normal(fsm):
    for t in range(10):
        fsm.update(LYING, now=t)
    assert fsm.state == FallState.NORMAL


def test_peak_then_stand_up_returns_normal(fsm):
    assert fsm.update(PEAK_UPRIGHT, now=0.0) == FallState.FALLING
    assert fsm.update(STAND, now=0.5) == FallState.NORMAL


def test_peak_then_lying_for_t1_is_suspect(fsm):
    states = [fsm.update(PEAK_LYING, now=0.0), fsm.update(LYING, now=1.0), fsm.update(LYING, now=2.0)]
    assert states == [FallState.FALLING, FallState.FALLING, FallState.SUSPECT]
    assert fsm.suspect_features == LYING


def test_lying_must_be_continuous(fsm):
    fsm.update(PEAK_LYING, now=0.0)
    fsm.update(LYING, now=1.5)
    fsm.update(BENT, now=1.8)   # 누움이 끊김 -> T1 다시 측정
    fsm.update(LYING, now=2.1)
    assert fsm.state == FallState.FALLING
    fsm.update(LYING, now=4.2)
    assert fsm.state == FallState.SUSPECT


def test_missing_person_is_not_standing(fsm):
    fsm.update(PEAK_LYING, now=0.0)
    fsm.update(None, now=0.5)
    assert fsm.state == FallState.FALLING


def test_missing_person_interrupts_lying_timer(fsm):
    fsm.update(PEAK_LYING, now=0.0)
    fsm.update(None, now=1.0)       # 가려짐도 '누움 연속'을 끊는다
    fsm.update(LYING, now=1.5)
    fsm.update(LYING, now=2.5)
    assert fsm.state == FallState.FALLING
    fsm.update(LYING, now=3.5)
    assert fsm.state == FallState.SUSPECT


def test_falling_times_out(fsm):
    fsm.update(PEAK_UPRIGHT, now=0.0)
    fsm.update(None, now=4.9)
    assert fsm.state == FallState.FALLING
    fsm.update(None, now=5.0)
    assert fsm.state == FallState.NORMAL


def test_suspect_holds_until_reset(fsm):
    fsm.update(PEAK_LYING, now=0.0)
    fsm.update(LYING, now=2.0)
    for t in range(3, 60):  # 오래 지나도, 일어서도 스스로 경보·복귀하지 않는다
        fsm.update(STAND if t % 2 else LYING, now=t)
    assert fsm.state == FallState.SUSPECT
    fsm.reset(now=60)
    assert fsm.state == FallState.NORMAL
    assert fsm.suspect_features is None
    fsm.update(PEAK_LYING, now=61)
    assert fsm.state == FallState.FALLING


def test_from_settings_uses_configured_thresholds():
    s = Settings(descent_velocity_peak=3.5, t1_lying_seconds=0.3, falling_timeout_seconds=7.0,
                 torso_angle_lying=55.0, aspect_ratio_lying=1.2,
                 torso_angle_standing=35.0, aspect_ratio_standing=0.7)
    f = FallFSM.from_settings(s)
    assert (f.descent_peak, f.t1, f.falling_timeout) == (3.5, 0.3, 7.0)
    assert (f.angle_lying, f.aspect_lying, f.angle_standing, f.aspect_standing) == (55.0, 1.2, 35.0, 0.7)
    assert f.state == FallState.NORMAL


def test_fall_state_is_plain_string_for_json():
    assert json.dumps({"state": FallState.SUSPECT}) == '{"state": "SUSPECT"}'


# ---------------------------------------------------------------- 가짜 추정기 / 가짜 프레임

def test_fake_pose_parses_keypoints():
    frame = json.dumps({"ls": [1, 2], "rs": [3, 4], "lh": [5, 6], "rh": [7, 8],
                        "bbox": [0, 0, 10, 20]}).encode()
    k = FakePoseEstimator().estimate(frame)
    assert k.left_shoulder == (1.0, 2.0) and k.right_hip == (7.0, 8.0)
    assert k.bbox == (0.0, 0.0, 10.0, 20.0)
    assert k.all_points.shape == (4, 2)
    assert list(k.shoulder_center) == [2.0, 3.0]


@pytest.mark.parametrize("frame", [
    b'{"none": true}',
    b"not json",
    b"\xff\xd8\xff",                      # 진짜 JPEG 앞부분
    b'{"ls": [1, 2]}',                    # 필드 누락
    b"[1, 2, 3]",
])
def test_fake_pose_returns_none_for_no_person_or_bad_frame(frame):
    assert FakePoseEstimator().estimate(frame) is None


def test_fake_frame_round_trip():
    est, ex = FakePoseEstimator(), FeatureExtractor()
    standing = ex.extract(est.estimate(fake_frame("standing")), 0.0)
    assert standing["torso_angle"] < 40 and standing["aspect_ratio"] < 0.8
    lying = ex.extract(est.estimate(fake_frame("lying")), 1.0)
    assert lying["torso_angle"] >= 60 and lying["aspect_ratio"] >= 1.0
    assert est.estimate(fake_frame("none")) is None

    moved = est.estimate(fake_frame("standing", offset_y=50))
    assert moved.hip_center[1] == pytest.approx(est.estimate(fake_frame("standing")).hip_center[1] + 50)


def test_fake_frame_rejects_unknown_kind():
    with pytest.raises(ValueError):
        fake_frame("sitting")


def test_fake_frames_through_pipeline_reach_suspect():
    """다른 모듈 테스트가 쓰는 순서: 서 있음 -> 빠른 하강 -> 누운 채 T1 유지."""
    est, ex = FakePoseEstimator(), FeatureExtractor()
    fsm = FallFSM.from_settings(Settings(t1_lying_seconds=0.3))
    t = 0.0
    frames = [fake_frame("standing")] * 5 + [fake_frame("standing", offset_y=dy) for dy in (30, 60, 90)]
    frames += [fake_frame("lying")] * 6
    states = []
    for frame in frames:
        k = est.estimate(frame)
        states.append(fsm.update(ex.extract(k, t) if k else None, t))
        t += 0.1
    assert FallState.FALLING in states
    assert states[-1] == FallState.SUSPECT


def test_slow_sit_down_with_fake_frames_stays_normal():
    est, ex = FakePoseEstimator(), FeatureExtractor()
    fsm = FallFSM.from_settings(Settings(t1_lying_seconds=0.3))
    for i in range(30):  # 3초에 걸쳐 30px 내려감 = 0.1 몸통/초
        k = est.estimate(fake_frame("standing", offset_y=i))
        assert fsm.update(ex.extract(k, i * 0.1), i * 0.1) == FallState.NORMAL


def test_create_pose_estimator():
    assert isinstance(create_pose_estimator(Settings(pose_backend="fake")), FakePoseEstimator)
    with pytest.raises(PoseEstimatorError):
        create_pose_estimator(Settings(pose_backend="mediapipe"))


# ---------------------------------------------------------------- YOLO 추정기 (ultralytics 가짜 모듈)

class _Boxes:
    def __init__(self, conf):
        self.conf = np.asarray(conf, dtype=float)

    def __len__(self):
        return len(self.conf)


def _person(dx, conf=0.9):
    """COCO 17점. 어깨(5,6)는 y=100, 엉덩이(11,12)는 y=200."""
    pts = np.zeros((17, 2))
    pts[:, 0] = 100 + dx
    pts[:, 1] = np.linspace(50, 300, 17)
    pts[5], pts[6] = [90 + dx, 100], [110 + dx, 100]
    pts[11], pts[12] = [95 + dx, 200], [105 + dx, 200]
    return pts, np.full(17, conf)


def _install_fake_ultralytics(monkeypatch, people, box_conf, fail_load=False):
    calls = []

    class YOLO:
        def __init__(self, path):
            if fail_load:
                raise FileNotFoundError(path)
            self.path = path

        def __call__(self, frame, verbose=False):
            calls.append(frame.shape)
            if not people:
                return [types.SimpleNamespace(keypoints=None, boxes=_Boxes([]))]
            xy = np.stack([p[0] for p in people])
            conf = np.stack([p[1] for p in people])
            kps = types.SimpleNamespace(xy=xy, conf=conf)
            return [types.SimpleNamespace(keypoints=kps, boxes=_Boxes(box_conf))]

    monkeypatch.setitem(sys.modules, "ultralytics", types.SimpleNamespace(YOLO=YOLO))
    return calls


def _jpeg():
    ok, buf = cv2.imencode(".jpg", np.zeros((48, 64, 3), dtype=np.uint8))
    assert ok
    return buf.tobytes()


def test_yolo_missing_package_raises(monkeypatch):
    monkeypatch.setitem(sys.modules, "ultralytics", None)  # import 시 ImportError
    with pytest.raises(PoseEstimatorError):
        YoloPoseEstimator("yolov8n-pose.pt", 0.5)


def test_yolo_model_load_failure_raises(monkeypatch):
    _install_fake_ultralytics(monkeypatch, [], [], fail_load=True)
    with pytest.raises(PoseEstimatorError):
        YoloPoseEstimator("missing.pt", 0.5)


def test_yolo_picks_most_confident_person(monkeypatch):
    calls = _install_fake_ultralytics(monkeypatch, [_person(0), _person(500)], box_conf=[0.4, 0.8])
    k = YoloPoseEstimator("m.pt", 0.5).estimate(_jpeg())
    assert calls == [(48, 64, 3)]               # JPEG를 디코딩해서 모델에 넘긴다
    assert k.left_shoulder == (590.0, 100.0)    # 두 번째 사람(신뢰도 0.8)
    assert k.hip_center.tolist() == [600.0, 200.0]
    assert k.confidence == pytest.approx(0.9)


def test_yolo_low_confidence_core_point_means_no_person(monkeypatch):
    pts, conf = _person(0)
    conf[11] = 0.2                              # 왼쪽 엉덩이가 불확실
    _install_fake_ultralytics(monkeypatch, [(pts, conf)], box_conf=[0.9])
    assert YoloPoseEstimator("m.pt", 0.5).estimate(_jpeg()) is None


def test_yolo_bbox_uses_only_confident_points(monkeypatch):
    pts, conf = _person(0)
    pts[0] = [-1000, -1000]                     # 엉뚱한 위치의 낮은 신뢰도 점은 박스에서 뺀다
    conf[0] = 0.1
    _install_fake_ultralytics(monkeypatch, [(pts, conf)], box_conf=[0.9])
    k = YoloPoseEstimator("m.pt", 0.5).estimate(_jpeg())
    assert k.bbox[0] > 0 and k.all_points.shape == (16, 2)


def test_yolo_no_person_and_bad_jpeg(monkeypatch):
    calls = _install_fake_ultralytics(monkeypatch, [], box_conf=[])
    est = YoloPoseEstimator("m.pt", 0.5)
    assert est.estimate(_jpeg()) is None
    assert est.estimate(b"not a jpeg") is None
    assert len(calls) == 1                      # 디코딩 실패 프레임은 모델에 넘기지 않는다


def test_create_pose_estimator_yolo_uses_settings(monkeypatch):
    _install_fake_ultralytics(monkeypatch, [], [])
    est = create_pose_estimator(Settings(pose_backend="yolo", yolo_model_path="x.pt",
                                         keypoint_min_confidence=0.7))
    assert isinstance(est, YoloPoseEstimator)
    assert est._model.path == "x.pt" and est.min_conf == 0.7
