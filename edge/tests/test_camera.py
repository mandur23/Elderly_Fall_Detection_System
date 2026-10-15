import os
import tempfile
import threading
import time
import unittest

import cv2
import numpy as np

from edge.camera import Camera, OpenCVSource, RateLimiter, encode_jpeg, make_source, prepare_frame
from edge.tests.fakes import FakeLiveSource


class FrameTest(unittest.TestCase):
    def test_fit_keeps_aspect_and_never_upscales(self):
        wide = np.zeros((720, 1280, 3), np.uint8)
        self.assertEqual(prepare_frame(wide, 640, 480, 0).shape, (360, 640, 3))
        small = np.zeros((240, 320, 3), np.uint8)
        self.assertEqual(prepare_frame(small, 640, 480, 0).shape, (240, 320, 3))

    def test_rotation_after_fit(self):
        frame = np.zeros((480, 640, 3), np.uint8)
        frame[0, 0] = 255  # 왼쪽 위 점
        rotated = prepare_frame(frame, 640, 480, 90)
        self.assertEqual(rotated.shape, (640, 480, 3))
        self.assertEqual(rotated[0, -1, 0], 255)  # 시계 방향 90도: 왼쪽 위 -> 오른쪽 위
        self.assertEqual(prepare_frame(frame, 640, 480, 180)[-1, -1, 0], 255)

    def test_drops_alpha_channel(self):
        self.assertEqual(prepare_frame(np.zeros((10, 10, 4), np.uint8), 640, 480, 0).shape, (10, 10, 3))

    def test_jpeg_quality(self):
        rng = np.random.default_rng(0)
        frame = rng.integers(0, 255, (240, 320, 3), dtype=np.uint8)
        low, high = encode_jpeg(frame, 20), encode_jpeg(frame, 95)
        self.assertTrue(low.startswith(b"\xff\xd8"))
        self.assertLess(len(low), len(high))


class RateLimiterTest(unittest.TestCase):
    def count(self, camera_fps, target_fps, seconds=10.0):
        limiter = RateLimiter()
        n = int(camera_fps * seconds)
        return sum(limiter.ready(i / camera_fps, target_fps) for i in range(n)) / seconds

    def test_downsample_to_target(self):
        self.assertAlmostEqual(self.count(30, 10), 10, delta=0.5)
        self.assertAlmostEqual(self.count(15, 10), 10, delta=1.0)
        self.assertAlmostEqual(self.count(25, 10), 10, delta=1.0)

    def test_slower_camera_passes_everything(self):
        self.assertAlmostEqual(self.count(8, 10), 8, delta=0.2)


class Collector:
    def __init__(self):
        self.frames = []
        self.lock = threading.Lock()

    def __call__(self, jpeg, ts):
        with self.lock:
            self.frames.append((ts, time.monotonic(), jpeg))


