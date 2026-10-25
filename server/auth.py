"""계정·세션·보호자 연결·기기·푸시 구독 관리와 FastAPI 인증 도우미.

권한의 기준은 docs/ARCHITECTURE.md '계정과 권한'이다.
- 보호자 연결은 초대받은 보호자가 수락(accepted)해야만 효력이 있다.
  요청(pending) 상태로는 상대의 기기·이벤트·경보를 하나도 받지 못한다.
- 비밀번호, 세션 토큰, Pi 기기 키는 원문을 저장하지 않는다.
  DB가 유출되어도 바로 로그인하거나 기기로 위장할 수 없게 하기 위함이다.

오류 규칙(라우터가 HTTP 상태로 바꾼다):
- ValueError: 입력이 잘못됨 (400)
- LookupError: 대상이 없음 (404)
- PermissionError: 대상은 있지만 권한이 없음 (403)
"""

import hashlib
import hmac
import logging
import re
import secrets
import sqlite3
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple, Union
from urllib.parse import urlparse

import bcrypt
from fastapi import HTTPException, Request, WebSocket

from server.config import Settings
from server.db import GUARDIAN_ACCEPTED, GUARDIAN_PENDING, Database, now_iso

log = logging.getLogger("server.auth")

PASSWORD_MIN_LENGTH = 8
# bcrypt는 72바이트까지만 비교한다. 그 뒤가 무시되는 것을 막으려고 길이를 제한한다.
PASSWORD_MAX_BYTES = 72
NAME_MAX_LENGTH = 50
# bcrypt 작업량. 기본값 12(약 0.2초)는 로그인에는 충분히 빠르고 무차별 대입에는 느리다.
# 테스트는 시간을 줄이려고 낮춰 쓴다.
BCRYPT_ROUNDS = 12
DEVICE_KINDS = ("pi", "phone")
# 형식만 대략 확인한다. 이메일 인증은 이번 범위가 아니다.
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
# 웹 푸시 구독 주소로 받는 브라우저 푸시 서비스(이 도메인과 그 하위 도메인).
# 서버가 이 주소로 직접 HTTP 요청을 보내므로, 아무 주소나 받으면 내부망 주소로 요청을 보내게
# 만들 수 있다(SSRF). Chrome·삼성 인터넷=FCM, Firefox=Mozilla, Edge=WNS, Safari=Apple.
PUSH_SERVICE_DOMAINS = ("fcm.googleapis.com", "push.services.mozilla.com", "notify.windows.com", "push.apple.com")


# ---------------------------------------------------------------------------
# 비밀번호·토큰 해시
# ---------------------------------------------------------------------------

def hash_password(pw: str) -> str:
    return bcrypt.hashpw(pw.encode("utf-8"), bcrypt.gensalt(rounds=BCRYPT_ROUNDS)).decode("ascii")


