"""systemd 알림(sd_notify) 최소 구현. 추가 패키지 없이 NOTIFY_SOCKET에 직접 쓴다.

서비스 파일에 Type=notify, WatchdogSec=...를 설정하면, 메인 루프가 멈췄을 때
(예: 카메라 드라이버 정지) systemd가 프로세스를 재시작한다. systemd 밖에서는 아무 일도 하지 않는다.
"""

import os
import socket


def notify(message: str) -> bool:
    addr = os.getenv("NOTIFY_SOCKET")
    if not addr:
        return False
    if addr.startswith("@"):
        addr = "\0" + addr[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.connect(addr)
            sock.sendall(message.encode())
        return True
    except OSError:
        return False


def ready() -> None:
    notify("READY=1")


def watchdog() -> None:
    notify("WATCHDOG=1")
