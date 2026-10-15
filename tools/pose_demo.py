"""자세 추정 데모: 웹캠 또는 영상 파일을 실제 L3 코드(자세 추정 -> 특징 계산 -> 낙상 판정)로
처리하면서 관절, 판정에 쓰는 몸통 선·영역, 특징값, 판정 상태를 화면에 보여 준다.

    python -m tools.pose_demo                         # 웹캠 0번
    python -m tools.pose_demo --source fall.mp4       # 영상 파일 (실제 속도로 반복 재생)
    python -m tools.pose_demo --source data/urfall/fall-01-cam0.mp4 --right-half
                                                      # UR Fall 원본 영상(왼쪽 깊이 | 오른쪽 컬러)
    python -m tools.pose_demo --source a.mp4 --gif out.gif   # 결과를 GIF로 저장 (발표 자료용)
    python -m tools.pose_demo --source a.mp4 --headless      # 창 없이 상태 변화만 출력

키: space 일시정지, r 판정 초기화, q/ESC 종료
화면에 보여 주기만 하고 프레임은 저장하지 않는다(--gif 제외).
"""

import argparse
import sys
import time
from collections import deque
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont  # mediapipe 의존성(matplotlib)으로 함께 설치된다

import config
from l3_perception import pose_detector as pd
from l3_perception.fall_detector import FrameAnalyzer
from l3_perception.fsm import FallState

# MediaPipe 33개 관절 연결선 (얼굴 일부 생략)
CONNECTIONS = [
    (0, 1), (1, 2), (2, 3), (3, 7), (0, 4), (4, 5), (5, 6), (6, 8), (9, 10),
    (11, 12), (11, 13), (13, 15), (12, 14), (14, 16), (15, 17), (15, 19), (15, 21), (17, 19),
    (16, 18), (16, 20), (16, 22), (18, 20), (11, 23), (12, 24), (23, 24),
    (23, 25), (25, 27), (27, 29), (29, 31), (27, 31), (24, 26), (26, 28), (28, 30), (30, 32), (28, 32),
]
STATE_RGB = {FallState.NORMAL: (46, 160, 67), FallState.FALLING: (230, 140, 0), FallState.SUSPECT: (220, 40, 40)}
STATE_LABEL = {FallState.NORMAL: "정상 감시", FallState.FALLING: "하강 감지 - 누움 확인 중",
               FallState.SUSPECT: "낙상 의심"}
FONTS = ["C:/Windows/Fonts/malgun.ttf", "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
         "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", "/System/Library/Fonts/AppleSDGothicNeo.ttc"]
PANEL_W = 340
WINDOW = "Pose Demo"

# 관절 33개 전체를 그리기 위해, 어깨·엉덩이만 남기기 전의 값을 가로챈다.
_last = {}
_keypoints_from = pd._keypoints_from


def _spy(points, conf, *a, **k):
    _last["points"], _last["conf"] = points, conf
    return _keypoints_from(points, conf, *a, **k)


pd._keypoints_from = _spy


def load_font(size):
    for path in FONTS:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            pass
    return ImageFont.load_default()


def pt(p):
    return int(p[0]), int(p[1])


def draw_pose(img, kp, state):
    color = STATE_RGB[state][::-1]  # BGR
    if "points" in _last:
        points, conf = _last["points"], _last["conf"]
        ok = conf >= config.KEYPOINT_MIN_CONFIDENCE
        for a, b in CONNECTIONS:
            if ok[a] and ok[b]:
                cv2.line(img, pt(points[a]), pt(points[b]), (220, 220, 220), 2, cv2.LINE_AA)
        for p in points[ok]:
            cv2.circle(img, pt(p), 3, (255, 255, 255), -1, cv2.LINE_AA)
    if kp is not None:
        x0, y0, x1, y1 = map(int, kp.bbox)
        cv2.rectangle(img, (x0, y0), (x1, y1), color, 2)  # 종횡비 계산 영역 (팔 제외)
        s, h = kp.shoulder_center, kp.hip_center
        cv2.line(img, pt(s), pt(h), color, 6, cv2.LINE_AA)  # 몸통 각도 계산 선
        for p in (s, h):
            cv2.circle(img, pt(p), 7, color, -1, cv2.LINE_AA)


