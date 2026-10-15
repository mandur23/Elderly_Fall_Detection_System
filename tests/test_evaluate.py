import tempfile
import unittest
from pathlib import Path

from tools.evaluate import Clip, ClipResult, evaluate, hold_last, load_labels, parse_grid, summarize

STAND = {"descent_velocity": 0.0, "torso_angle": 10.0, "aspect_ratio": 0.4}
LYING = {"descent_velocity": 0.0, "torso_angle": 80.0, "aspect_ratio": 1.5}


def fall_seq(peak=3.0, fall_at=2.0, fps=10, seconds=6):
    seq = []
    for i in range(seconds * fps):
        t = i / fps
        if t < fall_at:
            seq.append((t, STAND))
        elif abs(t - fall_at) < 1e-9:
            seq.append((t, dict(LYING, descent_velocity=peak)))
        else:
            seq.append((t, LYING))
    return seq


class EvaluateTest(unittest.TestCase):
    def test_metrics(self):
        a, b, c = Clip(Path("a"), True, 2.0), Clip(Path("b"), True, 1.0), Clip(Path("c"), False, None)
        s = summarize([ClipResult(a, 4.5, 1.0), ClipResult(b, None, 1.0), ClipResult(c, 3.0, 1.0)])
        self.assertEqual((s["detected_falls"], s["falls"], s["false_alarms"], s["normals"]), (1, 2, 1, 1))
        self.assertAlmostEqual(s["recall"], 0.5)
        self.assertAlmostEqual(s["false_alarm_rate"], 1.0)
        self.assertAlmostEqual(s["mean_latency"], 2.5)

    def test_grid_changes_outcome_and_restores_config(self):
        import config
        before = config.DESCENT_VELOCITY_PEAK
        clip = Clip(Path("f"), True, 2.0)
        seqs = {clip.file: fall_seq(peak=2.5)}
        grid = parse_grid(["DESCENT_VELOCITY_PEAK=2,3"])
        low, high = (evaluate([clip], seqs, p)[0] for p in grid)
        self.assertTrue(low.detected)
        self.assertAlmostEqual(low.latency, 2.0, places=5)   # 누운 자세 T1(2초) 후 감지
        self.assertFalse(high.detected)                       # 임계값이 피크보다 높으면 놓친다
        self.assertEqual(config.DESCENT_VELOCITY_PEAK, before)

    def test_hold_last_lets_short_clip_reach_t1(self):
        clip = Clip(Path("f"), True, 2.0)
        short = fall_seq(peak=3.0, fall_at=2.0, seconds=3)  # 넘어지고 1초 뒤에 끝나는 영상
        self.assertFalse(evaluate([clip], {clip.file: short}, {})[0].detected)
        held = hold_last(short, 2.0)
        self.assertEqual(held[-1][1]["descent_velocity"], 0.0)
        self.assertTrue(evaluate([clip], {clip.file: held}, {})[0].detected)

    def test_load_labels_resolves_relative_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "labels.csv"
            p.write_text("file,fall,fall_start\nv/a.mp4,1,3.5\nv/b.mp4,0,\n", encoding="utf-8")
            clips = load_labels(p)
        self.assertEqual(clips[0].file, Path(tmp) / "v/a.mp4")
        self.assertEqual((clips[0].fall, clips[0].fall_start), (True, 3.5))
        self.assertEqual((clips[1].fall, clips[1].fall_start), (False, None))


if __name__ == "__main__":
    unittest.main()
