"""점검에서 찾은 문제들의 회귀 시험.

- 현장 화면 API는 장치별 키나 관리자만 (같은 공유기·터널로 닿는 누구나 경보를 끄지 못하게)
- 같은 PC의 터널·프록시를 거친 요청은 '서버 PC 자신'(관리자)으로 보지 않는다
- 서버를 다시 켜도 장치를 기억해 연결 끊김을 알리고, 관리자는 치운 장치를 지울 수 있다
- 관리자 로그인은 세션마다 다른 값이고, 로그아웃하면 그 값은 다시 쓸 수 없다
- 영상 처리: 낙상 의심을 넘기다 오류가 나도 다시 시도, 분석 멈춤 표시, 잠깐 끊긴 재접속은 판정을 이어 감,
  빈 방은 초당 5장만 자세 추정
"""

import asyncio
import unittest
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

from server import app as app_mod
from server import vision
from server.app import ADMIN_COOKIE, ADMIN_HEADER, CLEAR_HEADER, FallServer
from server.events import collect_audio
from server.fall import FallParams, FallState
from server.tests.fakes import (TOKEN, FakeNotifier, FakePoseFactory, FakeRecognizer, fall_script, make_settings,
                                prompt_sound, standing, tiny_jpeg)
from server.tests.test_e2e import ServerTestBase
from server.vision import FrameAnalyzer, VideoPipeline
from shared import protocol as P

REMOTE = {"X-Forwarded-For": "203.0.113.9"}  # 같은 PC의 터널을 거쳐 온 바깥 요청


def key_for(device_id: str) -> dict:
    return {P.KIOSK_KEY_HEADER: P.kiosk_key(TOKEN, device_id)}


class KioskKeyTest(ServerTestBase):
    async def test_kiosk_api_needs_the_device_key(self):
        edge = await self.connect()
        await edge.hello("pi-1", name="거실")
        other = await self.connect()
        await other.hello("pi-2", name="안방")

        self.assertEqual((await self.client.get("/api/state?device=pi-1", headers=REMOTE)).status, 401)
        res = await self.client.get("/api/state?device=pi-1", headers={**REMOTE, **key_for("pi-1")})
        self.assertEqual(res.status, 200)
        data = await res.json()
        self.assertEqual([d["device_id"] for d in data["devices"]], ["pi-1"])  # 다른 방 정보는 보내지 않음
        self.assertNotIn("kiosk_url", data["devices"][0])
        res = await self.client.get("/api/state?device=pi-2", headers={**REMOTE, **key_for("pi-1")})
        self.assertEqual(res.status, 401)  # 거실 키로 안방을 볼 수 없다
        self.assertEqual((await self.client.get("/api/state", headers=REMOTE)).status, 401)  # 장치 선택은 관리자만

        self.assertEqual((await self.client.get("/api/events?device=pi-1", headers={**REMOTE, **key_for("pi-1")})).status,
                         200)
        self.assertEqual((await self.client.get("/api/events", headers={**REMOTE, **key_for("pi-1")})).status, 401)
        self.assertEqual((await self.client.get("/api/events?kind=system", headers=REMOTE)).status, 401)

        await self.fall(edge)
        res = await self.client.post("/api/clear?device=pi-1", headers={**REMOTE, CLEAR_HEADER: "1"})
        self.assertEqual(res.status, 401)  # 키 없이 경보를 끌 수 없다
        res = await self.client.post("/api/clear?device=pi-1", headers={**REMOTE, CLEAR_HEADER: "1",
                                                                        **key_for("pi-1")})
        self.assertEqual((await res.json())["cleared"], True)

    async def test_admin_is_not_open_through_a_local_tunnel(self):
        self.assertEqual((await self.client.get("/api/admin/overview")).status, 200)  # 서버 PC에서 직접
        self.assertEqual((await self.client.get("/api/admin/overview", headers={"Cf-Connecting-Ip": "1.2.3.4"})).status,
                         401)
        self.assertEqual((await self.client.get("/api/admin/overview", headers=REMOTE)).status, 401)
        res = await self.client.get("/api/admin/overview", headers={"Host": "evil.example"})
        self.assertEqual(res.status, 401)  # DNS rebinding

    async def test_admin_overview_gives_kiosk_urls(self):
        edge = await self.connect()
        await edge.hello("거실 1", name="거실")
        data = await (await self.client.get("/api/admin/overview")).json()
        url = data["devices"][0]["kiosk_url"]
        self.assertEqual(url, f"/kiosk?device=%EA%B1%B0%EC%8B%A4%201&key={P.kiosk_key(TOKEN, '거실 1')}")


