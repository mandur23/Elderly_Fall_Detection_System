"""서버 PC 자동 환경 설정. run_server.py --auto(= run_server.bat)가 쓴다.

한 번 해 둔 일은 다시 하지 않으므로 매번 실행해도 빠르다.
1. 패키지 설치: 가상환경(.venv)에 server/requirements.txt를 설치한다. NVIDIA GPU가 있으면
   GPU용 PyTorch를 먼저 깔아 YOLO-Pose가 GPU로 돈다. 목록이 바뀌었을 때만 다시 설치한다.
2. mkcert 준비: 없으면 winget·brew로 설치하거나 공식 배포 파일을 tools/에 내려받는다.
3. HTTPS 인증서: 없거나 지금 Wi-Fi 주소가 빠져 있으면 새로 만든다(휴대폰용 rootCA.pem도 data/에 복사).
4. Windows 방화벽: 관리자 권한이 있으면 서버 포트를 개인 네트워크에 연다.

이 파일은 패키지를 설치하기 전에 import되므로 맨 위에서는 표준 라이브러리만 쓴다.
"""

import hashlib
import ipaddress
import os
import platform
import shutil
import stat
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import List, Optional

ROOT = Path(__file__).resolve().parent
REQUIREMENTS = ROOT / "server" / "requirements.txt"
TOOLS_DIR = ROOT / "tools"

# --fake(모델 없이 시험)에 필요한 패키지만
FAKE_PACKAGES = ["fastapi", "uvicorn[standard]", "python-multipart", "bcrypt", "pywebpush", "numpy",
                 "opencv-python-headless"]
# NVIDIA GPU용 PyTorch. CUDA 12.4 빌드는 최근 드라이버면 대부분 돈다.
TORCH_CUDA_INDEX = "https://download.pytorch.org/whl/cu124"
MKCERT_VERSION = "v1.4.4"
FIREWALL_RULE = "Fall Detection Server"


def say(message: str = "") -> None:
    print(message, flush=True)


def run(cmd: List[str], **kwargs) -> subprocess.CompletedProcess:
    say("  > " + " ".join(cmd))
    return subprocess.run(cmd, **kwargs)


def in_venv() -> bool:
    return sys.prefix != sys.base_prefix


# ---------------------------------------------------------------------------
# 1. 패키지
# ---------------------------------------------------------------------------

def has_nvidia_gpu() -> bool:
    smi = shutil.which("nvidia-smi")
    if smi is None:
        return False
    return subprocess.run([smi, "-L"], capture_output=True).returncode == 0


def _torch_has_cuda() -> Optional[bool]:
    """설치된 PyTorch가 GPU용인지. 설치되어 있지 않으면 None."""
    result = subprocess.run([sys.executable, "-c", "import torch; print(torch.version.cuda or '')"],
                            capture_output=True, text=True)
    if result.returncode != 0:
        return None
    return bool(result.stdout.strip())


