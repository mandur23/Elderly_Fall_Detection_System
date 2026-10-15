"""보호자 계정·로그인·보호자 웹 시험."""

import asyncio
import tempfile
import unittest
from pathlib import Path

from server import app as app_mod
from server import store as st
from server.app import ADMIN_HEADER, GUARDIAN_COOKIE, GUARDIAN_HEADER
from server.guardians import (GuardianError, Guardians, LoginLimiter, hash_password, verify_password)
from server.notifier import alert_message
from server.store import Store
from server.tests.test_e2e import ServerTestBase


class PasswordTest(unittest.TestCase):
    def test_hash_and_verify(self):
        stored = hash_password("correct horse", iterations=1000)
        self.assertTrue(stored.startswith("pbkdf2_sha256$1000$"))
        self.assertNotIn("correct horse", stored)
        self.assertTrue(verify_password("correct horse", stored))
        self.assertFalse(verify_password("wrong", stored))
        self.assertFalse(verify_password("x", "garbage"))
        self.assertNotEqual(hash_password("same", iterations=1000), hash_password("same", iterations=1000))  # 소금


class GuardiansTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "e.db")
        self.g = Guardians(self.store)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_validation(self):
        with self.assertRaises(GuardianError):
            self.g.create("A!", "이름", "password1", [])
        with self.assertRaises(GuardianError):
            self.g.create("daughter", "큰딸", "short", [])
        self.g.create("Daughter", "큰딸", "password1", ["pi-1", "pi-1", " pi-2 "])
        with self.assertRaises(GuardianError):
            self.g.create("daughter", "중복", "password1", [])
        listed = self.g.list()
        self.assertEqual((listed[0]["username"], listed[0]["devices"]), ("daughter", ["pi-1", "pi-2"]))
        self.assertNotIn("password_hash", listed[0])

    def test_sessions(self):
        created = self.g.create("son", "아들", "password1", ["pi-1"])
        guardian = self.g.authenticate("son", "password1")
        self.assertIsNotNone(guardian)
        self.assertIsNone(self.g.authenticate("son", "nope"))
        self.assertIsNone(self.g.authenticate("nobody", "password1"))

        token = self.g.session_token(guardian, now=1000.0)
        self.assertEqual(self.g.from_session(token, now=1001.0)["username"], "son")
        self.assertIsNone(self.g.from_session(token[:-1] + ("0" if token[-1] != "0" else "1"), now=1001.0))  # 위조
        self.assertIsNone(self.g.from_session(token, now=1000.0 + 31 * 24 * 3600))  # 만료
        self.assertIsNone(self.g.from_session("1.1.99999999999", now=1001.0))

        # 서버를 다시 켜도(새 Guardians) 서명 키가 같아 로그인이 유지된다
        self.assertIsNotNone(Guardians(self.store).from_session(token, now=1001.0))

        self.g.update(created["id"], password="new-password")  # 비밀번호를 바꾸면 기존 로그인은 끊긴다
        self.assertIsNone(self.g.from_session(token, now=1001.0))
        new_token = self.g.session_token(self.g.authenticate("son", "new-password"), now=1000.0)
        self.g.delete(created["id"])
        self.assertIsNone(self.g.from_session(new_token, now=1001.0))

    def test_login_limiter(self):
        limiter = LoginLimiter(max_failures=3, window=60, lock=120)
        for t in (0, 1, 2):
            self.assertEqual(limiter.blocked_for("1.2.3.4", "son", now=t), 0)
            limiter.failed("1.2.3.4", "son", now=t)
        self.assertGreater(limiter.blocked_for("1.2.3.4", "son", now=3), 100)
        self.assertGreater(limiter.blocked_for("5.6.7.8", "son", now=3), 100)  # 다른 곳에서 같은 아이디도 막음
        self.assertEqual(limiter.blocked_for("5.6.7.8", "other", now=3), 0)
        self.assertEqual(limiter.blocked_for("1.2.3.4", "son", now=200), 0)  # 잠금 시간이 지나면 풀림

    def test_alert_message_link(self):
        event = {"started_at": "2026-10-15T10:00:00.000", "result": st.ALERT_NO_REPLY}
        self.assertIn("보호자 웹: http://pc:8000/g", alert_message(event, location="거실", link="http://pc:8000/g"))
        self.assertNotIn("보호자 웹", alert_message(event, location="거실"))


