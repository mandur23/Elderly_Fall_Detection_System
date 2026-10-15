"""이벤트 처리: 장치(라즈베리파이)마다 낙상 의심 -> 음성 확인 -> 해제 또는 경보·보호자 알림.
장치 감시와 재시작 복원도 여기서 한다.

진행 중인 이벤트(Case)는 장치마다 따로 둔다. 한 방에서 음성 확인을 하는 동안 다른 방의 낙상도 바로
처리하고, 경보음·해제도 그 장치에만 적용한다.

모든 상태는 이벤트 루프 한 곳에서만 바꾼다. 기다림(await)이 끝날 때마다 '아직 같은 이벤트가 같은 단계인지'를
확인한 뒤 바꾸므로, 음성 확인 도중 화면 버튼으로 해제해도 늦게 도착한 음성 결과가 경보를 다시 띄우지 않는다.

    (장치마다) NORMAL --낙상 의심--> SUSPECT --'괜찮다'/화면 버튼/관리자--> NORMAL
                                       |--도움 요청·무응답·불분명 반복·장치 오류·연결 끊김--> ALARM
                                                                   ALARM --화면 버튼/관리자--> NORMAL
"""

from __future__ import annotations

import asyncio
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
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
VOICE_WORKERS = 4                 # 동시에 음성 확인할 수 있는 장치 수 (넘으면 차례를 기다린다)
PURGE_INTERVAL_SECONDS = 24 * 3600
SERVER_KEY = ""  # 장치 감시에서 특정 장치가 아닌 서버 공통 문제(모델, 접속한 장치 없음)
CLEARED_BY_BUTTON, CLEARED_BY_ADMIN = "BUTTON", "ADMIN"
CLEARED_BY_GUARDIAN = "GUARDIAN:"  # 뒤에 보호자 아이디를 붙인다 (예: GUARDIAN:daughter)


def cleared_result(by: str) -> str:
    """의심 단계에서 해제했을 때 남길 결과 값."""
    if by.startswith(CLEARED_BY_GUARDIAN):
        return st.CLEARED_GUARDIAN
    return st.CLEARED_ADMIN if by == CLEARED_BY_ADMIN else st.CLEARED_BUTTON


