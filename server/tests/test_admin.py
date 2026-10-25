"""관리자 페이지·API 테스트 (server/admin.py).

서버 PC에서만 열리는지(local_access_problem)가 가장 중요하므로 검사를 바꿔치기하지 않고 진짜 검사를 시험한다.
- TestClient(client=(주소, 포트))로 접속한 주소를 정하고, base_url로 Host 헤더를 정한다.
  서버 PC의 브라우저 = client 127.0.0.1 + base_url https://localhost, 같은 Wi-Fi의 다른 기기 = client 192.168.x.x.
- 변경 요청(POST/DELETE)은 브라우저처럼 Origin과 X-Admin-Request: 1을 붙인다(ADMIN_HEADERS).
- 검사 함수 자체는 가짜 요청(scope)으로 따로 시험한다.

앱과 실시간 도우미(가짜 자세 추정·음성 인식, 카메라 WebSocket)는 test_realtime.py의 것을 그대로 쓴다.
"""

import csv
import io
import sqlite3
import types
from datetime import date, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request
from starlette.websockets import WebSocketDisconnect

from server import admin, auth
from server.admin import local_access_problem
from server.auth import Accounts
from server.config import Settings
from server.db import ALERT_HELP, ALERT_NO_REPLY, CLEARED_BUTTON, CLEARED_VOICE, Database
from server.tests.test_realtime import (
    PASSWORD,
    WS_BASE,
    camera,
    client_for,
    create_device,
    fall_and_get_voice_check,
    is_state,
    link_guardian,
    make_settings,
    open_camera,
    recv,
    recv_until,
    signup,
    start_app,
    upload,
    wait_until,
)

LOCAL = ("127.0.0.1", 50000)
REMOTE = ("192.168.0.20", 50000)
ADMIN_HEADERS = {"Origin": "https://localhost", "X-Admin-Request": "1"}
NOT_FOUND = {"detail": "Not Found"}


@pytest.fixture(autouse=True)
def fast_bcrypt(monkeypatch):
    monkeypatch.setattr(auth, "BCRYPT_ROUNDS", 4)


@pytest.fixture
def server(tmp_path):
    app, main = start_app(make_settings(tmp_path))
    with main:
        yield app, main


def admin_client(main, base_url="https://localhost", client=LOCAL, headers=None) -> TestClient:
    """관리자 화면처럼 요청하는 클라이언트. 이벤트 루프(portal)는 main과 같이 쓴다."""
    c = TestClient(main.app, base_url=base_url, client=client,
                   headers=ADMIN_HEADERS if headers is None else headers)
    c.portal = main.portal
    return c


@pytest.fixture
def family(server):
    """할머니(기기 주인) + 수락된 보호자 + Pi 기기 + 서버 PC의 관리자 화면."""
    app, main = server
    owner = signup(main, "grandma@example.com", "할머니")
    guardian = signup(main, "son@example.com", "아들")
    link_guardian(owner, guardian)
    device_id, key = create_device(owner)
    return types.SimpleNamespace(app=app, main=main, owner=owner, guardian=guardian, device_id=device_id,
                                 key=key, admin=admin_client(main))


def close_code(ws) -> int:
    """서버가 연결을 닫을 때까지 다른 메시지는 건너뛰고 닫는 코드를 돌려준다."""
    with pytest.raises(WebSocketDisconnect) as exc:
        recv_until(ws, lambda m: False)
    return exc.value.code


def connect_close_code(client, url) -> int:
    with client.websocket_connect(url) as ws:
        with pytest.raises(WebSocketDisconnect) as exc:
            recv(ws)
        return exc.value.code


def login(main, email, password=PASSWORD):
    return client_for(main).post("/api/login", json={"email": email, "password": password})


def raise_alarm(family, ws) -> int:
    """열린 카메라 연결로 낙상 -> 음성으로 도움 요청 -> 경보. 이벤트 id를 돌려준다."""
    open_camera(ws)
    event_id = fall_and_get_voice_check(ws)
    assert upload(family.main, event_id, text="도와주세요", key=family.key).json() == {"accepted": True}
    recv_until(ws, is_state("ALARM"))
    return event_id


# ---------------------------------------------------------------------------
# 서버 PC 확인: 검사 함수 (가짜 요청)
# ---------------------------------------------------------------------------

def fake_request(client=LOCAL, host="localhost:8443", method="GET", headers=None, admin_enabled=True) -> Request:
    app = types.SimpleNamespace(state=types.SimpleNamespace(settings=Settings(admin_enabled=admin_enabled)))
    all_headers = dict(headers or {})
    if host is not None:
        all_headers["host"] = host
    scope = {
        "type": "http",
        "app": app,
        "method": method,
        "path": "/admin/api/users/1/disable",
        "query_string": b"",
        "headers": [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in all_headers.items()],
        "client": client,
    }
    return Request(scope)


SAME_ORIGIN = {"origin": "https://localhost:8443", "x-admin-request": "1"}


