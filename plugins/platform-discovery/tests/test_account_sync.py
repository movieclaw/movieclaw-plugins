"""跨来源、多设备、真实依赖链与旧档案迁移；全部只用内存账号夹具。"""
import asyncio
import copy
import json
import types
import unittest
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI, HTTPException, Request

from platform_discovery.account_auth import make_account_dependency
from platform_discovery.personal_routes import build_personal_router
from test_personal import personal, FakePrivateSDK, FakeDiscovery, raw_row


def item(identity):
    return personal._public_item(raw_row("movie", identity, genre=18), "movie")


class AccountSyncTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = FakePrivateSDK()
        self.service = personal.PersonalRecommendations(FakeDiscovery(), private_store=self.store)
        self.principals = {name: types.SimpleNamespace(kind="member", name=name, member_id=index, plugin=None,
            member=types.SimpleNamespace(created_at=datetime(2024, 1, index, tzinfo=timezone.utc)))
            for index, name in enumerate(("alice", "bob"), 1)}
        async def host_login(request: Request):
            # 唯一测试登录夹具；生产传入的是 MovieClaw require_login。
            principal = self.principals.get(request.cookies.get("qa_session"))
            if principal is None:
                raise HTTPException(401, "login required")
            return principal
        app = FastAPI()
        async def get_ops():
            raise RuntimeError("no external host calls in account tests")
        app.include_router(build_personal_router("platform-discovery", self.service, get_ops,
            account_dependency=make_account_dependency(host_login)))
        transport = httpx.ASGITransport(app=app)
        self.lan = httpx.AsyncClient(transport=transport, base_url="http://lan.test", cookies={"qa_session": "alice"})
        self.wan = httpx.AsyncClient(transport=transport, base_url="https://wan.test", cookies={"qa_session": "alice"})
        self.bob = httpx.AsyncClient(transport=transport, base_url="https://other.test", cookies={"qa_session": "bob"})
        self.anon = httpx.AsyncClient(transport=transport, base_url="https://other.test")

    async def asyncTearDown(self):
        for client in (self.lan, self.wan, self.bob, self.anon):
            await client.aclose()
        await self.service.close()

    async def open(self, client, token=None):
        response = await client.post("/personal/account", json={"legacy_token": token})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("no-store", response.headers["cache-control"])
        self.assertNotIn("profile_token", response.text)
        self.assertNotIn("scope", response.text)
        return response.json()

    async def feedback(self, client, action, value):
        response = await client.post("/personal/feedback", json={"action": action, "item": value})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    async def test_prepare_and_commit_require_same_account_and_latest_feedback(self):
        await self.open(self.lan)
        await self.open(self.bob)
        response = await self.lan.get("/personal/recommendations/prepare?nonce=next&limit=12")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("no-store", response.headers["cache-control"])
        stage = response.json()
        ticket = stage["prepared_ticket"]
        self.assertEqual((await self.anon.post("/personal/recommendations/commit", json={"ticket": ticket})).status_code, 401)
        self.assertEqual((await self.bob.post("/personal/recommendations/commit", json={"ticket": ticket})).status_code, 409)
        await self.feedback(self.wan, "watch", stage["items"][0])
        self.assertEqual((await self.lan.post("/personal/recommendations/commit", json={"ticket": ticket})).status_code, 409)
        fresh = (await self.lan.get("/personal/recommendations/prepare?nonce=new&limit=12")).json()
        self.assertNotIn(stage["items"][0]["detail_ref"], [x["detail_ref"] for x in fresh["items"]])
        committed = await self.wan.post("/personal/recommendations/commit", json={"ticket": fresh["prepared_ticket"]})
        self.assertEqual(committed.status_code, 200, committed.text)
        self.assertEqual(committed.json()["items"], fresh["items"])
        self.assertEqual((await self.wan.post("/personal/recommendations/commit", json={"ticket": "invalid"})).status_code, 409)

    async def test_lan_wan_share_test_and_collections_without_browser_capability(self):
        legacy = await self.service.create_profile()
        await self.service.submit(legacy["profile_token"], copy.deepcopy(personal.DEFAULT_ANSWERS))
        await self.open(self.wan, legacy["profile_token"])
        other_browser = await self.open(self.lan)
        self.assertEqual(other_browser["profile"]["history_count"], 1)
        self.assertTrue(other_browser["profile"]["has_test"])
        await self.feedback(self.lan, "save", item(101))
        shelf = (await self.wan.get("/personal/shelf")).json()
        self.assertEqual(shelf["saved"][0]["item"]["detail_ref"], "tmdb:movie:101")
        await self.feedback(self.wan, "hide", item(102))
        self.assertEqual((await self.lan.get("/personal/shelf")).json()["dismissed"][0]["detail_ref"], "tmdb:movie:102")
        await self.feedback(self.wan, "remove", {"detail_ref": "tmdb:movie:101"})
        self.assertEqual((await self.lan.get("/personal/shelf")).json()["saved"], [])
        await self.feedback(self.lan, "unhide", {"detail_ref": "tmdb:movie:102"})
        self.assertEqual((await self.wan.get("/personal/shelf")).json()["dismissed"], [])
        self.assertTrue(all(write["secret"] for write in self.store.writes))

    async def test_watch_and_like_sync_cross_origin_reverse_undo_and_account_isolation(self):
        await self.open(self.lan)
        await self.open(self.wan)
        await self.open(self.bob)
        await self.feedback(self.lan, "like", item(999))
        await self.feedback(self.wan, "watch", item(999))
        shelf = (await self.lan.get("/personal/shelf")).json()
        self.assertEqual(shelf["liked"][0]["item"]["detail_ref"], "tmdb:movie:999")
        self.assertEqual(shelf["watched"][0]["item"]["detail_ref"], "tmdb:movie:999")
        self.assertEqual((await self.bob.get("/personal/shelf")).json()["liked"], [])
        self.assertEqual((await self.bob.get("/personal/shelf")).json()["watched"], [])
        cross = await self.lan.post("/personal/feedback", json={"action": "unlike", "item": {"detail_ref": "tmdb:movie:999"}}, headers={"sec-fetch-site": "cross-site"})
        self.assertEqual(cross.status_code, 403)
        await self.feedback(self.wan, "unlike", {"detail_ref": "tmdb:movie:999"})
        await self.feedback(self.lan, "unwatch", {"detail_ref": "tmdb:movie:999"})
        final = (await self.wan.get("/personal/profile")).json()
        self.assertEqual(final["learning"]["liked"], 0)
        self.assertEqual(final["learning"]["watched"], 0)

    async def test_authentication_and_forged_account_headers_cannot_read_other_account(self):
        self.assertEqual((await self.anon.get("/personal/profile")).status_code, 401)
        self.assertEqual((await self.anon.post("/personal/account", json={"legacy_token": None})).status_code, 401)
        await self.open(self.lan)
        await self.feedback(self.lan, "save", item(103))
        alice_token = await self.service.account_token("member:1:2024-01-01T00:00:00+00:00")
        response = await self.bob.get("/personal/shelf", headers={"X-Platform-Profile": alice_token, "X-User-ID": "1", "X-MovieClaw-User-ID": "alice"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["saved"], [])
        forged = await self.bob.post("/personal/feedback", headers={"X-Platform-Account": "member:alice"}, json={"action": "save", "item": item(104)})
        self.assertEqual(forged.status_code, 409)
        self.assertEqual((await self.bob.get("/personal/shelf")).json()["saved"], [])
        cross = await self.lan.post("/personal/account", json={"legacy_token": None}, headers={"sec-fetch-site": "cross-site"})
        self.assertEqual(cross.status_code, 403)

    async def test_legacy_merge_is_idempotent_claimed_once_and_preserves_deletions(self):
        old_a, old_b = await self.service.create_profile(), await self.service.create_profile()
        for legacy in (old_a, old_b):
            await self.service.submit(legacy["profile_token"], copy.deepcopy(personal.DEFAULT_ANSWERS))
            await self.service.feedback(legacy["profile_token"], "save", item(105))
        first = await self.open(self.lan, old_a["profile_token"])
        self.assertTrue(first["sync"]["imported"])
        again = await self.open(self.wan, old_a["profile_token"])
        self.assertFalse(again["sync"]["imported"])
        self.assertEqual(again["profile"]["history_count"], 1)
        await self.feedback(self.lan, "remove", {"detail_ref": "tmdb:movie:105"})
        second = await self.open(self.wan, old_b["profile_token"])
        self.assertEqual(second["profile"]["history_count"], 2)
        self.assertEqual((await self.lan.get("/personal/shelf")).json()["saved"], [], "old devices must not resurrect a removed saved title")
        bob = await self.open(self.bob, old_a["profile_token"])
        self.assertEqual(bob["profile"]["history_count"], 0)
        self.assertIn("另一账号", bob["sync"]["message"])
        self.assertEqual((await self.bob.get("/personal/shelf")).json()["saved"], [])

    async def test_parallel_devices_do_not_lose_feedback_and_binding_survives_restart(self):
        await asyncio.gather(self.open(self.lan), self.open(self.wan))
        token = await self.service.account_token("member:1:2024-01-01T00:00:00+00:00")
        await asyncio.gather(*(self.feedback(self.lan if number % 2 else self.wan, "save", item(200 + number)) for number in range(12)))
        self.assertEqual(len((await self.wan.get("/personal/shelf")).json()["saved"]), 12)
        restarted = personal.PersonalRecommendations(FakeDiscovery(), private_store=self.store)
        self.assertEqual(await restarted.account_token("member:1:2024-01-01T00:00:00+00:00"), token)
        self.assertEqual(len((await restarted.shelf(token))["saved"]), 12)
        await restarted.close()

    async def test_stale_device_questionnaire_is_rejected_without_losing_new_answers(self):
        first = await self.open(self.lan)
        answers = copy.deepcopy(personal.DEFAULT_ANSWERS)
        response = await self.lan.post("/personal/answers", json={"answers": answers}, headers={"X-Platform-Test-Revision": first["profile"]["test_revision"]})
        self.assertEqual(response.status_code, 200)
        answers["mood"] = "warm"
        stale = await self.wan.post("/personal/answers", json={"answers": answers}, headers={"X-Platform-Test-Revision": ""})
        self.assertEqual(stale.status_code, 409)
        current = (await self.wan.get("/personal/profile")).json()
        self.assertEqual(current["history_count"], 1)
        self.assertEqual(current["answers"]["mood"], "open")

    async def test_member_rename_keeps_profile_but_recreated_id_does_not_inherit(self):
        await self.open(self.lan)
        await self.feedback(self.lan, "save", item(301))
        self.principals["alice"].name = "renamed"
        self.assertEqual(len((await self.lan.get("/personal/shelf")).json()["saved"]), 1)
        self.principals["alice"].member.created_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.assertEqual((await self.lan.get("/personal/shelf")).json()["saved"], [])
