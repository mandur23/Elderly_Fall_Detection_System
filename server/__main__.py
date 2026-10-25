"""`python -m server`로 중앙 서버를 실행한다.

휴대폰 브라우저는 HTTPS 페이지에서만 카메라·마이크·웹 푸시를 쓸 수 있으므로 실제 시연은
FALL_SSL_CERT, FALL_SSL_KEY(mkcert 등으로 만든 인증서)를 설정하고 실행한다.
"""

import logging
import re

import uvicorn

from server.config import Settings
from server.main import create_app

log = logging.getLogger("server")

# 주소에 들어 있는 기기 키(?key=... / &key=...)
_KEY_IN_URL = re.compile(r"([?&]key=)[^&\s\"]*")


class HideDeviceKeys(logging.Filter):
    """uvicorn 접속 로그의 주소에서 기기 키를 가린다.

    현장 화면 주소와 Pi 접속 주소에 ?key=...가 들어갈 수 있고, uvicorn은 쿼리 문자열까지 로그에
    남긴다. DB에는 키의 해시만 두는데 로그에 원문이 남으면 로그를 읽는 사람이 기기로 위장할 수 있다.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(_KEY_IN_URL.sub(r"\1***", a) if isinstance(a, str) else a for a in record.args)
        return True


def install_log_filters() -> None:
    # 일반 요청은 uvicorn.access, WebSocket 접속은 uvicorn.error 로거로 남는다.
    for name in ("uvicorn.access", "uvicorn.error"):
        logging.getLogger(name).addFilter(HideDeviceKeys())


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = Settings()
    https = bool(settings.ssl_certfile and settings.ssl_keyfile)
    if not https:
        log.warning("HTTPS 인증서가 설정되지 않았습니다(FALL_SSL_CERT, FALL_SSL_KEY). "
                    "휴대폰 카메라·마이크·웹 푸시가 동작하지 않습니다.")
        if settings.cookie_secure:
            log.warning("HTTP에서는 Secure 로그인 쿠키가 저장되지 않습니다. "
                        "HTTPS 없이 시험하려면 FALL_COOKIE_SECURE=0으로 실행하세요.")
    install_log_filters()
    app = create_app(settings)
    scheme = "https" if https else "http"
    log.info("주소: %s://%s:%s", scheme, settings.host, settings.port)
    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        ssl_certfile=settings.ssl_certfile if https else None,
        ssl_keyfile=settings.ssl_keyfile if https else None,
        log_level="info",
        # 서버 앞에 리버스 프록시가 없으므로 X-Forwarded-For·X-Forwarded-Proto를 절대 믿지 않는다.
        # 켜 두면(uvicorn 기본값) FORWARDED_ALLOW_IPS 환경변수가 설정된 PC에서 같은 Wi-Fi 기기가
        # 'X-Forwarded-For: 127.0.0.1'로 접속 주소를 바꿔 관리자 검사(루프백 주소)를 통과한다.
        proxy_headers=False,
    )


if __name__ == "__main__":
    main()
