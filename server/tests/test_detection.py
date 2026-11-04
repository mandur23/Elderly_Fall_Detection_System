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


def test_short_break_in_lying_keeps_t1(fsm):
    fsm.update(PEAK_LYING, now=0.0)
    fsm.update(LYING, now=1.0)
    fsm.update(BENT, now=1.3)   # 뒤척임(누움도 기립도 아님)
    fsm.update(None, now=1.6)   # 잠깐 화면 밖
    fsm.update(LYING, now=2.0)  # 마지막 누움(1.0초)에서 1초 이내로 끊겼으므로 0초부터 이어서 잰다
    assert fsm.state == FallState.SUSPECT


def test_long_break_in_lying_restarts_t1(fsm):
    fsm.update(PEAK_LYING, now=0.0)
    fsm.update(LYING, now=1.0)
    fsm.update(BENT, now=1.5)
    fsm.update(BENT, now=2.1)   # 마지막 누움에서 1.1초 -> T1을 다시 잰다
    assert fsm.lying_since is None
    fsm.update(LYING, now=2.5)
    fsm.update(LYING, now=4.4)
    assert fsm.state == FallState.FALLING
    fsm.update(LYING, now=4.5)
    assert fsm.state == FallState.SUSPECT


def test_lying_gap_is_configurable():
    strict = FallFSM(descent_peak=2.0, t1_lying_seconds=2.0, falling_timeout=5.0, angle_lying=60, aspect_lying=1.0,
                     angle_standing=40, aspect_standing=0.8, lying_gap=0.0)
    strict.update(PEAK_LYING, now=0.0)
    strict.update(BENT, now=1.0)   # 허용 간격 0 -> 한 장만 끊겨도 다시 잰다(예전 동작)
    strict.update(LYING, now=2.0)
    assert strict.state == FallState.FALLING and strict.lying_since == 2.0


def test_missing_person_is_not_standing(fsm):
    fsm.update(PEAK_LYING, now=0.0)
    fsm.update(None, now=0.5)
    assert fsm.state == FallState.FALLING


def test_long_missing_person_interrupts_lying_timer(fsm):
    fsm.update(PEAK_LYING, now=0.0)
    fsm.update(None, now=0.6)
    fsm.update(None, now=1.2)       # 1초 넘게 안 보이면 '누움 연속'을 끊는다
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
    assert f.lying_gap == Settings().lying_gap_seconds == 1.0
    assert (f.angle_lying, f.aspect_lying, f.angle_standing, f.aspect_standing) == (55.0, 1.2, 35.0, 0.7)
    assert f.state == FallState.NORMAL


# 실제 낙상 영상(1920x1080, 60fps, 손으로 든 휴대폰)을 휴대폰 카메라 화면처럼 긴 변 640px·JPEG 0.7·초당 10장으로 바꿔
# YOLOv8n-pose와 FeatureExtractor에 넣어 얻은 특징값 (시각, (하강 속도, 몸통 각도, 종횡비)). 2.8초 앞은 의자에서
# 일어서는 장면(모두 서 있음)이라 생략했다. 3.2초에 옆으로 넘어지고, 3.6~3.8초에는 머리가 화면 밖으로 나가 어깨
# 신뢰도가 떨어져 '사람 없음'이 되며, 4.0초에는 다리를 들어 몸통 각도 59°·종횡비 0.85의 중간 자세가 한 장 나온다.
# 영상은 5.0초(바닥에 앉은 채)에서 끝난다.
RECORDED_FALL = [
    (2.80, (0.16, 9.47, 0.567)),
    (2.90, (0.555, 11.36, 0.635)),
    (3.00, (0.88, 15.99, 0.694)),
    (3.10, (1.236, 17.63, 0.841)),
    (3.20, (1.809, 17.85, 1.013)),
    (3.30, (2.579, 25.64, 1.07)),
    (3.40, (3.359, 40.74, 1.746)),
    (3.51, (2.33, 67.35, 2.739)),
    (3.61, None),
    (3.71, None),
    (3.81, None),
    (3.91, (-0.143, 52.78, 1.108)),
    (4.01, (-0.718, 59.15, 0.853)),
    (4.11, (-0.578, 69.55, 0.991)),
    (4.21, (0.484, 69.13, 0.985)),
    (4.31, (0.493, 68.27, 1.078)),
    (4.41, (-0.052, 73.48, 1.313)),
    (4.51, (0.313, 90.0, 1.926)),
    (4.61, (0.744, 76.05, 2.735)),
    (4.71, (1.254, 60.77, 2.405)),
    (4.81, (0.483, 56.38, 2.19)),
    (4.91, (-0.143, 48.23, 2.001)),
    (5.02, (-0.382, 40.62, 1.821)),
]


