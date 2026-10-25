"""계정 모듈 테스트: Accounts(저장소 규칙)와 /api 계정 라우터.

라우터 테스트는 최소 FastAPI 앱에 이 라우터만 붙여 돌린다(main.py에 의존하지 않음).
base_url을 https로 두어 Secure 쿠키가 실제처럼 오가게 한다.
"""

from datetime import datetime, timedelta

import pytest
from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient
from starlette.requests import Request

from server import auth
from server.auth import Accounts, check_origin, device_from_request, user_from_websocket
from server.config import Settings
from server.db import Database
from server.routes_accounts import router

PASSWORD = "password123"


# ---------------------------------------------------------------------------
# 준비
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def fast_bcrypt(monkeypatch):
    """테스트에서는 bcrypt 작업량을 최소로 낮춰 속도를 높인다."""
    monkeypatch.setattr(auth, "BCRYPT_ROUNDS", 4)


@pytest.fixture
def settings(tmp_path):
    return Settings(data_dir=tmp_path, cookie_secure=True, vapid_private_key=None, vapid_public_key=None)


@pytest.fixture
def db(settings):
    database = Database(settings.db_path)
    yield database
    database.close()


@pytest.fixture
def accounts(db, settings):
    return Accounts(db, settings)


@pytest.fixture
def app(settings, db, accounts):
    app = FastAPI()
    app.state.settings = settings
    app.state.db = db
    app.state.accounts = accounts
    app.include_router(router)
    return app


def new_client(app) -> TestClient:
    """사용자마다 쿠키 보관함이 따로 있어야 하므로 클라이언트를 따로 만든다."""
    return TestClient(app, base_url="https://testserver")


def signup(app, email, name="이름", password=PASSWORD) -> TestClient:
    client = new_client(app)
    res = client.post("/api/signup", json={"email": email, "password": password, "name": name})
    assert res.status_code == 201, res.text
    return client


def make_request(app, headers=None, query=b"") -> Request:
    """도우미 함수를 직접 시험하기 위한 가짜 HTTP 요청."""
    scope = {
        "type": "http",
        "app": app,
        "method": "GET",
        "path": "/",
        "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
        "query_string": query,
    }
    return Request(scope)


# ---------------------------------------------------------------------------
# 비밀번호 해시
# ---------------------------------------------------------------------------

def test_password_hash_roundtrip():
    hashed = auth.hash_password(PASSWORD)
    assert hashed != PASSWORD
    assert auth.verify_password(PASSWORD, hashed)
    assert not auth.verify_password("wrong-password", hashed)
    # 72바이트를 넘는 입력은 예외 대신 불일치로 처리
    assert not auth.verify_password("가" * 40, hashed)


# ---------------------------------------------------------------------------
# Accounts: 사용자·세션
# ---------------------------------------------------------------------------

def test_create_user_normalizes_email_and_stores_hash_only(accounts, db):
    user_id = accounts.create_user("  Grandma@Example.COM ", PASSWORD, " 할머니 ")
    user = accounts.get_user(user_id)
    assert user["email"] == "grandma@example.com"
    assert user["name"] == "할머니"
    assert "password_hash" not in user
    row = db.query_one("SELECT password_hash FROM users WHERE id = ?", (user_id,))
    assert PASSWORD not in row["password_hash"]


@pytest.mark.parametrize(
    "email, password, name",
    [
        ("not-an-email", PASSWORD, "이름"),
        ("a@b.com", "short", "이름"),
        ("a@b.com", "가" * 30, "이름"),  # 90바이트: bcrypt 한도 초과
        ("a@b.com", PASSWORD, "   "),
    ],
)
def test_create_user_rejects_invalid_input(accounts, email, password, name):
    with pytest.raises(ValueError):
        accounts.create_user(email, password, name)


def test_create_user_rejects_duplicate_email_case_insensitive(accounts):
    accounts.create_user("a@example.com", PASSWORD, "A")
    with pytest.raises(ValueError):
        accounts.create_user("A@EXAMPLE.com", PASSWORD, "A2")


def test_authenticate(accounts):
    user_id = accounts.create_user("a@example.com", PASSWORD, "A")
    assert accounts.authenticate("A@example.com", PASSWORD)["id"] == user_id
    assert accounts.authenticate("a@example.com", "wrong-password") is None
    assert accounts.authenticate("nobody@example.com", PASSWORD) is None


