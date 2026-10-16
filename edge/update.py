"""서버에서 edge 코드를 받아 바꾸는 자동 업데이트 (python -m edge.update).

fall-edge 서비스가 시작하기 전에(ExecStartPre=-.../python -m edge.update) 한 번 실행한다. 앞의 '-' 때문에 실패해도
서비스 시작을 막지 않으며, 요청마다 시간 제한을 두고 문제가 있으면 건너뛴다.

- edge/VERSION이 있을 때만 동작한다(서버의 한 줄 설치로 받은 장치). git으로 받은 개발 폴더에는 VERSION이 없어
  절대 바꾸지 않는다. 자동 업데이트를 끄려면 edge/VERSION을 지운다.
- 서버의 GET /edge/version이 이 장치의 VERSION과 다르면 GET /edge/bundle.tar.gz(장치 토큰 필요)를 받아
  모든 항목을 검사한 뒤(일반 파일·폴더만, edge/·shared/ 아래 상대 경로만) 임시 폴더에 풀고, 파일을 하나씩 바꾼다.
  edge/config.toml과 .venv는 건드리지 않고, edge/VERSION은 맨 마지막에 쓴다(중간에 끊기면 다음에 다시 받는다).
- deploy/requirements-pi.txt 내용이 바뀌었으면 파일을 바꾸기 전에 pip로 설치한다. 실패하면 이번 업데이트는 하지 않는다.
- 서버가 응답한 시도마다 시각과 목표 버전을 남긴다(~/.cache/fall-edge/update-attempt.json). client.py는 서버의
  welcome에 실린 edge_version이 다를 때 이 기록을 보고, 같은 버전으로 최근 30분 안에 시도하지 않았을 때만
  종료 코드 3으로 끝낸다(systemd가 다시 띄우며 이 모듈이 업데이트한다. 업데이트가 계속 실패해도 반복해서 끝내지 않게).
"""

import argparse
import http.client
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
import zlib
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from edge.config import DEFAULT_CONFIG_PATH, ConfigError, from_file, load_file, server_urls
from shared import protocol

log = logging.getLogger("edge.update")

INSTALL_DIR = Path(__file__).resolve().parent.parent   # edge/와 shared/가 있는 설치 폴더
VERSION_NAME = "edge/VERSION"
VERSION_FILE = INSTALL_DIR / "edge" / "VERSION"
REQUIREMENTS_NAME = "edge/deploy/requirements-pi.txt"
VERSION_PATH = "/edge/version"
BUNDLE_PATH = "/edge/bundle.tar.gz"
ALLOWED_TOPS = ("edge", "shared")
NEVER_WRITE = frozenset({"edge/config.toml"})   # 장치 설정(토큰). 묶음에 있어도 쓰지 않는다
REQUIRED_FILE = "edge/__main__.py"              # 이게 없는 묶음은 깨진 것으로 본다

RESTART_EXIT_CODE = 3            # 업데이트하려고 끝낼 때의 종료 코드 (fall-edge.service가 다시 띄운다)
RETRY_SECONDS = 30 * 60          # 같은 버전으로 시도한 뒤 이만큼은 다시 끝내지 않는다
REQUEST_TIMEOUT = 10.0           # 요청마다(연결·읽기) 시간 제한
DOWNLOAD_SECONDS = 60.0          # 묶음 받기 전체 시간 제한
PIP_SECONDS = 180.0              # pip 설치 시간 제한 (fall-edge.service의 TimeoutStartSec 안에 끝나게)
MAX_BUNDLE_BYTES = 20 * 1024 * 1024
MAX_UNPACKED_BYTES = 100 * 1024 * 1024
MAX_MEMBERS = 5000
VERSION_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,63}")

# 서버는 같은 공유기 안에 있으므로 환경 변수의 프록시를 쓰지 않는다(edge의 WebSocket 접속과 같게).
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class UpdateError(Exception):
    """이번 업데이트를 하지 못한 이유. 설치 폴더는 바꾸지 않았거나 VERSION을 쓰기 전이다."""


class Unreachable(UpdateError):
    """서버에 닿지 않음 (꺼져 있거나 네트워크가 아직 없음). 시도 기록을 남기지 않는다."""


