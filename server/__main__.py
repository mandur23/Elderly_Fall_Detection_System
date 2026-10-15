"""python -m server [--config server/config.toml] [--host 0.0.0.0] [--port 8000]"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from aiohttp import web

from server.app import FallServer
from server.config import ConfigError, load_settings

log = logging.getLogger("server")


def main(argv=None) -> int:
    if sys.version_info < (3, 11):
        print("Python 3.11 이상이 필요합니다.", file=sys.stderr)
        return 2
    parser = argparse.ArgumentParser(prog="python -m server", description="낙상 감지 PC 서버")
    parser.add_argument("--config", type=Path, help="설정 파일 (기본: server/config.toml)")
    parser.add_argument("--host", help="받을 주소 (기본 0.0.0.0)")
    parser.add_argument("--port", type=int, help="포트 (기본 8000)")
    parser.add_argument("--log-level", help="DEBUG, INFO, WARNING")
    args = parser.parse_args(argv)

    try:
        settings = load_settings(args.config, host=args.host, port=args.port, log_level=args.log_level)
    except ConfigError as e:
        print(f"설정 오류: {e}", file=sys.stderr)
        return 2
    logging.basicConfig(level=getattr(logging, settings.log_level.upper(), logging.INFO),
                        format="[%(asctime)s][%(name)s] %(levelname)s %(message)s")
    if not settings.device_token:
        log.error("DEVICE_TOKEN이 설정되지 않았습니다. server/config.toml의 device_token 또는 환경변수 "
                  "DEVICE_TOKEN에 라즈베리파이와 같은 값을 넣어 주세요.")
        return 2

    # MediaPipe(TensorFlow Lite)의 내부 경고 로그를 줄인다.
    os.environ.setdefault("GLOG_minloglevel", "2")
    os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "2")
    server = FallServer(settings)
    log.info("서버 시작: http://%s:%d (현장 화면 /kiosk, edge 접속 /ws/device)", settings.host, settings.port)
    web.run_app(server.app, host=settings.host, port=settings.port, access_log=None, print=None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