def wait(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


class VideoFileTest(unittest.TestCase):
    """영상 파일은 파일 속도대로(실시간처럼) 내보낸다."""

    FPS, COUNT = 20.0, 12

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.path = os.path.join(cls.tmp.name, "clip.avi")
        writer = cv2.VideoWriter(cls.path, cv2.VideoWriter_fourcc(*"MJPG"), cls.FPS, (160, 120))
        for i in range(cls.COUNT):
            frame = np.full((120, 160, 3), i * 20, np.uint8)
            writer.write(frame)
        writer.release()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def run_camera(self, loop_file, seconds):
        collector = Collector()
        camera = Camera("opencv", self.path, loop_file=loop_file, video={"fps": 30})
        camera.set_sink(collector, lambda: True)
        camera.start()
        try:
            time.sleep(seconds)
            status = camera.status()
        finally:
            camera.stop()
        return collector.frames, status

    def test_source_is_file(self):
        source = make_source("auto", self.path, 640, 480, 10)
        self.assertIsInstance(source, OpenCVSource)
        self.assertTrue(source.is_file)
        source.open()
        self.assertAlmostEqual(source.file_fps, self.FPS, delta=0.1)
        source.close()

    def test_paced_at_file_speed_then_ends(self):
        frames, (ok, error) = self.run_camera(False, self.COUNT / self.FPS + 0.8)
        self.assertEqual(len(frames), self.COUNT)
        stamps = [ts for ts, _, _ in frames]
        gaps = np.diff(stamps)
        self.assertTrue(np.allclose(gaps, 1 / self.FPS, atol=0.01), gaps)  # 촬영 시각은 파일 간격 그대로
        self.assertAlmostEqual(frames[-1][1] - frames[0][1], (self.COUNT - 1) / self.FPS, delta=0.15)  # 실시간 속도
        self.assertFalse(ok)
        self.assertIn("끝", error)

    def test_loops_when_enabled(self):
        frames, (ok, _) = self.run_camera(True, 2 * self.COUNT / self.FPS + 0.3)
        self.assertGreater(len(frames), self.COUNT + 3)
        stamps = [ts for ts, _, _ in frames]
        self.assertEqual(stamps, sorted(stamps))
        self.assertTrue(ok)

    def test_missing_file_reports_error(self):
        camera = Camera("opencv", os.path.join(self.tmp.name, "none.avi"), retry_delays=(0.1,))
        camera.start()
        try:
            self.assertTrue(wait(lambda: "없습니다" in camera.status()[1], 2.0))
            self.assertFalse(camera.status()[0])
        finally:
            camera.stop()


class CameraRecoveryTest(unittest.TestCase):
    def test_reopens_after_stall(self):
        sources = []

        def opener(*args):
            sources.append(FakeLiveSource(fps=50, fail_after=5 if not sources else None))
            return sources[-1]

        collector = Collector()
        camera = Camera("opencv", None, opener=opener, retry_delays=(0.1,), stall_seconds=0.3, video={"fps": 50})
        camera.set_sink(collector, lambda: True)
        camera.start()
        try:
            self.assertTrue(wait(lambda: camera.opens >= 2 and camera.status()[0], 3.0))
            self.assertTrue(sources[0].closed)
            count = len(collector.frames)
            self.assertTrue(wait(lambda: len(collector.frames) > count + 5, 2.0))
        finally:
            camera.stop()

    def test_open_failure_reported_and_retried(self):
        calls = []

        def opener(*args):
            calls.append(args)
            raise RuntimeError("카메라 없음")

        camera = Camera("opencv", None, opener=opener, retry_delays=(0.05, 0.1))
        camera.start()
        try:
            self.assertTrue(wait(lambda: len(calls) >= 3, 2.0))
            self.assertEqual(camera.status(), (False, "카메라 없음"))
        finally:
            camera.stop()

    def test_resolution_change_reopens(self):
        sizes = []

        def opener(backend, source, width, height, fps):
            sizes.append((width, height))
            return FakeLiveSource()

        camera = Camera("opencv", None, opener=opener)
        camera.set_sink(Collector(), lambda: True)
        camera.start()
        try:
            self.assertTrue(wait(lambda: camera.frames_read > 2, 2.0))
            camera.apply({"jpeg_quality": 50})        # 품질만 바뀌면 다시 열지 않는다
            time.sleep(0.2)
            self.assertEqual(len(sizes), 1)
            camera.apply({"width": 320, "height": 240})
            self.assertTrue(wait(lambda: len(sizes) == 2, 2.0))
            self.assertEqual(sizes[-1], (320, 240))
        finally:
            camera.stop()

    def test_no_encoding_when_not_wanted(self):
        collector = Collector()
        camera = Camera("opencv", None, opener=lambda *a: FakeLiveSource())
        camera.set_sink(collector, lambda: False)
        camera.start()
        try:
            self.assertTrue(wait(lambda: camera.frames_read > 5, 2.0))
            self.assertEqual(collector.frames, [])
        finally:
            camera.stop()

    def test_disabled_camera(self):
        camera = Camera("none")
        camera.start()
        self.assertFalse(camera.status()[0])
        camera.stop()


if __name__ == "__main__":
    unittest.main()
