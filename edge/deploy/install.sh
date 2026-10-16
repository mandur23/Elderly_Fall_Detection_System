#!/bin/bash
# 라즈베리파이 5 (Raspberry Pi OS 64비트 데스크톱, Bookworm 이후) edge 설치 스크립트.
# 두 가지로 쓴다.
#   1) 한 줄 설치: 서버 관리자 화면 '라즈베리파이 추가'의 명령(curl ... /edge/setup.sh | bash -s -- --token ...)이
#      코드를 받아 풀고 이 스크립트를 옵션과 함께 부른다 -> 설정까지 마치고 서비스를 바로 시작한다.
#   2) 저장소를 받은 폴더에서 일반 사용자로:  bash edge/deploy/install.sh [옵션]
#      옵션 없이 실행하면 예시 설정 파일만 만들고 채울 것을 안내한다.
# 옵션: --server 192.168.0.10:8000  --token 장치토큰  --id 장치id  --name 설치위치  --reboot(끝나면 다시 시작)
#
# 하는 일: 시스템 패키지 -> 가상환경 -> 설정 파일 -> 사용자 서비스(fall-edge) -> 키오스크 자동 실행
#          -> 화면 꺼짐 방지·데스크톱 자동 로그인. 다시 실행해도 안전하다(이미 된 단계는 같은 결과가 된다).
# 영상·음성 처리는 PC 서버가 하므로 모델은 내려받지 않는다.
set -euo pipefail

usage() {
  cat <<'USAGE'
사용법: bash edge/deploy/install.sh [--server 주소] [--token 장치토큰] [--id 장치id] [--name 설치위치] [--reboot]
  --server  PC 서버 주소 (예: 192.168.0.10:8000)
  --token   서버의 장치 토큰 (DEVICE_TOKEN)
  --id      장치 구분자 (장치마다 달라야 함. 새로 설치할 때 생략하면 컴퓨터 이름)
  --name    설치 위치 이름 (예: 거실)
  --reboot  설치가 끝나면 바로 다시 시작해 화면을 띄운다
옵션으로 준 값만 edge/config.toml에 저장하고, 다른 설정은 그대로 둔다.
USAGE
}

need_value() {
  if [ $# -lt 2 ]; then
    echo "$1 뒤에 값이 필요합니다 (--help 참고)" >&2
    exit 2
  fi
}

SERVER="" TOKEN="" DEVICE_ID="" NAME="" REBOOT=0
while [ $# -gt 0 ]; do
  case "$1" in
    --server) need_value "$@"; SERVER="$2"; shift 2 ;;
    --server=*) SERVER="${1#*=}"; shift ;;
    --token) need_value "$@"; TOKEN="$2"; shift 2 ;;
    --token=*) TOKEN="${1#*=}"; shift ;;
    --id) need_value "$@"; DEVICE_ID="$2"; shift 2 ;;
    --id=*) DEVICE_ID="${1#*=}"; shift ;;
    --name) need_value "$@"; NAME="$2"; shift 2 ;;
    --name=*) NAME="${1#*=}"; shift ;;
    --reboot) REBOOT=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "알 수 없는 옵션: $1 (--help 참고)" >&2; exit 2 ;;
  esac
done

INSTALL_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$INSTALL_DIR"
DEPLOY="$INSTALL_DIR/edge/deploy"

if [ "$(id -u)" -eq 0 ]; then
  echo "root가 아닌 일반 사용자로 실행하세요 (sudo는 스크립트가 필요할 때 씁니다)." >&2
  exit 1
fi
case "$INSTALL_DIR" in
  *[[:space:]]*)
    echo "설치 폴더 경로에 공백이 있으면 안 됩니다: $INSTALL_DIR" >&2
    exit 1 ;;
esac

