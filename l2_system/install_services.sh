#!/bin/bash
# Raspberry Pi에 systemd 서비스를 등록한다. 저장소 위치: /home/pi/fall-detect-system
set -euo pipefail
cd "$(dirname "$0")"

echo "[1/3] 시스템 서비스(L3, L4) 등록"
sudo cp fall-app.service fall-detect.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable fall-app.service fall-detect.service

echo "[2/3] 키오스크 화면 사용자 서비스 등록"
mkdir -p ~/.config/systemd/user
cp fall-ui.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable fall-ui.service

echo "[3/3] 완료. 시작하려면:"
echo "  sudo systemctl start fall-app fall-detect"
echo "  systemctl --user start fall-ui"
