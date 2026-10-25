"""실시간 처리(기기 연결·프레임 분석·이벤트·음성 확인·경보·해제)와 앱 조립 테스트.

대부분은 create_app()으로 만든 실제 앱에 TestClient로 접속해 처음부터 끝까지 확인한다.
- 자세 추정·음성 인식은 가짜(FakePoseEstimator, FakeTranscriber)를 쓴다. 프레임은 관절 좌표 JSON,
  녹음은 인식 문장(UTF-8)이다.
- T1(누운 자세 유지 시간)과 음성 대기 시간은 아주 짧게 줄인다.
- 사용자마다 쿠키 보관함이 따로 있어야 하므로 TestClient를 여러 개 쓰되, 이벤트 루프는 하나여야
  하므로(서버 상태가 그 루프에 묶여 있음) 첫 클라이언트의 portal을 함께 쓴다.
- Secure 쿠키를 보내도록 WebSocket은 wss:// 전체 주소로 연다.
"""

import asyncio
import json
import logging
import threading
import time
import types

import anyio
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from server import auth
from server import notify as notify_mod
from server import routes_realtime
from server.auth import Accounts
from server.config import Settings
from server.db import (
    ALERT_HELP,
    ALERT_NO_REPLY,
    ALERT_VOICE_FAIL,
    CLEARED_BUTTON,
    CLEARED_VOICE,
    INTERRUPTED,
    Database,
    now_iso,
)
from server.detection import FakePoseEstimator, PoseEstimator, fake_frame
from server.devices import DeviceHub
from server.events import EventService
from server.main import create_app
from server.notify import GuardianHub, Notifier
from server.voice import FakeTranscriber

PASSWORD = "password123"
BASE = "https://testserver"
WS_BASE = "wss://testserver"
FRAME_GAP = 0.05


# ---------------------------------------------------------------------------
# 준비
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def fast_bcrypt(monkeypatch):
    monkeypatch.setattr(auth, "BCRYPT_ROUNDS", 4)


def make_settings(tmp_path, **overrides):
    values = dict(
        data_dir=tmp_path / "data",
        web_dir=tmp_path / "web",
        cookie_secure=True,
        pose_backend="fake",
        stt_backend="fake",
        t1_lying_seconds=0.3,
        voice_max_attempts=2,
        voice_reply_timeout_seconds=0.6,
        camera_stale_seconds=1.0,
        vapid_private_key=None,
        vapid_public_key=None,
        push_retry_delays=[],
        device_reconnect_grace_seconds=0.2,
    )
    values.update(overrides)
    return Settings(**values)


@pytest.fixture
def settings(tmp_path):
    return make_settings(tmp_path)


def start_app(settings, transcriber=None):
    app = create_app(settings, pose_estimator=FakePoseEstimator(), transcriber=transcriber or FakeTranscriber())
    return app, TestClient(app, base_url=BASE)


@pytest.fixture
def server(settings):
    """(app, main 클라이언트). with 블록 안에서 lifespan(복구·점검 루프)이 돈다."""
    app, main = start_app(settings)
    with main:
        yield app, main


def client_for(main: TestClient) -> TestClient:
    """쿠키 보관함은 따로, 이벤트 루프(portal)는 main과 같이 쓰는 클라이언트."""
    client = TestClient(main.app, base_url=BASE)
    client.portal = main.portal
    return client


def signup(main, email, name):
    client = client_for(main)
    res = client.post("/api/signup", json={"email": email, "password": PASSWORD, "name": name})
    assert res.status_code == 201, res.text
    client.user = res.json()["user"]
    return client


def link_guardian(owner, guardian):
    res = owner.post("/api/guardians", json={"email": guardian.user["email"]})
    assert res.status_code == 201, res.text
    res = guardian.post(f"/api/guardians/{res.json()['link_id']}/accept")
    assert res.status_code == 200, res.text


def create_device(owner, kind="pi", name="거실 Pi"):
    res = owner.post("/api/devices", json={"name": name, "kind": kind})
    assert res.status_code == 201, res.text
    body = res.json()
    return body["device"]["id"], body.get("key")


@pytest.fixture
def family(server):
    """할머니(기기 주인) + 수락된 보호자 + Pi 기기."""
    app, main = server
    owner = signup(main, "grandma@example.com", "할머니")
    guardian = signup(main, "son@example.com", "아들")
    link_guardian(owner, guardian)
    device_id, key = create_device(owner)
    return {"app": app, "main": main, "owner": owner, "guardian": guardian, "device_id": device_id, "key": key}


# --- WebSocket 도우미 -------------------------------------------------------

def recv(ws, timeout=3.0):
    """시간 제한이 있는 receive_json. TestClient의 receive는 무한정 기다려 테스트가 멈출 수 있다."""
    async def _receive():
        with anyio.fail_after(timeout):
            return await ws._send_rx.receive()
    message = ws.portal.call(_receive)
    ws._raise_on_close(message)
    return json.loads(message["text"])


def recv_until(ws, predicate, timeout=5.0):
    """조건에 맞는 메시지가 올 때까지 다른 메시지(기기 상태 갱신 등)는 건너뛴다."""
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AssertionError("기다리던 메시지가 오지 않음")
        message = recv(ws, remaining)
        if predicate(message):
            return message


def is_event(state, event_id=None):
    def check(m):
        return (m["type"] == "event" and m["event"]["state"] == state
                and (event_id is None or m["event"]["id"] == event_id))
    return check


def is_state(state):
    return lambda m: m["type"] == "state" and m["state"] == state


def is_voice_check(attempt=None):
    return lambda m: m["type"] == "voice_check" and (attempt is None or m["attempt"] == attempt)


def camera(client, device_id, key=None, **kwargs):
    url = f"{WS_BASE}/ws/camera?device_id={device_id}" + (f"&key={key}" if key else "")
    return client.websocket_connect(url, **kwargs)


def open_camera(stack_ws):
    """접속 직후 hello와 현재 상태를 받는다."""
    hello = recv(stack_ws)
    assert hello["type"] == "hello"
    state = recv(stack_ws)
    assert state["type"] == "state"
    return hello, state


