"""영상 파일로 낙상 판정 성능을 측정한다 (docs/TEST_PLAN.md '측정 및 기록').

라벨 CSV (UTF-8, 머리글 필수):
    file,fall,fall_start
    videos/fall01.mp4,1,3.2      # 낙상 영상, 3.2초에 넘어지기 시작
    videos/sit01.mp4,0,          # 일상 동작 영상 (경보가 나면 오경보)

    python -m tools.evaluate labels.csv
    python -m tools.evaluate labels.csv --grid DESCENT_VELOCITY_PEAK=1.5,2,2.5 --grid T1_LYING_SECONDS=1,2

자세 추정은 영상마다 한 번만 하고 특징값을 캐시(--cache)에 저장한다. 임계값 조합(--grid)은
캐시된 특징값으로 상태 머신만 다시 돌리므로 빠르다. 결과는 --out 폴더에 CSV로 남긴다.
"""

import argparse
import csv
import hashlib
import itertools
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import config

FeatureSeq = List[Tuple[float, Optional[Dict[str, float]]]]


@dataclass
class Clip:
    file: Path
    fall: bool
    fall_start: Optional[float]


@dataclass
class ClipResult:
    clip: Clip
    detected_at: Optional[float]  # 처음 낙상 의심(SUSPECT)이 된 영상 시각(초)
    person_ratio: float           # 사람이 검출된 프레임 비율

    @property
    def detected(self) -> bool:
        return self.detected_at is not None

    @property
    def latency(self) -> Optional[float]:
        if self.detected and self.clip.fall_start is not None:
            return self.detected_at - self.clip.fall_start
        return None


def load_labels(path: Path) -> List[Clip]:
    clips = []
    with open(path, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            file = Path(row["file"])
            if not file.is_absolute():
                file = path.parent / file
            start = row.get("fall_start", "").strip()
            clips.append(Clip(file, row["fall"].strip() == "1", float(start) if start else None))
    return clips


def extract_features(video: Path, model: str, cache_dir: Path) -> FeatureSeq:
    """영상 프레임마다 (영상 시각, 특징값) 목록. 영상 내용·모델이 같으면 캐시를 쓴다."""
    digest = hashlib.sha1(video.read_bytes()).hexdigest()[:16]
    cache = cache_dir / f"{video.stem}-{model}-{digest}.json"
    if cache.exists():
        return [(t, f) for t, f in json.loads(cache.read_text(encoding="utf-8"))]

    from l3_perception.camera import OpenCVCamera, rotate
    from l3_perception.fall_detector import load_detector
    from l3_perception.features import FeatureExtractor

    cam = OpenCVCamera(str(video), config.FRAME_WIDTH, config.FRAME_HEIGHT)
    cam.open()
    fps = cam.file_fps or 30.0
    # 실제 장치와 같은 처리 속도를 흉내 내도록 TARGET_FPS에 맞춰 프레임을 건너뛴다.
    step = max(1, round(fps / config.TARGET_FPS))
    detector = load_detector(model)
    extractor = FeatureExtractor()
    seq: FeatureSeq = []
    index = 0
    try:
        while True:
            frame = cam.read()
            if frame is None:
                break
            if index % step == 0:
                import cv2
                frame = rotate(frame, config.CAMERA_ROTATION)
                frame = cv2.resize(frame, (config.FRAME_WIDTH, config.FRAME_HEIGHT))
                t = index / fps
                kp = detector.detect(frame, t)
                seq.append((t, extractor.extract(kp, t) if kp is not None else None))
            index += 1
    finally:
        cam.close()
        detector.close()
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(seq), encoding="utf-8")
    return seq


def run_fsm(seq: FeatureSeq) -> Optional[float]:
    """특징값 시퀀스에 상태 머신을 돌려 처음 SUSPECT가 된 시각을 반환한다."""
    from l3_perception.fall_detector import create_fsm
    from l3_perception.fsm import FallState
    fsm = create_fsm()
    for t, features in seq:
        if fsm.update(features, t) == FallState.SUSPECT:
            return t
    return None