# --- 버전과 시도 기록 ---
def local_version(path: Path = VERSION_FILE) -> Optional[str]:
    """설치된 edge 버전(edge/VERSION 첫 줄). 파일이 없거나 비었으면 None(개발 폴더: 자동 업데이트 안 함)."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return None
    text = lines[0].strip() if lines else ""
    return text or None


def attempt_path() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "fall-edge" / "update-attempt.json"


def last_attempt(path: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """마지막 업데이트 시도 {"at": 시각(time.time()), "target": 목표 버전, "result": 결과}. 없으면 None."""
    try:
        data = json.loads((path or attempt_path()).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def record_attempt(target: str, result: str, path: Optional[Path] = None, now: Optional[float] = None) -> bool:
    """업데이트 시도를 남긴다. 남기지 못하면 False."""
    path = path or attempt_path()
    at = time.time() if now is None else now
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps({"at": at, "target": target, "result": result}, ensure_ascii=False),
                       encoding="utf-8")
        os.replace(tmp, path)
        return True
    except OSError as e:
        log.warning("업데이트 시도 기록을 남기지 못했습니다 (%s): %s", path, e)
        return False


def restart_reason(server_version: Any, *, version_file: Path = VERSION_FILE, attempt_file: Optional[Path] = None,
                   now: Optional[float] = None, retry_seconds: float = RETRY_SECONDS) -> Optional[str]:
    """서버가 welcome으로 알려 준 edge 버전이 이 장치와 달라, 다시 시작해서(ExecStartPre) 업데이트해야 하면 그 이유.

    VERSION이 없으면(개발 폴더) None. 같은 버전으로 최근 retry_seconds 안에 시도했으면 None(업데이트가 실패해도
    계속 다시 시작하지 않게). 시계가 뒤로 갔으면(기록이 미래) 오래된 기록으로 본다.
    """
    if not isinstance(server_version, str) or not VERSION_RE.fullmatch(server_version.strip()):
        return None
    server_version = server_version.strip()
    local = local_version(version_file)
    if local is None or local == server_version:
        return None
    last = last_attempt(attempt_file)
    if last and last.get("target") == server_version:
        at = last.get("at")
        if isinstance(at, (int, float)) and not isinstance(at, bool):
            age = (time.time() if now is None else now) - at
            if 0 <= age < retry_seconds:
                return None
    return f"서버의 edge 버전({server_version})이 이 장치({local})와 다릅니다"


# --- 서버에서 받기 ---
def _open(url: str, headers: Optional[Dict[str, str]] = None, timeout: float = REQUEST_TIMEOUT):
    request = urllib.request.Request(url, headers={"User-Agent": "fall-edge-update", **(headers or {})})
    return _OPENER.open(request, timeout=timeout)


def fetch_version(base_url: str, timeout: float = REQUEST_TIMEOUT) -> str:
    """서버의 현재 edge 묶음 버전 (GET /edge/version)."""
    try:
        with _open(base_url + VERSION_PATH, timeout=timeout) as res:
            text = res.read(256).decode("utf-8", "replace").strip()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise UpdateError("서버가 edge 자동 업데이트를 지원하지 않습니다 (HTTP 404)") from None
        raise UpdateError(f"서버 버전을 읽지 못했습니다 (HTTP {e.code})") from None
    except (urllib.error.URLError, OSError, http.client.HTTPException) as e:
        reason = getattr(e, "reason", None) or e
        raise Unreachable(f"서버에 연결할 수 없습니다 ({base_url}): {reason}") from None
    if not VERSION_RE.fullmatch(text):
        raise UpdateError(f"서버가 알려 준 버전 형식이 틀립니다: {text[:40]!r}")
    return text


def download_bundle(base_url: str, token: str, dest: Path, *, timeout: float = REQUEST_TIMEOUT,
                    total_seconds: float = DOWNLOAD_SECONDS, max_bytes: int = MAX_BUNDLE_BYTES) -> int:
    """GET /edge/bundle.tar.gz를 dest에 받는다. 받은 바이트 수."""
    deadline = time.monotonic() + total_seconds
    size = 0
    try:
        with _open(base_url + BUNDLE_PATH, protocol.bearer(token), timeout) as res, dest.open("wb") as out:
            while True:
                if time.monotonic() > deadline:
                    raise UpdateError(f"묶음 받기가 {total_seconds:.0f}초 안에 끝나지 않았습니다")
                chunk = res.read(64 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > max_bytes:
                    raise UpdateError(f"묶음이 너무 큽니다 ({max_bytes // (1024 * 1024)}MB 초과)")
                out.write(chunk)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise UpdateError(f"서버가 장치 토큰을 거절했습니다 (HTTP {e.code}). config.toml의 token을 확인하세요") from None
        raise UpdateError(f"묶음을 받지 못했습니다 (HTTP {e.code})") from None
    except (urllib.error.URLError, OSError, http.client.HTTPException) as e:
        raise UpdateError(f"묶음을 받는 중 연결 문제: {getattr(e, 'reason', None) or e}") from None
    return size


# --- 묶음 검사와 풀기 ---
def member_name(member: tarfile.TarInfo) -> str:
    """묶음 항목의 안전한 상대 경로("edge/client.py"). 일반 파일·폴더가 아니거나, 절대 경로이거나, '..'이 있거나,
    edge/·shared/ 밖이면 UpdateError."""
    name = member.name
    if not (member.isfile() or member.isdir()):
        raise UpdateError(f"묶음에 파일·폴더가 아닌 항목(링크 등)이 있습니다: {name!r}")
    if not name or "\\" in name or "\x00" in name or name.startswith("/") or re.match(r"^[A-Za-z]:", name):
        raise UpdateError(f"묶음에 절대 경로이거나 잘못된 이름이 있습니다: {name!r}")
    parts = [p for p in name.split("/") if p not in ("", ".")]
    if not parts or ".." in parts:
        raise UpdateError(f"묶음에 설치 폴더 밖을 가리키는 이름이 있습니다: {name!r}")
    if parts[0] not in ALLOWED_TOPS or (member.isfile() and len(parts) < 2):
        raise UpdateError(f"묶음에 edge/·shared/ 밖의 항목이 있습니다: {name!r}")
    return "/".join(parts)


def _skipped(name: str) -> bool:
    parts = name.split("/")
    return name in NEVER_WRITE or "__pycache__" in parts or name.endswith(".pyc")


def unpack_bundle(bundle: Path, dest: Path, *, max_bytes: int = MAX_UNPACKED_BYTES,
                  max_members: int = MAX_MEMBERS) -> List[str]:
    """묶음의 모든 항목을 먼저 검사한 뒤 dest(임시 폴더)에 푼다. 푼 파일의 상대 경로 목록.

    Python 3.11.2에는 tarfile의 filter=가 없어 tar.extract를 쓰지 않고, 검사한 일반 파일의 내용만 직접 쓴다.
    하나라도 이상하면 아무것도 쓰지 않고 UpdateError.
    """
    try:
        with tarfile.open(bundle, "r:gz") as tar:
            members = tar.getmembers()
            if len(members) > max_members:
                raise UpdateError(f"묶음의 항목이 너무 많습니다 ({len(members)}개)")
            plan = [(m, member_name(m)) for m in members]
            unpacked = sum(m.size for m, _ in plan if m.isfile())
            if unpacked > max_bytes:
                raise UpdateError(f"묶음을 풀면 너무 큽니다 ({unpacked}바이트)")
            files: List[str] = []
            for m, name in plan:
                if _skipped(name):
                    continue
                target = dest.joinpath(*name.split("/"))
                if m.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                source = tar.extractfile(m)
                if source is None:
                    raise UpdateError(f"묶음 항목을 읽을 수 없습니다: {name!r}")
                with source, target.open("wb") as out:
                    shutil.copyfileobj(source, out)
                # 서버(Windows)에서 만든 묶음에는 실행 권한이 없을 수 있어 .sh는 늘 실행 가능하게 한다.
                os.chmod(target, 0o755 if (m.mode & 0o111 or name.endswith(".sh")) else 0o644)
                if name not in files:
                    files.append(name)
    except (tarfile.TarError, OSError, EOFError, zlib.error) as e:
        raise UpdateError(f"묶음을 풀 수 없습니다: {e}") from None
    return files


def _normalized(path: Path) -> Optional[str]:
    """줄바꿈(CRLF/LF)과 앞뒤 공백을 무시한 파일 내용. 없으면 None."""
    try:
        return path.read_bytes().decode("utf-8", "replace").replace("\r\n", "\n").strip()
    except OSError:
        return None


def install_files(stage: Path, install_dir: Path, names: List[str]) -> None:
    """풀어 둔 파일을 설치 폴더로 하나씩 옮긴다. 기존 파일에 덮어쓰지 않고 새 파일로 바꿔(os.replace) 실행 중인
    스크립트(kiosk.sh 등)가 깨지지 않게 한다. edge/VERSION은 여기서 쓰지 않는다(write_version이 맨 마지막에)."""
    for name in names:
        if name == VERSION_NAME or _skipped(name):
            continue
        parts = name.split("/")
        src, dst = stage.joinpath(*parts), install_dir.joinpath(*parts)
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_name(f".{dst.name}.update")
        shutil.copyfile(src, tmp)
        shutil.copymode(src, tmp)
        os.replace(tmp, dst)


def write_version(path: Path, version: str) -> None:
    tmp = path.with_name(f".{path.name}.update")
    tmp.write_bytes(f"{version}\n".encode("utf-8"))
    os.replace(tmp, path)


def run_pip(requirements: Path, install_dir: Path) -> bool:
    """가상환경(.venv)에 requirements를 설치한다. 출력은 그대로 서비스 로그(journal)로 간다."""
    python = install_dir / ".venv" / "bin" / "python"
    if not python.exists():
        python = Path(sys.executable)
    command = [str(python), "-m", "pip", "install", "--disable-pip-version-check", "--timeout", "10",
               "-r", str(requirements)]
    try:
        result = subprocess.run(command, timeout=PIP_SECONDS, check=False)
    except (OSError, subprocess.TimeoutExpired) as e:
        log.error("pip를 실행하지 못했습니다: %s", e)
        return False
    if result.returncode != 0:
        log.error("pip 설치가 실패했습니다 (종료 코드 %s)", result.returncode)
        return False
    return True


def apply_bundle(base_url: str, token: str, install_dir: Path, server_version: str,
                 pip: Callable[[Path, Path], bool] = run_pip) -> str:
    """묶음을 받아 검사하고 설치 폴더의 파일을 바꾼다. 설치한 버전."""
    with tempfile.TemporaryDirectory(prefix="fall-edge-update-") as tmp:
        bundle, stage = Path(tmp) / "bundle.tar.gz", Path(tmp) / "files"
        stage.mkdir()
        size = download_bundle(base_url, token, bundle)
        names = unpack_bundle(bundle, stage)
        if REQUIRED_FILE not in names:
            raise UpdateError(f"묶음에 {REQUIRED_FILE}이 없습니다")
        new_version = local_version(stage / "edge" / "VERSION") or server_version
        if not VERSION_RE.fullmatch(new_version):
            raise UpdateError(f"묶음의 VERSION 형식이 틀립니다: {new_version[:40]!r}")
        log.info("묶음을 받았습니다: %d바이트, 파일 %d개 (버전 %s)", size, len(names), new_version)
        # 새 코드가 쓸 패키지를 먼저 설치한다. 실패하면 파일을 바꾸지 않는다(다음 시작 때 다시 시도).
        new_requirements = stage / REQUIREMENTS_NAME
        if new_requirements.exists() and _normalized(new_requirements) != _normalized(install_dir / REQUIREMENTS_NAME):
            log.info("%s가 바뀌어 pip로 설치합니다", REQUIREMENTS_NAME)
            if not pip(new_requirements, install_dir):
                raise UpdateError("pip 설치에 실패해 이번 업데이트를 하지 않았습니다")
        try:
            install_files(stage, install_dir, names)
            write_version(install_dir / "edge" / "VERSION", new_version)
        except OSError as e:
            raise UpdateError(f"파일을 바꾸지 못했습니다: {e}") from None
        return new_version


def update(install_dir: Path = INSTALL_DIR, config_path: Optional[Path] = None, *,
           pip: Callable[[Path, Path], bool] = run_pip, attempt_file: Optional[Path] = None) -> str:
    """한 번 확인하고 필요하면 업데이트한다. 결과: developer(VERSION 없음), config(설정 오류), unreachable,
    current(최신), updated, failed."""
    local = local_version(install_dir / "edge" / "VERSION")
    if local is None:
        log.info("edge/VERSION이 없어(개발 폴더) 자동 업데이트를 하지 않습니다")
        return "developer"
    config_path = config_path or install_dir / "edge" / "config.toml"
    try:
        cfg = from_file(load_file(config_path))
        base_url = server_urls(cfg.server)[1]
    except ConfigError as e:
        log.warning("설정을 읽을 수 없어 업데이트를 건너뜁니다: %s", e)
        return "config"
    if not cfg.token:
        log.warning("장치 토큰이 없어 업데이트를 건너뜁니다 (%s)", config_path)
        return "config"
    try:
        remote = fetch_version(base_url)
    except Unreachable as e:
        log.warning("%s - 업데이트를 건너뜁니다", e)
        return "unreachable"
    except UpdateError as e:
        log.warning("%s - 업데이트를 건너뜁니다", e)
        return "failed"
    if remote == local:
        record_attempt(remote, "최신", attempt_file)
        log.info("edge 버전 %s - 서버와 같습니다", local)
        return "current"
    log.info("서버의 edge 버전이 다릅니다 (이 장치 %s, 서버 %s). 업데이트합니다", local, remote)
    record_attempt(remote, "업데이트 시작", attempt_file)
    try:
        installed = apply_bundle(base_url, cfg.token, install_dir, remote, pip)
    except UpdateError as e:
        record_attempt(remote, f"실패: {e}", attempt_file)
        log.error("업데이트하지 못했습니다: %s", e)
        return "failed"
    record_attempt(remote, f"업데이트함: {installed}", attempt_file)
    log.info("edge를 %s에서 %s로 업데이트했습니다", local, installed)
    return "updated"


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m edge.update",
        description="서버에서 edge 코드를 받아 업데이트합니다 (edge/VERSION이 있을 때만. 지우면 자동 업데이트 끔).")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH,
                        help="설정 파일 (기본: edge/config.toml). 서버 주소와 장치 토큰을 읽는다")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        result = update(INSTALL_DIR, args.config)
    except Exception:   # 어떤 문제도 서비스 시작을 막지 않는다(ExecStartPre의 '-'와 함께)
        log.exception("업데이트 중 예상하지 못한 오류")
        return 1
    return 1 if result == "failed" else 0


if __name__ == "__main__":
    sys.exit(main())
