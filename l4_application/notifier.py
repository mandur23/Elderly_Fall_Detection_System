"""보호자·관리자 알림.

텔레그램 봇으로 텍스트만 보낸다(사진·영상 미첨부). 토큰이 설정되지 않으면
전송한 것처럼 꾸미지 않고 notify_state를 'disabled'로 남겨 화면에 '알림 미설정'이 보이게 한다.
"""

import logging
from typing import Dict, Optional

import requests

log = logging.getLogger("l4.notify")

RESULT_LABELS = {
    "ALERT_HELP": "음성으로 도움 요청",
    "ALERT_NO_REPLY": "음성 확인에 응답 없음",
    "ALERT_VOICE_FAIL": "음성 확인 장치 오류로 확인 불가",
}


class NotifierError(RuntimeError):
    pass


class Notifier:
    """전송 인터페이스. send()는 실패 시 NotifierError를 던진다."""
    enabled = True

    def send(self, text: str) -> None:
        raise NotImplementedError


class DisabledNotifier(Notifier):
    enabled = False

    def send(self, text: str) -> None:
        log.info("[알림 미설정 - 전송하지 않음]\n%s", text)


class TelegramNotifier(Notifier):
    def __init__(self, bot_token: str, chat_id: str, timeout: float):
        self.url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        self.chat_id = chat_id
        self.timeout = timeout

    def send(self, text: str) -> None:
        try:
            res = requests.post(self.url, json={"chat_id": self.chat_id, "text": text}, timeout=self.timeout)
        except requests.RequestException as e:
            raise NotifierError(f"네트워크 오류: {type(e).__name__}") from e
        if res.status_code != 200:
            raise NotifierError(f"텔레그램 응답 {res.status_code}")


def create_notifier(bot_token: str, chat_id: str, timeout: float) -> Notifier:
    if bot_token and chat_id:
        return TelegramNotifier(bot_token, chat_id, timeout)
    log.warning("텔레그램 토큰/수신자가 설정되지 않아 알림을 보내지 않습니다.")
    return DisabledNotifier()


def alert_message(event: Dict, delayed: bool = False) -> str:
    lines = [
        "[낙상 의심 경보]" + (" (지연 전송)" if delayed else ""),
        f"발생 시각: {event['started_at'][:19].replace('T', ' ')}",
        f"판정: {RESULT_LABELS.get(event['result'], event['result'])}",
    ]
    if delayed:
        lines.append("통신 장애로 늦게 전달된 알림입니다. 발생 시각을 확인해 주세요.")
    lines.append("현장 상태를 확인해 주세요. 이 알림은 보조 수단이며 응급 신고를 대신하지 않습니다.")
    return "\n".join(lines)


def clear_message(event: Dict, cleared_by: Optional[str]) -> str:
    by = {"BUTTON": "현장 화면 버튼"}.get(cleared_by or "", cleared_by or "사용자")
    return "\n".join([
        "[경보 해제]",
        f"발생 시각: {event['started_at'][:19].replace('T', ' ')}",
        f"해제 방법: {by}",
        "현장에서 사용자가 경보를 해제했습니다.",
    ])
