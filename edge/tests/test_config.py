import io
import os
import tempfile
import tomllib
import unittest
import urllib.parse
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from edge.config import (ConfigError, EdgeConfig, build_parser, load, parse_source, save_settings, server_urls,
                         toml_string)
from edge.main import WAITING_PAGE, main
from shared import protocol

EXAMPLE = Path(__file__).resolve().parents[1] / "config.example.toml"


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

    def waiting(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(["--config", str(self.path), *argv, "--print-url", "waiting"])
        parts = urllib.parse.urlsplit(out.getvalue().strip())
        return code, parts, urllib.parse.parse_qs(parts.query)

    def test_waiting_page_url_for_kiosk_script(self):
        code, parts, query = self.waiting("--name", "거실 1")
        self.assertEqual(code, 0)
        self.assertEqual(parts.scheme, "file")
        self.assertTrue(parts.path.endswith("/edge/deploy/waiting.html"))
        self.assertTrue(WAITING_PAGE.exists())
        key = protocol.kiosk_key("file-token", "living-room")
        self.assertEqual(query["kiosk"], [f"http://10.0.0.5:8000/kiosk?device=living-room&key={key}"])
        self.assertEqual(query["health"], ["http://10.0.0.5:8000/api/health"])
        self.assertEqual(query["server"], ["http://10.0.0.5:8000"])
        self.assertEqual(query["name"], ["거실 1"])
        self.assertNotIn("error", query)
        self.assertNotIn("+", parts.query)   # 공백은 %20 (대기 화면의 URLSearchParams가 그대로 읽음)

    def test_waiting_page_shows_config_error(self):
        code, _, query = self.waiting("--token", "")   # 토큰이 없으면 안내가 붙은 주소와 종료 코드 2
        self.assertEqual(code, 2)
        self.assertIn("토큰", query["error"][0])
        self.assertNotIn("kiosk", query)
        self.assertEqual(query["server"], ["http://10.0.0.5:8000"])
        self.path.write_text("[server\naddress = ", encoding="utf-8")   # 설정 파일을 읽을 수 없어도 안내
        code, parts, query = self.waiting()
        self.assertEqual(code, 2)
        self.assertTrue(parts.path.endswith("/waiting.html"))
        self.assertIn("설정 파일", query["error"][0])

    def test_template_token_counts_as_missing(self):
        self.path.write_bytes(EXAMPLE.read_bytes())
        cfg = self.parse()
        self.assertEqual(cfg.token, "")
        with self.assertRaises(ConfigError):
            cfg.check()
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(main(["--config", str(self.path), "--print-url", "kiosk"]), 2)


class SaveSettingsTest(unittest.TestCase):
    """설치 스크립트가 쓰는 설정 저장(python -m edge --save-config)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "config.toml"

    def data(self):
        return tomllib.loads(self.path.read_text(encoding="utf-8"))

    def test_toml_string_escapes(self):
        for value in ("거실", "안방 침대", 'say "hi"', "back\\slash", "tab\there", "new\nline", "\x7f", "#1 '방'"):
            self.assertEqual(tomllib.loads(f"k = {toml_string(value)}")["k"], value)

    def test_updates_values_and_keeps_comments_and_other_settings(self):
        original = EXAMPLE.read_bytes()
        self.path.write_bytes(original)
        name = "거실 '큰방' #1 \"가운데\""
        save_settings(self.path, {("server", "address"): "192.168.0.20:8000", ("server", "token"): "a\"b\\c",
                                  ("device", "id"): "living-2", ("device", "name"): name})
        data = self.data()
        self.assertEqual(data["server"], {"address": "192.168.0.20:8000", "token": "a\"b\\c"})
        self.assertEqual(data["device"], {"id": "living-2", "name": name})
        self.assertEqual(data["camera"]["backend"], "auto")
        self.assertEqual(data["speaker"]["volume"], 1.0)
        text = self.path.read_bytes().decode("utf-8")
        self.assertIn("# 장치 구분자", text)          # 줄 끝 주석
        self.assertIn("# PC 서버 주소", text)         # 주석 줄
        old_lines, new_lines = original.decode("utf-8").splitlines(), text.splitlines()
        self.assertEqual(len(old_lines), len(new_lines))
        self.assertEqual(sum(a != b for a, b in zip(old_lines, new_lines)), 4, "바꾼 네 줄 말고는 그대로")
        self.assertEqual(b"\r\n" in original, b"\r\n" in self.path.read_bytes(), "줄바꿈 방식을 유지")
        cfg = load(build_parser().parse_args(["--config", str(self.path)]))
        self.assertEqual((cfg.server, cfg.token, cfg.device_id, cfg.name), ("192.168.0.20:8000", "a\"b\\c", "living-2", name))

    def test_creates_file_and_missing_keys(self):
        save_settings(self.path, {("server", "token"): "t1", ("device", "name"): "현관"})
        self.assertEqual(self.data(), {"server": {"token": "t1"}, "device": {"name": "현관"}})
        self.path.write_text('[server]\naddress = "pc"   # 서버\n\n# 카메라\n[camera]\nbackend = "opencv"\n',
                             encoding="utf-8")
        save_settings(self.path, {("server", "token"): "t2", ("device", "id"): "pi-9"})
        data = self.data()
        self.assertEqual(data["server"], {"address": "pc", "token": "t2"})
        self.assertEqual(data["camera"], {"backend": "opencv"})
        self.assertEqual(data["device"], {"id": "pi-9"})
        self.assertIn('address = "pc"   # 서버', self.path.read_text(encoding="utf-8"))

    def test_crlf_file_stays_crlf(self):
        self.path.write_bytes(b'[server]\r\naddress = "a"\r\n')
        save_settings(self.path, {("server", "address"): "b"})
        raw = self.path.read_bytes()
        self.assertEqual(raw.count(b"\n"), raw.count(b"\r\n"))
        self.assertEqual(self.data()["server"]["address"], "b")

    def test_broken_file_is_left_alone(self):
        self.path.write_text('[server]\naddress = """\nmulti\n"""\n', encoding="utf-8")
        before = self.path.read_bytes()
        with self.assertRaises(ConfigError):
            save_settings(self.path, {("server", "address"): "b"})
        self.assertEqual(self.path.read_bytes(), before)

    @unittest.skipUnless(os.name == "posix", "파일 권한은 POSIX에서만 확인")
    def test_private_permissions(self):
        self.path.write_text('[server]\naddress = "a"\n', encoding="utf-8")
        os.chmod(self.path, 0o644)
        save_settings(self.path, {("server", "token"): "secret"})
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)

    def test_save_config_command(self):
        self.path.write_bytes(EXAMPLE.read_bytes())
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(["--config", str(self.path), "--save-config", "--server", "10.0.0.9", "--token", "secret-token",
                         "--device-id", "pi-1", "--name", "안방 침대"])
        self.assertEqual(code, 0)
        self.assertNotIn("secret-token", out.getvalue(), "토큰은 화면에 보이지 않아야 합니다")
        cfg = load(build_parser().parse_args(["--config", str(self.path)]))
        self.assertEqual((cfg.server, cfg.token, cfg.device_id, cfg.name), ("10.0.0.9", "secret-token", "pi-1", "안방 침대"))
        cfg.check()
        before = self.path.read_bytes()
        for argv in (["--server", "ftp://x"], ["--token", " "], []):
            with self.subTest(argv=argv), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(main(["--config", str(self.path), "--save-config", *argv]), 2)
            self.assertEqual(self.path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
