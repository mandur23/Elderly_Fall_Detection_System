#!/bin/bash
# 라즈베리파이 한 줄 설치. 서버 관리자 화면 '라즈베리파이 추가'의 명령을 라즈베리파이 터미널에 붙여 넣는다:
#   curl -fsSL http://<서버>:8000/edge/setup.sh | bash -s -- --token '<장치 토큰>' --name '거실'
# 서버(GET /edge/setup.sh)가 아래 SERVER_URL 줄의 자리표시자를 자기 LAN 주소(예: http://192.168.0.10:8000)로 바꿔 보낸다.
#
# 하는 일: 서버에서 edge 코드 묶음(GET /edge/bundle.tar.gz, 장치 토큰 필요)을 받아 검사한 뒤 설치 폴더(기본 ~/fall-edge)에
#          풀고, edge/deploy/install.sh를 설정 값과 함께 실행한다(패키지·가상환경·설정·서비스·키오스크·자동 로그인).
# 다시 실행하면 업데이트: edge/config.toml과 .venv는 그대로 두고 코드만 바꾼다(주지 않은 장치 id·이름도 그대로).
# 옵션: --token 토큰(없으면 물어봄)  --name 설치 위치(기본: 장치 id)  --id 장치 id(기본: 컴퓨터 이름)
#       --dir 설치 폴더(기본 ~/fall-edge)  --reboot(끝나면 다시 시작)  --server URL(서버 주소를 직접 줄 때)
#
# 파이프(curl | bash)로 받는 도중에 끊겨도 반쯤 실행되지 않게 모든 내용을 함수에 넣고 맨 끝에서 부른다.
set -euo pipefail

SERVER_URL="__SERVER_URL__"

die() {
  echo "설치 중단: $*" >&2
  exit 1
}

usage() {
  cat <<'USAGE'
사용법: curl -fsSL http://<서버>:8000/edge/setup.sh | bash -s -- --token '<장치 토큰>' [옵션]
  --token 토큰   서버의 장치 토큰 (생략하면 입력을 받음. 화면에 보이지 않음)
  --name 이름    설치 위치 이름 (예: 거실. 기본: 장치 id)
  --id 장치id    장치 구분자. 영문·숫자·. _ - (기본: 컴퓨터 이름)
  --dir 폴더     설치 폴더 (기본: ~/fall-edge)
  --reboot       설치가 끝나면 바로 다시 시작해 화면을 띄운다
  --server URL   서버 주소 (기본: 이 스크립트를 내려 준 서버)
다시 실행하면 업데이트합니다 (edge/config.toml과 .venv는 그대로).
USAGE
}

need_value() {
  [ $# -ge 2 ] || die "$1 뒤에 값이 필요합니다 (--help 참고)"
}

# 컴퓨터 이름을 장치 id로 쓸 수 있게 바꾼다 (소문자, 영문·숫자·. _ - 외에는 -)
default_id() {
  local h
  h="$(hostname 2>/dev/null || cat /etc/hostname 2>/dev/null || true)"
  h="$(printf '%s' "$h" | tr '[:upper:]' '[:lower:]' | tr -c 'a-z0-9._-' '-' | sed -e 's/^[-.]*//' -e 's/[-.]*$//')"
  printf '%s' "${h:-raspberrypi}"
}

# 터미널(/dev/tty)을 쓸 수 있는지. 표준 입력은 curl 파이프라서 입력은 /dev/tty로 받는다.
have_tty() {
  (exec </dev/tty) 2>/dev/null
}

# 서버 URL에서 install.sh에 넘길 서버 주소: http면 '호스트:포트'(포트가 없으면 80), https면 URL 그대로(포트 443).
server_address() {
  local url="$1" scheme hostport port
  scheme="${url%%://*}"
  hostport="${url#*://}"
  hostport="${hostport%%/*}"
  port=80
  [ "$scheme" = https ] && port=443
  case "$hostport" in
    \[*\]:*) ;;                          # [IPv6]:포트
    \[*\]) hostport="$hostport:$port" ;;
    *:*) ;;
    *) hostport="$hostport:$port" ;;
  esac
  if [ "$scheme" = https ]; then
    printf 'https://%s' "$hostport"
  else
    printf '%s' "$hostport"
  fi
}