def ensure_packages(fake: bool) -> bool:
    """필요한 패키지를 설치한다. 같은 목록으로 이미 설치했으면 건너뛴다."""
    if not in_venv():
        say("가상환경(.venv) 밖이라 패키지를 자동 설치하지 않습니다. run_server.bat(또는 run_server.sh)로 실행하세요.")
        return True
    gpu = (not fake) and has_nvidia_gpu()
    wanted = "\n".join(FAKE_PACKAGES) if fake else REQUIREMENTS.read_text(encoding="utf-8")
    digest = hashlib.sha256(f"{wanted}\ngpu={gpu}".encode()).hexdigest()
    marker = Path(sys.prefix) / ("fall_packages_fake.sha256" if fake else "fall_packages.sha256")
    if marker.exists() and marker.read_text().strip() == digest:
        return True

    say("[설정] 패키지를 설치합니다. 처음에는 모델 패키지 때문에 몇 분 걸릴 수 있습니다.")
    pip = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check"]
    run([sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--upgrade", "pip"])
    if gpu and not _torch_has_cuda():
        # ultralytics가 일반 PyTorch(CPU용)를 먼저 깔지 않도록 GPU용을 먼저 설치한다.
        say("[설정] NVIDIA GPU를 찾았습니다. GPU용 PyTorch를 설치합니다.")
        if run(pip + ["torch", "torchvision", "--index-url", TORCH_CUDA_INDEX]).returncode != 0:
            say("  GPU용 PyTorch 설치에 실패했습니다. CPU로 계속합니다(느릴 수 있음).")
    args = pip + (FAKE_PACKAGES if fake else ["-r", str(REQUIREMENTS)])
    if run(args).returncode != 0:
        say("패키지 설치에 실패했습니다. 인터넷 연결을 확인하고 다시 실행하세요.")
        return False
    marker.write_text(digest)
    return True


# ---------------------------------------------------------------------------
# 2. mkcert
# ---------------------------------------------------------------------------

def _mkcert_name() -> str:
    return "mkcert.exe" if os.name == "nt" else "mkcert"


def find_mkcert() -> Optional[str]:
    candidates = [shutil.which("mkcert"), str(TOOLS_DIR / _mkcert_name())]
    if os.name == "nt":
        # winget으로 막 설치하면 이 창의 PATH에는 아직 없다.
        local = os.getenv("LOCALAPPDATA", "")
        candidates.append(str(Path(local) / "Microsoft" / "WinGet" / "Links" / "mkcert.exe"))
    for path in candidates:
        if path and Path(path).is_file():
            return path
    return None


def _mkcert_download_url() -> Optional[str]:
    system = {"Windows": "windows", "Darwin": "darwin", "Linux": "linux"}.get(platform.system())
    machine = platform.machine().lower()
    arch = {"amd64": "amd64", "x86_64": "amd64", "arm64": "arm64", "aarch64": "arm64"}.get(machine)
    if system is None or arch is None:
        return None
    suffix = ".exe" if system == "windows" else ""
    name = f"mkcert-{MKCERT_VERSION}-{system}-{arch}{suffix}"
    return f"https://github.com/FiloSottile/mkcert/releases/download/{MKCERT_VERSION}/{name}"


def install_mkcert() -> Optional[str]:
    say("[설정] 인증서 도구(mkcert)를 설치합니다.")
    if os.name == "nt" and shutil.which("winget"):
        run(["winget", "install", "-e", "--id", "FiloSottile.mkcert",
             "--accept-package-agreements", "--accept-source-agreements"])
    elif platform.system() == "Darwin" and shutil.which("brew"):
        run(["brew", "install", "mkcert"])
    found = find_mkcert()
    if found:
        return found
    url = _mkcert_download_url()
    if url is None:
        say("  이 컴퓨터용 mkcert를 찾지 못했습니다. https://github.com/FiloSottile/mkcert 에서 직접 설치하세요.")
        return None
    TOOLS_DIR.mkdir(exist_ok=True)
    target = TOOLS_DIR / _mkcert_name()
    say(f"  내려받는 중: {url}")
    try:
        urllib.request.urlretrieve(url, target)
    except OSError as e:
        say(f"  내려받지 못했습니다: {e}")
        return None
    target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return str(target)


# ---------------------------------------------------------------------------
# 3. HTTPS 인증서
# ---------------------------------------------------------------------------

def cert_names(cert_path: Path) -> Optional[set]:
    """인증서에 들어 있는 주소들. 읽을 수 없으면 None."""
    try:
        from cryptography import x509
        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        return {str(ip) for ip in san.get_values_for_type(x509.IPAddress)} | set(san.get_values_for_type(x509.DNSName))
    except Exception:
        return None


def cert_covers(cert_path: Path, ip: str) -> bool:
    """인증서가 지금 Wi-Fi 주소를 포함하는지. 공유기에서 주소가 바뀌면 휴대폰에 인증서 경고가 뜬다."""
    names = cert_names(cert_path)
    if names is None:
        return True  # 확인할 수 없으면 경고하지 않는다
    return str(ipaddress.ip_address(ip)) in names


def make_cert(data_dir: Path, ip: str, mkcert: Optional[str] = None) -> bool:
    """mkcert로 localhost와 이 PC의 Wi-Fi 주소용 인증서를 만들고, 휴대폰에 설치할 rootCA.pem을 복사해 둔다."""
    mkcert = mkcert or find_mkcert()
    if mkcert is None:
        say("mkcert가 없습니다. run_server.bat으로 실행하면 자동으로 설치합니다.")
        return False
    say("[설정] HTTPS 인증서를 만듭니다. 처음 한 번은 '인증서를 설치할까요?' 창이 뜨면 '예'를 누르세요.")
    if run([mkcert, "-install"]).returncode != 0:
        say("  mkcert -install에 실패했습니다. 인증서는 만들지만 이 PC 브라우저에 경고가 뜰 수 있습니다.")
    names = ["localhost", "127.0.0.1", "::1"] + ([ip] if ip != "127.0.0.1" else [])
    result = run([mkcert, "-cert-file", str(data_dir / "cert.pem"), "-key-file", str(data_dir / "key.pem")] + names)
    if result.returncode != 0:
        say("  인증서를 만들지 못했습니다.")
        return False
    caroot = subprocess.run([mkcert, "-CAROOT"], capture_output=True, text=True).stdout.strip()
    if caroot and (Path(caroot) / "rootCA.pem").exists():
        shutil.copy(Path(caroot) / "rootCA.pem", data_dir / "rootCA.pem")
    say("")
    say(f"  ★ 휴대폰에 {data_dir / 'rootCA.pem'} 을 설치해야 휴대폰 카메라·알림이 됩니다.")
    say("    방법: docs/PHONE_ACCESS.md '2. 휴대폰에 인증서 설치' (휴대폰마다 한 번)")
    say("")
    return True


def ensure_cert(data_dir: Path, ip: str) -> None:
    """인증서가 없거나 지금 주소가 빠져 있으면 새로 만든다. 실패해도 HTTP로는 켤 수 있으니 멈추지 않는다."""
    cert, key = data_dir / "cert.pem", data_dir / "key.pem"
    if cert.exists() and key.exists() and (ip == "127.0.0.1" or cert_covers(cert, ip)):
        return
    if cert.exists():
        say(f"[설정] 서버 PC 주소가 {ip}(으)로 바뀌어 인증서를 다시 만듭니다. 휴대폰 쪽은 다시 설치할 필요 없습니다.")
    mkcert = find_mkcert() or install_mkcert()
    if mkcert:
        make_cert(data_dir, ip, mkcert)


# ---------------------------------------------------------------------------
# 4. Windows 방화벽
# ---------------------------------------------------------------------------

def ensure_firewall(port: int) -> None:
    """휴대폰·Pi가 접속할 수 있게 서버 포트를 개인 네트워크에 연다. 관리자 권한이 없으면 안내만 한다."""
    if os.name != "nt":
        return
    netsh = ["netsh", "advfirewall", "firewall"]
    shown = subprocess.run(netsh + ["show", "rule", f"name={FIREWALL_RULE}"], capture_output=True)
    if shown.returncode == 0:
        return
    added = subprocess.run(netsh + ["add", "rule", f"name={FIREWALL_RULE}", "dir=in", "action=allow",
                                    "protocol=TCP", f"localport={port}", "profile=private"], capture_output=True)
    if added.returncode == 0:
        say(f"[설정] Windows 방화벽에서 {port}번 포트를 개인 네트워크에 열었습니다.")
    else:
        say("[안내] 처음 실행하면 Windows 방화벽 창이 뜹니다. '개인 네트워크'에 체크하고 '액세스 허용'을 누르세요.")
        say("       (그래도 휴대폰이 접속하지 못하면 Wi-Fi를 '개인 네트워크'로 바꾸세요.)")