class GuardianWebTest(ServerTestBase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self._delay, app_mod.ADMIN_LOGIN_FAIL_DELAY = app_mod.ADMIN_LOGIN_FAIL_DELAY, 0.0

    async def asyncTearDown(self):
        app_mod.ADMIN_LOGIN_FAIL_DELAY = self._delay
        await super().asyncTearDown()

    async def admin(self, method, path, json=None):
        return await self.client.request(method, path, json=json, headers={ADMIN_HEADER: "1"})

    async def login(self, username, password):
        return await self.client.post("/g/login", data={"username": username, "password": password},
                                      allow_redirects=False)

    async def test_guardian_sees_only_assigned_device_and_clears(self):
        res = await self.admin("POST", "/api/admin/guardians", {
            "username": "daughter", "name": "큰딸", "password": "password1", "devices": ["pi-1"]})
        self.assertEqual(res.status, 201)
        gid = (await res.json())["id"]
        res = await self.admin("POST", "/api/admin/guardians", {"username": "x", "password": "password1"})
        self.assertEqual(res.status, 400)  # 아이디가 너무 짧음
        self.assertIn("아이디", (await res.json())["error"])

        self.assertEqual((await self.client.get("/g", allow_redirects=False)).headers["Location"], "/g/login")
        self.assertEqual((await self.client.get("/api/g/overview")).status, 401)
        res = await self.login("daughter", "wrong-pass")
        self.assertEqual(res.headers["Location"], "/g/login?error=1")
        res = await self.login("daughter", "password1")
        self.assertEqual(res.headers["Location"], "/g")
        self.assertIn(GUARDIAN_COOKIE, res.cookies)
        page = await self.client.get("/g")
        self.assertEqual(page.status, 200)
        self.assertEqual(page.headers["X-Frame-Options"], "DENY")  # 다른 사이트가 틀에 넣지 못하게

        mine, other = await self.connect(), await self.connect()
        await mine.hello("pi-1", name="거실")
        await other.hello("pi-2", name="안방")
        await asyncio.gather(mine.status(), other.status())
        await mine.frames(4.0)
        for _ in range(100):
            data = await (await self.client.get("/api/g/overview")).json()
            if data["devices"][0]["view"] == "suspect":
                break
            await asyncio.sleep(0.02)
        self.assertEqual([d["device_id"] for d in data["devices"]], ["pi-1"])  # 맡은 장치만
        self.assertEqual((data["guardian"]["name"], data["devices"][0]["name"]), ("큰딸", "거실"))

        res = await self.client.post("/api/g/clear?device=pi-1")
        self.assertEqual(res.status, 403)  # 보호자 웹 스크립트 헤더 없이는 거절
        res = await self.client.post("/api/g/clear?device=pi-2", headers={GUARDIAN_HEADER: "1"})
        self.assertEqual(res.status, 403)  # 맡지 않은 장치
        res = await self.client.post("/api/g/clear?device=pi-1", headers={GUARDIAN_HEADER: "1"})
        self.assertEqual(await res.json(), {"cleared": True, "state": "NORMAL"})
        ev = (await self.events())[0]
        self.assertEqual((ev["result"], ev["cleared_by"]), (st.CLEARED_GUARDIAN, "GUARDIAN:daughter"))

        # 해제 권한을 빼면 해제할 수 없고, 비밀번호를 바꾸면 기존 로그인이 끊긴다
        await self.admin("POST", f"/api/admin/guardians/{gid}", {"can_clear": False})
        res = await self.client.post("/api/g/clear?device=pi-1", headers={GUARDIAN_HEADER: "1"})
        self.assertEqual(res.status, 403)
        await self.admin("POST", f"/api/admin/guardians/{gid}", {"password": "another-pass"})
        self.assertEqual((await self.client.get("/api/g/overview")).status, 401)

        listed = await (await self.client.get("/api/admin/guardians")).json()
        self.assertEqual(listed["guardians"][0]["can_clear"], False)
        self.assertEqual({d["device_id"] for d in listed["devices"]}, {"pi-1", "pi-2"})
        self.assertEqual((await self.admin("DELETE", f"/api/admin/guardians/{gid}")).status, 200)
        self.assertEqual((await self.admin("DELETE", f"/api/admin/guardians/{gid}")).status, 404)

    async def test_admin_api_needs_action_header(self):
        res = await self.client.post("/api/admin/guardians", json={"username": "abc", "password": "password1"})
        self.assertEqual(res.status, 403)

    async def test_repeated_failures_lock_login(self):
        await self.admin("POST", "/api/admin/guardians", {"username": "son", "password": "password1"})
        for _ in range(5):
            await self.login("son", "bad-password")
        res = await self.login("son", "password1")  # 맞는 비밀번호라도 잠긴 동안은 거절
        self.assertEqual(res.headers["Location"], "/g/login?error=locked")


if __name__ == "__main__":
    unittest.main()
