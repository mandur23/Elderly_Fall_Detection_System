#!/bin/bash
# 현장 화면(Chromium 키오스크). 데스크톱 자동 실행(~/.config/autostart/fall-kiosk.desktop)에서 호출된다.
# 켜자마자 이 폴더의 대기 화면(waiting.html, 라즈베리파이 안의 파일)을 연다. 대기 화면은 서버의 /api/health가
# 응답하면 현장 화면 http://<서버>:8000/kiosk?device=<id>&key=<장치별 키> 로 넘어간다. 그래서 서버가 늦게 켜져도
# 화면은 바로 보인다. 대기 화면 주소(현장 화면·health 주소, 장치 이름)는 edge/config.toml에서 만든다(edge와 같은 설정).
# 설정을 읽을 수 없으면 대기 화면에 '설정이 필요합니다'를 보이고, 30초마다(그리고 config.toml이 바뀌면) 다시 읽어
# 주소가 바뀌면 브라우저를 새 주소로 다시 띄운다. 브라우저가 꺼지면 다시 띄운다. 한 번에 하나만 실행된다(flock).
INSTALL_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
PYTHON="$INSTALL_DIR/.venv/bin/python"
CONFIG="$INSTALL_DIR/edge/config.toml"
WAITING="$INSTALL_DIR/edge/deploy/waiting.html"
RECHECK_SECONDS=30
cd "$INSTALL_DIR" || exit 1

# 자동 실행일 때는 기록을 파일에 남긴다(~/.cache/fall-edge/kiosk.log). 터미널에서 직접 실행하면 화면에 보인다.
if [ ! -t 1 ]; then
  LOG_DIR="${XDG_CACHE_HOME:-$HOME/.cache}/fall-edge"
  mkdir -p "$LOG_DIR"
  LOG="$LOG_DIR/kiosk.log"
  # 1MB가 넘으면 하나만 남기고 새로 쓴다.
  if [ "$(stat -c %s "$LOG" 2>/dev/null || echo 0)" -gt 1048576 ]; then mv -f "$LOG" "$LOG.old"; fi
  exec >>"$LOG" 2>&1
fi
log() { echo "$(date '+%F %T') $*"; }

# 자동 실행이 두 번 되어도(데스크톱 세션이 다시 시작되는 등) 브라우저가 둘 뜨지 않게 잠근다.
# 잠금(9번 파일)은 브라우저에도 이어지므로, 이 스크립트가 죽어도 브라우저가 떠 있는 동안에는 새로 띄우지 않는다.
LOCK="${XDG_RUNTIME_DIR:-/tmp}/fall-kiosk.lock"
if command -v flock >/dev/null && exec 9>>"$LOCK"; then
  if ! flock -n 9; then
    log "현장 화면이 이미 실행 중입니다 ($LOCK)"
    exit 0
  fi
else
  log "잠금 파일을 쓸 수 없어 잠그지 않고 실행합니다: $LOCK"
fi

BROWSER="$(command -v chromium || command -v chromium-browser)"
if [ -z "$BROWSER" ]; then
  log "Chromium이 없습니다: sudo apt install chromium"
  exit 1
fi

# 화면 꺼짐 방지 (X11 세션일 때. Wayland는 install.sh의 raspi-config 설정으로 끈다)
if [ -n "${DISPLAY:-}" ] && command -v xset >/dev/null; then
  xset s off; xset s noblank; xset -dpms
fi

# 대기 화면 주소를 URL에 넣는다. 설정이 맞으면 0, 틀리면 안내(error)가 붙은 주소와 0이 아닌 값.
waiting_url() {
  local url
  if url="$("$PYTHON" -m edge --print-url waiting)"; then
    URL="$url"
    return 0
  fi
  # 파이썬을 실행하지 못했으면(가상환경이 없음 등) 직접 만든다.
  URL="${url:-file://$WAITING?error=venv}"
  return 1
}

# 설정 파일이 바뀌었는지 볼 값 (수정 시각·크기·inode. 설치 스크립트는 새 파일로 바꿔 쓴다)
config_stamp() { stat -c '%Y %s %i' "$CONFIG" 2>/dev/null || echo none; }

stop_browser() {
  kill "$BROWSER_PID" 2>/dev/null
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    kill -0 "$BROWSER_PID" 2>/dev/null || return 0
    sleep 1
  done
  kill -9 "$BROWSER_PID" 2>/dev/null
}

log "현장 화면을 시작합니다 ($INSTALL_DIR)"
while true; do
  STAMP="$(config_stamp)"
  if waiting_url; then
    VALID=1
  else
    VALID=0
    log "설정을 읽을 수 없어 대기 화면에 안내를 보입니다. ${RECHECK_SECONDS}초마다 다시 봅니다."
  fi
  CHECKED=$SECONDS
  # 전용 프로필(--user-data-dir): 다른 Chromium 창이 떠 있어도 그 창에 넘기고 바로 끝나지 않게 한다
  # (넘기고 끝나면 3초마다 새 창이 열린다). 대기 화면(file://)이 서버를 확인하는 요청(no-cors)에는 따로 옵션이 필요 없다.
  "$BROWSER" --kiosk --noerrdialogs --disable-infobars --incognito --password-store=basic \
    --user-data-dir="$HOME/.config/fall-kiosk" \
    --disable-session-crashed-bubble --check-for-update-interval=31536000 "$URL" >/dev/null 2>&1 &
  BROWSER_PID=$!
  while kill -0 "$BROWSER_PID" 2>/dev/null; do
    sleep 2
    # 설정 파일이 바뀌었거나, 설정이 틀린 동안 30초가 지났으면 주소를 다시 만든다. 달라졌으면 브라우저를 다시 띄운다.
    if [ "$(config_stamp)" != "$STAMP" ] || { [ "$VALID" = 0 ] && [ $((SECONDS - CHECKED)) -ge "$RECHECK_SECONDS" ]; }; then
      STAMP="$(config_stamp)"
      CHECKED=$SECONDS
      OLD_URL="$URL"
      if waiting_url; then VALID=1; else VALID=0; fi
      if [ "$URL" != "$OLD_URL" ]; then
        log "설정이 바뀌어 화면을 새 주소로 다시 엽니다"
        stop_browser
        break
      fi
    fi
  done
  wait "$BROWSER_PID" 2>/dev/null
  log "브라우저가 끝났습니다 (종료 코드 $?). 3초 뒤 다시 엽니다."
  sleep 3
done
