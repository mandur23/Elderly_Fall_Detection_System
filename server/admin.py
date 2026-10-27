"""시스템 관리자 페이지(/admin)와 관리자 API(/admin/api/...).

관리자는 이 시스템을 운영하는 팀이고, 관리자 계정은 따로 없다. 대신 **서버 PC 자신에서 연 요청만**
받는다. 그래서 이 파일의 핵심은 '이 요청이 정말 서버 PC의 브라우저에서 왔는가'를 확인하는
local_access_problem()이다. 검사는 다섯 가지다.

1. 접속한 주소(request.client.host)가 루프백(127.0.0.0/8, ::1)인가.
   서버 앞에 리버스 프록시가 없으므로 X-Forwarded-For 같은 헤더는 절대 믿지 않는다.
2. Host 헤더가 localhost, 127.0.0.1, [::1] 중 하나인가.
   서버 PC의 브라우저로 연 악성 사이트가 자기 도메인을 127.0.0.1로 바꿔(DNS 리바인딩) 같은
   출처인 척 관리자 API를 읽는 것을 막는다. 이때 Host 헤더에는 그 사이트의 도메인이 남는다.
3. 상태를 바꾸는 요청(GET·HEAD가 아닌 것)은 Origin이 이 서버(루프백 이름, 같은 Host)이고
   X-Admin-Request: 1 헤더가 있어야 한다. 사용자 정의 헤더가 붙은 다른 출처 요청은 브라우저가
   먼저 CORS 사전 요청을 보내는데 이 서버는 허락하지 않으므로, 다른 사이트의 폼·fetch로
   관리자 동작을 몰래 시키는 것(CSRF)을 막는다.
4. 브라우저가 붙이는 Sec-Fetch-Site가 있으면 same-origin(관리자 페이지 자신의 링크·fetch) 또는
   none(주소창에 직접 입력, 즐겨찾기)이어야 한다. 읽기(GET)도 막는다. 서버 PC에서 연 다른 사이트가
   a.href='http://localhost:8443/admin/api/events.csv'를 누르게 하면 모든 사용자의 이벤트가
   그 PC에 내려받아지기 때문이다. 헤더가 없으면 브라우저가 아닌 로컬 도구(curl 등)로 보고 허용한다.
5. 실패하면 404. 같은 Wi-Fi의 다른 기기에게 관리자 페이지가 있다는 것조차 알리지 않는다.

검사는 두 곳에서 같은 함수로 한다.
- AdminGuard(미들웨어): /admin 아래 모든 요청을 라우팅·본문 해석 전에 막는다. 라우트 의존성만 쓰면
  잘못된 JSON은 422, 없는 메서드는 405로 먼저 답해 관리자 API가 있다는 것이 드러난다.
- require_local(의존성): 모든 관리자 라우트에 붙는다. 미들웨어 없이 라우터만 붙인 앱에서도 막히게 한다.

관리자 요청은 로그인 세션 쿠키를 쓰지 않는다(어느 사용자 계정과도 묶이지 않는다).
상태를 바꾸는 관리자 동작은 모두 log.warning("관리자: ...")으로 남긴다.
"""

import base64
import csv
import io
import ipaddress
import json
import logging
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field
from starlette.datastructures import MutableHeaders

from server.auth import normalize_email
from server.db import (
    ALERT_HELP,
    ALERT_NO_REPLY,
    ALERT_RESULTS,
    ALERT_VOICE_FAIL,
    CLEARED_BUTTON,
    CLEARED_VOICE,
    GUARDIAN_ACCEPTED,
    GUARDIAN_PENDING,
    INTERRUPTED,
    LABEL_FALSE_ALARM,
    NOTIFY_FAILED,
    NOTIFY_SENT,
    REVIEW_LABELS,
)
from server.notify import CHANNEL_PUSH, CHANNEL_WEBSOCKET, RESULT_LABELS, event_public_dict
from server.routes_realtime import CLOSE_FORBIDDEN

log = logging.getLogger("server.admin")

ADMIN_PREFIX = "/admin"
# Host·Origin에 올 수 있는 서버 PC 자신의 이름(urlparse는 [::1]의 괄호를 떼고 돌려준다)
LOCAL_HOSTNAMES = ("localhost", "127.0.0.1", "::1")
SAFE_METHODS = ("GET", "HEAD")
ADMIN_REQUEST_HEADER = "x-admin-request"
# Sec-Fetch-Site 중 받는 값: 같은 출처(관리자 페이지 자신), none(주소창 입력·즐겨찾기)
ALLOWED_FETCH_SITES = ("same-origin", "none")
# 관리자 응답에 붙이는 헤더.
# - 다른 사이트가 관리자 페이지를 iframe에 넣고 버튼을 누르게 속이는 것(클릭재킹)을 막는다.
#   iframe 안의 요청은 진짜 localhost 출처라 위 3번 검사를 통과하기 때문이다.
# - 사용자 정보가 브라우저 디스크 캐시에 남지 않게 한다.
ADMIN_RESPONSE_HEADERS = {
    "Cache-Control": "no-store",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}