@pytest.mark.parametrize("kwargs", [
    dict(),
    dict(client=("127.0.0.1", 1), host="127.0.0.1:8443"),
    dict(client=("127.8.9.10", 1), host="localhost"),                       # 127.0.0.0/8 전체가 루프백
    dict(client=("::1", 1), host="[::1]:8443"),
    dict(client=("::ffff:127.0.0.1", 1), host="LOCALHOST:8443"),            # IPv4를 담은 IPv6, 대문자 이름
    dict(method="POST", headers=SAME_ORIGIN),
    dict(method="DELETE", headers=SAME_ORIGIN),
    dict(method="POST", host="127.0.0.1:8443", headers={"origin": "http://127.0.0.1:8443", "x-admin-request": "1"}),
    # Sec-Fetch-Site: 관리자 페이지 자신의 요청(same-origin), 주소창 입력·즐겨찾기(none)
    dict(headers={"sec-fetch-site": "same-origin"}),
    dict(headers={"sec-fetch-site": "none"}),
    dict(method="POST", headers=dict(SAME_ORIGIN, **{"sec-fetch-site": "same-origin"})),
])
def test_local_access_allows_server_pc(kwargs):
    assert local_access_problem(fake_request(**kwargs)) is None


@pytest.mark.parametrize("kwargs", [
    # 1. 다른 기기에서 접속 (X-Forwarded-For는 믿지 않는다)
    dict(client=("192.168.0.20", 1)),
    dict(client=("192.168.0.20", 1), headers={"x-forwarded-for": "127.0.0.1", "x-real-ip": "127.0.0.1"}),
    dict(client=("10.0.0.1", 1)),
    dict(client=("::ffff:192.168.0.20", 1)),
    dict(client=("testclient", 50000)),
    dict(client=None),
    # 2. DNS 리바인딩: 주소는 127.0.0.1이지만 Host는 공격 사이트 이름
    dict(host="evil.example"),
    dict(host="evil.example:8443"),
    dict(host="localhost.evil.example"),
    dict(host="evil@localhost:8443"),
    dict(host="192.168.0.5:8443"),
    dict(host="localhost:abc"),
    dict(host=None),
    # 3. CSRF: 변경 요청에는 같은 출처의 Origin과 X-Admin-Request가 있어야 한다
    dict(method="POST"),
    dict(method="POST", headers={"x-admin-request": "1"}),
    dict(method="POST", headers={"origin": "https://evil.example", "x-admin-request": "1"}),
    dict(method="POST", headers={"origin": "null", "x-admin-request": "1"}),
    dict(method="POST", headers={"origin": "https://localhost:3000", "x-admin-request": "1"}),  # 다른 포트
    dict(method="POST", headers={"origin": "https://localhost:8443"}),
    dict(method="POST", headers={"origin": "https://localhost:8443", "x-admin-request": "0"}),
    dict(method="DELETE", headers={"origin": "https://evil.example", "x-admin-request": "1"}),
    dict(method="OPTIONS", headers={"origin": "https://evil.example"}),       # CORS 사전 요청도 거부
    # 4. 서버 PC 브라우저에서 연 다른 사이트가 링크·이미지 등으로 부른 읽기 요청
    dict(headers={"sec-fetch-site": "cross-site"}),
    dict(headers={"sec-fetch-site": "same-site"}),     # localhost의 다른 포트
    dict(headers={"sec-fetch-site": "CROSS-SITE"}),
    dict(method="POST", headers=dict(SAME_ORIGIN, **{"sec-fetch-site": "cross-site"})),
    # 설정으로 끈 경우
    dict(admin_enabled=False),
])
def test_local_access_refuses(kwargs):
    assert local_access_problem(fake_request(**kwargs)) is not None


# ---------------------------------------------------------------------------
# 서버 PC 확인: 실제 앱 (미들웨어 + 의존성)
# ---------------------------------------------------------------------------

def test_admin_api_only_from_server_pc(family):
    main, owner_id = family.main, family.owner.user["id"]

    res = family.admin.get("/admin/api/overview")
    assert res.status_code == 200
    assert res.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in res.headers["content-security-policy"]
    assert res.headers["cache-control"] == "no-store"
    assert admin_client(main, base_url="https://127.0.0.1:8443").get("/admin/api/overview").status_code == 200
    assert admin_client(main, base_url="https://[::1]:8443", client=("::1", 1)).get(
        "/admin/api/users").status_code == 200

    # 같은 Wi-Fi의 다른 기기: 없는 주소와 똑같은 404. 본문 해석(422)·메서드 검사(405)보다 먼저 막는다.
    remote = admin_client(main, client=REMOTE)
    unknown = remote.get("/no-such-page")
    for res in (
        remote.get("/admin"),
        remote.get("/admin/api/overview"),
        remote.get("/admin/api/overview", headers={"X-Forwarded-For": "127.0.0.1"}),
        remote.get(f"/admin/api/users/{owner_id}/disable"),
        remote.post(f"/admin/api/users/{owner_id}/disable"),
        remote.post("/admin/api/events/1/label", content=b"{not json"),
        remote.get("/admin/api/events.csv"),
    ):
        assert (res.status_code, res.json()) == (404, NOT_FOUND) == (unknown.status_code, unknown.json())
    # 로그인한 사용자 쿠키가 있어도 다른 기기에서는 안 된다(관리자는 계정과 무관)
    assert family.owner.get("/admin/api/overview").status_code == 404

    # DNS 리바인딩: 서버 PC에서 왔지만 Host가 다른 이름
    assert family.admin.get("/admin/api/overview", headers={"host": "evil.example:8443"}).status_code == 404
    assert family.admin.get("/admin", headers={"host": "evil.example"}).status_code == 404

    # CSRF: 서버 PC 브라우저에서 연 다른 사이트의 요청
    bare = admin_client(main, headers={})
    assert bare.post(f"/admin/api/users/{owner_id}/disable").status_code == 404
    assert bare.post(f"/admin/api/users/{owner_id}/disable", headers={"X-Admin-Request": "1"}).status_code == 404
    assert bare.post(f"/admin/api/users/{owner_id}/disable",
                     headers={"Origin": "https://evil.example", "X-Admin-Request": "1"}).status_code == 404
    assert bare.post(f"/admin/api/users/{owner_id}/disable",
                     headers={"Origin": "https://localhost"}).status_code == 404
    assert family.app.state.accounts.is_disabled(owner_id) is False  # 위 요청은 하나도 적용되지 않음
    # 읽기는 같은 출처 브라우저처럼 Origin 없이 된다(CSV 다운로드 링크 등)
    assert bare.get("/admin/api/events.csv").status_code == 200

    # /docs(같은 Wi-Fi에서도 열림)에 관리자 API가 나오지 않는다
    paths = main.get("/openapi.json").json()["paths"]
    assert not [p for p in paths if p.startswith("/admin")]


