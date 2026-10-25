"""음성 확인(분류·인식기)과 보호자 알림(WebSocket 허브·웹 푸시·전달 기록) 테스트.

계정 모듈(server/auth.py)에 기대지 않도록 푸시 구독은 작은 가짜 accounts 객체로 대신한다.
pytest-asyncio 없이 asyncio.run()으로 비동기 코드를 돌린다.
"""

import asyncio
import json
import sys
import types
from pathlib import Path

import pytest
from pywebpush import WebPushException

from server import notify as notify_mod
from server.config import Settings
from server.db import ALERT_HELP, CLEARED_VOICE, Database, now_iso
from server.notify import (
    GuardianHub,
    Notifier,
    PushSender,
    event_public_dict,
    event_state,
    format_time,
)
from server.voice import (
    DEVICE_FAIL,
    HELP,
    NO_REPLY,
    SAFE,
    UNCLEAR,
    FakeTranscriber,
    TranscriberError,
    WhisperTranscriber,
    classify,
    classify_with_settings,
    create_transcriber,
)


def make_settings(tmp_path, **overrides):
    values = dict(data_dir=tmp_path, vapid_private_key="test-private", vapid_public_key="test-public",
                  vapid_subject="mailto:test@example.com", push_retry_delays=[0.01, 0.01])
    values.update(overrides)
    return Settings(**values)


# ======================================================================== 음성 분류

@pytest.fixture
def c(tmp_path):
    settings = Settings(data_dir=tmp_path)
    return lambda text: classify_with_settings(text, settings)


def test_classify_safe(c):
    for text in ["괜찮아요", "네 괜찮아", "괜 찮 아 요", "일어났어요"]:
        assert c(text) == SAFE, text


def test_classify_help(c):
    for text in ["도와주세요", "살려줘", "아파요", "구급차 불러줘"]:
        assert c(text) == HELP, text


def test_classify_negation_and_mixed_lean_to_help(c):
    # '괜찮'이 들어 있어도 부정·도움 요청이 함께 있으면 경보 쪽으로 기운다.
    for text in ["안 괜찮아요", "괜찮지 않아", "괜찮은데 아파", "괜찮아 아니 도와줘"]:
        assert c(text) == HELP, text


def test_classify_no_reply_and_unclear(c):
    assert c("") == NO_REPLY
    assert c("   ") == NO_REPLY
    assert c(None) == NO_REPLY
    assert c("음 뭐라고") == UNCLEAR


def test_classify_uses_given_keywords():
    assert classify("ok", negated_safe=[], help_words=["help"], safe_words=["ok"]) == SAFE
    assert classify("help ok", negated_safe=[], help_words=["help"], safe_words=["ok"]) == HELP
    assert DEVICE_FAIL == "DEVICE_FAIL"  # 분류기는 내지 않지만 호출 쪽이 쓰는 결과 값


# ======================================================================== 음성 인식기

def test_fake_transcriber_reads_utf8_text():
    t = FakeTranscriber()
    assert t.transcribe(" 괜찮아요\n".encode("utf-8"), "a.wav") == "괜찮아요"
    assert t.transcribe(b"", "a.wav") == ""


def test_fake_transcriber_bad_bytes_is_transcriber_error():
    with pytest.raises(TranscriberError):
        FakeTranscriber().transcribe(b"\xff\xfe\xfa", "a.wav")


def test_create_transcriber_by_backend(tmp_path):
    assert isinstance(create_transcriber(Settings(data_dir=tmp_path, stt_backend="fake")), FakeTranscriber)
    w = create_transcriber(Settings(data_dir=tmp_path, stt_backend="whisper", whisper_model="tiny"))
    assert isinstance(w, WhisperTranscriber) and w.model_size == "tiny"
    with pytest.raises(ValueError):
        create_transcriber(Settings(data_dir=tmp_path, stt_backend="nope"))


