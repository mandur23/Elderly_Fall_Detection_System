"""가짜 서버에 실제로 접속해 규약대로 주고받는지 확인한다."""

import asyncio
import time
import unittest

import cv2
import numpy as np

from edge.camera import Camera
from edge.client import EdgeClient, retry_delay
from edge.config import EdgeConfig
from edge.speaker import NullSpeaker
from edge.tests.fake_server import FakeServer, tone
from edge.tests.fakes import FakeLiveSource, FakeMicrophone
from shared import protocol

TOKEN = "t0k-for-tests"
RETRY = (0.2, 0.4)


def frame_size(jpeg: bytes):
    image = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
    return image.shape[1], image.shape[0]


class EdgeTestCase(unittest.IsolatedAsyncioTestCase):
    client_token = TOKEN
    welcome_video = {"width": 320, "height": 240, "fps": 10, "jpeg_quality": 70}
    welcome_protocol = protocol.PROTOCOL_VERSION
    auto_welcome = True

    async def asyncSetUp(self):
        self.server = FakeServer(token=TOKEN, welcome_video=self.welcome_video, welcome_protocol=self.welcome_protocol)
        self.server.auto_welcome = self.auto_welcome
        await self.server.start()
        cfg = EdgeConfig(server=self.server.address, token=self.client_token, device_id="test-pi", name="시험실")
        self.camera = Camera("opencv", None, opener=lambda *a: FakeLiveSource(), stall_seconds=1.0, retry_delays=RETRY)
        self.mic = FakeMicrophone()
        self.speaker = NullSpeaker()
        for part in (self.camera, self.mic, self.speaker):
            part.start()
        self.client = EdgeClient(cfg, self.camera, self.mic, self.speaker, retry_delays=RETRY,
                                 status_interval=0.2, welcome_timeout=2.0)
        self.task = asyncio.create_task(self.client.run())

    async def asyncTearDown(self):
        self.client.stop()
        await asyncio.wait_for(self.task, 5.0)
        self.camera.stop()
        self.mic.stop()
        self.speaker.close()
        await self.server.stop()

    async def until(self, predicate, timeout=5.0, message=""):
        ok = await self.server.wait_until(predicate, timeout)
        self.assertTrue(ok, message or "시간 안에 조건이 맞지 않았습니다")

    async def connected(self):
        await self.until(lambda: self.server.hellos and self.client.connected, message="접속하지 못했습니다")


class HandshakeAndVideoTest(EdgeTestCase):
    async def test_hello_and_welcome_settings(self):
        await self.connected()
        hello = self.server.hellos[0]
        self.assertEqual(self.server.auth_headers[0], f"Bearer {TOKEN}")
        self.assertEqual(hello["protocol"], protocol.PROTOCOL_VERSION)
        self.assertEqual(hello["device_id"], "test-pi")
        self.assertEqual(hello["name"], "시험실")
        self.assertEqual(hello["video"]["format"], "jpeg")
        self.assertEqual(hello["audio"], {"rate": 16000, "channels": 1, "format": "s16le"})
        # welcome의 해상도(320x240)가 640x480 카메라 장면에 적용된다
        await self.until(lambda: len(self.server.frames) >= 3)
        self.assertEqual(frame_size(self.server.frames[-1][2]), (320, 240))

    async def test_video_rate_and_capture_timestamps(self):
        await self.connected()
        await asyncio.sleep(1.6)
        frames = list(self.server.frames)
        self.assertGreaterEqual(len(frames), 10)
        stamps = [ts for ts, _, _ in frames]
        self.assertEqual(stamps, sorted(stamps))
        self.assertEqual(len(set(stamps)), len(stamps))
        for ts, received, _ in frames:
            self.assertLessEqual(ts, received)          # 같은 시계(time.monotonic)
            self.assertLess(received - ts, 0.5)
        fps = (len(frames) - 1) / (stamps[-1] - stamps[0])
        self.assertAlmostEqual(fps, 10, delta=2.0)      # 30fps 카메라에서 10fps로 골라 보냄

    async def test_config_changes_rate_and_size(self):
        await self.connected()
        await self.server.send(protocol.CONFIG, video={"width": 160, "height": 120, "fps": 4})
        await asyncio.sleep(0.6)
        start = len(self.server.frames)
        await asyncio.sleep(1.6)
        frames = self.server.frames[start:]
        self.assertTrue(frames)
        self.assertEqual(frame_size(frames[-1][2]), (160, 120))
        fps = (len(frames) - 1) / (frames[-1][0] - frames[0][0])
        self.assertAlmostEqual(fps, 4, delta=1.0)

    async def test_status_every_interval(self):
        await self.connected()
        await self.until(lambda: any(s["fps"] > 0 for s in self.server.statuses))
        status = self.server.statuses[-1]
        self.assertTrue(status["camera_ok"])
        self.assertTrue(status["mic_ok"])
        self.assertTrue(status["speaker_ok"])
        self.assertIn("uptime_s", status)
        self.assertNotIn("camera_error", status)

    async def test_latest_frame_only_while_sending(self):
        """보내는 중이면 새 장면을 버린다(쌓지 않음)."""
        await self.connected()
        self.client._sending_video = True
        dropped = self.client.frames_dropped
        self.client._offer_frame(b"jpeg", time.monotonic())
        self.assertEqual(self.client.frames_dropped, dropped + 1)
        self.assertFalse(self.client._wants_frame())   # 카메라 스레드는 인코딩도 하지 않는다
        self.client._sending_video = False