def test_admin_get_from_other_site_refused(family):
    """서버 PC에서 연 다른 사이트가 a.href='http://localhost:8443/admin/api/events.csv'를 누르게 해도 받지 못한다.

    브라우저는 이때 Sec-Fetch-Site: cross-site(같은 localhost의 다른 포트면 same-site)를 붙인다.
    """
    bare = admin_client(family.main, headers={})
    for path in ("/admin/api/events.csv", "/admin/api/users", "/admin", "/admin/admin.js"):
        for site in ("cross-site", "same-site"):
            res = bare.get(path, headers={"Sec-Fetch-Site": site})
            assert (res.status_code, res.json()) == (404, NOT_FOUND), (path, site)
    # 관리자 페이지 안의 CSV 버튼(same-origin), 주소창 입력(none), 머리글 없는 로컬 도구는 그대로 된다
    for headers in ({"Sec-Fetch-Site": "same-origin"}, {"Sec-Fetch-Site": "none"}, {}):
        res = bare.get("/admin/api/events.csv", headers=headers)
        assert res.status_code == 200 and res.headers["content-type"].startswith("text/csv"), headers
    web_dir = family.app.state.settings.web_dir
    web_dir.mkdir(parents=True, exist_ok=True)
    (web_dir / "admin.html").write_text("<h1>관리자</h1>", encoding="utf-8")
    assert bare.get("/admin", headers={"Sec-Fetch-Site": "none"}).status_code == 200        # 주소창·즐겨찾기
    assert bare.get("/admin", headers={"Sec-Fetch-Site": "same-origin"}).status_code == 200
    assert bare.get("/admin", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 404


def test_admin_assets_only_under_guarded_prefix(tmp_path):
    """관리자 스크립트·스타일은 /static에 없고, /admin 아래에서 서버 PC에게만 나간다."""
    settings = make_settings(tmp_path)
    (settings.web_dir / "static" / "js").mkdir(parents=True)
    (settings.web_dir / "static" / "js" / "api.js").write_text("export const api = 1;", encoding="utf-8")
    (settings.web_dir / "admin").mkdir()
    (settings.web_dir / "admin" / "admin.js").write_text("// 관리자", encoding="utf-8")
    (settings.web_dir / "admin" / "admin.css").write_text("body {}", encoding="utf-8")
    app, main = start_app(settings)
    with main:
        local, remote = admin_client(main), admin_client(main, client=REMOTE)
        js = local.get("/admin/admin.js", headers={"Sec-Fetch-Site": "same-origin"})
        assert js.status_code == 200 and js.text == "// 관리자"
        assert js.headers["content-type"].startswith("application/javascript")
        assert js.headers["cache-control"] == "no-store"
        css = local.get("/admin/admin.css")
        assert css.status_code == 200 and css.headers["content-type"].startswith("text/css")
        for path in ("/admin/admin.js", "/admin/admin.css"):
            assert (remote.get(path).status_code, remote.get(path).json()) == (404, NOT_FOUND)
        # 다른 파일은 고를 수 없다
        assert local.get("/admin/api.js").status_code == 404
        # 공용 모듈은 /static에 그대로 있고, 관리자 파일은 /static에 없다
        assert remote.get("/static/js/api.js").status_code == 200
        assert remote.get("/static/js/admin.js").status_code == 404
        assert remote.get("/static/css/admin.css").status_code == 404


def test_repository_admin_files_are_not_public():
    """실제 web/ 폴더: 관리자 파일이 /static 아래에 없고 admin.html이 /admin/ 주소로 부른다."""
    from server.config import BASE_DIR
    web = BASE_DIR / "web"
    assert not (web / "static" / "js" / "admin.js").exists()
    assert not (web / "static" / "css" / "admin.css").exists()
    assert (web / "admin" / "admin.js").is_file() and (web / "admin" / "admin.css").is_file()
    html = (web / "admin.html").read_text(encoding="utf-8")
    assert 'src="/admin/admin.js"' in html and 'href="/admin/admin.css"' in html
    assert "/static/js/admin.js" not in html and "/static/css/admin.css" not in html


def test_uvicorn_ignores_forwarded_headers(tmp_path, monkeypatch):
    """python -m server는 X-Forwarded-For를 믿지 않는다(proxy_headers=False).

    uvicorn 기본값(proxy_headers=True)이면 FORWARDED_ALLOW_IPS='*'가 설정된 PC에서 같은 Wi-Fi 기기가
    'X-Forwarded-For: 127.0.0.1'로 접속 주소를 바꿔 관리자 API를 연다.
    """
    import logging

    import uvicorn
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

    from server import __main__ as server_main

    for name, value in {"FALL_DATA_DIR": str(tmp_path / "data"), "FALL_WEB_DIR": str(tmp_path / "web"),
                        "FALL_POSE_BACKEND": "fake", "FALL_STT_BACKEND": "fake",
                        "FORWARDED_ALLOW_IPS": "*"}.items():
        monkeypatch.setenv(name, value)
    captured = {}
    monkeypatch.setattr(server_main.uvicorn, "run", lambda app, **kwargs: captured.update(app=app, kwargs=kwargs))
    loggers = [logging.getLogger(n) for n in ("uvicorn.access", "uvicorn.error")]
    try:
        server_main.main()
    finally:
        for logger in loggers:
            for f in [f for f in logger.filters if isinstance(f, server_main.HideDeviceKeys)]:
                logger.removeFilter(f)

    app = captured["app"]
    try:
        assert captured["kwargs"]["proxy_headers"] is False
        config = uvicorn.Config(app, **captured["kwargs"])
        config.load()
        assert config.proxy_headers is False
        assert not isinstance(config.loaded_app, ProxyHeadersMiddleware)
        # uvicorn이 실제로 돌릴 앱에 같은 Wi-Fi 기기가 X-Forwarded-For를 붙여 보내도 404
        remote = TestClient(config.loaded_app, base_url="http://localhost:8443", client=REMOTE)
        res = remote.get("/admin/api/overview", headers={"X-Forwarded-For": "127.0.0.1"})
        assert (res.status_code, res.json()) == (404, NOT_FOUND)
    finally:
        app.state.db.close()


def test_admin_can_be_turned_off(tmp_path):
    app, main = start_app(make_settings(tmp_path, admin_enabled=False))
    with main:
        assert admin_client(main).get("/admin/api/overview").status_code == 404


def test_require_local_dependency_without_middleware(tmp_path):
    """미들웨어 없이 라우터만 붙여도 모든 관리자 라우트가 막힌다."""
    app = FastAPI()
    app.state.settings = make_settings(tmp_path)
    app.include_router(admin.router)
    remote = TestClient(app, base_url="https://localhost", client=REMOTE)
    assert remote.get("/admin/api/overview").status_code == 404
    assert remote.get("/admin").status_code == 404
    local = TestClient(app, base_url="https://localhost", client=LOCAL)
    assert local.post("/admin/api/users/1/enable", headers={"Origin": "https://evil.example",
                                                            "X-Admin-Request": "1"}).status_code == 404
    for route in admin.router.routes:
        assert any(d.call is admin.require_local for d in route.dependant.dependencies), route.path


def test_admin_page_served_when_file_exists(tmp_path):
    settings = make_settings(tmp_path)
    app, main = start_app(settings)
    with main:
        local = admin_client(main)
        assert local.get("/admin").status_code == 404  # web/admin.html이 아직 없음
        settings.web_dir.mkdir(parents=True)
        (settings.web_dir / "admin.html").write_text("<h1>관리자</h1>", encoding="utf-8")
        res = local.get("/admin")
        assert res.status_code == 200 and res.text == "<h1>관리자</h1>"
        assert res.headers["content-type"].startswith("text/html")
        assert res.headers["cache-control"] == "no-store"
        assert admin_client(main, client=REMOTE).get("/admin").status_code == 404


# ---------------------------------------------------------------------------
# 현황·사용자
# ---------------------------------------------------------------------------

def test_overview_shows_devices_active_event_and_failures(family):
    with camera(family.main, family.device_id, family.key) as ws:
        event_id = raise_alarm(family, ws)
        wait_until(lambda: family.app.state.db.query(
            "SELECT id FROM notifications WHERE event_id = ? AND channel = 'push'", (event_id,)))
        body = family.admin.get("/admin/api/overview").json()

    server = body["server"]
    assert server["pose_backend"] == "fake" and server["stt_backend"] == "fake"
    assert server["push_enabled"] is False and server["https"] is False
    assert server["uptime_seconds"] >= 0 and server["started_at"]
    assert server["thresholds"] == {"descent_velocity_peak": 2.0, "t1_lying_seconds": 0.3,
                                    "voice_reply_timeout_seconds": 0.6, "voice_max_attempts": 2}
    assert body["counts"] == {"users": 2, "disabled_users": 0, "devices": 1, "online_devices": 1,
                              "guardian_links_accepted": 1, "guardian_links_pending": 0,
                              "events_today": 1, "active_events": 1}
    [device] = body["devices"]
    assert device["device_id"] == family.device_id and device["owner_name"] == "할머니"
    assert device["online"] is True and device["state"] == "ALARM" and device["created_at"]
    [event] = body["active_events"]
    assert event["id"] == event_id and event["state"] == "ALARM" and event["result"] == ALERT_HELP
    # 보호자 화면이 닫혀 있고 푸시도 꺼져 있으므로 두 채널 모두 실패로 기록된다
    failures = body["notification_failures"]
    assert {f["channel"] for f in failures} == {"websocket", "push"}
    assert all(f["event_id"] == event_id and f["guardian_name"] == "아들" for f in failures)
    # 경보로 바뀐 시각이 남는다(경보 -> 해제 시간 통계)
    assert family.app.state.db.query_one("SELECT alarm_at FROM events WHERE id = ?", (event_id,))["alarm_at"]


def test_users_list_search_and_detail(family):
    pending = signup(family.main, "daughter@example.com", "딸_100%")
    pending.post("/api/guardians", json={"email": family.owner.user["email"]})  # 딸이 할머니를 보호자로 요청(수락 전)

    users = family.admin.get("/admin/api/users").json()["users"]
    assert [u["email"] for u in users] == ["grandma@example.com", "son@example.com", "daughter@example.com"]
    grandma = users[0]
    assert grandma == {
        "id": family.owner.user["id"], "email": "grandma@example.com", "name": "할머니",
        "created_at": grandma["created_at"], "disabled_at": None, "device_count": 1, "online_device_count": 0,
        "guardians_accepted": 1, "guardians_pending": 0, "wards_count": 0,
    }
    assert users[1]["wards_count"] == 1 and users[2]["guardians_pending"] == 1

    def search(text):
        return [u["email"] for u in family.admin.get("/admin/api/users", params={"query": text}).json()["users"]]
    assert search("SON@") == ["son@example.com"]
    assert search("할머") == ["grandma@example.com"]
    assert search("_100%") == ["daughter@example.com"]   # %와 _는 글자 그대로 찾는다
    assert search("%") == ["daughter@example.com"]
    assert search("없는사람") == []

    detail = family.admin.get(f"/admin/api/users/{family.owner.user['id']}").json()
    assert detail["user"]["email"] == "grandma@example.com"
    assert [(l["status"], l["user"]["name"]) for l in detail["guardians"]] == [("accepted", "아들")]
    # 수락 전 요청도 상대 이름까지 보여 준다(운영자 화면)
    assert [(l["status"], l["user"]["name"]) for l in detail["wards"]] == [("pending", "딸_100%")]
    [device] = detail["devices"]
    assert device["device_id"] == family.device_id and device["online"] is False and "key_hash" not in device
    assert detail["recent_events"] == []
    assert family.admin.get("/admin/api/users/9999").status_code == 404


# ---------------------------------------------------------------------------
# 계정 정지·삭제
# ---------------------------------------------------------------------------

def test_disable_blocks_login_sessions_and_devices_then_enable_restores(family):
    owner_id = family.owner.user["id"]
    camera_url = f"{WS_BASE}/ws/camera?device_id={family.device_id}&key={family.key}"
    phone_id, _ = create_device(family.owner, kind="phone", name="휴대폰")
    assert family.owner.get("/api/me").status_code == 200

    with family.main.websocket_connect(camera_url) as cam, \
            family.owner.websocket_connect(f"{WS_BASE}/ws/guardian") as screen:
        open_camera(cam)
        assert recv(screen)["type"] == "snapshot"
        res = family.admin.post(f"/admin/api/users/{owner_id}/disable")
        assert res.status_code == 200, res.text
        assert res.json()["user"]["disabled_at"]
        assert close_code(cam) == 4403      # 카메라 연결을 끊고
        assert close_code(screen) == 4401   # 열린 화면은 로그인 화면으로
    wait_until(lambda: not family.app.state.devices.is_online(family.device_id))

    assert family.owner.get("/api/me").status_code == 401   # 기존 세션 삭제
    res = login(family.main, "grandma@example.com")
    assert res.status_code == 403 and "정지된 계정입니다" in res.json()["detail"]
    assert login(family.main, "grandma@example.com", "wrong-password").status_code == 401  # 비밀번호가 틀리면 그대로
    assert connect_close_code(family.main, camera_url) == 4403   # 다시 접속해도 거부
    assert connect_close_code(family.owner, f"{WS_BASE}/ws/camera?device_id={phone_id}") == 4403
    assert family.admin.get("/admin/api/overview").json()["counts"]["disabled_users"] == 1

    res = family.admin.post(f"/admin/api/users/{owner_id}/enable")
    assert res.status_code == 200 and res.json()["user"]["disabled_at"] is None
    assert login(family.main, "grandma@example.com").status_code == 200
    with family.main.websocket_connect(camera_url) as cam:
        assert recv(cam)["type"] == "hello"

    assert family.admin.post("/admin/api/users/9999/disable").status_code == 404
    assert family.admin.post("/admin/api/users/9999/enable").status_code == 404


def test_suspended_owner_device_key_refused_on_http(family):
    """정지된 주인의 Pi 키로는 현장 화면 상태·해제·음성 업로드를 못 한다. 카메라 연결은 4403 'owner suspended'."""
    main, owner_id, key = family.main, family.owner.user["id"], family.key
    key_header = {"X-Device-Key": key}
    with camera(main, family.device_id, key) as ws:
        open_camera(ws)
        event_id = fall_and_get_voice_check(ws)
        assert family.admin.post(f"/admin/api/users/{owner_id}/disable").status_code == 200
        assert close_code(ws) == 4403

    assert main.get(f"/api/devices/{family.device_id}/state", headers=key_header).status_code == 401
    assert main.get(f"/api/devices/{family.device_id}/state?key={key}").status_code == 401
    assert main.post(f"/api/events/{event_id}/clear", headers=key_header).status_code == 401
    assert upload(main, event_id, text="괜찮아요", key=key).status_code == 401
    with main.websocket_connect(f"{WS_BASE}/ws/camera?device_id={family.device_id}&key={key}") as ws:
        with pytest.raises(WebSocketDisconnect) as exc:
            recv(ws)
        assert (exc.value.code, exc.value.reason) == (4403, "owner suspended")
    # 틀린 키는 정지 여부를 알려 주지 않고 4401
    assert connect_close_code(main, f"{WS_BASE}/ws/camera?device_id={family.device_id}&key=wrong") == 4401

    family.admin.post(f"/admin/api/users/{owner_id}/enable")
    assert main.get(f"/api/devices/{family.device_id}/state", headers=key_header).status_code == 200


def test_disable_by_guardian_closes_only_that_users_screens(family):
    with family.guardian.websocket_connect(f"{WS_BASE}/ws/guardian") as son_screen, \
            family.owner.websocket_connect(f"{WS_BASE}/ws/guardian") as grandma_screen:
        recv(son_screen), recv(grandma_screen)
        family.admin.post(f"/admin/api/users/{family.guardian.user['id']}/disable")
        assert close_code(son_screen) == 4401
        # 할머니 화면은 그대로 열려 있다
        assert family.guardian.user["id"] not in family.app.state.guardian_hub.connected_user_ids()
        assert family.owner.user["id"] in family.app.state.guardian_hub.connected_user_ids()
    # 정지된 보호자에게는 경보를 보내지 않는다(연결은 남아 있어도)
    assert family.app.state.accounts.guardian_ids_for(family.owner.user["id"]) == []
    family.admin.post(f"/admin/api/users/{family.guardian.user['id']}/enable")
    assert family.app.state.accounts.guardian_ids_for(family.owner.user["id"]) == [family.guardian.user["id"]]


def test_delete_user_requires_confirm_email_and_cleans_up(family):
    app, owner_id = family.app, family.owner.user["id"]
    url = f"/admin/api/users/{owner_id}"
    with camera(family.main, family.device_id, family.key) as ws:
        raise_alarm(family, ws)
        assert family.admin.request("DELETE", url).status_code == 400
        assert family.admin.request("DELETE", url, json={"confirm_email": "son@example.com"}).status_code == 400
        bare = admin_client(family.main, headers={})
        assert bare.request("DELETE", url, json={"confirm_email": "grandma@example.com"}).status_code == 404
        assert app.state.accounts.get_user(owner_id) is not None

        res = family.admin.request("DELETE", url, json={"confirm_email": " Grandma@Example.com "})
        assert res.status_code == 200 and res.json() == {"ok": True, "deleted_devices": 1}
        assert close_code(ws) == 4401   # 기기 삭제와 같이 끊는다

    assert app.state.accounts.get_user(owner_id) is None
    assert app.state.db.query("SELECT id FROM devices") == []
    assert app.state.db.query("SELECT id FROM events") == []
    assert app.state.db.query("SELECT id FROM guardians") == []
    assert not app.state.devices.is_online(family.device_id)
    assert app.state.events.active_for_device(family.device_id) is None
    assert app.state.accounts.get_user(family.guardian.user["id"]) is not None   # 보호자 계정은 남는다
    assert login(family.main, "grandma@example.com").status_code == 401
    assert family.admin.request("DELETE", url, json={"confirm_email": "grandma@example.com"}).status_code == 404
    assert family.admin.get("/admin/api/overview").json()["counts"]["active_events"] == 0


# ---------------------------------------------------------------------------
# 이벤트·라벨·통계·CSV
# ---------------------------------------------------------------------------

def insert_event(db, device_id, started, result, after_alarm=None, after_end=None, voice_result=None):
    """started 기준 몇 초 뒤에 경보·종료되었는지로 이벤트 행을 직접 넣는다."""
    def later(seconds):
        return (started + timedelta(seconds=seconds)).isoformat(timespec="milliseconds") if seconds is not None else None
    cur = db.execute(
        "INSERT INTO events (device_id, started_at, alarm_at, ended_at, result, voice_result, feature_snapshot) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (device_id, started.isoformat(timespec="milliseconds"), later(after_alarm), later(after_end), result,
         voice_result, '{"descent_velocity": 2.5}'),
    )
    return cur.lastrowid


