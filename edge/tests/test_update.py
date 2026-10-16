"""자동 업데이트(edge/update.py)와 서버 버전이 다를 때의 다시 시작(client.py, 종료 코드 3).

작은 HTTP 서버(빈 포트)가 /edge/version과 /edge/bundle.tar.gz를 내주고, 임시 설치 폴더가 바르게 바뀌는지 본다.
pip는 실제로 실행하지 않는다(가짜 함수).
"""

import asyncio
import io
import os
import tarfile
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from edge import update
from edge.camera import Camera
from edge.client import EdgeClient
from edge.config import EdgeConfig
from edge.main import _run
from edge.speaker import NullSpeaker
from edge.tests.fake_server import FakeServer
from edge.tests.fakes import FakeMicrophone

TOKEN = "t0k-for-tests"
REQ = "edge/deploy/requirements-pi.txt"

OLD = {
    "edge/__init__.py": b"old init\n",
    "edge/__main__.py": b"old main\n",
    REQ: b"aiohttp>=3.8\r\nsounddevice>=0.4.6\r\n",
    "edge/deploy/kiosk.sh": b"#!/bin/bash\necho old\n",
    "shared/protocol.py": b"old protocol\n",
}
NEW = {
    "edge/__init__.py": b"new init\n",
    "edge/__main__.py": b"new main\n",
    "edge/update.py": b"new module\n",
    REQ: b"aiohttp>=3.8\r\nsounddevice>=0.4.6\r\n",
    "edge/deploy/kiosk.sh": b"#!/bin/bash\necho new\n",
    "shared/protocol.py": b"new protocol\n",
    "edge/VERSION": b"v2\n",
}


