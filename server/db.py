"""SQLite 저장소. 스키마는 docs/ARCHITECTURE.md '데이터베이스'를 따른다.

서버는 asyncio 이벤트 루프 한 곳에서 DB를 쓰지만, 음성 인식·푸시 전송처럼
스레드에서 도는 작업도 있어 잠금으로 직렬화한다. 영상·음성 원본은 저장하지 않는다.
"""

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

# events.result
CLEARED_VOICE = "CLEARED_VOICE"        # 음성으로 '괜찮다' 응답
CLEARED_BUTTON = "CLEARED_BUTTON"      # 경보 전에 화면 버튼으로 해제
ALERT_HELP = "ALERT_HELP"              # 음성으로 도움 요청
ALERT_NO_REPLY = "ALERT_NO_REPLY"      # 무응답·불분명
ALERT_VOICE_FAIL = "ALERT_VOICE_FAIL"  # 녹음·인식 실패, 마이크 권한 거부, 기기 연결 끊김
INTERRUPTED = "INTERRUPTED"            # 처리 도중 서버 재시작
ALERT_RESULTS = (ALERT_HELP, ALERT_NO_REPLY, ALERT_VOICE_FAIL)

# guardians.status
GUARDIAN_PENDING = "pending"
GUARDIAN_ACCEPTED = "accepted"

# notifications.state
NOTIFY_SENT = "sent"
NOTIFY_FAILED = "failed"

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS guardians (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,      -- 보호받는 사용자
    guardian_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,  -- 보호자
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (user_id, guardian_id)
);
CREATE TABLE IF NOT EXISTS devices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,          -- 'pi' | 'phone'
    key_hash TEXT,               -- Pi 기기 키의 해시. 휴대폰은 NULL(세션으로 인증)
    created_at TEXT NOT NULL,
    last_seen_at TEXT
);
CREATE TABLE IF NOT EXISTS push_subscriptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    endpoint TEXT NOT NULL UNIQUE,
    p256dh TEXT NOT NULL,
    auth TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    result TEXT,                 -- NULL이면 음성 확인 중
    voice_result TEXT,
    voice_text TEXT,
    feature_snapshot TEXT,
    cleared_by TEXT              -- 'user:<id>' | 'device:<id>'
);
CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    guardian_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,          -- 'alert' | 'clear'
    channel TEXT NOT NULL,       -- 'websocket' | 'push'
    state TEXT NOT NULL,
    detail TEXT,
    created_at TEXT NOT NULL
);
"""


def now_iso() -> str:
    return datetime.now().isoformat(timespec="milliseconds")


class Database:
    """얇은 SQLite 래퍼. 테이블별 쿼리는 각 모듈이 execute/query로 직접 쓴다."""

    def __init__(self, path: Path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock, self._conn:
            self._conn.execute("PRAGMA foreign_keys = ON")
            self._conn.executescript(SCHEMA)

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock, self._conn:
            return self._conn.execute(sql, params)

    def query(self, sql: str, params: tuple = ()) -> List[Dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def query_one(self, sql: str, params: tuple = ()) -> Optional[Dict[str, Any]]:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def close(self) -> None:
        self._conn.close()


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)
