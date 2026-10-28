"""녹화한 영상으로 낙상 판정을 시험한다 (서버·휴대폰 없이).

    python -m server.replay_video 넘어짐.mp4
    python -m server.replay_video 넘어짐.mp4 --hold 2      # 영상이 끝난 뒤 마지막 장면을 2초 더(누운 채로 있음)
    python -m server.replay_video 넘어짐.mp4 --fps 5 --raw  # 초당 5장, YOLO 원시 신뢰도도 출력

영상을 휴대폰 카메라 화면(web/static/js/camera.js)과 같은 조건, 곧 긴 변 640px·JPEG 품질 0.7·초당 10장으로
바꿔 서버가 쓰는 자세 추정(YOLO-Pose)과 낙상 감지기에 그대로 넣고 프레임마다 결과를 출력한다.
감지 방식과 기준값은 서버와 같은 설정(server/config.py, FALL_* 환경변수)을 쓴다.
    기본(Random Forest): 분석 점수와 상태.   FALL_RF_THRESHOLD=0.5 처럼 기준을 바꿀 수 있다.
    FALL_DETECTION_BACKEND=fsm: 규칙 기반 FSM(하강 속도·누운 자세).   예: FALL_T1_SECONDS=1.5
특징값(하강 속도·각도·종횡비)은 두 방식 모두 참고로 출력한다.

시각은 영상 안의 시각을 쓴다. 실제 서버는 '받은 시각'을 쓰고 분석이 밀리면 프레임을 버리므로, 느린 PC에서는
결과가 조금 다를 수 있다.
"""

import argparse
import sys
from typing import List, Tuple

import numpy as np

from server.config import Settings
from server.detection import FallFSM, FeatureExtractor, create_pose_estimator
from server.devices import DeviceHub

FRAME_LONG_SIDE = 640   # camera.js와 같은 값
JPEG_QUALITY = 70


def sample_frames(path: str, fps: float, hold: float) -> List[Tuple[float, bytes]]:
    """영상을 초당 fps장으로 뽑아 (영상 안 시각, JPEG 바이트) 목록으로 만든다."""
    import cv2

    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        raise SystemExit(f"영상을 열 수 없습니다: {path}")
    video_fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    frames: List[Tuple[float, bytes]] = []
    index, next_t = 0, 0.0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        t = index / video_fps
        if t + 1e-9 >= next_t:
            h, w = frame.shape[:2]
            scale = min(1.0, FRAME_LONG_SIDE / max(h, w))
            if scale < 1.0:
                frame = cv2.resize(frame, (round(w * scale), round(h * scale)), interpolation=cv2.INTER_AREA)
            ok, jpg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
            if ok:
                frames.append((t, jpg.tobytes()))
            next_t += 1.0 / fps
        index += 1
    cap.release()
    if hold > 0 and frames:
        last_t, last = frames[-1]
        for k in range(1, int(round(hold * fps)) + 1):
            frames.append((last_t + k / fps, last))
    return frames


