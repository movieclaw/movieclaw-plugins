import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from fastapi import FastAPI
import httpx
from pt_pack import apply, Settings
from pt_pack.workspace import validate_config
from pt_pack.identity import IdentityClient
from .workspace_harness import RuntimeContext
from movieclaw_tracker import list_sites, get_site_config

CONFIG = {"site_id": "fixturept", "display_name": "Fixture PT", "base_url": "https://fixtures.example", "framework": "nexusphp", "selectors": {"torrent_category_css": "td:first-child a::attr(href)"}, "categories": {"movie": [401]}}


class WorkspaceRoutes(unittest.IsolatedAsyncioTestCase):
    async def test_native_sibling_profile_selector_is_importable(self):
        config={**CONFIG,'selectors':{'profile_seeding_css':"img[alt='做种数']::next_sibling_text"}}
        self.assertEqual(validate_config(config)['selectors']['profile_seeding_css'],"img[alt='做种数']::next_sibling_text")

    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.ctx = RuntimeContext(self.folder.name, Settings(hitpt_user_agent="legacy-browser"))
        await self.mount()

    async def mount(self):
        await apply.apply(self.ctx)
        app = FastAPI(); app.include_router(self.ctx.routes)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://testserver", headers={"Origin":"http://testserver"})

    async def asyncTearDown(self):
        await self.client.aclose(); await self.ctx.close(); self.folder.cleanup()

    async def post(self, url, payload):
        workspace = (await self.client.get("/workspace")).json()
        return await self.client.post(url, json={"revision":workspace["revision"],**payload})

    async def test_fresh_server_only_added_sites_and_legacy_identity_migration(self):
        self.assertEqual((await self.client.get("/sites")).json()["sites"], [])
        catalog = (await self.client.get("/catalog")).json()
        self.assertGreater(len(catalog["sites"]), 20)
        ws = (await self.client.get("/workspace")).json()
        self.assertEqual(ws["user_agents"]["hitpt"], "legacy-browser")
        result = await self.post("/workspace/select", {"site_id":"hitpt"})
        self.assertEqual(result.status_code, 200)
        self.assertEqual([s["site_id"] for s in (await self.client.get("/sites")).json()["sites"]], ["hitpt"])
        self.assertEqual(self.ctx.ops.network_calls, [])

    async def test_import_native_auth_search_detail_export_edit_rollback_reload_archive(self):
        response = await self.post("/workspace/import", {"yaml":json.dumps(CONFIG)})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(get_site_config("fixturept").base_url, CONFIG["base_url"])
        self.ctx.ops.accounts["fixturept"] = {"site_id":"fixturept","enabled":True,"status":"pending","usable":False}
        response = await self.client.post("/sites/fixturept/retry")
        self.assertEqual(response.status_code, 200)
        response = await self.client.post("/sites/fixturept/probe?keyword=Fixture")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["search"]["ok"])
        self.assertTrue(response.json()["detail"]["ok"])
        self.assertTrue(response.json()["torrent"]["skipped"])
        self.assertFalse(any(r.url.path=="/download.php" for r in self.ctx.ops.network_calls))
        exported = (await self.client.get("/workspace/export/fixturept")).text
        self.assertNotIn("uid", exported.replace("profile_uid_css", ""))
        self.assertNotIn("fixture-cookie", exported)
        new = {**CONFIG,"display_name":"New version"}
        self.assertEqual((await self.post("/workspace/import", {"yaml":json.dumps(new)})).status_code, 200)
        self.assertEqual(get_site_config("fixturept").display_name, "New version")
        self.assertEqual((await self.post("/workspace/custom/fixturept/rollback", {})).status_code, 200)
        self.assertEqual(get_site_config("fixturept").display_name, "Fixture PT")
        self.assertEqual((await self.post("/workspace/custom/fixturept/disable", {})).status_code, 200)
        self.assertNotIn("fixturept", [s.site_id for s in list_sites()])
        values, accounts = self.ctx.values, self.ctx.ops.accounts
        await self.client.aclose(); await self.ctx.close()
        self.ctx = RuntimeContext(self.folder.name, Settings(), values); self.ctx.ops.accounts=accounts
        await self.mount()
        self.assertFalse((await self.client.get("/workspace")).json()["custom"]["fixturept"]["enabled"])
        self.assertEqual((await self.post("/workspace/custom/fixturept/enable", {})).status_code, 200)
        self.assertIn("fixturept", [s.site_id for s in list_sites()])
        self.assertEqual((await self.post("/workspace/custom/fixturept/archive", {})).status_code, 200)
        self.assertEqual((await self.client.get("/sites")).json()["sites"], [])
        self.assertIn("fixturept", self.ctx.ops.accounts)

    async def test_identity_all_official_and_bundled_clients_and_live_override(self):
        result=await self.post("/workspace/identity", {"user_agent":"global-browser"})
        self.assertEqual(result.status_code, 200)
        for cfg in list_sites():
            with self.subTest(site=cfg.site_id):
                site = await self.ctx.ops.instance(cfg.site_id)
                try:
                    self.assertIsInstance(site.client, IdentityClient)
                    site.client.set_header("X-Api-Key", "fixture-api")
                    await site.client.raw_get("https://fixtures.example/index.php")
                    request=self.ctx.ops.network_calls[-1]
                    self.assertEqual(request.headers["user-agent"], "legacy-browser" if cfg.site_id=="hitpt" else "global-browser")
                    self.assertEqual(request.headers["x-api-key"],"fixture-api")
                    self.assertIn("uid=fixture",request.headers["cookie"])
                finally: await site.client.close()
        site=await self.ctx.ops.instance("btschool")
        try:
            await self.post("/workspace/identity", {"site_id":"btschool","user_agent":"local-browser"})
            await site.client.get("https://fixtures.example/index.php")
            self.assertEqual(self.ctx.ops.network_calls[-1].headers["user-agent"],"local-browser")
            await self.post("/workspace/identity", {"site_id":"btschool","user_agent":""})
            await site.client.get("https://fixtures.example/index.php")
            self.assertEqual(self.ctx.ops.network_calls[-1].headers["user-agent"],"global-browser")
        finally: await site.client.close()

    async def test_failed_persistence_restores_catalog_and_existing_state(self):
        self.assertEqual((await self.post("/workspace/import", {"yaml":json.dumps(CONFIG)})).status_code, 200)
        previous = (await self.client.get("/workspace")).json()
        save = self.ctx.data.set.side_effect
        self.ctx.data.set.side_effect = OSError("fixture storage unavailable")
        with self.assertRaises(OSError):
            await self.post("/workspace/import", {"yaml":json.dumps({**CONFIG,"display_name":"Should not commit"})})
        self.ctx.data.set.side_effect = save
        self.assertEqual(get_site_config("fixturept").display_name, "Fixture PT")
        self.assertEqual((await self.client.get("/workspace")).json(), previous)

    async def test_independent_server_does_not_inherit_sites_or_browser_identity(self):
        await self.post("/workspace/import", {"yaml":json.dumps(CONFIG)})
        await self.post("/workspace/identity", {"user_agent":"server-one-browser"})
        await self.client.aclose(); await self.ctx.close()
        with tempfile.TemporaryDirectory() as other:
            self.ctx = RuntimeContext(other, Settings())
            await self.mount()
            state = (await self.client.get("/workspace")).json()
            self.assertEqual(state["custom"], {})
            self.assertEqual(state["user_agents"], {})
            self.assertEqual(state["default_user_agent"], "")
            self.assertEqual((await self.client.get("/sites")).json()["sites"], [])
            self.assertNotIn("fixturept", [s.site_id for s in list_sites()])

    async def test_native_settings_hidden_while_raw_legacy_config_is_migrated(self):
        self.assertIsNone(apply.config)
        await self.client.aclose(); await self.ctx.close()
        self.ctx = RuntimeContext(self.folder.name, Settings())
        self.ctx.config = {"hitpt_user_agent":"raw-legacy-browser","btschool":False,"management_url":"https://old.example/panel"}
        await self.mount()
        state = (await self.client.get("/workspace")).json()
        self.assertEqual(state["user_agents"]["hitpt"], "raw-legacy-browser")
        self.assertNotIn("btschool", [s.site_id for s in list_sites()])
        self.assertEqual(self.ctx.config["management_url"], "https://old.example/panel")

    async def test_conflicts_credentials_header_injection_and_cas(self):
        self.assertEqual((await self.post("/workspace/import", {"yaml":json.dumps({**CONFIG,"site_id":"hitpt"})})).status_code,400)
        self.assertEqual((await self.post("/workspace/import", {"yaml":json.dumps({**CONFIG,"base_url":"https://www.hitpt.com"})})).status_code,400)
        for config in [{**CONFIG,"custom_class":"evil"},{**CONFIG,"cookie":"secret"},{**CONFIG,"base_url":"https://127.0.0.1"},{**CONFIG,"selectors":{"torrent_row_css":"["}}]:
            self.assertEqual((await self.post("/workspace/import", {"yaml":json.dumps(config)})).status_code,400)
        self.assertEqual((await self.post("/workspace/identity", {"user_agent":"a\r\nCookie: bad"})).status_code,400)
        ws=(await self.client.get("/workspace")).json()
        results=await asyncio.gather(*[self.client.post("/workspace/identity",json={"revision":ws["revision"],"user_agent":s}) for s in ["one","two"]])
        self.assertEqual(sorted(r.status_code for r in results),[200,409])
        self.assertEqual((await self.client.post("/workspace/select",json={"revision":0,"site_id":"hitpt"},headers={"Origin":"https://evil.example"})).status_code,403)

    async def test_assistant_generation_jobs_and_failed_new_job_has_no_stale_draft(self):
        body={"site_url":"https://new-fixture.example","list_html":"<table><tr><td>fixture</td></tr></table>","allow_model":True}
        self.assertEqual((await self.client.post("/assistant/generate",json={**body,"allow_model":False})).status_code,400)
        result=await self.client.post("/assistant/generate",json=body);self.assertEqual(result.status_code,200)
        first=result.json()["id"]; await self.ctx.pending.pop()
        first_job=(await self.client.get("/assistant/jobs/"+first)).json()
        self.assertTrue(first_job["has_draft"])
        self.ctx.agent.ask.assert_awaited_once()
        self.assertEqual(self.ctx.agent.ask.await_args.kwargs["tools"],"none")
        config=first_job["draft"]
        self.assertEqual((await self.post("/workspace/import", {"yaml":json.dumps(config)})).status_code,200)
        self.ctx.agent.ask.return_value.text='{"framework":"unsupported"}'
        result=await self.client.post("/assistant/generate",json={**body,"site_url":"https://other-fixture.example"})
        second=result.json()["id"]; await self.ctx.pending.pop()
        self.assertFalse((await self.client.get("/assistant/jobs/"+second)).json()["has_draft"])
        self.assertTrue((await self.client.get("/assistant/jobs/"+first)).json()["has_draft"])
        self.assertEqual((await self.client.get("/draft")).status_code,404)


class ConfigValidation(unittest.TestCase):
    def test_export_input_does_not_accept_python_or_unsafe_fields(self):
        for text in ['!!python/object/apply:os.system [echo]', '[]', 'null']:
            with self.assertRaises(ValueError): validate_config(text)
        for extra in [{"selectors":{"torrent_list_path":"https://evil.example"}}, {"auth":{"supported":["credential"]}}, {"selectors":{"login_extra_form_data":[["password","secret"]]}}, {"max_retries":True}]:
            with self.assertRaises(ValueError): validate_config({**CONFIG,**extra})
        config=validate_config(CONFIG)
        self.assertEqual(config["auth"],{"supported":["cookie"]})
        self.assertEqual(config["min_request_interval"],3)
