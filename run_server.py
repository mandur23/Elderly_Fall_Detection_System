"""중앙 서버 통합 실행 파일 (서버 + 웹 + 관리자 화면).

    run_server.bat                    # Windows: 더블클릭하면 환경 설정부터 실행까지 자동 (= --auto)
    python run_server.py              # 실제 모델(YOLO-Pose, Whisper)로 실행
    python run_server.py --fake       # 장비·모델 없이 가짜 자세 추정·가짜 음성 인식으로 실행
    python run_server.py --make-cert  # 휴대폰용 HTTPS 인증서를 다시 만든 뒤 실행 (mkcert 필요)
    python run_server.py --local      # 이 PC에서만 접속(https://localhost:8443). 휴대폰·Pi는 접속할 수 없음
    python run_server.py --local --http  # 이 PC에서 HTTP로(http://localhost:8443). localhost는 HTTP여도 카메라가 된다

실행하면 다음을 알아서 한다.
1. 필요한 패키지가 있는지 확인한다. --auto면 가상환경에 설치하고, mkcert·인증서·방화벽까지
   준비한다(auto_setup.py). 아니면 설치 명령을 알려 주고 멈춘다.
2. data/cert.pem, data/key.pem이 있으면 HTTPS로 켠다. 휴대폰 카메라·마이크·웹 푸시는 HTTPS에서만 된다.
3. 웹 푸시 키(VAPID)가 없으면 data/에 한 번 만들어 두고 계속 쓴다.
4. 휴대폰과 Pi가 접속할 주소를 화면에 보여 준다.
5. 서버가 뜨면 이 PC의 브라우저로 관리자 화면(localhost/admin)을 연다.

웹 화면은 같은 서버가 함께 제공하므로 웹 서버를 따로 켤 필요가 없다.
환경변수(FALL_*)를 직접 지정하면 그 값이 우선한다. 자세한 설정은 server/README.md 참고.
"""

import argparse
import base64
import importlib.util
import os
import socket
import ssl
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

import auto_setup

ROOT = Path(__file__).resolve().parent

# 서버를 띄우는 데 꼭 필요한 패키지 (import 이름 -> pip 이름)
REQUIRED = {"fastapi": "fastapi", "uvicorn": "uvicorn", "multipart": "python-multipart", "bcrypt": "bcrypt",
            "pywebpush": "pywebpush", "numpy": "numpy", "cv2": "opencv-python-headless"}
# 실제 모델. --fake면 없어도 된다.
MODELS = {"ultralytics": "ultralytics", "faster_whisper": "faster-whisper", "sklearn": "scikit-learn", "joblib": "joblib"}


def say(message: str = "") -> None:
    print(message, flush=True)


