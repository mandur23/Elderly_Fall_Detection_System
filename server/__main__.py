"""python -m server [--config server/config.toml] [--host 0.0.0.0] [--port 8000] [--log-file data/logs/server.log]"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

from aiohttp import web

from server.app import FallServer
from server.config import ConfigError, load_settings

log = logging.getLogger("server")

LOG_FORMAT = "[%(asctime)s][%(name)s] %(levelname)s %(message)s"
LOG_FILE_MAX_BYTES = 10 * 1024 * 1024   # 기록 파일이 이 크기를 넘으면 server.log.1 ... 로 돌려 쓴다
LOG_FILE_BACKUPS = 5


def setup_logging(level_name: str, log_file: Optional[Path]) -> None:
    """콘솔 기록. log_file(--log-file 또는 환경변수 FALL_LOG_FILE)이 있으면 크기로 돌려 쓰는 기록 파일에도 남긴다.

    기록 파일을 쓸 때 콘솔에는 경고 이상만 낸다. start_server.bat는 콘솔 출력(오류 추적 등)을 따로 파일에 모으는데,
    서버가 오래 켜져 있어도 그 파일이 같은 내용으로 계속 커지지 않게 하기 위해서다.
    """
    level = getattr(logging, level_name.upper(), logging.INFO)
    console = logging.StreamHandler()
    handlers: list[logging.Handler] = [console]
    problem = None
    if log_file:
        try:
            log_file.parent.mkdir(parents=True, exist_ok=True)
            handlers.append(RotatingFileHandler(log_file, maxBytes=LOG_FILE_MAX_BYTES,
                                                backupCount=LOG_FILE_BACKUPS, encoding="utf-8"))
            console.setLevel(max(level, logging.WARNING))
        except OSError as e:
            problem = e     # 기록 파일을 못 열어도 서버는 콘솔 기록만으로 계속 동작한다
    logging.basicConfig(level=level, format=LOG_FORMAT, handlers=handlers)
    if problem:
        log.error("기록 파일을 열지 못해 콘솔에만 기록합니다 (%s): %s", log_file, problem)


def main(argv=None) -> int:
    if sys.version_info < (3, 11):
        print("Python 3.11 이상이 필요합니다.", file=sys.stderr)
        return 2
    parser = argparse.ArgumentParser(prog="python -m server", description="낙상 감지 PC 서버")
    parser.add_argument("--config", type=Path, help="설정 파일 (기본: server/config.toml)")
    parser.add_argument("--host", help="받을 주소 (기본 0.0.0.0)")
    parser.add_argument("--port", type=int, help="포트 (기본 8000)")
    parser.add_argument("--log-level", help="DEBUG, INFO, WARNING")
    parser.add_argument("--log-file", type=Path, default=os.environ.get("FALL_LOG_FILE") or None,
                        help="기록 파일 (10 MB씩 5개까지 돌려 씀, 기본: 환경변수 FALL_LOG_FILE, 없으면 콘솔만)")
    args = parser.parse_args(argv)

    try:
        settings = load_settings(args.config, host=args.host, port=args.port, log_level=args.log_level)
    except ConfigError as e:
        print(f"설정 오류: {e}", file=sys.stderr)
        return 2
    setup_logging(settings.log_level, args.log_file)
    if not settings.device_token:
        log.error("DEVICE_TOKEN이 설정되지 않았습니다. server/config.toml의 device_token 또는 환경변수 "
                  "DEVICE_TOKEN에 라즈베리파이와 같은 값을 넣어 주세요.")
        return 2

    server = FallServer(settings)
    log.info("서버 시작: http://%s:%d (현장 화면 /kiosk, edge 접속 /ws/device)", settings.host, settings.port)
    web.run_app(server.app, host=settings.host, port=settings.port, access_log=None, print=None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
