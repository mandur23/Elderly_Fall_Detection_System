#!/bin/bash
# 현장 화면(Chromium 키오스크). 데스크톱 자동 실행(~/.config/autostart/fall-kiosk.desktop)에서 호출된다.
# 서버의 /api/health가 응답할 때까지 기다린 뒤 http://<서버>:8000/kiosk 를 열고, 브라우저가 꺼지면 다시 띄운다.
# 서버 주소는 edge/config.toml에서 읽는다(edge와 같은 설정).
INSTALL_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
PYTHON="$INSTALL_DIR/.venv/bin/python"
cd "$INSTALL_DIR" || exit 1

BROWSER="$(command -v chromium || command -v chromium-browser)"
if [ -z "$BROWSER" ]; then
  echo "Chromium이 없습니다: sudo apt install chromium" >&2
  exit 1
fi

until KIOSK_URL="$("$PYTHON" -m edge --print-url kiosk)" && HEALTH_URL="$("$PYTHON" -m edge --print-url health)"; do
  echo "edge/config.toml의 서버 주소를 읽을 수 없습니다. 30초 뒤 다시 봅니다." >&2
  sleep 30
done

# 화면 꺼짐 방지 (X11 세션일 때. Wayland는 install.sh의 raspi-config 설정으로 끈다)
if [ -n "${DISPLAY:-}" ] && command -v xset >/dev/null; then
  xset s off; xset s noblank; xset -dpms
fi

echo "서버를 기다립니다: $HEALTH_URL"
until curl -sf --max-time 3 "$HEALTH_URL" >/dev/null; do sleep 2; done

while true; do
  "$BROWSER" --kiosk --noerrdialogs --disable-infobars --incognito --password-store=basic \
    --disable-session-crashed-bubble --check-for-update-interval=31536000 "$KIOSK_URL"
  sleep 3
  # 서버가 다시 시작되는 중이면 뜰 때까지 기다렸다가 연다.
  until curl -sf --max-time 3 "$HEALTH_URL" >/dev/null; do sleep 2; done
done