def test_session_stores_hash_and_expires(accounts, db):
    user_id = accounts.create_user("a@example.com", PASSWORD, "A")
    token = accounts.create_session(user_id)
    assert accounts.user_for_session(token)["id"] == user_id
    # 원문 토큰은 DB에 없다
    assert db.query_one("SELECT * FROM sessions WHERE token_hash = ?", (token,)) is None

    past = (datetime.now() - timedelta(seconds=1)).isoformat()
    db.execute("UPDATE sessions SET expires_at = ?", (past,))
    assert accounts.user_for_session(token) is None
    assert db.query("SELECT * FROM sessions") == []  # 만료 세션은 지워진다

    assert accounts.user_for_session(None) is None
    assert accounts.user_for_session("garbage") is None


# ---------------------------------------------------------------------------
# Accounts: 보호자 연결
# ---------------------------------------------------------------------------

@pytest.fixture
def people(accounts):
    """ward=보호받는 사용자, guardian=초대받는 보호자, other=관계없는 사람."""
    return {
        "ward": accounts.create_user("ward@example.com", PASSWORD, "어르신"),
        "guardian": accounts.create_user("guardian@example.com", PASSWORD, "보호자"),
        "other": accounts.create_user("other@example.com", PASSWORD, "남"),
    }


def test_pending_link_grants_nothing(accounts, people):
    link = accounts.request_guardian(people["ward"], "GUARDIAN@example.com")
    assert link["status"] == "pending"
    # 수락 전에는 요청한 사람에게 상대의 이름·id를 보여 주지 않는다(입력한 이메일만)
    assert link["user"] == {"id": None, "name": None, "email": "guardian@example.com"}
    # 수락 전에는 경보 대상도 아니고, 피보호자를 볼 수도 없다
    assert accounts.guardian_ids_for(people["ward"]) == []
    assert accounts.wards_of(people["guardian"]) == []
    assert accounts.visible_owner_ids(people["guardian"]) == [people["guardian"]]


def test_accept_makes_link_effective(accounts, people):
    link = accounts.request_guardian(people["ward"], "guardian@example.com")
    accepted = accounts.accept_guardian(link["link_id"], people["guardian"])
    assert accepted["status"] == "accepted"
    assert accepted["user"]["id"] == people["ward"]  # 보호자 쪽에서 본 상대방
    assert accounts.guardian_ids_for(people["ward"]) == [people["guardian"]]
    assert accounts.wards_of(people["guardian"]) == [people["ward"]]


def test_pending_request_hides_invitee_name_until_accepted(accounts, people):
    """이메일만 넣어 보고 상대의 실명을 알아내지 못하게 한다. 수락하면 그때 보여 준다."""
    link = accounts.request_guardian(people["ward"], "guardian@example.com")
    (mine,) = accounts.guardian_links_of(people["ward"])
    assert mine["user"]["name"] is None and mine["user"]["id"] is None
    # 요청을 받은 보호자는 누가 요청했는지 보고 결정해야 한다
    (received,) = accounts.ward_links_of(people["guardian"], "pending")
    assert received["user"]["name"] == "어르신"

    accounts.accept_guardian(link["link_id"], people["guardian"])
    (mine,) = accounts.guardian_links_of(people["ward"])
    assert mine["user"] == {"id": people["guardian"], "name": "보호자", "email": "guardian@example.com"}
    assert accounts.visible_owner_ids(people["guardian"]) == [people["guardian"], people["ward"]]
    # 보호 관계는 한 방향이다: 어르신이 보호자의 기기를 보게 되지는 않는다
    assert accounts.visible_owner_ids(people["ward"]) == [people["ward"]]
    # 다시 수락해도 그대로
    assert accounts.accept_guardian(link["link_id"], people["guardian"])["status"] == "accepted"


def test_only_invited_guardian_can_accept(accounts, people):
    link = accounts.request_guardian(people["ward"], "guardian@example.com")
    with pytest.raises(PermissionError):
        accounts.accept_guardian(link["link_id"], people["ward"])  # 요청한 본인
    with pytest.raises(PermissionError):
        accounts.accept_guardian(link["link_id"], people["other"])
    with pytest.raises(LookupError):
        accounts.accept_guardian(9999, people["guardian"])
    assert accounts.guardian_ids_for(people["ward"]) == []


