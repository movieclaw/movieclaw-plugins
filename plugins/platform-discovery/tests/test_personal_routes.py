"""Private capability handling and public detail projection; no live calls."""
import asyncio
import copy
import importlib.util
import json
import unittest
import types
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request


spec = importlib.util.spec_from_file_location("screening_personal_routes_tests", Path(__file__).parents[1] / "platform_discovery/personal_routes.py")
routes = importlib.util.module_from_spec(spec)
spec.loader.exec_module(routes)
TOKEN_A, TOKEN_B = "A" * 43, "B" * 43


class ProfileError(ValueError):
    def __init__(self, status):
        super().__init__("private diagnostic at http://proxy-user:private-password@127.0.0.1")
        self.status = status


class FakePersonal:
    from test_personal import personal as _core
    valid_wall_filters = staticmethod(_core.valid_wall_filters)
    async def sync_subscriptions(self, token, refs):
        self.calls.append(("subscriptions", token, refs))
        return {"learning": {"subscriptions": len(refs)}, "profile_token": token}

    async def learning(self, token, enabled):
        self.calls.append(("learning", token, enabled))
        return {"learning": {"enabled": enabled}}

    async def shelf(self, token):
        return {"saved": [], "dismissed": [], "profile_token": token}

    async def feedback(self, token, action, item, *, undoable=False):
        self.calls.append(("feedback", token, action, item))
        return {"saved": [], "dismissed": []}

    def __init__(self):
        self.calls = []
        self.failure = None

    async def create_profile(self):
        return {"profile_token": TOKEN_A, "profile": {"has_test": False, "scope": "must-not-escape", "profile_token": TOKEN_A}}

    async def questionnaire(self):
        return {"version": 1, "questions": [{"id": "mood", "options": [{"id": "quiet", "label": "安静"}]}]}

    async def get_profile(self, token):
        self.calls.append(("get", token))
        if self.failure:
            raise self.failure
        if token not in {TOKEN_A, TOKEN_B}:
            raise ProfileError(404)
        return {"has_test": True, "answers": {"mood": "quiet" if token == TOKEN_A else "active"},
                "profile_token": token, "token_hash": "private-scope", "nested": {"library_status": {"hidden": 1}}}

    async def submit(self, token, answers):
        self.calls.append(("submit", token, answers))
        if self.failure:
            raise self.failure
        return {"has_test": True, "answers": answers, "token": token}

    async def recommend(self, token, nonce="0", limit=48, **filters):
        self.filters = filters
        self.calls.append(("recommend", token, nonce, limit))
        if self.failure:
            raise self.failure
        return {"items": [{"title": "公开作品", "library_status": {"hidden": 1}}], "nonce": nonce}


def title_fixture():
    return {"title": {"title_ref": "tmdb:movie:550", "provider": "tmdb", "external_id": "550", "media_type": "movie",
                      "title": "搏击俱乐部", "original_title": "Fight Club", "release_year": 1999,
                      "provider_rating": 8.4, "genres": ["剧情", "悬疑"], "extent_label": "139 分钟",
                      "overview": "公开剧情简介", "poster_url": "https://image.tmdb.org/t/p/w500/poster.jpg",
                      "backdrop_url": "https://image.tmdb.org/t/p/w1280/backdrop.jpg",
                      "library_status": {"library_id": 123, "hidden_title": "私密库存"}},
            "metadata": {"directors": ["大卫·芬奇"], "cast": [{"name": "演员", "role": "角色", "avatar_url": "https://image.tmdb.org/t/p/w185/actor.jpg", "tmdb_person_id": 100,
                                                                 "library_links": [{"path": "/private"}]}],
                         "director_credits": [{"name": "大卫·芬奇", "role": "Director", "avatar_url": None}],
                         "country": "美国", "language": "英语", "released": "1999-10-15", "network": None,
                         "library_status": {"private": 1}},
            "posters": [{"preview_url": "https://image.tmdb.org/t/p/w342/poster.jpg", "full_url": "https://image.tmdb.org/t/p/original/poster.jpg", "width": 1000, "height": 1500, "secret": "must-not-escape"}],
            "backdrops": [], "library_links": [{"file_path": "/private/media/folder"}],
            "recommendations": [{"title": "不能透传", "library_status": {"private": 1}}], "debug": "private-password"}


