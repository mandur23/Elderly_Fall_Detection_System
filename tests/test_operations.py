"""장치 이상 알림, 일일 보고, 현장 경보음, 내부 API 키, 설정 덮어쓰기 테스트."""

import os
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path

import numpy as np

from l3_perception.camera import rotate
from l4_application import database as dbm
from l4_application.audio import Siren
from l4_application.database import Database
from l4_application.server import create_app
from tests.test_app_service import FakeNotifier, make_service, wait_until

ROOT = Path(__file__).resolve().parent.parent


class FakeSiren(Siren):
    def __init__(self):
        self.playing = False

    def start(self):
        self.playing = True

    def stop(self):
        self.playing = False


class DeviceMonitorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.notifier = FakeNotifier()
        self.svc, self.voice = make_service(Database(Path(self.tmp.name) / "e.db"), self.notifier)
        self.svc.device_alert_after = 60
        self.svc.daily_report_time = "09:00"
        self.wall = datetime(2026, 10, 1, 8, 0)

    def tearDown(self):
        self.svc.clear_by_button()
        time.sleep(0.5)
        self.svc.stop()
        self.svc.db.close()
        self.tmp.cleanup()

    def heartbeat_at(self, mono, **status):
        self.svc.heartbeat({"camera_ok": True, "detector_ok": True, **status})
        self.svc._heartbeat_at = mono

    def test_problem_alert_after_delay_once_then_recovery(self):
        self.heartbeat_at(0.0, camera_ok=False)
        self.svc.check_devices(0.0, self.wall)
        self.svc.check_devices(30.0, self.wall)
        self.assertEqual(self.notifier.sent, [])  # 짧은 끊김은 알리지 않는다

        self.heartbeat_at(60.0, camera_ok=False)
        self.svc.check_devices(60.0, self.wall)
        self.svc.check_devices(65.0, self.wall)
        self.assertEqual(len(self.notifier.sent), 1)
        self.assertIn("카메라 연결을 확인해 주세요", self.notifier.sent[0])

        self.heartbeat_at(70.0, camera_ok=True)
        self.svc.check_devices(70.0, self.wall)
        self.assertEqual(len(self.notifier.sent), 2)
        self.assertIn("정상 복구", self.notifier.sent[1])
        kinds = [e["kind"] for e in self.svc.db.recent_system_events()]
        self.assertEqual(kinds, ["DEVICE_RECOVERED", "DEVICE_PROBLEM"])

    def test_stale_heartbeat_counts_as_problem(self):
        self.heartbeat_at(0.0)
        self.svc.check_devices(0.0, self.wall)
        self.svc.check_devices(10.0, self.wall)   # 하트비트 끊김 시작
        self.svc.check_devices(80.0, self.wall)
        self.assertEqual(len(self.notifier.sent), 1)
        self.assertIn("영상 감지 프로그램이 응답하지 않습니다", self.notifier.sent[0])

    def test_failed_device_alert_is_retried(self):
        self.notifier.fail = True
        self.heartbeat_at(100.0, camera_ok=False)
        self.svc.check_devices(0.0, self.wall)
        self.svc.check_devices(61.0, self.wall)
        self.notifier.fail = False
        self.svc.check_devices(66.0, self.wall)
        self.assertEqual(len(self.notifier.sent), 1)

    def test_short_problem_without_alert_sends_nothing_on_recovery(self):
        self.heartbeat_at(0.0, camera_ok=False)
        self.svc.check_devices(0.0, self.wall)
        self.heartbeat_at(10.0)
        self.svc.check_devices(10.0, self.wall)
        self.assertEqual(self.notifier.sent, [])

    def test_daily_report_once_per_day(self):
        self.heartbeat_at(0.0)
        at_nine = datetime(2026, 10, 1, 9, 0, 10)
        self.svc.check_devices(0.0, at_nine)
        self.svc.check_devices(1.0, at_nine)
        self.assertEqual(len(self.notifier.sent), 1)
        self.assertIn("일일 상태", self.notifier.sent[0])
        self.assertIn("정상 동작 중", self.notifier.sent[0])
        self.svc.check_devices(2.0, datetime(2026, 10, 2, 9, 0, 5))
        self.assertEqual(len(self.notifier.sent), 2)


class SirenTest(unittest.TestCase):
    def test_siren_plays_during_alarm_and_stops_on_clear(self):
        tmp = tempfile.TemporaryDirectory()
        svc, voice = make_service(Database(Path(tmp.name) / "e.db"), FakeNotifier())
        svc.siren = siren = FakeSiren()
        svc.report_suspect({})
        voice.inject("도와주세요")
        self.assertTrue(wait_until(lambda: siren.playing))
        svc.clear_by_button()
        self.assertFalse(siren.playing)
        time.sleep(0.2)
        svc.stop()
        svc.db.close()
        tmp.cleanup()


class InternalApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.svc, _ = make_service(Database(Path(self.tmp.name) / "e.db"), FakeNotifier())
        self.client = create_app(self.svc, internal_token="secret").test_client()

    def tearDown(self):
        self.svc.clear_by_button()
        time.sleep(0.5)
        self.svc.stop()
        self.svc.db.close()
        self.tmp.cleanup()

    def test_token_required_for_l3_endpoints(self):
        self.assertEqual(self.client.post("/api/heartbeat", json={}).status_code, 403)
        self.assertEqual(self.client.post("/api/event", json={"event_type": "FALL_SUSPECT"},
                                          headers={"X-Internal-Token": "wrong"}).status_code, 403)
        res = self.client.post("/api/heartbeat", json={}, headers={"X-Internal-Token": "secret"})
        self.assertEqual(res.status_code, 200)

    def test_health_reports_problems(self):
        self.assertEqual(self.client.get("/api/health").status_code, 503)  # 아직 하트비트 없음
        self.client.post("/api/heartbeat", json={"camera_ok": True, "detector_ok": True},
                         headers={"X-Internal-Token": "secret"})
        self.assertEqual(self.client.get("/api/health").status_code, 200)


class CameraRotateTest(unittest.TestCase):
    def test_rotate(self):
        frame = np.arange(6).reshape(2, 3)
        self.assertEqual(rotate(frame, 0).shape, (2, 3))
        self.assertEqual(rotate(frame, 90).shape, (3, 2))
        self.assertEqual(rotate(frame, 90)[0].tolist(), [3, 0])  # 시계 방향
        self.assertEqual(rotate(frame, 180)[0].tolist(), [5, 4, 3])


class ConfigOverrideTest(unittest.TestCase):
    def run_config(self, toml_text):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.toml"
            path.write_text(toml_text, encoding="utf-8")
            env = dict(os.environ, FALL_CONFIG=str(path), PYTHONIOENCODING="utf-8")
            return subprocess.run(
                [sys.executable, "-c", "import config; print(config.CAMERA_BACKEND, config.DATA_DIR.name, "
                                       "config.VOSK_MODEL_PATH.parent.name, config.DB_PATH.parent.name)"],
                cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8",
            )

    def test_override_and_derived_paths(self):
        res = self.run_config('CAMERA_BACKEND = "picamera2"\nDATA_DIR = "/srv/falldata"\n')
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stdout.split(), ["picamera2", "falldata", "falldata", "falldata"])

    def test_unknown_key_is_rejected(self):
        res = self.run_config('CAMERA_BACKND = "typo"\n')
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("CAMERA_BACKND", res.stderr)


if __name__ == "__main__":
    unittest.main()
