import asyncio
import sys
import threading
import time
import unittest
from unittest import mock

from edge.mic import ChunkAssembler, SoundDeviceMicrophone
from edge.speaker import BYTES_PER_SECOND, NullSpeaker, PlaybackBuffer, SoundDeviceSpeaker
from shared import protocol


class FakeStream:
    def __init__(self, callback):
        self.callback, self.active, self.device, self.latency = callback, False, 0, 0.05

    def start(self):
        self.active = True

    def abort(self):
        self.active = False

    def close(self):
        self.active = False


class FakeSoundDevice:
    """실제 장치를 열지 않는 sounddevice 흉내. fail이 True인 동안은 장치를 열 수 없다(USB 장치를 뺀 상태)."""

    def __init__(self):
        self.fail = True
        self.opens = 0
        self.streams = []

    def _open(self, **kwargs):
        self.opens += 1
        if self.fail:
            raise OSError("장치 없음")
        self.streams.append(FakeStream(kwargs["callback"]))
        return self.streams[-1]

    RawInputStream = RawOutputStream = _open


def wait(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


class ChunkAssemblerTest(unittest.TestCase):
    def test_cuts_into_chunks_with_first_sample_time(self):
        asm = ChunkAssembler()
        out = asm.push(b"\x01" * 1000, 10.0)
        self.assertEqual(out, [])
        out = asm.push(b"\x02" * 6000, 10.0 + 1000 / 32000)
        self.assertEqual([len(c) for c, _ in out], [3200, 3200])
        self.assertEqual(out[0][0][:1000], b"\x01" * 1000)
        self.assertAlmostEqual(out[0][1], 10.0)
        self.assertAlmostEqual(out[1][1], 10.1)
        out = asm.push(b"\x03" * 2600, 10.0 + 7000 / 32000)  # 남은 600 + 2600 = 3200
        self.assertEqual(len(out), 1)
        self.assertAlmostEqual(out[0][1], 10.2)

    def test_gap_discards_partial_chunk(self):
        asm = ChunkAssembler()
        asm.push(b"\x01" * 1000, 5.0)
        out = asm.push(b"\x02" * 3200, 7.0)  # 녹음이 끊겼다가 다시 옴
        self.assertEqual(out, [(b"\x02" * 3200, 7.0)])

    def test_chunk_size_matches_protocol(self):
        self.assertEqual(ChunkAssembler().chunk_bytes, protocol.AUDIO_CHUNK_BYTES)


class PlaybackBufferTest(unittest.TestCase):
    def test_plays_clips_in_order(self):
        buf = PlaybackBuffer()
        buf.begin("a")
        buf.feed(b"\x01\x00" * 3)
        buf.end("a")
        buf.begin("b")
        buf.feed(b"\x02\x00" * 2)
        buf.end("b")
        data, finished = buf.read(20)
        self.assertEqual(data, b"\x01\x00" * 3 + b"\x02\x00" * 2 + bytes(10))
        self.assertEqual(finished, [("a", 6), ("b", 10)])
        self.assertFalse(buf.busy)

    def test_waits_with_silence_until_end(self):
        buf = PlaybackBuffer()
        buf.begin("a")
        buf.feed(b"\x01\x00")
        self.assertEqual(buf.read(6), (b"\x01\x00" + bytes(4), []))   # 더 올 소리를 기다림
        buf.feed(b"\x02\x00")
        buf.end("a")
        self.assertEqual(buf.read(4), (b"\x02\x00" + bytes(2), [("a", 2)]))

    def test_loop_repeats_until_stop(self):
        buf = PlaybackBuffer()
        buf.begin("siren", loop=True)
        buf.feed(b"\x01\x00\x02\x00")
        buf.end("siren")
        data, finished = buf.read(12)
        self.assertEqual(data, b"\x01\x00\x02\x00" * 3)
        self.assertEqual(finished, [])
        self.assertEqual(buf.stop(), ["siren"])
        self.assertEqual(buf.read(4), (bytes(4), []))

    def test_new_clip_cuts_loop(self):
        buf = PlaybackBuffer()
        buf.begin("siren", loop=True)
        buf.feed(b"\x01\x00")
        buf.end("siren")
        self.assertEqual(buf.begin("voice"), ["siren"])
        buf.feed(b"\x05\x00")
        buf.end("voice")
        self.assertEqual(buf.read(4), (b"\x05\x00" + bytes(2), [("voice", 2)]))

    def test_audio_without_begin_is_ignored(self):
        buf = PlaybackBuffer()
        self.assertFalse(buf.feed(b"\x01\x00"))
        buf.begin("a")
        buf.end("a")
        self.assertFalse(buf.feed(b"\x01\x00"))   # play_end 뒤에 온 소리
        self.assertEqual(buf.read(2), (bytes(2), [("a", 0)]))

    def test_odd_byte_and_size_limit(self):
        buf = PlaybackBuffer(max_clip_bytes=4)
        buf.begin("a")
        buf.feed(b"\x01\x00\x02")   # 홀수 바이트는 마지막 바이트를 버림
        buf.feed(b"\x03\x00\x04\x00")
        buf.end("a")
        data, _ = buf.read(8)
        self.assertEqual(data, b"\x01\x00\x03\x00" + bytes(4))

    def test_begin_without_end_finishes_previous_clip(self):
        """play_end를 받지 못한 소리가 있어도 다음 소리가 막히지 않는다(받은 데까지 재생)."""
        buf = PlaybackBuffer()
        buf.begin("a")
        buf.feed(b"\x01\x00")
        buf.begin("b")                  # a의 play_end가 오지 않음
        buf.feed(b"\x02\x00")
        buf.end("b")
        self.assertEqual(buf.read(6), (b"\x01\x00\x02\x00" + bytes(2), [("a", 2), ("b", 4)]))
        self.assertFalse(buf.busy)


class SpeakerTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.done = []
        self.speaker = NullSpeaker()
        self.speaker.attach(asyncio.get_running_loop(), lambda i, s: self.done.append((i, s)))
        self.speaker.start()

    async def asyncTearDown(self):
        self.speaker.close()

    async def test_done_after_real_time(self):
        loop = asyncio.get_running_loop()
        start = loop.time()
        self.speaker.begin("a")
        self.speaker.feed(bytes(int(BYTES_PER_SECOND * 0.2)))
        self.speaker.end("a")
        while not self.done and loop.time() - start < 2:
            await asyncio.sleep(0.01)
        self.assertEqual(self.done, [("a", False)])
        self.assertGreaterEqual(loop.time() - start, 0.18)

    async def test_stop_reports_every_unfinished_clip(self):
        for clip in ("a", "b"):
            self.speaker.begin(clip)
            self.speaker.feed(bytes(BYTES_PER_SECOND))
            self.speaker.end(clip)
        await asyncio.sleep(0.1)
        self.assertEqual(self.speaker.stop(), ["a", "b"])
        self.assertEqual(self.done, [("a", True), ("b", True)])

    async def test_stop_without_notify(self):
        self.speaker.begin("a", loop=True)
        self.speaker.feed(bytes(320))
        self.speaker.end("a")
        self.assertEqual(self.speaker.stop(notify=False), ["a"])
        await asyncio.sleep(0.1)
        self.assertEqual(self.done, [])

    def test_volume(self):
        speaker = NullSpeaker(volume=0.5)
        speaker.begin("a")
        speaker.feed((1000).to_bytes(2, "little", signed=True))
        speaker.end("a")
        data = speaker.render(2, 0.0)
        self.assertEqual(int.from_bytes(data, "little", signed=True), 500)


class DeviceFailureTest(unittest.TestCase):
    """USB 마이크·스피커를 열지 못하는 시간과 사운드 카드 변화(다시 시작해야 PortAudio가 새 장치를 찾음)."""

    def setUp(self):
        self.sd = FakeSoundDevice()
        self.cards = ["0 [vc4hdmi0]\n2 [Device]: USB-Audio"]
        patcher = mock.patch.dict(sys.modules, {"sounddevice": self.sd})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_mic_failing_time_and_card_change(self):
        mic = SoundDeviceMicrophone(retry_delays=(0.02,), cards=lambda: self.cards[0])
        self.assertEqual(mic.failing_for(), 0.0)
        with self.assertLogs("edge.mic", level="WARNING"):
            mic.start()
            try:
                self.assertTrue(wait(lambda: self.sd.opens >= 3, 2.0))
                time.sleep(0.1)
                self.assertGreater(mic.failing_for(), 0.1)
                self.assertIn("열 수 없습니다", mic.status()[1])
                self.assertFalse(mic.devices_changed(), "카드가 그대로면 다시 시작해도 같다")
                self.cards[0] = "0 [vc4hdmi0]"         # USB 장치를 뺐다
                self.assertTrue(mic.devices_changed())
            finally:
                mic.stop()

    def test_mic_recovers_when_device_works(self):
        mic = SoundDeviceMicrophone(retry_delays=(0.02,), cards=lambda: self.cards[0])
        feeding = threading.Event()

        def feed():   # 열린 스트림에 0.1초마다 녹음 콜백을 부른다
            while not feeding.wait(0.1):
                for stream in list(self.sd.streams):
                    if stream.active:
                        stream.callback(bytes(protocol.AUDIO_CHUNK_BYTES), 1600, None, None)

        feeder = threading.Thread(target=feed, daemon=True)
        feeder.start()
        with self.assertLogs("edge.mic", level="INFO"):
            mic.start()
            try:
                self.assertTrue(wait(lambda: mic.failing_for() > 0.1, 2.0))
                self.sd.fail = False                     # 장치가 다시 열림
                self.assertTrue(wait(lambda: mic.failing_for() == 0.0 and mic.status()[0], 3.0))
            finally:
                mic.stop()
                feeding.set()
                feeder.join(1.0)

    def test_speaker_failing_time_and_card_change(self):
        speaker = SoundDeviceSpeaker(retry_delays=(0.02,), cards=lambda: self.cards[0])
        with self.assertLogs("edge.speaker", level="WARNING"):
            speaker.start()
            try:
                self.assertTrue(wait(lambda: self.sd.opens >= 3, 2.0))
                time.sleep(0.1)
                self.assertGreater(speaker.failing_for(), 0.1)
                self.assertFalse(speaker.status()[0])
                self.assertFalse(speaker.devices_changed())
                self.cards[0] += "\n3 [Device_1]: USB-Audio"   # 다시 꽂아 새 번호로 잡혔다
                self.assertTrue(speaker.devices_changed())
            finally:
                speaker.close()

    def test_unknown_cards_never_changed(self):
        """사운드 카드 목록을 읽을 수 없으면(리눅스가 아님) 다시 시작하지 않는다."""
        mic = SoundDeviceMicrophone(retry_delays=(0.02,), cards=lambda: None)
        with self.assertLogs("edge.mic", level="WARNING"):
            mic.start()
            try:
                self.assertTrue(wait(lambda: self.sd.opens >= 2, 2.0))
                self.assertFalse(mic.devices_changed())
            finally:
                mic.stop()


if __name__ == "__main__":
    unittest.main()