@pytest.mark.parametrize("email", ["nobody@example.com", "ward@example.com"])
def test_request_guardian_rejects_unknown_and_self(accounts, people, email):
    with pytest.raises(ValueError):
        accounts.request_guardian(people["ward"], email)


def test_request_guardian_rejects_duplicate(accounts, people):
    link = accounts.request_guardian(people["ward"], "guardian@example.com")
    with pytest.raises(ValueError):
        accounts.request_guardian(people["ward"], "guardian@example.com")
    accounts.accept_guardian(link["link_id"], people["guardian"])
    with pytest.raises(ValueError):
        accounts.request_guardian(people["ward"], "guardian@example.com")


def test_remove_guardian_by_either_party(accounts, people):
    link = accounts.request_guardian(people["ward"], "guardian@example.com")
    accounts.accept_guardian(link["link_id"], people["guardian"])
    with pytest.raises(PermissionError):
        accounts.remove_guardian(link["link_id"], people["other"])

    accounts.remove_guardian(link["link_id"], people["guardian"])
    assert accounts.guardian_ids_for(people["ward"]) == []
    with pytest.raises(LookupError):
        accounts.remove_guardian(link["link_id"], people["ward"])

    # 해제 후에는 다시 요청할 수 있고, 이번엔 요청한 쪽이 취소한다
    link2 = accounts.request_guardian(people["ward"], "guardian@example.com")
    accounts.remove_guardian(link2["link_id"], people["ward"])
    assert accounts.guardian_links_of(people["ward"]) == []


# ---------------------------------------------------------------------------
# Accounts: 기기·푸시
# ---------------------------------------------------------------------------

def test_pi_device_key_returned_once_and_verifies(accounts, db, people):
    device, key = accounts.create_device(people["ward"], "거실 Pi", "pi")
    assert key and device["kind"] == "pi"
    assert "key_hash" not in device
    assert db.query_one("SELECT key_hash FROM devices WHERE id = ?", (device["id"],))["key_hash"] != key
    assert accounts.verify_device_key(device["id"], key)["id"] == device["id"]
    assert accounts.verify_device_key(device["id"], "wrong-key") is None
    assert accounts.verify_device_key(device["id"], None) is None
    assert accounts.verify_device_key(9999, key) is None
    # 다른 기기의 키로는 이 기기로 접속할 수 없다
    other_device, other_key = accounts.create_device(people["ward"], "안방 Pi", "pi")
    assert accounts.verify_device_key(device["id"], other_key) is None


def test_device_key_refused_while_owner_suspended(app, accounts, people):
    """주인 계정이 정지되면 키가 맞아도 기기 키로 여는 API(현장 화면 상태, 해제, 음성 업로드)를 못 쓴다."""
    device, key = accounts.create_device(people["ward"], "거실 Pi", "pi")
    accounts.set_disabled(people["ward"], True)
    assert accounts.verify_device_key(device["id"], key) is None
    assert device_from_request(make_request(app, headers={"X-Device-Key": key}), device["id"]) is None
    # 카메라 WebSocket만 키를 따로 확인해 '주인 계정 정지'(4403)라고 알려 준다. 틀린 키는 그래도 거부.
    assert accounts.verify_device_key(device["id"], key, allow_suspended_owner=True)["id"] == device["id"]
    assert accounts.verify_device_key(device["id"], "wrong-key", allow_suspended_owner=True) is None

    accounts.set_disabled(people["ward"], False)
    assert accounts.verify_device_key(device["id"], key)["id"] == device["id"]


def test_phone_device_has_no_key(accounts, people):
    device, key = accounts.create_device(people["ward"], "내 휴대폰", "phone")
    assert key is None
    assert accounts.verify_device_key(device["id"], "") is None
    assert accounts.verify_device_key(device["id"], "anything") is None


def test_create_device_validates(accounts, people):
    with pytest.raises(ValueError):
        accounts.create_device(people["ward"], "x", "tablet")
    with pytest.raises(ValueError):
        accounts.create_device(people["ward"], "  ", "pi")


