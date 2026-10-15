import struct
import tempfile
import unittest
from pathlib import Path

from server.config import ROOT_DIR, ConfigError, load_settings
from server.speech import BYTES_PER_SECOND, chime_pcm, duration, parse_wav, prepare_prompt, siren_pcm, to_protocol_pcm


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, text: str) -> Path:
        path = self.dir / "config.toml"
        path.write_text(text, encoding="utf-8")
        return path

    def test_defaults_without_file(self):
        s = load_settings(env={"FALL_SERVER_CONFIG": str(self.dir / "없음.toml")})
        self.assertEqual((s.host, s.port, s.device_token), ("0.0.0.0", 8000, ""))
        self.assertEqual(s.descent_velocity_peak, 1.5)
        self.assertFalse(s.notifier_configured)

    def test_file_then_env_then_args(self):
        path = self.write('device_token = "from-file"\nport = 9000\nlisten_seconds = 4\ndevice_name = "안방"\n')
        s = load_settings(path, env={"DEVICE_TOKEN": "from-env", "FALL_SERVER_PORT": "9100"}, host="127.0.0.1")
        self.assertEqual(s.device_token, "from-env")
        self.assertEqual(s.port, 9100)
        self.assertEqual(s.listen_seconds, 4.0)
        self.assertIsInstance(s.listen_seconds, float)
        self.assertEqual(s.device_name, "안방")
        self.assertEqual(s.host, "127.0.0.1")

    def test_unknown_key_and_bad_type(self):
        with self.assertRaises(ConfigError):
            load_settings(self.write("no_such_key = 1\n"), env={})
        with self.assertRaises(ConfigError):
            load_settings(self.write('port = "abc"\n'), env={})
        with self.assertRaises(ConfigError):
            load_settings(self.dir / "없음.toml", env={})

    def test_relative_paths_are_from_repo_root(self):
        s = load_settings(env={"FALL_SERVER_CONFIG": str(self.dir / "없음.toml")})
        self.assertEqual(s.path("data/x.db"), ROOT_DIR / "data" / "x.db")
        self.assertEqual(s.path(str(self.dir / "y.db")), self.dir / "y.db")


def wav_bytes(pcm: bytes, rate: int, channels: int, width: int, bogus_size: bool = False) -> bytes:
    fmt = struct.pack("<HHIIHH", 1, channels, rate, rate * channels * width, channels * width, width * 8)
    size = 0xFFFFFFFF if bogus_size else len(pcm)
    return (b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVE" + b"fmt " + struct.pack("<I", 16) + fmt
            + b"data" + struct.pack("<I", size) + pcm)


class SpeechTest(unittest.TestCase):
    def test_parse_wav_with_bogus_size(self):
        # espeak-ng --stdout은 크기 칸을 0xFFFFFFFF로 쓴다
        pcm = b"\x01\x00" * 100
        self.assertEqual(parse_wav(wav_bytes(pcm, 22050, 1, 2, bogus_size=True)), (22050, 1, 2, pcm))
        with self.assertRaises(ValueError):
            parse_wav(b"not a wav")

    def test_convert_to_protocol_format(self):
        stereo_44k = b"\x00\x10\x00\x10" * 44100  # 1초
        out = to_protocol_pcm(stereo_44k, 44100, 2, 2)
        self.assertAlmostEqual(duration(out), 1.0, delta=0.01)
        u8 = bytes([200]) * 8000  # 8kHz 8bit 1초
        self.assertAlmostEqual(duration(to_protocol_pcm(u8, 8000, 1, 1)), 1.0, delta=0.01)

    def test_generated_sounds(self):
        self.assertAlmostEqual(len(siren_pcm()) / BYTES_PER_SECOND, 2.0)
        self.assertGreater(len(chime_pcm()), BYTES_PER_SECOND // 2)

    def test_prompt_from_recorded_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "ask.wav"
            path.write_bytes(wav_bytes(b"\x00\x08" * 32000, 32000, 1, 2))
            sound = prepare_prompt("괜찮으신가요?", path)
            self.assertEqual(sound.source, "file")
            self.assertAlmostEqual(sound.seconds, 1.0, delta=0.01)


if __name__ == "__main__":
    unittest.main()