echo "[1/7] 시스템 패키지"
sudo apt-get update
# numpy·OpenCV·picamera2는 apt 것을 쓴다(서로 맞춰 빌드되어 있음). pip로 numpy 2.x가 깔리면 picamera2가 깨질 수 있다.
# libportaudio2: sounddevice, fonts-nanum: 현장 화면 한글, curl: 한 줄 설치·서버 확인
sudo apt-get install -y python3-venv python3-picamera2 python3-opencv python3-numpy \
  libportaudio2 alsa-utils curl fonts-nanum
# Chromium 패키지 이름은 OS 버전에 따라 다르다.
sudo apt-get install -y chromium || sudo apt-get install -y chromium-browser

echo "[2/7] 가상환경과 파이썬 패키지"
# apt로 설치한 picamera2·OpenCV를 쓰려고 시스템 패키지를 볼 수 있는 가상환경을 만든다(이미 있으면 그대로 쓴다).
python3 -m venv --system-site-packages .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r edge/deploy/requirements-pi.txt
.venv/bin/python -c "import cv2, numpy, aiohttp, sounddevice; print('  파이썬 패키지 확인: OpenCV', cv2.__version__)"
.venv/bin/python -c "from picamera2 import Picamera2; print('  카메라:', Picamera2.global_camera_info() or '없음 (USB 웹캠이면 [camera] backend = \"opencv\")')" \
  || echo "  picamera2를 불러오지 못했습니다. 카메라 모듈을 쓰려면 확인이 필요합니다."

echo "[3/7] 설정 파일"
SET_ARGS=()
if [ -n "$SERVER" ]; then SET_ARGS+=(--server "$SERVER"); fi
if [ -n "$TOKEN" ]; then SET_ARGS+=(--token "$TOKEN"); fi
if [ -n "$DEVICE_ID" ]; then SET_ARGS+=(--device-id "$DEVICE_ID"); fi
if [ -n "$NAME" ]; then SET_ARGS+=(--name "$NAME"); fi
if [ ! -f edge/config.toml ]; then
  cp edge/config.example.toml edge/config.toml
  echo "  edge/config.toml 생성됨 (예시에서 복사)"
  # 장치 id를 주지 않았으면 컴퓨터 이름
  if [ -z "$DEVICE_ID" ]; then SET_ARGS+=(--device-id "$(hostname)"); fi
