"""이벤트 처리: 낙상 의심 -> 음성 확인 -> 해제 또는 경보·보호자 알림. 장치 감시와 재시작 복원도 여기서 한다.

모든 상태는 이벤트 루프 한 곳에서만 바꾼다. 기다림(await)이 끝날 때마다 '아직 같은 이벤트가 같은 단계인지'를
확인한 뒤 바꾸므로, 음성 확인 도중 화면 버튼으로 해제해도 늦게 도착한 음성 결과가 경보를 다시 띄우지 않는다.

    NORMAL --낙상 의심--> SUSPECT --'괜찮다'/화면 버튼--> NORMAL
                             |--도움 요청·무응답·불분명 반복·장치 오류·연결 끊김--> ALARM --화면 버튼--> NORMAL
"""

from __future__ import annotations

import asyncio
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Awaitable, Callable, Dict, List, Optional, Tuple

import numpy as np

from server import store as st
from server.device import DeviceDisconnected, DeviceLink, DeviceRegistry
from server.notifier import (Notifier, NotifierError, alert_message, clear_message, device_problem_message,
                             device_recovered_message)
from server.speech import BYTES_PER_SECOND, Sound
from server.store import Store, now_iso
from server.voice import DEVICE_FAIL, HELP, NO_REPLY, SAFE, Keywords, Recognizer, judge

log = logging.getLogger("server.events")

NORMAL, SUSPECT, ALARM = "NORMAL", "SUSPECT", "ALARM"
PHASE_PROMPT, PHASE_LISTEN = "prompt", "listen"
SYSTEM_RETRY_SECONDS = 30.0