# 결과가 아직 없는 이벤트(음성 확인 중)를 통계에서 부르는 이름
RESULT_SUSPECT = "SUSPECT"
ALL_RESULTS = (CLEARED_VOICE, CLEARED_BUTTON, ALERT_HELP, ALERT_NO_REPLY, ALERT_VOICE_FAIL, INTERRUPTED)
CLEARED_RESULTS = (CLEARED_VOICE, CLEARED_BUTTON)
# 이벤트 목록 result 필터: 결과 하나, 또는 묶음(ALERT=경보 전체, CLEARED=해제 전체), SUSPECT=음성 확인 중
RESULT_FILTERS = dict({r: (r,) for r in ALL_RESULTS}, ALERT=ALERT_RESULTS, CLEARED=CLEARED_RESULTS)
LABEL_NONE = "none"  # 이벤트 목록 label 필터: 아직 라벨이 없는 이벤트
NOTE_MAX_LENGTH = 200
EVENTS_PAGE_MAX = 500
RECENT_FAILURES = 20
USER_RECENT_EVENTS = 20


# ---------------------------------------------------------------------------
# 서버 PC에서 온 요청인지 확인
# ---------------------------------------------------------------------------

def is_loopback_address(address: Optional[str]) -> bool:
    """127.0.0.0/8, ::1, ::ffff:127.x.x.x(IPv4를 담은 IPv6)이면 True. 주소가 아니면 False."""
    try:
        ip = ipaddress.ip_address(address or "")
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_loopback


def _hostname(host_header: Optional[str]) -> Optional[str]:
    """Host 헤더 'localhost:8443', '[::1]:8443'에서 이름만 꺼낸다. 이상한 값이면 None."""
    if not host_header or any(c in host_header for c in "@/\\?# \t"):
        return None
    try:
        parsed = urlparse("//" + host_header)
        parsed.port  # 포트가 숫자가 아니면 ValueError
    except ValueError:
        return None
    return (parsed.hostname or "").lower() or None


def _origin_is_this_server(origin: Optional[str], host_header: str) -> bool:
    """Origin이 루프백 이름이고, 지금 접속한 주소(Host: 이름과 포트)와 같은가."""
    if not origin:
        return False
    try:
        url = urlparse(origin)
        url.port
    except ValueError:
        return False
    return (url.scheme in ("http", "https")
            and (url.hostname or "").lower() in LOCAL_HOSTNAMES
            and url.netloc.lower() == host_header.lower())


def local_access_problem(request: Request) -> Optional[str]:
    """관리자 요청을 받으면 안 되는 이유. 받아도 되면 None (머리말의 검사 1~4)."""
    if not request.app.state.settings.admin_enabled:
        return "관리자 페이지가 꺼져 있음(FALL_ADMIN=0)"
    client = request.client.host if request.client else None
    if not is_loopback_address(client):
        return "서버 PC가 아닌 곳에서 접속"
    host_header = request.headers.get("host") or ""
    if _hostname(host_header) not in LOCAL_HOSTNAMES:
        return "Host 헤더가 서버 PC 이름이 아님(DNS 리바인딩 의심)"
    fetch_site = request.headers.get("sec-fetch-site")
    if fetch_site is not None and fetch_site.lower() not in ALLOWED_FETCH_SITES:
        return f"다른 사이트에서 연 요청(Sec-Fetch-Site: {fetch_site})"
    if request.method not in SAFE_METHODS:
        if not _origin_is_this_server(request.headers.get("origin"), host_header):
            return "다른 출처에서 보낸 변경 요청(CSRF 의심)"
        if request.headers.get(ADMIN_REQUEST_HEADER) != "1":
            return "X-Admin-Request 헤더가 없는 변경 요청"
    return None


def _log_refused(request: Request, problem: str) -> None:
    client = request.client.host if request.client else None
    log.warning("관리자 페이지 접근 거부: %s (client=%s, host=%s, %s %s)",
                problem, client, request.headers.get("host"), request.method, request.url.path)


def require_local(request: Request) -> None:
    """모든 관리자 라우트의 의존성. 서버 PC에서 온 요청이 아니면 404."""
    problem = local_access_problem(request)
    if problem:
        _log_refused(request, problem)
        raise HTTPException(status_code=404, detail="Not Found")


def is_admin_path(path: str) -> bool:
    return path == ADMIN_PREFIX or path.startswith(ADMIN_PREFIX + "/")