def lan_ip() -> str:
    """같은 Wi-Fi의 휴대폰·Pi가 이 PC에 접속할 주소. 실제로 패킷을 보내지는 않는다."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("192.0.2.1", 80))  # 문서용 주소: 경로만 고르고 아무것도 보내지 않음
            return s.getsockname()[0]
        except OSError:
            return "127.0.0.1"


def check_packages(fake: bool) -> bool:
    wanted = dict(REQUIRED)
    if not fake:
        wanted.update(MODELS)
    missing = [pip for module, pip in wanted.items() if importlib.util.find_spec(module) is None]
    if not missing:
        return True
    say("필요한 패키지가 없습니다: " + ", ".join(missing))
    say(f"  {sys.executable} -m pip install -r server/requirements.txt")
    if not fake and set(missing) & set(MODELS.values()):
        say("모델 없이 화면만 시험하려면:  python run_server.py --fake")
    return False


def ensure_vapid_keys(data_dir: Path) -> None:
    """웹 푸시 키를 한 번 만들어 data/에 두고 계속 쓴다. 키가 바뀌면 보호자가 알림을 다시 켜야 하기 때문이다."""
    if os.getenv("FALL_VAPID_PRIVATE_KEY") and os.getenv("FALL_VAPID_PUBLIC_KEY"):
        return
    from cryptography.hazmat.primitives import serialization
    from py_vapid import Vapid

    private_path = data_dir / "vapid_private.pem"
    if private_path.exists():
        vapid = Vapid.from_file(str(private_path))
    else:
        vapid = Vapid()
        vapid.generate_keys()
        vapid.save_key(str(private_path))
        say(f"웹 푸시 키를 만들었습니다: {private_path}")
    raw = vapid.public_key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    os.environ["FALL_VAPID_PRIVATE_KEY"] = str(private_path)
    os.environ["FALL_VAPID_PUBLIC_KEY"] = base64.urlsafe_b64encode(raw).decode().rstrip("=")


def open_admin_when_ready(url: str, timeout: float = 120.0) -> None:
    """서버가 응답하기 시작하면 관리자 화면을 연다. 모델을 처음 내려받으면 시간이 걸릴 수 있다."""
    probe = url.replace("/admin", "/api/push/vapid-public-key")
    # 이 PC 안에서 서버가 떴는지만 보는 요청이라 인증서 확인을 하지 않는다(mkcert를 설치하지 않은 경우 대비).
    context = ssl._create_unverified_context()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(probe, timeout=2, context=context)
            webbrowser.open(url)
            return
        except Exception:
            time.sleep(0.5)
    say(f"서버가 {int(timeout)}초 안에 응답하지 않아 관리자 화면을 열지 못했습니다. 직접 여세요: {url}")


def main() -> int:
    parser = argparse.ArgumentParser(description="낙상 감지 중앙 서버 통합 실행")
    parser.add_argument("--fake", action="store_true", help="모델 없이 가짜 자세 추정·가짜 음성 인식으로 실행(시험용)")
    parser.add_argument("--make-cert", action="store_true", help="mkcert로 HTTPS 인증서를 다시 만든 뒤 실행")
    parser.add_argument("--auto", action="store_true",
                        help="패키지 설치, mkcert·인증서, 방화벽까지 자동으로 준비(run_server.bat이 사용)")
    parser.add_argument("--port", type=int, default=int(os.getenv("FALL_PORT", "8443")))
    parser.add_argument("--no-browser", action="store_true", help="관리자 화면을 자동으로 열지 않음")
    parser.add_argument("--http", action="store_true",
                        help="HTTPS 인증서 없이 HTTP로 실행. 휴대폰은 HTTP에서 카메라·마이크·푸시를 못 쓰므로 --local과 함께 쓴다")
    parser.add_argument("--local", action="store_true",
                        help="이 PC에서만 접속(localhost, FALL_HOST=127.0.0.1). 휴대폰·Pi는 접속할 수 없음")
    args = parser.parse_args()
    use_http = args.http
    if args.local:
        os.environ["FALL_HOST"] = "127.0.0.1"
    if use_http:
        # 인증서를 쓰지 않는다. 환경변수로 준 인증서도 끈다.
        os.environ.pop("FALL_SSL_CERT", None)
        os.environ.pop("FALL_SSL_KEY", None)
    # 서버가 듣는 주소(server/config.py의 FALL_HOST). 루프백이면 같은 Wi-Fi의 휴대폰·Pi는 접속할 수 없다.
    local_only = os.getenv("FALL_HOST", "0.0.0.0").strip("[]") in ("127.0.0.1", "localhost", "::1")

    if not (3, 10) <= sys.version_info[:2] <= (3, 13):
        say(f"Python 3.10~3.13이 필요합니다(지금은 {sys.version.split()[0]}). "
            "더 새로운 Python은 numpy·OpenCV·PyTorch의 설치 파일(wheel)이 아직 없어 패키지 설치에 실패합니다.")
        say("해결: .venv 폴더를 지우고 run_server.bat(macOS·Linux는 run_server.sh)을 다시 실행하면 지원되는 Python으로 다시 만듭니다.")
        return 1
    os.chdir(ROOT)  # 어디서 실행해도 web/, data/를 찾도록
    if args.auto and not auto_setup.ensure_packages(args.fake):
        return 1
    if not check_packages(args.fake):
        return 1

    data_dir = Path(os.getenv("FALL_DATA_DIR", ROOT / "data"))
    data_dir.mkdir(parents=True, exist_ok=True)
    ip = lan_ip()
    if args.auto:
        if not os.getenv("FALL_SSL_CERT") and not use_http:
            auto_setup.ensure_cert(data_dir, ip)
        if not local_only:   # 이 PC에서만 접속하면 방화벽을 열 필요가 없다
            auto_setup.ensure_firewall(args.port)
    os.environ["FALL_PORT"] = str(args.port)
    if args.fake:
        os.environ["FALL_POSE_BACKEND"] = "fake"
        os.environ["FALL_STT_BACKEND"] = "fake"

    if args.make_cert and not auto_setup.make_cert(data_dir, ip):
        return 1
    cert, key = data_dir / "cert.pem", data_dir / "key.pem"
    if not use_http and not os.getenv("FALL_SSL_CERT") and cert.exists() and key.exists():
        os.environ["FALL_SSL_CERT"], os.environ["FALL_SSL_KEY"] = str(cert), str(key)
    https = bool(os.getenv("FALL_SSL_CERT") and os.getenv("FALL_SSL_KEY"))
    if not https:
        # HTTP로는 Secure 쿠키가 저장되지 않아 로그인이 안 된다. 이 PC에서 시험만 할 수 있게 끈다.
        os.environ.setdefault("FALL_COOKIE_SECURE", "0")
    ensure_vapid_keys(data_dir)

    scheme = "https" if https else "http"
    admin_url = f"{scheme}://localhost:{args.port}/admin"
    phone_url = f"{scheme}://{ip}:{args.port}"
    say("=" * 64)
    say(" 낙상 감지 중앙 서버")
    say(f"  모드        : {'가짜 모델(시험용)' if args.fake else '실제 모델(YOLO-Pose, Whisper)'}")
    say(f"  관리자 화면 : {admin_url}   (이 PC에서만 열림)")
    if local_only:
        say(f"  접속 주소   : {scheme}://localhost:{args.port}   (이 PC에서만. 휴대폰·Pi는 접속할 수 없음)")
    else:
        say(f"  휴대폰 접속 : {phone_url}   (같은 Wi-Fi·핫스팟)")
        say(f"  Pi 설정     : PI_SERVER_URL={phone_url}")
    if not https and local_only:
        say("  HTTP로 실행합니다. localhost는 HTTP에서도 카메라·마이크를 쓸 수 있습니다.")
    elif not https and use_http:
        say("  ! HTTP로 실행합니다. 휴대폰은 HTTP 주소에서 카메라·마이크·웹 푸시를 쓸 수 없습니다.")
    elif not https:
        say("  ! HTTPS 인증서가 없습니다. 휴대폰 카메라·마이크·웹 푸시가 동작하지 않습니다.")
        say("    run_server.bat(또는 python run_server.py --auto)으로 실행하면 인증서를 자동으로 만듭니다.")
    elif not local_only and ip != "127.0.0.1" and not auto_setup.cert_covers(Path(os.environ["FALL_SSL_CERT"]), ip):
        say(f"  ! 인증서에 지금 주소({ip})가 없습니다. 휴대폰에 경고가 뜨면 --make-cert로 다시 만드세요.")
    say("  끄려면 이 창에서 Ctrl+C")
    say("=" * 64)

    if not args.no_browser:
        threading.Thread(target=open_admin_when_ready, args=(admin_url,), daemon=True).start()

    from server.__main__ import main as run_server
    try:
        run_server()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
