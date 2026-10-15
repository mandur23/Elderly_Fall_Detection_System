"""가짜 edge(aiohttp 클라이언트)로 서버 전체 흐름을 끝까지 확인한다.

접속 -> 영상 -> 낙상 의심 -> 안내 재생(playback_done 흉내) -> 마이크 소리 -> 해제/경보 -> 화면 버튼 해제.
자세 추정과 음성 인식은 대역(fakes)을 써서 모델 없이 돈다. 영상 시각은 10fps로 붙이되 빠르게 보낸다.
"""

import asyncio
import tempfile
import unittest
from pathlib import Path
from typing import Dict, List, Optional

import aiohttp
from aiohttp.test_utils import TestClient, TestServer

from server import store as st
from server.app import CLEAR_HEADER, FallServer
from server.tests.fakes import (TOKEN, FakeNotifier, FakePoseFactory, FakeRecognizer, fall_script, loud_pcm,
                                make_settings, prompt_sound, quiet_pcm, standing, tiny_jpeg)
from shared import protocol as P

HELLO = dict(protocol=P.PROTOCOL_VERSION, video={"width": 640, "height": 480, "fps": 10, "format": "jpeg"},
             audio={"rate": 16000, "channels": 1, "format": "s16le"}, name="거실", software="test")


class Edge:
    """규약대로 움직이는 가짜 edge. 서버 메시지는 순서대로 쌓아 두고 expect()로 꺼낸다."""

    def __init__(self, ws: aiohttp.ClientWebSocketResponse):
        self.ws = ws
        self.t = 1000.0  # edge 시계 (time.monotonic()처럼 임의 시작값)
        self.jpeg = tiny_jpeg()
        self.audio_out: Dict[str, bytearray] = {}
        self._receiving: Optional[str] = None
        self.seen: List[Dict] = []

    async def hello(self, device_id: str = "pi-1", **fields) -> Dict:
        await self.ws.send_str(P.encode(P.HELLO, **{**HELLO, "device_id": device_id, **fields}))
        welcome = await self.expect(P.WELCOME)
        assert welcome["protocol"] == P.PROTOCOL_VERSION and "video" in welcome
        return welcome

    async def status(self, **fields) -> None:
        await self.ws.send_str(P.encode(P.STATUS, **{"camera_ok": True, "mic_ok": True, "speaker_ok": True,
                                                    "fps": 10.0, **fields}))

    async def frames(self, seconds: float) -> None:
        for _ in range(int(round(seconds * 10))):
            await self.ws.send_bytes(P.pack(P.VIDEO, self.t, self.jpeg))
            self.t += 0.1
            await asyncio.sleep(0.004)

    async def speak(self, pcm: bytes) -> None:
        for i in range(0, len(pcm), P.AUDIO_CHUNK_BYTES):
            await self.ws.send_bytes(P.pack(P.AUDIO_IN, self.t, pcm[i:i + P.AUDIO_CHUNK_BYTES]))
            await asyncio.sleep(0.002)

    async def next(self, timeout: float = 5.0) -> Dict:
        """다음 JSON 메시지. AUDIO_OUT은 재생 중인 id에 모은다."""
        while True:
            msg = await self.ws.receive(timeout=timeout)
            if msg.type == aiohttp.WSMsgType.BINARY:
                kind, _, payload = P.unpack(msg.data)
                assert kind == P.AUDIO_OUT, kind
                assert self._receiving is not None, "play_begin 없이 AUDIO_OUT"
                self.audio_out[self._receiving] += payload
                continue
            if msg.type != aiohttp.WSMsgType.TEXT:
                return {"type": "_closed", "code": self.ws.close_code, "msg": msg}
            data = P.decode(msg.data)
            assert data["type"] in P.SERVER_TO_EDGE, data
            if data["type"] == P.PLAY_BEGIN:
                self.audio_out[data["id"]] = bytearray()
                self._receiving = data["id"]
            elif data["type"] == P.PLAY_END:
                self._receiving = None
            self.seen.append(data)
            return data

    async def expect(self, type_: str, timeout: float = 5.0, **match) -> Dict:
        while True:
            data = await self.next(timeout)
            if data["type"] == type_ and all(data.get(k) == v for k, v in match.items()):
                return data
            if data["type"] == "_closed":
                raise AssertionError(f"{type_}을 기다리는 중 연결이 닫힘: {data}")

    async def play_prompt(self) -> str:
        """안내 음성을 받아 재생한 척하고 playback_done을 보낸다. 마이크는 그 뒤에 켜져야 한다."""
        begin = await self.expect(P.PLAY_BEGIN)
        assert not begin.get("loop"), begin
        await self.expect(P.PLAY_END, id=begin["id"])
        assert len(self.audio_out[begin["id"]]) > 0
        try:
            early = await self.next(timeout=0.2)
            raise AssertionError(f"재생이 끝나기 전에 메시지가 옴: {early}")
        except asyncio.TimeoutError:
            pass
        await self.ws.send_str(P.encode(P.PLAYBACK_DONE, id=begin["id"]))
        await self.expect(P.MIC, enabled=True)
        return begin["id"]


class ServerTestBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = make_settings(Path(self.tmp.name))
        self.pose = FakePoseFactory(fall_script(fall_at=1.0))
        self.recognizer = FakeRecognizer("괜찮아요")
        self.notifier = FakeNotifier()
        self.server = FallServer(self.settings, pose_factory=self.pose, recognizer=self.recognizer,
                                 notifier=self.notifier, prompt=prompt_sound())
        self.client = TestClient(TestServer(self.server.app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.tmp.cleanup()

    async def connect(self, token: str = TOKEN) -> Edge:
        ws = await self.client.ws_connect(P.WS_PATH, headers=P.bearer(token))
        return Edge(ws)

    async def state(self) -> Dict:
        res = await self.client.get("/api/state")
        self.assertEqual(res.status, 200)
        return await res.json()

    async def wait_state(self, state: str, timeout: float = 5.0) -> Dict:
        for _ in range(int(timeout / 0.02)):
            data = await self.state()
            if data["state"] == state:
                return data
            await asyncio.sleep(0.02)
        self.fail(f"상태가 {state}가 되지 않았습니다: {data}")

    async def events(self) -> List[Dict]:
        return await (await self.client.get("/api/events")).json()

    async def fall(self, edge: Edge) -> Dict:
        """낙상 영상(서 있다가 1초에 넘어져 누움)을 보내 낙상 의심이 되게 한다."""
        await edge.status()
        await edge.frames(4.0)
        return await self.wait_state("SUSPECT")


class ConnectionTest(ServerTestBase):
    async def test_health_and_kiosk(self):
        res = await self.client.get(P.HEALTH_PATH)
        self.assertEqual(res.status, 200)
        self.assertEqual((await res.json())["protocol"], P.PROTOCOL_VERSION)
        res = await self.client.get(P.KIOSK_PATH)
        self.assertEqual(res.status, 200)
        page = await res.text()
        self.assertIn("괜찮아요", page)
        self.assertNotIn("{{PROMPT_TEXT}}", page)
        self.assertEqual((await self.client.get("/static/kiosk.js")).status, 200)
        data = await self.state()
        self.assertEqual(data["view"], "disconnected")

    async def test_wrong_token_is_401(self):
        for headers in ({}, P.bearer("wrong"), {"Authorization": f"Basic {TOKEN}"}):
            with self.assertRaises(aiohttp.WSServerHandshakeError) as cm:
                await self.client.ws_connect(P.WS_PATH, headers=headers)
            self.assertEqual(cm.exception.status, 401)

    async def test_wrong_version_is_closed_4400(self):
        edge = await self.connect()
        await edge.ws.send_str(P.encode(P.HELLO, **{**HELLO, "device_id": "pi-1", "protocol": 99}))
        err = await edge.expect(P.ERROR)
        self.assertIn("버전", err["message"])
        await edge.ws.receive(timeout=5)
        self.assertEqual(edge.ws.close_code, P.CLOSE_PROTOCOL_ERROR)

    async def test_bad_message_is_closed_4400(self):
        edge = await self.connect()
        await edge.hello()
        await edge.ws.send_str('{"type": "mic", "enabled": true}')  # 서버만 보내는 메시지
        await edge.expect(P.ERROR)
        await edge.ws.receive(timeout=5)
        self.assertEqual(edge.ws.close_code, P.CLOSE_PROTOCOL_ERROR)

    async def test_same_device_reconnect_closes_old_with_4409(self):
        old = await self.connect()
        await old.hello("pi-1")
        new = await self.connect()
        await new.hello("pi-1")
        closed = await old.next()
        self.assertEqual(closed["type"], "_closed")
        self.assertEqual(old.ws.close_code, P.CLOSE_REPLACED)
        await new.status()
        await asyncio.sleep(0.05)
        data = await self.state()
        self.assertEqual([d["connected"] for d in data["devices"]], [True])

    async def test_device_problems_on_kiosk(self):
        edge = await self.connect()
        await edge.hello()
        await edge.status(mic_ok=False, mic_error="USB 마이크 없음")
        await edge.frames(0.3)
        await asyncio.sleep(0.05)
        data = await self.state()
        self.assertEqual(data["view"], "check")
        self.assertTrue(any("마이크" in p and "USB 마이크 없음" in p for p in data["problems"]))
        await edge.ws.close()
        await asyncio.sleep(0.05)
        data = await self.state()
        self.assertEqual(data["view"], "disconnected")
        self.assertIn("라즈베리파이 연결이 끊겼습니다", data["problems"])

    async def test_mic_is_off_by_default_and_audio_ignored(self):
        edge = await self.connect()
        await edge.hello()
        self.pose.script = lambda t: standing()
        await edge.speak(loud_pcm(0.3))  # 마이크를 켜지 않았으면 버린다
        await edge.frames(1.0)
        await asyncio.sleep(0.05)
        self.assertEqual(self.recognizer.heard, [])
        self.assertEqual((await self.state())["state"], "NORMAL")


class VoiceFlowTest(ServerTestBase):
    async def test_fall_then_voice_safe_clears(self):
        edge = await self.connect()
        await edge.hello()
        data = await self.fall(edge)
        self.assertEqual(data["view"], "suspect")
        await edge.play_prompt()
        self.assertEqual((await self.state())["event"]["phase"], "listen")
        await edge.speak(loud_pcm(0.6))
        await edge.expect(P.MIC, enabled=False)
        data = await self.wait_state("NORMAL")
        self.assertEqual(data["view"], "normal")
        ev = (await self.events())[0]
        self.assertEqual((ev["result"], ev["voice_result"], ev["voice_attempts"]), (st.CLEARED_VOICE, "SAFE", 1))
        self.assertEqual(ev["notify_state"], st.NOTIFY_NONE)
        self.assertEqual(self.notifier.sent, [])
        self.assertIsNone(self.server.store.get_event(ev["id"])["voice_text"])  # 인식 문장 원문 저장 안 함
        # 감시 재개: 다시 넘어지면 새 이벤트
        self.pose.script = fall_script(fall_at=0.5)
        self.server.pipelines["pi-1"]._analyzer.estimator.t0 = None
        await edge.frames(3.5)
        await self.wait_state("SUSPECT")

    async def test_no_reply_alarm_then_button_clear(self):
        edge = await self.connect()
        await edge.hello()
        await self.fall(edge)
        for attempt in (1, 2):  # 무응답이면 한 번 더 묻는다
            await edge.play_prompt()
            await edge.speak(quiet_pcm(0.6))
            await edge.expect(P.MIC, enabled=False)
        siren = await edge.expect(P.PLAY_BEGIN, loop=True)
        await edge.expect(P.PLAY_END, id=siren["id"])
        self.assertGreater(len(edge.audio_out[siren["id"]]), 16000)
        data = await self.wait_state("ALARM")
        self.assertEqual(data["view"], "alarm")
        self.assertEqual(data["event"]["result"], st.ALERT_NO_REPLY)
        await asyncio.sleep(0.05)
        self.assertEqual(len(self.notifier.sent), 1)
        self.assertIn("음성 확인에 응답 없음", self.notifier.sent[0])
        self.assertEqual((await self.state())["event"]["notify_state"], st.NOTIFY_SENT)

        res = await self.client.post("/api/clear")  # 화면 스크립트 헤더 없이는 거절
        self.assertEqual(res.status, 403)
        res = await self.client.post("/api/clear", headers={CLEAR_HEADER: "1"})
        self.assertEqual(await res.json(), {"cleared": True, "state": "NORMAL"})
        await edge.expect(P.PLAY_STOP)
        await edge.ws.send_str(P.encode(P.PLAYBACK_DONE, id=siren["id"], stopped=True))
        await asyncio.sleep(0.05)
        ev = (await self.events())[0]
        self.assertEqual((ev["result"], ev["cleared_by"], ev["voice_attempts"]), (st.ALERT_NO_REPLY, "BUTTON", 2))
        self.assertIsNotNone(ev["ended_at"])
        self.assertIn("경보 해제", self.notifier.sent[-1])
        self.assertEqual((await self.state())["state"], "NORMAL")

    async def test_help_request_alarms_at_once(self):
        self.recognizer.text = "도와주세요"
        edge = await self.connect()
        await edge.hello()
        await self.fall(edge)
        await edge.play_prompt()
        await edge.speak(loud_pcm(0.6))
        await edge.expect(P.PLAY_BEGIN, loop=True)
        data = await self.wait_state("ALARM")
        self.assertEqual(data["event"]["result"], st.ALERT_HELP)
        self.assertEqual(data["event"]["voice_attempt"], 1)

    async def test_disconnect_during_voice_check_alarms_and_siren_on_reconnect(self):
        edge = await self.connect()
        await edge.hello()
        await self.fall(edge)
        await edge.play_prompt()
        await edge.ws.close()
        data = await self.wait_state("ALARM")
        self.assertEqual(data["event"]["result"], st.ALERT_DISCONNECTED)
        self.assertEqual(data["view"], "alarm")
        again = await self.connect()
        await again.hello()
        await again.expect(P.PLAY_BEGIN, loop=True)  # 다시 접속하면 경보음을 다시 울린다

    async def test_button_during_voice_check_stops_prompt_and_mic(self):
        edge = await self.connect()
        await edge.hello()
        await self.fall(edge)
        await edge.play_prompt()  # 듣는 중
        res = await self.client.post("/api/clear", headers={CLEAR_HEADER: "1"})
        self.assertEqual((await res.json())["cleared"], True)
        types = {(await edge.next())["type"] for _ in range(2)}
        self.assertEqual(types, {P.MIC, P.PLAY_STOP})
        ev = (await self.events())[0]
        self.assertEqual(ev["result"], st.CLEARED_BUTTON)
        self.assertEqual(self.notifier.sent, [])

    async def test_speaker_problem_means_device_fail_alarm(self):
        edge = await self.connect()
        await edge.hello()
        await edge.status(speaker_ok=False, speaker_error="재생 장치 없음")
        await edge.frames(4.0)
        await edge.expect(P.PLAY_BEGIN, loop=True)  # 안내 없이 바로 경보(경보음은 보내 본다)
        data = await self.wait_state("ALARM")
        self.assertEqual(data["event"]["result"], st.ALERT_VOICE_FAIL)

    async def test_missing_playback_done_means_device_fail(self):
        edge = await self.connect()
        await edge.hello()
        await self.fall(edge)
        await edge.expect(P.PLAY_END)  # playback_done을 보내지 않음 (0.1초 + 여유 1초)
        siren = await edge.expect(P.PLAY_BEGIN, timeout=5, loop=True)
        self.assertTrue(siren["id"])
        data = await self.wait_state("ALARM")
        self.assertEqual(data["event"]["result"], st.ALERT_VOICE_FAIL)
        self.assertFalse(any(m["type"] == P.MIC for m in edge.seen))  # 마이크는 켜지 않았다

    async def test_silent_microphone_means_device_fail(self):
        edge = await self.connect()
        await edge.hello()
        await self.fall(edge)
        await edge.play_prompt()
        await edge.speak(b"\x00\x00" * 8000)  # 완전한 무음 = 마이크 고장
        await edge.expect(P.MIC, enabled=False)
        data = await self.wait_state("ALARM")
        self.assertEqual(data["event"]["result"], st.ALERT_VOICE_FAIL)


class DisabledNotifierTest(ServerTestBase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.notifier.enabled = False

    async def test_alarm_shows_notifier_not_configured(self):
        self.recognizer.text = "살려주세요"
        edge = await self.connect()
        await edge.hello()
        await self.fall(edge)
        await edge.play_prompt()
        await edge.speak(loud_pcm(0.6))
        data = await self.wait_state("ALARM")
        await asyncio.sleep(0.05)
        data = await self.state()
        self.assertFalse(data["notifier_enabled"])
        self.assertEqual(data["event"]["notify_state"], st.NOTIFY_DISABLED)


if __name__ == "__main__":
    unittest.main()
