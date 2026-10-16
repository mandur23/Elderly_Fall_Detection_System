"""라즈베리파이(edge) 설치·업데이트 파일을 서버가 직접 내려준다.

라즈베리파이에서 명령 한 줄로 설치하고(GitHub·git 없이), 서버를 업데이트하면 라즈베리파이도 같은 버전으로
따라오게 하기 위함이다(edge/update.py가 시작할 때 /edge/version을 보고 바뀌었으면 묶음을 받는다).

  GET /edge/setup.sh          설치 스크립트 (edge/deploy/setup.sh, __SERVER_URL__을 이 서버 주소로 바꿈)
  GET /edge/version           묶음 버전 (내용의 해시)
  GET /edge/bundle.tar.gz     edge/ + shared/ 코드 묶음 (장치 토큰 필요). edge/VERSION에 버전을 넣는다

묶음에는 시험(tests)·캐시·설정 파일(edge/config.toml)을 넣지 않는다. 서버가 켜질 때 한 번 만든다
(자동 업데이트로 코드가 바뀌면 서버가 다시 켜지므로 새로 만들어진다).
"""

from __future__ import annotations

import gzip
import hashlib
import io
import socket
import tarfile
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from server.config import SERVER_DIR

REPO_DIR = SERVER_DIR.parent
PACKAGES = ("edge", "shared")
SKIP_DIRS = {"tests", "__pycache__"}
SKIP_FILES = {"edge/config.toml", "edge/VERSION"}
SKIP_SUFFIXES = (".pyc", ".pyo")
SETUP_SCRIPT = REPO_DIR / "edge" / "deploy" / "setup.sh"
SERVER_URL_PLACEHOLDER = "__SERVER_URL__"
EXECUTABLE_SUFFIXES = (".sh",)


@dataclass(frozen=True)
class Bundle:
    version: str
    data: bytes          # tar.gz
    files: int


def bundle_files(root: Path = REPO_DIR) -> List[str]:
    """묶음에 넣을 파일 (저장소 기준 상대 경로, '/' 구분, 정렬)."""
    out = []
    for package in PACKAGES:
        for path in (root / package).rglob("*"):
            rel = path.relative_to(root).as_posix()
            if (not path.is_file() or SKIP_DIRS.intersection(path.relative_to(root).parts)
                    or rel in SKIP_FILES or rel.endswith(SKIP_SUFFIXES)):
                continue
            out.append(rel)
    return sorted(out)


def build_bundle(root: Path = REPO_DIR) -> Bundle:
    """edge/ + shared/ 묶음. 버전은 파일 경로와 내용의 해시라 코드가 같으면 늘 같다."""
    files = bundle_files(root)
    contents = [(rel, (root / rel).read_bytes()) for rel in files]
    digest = hashlib.sha256()
    for rel, data in contents:
        digest.update(rel.encode() + b"\0" + hashlib.sha256(data).digest())
    version = digest.hexdigest()[:12]

    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for rel, data in [*contents, ("edge/VERSION", version.encode() + b"\n")]:
            info = tarfile.TarInfo(rel)
            info.size = len(data)
            info.mode = 0o755 if rel.endswith(EXECUTABLE_SUFFIXES) else 0o644
            info.mtime = 0  # 같은 코드면 같은 묶음
            tar.addfile(info, io.BytesIO(data))
    return Bundle(version, gzip.compress(raw.getvalue(), compresslevel=9, mtime=0), len(contents))


def setup_script(server_url: str) -> Optional[str]:
    """라즈베리파이에서 curl로 받아 실행할 설치 스크립트. 파일이 없으면 None."""
    if not SETUP_SCRIPT.exists():
        return None
    # bash가 읽으므로 줄 끝을 LF로 맞춘다(Windows에서 받은 저장소라도).
    text = SETUP_SCRIPT.read_text(encoding="utf-8").replace("\r\n", "\n")
    return text.replace(SERVER_URL_PLACEHOLDER, server_url.rstrip("/"))


def lan_ip() -> str:
    """이 PC의 공유기(LAN) 쪽 IPv4 주소. 실제로 보내지는 않고 경로만 고른다. 모르면 127.0.0.1."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("10.255.255.255", 1))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()
