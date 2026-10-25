"""낙상 이벤트 관리: 낙상 의심 -> 음성 확인 -> 해제 또는 경보 -> 종료.

    FSM SUSPECT --> 이벤트 생성(result NULL, '음성 확인 중')
                    |-- 음성 '괜찮다' ----------------> CLEARED_VOICE (종료)
                    |-- 화면 버튼 -------------------> CLEARED_BUTTON (종료)
                    |-- 도움 요청 / 무응답 / 장치 오류 --> ALERT_* (경보, ended_at 비어 있음)
                                                         |-- 버튼(본인·보호자) --> ended_at, cleared_by (종료)

- 기기마다 진행 중인 이벤트는 하나뿐이다. 같은 낙상으로 이벤트가 여러 번 생기지 않게 한다.
- 음성 확인의 각 단계는 기다린 뒤마다 '이벤트가 아직 음성 확인 중인지' 다시 확인한다.
  버튼으로 이미 해제했는데 늦게 온 녹음이 경보를 다시 울리면 안 되기 때문이다.
- 확인할 수 없는 경우(녹음 실패, 인식 실패, 기기 연결 끊김)는 경보 쪽으로 처리한다.
  놓친 낙상이 잘못 울린 경보보다 위험하기 때문이다. 연결 끊김만은 화면 새로 고침처럼 곧 다시
  접속하는 경우가 많아 settings.device_reconnect_grace_seconds만큼 기다린 뒤 판단한다.
- SUSPECT(음성 확인 중)는 웹 푸시로 보내지 않고 열린 화면에만 보여 준다. 대부분 음성으로 바로
  해제되므로 보호자를 매번 깨우지 않기 위함이다. 웹 푸시는 경보(ALARM)와 경보 해제만 보낸다.
"""

import asyncio
import logging
import sqlite3
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from server import voice
from server.config import Settings
from server.db import (
    ALERT_HELP,
    ALERT_NO_REPLY,
    ALERT_RESULTS,
    ALERT_VOICE_FAIL,
    CLEARED_BUTTON,
    CLEARED_VOICE,
    INTERRUPTED,
    NOTIFY_SENT,
    Database,
    dumps,
    now_iso,
)
from server.devices import BackgroundTasks, watcher_ids
from server.notify import KIND_ALERT, KIND_CLEAR, event_public_dict

log = logging.getLogger("server.events")

STATE_SUSPECT = "SUSPECT"
STATE_ALARM = "ALARM"
STATE_ENDED = "ENDED"

# 음성 분류 결과 -> 경보 결과
ALARM_FOR_VOICE = {
    voice.HELP: ALERT_HELP,
    voice.DEVICE_FAIL: ALERT_VOICE_FAIL,
    voice.NO_REPLY: ALERT_NO_REPLY,
    voice.UNCLEAR: ALERT_NO_REPLY,
}


@dataclass
class VoiceUpload:
    audio: bytes
    filename: str
    error: Optional[str] = None


# 음성 확인을 기다리는 중에 기다림을 끝내는 신호들
DEVICE_DISCONNECTED = "device_disconnected"
EVENT_CLEARED = "event_cleared"


@dataclass
class ActiveEvent:
    """메모리에 있는 진행 중 이벤트. DB 행과 같은 id를 쓴다."""

    id: int
    device_id: int
    owner_id: int
    state: str                                  # SUSPECT | ALARM | ENDED
    waiter: Optional[asyncio.Future] = None     # 녹음 업로드를 기다리는 중이면 채워져 있다
    waiting_attempt: Optional[int] = None