def _install_fake_whisper(monkeypatch, text="괜찮아요", fail_transcribe=False, fail_load=False):
    """faster_whisper 대신 쓰는 가짜 모듈. 호출 기록을 calls에 남긴다."""
    calls = {"loads": 0, "transcribe": []}

    class WhisperModel:
        def __init__(self, size, device=None, compute_type=None):
            if fail_load:
                raise RuntimeError("모델 파일 없음")
            calls["loads"] += 1
            calls["size"] = size

        def transcribe(self, path, language=None):
            p = Path(path)
            # 인식 중에는 임시 파일이 있어야 하고 올린 바이트가 그대로 들어 있어야 한다.
            calls["transcribe"].append({"path": p, "exists": p.exists(), "data": p.read_bytes(),
                                        "language": language})
            if fail_transcribe:
                raise RuntimeError("디코딩 실패")

            def gen():
                # 실제 라이브러리처럼 지연 생성기로 돌려준다.
                yield types.SimpleNamespace(text=" " + text[:2])
                yield types.SimpleNamespace(text=text[2:] + " ")
            return gen(), types.SimpleNamespace(language="ko")

    monkeypatch.setitem(sys.modules, "faster_whisper", types.SimpleNamespace(WhisperModel=WhisperModel))
    return calls


def test_whisper_transcribes_korean_and_deletes_temp_file(monkeypatch):
    calls = _install_fake_whisper(monkeypatch)
    t = WhisperTranscriber("small")
    assert t.transcribe(b"webm-bytes", "reply.webm") == "괜찮아요"
    assert t.transcribe(b"wav-bytes", "reply.wav") == "괜찮아요"

    assert calls["loads"] == 1 and calls["size"] == "small"   # 모델은 한 번만 올린다
    first, second = calls["transcribe"]
    assert first["exists"] and first["data"] == b"webm-bytes" and first["path"].suffix == ".webm"
    assert second["path"].suffix == ".wav"
    assert first["language"] == "ko"
    assert not first["path"].exists() and not second["path"].exists()  # 녹음은 남기지 않는다


def test_whisper_unknown_suffix_falls_back_to_wav(monkeypatch):
    calls = _install_fake_whisper(monkeypatch)
    WhisperTranscriber("small").transcribe(b"x", "upload.exe")
    assert calls["transcribe"][0]["path"].suffix == ".wav"


def test_whisper_failure_is_wrapped_and_temp_file_removed(monkeypatch):
    calls = _install_fake_whisper(monkeypatch, fail_transcribe=True)
    with pytest.raises(TranscriberError):
        WhisperTranscriber("small").transcribe(b"x", "a.wav")
    assert not calls["transcribe"][0]["path"].exists()


def test_whisper_load_failure_and_missing_package(monkeypatch):
    _install_fake_whisper(monkeypatch, fail_load=True)
    with pytest.raises(TranscriberError):
        WhisperTranscriber("small").load()

    monkeypatch.setitem(sys.modules, "faster_whisper", None)  # import 시 ImportError
    with pytest.raises(TranscriberError):
        WhisperTranscriber("small").transcribe(b"x", "a.wav")


# ======================================================================== 이벤트 표시

def _event(**kw):
    row = dict(id=7, device_id=3, started_at="2026-10-25T14:03:12.345", ended_at=None, result=None,
               voice_result=None, voice_text="개인정보", feature_snapshot="{}", cleared_by=None)
    row.update(kw)
    return row


def test_event_state():
    assert event_state(_event()) == "SUSPECT"
    assert event_state(_event(result=ALERT_HELP)) == "ALARM"
    assert event_state(_event(result=ALERT_HELP, ended_at="x", cleared_by="user:1")) == "ENDED"
    assert event_state(_event(result=CLEARED_VOICE, ended_at="x")) == "ENDED"


def test_event_public_dict_hides_voice_text():
    d = event_public_dict(_event(result=ALERT_HELP), {"id": 3, "name": "거실", "owner_id": 1},
                          {"id": 1, "name": "김할머니"})
    assert d == {
        "id": 7, "device_id": 3, "device_name": "거실", "owner_id": 1, "owner_name": "김할머니",
        "started_at": "2026-10-25T14:03:12.345", "ended_at": None, "result": ALERT_HELP,
        "result_label": "음성으로 도움 요청", "voice_result": None, "cleared_by": None, "state": "ALARM",
    }
    # 기기·주인 행이 없어도(삭제 등) 깨지지 않는다.
    d = event_public_dict(_event(), None, None)
    assert d["device_name"] is None and d["owner_id"] is None and d["state"] == "SUSPECT"


