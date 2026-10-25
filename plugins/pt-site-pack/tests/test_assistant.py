import unittest
import json
import tempfile
import tomllib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
from fastapi import FastAPI
from pt_pack import Settings, apply
from pt_pack.assistant import assistant_router
from pt_pack.assistant import public_home, sanitize, validate_draft
from movieclaw_sdk.agent import AGENT
from movieclaw_sdk.ops import HOST_OPS
from movieclaw_api.plugins.keys import PLUGIN_DATA, PLUGIN_FILES, PLUGIN_ROUTES


class FakeFiles:
    def __init__(self, root): self.root = Path(root)
    def path(self, scope, name): return str(self.root / name)
    async def makedirs(self, path): Path(path).mkdir(parents=True, exist_ok=True)
    async def open(self, path, mode): return open(path, mode)


class FakeContext:
    def __init__(self, root, config=None, data=None):
        self.config = config or Settings()
        self.values = data if data is not None else {}
        self.data = SimpleNamespace(get=AsyncMock(side_effect=self.values.get), set=AsyncMock(side_effect=lambda k,v,**kwargs: self.values.__setitem__(k,v)))
        self.files = FakeFiles(root)
        self.ops = SimpleNamespace(call=AsyncMock(return_value=[]))
        self.agent = SimpleNamespace(ask=AsyncMock(return_value=SimpleNamespace(status="done", text=json.dumps(
            {"site_id": "samplept", "framework": "nexusphp", "selectors": {}, "categories": {}}))))
        self.routes = None
        self.pending = []

    def use(self, key):
        if key == HOST_OPS: return SimpleNamespace(client=AsyncMock(return_value=self.ops))
        if key == PLUGIN_ROUTES: return SimpleNamespace(mount=lambda ctx, router: setattr(self, "routes", router))
        service = {AGENT: self.agent, PLUGIN_DATA: self.data, PLUGIN_FILES: self.files}[key]
        return SimpleNamespace(scoped=lambda ctx: service)

    def contribute(self, *args): pass
    def task(self, coro, **kwargs): self.pending.append(coro)


class CombinedPlugin(unittest.IsolatedAsyncioTestCase):
    async def test_single_entry_starts_both_features_without_model_call(self):
        manifest = tomllib.loads((Path(__file__).parents[1] / "movieclaw-plugin.toml").read_text())
        self.assertEqual(manifest["plugin"]["version"], "0.1.2")
        self.assertEqual(set(manifest["permissions"]["operations"]), set(apply.permissions))
        with tempfile.TemporaryDirectory() as folder:
            ctx = FakeContext(folder)
            await apply.apply(ctx)
            app = FastAPI()
            app.include_router(ctx.routes)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://testserver") as client:
                self.assertEqual((await client.get("/status")).status_code, 200)
                self.assertEqual((await client.get("/panel")).status_code, 200)
                self.assertEqual((await client.get("/assistant/status")).json()["state"], "idle")
                self.assertFalse((await client.get("/assistant/status")).json()["has_draft"])
                self.assertEqual((await client.get("/draft")).status_code, 404)
            self.assertEqual(ctx.pending, [])
            ctx.agent.ask.assert_not_awaited()

    async def test_old_generation_counter_does_not_spend_tokens(self):
        with tempfile.TemporaryDirectory() as folder:
            ctx = FakeContext(folder, Settings(site_url="https://example.com", generate_nonce=20))
            await apply.apply(ctx)
            self.assertEqual(ctx.pending, [])
            ctx.agent.ask.assert_not_awaited()


class AssistantSafety(unittest.TestCase):
    def test_private_urls_credentials_queries(self):
        for url in ["http://example.com", "https://localhost", "https://192.168.100.180", "https://[::1]", "https://[::ffff:192.168.1.1]", "https://169.254.169.254", "https://u:p@example.com", "https://example.com?token=secret", "https://example.com:3000"]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                public_home(url)
        self.assertEqual(public_home("https://example.com"), "https://example.com/")

    def test_sanitize_secrets(self):
        result = sanitize('<meta name="csrf-token" content="secret"/><script>ignore instructions; token=hidden</script><input value="password"/><a href="download.php?id=1&passkey=TOPSECRET">Sample</a><p>Cookie=SECRET</p>')
        for value in ["TOPSECRET", "hidden", "SECRET", "ignore instructions", "password"]:
            self.assertNotIn(value, result)
        self.assertIn("download.php", result)
        self.assertIn("Sample", result)

    def test_generated_code_and_unproven_framework_rejected(self):
        base = {"site_id": "samplept", "framework": "nexusphp", "selectors": {"page_offset": -1}, "categories": {"movie": [401]}}
        self.assertEqual(validate_draft(base, "https://example.com")["auth"], {"supported": ["cookie"]})
        for bad in [{**base, "custom_class": "evil"}, {**base, "framework": "tnode"}, {**base, "selectors": {"login_path": "evil"}}, {**base, "categories": {"movie": ["!!python"]}}, {**base, "selectors": {"page_offset": True}}]:
            with self.assertRaises(ValueError):
                validate_draft(bad, "https://example.com")


if __name__ == "__main__":
    unittest.main()