def make_bundle(files, extra=()):
    """{경로: 내용} 파일(서버가 Windows라 실행 권한 없음)과 extra[(TarInfo, 내용 또는 None)]로 tar.gz를 만든다."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), 0o644
            tar.addfile(info, io.BytesIO(data))
        for info, data in extra:
            tar.addfile(info, None if data is None else io.BytesIO(data))
    return buf.getvalue()


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        server = self.server
        server.paths.append(self.path)
        if self.path == update.VERSION_PATH and server.version is not None:
            self._send(200, server.version.encode())
        elif self.path == update.BUNDLE_PATH and server.bundle is not None:
            if self.headers.get("Authorization") != f"Bearer {server.token}":
                self._send(401, b"unauthorized")
            else:
                self._send(200, server.bundle, "application/gzip")
        else:
            self._send(404, b"not found")

    def _send(self, code, body, ctype="text/plain"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class BundleServer(ThreadingHTTPServer):
    """버전과 묶음을 내주는 시험용 서버 (127.0.0.1의 빈 포트)."""
    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), _Handler)
        self.paths, self.version, self.bundle, self.token = [], None, None, TOKEN
        self._closed = False
        threading.Thread(target=self.serve_forever, daemon=True).start()

    @property
    def address(self):
        return f"127.0.0.1:{self.server_address[1]}"

    def close(self):
        if not self._closed:
            self._closed = True
            self.shutdown()
            self.server_close()


class UpdateTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name) / "fall-edge"
        self.attempts = Path(tmp.name) / "cache" / "update-attempt.json"
        self.server = BundleServer()
        self.addCleanup(self.server.close)
        for name, data in OLD.items():
            self.write(name, data)
        self.config = (f'[server]\naddress = "{self.server.address}"\ntoken = "{TOKEN}"\n\n'
                       '[device]\nname = "거실"   # 그대로 둬야 함\n').encode()
        self.write("edge/config.toml", self.config)
        self.write(".venv/marker", b"venv")
        self.write("edge/VERSION", b"v1\n")
        self.server.version = "v2"
        self.server.bundle = make_bundle(NEW)
        self.pip_calls = []

    def write(self, name, data):
        path = self.dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def read(self, name):
        return (self.dir / name).read_bytes()

    def pip(self, requirements, install_dir):
        # pip는 파일을 바꾸기 전에 불려야 한다(실패하면 옛 코드를 그대로 쓰게)
        self.pip_calls.append((requirements.read_bytes(), self.read("edge/__init__.py"), install_dir))
        return True

    def run_update(self, pip=None):
        return update.update(self.dir, pip=pip or self.pip, attempt_file=self.attempts)

    def assert_unchanged(self):
        for name, data in OLD.items():
            self.assertEqual(self.read(name), data, name)
        self.assertEqual(self.read("edge/VERSION"), b"v1\n")
        self.assertEqual(self.read("edge/config.toml"), self.config)
        self.assertEqual(self.read(".venv/marker"), b"venv")
        self.assertFalse((self.dir / "edge" / "update.py").exists())

    def test_developer_checkout_is_never_updated(self):
        (self.dir / "edge" / "VERSION").unlink()
        self.assertEqual(self.run_update(), "developer")
        self.assertEqual(self.server.paths, [], "VERSION이 없으면 서버에 묻지도 않는다")
        self.assertIsNone(update.last_attempt(self.attempts))

    def test_same_version_downloads_nothing(self):
        self.server.version = "v1"
        self.assertEqual(self.run_update(), "current")
        self.assertEqual(self.server.paths, [update.VERSION_PATH])
        self.assert_unchanged()
        self.assertEqual(update.last_attempt(self.attempts)["target"], "v1")

    def test_new_version_replaces_code_but_not_config(self):
        replaced = []
        real_replace = os.replace

        def spy(src, dst):
            replaced.append(Path(dst))
            return real_replace(src, dst)

        with mock.patch.object(update.os, "replace", side_effect=spy):
            self.assertEqual(self.run_update(), "updated")
        for name, data in NEW.items():
            self.assertEqual(self.read(name), data, name)
        self.assertEqual(self.read("edge/config.toml"), self.config)
        self.assertEqual(self.read(".venv/marker"), b"venv")
        installed = [p for p in replaced if self.dir in p.parents]
        self.assertEqual(len(installed), len(NEW))
        self.assertEqual(installed[-1], self.dir / "edge" / "VERSION", "VERSION은 맨 마지막에 써야 합니다")
        self.assertEqual(self.pip_calls, [], "requirements가 같으면 pip를 부르지 않는다")
        self.assertEqual(update.local_version(self.dir / "edge" / "VERSION"), "v2")
        self.assertEqual(update.last_attempt(self.attempts)["target"], "v2")
        self.assertEqual([p.name for p in self.dir.rglob("*.update")], [], "임시 파일이 남으면 안 됩니다")
        if os.name == "posix":   # 서버(Windows)가 실행 권한 없이 묶어도 .sh는 실행할 수 있어야 한다
            self.assertTrue(os.access(self.dir / "edge" / "deploy" / "kiosk.sh", os.X_OK))
            self.assertFalse(os.access(self.dir / "edge" / "update.py", os.X_OK))

    def test_bundle_never_overwrites_config(self):
        files = {**NEW, "edge/config.toml": b'[server]\naddress = "evil"\n', "edge/__pycache__/x.pyc": b"pyc"}
        self.server.bundle = make_bundle(files)
        self.assertEqual(self.run_update(), "updated")
        self.assertEqual(self.read("edge/config.toml"), self.config)
        self.assertFalse((self.dir / "edge" / "__pycache__").exists())

    def test_malicious_members_are_rejected(self):
        def file_member(name):
            info = tarfile.TarInfo(name)
            info.size = 4
            return info, b"evil"

        link = tarfile.TarInfo("edge/link.py")
        link.type, link.linkname = tarfile.SYMTYPE, "/etc/passwd"
        hard = tarfile.TarInfo("edge/hard.py")
        hard.type, hard.linkname = tarfile.LNKTYPE, "edge/__init__.py"
        device = tarfile.TarInfo("edge/dev")
        device.type = tarfile.CHRTYPE
        cases = [file_member(name) for name in ("../evil.py", "/tmp/evil.py", "edge/../../evil.py", "other/evil.py",
                                                "evil.py", "C:/evil.py", "edge\\..\\evil.py", "edge")]
        cases += [(link, None), (hard, None), (device, None)]
        for info, data in cases:
            with self.subTest(name=info.name, type=info.type):
                # 정상 파일 뒤에 넣는다: 다 검사하기 전에 쓰기 시작했다면 정상 파일이 바뀌어 있을 것이다
                self.server.bundle = make_bundle(NEW, extra=[(info, data)])
                with self.assertLogs("edge.update", "ERROR"):
                    self.assertEqual(self.run_update(), "failed")
                self.assert_unchanged()
                self.assertFalse((self.dir.parent / "evil.py").exists())
                self.assertFalse((self.dir / "evil.py").exists())
                self.assertFalse((self.dir / "edge" / "link.py").exists())
                self.assertTrue(update.last_attempt(self.attempts)["result"].startswith("실패"))

    def test_changed_requirements_installed_before_files(self):
        files = {**NEW, REQ: b"aiohttp>=3.9\nsounddevice>=0.4.6\nnew-package\n"}
        self.server.bundle = make_bundle(files)
        self.assertEqual(self.run_update(), "updated")
        self.assertEqual(len(self.pip_calls), 1)
        requirements, init_at_pip_time, install_dir = self.pip_calls[0]
        self.assertEqual(requirements, files[REQ])
        self.assertEqual(init_at_pip_time, OLD["edge/__init__.py"])
        self.assertEqual(install_dir, self.dir)
        self.assertEqual(self.read(REQ), files[REQ])

    def test_pip_failure_keeps_old_code(self):
        self.server.bundle = make_bundle({**NEW, REQ: b"new-package\n"})
        with self.assertLogs("edge.update", "ERROR"):
            self.assertEqual(self.run_update(pip=lambda requirements, install_dir: False), "failed")
        self.assert_unchanged()

    def test_line_endings_alone_do_not_run_pip(self):
        self.server.bundle = make_bundle({**NEW, REQ: OLD[REQ].replace(b"\r\n", b"\n")})
        self.assertEqual(self.run_update(), "updated")
        self.assertEqual(self.pip_calls, [])

    def test_run_pip_command(self):
        with mock.patch.object(update.subprocess, "run", return_value=mock.Mock(returncode=0)) as run:
            self.assertTrue(update.run_pip(Path("req.txt"), self.dir))
        command = run.call_args[0][0]
        self.assertEqual(command[1:4], ["-m", "pip", "install"])
        self.assertEqual(command[-2:], ["-r", "req.txt"])
        with mock.patch.object(update.subprocess, "run", return_value=mock.Mock(returncode=1)):
            with self.assertLogs("edge.update", "ERROR"):
                self.assertFalse(update.run_pip(Path("req.txt"), self.dir))

    def test_rejected_token(self):
        self.server.token = "other"
        with self.assertLogs("edge.update", "ERROR") as logs:
            self.assertEqual(self.run_update(), "failed")
        self.assertIn("401", "\n".join(logs.output))
        self.assert_unchanged()

    def test_broken_or_incomplete_bundle(self):
        no_main = {k: v for k, v in NEW.items() if k != "edge/__main__.py"}
        for bundle in (make_bundle(NEW)[:120], b"<html>error</html>", make_bundle(no_main)):
            with self.subTest(size=len(bundle)):
                self.server.bundle = bundle
                with self.assertLogs("edge.update", "ERROR"):
                    self.assertEqual(self.run_update(), "failed")
                self.assert_unchanged()

    def test_old_server_without_update_support(self):
        self.server.version = None   # /edge/version 404
        with self.assertLogs("edge.update", "WARNING"):
            self.assertEqual(self.run_update(), "failed")
        self.assert_unchanged()

    def test_unreachable_server_is_skipped(self):
        self.server.close()
        started = time.monotonic()
        with self.assertLogs("edge.update", "WARNING"):
            self.assertEqual(self.run_update(), "unreachable")
        self.assertLess(time.monotonic() - started, update.REQUEST_TIMEOUT)
        self.assertIsNone(update.last_attempt(self.attempts), "서버에 닿지 않은 것은 시도로 남기지 않는다")
        self.assert_unchanged()

    def test_missing_token_is_skipped(self):
        self.write("edge/config.toml", f'[server]\naddress = "{self.server.address}"\n'.encode())
        with self.assertLogs("edge.update", "WARNING"):
            self.assertEqual(self.run_update(), "config")
        self.assertEqual(self.server.paths, [])


class RestartReasonTest(unittest.TestCase):
    """서버가 welcome으로 알려 준 버전을 보고 다시 시작(업데이트)할지."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.version = Path(tmp.name) / "VERSION"
        self.attempts = Path(tmp.name) / "update-attempt.json"

    def reason(self, server_version, now=None):
        return update.restart_reason(server_version, version_file=self.version, attempt_file=self.attempts, now=now)

    def test_developer_checkout_never_restarts(self):
        self.assertIsNone(self.reason("v2"))

    def test_same_or_unusable_server_version(self):
        self.version.write_text("v1\n", encoding="utf-8")
        for value in ("v1", " v1 ", None, "", 5, "버전 아님", "a" * 100):
            self.assertIsNone(self.reason(value), value)

    def test_differs_without_recent_attempt(self):
        self.version.write_text("v1\n", encoding="utf-8")
        self.assertIn("v2", self.reason("v2"))

    def test_recent_attempt_for_same_version_waits(self):
        self.version.write_text("v1\n", encoding="utf-8")
        self.assertTrue(update.record_attempt("v2", "실패: 시험", self.attempts, now=1000.0))
        self.assertIsNone(self.reason("v2", now=1000.0 + 60))
        self.assertIsNotNone(self.reason("v2", now=1000.0 + update.RETRY_SECONDS + 1), "30분 뒤에는 다시 시도")
        self.assertIsNotNone(self.reason("v3", now=1000.0 + 60), "서버 버전이 또 바뀌면 바로 시도")
        self.assertIsNotNone(self.reason("v2", now=1000.0 - 3600), "시계가 뒤로 갔으면 오래된 기록으로 본다")

    def test_broken_attempt_record(self):
        self.version.write_text("v1\n", encoding="utf-8")
        self.attempts.write_text("{not json", encoding="utf-8")
        self.assertIsNotNone(self.reason("v2"))