def send_fall(ws):
    """서 있다가(5장) 엉덩이가 몸통 길이 2배만큼 떨어져 누운 뒤, T1(0.3초)보다 오래 누워 있는 영상."""
    for _ in range(5):
        ws.send_bytes(fake_frame("standing"))
        time.sleep(FRAME_GAP)
    for _ in range(12):
        ws.send_bytes(fake_frame("lying", offset_y=100))
        time.sleep(FRAME_GAP)


def fall_and_get_voice_check(ws):
    send_fall(ws)
    recv_until(ws, is_state("SUSPECT"))
    check = recv_until(ws, is_voice_check(1))
    return check["event_id"]


def upload(client, event_id, text=None, key=None, error=None, attempt=None, audio=None):
    files = None
    if audio is not None or text is not None:
        files = {"audio": ("reply.wav", audio if audio is not None else text.encode("utf-8"), "audio/wav")}
    data = {}
    if error:
        data["error"] = error
    if attempt is not None:
        data["attempt"] = str(attempt)
    headers = {"X-Device-Key": key} if key else {}
    return client.post(f"/api/events/{event_id}/voice", files=files, data=data, headers=headers)


def event_row(app, event_id):
    return app.state.db.query_one("SELECT * FROM events WHERE id = ?", (event_id,))


def notifications(app, event_id, kind=None):
    rows = app.state.db.query("SELECT * FROM notifications WHERE event_id = ? ORDER BY id", (event_id,))
    return [r for r in rows if kind is None or r["kind"] == kind]