class EventService:
    def __init__(self, db: Database, accounts: Any, devices: Any, guardian_hub: Any, notifier: Any,
                 transcriber: voice.Transcriber, settings: Settings):
        self.db = db
        self.accounts = accounts
        self.devices = devices
        self.guardian_hub = guardian_hub
        self.notifier = notifier
        self.transcriber = transcriber
        self.settings = settings
        self._active: Dict[int, ActiveEvent] = {}   # device_id -> 진행 중 이벤트
        self.background = BackgroundTasks("events")

    # --- 시작·종료 ---------------------------------------------------------

    def recover(self) -> None:
        """서버 시작 시 지난 실행에서 끝나지 않은 이벤트를 정리한다.

        - 음성 확인 중이던 이벤트: 이어서 확인할 방법이 없으므로 '중단'으로 끝낸다.
        - 경보 중이던 이벤트: 아직 아무도 해제하지 않았으므로 그대로 둔다. 다시 연결한 보호자 화면이
          snapshot으로 받아 해제할 수 있게 메모리에 다시 올린다.
        """
        cur = self.db.execute(
            "UPDATE events SET result = ?, ended_at = ? WHERE result IS NULL AND ended_at IS NULL",
            (INTERRUPTED, now_iso()),
        )
        if cur.rowcount:
            log.warning("서버 재시작: 음성 확인 중이던 이벤트 %d건을 '중단'으로 기록", cur.rowcount)
        marks = ",".join("?" for _ in ALERT_RESULTS)
        rows = self.db.query(
            f"SELECT e.*, d.owner_id FROM events e JOIN devices d ON d.id = e.device_id "
            f"WHERE e.result IN ({marks}) AND e.ended_at IS NULL ORDER BY e.id",
            tuple(ALERT_RESULTS),
        )
        for row in rows:
            # 정상이라면 기기당 하나뿐이다. 혹시 여럿이면 가장 최근 것을 남긴다(id 오름차순이라 덮어씀).
            self._active[row["device_id"]] = ActiveEvent(row["id"], row["device_id"], row["owner_id"], STATE_ALARM)
        if rows:
            log.warning("서버 재시작: 경보 중인 이벤트 %d건을 다시 불러옴", len(rows))

    async def resend_undelivered_alarms(self) -> None:
        """recover() 뒤에 부른다. 재시작 전에 보호자에게 전달하지 못한 경보를 다시 보낸다.

        푸시 재전송은 메모리 작업이라 재시작하면 사라지고, 경보로 바꾼 직후 서버가 죽었으면
        알림을 한 번도 보내지 못했을 수 있다. 그대로 두면 보호자 화면을 열기 전까지 아무도 모른다.
        이미 전달된(화면 또는 푸시로 'sent' 기록이 있는) 보호자에게는 다시 보내지 않는다.
        """
        for ev in sorted(self._active.values(), key=lambda e: e.id):
            if ev.state != STATE_ALARM:
                continue
            delivered = {r["guardian_id"] for r in self.db.query(
                "SELECT DISTINCT guardian_id FROM notifications WHERE event_id = ? AND kind = ? AND state = ?",
                (ev.id, KIND_ALERT, NOTIFY_SENT),
            )}
            missing = [g for g in self.accounts.guardian_ids_for(ev.owner_id) if g not in delivered]
            if missing:
                log.warning("이벤트 %s: 재시작 전에 전달하지 못한 경보를 보호자 %s에게 다시 보냄", ev.id, missing)
                await self._notify(ev, KIND_ALERT, guardian_ids=missing, delayed=True)

    async def aclose(self) -> None:
        """서버 종료: 음성 확인 작업을 멈춘다. 남은 '음성 확인 중' 이벤트는 다음 시작 때 recover()가 정리한다."""
        await self.background.cancel_all()

    # --- 조회 -------------------------------------------------------------

    def state_for_device(self, device_id: int) -> Optional[str]:
        ev = self._active.get(device_id)
        return ev.state if ev is not None else None

    def active_for_device(self, device_id: int) -> Optional[ActiveEvent]:
        return self._active.get(device_id)

    def _find_active(self, event_id: int) -> Optional[ActiveEvent]:
        for ev in self._active.values():
            if ev.id == event_id:
                return ev
        return None

    def get_row(self, event_id: int) -> Optional[Dict[str, Any]]:
        return self.db.query_one("SELECT * FROM events WHERE id = ?", (event_id,))

    def public(self, row: Dict[str, Any]) -> Dict[str, Any]:
        device = self.accounts.get_device(row["device_id"])
        owner = self.accounts.get_user(device["owner_id"]) if device else None
        return event_public_dict(row, device, owner)

    def can_view(self, user_id: int, owner_id: int) -> bool:
        return owner_id in self.accounts.visible_owner_ids(user_id)

    def active_events_visible_to(self, user: Dict[str, Any]) -> List[Dict[str, Any]]:
        visible = set(self.accounts.visible_owner_ids(user["id"]))
        result = []
        for ev in sorted(self._active.values(), key=lambda e: e.id):
            if ev.owner_id in visible:
                row = self.get_row(ev.id)
                if row is not None:
                    result.append(self.public(row))
        return result

    def recent_events_visible_to(self, user: Dict[str, Any], limit: int = 50) -> List[Dict[str, Any]]:
        visible = self.accounts.visible_owner_ids(user["id"])
        marks = ",".join("?" for _ in visible)
        rows = self.db.query(
            f"SELECT e.* FROM events e JOIN devices d ON d.id = e.device_id "
            f"WHERE d.owner_id IN ({marks}) ORDER BY e.id DESC LIMIT ?",
            tuple(visible) + (int(limit),),
        )
        return [self.public(row) for row in rows]

    def pending_voice_check(self, device_id: int) -> Optional[Dict[str, Any]]:
        """음성 확인을 기다리는 중이면 그 요청 메시지. 기기가 다시 접속했을 때 다시 보내 준다."""
        ev = self._active.get(device_id)
        if ev is None or ev.state != STATE_SUSPECT or ev.waiter is None or ev.waiter.done():
            return None
        return {"type": "voice_check", "event_id": ev.id, "attempt": ev.waiting_attempt}

    # --- 보내기 도우미 ----------------------------------------------------------

    async def _broadcast_event(self, ev: ActiveEvent, user_ids: List[int]) -> None:
        row = self.get_row(ev.id)
        if row is None:
            return
        await self.guardian_hub.send_to_users(user_ids, {"type": "event", "event": self.public(row)})

    async def _send_state(self, ev: ActiveEvent, state: str) -> None:
        await self.devices.send(ev.device_id, {"type": "state", "state": state, "event_id": ev.id})

    # --- 낙상 의심 -----------------------------------------------------------

    async def on_suspect(self, session: Any, features: Dict[str, float]) -> Optional[int]:
        """FSM이 SUSPECT가 되었을 때 DeviceHub가 부른다. 만든 이벤트 id(무시했으면 None)."""
        device = session.device
        if device["id"] in self._active:
            log.info("기기 %s: 진행 중인 이벤트가 있어 새 낙상 의심은 무시", device["id"])
            return None
        try:
            cur = self.db.execute(
                "INSERT INTO events (device_id, started_at, feature_snapshot) VALUES (?, ?, ?)",
                (device["id"], now_iso(), dumps(features)),
            )
        except sqlite3.IntegrityError:
            log.warning("기기 %s: 삭제된 기기라 이벤트를 만들지 않음", device["id"])
            return None
        ev = ActiveEvent(cur.lastrowid, device["id"], device["owner_id"], STATE_SUSPECT)
        self._active[device["id"]] = ev
        log.info("이벤트 %s 생성: 기기 %s 낙상 의심 %s", ev.id, device["id"], features)
        self.background.spawn(self._voice_flow(ev))
        return ev.id

    def _still_suspect(self, ev: ActiveEvent) -> bool:
        return self._active.get(ev.device_id) is ev and ev.state == STATE_SUSPECT

    def _still_alarm(self, ev: ActiveEvent) -> bool:
        return self._active.get(ev.device_id) is ev and ev.state == STATE_ALARM

    async def _voice_flow(self, ev: ActiveEvent) -> None:
        try:
            # 기기 화면이 먼저 바뀌어야 사용자가 안내 음성을 이해한다. 보호자 화면 전송은 느린 화면
            # 하나 때문에 음성 확인이 늦어지지 않도록 따로 돌린다.
            await self._send_state(ev, STATE_SUSPECT)
            self.background.spawn(self._broadcast_event(ev, watcher_ids(self.accounts, ev.owner_id)))
            self.background.spawn(self.devices.broadcast_status(ev.device_id))

            last_result, last_text = voice.NO_REPLY, None
            for attempt in range(1, max(1, self.settings.voice_max_attempts) + 1):
                if not self._still_suspect(ev):
                    return
                last_result, last_text = await self._ask_once(ev, attempt)
                if not self._still_suspect(ev):
                    log.info("이벤트 %s: 확인하는 사이 해제되어 음성 결과(%s)를 버림", ev.id, last_result)
                    return
                log.info("이벤트 %s: 음성 확인 %d회차 결과 %s", ev.id, attempt, last_result)
                if last_result == voice.SAFE:
                    await self._finish(ev, CLEARED_VOICE, voice_result=last_result, text=last_text)
                    return
                if last_result in (voice.HELP, voice.DEVICE_FAIL):
                    await self._raise_alarm(ev, ALARM_FOR_VOICE[last_result], last_result, last_text)
                    return
                # 무응답·불분명은 한 번 더 묻는다.
            await self._raise_alarm(ev, ALERT_NO_REPLY, last_result, last_text)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("이벤트 %s 음성 확인 중 오류", ev.id)
            if self._still_suspect(ev):
                await self._raise_alarm(ev, ALERT_VOICE_FAIL, voice.DEVICE_FAIL, None)

    async def _ask_once(self, ev: ActiveEvent, attempt: int) -> Tuple[str, Optional[str]]:
        """음성 확인을 한 번 요청하고 (분류 결과, 인식 문장)을 돌려준다."""
        waiter = asyncio.get_running_loop().create_future()
        ev.waiter, ev.waiting_attempt = waiter, attempt
        try:
            sent = await self.devices.send(ev.device_id, {"type": "voice_check", "event_id": ev.id, "attempt": attempt})
            if not sent:
                # 지금 연결이 없어도 잠시 뒤 다시 접속하면 그때 요청을 다시 보낸다. 끝내 돌아오지 않으면
                # _fail_if_still_offline이 기다림을 깨워 '확인 불가'로 처리한다.
                self.background.spawn(self._fail_if_still_offline(ev, waiter))
            try:
                upload = await asyncio.wait_for(waiter, timeout=self.settings.voice_reply_timeout_seconds)
            except asyncio.TimeoutError:
                return voice.NO_REPLY, None
        finally:
            ev.waiter, ev.waiting_attempt = None, None

        if not isinstance(upload, VoiceUpload):
            if upload == DEVICE_DISCONNECTED:
                return voice.DEVICE_FAIL, None
            return voice.NO_REPLY, None  # EVENT_CLEARED: 호출 쪽이 _still_suspect로 걸러 낸다
        if upload.error:
            log.info("이벤트 %s: 기기가 녹음 실패를 알림 (%s)", ev.id, upload.error)
            return voice.DEVICE_FAIL, None
        try:
            text = await asyncio.to_thread(self.transcriber.transcribe, upload.audio, upload.filename)
        except Exception as e:
            log.warning("이벤트 %s: 음성 인식 실패 (%s)", ev.id, e)
            return voice.DEVICE_FAIL, None
        return voice.classify_with_settings(text, self.settings), text

    def _wake(self, ev: ActiveEvent, value: Any) -> None:
        if ev.waiter is not None and not ev.waiter.done():
            ev.waiter.set_result(value)

    def submit_voice(self, event_id: int, device: Dict[str, Any], audio_bytes: bytes, filename: str,
                     error: Optional[str] = None, attempt: Optional[int] = None) -> bool:
        """기기가 올린 녹음을 기다리던 음성 확인에 넘긴다. 기다리는 확인이 없으면 무시하고 False."""
        ev = self._find_active(event_id)
        if ev is None or ev.device_id != device["id"] or not self._still_suspect(ev):
            return False
        if ev.waiter is None or ev.waiter.done():
            return False
        if attempt is not None and attempt != ev.waiting_attempt:
            return False  # 앞 회차의 녹음이 늦게 도착
        ev.waiter.set_result(VoiceUpload(audio_bytes, filename or "", error or None))
        return True

    def on_device_disconnected(self, device_id: int) -> None:
        """음성 확인을 기다리는 중에 기기 연결이 끊겼다.

        화면 새로 고침이나 짧은 Wi-Fi 끊김은 이전 연결이 먼저 닫히고 곧 다시 접속한다.
        바로 '확인 불가'로 경보를 울리지 않고 잠시 기다려, 다시 접속하면 음성 확인 요청을
        다시 보내 응답을 받는다(camera_socket의 pending_voice_check).
        """
        ev = self._active.get(device_id)
        if ev is not None and ev.state == STATE_SUSPECT and ev.waiter is not None and not ev.waiter.done():
            self.background.spawn(self._fail_if_still_offline(ev, ev.waiter))

    async def _fail_if_still_offline(self, ev: ActiveEvent, waiter: asyncio.Future) -> None:
        await asyncio.sleep(self.settings.device_reconnect_grace_seconds)
        if not waiter.done() and not self.devices.is_online(ev.device_id):
            waiter.set_result(DEVICE_DISCONNECTED)

    def forget_device(self, device_id: int) -> None:
        """기기가 삭제되었다. 진행 중 이벤트(DB 행은 함께 지워짐)를 메모리에서도 지운다.

        남겨 두면 지울 수 없는 경보가 현장 화면에 계속 보이고, 음성 확인 작업도 계속 기다린다.
        """
        ev = self._active.pop(device_id, None)
        if ev is None:
            return
        ev.state = STATE_ENDED
        self._wake(ev, EVENT_CLEARED)
        log.info("이벤트 %s: 기기 %s가 삭제되어 진행을 멈춤", ev.id, device_id)

    # --- 끝내기·경보 -----------------------------------------------------------

    async def _finish(self, ev: ActiveEvent, result: str, voice_result: Optional[str] = None,
                      text: Optional[str] = None, cleared_by: Optional[str] = None) -> None:
        """경보 전에 끝난 이벤트(음성 해제, 버튼 해제). 보호자에게 푸시는 보내지 않는다."""
        # 기다림 없이 먼저 메모리 상태를 바꿔, 다른 작업이 같은 이벤트를 두 번 끝내지 못하게 한다.
        ev.state = STATE_ENDED
        if self._active.get(ev.device_id) is ev:
            del self._active[ev.device_id]
        self._wake(ev, EVENT_CLEARED)
        self.db.execute(
            "UPDATE events SET result = ?, voice_result = COALESCE(?, voice_result), voice_text = ?, "
            "ended_at = ?, cleared_by = ? WHERE id = ?",
            (result, voice_result, text if self.settings.store_voice_text else None, now_iso(), cleared_by, ev.id),
        )
        log.info("이벤트 %s 종료: %s", ev.id, result)
        self.devices.reset_detection(ev.device_id)
        await self._send_state(ev, "NORMAL")
        await self._broadcast_event(ev, watcher_ids(self.accounts, ev.owner_id))
        await self.devices.broadcast_status(ev.device_id)

    async def _raise_alarm(self, ev: ActiveEvent, result: str, voice_result: Optional[str],
                           text: Optional[str]) -> None:
        ev.state = STATE_ALARM
        self.db.execute(
            "UPDATE events SET result = ?, voice_result = ?, voice_text = ? WHERE id = ?",
            (result, voice_result, text if self.settings.store_voice_text else None, ev.id),
        )
        log.warning("이벤트 %s 경보: %s", ev.id, result)
        await self._send_state(ev, STATE_ALARM)
        # 보호자에게는 Notifier가 WebSocket과 푸시로 보내므로, 여기서는 주인 화면에만 보낸다(중복 방지).
        await self._broadcast_event(ev, [ev.owner_id])
        await self.devices.broadcast_status(ev.device_id)
        # 위 전송을 기다리는 사이 버튼으로 해제되었을 수 있다. 그때 경보 알림을 보내면 해제 알림보다
        # 늦게 도착해, 보호자 휴대폰에 이미 끝난 경보가 남는다.
        if not self._still_alarm(ev):
            log.info("이벤트 %s: 경보를 알리기 전에 해제되어 보호자 알림을 보내지 않음", ev.id)
            return
        await self._notify(ev, KIND_ALERT)

    async def _notify(self, ev: ActiveEvent, kind: str, guardian_ids: Optional[List[int]] = None,
                      delayed: bool = False) -> None:
        row = self.get_row(ev.id)
        if row is None:
            return
        if guardian_ids is None:
            guardian_ids = self.accounts.guardian_ids_for(ev.owner_id)
        try:
            await self.notifier.notify(row, self.accounts.get_user(ev.owner_id), guardian_ids, kind,
                                       delayed=delayed)
        except Exception:
            log.exception("이벤트 %s 보호자 알림(%s) 실패", ev.id, kind)

    # --- 해제 -------------------------------------------------------------

    def _check_clear_permission(self, row: Dict[str, Any], device: Optional[Dict[str, Any]],
                                actor: Tuple[str, Dict[str, Any]]) -> str:
        """해제할 수 있으면 cleared_by 값을, 아니면 PermissionError."""
        kind, who = actor
        if kind == "device":
            if who["id"] != row["device_id"]:
                raise PermissionError("이 기기의 이벤트가 아닙니다.")
            return f"device:{who['id']}"
        if kind == "user":
            if device is None or not self.can_view(who["id"], device["owner_id"]):
                raise PermissionError("본인이나 연결된 보호자만 해제할 수 있습니다.")
            return f"user:{who['id']}"
        raise PermissionError("알 수 없는 요청자입니다.")

    async def clear(self, event_id: int, actor: Tuple[str, Dict[str, Any]]) -> bool:
        """'괜찮아요' 버튼 또는 보호자의 경보 종료. 이미 끝난 이벤트면 False.

        actor: ('user', 사용자) 또는 ('device', 기기). 없는 이벤트는 LookupError, 권한이 없으면 PermissionError.
        """
        row = self.get_row(event_id)
        if row is None:
            raise LookupError("이벤트를 찾을 수 없습니다.")
        device = self.accounts.get_device(row["device_id"])
        cleared_by = self._check_clear_permission(row, device, actor)
        ev = self._find_active(event_id)
        if ev is None or ev.state == STATE_ENDED:
            return False

        if ev.state == STATE_SUSPECT:
            # 음성 확인 중 해제: 기다리던 녹음은 버린다(_finish가 기다림을 깨운다).
            await self._finish(ev, CLEARED_BUTTON, cleared_by=cleared_by)
            return True

        # 경보 해제
        ev.state = STATE_ENDED
        del self._active[ev.device_id]
        self.db.execute("UPDATE events SET ended_at = ?, cleared_by = ? WHERE id = ?", (now_iso(), cleared_by, ev.id))
        log.info("이벤트 %s 경보 해제 (%s)", ev.id, cleared_by)
        self.devices.reset_detection(ev.device_id)
        await self._send_state(ev, "NORMAL")
        await self._broadcast_event(ev, [ev.owner_id])
        await self.devices.broadcast_status(ev.device_id)
        # 푸시 전송은 몇 초 걸릴 수 있어 버튼 응답을 붙잡지 않도록 따로 돌린다.
        self.background.spawn(self._notify(ev, KIND_CLEAR))
        return True