# 묶음을 받는다. 토큰이 명령행(ps)에 보이지 않게 curl 설정을 표준 입력(-K -)으로 넘긴다.
download_bundle() {
  local dest="$1" escaped code
  escaped="${TOKEN//\\/\\\\}"
  escaped="${escaped//\"/\\\"}"
  if ! code="$(printf 'header = "Authorization: Bearer %s"\n' "$escaped" \
      | curl -sS -K - --connect-timeout 10 --max-time 300 -o "$dest" -w '%{http_code}' "$SERVER_URL/edge/bundle.tar.gz")"; then
    die "서버에 연결할 수 없습니다 ($SERVER_URL). 서버 PC가 켜져 있는지, 라즈베리파이와 같은 공유기(네트워크)에 있는지, 방화벽이 포트를 막지 않는지 확인하세요."
  fi
  case "$code" in
    200) ;;
    401|403) die "서버가 장치 토큰을 거절했습니다 (HTTP $code). 관리자 화면의 '라즈베리파이 추가'에서 명령을 다시 복사하세요." ;;
    404) die "서버가 edge 설치 묶음을 제공하지 않습니다 (HTTP 404). 서버를 최신 버전으로 업데이트하세요." ;;
    503) die "서버에 edge 설치 묶음이 준비되어 있지 않습니다 (HTTP 503). 서버 로그를 확인하세요." ;;
    *) die "설치 묶음을 받지 못했습니다 (HTTP $code)." ;;
  esac
}

# 묶음 항목 검사: edge/·shared/ 아래 상대 경로의 일반 파일·폴더만 허용한다.
check_bundle() {
  local bundle="$1" listing bad types
  listing="$(tar -tzf "$bundle")" || die "설치 묶음이 손상되었습니다. 다시 실행해 보세요."
  [ -n "$listing" ] || die "설치 묶음이 비어 있습니다."
  bad="$(printf '%s\n' "$listing" | grep -Ev '^(\./)?(edge|shared)(/|$)' || true)"
  [ -z "$bad" ] || die "설치 묶음에 허용되지 않은 경로가 있습니다: $(printf '%s' "$bad" | head -n 3 | tr '\n' ' ')"
  if printf '%s\n' "$listing" | grep -Eq '(^|/)\.\.(/|$)'; then
    die "설치 묶음에 '..' 경로가 있습니다."
  fi
  # tar -tv의 첫 글자: '-' 일반 파일, 'd' 폴더 (l 링크, h 하드 링크, c/b 장치 등은 거부)
  types="$(tar -tvzf "$bundle" | cut -c1 | sort -u | tr -d '\n')"
  case "$types" in
    *[!-d]*) die "설치 묶음에 파일·폴더가 아닌 항목(링크 등)이 있습니다." ;;
  esac
}