def test_format_time():
    assert format_time("2026-10-25T14:03:12.345") == "10월 25일 14:03"
    assert format_time("모름") == "모름"
    assert format_time(None) == ""


# ======================================================================== GuardianHub

class FakeWS:
    def __init__(self, fail=False, delay=0.0):
        self.fail = fail
        self.delay = delay
        self.sent = []

    async def send_json(self, message):
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise RuntimeError("연결 끊김")
        self.sent.append(message)


def test_hub_delivers_to_every_socket_and_drops_broken_ones():
    hub = GuardianHub()
    phone, pc, broken, other = FakeWS(), FakeWS(), FakeWS(fail=True), FakeWS()
    hub.register(1, phone)
    hub.register(1, pc)
    hub.register(1, broken)
    hub.register(2, other)

    delivered = asyncio.run(hub.send_to_users([1, 1, 3], {"type": "event"}))

    assert delivered == [1]                      # 중복 id는 한 번만, 소켓 없는 3은 빠짐
    assert phone.sent == pc.sent == [{"type": "event"}]
    assert other.sent == []                      # 대상이 아닌 사용자는 받지 않는다
    # 끊긴 소켓은 빠지고, 다음 전송 때 다시 시도하지 않는다.
    asyncio.run(hub.send_to_users([1], {"n": 2}))
    assert len(phone.sent) == 2


def test_hub_user_with_only_broken_socket_is_not_delivered_and_removed():
    hub = GuardianHub()
    hub.register(5, FakeWS(fail=True))
    assert asyncio.run(hub.send_to_users([5], {})) == []
    assert hub.connected_user_ids() == []


def test_hub_unregister_and_slow_socket_timeout():
    hub = GuardianHub(send_timeout=0.05)
    ws, slow = FakeWS(), FakeWS(delay=1.0)
    hub.register(1, ws)
    hub.unregister(1, ws)
    hub.unregister(1, ws)        # 두 번 빼도 오류 없음
    assert hub.connected_user_ids() == []

    hub.register(2, slow)
    assert asyncio.run(hub.send_to_users([2], {})) == []   # 멈춘 화면 때문에 오래 기다리지 않는다
    assert hub.connected_user_ids() == []


# ======================================================================== PushSender

class FakeAccounts:
    """PushSender가 쓰는 계정 메서드 두 개만 흉내 낸다."""

    def __init__(self, subs):
        self.subs = [dict(s) for s in subs]
        self.deleted = []

    def push_subscriptions_for(self, user_ids):
        return [dict(s) for s in self.subs if s["user_id"] in user_ids]

    def delete_push_subscription(self, endpoint, user_id=None):
        self.deleted.append(endpoint)
        self.subs = [s for s in self.subs if s["endpoint"] != endpoint]


def _sub(user_id, endpoint):
    return {"user_id": user_id, "endpoint": endpoint, "p256dh": "p-" + endpoint, "auth": "a-" + endpoint}


def install_fake_webpush(monkeypatch, outcomes=None):
    """outcomes: endpoint -> 'ok' | 상태 코드(int) | 'network' | 리스트(호출마다 차례로). 기본은 'ok'."""
    outcomes = dict(outcomes or {})
    calls = []

    def fake_webpush(subscription_info, data, vapid_private_key, vapid_claims, **kw):
        endpoint = subscription_info["endpoint"]
        calls.append({"info": subscription_info, "data": data, "key": vapid_private_key,
                      "claims": dict(vapid_claims), **kw})
        outcome = outcomes.get(endpoint, "ok")
        if isinstance(outcome, list):
            outcome = outcome.pop(0) if len(outcome) > 1 else outcome[0]
        if outcome == "ok":
            return types.SimpleNamespace(status_code=201)
        if outcome == "network":
            raise ConnectionError("인터넷 끊김")
        raise WebPushException("push failed", response=types.SimpleNamespace(status_code=outcome, text=""))

    monkeypatch.setattr(notify_mod, "webpush", fake_webpush)
    return calls