def test_devices_of_and_delete(accounts, people):
    d1, _ = accounts.create_device(people["ward"], "A", "pi")
    d2, _ = accounts.create_device(people["guardian"], "B", "phone")
    assert [d["id"] for d in accounts.devices_of([people["ward"]])] == [d1["id"]]
    assert [d["id"] for d in accounts.devices_of([people["ward"], people["guardian"]])] == [d1["id"], d2["id"]]
    assert accounts.devices_of([]) == []

    with pytest.raises(PermissionError):
        accounts.delete_device(d1["id"], people["guardian"])
    accounts.delete_device(d1["id"], people["ward"])
    assert accounts.get_device(d1["id"]) is None
    with pytest.raises(LookupError):
        accounts.delete_device(d1["id"], people["ward"])


@pytest.mark.parametrize("endpoint", [
    "https://fcm.googleapis.com/fcm/send/abc",
    "https://updates.push.services.mozilla.com/wpush/v2/abc",
    "https://wns2-par02p.notify.windows.com/w/?token=abc",
    "https://web.push.apple.com/abc",
])
def test_push_subscription_accepts_browser_push_services(accounts, people, endpoint):
    accounts.save_push_subscription(people["guardian"], endpoint, "p", "a")
    assert [s["endpoint"] for s in accounts.push_subscriptions_for([people["guardian"]])] == [endpoint]


@pytest.mark.parametrize("endpoint", [
    "http://fcm.googleapis.com/fcm/send/abc",          # https가 아님
    "https://127.0.0.1/admin",                         # 내부 주소
    "https://169.254.169.254/latest/meta-data",        # 클라우드 메타데이터
    "https://fcm.googleapis.com.evil.example/x",       # 도메인 뒤에 덧붙임
    "https://evilfcm.googleapis.com.example/x",
    "https://fcm.googleapis.com:8443/x",               # 다른 포트
    "https://user@fcm.googleapis.com/x",
    "",
])
def test_push_subscription_rejects_other_endpoints(accounts, people, endpoint):
    with pytest.raises(ValueError):
        accounts.save_push_subscription(people["guardian"], endpoint, "p", "a")
    assert accounts.push_subscriptions_for([people["guardian"]]) == []


def test_push_subscriptions_skip_suspended_users(accounts, people):
    """정지된 보호자에게는 푸시(재시도 포함)를 보내지 않는다. 정지를 풀면 구독이 그대로 다시 쓰인다."""
    accounts.save_push_subscription(people["guardian"], "https://fcm.googleapis.com/fcm/send/g", "p", "a")
    accounts.save_push_subscription(people["other"], "https://fcm.googleapis.com/fcm/send/o", "p", "a")
    both = [people["guardian"], people["other"]]
    accounts.set_disabled(people["guardian"], True)
    assert [s["user_id"] for s in accounts.push_subscriptions_for(both)] == [people["other"]]
    accounts.set_disabled(people["guardian"], False)
    assert [s["user_id"] for s in accounts.push_subscriptions_for(both)] == both


def test_mark_device_seen(accounts, people):
    device, _ = accounts.create_device(people["ward"], "A", "pi")
    assert device["last_seen_at"] is None
    accounts.mark_device_seen(device["id"])
    assert accounts.get_device(device["id"])["last_seen_at"] is not None


def test_push_subscription_upsert_and_delete(accounts, people):
    accounts.save_push_subscription(people["guardian"], "https://fcm.googleapis.com/fcm/send/1", "p1", "a1")
    accounts.save_push_subscription(people["guardian"], "https://fcm.googleapis.com/fcm/send/1", "p2", "a2")  # 같은 endpoint
    subs = accounts.push_subscriptions_for([people["guardian"]])
    assert len(subs) == 1 and subs[0]["p256dh"] == "p2" and subs[0]["auth"] == "a2"

    # 같은 브라우저에서 다른 계정으로 켜면 그 계정으로 옮겨 간다
    accounts.save_push_subscription(people["other"], "https://fcm.googleapis.com/fcm/send/1", "p3", "a3")
    assert accounts.push_subscriptions_for([people["guardian"]]) == []
    assert len(accounts.push_subscriptions_for([people["other"], people["ward"]])) == 1
    assert accounts.push_subscriptions_for([]) == []

    # 남의 구독은 user_id를 지정하면 지워지지 않는다
    accounts.delete_push_subscription("https://fcm.googleapis.com/fcm/send/1", user_id=people["guardian"])
    assert len(accounts.push_subscriptions_for([people["other"]])) == 1
    accounts.delete_push_subscription("https://fcm.googleapis.com/fcm/send/1")  # 만료 정리용: 무조건 삭제
    assert accounts.push_subscriptions_for([people["other"]]) == []