def summarize(results: Sequence[ClipResult]) -> Dict[str, float]:
    falls = [r for r in results if r.clip.fall]
    normals = [r for r in results if not r.clip.fall]
    tp = sum(r.detected for r in falls)
    fp = sum(r.detected for r in normals)
    latencies = [r.latency for r in falls if r.latency is not None]
    return {
        "falls": len(falls),
        "normals": len(normals),
        "detected_falls": tp,
        "false_alarms": fp,
        "recall": tp / len(falls) if falls else float("nan"),
        "false_alarm_rate": fp / len(normals) if normals else float("nan"),
        "precision": tp / (tp + fp) if tp + fp else float("nan"),
        "mean_latency": sum(latencies) / len(latencies) if latencies else float("nan"),
        "max_latency": max(latencies) if latencies else float("nan"),
    }


def parse_grid(items: Sequence[str]) -> List[Dict[str, float]]:
    axes = []
    for item in items:
        key, _, values = item.partition("=")
        if not hasattr(config, key):
            raise SystemExit(f"알 수 없는 설정: {key}")
        axes.append([(key, float(v)) for v in values.split(",")])
    return [dict(combo) for combo in itertools.product(*axes)] if axes else [{}]


def evaluate(clips: Sequence[Clip], sequences: Dict[Path, FeatureSeq], params: Dict[str, float]) -> List[ClipResult]:
    saved = {k: getattr(config, k) for k in params}
    try:
        for k, v in params.items():
            setattr(config, k, v)
        results = []
        for clip in clips:
            seq = sequences[clip.file]
            seen = sum(f is not None for _, f in seq)
            results.append(ClipResult(clip, run_fsm(seq), seen / len(seq) if seq else 0.0))
        return results
    finally:
        for k, v in saved.items():
            setattr(config, k, v)


def fmt(x: float) -> str:
    return "-" if x != x else f"{x:.2f}"  # NaN -> '-'


def main() -> int:
    parser = argparse.ArgumentParser(description="영상 파일로 낙상 판정 성능 측정")
    parser.add_argument("labels", type=Path)
    parser.add_argument("--model", default=config.POSE_MODEL, choices=["mediapipe", "yolo"])
    parser.add_argument("--grid", action="append", default=[], help="KEY=v1,v2,... (여러 번 지정 가능)")
    parser.add_argument("--cache", type=Path, default=config.DATA_DIR / "eval_cache")
    parser.add_argument("--out", type=Path, default=config.DATA_DIR / "eval_results")
    args = parser.parse_args()

    clips = load_labels(args.labels)
    missing = [c.file for c in clips if not c.file.exists()]
    if missing:
        raise SystemExit("영상 파일이 없습니다:\n" + "\n".join(map(str, missing)))

    sequences = {}
    for i, clip in enumerate(clips, 1):
        print(f"[{i}/{len(clips)}] 특징 추출: {clip.file.name}", flush=True)
        sequences[clip.file] = extract_features(clip.file, args.model, args.cache)

    args.out.mkdir(parents=True, exist_ok=True)
    summary_rows = []
    for params in parse_grid(args.grid):
        results = evaluate(clips, sequences, params)
        s = summarize(results)
        summary_rows.append({**params, **s})
        label = ", ".join(f"{k}={v}" for k, v in params.items()) or "현재 설정"
        print(f"\n== {label} ==")
        print(f"낙상 감지 {s['detected_falls']}/{s['falls']} (재현율 {fmt(s['recall'])}), "
              f"오경보 {s['false_alarms']}/{s['normals']} (비율 {fmt(s['false_alarm_rate'])}), "
              f"평균 지연 {fmt(s['mean_latency'])}초, 최대 지연 {fmt(s['max_latency'])}초")
        for r in results:
            mark = "OK " if r.detected == r.clip.fall else "ERR"
            kind = "낙상" if r.clip.fall else "일상"
            print(f"  [{mark}] {kind} {r.clip.file.name}: 감지={'예' if r.detected else '아니오'}"
                  f" 지연={fmt(r.latency) if r.latency is not None else '-'}"
                  f" 사람검출={r.person_ratio:.0%}")

    out = args.out / "summary.csv"
    with open(out, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(summary_rows[0].keys()))
        writer.writeheader()
        writer.writerows(summary_rows)
    print(f"\n요약 저장: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