fi
chmod 600 edge/config.toml  # 장치 토큰이 들어 있으므로 본인만 읽게
if [ ${#SET_ARGS[@]} -gt 0 ]; then
  # 한글·공백·따옴표가 든 이름도 TOML 문자열로 바르게 쓰고, 다른 설정과 주석은 그대로 둔다.
  .venv/bin/python -m edge --save-config "${SET_ARGS[@]}"
fi
# 서버 주소와 토큰이 있으면(예시 값이 아니면) 설정이 된 것으로 본다.
if .venv/bin/python -m edge --print-url kiosk >/dev/null 2>&1; then
  CONFIG_OK=1
  echo "  서버: $(.venv/bin/python -m edge --print-url http)"
else
  CONFIG_OK=0
  echo "  서버 주소(address)와 장치 토큰(token)을 edge/config.toml에 채워야 합니다."
fi

echo "[4/7] 사용자 서비스 등록 (fall-edge)"
# 오디오(PipeWire)가 사용자 세션에서 동작하므로 시스템 서비스가 아니라 사용자 서비스로 실행한다.
UNIT_DIR="$HOME/.config/systemd/user"
mkdir -p "$UNIT_DIR"
sed "s#__INSTALL_DIR__#$INSTALL_DIR#g" "$DEPLOY/fall-edge.service" > "$UNIT_DIR/fall-edge.service"
systemctl --user daemon-reload
systemctl --user enable fall-edge.service
# 로그인하지 않아도(재부팅 직후 등) 사용자 서비스가 시작되게 한다.
sudo loginctl enable-linger "$USER"
# 로그가 저장 공간을 다 쓰지 않게 제한한다(내용이 같으면 그대로 둔다).
JOURNAL_CONF=/etc/systemd/journald.conf.d/fall-edge.conf
if [ "$(cat "$JOURNAL_CONF" 2>/dev/null || true)" != "$(printf '[Journal]\nSystemMaxUse=200M')" ]; then
  sudo mkdir -p /etc/systemd/journald.conf.d
  printf '[Journal]\nSystemMaxUse=200M\n' | sudo tee "$JOURNAL_CONF" >/dev/null
  sudo systemctl restart systemd-journald
fi
if [ "$CONFIG_OK" = 1 ]; then
  # 이미 실행 중이면 새 설정·코드로 다시 시작한다(시작 전에 서버 버전 확인: python -m edge.update).
  if systemctl --user restart fall-edge.service; then
    echo "  서비스 시작됨 (로그: journalctl --user -u fall-edge -f)"
  else
    echo "  서비스를 시작하지 못했습니다. journalctl --user -u fall-edge 로 확인하세요."
  fi
fi

echo "[5/7] 키오스크 화면 자동 실행"
# 데스크톱이 뜨면 ~/.config/autostart의 항목을 실행한다(X11·Wayland 모두).
# ~/.config/labwc/autostart는 만들지 않는다: 사용자 파일이 있으면 시스템 기본 자동 실행(작업 표시줄 등)을 대신해 버린다.
chmod +x "$DEPLOY/kiosk.sh"
mkdir -p "$HOME/.config/autostart"
cat > "$HOME/.config/autostart/fall-kiosk.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=Fall detection kiosk
Exec=$DEPLOY/kiosk.sh
Terminal=false
X-GNOME-Autostart-enabled=true
DESKTOP

echo "[6/7] 화면 꺼짐 방지, 데스크톱 자동 로그인"
if command -v raspi-config >/dev/null; then
  sudo raspi-config nonint do_blanking 1 || echo "  화면 꺼짐을 끄지 못했습니다. sudo raspi-config → Display Options → Screen Blanking에서 끄세요."
  # 켜자마자 로그인 화면 없이 데스크톱으로 들어가야 키오스크가 뜬다(B4: Desktop Autologin).
  sudo raspi-config nonint do_boot_behaviour B4 \
    || echo "  데스크톱 자동 로그인을 켜지 못했습니다. sudo raspi-config → System Options → Boot / Auto Login → Desktop Autologin"
else
  echo "  raspi-config가 없습니다. 화면 꺼짐 방지와 데스크톱 자동 로그인을 직접 설정하세요."
fi

echo "[7/7] 마무리"
if [ "$CONFIG_OK" = 1 ]; then
  if [ "$REBOOT" = 1 ]; then
    echo "  설치 완료. 5초 뒤 다시 시작합니다. 켜지면 대기 화면 -> 서버에 연결되면 현장 화면이 바로 뜹니다."
    sleep 5
    sudo reboot
    exit 0
  fi
  cat <<MSG

설치 완료. 다시 시작하면 화면이 바로 뜹니다(대기 화면 -> 서버에 연결되면 현장 화면):  sudo reboot
  장치 확인:    .venv/bin/python -m edge --list-devices
  서버 확인:    curl \$(.venv/bin/python -m edge --print-url health)
  로그 보기:    journalctl --user -u fall-edge -f
설치 폴더: $INSTALL_DIR
MSG
else
  cat <<MSG

설치 완료. 다음 순서로 진행하세요.
  1. 설정: bash edge/deploy/install.sh --server <PC 서버 IP:8000> --token <장치 토큰> --name <설치 위치>
     (또는 edge/config.toml을 직접 편집: [server] address, token, [device] name)
  2. 장치 확인:    .venv/bin/python -m edge --list-devices
  3. 서버 주소 확인: curl \$(.venv/bin/python -m edge --print-url health)
  4. 서비스 시작:  systemctl --user restart fall-edge
  5. 재부팅(sudo reboot) 후 화면(키오스크)과 서비스가 자동으로 뜨는지 확인
로그 보기: journalctl --user -u fall-edge -f
MSG
fi