# ---------------------------------------------------------------------------
# 라우터: 회원가입·로그인·로그아웃
# ---------------------------------------------------------------------------

def test_signup_sets_secure_httponly_cookie(app):
    client = new_client(app)
    res = client.post("/api/signup", json={"email": "A@Example.com", "password": PASSWORD, "name": "A"})
    assert res.status_code == 201
    assert res.json()["user"]["email"] == "a@example.com"
    cookie = res.headers["set-cookie"].lower()
    assert "httponly" in cookie and "secure" in cookie and "samesite=lax" in cookie
    me = client.get("/api/me")
    assert me.status_code == 200
    assert me.json()["user"] == {"id": res.json()["user"]["id"], "email": "a@example.com", "name": "A"}


def test_signup_errors(app):
    signup(app, "a@example.com")
    client = new_client(app)
    dup = client.post("/api/signup", json={"email": "a@example.com", "password": PASSWORD, "name": "B"})
    assert dup.status_code == 400 and "이미" in dup.json()["detail"]
    short = client.post("/api/signup", json={"email": "b@example.com", "password": "1234", "name": "B"})
    assert short.status_code == 400
    missing = client.post("/api/signup", json={"email": "b@example.com"})
    assert missing.status_code == 422
    assert client.get("/api/me").status_code == 401


def test_login_logout(app):
    signup(app, "a@example.com")
    client = new_client(app)
    assert client.get("/api/me").status_code == 401

    res = client.post("/api/login", json={"email": "a@example.com", "password": PASSWORD})
    assert res.status_code == 200 and res.json()["user"]["email"] == "a@example.com"
    assert client.get("/api/me").status_code == 200

    assert client.post("/api/logout").status_code == 200
    assert client.get("/api/me").status_code == 401


def test_logout_invalidates_session_on_server(app):
    """쿠키를 지운 뒤에도 예전 토큰을 다시 보내면 거부되어야 한다."""
    client = signup(app, "a@example.com")
    token = client.cookies.get("session")
    client.post("/api/logout")
    stale = new_client(app)
    stale.cookies.set("session", token)
    assert stale.get("/api/me").status_code == 401


def test_login_same_message_for_unknown_email_and_wrong_password(app):
    signup(app, "a@example.com")
    client = new_client(app)
    wrong = client.post("/api/login", json={"email": "a@example.com", "password": "wrong-password"})
    unknown = client.post("/api/login", json={"email": "nobody@example.com", "password": PASSWORD})
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json()["detail"] == unknown.json()["detail"]
    assert "set-cookie" not in wrong.headers


# ---------------------------------------------------------------------------
# 라우터: 보호자 연결
# ---------------------------------------------------------------------------

def test_guardian_request_accept_flow(app):
    ward = signup(app, "ward@example.com", "어르신")
    guardian = signup(app, "guardian@example.com", "보호자")

    res = ward.post("/api/guardians", json={"email": "guardian@example.com"})
    assert res.status_code == 201
    link_id = res.json()["link_id"]
    assert res.json()["status"] == "pending"

    # 요청 중: 어르신은 '요청 중' 보호자를 보고, 보호자는 받은 요청만 본다(피보호자는 아직 아님)
    ward_me = ward.get("/api/me").json()
    assert [(g["link_id"], g["status"], g["user"]["name"], g["user"]["email"]) for g in ward_me["guardians"]] == [
        (link_id, "pending", None, "guardian@example.com")   # 수락 전에는 이름을 보여 주지 않는다
    ]
    guardian_me = guardian.get("/api/me").json()
    assert guardian_me["wards"] == []
    assert [(r["link_id"], r["user"]["name"]) for r in guardian_me["requests"]] == [(link_id, "어르신")]

    assert guardian.post(f"/api/guardians/{link_id}/accept").status_code == 200

    guardian_me = guardian.get("/api/me").json()
    assert guardian_me["requests"] == []
    assert [(w["link_id"], w["status"], w["user"]["email"]) for w in guardian_me["wards"]] == [
        (link_id, "accepted", "ward@example.com")
    ]
    assert ward.get("/api/me").json()["guardians"][0]["status"] == "accepted"
    accounts = app.state.accounts
    ward_id = ward.get("/api/me").json()["user"]["id"]
    guardian_id = guardian_me["user"]["id"]
    assert accounts.guardian_ids_for(ward_id) == [guardian_id]