def test_push_success_sends_vapid_and_payload(tmp_path, monkeypatch):
    calls = install_fake_webpush(monkeypatch)
    sender = PushSender(make_settings(tmp_path), FakeAccounts([_sub(1, "e1")]))
    assert sender.enabled

    result = asyncio.run(sender.send_to_users([1, 2], {"title": "경보"}))

    assert result == {1: True, 2: False}         # 2는 구독이 없다
    (call,) = calls
    assert call["info"] == {"endpoint": "e1", "keys": {"p256dh": "p-e1", "auth": "a-e1"}}
    assert call["key"] == "test-private"
    assert call["claims"] == {"sub": "mailto:test@example.com"}
    assert '"title": "경보"' in call["data"]     # 한글이 이스케이프되지 않은 JSON


def test_push_410_and_404_delete_subscription(tmp_path, monkeypatch):
    install_fake_webpush(monkeypatch, {"gone": 410, "missing": 404, "e2": 500})
    accounts = FakeAccounts([_sub(1, "gone"), _sub(2, "missing"), _sub(3, "e2")])
    sender = PushSender(make_settings(tmp_path), accounts)

    result = asyncio.run(sender.send_to_users([1, 2, 3], {}))

    assert result == {1: False, 2: False, 3: False}
    assert sorted(accounts.deleted) == ["gone", "missing"]   # 만료 구독만 지우고
    assert [s["endpoint"] for s in accounts.subs] == ["e2"]  # 일시 오류(500)는 남긴다


def test_push_one_of_several_subscriptions_is_enough(tmp_path, monkeypatch):
    install_fake_webpush(monkeypatch, {"old-phone": "network"})
    sender = PushSender(make_settings(tmp_path), FakeAccounts([_sub(1, "old-phone"), _sub(1, "new-phone")]))
    assert asyncio.run(sender.send_to_users([1], {})) == {1: True}


def test_push_disabled_without_vapid_keys(tmp_path, monkeypatch):
    calls = install_fake_webpush(monkeypatch)
    sender = PushSender(make_settings(tmp_path, vapid_private_key=None), FakeAccounts([_sub(1, "e1")]))
    assert not sender.enabled
    assert asyncio.run(sender.send_to_users([1], {})) == {1: False}
    assert calls == []


# ======================================================================== Notifier

@pytest.fixture
def world(tmp_path):
    """주인 1명, 수락된 보호자 2명, 기기 1대, 경보 중 이벤트 1건이 있는 DB."""
    db = Database(tmp_path / "t.db")
    for uid, name in [(1, "김할머니"), (2, "딸"), (3, "아들")]:
        db.execute("INSERT INTO users (id, email, password_hash, name, created_at) VALUES (?, ?, 'x', ?, ?)",
                   (uid, f"u{uid}@example.com", name, now_iso()))
    db.execute("INSERT INTO devices (id, owner_id, name, kind, created_at) VALUES (1, 1, '거실 카메라', 'pi', ?)",
               (now_iso(),))
    for guardian_id in (2, 3):  # 딸·아들 모두 수락된 보호자
        db.execute("INSERT INTO guardians (user_id, guardian_id, status, created_at) VALUES (1, ?, 'accepted', ?)",
                   (guardian_id, now_iso()))
    db.execute("INSERT INTO events (id, device_id, started_at, result, voice_result, voice_text) "
               "VALUES (1, 1, '2026-10-25T14:03:12.000', ?, 'HELP', '도와주세요')", (ALERT_HELP,))
    yield db
    db.close()


def get_event(db):
    return db.query_one("SELECT * FROM events WHERE id = 1")


def owner(db):
    return db.query_one("SELECT * FROM users WHERE id = 1")


