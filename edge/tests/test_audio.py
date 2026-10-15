import asyncio
import unittest

from edge.mic import ChunkAssembler
from edge.speaker import BYTES_PER_SECOND, NullSpeaker, PlaybackBuffer
from shared import protocol


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


if __name__ == "__main__":
    unittest.main()
