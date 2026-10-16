"""이벤트 처리 단위 테스트: 알림 재시도·지연 전송, 재시작 복원, 장치 이상 알림, 최신 장면만 처리."""

import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from server import store as st
from server.device import DeviceRegistry
from server.events import ALARM, NORMAL, Case, EventService
from server.fall import FallParams, FallState
from server.store import Store, now_iso
from server.tests.fakes import (FakeNotifier, FakePoseFactory, FakeRecognizer, fall_script, make_settings,
                                prompt_sound, tiny_jpeg)
from server.vision import VideoPipeline


class FakeLink:
    """장치 감시 시험용: problems()만 흉내 낸다."""

    def __init__(self, device_id="pi-1", name="거실", problems=()):
        self.device_id, self.name, self._problems = device_id, name, list(problems)

    def problems(self, now, status_stale, video_stale):
        return list(self._problems)


class EventServiceTestBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = make_settings(Path(self.tmp.name), notify_retry_check_seconds=3600,
                                      monitor_interval_seconds=3600)
        self.store = Store(self.settings.path(self.settings.db_path))
        self.devices = DeviceRegistry()
        self.notifier = FakeNotifier()
        self.finished = 0

    def make_service(self) -> EventService:
        async def prompt():
            return prompt_sound()

        def on_finished(device_id):
            self.finished += 1

        self.service = EventService(self.settings, self.store, self.devices, FakeRecognizer(), self.notifier,
                                    prompt=prompt, siren=b"\x00\x00", on_finished=on_finished)
        return self.service

    async def asyncTearDown(self):
        await self.service.stop()
        self.store.close()
        self.tmp.cleanup()

    def alarm_event(self, result=st.ALERT_NO_REPLY, notify_state=st.NOTIFY_PENDING) -> int:
        event_id = self.store.insert_event("pi-1", {"torso_angle": 80})
        self.store.set_result(event_id, result, "NO_REPLY", ended=False)
        self.store.set_notify_state(event_id, notify_state)
        return event_id


class RestartTest(EventServiceTestBase):
    async def test_restore_after_restart(self):
        alarm = self.alarm_event()                               # 경보 알림을 보내는 도중 꺼짐
        service = self.make_service()
        await service.start()

        self.assertEqual((service.state, service.case("pi-1").event_id), (ALARM, alarm))  # 해제 안 된 경보 복원
        self.assertEqual(self.store.get_event(alarm)["notify_state"], st.NOTIFY_FAILED)

        await service.retry_due()  # 끊긴 알림 다시 보내기
        self.assertEqual(len(self.notifier.sent), 1)
        self.assertIn("지연 전송", self.notifier.sent[0])
        self.assertIn(self.store.get_event(alarm)["started_at"][:19].replace("T", " "), self.notifier.sent[0])
        self.assertEqual(self.store.get_event(alarm)["notify_state"], st.NOTIFY_SENT)

        self.assertTrue(await service.clear())
        await asyncio.sleep(0.05)
        self.assertEqual(service.state, NORMAL)
        self.assertEqual(self.store.get_event(alarm)["cleared_by"], "BUTTON")
        self.assertIn("경보 해제", self.notifier.sent[-1])
        self.assertEqual(self.finished, 1)

    async def test_voice_check_cut_by_restart_becomes_alarm(self):
        # 음성 확인 도중 서버가 꺼지면 넘어진 사람은 계속 누워 있어 다시 감지되지 않을 수 있다: 경보로 올리고 알린다.
        unfinished = self.store.insert_event("pi-2", None)
        service = self.make_service()
        await service.start()
        ev = self.store.get_event(unfinished)
        self.assertEqual((ev["result"], ev["ended_at"]), (st.ALERT_INTERRUPTED, None))
        self.assertEqual((service.state_of("pi-2"), service.case("pi-2").event_id), (ALARM, unfinished))
        self.assertIn("pi-2", service.problems_by_subject(0.0))  # 아직 다시 접속하지 않은 장치도 감시 목록에 있다
        await service.retry_due()
        self.assertEqual(len(self.notifier.sent), 1)
        self.assertIn("서버가 다시 시작", self.notifier.sent[0])
        self.assertIn("지연 전송", self.notifier.sent[0])

    async def test_purge_keeps_open_alarm(self):
        alarm = self.alarm_event(notify_state=st.NOTIFY_SENT)
        done = self.store.insert_event("pi-1", None)
        self.store.set_result(done, st.CLEARED_VOICE)
        old = now_iso(datetime.now() - timedelta(days=40))
        self.store._execute("UPDATE events SET started_at = ?", (old,))
        self.settings.event_retention_days = 30
        service = self.make_service()
        await service.start()
        self.assertIsNone(self.store.get_event(done))
        self.assertIsNotNone(self.store.get_event(alarm))  # 해제 전 경보는 지우지 않는다
        self.assertEqual(service.event_snapshot("pi-1")["id"], alarm)
        self.assertTrue(await service.clear("pi-1"))

    async def test_retry_loop_survives_errors(self):
        self.settings.notify_retry_check_seconds = 0.01
        service = self.make_service()
        calls = []

        async def broken():
            calls.append(1)
            raise RuntimeError("boom")

        service.retry_due = broken
        await service.start()
        await asyncio.sleep(0.1)
        self.assertGreater(len(calls), 2)  # 오류가 나도 다시 보내기 확인을 멈추지 않는다

    async def test_restore_alarms_of_each_device(self):
        first = self.alarm_event()
        other = self.store.insert_event("pi-2", None)
        self.store.set_result(other, st.ALERT_HELP, "HELP", ended=False)
        service = self.make_service()
        await service.start()
        self.assertEqual({d: c.event_id for d, c in service.cases.items()}, {"pi-1": first, "pi-2": other})
        self.assertFalse(await service.clear())  # 진행 중인 이벤트가 여러 개면 장치를 정해야 한다
        self.assertTrue(await service.clear("pi-2"))
        self.assertEqual((service.state_of("pi-1"), service.state_of("pi-2")), (ALARM, NORMAL))
        self.assertTrue(await service.clear())  # 하나만 남으면 장치를 정하지 않아도 그것을 해제

    async def test_cleared_alarm_is_not_resent(self):
        alarm = self.alarm_event()
        self.store.close_event(alarm, "BUTTON")
        service = self.make_service()
        await service.start()
        self.assertEqual(service.state, NORMAL)
        self.assertEqual(self.store.get_event(alarm)["notify_state"], st.NOTIFY_CANCELLED)


