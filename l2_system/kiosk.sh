#!/bin/bash
# 현장 화면(Chromium 키오스크). 데스크톱 자동 실행(labwc autostart)에서 호출된다.
# 서버가 뜰 때까지 기다리고, 브라우저가 종료되면 다시 띄운다.
URL="http://127.0.0.1:5000"
BROWSER="$(command -v chromium-browser || command -v chromium)"

until curl -sf "$URL/api/state" >/dev/null; do sleep 2; done
while true; do
  "$BROWSER" --kiosk --noerrdialogs --disable-infobars --incognito \
    --disable-session-crashed-bubble --check-for-update-interval=31536000 "$URL"
  sleep 3
done
