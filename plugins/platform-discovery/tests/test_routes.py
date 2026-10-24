"""入口、权限边界与秘密字段保护的 ASGI 验收，不请求真实平台。"""

import asyncio
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi import FastAPI, APIRouter
from pydantic import ValidationError

from movieclaw_sdk.config_schema import describe_config, secret_fields
from movieclaw_sdk.ops import OpsError
from platform_discovery import PLATFORM_IDS, Settings, apply
from platform_discovery.routes import build_routers
from platform_discovery.sources import DiscoverySources

PACKAGE = Path(__file__).resolve().parents[1]
BASE = "/api/v1/plugins/platform-discovery"
SECRET = "private-token-must-not-be-returned"
PROXY_SECRET = "http://private-user:private-password@proxy.example:7890"


class FakeSources:
    def __init__(self, config, **kwargs):
        self.config, self.closed = config, False

    async def catalog(self):
        return {"platforms": [{"id": "netflix", "name": "Netflix"}], "region": self.config.watch_region}

    async def feed(self, platform, kind="all", page=1):
        return {"platform_id": platform, "kind": kind, "page": page, "items": [], "status": "ready"}

    async def close(self):
        self.closed = True

    async def initialize(self):
        pass

    async def connection_test(self):
        return {"ok": True, "mode": self.config.proxy_mode, "message": "榜源连接正常", "latency_ms": 21,
                "proxy_url": PROXY_SECRET}


class FakeOps:
    def __init__(self):
        self.calls = []

    async def call(self, name, args):
        self.calls.append((name, args))
        if args["title_ref"] == "tmdb:tv:999":
            raise OpsError(502, "UPSTREAM", "token=" + SECRET)
        return {"title": {"title_ref": args["title_ref"]}}


class FakeRecommendations:
    def __init__(self):
        self.calls, self.closed = [], False

    async def categories(self):
        return {"categories": [{"id": "korean", "name": "韩剧"}, {"id": "chinese", "name": "华语剧"}]}

    async def recommend(self, category, nonce="0", limit=12, session_id="default"):
        self.calls.append((category, nonce, limit, session_id))
        return {"category_id": category, "items": [], "status": "ready", "nonce": nonce}

    async def close(self):
        self.closed = True


