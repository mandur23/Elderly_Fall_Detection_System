#!/bin/bash
# 라즈베리파이 5 (Raspberry Pi OS 64비트 데스크톱, Bookworm 이후) edge 설치 스크립트.
# 저장소를 받은 폴더에서 일반 사용자로 실행한다:  bash edge/deploy/install.sh
#
# 하는 일: 시스템 패키지 -> 가상환경 -> 설정 파일 -> 사용자 서비스(fall-edge) -> 키오스크 자동 실행 -> 화면 꺼짐 방지
# 영상·음성 처리는 PC 서버가 하므로 모델은 내려받지 않는다.
set -euo pipefail

INSTALL_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$INSTALL_DIR"
DEPLOY="$INSTALL_DIR/edge/deploy"

if [ "$(id -u)" -eq 0 ]; then
  echo "root가 아닌 일반 사용자로 실행하세요 (sudo는 스크립트가 필요할 때 씁니다)." >&2
  exit 1
fi

echo "[1/6] 시스템 패키지"
sudo apt-get update
# numpy·OpenCV·picamera2는 apt 것을 쓴다(서로 맞춰 빌드되어 있음). pip로 numpy 2.x가 깔리면 picamera2가 깨질 수 있다.
# libportaudio2: sounddevice, fonts-nanum: 현장 화면 한글, curl: 키오스크가 서버를 기다릴 때
sudo apt-get install -y python3-venv python3-picamera2 python3-opencv python3-numpy \
  libportaudio2 alsa-utils curl fonts-nanum
# Chromium 패키지 이름은 OS 버전에 따라 다르다.
sudo apt-get install -y chromium || sudo apt-get install -y chromium-browser

echo "[2/6] 가상환경과 파이썬 패키지"
# apt로 설치한 picamera2·OpenCV를 쓰려고 시스템 패키지를 볼 수 있는 가상환경을 만든다.
python3 -m venv --system-site-packages .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r edge/deploy/requirements-pi.txt
.venv/bin/python -c "import cv2, numpy, aiohttp, sounddevice; print('  파이썬 패키지 확인: OpenCV', cv2.__version__)"
.venv/bin/python -c "from picamera2 import Picamera2; print('  카메라:', Picamera2.global_camera_info() or '없음 (USB 웹캠이면 [camera] backend = \"opencv\")')" \
  || echo "  picamera2를 불러오지 못했습니다. 카메라 모듈을 쓰려면 확인이 필요합니다."

echo "[3/6] 설정 파일"
if [ ! -f edge/config.toml ]; then
  cp edge/config.example.toml edge/config.toml
  sed -i "s/^id = \"living-room\"/id = \"$(hostname)\"/" edge/config.toml
  echo "  edge/config.toml 생성됨 - 서버 주소(address)와 장치 토큰(token)을 채우세요."
fi
chmod 600 edge/config.toml  # 장치 토큰이 들어 있으므로 본인만 읽게

echo "[4/6] 사용자 서비스 등록 (fall-edge)"
# 오디오(PipeWire)가 사용자 세션에서 동작하므로 시스템 서비스가 아니라 사용자 서비스로 실행한다.
UNIT_DIR="$HOME/.config/systemd/user"
mkdir -p "$UNIT_DIR"
sed "s#__INSTALL_DIR__#$INSTALL_DIR#g" "$DEPLOY/fall-edge.service" > "$UNIT_DIR/fall-edge.service"
systemctl --user daemon-reload
systemctl --user enable fall-edge.service
# 로그인하지 않아도(재부팅 직후 등) 사용자 서비스가 시작되게 한다.
sudo loginctl enable-linger "$USER"
# 로그가 저장 공간을 다 쓰지 않게 제한한다.
sudo mkdir -p /etc/systemd/journald.conf.d
printf '[Journal]\nSystemMaxUse=200M\n' | sudo tee /etc/systemd/journald.conf.d/fall-edge.conf >/dev/null
sudo systemctl restart systemd-journald

echo "[5/6] 키오스크 화면 자동 실행"
chmod +x "$DEPLOY/kiosk.sh"
mkdir -p "$HOME/.config/autostart"
cat > "$HOME/.config/autostart/fall-kiosk.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=Fall detection kiosk
Exec=$DEPLOY/kiosk.sh
X-GNOME-Autostart-enabled=true
DESKTOP

echo "[6/6] 화면 꺼짐 방지"
sudo raspi-config nonint do_blanking 1 || echo "  raspi-config로 화면 꺼짐을 끄지 못했습니다. 설정 > Display에서 끄세요."

cat <<MSG

설치 완료. 다음 순서로 진행하세요.
  1. edge/config.toml 편집: [server] address(PC 서버 IP), token(서버와 같은 장치 토큰), [device] name
  2. 장치 확인:    .venv/bin/python -m edge --list-devices
  3. 서버 주소 확인: curl \$(.venv/bin/python -m edge --print-url health)
  4. 서비스 시작:  systemctl --user start fall-edge
  5. 재부팅 후 화면(키오스크)과 서비스가 자동으로 뜨는지 확인
로그 보기: journalctl --user -u fall-edge -f
MSG
