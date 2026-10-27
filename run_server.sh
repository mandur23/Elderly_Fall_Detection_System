#!/usr/bin/env bash
# 중앙 서버 원클릭 실행 (macOS·Linux): 가상환경 만들기 → 패키지·인증서 자동 설정 → 서버 실행 → 관리자 화면 열기.
# 모델 없이 시험하려면: ./run_server.sh --fake
set -e
cd "$(dirname "$0")"
# 3.13보다 새로운 Python으로 만든 .venv는 numpy·PyTorch 설치 파일(wheel)이 없어 패키지 설치에 실패한다. 지우고 다시 만든다.
if [ -x .venv/bin/python ] && ! .venv/bin/python -c 'import sys; sys.exit(0 if (3,10) <= sys.version_info[:2] <= (3,13) else 1)'; then
  echo "[설정] 기존 .venv의 Python($(.venv/bin/python -c 'import sys; print(sys.version.split()[0])'))은 지원하지 않는 버전입니다. .venv를 지우고 다시 만듭니다."
  rm -rf .venv
fi
if [ ! -x .venv/bin/python ]; then
  PY=""
  for c in python3.12 python3.11 python3.10 python3.13 python3; do
    if command -v "$c" >/dev/null && "$c" -c 'import sys; sys.exit(0 if (3,10) <= sys.version_info[:2] <= (3,13) else 1)'; then
      PY="$c"; break
    fi
  done
  if [ -z "$PY" ]; then
    echo "Python 3.10~3.13이 필요합니다. 설치한 뒤 다시 실행하세요 (macOS: brew install python@3.11, Ubuntu: sudo apt install python3 python3-venv)."
    exit 1
  fi
  echo "[설정] 가상환경(.venv)을 만듭니다: $PY"
  "$PY" -m venv .venv
fi
exec .venv/bin/python run_server.py --auto "$@"