def add_notification(db, event_id, guardian_id, channel, state):
    db.execute("INSERT INTO notifications (event_id, guardian_id, kind, channel, state, created_at) "
               "VALUES (?, ?, 'alert', ?, ?, ?)", (event_id, guardian_id, channel, state, datetime.now().isoformat()))


@pytest.fixture
def history(family):
    """오늘 2건, 어제 1건, 30일 전 1건(통계 기간 밖)."""
    db, device_id, guardian_id = family.app.state.db, family.device_id, family.guardian.user["id"]
    today = datetime.combine(date.today(), datetime.min.time())
    yesterday = today - timedelta(days=1)
    ids = types.SimpleNamespace(
        old=insert_event(db, device_id, today - timedelta(days=30, hours=-1), CLEARED_BUTTON, after_end=5),
        no_reply=insert_event(db, device_id, yesterday + timedelta(hours=9), ALERT_NO_REPLY, after_alarm=30,
                              voice_result="NO_REPLY"),
        safe=insert_event(db, device_id, today + timedelta(seconds=1), CLEARED_VOICE, after_end=12,
                          voice_result="SAFE"),
        help=insert_event(db, device_id, today + timedelta(seconds=2), ALERT_HELP, after_alarm=20, after_end=80,
                          voice_result="HELP"),
    )
    add_notification(db, ids.help, guardian_id, "websocket", "sent")
    add_notification(db, ids.help, guardian_id, "push", "failed")
    family.history = ids
    return family