class RestartAndForgetTest(ServerTestBase):
    async def restart(self) -> None:
        await self.client.close()
        self.server = FallServer(self.settings, pose_factory=self.pose, recognizer=self.recognizer,
                                 notifier=self.notifier, prompt=prompt_sound())
        self.client = TestClient(TestServer(self.server.app))
        await self.client.start_server()

    async def test_device_remembered_after_restart_then_forgotten(self):
        edge = await self.connect()
        await edge.hello("pi-1", name="거실")
        await edge.ws.close()
        await self.restart()

        data = await (await self.client.get("/api/admin/overview")).json()
        self.assertEqual([(d["device_id"], d["name"], d["view"]) for d in data["devices"]],
                         [("pi-1", "거실", "disconnected")])  # 다시 붙지 않아도 목록에 남아 감시된다
        self.assertIn("라즈베리파이 연결이 끊겼습니다", self.server.events.problems_by_subject()["pi-1"])

        live = await self.connect()
        await live.hello("pi-2", name="안방")
        res = await self.client.delete("/api/admin/devices/pi-2", headers={ADMIN_HEADER: "1"})
        self.assertEqual(res.status, 409)  # 연결된 장치는 지울 수 없다
        self.assertEqual((await self.client.delete("/api/admin/devices/pi-1")).status, 403)  # 헤더 필요
        res = await self.client.delete("/api/admin/devices/pi-1", headers={ADMIN_HEADER: "1"})
        self.assertEqual(await res.json(), {"forgotten": True})
        await self.restart()
        data = await (await self.client.get("/api/admin/overview")).json()
        self.assertEqual([d["device_id"] for d in data["devices"]], ["pi-2"])


class AdminSessionTest(ServerTestBase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.client.close()
        self.settings = make_settings(Path(self.tmp.name), admin_password="pw-1234")
        self._delay, app_mod.ADMIN_LOGIN_FAIL_DELAY = app_mod.ADMIN_LOGIN_FAIL_DELAY, 0.0
        await self.start()

    async def start(self) -> None:
        self.server = FallServer(self.settings, pose_factory=self.pose, recognizer=FakeRecognizer(),
                                 notifier=FakeNotifier(), prompt=prompt_sound())
        self.client = TestClient(TestServer(self.server.app))
        await self.client.start_server()

    async def asyncTearDown(self):
        app_mod.ADMIN_LOGIN_FAIL_DELAY = self._delay
        await super().asyncTearDown()

    async def login(self, password: str):
        return await self.client.post("/admin/login", data={"password": password}, allow_redirects=False)

    async def test_session_survives_restart_and_logout_revokes_it(self):
        res = await self.login("pw-1234")
        token = res.cookies[ADMIN_COOKIE].value
        await self.client.close()
        await self.start()  # 서버를 다시 켜도(자동 업데이트 등) 로그인이 유지된다
        self.client.session.cookie_jar.update_cookies({ADMIN_COOKIE: token})
        self.assertEqual((await self.client.get("/api/admin/overview")).status, 200)
        await self.client.post("/admin/logout", allow_redirects=False)
        self.client.session.cookie_jar.update_cookies({ADMIN_COOKIE: token})
        self.assertEqual((await self.client.get("/api/admin/overview")).status, 401)  # 로그아웃한 값은 못 씀

    async def test_admin_login_is_rate_limited(self):
        for _ in range(5):
            await self.login("wrong")
        res = await self.login("pw-1234")
        self.assertEqual(res.headers["Location"], "/admin/login?error=locked")
        # 다른 곳(터널을 거친 다른 주소)에서는 잠기지 않는다
        res = await self.client.post("/admin/login", data={"password": "pw-1234"}, allow_redirects=False,
                                     headers={"X-Forwarded-For": "198.51.100.7"})
        self.assertEqual(res.headers["Location"], "/admin")


class GuardianInputTest(ServerTestBase):
    async def test_wrong_types_are_rejected(self):
        async def create(**body):
            return await self.client.post("/api/admin/guardians", headers={ADMIN_HEADER: "1"},
                                          json={"username": "son", "password": "password1", **body})

        self.assertEqual((await create(devices="pi-1")).status, 400)  # 문자열을 글자 목록으로 바꾸지 않는다
        self.assertEqual((await create(can_clear="false")).status, 400)
        self.assertEqual((await create(name=123)).status, 400)
        self.assertEqual((await create(devices=["pi-1"], can_clear=False)).status, 201)

    async def test_non_ascii_cookie_is_not_a_server_error(self):
        self.client.session.cookie_jar.update_cookies({"fall_guardian": "1.1.9999999999.가나다"})
        self.assertEqual((await self.client.get("/api/g/overview")).status, 401)


class VideoPipelineRobustnessTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.jpeg = tiny_jpeg()

    async def feed(self, pipeline, t0, seconds):
        for i in range(int(round(seconds * 10))):
            pipeline.submit(t0 + i * 0.1, self.jpeg)
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)

    async def test_failed_suspect_report_is_retried(self):
        calls = []

        async def on_suspect(device_id, features):
            calls.append(device_id)
            if len(calls) == 1:
                raise RuntimeError("database is locked")

        pipeline = VideoPipeline("pi-1", FakePoseFactory(fall_script(fall_at=1.0)), FallParams(), 0.3, on_suspect)
        pipeline.start()
        await self.feed(pipeline, 100.0, 4.0)
        self.assertEqual(len(calls), 1)
        self.assertIn("낙상 의심을 처리하지 못했습니다", " ".join(pipeline.problems()))
        pipeline._suspect_retry_at = 0.0       # 다시 시도할 때가 됨 (보통 1초 뒤)
        await self.feed(pipeline, 104.0, 0.3)  # 다음 장면에서 다시 시도
        self.assertEqual(len(calls), 2)
        self.assertEqual(pipeline.problems(), [])
        await pipeline.close()

    async def test_stall_and_broken_frames_are_reported(self):
        pipeline = VideoPipeline("pi-1", FakePoseFactory(), FallParams(), 0.3, self._noop)
        now = pipeline.session_started_at + 20
        pipeline.last_frame_at = now - 0.5           # 영상은 들어오는데
        pipeline.last_processed_at = now - 8          # 8초째 처리하지 못함
        self.assertEqual(pipeline.problems(now), ["영상 분석이 멈췄습니다"])
        pipeline.decode_fail_run = vision.DECODE_FAIL_FRAMES
        self.assertIn("깨져서", pipeline.problems(now)[0])
        pipeline.last_frame_at = now - 30             # 영상이 안 들어오는 것은 장치 쪽 문제로 따로 표시
        self.assertEqual(pipeline.problems(now), [])
        await pipeline.close()

    async def _noop(self, device_id, features):
        pass

    async def test_short_reconnect_keeps_detection_but_reboot_resets(self):
        suspects = []

        async def on_suspect(device_id, features):
            suspects.append(features)

        pipeline = VideoPipeline("pi-1", FakePoseFactory(fall_script(fall_at=1.0)), FallParams(), 0.3, on_suspect)
        pipeline.start()
        await self.feed(pipeline, 500.0, 2.0)  # 넘어져 누운 지 0.7초
        self.assertEqual(pipeline.state, FallState.FALLING)
        pipeline.new_session()                 # 와이파이가 잠깐 끊겼다 다시 접속 (edge 시계는 이어짐)
        await self.feed(pipeline, 502.5, 2.0)
        self.assertEqual(len(suspects), 1)     # 넘어지던 판정을 이어 가 놓치지 않는다
        self.assertEqual(pipeline._analyzer.estimator.resets, 0)

        pipeline.reset_detection()
        pipeline.new_session()                 # 라즈베리파이 재부팅: 시계가 처음부터
        await self.feed(pipeline, 5.0, 0.5)
        self.assertEqual(pipeline._analyzer.estimator.resets, 1)
        await pipeline.close()


