"""라즈베리파이 설치·업데이트 파일 내려주기(server/edge_dist.py) 시험."""

import gzip
import io
import tarfile
import tempfile
import unittest
from pathlib import Path

from server import edge_dist
from server.edge_dist import build_bundle, bundle_files
from server.tests.fakes import TOKEN
from server.tests.test_e2e import ServerTestBase
from shared import protocol as P

REMOTE = {"X-Forwarded-For": "203.0.113.9"}


def tar_names(data: bytes):
    with tarfile.open(fileobj=io.BytesIO(gzip.decompress(data))) as tar:
        return {m.name: m for m in tar.getmembers()}, tar.extractfile("edge/VERSION").read().decode().strip()


class BundleTest(unittest.TestCase):
    def make_repo(self, root: Path) -> None:
        for rel, text in {"edge/client.py": "x = 1\n", "edge/deploy/kiosk.sh": "#!/bin/bash\n",
                          "edge/config.toml": "token = 'secret'\n", "edge/tests/test_a.py": "",
                          "edge/__pycache__/client.cpython-311.pyc": "", "shared/protocol.py": "y = 2\n",
                          "shared/tests/test_p.py": "", "server/app.py": "z = 3\n"}.items():
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)

    def test_bundle_has_only_edge_and_shared_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_repo(root)
            self.assertEqual(bundle_files(root), ["edge/client.py", "edge/deploy/kiosk.sh", "shared/protocol.py"])
            bundle = build_bundle(root)
            members, version = tar_names(bundle.data)
            self.assertEqual(set(members), {"edge/client.py", "edge/deploy/kiosk.sh", "shared/protocol.py",
                                            "edge/VERSION"})  # 설정(토큰)·시험·캐시·서버 코드는 넣지 않는다
            self.assertEqual(version, bundle.version)
            self.assertEqual(members["edge/deploy/kiosk.sh"].mode, 0o755)
            self.assertEqual(build_bundle(root), bundle)  # 같은 코드면 같은 버전·같은 묶음
            (root / "edge/client.py").write_text("x = 2\n")
            self.assertNotEqual(build_bundle(root).version, bundle.version)

    def test_setup_script_gets_server_url_and_lf(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "setup.sh"
            script.write_bytes(b'SERVER_URL="__SERVER_URL__"\r\necho ok\r\n')
            original, edge_dist.SETUP_SCRIPT = edge_dist.SETUP_SCRIPT, script
            try:
                self.assertEqual(edge_dist.setup_script("http://192.168.0.10:8000/"),
                                 'SERVER_URL="http://192.168.0.10:8000"\necho ok\n')
                edge_dist.SETUP_SCRIPT = Path(tmp) / "missing.sh"
                self.assertIsNone(edge_dist.setup_script("http://x"))
            finally:
                edge_dist.SETUP_SCRIPT = original


class EdgeDistServerTest(ServerTestBase):
    async def test_version_bundle_and_welcome(self):
        version = await (await self.client.get("/edge/version")).text()
        self.assertEqual(version, self.server.edge_bundle.version)
        self.assertEqual((await self.client.get("/edge/bundle.tar.gz")).status, 401)  # 장치 토큰 필요
        res = await self.client.get("/edge/bundle.tar.gz", headers=P.bearer(TOKEN))
        self.assertEqual(res.status, 200)
        members, bundled = tar_names(await res.read())
        self.assertEqual(bundled, version)
        self.assertIn("edge/client.py", members)
        self.assertFalse(any(name.startswith(("server/", "edge/tests/")) for name in members))

        edge = await self.connect()
        welcome = await edge.hello("pi-1")
        self.assertEqual(welcome["edge_version"], version)  # edge가 새 버전을 알아채고 업데이트한다

    async def test_admin_edge_setup_command(self):
        self.assertEqual((await self.client.get("/api/admin/edge-setup", headers=REMOTE)).status, 401)
        data = await (await self.client.get("/api/admin/edge-setup")).json()
        # 서버 PC(127.0.0.1)에서 열어도 라즈베리파이가 닿는 공유기 쪽 주소로 알려 준다
        self.assertEqual(data["server_url"], f"http://{edge_dist.lan_ip()}:8000")
        self.assertEqual(data["command"],
                         f"curl -fsSL {data['server_url']}/edge/setup.sh | bash -s -- --token {TOKEN}")


if __name__ == "__main__":
    unittest.main()