def test_guardian_request_errors(app):
    ward = signup(app, "ward@example.com")
    assert ward.post("/api/guardians", json={"email": "nobody@example.com"}).status_code == 400
    assert ward.post("/api/guardians", json={"email": "ward@example.com"}).status_code == 400
    signup(app, "guardian@example.com")
    assert ward.post("/api/guardians", json={"email": "guardian@example.com"}).status_code == 201
    assert ward.post("/api/guardians", json={"email": "guardian@example.com"}).status_code == 400
    assert new_client(app).post("/api/guardians", json={"email": "guardian@example.com"}).status_code == 401


def test_guardian_accept_and_delete_permissions(app):
    ward = signup(app, "ward@example.com")
    guardian = signup(app, "guardian@example.com")
    other = signup(app, "other@example.com")
    link_id = ward.post("/api/guardians", json={"email": "guardian@example.com"}).json()["link_id"]

    # 요청한 본인도, 제3자도 수락할 수 없다
    assert ward.post(f"/api/guardians/{link_id}/accept").status_code == 403
    assert other.post(f"/api/guardians/{link_id}/accept").status_code == 403
    assert guardian.post("/api/guardians/9999/accept").status_code == 404
    assert other.delete(f"/api/guardians/{link_id}").status_code == 403
    assert ward.get("/api/me").json()["guardians"][0]["status"] == "pending"

    # 보호자는 요청을 거절(삭제)할 수 있다
    assert guardian.delete(f"/api/guardians/{link_id}").status_code == 200
    assert ward.get("/api/me").json()["guardians"] == []
    assert guardian.delete(f"/api/guardians/{link_id}").status_code == 404


def test_ward_can_remove_accepted_guardian(app):
    ward = signup(app, "ward@example.com")
    guardian = signup(app, "guardian@example.com")
    link_id = ward.post("/api/guardians", json={"email": "guardian@example.com"}).json()["link_id"]
    guardian.post(f"/api/guardians/{link_id}/accept")
    assert ward.delete(f"/api/guardians/{link_id}").status_code == 200
    assert guardian.get("/api/me").json()["wards"] == []


# ---------------------------------------------------------------------------
# 라우터: 기기
# ---------------------------------------------------------------------------

def test_device_routes(app):
    owner = signup(app, "owner@example.com")
    other = signup(app, "other@example.com")

    pi = owner.post("/api/devices", json={"name": "거실 Pi", "kind": "pi"})
    assert pi.status_code == 201
    pi_device, pi_key = pi.json()["device"], pi.json()["key"]
    assert app.state.accounts.verify_device_key(pi_device["id"], pi_key)

    phone = owner.post("/api/devices", json={"name": "내 휴대폰", "kind": "phone"})
    assert phone.status_code == 201 and "key" not in phone.json()

    assert owner.post("/api/devices", json={"name": "x", "kind": "tablet"}).status_code == 422
    assert owner.post("/api/devices", json={"name": " ", "kind": "pi"}).status_code == 400

    # 목록에는 키(해시 포함)가 다시 나오지 않는다
    listed = owner.get("/api/devices").json()["devices"]
    assert [d["name"] for d in listed] == ["거실 Pi", "내 휴대폰"]
    assert all("key" not in d and "key_hash" not in d for d in listed)
    assert other.get("/api/devices").json()["devices"] == []

    assert other.delete(f"/api/devices/{pi_device['id']}").status_code == 403
    assert owner.delete(f"/api/devices/{pi_device['id']}").status_code == 200
    assert owner.delete(f"/api/devices/{pi_device['id']}").status_code == 404
    assert new_client(app).get("/api/devices").status_code == 401


# ---------------------------------------------------------------------------
# 라우터: 웹 푸시
# ---------------------------------------------------------------------------

def test_vapid_public_key(app, settings):
    client = new_client(app)
    assert client.get("/api/push/vapid-public-key").json() == {"key": None, "enabled": False}
    settings.vapid_public_key = "PUBLIC"
    settings.vapid_private_key = "PRIVATE"
    assert client.get("/api/push/vapid-public-key").json() == {"key": "PUBLIC", "enabled": True}