def event_ids(family, **params):
    res = family.admin.get("/admin/api/events", params=params)
    assert res.status_code == 200, res.text
    return [e["id"] for e in res.json()["events"]]


def test_events_list_filters_and_shape(history):
    ids, today, yesterday = history.history, date.today(), date.today() - timedelta(days=1)
    body = history.admin.get("/admin/api/events").json()
    assert body["total"] == 4 and body["limit"] == 100 and body["offset"] == 0
    assert [e["id"] for e in body["events"]] == [ids.help, ids.safe, ids.no_reply, ids.old]   # 최근 것부터
    help_event = body["events"][0]
    assert help_event["device_name"] == "거실 Pi" and help_event["owner_name"] == "할머니"
    assert help_event["owner_email"] == "grandma@example.com"
    assert help_event["result_label"] == "음성으로 도움 요청" and help_event["state"] == "ENDED"
    assert help_event["duration_seconds"] == 80.0 and help_event["alarm_at"]
    assert help_event["features"] == {"descent_velocity": 2.5}
    assert help_event["review_label"] is None and help_event["review_note"] is None
    assert help_event["notifications"] == {"websocket": {"sent": 1, "failed": 0}, "push": {"sent": 0, "failed": 1}}
    assert body["events"][2]["state"] == "ALARM"   # 해제되지 않은 경보

    assert event_ids(history, result="ALERT") == [ids.help, ids.no_reply]
    assert event_ids(history, result="CLEARED") == [ids.safe, ids.old]
    assert event_ids(history, result=CLEARED_VOICE) == [ids.safe]
    assert event_ids(history, result="SUSPECT") == []
    assert event_ids(history, **{"from": today.isoformat()}) == [ids.help, ids.safe]
    assert event_ids(history, to=yesterday.isoformat()) == [ids.no_reply, ids.old]
    assert event_ids(history, **{"from": yesterday.isoformat(), "to": yesterday.isoformat()}) == [ids.no_reply]
    assert event_ids(history, user_id=history.owner.user["id"]) == [ids.help, ids.safe, ids.no_reply, ids.old]
    assert event_ids(history, user_id=history.guardian.user["id"]) == []
    assert event_ids(history, device_id=history.device_id, limit=2, offset=1) == [ids.safe, ids.no_reply]
    assert event_ids(history, label="none") == [ids.help, ids.safe, ids.no_reply, ids.old]
    for bad in ({"from": "어제"}, {"result": "NOPE"}, {"label": "nope"}):
        assert history.admin.get("/admin/api/events", params=bad).status_code == 400
    assert history.admin.get("/admin/api/events", params={"limit": 0}).status_code == 422

    detail = history.admin.get(f"/admin/api/users/{history.owner.user['id']}").json()
    assert [e["id"] for e in detail["recent_events"]] == [ids.help, ids.safe, ids.no_reply, ids.old]


