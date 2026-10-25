"""보호자에게 경보 전달: 열린 보호자 화면(WebSocket)과 웹 푸시.

- 열려 있는 보호자 화면에는 WebSocket으로 바로 보낸다.
- 브라우저를 닫은 보호자 휴대폰에는 웹 푸시로 보낸다. 푸시 내용에는 영상·음성을 넣지 않고
  사용자 이름·시각·판정만 담는다(docs/ARCHITECTURE.md '데이터와 개인정보').
- 전송 결과는 보호자·채널별로 notifications 테이블에 남긴다. 전송한 것처럼 꾸미지 않고
  실패도 그대로 기록해 보호자 화면·마이페이지에서 '알림 실패'를 보여 줄 수 있게 한다.
- 푸시 실패는 settings.push_retry_delays 간격으로 다시 보낸다. 그 사이 경보가 해제되었으면
  보호자를 놀라게 하지 않도록 경보 재전송은 하지 않는다.
"""

import asyncio
import logging
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Set

from pywebpush import WebPushException, webpush

from server.config import Settings
from server.db import (
    ALERT_HELP,
    ALERT_NO_REPLY,
    ALERT_RESULTS,
    ALERT_VOICE_FAIL,
    CLEARED_BUTTON,
    CLEARED_VOICE,
    GUARDIAN_ACCEPTED,
    INTERRUPTED,
    NOTIFY_FAILED,
    NOTIFY_SENT,
    Database,
    dumps,
    now_iso,
)

log = logging.getLogger("server.notify")

RESULT_LABELS = {
    ALERT_HELP: "음성으로 도움 요청",
    ALERT_NO_REPLY: "음성 확인에 응답 없음",
    ALERT_VOICE_FAIL: "음성 확인 불가(장치 오류)",
    CLEARED_VOICE: "음성으로 괜찮다고 응답",
    CLEARED_BUTTON: "화면 버튼으로 해제",
    INTERRUPTED: "서버 재시작으로 중단",
}

KIND_ALERT = "alert"
KIND_CLEAR = "clear"
CHANNEL_WEBSOCKET = "websocket"
CHANNEL_PUSH = "push"

# 휴대폰이 잠시 꺼져 있어도 푸시 서비스가 이 시간 동안 보관했다가 전달한다.
PUSH_TTL_SECONDS = 3600
# 안드로이드 절전(Doze) 중에도 바로 깨워 표시하도록 긴급도를 높인다.
PUSH_HEADERS = {"Urgency": "high"}
# 응답이 멈춘 보호자 화면 하나 때문에 다른 보호자에게 가는 경보가 늦어지지 않게 한다.
WEBSOCKET_SEND_TIMEOUT = 5.0
# 서버가 보호자 화면 연결을 닫을 때 쓰는 코드. 1011이면 화면이 다시 접속해 snapshot을 새로 받고,
# 4401(로그인 필요)이면 다시 접속하지 않고 로그인 화면으로 보낸다(routes_realtime 머리말).
CLOSE_SEND_FAILED = 1011
CLOSE_SESSION_ENDED = 4401


# ---------------------------------------------------------------------------
# 이벤트를 화면·알림용으로 바꾸기
# ---------------------------------------------------------------------------

def event_state(event_row: Dict[str, Any]) -> str:
    """result가 비어 있으면 음성 확인 중, 경보 결과인데 끝나지 않았으면 경보 중, 나머지는 종료."""
    result = event_row.get("result")
    if result is None and event_row.get("ended_at") is None:
        return "SUSPECT"
    if result in ALERT_RESULTS and event_row.get("ended_at") is None:
        return "ALARM"
    return "ENDED"