class AdminGuard:
    """/admin 아래 요청을 라우팅 전에 검사하는 ASGI 미들웨어. 통과한 응답에는 ADMIN_RESPONSE_HEADERS를 붙인다.

    거부할 때는 없는 주소와 똑같은 응답({"detail": "Not Found"}, 404)을 돌려준다.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not is_admin_path(scope["path"]):
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        problem = local_access_problem(request)
        if problem:
            _log_refused(request, problem)
            await JSONResponse({"detail": "Not Found"}, status_code=404)(scope, receive, send)
            return

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in ADMIN_RESPONSE_HEADERS.items():
                    headers[name] = value
            await send(message)

        await self.app(scope, receive, send_with_headers)


router = APIRouter(dependencies=[Depends(require_local)])


# ---------------------------------------------------------------------------
# 공통 도우미
# ---------------------------------------------------------------------------

def _state(request: Request):
    return request.app.state


def _seconds_between(start: Optional[str], end: Optional[str]) -> Optional[float]:
    if not start or not end:
        return None
    try:
        return round((datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds(), 1)
    except ValueError:
        return None


def _average(values: List[float]) -> Optional[float]:
    return round(sum(values) / len(values), 1) if values else None


def _rate(part: int, whole: int) -> Optional[float]:
    return round(part / whole, 4) if whole else None


def _device_view(state, device: Dict[str, Any]) -> Dict[str, Any]:
    """기기 실시간 상태(DeviceHub.status: online, camera_ok, state, fps, owner_name ...)에 등록·접속 시각을 더한다."""
    view = dict(state.devices.status(device["id"], device))
    view["created_at"] = device["created_at"]
    view["last_seen_at"] = device["last_seen_at"]
    return view


def _all_devices(state, owner_id: Optional[int] = None) -> List[Dict[str, Any]]:
    # 열을 직접 골라 기기 키 해시(key_hash)는 읽지 않는다.
    sql = "SELECT id, owner_id, name, kind, created_at, last_seen_at FROM devices"
    params: tuple = ()
    if owner_id is not None:
        sql += " WHERE owner_id = ?"
        params = (owner_id,)
    return [_device_view(state, d) for d in state.db.query(sql + " ORDER BY id", params)]


# --- 사용자 ---

USER_SELECT = f"""
SELECT u.id, u.email, u.name, u.created_at, u.disabled_at,
    (SELECT COUNT(*) FROM devices d WHERE d.owner_id = u.id) AS device_count,
    (SELECT COUNT(*) FROM guardians g WHERE g.user_id = u.id AND g.status = '{GUARDIAN_ACCEPTED}') AS guardians_accepted,
    (SELECT COUNT(*) FROM guardians g WHERE g.user_id = u.id AND g.status = '{GUARDIAN_PENDING}') AS guardians_pending,
    (SELECT COUNT(*) FROM guardians g WHERE g.guardian_id = u.id AND g.status = '{GUARDIAN_ACCEPTED}') AS wards_count
