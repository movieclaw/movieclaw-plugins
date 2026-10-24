import unittest
import tempfile
from unittest.mock import patch
from pathlib import Path

import httpx
from fastapi import FastAPI
from pt_pack.management import merge_sites, redacted_error, public_url, management_router, icon_asset


CATALOG = [{"site_id": "officialpt", "display_name": "官方站", "base_url": "https://official.example"},
           {"site_id": "custompt", "display_name": "插件站", "base_url": "https://custom.example"}]
OFFICIAL = [CATALOG[0]]
PACK = [CATALOG[1]]
ACCOUNTS = [{"site_id": "officialpt", "enabled": True, "status": "active", "usable": True,
             "cookie": "must-not-expose", "profile": {"username": "private-name"}},
            {"site_id": "custompt", "enabled": True, "status": "failed", "usable": False,
             "last_error": "token=private-secret"}]


class MergeTests(unittest.TestCase):
    def test_source_and_connection_separate(self):
        rows = {r["site_id"]: r for r in merge_sites(CATALOG, ACCOUNTS, {}, OFFICIAL, PACK, [])}
        self.assertEqual(rows["officialpt"]["source"], "official")
        self.assertTrue(rows["officialpt"]["usable"])
        self.assertEqual(rows["custompt"]["state"], "failed")
        self.assertTrue(rows["custompt"]["configured"])
        self.assertFalse(rows["custompt"]["usable"])
        self.assertNotIn("cookie", rows["officialpt"])
        self.assertNotIn("private-name", str(rows))
        self.assertNotIn("private-secret", str(rows))
        self.assertNotIn("wintersakura", rows)

    def test_no_accounts_not_marked_connected(self):
        rows = merge_sites(CATALOG, [], {}, OFFICIAL, PACK, [])
        self.assertTrue(all(not r["configured"] for r in rows))
        self.assertTrue(all(not r["retry_allowed"] for r in rows))

    def test_paused_verifying_and_missing_adapter(self):
        accounts = [{**ACCOUNTS[0], "enabled": False}, {**ACCOUNTS[1], "status": "verifying"},
                    {"site_id": "removedpt", "enabled": True, "status": "active", "usable": True}]
        rows = {r["site_id"]: r for r in merge_sites(CATALOG, accounts, {}, OFFICIAL, PACK, [])}
        self.assertEqual(rows["officialpt"]["state"], "paused")
        self.assertFalse(rows["officialpt"]["usable"])
        self.assertFalse(rows["custompt"]["retry_allowed"])
        self.assertEqual(rows["removedpt"]["state"], "unavailable")
        self.assertFalse(rows["removedpt"]["usable"])

    def test_official_added_later_takes_source_precedence(self):
        rows = merge_sites(CATALOG, [], {}, CATALOG, PACK, [])
        self.assertEqual(next(r for r in rows if r["site_id"] == "custompt")["source"], "official")

    def test_secrets_and_unsafe_links(self):
        self.assertNotIn("SECRET", redacted_error("https://test.example/a?passkey=SECRET&token=SECRET"))
        self.assertIsNone(public_url("javascript:alert(1)"))
        self.assertIsNone(public_url("https://user:password@test.example"))
        self.assertEqual(public_url("https://test.example/path?token=SECRET"), "https://test.example")

    def test_web_link_uses_website_not_api_and_drops_secrets(self):
        official = [{**OFFICIAL[0], "base_url": "https://api.example", "web_base_url": "https://web.example/path?token=SECRET"}]
        rows = merge_sites(CATALOG, ACCOUNTS, {}, official, PACK, [])
        site = next(r for r in rows if r["site_id"] == "officialpt")
        self.assertEqual(site["website_url"], "https://web.example")
        self.assertNotIn("SECRET", str(site))
        self.assertNotIn("wintersakura", [r["site_id"] for r in rows])
        mteam = {"site_id": "mteam", "base_url": "https://api.m-team.cc", "web_base_url": "https://tp.m-team.cc"}
        self.assertEqual(merge_sites([mteam], [], {}, [mteam], [], [])[0]["website_url"], "https://kp.m-team.cc")

    def test_icon_assets_are_local_and_id_scoped(self):
        for site_id in ("../panel", "../../config", "x.ico", "https://evil.example/a", ""):
            self.assertIsNone(icon_asset(site_id))
        with tempfile.TemporaryDirectory() as folder, patch("pt_pack.management.ASSETS", Path(folder)):
            icons = Path(folder) / "icons"
            icons.mkdir()
            (icons / "custompt.png").write_bytes(b"test-raster")
            rows = merge_sites(CATALOG, ACCOUNTS, {}, OFFICIAL, PACK, [])
            self.assertEqual(next(r for r in rows if r["site_id"] == "custompt")["icon_url"], "/api/v1/plugins/pt-site-pack/icons/custompt")


class FakeOps:
    def __init__(self):
        self.calls = []
        self.accounts = ACCOUNTS
        self.stats_failure = False

    async def call(self, name, args):
        self.calls.append((name, args))
        if name == "site.catalog": return CATALOG
        if name == "site.list": return self.accounts
        if name == "site.stats":
            if self.stats_failure: raise RuntimeError("stats offline")
            return {}
        if name == "site.verify": return {"status": "verifying"}
        if name == "search.torrents":
            from pt_pack.diagnostics import TICKETS, REPORTS, PREFIX
            ticket = TICKETS.pop(args["keyword"][len(PREFIX):])
            if ticket["site_id"] != "custompt" or args["sites"] != ["custompt"] or args["no_history"] is not True:
                raise AssertionError("Probe escaped its scope")
            REPORTS["custompt"] = {"site_id": "custompt", "search": {"ok": True}, "detail": {"ok": True}, "torrent": {"ok": True}}
            return {"sites": []}
        raise AssertionError("Unexpected operation: " + name)