def test_push_subscribe_upsert_and_unsubscribe(app):
    guardian = signup(app, "guardian@example.com")
    other = signup(app, "other@example.com")
    accounts = app.state.accounts
    guardian_id = guardian.get("/api/me").json()["user"]["id"]
    body = {"endpoint": "https://fcm.googleapis.com/fcm/send/abc", "keys": {"p256dh": "P1", "auth": "A1"}}

    assert guardian.post("/api/push/subscribe", json=body).status_code == 200
    body["keys"] = {"p256dh": "P2", "auth": "A2"}
    assert guardian.post("/api/push/subscribe", json=body).status_code == 200
    subs = accounts.push_subscriptions_for([guardian_id])
    assert [(s["endpoint"], s["p256dh"]) for s in subs] == [("https://fcm.googleapis.com/fcm/send/abc", "P2")]

    assert guardian.post("/api/push/subscribe", json={"endpoint": "x"}).status_code == 422
    # 푸시 서비스가 아닌 주소(내부망 등)는 받지 않는다. 서버가 그 주소로 요청을 보내게 되기 때문이다.
    bad = dict(body, endpoint="http://127.0.0.1:8080/admin")
    assert guardian.post("/api/push/subscribe", json=bad).status_code == 400
    assert new_client(app).post("/api/push/subscribe", json=body).status_code == 401

    # 다른 사람은 내 구독을 끌 수 없다
    other.post("/api/push/unsubscribe", json={"endpoint": "https://fcm.googleapis.com/fcm/send/abc"})
    assert len(accounts.push_subscriptions_for([guardian_id])) == 1
    guardian.post("/api/push/unsubscribe", json={"endpoint": "https://fcm.googleapis.com/fcm/send/abc"})
    assert accounts.push_subscriptions_for([guardian_id]) == []


# ---------------------------------------------------------------------------
# 인증 도우미: 기기 키, Origin 검사, WebSocket 쿠키
# ---------------------------------------------------------------------------

def test_device_from_request_header_or_query(app, accounts):
    owner = accounts.create_user("a@example.com", PASSWORD, "A")
    device, key = accounts.create_device(owner, "Pi", "pi")
    by_header = make_request(app, headers={"X-Device-Key": key})
    by_query = make_request(app, query=f"key={key}".encode())
    assert device_from_request(by_header, device["id"])["id"] == device["id"]
    assert device_from_request(by_query, device["id"])["id"] == device["id"]
    assert device_from_request(make_request(app, headers={"X-Device-Key": "nope"}), device["id"]) is None
    assert device_from_request(make_request(app), device["id"]) is None


@pytest.mark.parametrize(
    "headers, allowed",
    [
        ({"host": "192.168.0.10:8443"}, True),                                       # Pi 등 비브라우저
        ({"host": "192.168.0.10:8443", "origin": "https://192.168.0.10:8443"}, True),
        ({"host": "Server.local", "origin": "https://server.local"}, True),           # 대소문자 무시
        ({"host": "192.168.0.10:8443", "origin": "https://evil.example"}, False),
        ({"host": "192.168.0.10:8443", "origin": "https://192.168.0.10:9999"}, False),
        ({"host": "192.168.0.10:8443", "origin": "null"}, False),
    ],
)
def test_check_origin(app, headers, allowed):
    assert check_origin(make_request(app, headers=headers)) is allowed


def test_user_from_websocket_reads_session_cookie(app):
    @app.websocket("/ws/whoami")
    async def whoami(ws: WebSocket):
        await ws.accept()
        user = user_from_websocket(ws)
        await ws.send_json({"user_id": user["id"] if user else None, "origin_ok": check_origin(ws)})
        await ws.close()

    client = signup(app, "a@example.com")
    user_id = client.get("/api/me").json()["user"]["id"]
    # TestClient는 상대 경로를 ws://로 열어 Secure 쿠키를 보내지 않으므로 wss:// 전체 주소를 쓴다.
    with client.websocket_connect("wss://testserver/ws/whoami") as ws:
        assert ws.receive_json() == {"user_id": user_id, "origin_ok": True}
    with client.websocket_connect("wss://testserver/ws/whoami", headers={"origin": "https://evil.example"}) as ws:
        assert ws.receive_json()["origin_ok"] is False
    with new_client(app).websocket_connect("wss://testserver/ws/whoami") as ws:
        assert ws.receive_json()["user_id"] is None