def test_label_events_and_stats(history):
    ids, admin_c = history.history, history.admin

    def label(event_id, body):
        return admin_c.post(f"/admin/api/events/{event_id}/label", json=body)

    res = label(ids.safe, {"label": "false_alarm", "note": "  의자에 털썩 앉음 "})
    assert res.status_code == 200
    assert res.json()["event"]["review_label"] == "false_alarm"
    assert res.json()["event"]["review_note"] == "의자에 털썩 앉음"
    assert label(ids.help, {"label": "staged_fall"}).status_code == 200
    assert label(ids.no_reply, {"label": "real_fall", "note": "x"}).status_code == 200
    assert label(ids.no_reply, {"label": None, "note": ""}).json()["event"]["review_label"] is None  # 라벨 지우기
    assert label(ids.safe, {"label": "unknown"}).status_code == 400
    assert label(ids.safe, {"label": "real_fall", "note": "가" * 201}).status_code == 422
    assert label(9999, {"label": "real_fall"}).status_code == 404
    assert event_ids(history, label="false_alarm") == [ids.safe]
    assert event_ids(history, label="none") == [ids.no_reply, ids.old]

    stats = admin_c.get("/admin/api/stats").json()
    assert stats["days"] == 14 and stats["to"] == date.today().isoformat()
    assert stats["from"] == (date.today() - timedelta(days=13)).isoformat()
    assert len(stats["per_day"]) == 14
    assert stats["per_day"][-1]["date"] == date.today().isoformat()
    assert stats["per_day"][-1]["total"] == 2
    assert stats["per_day"][-1]["by_result"][CLEARED_VOICE] == 1
    assert stats["per_day"][-2]["by_result"][ALERT_NO_REPLY] == 1
    assert stats["per_day"][0]["total"] == 0
    assert stats["totals"]["total"] == 3   # 30일 전 이벤트는 빠진다
    assert stats["totals"]["by_result"] == {CLEARED_VOICE: 1, CLEARED_BUTTON: 0, ALERT_HELP: 1, ALERT_NO_REPLY: 1,
                                            "ALERT_VOICE_FAIL": 0, "INTERRUPTED": 0, "SUSPECT": 0}
    assert stats["result_labels"]["SUSPECT"] == "음성 확인 중"
    assert stats["voice_results"] == {"NO_REPLY": 1, "SAFE": 1, "HELP": 1}
    assert stats["durations"] == {
        "suspect_to_clear": {"average_seconds": 12.0, "count": 1},
        "suspect_to_alarm": {"average_seconds": 25.0, "count": 2},
        "alarm_to_clear": {"average_seconds": 60.0, "count": 1},
    }
    assert stats["notifications"] == {
        "websocket": {"sent": 1, "failed": 0, "success_rate": 1.0},
        "push": {"sent": 0, "failed": 1, "success_rate": 0.0},
    }
    assert stats["labels"] == {
        "counts": {"real_fall": 0, "staged_fall": 1, "daily_activity": 0, "false_alarm": 1},
        "labeled": 2, "unlabeled": 1, "false_alarm_rate": 0.5,
    }

    one_day = admin_c.get("/admin/api/stats", params={"days": 1}).json()
    assert one_day["totals"]["total"] == 2 and len(one_day["per_day"]) == 1
    assert admin_c.get("/admin/api/stats", params={"days": 0}).status_code == 422