class PublicDetailProjectionTests(unittest.TestCase):
    def test_whitelist_retains_metadata_and_never_returns_nested_inventory(self):
        result = routes.public_title_details({"data": title_fixture()}, "tmdb:movie:550")
        self.assertEqual(result["title"], "搏击俱乐部")
        self.assertEqual(result["runtime_minutes"], 139)
        self.assertEqual(result["directors"], ["大卫·芬奇"])
        self.assertEqual(result["cast"][0]["character"], "角色")
        self.assertEqual(result["cast"][0]["profile_url"], "https://image.tmdb.org/t/p/w185/actor.jpg")
        self.assertEqual(result["native_url"], "/media/movie/550")
        text = json.dumps(result, ensure_ascii=False)
        for private in ("library_status", "library_links", "file_path", "private-password", "must-not-escape", "私密库存", "不能透传"):
            self.assertNotIn(private, text)

    def test_identity_mismatch_is_rejected_instead_of_opening_the_wrong_title(self):
        for field, value in [("title_ref", "tmdb:movie:999"), ("external_id", "999"), ("media_type", "tv"), ("provider", "douban")]:
            payload = title_fixture()
            payload["title"][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                routes.public_title_details(payload, "tmdb:movie:550")

    def test_local_credentials_and_unknown_image_hosts_are_not_projected(self):
        for url in ["/api/v1/libraries/hidden/image", "file:///private/image", "http://127.0.0.1/private", "https://user:password@image.tmdb.org/image.jpg", "https://private.invalid/image.jpg"]:
            payload = title_fixture()
            payload["title"]["poster_url"] = url
            with self.subTest(url=url):
                self.assertIsNone(routes.public_title_details(payload, "tmdb:movie:550")["poster_url"])
        payload = title_fixture()
        payload["title"]["poster_url"] = "http://image.tmdb.org/t/p/w500/x.jpg?token=private#fragment"
        self.assertEqual(routes.public_title_details(payload, "tmdb:movie:550")["poster_url"], "https://image.tmdb.org/t/p/w500/x.jpg")

    def test_runtime_parses_explicit_units_and_does_not_guess_episode_counts(self):
        for value, expected in [("2小时19分钟", 139), ("90 分钟", 90), ("2小时", 120), ("1 季", None), ("全30集", None), ("未知", None)]:
            with self.subTest(value=value):
                self.assertEqual(routes._runtime_minutes(value), expected)


class PersonalRoutesTests(unittest.IsolatedAsyncioTestCase):
    async def test_learning_and_subscription_sync_are_private_bounded_same_origin(self):
        for path, body in [("/personal/subscriptions", {"refs": ["tmdb:tv:42"]}), ("/personal/learning", {"enabled": False})]:
            self.assertEqual((await self.client.post(path, json=body)).status_code, 404)
            response = await self.client.post(path, json=body, headers=self.private_headers())
            self.assertEqual(response.status_code, 200)
            self.assert_no_store(response)
            self.assertNotIn(TOKEN_A, response.text)
            self.assertEqual((await self.client.post(path, json=body, headers={**self.private_headers(), "sec-fetch-site": "cross-site"})).status_code, 403)
            self.assertEqual((await self.client.post(path, content="bad", headers=self.private_headers())).status_code, 415)
            self.assertEqual((await self.client.post(path, json={**body, "scope": "another"}, headers=self.private_headers())).status_code, 422)
        self.assertEqual((await self.client.post("/personal/subscriptions", json={"refs": ["x" * 70000]}, headers=self.private_headers())).status_code, 413)

    async def test_shelf_requires_capability_and_writes_are_bounded_same_origin_json(self):
        self.assertEqual((await self.client.get("/personal/shelf")).status_code, 404)
        response = await self.client.get("/personal/shelf", headers={"X-Platform-Profile": TOKEN_A})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(TOKEN_A, response.text)
        headers = {"X-Platform-Profile": TOKEN_A}
        body = {"action": "remove", "item": {"detail_ref": "tmdb:movie:550"}}
        self.assertEqual((await self.client.post("/personal/feedback", headers=headers, json=body)).status_code, 200)
        self.assertEqual((await self.client.post("/personal/feedback", headers={**headers, "sec-fetch-site": "cross-site"}, json=body)).status_code, 403)
        self.assertEqual((await self.client.post("/personal/feedback", headers=headers, json={"item": "x" * 20000})).status_code, 413)
        self.assertEqual((await self.client.post("/personal/feedback", headers=headers, content="bad")).status_code, 415)

    async def test_filters_are_validated_and_forwarded(self):
        response = await self.client.get("/personal/recommendations?media_type=movie&era=classic", headers={"X-Platform-Profile": TOKEN_A})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.personal.filters, {"media_type": "movie", "era": "classic"})
        response = await self.client.get("/personal/recommendations?era=future", headers={"X-Platform-Profile": TOKEN_A})
        self.assertEqual(response.status_code, 400)

    async def test_country_and_rich_era_are_validated_and_forwarded(self):
        response = await self.client.get("/personal/recommendations?media_type=tv&era=1990s&country=KR", headers={"X-Platform-Profile": TOKEN_A})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.personal.filters, {"media_type":"tv", "era":"1990s", "country":"KR"})
        for query in ["country=ZZ", "country=jp", "country=JP%7CKR", "era=1999s", "era=2030s"]:
            response = await self.client.get("/personal/recommendations?" + query, headers={"X-Platform-Profile": TOKEN_A})
            self.assertEqual(response.status_code, 400)

    async def asyncSetUp(self):
        self.personal = FakePersonal()
        self.ops_calls = []
        self.upstream = title_fixture()
        self.ops_error = None
        outer = self
        class Ops:
            async def call(self, operation, arguments, timeout=None):
                outer.ops_calls.append((operation, arguments, timeout))
                if outer.ops_error:
                    raise outer.ops_error
                return copy.deepcopy(outer.upstream)
        async def get_ops():
            return Ops()
        app = FastAPI()
        app.include_router(routes.build_personal_router("platform-discovery", self.personal, get_ops))
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://movieclaw.test")

    async def asyncTearDown(self):
        await self.client.aclose()

    def private_headers(self, token=TOKEN_A):
        return {routes.PROFILE_HEADER: token}

    def assert_no_store(self, response):
        self.assertIn("no-store", response.headers.get("cache-control", ""))

    async def test_create_returns_token_once_and_get_does_not_echo_credentials(self):
        created = await self.client.post("/personal/profile")
        self.assertEqual(created.status_code, 200)
        self.assertEqual(created.json()["profile_token"], TOKEN_A)
        self.assertNotIn("scope", created.json()["profile"])
        self.assertNotIn("profile_token", created.json()["profile"])
        self.assert_no_store(created)
        profile = await self.client.get("/personal/profile", headers=self.private_headers())
        self.assertEqual(profile.status_code, 200)
        for private in (TOKEN_A, "profile_token", "token_hash", "library_status"):
            self.assertNotIn(private, profile.text)
        self.assert_no_store(profile)

    async def test_profile_is_chosen_only_by_capability_not_client_user_identity(self):
        headers = {**self.private_headers(), "X-MovieClaw-User-ID": "user-b", "X-User-ID": "user-b"}
        response = await self.client.get("/personal/profile?user_id=user-b", headers=headers)
        self.assertEqual(response.json()["answers"]["mood"], "quiet")
        self.assertEqual(self.personal.calls[-1], ("get", TOKEN_A))
        response_b = await self.client.get("/personal/profile", headers=self.private_headers(TOKEN_B))
        self.assertEqual(response_b.json()["answers"]["mood"], "active")

    async def test_query_tokens_missing_tokens_and_duplicate_headers_are_rejected(self):
        for kwargs in [{}, {"params": {"profile_token": TOKEN_A}}, {"headers": [(routes.PROFILE_HEADER, TOKEN_A), (routes.PROFILE_HEADER, TOKEN_B)]}, {"headers": self.private_headers("invalid-token")}]:
            with self.subTest(kind=list(kwargs)):
                response = await self.client.get("/personal/profile", **kwargs)
                self.assertEqual(response.status_code, 404)
                self.assertNotIn(TOKEN_A, response.text)
                self.assert_no_store(response)
        self.assertEqual(self.personal.calls, [])

    async def test_unknown_profile_is_distinct_from_temporary_storage_failure_and_redacted(self):
        for status in (404, 503):
            self.personal.failure = ProfileError(status)
            response = await self.client.get("/personal/profile", headers=self.private_headers())
            self.assertEqual(response.status_code, status)
            for secret in ("private-password", "proxy-user", "127.0.0.1", TOKEN_A):
                self.assertNotIn(secret, response.text)
            self.assert_no_store(response)

    async def test_answer_payload_is_bounded_and_never_echoes_extra_secret_fields(self):
        cases = [({"answers": {}, "profile_token": TOKEN_A}, 422), ({"answers": "wrong"}, 422), ({"answers": {"large": "x" * 34000}}, 413)]
        for body, status in cases:
            response = await self.client.post("/personal/answers", headers=self.private_headers(), json=body)
            self.assertEqual(response.status_code, status)
            self.assertNotIn(TOKEN_A, response.text)
            self.assert_no_store(response)
        self.assertFalse(any(call[0] == "submit" for call in self.personal.calls))

    async def test_valid_answers_and_recommendations_forward_only_expected_arguments(self):
        answers = {"mood": "quiet", "genres": ["drama"]}
        response = await self.client.post("/personal/answers", headers=self.private_headers(), json={"answers": answers})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.personal.calls[-1], ("submit", TOKEN_A, answers))
        self.assertNotIn(TOKEN_A, response.text)
        response = await self.client.get("/personal/recommendations?nonce=batch_2", headers=self.private_headers())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.personal.calls[-1], ("recommend", TOKEN_A, "batch_2", 48))
        self.assertNotIn("library_status", response.text)
        self.assert_no_store(response)

    async def test_malformed_json_and_nonce_errors_have_safe_no_store_responses(self):
        response = await self.client.post("/personal/answers", headers={**self.private_headers(), "Content-Type": "application/json"}, content=b"{")
        self.assertEqual(response.status_code, 422)
        self.assert_no_store(response)

        response = await self.client.get("/personal/recommendations?nonce=%3Cscript%3E", headers=self.private_headers())
        self.assertEqual(response.status_code, 400)
        self.assertNotIn("<script>", response.text)
        self.assert_no_store(response)

    async def test_viewport_count_is_bounded_and_forwarded(self):
        for count in (3, 6, 9, 33, 96):
            response = await self.client.get(f"/personal/recommendations?nonce=fit&limit={count}", headers=self.private_headers())
            self.assertEqual(response.status_code, 200)
            self.assertEqual(self.personal.calls[-1], ("recommend", TOKEN_A, "fit", count))
        for count in ("0", "2", "97", "1000", "NaN", "1.5", "-1"):
            response = await self.client.get(f"/personal/recommendations?nonce=fit&limit={count}", headers=self.private_headers())
            self.assertEqual(response.status_code, 400)
            self.assert_no_store(response)

    async def test_cross_site_browser_mutations_are_rejected(self):
        response = await self.client.post("/personal/profile", headers={"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(response.status_code, 403)
        self.assert_no_store(response)

    async def test_questionnaire_is_public_metadata_but_requires_member_mount_in_production(self):
        response = await self.client.get("/personal/questionnaire")
        self.assertEqual(response.status_code, 200)
        self.assertIn("questions", response.json())
        self.assert_no_store(response)

    async def test_public_details_use_existing_readonly_operation_and_safe_projection(self):
        response = await self.client.get("/public-titles/tmdb:movie:550")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.ops_calls, [("discover.get-title-details", {"title_ref": "tmdb:movie:550"}, 20)])
        self.assertEqual(response.json()["cast"][0]["character"], "角色")
        self.assertEqual(response.json()["runtime_minutes"], 139)
        for secret in ("library_status", "library_links", "private-password", "/private/media"):
            self.assertNotIn(secret, response.text)
        self.assert_no_store(response)

    async def test_public_detail_errors_and_invalid_refs_never_expose_host_diagnostics(self):
        response = await self.client.get("/public-titles/not-a-valid-reference")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.ops_calls, [])
        self.ops_error = RuntimeError("private-password from tmdb token")
        response = await self.client.get("/public-titles/tmdb:movie:550")
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("private-password", response.text)
        self.assert_no_store(response)

    async def test_native_status_checks_only_host_and_returns_two_public_fields(self):
        async def direct_fetch(*args, **kwargs):
            self.fail("Plugin TMDB metadata must not substitute for host health")
        self.personal.discovery = types.SimpleNamespace(_token="private-tmdb-token", _json=direct_fetch)
        response = await self.client.get("/native-title-status/tmdb:movie:550?url=https://private.invalid&timeout=999")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(set(response.json()), {"available", "message"})
        self.assertTrue(response.json()["available"])
        self.assertEqual(self.ops_calls, [("discover.get-title-details", {"title_ref": "tmdb:movie:550"}, 10)])
        for secret in ("library_status", "library_links", "private-password", "搏击俱乐部", "private-tmdb-token"):
            self.assertNotIn(secret, response.text)
        self.assert_no_store(response)

    async def test_native_status_accepts_verified_wrapped_tv_and_douban_identities(self):
        for reference, provider, kind, identity in [("tmdb:tv:550", "tmdb", "tv", 550), ("douban:1291546", "douban", "movie", "1291546")]:
            payload = title_fixture()
            payload["title"].update(title_ref=reference, provider=provider, media_type=kind, external_id=identity)
            self.upstream = {"success": True, "data": payload}
            response = await self.client.get("/native-title-status/" + reference)
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.json()["available"])

    async def test_native_status_502_and_permission_errors_are_redacted_unavailable(self):
        for error in [RuntimeError("HTTP 502 from http://proxy-user:private-password@127.0.0.1?api_key=secret"), PermissionError("grant denied for private admin identity")]:
            self.ops_error = error
            response = await self.client.get("/native-title-status/tmdb:movie:550")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), {"available": False, "message": routes.NATIVE_UNAVAILABLE})
            for secret in ("502", "proxy-user", "private-password", "127.0.0.1", "api_key", "grant", "admin"):
                self.assertNotIn(secret, response.text)
            self.assert_no_store(response)

    async def test_native_status_rejects_invalid_ids_before_any_host_call(self):
        for reference in ["tmdb:movie:0", "tmdb:movie:-1", "tmdb:person:550", "tmdb:movie:123456789012", "douban:0", "https:private:550"]:
            response = await self.client.get("/native-title-status/" + reference)
            self.assertEqual(response.status_code, 400)
            self.assertEqual(set(response.json()), {"available", "message"})
            self.assertFalse(response.json()["available"])
            self.assert_no_store(response)
        self.assertEqual(self.ops_calls, [])

    async def test_native_status_requires_actual_matching_title_identity(self):
        variants = [None, {}, {"title": {}}, {"success": False, "data": title_fixture()}]
        for field, value in [("title", " "), ("title_ref", "tmdb:movie:999"), ("provider", "douban"), ("external_id", "999"), ("media_type", "tv")]:
            payload = title_fixture()
            payload["title"][field] = value
            variants.append(payload)
        for field in ("title_ref", "provider", "external_id", "media_type"):
            payload = title_fixture()
            del payload["title"][field]
            variants.append(payload)
        for payload in variants:
            with self.subTest(payload=repr(payload)[:80]):
                self.upstream = payload
                response = await self.client.get("/native-title-status/tmdb:movie:550")
                self.assertEqual(response.json(), {"available": False, "message": routes.NATIVE_UNAVAILABLE})

    async def test_native_status_has_outer_timeout_including_ops_acquisition(self):
        async def slow_ops():
            await asyncio.sleep(1)
            self.fail("The acquisition must be cancelled by the bounded check")
        app = FastAPI()
        app.include_router(routes.build_personal_router("platform-discovery", self.personal, slow_ops))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://movieclaw.test") as client:
            with patch.object(routes, "NATIVE_STATUS_TIMEOUT", 0.01):
                response = await asyncio.wait_for(client.get("/native-title-status/tmdb:movie:550"), timeout=0.5)
        self.assertEqual(response.json(), {"available": False, "message": routes.NATIVE_UNAVAILABLE})
        self.assert_no_store(response)

    async def test_production_mount_keeps_native_status_in_authenticated_member_zone(self):
        # Exercise actual plugin bootstrap and model the host's zone guard. The
        # process receives no Principal, so authentication belongs to this mount.
        import platform_discovery as plugin
        app, mounts = FastAPI(), []
        outer = self
        async def require_member(request: Request):
            if request.headers.get("X-Test-Member") != "logged-in":
                raise HTTPException(401, "login required")
        class RouteService:
            def mount(self, ctx, router, *, zone):
                mounts.append((zone, router))
                app.include_router(router, dependencies=[Depends(require_member)])
        class Context:
            entry_id = "platform-discovery"
            config = types.SimpleNamespace(watch_region="US", tmdb_token=None, auto_refresh=False, auto_refresh_minutes=60)
            def use(self, key):
                if key is plugin.PLUGIN_ROUTES:
                    return RouteService()
                if key is plugin.HOST_OPS:
                    return types.SimpleNamespace(client=get_client)
                return types.SimpleNamespace(scoped=lambda ctx: object())
            def effect(self, callback, *, label):
                pass
        class Source:
            async def initialize(self):
                pass
            async def close(self):
                pass
        class Ops:
            async def call(self, operation, arguments, timeout=None):
                outer.ops_calls.append((operation, arguments, timeout))
                return title_fixture()
        async def get_client(ctx):
            return Ops()
        source = Source()
        personal = FakePersonal()
        personal.close = source.close
        updater = types.SimpleNamespace(close=source.close, start=lambda ctx: None)
        with patch.object(plugin, "build_collection_router", return_value=APIRouter()), patch.object(plugin, "theater_cache_root", return_value=Path("/tmp/movieclaw-artwork-unit-unused")), patch.object(plugin, "build_theater_router", return_value=APIRouter()), patch.object(plugin, "host_account_dependency", return_value=require_member), patch.object(plugin, "PublicCacheStore", return_value=object()), patch.object(plugin, "DiscoverySources", return_value=source), patch.object(plugin, "RecommendationSources", return_value=source), patch.object(plugin, "PersonalRecommendations", return_value=personal), patch.object(plugin, "FeedUpdater", return_value=updater), patch.object(plugin, "build_routers", return_value=(APIRouter(), APIRouter())):
            await plugin.apply.apply(Context())
        owners = [zone for zone, router in mounts if any(route.path == "/native-title-status/{title_ref}" for route in router.routes)]
        self.assertEqual(owners, ["member"])
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://movieclaw.test") as client:
            denied = await client.get("/native-title-status/tmdb:movie:550")
            self.assertEqual(denied.status_code, 401)
            self.assertEqual(self.ops_calls, [])
            allowed = await client.get("/native-title-status/tmdb:movie:550", headers={"X-Test-Member": "logged-in"})
            self.assertEqual(allowed.status_code, 200)
            self.assertTrue(allowed.json()["available"])

    def direct_fixture(self):
        return {"id": 550, "title": "搏击俱乐部", "original_title": "Fight Club", "release_date": "1999-10-15",
                "overview": "TMDB 中文公开简介", "runtime": 139, "vote_average": 8.4,
                "genres": [{"id": 18, "name": "剧情"}], "production_countries": [{"iso_3166_1": "US"}],
                "original_language": "en", "poster_path": "/poster.jpg", "backdrop_path": "/backdrop.jpg",
                "credits": {"cast": [{"id": 100, "name": "演员", "character": "角色", "profile_path": "/actor.jpg"}],
                            "crew": [{"id": 200, "name": "导演", "job": "Director", "profile_path": "/director.jpg"}]},
                "library_status": {"hidden": "must-not-escape"}}

    async def test_configured_plugin_token_reads_fixed_tmdb_details_before_host(self):
        calls = []
        async def fetch(url, params, tmdb=False):
            calls.append((url, params, tmdb))
            return self.direct_fixture()
        self.personal.discovery = types.SimpleNamespace(_token="private-tmdb-token", _json=fetch)
        response = await self.client.get("/public-titles/tmdb:movie:550")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.ops_calls, [])
        self.assertEqual(calls, [("https://api.themoviedb.org/3/movie/550", {"language": "zh-CN", "append_to_response": "credits"}, True)])
        self.assertEqual(response.json()["directors"], ["导演"])
        self.assertEqual(response.json()["cast"][0]["character"], "角色")
        self.assertEqual(response.json()["runtime_minutes"], 139)
        self.assertEqual(response.json()["posters"][0]["full_url"], "https://image.tmdb.org/t/p/original/poster.jpg")
        self.assertNotIn("must-not-escape", response.text)
        self.assertNotIn("private-tmdb-token", response.text)
        self.assert_no_store(response)

    async def test_empty_chinese_overview_uses_same_identity_english_translation(self):
        calls = []
        async def fetch(url, params, tmdb=False):
            calls.append(params)
            payload = self.direct_fixture()
            payload["overview"] = "" if params["language"] == "zh-CN" else "English public overview"
            return payload
        self.personal.discovery = types.SimpleNamespace(_token="private-tmdb-token", _json=fetch)
        response = await self.client.get("/public-titles/tmdb:movie:550")
        self.assertEqual(response.json()["overview"], "English public overview")
        self.assertEqual(response.json()["title"], "搏击俱乐部")
        self.assertEqual([call["language"] for call in calls], ["zh-CN", "en-US"])
        self.assertEqual(self.ops_calls, [])

    async def test_direct_failure_falls_back_to_authorized_host_without_exposing_token(self):
        async def fetch(url, params, tmdb=False):
            raise RuntimeError("private-tmdb-token at http://proxy-user:private-password@127.0.0.1")
        self.personal.discovery = types.SimpleNamespace(_token="private-tmdb-token", _json=fetch)
        response = await self.client.get("/public-titles/tmdb:movie:550")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.ops_calls), 1)
        self.assertNotIn("private-tmdb-token", response.text)
        self.assertNotIn("private-password", response.text)

    async def test_direct_wrong_identity_is_rejected_and_uses_host_projection(self):
        async def fetch(url, params, tmdb=False):
            payload = self.direct_fixture()
            payload.update(id=999, title="错误作品")
            return payload
        self.personal.discovery = types.SimpleNamespace(_token="private-tmdb-token", _json=fetch)
        response = await self.client.get("/public-titles/tmdb:movie:550")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["title"], "搏击俱乐部")
        self.assertEqual(len(self.ops_calls), 1)

    async def test_translation_failure_preserves_valid_cast_and_public_poster(self):
        async def fetch(url, params, tmdb=False):
            if params["language"] == "en-US":
                raise RuntimeError("private translation transport failure")
            payload = self.direct_fixture()
            payload["overview"] = ""
            return payload
        self.personal.discovery = types.SimpleNamespace(_token="private-tmdb-token", _json=fetch)
        response = await self.client.get("/public-titles/tmdb:movie:550")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["overview"], "")
        self.assertEqual(response.json()["cast"][0]["name"], "演员")
        self.assertTrue(response.json()["poster_url"])
        self.assertEqual(self.ops_calls, [])


if __name__ == "__main__":
    unittest.main()
