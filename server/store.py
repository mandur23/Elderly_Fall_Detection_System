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
ALERT_HELP = "ALERT_HELP"                  # 음성으로 도움 요청
ALERT_NO_REPLY = "ALERT_NO_REPLY"          # 무응답·불분명
ALERT_VOICE_FAIL = "ALERT_VOICE_FAIL"      # 마이크·스피커·인식 모델 오류로 확인 불가
ALERT_DISCONNECTED = "ALERT_DISCONNECTED"  # 음성 확인 중 장치 연결 끊김
INTERRUPTED = "INTERRUPTED"                # 처리 도중 서버 재시작
ALERT_RESULTS = (ALERT_HELP, ALERT_NO_REPLY, ALERT_VOICE_FAIL, ALERT_DISCONNECTED)

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
    notify_state TEXT NOT NULL DEFAULT 'none'
);
"""


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

    def recent_events(self, limit: int = 20) -> List[Dict]:
        return self._query(
            """SELECT id, started_at, ended_at, device_id, event_type, result, voice_result, voice_attempts,
                      duration_ms, notify_state, notified_at, cleared_by
               FROM events ORDER BY id DESC LIMIT ?""",
            (max(1, min(limit, 200)),),
        )

    # --- 재시작 정리 ---
    def mark_interrupted(self) -> int:
        """결과 없이 남은 이벤트(처리 도중 재시작)를 '중단'으로 정리한다."""
        return self._execute("UPDATE events SET result = ?, ended_at = ? WHERE result IS NULL",
                             (INTERRUPTED, now_iso())).rowcount

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

    def unfinished_alarm(self) -> Optional[Dict]:
        """경보가 났지만 아직 해제되지 않은 가장 최근 이벤트."""
        marks = ",".join("?" * len(ALERT_RESULTS))
        rows = self._query(
            f"SELECT * FROM events WHERE result IN ({marks}) AND ended_at IS NULL ORDER BY id DESC LIMIT 1",
            ALERT_RESULTS)
        return rows[0] if rows else None

    def purge_older_than(self, days: int) -> int:
        cutoff = now_iso(datetime.now() - timedelta(days=days))
        n = self._execute("DELETE FROM events WHERE started_at < ? AND notify_state NOT IN (?, ?)",
                          (cutoff, NOTIFY_PENDING, NOTIFY_FAILED)).rowcount
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
    def insert_system_event(self, kind: str, message: str, notify_state: str = NOTIFY_NONE) -> int:
        return self._execute("INSERT INTO system_events (at, kind, message, notify_state) VALUES (?, ?, ?, ?)",
                             (now_iso(), kind, message, notify_state)).lastrowid

    def recent_system_events(self, limit: int = 20) -> List[Dict]:
        return self._query("SELECT * FROM system_events ORDER BY id DESC LIMIT ?", (max(1, min(limit, 200)),))