def render(frame, fsm, kp, feats, state, now, peak, fps, fonts, footer):
    h, w = frame.shape[:2]
    canvas = np.zeros((h, w + PANEL_W, 3), np.uint8)
    canvas[:, :w] = frame
    canvas[:, w:] = (34, 30, 28)
    img = Image.fromarray(cv2.cvtColor(canvas, cv2.COLOR_BGR2RGB))
    d = ImageDraw.Draw(img)
    big, body, small = fonts
    x, y = w + 16, 80
    d.rectangle([w, 0, w + PANEL_W, 64], fill=STATE_RGB[state])
    d.text((x, 16), STATE_LABEL[state], font=big, fill=(255, 255, 255))

    def row(label, value, note="", hot=False):
        nonlocal y
        d.text((x, y), label, font=body, fill=(170, 170, 170))
        d.text((x + 110, y), value, font=body, fill=(255, 90, 90) if hot else (255, 255, 255))
        if note:
            d.text((x + 110, y + 22), note, font=small, fill=(130, 130, 130))
            y += 18
        y += 30

    row("사람", "검출됨" if kp is not None else "없음", hot=kp is None)
    if feats is not None:
        angle, aspect, vel = feats["torso_angle"], feats["aspect_ratio"], feats["descent_velocity"]
        row("몸통 각도", f"{angle:5.1f}°",
            f"누움 ≥{config.TORSO_ANGLE_LYING:.0f}°, 일어섬 <{config.TORSO_ANGLE_STANDING:.0f}°",
            hot=angle >= config.TORSO_ANGLE_LYING)
        row("종횡비", f"{aspect:5.2f}", f"보조 기준 ≥{config.ASPECT_RATIO_LYING:.1f} (팔 제외 영역)",
            hot=aspect >= config.ASPECT_RATIO_LYING)
        row("하강 속도", f"{vel:5.2f}", f"기준 ≥{config.DESCENT_VELOCITY_PEAK:.1f} 몸통길이/초",
            hot=vel >= config.DESCENT_VELOCITY_PEAK)
        row("최근 최고", f"{peak:5.2f}", "최근 2초 하강 속도 최댓값", hot=peak >= config.DESCENT_VELOCITY_PEAK)
        posture = "누움" if fsm.is_lying(feats) else ("서 있음/앉음" if fsm.is_standing(feats) else "중간")
        row("자세 판단", posture)
    if state == FallState.FALLING and fsm.lying_since is not None:
        row("누움 유지", f"{now - fsm.lying_since:3.1f} / {config.T1_LYING_SECONDS:.1f}초",
            "사람이 안 보여도 누움으로 계산" if config.MISSING_COUNTS_AS_LYING and kp is None else "", hot=True)
    if state == FallState.SUSPECT:
        d.text((x, h - 100), "실제 시스템은 여기서 음성 확인 시작", font=small, fill=(255, 140, 140))
    d.text((x, h - 72), footer, font=small, fill=(255, 210, 120))
    d.text((x, h - 48), f"FPS {fps:4.1f}   space 정지  r 초기화  q 종료", font=small, fill=(170, 170, 170))
    return cv2.cvtColor(np.asarray(img), cv2.COLOR_RGB2BGR)