main() {
  local token="" name="" device_id="" dir="" reboot=0
  while [ $# -gt 0 ]; do
    case "$1" in
      --token) need_value "$@"; token="$2"; shift 2 ;;
      --token=*) token="${1#*=}"; shift ;;
      --name) need_value "$@"; name="$2"; shift 2 ;;
      --name=*) name="${1#*=}"; shift ;;
      --id) need_value "$@"; device_id="$2"; shift 2 ;;
      --id=*) device_id="${1#*=}"; shift ;;
      --dir) need_value "$@"; dir="$2"; shift 2 ;;
      --dir=*) dir="${1#*=}"; shift ;;
      --server) need_value "$@"; SERVER_URL="$2"; shift 2 ;;
      --server=*) SERVER_URL="${1#*=}"; shift ;;
      --reboot) reboot=1; shift ;;
      -h|--help) usage; exit 0 ;;
      *) die "알 수 없는 옵션: $1 (--help 참고)" ;;
    esac
  done

  [ "$(id -u)" -ne 0 ] || die "root(sudo)가 아닌 일반 사용자로 실행하세요: curl ... | bash -s -- --token ... (sudo bash가 아님)"
  command -v curl >/dev/null || die "curl이 없습니다: sudo apt install curl"
  command -v tar >/dev/null || die "tar가 없습니다: sudo apt install tar"

  SERVER_URL="${SERVER_URL%/}"
  case "$SERVER_URL" in
    __*__) die "서버 주소를 모릅니다. 서버에서 받은 명령(curl .../edge/setup.sh | bash ...)으로 실행하거나 --server http://<서버>:8000 을 주세요." ;;
    http://?*|https://?*) ;;
    *) die "서버 주소는 http:// 또는 https:// 로 시작해야 합니다: $SERVER_URL" ;;
  esac

  if [ -z "$token" ]; then
    have_tty || die "장치 토큰이 필요합니다: ... | bash -s -- --token '<장치 토큰>'"
    printf '장치 토큰을 입력하세요 (화면에 보이지 않음): ' >/dev/tty
    IFS= read -r -s token </dev/tty || true
    printf '\n' >/dev/tty
    [ -n "$token" ] || die "장치 토큰이 비어 있습니다."
  fi
  case "$token" in
    *[[:space:]]*) die "장치 토큰에 공백이나 줄바꿈이 있으면 안 됩니다. 명령을 다시 복사하세요." ;;
  esac
  TOKEN="$token"

  if [ -n "$device_id" ] && ! [[ "$device_id" =~ ^[A-Za-z0-9._-]{1,64}$ ]]; then
    die "장치 id는 영문·숫자·점(.)·밑줄(_)·하이픈(-)으로 64자까지 쓸 수 있습니다: $device_id"
  fi

  dir="${dir:-$HOME/fall-edge}"
  dir="${dir/#\~/$HOME}"
  case "$dir" in
    *[[:space:]]*) die "설치 폴더 경로에 공백이 있으면 안 됩니다: $dir" ;;
  esac
  mkdir -p "$dir" || die "설치 폴더를 만들 수 없습니다: $dir"
  dir="$(cd "$dir" && pwd)"
  [ ! -e "$dir/.git" ] || die "$dir 는 git 저장소입니다. 저장소에서는 git pull 뒤 bash edge/deploy/install.sh 를 쓰거나, --dir로 다른 폴더를 주세요."

  # 처음 설치할 때만 장치 id·이름의 기본값을 쓴다. 다시 실행(업데이트)하면 주지 않은 값은 기존 설정을 그대로 둔다.
  local note=""
  if [ -f "$dir/edge/config.toml" ]; then
    note=" (이미 설치됨: 코드만 바꾸고 설정은 유지)"
  else
    device_id="${device_id:-$(default_id)}"
    name="${name:-$device_id}"
  fi

  echo "낙상 감지 edge 설치"
  echo "  서버:      $SERVER_URL"
  echo "  설치 폴더: $dir$note"
  [ -z "$device_id" ] || echo "  장치 id:   $device_id"
  [ -z "$name" ] || echo "  설치 위치: $name"

  local tmp
  tmp="$(mktemp -d)"
  # shellcheck disable=SC2064  # 지금의 경로로 지운다
  trap "rm -rf '$tmp'" EXIT

  echo "[설치 묶음] 서버에서 받는 중..."
  download_bundle "$tmp/bundle.tar.gz"
  check_bundle "$tmp/bundle.tar.gz"
  mkdir "$tmp/files"
  # --touch: 묶음의 파일 시각(0)을 쓰지 않는다. edge/config.toml은 묶음에 있어도 풀지 않는다.
  tar -xzf "$tmp/bundle.tar.gz" -C "$tmp/files" --no-same-owner --touch \
    --exclude='edge/config.toml' --exclude='./edge/config.toml' || die "설치 묶음을 풀 수 없습니다."
  [ -f "$tmp/files/edge/deploy/install.sh" ] || die "설치 묶음에 edge/deploy/install.sh가 없습니다."
  local version=""
  if [ -f "$tmp/files/edge/VERSION" ]; then
    version="$(head -n 1 "$tmp/files/edge/VERSION" | tr -d '\r')"
    mv "$tmp/files/edge/VERSION" "$tmp/VERSION"
  fi

  # 실행 중인 스크립트(kiosk.sh 등)가 깨지지 않게 파일을 덮어쓰지 않고 지운 뒤 새로 만든다(--remove-destination).
  # edge/config.toml(묶음에서 뺌)과 .venv는 건드리지 않는다.
  local part
  for part in edge shared; do
    if [ -d "$tmp/files/$part" ]; then
      cp -R --remove-destination "$tmp/files/$part" "$dir/" || die "파일을 $dir 에 쓸 수 없습니다."
    fi
  done
  chmod +x "$dir"/edge/deploy/*.sh
  # 버전은 코드를 다 바꾼 뒤에 쓴다. 이 파일이 있으면 서비스가 시작할 때 서버 버전을 보고 자동 업데이트한다.
  if [ -f "$tmp/VERSION" ]; then
    cp --remove-destination "$tmp/VERSION" "$dir/edge/VERSION"
    echo "  설치한 버전: $version"
  else
    echo "  묶음에 VERSION이 없어 자동 업데이트는 꺼집니다."
  fi

  local args=(--server "$(server_address "$SERVER_URL")" --token "$TOKEN")
  [ -z "$device_id" ] || args+=(--id "$device_id")
  [ -z "$name" ] || args+=(--name "$name")
  [ "$reboot" = 0 ] || args+=(--reboot)
  # install.sh가 sudo 암호 등을 물을 수 있게 터미널을 입력으로 준다(표준 입력은 curl 파이프).
  local input=/dev/null
  if have_tty; then input=/dev/tty; fi
  bash "$dir/edge/deploy/install.sh" "${args[@]}" <"$input"
}

main "$@"