def event_public_dict(event_row: Dict[str, Any], device_row: Optional[Dict[str, Any]],
                      owner_row: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """웹(보호자 화면·현장 화면)에 보내는 이벤트 모양. 음성 인식 문장과 특징값은 빼고 보낸다."""
    owner_id = None
    if device_row is not None:
        owner_id = device_row.get("owner_id")
    elif owner_row is not None:
        owner_id = owner_row.get("id")
    result = event_row.get("result")
    return {
        "id": event_row["id"],
        "device_id": event_row["device_id"],
        "device_name": device_row.get("name") if device_row else None,
        "owner_id": owner_id,
        "owner_name": owner_row.get("name") if owner_row else None,
        "started_at": event_row.get("started_at"),
        "ended_at": event_row.get("ended_at"),
        "result": result,
        "result_label": RESULT_LABELS.get(result) if result else None,
        "voice_result": event_row.get("voice_result"),
        "cleared_by": event_row.get("cleared_by"),
        "state": event_state(event_row),
    }


def format_time(iso: Optional[str]) -> str:
    """'2026-10-25T14:03:12.345' -> '10월 25일 14:03'. 형식이 다르면 원문을 그대로 쓴다."""
    if not iso:
        return ""
    try:
        t = datetime.fromisoformat(iso)
    except ValueError:
        return iso
    return f"{t.month}월 {t.day}일 {t.hour:02d}:{t.minute:02d}"


# ---------------------------------------------------------------------------
# 열린 보호자 화면(WebSocket)
# ---------------------------------------------------------------------------

class GuardianHub:
    """사용자별로 열려 있는 보호자 화면 WebSocket을 모아 둔다.

    같은 보호자가 휴대폰과 PC에서 동시에 열 수 있으므로 사용자 하나에 소켓 여러 개를 둔다.
    이벤트 루프 한 곳에서만 쓰므로 잠금은 필요 없다.
    """

    def __init__(self, send_timeout: float = WEBSOCKET_SEND_TIMEOUT):
        self._sockets: Dict[int, Set[Any]] = {}
        # 소켓을 연 로그인 세션 토큰. 로그아웃하면 그 세션으로 연 화면만 닫기 위해 둔다.
        self._session_of: Dict[Any, str] = {}
        self._closing: Set[asyncio.Task] = set()
        self.send_timeout = send_timeout

    def register(self, user_id: int, ws: Any, session_token: Optional[str] = None) -> None:
        self._sockets.setdefault(user_id, set()).add(ws)
        if session_token:
            self._session_of[ws] = session_token

    def unregister(self, user_id: int, ws: Any) -> None:
        self._session_of.pop(ws, None)
        sockets = self._sockets.get(user_id)
        if not sockets:
            return
        sockets.discard(ws)
        if not sockets:
            del self._sockets[user_id]

    def _drop(self, user_id: int, ws: Any, code: int) -> None:
        """목록에서 빼고 연결도 닫는다.

        빼기만 하면 화면은 연결된 것처럼 보이면서 더는 아무 메시지도 받지 못한다. 닫아야 화면이
        끊김을 알고 다시 접속하거나(새 snapshot) 로그인 화면으로 간다. 닫기도 멈출 수 있으므로
        기다리지 않고 따로 돌린다.
        """
        self.unregister(user_id, ws)

        async def close() -> None:
            try:
                await asyncio.wait_for(ws.close(code=code), timeout=self.send_timeout)
            except Exception:
                pass  # 이미 끊긴 연결

        task = asyncio.ensure_future(close())
        self._closing.add(task)
        task.add_done_callback(self._closing.discard)

    async def close_session(self, session_token: Optional[str]) -> None:
        """로그아웃한 세션으로 열린 보호자 화면을 닫는다. 같은 사용자의 다른 기기 화면은 그대로 둔다."""
        if not session_token:
            return
        for user_id, sockets in list(self._sockets.items()):
            for ws in list(sockets):
                if self._session_of.get(ws) == session_token:
                    log.info("로그아웃한 세션의 보호자 화면을 닫음 (user=%s)", user_id)
                    self._drop(user_id, ws, CLOSE_SESSION_ENDED)
        await self.wait_closed()

    async def wait_closed(self) -> None:
        """닫는 중인 연결이 모두 닫힐 때까지 기다린다."""
        while self._closing:
            await asyncio.gather(*list(self._closing), return_exceptions=True)

    def connected_user_ids(self) -> List[int]:
        return list(self._sockets)

    async def send_to_users(self, user_ids: Iterable[int], message: Dict[str, Any]) -> List[int]:
        """메시지를 보내고, 소켓 하나라도 받은 사용자 id 목록을 돌려준다.

        보내다 오류가 난 소켓은 이미 끊긴 것으로 보고 목록에서 뺀다.
        """
        delivered: List[int] = []
        for user_id in dict.fromkeys(user_ids):  # 중복 제거, 순서 유지
            ok = False
            # 보내는 중에 register/unregister가 끼어들 수 있으므로 복사본으로 돈다.
            for ws in list(self._sockets.get(user_id, ())):
                try:
                    await asyncio.wait_for(ws.send_json(message), timeout=self.send_timeout)
                    ok = True
                except Exception as e:
                    log.info("보호자 화면 전송 실패, 연결 닫음 (user=%s): %s", user_id, type(e).__name__)
                    self._drop(user_id, ws, CLOSE_SEND_FAILED)
            if ok:
                delivered.append(user_id)
        return delivered


# ---------------------------------------------------------------------------
# 웹 푸시
# ---------------------------------------------------------------------------

class PushSender:
    """VAPID 웹 푸시 전송. 서버에 VAPID 키가 없으면 enabled=False이고 아무것도 보내지 않는다."""

    def __init__(self, settings: Settings, accounts: Any, timeout: float = 10.0):
        self.settings = settings
        self.accounts = accounts
        self.timeout = timeout
        self.enabled = settings.push_enabled
        if not self.enabled:
            log.warning("VAPID 키가 설정되지 않아 웹 푸시를 보내지 않습니다(보호자 화면 알림만 동작)")

    def subscribed_user_ids(self, user_ids: Iterable[int]) -> List[int]:
        """푸시 구독이 하나라도 있는 사용자. 구독이 없는 사람은 재시도해도 소용이 없어 구분한다."""
        subs = self.accounts.push_subscriptions_for(list(user_ids))
        return list(dict.fromkeys(s["user_id"] for s in subs))

    def _send_one(self, sub: Dict[str, Any], payload: Dict[str, Any]) -> bool:
        """구독 하나에 보낸다(막히는 HTTP 호출, 스레드에서 실행). 성공하면 True."""
        try:
            webpush(
                subscription_info={
                    "endpoint": sub["endpoint"],
                    "keys": {"p256dh": sub["p256dh"], "auth": sub["auth"]},
                },
                data=dumps(payload),
                vapid_private_key=self.settings.vapid_private_key,
                # pywebpush가 이 dict에 aud/exp를 채워 넣으므로 호출마다 새로 만든다.
                vapid_claims={"sub": self.settings.vapid_subject},
                ttl=PUSH_TTL_SECONDS,
                headers=dict(PUSH_HEADERS),
                timeout=self.timeout,
            )
            return True
        except WebPushException as e:
            status = getattr(e.response, "status_code", None)
            if status in (404, 410):
                # 구독이 만료되었거나 사용자가 알림 권한을 끈 경우다. 다시 보내도 소용없으므로 지우고,
                # 마이페이지에서 다시 켜도록 안내한다.
                log.info("만료된 푸시 구독 삭제 (user=%s, 응답 %s)", sub.get("user_id"), status)
                self.accounts.delete_push_subscription(sub["endpoint"])
            else:
                log.warning("웹 푸시 실패 (user=%s, 응답 %s)", sub.get("user_id"), status)
            return False
        except Exception as e:
            # 인터넷 끊김 등. 구독은 그대로 두고 나중에 다시 보낸다.
            log.warning("웹 푸시 실패 (user=%s): %s", sub.get("user_id"), type(e).__name__)
            return False

    async def send_to_users(self, user_ids: Iterable[int], payload: Dict[str, Any]) -> Dict[int, bool]:
        """사용자별로 구독 하나라도 전달되면 True. 구독이 없거나 꺼져 있으면 False."""
        user_ids = list(dict.fromkeys(user_ids))
        result = {uid: False for uid in user_ids}
        if not self.enabled or not user_ids:
            return result
        subs = self.accounts.push_subscriptions_for(user_ids)
        oks = await asyncio.gather(*(asyncio.to_thread(self._send_one, s, payload) for s in subs))
        for sub, ok in zip(subs, oks):
            if ok:
                result[sub["user_id"]] = True
        return result


# ---------------------------------------------------------------------------
# 경보 전달과 기록
# ---------------------------------------------------------------------------

class Notifier:
    """이벤트 하나를 보호자들에게 알리고 결과를 기록한다."""

    def __init__(self, db: Database, hub: GuardianHub, push: PushSender, settings: Settings):
        self.db = db
        self.hub = hub
        self.push = push
        self.settings = settings
        self._retry_tasks: Set[asyncio.Task] = set()
        # 이벤트별 전송 잠금. 경보 알림을 보내는 도중 해제되면 해제 알림이 경보 알림보다 먼저 도착해
        # 보호자 휴대폰에 끝난 경보가 남을 수 있다(같은 tag라 나중 것이 덮어씀). 한 이벤트의 알림은
        # 차례로 보낸다.
        self._event_locks: Dict[int, asyncio.Lock] = {}

    def _lock_for(self, event_id: int) -> asyncio.Lock:
        return self._event_locks.setdefault(event_id, asyncio.Lock())

    def _current_guardians(self, owner_id: Optional[int], guardian_ids: List[int]) -> List[int]:
        """그사이 연결이 해제된 보호자를 뺀다. 권한을 잃은 사람에게 늦은 알림이 가지 않게 한다."""
        rows = self.db.query("SELECT guardian_id FROM guardians WHERE user_id = ? AND status = ?",
                             (owner_id, GUARDIAN_ACCEPTED))
        linked = {r["guardian_id"] for r in rows}
        return [g for g in guardian_ids if g in linked]

    # --- 메시지 만들기 ---

    def _cleared_by_text(self, cleared_by: Optional[str], owner_id: Optional[int]) -> str:
        if not cleared_by:
            return "해제"
        who, _, raw_id = cleared_by.partition(":")
        if who == "device":
            return "현장 화면에서 해제"
        if who == "user":
            if owner_id is not None and raw_id == str(owner_id):
                return "본인이 해제"
            row = self.db.query_one("SELECT name FROM users WHERE id = ?", (raw_id,))
            if row:
                return f"{row['name']}님이 해제"
        return "해제"

    def build_push_payload(self, event: Dict[str, Any], kind: str, delayed: bool = False) -> Dict[str, Any]:
        """푸시 알림 내용. event는 event_public_dict()의 결과다."""
        name = event.get("owner_name") or "사용자"
        when = format_time(event.get("started_at"))
        if kind == KIND_ALERT:
            title = f"[낙상 경보] {name}님"
            label = event.get("result_label") or "낙상 의심"
            body = f"{when} {label}. 현장 상태를 확인해 주세요."
            if delayed:
                title += " (지연 전송)"
                body += " 통신 장애로 늦게 전달된 알림입니다."
        else:
            title = f"[경보 해제] {name}님"
            how = self._cleared_by_text(event.get("cleared_by"), event.get("owner_id"))
            body = f"{when}에 발생한 경보가 끝났습니다({how})."
        return {
            "title": title,
            "body": body,
            "url": "/guardian",
            "event_id": event["id"],
            "kind": kind,
            # 같은 이벤트의 해제 알림이 경보 알림을 덮어쓰도록 서비스 워커가 tag로 쓴다.
            "tag": f"event-{event['id']}",
        }

    # --- 기록 ---

    def _record(self, event_id: int, guardian_id: int, kind: str, channel: str, ok: bool,
                detail: Optional[str]) -> None:
        self.db.execute(
            "INSERT INTO notifications (event_id, guardian_id, kind, channel, state, detail, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (event_id, guardian_id, kind, channel, NOTIFY_SENT if ok else NOTIFY_FAILED, detail, now_iso()),
        )

    # --- 전달 ---

    async def notify(self, event_row: Dict[str, Any], owner: Optional[Dict[str, Any]],
                     guardian_ids: Iterable[int], kind: str, delayed: bool = False) -> Dict[int, Dict[str, bool]]:
        """보호자들에게 WebSocket·웹 푸시로 알리고 {보호자 id: {'websocket': bool, 'push': bool}}를 돌려준다.

        WebSocket 메시지({"type": "event", ...})도 여기서 guardian_ids에게 보낸다.
        delayed=True는 늦게 보내는 경보(서버 재시작 뒤 재전송)라 푸시 제목에 '지연 전송'을 붙인다.
        """
        if kind not in (KIND_ALERT, KIND_CLEAR):
            raise ValueError(f"알 수 없는 알림 종류: {kind}")
        guardian_ids = list(dict.fromkeys(guardian_ids))
        event_id = event_row["id"]
        if not guardian_ids:
            log.info("이벤트 %s: 수락된 보호자가 없어 알림을 보내지 않음", event_id)
            return {}
        async with self._lock_for(event_id):
            try:
                return await self._notify_locked(event_row, owner, guardian_ids, kind, delayed)
            finally:
                if kind == KIND_CLEAR:
                    self._event_locks.pop(event_id, None)  # 해제 뒤에는 이 이벤트의 경보 알림이 없다

    async def _notify_locked(self, event_row: Dict[str, Any], owner: Optional[Dict[str, Any]],
                             guardian_ids: List[int], kind: str, delayed: bool) -> Dict[int, Dict[str, bool]]:
        event_id = event_row["id"]
        # 잠금을 기다리는 사이 경보가 해제되었으면 경보 알림은 보내지 않는다.
        reason = self._skip_reason(event_id, kind)
        if reason:
            log.info("이벤트 %s %s 알림 보내지 않음: %s", event_id, kind, reason)
            return {}

        device = self.db.query_one("SELECT * FROM devices WHERE id = ?", (event_row["device_id"],))
        event = event_public_dict(event_row, device, owner)
        summary = {gid: {CHANNEL_WEBSOCKET: False, CHANNEL_PUSH: False} for gid in guardian_ids}

        # 1) 열린 보호자 화면. 닫혀 있으면 다시 열 때 snapshot으로 받으므로 재시도하지 않는다.
        delivered = set(await self.hub.send_to_users(guardian_ids, {"type": "event", "event": event}))
        for gid in guardian_ids:
            ok = gid in delivered
            summary[gid][CHANNEL_WEBSOCKET] = ok
            self._record(event_id, gid, kind, CHANNEL_WEBSOCKET, ok, None if ok else "열린 보호자 화면 없음")

        # 2) 웹 푸시
        payload = self.build_push_payload(event, kind, delayed=delayed)
        failed = await self._push_round(event_id, guardian_ids, payload, kind, attempt=0, summary=summary)
        if failed and self.settings.push_retry_delays:
            self._start_retry(event, failed, kind)

        log.info("이벤트 %s %s 알림: %s", event_id, kind, summary)
        return summary

    async def _push_round(self, event_id: int, guardian_ids: List[int], payload: Dict[str, Any], kind: str,
                          attempt: int, summary: Optional[Dict[int, Dict[str, bool]]] = None) -> List[int]:
        """푸시를 한 번 보내고 기록한다. 다시 보낼 가치가 있는(구독은 있는데 실패한) 보호자를 돌려준다."""
        suffix = f" (재시도 {attempt}회차)" if attempt else ""
        if not self.push.enabled:
            for gid in guardian_ids:
                self._record(event_id, gid, kind, CHANNEL_PUSH, False, "서버에 웹 푸시가 설정되지 않음")
            return []

        subscribed = set(self.push.subscribed_user_ids(guardian_ids))
        for gid in guardian_ids:
            if gid not in subscribed:
                self._record(event_id, gid, kind, CHANNEL_PUSH, False, "푸시 알림을 켜지 않음" + suffix)
        targets = [gid for gid in guardian_ids if gid in subscribed]
        if not targets:
            return []

        results = await self.push.send_to_users(targets, payload)
        failed = []
        for gid in targets:
            ok = results.get(gid, False)
            if summary is not None:
                summary[gid][CHANNEL_PUSH] = ok
            if ok:
                detail = f"재시도 {attempt}회차 성공" if attempt else None
            else:
                detail = "전송 실패" + suffix
            self._record(event_id, gid, kind, CHANNEL_PUSH, ok, detail)
            if not ok:
                failed.append(gid)
        return failed

    # --- 재시도 ---

    def _start_retry(self, event: Dict[str, Any], guardian_ids: List[int], kind: str) -> None:
        task = asyncio.create_task(self._retry(event, guardian_ids, kind))
        # 참조를 들고 있지 않으면 실행 중인 작업이 가비지 컬렉션될 수 있다.
        self._retry_tasks.add(task)
        task.add_done_callback(self._retry_tasks.discard)

    def _skip_reason(self, event_id: int, kind: str) -> Optional[str]:
        row = self.db.query_one("SELECT ended_at, cleared_by FROM events WHERE id = ?", (event_id,))
        if row is None:
            return "이벤트가 삭제됨"
        if kind == KIND_ALERT and (row["ended_at"] is not None or row["cleared_by"] is not None):
            return "경보가 이미 해제됨"
        return None

    async def _retry(self, event: Dict[str, Any], guardian_ids: List[int], kind: str) -> None:
        remaining = list(guardian_ids)
        payload = self.build_push_payload(event, kind, delayed=(kind == KIND_ALERT))
        try:
            for attempt, delay in enumerate(self.settings.push_retry_delays, start=1):
                await asyncio.sleep(delay)
                async with self._lock_for(event["id"]):
                    # 기다리는 동안 경보가 해제되었을 수 있다. 해제된 경보를 늦게 울리면 혼란만 준다.
                    reason = self._skip_reason(event["id"], kind)
                    if reason:
                        log.info("이벤트 %s 푸시 재전송 취소: %s", event["id"], reason)
                        return
                    remaining = self._current_guardians(event.get("owner_id"), remaining)
                    if not remaining:
                        log.info("이벤트 %s 푸시 재전송 취소: 남은 보호자의 연결이 해제됨", event["id"])
                        return
                    remaining = await self._push_round(event["id"], remaining, payload, kind, attempt=attempt)
                if not remaining:
                    return
            log.warning("이벤트 %s 푸시 재전송 모두 실패: 보호자 %s", event["id"], remaining)
        except Exception:  # CancelledError(종료 시 취소)는 여기 걸리지 않고 그대로 올라간다
            log.exception("이벤트 %s 푸시 재전송 중 오류", event["id"])

    async def wait_retries(self) -> None:
        """진행 중인 재전송이 모두 끝날 때까지 기다린다(테스트용)."""
        while self._retry_tasks:
            await asyncio.gather(*list(self._retry_tasks), return_exceptions=True)

    async def aclose(self) -> None:
        """서버 종료 시 남은 재전송 작업을 멈춘다."""
        tasks = list(self._retry_tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._retry_tasks.clear()