class SettingsTests(unittest.TestCase):
    def test_secret_schema_remains_editable_and_protected(self):
        described = describe_config(Settings)
        self.assertNotIn("unsupported", described)
        self.assertEqual(secret_fields(described["schema"]), {"tmdb_token", "proxy_url", "fanart_api_key", "fanart_client_key"})
        self.assertNotIn(SECRET, Settings(tmdb_token=SECRET).model_dump_json())
        self.assertNotIn(PROXY_SECRET, Settings(proxy_url=PROXY_SECRET).model_dump_json())
        self.assertEqual(described["schema"]["properties"]["proxy_mode"]["enum"], ["host", "direct", "custom"])

    def test_proxy_modes_and_supported_addresses(self):
        self.assertEqual(Settings().proxy_mode, "host")
        self.assertIsNone(Settings(proxy_mode="direct").proxy_url)
        for url in (PROXY_SECRET, "https://proxy.example:443", "socks5://127.0.0.1:1080", "socks5h://[::1]:1080", "http://localhost:7890"):
            self.assertEqual(Settings(proxy_mode="custom", proxy_url=url).proxy_url.get_secret_value(), url)
        for values in ({"proxy_mode": "custom"}, {"proxy_mode": "custom", "proxy_url": "  "}, {"proxy_mode": "invalid"}):
            with self.subTest(values=values), self.assertRaises(ValidationError):
                Settings(**values)
        for url in ("ftp://proxy.example", "http://", "http://proxy.example:0", "http://proxy.example:65536", "http://proxy.example:bad", "http://proxy.example/path", "http://proxy.example?token=private", "http://proxy.example#private", "http://bad host:7890", "http://-bad.example:7890"):
            with self.subTest(url=url), self.assertRaises(ValidationError):
                Settings(proxy_mode="custom", proxy_url=url)

    def test_plain_proxy_address_is_visible_and_sufficient_for_custom_mode(self):
        described = describe_config(Settings)
        self.assertNotIn("unsupported", described)
        field = described["schema"]["properties"]["proxy_address"]
        self.assertEqual(field["type"], "string")
        self.assertFalse(field.get("writeOnly", False))
        self.assertEqual(field["default"], "")
        for url in ("http://192.168.1.2:7890", "https://proxy.example:443", "socks5://127.0.0.1:1080", "socks5h://[::1]:1080"):
            with self.subTest(url=url):
                settings = Settings(proxy_mode="custom", proxy_address=url)
                self.assertEqual(settings.proxy_address, url)
                self.assertIsNone(settings.proxy_url)

    def test_plain_proxy_address_rejects_authentication_details(self):
        for url in (PROXY_SECRET, "http://private-user@proxy.example:7890", "socks5://private-user:private-password@proxy.example:1080"):
            with self.subTest(url=url), self.assertRaises(ValidationError):
                Settings(proxy_mode="custom", proxy_address=url)

    def test_region_and_platform_settings(self):
        self.assertEqual(Settings(watch_region="hk").watch_region, "HK")
        with self.assertRaises(ValidationError):
            Settings(watch_region="USA")

    def enabled_platforms(self, settings):
        return {platform for platform in PLATFORM_IDS if getattr(settings, "enable_" + platform)}

    def test_platform_schema_exposes_eight_boolean_switches(self):
        described = describe_config(Settings)
        self.assertNotIn("unsupported", described)
        fields = described["schema"]["properties"]
        self.assertNotIn("enabled_platforms", fields)
        self.assertNotIn("enable_domestic", fields)
        switches = {"enable_" + platform for platform in PLATFORM_IDS}
        self.assertEqual({name for name in fields if name.startswith("enable_")}, switches)
        for name in switches:
            self.assertEqual(fields[name]["type"], "boolean")
            self.assertIs(fields[name]["default"], True)
        self.assertEqual(self.enabled_platforms(Settings()), set(PLATFORM_IDS))

    def test_legacy_platform_list_preserves_selection(self):
        settings = Settings(enabled_platforms=["tencent", "netflix", "netflix"])
        self.assertEqual(self.enabled_platforms(settings), {"tencent", "netflix"})
        self.assertNotIn("enabled_platforms", settings.model_dump())
        self.assertNotIn("enable_domestic", settings.model_dump())

    def test_legacy_chinese_separators_and_screenshot_value(self):
        for value, selected in (
            (["iqiyi、 tencent、netflix"], {"iqiyi", "tencent", "netflix"}),
            ("iqiyi、 tencent，youku\nnetflix,disney", {"iqiyi", "tencent", "youku", "netflix", "disney"}),
        ):
            with self.subTest(value=value):
                self.assertEqual(self.enabled_platforms(Settings(enabled_platforms=value)), selected)

    def test_legacy_domestic_switch_stays_off(self):
        self.assertEqual(self.enabled_platforms(Settings(enable_domestic=False)), {"netflix", "hbo", "disney"})
        settings = Settings(enable_domestic=False, enabled_platforms=["iqiyi", "mango", "netflix", "hbo"])
        self.assertEqual(self.enabled_platforms(settings), {"netflix", "hbo"})

    def test_explicit_new_switches_override_legacy_settings(self):
        settings = Settings(enabled_platforms=["iqiyi", "netflix"], enable_domestic=False,
                            enable_iqiyi=True, enable_netflix=False, enable_disney=True)
        self.assertEqual(self.enabled_platforms(settings), {"iqiyi", "disney"})

    def test_all_platforms_can_be_switched_off(self):
        switches = {"enable_" + platform: False for platform in PLATFORM_IDS}
        self.assertEqual(self.enabled_platforms(Settings(**switches)), set())
        self.assertEqual(self.enabled_platforms(Settings(enabled_platforms=[])), set())

    def test_invalid_legacy_platform_has_readable_error(self):
        with self.assertRaises(ValidationError) as failure:
            Settings(enabled_platforms=["netflix", "unknown"])
        messages = " ".join(error["msg"] for error in failure.exception.errors())
        self.assertIn("平台", messages)
        self.assertRegex(messages, "未知|不支持|无法识别|未识别|不认识")

    def test_legacy_migration_preserves_proxy_and_token(self):
        settings = Settings(enabled_platforms=["netflix"], proxy_mode="custom", proxy_url=PROXY_SECRET, tmdb_token=SECRET)
        self.assertEqual(self.enabled_platforms(settings), {"netflix"})
        self.assertEqual(settings.proxy_mode, "custom")
        self.assertEqual(settings.proxy_url.get_secret_value(), PROXY_SECRET)
        self.assertEqual(settings.tmdb_token.get_secret_value(), SECRET)

    def test_manifest_requests_only_readonly_discovery_operations(self):
        manifest = tomllib.loads((PACKAGE / "movieclaw-plugin.toml").read_text())
        self.assertEqual(manifest["plugin"]["runtime"], "inline")
        self.assertEqual(manifest["plugin"]["version"], "0.11.1")
        self.assertEqual(manifest["requires"]["plugin-data"], "^1.0")
        self.assertEqual(manifest["permissions"]["operations"], list(apply.permissions))
        self.assertEqual(list(apply.permissions), ["discover.get-title-details", "discover.filter-titles"])
        self.assertEqual(manifest["permissions"]["paths"], [])


class RoutesTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.config = Settings(tmdb_token=SECRET, watch_region="hk", proxy_mode="custom", proxy_url=PROXY_SECRET)
        self.sources, self.ops = FakeSources(self.config), FakeOps()
        self.recommendations = FakeRecommendations()

        async def get_ops():
            return self.ops

        pages, details = build_routers("platform-discovery", self.config, self.sources, get_ops, self.recommendations)
        app = FastAPI()
        app.include_router(pages, prefix=BASE)
        app.include_router(details, prefix=BASE)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    async def asyncTearDown(self):
        await self.client.aclose()

    async def test_entry_redirect_and_status_never_return_token(self):
        for suffix in ("", "/"):
            response = await self.client.get(BASE + suffix)
            self.assertEqual(response.status_code, 307)
            self.assertEqual(response.headers["location"], BASE + "/panel")
        response = await self.client.get(BASE + "/status")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(SECRET, response.text)
        self.assertNotIn(PROXY_SECRET, response.text)
        self.assertNotIn("private-password", response.text)
        self.assertTrue(response.json()["tmdb_configured"])
        self.assertEqual(response.json()["theme_preferences_url"], "/api/v1/ui/preferences")
        self.assertEqual(response.json()["region"], "HK")
        self.assertEqual(response.json()["recommendation_url"], BASE + "/panel?view=random")
        self.assertEqual(response.json()["proxy"], {"mode": "custom", "label": "独立代理", "custom_configured": True})
        catalog = await self.client.get(BASE + "/catalog")
        self.assertEqual(catalog.json()["entry_url"], BASE + "/panel")

    async def test_connection_test_returns_only_safe_result(self):
        response = await self.client.get(BASE + "/connection-test")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": True, "mode": "custom", "message": "榜源连接正常", "latency_ms": 21})
        self.assertNotIn(PROXY_SECRET, response.text)
        self.assertNotIn("private-password", response.text)

    async def test_connection_test_integrates_fixed_source_and_safe_errors(self):
        async def request(config, handler):
            source = DiscoverySources(config, transport=httpx.MockTransport(handler))
            pages, _ = build_routers("platform-discovery", config, source, lambda: None)
            app = FastAPI()
            app.include_router(pages, prefix=BASE)
            try:
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                    response = await client.get(BASE + "/connection-test")
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(set(response.json()), {"ok", "mode", "message", "latency_ms"})
                    self.assertEqual(response.json()["mode"], config.proxy_mode)
                    self.assertNotIn("private-password", response.text)
                    return response.json()
            finally:
                await source.close()

        def healthy(req):
            self.assertEqual(str(req.url), "https://www.netflix.com/tudum/top10/data/all-weeks-global.tsv")
            return httpx.Response(200, text="week\tcategory\tweekly_rank\tshow_title\n")

        for mode in ("host", "direct", "custom"):
            result = await request(Settings(proxy_mode=mode, proxy_url=PROXY_SECRET if mode == "custom" else None), healthy)
            self.assertTrue(result["ok"])

        def failing(req):
            raise httpx.ProxyError(PROXY_SECRET)

        result = await request(self.config, failing)
        self.assertFalse(result["ok"])

    async def test_assets_have_security_headers_and_whitelist(self):
        for path, media in (("/yingyu-icon.svg", "image/svg+xml"), ("/assets/yingyu-icon.svg", "image/svg+xml"), ("/workspace.js", "application/javascript"), ("/assets/workspace.css", "text/css"), ("/wall-tools.js", "application/javascript"), ("/assets/wall-tools.js", "application/javascript"), ("/panel", "text/html"), ("/page.css", "text/css"), ("/page.js", "application/javascript"), ("/assets/subscription.js", "application/javascript"), ("/assets/transport.js", "application/javascript"), ("/transport.js", "application/javascript"), ("/personal.js", "application/javascript"), ("/posters.js", "application/javascript"), ("/assets/posters.js", "application/javascript")):
            response = await self.client.get(BASE + path)
            self.assertEqual(response.status_code, 200)
            self.assertIn(media, response.headers["content-type"])
            self.assertEqual(response.headers["x-content-type-options"], "nosniff")
            self.assertIn("object-src 'none'", response.headers["content-security-policy"])
        for path in ("/assets/routes.py", "/assets/__init__.py", "/assets/%2E%2E%2Froutes.py"):
            self.assertEqual((await self.client.get(BASE + path)).status_code, 404)

    async def test_feed_rejects_disabled_platform_and_invalid_query(self):
        for path, status in (("/feeds/youku", 404), ("/feeds/netflix?kind=bad", 422), ("/feeds/netflix?page=0", 422), ("/feeds/netflix?page=101", 422)):
            self.assertEqual((await self.client.get(BASE + path)).status_code, status)
        response = await self.client.get(BASE + "/feeds/netflix?kind=tv&page=2")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["kind"], "tv")
        self.assertEqual(response.json()["page"], 2)

    async def test_detail_reference_and_host_error_redaction(self):
        for reference in ("wrong", "tmdb:tv:0", "tmdb:tv:-1", "tmdb:person:12"):
            self.assertEqual((await self.client.get(BASE + "/titles/" + reference)).status_code, 400)
        response = await self.client.get(BASE + "/titles/tmdb:tv:12")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.ops.calls[-1], ("discover.get-title-details", {"title_ref": "tmdb:tv:12"}))
        response = await self.client.get(BASE + "/titles/tmdb:tv:999")
        self.assertEqual(response.status_code, 502)
        self.assertNotIn(SECRET, response.text)

    async def test_recommendations_validate_category_and_nonce(self):
        response = await self.client.get(BASE + "/recommendation-categories")
        self.assertEqual([c["id"] for c in response.json()["categories"]], ["korean", "chinese"])
        response = await self.client.get(BASE + "/recommendations/korean?nonce=batch-123_abc&session_id=browser_1")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.recommendations.calls[-1], ("korean", "batch-123_abc", 12, "browser_1"))
        self.assertNotIn(SECRET, response.text)
        self.assertNotIn(PROXY_SECRET, response.text)
        for suffix, status in (("unknown", 404), ("korean?nonce=", 422), ("korean?nonce=%3Cscript%3E", 422), ("korean?nonce=" + "a" * 65, 422), ("korean?session_id=", 422), ("korean?session_id=%2Fother%2Fmember", 422)):
            self.assertEqual((await self.client.get(BASE + "/recommendations/" + suffix)).status_code, status)
        self.assertEqual(len(self.recommendations.calls), 1)

    async def test_lifecycle_keeps_host_details_in_admin_zone(self):
        mounts, effects = [], []

        class RouteService:
            def mount(self, ctx, router, *, zone):
                mounts.append((zone, router))

            def scoped(self, ctx):
                return object()

        class Context:
            config, entry_id = self.config, "platform-discovery"

            def use(self, key):
                return RouteService()

            def effect(self, callback, *, label):
                effects.append(callback)

            def task(self, coroutine, *, name):
                return asyncio.create_task(coroutine, name=name)

        async def guard():
            pass
        with patch("platform_discovery.build_collection_router", return_value=APIRouter()), patch("platform_discovery.theater_cache_root", return_value=Path("/tmp/movieclaw-artwork-unit-unused")), patch("platform_discovery.build_theater_router", return_value=APIRouter()), patch("platform_discovery.host_account_dependency", return_value=guard), patch("platform_discovery.DiscoverySources", return_value=self.sources) as constructor, patch("platform_discovery.RecommendationSources", return_value=self.recommendations) as recommendation_constructor:
            await apply.apply(Context())
        self.assertEqual(constructor.call_args.args, (self.config,))
        self.assertEqual(constructor.call_args.kwargs["entry_id"], "platform-discovery")
        self.assertIsNotNone(constructor.call_args.kwargs["storage"])
        self.assertEqual(recommendation_constructor.call_args.args[0], self.sources)
        self.assertEqual([zone for zone, _ in mounts], ["member", "admin"])
        self.assertEqual([r.path for r in mounts[1][1].routes], ["/titles/{title_ref}"])
        self.assertTrue(all(r.operation_id.startswith("plugins.platform-discovery.") for _, router in mounts for r in router.routes))
        for dispose in effects:
            await dispose()
        self.assertTrue(self.sources.closed)
        self.assertTrue(self.recommendations.closed)


if __name__ == "__main__":
    unittest.main()
