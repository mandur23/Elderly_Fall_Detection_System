#!/usr/bin/env bash
# 중앙 서버 통합 실행 (macOS·Linux). 서버와 웹이 켜지고 관리자 화면이 열립니다.
# 모델 없이 시험하려면: ./run_server.sh --fake
cd "$(dirname "$0")"
if [ -x .venv/bin/python ]; then PY=.venv/bin/python; else PY=python3; fi
exec "$PY" run_server.py "$@"
