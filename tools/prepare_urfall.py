"""UR Fall Detection Dataset으로 tools/evaluate.py를 돌릴 수 있게 영상과 라벨을 준비한다.

    python -m tools.prepare_urfall data/urfall --download   # 내려받기(낙상 30·일상 40, 약 90MB) + 준비
    python -m tools.prepare_urfall data/urfall              # 이미 받은 *-cam0.mp4만 준비
    python -m tools.evaluate data/urfall/rgb/labels.csv

원본 cam0 영상은 왼쪽이 깊이 영상, 오른쪽이 컬러 영상이다. 컬러 절반만 잘라 무손실(FFV1) avi로 저장한다.
출처: http://fenix.ur.edu.pl/~mkepski/ds/uf.html (CC BY-NC-SA 4.0, 비상업·학술용).
결과를 보고서에 쓸 때는 데이터셋 논문을 인용한다:
Kwolek B., Kepski M., Human fall detection on embedded platform using depth maps and wireless
accelerometer, Computer Methods and Programs in Biomedicine 117(3), 2014.
"""

import argparse
import csv
import sys
import time
from pathlib import Path

import cv2
import requests

BASE_URL = "https://fenix.ur.edu.pl/~mkepski/ds/data/"
CLIPS = [f"fall-{i:02d}-cam0.mp4" for i in range(1, 31)] + [f"adl-{i:02d}-cam0.mp4" for i in range(1, 41)]


def download(dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    for i, name in enumerate(CLIPS, 1):
        path = dest / name
        if path.exists() and path.stat().st_size > 0:
            continue
        for attempt in range(4):  # 서버가 가끔 요청을 거절해 몇 번 다시 시도한다
            try:
                res = requests.get(BASE_URL + name, timeout=60)
                if res.status_code == 200 and res.headers.get("Content-Type", "").startswith("video/"):
                    path.write_bytes(res.content)
                    print(f"[{i}/{len(CLIPS)}] {name} {len(res.content) // 1024}KB", flush=True)
                    break
            except requests.RequestException:
                pass
            time.sleep(2 * (attempt + 1))
        else:
            raise SystemExit(f"내려받지 못했습니다: {name}")


def crop_color_half(src: Path, dst: Path) -> None:
    cap = cv2.VideoCapture(str(src))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    w, h = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    writer = cv2.VideoWriter(str(dst), cv2.VideoWriter_fourcc(*"FFV1"), fps, (w // 2, h))
    if not writer.isOpened():
        raise SystemExit("FFV1 코덱으로 저장할 수 없습니다 (OpenCV 빌드 확인)")
    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            writer.write(frame[:, w // 2:])
    finally:
        cap.release()
        writer.release()


def main() -> int:
    parser = argparse.ArgumentParser(description="UR Fall 영상을 평가용으로 준비")
    parser.add_argument("folder", type=Path, help="원본 *-cam0.mp4가 있는(또는 받을) 폴더")
    parser.add_argument("--download", action="store_true", help="원본 영상을 먼저 내려받는다")
    args = parser.parse_args()

    if args.download:
        download(args.folder)
    videos = sorted(args.folder.glob("*-cam0.mp4"))
    if not videos:
        raise SystemExit(f"{args.folder}에 *-cam0.mp4가 없습니다 (--download로 받을 수 있습니다)")
    out = args.folder / "rgb"
    out.mkdir(exist_ok=True)
    rows = []
    for video in videos:
        dst = out / (video.stem + ".avi")
        if not dst.exists():
            crop_color_half(video, dst)
        rows.append({"file": dst.name, "fall": "1" if video.stem.startswith("fall") else "0", "fall_start": ""})
    with open(out / "labels.csv", "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["file", "fall", "fall_start"])
        writer.writeheader()
        writer.writerows(rows)
    falls = sum(r["fall"] == "1" for r in rows)
    print(f"준비 완료: 낙상 {falls}개, 일상 {len(rows) - falls}개 -> {out / 'labels.csv'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