FROM users u
"""


def _user_rows(state, where: str = "", params: tuple = ()) -> List[Dict[str, Any]]:
    rows = state.db.query(USER_SELECT + where + " ORDER BY u.id", params)
    for row in rows:
        row["online_device_count"] = sum(
            1 for d in state.db.query("SELECT id FROM devices WHERE owner_id = ?", (row["id"],))
            if state.devices.is_online(d["id"])
        )
    return rows


def _user_or_404(state, user_id: int) -> Dict[str, Any]:
    rows = _user_rows(state, "WHERE u.id = ?", (user_id,))
    if not rows:
        raise HTTPException(status_code=404, detail="사용자를 찾을 수 없습니다.")
    return rows[0]


def _links(state, user_id: int, as_guardian: bool) -> List[Dict[str, Any]]:
    """as_guardian=False: 이 사용자가 등록한 보호자들. True: 이 사용자를 보호자로 등록한 사람들.

    마이페이지와 달리 요청 중(pending)인 상대의 이름도 그대로 보여 준다(운영자 화면).
    """
    me, other = ("guardian_id", "user_id") if as_guardian else ("user_id", "guardian_id")
    rows = state.db.query(
        f"SELECT g.id AS link_id, g.status, g.created_at, u.id, u.email, u.name, u.disabled_at "
        f"FROM guardians g JOIN users u ON u.id = g.{other} WHERE g.{me} = ? ORDER BY g.id",
        (user_id,),
    )
    return [{
        "link_id": r["link_id"],
        "status": r["status"],
        "created_at": r["created_at"],
        "user": {"id": r["id"], "email": r["email"], "name": r["name"], "disabled_at": r["disabled_at"]},
    } for r in rows]


# --- 이벤트 ---

EVENTS_FROM = (
    "FROM events e JOIN devices d ON d.id = e.device_id JOIN users u ON u.id = d.owner_id"
)
EVENTS_SELECT = (
    "SELECT e.*, d.name AS device_name, d.owner_id, u.name AS owner_name, u.email AS owner_email "
    + EVENTS_FROM
)


def _parse_day(value: Optional[str], name: str) -> Optional[date]:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"{name}은(는) YYYY-MM-DD 형식이어야 합니다.")


def event_filters(
    date_from: Optional[str] = Query(None, alias="from", description="이 날(YYYY-MM-DD)부터"),
    date_to: Optional[str] = Query(None, alias="to", description="이 날(YYYY-MM-DD)까지(그날 포함)"),
    result: Optional[str] = Query(None, description="결과 하나, ALERT, CLEARED, SUSPECT(음성 확인 중)"),
    label: Optional[str] = Query(None, description="검토 라벨 하나 또는 none(라벨 없음)"),
    device_id: Optional[int] = None,
    user_id: Optional[int] = None,
) -> Tuple[str, tuple]:
    """이벤트 목록·CSV가 함께 쓰는 필터. (WHERE 절, 값)을 돌려준다."""
    conditions: List[str] = []
    params: List[Any] = []
    start, end = _parse_day(date_from, "from"), _parse_day(date_to, "to")
    if start is not None:
        conditions.append("e.started_at >= ?")
        params.append(start.isoformat())
    if end is not None:
        conditions.append("e.started_at < ?")  # 그날 하루 전체를 넣으려고 다음 날 0시 전까지
        params.append((end + timedelta(days=1)).isoformat())
    if result:
        if result == RESULT_SUSPECT:
            conditions.append("e.result IS NULL")
        elif result in RESULT_FILTERS:
            results = RESULT_FILTERS[result]
            conditions.append(f"e.result IN ({','.join('?' for _ in results)})")
            params.extend(results)
        else:
            raise HTTPException(status_code=400, detail="알 수 없는 결과 필터입니다.")
    if label:
        if label == LABEL_NONE:
            conditions.append("e.review_label IS NULL")
        elif label in REVIEW_LABELS:
            conditions.append("e.review_label = ?")
            params.append(label)
        else:
            raise HTTPException(status_code=400, detail="알 수 없는 라벨 필터입니다.")
    if device_id is not None:
        conditions.append("e.device_id = ?")
        params.append(device_id)
    if user_id is not None:
        conditions.append("d.owner_id = ?")
        params.append(user_id)
    where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
    return where, tuple(params)


def _empty_notification_counts() -> Dict[str, Dict[str, int]]:
    return {channel: {NOTIFY_SENT: 0, NOTIFY_FAILED: 0} for channel in (CHANNEL_WEBSOCKET, CHANNEL_PUSH)}


def _notification_counts(state, where: str, params: tuple) -> Dict[int, Dict[str, Dict[str, int]]]:
    """필터에 맞는 이벤트별·채널별 알림 성공/실패 건수."""
    rows = state.db.query(
        f"SELECT n.event_id, n.channel, n.state, COUNT(*) AS n FROM notifications n "
        f"JOIN events e ON e.id = n.event_id JOIN devices d ON d.id = e.device_id "
        f"JOIN users u ON u.id = d.owner_id{where} GROUP BY n.event_id, n.channel, n.state",
        params,
    )
    counts: Dict[int, Dict[str, Dict[str, int]]] = {}
    for r in rows:
        per_event = counts.setdefault(r["event_id"], _empty_notification_counts())
        if r["channel"] in per_event and r["state"] in per_event[r["channel"]]:
            per_event[r["channel"]][r["state"]] = r["n"]
    return counts


def _features(snapshot: Optional[str]) -> Optional[Dict[str, Any]]:
    try:
        value = json.loads(snapshot) if snapshot else None
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _event_view(row: Dict[str, Any], counts: Dict[int, Dict[str, Dict[str, int]]]) -> Dict[str, Any]:
    """보호자 화면과 같은 이벤트 모양(event_public_dict)에 운영·시험 검토용 값을 더한다."""
    view = event_public_dict(row, {"name": row["device_name"], "owner_id": row["owner_id"]},
                             {"name": row["owner_name"]})
    view.update(
        owner_email=row["owner_email"],
        alarm_at=row["alarm_at"],
        duration_seconds=_seconds_between(row["started_at"], row["ended_at"]),
        review_label=row["review_label"],
        review_note=row["review_note"],
        # 낙상 판정에 쓴 특징값(하강 속도, 몸통 각도 등). 임계값을 시험 결과로 정할 때 본다.
        features=_features(row["feature_snapshot"]),
        notifications=counts.get(row["id"], _empty_notification_counts()),
    )
    return view


def _query_events(state, where: str, params: tuple, limit: Optional[int] = None,
                  offset: int = 0) -> List[Dict[str, Any]]:
    sql = EVENTS_SELECT + where + " ORDER BY e.id DESC"
    page: tuple = ()
    if limit is not None:
        sql += " LIMIT ? OFFSET ?"
        page = (limit, offset)
    rows = state.db.query(sql, params + page)
    counts = _notification_counts(state, where, params)
    return [_event_view(row, counts) for row in rows]


def _event_or_404(state, event_id: int) -> Dict[str, Any]:
    events = _query_events(state, " WHERE e.id = ?", (event_id,))
    if not events:
        raise HTTPException(status_code=404, detail="이벤트를 찾을 수 없습니다.")
    return events[0]


# ---------------------------------------------------------------------------
# 페이지
# ---------------------------------------------------------------------------

@router.get("/admin", include_in_schema=False)
async def admin_page(request: Request):
    path = Path(_state(request).settings.web_dir) / "admin.html"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="페이지가 아직 없습니다.")
    return FileResponse(path, media_type="text/html")


# 관리자 페이지 전용 스크립트·스타일(web/admin/). /static에 두면 같은 Wi-Fi의 누구나 받아 가
# 관리자 페이지가 있다는 것과 관리자 API 모양을 알 수 있으므로, /admin 아래에서 같은 검사를 거쳐 준다.
# 정해 둔 파일만 연결한다(주소로 다른 파일을 고를 수 없게). 공용 모듈(api.js, ui.js)은 /static에서 받는다.
ADMIN_ASSETS = {
    "admin.js": "application/javascript",
    "admin.css": "text/css",
}


def _asset_handler(name: str, media_type: str):
    async def asset(request: Request):
        path = Path(_state(request).settings.web_dir) / "admin" / name
        if not path.is_file():
            raise HTTPException(status_code=404, detail="Not Found")
        return FileResponse(path, media_type=media_type)
    asset.__name__ = "admin_asset_" + name.replace(".", "_")
    return asset


for _name, _media_type in ADMIN_ASSETS.items():
    router.add_api_route(f"{ADMIN_PREFIX}/{_name}", _asset_handler(_name, _media_type), methods=["GET"],
                         include_in_schema=False)


# ---------------------------------------------------------------------------
# 현황
# ---------------------------------------------------------------------------

@router.get("/admin/api/overview")
async def overview(request: Request):
    state = _state(request)
    settings, db = state.settings, state.db
    devices = _all_devices(state)
    active_events = state.events.all_active_events()

    def count(sql: str, params: tuple = ()) -> int:
        return db.query_one(sql, params)["n"]

    failures = db.query(
        "SELECT n.id, n.event_id, n.guardian_id, g.name AS guardian_name, n.kind, n.channel, n.detail, "
        "n.created_at, d.name AS device_name, u.name AS owner_name "
        "FROM notifications n JOIN users g ON g.id = n.guardian_id JOIN events e ON e.id = n.event_id "
        "JOIN devices d ON d.id = e.device_id JOIN users u ON u.id = d.owner_id "
        "WHERE n.state = ? ORDER BY n.id DESC LIMIT ?",
        (NOTIFY_FAILED, RECENT_FAILURES),
    )
    started_at = state.started_at
    return {
        "server": {
            "pose_backend": settings.pose_backend,
            "stt_backend": settings.stt_backend,
            "push_enabled": state.push.enabled,
            "https": settings.https_enabled,
            "started_at": datetime.fromtimestamp(started_at).isoformat(timespec="seconds"),
            "uptime_seconds": int(time.time() - started_at),
            "thresholds": {
                "descent_velocity_peak": settings.descent_velocity_peak,
                "t1_lying_seconds": settings.t1_lying_seconds,
                "voice_reply_timeout_seconds": settings.voice_reply_timeout_seconds,
                "voice_max_attempts": settings.voice_max_attempts,
            },
        },
        "counts": {
            "users": count("SELECT COUNT(*) AS n FROM users"),
            "disabled_users": count("SELECT COUNT(*) AS n FROM users WHERE disabled_at IS NOT NULL"),
            "devices": len(devices),
            "online_devices": sum(1 for d in devices if d["online"]),
            "guardian_links_accepted": count("SELECT COUNT(*) AS n FROM guardians WHERE status = ?",
                                             (GUARDIAN_ACCEPTED,)),
            "guardian_links_pending": count("SELECT COUNT(*) AS n FROM guardians WHERE status = ?",
                                            (GUARDIAN_PENDING,)),
            "events_today": count("SELECT COUNT(*) AS n FROM events WHERE started_at >= ?",
                                  (date.today().isoformat(),)),
            "active_events": len(active_events),
        },
        "devices": devices,
        "active_events": active_events,
        "notification_failures": failures,
    }


def _detection_thresholds(settings) -> Dict[str, float]:
    """실시간 보기 옆에 보여 줄 낙상 판정 기준값(server/detection/fsm.py가 쓰는 값 전부)."""
    return {
        "descent_velocity_peak": settings.descent_velocity_peak,
        "t1_lying_seconds": settings.t1_lying_seconds,
        "lying_gap_seconds": settings.lying_gap_seconds,
        "falling_timeout_seconds": settings.falling_timeout_seconds,
        "torso_angle_lying": settings.torso_angle_lying,
        "aspect_ratio_lying": settings.aspect_ratio_lying,
        "torso_angle_standing": settings.torso_angle_standing,
        "aspect_ratio_standing": settings.aspect_ratio_standing,
        "camera_stale_seconds": settings.camera_stale_seconds,
    }


@router.get("/admin/api/devices/{device_id}/live")
async def device_live(device_id: int, request: Request, after: int = Query(0, ge=0)):
    """기기가 보낸 가장 최근 장면과 그 장면의 낙상 판정 상태(관리자 화면의 실시간 보기).

    관리자 화면이 0.2초마다 부른다. after에 이전 응답의 seq를 주면 새 장면이 없을 때 204로 답해
    같은 장면을 다시 보내지 않는다. 영상은 서버 메모리의 마지막 한 장뿐이고 어디에도 저장하지 않으며,
    다른 관리자 API처럼 서버 PC에서만 열린다(AdminGuard).
    """
    state = _state(request)
    if state.accounts.get_device(device_id) is None:
        raise HTTPException(status_code=404, detail="기기가 없습니다.")
    thresholds = _detection_thresholds(state.settings)
    view = state.devices.live_view(device_id)
    if view is None:
        return {"device_id": device_id, "online": state.devices.is_online(device_id), "seq": 0,
                "frame": None, "thresholds": thresholds}
    if after and view["seq"] == after:
        return Response(status_code=204)
    frame = view.pop("frame")
    view["online"] = True
    view["frame"] = base64.b64encode(frame).decode("ascii")
    # 휴대폰·Pi는 JPEG를 보낸다. 시험용 가짜 추정기의 프레임은 JSON이라 화면에는 그릴 수 없다.
    view["frame_mime"] = "image/jpeg" if frame[:2] == b"\xff\xd8" else "application/octet-stream"
    view["thresholds"] = thresholds
    return view


# ---------------------------------------------------------------------------
# 사용자
# ---------------------------------------------------------------------------

class DeleteUserBody(BaseModel):
    confirm_email: str = Field("", max_length=254)


def _like_pattern(text: str) -> str:
    """LIKE 검색어. %와 _를 글자 그대로 찾도록 \\로 감싼다."""
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


@router.get("/admin/api/users")
async def list_users(request: Request, query: str = Query("", max_length=100)):
    state = _state(request)
    query = query.strip()
    if not query:
        return {"users": _user_rows(state)}
    pattern = _like_pattern(query.lower())
    users = _user_rows(state, "WHERE lower(u.email) LIKE ? ESCAPE '\\' OR lower(u.name) LIKE ? ESCAPE '\\'",
                       (pattern, pattern))
    return {"users": users}


@router.get("/admin/api/users/{user_id}")
async def user_detail(user_id: int, request: Request):
    state = _state(request)
    user = _user_or_404(state, user_id)
    return {
        "user": user,
        "guardians": _links(state, user_id, as_guardian=False),   # 이 사용자가 등록한 보호자
        "wards": _links(state, user_id, as_guardian=True),        # 이 사용자를 보호자로 등록한 사람
        "devices": _all_devices(state, owner_id=user_id),
        "recent_events": _query_events(state, " WHERE d.owner_id = ?", (user_id,), limit=USER_RECENT_EVENTS),
    }


@router.post("/admin/api/users/{user_id}/disable")
async def disable_user(user_id: int, request: Request):
    """계정 정지: 로그인·세션을 막고, 열린 보호자 화면과 이 사용자 기기의 카메라 연결을 끊는다.

    다시 접속하려는 기기는 routes_realtime이 4403(주인 계정 정지)으로 거부한다.
    """
    state = _state(request)
    try:
        state.accounts.set_disabled(user_id, True)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    await state.guardian_hub.close_user(user_id)
    devices = state.accounts.devices_of([user_id])
    for device in devices:
        await state.devices.close_device(device["id"], CLOSE_FORBIDDEN, "owner suspended")
    log.warning("관리자: 사용자 %s 계정 정지 (기기 %d대 연결 끊음)", user_id, len(devices))
    return {"ok": True, "user": _user_or_404(state, user_id)}


@router.post("/admin/api/users/{user_id}/enable")
async def enable_user(user_id: int, request: Request):
    state = _state(request)
    try:
        state.accounts.set_disabled(user_id, False)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    log.warning("관리자: 사용자 %s 계정 정지 해제", user_id)
    return {"ok": True, "user": _user_or_404(state, user_id)}


@router.delete("/admin/api/users/{user_id}")
async def delete_user(user_id: int, request: Request, body: Optional[DeleteUserBody] = None):
    """사용자 삭제(되돌릴 수 없음). 실수를 막으려고 본문에 그 사용자의 이메일을 다시 적어야 한다.

    기기 삭제(routes_accounts.delete_device)와 같이 DB에서 지운 뒤 메모리 상태도 정리한다.
    """
    state = _state(request)
    user = state.accounts.get_user(user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="사용자를 찾을 수 없습니다.")
    if normalize_email(body.confirm_email if body else "") != user["email"]:
        raise HTTPException(status_code=400, detail="확인용 이메일이 이 사용자의 이메일과 다릅니다.")
    devices = state.accounts.devices_of([user_id])
    state.accounts.delete_user(user_id)
    for device in devices:
        state.events.forget_device(device["id"])
        await state.devices.remove_device(device["id"])
    await state.guardian_hub.close_user(user_id)
    log.warning("관리자: 사용자 %s 삭제 (기기 %d대와 이벤트 기록 함께 삭제)", user_id, len(devices))
    return {"ok": True, "deleted_devices": len(devices)}


# ---------------------------------------------------------------------------
# 이벤트
# ---------------------------------------------------------------------------

class LabelBody(BaseModel):
    label: Optional[str] = None   # REVIEW_LABELS 중 하나, null이면 라벨 지움
    note: Optional[str] = Field(None, max_length=NOTE_MAX_LENGTH)


@router.get("/admin/api/events")
async def list_events(request: Request, filters: Tuple[str, tuple] = Depends(event_filters),
                      limit: int = Query(100, ge=1, le=EVENTS_PAGE_MAX), offset: int = Query(0, ge=0)):
    state = _state(request)
    where, params = filters
    total = state.db.query_one(f"SELECT COUNT(*) AS n {EVENTS_FROM}{where}", params)["n"]
    return {
        "events": _query_events(state, where, params, limit=limit, offset=offset),
        "total": total,
        "limit": limit,
        "offset": offset,
    }


CSV_COLUMNS = [
    ("이벤트 번호", "id"),
    ("시작 시각", "started_at"),
    ("경보 시각", "alarm_at"),
    ("종료 시각", "ended_at"),
    ("걸린 시간(초)", "duration_seconds"),
    ("사용자 번호", "owner_id"),
    ("사용자 이름", "owner_name"),
    ("기기 번호", "device_id"),
    ("기기 이름", "device_name"),
    ("판정", "result"),
    ("판정 설명", "result_label"),
    ("음성 결과", "voice_result"),
    ("해제한 쪽", "cleared_by"),
    ("검토 라벨", "review_label"),
    ("검토 메모", "review_note"),
    ("화면 알림 성공", ("notifications", CHANNEL_WEBSOCKET, NOTIFY_SENT)),
    ("화면 알림 실패", ("notifications", CHANNEL_WEBSOCKET, NOTIFY_FAILED)),
    ("푸시 성공", ("notifications", CHANNEL_PUSH, NOTIFY_SENT)),
    ("푸시 실패", ("notifications", CHANNEL_PUSH, NOTIFY_FAILED)),
    ("특징값", "features"),
]


def _csv_cell(event: Dict[str, Any], key: Any) -> Any:
    if isinstance(key, tuple):
        value: Any = event
        for part in key:
            value = value[part]
        return value
    value = event.get(key)
    if value is None:
        return ""
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        # 엑셀이 수식으로 실행하지 않게 한다(사용자가 정한 기기 이름·메모가 '='로 시작할 수 있다).
        return "'" + value
    return value


@router.get("/admin/api/events.csv")
async def export_events_csv(request: Request, filters: Tuple[str, tuple] = Depends(event_filters)):
    """시험 결과 정리용 CSV. 엑셀이 한글을 깨뜨리지 않도록 UTF-8 BOM을 붙인다."""
    where, params = filters
    events = _query_events(_state(request), where, params)
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow([title for title, _ in CSV_COLUMNS])
    for event in events:
        writer.writerow([_csv_cell(event, key) for _, key in CSV_COLUMNS])
    filename = f"events-{datetime.now().strftime('%Y%m%d-%H%M')}.csv"
    return Response(
        content="﻿" + out.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/admin/api/events/{event_id}/label")
async def label_event(event_id: int, body: LabelBody, request: Request):
    """시험 검토 라벨과 메모. 오경보 통계(docs/TEST_PLAN.md)에 쓴다."""
    state = _state(request)
    _event_or_404(state, event_id)
    if body.label is not None and body.label not in REVIEW_LABELS:
        raise HTTPException(status_code=400, detail="라벨은 " + ", ".join(REVIEW_LABELS) + " 중 하나여야 합니다.")
    note = (body.note or "").strip() or None
    state.db.execute("UPDATE events SET review_label = ?, review_note = ? WHERE id = ?",
                     (body.label, note, event_id))
    log.warning("관리자: 이벤트 %s 검토 라벨 %s", event_id, body.label)
    return {"ok": True, "event": _event_or_404(state, event_id)}


# ---------------------------------------------------------------------------
# 통계
# ---------------------------------------------------------------------------

def _by_result() -> Dict[str, int]:
    return dict({r: 0 for r in ALL_RESULTS}, **{RESULT_SUSPECT: 0})


@router.get("/admin/api/stats")
async def stats(request: Request, days: int = Query(14, ge=1, le=366)):
    """최근 days일(오늘 포함) 이벤트 통계."""
    db = _state(request).db
    today = date.today()
    first_day = today - timedelta(days=days - 1)
    rows = db.query(
        "SELECT id, started_at, ended_at, alarm_at, result, voice_result, review_label "
        "FROM events WHERE started_at >= ? ORDER BY id",
        (first_day.isoformat(),),
    )

    per_day = {(first_day + timedelta(days=i)).isoformat(): _by_result() for i in range(days)}
    totals = _by_result()
    voice_results: Dict[str, int] = {}
    labels = {label: 0 for label in REVIEW_LABELS}
    suspect_to_clear: List[float] = []   # 낙상 의심 -> 음성·버튼으로 해제
    suspect_to_alarm: List[float] = []   # 낙상 의심 -> 경보(음성 확인에 걸린 시간)
    alarm_to_clear: List[float] = []     # 경보 -> 본인·보호자가 해제
    for row in rows:
        result = row["result"] or RESULT_SUSPECT
        day = row["started_at"][:10]
        if day in per_day and result in per_day[day]:
            per_day[day][result] += 1
        if result in totals:
            totals[result] += 1
        voice_key = row["voice_result"] or "NONE"
        voice_results[voice_key] = voice_results.get(voice_key, 0) + 1
        if row["review_label"] in labels:
            labels[row["review_label"]] += 1

        if row["result"] in CLEARED_RESULTS:
            seconds = _seconds_between(row["started_at"], row["ended_at"])
            if seconds is not None:
                suspect_to_clear.append(seconds)
        elif row["result"] in ALERT_RESULTS:
            seconds = _seconds_between(row["started_at"], row["alarm_at"])
            if seconds is not None:
                suspect_to_alarm.append(seconds)
            seconds = _seconds_between(row["alarm_at"], row["ended_at"])
            if seconds is not None:
                alarm_to_clear.append(seconds)

    notification_rows = db.query(
        "SELECT n.channel, n.state, COUNT(*) AS n FROM notifications n JOIN events e ON e.id = n.event_id "
        "WHERE e.started_at >= ? GROUP BY n.channel, n.state",
        (first_day.isoformat(),),
    )
    notifications = _empty_notification_counts()
    for r in notification_rows:
        if r["channel"] in notifications and r["state"] in notifications[r["channel"]]:
            notifications[r["channel"]][r["state"]] = r["n"]
    for counts in notifications.values():
        counts["success_rate"] = _rate(counts[NOTIFY_SENT], counts[NOTIFY_SENT] + counts[NOTIFY_FAILED])

    labeled = sum(labels.values())
    return {
        "days": days,
        "from": first_day.isoformat(),
        "to": today.isoformat(),
        "result_labels": dict(RESULT_LABELS, **{RESULT_SUSPECT: "음성 확인 중"}),
        "per_day": [{"date": day, "total": sum(c.values()), "by_result": c} for day, c in per_day.items()],
        "totals": {"total": len(rows), "by_result": totals},
        "voice_results": voice_results,
        "durations": {
            "suspect_to_clear": {"average_seconds": _average(suspect_to_clear), "count": len(suspect_to_clear)},
            "suspect_to_alarm": {"average_seconds": _average(suspect_to_alarm), "count": len(suspect_to_alarm)},
            "alarm_to_clear": {"average_seconds": _average(alarm_to_clear), "count": len(alarm_to_clear)},
        },
        "notifications": notifications,
        "labels": {
            "counts": labels,
            "labeled": labeled,
            "unlabeled": len(rows) - labeled,
            "false_alarm_rate": _rate(labels[LABEL_FALSE_ALARM], labeled),
        },
    }