class IdleSkipTest(unittest.TestCase):
    def test_empty_room_is_checked_five_times_a_second(self):
        factory = FakePoseFactory(lambda t: None)  # 아무도 없음
        analyzer = FrameAnalyzer(factory.create(), FallParams(), 0.3)
        jpeg = tiny_jpeg()
        for i in range(40):  # 10fps로 4초
            analyzer.process(jpeg, i / 10)
        # 처음 2초는 매 장면(20장), 그 뒤 2초는 0.2초마다(10장)
        self.assertEqual(len(factory.seen), 20 + 10)

        factory.script = lambda t: standing()
        seen = len(factory.seen)
        for i in range(40, 46):
            result = analyzer.process(jpeg, i / 10)
        self.assertGreaterEqual(len(factory.seen) - seen, 5)  # 사람이 보이면 곧바로 매 장면
        self.assertTrue(result.person)
        self.assertEqual(result.state, FallState.NORMAL)

    def test_no_skip_while_falling(self):
        factory = FakePoseFactory(lambda t: None)
        analyzer = FrameAnalyzer(factory.create(), FallParams(), 0.3)
        analyzer.empty_since = 0.0
        analyzer.fsm.state = FallState.FALLING  # 넘어지는 중에는 사람이 안 보여도 매 장면 본다
        for i in range(30, 40):
            analyzer.process(tiny_jpeg(), i / 10)
        self.assertEqual(len(factory.seen), 10)


class CollectAudioTest(unittest.IsolatedAsyncioTestCase):
    async def test_joins_queued_chunks_and_stops_at_disconnect(self):
        queue = asyncio.Queue()
        for chunk in (b"bb", b"cc", None, b"dd"):
            queue.put_nowait(chunk)
        self.assertEqual(collect_audio(queue, b"aa"), (b"aabbcc", True))
        self.assertEqual(collect_audio(queue, queue.get_nowait()), (b"dd", False))


if __name__ == "__main__":
    unittest.main()
