import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from edge.config import ConfigError, EdgeConfig, build_parser, load, parse_source, server_urls
from edge.main import main
from shared import protocol


class ServerUrlTest(unittest.TestCase):
    def test_forms(self):
        cases = {
            "192.168.0.10": ("ws://192.168.0.10:8000/ws/device", "http://192.168.0.10:8000"),
            "192.168.0.10:9000": ("ws://192.168.0.10:9000/ws/device", "http://192.168.0.10:9000"),
            "http://server.local:8000/": ("ws://server.local:8000/ws/device", "http://server.local:8000"),
            "http://server.local:8000/kiosk": ("ws://server.local:8000/ws/device", "http://server.local:8000"),
            "ws://10.0.0.2:8000/ws/device": ("ws://10.0.0.2:8000/ws/device", "http://10.0.0.2:8000"),
            "wss://pc.example:8443": ("wss://pc.example:8443/ws/device", "https://pc.example:8443"),
            "[::1]:8000": ("ws://[::1]:8000/ws/device", "http://[::1]:8000"),
        }
        for given, expected in cases.items():
            self.assertEqual(server_urls(given), expected, given)

    def test_bad_addresses(self):
        for bad in ("", "   ", "ftp://x", "ws://", "host:notaport"):
            with self.assertRaises(ConfigError, msg=bad):
                server_urls(bad)


class LoadTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "config.toml"
        self.path.write_text(
            '[server]\naddress = "10.0.0.5"\ntoken = "file-token"\n'
            '[device]\nid = "living-room"\nname = "거실"\n'
            '[camera]\nbackend = "opencv"\nsource = 1\nrotation = 180\nloop_file = false\n'
            '[microphone]\nenabled = false\ndevice = "USB"\n'
            '[speaker]\nbackend = "null"\ndevice = 3\nvolume = 0.5\n'
            '[log]\nlevel = "debug"\n', encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def parse(self, *argv):
        return load(build_parser().parse_args(["--config", str(self.path), *argv]))

    def test_file_values(self):
        cfg = self.parse()
        self.assertEqual((cfg.server, cfg.token, cfg.device_id, cfg.name), ("10.0.0.5", "file-token", "living-room", "거실"))
        self.assertEqual((cfg.camera, cfg.source, cfg.rotation, cfg.loop_file), ("opencv", 1, 180, False))
        self.assertEqual((cfg.mic, cfg.mic_device), (False, "USB"))
        self.assertEqual((cfg.speaker, cfg.speaker_device, cfg.volume), ("null", 3, 0.5))
        self.assertEqual(cfg.log_level, "DEBUG")
        cfg.check()

    def test_arguments_override_file(self):
        cfg = self.parse("--server", "127.0.0.1:8001", "--token", "cli", "--device-id", "pc", "--source", "clip.avi",
                         "--camera", "auto", "--speaker", "sounddevice", "--volume", "0.1", "--no-mic")
        self.assertEqual((cfg.ws_url, cfg.token, cfg.device_id), ("ws://127.0.0.1:8001/ws/device", "cli", "pc"))
        self.assertEqual((cfg.source, cfg.camera, cfg.speaker, cfg.volume, cfg.mic), ("clip.avi", "auto", "sounddevice", 0.1, False))

    def test_missing_file_uses_defaults(self):
        cfg = load(build_parser().parse_args(["--config", str(Path(self.tmp.name) / "none.toml")]))
        self.assertEqual(cfg.camera, "auto")
        self.assertTrue(cfg.device_id)
        with self.assertRaises(ConfigError):
            cfg.check()   # 서버 주소·토큰이 없음

    def test_check(self):
        good = EdgeConfig(server="pc", token="t")
        good.check()
        for change in ({"token": ""}, {"rotation": 45}, {"volume": 2.0}, {"camera": "usb"}, {"speaker": "hdmi"}):
            cfg = EdgeConfig(server="pc", token="t")
            for key, value in change.items():
                setattr(cfg, key, value)
            with self.assertRaises(ConfigError, msg=change):
                cfg.check()

    def test_parse_source(self):
        self.assertEqual(parse_source("0"), 0)
        self.assertEqual(parse_source(""), None)
        self.assertEqual(parse_source(os.path.join("a", "b.avi")), os.path.join("a", "b.avi"))

    def test_print_url_for_kiosk_script(self):
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(main(["--config", str(self.path), "--print-url", "kiosk"]), 0)
        key = protocol.kiosk_key("file-token", "living-room")   # 서버가 같은 토큰으로 계산하는 값
        self.assertEqual(out.getvalue().strip(), f"http://10.0.0.5:8000/kiosk?device=living-room&key={key}")
        out = io.StringIO()
        with redirect_stdout(out):
            main(["--config", str(self.path), "--print-url", "health"])
        self.assertEqual(out.getvalue().strip(), "http://10.0.0.5:8000/api/health")

    def test_kiosk_url_quotes_id_and_needs_token(self):
        out = io.StringIO()
        with redirect_stdout(out):
            main(["--config", str(self.path), "--device-id", "거실 1/a", "--print-url", "kiosk"])
        key = protocol.kiosk_key("file-token", "거실 1/a")
        self.assertEqual(out.getvalue().strip(),
                         f"http://10.0.0.5:8000/kiosk?device=%EA%B1%B0%EC%8B%A4%201%2Fa&key={key}")
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):   # 토큰이 없으면 키를 만들 수 없다 (kiosk.sh는 기다렸다 다시 봄)
            self.assertEqual(main(["--config", str(self.path), "--token", "", "--print-url", "kiosk"]), 2)
        self.assertEqual(out.getvalue(), "")
        self.assertIn("토큰", err.getvalue())


if __name__ == "__main__":
    unittest.main()