def notifications(db):
    return db.query("SELECT guardian_id, kind, channel, state, detail FROM notifications ORDER BY id")


def make_notifier(world, tmp_path, monkeypatch, subs, outcomes=None, **settings_overrides):
    calls = install_fake_webpush(monkeypatch, outcomes)
    settings = make_settings(tmp_path, **settings_overrides)
    hub = GuardianHub()
    notifier = Notifier(world, hub, PushSender(settings, FakeAccounts(subs)), settings)
    return notifier, hub, calls


def test_notify_alert_sends_ws_and_push_and_records(world, tmp_path, monkeypatch):
    # 딸(2): 보호자 화면 열림 + 푸시 성공. 아들(3): 화면 닫힘 + 푸시 구독 없음.
    notifier, hub, calls = make_notifier(world, tmp_path, monkeypatch, [_sub(2, "e2")], push_retry_delays=[])
    ws = FakeWS()
    hub.register(2, ws)

    summary = asyncio.run(notifier.notify(get_event(world), owner(world), [2, 3], "alert"))

    assert summary == {2: {"websocket": True, "push": True}, 3: {"websocket": False, "push": False}}
    (msg,) = ws.sent
    assert msg["type"] == "event"
    assert msg["event"]["state"] == "ALARM" and msg["event"]["owner_name"] == "김할머니"
    assert msg["event"]["device_name"] == "거실 카메라"
    assert "voice_text" not in msg["event"]

    (call,) = calls
    payload = json.loads(call["data"])
    assert payload["url"] == "/guardian" and payload["event_id"] == 1 and payload["kind"] == "alert"
    assert "김할머니" in payload["title"]
    assert "10월 25일 14:03" in payload["body"] and "음성으로 도움 요청" in payload["body"]
    assert "도와주세요" not in call["data"]      # 인식 문장(음성 내용)은 푸시에 넣지 않는다

    assert notifications(world) == [
        {"guardian_id": 2, "kind": "alert", "channel": "websocket", "state": "sent", "detail": None},
        {"guardian_id": 3, "kind": "alert", "channel": "websocket", "state": "failed", "detail": "열린 보호자 화면 없음"},
        {"guardian_id": 3, "kind": "alert", "channel": "push", "state": "failed", "detail": "푸시 알림을 켜지 않음"},
        {"guardian_id": 2, "kind": "alert", "channel": "push", "state": "sent", "detail": None},
    ]


def test_notify_without_guardians_does_nothing(world, tmp_path, monkeypatch):
    notifier, _, calls = make_notifier(world, tmp_path, monkeypatch, [])
    assert asyncio.run(notifier.notify(get_event(world), owner(world), [], "alert")) == {}
    assert notifications(world) == [] and calls == []


def test_notify_rejects_unknown_kind(world, tmp_path, monkeypatch):
    notifier, _, _ = make_notifier(world, tmp_path, monkeypatch, [])
    with pytest.raises(ValueError):
        asyncio.run(notifier.notify(get_event(world), owner(world), [2], "hello"))


def test_push_disabled_is_recorded_as_failed(world, tmp_path, monkeypatch):
    notifier, _, calls = make_notifier(world, tmp_path, monkeypatch, [_sub(2, "e2")], vapid_private_key=None)
    asyncio.run(notifier.notify(get_event(world), owner(world), [2], "alert"))
    push_rows = [r for r in notifications(world) if r["channel"] == "push"]
    assert push_rows == [{"guardian_id": 2, "kind": "alert", "channel": "push", "state": "failed",
                          "detail": "서버에 웹 푸시가 설정되지 않음"}]
    assert calls == []


def test_failed_push_is_retried_until_delivered(world, tmp_path, monkeypatch):
    notifier, _, calls = make_notifier(world, tmp_path, monkeypatch, [_sub(2, "e2")],
                                       outcomes={"e2": ["network", "network", "ok"]})

    async def run():
        await notifier.notify(get_event(world), owner(world), [2], "alert")
        await notifier.wait_retries()
    asyncio.run(run())

    assert len(calls) == 3                       # 처음 + 재시도 2회
    assert "지연 전송" in json.loads(calls[-1]["data"])["title"]
    push_rows = [(r["state"], r["detail"]) for r in notifications(world) if r["channel"] == "push"]
    assert push_rows == [("failed", "전송 실패"), ("failed", "전송 실패 (재시도 1회차)"),
                         ("sent", "재시도 2회차 성공")]