class MicTest(EdgeTestCase):
    async def test_mic_only_when_enabled(self):
        await self.connected()
        await asyncio.sleep(0.4)
        self.assertEqual(self.server.audio, [], "켜기 전에는 마이크 소리를 보내면 안 됩니다")

        await self.server.send(protocol.MIC, enabled=True)
        await self.until(lambda: len(self.server.audio) >= 5)
        for ts, received, pcm in self.server.audio:
            self.assertEqual(len(pcm), protocol.AUDIO_CHUNK_BYTES)
            self.assertLessEqual(ts, received)
        gaps = [b[0] - a[0] for a, b in zip(self.server.audio, self.server.audio[1:])]
        for gap in gaps:
            self.assertAlmostEqual(gap, 0.1, delta=0.03)

        await self.server.send(protocol.MIC, enabled=False)
        await asyncio.sleep(0.3)
        count = len(self.server.audio)
        await asyncio.sleep(0.4)
        self.assertEqual(len(self.server.audio), count, "끈 뒤에도 마이크 소리를 보냈습니다")


class PlaybackTest(EdgeTestCase):
    async def test_playback_done_after_clip(self):
        await self.connected()
        start = time.monotonic()
        await self.server.play("p1", tone(0.3))
        await self.until(lambda: self.server.done)
        received, done = self.server.done[0]
        self.assertEqual(done, {"type": "playback_done", "id": "p1"})
        self.assertGreaterEqual(received - start, 0.25)

    async def test_numeric_id_and_sequential_clips(self):
        await self.connected()
        await self.server.play(7, tone(0.1))
        await self.server.play("second", tone(0.1))
        await self.until(lambda: len(self.server.done) >= 2)
        self.assertEqual([d["id"] for _, d in self.server.done], [7, "second"])
        self.assertTrue(all("stopped" not in d for _, d in self.server.done))

    async def test_loop_until_play_stop(self):
        await self.connected()
        await self.server.play("siren", tone(0.1), loop=True)
        await asyncio.sleep(0.6)
        self.assertEqual(self.server.done, [], "반복 재생은 play_stop 전에 끝나면 안 됩니다")
        self.assertTrue(self.speaker.buffer.busy)
        await self.server.send(protocol.PLAY_STOP)
        await self.until(lambda: self.server.done, timeout=1.0)
        self.assertEqual(self.server.done[0][1], {"type": "playback_done", "id": "siren", "stopped": True})
        self.assertFalse(self.speaker.buffer.busy)

    async def test_play_stop_mid_clip(self):
        await self.connected()
        await self.server.play("long", tone(2.0))
        await asyncio.sleep(0.2)
        stop_at = time.monotonic()
        await self.server.send(protocol.PLAY_STOP)
        await self.until(lambda: self.server.done, timeout=1.0)
        received, done = self.server.done[0]
        self.assertEqual(done, {"type": "playback_done", "id": "long", "stopped": True})
        self.assertLess(received - stop_at, 0.3)
        await asyncio.sleep(0.3)
        self.assertEqual(len(self.server.done), 1)

    async def test_new_clip_cuts_loop(self):
        await self.connected()
        await self.server.play("siren", tone(0.1), loop=True)
        await asyncio.sleep(0.3)
        await self.server.play("voice", tone(0.2))
        await self.until(lambda: len(self.server.done) >= 2)
        self.assertEqual([d for _, d in self.server.done],
                         [{"type": "playback_done", "id": "siren", "stopped": True},
                          {"type": "playback_done", "id": "voice"}])