class NotifyRetryTest(EventServiceTestBase):
    async def test_retry_delays_and_delayed_message(self):
        self.notifier.fail = 2
        service = self.make_service()
        await service.start()
        event_id = self.alarm_event(notify_state=st.NOTIFY_NONE)

        before = datetime.now()
        await service._send_alert(event_id)
        ev = self.store.get_event(event_id)
        self.assertEqual((ev["notify_state"], ev["notify_attempts"]), (st.NOTIFY_FAILED, 1))
        wait = datetime.fromisoformat(ev["notify_next_at"]) - before
        self.assertAlmostEqual(wait.total_seconds(), 30, delta=2)  # 첫 재시도 30초 뒤
        await service.retry_due()
        self.assertEqual(self.notifier.attempts, 1)  # 아직 차례가 아님

        self.store.set_notify_state(event_id, st.NOTIFY_FAILED, next_at=now_iso(datetime.now() - timedelta(seconds=1)))
        await service.retry_due()
        ev = self.store.get_event(event_id)
        wait = datetime.fromisoformat(ev["notify_next_at"]) - datetime.now()
        self.assertAlmostEqual(wait.total_seconds(), 120, delta=2)  # 두 번째는 2분 뒤

        self.store.set_notify_state(event_id, st.NOTIFY_FAILED, next_at=None)
        await service.retry_due()
        self.assertEqual(self.store.get_event(event_id)["notify_state"], st.NOTIFY_SENT)
        self.assertIn("지연 전송", self.notifier.sent[0])

    async def test_clear_cancels_pending_retry(self):
        self.notifier.fail = 1
        service = self.make_service()
        await service.start()
        event_id = self.alarm_event()
        service.cases["pi-1"] = Case("pi-1", event_id, ALARM)
        await service._send_alert(event_id)
        await service.clear()
        self.assertEqual(self.store.get_event(event_id)["notify_state"], st.NOTIFY_CANCELLED)
        self.store.set_notify_state(event_id, st.NOTIFY_CANCELLED, next_at=None)
        await service.retry_due()
        self.assertEqual(self.notifier.sent, [])

    async def test_disabled_notifier_does_not_pretend(self):
        self.notifier.enabled = False
        service = self.make_service()
        await service.start()
        event_id = self.alarm_event(notify_state=st.NOTIFY_NONE)
        await service._send_alert(event_id)
        self.assertEqual(self.store.get_event(event_id)["notify_state"], st.NOTIFY_DISABLED)


