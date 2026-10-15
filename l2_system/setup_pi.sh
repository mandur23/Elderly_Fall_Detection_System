#!/bin/bash
# Raspberry Pi 5 (Raspberry Pi OS Bookworm 64-bit, 데스크톱) 설치 스크립트.
# 저장소를 받은 폴더에서 일반 사용자로 실행한다:  bash l2_system/setup_pi.sh
#
# 하는 일: 시스템 패키지 설치 -> 가상환경·파이썬 패키지 -> 모델 내려받기 -> 소리 파일 생성
#          -> 설정 파일 생성 -> 사용자 서비스 등록 -> 키오스크 자동 실행 -> 화면 꺼짐 방지
set -euo pipefail

INSTALL_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$INSTALL_DIR"
MP_MODEL_URL="https://storage.googleapis.com/mediapipe-models/pose_landmarker/pose_landmarker_lite/float16/latest/pose_landmarker_lite.task"
VOSK_MODEL_URL="https://alphacephei.com/vosk/models/vosk-model-small-ko-0.22.zip"

if [ "$(id -u)" -eq 0 ]; then
  echo "root가 아닌 일반 사용자로 실행하세요 (sudo는 스크립트가 필요할 때 사용합니다)." >&2
  exit 1
fi

echo "[1/8] 시스템 패키지"
sudo apt-get update
# fonts-nanum: 현장 화면(Chromium)과 자세 추정 데모의 한글 표시
sudo apt-get install -y python3-venv python3-dev python3-picamera2 libportaudio2 \
  alsa-utils espeak-ng curl unzip chromium-browser fonts-nanum

echo "[2/8] 가상환경과 파이썬 패키지"
# picamera2는 apt 패키지라 시스템 패키지를 볼 수 있는 가상환경을 만든다.
python3 -m venv --system-site-packages .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt -r requirements-device.txt

echo "[3/8] 모델 내려받기"
mkdir -p assets/models data
[ -f assets/models/pose_landmarker_lite.task ] || curl -fL -o assets/models/pose_landmarker_lite.task "$MP_MODEL_URL"
if [ ! -d data/vosk-model-small-ko ]; then
  curl -fL -o /tmp/vosk-ko.zip "$VOSK_MODEL_URL"
  unzip -q /tmp/vosk-ko.zip -d data/
  mv data/vosk-model-small-ko-0.22 data/vosk-model-small-ko
  rm /tmp/vosk-ko.zip
fi

echo "[4/8] 알림음·경보음 생성"
.venv/bin/python -m tools.make_sounds

echo "[5/8] 설정 파일"
if [ ! -f config.local.toml ]; then
  TOKEN="$(.venv/bin/python -c 'import secrets; print(secrets.token_hex(16))')"
  sed "s/__INTERNAL_API_TOKEN__/$TOKEN/" l2_system/config.local.toml.example > config.local.toml
  chmod 600 config.local.toml  # 텔레그램 토큰이 들어가므로 본인만 읽을 수 있게
  echo "  config.local.toml 생성됨 - 텔레그램 토큰, 설치 위치 이름 등을 채우세요."
fi

echo "[6/8] 사용자 서비스 등록"
UNIT_DIR="$HOME/.config/systemd/user"
mkdir -p "$UNIT_DIR"
for unit in fall-app.service fall-detect.service; do
  sed "s#__INSTALL_DIR__#$INSTALL_DIR#g" "l2_system/$unit" > "$UNIT_DIR/$unit"
done
systemctl --user daemon-reload
systemctl --user enable fall-app.service fall-detect.service
# 로그인하지 않아도(재부팅 직후 등) 사용자 서비스가 시작되게 한다.
sudo loginctl enable-linger "$USER"
# 로그가 저장 공간을 다 쓰지 않게 제한한다.
sudo mkdir -p /etc/systemd/journald.conf.d
printf '[Journal]\nStorage=persistent\nSystemMaxUse=200M\n' | sudo tee /etc/systemd/journald.conf.d/fall-detect.conf >/dev/null
sudo systemctl restart systemd-journald

echo "[7/8] 키오스크 화면 자동 실행"
mkdir -p "$HOME/.config/labwc"
AUTOSTART="$HOME/.config/labwc/autostart"
grep -qs "l2_system/kiosk.sh" "$AUTOSTART" || echo "$INSTALL_DIR/l2_system/kiosk.sh &" >> "$AUTOSTART"

echo "[8/8] 화면 꺼짐 방지"
sudo raspi-config nonint do_blanking 1 || echo "  raspi-config로 화면 꺼짐을 끄지 못했습니다. 수동으로 설정하세요."

cat <<MSG

설치 완료. 다음 순서로 진행하세요.
  1. config.local.toml 편집 (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, DEVICE_NAME)
  2. 안내 음성 녹음 파일을 assets/audio/ask_status.wav 로 저장 (없으면 합성 음성 사용)
  3. 장치 점검:  .venv/bin/python -m tools.check_device --send-test
  4. 서비스 시작: systemctl --user start fall-app fall-detect
  5. 재부팅 후 화면과 서비스가 자동으로 뜨는지 확인
로그 보기: journalctl --user -u fall-app -u fall-detect -f
MSG