def raw_scores(pose, jpg: bytes) -> str:
    """YOLO가 찾은 사람 수, 가장 확실한 사람의 상자 신뢰도, 어깨·엉덩이 4점 신뢰도."""
    import cv2
    from server.detection.pose import (COCO_LEFT_HIP, COCO_LEFT_SHOULDER, COCO_RIGHT_HIP,
                                       COCO_RIGHT_SHOULDER, _to_numpy)

    model = getattr(pose, "_model", None)
    if model is None:
        return ""
    result = model(cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_COLOR), verbose=False)[0]
    if result.boxes is None or len(result.boxes) == 0 or result.keypoints is None:
        return "  [사람 0]"
    best = int(np.argmax(_to_numpy(result.boxes.conf)))
    conf = _to_numpy(result.keypoints.conf)[best] if result.keypoints.conf is not None else None
    core = "-" if conf is None else ",".join(
        f"{conf[k]:.2f}" for k in (COCO_LEFT_SHOULDER, COCO_RIGHT_SHOULDER, COCO_LEFT_HIP, COCO_RIGHT_HIP))
    return f"  [사람 {len(result.boxes)}, 상자 {float(_to_numpy(result.boxes.conf)[best]):.2f}, 어깨·엉덩이 {core}]"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="녹화한 영상으로 낙상 판정 시험")
    parser.add_argument("video", help="영상 파일(mp4 등)")
    parser.add_argument("--fps", type=float, default=10.0, help="초당 분석할 장 수(기본 10, 휴대폰 화면과 같음)")
    parser.add_argument("--hold", type=float, default=0.0, help="영상 끝의 마지막 장면을 이 시간(초)만큼 더 넣음")
    parser.add_argument("--raw", action="store_true", help="YOLO 원시 신뢰도도 출력(사람 없음의 이유 확인)")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")   # Windows 콘솔에서 한글이 깨지지 않게

    settings = Settings()
    if settings.pose_backend != "yolo":
        print("FALL_POSE_BACKEND가 yolo가 아니면 실제 영상을 분석할 수 없습니다.")
        return 1
    pose = create_pose_estimator(settings)
    extractor = FeatureExtractor()
    rules = FallFSM.from_settings(settings)   # 자세 표시(참고)와 FSM 모드의 판정
    # 서버와 똑같이 감지기를 고른다: DeviceHub가 Random Forest 모델을 불러오고 검사한다.
    hub = DeviceHub(settings, accounts=None, guardian_hub=None, pose=pose)
    if hub.rf_model is not None:
        from server.detection.random_forest import RandomForestDetector
        detector = RandomForestDetector(hub.rf_model, settings.rf_threshold)
    else:
        detector = rules
    rf = hub.rf_model is not None
    frames = sample_frames(args.video, args.fps, args.hold)

    print(f"영상 {args.video}: {len(frames)}장 (초당 {args.fps:g}장" + (f", 끝에 {args.hold:g}초 더" if args.hold else "") + ")")
    if rf:
        print(f"감지 방식: Random Forest ({settings.rf_model_path}). 점수가 {settings.rf_threshold}를 넘는 판정이 "
              "최근 1.2초 동안 절반 이상(3회 이상)이면 의심. 특징값·자세는 참고")
        print("  시각  하강속도  각도   종횡비  자세      상태      점수")
    else:
        print(f"감지 방식: 규칙 기반. 하강 속도 {rules.descent_peak} 이상 -> 하강, 누움(각도 {rules.angle_lying}° 이상 또는 "
              f"종횡비 {rules.aspect_lying} 이상)이 T1 {rules.t1}초 유지 -> 의심 ({rules.lying_gap}초 이하 끊김은 이어서 잼), "
              f"서 있음(각도 {rules.angle_standing}° 미만이고 종횡비 {rules.aspect_standing} 미만) -> 정상")
        print("  시각  하강속도  각도   종횡비  자세      상태      누운시간")
    suspect_at = None
    fall_at = None
    for t, jpg in frames:
        keypoints = pose.estimate(jpg)
        features = extractor.extract(keypoints, t) if keypoints is not None else None
        before = detector.state
        state = detector.update(keypoints if rf else features, t)
        if features is None:
            posture, values = "-", "사람 없음".ljust(22)
        else:
            posture = "누움" if rules.is_lying(features) else "서 있음" if rules.is_standing(features) else "중간"
            values = (f"{features['descent_velocity']:7.2f} {features['torso_angle']:6.1f}° "
                      f"{features['aspect_ratio']:6.2f}")
        if rf:
            last = "대기" if detector.probability is None else f"{detector.probability:.2f}"
        else:
            last = f"{t - rules.lying_since:.1f}초" if rules.lying_since is not None else "-"
        change = f"  <== {before.value} -> {state.value}" if state != before else ""
        extra = raw_scores(pose, jpg) if args.raw else ""
        print(f"{t:6.2f}s {values}  {posture:6} {state.value:8} {last:6}{extra}{change}")
        if state.value == "FALLING" and fall_at is None:
            fall_at = t
        if state.value == "SUSPECT" and suspect_at is None:
            suspect_at = t
    print()
    if suspect_at is not None:
        print(f"결과: {suspect_at:.2f}초에 낙상 의심" + (f" (하강 감지 {fall_at:.2f}초)" if fall_at is not None else ""))
    elif rf:
        print("결과: 영상이 끝날 때까지 낙상 의심 판정이 나오지 않음. 더 누워 있는 상황은 --hold로 시험할 수 있습니다.")
    elif fall_at is not None:
        print(f"결과: {fall_at:.2f}초에 하강을 감지했지만 영상이 끝날 때까지 누운 자세가 T1({rules.t1}초)을 채우지 못함 "
              f"(마지막 상태 {rules.state.value}). 더 누워 있는 상황은 --hold로 시험할 수 있습니다.")
    else:
        print("결과: 하강을 감지하지 못함")
    return 0


if __name__ == "__main__":
    sys.exit(main())