class DeviceMonitorTest(EventServiceTestBase):
    async def test_problem_alert_after_180s_and_recovery(self):
        service = self.make_service()
        await service.start()
        await service.check_devices(1000.0)  # 장치가 아직 연결되지 않음
        await service.check_devices(1179.0)
        self.assertEqual(self.notifier.sent, [])
        await service.check_devices(1181.0)
        self.assertEqual(len(self.notifier.sent), 1)
        self.assertIn("장치 이상", self.notifier.sent[0])
        self.assertIn("라즈베리파이가 아직 연결되지 않았습니다", self.notifier.sent[0])
        await service.check_devices(1300.0)
        self.assertEqual(len(self.notifier.sent), 1)  # 한 번만

        self.devices.links["pi-1"] = FakeLink()
        self.notifier.fail = 1
        await service.check_devices(1301.0)  # 복구 알림 실패 -> 30초 뒤 다시
        await service.check_devices(1310.0)
        self.assertEqual(self.notifier.attempts, 2)
        await service.check_devices(1332.0)
        self.assertIn("정상 복구", self.notifier.sent[-1])
        self.assertFalse(service.device_alert_sent)
        kinds = [e["kind"] for e in self.store.recent_system_events()]
        self.assertEqual(kinds, ["DEVICE_RECOVERED", "DEVICE_PROBLEM"])

    async def test_each_device_is_watched_separately(self):
        service = self.make_service()
        await service.start()
        self.devices.known.update({"pi-1": {"name": "거실"}, "pi-2": {"name": "안방"}})
        self.devices.links["pi-1"] = FakeLink("pi-1", "거실")  # 안방은 연결이 끊김
        await service.check_devices(0.0)
        await service.check_devices(181.0)
        self.assertEqual(len(self.notifier.sent), 1)
        self.assertIn("[장치 이상 - 낙상 감지 중단 가능] 안방", self.notifier.sent[0])
        self.assertIn("라즈베리파이 연결이 끊겼습니다", self.notifier.sent[0])
        self.assertEqual(service.problems(device_id="pi-1"), [])
        self.assertEqual(service.problems(), ["안방: 라즈베리파이 연결이 끊겼습니다"])
        self.assertTrue(service.alert_sent_for("pi-2"))
        self.assertFalse(service.alert_sent_for("pi-1"))
        self.assertEqual(self.store.recent_system_events()[0]["device_id"], "pi-2")

    async def test_short_problem_is_not_reported(self):
        service = self.make_service()
        await service.start()
        self.devices.links["pi-1"] = FakeLink(problems=["카메라를 확인해 주세요"])
        await service.check_devices(0.0)
        self.devices.links["pi-1"] = FakeLink()
        await service.check_devices(100.0)
        self.devices.links["pi-1"] = FakeLink(problems=["카메라를 확인해 주세요"])
        await service.check_devices(150.0)
        await service.check_devices(300.0)  # 다시 시작된 문제는 150초부터 잼
        self.assertEqual(self.notifier.sent, [])
        await service.check_devices(331.0)
        self.assertIn("카메라를 확인해 주세요", self.notifier.sent[0])


class VideoPipelineTest(unittest.IsolatedAsyncioTestCase):
    async def test_only_latest_frame_is_processed_when_behind(self):
        factory = FakePoseFactory(delay=0.1)  # 처리 한 장에 0.1초
        suspects = []

        async def on_suspect(device_id, features):
            suspects.append(device_id)

        pipeline = VideoPipeline("pi-1", factory, FallParams(), 0.3, on_suspect)
        pipeline.start()
        jpeg = tiny_jpeg()
        for i in range(10):
            pipeline.submit(100 + i * 0.1, jpeg)
            await asyncio.sleep(0.005)
        await asyncio.sleep(0.4)
        await pipeline.close()
        self.assertEqual(factory.seen[-1], 100 + 9 * 0.1)  # 마지막 장면은 반드시 처리
        self.assertLessEqual(len(factory.seen), 3)
        self.assertGreaterEqual(pipeline.dropped, 7)

    async def test_fall_detected_with_edge_timestamps_and_reset(self):
        factory = FakePoseFactory(fall_script(fall_at=1.0))
        suspects = []

        async def on_suspect(device_id, features):
            suspects.append(features)

        pipeline = VideoPipeline("pi-1", factory, FallParams(), 0.3, on_suspect)
        pipeline.start()
        jpeg = tiny_jpeg()

        async def feed(t0, seconds):
            for i in range(int(seconds * 10)):
                pipeline.submit(t0 + i * 0.1, jpeg)  # 실제로는 빠르게 보내지만 시각은 10fps
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.05)

        await feed(5000.0, 4.0)
        self.assertEqual(len(suspects), 1)
        self.assertEqual(pipeline.state, FallState.SUSPECT)
        await feed(5004.0, 3.0)
        self.assertEqual(len(suspects), 1)  # 이벤트가 끝나기(reset) 전에는 다시 보고하지 않음

        pipeline.reset_detection()  # 이벤트 종료: 계속 누워 있어도 새 하강이 없으면 다시 보고하지 않음
        await feed(5007.0, 3.0)
        self.assertEqual(len(suspects), 1)
        self.assertEqual(pipeline.state, FallState.NORMAL)

        # 다시 접속: edge 시계가 처음부터 다시 시작해도(재부팅) 새 기록으로 판정한다.
        # 판정 기록을 지우지 않으면 시각이 거꾸로 간 장면으로 보고 모두 버리게 된다.
        pipeline.new_session()
        await feed(10.0, 4.0)
        self.assertEqual(len(suspects), 2)
        await pipeline.close()


if __name__ == "__main__":
    unittest.main()