class FakeService:
    def __init__(self, ops): self.ops = ops
    async def client(self, ctx): return self.ops


class FakeContext:
    def __init__(self, ops): self.ops = ops
    def use(self, key): return FakeService(self.ops)


class RoutesTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.ops = FakeOps()
        app = FastAPI()
        app.include_router(management_router(FakeContext(self.ops), OFFICIAL, PACK, []), prefix="/api/v1/plugins/pt-site-pack")
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://testserver")
        self.prefix = "/api/v1/plugins/pt-site-pack"

    async def asyncTearDown(self): await self.client.aclose()

    async def test_panel_and_snapshot(self):
        panel = await self.client.get(self.prefix + "/panel")
        self.assertEqual(panel.status_code, 200)
        self.assertIn("frame-ancestors 'none'", panel.headers["content-security-policy"])
        self.assertEqual(panel.headers["cache-control"], "no-store")
        self.assertIn("我的站点", panel.text)
        state = (await self.client.get(self.prefix + "/sites")).json()
        self.assertEqual(state["counts"]["configured"], 2)
        self.assertEqual(state["counts"]["failed"], 1)
        self.assertTrue(all(c[0] != "site.verify" for c in self.ops.calls))

    async def test_native_page_has_single_document_and_top_level_settings_links(self):
        source = Path(__file__).parents[1]
        panel = (await self.client.get(self.prefix + "/panel")).text
        self.assertEqual(panel, (source / "web/index.html").read_text())
        self.assertNotIn("<iframe", panel)
        self.assertIn('id="assistant-pane"', panel)
        self.assertNotIn('href="/settings/plugins/pt-site-pack"', panel)
        self.assertIn('href="/api/v1/plugins/pt-site-pack/panel.css"', panel)

    async def test_icon_route_only_reads_packaged_image(self):
        self.assertEqual((await self.client.get(self.prefix + "/icons/no-such-site")).status_code, 404)
        with tempfile.TemporaryDirectory() as folder, patch("pt_pack.management.ASSETS", Path(folder)):
            icons = Path(folder) / "icons"
            icons.mkdir()
            (icons / "custompt.png").write_bytes(b"test-raster")
            response = await self.client.get(self.prefix + "/icons/custompt")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.content, b"test-raster")
            self.assertEqual(response.headers["content-type"], "image/png")
            self.assertIn("private", response.headers["cache-control"])
            self.assertIn("img-src 'self'", response.headers["content-security-policy"])
        self.assertEqual(self.ops.calls, [])

    async def test_csrf_and_get_cannot_trigger_verify(self):
        target = self.prefix + "/sites/custompt/retry"
        self.assertEqual((await self.client.get(target)).status_code, 405)
        self.assertEqual((await self.client.post(target)).status_code, 403)
        self.assertEqual((await self.client.post(target, headers={"Origin": "http://evil.example"})).status_code, 403)
        self.assertEqual((await self.client.post(target, headers={"Origin": "http://testserver", "Sec-Fetch-Site": "cross-site"})).status_code, 403)
        self.assertEqual(self.ops.calls, [])

    async def test_manual_retry_and_cooldown(self):
        target = self.prefix + "/sites/custompt/retry"
        response = await self.client.post(target, headers={"Origin": "http://testserver"}, json={})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["verification"], "verifying")
        self.assertEqual(self.ops.calls.count(("site.verify", {"site_id": "custompt"})), 1)
        self.assertEqual((await self.client.post(target, headers={"Authorization": "Bearer test-cli"})).status_code, 429)

    async def test_unconfigured_and_verifying_rejected(self):
        self.ops.accounts = []
        target = self.prefix + "/sites/custompt/retry"
        self.assertEqual((await self.client.post(target, headers={"Origin": "http://testserver"})).status_code, 409)
        self.ops.accounts = [{**ACCOUNTS[1], "status": "verifying"}]
        self.assertEqual((await self.client.post(target, headers={"Origin": "http://testserver"})).status_code, 409)
        self.assertTrue(all(c[0] != "site.verify" for c in self.ops.calls))

    async def test_stats_failure_does_not_fake_status(self):
        self.ops.stats_failure = True
        state = (await self.client.get(self.prefix + "/sites")).json()
        self.assertIsNotNone(state["warning"])
        self.assertEqual(state["counts"]["failed"], 1)

    async def test_probe_is_explicit_scoped_and_rate_limited(self):
        target = self.prefix + "/sites/custompt/probe?keyword=Sample"
        self.assertEqual((await self.client.get(target)).status_code, 405)
        self.assertEqual((await self.client.post(target)).status_code, 403)
        headers = {"Origin": "http://testserver"}
        self.assertEqual((await self.client.post(target, headers=headers)).status_code, 409)
        self.ops.accounts = [{**ACCOUNTS[1], "status": "active", "usable": True}]
        self.assertEqual((await self.client.post(self.prefix + "/sites/officialpt/probe?keyword=Sample", headers=headers)).status_code, 409)
        self.assertEqual((await self.client.post(target, headers=headers)).status_code, 200)
        self.assertEqual((await self.client.post(target, headers=headers)).status_code, 429)
        self.assertTrue(all(name in ("site.catalog", "site.list", "site.stats", "search.torrents") for name, args in self.ops.calls))
        self.assertEqual(len([c for c in self.ops.calls if c[0] == "search.torrents"]), 1)


if __name__ == "__main__": unittest.main()
