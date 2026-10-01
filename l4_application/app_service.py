"""L4 이벤트 처리: 낙상 의심 -> 음성 확인 -> 해제 또는 경보·알림.

이벤트 진행 상태는 이 클래스 한 곳에서만 관리한다. 모든 상태 변경은 잠금 안에서
'아직 같은 이벤트가 진행 중인지'를 확인한 뒤에 한다. 그래서 음성 확인 도중 화면
버튼으로 해제해도, 늦게 도착한 음성 결과가 경보를 다시 띄우지 않는다.
"""

import logging
import threading
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Sequence

from l4_application import database as db_mod
from l4_application.database import Database
from l4_application.audio import Siren
from l4_application.notifier import (Notifier, NotifierError, alert_message, clear_message, daily_message,
                                     device_problem_message, device_recovered_message)
from l4_application import voice_checker as voice_mod
from l4_application.voice_checker import VoiceChecker

log = logging.getLogger("l4.app")

NORMAL, SUSPECT, ALARM = "NORMAL", "SUSPECT", "ALARM"


class ApplicationService:
    def __init__(self, db: Database, voice: VoiceChecker, notifier: Notifier, *,
                 voice_attempts: int, listen_seconds: float, retry_delays: Sequence[float],
                 retry_check_seconds: float, heartbeat_stale_seconds: float,
                 store_voice_text: bool = False, retention_days: Optional[int] = None,
                 siren: Optional[Siren] = None, device_alert_after: float = 180.0,
                 daily_report_time: Optional[str] = None, location: str = "",
                 monitor_interval: float = 5.0):
        self.db = db
        self.voice = voice
        self.notifier = notifier
        self.voice_attempts = voice_attempts
        self.listen_seconds = listen_seconds
        self.retry_delays = list(retry_delays)
        self.retry_check_seconds = retry_check_seconds
        self.heartbeat_stale_seconds = heartbeat_stale_seconds
        self.store_voice_text = store_voice_text
        self.retention_days = retention_days
        self.siren = siren or Siren()
        self.device_alert_after = device_alert_after
        self.daily_report_time = daily_report_time
        self.location = location
        self.monitor_interval = monitor_interval

        self._lock = threading.RLock()
        self.state = NORMAL
        self.event_id: Optional[int] = None
        self.voice_attempt = 0
        self._heartbeat: Dict = {}
        self._heartbeat_at: Optional[float] = None
        self._stop = threading.Event()
        self._retry_thread = threading.Thread(target=self._retry_loop, name="notify-retry", daemon=True)
        self._monitor_thread = threading.Thread(target=self._monitor_loop, name="device-monitor", daemon=True)
        # 장치 이상 알림 상태
        self._problem_since: Optional[float] = None
        self._problem_since_iso: Optional[str] = None
        self._device_alert_sent = False
        self._last_daily_date: Optional[str] = None

    # --- 수명 주기 ---
    def start(self) -> None:
        interrupted = self.db.mark_interrupted()
        if interrupted:
            log.warning("재시작 전 처리되지 않은 이벤트 %d건을 INTERRUPTED로 기록했습니다.", interrupted)
        if self.retention_days:
            self.db.purge_older_than(self.retention_days)
        self._retry_thread.start()
        self._monitor_thread.start()

    def stop(self) -> None:
        self._stop.set()
        self.siren.stop()

    def _is_active(self, event_id: int, state: str) -> bool:
        return self.event_id == event_id and self.state == state

    # --- L3 입력 ---
    def report_suspect(self, features: Optional[Dict]) -> int:
        """L3의 낙상 의심 보고. 이미 진행 중인 이벤트가 있으면 새로 만들지 않는다."""
        with self._lock:
            if self.event_id is not None:
                return self.event_id
            event_id = self.db.insert_event("FALL_SUSPECT", features)
            self.event_id, self.state, self.voice_attempt = event_id, SUSPECT, 0
        log.info("낙상 의심 이벤트 #%d 시작", event_id)
        threading.Thread(target=self._voice_flow, args=(event_id,), name=f"voice-{event_id}",
                         daemon=True).start()
        return event_id

    def heartbeat(self, status: Dict) -> Dict:
        with self._lock:
            self._heartbeat = dict(status)
            self._heartbeat_at = time.monotonic()
            return {"state": self.state, "event_id": self.event_id}

    # --- 음성 확인 ---
    def _voice_flow(self, event_id: int) -> None:
        last_result, last_text = voice_mod.NO_REPLY, None
        for attempt in range(1, self.voice_attempts + 1):
            with self._lock:
                if not self._is_active(event_id, SUSPECT):
                    return
                self.voice_attempt = attempt
            log.info("이벤트 #%d 음성 확인 %d/%d", event_id, attempt, self.voice_attempts)
            self.voice.play_prompt()
            if not self._is_active(event_id, SUSPECT):
                return
            result, text = self.voice.listen(self.listen_seconds)
            log.info("이벤트 #%d 음성 확인 결과: %s", event_id, result)

            with self._lock:
                if not self._is_active(event_id, SUSPECT):
                    return
                if result == voice_mod.SAFE:
                    self._finish(event_id, db_mod.CLEARED_VOICE, result, text)
                    return
                if result == voice_mod.HELP:
                    self._raise_alarm(event_id, db_mod.ALERT_HELP, result, text)
                    break
                if result == voice_mod.DEVICE_FAIL:
                    # 확인할 수 없으면 경보로 처리한다.
                    self._raise_alarm(event_id, db_mod.ALERT_VOICE_FAIL, result, text)
                    break
                last_result, last_text = result, text
        else:
            with self._lock:
                if not self._is_active(event_id, SUSPECT):
                    return
                self._raise_alarm(event_id, db_mod.ALERT_NO_REPLY, last_result, last_text)
        self._send_alert(event_id)

    def _voice_text(self, text: Optional[str]) -> Optional[str]:
        return text if self.store_voice_text else None

    def _finish(self, event_id: int, result: str, voice_result: Optional[str], text: Optional[str]) -> None:
        self.db.set_result(event_id, result, voice_result, self._voice_text(text))
        self.state, self.event_id, self.voice_attempt = NORMAL, None, 0
        log.info("이벤트 #%d 해제: %s", event_id, result)

    def _raise_alarm(self, event_id: int, result: str, voice_result: Optional[str], text: Optional[str]) -> None:
        self.db.set_result(event_id, result, voice_result, self._voice_text(text), ended=False)
        self.db.set_notify_state(event_id, db_mod.NOTIFY_PENDING)
        self.state = ALARM
        self.siren.start()
        log.warning("이벤트 #%d 경보: %s", event_id, result)

    # --- 알림 ---
    def _send_alert(self, event_id: int, delayed: bool = False) -> None:
        event = self.db.get_event(event_id)
        if event is None:
            return
        if not self.notifier.enabled:
            self.notifier.send(alert_message(event, delayed, self.location))
            self.db.set_notify_state(event_id, db_mod.NOTIFY_DISABLED)
            return
        self.db.set_notify_state(event_id, db_mod.NOTIFY_PENDING)
        try:
            self.notifier.send(alert_message(event, delayed, self.location))
        except NotifierError as e:
            delay = self.retry_delays[min(event["notify_attempts"], len(self.retry_delays) - 1)]
            next_at = (datetime.now() + timedelta(seconds=delay)).isoformat(timespec="milliseconds")
            with self._lock:
                # 전송 중에 사용자가 해제했다면 다시 보내지 않는다.
                cleared = self.db.get_event(event_id)["cleared_by"] is not None
                state = db_mod.NOTIFY_CANCELLED if cleared else db_mod.NOTIFY_FAILED
                self.db.set_notify_state(event_id, state, next_at=None if cleared else next_at,
                                         count_attempt=True)
            log.error("이벤트 #%d 알림 전송 실패 (%s), %s초 후 재시도", event_id, e, delay)
            return
        with self._lock:
            self.db.set_notify_state(event_id, db_mod.NOTIFY_SENT, count_attempt=True)
            cleared_by = self.db.get_event(event_id)["cleared_by"]
        log.info("이벤트 #%d 알림 전송 완료", event_id)
        if cleared_by:
            self._send_clear(event_id)

    def _send_clear(self, event_id: int) -> None:
        event = self.db.get_event(event_id)
        try:
            self.notifier.send(clear_message(event, event["cleared_by"], self.location))
        except NotifierError as e:
            log.error("이벤트 #%d 해제 알림 전송 실패: %s", event_id, e)

    def _retry_loop(self) -> None:
        while not self._stop.wait(self.retry_check_seconds):
            for event in self.db.due_retries():
                log.info("이벤트 #%d 알림 재전송 시도", event["id"])
                self._send_alert(event["id"], delayed=True)

    # --- 장치 상태 감시 ---
    def _monitor_loop(self) -> None:
        while not self._stop.wait(self.monitor_interval):
            try:
                self.check_devices(time.monotonic(), datetime.now())
            except Exception:
                log.exception("장치 상태 점검 중 오류")

    def _send_system(self, kind: str, text: str) -> bool:
        """장치 이상·복구·일일 보고 전송. 성공(또는 알림 미설정)이면 True."""
        if not self.notifier.enabled:
            self.notifier.send(text)
            self.db.insert_system_event(kind, text, db_mod.NOTIFY_DISABLED)
            return True
        try:
            self.notifier.send(text)
        except NotifierError as e:
            log.error("%s 알림 전송 실패: %s", kind, e)
            return False
        self.db.insert_system_event(kind, text, db_mod.NOTIFY_SENT)
        return True

    def check_devices(self, mono_now: float, wall_now: datetime) -> None:
        """감지가 멈춘 상태가 오래 이어지면 보호자에게 알리고, 복구되면 다시 알린다.

        현장 화면 표시만으로는 아무도 화면을 보지 않을 때 감지 중단을 알 수 없기 때문이다.
        전송에 실패하면 다음 점검 때 다시 시도한다.
        """
        with self._lock:
            problems = self._device_problems(mono_now)
        if problems:
            if self._problem_since is None:
                self._problem_since = mono_now
                self._problem_since_iso = wall_now.isoformat(timespec="milliseconds")
                log.warning("장치 이상 감지: %s", problems)
            if not self._device_alert_sent and mono_now - self._problem_since >= self.device_alert_after:
                text = device_problem_message(problems, self._problem_since_iso, self.location)
                self._device_alert_sent = self._send_system("DEVICE_PROBLEM", text)
        elif self._problem_since is not None:
            if self._device_alert_sent:
                if not self._send_system("DEVICE_RECOVERED", device_recovered_message(self.location)):
                    return
            log.info("장치 상태 정상 복구")
            self._problem_since = self._problem_since_iso = None
            self._device_alert_sent = False

        if self.daily_report_time and wall_now.strftime("%H:%M") == self.daily_report_time:
            today = wall_now.date().isoformat()
            if self._last_daily_date != today:
                since = (wall_now - timedelta(days=1)).isoformat(timespec="milliseconds")
                counts = self.db.counts_since(since)
                text = daily_message(counts["events"], counts["alarms"], problems, self.location)
                if self._send_system("DAILY_REPORT", text):
                    self._last_daily_date = today

    # --- 화면 입력 ---
    def clear_by_button(self) -> bool:
        """화면 '괜찮아요' 버튼. 의심 단계면 해제, 경보 단계면 경보를 끝낸다."""
        send_clear = None
        with self._lock:
            event_id = self.event_id
            if event_id is None:
                return False
            if self.state == SUSPECT:
                self._finish(event_id, db_mod.CLEARED_BUTTON, None, None)
                return True
            self.siren.stop()
            self.db.close_event(event_id, "BUTTON")
            notify_state = self.db.get_event(event_id)["notify_state"]
            if notify_state == db_mod.NOTIFY_SENT:
                send_clear = event_id
            elif notify_state == db_mod.NOTIFY_FAILED:
                self.db.set_notify_state(event_id, db_mod.NOTIFY_CANCELLED)
            # pending이면 전송 중인 스레드가 끝난 뒤 해제 알림을 보낸다.
            self.state, self.event_id, self.voice_attempt = NORMAL, None, 0
            log.info("이벤트 #%d 경보를 화면 버튼으로 해제", event_id)
        if send_clear is not None:
            threading.Thread(target=self._send_clear, args=(send_clear,), daemon=True).start()
        return True

    # --- 화면 조회 ---
    def _device_problems(self, mono_now: Optional[float] = None) -> List[str]:
        mono_now = time.monotonic() if mono_now is None else mono_now
        problems = []
        hb = self._heartbeat
        if self._heartbeat_at is None or mono_now - self._heartbeat_at > self.heartbeat_stale_seconds:
            problems.append("영상 감지 프로그램이 응답하지 않습니다")
        else:
            if not hb.get("detector_ok", True):
                problems.append("영상 분석 모델을 불러오지 못했습니다")
            if not hb.get("camera_ok", True):
                problems.append("카메라 연결을 확인해 주세요")
        if not self.voice.ok:
            problems.append("마이크 또는 음성 인식을 확인해 주세요")
        return problems

    def get_status(self) -> Dict:
        with self._lock:
            event = None
            if self.event_id is not None:
                row = self.db.get_event(self.event_id)
                event = {
                    "id": row["id"],
                    "started_at": row["started_at"],
                    "result": row["result"],
                    "notify_state": row["notify_state"],
                    "voice_attempt": self.voice_attempt,
                    "voice_attempts": self.voice_attempts,
                }
            hb = self._heartbeat
            return {
                "state": self.state,
                "event": event,
                "problems": self._device_problems(),
                "notifier_enabled": self.notifier.enabled,
                "device_alert_sent": self._device_alert_sent,
                "person_detected": hb.get("person_detected"),
                "fps": hb.get("fps"),
                "features": hb.get("features"),
            }