def _recorded(hold_seconds=0.0):
    """RECORDED_FALL 뒤에 마지막 장면(바닥에 앉음)을 hold_seconds만큼 초당 10장으로 더 붙인다."""
    frames = [(t, None if f is None else dict(zip(("descent_velocity", "torso_angle", "aspect_ratio"), f)))
              for t, f in RECORDED_FALL]
    last_t, last = frames[-1]
    for k in range(1, int(round(hold_seconds * 10)) + 1):
        frames.append((round(last_t + k / 10, 2), last))
    return frames


def test_recorded_fall_keeps_lying_timer_through_short_gaps(fsm):
    """넘어진 순간(3.3초)부터 T1을 재고, 화면 밖으로 나간 0.3초와 다리를 든 한 장에 끊기지 않는다."""
    lying_since = []
    for t, f in _recorded():
        fsm.update(f, now=t)
        lying_since.append(fsm.lying_since)
    assert fsm.state == FallState.FALLING          # 영상 길이(누운 지 1.7초)로는 아직 T1(2초) 전
    assert set(lying_since[lying_since.index(3.30):]) == {3.30}


def test_recorded_fall_reaches_suspect_two_seconds_after_falling(fsm):
    states = {}
    for t, f in _recorded(hold_seconds=1.0):
        states[t] = fsm.update(f, now=t)
    first_suspect = min(t for t, s in states.items() if s == FallState.SUSPECT)
    assert first_suspect == pytest.approx(3.30 + 2.0, abs=0.11)
    assert fsm.suspect_features["aspect_ratio"] >= 1.0


def test_recorded_standing_up_from_chair_is_not_a_fall(fsm):
    """같은 영상의 일어서기 구간(하강 속도 1 미만)만으로는 하강으로 보지 않는다."""
    for t, f in _recorded():
        if t >= 3.0:
            break
        assert fsm.update(f, now=t) == FallState.NORMAL


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
    # 뼈대 선용 17점: 순서를 지키고 신뢰도가 낮은 점만 None
    assert len(k.skeleton) == 17 and k.skeleton[0] is None
    assert k.skeleton[5] == k.left_shoulder and k.skeleton[12] == k.right_hip


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


def test_replay_video_samples_like_phone_camera(tmp_path):
    """녹화 영상 시험 도구: 휴대폰 화면처럼 긴 변 640px, 초당 10장으로 뽑고 --hold만큼 마지막 장면을 더 넣는다."""
    from server.replay_video import sample_frames

    path = str(tmp_path / "clip.avi")
    writer = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"MJPG"), 30.0, (1280, 720))
    for i in range(30):   # 1초
        writer.write(np.full((720, 1280, 3), i * 8, dtype=np.uint8))
    writer.release()

    frames = sample_frames(path, fps=10.0, hold=0.5)
    assert len(frames) == 10 + 5
    times = [t for t, _ in frames]
    assert times[:3] == pytest.approx([0.0, 0.1, 0.2]) and times[-1] == pytest.approx(0.9 + 0.5)
    first = cv2.imdecode(np.frombuffer(frames[0][1], np.uint8), cv2.IMREAD_COLOR)
    assert first.shape[:2] == (360, 640)
    assert frames[-1][1] == frames[9][1]
