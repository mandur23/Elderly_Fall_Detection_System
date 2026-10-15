"""라즈베리파이 여러 대와 관리자 화면 시험.

장치마다 낙상 의심·음성 확인·경보가 따로 돌고(한 방의 확인 중에도 다른 방 낙상을 처리), 경보음·해제도
그 장치에만 적용되는지 확인한다. 관리자 화면은 비밀번호·서버 PC 제한과 관리자 해제를 확인한다.
"""

import asyncio
import unittest
from pathlib import Path
from typing import Dict

from server import app as app_mod
from server import store as st
from server.app import ADMIN_HEADER, CLEAR_HEADER, FallServer, _is_loopback
from server.tests.fakes import (FakeNotifier, FakePoseFactory, FakeRecognizer, fall_script, loud_pcm, make_settings,
                                prompt_sound, quiet_pcm)
from server.tests.test_e2e import Edge, ServerTestBase
from shared import protocol as P


class MultiDeviceTest(ServerTestBase):
    async def device_state(self, device_id: str) -> Dict:
        res = await self.client.get(f"/api/state?device={device_id}")
        self.assertEqual(res.status, 200)
        return await res.json()

    async def wait_device(self, device_id: str, state: str, timeout: float = 5.0) -> Dict:
        for _ in range(int(timeout / 0.02)):
            data = await self.device_state(device_id)
            if data["state"] == state:
                return data
            await asyncio.sleep(0.02)
        self.fail(f"{device_id} 상태가 {state}가 되지 않았습니다: {data}")

    async def assert_no_message(self, edge: Edge, seconds: float = 0.3) -> None:
        try:
            msg = await edge.next(timeout=seconds)
        except asyncio.TimeoutError:
            return
        self.fail(f"이 장치에는 메시지가 오지 않아야 합니다: {msg}")

    async def test_two_devices_fall_and_resolve_independently(self):
        living, bedroom = await self.connect(), await self.connect()
        await living.hello("pi-1", name="거실")
        await bedroom.hello("pi-2", name="안방")
        await asyncio.gather(living.status(), bedroom.status())
        await asyncio.gather(living.frames(4.0), bedroom.frames(4.0))  # 두 방에서 동시에 넘어짐

        a = await self.wait_device("pi-1", "SUSPECT")
        b = await self.wait_device("pi-2", "SUSPECT")
        self.assertNotEqual(a["event"]["id"], b["event"]["id"])  # 한쪽을 무시하지 않고 이벤트를 따로 만든다
        self.assertEqual((a["location"], b["location"]), ("거실", "안방"))  # 각 장치가 보낸 이름
        self.assertEqual((await self.state())["view"], "select")  # 장치를 정하지 않은 현장 화면

        async def living_answers():
            await living.play_prompt()
            await living.speak(loud_pcm(0.6))  # '괜찮아요'
            await living.expect(P.MIC, enabled=False)

        async def bedroom_silent():
            for _ in range(self.settings.voice_max_attempts):
                await bedroom.play_prompt()
                await bedroom.speak(quiet_pcm(0.6))
                await bedroom.expect(P.MIC, enabled=False)
            return await bedroom.expect(P.PLAY_BEGIN, loop=True)  # 경보음

        _, siren = await asyncio.gather(living_answers(), bedroom_silent())
        self.assertTrue(siren["id"].startswith("loop"))
        self.assertEqual((await self.wait_device("pi-1", "NORMAL"))["view"], "normal")
        self.assertEqual((await self.wait_device("pi-2", "ALARM"))["view"], "alarm")
        await self.assert_no_message(living)  # 경보음은 안방에서만

        res = await self.client.post("/api/clear?device=pi-1", headers={CLEAR_HEADER: "1"})
        self.assertFalse((await res.json())["cleared"])  # 거실 화면 버튼으로 안방 경보를 끌 수 없다
        self.assertEqual((await self.device_state("pi-2"))["state"], "ALARM")
        res = await self.client.post("/api/clear?device=pi-2", headers={CLEAR_HEADER: "1"})
        self.assertEqual(await res.json(), {"cleared": True, "state": "NORMAL"})
        await bedroom.expect(P.PLAY_STOP)
        await self.assert_no_message(living)

        events = {e["device_id"]: e for e in await self.events()}
        self.assertEqual(events["pi-1"]["result"], st.CLEARED_VOICE)
        self.assertEqual((events["pi-2"]["result"], events["pi-2"]["cleared_by"]), (st.ALERT_NO_REPLY, "BUTTON"))
        self.assertIn("[낙상 의심 경보] 안방", self.notifier.sent[0])  # 알림에 그 장치의 설치 위치
        only_bedroom = await (await self.client.get("/api/events?device=pi-2")).json()
        self.assertEqual([e["device_id"] for e in only_bedroom], ["pi-2"])

    async def test_one_device_disconnect_does_not_affect_other(self):
        living, bedroom = await self.connect(), await self.connect()
        await living.hello("pi-1", name="거실")
        await bedroom.hello("pi-2", name="안방")
        await asyncio.gather(living.status(), bedroom.status())
        await bedroom.ws.close()
        await asyncio.sleep(0.1)
        self.assertEqual((await self.device_state("pi-2"))["view"], "disconnected")
        living_view = await self.device_state("pi-1")
        self.assertEqual(living_view["view"], "normal")
        self.assertEqual(living_view["problems"], [])