def test_alert_retry_skipped_after_event_cleared(world, tmp_path, monkeypatch):
    notifier, _, calls = make_notifier(world, tmp_path, monkeypatch, [_sub(2, "e2")],
                                       outcomes={"e2": "network"}, push_retry_delays=[0.05, 0.05])

    async def run():
        await notifier.notify(get_event(world), owner(world), [2], "alert")
        # 재시도를 기다리는 동안 현장에서 경보를 끝낸다.
        world.execute("UPDATE events SET ended_at = ?, cleared_by = 'device:1' WHERE id = 1", (now_iso(),))
        await notifier.wait_retries()
    asyncio.run(run())

    assert len(calls) == 1                       # 해제된 경보는 다시 울리지 않는다
    assert len([r for r in notifications(world) if r["channel"] == "push"]) == 1


def test_clear_notice_is_retried_even_though_event_ended(world, tmp_path, monkeypatch):
    world.execute("UPDATE events SET ended_at = ?, cleared_by = 'user:2' WHERE id = 1", (now_iso(),))
    notifier, _, calls = make_notifier(world, tmp_path, monkeypatch, [_sub(3, "e3")],
                                       outcomes={"e3": ["network", "ok"]})

    async def run():
        await notifier.notify(get_event(world), owner(world), [3], "clear")
        await notifier.wait_retries()
    asyncio.run(run())

    assert len(calls) == 2
    payload = json.loads(calls[0]["data"])
    assert payload["kind"] == "clear" and "경보 해제" in payload["title"]
    assert "딸님이 해제" in payload["body"]       # 누가 끝냈는지 보호자에게 알려 준다


def test_expired_subscription_is_not_retried(world, tmp_path, monkeypatch):
    notifier, _, calls = make_notifier(world, tmp_path, monkeypatch, [_sub(2, "e2")], outcomes={"e2": 410})

    async def run():
        await notifier.notify(get_event(world), owner(world), [2], "alert")
        await notifier.wait_retries()
    asyncio.run(run())

    assert len(calls) == 1                       # 지워진 구독으로는 다시 보내지 않는다
    push_rows = [(r["state"], r["detail"]) for r in notifications(world) if r["channel"] == "push"]
    assert push_rows == [("failed", "전송 실패"), ("failed", "푸시 알림을 켜지 않음 (재시도 1회차)")]


def test_aclose_cancels_pending_retries(world, tmp_path, monkeypatch):
    notifier, _, calls = make_notifier(world, tmp_path, monkeypatch, [_sub(2, "e2")],
                                       outcomes={"e2": "network"}, push_retry_delays=[10.0])

    async def run():
        await notifier.notify(get_event(world), owner(world), [2], "alert")
        await notifier.aclose()
        await notifier.wait_retries()            # 남은 작업이 없으니 바로 끝난다
    asyncio.run(run())
    assert len(calls) == 1


# ======================================================================== 리뷰에서 찾은 문제(회귀 시험)

class ClosableWS(FakeWS):
    """서버가 연결을 닫았는지까지 기록하는 가짜 보호자 화면. slow_once면 첫 전송만 느리다."""

    def __init__(self, slow_once=0.0, **kwargs):
        super().__init__(**kwargs)
        self.slow_once = slow_once
        self.closed_with = None

    async def send_json(self, message):
        if self.slow_once:
            delay, self.slow_once = self.slow_once, 0.0
            await asyncio.sleep(delay)
        await super().send_json(message)

    async def close(self, code=1000, reason=None):
        self.closed_with = code