def verify_password(pw: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(pw.encode("utf-8"), hashed.encode("ascii"))
    except ValueError:
        # 72바이트 초과 비밀번호나 깨진 해시는 일치하지 않는 것으로 본다.
        return False


# 없는 이메일로 로그인할 때도 bcrypt 비교를 한 번 해서, 응답 시간으로
# 가입 여부를 알아내기 어렵게 한다.
_DUMMY_HASH = hash_password("dummy-password-for-timing")


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def is_push_service_endpoint(endpoint: str) -> bool:
    """https이고 호스트가 PUSH_SERVICE_DOMAINS 중 하나(또는 그 하위 도메인)이며 기본 포트인 주소."""
    try:
        url = urlparse(endpoint or "")
        port = url.port
    except ValueError:
        return False
    host = (url.hostname or "").lower()
    if url.scheme != "https" or port not in (None, 443) or url.username or url.password:
        return False
    return any(host == d or host.endswith("." + d) for d in PUSH_SERVICE_DOMAINS)


# ---------------------------------------------------------------------------
# 계정 저장소
# ---------------------------------------------------------------------------

def _public_user(row: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """비밀번호 해시를 뺀 사용자 정보."""
    if row is None:
        return None
    return {"id": row["id"], "email": row["email"], "name": row["name"], "created_at": row["created_at"]}


def _public_device(row: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """기기 키 해시를 뺀 기기 정보."""
    if row is None:
        return None
    return {
        "id": row["id"],
        "owner_id": row["owner_id"],
        "name": row["name"],
        "kind": row["kind"],
        "created_at": row["created_at"],
        "last_seen_at": row["last_seen_at"],
    }


class Accounts:
    """users, sessions, guardians, devices, push_subscriptions 테이블을 다룬다."""

    def __init__(self, db: Database, settings: Settings):
        self.db = db
        self.settings = settings

    # --- 사용자 -------------------------------------------------------------

    def create_user(self, email: str, password: str, name: str) -> int:
        email = normalize_email(email)
        name = (name or "").strip()
        if not EMAIL_PATTERN.match(email):
            raise ValueError("이메일 형식이 올바르지 않습니다.")
        if len(password or "") < PASSWORD_MIN_LENGTH:
            raise ValueError(f"비밀번호는 {PASSWORD_MIN_LENGTH}자 이상이어야 합니다.")
        if len(password.encode("utf-8")) > PASSWORD_MAX_BYTES:
            raise ValueError("비밀번호가 너무 깁니다.")
        if not name:
            raise ValueError("이름을 입력해 주세요.")
        if len(name) > NAME_MAX_LENGTH:
            raise ValueError(f"이름은 {NAME_MAX_LENGTH}자 이하로 입력해 주세요.")
        if self.db.query_one("SELECT id FROM users WHERE email = ?", (email,)):
            raise ValueError("이미 가입된 이메일입니다.")
        try:
            cur = self.db.execute(
                "INSERT INTO users (email, password_hash, name, created_at) VALUES (?, ?, ?, ?)",
                (email, hash_password(password), name, now_iso()),
            )
        except sqlite3.IntegrityError:
            # 위 확인과 INSERT 사이에 같은 이메일이 먼저 들어온 경우
            raise ValueError("이미 가입된 이메일입니다.")
        log.info("회원가입: user=%s", cur.lastrowid)
        return cur.lastrowid

    def authenticate(self, email: str, password: str) -> Optional[Dict[str, Any]]:
        row = self.db.query_one("SELECT * FROM users WHERE email = ?", (normalize_email(email),))
        if row is None:
            verify_password(password or "", _DUMMY_HASH)
            return None
        if not verify_password(password or "", row["password_hash"]):
            return None
        # 비밀번호가 맞은 뒤에만 알려 준다(비밀번호를 모르는 사람에게 정지 여부를 알리지 않으려고).
        if row["disabled_at"]:
            raise PermissionError("정지된 계정입니다. 시스템 관리자에게 문의해 주세요.")
        return _public_user(row)

    def get_user(self, user_id: int) -> Optional[Dict[str, Any]]:
        return _public_user(self.db.query_one("SELECT * FROM users WHERE id = ?", (user_id,)))

    def get_user_by_email(self, email: str) -> Optional[Dict[str, Any]]:
        return _public_user(self.db.query_one("SELECT * FROM users WHERE email = ?", (normalize_email(email),)))

    # --- 계정 정지·삭제 (관리자 페이지) ------------------------------------------
    # 메모리 상태(열린 화면, 기기 연결) 정리는 server/admin.py가 한다.

    def is_disabled(self, user_id: int) -> bool:
        row = self.db.query_one("SELECT disabled_at FROM users WHERE id = ?", (user_id,))
        return bool(row and row["disabled_at"])

    def set_disabled(self, user_id: int, disabled: bool) -> None:
        """계정을 정지하거나 정지를 푼다. 정지하면 로그인 세션도 모두 지운다. 없는 사용자는 LookupError."""
        if self.get_user(user_id) is None:
            raise LookupError("사용자를 찾을 수 없습니다.")
        if disabled:
            # 이미 정지된 계정이면 처음 정지한 시각을 그대로 둔다.
            self.db.execute("UPDATE users SET disabled_at = COALESCE(disabled_at, ?) WHERE id = ?",
                            (now_iso(), user_id))
            self.db.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        else:
            self.db.execute("UPDATE users SET disabled_at = NULL WHERE id = ?", (user_id,))

    def delete_user(self, user_id: int) -> None:
        """사용자와 그 세션·보호자 연결·기기·이벤트·알림 기록을 모두 지운다(외래 키 ON DELETE CASCADE)."""
        cur = self.db.execute("DELETE FROM users WHERE id = ?", (user_id,))
        if cur.rowcount == 0:
            raise LookupError("사용자를 찾을 수 없습니다.")

    # --- 세션 ---------------------------------------------------------------

    def create_session(self, user_id: int) -> str:
        """새 세션 토큰을 돌려준다. DB에는 토큰의 sha256만 남는다."""
        token = secrets.token_urlsafe(32)
        now = datetime.now()
        expires = now + timedelta(days=self.settings.session_days)
        # 만료된 세션은 새로 만들 때 함께 정리한다(따로 청소 작업을 두지 않으려고).
        self.db.execute("DELETE FROM sessions WHERE expires_at < ?", (now.isoformat(timespec="milliseconds"),))
        self.db.execute(
            "INSERT INTO sessions (token_hash, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
            (_sha256(token), user_id, now.isoformat(timespec="milliseconds"), expires.isoformat(timespec="milliseconds")),
        )
        return token

    def user_for_session(self, token: Optional[str]) -> Optional[Dict[str, Any]]:
        if not token:
            return None
        row = self.db.query_one(
            "SELECT u.*, s.expires_at AS session_expires_at FROM sessions s "
            "JOIN users u ON u.id = s.user_id WHERE s.token_hash = ? AND u.disabled_at IS NULL",
            (_sha256(token),),
        )
        if row is None:
            return None
        if datetime.fromisoformat(row["session_expires_at"]) < datetime.now():
            self.delete_session(token)
            return None
        return _public_user(row)

    def delete_session(self, token: Optional[str]) -> None:
        if token:
            self.db.execute("DELETE FROM sessions WHERE token_hash = ?", (_sha256(token),))

    # --- 보호자 연결 ----------------------------------------------------------
    # guardians.user_id = 보호받는 사용자(요청한 사람), guardians.guardian_id = 보호자(초대받은 사람)

    def _get_link(self, link_id: int) -> Dict[str, Any]:
        link = self.db.query_one("SELECT * FROM guardians WHERE id = ?", (link_id,))
        if link is None:
            raise LookupError("보호자 연결을 찾을 수 없습니다.")
        return link

    def _link_view(self, link: Dict[str, Any], other_user_id: int) -> Dict[str, Any]:
        """화면에 보여 줄 연결 정보. user는 '상대방'이다.

        요청한 사람에게는 상대가 수락하기 전까지 이메일(자기가 입력한 값)만 보여 준다. 이름과 id까지
        보여 주면 이메일만 넣어 보고 남의 실명을 알아낼 수 있기 때문이다. 요청을 받은 보호자는
        누가 요청했는지 보고 수락을 정해야 하므로 요청자 정보를 그대로 본다.
        """
        other = self.get_user(other_user_id)
        hidden = link["status"] != GUARDIAN_ACCEPTED and other_user_id == link["guardian_id"]
        return {
            "link_id": link["id"],
            "user": {"id": None if hidden else other["id"], "name": None if hidden else other["name"],
                     "email": other["email"]},
            "status": link["status"],
            "created_at": link["created_at"],
        }

    def request_guardian(self, user_id: int, guardian_email: str) -> Dict[str, Any]:
        """보호자 등록 요청. 수락 전까지는 아무 권한도 주지 않는다."""
        guardian = self.get_user_by_email(guardian_email)
        if guardian is None:
            raise ValueError("가입되지 않은 이메일입니다. 보호자도 먼저 회원가입해야 합니다.")
        if guardian["id"] == user_id:
            raise ValueError("자기 자신은 보호자로 등록할 수 없습니다.")
        existing = self.db.query_one(
            "SELECT * FROM guardians WHERE user_id = ? AND guardian_id = ?", (user_id, guardian["id"])
        )
        if existing:
            if existing["status"] == GUARDIAN_ACCEPTED:
                raise ValueError("이미 연결된 보호자입니다.")
            raise ValueError("이미 요청을 보냈습니다. 상대방의 수락을 기다려 주세요.")
        try:
            cur = self.db.execute(
                "INSERT INTO guardians (user_id, guardian_id, status, created_at) VALUES (?, ?, ?, ?)",
                (user_id, guardian["id"], GUARDIAN_PENDING, now_iso()),
            )
        except sqlite3.IntegrityError:
            raise ValueError("이미 요청을 보냈습니다. 상대방의 수락을 기다려 주세요.")
        log.info("보호자 요청: user=%s -> guardian=%s (link=%s)", user_id, guardian["id"], cur.lastrowid)
        return self._link_view(self._get_link(cur.lastrowid), guardian["id"])

    def accept_guardian(self, link_id: int, guardian_user_id: int) -> Dict[str, Any]:
        """초대받은 보호자 본인만 수락할 수 있다. 요청한 사용자가 스스로 수락하면 동의의 의미가 없다."""
        link = self._get_link(link_id)
        if link["guardian_id"] != guardian_user_id:
            raise PermissionError("이 요청을 받은 보호자만 수락할 수 있습니다.")
        if link["status"] != GUARDIAN_ACCEPTED:
            self.db.execute("UPDATE guardians SET status = ? WHERE id = ?", (GUARDIAN_ACCEPTED, link_id))
            log.info("보호자 수락: link=%s (user=%s, guardian=%s)", link_id, link["user_id"], guardian_user_id)
        return self._link_view(self._get_link(link_id), link["user_id"])

    def remove_guardian(self, link_id: int, acting_user_id: int) -> Dict[str, Any]:
        """연결 해제. 사용자·보호자 어느 쪽이든 할 수 있다(요청 취소, 거절 포함)."""
        link = self._get_link(link_id)
        if acting_user_id not in (link["user_id"], link["guardian_id"]):
            raise PermissionError("연결된 당사자만 해제할 수 있습니다.")
        self.db.execute("DELETE FROM guardians WHERE id = ?", (link_id,))
        log.info("보호자 연결 해제: link=%s by user=%s", link_id, acting_user_id)
        return link

    def guardian_links_of(self, user_id: int) -> List[Dict[str, Any]]:
        """내가 등록한 보호자 목록(요청 중 포함). user는 보호자."""
        links = self.db.query("SELECT * FROM guardians WHERE user_id = ? ORDER BY id", (user_id,))
        return [self._link_view(link, link["guardian_id"]) for link in links]

    def ward_links_of(self, guardian_id: int, status: str) -> List[Dict[str, Any]]:
        """나를 보호자로 등록한 연결 목록. user는 보호받는 사용자."""
        links = self.db.query(
            "SELECT * FROM guardians WHERE guardian_id = ? AND status = ? ORDER BY id", (guardian_id, status)
        )
        return [self._link_view(link, link["user_id"]) for link in links]

    def guardian_ids_for(self, user_id: int) -> List[int]:
        """경보를 받을 보호자. 수락한 연결만 세고, 관리자가 정지한 보호자 계정은 뺀다."""
        rows = self.db.query(
            "SELECT g.guardian_id FROM guardians g JOIN users u ON u.id = g.guardian_id "
            "WHERE g.user_id = ? AND g.status = ? AND u.disabled_at IS NULL ORDER BY g.guardian_id",
            (user_id, GUARDIAN_ACCEPTED),
        )
        return [r["guardian_id"] for r in rows]

    def wards_of(self, guardian_id: int) -> List[int]:
        """이 보호자가 보호하는 사용자. 수락한 연결만 센다."""
        rows = self.db.query(
            "SELECT user_id FROM guardians WHERE guardian_id = ? AND status = ? ORDER BY user_id",
            (guardian_id, GUARDIAN_ACCEPTED),
        )
        return [r["user_id"] for r in rows]

    def visible_owner_ids(self, user_id: int) -> List[int]:
        """이 사용자가 기기·이벤트를 볼 수 있는 주인 목록: 본인 + 수락된 피보호자."""
        return [user_id] + [w for w in self.wards_of(user_id) if w != user_id]

    # --- 기기 ---------------------------------------------------------------

    def create_device(self, owner_id: int, name: str, kind: str) -> Tuple[Dict[str, Any], Optional[str]]:
        """기기 등록. Pi는 기기 키를 만들어 이때 한 번만 돌려준다(DB에는 해시만)."""
        name = (name or "").strip()
        if kind not in DEVICE_KINDS:
            raise ValueError("기기 종류는 'pi' 또는 'phone'이어야 합니다.")
        if not name:
            raise ValueError("기기 이름을 입력해 주세요.")
        if len(name) > NAME_MAX_LENGTH:
            raise ValueError(f"기기 이름은 {NAME_MAX_LENGTH}자 이하로 입력해 주세요.")
        key = secrets.token_urlsafe(24) if kind == "pi" else None
        cur = self.db.execute(
            "INSERT INTO devices (owner_id, name, kind, key_hash, created_at) VALUES (?, ?, ?, ?, ?)",
            (owner_id, name, kind, _sha256(key) if key else None, now_iso()),
        )
        log.info("기기 등록: device=%s owner=%s kind=%s", cur.lastrowid, owner_id, kind)
        return self.get_device(cur.lastrowid), key

    def get_device(self, device_id: int) -> Optional[Dict[str, Any]]:
        return _public_device(self.db.query_one("SELECT * FROM devices WHERE id = ?", (device_id,)))

    def devices_of(self, owner_ids: List[int]) -> List[Dict[str, Any]]:
        if not owner_ids:
            return []
        marks = ",".join("?" for _ in owner_ids)
        rows = self.db.query(f"SELECT * FROM devices WHERE owner_id IN ({marks}) ORDER BY id", tuple(owner_ids))
        return [_public_device(r) for r in rows]

    def verify_device_key(self, device_id: int, key: Optional[str]) -> Optional[Dict[str, Any]]:
        if not key:
            return None
        row = self.db.query_one("SELECT * FROM devices WHERE id = ?", (device_id,))
        if row is None or not row["key_hash"]:
            return None  # 없는 기기이거나 키가 없는 휴대폰 기기
        if not hmac.compare_digest(row["key_hash"], _sha256(key)):
            return None
        return _public_device(row)

    def mark_device_seen(self, device_id: int) -> None:
        self.db.execute("UPDATE devices SET last_seen_at = ? WHERE id = ?", (now_iso(), device_id))

    def delete_device(self, device_id: int, owner_id: int) -> None:
        device = self.get_device(device_id)
        if device is None:
            raise LookupError("기기를 찾을 수 없습니다.")
        if device["owner_id"] != owner_id:
            # 보호자도 피보호자의 기기를 지울 수는 없다.
            raise PermissionError("기기 주인만 삭제할 수 있습니다.")
        self.db.execute("DELETE FROM devices WHERE id = ?", (device_id,))
        log.info("기기 삭제: device=%s owner=%s", device_id, owner_id)

    # --- 웹 푸시 구독 ---------------------------------------------------------

    def save_push_subscription(self, user_id: int, endpoint: str, p256dh: str, auth: str) -> None:
        """endpoint 기준으로 덮어쓴다. 같은 브라우저에서 다른 계정으로 다시 켜면 그 계정으로 옮긴다.

        endpoint는 알려진 브라우저 푸시 서비스의 https 주소만 받는다(PUSH_SERVICE_DOMAINS).
        """
        if not is_push_service_endpoint(endpoint):
            raise ValueError("브라우저 푸시 서비스의 구독 주소가 아닙니다.")
        self.db.execute(
            "INSERT INTO push_subscriptions (user_id, endpoint, p256dh, auth, created_at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(endpoint) DO UPDATE SET user_id = excluded.user_id, p256dh = excluded.p256dh, "
            "auth = excluded.auth, created_at = excluded.created_at",
            (user_id, endpoint, p256dh, auth, now_iso()),
        )

    def push_subscriptions_for(self, user_ids: List[int]) -> List[Dict[str, Any]]:
        if not user_ids:
            return []
        marks = ",".join("?" for _ in user_ids)
        return self.db.query(
            f"SELECT * FROM push_subscriptions WHERE user_id IN ({marks}) ORDER BY id", tuple(user_ids)
        )

    def delete_push_subscription(self, endpoint: str, user_id: Optional[int] = None) -> None:
        """user_id를 주면 그 사용자의 구독일 때만 지운다(웹에서 끄기). 없으면 무조건(만료 구독 정리)."""
        if user_id is None:
            self.db.execute("DELETE FROM push_subscriptions WHERE endpoint = ?", (endpoint,))
        else:
            self.db.execute(
                "DELETE FROM push_subscriptions WHERE endpoint = ? AND user_id = ?", (endpoint, user_id)
            )


# ---------------------------------------------------------------------------
# FastAPI 인증 도우미
# ---------------------------------------------------------------------------

Connection = Union[Request, WebSocket]


def _session_user(conn: Connection) -> Optional[Dict[str, Any]]:
    settings: Settings = conn.app.state.settings
    accounts: Accounts = conn.app.state.accounts
    return accounts.user_for_session(conn.cookies.get(settings.cookie_name))


def current_user(request: Request) -> Dict[str, Any]:
    """로그인이 필요한 API의 의존성. 세션이 없거나 만료되면 401."""
    user = _session_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="로그인이 필요합니다.")
    return user


def optional_user(request: Request) -> Optional[Dict[str, Any]]:
    """기기 키로도 접근할 수 있는 API에서 쓴다. 로그인하지 않았으면 None."""
    return _session_user(request)


def user_from_websocket(ws: WebSocket) -> Optional[Dict[str, Any]]:
    return _session_user(ws)


def device_from_request(conn: Connection, device_id: int) -> Optional[Dict[str, Any]]:
    """Pi·키오스크를 기기 키로 인증한다. 키는 X-Device-Key 헤더 또는 key 쿼리 파라미터."""
    key = conn.headers.get("x-device-key") or conn.query_params.get("key")
    accounts: Accounts = conn.app.state.accounts
    return accounts.verify_device_key(device_id, key)


def check_origin(conn: Connection) -> bool:
    """쿠키로 인증하는 WebSocket이 다른 사이트에서 열리지 않게 막는다.

    브라우저는 WebSocket에 쿠키를 자동으로 붙이므로, 다른 사이트의 페이지가
    로그인한 사용자 몰래 접속할 수 있다(cross-site WebSocket hijacking).
    Origin이 없는 접속(Pi 클라이언트 같은 비브라우저)은 허용한다.
    """
    origin = conn.headers.get("origin")
    if origin is None:
        return True
    host = (conn.headers.get("host") or "").lower()
    origin_host = urlparse(origin).netloc.lower()
    if not origin_host or origin_host != host:
        log.warning("다른 출처의 WebSocket 접속 거부: origin=%s host=%s", origin, host)
        return False
    return True