class AdminTest(ServerTestBase):
    async def test_admin_from_server_pc_without_password(self):
        edge = await self.connect()
        await edge.hello("pi-1", name="거실")
        await edge.status()
        res = await self.client.get("/admin")  # 테스트 클라이언트는 127.0.0.1에서 접속
        self.assertEqual(res.status, 200)
        self.assertIn("낙상 감지 관리자", await res.text())
        data = await (await self.client.get("/api/admin/overview")).json()
        self.assertEqual(data["summary"]["connected"], 1)
        self.assertEqual(data["devices"][0]["name"], "거실")
        self.assertEqual(data["devices"][0]["view"], "normal")
        self.assertEqual(data["devices"][0]["last_24h"], {"events": 0, "alarms": 0})

    def test_loopback_check(self):
        self.assertTrue(_is_loopback("127.0.0.1"))
        self.assertTrue(_is_loopback("::1"))
        self.assertFalse(_is_loopback("192.168.0.10"))
        self.assertFalse(_is_loopback(None))


class AdminPasswordTest(ServerTestBase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.client.close()
        self.settings = make_settings(Path(self.tmp.name), admin_password="pw-1234")
        self.notifier = FakeNotifier()
        self.server = FallServer(self.settings, pose_factory=FakePoseFactory(fall_script(fall_at=1.0)),
                                 recognizer=FakeRecognizer("괜찮아요"), notifier=self.notifier,
                                 prompt=prompt_sound())
        from aiohttp.test_utils import TestClient, TestServer
        self.client = TestClient(TestServer(self.server.app))
        await self.client.start_server()
        self._delay, app_mod.ADMIN_LOGIN_FAIL_DELAY = app_mod.ADMIN_LOGIN_FAIL_DELAY, 0.0

    async def asyncTearDown(self):
        app_mod.ADMIN_LOGIN_FAIL_DELAY = self._delay
        await super().asyncTearDown()

    async def test_login_then_admin_clear(self):
        res = await self.client.get("/admin", allow_redirects=False)
        self.assertEqual((res.status, res.headers["Location"]), (302, "/admin/login"))
        self.assertEqual((await self.client.get("/api/admin/overview")).status, 401)

        res = await self.client.post("/admin/login", data={"password": "wrong"}, allow_redirects=False)
        self.assertEqual(res.headers["Location"], "/admin/login?error=1")
        self.assertEqual((await self.client.get("/api/admin/overview")).status, 401)

        res = await self.client.post("/admin/login", data={"password": "pw-1234"}, allow_redirects=False)
        self.assertEqual((res.status, res.headers["Location"]), (302, "/admin"))
        self.assertEqual((await self.client.get("/api/admin/overview")).status, 200)

        edge = await self.connect()
        await edge.hello("pi-1", name="거실")
        await self.fall(edge)
        res = await self.client.post("/api/admin/clear?device=pi-1")
        self.assertEqual(res.status, 403)  # 관리자 화면 스크립트 헤더 없이는 거절
        res = await self.client.post("/api/admin/clear?device=pi-1", headers={ADMIN_HEADER: "1"})
        self.assertEqual(await res.json(), {"cleared": True, "state": "NORMAL"})
        ev = (await self.events())[0]
        self.assertEqual((ev["result"], ev["cleared_by"]), (st.CLEARED_ADMIN, "ADMIN"))

        await self.client.post("/admin/logout", allow_redirects=False)
        self.assertEqual((await self.client.get("/api/admin/overview")).status, 401)


if __name__ == "__main__":
    unittest.main()