def test_hub_closes_socket_it_drops_so_screen_reconnects():
    """전송이 한 번 멈춘 화면을 목록에서만 빼면, 화면은 연결된 것처럼 보이면서 다시는 받지 못한다."""
    async def run():
        hub = GuardianHub(send_timeout=0.05)
        ws = ClosableWS(slow_once=0.2)
        hub.register(7, ws)
        assert await hub.send_to_users([7], {"n": 1}) == []
        await hub.wait_closed()
        return hub, ws

    hub, ws = asyncio.run(run())
    assert ws.closed_with == 1011                # 닫아야 화면이 다시 접속해 snapshot을 받는다
    assert hub.connected_user_ids() == []


def test_hub_close_session_closes_only_screens_of_that_session():
    async def run():
        hub = GuardianHub()
        shared_pc, phone = ClosableWS(), ClosableWS()
        hub.register(1, shared_pc, session_token="pc-token")
        hub.register(1, phone, session_token="phone-token")
        await hub.close_session("pc-token")      # 공용 PC에서 로그아웃
        await hub.close_session(None)            # 쿠키 없는 로그아웃은 아무것도 닫지 않는다
        delivered = await hub.send_to_users([1], {"type": "event"})
        return shared_pc, phone, delivered

    shared_pc, phone, delivered = asyncio.run(run())
    assert shared_pc.closed_with == 4401 and shared_pc.sent == []
    assert phone.closed_with is None and phone.sent == [{"type": "event"}]
    assert delivered == [1]


def test_alert_not_sent_for_event_already_cleared(world, tmp_path, monkeypatch):
    world.execute("UPDATE events SET ended_at = ?, cleared_by = 'device:1' WHERE id = 1", (now_iso(),))
    notifier, hub, calls = make_notifier(world, tmp_path, monkeypatch, [_sub(2, "e2")])
    ws = FakeWS()
    hub.register(2, ws)
    assert asyncio.run(notifier.notify(get_event(world), owner(world), [2], "alert")) == {}
    assert calls == [] and ws.sent == [] and notifications(world) == []


def test_clear_notice_is_never_sent_before_alert_in_flight(world, tmp_path, monkeypatch):
    """경보 알림을 보내는 도중 해제되면, 해제 알림은 경보 알림이 끝난 뒤에 간다.

    둘은 같은 tag라 나중에 도착한 것이 남는다. 순서가 바뀌면 보호자 휴대폰에 끝난 경보가 남는다.
    """
    notifier, hub, calls = make_notifier(world, tmp_path, monkeypatch, [_sub(2, "e2")], push_retry_delays=[])
    hub.register(2, ClosableWS(slow_once=0.2))   # 보호자 화면 전송이 한 번 느림(약한 네트워크)

    async def run():
        alert = asyncio.create_task(notifier.notify(get_event(world), owner(world), [2], "alert"))
        await asyncio.sleep(0.05)                # 경보 알림이 화면 전송을 기다리는 중
        world.execute("UPDATE events SET ended_at = ?, cleared_by = 'device:1' WHERE id = 1", (now_iso(),))
        await notifier.notify(get_event(world), owner(world), [2], "clear")
        await alert
    asyncio.run(run())

    assert [json.loads(c["data"])["kind"] for c in calls] == ["alert", "clear"]


def test_alert_retry_skips_guardian_unlinked_meanwhile(world, tmp_path, monkeypatch):
    """재시도를 기다리는 사이 연결이 해제된 보호자에게는 늦은 경보(이름·판정 포함)를 보내지 않는다."""
    notifier, _, calls = make_notifier(world, tmp_path, monkeypatch, [_sub(2, "e2"), _sub(3, "e3")],
                                       outcomes={"e2": "network", "e3": "network"}, push_retry_delays=[0.05])

    async def run():
        await notifier.notify(get_event(world), owner(world), [2, 3], "alert")
        world.execute("DELETE FROM guardians WHERE user_id = 1 AND guardian_id = 3")
        await notifier.wait_retries()
    asyncio.run(run())

    endpoints = [c["info"]["endpoint"] for c in calls]
    assert sorted(endpoints[:2]) == ["e2", "e3"]  # 처음에는 두 사람 모두
    assert endpoints[2:] == ["e2"]                # 재시도는 아직 연결된 보호자만