class ClientUpdateExitTest(unittest.IsolatedAsyncioTestCase):
    """welcome의 edge_version이 다르면 영상을 보내기 전에 종료 코드 3으로 끝난다."""

    async def asyncSetUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.version = Path(tmp.name) / "VERSION"
        self.attempts = Path(tmp.name) / "update-attempt.json"
        self.server = FakeServer(token=TOKEN, welcome_extra={"edge_version": "v2"})
        await self.server.start()
        self.camera, self.mic, self.speaker = Camera("none"), FakeMicrophone(), NullSpeaker()

    async def asyncTearDown(self):
        await self.server.stop()

    def client(self):
        cfg = EdgeConfig(server=self.server.address, token=TOKEN, device_id="test-pi")
        return EdgeClient(cfg, self.camera, self.mic, self.speaker, retry_delays=(0.2, 0.4), status_interval=0.2,
                          welcome_timeout=2.0, version_file=self.version, attempt_file=self.attempts)

    async def test_exits_with_code_3(self):
        self.version.write_text("v1\n", encoding="utf-8")
        client = self.client()
        with self.assertLogs("edge", "WARNING") as logs:
            code = await asyncio.wait_for(_run(client), 5.0)
        self.assertEqual(code, update.RESTART_EXIT_CODE)
        self.assertIn("v2", "\n".join(logs.output))
        self.assertEqual(client.connections, 0, "영상·상태를 보내기 전에 끝내야 합니다")
        self.assertEqual(self.server.statuses, [])
        self.assertTrue(await self.server.wait_until(lambda: self.server.close_codes, 2.0))
        self.assertEqual(self.server.close_codes[0], 1001)
        self.assertEqual(update.last_attempt(self.attempts)["target"], "v2", "다시 끝내지 않게 먼저 기록")

    async def stays_connected(self, client):
        task = asyncio.create_task(client.run())
        try:
            self.assertTrue(await self.server.wait_until(lambda: client.connected, 3.0))
            await asyncio.sleep(0.3)
            self.assertTrue(client.connected)
            self.assertFalse(task.done())
        finally:
            client.stop()
            await asyncio.wait_for(task, 3.0)

    async def test_stays_after_recent_attempt(self):
        self.version.write_text("v1\n", encoding="utf-8")
        update.record_attempt("v2", "실패: 시험", self.attempts)
        await self.stays_connected(self.client())
        self.assertIn("edge v1", self.server.hellos[0]["software"])

    async def test_developer_checkout_stays(self):
        await self.stays_connected(self.client())   # VERSION 없음
        self.assertIsNone(update.last_attempt(self.attempts))
        self.assertNotIn("(edge ", self.server.hellos[0]["software"])

    async def test_same_version_stays(self):
        self.version.write_text("v2\n", encoding="utf-8")
        await self.stays_connected(self.client())

    async def test_waits_while_playing(self):
        self.version.write_text("v1\n", encoding="utf-8")
        self.speaker.begin("voice")   # 재생 중이면 끝내지 않는다
        try:
            await self.stays_connected(self.client())
        finally:
            self.speaker.stop(notify=False)


if __name__ == "__main__":
    unittest.main()