def pcm_peak(pcm: bytes) -> int:
    samples = np.frombuffer(pcm[:len(pcm) // 2 * 2], dtype="<i2")
    return int(np.abs(samples.astype(np.int32)).max()) if len(samples) else 0


class EventService:
    def __init__(self, settings, store: Store, devices: DeviceRegistry, recognizer: Recognizer,
                 notifier: Notifier, *, prompt: Callable[[], Awaitable[Sound]], siren: bytes,
                 on_finished: Callable[[], None] = lambda: None,
                 model_problems: Callable[[], List[str]] = lambda: []):
        self.s = settings
        self.store = store
        self.devices = devices
        self.recognizer = recognizer
        self.notifier = notifier
        self.keywords = Keywords.from_settings(settings)
        self._prompt = prompt
        self.siren = siren
        self.on_finished = on_finished
        self.model_problems = model_problems
        self._voice_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="voice")

        self.state = NORMAL
        self.event_id: Optional[int] = None
        self.device_id: Optional[str] = None
        self.voice_attempt = 0
        self.phase: Optional[str] = None
        self._voice_task: Optional[asyncio.Task] = None
        self._tasks: set = set()
        self._loops: List[asyncio.Task] = []
        # 장치 이상 알림
        self._problem_since: Optional[float] = None
        self._problem_since_iso: Optional[str] = None
        self._last_problems: List[str] = []
        self.device_alert_sent = False
        self._system_retry_at = 0.0

    # --- 수명 주기 ---
    async def start(self) -> None:
        interrupted = self.store.mark_interrupted()
        if interrupted:
            log.warning("재시작 전 처리 중이던 이벤트 %d건을 INTERRUPTED로 정리했습니다.", interrupted)
        requeued = self.store.requeue_pending_notifications()
        if requeued:
            log.warning("재시작 전 보내는 도중 끊긴 경보 알림 %d건을 다시 보냅니다.", requeued)
        alarm = self.store.unfinished_alarm()
        if alarm is not None:
            # 해제되지 않은 경보는 재시작 후에도 경보로 남겨 현장에서 확인·해제할 수 있게 한다.
            self.state, self.event_id, self.device_id = ALARM, alarm["id"], alarm["device_id"]
            log.warning("재시작 전 해제되지 않은 경보 #%d를 다시 표시합니다.", alarm["id"])
        if self.s.event_retention_days > 0:
            self.store.purge_older_than(self.s.event_retention_days)
        self._loops = [asyncio.create_task(self._retry_loop(), name="notify-retry"),
                       asyncio.create_task(self._monitor_loop(), name="device-monitor")]

    async def stop(self) -> None:
        tasks = [*self._loops, *self._tasks, *([self._voice_task] if self._voice_task else [])]
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._voice_executor.shutdown(wait=False)

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        task.add_done_callback(self._log_task_error)
        return task

    @staticmethod
    def _log_task_error(task: asyncio.Task) -> None:
        if not task.cancelled() and task.exception() is not None:
            log.error("백그라운드 작업 오류", exc_info=task.exception())

    def _active(self, event_id: int, state: str) -> bool:
        return self.event_id == event_id and self.state == state

    # --- 낙상 의심 ---
    async def report_suspect(self, device_id: str, features: Optional[Dict]) -> Optional[int]:
        """영상 판정의 낙상 의심. 이미 진행 중인 이벤트가 있으면 새로 만들지 않는다."""
        if self.event_id is not None:
            log.info("이벤트 #%d 진행 중이라 새 낙상 의심(%s)은 무시합니다.", self.event_id, device_id)
            return None
        event_id = self.store.insert_event(device_id, features)
        self.state, self.event_id, self.device_id = SUSPECT, event_id, device_id
        self.voice_attempt, self.phase = 0, None
        log.warning("낙상 의심 이벤트 #%d 시작 (%s)", event_id, device_id)
        self._voice_task = self._spawn(self._voice_flow(event_id, device_id))
        return event_id

    # --- 음성 확인 ---
    async def _voice_flow(self, event_id: int, device_id: str) -> None:
        last: Tuple[str, Optional[str]] = (NO_REPLY, None)
        try:
            for attempt in range(1, self.s.voice_max_attempts + 1):
                if not self._active(event_id, SUSPECT):
                    return
                self.voice_attempt = attempt
                self.store.set_voice_attempts(event_id, attempt)
                log.info("이벤트 #%d 음성 확인 %d/%d", event_id, attempt, self.s.voice_max_attempts)
                result, text = await self._ask(device_id)
                if not self._active(event_id, SUSPECT):
                    return
                log.info("이벤트 #%d 음성 확인 결과: %s", event_id, result)
                if result == SAFE:
                    self._finish(event_id, st.CLEARED_VOICE, result, text)
                    return
                if result == HELP:
                    outcome = (st.ALERT_HELP, result, text)
                    break
                if result == DEVICE_FAIL:
                    outcome = (st.ALERT_VOICE_FAIL, result, text)  # 확인할 수 없으면 안전 쪽(경보)
                    break
                last = (result, text)
            else:
                outcome = (st.ALERT_NO_REPLY, *last)
        except DeviceDisconnected:
            log.error("이벤트 #%d 음성 확인 중 장치 연결이 끊겼습니다.", event_id)
            outcome = (st.ALERT_DISCONNECTED, None, None)
        except Exception:
            # 예상하지 못한 오류로 이벤트가 '확인 중'에 멈춰 있지 않게 안전 쪽(경보)으로 끝낸다.
            log.exception("이벤트 #%d 음성 확인 중 오류", event_id)
            outcome = (st.ALERT_VOICE_FAIL, DEVICE_FAIL, None)
        finally:
            self.phase = None
        if self._active(event_id, SUSPECT):
            self._raise_alarm(event_id, *outcome)

    async def _ask(self, device_id: str) -> Tuple[str, Optional[str]]:
        """안내 음성 재생 -> playback_done -> 마이크 켜고 듣기 -> 판단."""
        link = self.devices.get(device_id)
        if link is None:
            raise DeviceDisconnected(device_id)
        problem = link.voice_device_problem()
        if problem:
            log.error("음성 확인 불가: %s", problem)
            return DEVICE_FAIL, None
        if not self.recognizer.ok:
            log.error("음성 확인 불가: %s", self.recognizer.error)
            return DEVICE_FAIL, None
        prompt = await self._prompt()
        self.phase = PHASE_PROMPT
        pid = await link.play(prompt.pcm)
        try:
            await link.wait_playback(pid, prompt.seconds + self.s.playback_timeout_margin)
        except asyncio.TimeoutError:
            log.error("안내 음성 재생 완료(playback_done)가 오지 않았습니다 (스피커 확인 필요)")
            return DEVICE_FAIL, None
        self.phase = PHASE_LISTEN
        return await self._listen(link)

    async def _listen(self, link: DeviceLink) -> Tuple[str, Optional[str]]:
        """마이크를 켜고 받은 소리를 실시간으로 인식한다. listen_seconds만큼 듣고 마이크를 끈다."""
        loop = asyncio.get_running_loop()
        session = await loop.run_in_executor(self._voice_executor, self.recognizer.session)
        needed = int(self.s.listen_seconds * BYTES_PER_SECOND)
        received = peak = 0
        queue = await link.start_mic()
        deadline = loop.time() + self.s.listen_seconds + self.s.listen_grace_seconds
        try:
            while received < needed:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    break
                try:
                    chunk = await asyncio.wait_for(queue.get(), remaining)
                except asyncio.TimeoutError:
                    break
                if chunk is None:
                    raise DeviceDisconnected(link.device_id)
                received += len(chunk)
                peak = max(peak, pcm_peak(chunk))
                await loop.run_in_executor(self._voice_executor, session.accept, chunk)
        finally:
            await link.stop_mic()
        if received == 0:
            log.error("마이크 소리가 들어오지 않았습니다")
            return DEVICE_FAIL, None
        if peak == 0:
            # 완전한 무음은 조용한 방이 아니라 마이크 고장·음소거일 가능성이 높다.
            log.error("마이크 입력이 완전한 무음입니다 (연결·음소거 확인)")
            return DEVICE_FAIL, None
        rec = await loop.run_in_executor(self._voice_executor, session.finish)
        return judge(rec, self.keywords, self.s.voice_grammar_min_conf)

    def _voice_text(self, text: Optional[str]) -> Optional[str]:
        return text if self.s.store_voice_text else None

    def _end_event(self) -> None:
        self.state, self.event_id, self.device_id = NORMAL, None, None
        self.voice_attempt, self.phase, self._voice_task = 0, None, None
        self.on_finished()  # 영상 판정을 다시 시작한다

    def _finish(self, event_id: int, result: str, voice_result: Optional[str], text: Optional[str]) -> None:
        self.store.set_result(event_id, result, voice_result, self._voice_text(text))
        log.info("이벤트 #%d 해제: %s", event_id, result)
        self._end_event()

    def _raise_alarm(self, event_id: int, result: str, voice_result: Optional[str], text: Optional[str]) -> None:
        self.store.set_result(event_id, result, voice_result, self._voice_text(text), ended=False)
        # 알림 상태를 먼저 정해 둔다. 보내기 전에 해제 버튼이 눌려도 해제 알림 순서가 맞게 하기 위함이다.
        self.store.set_notify_state(event_id, st.NOTIFY_PENDING if self.notifier.enabled else st.NOTIFY_DISABLED)
        self.state, self.phase = ALARM, None
        log.warning("이벤트 #%d 경보: %s", event_id, result)
        for link in self.devices.connected():
            self._spawn(self._play_siren(link, event_id))
        self._spawn(self._send_alert(event_id))

    async def _play_siren(self, link: DeviceLink, event_id: int) -> None:
        try:
            await link.play(self.siren, loop=True, still_wanted=lambda: self._active(event_id, ALARM))
        except DeviceDisconnected:
            pass

    async def on_device_connected(self, link: DeviceLink) -> None:
        """경보 중에 접속한 장치(재접속·재시작 복원)에도 경보음을 울린다."""
        if self.state == ALARM and self.event_id is not None:
            await self._play_siren(link, self.event_id)

    # --- 화면 버튼 ---
    async def clear_by_button(self) -> bool:
        """현장 화면 '괜찮아요' 버튼. 의심 단계면 해제, 경보 단계면 경보를 끝낸다."""
        event_id = self.event_id
        if event_id is None:
            return False
        if self.state == SUSPECT:
            task = self._voice_task
            self._finish(event_id, st.CLEARED_BUTTON, None, None)
            if task is not None:
                task.cancel()  # 듣는 중이면 마이크를 끈다(finally)
            for link in self.devices.connected():
                self._spawn(link.stop_playback())  # 안내 음성 중단
            return True

        self.store.close_event(event_id, "BUTTON")
        notify_state = self.store.get_event(event_id)["notify_state"]
        if notify_state in (st.NOTIFY_SENT, st.NOTIFY_DISABLED):
            self._spawn(self._send_clear(event_id))
        elif notify_state == st.NOTIFY_FAILED:
            self.store.set_notify_state(event_id, st.NOTIFY_CANCELLED)
        # pending이면 보내는 작업이 끝난 뒤 해제 알림을 보낸다.
        log.info("이벤트 #%d 경보를 화면 버튼으로 해제", event_id)
        self._end_event()
        for link in self.devices.connected():
            self._spawn(link.stop_playback())  # 경보음 중단
        return True

    # --- 보호자 알림 ---
    async def _send_alert(self, event_id: int, delayed: bool = False) -> None:
        event = self.store.get_event(event_id)
        if event is None or event["notify_state"] == st.NOTIFY_CANCELLED:
            return
        message = alert_message(event, delayed, self.s.device_name)
        if not self.notifier.enabled:
            await self.notifier.send(message)
            self.store.set_notify_state(event_id, st.NOTIFY_DISABLED)
            return
        self.store.set_notify_state(event_id, st.NOTIFY_PENDING)
        try:
            await self.notifier.send(message)
        except NotifierError as e:
            delays = self.s.notify_retry_delays
            delay = delays[min(event["notify_attempts"], len(delays) - 1)]
            cleared = self.store.get_event(event_id)["cleared_by"] is not None
            next_at = None if cleared else now_iso(datetime.now() + timedelta(seconds=delay))
            self.store.set_notify_state(event_id, st.NOTIFY_CANCELLED if cleared else st.NOTIFY_FAILED,
                                        next_at=next_at, count_attempt=True)
            if cleared:
                log.error("이벤트 #%d 알림 전송 실패 (%s), 이미 해제되어 다시 보내지 않음", event_id, e)
            else:
                log.error("이벤트 #%d 알림 전송 실패 (%s), %.0f초 후 다시 보냄", event_id, e, delay)
            return
        self.store.set_notify_state(event_id, st.NOTIFY_SENT, count_attempt=True)
        log.info("이벤트 #%d 보호자 알림 전송 완료%s", event_id, " (지연 전송)" if delayed else "")
        if self.store.get_event(event_id)["cleared_by"]:
            await self._send_clear(event_id)

    async def _send_clear(self, event_id: int) -> None:
        event = self.store.get_event(event_id)
        try:
            await self.notifier.send(clear_message(event, self.s.device_name))
        except NotifierError as e:
            log.error("이벤트 #%d 해제 알림 전송 실패: %s", event_id, e)

    async def _retry_loop(self) -> None:
        while True:
            await asyncio.sleep(self.s.notify_retry_check_seconds)
            await self.retry_due()

    async def retry_due(self) -> None:
        for event in self.store.due_retries():
            log.info("이벤트 #%d 알림 다시 보내기", event["id"])
            await self._send_alert(event["id"], delayed=True)

    # --- 장치 감시 ---
    def problems(self, now: Optional[float] = None) -> List[str]:
        now = time.monotonic() if now is None else now
        return [*self.devices.problems(now, self.s.status_stale_seconds, self.s.video_stale_seconds),
                *self.model_problems()]

    async def _monitor_loop(self) -> None:
        while True:
            await asyncio.sleep(self.s.monitor_interval_seconds)
            try:
                await self.check_devices(time.monotonic())
            except Exception:
                log.exception("장치 상태 점검 중 오류")

    async def _send_system(self, kind: str, text: str) -> bool:
        """장치 이상·복구 알림. 보냈거나 알림 미설정이면 True."""
        if not self.notifier.enabled:
            await self.notifier.send(text)
            self.store.insert_system_event(kind, text, st.NOTIFY_DISABLED)
            return True
        try:
            await self.notifier.send(text)
        except NotifierError as e:
            log.error("%s 알림 전송 실패 (%s), %.0f초 후 다시 보냄", kind, e, SYSTEM_RETRY_SECONDS)
            return False
        self.store.insert_system_event(kind, text, st.NOTIFY_SENT)
        return True

    async def check_devices(self, now: float) -> None:
        """장치 문제가 device_alert_after_seconds 이상 이어지면 보호자에게 알리고, 복구되면 다시 알린다.

        현장 화면 표시만으로는 아무도 화면을 보지 않을 때 감지가 멈춘 것을 알 수 없기 때문이다.
        """
        problems = self.problems(now)
        if problems != self._last_problems:
            if problems:
                log.warning("장치 문제: %s", ", ".join(problems))
            self._last_problems = problems
        if problems:
            if self._problem_since is None:
                self._problem_since, self._problem_since_iso = now, now_iso()
            if (not self.device_alert_sent and now - self._problem_since >= self.s.device_alert_after_seconds
                    and now >= self._system_retry_at):
                text = device_problem_message(problems, self._problem_since_iso, self.s.device_name)
                self.device_alert_sent = await self._send_system("DEVICE_PROBLEM", text)
                if not self.device_alert_sent:
                    self._system_retry_at = now + SYSTEM_RETRY_SECONDS
        elif self._problem_since is not None:
            if self.device_alert_sent:
                if now < self._system_retry_at:
                    return
                if not await self._send_system("DEVICE_RECOVERED", device_recovered_message(self.s.device_name)):
                    self._system_retry_at = now + SYSTEM_RETRY_SECONDS
                    return
            log.info("장치 상태 정상 복구")
            self._problem_since = self._problem_since_iso = None
            self.device_alert_sent = False
            self._system_retry_at = 0.0

    # --- 화면 조회 ---
    def event_snapshot(self) -> Optional[Dict]:
        if self.event_id is None:
            return None
        row = self.store.get_event(self.event_id)
        return {
            "id": row["id"],
            "device_id": row["device_id"],
            "started_at": row["started_at"],
            "result": row["result"],
            "notify_state": row["notify_state"],
            "voice_attempt": self.voice_attempt,
            "voice_attempts": self.s.voice_max_attempts,
            "phase": self.phase,
        }
