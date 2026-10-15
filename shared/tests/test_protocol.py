import json
import unittest

from shared import protocol as p


class BinaryTest(unittest.TestCase):
    def test_round_trip(self):
        data = p.pack(p.VIDEO, 12.345, b"\xff\xd8jpeg")
        self.assertEqual(len(data), p.HEADER_SIZE + 6)
        self.assertEqual(p.unpack(data), (p.VIDEO, 12.345, b"\xff\xd8jpeg"))

    def test_rejects_short_and_unknown(self):
        with self.assertRaises(p.ProtocolError):
            p.unpack(b"\x01\x00")
        with self.assertRaises(p.ProtocolError):
            p.unpack(bytes([0x7F]) + b"\x00" * 8)
        with self.assertRaises(p.ProtocolError):
            p.pack(0x7F, 0.0, b"")

    def test_audio_chunk_size(self):
        self.assertEqual(p.AUDIO_CHUNK_BYTES, 3200)  # 0.1초, 16kHz, 16비트 mono


class JsonTest(unittest.TestCase):
    def test_round_trip(self):
        text = p.encode(p.HELLO, protocol=p.PROTOCOL_VERSION, device_id="living-room",
                        video=p.DEFAULT_VIDEO, audio={"rate": p.AUDIO_RATE})
        msg = p.decode(text)
        self.assertEqual(msg["type"], p.HELLO)
        self.assertEqual(msg["device_id"], "living-room")
        self.assertIn("한", p.encode(p.ERROR, message="한글"))  # ensure_ascii=False

    def test_missing_fields_and_unknown_type(self):
        with self.assertRaises(p.ProtocolError):
            p.encode(p.MIC)
        with self.assertRaises(p.ProtocolError):
            p.decode(json.dumps({"type": "mic"}))
        with self.assertRaises(p.ProtocolError):
            p.decode(json.dumps({"type": "nope"}))
        with self.assertRaises(p.ProtocolError):
            p.decode("not json")
        with self.assertRaises(p.ProtocolError):
            p.decode("[1, 2]")

    def test_every_type_has_a_direction(self):
        self.assertEqual(set(p.REQUIRED), p.EDGE_TO_SERVER | p.SERVER_TO_EDGE)

    def test_bearer(self):
        header = p.bearer("abc")[p.AUTH_HEADER]
        self.assertEqual(p.token_from_header(header), "abc")
        self.assertEqual(p.token_from_header("Basic abc"), "")
        self.assertEqual(p.token_from_header(""), "")


if __name__ == "__main__":
    unittest.main()
