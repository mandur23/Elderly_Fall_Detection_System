"""보호자 알림(텔레그램)과 알림 문구.

텍스트만 보낸다(사진·영상 미첨부). 토큰·수신자가 없으면 보낸 척하지 않고 '알림 미설정'(disabled)으로 남겨
현장 화면에 보이게 한다.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Dict, List, Optional

import aiohttp

log = logging.getLogger("server.notify")

RESULT_LABELS = {
    "ALERT_HELP": "음성으로 도움 요청",
    "ALERT_NO_REPLY": "음성 확인에 응답 없음",
    "ALERT_VOICE_FAIL": "장치 오류로 음성 확인 불가",
    "ALERT_INTERRUPTED": "음성 확인 도중 서버가 다시 시작되어 확인하지 못함",
    "ALERT_DISCONNECTED": "음성 확인 중 장치 연결 끊김",
}


class NotifierError(RuntimeError):
    pass


class Notifier:
    """send()는 실패하면 NotifierError를 던진다."""
    enabled = True

    async def send(self, text: str) -> None:
        raise NotImplementedError

    async def close(self) -> None:
        pass


class DisabledNotifier(Notifier):
    enabled = False

    async def send(self, text: str) -> None:
        log.info("[알림 미설정 - 보내지 않음]\n%s", text)


class TelegramNotifier(Notifier):
    def __init__(self, bot_token: str, chat_id: str, timeout: float):
        self._url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        self.chat_id = chat_id
        self.timeout = timeout
        self._session: Optional[aiohttp.ClientSession] = None

    async def send(self, text: str) -> None:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self.timeout))
        try:
            async with self._session.post(self._url, json={"chat_id": self.chat_id, "text": text}) as res:
                if res.status != 200:
                    raise NotifierError(f"텔레그램 응답 {res.status}")
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            # 오류 문구에 토큰이 든 주소가 섞일 수 있어 종류만 남긴다.
            raise NotifierError(f"네트워크 오류: {type(e).__name__}") from None

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()


def create_notifier(settings) -> Notifier:
    if settings.notifier_configured:
        return TelegramNotifier(settings.telegram_bot_token, settings.telegram_chat_id,
                                settings.notify_timeout_seconds)
    log.warning("텔레그램 토큰/수신자가 설정되지 않아 보호자 알림을 보내지 않습니다 (알림 미설정).")
    return DisabledNotifier()


# --- 문구 ---
def _fmt(iso: str) -> str:
    return (iso or "")[:19].replace("T", " ")


def _head(title: str, location: str) -> str:
    return f"[{title}] {location}".rstrip()


def alert_message(event: Dict, delayed: bool = False, location: str = "", link: str = "") -> str:
    lines = [
        _head("낙상 의심 경보" + (" - 지연 전송" if delayed else ""), location),
        f"발생 시각: {_fmt(event['started_at'])}",
        f"판정: {RESULT_LABELS.get(event['result'], event['result'])}",
    ]
    if delayed:
        lines.append("통신 장애나 서버 재시작으로 늦게 전달된 알림입니다. 위 발생 시각을 확인해 주세요.")
    lines.append("현장 상태를 확인해 주세요. 이 알림은 보조 수단이며 응급 신고를 대신하지 않습니다.")
    if link:
        lines.append(f"보호자 웹: {link}")
    return "\n".join(lines)


CLEARED_BY_LABELS = {"BUTTON": "현장 화면 버튼", "ADMIN": "관리자 화면"}


def cleared_by_label(cleared_by: str) -> str:
    if cleared_by.startswith("GUARDIAN:"):
        return f"보호자 웹 ({cleared_by.split(':', 1)[1]})"
    return CLEARED_BY_LABELS.get(cleared_by, cleared_by or "사용자")


def clear_message(event: Dict, location: str = "") -> str:
    cleared_by = event.get("cleared_by") or ""
    if cleared_by == "ADMIN":
        who = "관리자가 경보를 해제했습니다."
    elif cleared_by.startswith("GUARDIAN:"):
        who = "보호자가 보호자 웹에서 경보를 해제했습니다."
    else:
        who = "현장에서 경보를 해제했습니다."
    return "\n".join([
        _head("경보 해제", location),
        f"발생 시각: {_fmt(event['started_at'])}",
        f"해제 방법: {cleared_by_label(cleared_by)}",
        who,
    ])


def device_problem_message(problems: List[str], since_iso: str, location: str = "") -> str:
    return "\n".join([
        _head("장치 이상 - 낙상 감지 중단 가능", location),
        f"시작 시각: {_fmt(since_iso)}",
        *[f"- {p}" for p in problems],
        "이 상태에서는 낙상을 감지하지 못할 수 있습니다. 장치를 확인해 주세요.",
    ])


def device_recovered_message(location: str = "") -> str:
    return "\n".join([_head("장치 정상 복구", location), "낙상 감지가 다시 동작합니다."])
