"""SQLite 이벤트 기록.

기록 항목은 문서(ARCHITECTURE.md)의 최소 정보 원칙을 따른다:
발생 시각, 판단 상태, 확인 결과, 알림 결과. 영상·음성 원본은 저장하지 않는다.
"""

import json
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

# result 값
CLEARED_VOICE = "CLEARED_VOICE"      # 음성으로 '괜찮다' 응답
CLEARED_BUTTON = "CLEARED_BUTTON"    # 경보 전에 화면 버튼으로 해제
ALERT_HELP = "ALERT_HELP"            # 음성으로 도움 요청
ALERT_NO_REPLY = "ALERT_NO_REPLY"    # 무응답·불분명
ALERT_VOICE_FAIL = "ALERT_VOICE_FAIL"  # 마이크·음성 인식 장치 오류로 확인 불가
INTERRUPTED = "INTERRUPTED"          # 처리 도중 시스템 재시작
ALERT_RESULTS = (ALERT_HELP, ALERT_NO_REPLY, ALERT_VOICE_FAIL)

# notify_state 값
NOTIFY_NONE = "none"          # 알림 대상 아님 (경보로 가지 않음)
NOTIFY_PENDING = "pending"    # 전송 중
NOTIFY_SENT = "sent"
NOTIFY_FAILED = "failed"      # 실패, 재시도 대기
NOTIFY_DISABLED = "disabled"  # 알림 채널 미설정 (시험 모드)
NOTIFY_CANCELLED = "cancelled"  # 전송 전에 사용자가 해제해 재시도 중단

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    event_type TEXT NOT NULL,
    result TEXT,
    voice_result TEXT,
    voice_text TEXT,
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
    notify_state TEXT NOT NULL DEFAULT 'none'
);
"""


def now_iso() -> str:
    return datetime.now().isoformat(timespec="milliseconds")


class Database:
    def __init__(self, db_path: Path):
        db_path = Path(db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        # Flask 요청 스레드, 음성 확인 스레드, 재전송 스레드가 함께 쓰므로 잠금으로 직렬화한다.
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock, self._conn:
            # WAL: 전원 차단 시 손상 위험을 줄이고 읽기·쓰기가 서로 막지 않게 한다.
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(SCHEMA)

    def _execute(self, sql: str, params=()) -> sqlite3.Cursor:
        with self._lock, self._conn:
            return self._conn.execute(sql, params)

    def _query(self, sql: str, params=()) -> List[Dict]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def close(self) -> None:
        self._conn.close()

    # --- 이벤트 ---
    def insert_event(self, event_type: str, features: Optional[Dict]) -> int:
        cur = self._execute(
            "INSERT INTO events (started_at, event_type, feature_snapshot) VALUES (?, ?, ?)",
            (now_iso(), event_type, json.dumps(features or {}, ensure_ascii=False)),
        )
        return cur.lastrowid

    def get_event(self, event_id: int) -> Optional[Dict]:
        rows = self._query("SELECT * FROM events WHERE id = ?", (event_id,))
        return rows[0] if rows else None

    def set_result(self, event_id: int, result: str, voice_result: Optional[str] = None,
                   voice_text: Optional[str] = None, ended: bool = True) -> None:
        ended_at = now_iso() if ended else None
        self._execute(
            """UPDATE events SET result = ?, voice_result = ?, voice_text = ?,
                   ended_at = COALESCE(?, ended_at),
                   duration_ms = CAST((julianday(COALESCE(?, ?)) - julianday(started_at)) * 86400000 AS INTEGER)
               WHERE id = ?""",
            (result, voice_result, voice_text, ended_at, ended_at, now_iso(), event_id),
        )

    def close_event(self, event_id: int, cleared_by: str) -> None:
        """경보 이후 사용자가 해제했을 때 종료 시각과 해제 주체를 남긴다(result는 유지)."""
        self._execute("UPDATE events SET ended_at = ?, cleared_by = ? WHERE id = ?",
                      (now_iso(), cleared_by, event_id))

    def recent_events(self, limit: int = 20) -> List[Dict]:
        return self._query(
            """SELECT id, started_at, ended_at, event_type, result, voice_result, duration_ms,
                      notify_state, notified_at, cleared_by
               FROM events ORDER BY id DESC LIMIT ?""",
            (max(1, min(limit, 200)),),
        )

    def mark_interrupted(self) -> int:
        """재시작 시 결과 없이 남은 이벤트를 '중단'으로 정리한다."""
        cur = self._execute("UPDATE events SET result = ?, ended_at = ? WHERE result IS NULL",
                            (INTERRUPTED, now_iso()))
        return cur.rowcount

    def requeue_pending_notifications(self) -> int:
        """재시작 시 전송 중(pending)으로 남은 경보 알림을 재전송 대기로 돌린다.

        전송이 끝났는지 알 수 없으므로 다시 보낸다(중복이 누락보다 낫다).
        이미 현장에서 해제된 이벤트는 재시도하지 않는다.
        """
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE events SET notify_state = ?, notify_next_at = NULL "
                "WHERE notify_state = ? AND cleared_by IS NOT NULL",
                (NOTIFY_CANCELLED, NOTIFY_PENDING),
            )
            cur = self._conn.execute(
                "UPDATE events SET notify_state = ?, notify_next_at = NULL WHERE notify_state = ?",
                (NOTIFY_FAILED, NOTIFY_PENDING),
            )
        return cur.rowcount

    def unfinished_alarm(self) -> Optional[Dict]:
        """경보가 났지만 아직 해제되지 않은 가장 최근 이벤트."""
        rows = self._query(
            "SELECT * FROM events WHERE result IN (?, ?, ?) AND ended_at IS NULL ORDER BY id DESC LIMIT 1",
            ALERT_RESULTS,
        )
        return rows[0] if rows else None

    def purge_older_than(self, days: int) -> int:
        cutoff = (datetime.now() - timedelta(days=days)).isoformat(timespec="milliseconds")
        cur = self._execute(
            "DELETE FROM events WHERE started_at < ? AND notify_state NOT IN (?, ?)",
            (cutoff, NOTIFY_PENDING, NOTIFY_FAILED),
        )
        return cur.rowcount

    # --- 알림 상태 ---
    def set_notify_state(self, event_id: int, state: str, next_at: Optional[str] = None,
                         count_attempt: bool = False) -> None:
        self._execute(
            """UPDATE events SET notify_state = ?, notify_next_at = ?,
                   notify_attempts = notify_attempts + ?,
                   notified_at = CASE WHEN ? = 'sent' THEN ? ELSE notified_at END
               WHERE id = ?""",
            (state, next_at, 1 if count_attempt else 0, state, now_iso(), event_id),
        )

    def due_retries(self) -> List[Dict]:
        return self._query(
            "SELECT * FROM events WHERE notify_state = ? AND (notify_next_at IS NULL OR notify_next_at <= ?) "
            "ORDER BY id",
            (NOTIFY_FAILED, now_iso()),
        )

    # --- 시스템 이벤트 (장치 이상·복구·일일 보고 기록) ---
    def insert_system_event(self, kind: str, message: str, notify_state: str = NOTIFY_NONE) -> int:
        cur = self._execute("INSERT INTO system_events (at, kind, message, notify_state) VALUES (?, ?, ?, ?)",
                            (now_iso(), kind, message, notify_state))
        return cur.lastrowid

    def recent_system_events(self, limit: int = 20) -> List[Dict]:
        return self._query("SELECT * FROM system_events ORDER BY id DESC LIMIT ?", (max(1, min(limit, 200)),))

    def counts_since(self, since_iso: str) -> Dict[str, int]:
        rows = self._query(
            "SELECT COUNT(*) AS events, SUM(CASE WHEN result IN (?, ?, ?) THEN 1 ELSE 0 END) AS alarms "
            "FROM events WHERE started_at >= ?",
            (*ALERT_RESULTS, since_iso),
        )
        return {"events": rows[0]["events"] or 0, "alarms": rows[0]["alarms"] or 0}
