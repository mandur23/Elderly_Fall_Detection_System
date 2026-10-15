"""SQLite 이벤트 기록.

최소 정보만 남긴다: 발생 시각, 판정 결과, 음성 확인 결과(분류), 알림 결과. 영상·음성 원본은 저장하지 않고,
인식 문장 원문도 설정(store_voice_text)을 켰을 때만 저장한다.
이벤트 루프 한 곳에서만 쓰므로 잠금은 두지 않는다(쿼리는 짧다).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

# result 값
CLEARED_VOICE = "CLEARED_VOICE"            # 음성으로 '괜찮다' 응답
CLEARED_BUTTON = "CLEARED_BUTTON"          # 경보 전에 화면 버튼으로 해제
CLEARED_ADMIN = "CLEARED_ADMIN"            # 경보 전에 관리자 화면에서 해제
CLEARED_GUARDIAN = "CLEARED_GUARDIAN"      # 경보 전에 보호자 웹에서 해제
ALERT_HELP = "ALERT_HELP"                  # 음성으로 도움 요청
ALERT_NO_REPLY = "ALERT_NO_REPLY"          # 무응답·불분명
ALERT_VOICE_FAIL = "ALERT_VOICE_FAIL"      # 마이크·스피커·인식 모델 오류로 확인 불가
ALERT_DISCONNECTED = "ALERT_DISCONNECTED"  # 음성 확인 중 장치 연결 끊김
ALERT_INTERRUPTED = "ALERT_INTERRUPTED"    # 음성 확인 도중 서버 재시작 (확인하지 못했으므로 경보)
INTERRUPTED = "INTERRUPTED"                # (예전 기록) 처리 도중 서버 재시작
ALERT_RESULTS = (ALERT_HELP, ALERT_NO_REPLY, ALERT_VOICE_FAIL, ALERT_DISCONNECTED, ALERT_INTERRUPTED)

# notify_state 값
NOTIFY_NONE = "none"            # 알림 대상 아님
NOTIFY_PENDING = "pending"      # 보내는 중
NOTIFY_SENT = "sent"
NOTIFY_FAILED = "failed"        # 실패, 다시 보낼 차례를 기다림
NOTIFY_DISABLED = "disabled"    # 알림 미설정
NOTIFY_CANCELLED = "cancelled"  # 보내기 전에 현장에서 해제해 다시 보내지 않음

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    device_id TEXT,
    event_type TEXT NOT NULL,
    result TEXT,
    voice_result TEXT,
    voice_text TEXT,
    voice_attempts INTEGER NOT NULL DEFAULT 0,
    duration_ms INTEGER,
    feature_snapshot TEXT,
    notify_state TEXT NOT NULL DEFAULT 'none',
    notify_attempts INTEGER NOT NULL DEFAULT 0,
    notify_next_at TEXT,
    notified_at TEXT,
    cleared_by TEXT
);
CREATE TABLE IF NOT EXISTS system_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    kind TEXT NOT NULL,
    message TEXT,
    notify_state TEXT NOT NULL DEFAULT 'none',
    device_id TEXT
);
CREATE TABLE IF NOT EXISTS guardians (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    devices TEXT NOT NULL DEFAULT '[]',
    can_clear INTEGER NOT NULL DEFAULT 1,
    session_version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    last_login_at TEXT
);
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS devices (
    device_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS admin_sessions (
    token_hash TEXT PRIMARY KEY,
    expires_at REAL NOT NULL,
    password_tag TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_device ON events (device_id, id);
CREATE INDEX IF NOT EXISTS events_started ON events (started_at);
CREATE INDEX IF NOT EXISTS events_notify ON events (notify_state);
CREATE INDEX IF NOT EXISTS events_open ON events (ended_at, result);
CREATE INDEX IF NOT EXISTS system_events_device ON system_events (device_id, id);
"""
# 예전 기록 파일에 없던 열 (장치가 여러 대가 되면서 추가)
MIGRATIONS = [("system_events", "device_id", "TEXT")]


def now_iso(now: Optional[datetime] = None) -> str:
    return (now or datetime.now()).isoformat(timespec="milliseconds")