def wait_until(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("조건이 시간 안에 만족되지 않음")


# ---------------------------------------------------------------------------
# 전체 흐름
# ---------------------------------------------------------------------------

def test_voice_safe_clears_event_without_alarm(family):
    app, main, guardian = family["app"], family["main"], family["guardian"]
    device_id, key = family["device_id"], family["key"]

    with guardian.websocket_connect(f"{WS_BASE}/ws/guardian") as gws:
        snapshot = recv(gws)
        assert snapshot["type"] == "snapshot"
        assert [d["device_id"] for d in snapshot["devices"]] == [device_id]
        assert snapshot["devices"][0]["online"] is False
        assert snapshot["devices"][0]["owner_name"] == "할머니"
        assert snapshot["active_events"] == []

        with camera(main, device_id, key) as ws:
            hello, state = open_camera(ws)
            assert hello["device_id"] == device_id
            assert state["state"] == "NORMAL"
            online = recv_until(gws, lambda m: m["type"] == "device" and m["device"]["online"])
            assert online["device"]["device_id"] == device_id

            event_id = fall_and_get_voice_check(ws)
            suspect = recv_until(gws, is_event("SUSPECT", event_id))
            assert suspect["event"]["owner_name"] == "할머니"
            assert json.loads(event_row(app, event_id)["feature_snapshot"])["torso_angle"] >= 60

            # 주인 계정 쿠키가 있는 브라우저에서 올려도 맞는 기기 키가 있으면 기기로 인증한다.
            res = upload(family["owner"], event_id, "네 괜찮아요", key=key, attempt=1)
            assert res.status_code == 200 and res.json() == {"accepted": True}
            recv_until(ws, is_state("NORMAL"))
            ended = recv_until(gws, is_event("ENDED", event_id))
            assert ended["event"]["result"] == CLEARED_VOICE

            row = event_row(app, event_id)
            assert row["result"] == CLEARED_VOICE and row["voice_result"] == "SAFE"
            assert row["ended_at"] is not None and row["cleared_by"] is None
            assert row["voice_text"] is None  # 기본 설정은 인식 문장을 저장하지 않는다
            assert notifications(app, event_id) == []  # 경보가 아니므로 보호자 알림 없음

            # 감지가 초기화되어 다음 낙상은 새 이벤트가 된다.
            second = fall_and_get_voice_check(ws)
            assert second != event_id


def test_help_raises_alarm_notifies_guardian_and_guardian_clears(family):
    app, main, owner, guardian = family["app"], family["main"], family["owner"], family["guardian"]
    device_id, key = family["device_id"], family["key"]

    with guardian.websocket_connect(f"{WS_BASE}/ws/guardian") as gws, \
            owner.websocket_connect(f"{WS_BASE}/ws/guardian") as ows:
        recv(gws), recv(ows)
        with camera(main, device_id, key) as ws:
            open_camera(ws)
            event_id = fall_and_get_voice_check(ws)

            assert upload(main, event_id, "도와주세요", key=key).json() == {"accepted": True}
            recv_until(ws, is_state("ALARM"))
            alarm = recv_until(gws, is_event("ALARM", event_id))
            assert alarm["event"]["result"] == ALERT_HELP
            assert alarm["event"]["result_label"] == "음성으로 도움 요청"
            recv_until(ows, is_event("ALARM", event_id))  # 주인 화면에도 보인다

            row = event_row(app, event_id)
            assert row["result"] == ALERT_HELP and row["voice_result"] == "HELP" and row["ended_at"] is None

            wait_until(lambda: len(notifications(app, event_id, "alert")) == 2)
            sent = {(r["channel"], r["state"]) for r in notifications(app, event_id, "alert")}
            assert sent == {("websocket", "sent"), ("push", "failed")}  # 테스트 서버에는 VAPID 키가 없다
            assert all(r["guardian_id"] == guardian.user["id"] for r in notifications(app, event_id))

            # 진행 중인 이벤트가 있는 동안 같은 기기의 새 낙상 의심은 이벤트를 만들지 않는다.
            session = app.state.devices.sessions[device_id]
            assert main.portal.call(app.state.events.on_suspect, session, {}) is None

            # 보호자가 경보를 끝낸다.
            res = guardian.post(f"/api/events/{event_id}/clear")
            assert res.status_code == 200, res.text
            assert res.json()["event"]["state"] == "ENDED"
            recv_until(ws, is_state("NORMAL"))
            recv_until(ows, is_event("ENDED", event_id))
            recv_until(gws, is_event("ENDED", event_id))

            row = event_row(app, event_id)
            assert row["result"] == ALERT_HELP  # 판정은 그대로 두고 종료 정보만 채운다
            assert row["ended_at"] is not None
            assert row["cleared_by"] == f"user:{guardian.user['id']}"
            wait_until(lambda: len(notifications(app, event_id, "clear")) == 2)

            # 이미 끝난 이벤트를 다시 해제하면 409
            assert owner.post(f"/api/events/{event_id}/clear").status_code == 409


def test_no_reply_after_all_attempts_raises_alarm(family):
    app, main, guardian = family["app"], family["main"], family["guardian"]
    device_id, key = family["device_id"], family["key"]

    with guardian.websocket_connect(f"{WS_BASE}/ws/guardian") as gws:
        recv(gws)
        with camera(main, device_id, key) as ws:
            open_camera(ws)
            event_id = fall_and_get_voice_check(ws)
            # 응답하지 않으면 한 번 더 묻는다.
            second = recv_until(ws, is_voice_check(2))
            assert second["event_id"] == event_id
            recv_until(ws, is_state("ALARM"))
            alarm = recv_until(gws, is_event("ALARM", event_id))
            assert alarm["event"]["result"] == ALERT_NO_REPLY

    row = event_row(app, event_id)
    assert row["result"] == ALERT_NO_REPLY and row["voice_result"] == "NO_REPLY"


def test_unclear_reply_then_help_on_second_attempt(family):
    app, main, key, device_id = family["app"], family["main"], family["key"], family["device_id"]
    with camera(main, device_id, key) as ws:
        open_camera(ws)
        event_id = fall_and_get_voice_check(ws)
        assert upload(main, event_id, "음 뭐라고", key=key, attempt=1).json()["accepted"] is True
        recv_until(ws, is_voice_check(2))
        # 앞 회차 번호로 늦게 온 녹음은 받지 않는다.
        assert upload(main, event_id, "괜찮아요", key=key, attempt=1).json()["accepted"] is False
        assert upload(main, event_id, "안 괜찮아요", key=key, attempt=2).json()["accepted"] is True
        recv_until(ws, is_state("ALARM"))
    assert event_row(app, event_id)["result"] == ALERT_HELP


def test_button_clear_during_suspect_ignores_late_voice(family):
    app, main, key, device_id = family["app"], family["main"], family["key"], family["device_id"]
    with camera(main, device_id, key) as ws:
        open_camera(ws)
        event_id = fall_and_get_voice_check(ws)

        # 현장 화면의 '괜찮아요' 버튼(기기 키로 인증)
        res = main.post(f"/api/events/{event_id}/clear", headers={"X-Device-Key": key})
        assert res.status_code == 200, res.text
        recv_until(ws, is_state("NORMAL"))

        assert upload(main, event_id, "도와주세요", key=key).json() == {"accepted": False}
        time.sleep(app.state.settings.voice_reply_timeout_seconds + 0.2)  # 남은 대기 시간이 지나도

    row = event_row(app, event_id)
    assert row["result"] == CLEARED_BUTTON
    assert row["cleared_by"] == f"device:{device_id}"
    assert notifications(app, event_id) == []


class GatedTranscriber(FakeTranscriber):
    """인식이 오래 걸리는 상황: gate가 열릴 때까지 인식을 끝내지 않는다."""

    def __init__(self):
        self.started = threading.Event()
        self.gate = threading.Event()

    def transcribe(self, audio_bytes, filename):
        self.started.set()
        self.gate.wait(5)
        return super().transcribe(audio_bytes, filename)


def test_clear_while_transcribing_never_raises_alarm(settings):
    transcriber = GatedTranscriber()
    app, main = start_app(settings, transcriber)
    with main:
        owner = signup(main, "grandma@example.com", "할머니")
        device_id, key = create_device(owner)
        with camera(main, device_id, key) as ws:
            open_camera(ws)
            event_id = fall_and_get_voice_check(ws)
            assert upload(main, event_id, "도와주세요", key=key).json()["accepted"] is True
            assert transcriber.started.wait(2)
            assert owner.post(f"/api/events/{event_id}/clear").status_code == 200
            transcriber.gate.set()  # 해제된 뒤에 '도와주세요' 인식 결과가 나온다
            main.portal.call(app.state.events.background.wait)
            row = event_row(app, event_id)
            assert row["result"] == CLEARED_BUTTON and row["voice_result"] is None
            assert notifications(app, event_id) == []
            assert app.state.events.state_for_device(device_id) is None


def test_device_disconnect_during_voice_check_raises_voice_fail(family):
    app, main, guardian = family["app"], family["main"], family["guardian"]
    device_id, key = family["device_id"], family["key"]
    with guardian.websocket_connect(f"{WS_BASE}/ws/guardian") as gws:
        recv(gws)
        with camera(main, device_id, key) as ws:
            open_camera(ws)
            event_id = fall_and_get_voice_check(ws)
        # 카메라 연결이 끊김 -> 확인할 수 없으므로 바로 경보
        alarm = recv_until(gws, is_event("ALARM", event_id), timeout=2.0)
        assert alarm["event"]["result"] == ALERT_VOICE_FAIL
        status = app.state.devices.status(device_id)
        assert status["online"] is False and status["state"] == "ALARM"
    assert event_row(app, event_id)["voice_result"] == "DEVICE_FAIL"


@pytest.mark.parametrize("kwargs", [
    {"error": "mic_denied"},          # 기기가 녹음 실패를 알림
    {"audio": b"\xff\xfe\x00"},       # 인식 실패(가짜 인식기는 UTF-8이 아니면 실패)
])
def test_recording_or_transcription_failure_raises_voice_fail(family, kwargs):
    app, main, key, device_id = family["app"], family["main"], family["key"], family["device_id"]
    with camera(main, device_id, key) as ws:
        open_camera(ws)
        event_id = fall_and_get_voice_check(ws)
        assert upload(main, event_id, key=key, **kwargs).json()["accepted"] is True
        recv_until(ws, is_state("ALARM"))
    assert event_row(app, event_id)["result"] == ALERT_VOICE_FAIL


def test_phone_device_uses_session_for_camera_and_voice(server):
    app, main = server
    owner = signup(main, "grandma@example.com", "할머니")
    device_id, key = create_device(owner, kind="phone", name="할머니 휴대폰")
    assert key is None
    with camera(owner, device_id, headers={"origin": BASE}) as ws:
        open_camera(ws)
        event_id = fall_and_get_voice_check(ws)
        res = owner.post(f"/api/events/{event_id}/voice",
                         files={"audio": ("reply.webm", "괜찮아".encode(), "audio/webm")})
        assert res.json() == {"accepted": True}
        recv_until(ws, is_state("NORMAL"))
    assert event_row(app, event_id)["result"] == CLEARED_VOICE


# ---------------------------------------------------------------------------
# 권한
# ---------------------------------------------------------------------------

def test_unrelated_and_pending_users_cannot_see_or_clear(family):
    app, main, owner = family["app"], family["main"], family["owner"]
    device_id, key = family["device_id"], family["key"]
    stranger = signup(main, "stranger@example.com", "남")
    pending = signup(main, "pending@example.com", "대기")
    assert owner.post("/api/guardians", json={"email": "pending@example.com"}).status_code == 201  # 수락 안 함

    with stranger.websocket_connect(f"{WS_BASE}/ws/guardian") as sws, \
            pending.websocket_connect(f"{WS_BASE}/ws/guardian") as pws:
        for ws in (sws, pws):
            snapshot = recv(ws)
            assert snapshot["devices"] == [] and snapshot["active_events"] == []

        with camera(main, device_id, key) as ws:
            open_camera(ws)
            event_id = fall_and_get_voice_check(ws)
            upload(main, event_id, "도와주세요", key=key)
            recv_until(ws, is_state("ALARM"))

            for user in (stranger, pending):
                assert user.get("/api/events").json() == {"events": []}
                assert user.post(f"/api/events/{event_id}/clear").status_code == 403
                assert user.get(f"/api/devices/{device_id}/state").status_code == 403
                assert upload(user, event_id, "괜찮아요").status_code == 403
            # 다른 기기의 키로는 해제할 수 없다
            _, other_key = create_device(owner, name="안방 Pi")
            assert main.post(f"/api/events/{event_id}/clear", headers={"X-Device-Key": other_key}).status_code == 401
            # 로그인도 기기 키도 없으면 401
            assert main.post(f"/api/events/{event_id}/clear").status_code == 401
            assert app.state.events.state_for_device(device_id) == "ALARM"

        # 어떤 메시지도 받지 않았어야 한다
        for ws in (sws, pws):
            with pytest.raises(TimeoutError):
                recv(ws, timeout=0.3)

    assert owner.get("/api/events").json()["events"][0]["id"] == event_id
    assert main.get("/api/events").status_code == 401


def test_camera_socket_authentication(server):
    app, main = server
    owner = signup(main, "grandma@example.com", "할머니")
    other = signup(main, "other@example.com", "남")
    pi_id, key = create_device(owner)
    phone_id, _ = create_device(owner, kind="phone", name="휴대폰")

    def close_code(client, url, **kwargs):
        with client.websocket_connect(url, **kwargs) as ws:
            with pytest.raises(WebSocketDisconnect) as exc:
                recv(ws)
            return exc.value.code

    assert close_code(main, f"{WS_BASE}/ws/camera") == 4400
    assert close_code(main, f"{WS_BASE}/ws/camera?device_id=999&key={key}") == 4401
    assert close_code(main, f"{WS_BASE}/ws/camera?device_id={pi_id}&key=wrong") == 4401
    assert close_code(main, f"{WS_BASE}/ws/camera?device_id={pi_id}") == 4401
    assert close_code(main, f"{WS_BASE}/ws/camera?device_id={phone_id}") == 4401
    assert close_code(other, f"{WS_BASE}/ws/camera?device_id={phone_id}") == 4403
    assert close_code(owner, f"{WS_BASE}/ws/camera?device_id={phone_id}",
                      headers={"origin": "https://evil.example"}) == 4403
    assert close_code(main, f"{WS_BASE}/ws/guardian") == 4401
    assert close_code(owner, f"{WS_BASE}/ws/guardian", headers={"origin": "https://evil.example"}) == 4403

    # 키는 헤더로도 보낼 수 있다(Pi 클라이언트)
    with main.websocket_connect(f"{WS_BASE}/ws/camera?device_id={pi_id}", headers={"x-device-key": key}) as ws:
        assert recv(ws)["type"] == "hello"


def test_new_camera_connection_replaces_old(family):
    app, main, key, device_id = family["app"], family["main"], family["key"], family["device_id"]
    with camera(main, device_id, key) as first:
        open_camera(first)
        with camera(main, device_id, key) as second:
            open_camera(second)
            with pytest.raises(WebSocketDisconnect) as exc:
                recv(first)
            assert exc.value.code == 4409
            # 이전 연결이 끊겨도 새 연결은 그대로 접속 상태
            assert app.state.devices.is_online(device_id)
            event_id = fall_and_get_voice_check(second)
            assert event_id
        wait_until(lambda: not app.state.devices.is_online(device_id))


def test_device_state_endpoint_for_kiosk_owner_and_guardian(family):
    app, main, owner, guardian = family["app"], family["main"], family["owner"], family["guardian"]
    device_id, key = family["device_id"], family["key"]

    res = main.get(f"/api/devices/{device_id}/state?key={key}")
    assert res.status_code == 200
    assert res.json()["device"]["online"] is False and res.json()["active_event"] is None
    assert main.get(f"/api/devices/{device_id}/state").status_code == 401

    with camera(main, device_id, key) as ws:
        open_camera(ws)
        ws.send_bytes(fake_frame("standing"))
        wait_until(lambda: main.get(f"/api/devices/{device_id}/state?key={key}").json()["device"]["camera_ok"])
        status = owner.get(f"/api/devices/{device_id}/state").json()["device"]
        assert status["online"] and status["person_detected"] and status["state"] == "NORMAL"

        event_id = fall_and_get_voice_check(ws)
        body = guardian.get(f"/api/devices/{device_id}/state").json()
        assert body["active_event"]["id"] == event_id and body["active_event"]["state"] == "SUSPECT"
        assert body["device"]["state"] == "SUSPECT"


def test_camera_stale_is_reported(family):
    app, main, guardian = family["app"], family["main"], family["guardian"]
    device_id, key = family["device_id"], family["key"]
    with guardian.websocket_connect(f"{WS_BASE}/ws/guardian") as gws, camera(main, device_id, key) as ws:
        recv(gws)
        open_camera(ws)
        ws.send_bytes(fake_frame("standing"))
        recv_until(gws, lambda m: m["type"] == "device" and m["device"]["camera_ok"])
        # 프레임이 끊기면(camera_stale_seconds=1) 점검 루프가 '카메라 응답 없음'을 알린다.
        stale = recv_until(gws, lambda m: m["type"] == "device" and not m["device"]["camera_ok"], timeout=4)
        assert stale["device"]["online"] is True


# ---------------------------------------------------------------------------
# 서버 재시작
# ---------------------------------------------------------------------------

def test_restart_keeps_alarm_and_interrupts_voice_check(settings):
    app, main = start_app(settings)
    with main:
        owner = signup(main, "grandma@example.com", "할머니")
        guardian = signup(main, "son@example.com", "아들")
        link_guardian(owner, guardian)
        device_id, key = create_device(owner)
        other_id, _ = create_device(owner, name="안방 Pi")
        with camera(main, device_id, key) as ws:
            open_camera(ws)
            alarm_id = fall_and_get_voice_check(ws)
            upload(main, alarm_id, "살려줘", key=key)
            recv_until(ws, is_state("ALARM"))
        # 다른 기기는 음성 확인 도중 서버가 꺼진 상황
        cur = app.state.db.execute("INSERT INTO events (device_id, started_at) VALUES (?, ?)", (other_id, now_iso()))
        in_progress_id = cur.lastrowid
        # 세션은 DB에 있으므로 재시작 뒤에도 같은 쿠키로 로그인 상태가 유지된다.
        owner_cookies, guardian_cookies = owner.cookies, guardian.cookies

    app2, main2 = start_app(settings)
    with main2:
        assert event_row(app2, in_progress_id)["result"] == INTERRUPTED
        assert event_row(app2, in_progress_id)["ended_at"] is not None
        assert event_row(app2, alarm_id)["ended_at"] is None

        guardian2 = client_for(main2)
        guardian2.cookies = guardian_cookies
        with guardian2.websocket_connect(f"{WS_BASE}/ws/guardian") as gws:
            snapshot = recv(gws)
            assert [e["id"] for e in snapshot["active_events"]] == [alarm_id]
            assert snapshot["active_events"][0]["state"] == "ALARM"
            assert {d["device_id"]: d["state"] for d in snapshot["devices"]}[device_id] == "ALARM"

            # 다시 접속한 기기도 경보 상태를 받는다
            with camera(main2, device_id, key) as ws:
                assert open_camera(ws)[1]["state"] == "ALARM"
                assert guardian2.post(f"/api/events/{alarm_id}/clear").status_code == 200
                recv_until(ws, is_state("NORMAL"))
            recv_until(gws, is_event("ENDED", alarm_id))
        owner2 = client_for(main2)
        owner2.cookies = owner_cookies
        states = {e["id"]: e["state"] for e in owner2.get("/api/events").json()["events"]}
        assert states == {alarm_id: "ENDED", in_progress_id: "ENDED"}


# ---------------------------------------------------------------------------
# 웹 푸시까지 이어지는지
# ---------------------------------------------------------------------------

def test_alarm_sends_web_push_to_subscribed_guardian(tmp_path, monkeypatch):
    sent = []

    def fake_webpush(subscription_info, data, **kwargs):
        sent.append((subscription_info["endpoint"], json.loads(data)))

    monkeypatch.setattr(notify_mod, "webpush", fake_webpush)
    settings = make_settings(tmp_path, vapid_private_key="priv", vapid_public_key="pub")
    app, main = start_app(settings)
    with main:
        owner = signup(main, "grandma@example.com", "할머니")
        guardian = signup(main, "son@example.com", "아들")
        link_guardian(owner, guardian)
        device_id, key = create_device(owner)
        guardian.post("/api/push/subscribe",
                      json={"endpoint": "https://fcm.googleapis.com/fcm/send/abc", "keys": {"p256dh": "p", "auth": "a"}})
        with camera(main, device_id, key) as ws:
            open_camera(ws)
            event_id = fall_and_get_voice_check(ws)
            # SUSPECT 단계에서는 푸시를 보내지 않는다
            assert sent == []
            upload(main, event_id, "도와주세요", key=key)
            recv_until(ws, is_state("ALARM"))
            wait_until(lambda: len(sent) == 1)
        endpoint, payload = sent[0]
        assert endpoint == "https://fcm.googleapis.com/fcm/send/abc"
        assert payload["kind"] == "alert" and payload["event_id"] == event_id
        assert "할머니" in payload["title"] and payload["url"] == "/guardian"
        push_rows = [r for r in notifications(app, event_id) if r["channel"] == "push"]
        assert [r["state"] for r in push_rows] == ["sent"]


# ---------------------------------------------------------------------------
# 웹페이지 제공
# ---------------------------------------------------------------------------

def test_web_pages_service_worker_and_static(tmp_path):
    web = tmp_path / "web"
    (web / "static").mkdir(parents=True)
    (web / "index.html").write_text("<h1>home</h1>", encoding="utf-8")
    (web / "camera.html").write_text("<h1>camera</h1>", encoding="utf-8")
    (web / "kiosk.html").write_text("<h1>kiosk</h1>", encoding="utf-8")
    (web / "sw.js").write_text("self.addEventListener('push', () => {});", encoding="utf-8")
    (web / "static" / "app.js").write_text("console.log(1);", encoding="utf-8")

    app, main = start_app(make_settings(tmp_path))
    with main:
        assert main.get("/").text == "<h1>home</h1>"
        assert main.get("/kiosk").status_code == 200  # 현장 화면은 로그인 없이 연다
        res = main.get("/camera", follow_redirects=False)
        assert res.status_code == 303 and res.headers["location"] == "/login?next=/camera"
        user = signup(main, "grandma@example.com", "할머니")
        assert user.get("/camera").text == "<h1>camera</h1>"
        assert main.get("/guardian").status_code == 404  # 아직 만들지 않은 페이지
        sw = main.get("/sw.js")
        assert sw.status_code == 200 and sw.headers["service-worker-allowed"] == "/"
        assert main.get("/static/app.js").text == "console.log(1);"


def test_app_without_web_dir_still_serves_api(server):
    app, main = server
    assert main.get("/").status_code == 404
    assert main.get("/sw.js").status_code == 404
    assert main.get("/api/push/vapid-public-key").json() == {"key": None, "enabled": False}


# ---------------------------------------------------------------------------
# DeviceHub 단위 시험 (가짜 시계로 시간을 직접 진행)
# ---------------------------------------------------------------------------

class FakeSocket:
    def __init__(self):
        self.messages = []
        self.closed_with = None

    async def send_json(self, message):
        self.messages.append(message)

    async def close(self, code=1000, reason=None):
        self.closed_with = code


class FakeEvents:
    def __init__(self):
        self.suspects = []

    def state_for_device(self, device_id):
        return None

    def on_device_disconnected(self, device_id):
        pass

    async def on_suspect(self, session, features):
        self.suspects.append(dict(features))


class GatedPose(PoseEstimator):
    def __init__(self):
        self.gate = threading.Event()
        self.inner = FakePoseEstimator()

    def estimate(self, frame_bytes):
        self.gate.wait(5)
        return self.inner.estimate(frame_bytes)


@pytest.fixture
def hub_parts(tmp_path):
    settings = make_settings(tmp_path)
    db = Database(settings.db_path)
    accounts = Accounts(db, settings)
    owner_id = accounts.create_user("grandma@example.com", PASSWORD, "할머니")
    guardian_id = accounts.create_user("son@example.com", PASSWORD, "아들")
    link = accounts.request_guardian(owner_id, "son@example.com")
    accounts.accept_guardian(link["link_id"], guardian_id)
    device, _ = accounts.create_device(owner_id, "거실 Pi", "pi")
    yield settings, accounts, device, guardian_id
    db.close()


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_hub_detects_fall_once_with_real_timestamps(hub_parts):
    settings, accounts, device, _ = hub_parts
    clock, events = Clock(), FakeEvents()

    async def scenario():
        hub = DeviceHub(settings, accounts, GuardianHub(), FakePoseEstimator(), clock=clock)
        hub.events = events
        session = await hub.connect(device, FakeSocket())

        async def frame(kind, offset=0.0, dt=0.1):
            clock.now += dt
            assert await hub.handle_frame(session, fake_frame(kind, offset))

        for _ in range(4):
            await frame("standing")
        await frame("lying", 100)                 # 0.1초 만에 몸통 길이 2배 하강
        assert session.fsm.state.value == "FALLING"
        await frame("lying", 100, dt=0.2)
        assert events.suspects == []              # 아직 T1(0.3초) 전
        await frame("lying", 100, dt=0.15)
        assert session.fsm.state.value == "SUSPECT"
        await frame("lying", 100)
        assert len(events.suspects) == 1          # SUSPECT로 바뀌는 순간 한 번만
        assert events.suspects[0]["torso_angle"] >= 60

        hub.reset_detection(device["id"])
        assert session.fsm.state.value == "NORMAL"
        await hub.stop()

    asyncio.run(scenario())


def test_hub_drops_frames_while_busy(hub_parts):
    settings, accounts, device, _ = hub_parts
    pose = GatedPose()

    async def scenario():
        hub = DeviceHub(settings, accounts, GuardianHub(), pose)
        session = await hub.connect(device, FakeSocket())
        first = asyncio.create_task(hub.handle_frame(session, fake_frame("standing")))
        await asyncio.sleep(0.05)
        assert session.busy
        assert hub.submit_frame(session, fake_frame("standing")) is False
        assert await hub.handle_frame(session, fake_frame("standing")) is False
        pose.gate.set()
        assert await first is True
        assert (session.frames_processed, session.frames_dropped) == (1, 2)
        assert session.person_detected and not session.busy
        await hub.stop()

    asyncio.run(scenario())


def test_hub_status_and_staleness_broadcast(hub_parts):
    settings, accounts, device, guardian_id = hub_parts
    clock = Clock()
    guardian_ws = FakeSocket()

    async def scenario():
        ghub = GuardianHub()
        ghub.register(guardian_id, guardian_ws)
        hub = DeviceHub(settings, accounts, ghub, FakePoseEstimator(), clock=clock)
        ws = FakeSocket()
        session = await hub.connect(device, ws)
        assert guardian_ws.messages[-1]["device"]["online"] is True
        assert guardian_ws.messages[-1]["device"]["camera_ok"] is False

        for _ in range(5):
            clock.now += 0.1
            await hub.handle_frame(session, fake_frame("standing"))
        await hub.background.wait()
        status = hub.status(device["id"])
        assert status["camera_ok"] and status["person_detected"]
        assert status["fps"] == pytest.approx(10.0)
        assert guardian_ws.messages[-1]["device"]["camera_ok"] is True

        clock.now += settings.camera_stale_seconds + 0.5
        await hub.check_all()
        last = guardian_ws.messages[-1]["device"]
        assert last["camera_ok"] is False and last["person_detected"] is False and last["fps"] == 0.0
        count = len(guardian_ws.messages)
        await hub.check_all()
        assert len(guardian_ws.messages) == count  # 바뀐 것이 없으면 보내지 않는다

        assert await hub.send(device["id"], {"type": "ping"}) is True
        assert ws.messages[-1] == {"type": "ping"}
        await hub.disconnect(session)
        assert guardian_ws.messages[-1]["device"]["online"] is False
        assert await hub.send(device["id"], {"type": "ping"}) is False
        assert accounts.get_device(device["id"])["last_seen_at"] is not None
        await hub.stop()

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# 리뷰에서 찾은 문제(회귀 시험)
# ---------------------------------------------------------------------------

class RecordingPush:
    """푸시 종류(alert/clear)를 보낸 순서대로 기록하는 가짜 PushSender."""

    enabled = True

    def __init__(self):
        self.kinds = []

    def subscribed_user_ids(self, user_ids):
        return list(user_ids)

    async def send_to_users(self, user_ids, payload):
        self.kinds.append(payload["kind"])
        return {uid: True for uid in user_ids}


class SlowAlarmDevices:
    """기기로 ALARM 상태를 보내는 데 시간이 걸리는 상황(약한 Wi-Fi)을 흉내 낸다."""

    async def send(self, device_id, message):
        if message.get("state") == "ALARM":
            await asyncio.sleep(0.3)
        return True

    def is_online(self, device_id):
        return True

    def reset_detection(self, device_id):
        pass

    async def broadcast_status(self, device_id):
        pass


def test_clear_while_alarm_is_being_raised_sends_no_late_alert(hub_parts):
    """경보로 바뀌어 기기에 알리는 사이 '괜찮아요'를 누르면, 경보 알림이 해제 알림 뒤에 가면 안 된다."""
    settings, accounts, device, _ = hub_parts
    settings.voice_max_attempts = 1
    settings.voice_reply_timeout_seconds = 0.1

    async def scenario():
        hub, push = GuardianHub(), RecordingPush()
        notifier = Notifier(accounts.db, hub, push, settings)
        events = EventService(accounts.db, accounts, SlowAlarmDevices(), hub, notifier, FakeTranscriber(), settings)
        event_id = await events.on_suspect(types.SimpleNamespace(device=device), {})
        while events.state_for_device(device["id"]) != "ALARM":   # 무응답 -> 경보로 넘어가는 중
            await asyncio.sleep(0.01)
        assert await events.clear(event_id, ("device", device))
        await events.background.wait()
        return push.kinds, notifier

    kinds, notifier = asyncio.run(scenario())
    assert kinds == ["clear"]


def test_reconnect_during_voice_check_resends_request_instead_of_alarm(tmp_path):
    """화면 새로 고침처럼 끊겼다 곧 다시 접속하면 음성 확인을 이어 간다(바로 '확인 불가' 경보가 아님)."""
    settings = make_settings(tmp_path, device_reconnect_grace_seconds=2.0, voice_reply_timeout_seconds=3.0)
    app, main = start_app(settings)
    with main:
        owner = signup(main, "grandma@example.com", "할머니")
        device_id, key = create_device(owner)
        with camera(main, device_id, key) as ws:
            open_camera(ws)
            event_id = fall_and_get_voice_check(ws)
        # 이전 연결이 먼저 닫히고 다시 접속한다.
        with camera(main, device_id, key) as ws:
            _, state = open_camera(ws)
            assert state["state"] == "SUSPECT"
            check = recv_until(ws, is_voice_check(1))
            assert check["event_id"] == event_id
            assert upload(main, event_id, "괜찮아요", key=key, attempt=1).json() == {"accepted": True}
            recv_until(ws, is_state("NORMAL"))
        assert event_row(app, event_id)["result"] == CLEARED_VOICE


def test_restart_resends_alarm_that_never_reached_guardians(tmp_path, monkeypatch):
    """경보로 바꾼 직후 서버가 죽었거나 푸시 재전송이 남은 채 재시작해도 보호자에게 알린다."""
    sent = []
    monkeypatch.setattr(notify_mod, "webpush", lambda subscription_info, data, **kw: sent.append(json.loads(data)))
    settings = make_settings(tmp_path, vapid_private_key="priv", vapid_public_key="pub")
    app, main = start_app(settings)
    with main:
        owner = signup(main, "grandma@example.com", "할머니")
        guardian = signup(main, "son@example.com", "아들")
        link_guardian(owner, guardian)
        guardian.post("/api/push/subscribe",
                      json={"endpoint": "https://fcm.googleapis.com/fcm/send/abc", "keys": {"p256dh": "p", "auth": "a"}})
        living_id, _ = create_device(owner)
        bedroom_id, _ = create_device(owner, name="안방 Pi")
        db = app.state.db
        # 거실: 경보로 바꾸었지만 알림을 하나도 보내지 못함. 안방: 경보가 이미 푸시로 전달됨.
        lost_id = db.execute("INSERT INTO events (device_id, started_at, result) VALUES (?, ?, ?)",
                             (living_id, now_iso(), ALERT_NO_REPLY)).lastrowid
        done_id = db.execute("INSERT INTO events (device_id, started_at, result) VALUES (?, ?, ?)",
                             (bedroom_id, now_iso(), ALERT_HELP)).lastrowid
        db.execute("INSERT INTO notifications (event_id, guardian_id, kind, channel, state, created_at) "
                   "VALUES (?, ?, 'alert', 'push', 'sent', ?)", (done_id, guardian.user["id"], now_iso()))

    app2, main2 = start_app(settings)
    with main2:
        wait_until(lambda: len(sent) == 1)
        main2.portal.call(app2.state.events.background.wait)
        assert [p["event_id"] for p in sent] == [lost_id]      # 이미 전달된 경보는 다시 보내지 않는다
        assert sent[0]["kind"] == "alert" and "지연 전송" in sent[0]["title"]
        assert [r["state"] for r in notifications(app2, lost_id, "alert") if r["channel"] == "push"] == ["sent"]


def test_deleting_device_disconnects_it_and_drops_its_alarm(family):
    """기기를 지우면(키 유출 시 무효화 방법) 연결이 바로 끊기고, 지울 수 없는 경보가 남지 않는다."""
    app, main, owner = family["app"], family["main"], family["owner"]
    device_id, key = family["device_id"], family["key"]
    with camera(main, device_id, key) as ws:
        open_camera(ws)
        event_id = fall_and_get_voice_check(ws)
        upload(main, event_id, "도와주세요", key=key)
        recv_until(ws, is_state("ALARM"))

        assert owner.delete(f"/api/devices/{device_id}").status_code == 200
        with pytest.raises(WebSocketDisconnect) as closed:
            recv_until(ws, lambda m: False)
        assert closed.value.code == 4401
    assert app.state.events.state_for_device(device_id) is None
    assert app.state.events.active_events_visible_to(owner.user) == []
    assert device_id not in app.state.devices.sessions


def test_deleting_device_during_voice_check_stops_the_check(family):
    app, main, owner = family["app"], family["main"], family["owner"]
    device_id, key = family["device_id"], family["key"]
    with camera(main, device_id, key) as ws:
        open_camera(ws)
        fall_and_get_voice_check(ws)
        assert owner.delete(f"/api/devices/{device_id}").status_code == 200
    main.portal.call(app.state.events.background.wait)   # 음성 확인 작업이 기다림에서 풀려 끝난다
    assert app.state.events.state_for_device(device_id) is None
    assert app.state.db.query("SELECT * FROM notifications") == []


def _multipart(field_bytes: bytes, boundary: str = "testboundary") -> bytes:
    return (f"--{boundary}\r\nContent-Disposition: form-data; name=\"audio\"; filename=\"a.wav\"\r\n"
            f"Content-Type: audio/wav\r\n\r\n").encode() + field_bytes + f"\r\n--{boundary}--\r\n".encode()


def test_voice_upload_checks_event_and_caller_before_reading_body(family, monkeypatch):
    """로그인도 기기 키도 없는 요청이 큰 파일을 서버 디스크에 올려 두지 못하게 한다."""
    from starlette.requests import Request as StarletteRequest

    parsed = []
    original_form = StarletteRequest.form

    def spying_form(self, *args, **kwargs):
        parsed.append(self.url.path)
        return original_form(self, *args, **kwargs)

    monkeypatch.setattr(StarletteRequest, "form", spying_form)
    app, main, device_id = family["app"], family["main"], family["device_id"]
    event_id = app.state.db.execute("INSERT INTO events (device_id, started_at) VALUES (?, ?)",
                                    (device_id, now_iso())).lastrowid
    files = {"audio": ("a.wav", b"x" * 5000, "audio/wav")}

    assert main.post("/api/events/999/voice", files=files).status_code == 404
    assert main.post(f"/api/events/{event_id}/voice", files=files).status_code == 401
    assert main.post(f"/api/events/{event_id}/voice", files=files,
                     headers={"X-Device-Key": "wrong"}).status_code == 401
    assert parsed == []                          # 거절한 요청은 본문을 읽지 않았다

    res = main.post(f"/api/events/{event_id}/voice", files=files, headers={"X-Device-Key": family["key"]})
    assert res.status_code == 200 and res.json() == {"accepted": False}   # 기다리는 확인이 없는 이벤트
    assert len(parsed) == 1


def test_voice_upload_size_limit_without_trusting_content_length(family, monkeypatch):
    monkeypatch.setattr(routes_realtime, "MAX_AUDIO_BYTES", 1500)
    monkeypatch.setattr(routes_realtime, "MAX_VOICE_BODY_BYTES", 2000)
    app, main, device_id, key = family["app"], family["main"], family["device_id"], family["key"]
    event_id = app.state.db.execute("INSERT INTO events (device_id, started_at) VALUES (?, ?)",
                                    (device_id, now_iso())).lastrowid
    url = f"/api/events/{event_id}/voice"
    headers = {"X-Device-Key": key, "Content-Type": "multipart/form-data; boundary=testboundary"}

    # Content-Length가 상한을 넘으면 읽지 않고 거절
    assert main.post(url, content=_multipart(b"x" * 5000), headers=headers).status_code == 413
    # Content-Length 없이(chunked) 보내도 받은 양이 상한을 넘으면 거절
    body = _multipart(b"x" * 5000)
    chunks = (body[i:i + 500] for i in range(0, len(body), 500))
    assert main.post(url, content=chunks, headers=headers).status_code == 413
    # 본문은 상한 안이지만 녹음 파일 자체가 상한을 넘는 경우
    assert main.post(url, content=_multipart(b"x" * 1600), headers=headers).status_code == 413
    # 상한 안의 녹음은 받는다
    assert main.post(url, content=_multipart(b"x" * 100), headers=headers).status_code == 200
    # multipart가 아니면 400
    assert main.post(url, content=b"hello", headers={"X-Device-Key": key}).status_code == 400


def test_voice_upload_rejects_bad_attempt(family):
    app, main, device_id, key = family["app"], family["main"], family["device_id"], family["key"]
    event_id = app.state.db.execute("INSERT INTO events (device_id, started_at) VALUES (?, ?)",
                                    (device_id, now_iso())).lastrowid
    res = upload(main, event_id, "괜찮아요", key=key, attempt="first")
    assert res.status_code == 400


def test_logout_closes_open_guardian_screen(family):
    app, guardian, owner = family["app"], family["guardian"], family["owner"]
    with guardian.websocket_connect(f"{WS_BASE}/ws/guardian") as gws, \
            owner.websocket_connect(f"{WS_BASE}/ws/guardian") as ows:
        recv(gws), recv(ows)
        assert guardian.post("/api/logout").status_code == 200
        with pytest.raises(WebSocketDisconnect) as closed:
            recv(gws)
        assert closed.value.code == 4401         # 다시 접속하지 말고 로그인 화면으로
        assert app.state.guardian_hub.connected_user_ids() == [owner.user["id"]]


def test_guardian_screen_closed_when_session_is_gone(family, monkeypatch):
    """세션이 만료·삭제되면 이미 열린 보호자 화면도 더는 상태를 받지 못한다."""
    monkeypatch.setattr(routes_realtime, "SESSION_RECHECK_SECONDS", 0.1)
    app, guardian = family["app"], family["guardian"]
    with guardian.websocket_connect(f"{WS_BASE}/ws/guardian") as gws:
        recv(gws)
        app.state.db.execute("DELETE FROM sessions WHERE user_id = ?", (guardian.user["id"],))
        with pytest.raises(WebSocketDisconnect) as closed:
            recv_until(gws, lambda m: False)
        assert closed.value.code == 4401


def test_access_log_hides_device_key():
    """uvicorn 접속 로그에 ?key=...의 기기 키 원문이 남지 않는다."""
    import uvicorn
    from fastapi import FastAPI

    from server.__main__ import HideDeviceKeys, install_log_filters

    access = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
                               ("10.0.0.5:5000", "GET", "/api/devices/3/state?key=SECRET&x=1", "1.1", 200), None)
    websocket = logging.LogRecord("uvicorn.error", logging.INFO, __file__, 1, '%s - "WebSocket %s" [accepted]',
                                  ("10.0.0.5:5000", "/ws/camera?device_id=3&key=SECRET"), None)
    for record in (access, websocket):
        assert HideDeviceKeys().filter(record)
        assert "SECRET" not in record.getMessage()
    assert "/api/devices/3/state?key=***&x=1" in access.getMessage()
    assert "/ws/camera?device_id=3&key=***" in websocket.getMessage()

    # uvicorn이 실행하며 로그 설정을 다시 해도 필터가 남아 있어야 한다.
    loggers = [logging.getLogger(n) for n in ("uvicorn.access", "uvicorn.error")]
    try:
        install_log_filters()
        uvicorn.Config(FastAPI(), log_level="info")
        for logger in loggers:
            assert any(isinstance(f, HideDeviceKeys) for f in logger.filters)
    finally:
        for logger in loggers:
            for f in [f for f in logger.filters if isinstance(f, HideDeviceKeys)]:
                logger.removeFilter(f)