def pcm_peak(pcm: bytes) -> int:
    samples = np.frombuffer(pcm[:len(pcm) // 2 * 2], dtype="<i2")
    return int(np.abs(samples.astype(np.int32)).max()) if len(samples) else 0


def collect_audio(queue: "asyncio.Queue[Optional[bytes]]", first: Optional[bytes]) -> Tuple[bytes, bool]:
    """first와 큐에 이미 쌓여 있는 소리 조각을 이어 붙인다. (소리, 연결 끊김 표시(None)를 만났는지)."""
    parts = []
    item = first
    while True:
        if item is None:
            return b"".join(parts), True
        parts.append(item)
        if queue.empty():
            return b"".join(parts), False
        item = queue.get_nowait()


@dataclass
class Case:
    """한 장치에서 진행 중인 이벤트."""
    device_id: str
    event_id: int
    state: str
    voice_attempt: int = 0
    phase: Optional[str] = None
    voice_task: Optional[asyncio.Task] = None


@dataclass
class Watch:
    """장치(또는 서버 공통) 문제가 이어진 시간과 보호자 알림 여부."""
    since: Optional[float] = None
    since_iso: Optional[str] = None
    alert_sent: bool = False
    retry_at: float = 0.0
    last: List[str] = field(default_factory=list)


class EventService:
    def __init__(self, settings, store: Store, devices: DeviceRegistry, recognizer: Recognizer,
                 notifier: Notifier, *, prompt: Callable[[], Awaitable[Sound]], siren: bytes,
                 on_finished: Callable[[str], None] = lambda device_id: None,
                 model_problems: Callable[[], List[str]] = lambda: [],
                 device_problems: Callable[[str, float], List[str]] = lambda device_id, now: []):
        self.s = settings
        self.store = store
        self.devices = devices
        self.recognizer = recognizer
        self.notifier = notifier
        self.keywords = Keywords.from_settings(settings)
        self._prompt = prompt
        self.siren = siren
        self.on_finished = on_finished  # 장치 하나의 이벤트가 끝나면 그 장치의 영상 판정을 다시 시작한다
        self.model_problems = model_problems
        self.device_problems = device_problems  # 장치의 영상 분석 문제 (멈춤·깨진 영상·느림)
        # 여러 방에서 동시에 음성 확인을 해도 서로 기다리지 않게 스레드를 여럿 둔다. Vosk 모델은 함께 쓰고,
        # 인식기(세션)는 확인마다 따로이며 한 세션의 호출은 차례로 기다리므로 동시에 쓰이지 않는다.
        self._voice_executor = ThreadPoolExecutor(max_workers=VOICE_WORKERS, thread_name_prefix="voice")

        self.cases: Dict[str, Case] = {}      # 진행 중인 이벤트 (장치 id -> Case)
        self._watches: Dict[str, Watch] = {}  # 장치 감시 (장치 id 또는 SERVER_KEY -> Watch)
        self._tasks: set = set()
        self._loops: List[asyncio.Task] = []
        self._last_purge = 0.0

    # --- 요약 조회 ---
    @property
    def state(self) -> str:
        """전체 상태: 경보인 장치가 하나라도 있으면 ALARM, 의심이면 SUSPECT."""
        states = {c.state for c in self.cases.values()}
        return ALARM if ALARM in states else SUSPECT if SUSPECT in states else NORMAL

    def case(self, device_id: Optional[str]) -> Optional[Case]:
        return self.cases.get(device_id) if device_id is not None else None

    def state_of(self, device_id: str) -> str:
        case = self.cases.get(device_id)
        return case.state if case else NORMAL

    @property
    def device_alert_sent(self) -> bool:
        return any(w.alert_sent for w in self._watches.values())

    def alert_sent_for(self, device_id: str) -> bool:
        return any(self._watches.get(k, Watch()).alert_sent for k in (SERVER_KEY, device_id))

    def device_label(self, device_id: Optional[str]) -> str:
        """알림·화면에 쓸 설치 위치 이름."""
        info = self.devices.known.get(device_id or "")
        return (info or {}).get("name") or self.s.device_name or device_id or ""

    # --- 수명 주기 ---
    async def start(self) -> None:
        # 오래된 기록을 먼저 지운다(해제되지 않은 경보는 지우지 않는다). 그다음 남은 경보를 되살린다.
        self._purge()
        interrupted = self.store.escalate_interrupted()
        if interrupted:
            # 음성 확인 도중 서버가 꺼졌다. 넘어진 사람은 계속 누워 있어 다시 감지되지 않을 수 있으므로
            # 확인하지 못한 것으로 보고 경보를 띄우고 보호자에게 알린다(안전 쪽).
            log.warning("재시작 전 음성 확인 중이던 이벤트 %d건을 경보로 올려 보호자에게 알립니다.", interrupted)
        requeued = self.store.requeue_pending_notifications()
        if requeued:
            log.warning("재시작 전 보내는 도중 끊긴 경보 알림 %d건을 다시 보냅니다.", requeued)
        for alarm in self.store.unfinished_alarms():
            # 해제되지 않은 경보는 재시작 후에도 경보로 남겨 현장·관리자 화면에서 확인·해제할 수 있게 한다.
            device_id = alarm["device_id"] or ""
            self.cases[device_id] = Case(device_id, alarm["id"], ALARM)
            log.warning("[%s] 재시작 전 해제되지 않은 경보 #%d를 다시 표시합니다.", device_id, alarm["id"])
        self._loops = [asyncio.create_task(self._retry_loop(), name="notify-retry"),
                       asyncio.create_task(self._monitor_loop(), name="device-monitor")]

    async def stop(self) -> None:
        tasks = [*self._loops, *self._tasks, *(c.voice_task for c in self.cases.values() if c.voice_task)]
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

    def _active(self, case: Case, state: str) -> bool:
        """case가 아직 그 장치의 진행 중인 이벤트이고 state 단계인지."""
        return self.cases.get(case.device_id) is case and case.state == state

    # --- 낙상 의심 ---
    async def report_suspect(self, device_id: str, features: Optional[Dict]) -> Optional[int]:
        """영상 판정의 낙상 의심. 그 장치에 이미 진행 중인 이벤트가 있으면 새로 만들지 않는다."""
        current = self.cases.get(device_id)
        if current is not None:
            log.info("[%s] 이벤트 #%d 진행 중이라 같은 장치의 새 낙상 의심은 무시합니다.", device_id, current.event_id)
            return None
        event_id = self.store.insert_event(device_id, features)
        case = Case(device_id, event_id, SUSPECT)
        self.cases[device_id] = case
        log.warning("[%s] 낙상 의심 이벤트 #%d 시작", device_id, event_id)
        case.voice_task = self._spawn(self._voice_flow(case))
        return event_id

    # --- 음성 확인 ---
    async def _voice_flow(self, case: Case) -> None:
        event_id = case.event_id
        last: Tuple[str, Optional[str]] = (NO_REPLY, None)
        try:
            for attempt in range(1, self.s.voice_max_attempts + 1):
                if not self._active(case, SUSPECT):
                    return
                case.voice_attempt = attempt
                self.store.set_voice_attempts(event_id, attempt)
                log.info("[%s] 이벤트 #%d 음성 확인 %d/%d", case.device_id, event_id, attempt,
                         self.s.voice_max_attempts)
                result, text = await self._ask(case)
                if not self._active(case, SUSPECT):
                    return
                log.info("[%s] 이벤트 #%d 음성 확인 결과: %s", case.device_id, event_id, result)
                if result == SAFE:
                    self._finish(case, st.CLEARED_VOICE, result, text)
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
            log.error("[%s] 이벤트 #%d 음성 확인 중 장치 연결이 끊겼습니다.", case.device_id, event_id)
            outcome = (st.ALERT_DISCONNECTED, None, None)
        except Exception:
            # 예상하지 못한 오류로 이벤트가 '확인 중'에 멈춰 있지 않게 안전 쪽(경보)으로 끝낸다.
            log.exception("[%s] 이벤트 #%d 음성 확인 중 오류", case.device_id, event_id)
            outcome = (st.ALERT_VOICE_FAIL, DEVICE_FAIL, None)
        finally:
            case.phase = None
        if self._active(case, SUSPECT):
            self._raise_alarm(case, *outcome)

    async def _ask(self, case: Case) -> Tuple[str, Optional[str]]:
        """안내 음성 재생 -> playback_done -> 마이크 켜고 듣기 -> 판단."""
        link = self.devices.get(case.device_id)
        if link is None:
            raise DeviceDisconnected(case.device_id)
        problem = link.voice_device_problem()
        if problem:
            log.error("[%s] 음성 확인 불가: %s", case.device_id, problem)
            return DEVICE_FAIL, None
        if not self.recognizer.ok:
            log.error("음성 확인 불가: %s", self.recognizer.error)
            return DEVICE_FAIL, None
        prompt = await self._prompt()
        case.phase = PHASE_PROMPT
        pid = await link.play(prompt.pcm)
        try:
            await link.wait_playback(pid, prompt.seconds + self.s.playback_timeout_margin)
        except asyncio.TimeoutError:
            log.error("[%s] 안내 음성 재생 완료(playback_done)가 오지 않았습니다 (스피커 확인 필요)", case.device_id)
            return DEVICE_FAIL, None
        case.phase = PHASE_LISTEN
        return await self._listen(link)

    async def _listen(self, link: DeviceLink) -> Tuple[str, Optional[str]]:
        """마이크를 켜고 받은 소리를 실시간으로 인식한다. listen_seconds만큼 듣고 마이크를 끈다.

        인식이 소리보다 늦어지면(서버 PC 부하) 그사이 쌓인 소리를 한꺼번에 넘기고, 듣는 시간이 끝났을 때
        이미 도착해 있던 소리도 인식한다. 대답 끝부분을 버려 '무응답'으로 잘못 판단하지 않게 하기 위함이다.
        """
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
                pcm, ended = collect_audio(queue, chunk)
                if ended:
                    raise DeviceDisconnected(link.device_id)
                received += len(pcm)
                peak = max(peak, pcm_peak(pcm))
                await loop.run_in_executor(self._voice_executor, session.accept, pcm)
        finally:
            await link.stop_mic()
        if received < needed and not queue.empty():
            pcm, _ = collect_audio(queue, queue.get_nowait())
            pcm = pcm[:needed - received]
            if pcm:
                received += len(pcm)
                peak = max(peak, pcm_peak(pcm))
                await loop.run_in_executor(self._voice_executor, session.accept, pcm)
        if received == 0:
            log.error("[%s] 마이크 소리가 들어오지 않았습니다", link.device_id)
            return DEVICE_FAIL, None
        if peak == 0:
            # 완전한 무음은 조용한 방이 아니라 마이크 고장·음소거일 가능성이 높다.
            log.error("[%s] 마이크 입력이 완전한 무음입니다 (연결·음소거 확인)", link.device_id)
            return DEVICE_FAIL, None
        rec = await loop.run_in_executor(self._voice_executor, session.finish)
        return judge(rec, self.keywords, self.s.voice_grammar_min_conf)

    def _voice_text(self, text: Optional[str]) -> Optional[str]:
        return text if self.s.store_voice_text else None

    def _end_case(self, case: Case) -> None:
        if self.cases.get(case.device_id) is case:
            del self.cases[case.device_id]
        case.state, case.phase, case.voice_task = NORMAL, None, None
        self.on_finished(case.device_id)

    def _finish(self, case: Case, result: str, voice_result: Optional[str], text: Optional[str]) -> None:
        self.store.set_result(case.event_id, result, voice_result, self._voice_text(text))
        log.info("[%s] 이벤트 #%d 해제: %s", case.device_id, case.event_id, result)
        self._end_case(case)

    def _raise_alarm(self, case: Case, result: str, voice_result: Optional[str], text: Optional[str]) -> None:
        event_id = case.event_id
        # 기록 파일에 문제가 있어도 현장 경보(화면·경보음)는 먼저 띄운다.
        case.state, case.phase = ALARM, None
        log.warning("[%s] 이벤트 #%d 경보: %s", case.device_id, event_id, result)
        link = self.devices.get(case.device_id)
        if link is not None:  # 경보음은 넘어진 사람이 있는 그 장치에서만 울린다
            self._spawn(self._play_siren(link, case))
        try:
            self.store.set_result(event_id, result, voice_result, self._voice_text(text), ended=False)
            # 알림 상태를 먼저 정해 둔다. 보내기 전에 해제 버튼이 눌려도 해제 알림 순서가 맞게 하기 위함이다.
            self.store.set_notify_state(event_id, st.NOTIFY_PENDING if self.notifier.enabled else st.NOTIFY_DISABLED)
        except Exception:
            log.exception("[%s] 이벤트 #%d 경보를 기록하지 못했습니다", case.device_id, event_id)
        self._spawn(self._send_alert(event_id))

    async def _play_siren(self, link: DeviceLink, case: Case) -> None:
        try:
            await link.play(self.siren, loop=True, still_wanted=lambda: self._active(case, ALARM))
        except DeviceDisconnected:
            pass

    async def on_device_connected(self, link: DeviceLink) -> None:
        """경보 중인 장치가 다시 접속하면(재접속·재시작 복원) 경보음을 다시 울린다."""
        case = self.cases.get(link.device_id)
        if case is not None and case.state == ALARM:
            await self._play_siren(link, case)

    # --- 해제 (현장 화면 버튼, 관리자 화면) ---
    async def clear(self, device_id: Optional[str] = None, by: str = CLEARED_BY_BUTTON) -> bool:
        """의심 단계면 해제, 경보 단계면 경보를 끝낸다.

        device_id가 없으면 진행 중인 이벤트가 하나뿐일 때만 그것을 해제한다(장치가 한 대인 현장 화면).
        """
        if device_id is None:
            if len(self.cases) != 1:
                return False
            device_id = next(iter(self.cases))
        case = self.cases.get(device_id)
        if case is None:
            return False
        link = self.devices.get(device_id)
        if case.state == SUSPECT:
            task = case.voice_task
            self._finish(case, cleared_result(by), None, None)
            self.store.close_event(case.event_id, by)
            if task is not None:
                task.cancel()  # 듣는 중이면 마이크를 끈다(finally)
            if link is not None:
                self._spawn(link.stop_playback())  # 안내 음성 중단
            return True

        event_id = case.event_id
        self.store.close_event(event_id, by)
        notify_state = (self.store.get_event(event_id) or {}).get("notify_state")
        if notify_state in (st.NOTIFY_SENT, st.NOTIFY_DISABLED):
            self._spawn(self._send_clear(event_id))
        elif notify_state == st.NOTIFY_FAILED:
            self.store.set_notify_state(event_id, st.NOTIFY_CANCELLED)
        # pending이면 보내는 작업이 끝난 뒤 해제 알림을 보낸다.
        log.info("[%s] 이벤트 #%d 경보 해제 (%s)", device_id, event_id, by)
        self._end_case(case)
        if link is not None:
            self._spawn(link.stop_playback())  # 경보음 중단
        return True

    async def clear_by_button(self, device_id: Optional[str] = None) -> bool:
        return await self.clear(device_id, CLEARED_BY_BUTTON)

    # --- 보호자 알림 ---
    async def _send_alert(self, event_id: int, delayed: bool = False) -> None:
        event = self.store.get_event(event_id)
        if event is None or event["notify_state"] == st.NOTIFY_CANCELLED:
            return
        link = f"{self.s.public_url.rstrip('/')}/g" if self.s.public_url else ""
        message = alert_message(event, delayed, self.device_label(event["device_id"]), link)
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
            await self.notifier.send(clear_message(event, self.device_label(event["device_id"])))
        except NotifierError as e:
            log.error("이벤트 #%d 해제 알림 전송 실패: %s", event_id, e)

    async def _retry_loop(self) -> None:
        while True:
            await asyncio.sleep(self.s.notify_retry_check_seconds)
            try:
                await self.retry_due()
                if time.monotonic() - self._last_purge >= PURGE_INTERVAL_SECONDS:
                    self._purge()
            except Exception:  # 한 번의 오류로 알림 다시 보내기가 영영 멈추지 않게 한다
                log.exception("알림 다시 보내기 중 오류")

    async def retry_due(self) -> None:
        for event in self.store.due_retries():
            log.info("이벤트 #%d 알림 다시 보내기", event["id"])
            try:
                await self._send_alert(event["id"], delayed=True)
            except Exception:
                log.exception("이벤트 #%d 알림 다시 보내기 실패", event["id"])

    def _purge(self) -> None:
        """보관 기간(event_retention_days)이 지난 기록을 지운다. 시작할 때와 하루에 한 번."""
        self._last_purge = time.monotonic()
        if self.s.event_retention_days > 0:
            n = self.store.purge_older_than(self.s.event_retention_days)
            if n:
                log.info("보관 기간이 지난 기록 %d건을 지웠습니다", n)

    # --- 장치 감시 ---
    def problems_by_subject(self, now: Optional[float] = None) -> Dict[str, List[str]]:
        """서버 공통(SERVER_KEY)과 장치별 문제. 한 번이라도 접속한 장치는 끊겨도 목록에 남는다."""
        now = time.monotonic() if now is None else now
        out = {SERVER_KEY: list(self.model_problems())}
        if not self.devices.links and not self.devices.known:
            out[SERVER_KEY].insert(0, "라즈베리파이가 아직 연결되지 않았습니다")
        # 재시작 후 되살린 경보의 장치는 아직 다시 접속하지 않았어도 목록에 넣는다.
        for device_id in dict.fromkeys([*self.devices.known, *self.devices.links, *filter(None, self.cases)]):
            link = self.devices.links.get(device_id)
            out[device_id] = (link.problems(now, self.s.status_stale_seconds, self.s.video_stale_seconds)
                              + self.device_problems(device_id, now)
                              if link is not None else ["라즈베리파이 연결이 끊겼습니다"])
        return out

    def problems(self, now: Optional[float] = None, device_id: Optional[str] = None) -> List[str]:
        """화면에 보여 줄 문제. device_id를 주면 서버 공통 문제와 그 장치의 문제만."""
        subjects = self.problems_by_subject(now)
        out = list(subjects.pop(SERVER_KEY))
        if device_id is not None:
            return out + subjects.get(device_id, [])
        many = len(subjects) > 1
        for key, problems in subjects.items():
            out.extend(f"{self.device_label(key)}: {p}" if many else p for p in problems)
        return out

    async def _monitor_loop(self) -> None:
        while True:
            await asyncio.sleep(self.s.monitor_interval_seconds)
            try:
                await self.check_devices(time.monotonic())
            except Exception:
                log.exception("장치 상태 점검 중 오류")

    async def _send_system(self, kind: str, text: str, device_id: Optional[str]) -> bool:
        """장치 이상·복구 알림. 보냈거나 알림 미설정이면 True."""
        if not self.notifier.enabled:
            await self.notifier.send(text)
            self.store.insert_system_event(kind, text, st.NOTIFY_DISABLED, device_id)
            return True
        try:
            await self.notifier.send(text)
        except NotifierError as e:
            log.error("%s 알림 전송 실패 (%s), %.0f초 후 다시 보냄", kind, e, SYSTEM_RETRY_SECONDS)
            return False
        self.store.insert_system_event(kind, text, st.NOTIFY_SENT, device_id)
        return True

    async def check_devices(self, now: float) -> None:
        """장치(또는 서버 공통) 문제가 device_alert_after_seconds 이상 이어지면 보호자에게 알리고, 복구되면 다시 알린다.

        현장 화면 표시만으로는 아무도 화면을 보지 않을 때 감지가 멈춘 것을 알 수 없기 때문이다.
        장치마다 따로 재므로 한 방의 장치가 고장 나도 다른 방 알림과 섞이지 않는다.
        """
        for key, problems in self.problems_by_subject(now).items():
            await self._check_subject(key, problems, now)

    async def _check_subject(self, key: str, problems: List[str], now: float) -> None:
        w = self._watches.setdefault(key, Watch())
        device_id = key or None
        location = self.device_label(device_id) if device_id else self.s.device_name
        if problems != w.last:
            if problems:
                log.warning("장치 문제%s: %s", f" [{key}]" if key else "", ", ".join(problems))
            w.last = problems
        if problems:
            if w.since is None:
                w.since, w.since_iso = now, now_iso()
            if not w.alert_sent and now - w.since >= self.s.device_alert_after_seconds and now >= w.retry_at:
                text = device_problem_message(problems, w.since_iso, location)
                w.alert_sent = await self._send_system("DEVICE_PROBLEM", text, device_id)
                if not w.alert_sent:
                    w.retry_at = now + SYSTEM_RETRY_SECONDS
        elif w.since is not None:
            if w.alert_sent:
                if now < w.retry_at:
                    return
                if not await self._send_system("DEVICE_RECOVERED", device_recovered_message(location), device_id):
                    w.retry_at = now + SYSTEM_RETRY_SECONDS
                    return
            log.info("장치 상태 정상 복구%s", f" [{key}]" if key else "")
            w.since = w.since_iso = None
            w.alert_sent = False
            w.retry_at = 0.0

    def forget(self, device_id: str) -> None:
        """관리자가 목록에서 지운 장치의 감시 기록을 지운다."""
        self._watches.pop(device_id, None)

    # --- 화면 조회 ---
    def event_snapshot(self, device_id: Optional[str] = None) -> Optional[Dict]:
        """device_id 장치의 진행 중인 이벤트. device_id가 없으면 진행 중인 이벤트가 하나뿐일 때 그것."""
        if device_id is None:
            if len(self.cases) != 1:
                return None
            device_id = next(iter(self.cases))
        case = self.cases.get(device_id)
        if case is None:
            return None
        row = self.store.get_event(case.event_id) or {}  # 기록이 없어도(직접 지움 등) 화면은 그린다
        return {
            "id": case.event_id,
            "device_id": case.device_id,
            "started_at": row.get("started_at"),
            "state": case.state,
            "result": row.get("result"),
            "notify_state": row.get("notify_state"),
            "voice_attempt": case.voice_attempt,
            "voice_attempts": self.s.voice_max_attempts,
            "phase": case.phase,
        }
