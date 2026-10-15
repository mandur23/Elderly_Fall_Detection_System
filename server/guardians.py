"""보호자 계정, 비밀번호, 로그인 세션.

보호자는 관리자가 관리자 화면에서 만든 계정으로 보호자 웹(/g)에 로그인해, 맡은 장치만 본다.

- 비밀번호는 PBKDF2-SHA256(무작위 소금, 20만 번 반복)으로 만든 해시만 기록 파일에 저장한다.
- 세션은 서명한 쿠키다(30일). 서명 키는 기록 파일에 두어 서버를 다시 켜도 로그인이 유지된다.
  비밀번호를 바꾸거나 계정을 지우면 그 계정의 기존 로그인이 모두 끊긴다(session_version).
- 같은 곳에서 비밀번호를 여러 번 틀리면 잠시 로그인을 막는다(LoginLimiter).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import time
from collections import defaultdict, deque
from typing import Deque, Dict, List, Optional, Tuple

from server.store import Store

PBKDF2_ITERATIONS = 200_000
SESSION_SECONDS = 30 * 24 * 3600
MIN_PASSWORD_LENGTH = 8
USERNAME_RE = re.compile(r"^[a-z0-9_.-]{3,32}$")
SECRET_KEY = "guardian_session_secret"
PRUNE_AT = 10_000  # 로그인 실패 기록이 이만큼 쌓이면 지난 것을 지운다


class GuardianError(ValueError):
    """사람에게 보여 줄 수 있는 입력 오류."""


def hash_password(password: str, *, salt: Optional[bytes] = None, iterations: int = PBKDF2_ITERATIONS) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return "pbkdf2_sha256${}${}${}".format(iterations, base64.b64encode(salt).decode(), base64.b64encode(digest).decode())


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, iterations, salt, digest = stored.split("$")
        if scheme != "pbkdf2_sha256":
            return False
        expected = base64.b64decode(digest)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), base64.b64decode(salt), int(iterations))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


MAX_NAME_LENGTH = 40
MAX_DEVICE_ID_LENGTH = 64


def clean_devices(devices) -> List[str]:
    if not isinstance(devices, (list, tuple)) or not all(isinstance(d, str) for d in devices):
        raise GuardianError("devices는 장치 id(문자열) 목록이어야 합니다")
    out = []
    for d in devices:
        d = d.strip()
        if len(d) > MAX_DEVICE_ID_LENGTH:
            raise GuardianError(f"장치 id가 너무 깁니다: {d[:20]}…")
        if d and d not in out:
            out.append(d)
    return out


def clean_name(name) -> str:
    if name is not None and not isinstance(name, str):
        raise GuardianError("이름은 문자열이어야 합니다")
    name = (name or "").strip()
    if len(name) > MAX_NAME_LENGTH:
        raise GuardianError(f"이름은 {MAX_NAME_LENGTH}자 이하여야 합니다")
    return name


def clean_flag(value, label: str) -> bool:
    if not isinstance(value, bool):  # "false" 같은 문자열을 참으로 잘못 받지 않게
        raise GuardianError(f"{label}은(는) true/false여야 합니다")
    return value


def check_password(password) -> str:
    if not isinstance(password, str) or len(password) < MIN_PASSWORD_LENGTH:
        raise GuardianError(f"비밀번호는 {MIN_PASSWORD_LENGTH}자 이상이어야 합니다")
    return password


def public(guardian: Dict) -> Dict:
    """화면·API로 내보낼 항목 (비밀번호 해시 제외)."""
    return {k: guardian[k] for k in ("id", "username", "name", "devices", "can_clear", "created_at", "last_login_at")}


class Guardians:
    def __init__(self, store: Store):
        self.store = store
        secret = store.kv_get(SECRET_KEY)
        if secret is None:
            secret = secrets.token_hex(32)
            store.kv_set(SECRET_KEY, secret)
        self._secret = bytes.fromhex(secret)

    # --- 계정 관리 (관리자) ---
    # 기록 파일은 이벤트 루프 한 곳에서만 쓴다. 비밀번호 해시(느림)는 서버가 다른 스레드에서 미리 만들어
    # password_hash로 넘긴다. 시험·도구에서는 password를 그대로 줘도 된다.
    def create(self, username, name, password=None, devices=(), can_clear=True, *,
               password_hash: Optional[str] = None) -> Dict:
        if username is not None and not isinstance(username, str):
            raise GuardianError("아이디는 문자열이어야 합니다")
        username = (username or "").strip().lower()
        if not USERNAME_RE.match(username):
            raise GuardianError("아이디는 영문 소문자·숫자·._- 3~32자여야 합니다")
        name = clean_name(name) or username
        devices = clean_devices(devices)
        can_clear = clean_flag(can_clear, "해제 권한")
        if self.store.get_guardian_by_username(username) is not None:
            raise GuardianError(f"이미 있는 아이디입니다: {username}")
        password_hash = password_hash or hash_password(check_password(password))
        guardian_id = self.store.insert_guardian(username, name, password_hash, devices, can_clear)
        return public(self.store.get_guardian(guardian_id))

    def update(self, guardian_id: int, *, name=None, devices=None, can_clear=None, password=None,
               password_hash: Optional[str] = None) -> Dict:
        if self.store.get_guardian(guardian_id) is None:
            raise KeyError(guardian_id)
        if password_hash is None and password:
            password_hash = hash_password(check_password(password))
        self.store.update_guardian(
            guardian_id,
            name=clean_name(name) or None if name is not None else None,
            devices=clean_devices(devices) if devices is not None else None,
            can_clear=clean_flag(can_clear, "해제 권한") if can_clear is not None else None,
            password_hash=password_hash,
        )
        return public(self.store.get_guardian(guardian_id))

    def delete(self, guardian_id: int) -> bool:
        return self.store.delete_guardian(guardian_id)

    def list(self) -> List[Dict]:
        return [public(g) for g in self.store.list_guardians()]

    # --- 로그인 ---
    def find(self, username: str) -> Optional[Dict]:
        return self.store.get_guardian_by_username(str(username or "").strip().lower())

    @staticmethod
    def password_ok(guardian: Optional[Dict], password: str) -> bool:
        """비밀번호 확인(느림, 기록 파일은 쓰지 않으므로 다른 스레드에서 불러도 된다)."""
        if guardian is None:
            hash_password(password or "")  # 아이디가 없을 때도 비슷한 시간이 걸리게 한다
            return False
        return verify_password(password or "", guardian["password_hash"])

    def mark_login(self, guardian: Dict) -> None:
        self.store.touch_guardian_login(guardian["id"])

    def authenticate(self, username: str, password: str) -> Optional[Dict]:
        guardian = self.find(username)
        if not self.password_ok(guardian, password):
            return None
        self.mark_login(guardian)
        return guardian

    def _sign(self, payload: str) -> str:
        return hmac.new(self._secret, payload.encode(), hashlib.sha256).hexdigest()

    def session_token(self, guardian: Dict, now: Optional[float] = None) -> str:
        expires = int((now or time.time()) + SESSION_SECONDS)
        payload = f"{guardian['id']}.{guardian['session_version']}.{expires}"
        return f"{payload}.{self._sign(payload)}"

    def from_session(self, token: str, now: Optional[float] = None) -> Optional[Dict]:
        """쿠키의 세션 값이 맞으면 보호자 계정, 아니면(위조·만료·비밀번호 변경·삭제) None."""
        try:
            guardian_id, version, expires, sig = (token or "").split(".")
        except ValueError:
            return None
        payload = f"{guardian_id}.{version}.{expires}"
        if not hmac.compare_digest(sig.encode(), self._sign(payload).encode()):  # bytes: ASCII가 아닌 쿠키도 안전
            return None
        try:
            guardian_id, version, expires = int(guardian_id), int(version), int(expires)
        except ValueError:
            return None
        if expires < (now or time.time()):
            return None
        guardian = self.store.get_guardian(guardian_id)
        if guardian is None or guardian["session_version"] != version:
            return None
        return guardian


class LoginLimiter:
    """같은 주소(또는 같은 아이디)에서 window초 안에 max_failures번 틀리면 lock초 동안 로그인을 막는다."""

    def __init__(self, max_failures: int = 5, window: float = 300.0, lock: float = 300.0, by_user: bool = True):
        self.max_failures, self.window, self.lock = max_failures, window, lock
        # by_user=False: 주소로만 센다 (관리자처럼 아이디가 하나뿐이면 아이디로 잠그면 진짜 관리자도 잠긴다)
        self.by_user = by_user
        self._failures: Dict[str, Deque[float]] = defaultdict(deque)
        self._locked_until: Dict[str, float] = {}

    def _keys(self, remote: Optional[str], username: str) -> Tuple[str, ...]:
        ip = f"ip:{remote or '-'}"
        return (ip, f"user:{(username or '').strip().lower()}") if self.by_user else (ip,)

    def blocked_for(self, remote: Optional[str], username: str, now: Optional[float] = None) -> float:
        now = time.monotonic() if now is None else now
        return max([self._locked_until.get(k, 0.0) - now for k in self._keys(remote, username)] + [0.0])

    def failed(self, remote: Optional[str], username: str, now: Optional[float] = None) -> None:
        now = time.monotonic() if now is None else now
        if len(self._failures) + len(self._locked_until) > PRUNE_AT:
            self._prune(now)
        for key in self._keys(remote, username):
            q = self._failures[key]
            q.append(now)
            while q and now - q[0] > self.window:
                q.popleft()
            if len(q) >= self.max_failures:
                self._locked_until[key] = now + self.lock
                q.clear()

    def _prune(self, now: float) -> None:
        """없는 아이디를 마구 넣어 보는 경우에도 기록이 끝없이 늘지 않게 지난 기록을 지운다."""
        for key in [k for k, q in self._failures.items() if not q or now - q[-1] > self.window]:
            del self._failures[key]
        for key in [k for k, until in self._locked_until.items() if until <= now]:
            del self._locked_until[key]

    def succeeded(self, remote: Optional[str], username: str) -> None:
        for key in self._keys(remote, username):
            self._failures.pop(key, None)
            self._locked_until.pop(key, None)