class ReconnectTest(EdgeTestCase):
    async def test_reconnect_stops_playback_and_mic(self):
        await self.connected()
        await self.server.send(protocol.MIC, enabled=True)
        await self.server.play("siren", tone(0.1), loop=True)
        await self.until(lambda: len(self.server.audio) >= 2)
        frames_before = len(self.server.frames)
        last_ts = self.server.frames[-1][0]

        dropped_at = time.monotonic()
        await self.server.drop()
        await self.until(lambda: len(self.server.hellos) >= 2, timeout=3.0, message="다시 접속하지 않았습니다")
        self.assertLess(time.monotonic() - dropped_at, 1.5)       # 첫 재시도는 짧게
        self.assertFalse(self.speaker.buffer.busy, "끊기면 재생을 멈춰야 합니다")
        self.assertFalse(self.mic.active, "끊기면 마이크를 꺼야 합니다")

        await self.until(lambda: len(self.server.frames) > frames_before + 3)
        self.assertGreater(self.server.frames[-1][0], last_ts)
        count = len(self.server.audio)
        await asyncio.sleep(0.4)
        self.assertEqual(len(self.server.audio), count, "다시 접속한 뒤에는 서버가 켤 때까지 마이크를 보내면 안 됩니다")
        self.assertEqual(self.client.connections, 2)

    async def test_reconnects_when_server_restarts(self):
        await self.connected()
        port = self.server.port
        await self.server.stop()
        await asyncio.sleep(0.5)
        self.assertFalse(self.client.connected)
        self.server = FakeServer(token=TOKEN, welcome_video=self.welcome_video, port=port)
        await self.server.start()
        await self.until(lambda: self.server.hellos, timeout=3.0, message="서버가 다시 뜬 뒤 접속하지 않았습니다")


class BadTokenTest(EdgeTestCase):
    client_token = "wrong"

    async def test_rejected_and_retried(self):
        await self.until(lambda: self.server.auth_failures >= 2, timeout=3.0)
        self.assertEqual(self.server.hellos, [])
        self.assertFalse(self.client.connected)


class ProtocolMismatchTest(EdgeTestCase):
    welcome_protocol = 99

    async def test_reports_error_and_closes(self):
        await self.until(lambda: self.server.errors and self.server.close_codes)
        self.assertIn("규약 버전", self.server.errors[0]["message"])
        self.assertEqual(self.server.close_codes[0], protocol.CLOSE_PROTOCOL_ERROR)
        self.assertEqual(self.server.frames, [])


class NoWelcomeTest(EdgeTestCase):
    auto_welcome = False

    async def test_no_video_before_welcome_and_prompt_stop(self):
        await self.until(lambda: self.server.hellos)
        await asyncio.sleep(0.3)
        self.assertFalse(self.client.connected)
        self.assertEqual(self.server.frames, [])
        self.assertEqual(self.server.statuses, [])
        started = time.monotonic()
        self.client.stop()                       # welcome을 기다리는 중에도 바로 끝나야 한다
        await asyncio.wait_for(self.task, 2.0)
        self.assertLess(time.monotonic() - started, 1.0)


class RetryDelayTest(unittest.TestCase):
    def test_backoff_sequence(self):
        self.assertEqual([retry_delay(i) for i in range(6)], [1, 2, 5, 10, 10, 10])

    def test_long_wait_when_retrying_cannot_help(self):
        for code in (401, protocol.CLOSE_PROTOCOL_ERROR, protocol.CLOSE_REPLACED):
            self.assertEqual(retry_delay(0, close_code=code), 10)
        self.assertEqual(retry_delay(0, close_code=1006), 1)


if __name__ == "__main__":
    unittest.main()