class Store:
    def __init__(self, path: Path):
        path = Path(path)
        if str(path) != ":memory:":
            path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path))
        self._conn.row_factory = sqlite3.Row
        with self._conn:
            # WAL: 전원이 갑자기 꺼져도 손상 위험을 줄인다.
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(SCHEMA)
            for table, column, kind in MIGRATIONS:
                columns = {r["name"] for r in self._conn.execute(f"PRAGMA table_info({table})")}
                if column not in columns:
                    self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")

    def close(self) -> None:
        self._conn.close()

    def _execute(self, sql: str, params=()) -> sqlite3.Cursor:
        with self._conn:
            return self._conn.execute(sql, params)

    def _query(self, sql: str, params=()) -> List[Dict]:
        return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    # --- 이벤트 ---
    def insert_event(self, device_id: str, features: Optional[Dict], event_type: str = "FALL_SUSPECT") -> int:
        cur = self._execute(
            "INSERT INTO events (started_at, device_id, event_type, feature_snapshot) VALUES (?, ?, ?, ?)",
            (now_iso(), device_id, event_type, json.dumps(features or {}, ensure_ascii=False)),
        )
        return cur.lastrowid

    def get_event(self, event_id: int) -> Optional[Dict]:
        rows = self._query("SELECT * FROM events WHERE id = ?", (event_id,))
        return rows[0] if rows else None

    def set_voice_attempts(self, event_id: int, attempts: int) -> None:
        self._execute("UPDATE events SET voice_attempts = ? WHERE id = ?", (attempts, event_id))

    def set_result(self, event_id: int, result: str, voice_result: Optional[str] = None,
                   voice_text: Optional[str] = None, ended: bool = True) -> None:
        now = now_iso()
        self._execute(
            """UPDATE events SET result = ?, voice_result = ?, voice_text = ?,
                   ended_at = CASE WHEN ? THEN ? ELSE ended_at END,
                   duration_ms = CAST((julianday(?) - julianday(started_at)) * 86400000 AS INTEGER)
               WHERE id = ?""",
            (result, voice_result, voice_text, 1 if ended else 0, now, now, event_id),
        )

    def close_event(self, event_id: int, cleared_by: str) -> None:
        """경보 뒤 현장에서 해제했을 때 종료 시각과 해제 주체를 남긴다(result는 그대로)."""
        self._execute("UPDATE events SET ended_at = ?, cleared_by = ? WHERE id = ?",
                      (now_iso(), cleared_by, event_id))

    def recent_events(self, limit: int = 20, device_id: Optional[str] = None) -> List[Dict]:
        where, params = ("WHERE device_id = ?", (device_id,)) if device_id else ("", ())
        return self._query(
            f"""SELECT id, started_at, ended_at, device_id, event_type, result, voice_result, voice_attempts,
                       duration_ms, notify_state, notified_at, cleared_by
                FROM events {where} ORDER BY id DESC LIMIT ?""",
            (*params, max(1, min(limit, 200))),
        )

    def counts_since(self, since_iso: str) -> Dict[str, Dict[str, int]]:
        """장치별 낙상 의심·경보 건수."""
        marks = ",".join("?" * len(ALERT_RESULTS))
        rows = self._query(
            f"""SELECT device_id, COUNT(*) AS events,
                       SUM(CASE WHEN result IN ({marks}) THEN 1 ELSE 0 END) AS alarms
                FROM events WHERE started_at >= ? GROUP BY device_id""",
            (*ALERT_RESULTS, since_iso))
        return {r["device_id"]: {"events": r["events"], "alarms": r["alarms"] or 0} for r in rows}

    # --- 재시작 정리 ---
    def escalate_interrupted(self) -> int:
        """결과 없이 남은 이벤트(음성 확인 도중 서버 재시작)를 경보로 올리고 알림을 곧바로 보낼 차례로 둔다.

        해제하기 전까지 경보로 남고(unfinished_alarms), 알림은 다시 보내기(due_retries)가 지연 전송한다."""
        return self._execute(
            "UPDATE events SET result = ?, notify_state = ?, notify_next_at = NULL WHERE result IS NULL",
            (ALERT_INTERRUPTED, NOTIFY_FAILED)).rowcount

    def requeue_pending_notifications(self) -> int:
        """보내는 도중(pending) 끊긴 알림을 곧바로 다시 보낼 차례로 돌린다.

        전송이 끝났는지 알 수 없으므로 다시 보낸다(중복이 누락보다 낫다). 이미 현장에서 해제된 이벤트는 보내지 않는다.
        """
        with self._conn:
            self._conn.execute(
                "UPDATE events SET notify_state = ?, notify_next_at = NULL "
                "WHERE notify_state = ? AND cleared_by IS NOT NULL", (NOTIFY_CANCELLED, NOTIFY_PENDING))
            return self._conn.execute(
                "UPDATE events SET notify_state = ?, notify_next_at = NULL WHERE notify_state = ?",
                (NOTIFY_FAILED, NOTIFY_PENDING)).rowcount

    def unfinished_alarms(self) -> List[Dict]:
        """경보가 났지만 아직 해제되지 않은 이벤트. 장치마다 가장 최근 것 하나씩."""
        marks = ",".join("?" * len(ALERT_RESULTS))
        rows = self._query(
            f"SELECT * FROM events WHERE result IN ({marks}) AND ended_at IS NULL ORDER BY id DESC", ALERT_RESULTS)
        latest: Dict[str, Dict] = {}
        for row in rows:
            latest.setdefault(row["device_id"] or "", row)
        return list(latest.values())

    def purge_older_than(self, days: int) -> int:
        """보관 기간이 지난 기록을 지운다. 끝나지 않은 이벤트(해제 전 경보)와 보낼 알림이 남은 이벤트는 둔다."""
        cutoff = now_iso(datetime.now() - timedelta(days=days))
        n = self._execute("DELETE FROM events WHERE started_at < ? AND ended_at IS NOT NULL "
                          "AND notify_state NOT IN (?, ?)", (cutoff, NOTIFY_PENDING, NOTIFY_FAILED)).rowcount
        return n + self._execute("DELETE FROM system_events WHERE at < ?", (cutoff,)).rowcount

    # --- 알림 상태 ---
    def set_notify_state(self, event_id: int, state: str, next_at: Optional[str] = None,
                         count_attempt: bool = False) -> None:
        now = now_iso()
        self._execute(
            """UPDATE events SET notify_state = ?, notify_next_at = ?,
                   notify_attempts = notify_attempts + ?,
                   notified_at = CASE WHEN ? = 'sent' THEN ? ELSE notified_at END
               WHERE id = ?""",
            (state, next_at, 1 if count_attempt else 0, state, now, event_id),
        )

    def due_retries(self, now: Optional[datetime] = None) -> List[Dict]:
        return self._query(
            "SELECT * FROM events WHERE notify_state = ? AND (notify_next_at IS NULL OR notify_next_at <= ?) "
            "ORDER BY id", (NOTIFY_FAILED, now_iso(now)))

    # --- 시스템 이벤트 (장치 이상·복구) ---
    def insert_system_event(self, kind: str, message: str, notify_state: str = NOTIFY_NONE,
                            device_id: Optional[str] = None) -> int:
        return self._execute(
            "INSERT INTO system_events (at, kind, message, notify_state, device_id) VALUES (?, ?, ?, ?, ?)",
            (now_iso(), kind, message, notify_state, device_id)).lastrowid

    def recent_system_events(self, limit: int = 20, device_ids: Optional[List[str]] = None) -> List[Dict]:
        if device_ids is None:
            return self._query("SELECT * FROM system_events ORDER BY id DESC LIMIT ?", (max(1, min(limit, 200)),))
        marks = ",".join("?" * len(device_ids)) or "NULL"
        return self._query(f"SELECT * FROM system_events WHERE device_id IN ({marks}) ORDER BY id DESC LIMIT ?",
                           (*device_ids, max(1, min(limit, 200))))

    def events_for_devices(self, device_ids: List[str], limit: int = 20) -> List[Dict]:
        marks = ",".join("?" * len(device_ids)) or "NULL"
        return self._query(
            f"""SELECT id, started_at, ended_at, device_id, event_type, result, voice_result, voice_attempts,
                       duration_ms, notify_state, notified_at, cleared_by
                FROM events WHERE device_id IN ({marks}) ORDER BY id DESC LIMIT ?""",
            (*device_ids, max(1, min(limit, 200))))

    # --- 보호자 계정 (비밀번호는 해시만 저장. server/guardians.py) ---
    @staticmethod
    def _guardian(row: Dict) -> Dict:
        row["devices"] = json.loads(row["devices"])
        row["can_clear"] = bool(row["can_clear"])
        return row

    def list_guardians(self) -> List[Dict]:
        return [self._guardian(r) for r in self._query("SELECT * FROM guardians ORDER BY id")]

    def get_guardian(self, guardian_id: int) -> Optional[Dict]:
        rows = self._query("SELECT * FROM guardians WHERE id = ?", (guardian_id,))
        return self._guardian(rows[0]) if rows else None

    def get_guardian_by_username(self, username: str) -> Optional[Dict]:
        rows = self._query("SELECT * FROM guardians WHERE username = ?", (username,))
        return self._guardian(rows[0]) if rows else None

    def insert_guardian(self, username: str, name: str, password_hash: str, devices: List[str],
                        can_clear: bool) -> int:
        return self._execute(
            "INSERT INTO guardians (username, name, password_hash, devices, can_clear, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (username, name, password_hash, json.dumps(devices, ensure_ascii=False), int(can_clear), now_iso()),
        ).lastrowid

    def update_guardian(self, guardian_id: int, *, name: Optional[str] = None, devices: Optional[List[str]] = None,
                        can_clear: Optional[bool] = None, password_hash: Optional[str] = None) -> bool:
        """바꿀 항목만 준다. 비밀번호를 바꾸면 session_version을 올려 기존 로그인을 모두 끊는다."""
        changes = []
        if name is not None:
            changes.append(("name = ?", name))
        if devices is not None:
            changes.append(("devices = ?", json.dumps(devices, ensure_ascii=False)))
        if can_clear is not None:
            changes.append(("can_clear = ?", int(can_clear)))
        if password_hash is not None:
            changes.append(("password_hash = ?, session_version = session_version + 1", password_hash))
        sets = [sql for sql, _ in changes]
        params = [value for _, value in changes]
        if not sets:
            return self.get_guardian(guardian_id) is not None
        return self._execute(f"UPDATE guardians SET {', '.join(sets)} WHERE id = ?",
                             (*params, guardian_id)).rowcount == 1

    def delete_guardian(self, guardian_id: int) -> bool:
        return self._execute("DELETE FROM guardians WHERE id = ?", (guardian_id,)).rowcount == 1

    def touch_guardian_login(self, guardian_id: int) -> None:
        self._execute("UPDATE guardians SET last_login_at = ? WHERE id = ?", (now_iso(), guardian_id))

    # --- 장치 (한 번이라도 접속한 라즈베리파이. 서버를 다시 켜도 연결 끊김을 감시하기 위함) ---
    def list_devices(self) -> List[Dict]:
        return self._query("SELECT * FROM devices ORDER BY first_seen")

    def save_device(self, device_id: str, name: str) -> None:
        now = now_iso()
        self._execute("INSERT INTO devices (device_id, name, first_seen, last_seen) VALUES (?, ?, ?, ?) "
                      "ON CONFLICT(device_id) DO UPDATE SET name = excluded.name, last_seen = excluded.last_seen",
                      (device_id, name, now, now))

    def touch_device(self, device_id: str) -> None:
        self._execute("UPDATE devices SET last_seen = ? WHERE device_id = ?", (now_iso(), device_id))

    def delete_device(self, device_id: str) -> bool:
        return self._execute("DELETE FROM devices WHERE device_id = ?", (device_id,)).rowcount == 1

    # --- 관리자 로그인 세션 (토큰은 해시만 저장) ---
    def add_admin_session(self, token_hash: str, expires_at: float, password_tag: str) -> None:
        self._execute("INSERT INTO admin_sessions (token_hash, expires_at, password_tag) VALUES (?, ?, ?)",
                      (token_hash, expires_at, password_tag))

    def admin_session(self, token_hash: str) -> Optional[Dict]:
        rows = self._query("SELECT * FROM admin_sessions WHERE token_hash = ?", (token_hash,))
        return rows[0] if rows else None

    def delete_admin_session(self, token_hash: str) -> None:
        self._execute("DELETE FROM admin_sessions WHERE token_hash = ?", (token_hash,))

    def purge_admin_sessions(self, now: float) -> None:
        self._execute("DELETE FROM admin_sessions WHERE expires_at < ?", (now,))

    # --- 서버 값 (세션 서명 키 등) ---
    def kv_get(self, key: str) -> Optional[str]:
        rows = self._query("SELECT value FROM kv WHERE key = ?", (key,))
        return rows[0]["value"] if rows else None

    def kv_set(self, key: str, value: str) -> None:
        self._execute("INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                      (key, value))