def frames(cap, is_webcam, fps, right_half):
    """(프레임, 시각, 안내 문구). 영상 파일은 영상 속 시각을 쓰고 실제 장치처럼 TARGET_FPS로 건너뛴다."""
    step = 1 if is_webcam else max(1, round(fps / config.TARGET_FPS))
    idx, fails = 0, 0
    while True:
        ok, frame = cap.read()
        if not ok:
            fails += 1
            if not is_webcam or fails > 30:
                return
            continue
        fails = 0
        if is_webcam:
            yield cv2.flip(frame, 1), time.time(), "웹캠"  # 거울처럼 (특징값은 좌우 대칭이라 영향 없음)
            continue
        if idx % step == 0:
            if right_half:
                frame = frame[:, frame.shape[1] // 2:]
            frame = cv2.resize(frame, (config.FRAME_WIDTH, config.FRAME_HEIGHT))
            yield frame, idx / fps, f"영상 {idx / fps:4.1f}초"
        idx += 1


def main() -> int:
    parser = argparse.ArgumentParser(description="자세 추정·낙상 판정 데모")
    parser.add_argument("--source", default="0", help="웹캠 번호 또는 영상 파일 경로")
    parser.add_argument("--right-half", action="store_true", help="영상의 오른쪽 절반만 사용 (UR Fall 원본 영상)")
    parser.add_argument("--no-loop", action="store_true", help="영상 파일을 한 번만 재생")
    parser.add_argument("--headless", action="store_true", help="창 없이 처리하고 상태 변화만 출력")
    parser.add_argument("--gif", type=Path, help="처리 결과를 저장할 GIF 경로 (한 번 재생)")
    args = parser.parse_args()

    source = int(args.source) if args.source.isdigit() else args.source
    is_webcam = isinstance(source, int)
    try:
        detector = pd.MediaPipePoseDetector(config.KEYPOINT_MIN_CONFIDENCE, config.MEDIAPIPE_MODEL_PATH)
    except pd.PoseDetectorError as e:
        print(f"자세 추정 모델을 불러오지 못했습니다: {e}", file=sys.stderr)
        return 1
    analyzer = FrameAnalyzer(detector)
    fonts = (load_font(24), load_font(18), load_font(14))
    show = not args.headless
    once = is_webcam or args.no_loop or args.headless or args.gif
    gif_frames, t_offset, fps_file = [], 0.0, 30.0
    if show:
        cv2.namedWindow(WINDOW, cv2.WINDOW_NORMAL)
    try:
        while True:
            cap = cv2.VideoCapture(source, cv2.CAP_DSHOW) if is_webcam and sys.platform == "win32" \
                else cv2.VideoCapture(source)
            if is_webcam:
                cap.set(cv2.CAP_PROP_FRAME_WIDTH, config.FRAME_WIDTH)
                cap.set(cv2.CAP_PROP_FRAME_HEIGHT, config.FRAME_HEIGHT)
            if not cap.isOpened():
                print(f"영상을 열 수 없습니다: {source}", file=sys.stderr)
                return 1
            fps_file = cap.get(cv2.CAP_PROP_FPS) or 30.0
            recent, fps, last_tick, prev, now = deque(), 0.0, None, FallState.NORMAL, t_offset
            start_wall = time.monotonic()
            for frame, t, footer in frames(cap, is_webcam, fps_file, args.right_half):
                now = t if is_webcam else t + t_offset
                _last.clear()
                kp, feats, state = analyzer.process(frame, now)
                tick = time.monotonic()
                if last_tick is not None:
                    inst = 1.0 / max(1e-6, tick - last_tick)
                    fps = 0.9 * fps + 0.1 * inst if fps else inst
                last_tick = tick
                if feats is not None:
                    recent.append((now, feats["descent_velocity"]))
                while recent and now - recent[0][0] > 2.0:
                    recent.popleft()
                peak = max((v for _, v in recent), default=0.0)
                if args.headless and state != prev:
                    print(f"{footer}: {prev.value} -> {state.value}  {feats}")
                prev = state
                if not (show or args.gif):
                    continue

                draw_pose(frame, kp, state)
                view = render(frame, analyzer.fsm, kp, feats, state, now, peak, fps, fonts, footer)
                if args.gif:
                    small = cv2.resize(view, None, fx=0.6, fy=0.6, interpolation=cv2.INTER_AREA)
                    gif_frames.append(Image.fromarray(cv2.cvtColor(small, cv2.COLOR_BGR2RGB)))
                if not show:
                    continue
                cv2.imshow(WINDOW, view)
                if not is_webcam:  # 영상 파일은 실제 속도로 재생
                    wait = (start_wall + t) - time.monotonic()
                    if wait > 0:
                        time.sleep(wait)
                key = cv2.waitKey(1) & 0xFF
                if key == ord(" "):
                    key = cv2.waitKey(0) & 0xFF
                    start_wall = time.monotonic() - t
                if key in (ord("q"), 27) or cv2.getWindowProperty(WINDOW, cv2.WND_PROP_VISIBLE) < 1:
                    return 0
                if key == ord("r"):
                    analyzer.reset(now)
                    recent.clear()
            cap.release()
            if once:
                return 0
            t_offset = now + 1.0  # 반복 재생: 판정을 초기화하고 시각은 계속 늘린다
            analyzer.reset(t_offset)
    finally:
        detector.close()
        if show:
            cv2.destroyAllWindows()
        if gif_frames:
            gif_frames[0].save(args.gif, save_all=True, append_images=gif_frames[1:],
                               duration=int(1000 / config.TARGET_FPS), loop=0, optimize=True)
            print(f"GIF 저장: {args.gif} ({len(gif_frames)}프레임)")


if __name__ == "__main__":
    sys.exit(main())
