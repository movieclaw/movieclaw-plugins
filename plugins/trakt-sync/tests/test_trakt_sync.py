"""v0.2.1 的关键回归，适配插件仓库的 unittest CI；所有 Trakt 请求均为模拟。"""

import copy
import json
import logging
import unittest

import httpx
from trakt_sync.application import DEFAULT_CLIENT_ID, application_credentials
from trakt_sync.client import TraktClient, TraktError
from trakt_sync.engine import Engine
from trakt_sync.policy import iso_time


class Store:
    def __init__(self):
        self.data = {}

    async def get(self, key, *, scope="global", default=None):
        return copy.deepcopy(self.data.get((scope, key), default))

    async def set(self, key, value, *, scope="global", secret=False):
        self.data[scope, key] = copy.deepcopy(value)

    async def delete(self, key, *, scope="global"):
        return self.data.pop((scope, key), None) is not None

    async def scopes(self, key, *, entity=None):
        return {
            scope: copy.deepcopy(value)
            for (scope, name), value in self.data.items()
            if name == key and (entity is None or scope.startswith(entity + ":"))
        }


class Health:
    async def degraded(self, key, message, **kwargs):
        pass

    async def ok(self, key):
        pass


class TrackingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.now = 1800000000.0
        self.requests = []
        self.handler = None

        async def dispatch(request):
            self.requests.append(request)
            if self.handler:
                result = self.handler(request)
                if result is not None:
                    return result
            if request.url.path.startswith("/sync/history/"):
                return httpx.Response(200, json=[])
            return httpx.Response(201, json={"action": "scrobble"})

        http = httpx.AsyncClient(transport=httpx.MockTransport(dispatch))
        self.addAsyncCleanup(http.aclose)
        self.engine = Engine(
            Store(),
            TraktClient("fixture-client", "fixture-secret", http),
            Health(),
            logging.getLogger("test"),
            clock=lambda: self.now,
        )
        self.account = {
            "access_token": "fixture-access",
            "refresh_token": "fixture-refresh",
            "expires_at": self.now + 6000,
            "generation": "fixture-generation",
            "client_id": "fixture-client",
        }
        await self.engine.set(0, "account", self.account, secret=True)
        self.event = {
            "member_id": 0,
            "media": {"id": 7, "kind": "movie", "tmdb_id": 11},
            "unit": None,
            "position_ms": 90000,
            "duration_ms": 100000,
            "client": {"device_id": "fixture-device"},
        }

    async def test_start_completion_and_duplicate_produce_one_watch(self):
        e = self.engine
        await e.accept(
            "started", {**self.event, "position_ms": 0}, "start", iso_time(self.now)
        )
        await e.drain(0)
        self.now += 100
        for kind, key in [
            ("stopped", "stop"),
            ("completed", "done"),
            ("stopped", "stop"),
        ]:
            await e.accept(kind, self.event, key, iso_time(self.now))
        await e.drain(0)
        await e.drain(0)
        self.assertEqual(
            [r.url.path for r in self.requests], ["/scrobble/start", "/scrobble/stop"]
        )
        self.assertEqual((await e.status(0))["pending"], 0)

    async def test_member_binding_never_falls_back_to_administrator(self):
        e = self.engine
        event = {**self.event, "member_id": 1}
        await e.accept("completed", event, "other", iso_time(self.now))
        await e.drain(1)
        self.assertEqual(self.requests, [])
        await e.set(
            1, "account", {**self.account, "access_token": "member-one"}, secret=True
        )
        await e.accept("completed", event, "other", iso_time(self.now))
        await e.drain(1)
        self.assertEqual(self.requests[0].headers["authorization"], "Bearer member-one")
        self.assertEqual((await e.get(0, "account"))["access_token"], "fixture-access")

    async def test_rate_limit_waits_for_retry_after(self):
        self.handler = lambda r: httpx.Response(429, headers={"Retry-After": "120"})
        e = self.engine
        await e.accept("completed", self.event, "done", iso_time(self.now))
        await e.drain(0)
        self.handler = None
        self.now += 119
        await e.drain(0)
        self.assertEqual(len(self.requests), 1)
        self.now += 1
        await e.drain(0)
        self.assertEqual(
            [r.url.path for r in self.requests],
            [
                "/scrobble/stop",
                "/sync/history/movies",
                "/sync/history",
            ],
        )
        self.assertEqual((await e.status(0))["pending"], 0)

    async def test_old_completion_keeps_original_timestamp(self):
        e = self.engine
        when = iso_time(self.now - 86400)
        await e.accept("completed", self.event, "old", when)
        await e.drain(0)
        self.assertEqual(
            [r.url.path for r in self.requests],
            ["/sync/history/movies", "/sync/history"],
        )
        self.assertEqual(
            json.loads(self.requests[-1].content)["movies"][0]["watched_at"], when
        )

    async def test_reload_preserves_queue_and_event_deduplication(self):
        e = self.engine
        await e.accept("completed", self.event, "done", iso_time(self.now))
        e = Engine(e.store, e.client, e.health, e.log, clock=e.clock)
        await e.accept("completed", self.event, "done", iso_time(self.now))
        await e.drain(0)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.requests[0].url.path, "/scrobble/stop")

    async def test_oauth_refuses_rebind_and_untrusted_verification_url(self):
        e = self.engine
        with self.assertRaises(ValueError):
            await e.begin(0)
        self.assertEqual(self.requests, [])
        await e.store.delete("account", scope="member:0")
        for url in ["http://trakt.tv/activate", "https://evil.invalid/"]:
            with self.subTest(url=url):
                self.handler = lambda r, url=url: httpx.Response(
                    200,
                    json={
                        "verification_url": url,
                        "user_code": "ABCD",
                        "device_code": "fixture",
                    },
                )
                with self.assertRaises(TraktError):
                    await e.begin(0)

    async def test_default_app_omits_custom_secret(self):
        self.assertEqual(
            application_credentials("  ", "old-secret"), (DEFAULT_CLIENT_ID, "")
        )
        self.assertEqual(
            application_credentials("custom", "custom-secret"),
            ("custom", "custom-secret"),
        )