def test_notify_delayed_alert_is_marked(world, tmp_path, monkeypatch):
    notifier, _, calls = make_notifier(world, tmp_path, monkeypatch, [_sub(2, "e2")])
    asyncio.run(notifier.notify(get_event(world), owner(world), [2], "alert", delayed=True))
    assert "지연 전송" in json.loads(calls[0]["data"])["title"]


def test_alert_retry_skips_guardian_suspended_meanwhile(world, tmp_path, monkeypatch):
    """첫 푸시가 실패한 뒤 관리자가 정지한 보호자에게는 재시도로도 경보를 보내지 않는다."""
    notifier, _, calls = make_notifier(world, tmp_path, monkeypatch, [_sub(2, "e2"), _sub(3, "e3")],
                                       outcomes={"e2": "network", "e3": "network"}, push_retry_delays=[0.05])

    async def run():
        await notifier.notify(get_event(world), owner(world), [2, 3], "alert")
        world.execute("UPDATE users SET disabled_at = ? WHERE id = 3", (now_iso(),))
        await notifier.wait_retries()
    asyncio.run(run())

    endpoints = [c["info"]["endpoint"] for c in calls]
    assert sorted(endpoints[:2]) == ["e2", "e3"]
    assert endpoints[2:] == ["e2"]                # 정지된 아들(3)은 재시도에서 빠진다


class DeletingWS(FakeWS):
    """보낼 때 on_send를 부른다. 화면 전송 도중 관리자가 그 보호자를 삭제하는 상황을 만든다."""

    def __init__(self, on_send):
        super().__init__()
        self.on_send = on_send

    async def send_json(self, message):
        await super().send_json(message)
        self.on_send()


def test_guardian_deleted_during_screen_send_does_not_stop_others(world, tmp_path, monkeypatch):
    """지워진 보호자의 전송 기록(외래 키 오류) 때문에 다른 보호자에게 가는 푸시가 멈추면 안 된다."""
    notifier, hub, calls = make_notifier(world, tmp_path, monkeypatch, [_sub(2, "e2"), _sub(3, "e3")],
                                         push_retry_delays=[])
    hub.register(3, DeletingWS(lambda: world.execute("DELETE FROM users WHERE id = 3")))

    summary = asyncio.run(notifier.notify(get_event(world), owner(world), [2, 3], "alert"))

    assert [c["info"]["endpoint"] for c in calls] == ["e2"]   # 딸(2)은 푸시를 받고, 지워진 아들은 빠진다
    assert summary[2]["push"] is True
    assert [(r["guardian_id"], r["channel"], r["state"]) for r in notifications(world)] == [
        (2, "websocket", "failed"), (2, "push", "sent")]


def test_guardian_deleted_during_push_does_not_stop_retries_for_others(world, tmp_path, monkeypatch):
    notifier, _, calls = make_notifier(world, tmp_path, monkeypatch, [_sub(2, "e2"), _sub(3, "e3")],
                                       outcomes={"e2": ["network", "ok"], "e3": "network"},
                                       push_retry_delays=[0.01])
    send = notifier.push.send_to_users

    async def send_then_delete(user_ids, payload):
        result = await send(user_ids, payload)
        world.execute("DELETE FROM users WHERE id = 3")   # 푸시를 보내는 사이 관리자가 아들(3)을 삭제
        return result
    monkeypatch.setattr(notifier.push, "send_to_users", send_then_delete)

    async def run():
        await notifier.notify(get_event(world), owner(world), [2, 3], "alert")   # 오류 없이 끝나야 한다
        await notifier.wait_retries()
    asyncio.run(run())

    endpoints = [c["info"]["endpoint"] for c in calls]
    assert sorted(endpoints[:2]) == ["e2", "e3"] and endpoints[2:] == ["e2"]
    push_rows = [(r["guardian_id"], r["state"], r["detail"]) for r in notifications(world) if r["channel"] == "push"]
    assert push_rows == [(2, "failed", "전송 실패"), (2, "sent", "재시도 1회차 성공")]