def test_stats_without_events(family):
    stats = family.admin.get("/admin/api/stats").json()
    assert stats["totals"]["total"] == 0
    assert stats["durations"]["alarm_to_clear"] == {"average_seconds": None, "count": 0}
    assert stats["notifications"]["push"]["success_rate"] is None
    assert stats["labels"]["false_alarm_rate"] is None


def test_events_csv_has_bom_header_and_blocks_formulas(history):
    evil_id, _ = create_device(history.owner, name="=HYPERLINK(\"http://evil\")")
    evil_event = insert_event(history.app.state.db, evil_id, datetime.now(), CLEARED_BUTTON, after_end=3)
    history.admin.post(f"/admin/api/events/{evil_event}/label", json={"label": "daily_activity", "note": "@SUM(1)"})

    res = history.admin.get("/admin/api/events.csv")
    assert res.status_code == 200
    assert res.headers["content-type"] == "text/csv; charset=utf-8"
    assert res.headers["content-disposition"].startswith('attachment; filename="events-')
    assert res.content.startswith(b"\xef\xbb\xbf")   # UTF-8 BOM (엑셀 한글)
    rows = list(csv.reader(io.StringIO(res.content.decode("utf-8-sig"))))
    assert rows[0] == [title for title, _ in admin.CSV_COLUMNS]
    assert rows[0][:3] == ["이벤트 번호", "시작 시각", "경보 시각"]
    assert len(rows) == 1 + 5
    first = dict(zip(rows[0], rows[1]))
    assert first["이벤트 번호"] == str(evil_event)
    assert first["기기 이름"] == "'=HYPERLINK(\"http://evil\")"   # 수식으로 실행되지 않게
    assert first["검토 메모"] == "'@SUM(1)"
    assert first["사용자 이름"] == "할머니" and first["판정 설명"] == "화면 버튼으로 해제"
    help_row = next(dict(zip(rows[0], r)) for r in rows[1:] if r[0] == str(history.history.help))
    assert (help_row["화면 알림 성공"], help_row["푸시 실패"], help_row["걸린 시간(초)"]) == ("1", "1", "80.0")

    filtered = history.admin.get("/admin/api/events.csv", params={"result": "ALERT"})
    assert len(list(csv.reader(io.StringIO(filtered.content.decode("utf-8-sig"))))) == 1 + 2


