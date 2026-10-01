import tempfile
import time
import unittest
from pathlib import Path

import config
from l4_application import database as dbm
from l4_application.app_service import ALARM, NORMAL, SUSPECT, ApplicationService
from l4_application.database import Database
from l4_application.notifier import DisabledNotifier, Notifier, NotifierError
from l4_application.server import create_app
from l4_application.voice_checker import SimulatedVoiceChecker

KEYWORDS = (config.KEYWORDS_NEGATED_SAFE, config.KEYWORDS_HELP, config.KEYWORDS_SAFE)


class FakeNotifier(Notifier):
    def __init__(self):
        self.sent = []
        self.fail = False

    def send(self, text):
        if self.fail:
            raise NotifierError("network down")
        self.sent.append(text)


def wait_until(cond, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.02)
    return False


def make_service(db, notifier, attempts=2, listen=0.3):
    voice = SimulatedVoiceChecker(KEYWORDS)
    svc = ApplicationService(
        db, voice, notifier,
        voice_attempts=attempts, listen_seconds=listen, retry_delays=[0], retry_check_seconds=3600,
        heartbeat_stale_seconds=5,
    )
    return svc, voice


class ServiceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.notifier = FakeNotifier()
        self.svc, self.voice = make_service(Database(Path(self.tmp.name) / "events.db"), self.notifier)

    def tearDown(self):
        # 진행 중인 음성 확인 스레드가 끝난 뒤 DB를 닫는다.
        self.svc.clear_by_button()
        time.sleep(0.5)
        self.svc.stop()
        self.svc.db.close()
        self.tmp.cleanup()

    def event(self, event_id):
        return self.svc.db.get_event(event_id)

    def test_voice_safe_clears_without_alert(self):
        eid = self.svc.report_suspect({"torso_angle": 80})
        self.assertEqual(self.svc.state, SUSPECT)
        self.voice.inject("괜찮아요")
        self.assertTrue(wait_until(lambda: self.svc.state == NORMAL))
        ev = self.event(eid)
        self.assertEqual(ev["result"], dbm.CLEARED_VOICE)
        self.assertEqual(ev["notify_state"], dbm.NOTIFY_NONE)
        self.assertIsNone(ev["voice_text"])  # 기본 설정은 인식 문장 원문을 저장하지 않는다
        self.assertEqual(self.notifier.sent, [])

    def test_duplicate_report_reuses_event(self):
        first = self.svc.report_suspect({})
        self.assertEqual(self.svc.report_suspect({}), first)
        self.assertEqual(len(self.svc.db.recent_events()), 1)

    def test_no_reply_raises_alarm_and_notifies_once(self):
        eid = self.svc.report_suspect({})
        self.assertTrue(wait_until(lambda: self.event(eid)["notify_state"] == dbm.NOTIFY_SENT))
        self.assertEqual(self.svc.state, ALARM)
        self.assertEqual(self.event(eid)["result"], dbm.ALERT_NO_REPLY)
        self.assertEqual(len(self.notifier.sent), 1)

    def test_negated_safe_raises_help_alarm(self):
        eid = self.svc.report_suspect({})
        self.voice.inject("안 괜찮아요")
        self.assertTrue(wait_until(lambda: self.svc.state == ALARM))
        self.assertEqual(self.event(eid)["result"], dbm.ALERT_HELP)

    def test_button_during_listening_ignores_late_voice_result(self):
        eid = self.svc.report_suspect({})
        time.sleep(0.05)  # 첫 번째 듣기 진행 중
        self.assertTrue(self.svc.clear_by_button())
        self.voice.inject("도와주세요")  # 해제 뒤 늦게 들어온 결과
        time.sleep(0.8)
        self.assertEqual(self.svc.state, NORMAL)
        self.assertEqual(self.event(eid)["result"], dbm.CLEARED_BUTTON)
        self.assertEqual(self.notifier.sent, [])

    def test_failed_notification_is_retried_with_original_time(self):
        self.notifier.fail = True
        eid = self.svc.report_suspect({})
        self.assertTrue(wait_until(lambda: self.event(eid)["notify_state"] == dbm.NOTIFY_FAILED))
        self.assertEqual(self.svc.get_status()["event"]["notify_state"], dbm.NOTIFY_FAILED)

        self.notifier.fail = False
        self.assertEqual([e["id"] for e in self.svc.db.due_retries()], [eid])
        self.svc._send_alert(eid, delayed=True)
        ev = self.event(eid)
        self.assertEqual(ev["notify_state"], dbm.NOTIFY_SENT)
        self.assertEqual(ev["notify_attempts"], 2)
        self.assertIn("지연 전송", self.notifier.sent[0])
        self.assertIn(ev["started_at"][:19].replace("T", " "), self.notifier.sent[0])

    def test_clear_after_failed_notification_cancels_retry(self):
        self.notifier.fail = True
        eid = self.svc.report_suspect({})
        self.assertTrue(wait_until(lambda: self.event(eid)["notify_state"] == dbm.NOTIFY_FAILED))
        self.svc.clear_by_button()
        ev = self.event(eid)
        self.assertEqual(ev["notify_state"], dbm.NOTIFY_CANCELLED)
        self.assertEqual(ev["result"], dbm.ALERT_NO_REPLY)  # 경보가 있었다는 기록은 남는다
        self.assertEqual(ev["cleared_by"], "BUTTON")
        self.assertEqual(self.svc.db.due_retries(), [])

    def test_clear_after_sent_notification_sends_clear_message(self):
        eid = self.svc.report_suspect({})
        self.assertTrue(wait_until(lambda: self.event(eid)["notify_state"] == dbm.NOTIFY_SENT))
        self.svc.clear_by_button()
        self.assertTrue(wait_until(lambda: len(self.notifier.sent) == 2))
        self.assertIn("경보 해제", self.notifier.sent[1])
        self.assertEqual(self.svc.state, NORMAL)

    def test_disabled_notifier_is_not_reported_as_sent(self):
        svc, _ = make_service(self.svc.db, DisabledNotifier())
        eid = svc.report_suspect({})
        self.assertTrue(wait_until(lambda: self.event(eid)["notify_state"] == dbm.NOTIFY_DISABLED))

    def test_device_failure_raises_alarm(self):
        eid = self.svc.report_suspect({})
        self.voice.inject("DEVICE_FAIL")
        self.assertTrue(wait_until(lambda: self.svc.state == ALARM))
        self.assertEqual(self.event(eid)["result"], dbm.ALERT_VOICE_FAIL)

    def test_restart_marks_unfinished_event_interrupted(self):
        eid = self.svc.db.insert_event("FALL_SUSPECT", {})
        self.svc.start()
        self.assertEqual(self.event(eid)["result"], dbm.INTERRUPTED)

    def test_device_problems(self):
        self.assertIn("영상 감지 프로그램이 응답하지 않습니다", self.svc.get_status()["problems"])
        self.svc.heartbeat({"camera_ok": False, "detector_ok": True})
        self.assertEqual(self.svc.get_status()["problems"], ["카메라 연결을 확인해 주세요"])
        self.svc.heartbeat({"camera_ok": True, "detector_ok": True})
        self.assertEqual(self.svc.get_status()["problems"], [])


class ServerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.svc, _ = make_service(Database(Path(self.tmp.name) / "e.db"), FakeNotifier(),
                                   attempts=1, listen=2)
        self.client = create_app(self.svc).test_client()

    def tearDown(self):
        # 진행 중인 음성 확인 스레드가 끝난 뒤 DB를 닫는다.
        self.svc.clear_by_button()
        time.sleep(0.5)
        self.svc.stop()
        self.svc.db.close()
        self.tmp.cleanup()

    def test_event_heartbeat_clear_cycle(self):
        res = self.client.post("/api/event", json={"event_type": "FALL_SUSPECT", "features": {}})
        eid = res.get_json()["event_id"]
        hb = self.client.post("/api/heartbeat", json={"l3_state": "SUSPECT"}).get_json()
        self.assertEqual(hb, {"state": SUSPECT, "event_id": eid})

        self.assertTrue(self.client.post("/api/clear").get_json()["cleared"])
        hb = self.client.post("/api/heartbeat", json={"l3_state": "SUSPECT"}).get_json()
        self.assertIsNone(hb["event_id"])  # L3는 이것을 보고 감시를 재개한다

        events = self.client.get("/api/events?limit=3").get_json()
        self.assertEqual(events[0]["result"], dbm.CLEARED_BUTTON)
        self.assertNotIn("feature_snapshot", events[0])

    def test_rejects_unknown_event_type(self):
        self.assertEqual(self.client.post("/api/event", json={"event_type": "ALARM"}).status_code, 400)

    def test_index_renders(self):
        res = self.client.get("/")
        self.assertEqual(res.status_code, 200)
        self.assertIn("괜찮아요", res.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