# ---------------------------------------------------------------------------
# DB 갱신(예전 스키마)
# ---------------------------------------------------------------------------

OLD_SCHEMA = """
CREATE TABLE users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE devices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    key_hash TEXT,
    created_at TEXT NOT NULL,
    last_seen_at TEXT
);
CREATE TABLE events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    result TEXT,
    voice_result TEXT,
    voice_text TEXT,
    feature_snapshot TEXT,
    cleared_by TEXT
);
"""


def columns(path, table):
    conn = sqlite3.connect(str(path))
    try:
        return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
    finally:
        conn.close()


def test_old_database_gets_new_columns(tmp_path):
    path = tmp_path / "server.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(OLD_SCHEMA)
    conn.execute("INSERT INTO users (email, password_hash, name, created_at) VALUES (?, ?, ?, ?)",
                 ("old@example.com", auth.hash_password(PASSWORD), "예전 사용자", "2026-01-01T00:00:00"))
    conn.execute("INSERT INTO devices (owner_id, name, kind, created_at) VALUES (1, '거실', 'phone', '2026-01-01')")
    conn.execute("INSERT INTO events (device_id, started_at, result) VALUES (1, '2026-01-01T10:00:00', 'CLEARED_VOICE')")
    conn.commit()
    conn.close()
    assert "disabled_at" not in columns(path, "users")

    for _ in range(2):  # 두 번째로 열 때는 이미 있으므로 아무것도 하지 않는다
        db = Database(path)
        db.close()
    assert columns(path, "users")[-1] == "disabled_at"
    assert columns(path, "events")[-3:] == ["alarm_at", "review_label", "review_note"]

    db = Database(path)
    try:
        accounts = Accounts(db, Settings(data_dir=tmp_path))
        user = accounts.authenticate("old@example.com", PASSWORD)
        assert user["name"] == "예전 사용자" and accounts.is_disabled(user["id"]) is False
        row = db.query_one("SELECT * FROM events")
        assert row["result"] == "CLEARED_VOICE" and row["review_label"] is None
        accounts.set_disabled(user["id"], True)
        with pytest.raises(PermissionError):
            accounts.authenticate("old@example.com", PASSWORD)
    finally:
        db.close()
