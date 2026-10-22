"""市场审查修复的关键回归，适配插件仓库的 unittest CI；所有 Trakt 请求均为模拟。"""

import asyncio
import copy
import json
import logging
import unittest
from unittest.mock import AsyncMock

import httpx

from trakt_sync.application import DEFAULT_CLIENT_ID, application_credentials
from trakt_sync.client import VERSION, TraktClient, TraktError
from trakt_sync.engine import Engine
from trakt_sync.policy import iso_time


class Store:
    def __init__(self):
        self.data = {}
        self.fail_account_write = False
        self.scans = []

    async def get(self, key, *, scope="global", default=None):
        return copy.deepcopy(self.data.get((scope, key), default))

    async def set(self, key, value, *, scope="global", secret=False):
        if key == "account" and self.fail_account_write:
            raise OSError("fixture storage failure")
        self.data[scope, key] = copy.deepcopy(value)

    async def delete(self, key, *, scope="global"):
        return self.data.pop((scope, key), None) is not None

    async def scopes(self, key, *, entity=None):
        self.scans.append(key)
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
        await e.accept("started", {**self.event, "position_ms": 0}, "start", iso_time(self.now))
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
        self.assertEqual([r.url.path for r in self.requests], ["/scrobble/start", "/scrobble/stop"])
        self.assertEqual((await e.status(0))["pending"], 0)

    async def test_member_binding_never_falls_back_to_administrator(self):
        e = self.engine
        event = {**self.event, "member_id": 1}
        await e.accept("completed", event, "other", iso_time(self.now))
        await e.drain(1)
        self.assertEqual(self.requests, [])
        await e.set(1, "account", {**self.account, "access_token": "member-one"}, secret=True)
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
        self.assertEqual(json.loads(self.requests[-1].content)["movies"][0]["watched_at"], when)

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
        self.assertEqual(application_credentials("  ", "old-secret"), (DEFAULT_CLIENT_ID, ""))
        self.assertEqual(
            application_credentials("custom", "custom-secret"),
            ("custom", "custom-secret"),
        )

    async def test_rejected_auth_skips_over_100_events_and_other_member_continues(self):
        e = self.engine
        self.handler = lambda r: httpx.Response(400 if r.url.path == "/oauth/token" else 401)
        await e.accept("completed", self.event, "rejected", iso_time(self.now))
        await e.drain(0)
        self.assertTrue((await e.status(0))["needs_reconnect"])
        self.assertFalse((await e.status(0))["connected"])
        self.assertEqual((await e.status(0))["pending"], 0)
        count = len(self.requests)
        for i in range(110):
            await e.accept(
                "completed",
                {**self.event, "media": {"id": i, "kind": "movie", "tmdb_id": i + 1}},
                f"blocked-{i}",
                iso_time(self.now),
            )
            await e.drain(0)
        self.assertEqual(len(self.requests), count)
        self.assertEqual((await e.ledger(0))["queue"], [])
        with self.assertRaises(ValueError):
            await e.retry_failed(0)
        self.handler = None
        await e.set(1, "account", {**self.account, "access_token": "other"}, secret=True)
        await e.accept("completed", {**self.event, "member_id": 1}, "healthy", iso_time(self.now))
        await e.drain(1)
        self.assertEqual(self.requests[-1].headers["authorization"], "Bearer other")

    async def test_network_outage_overflow_is_bounded_and_visible(self):
        e = self.engine
        self.handler = lambda r: httpx.Response(503)
        for i in range(95):
            event = {
                **self.event,
                "media": {"id": i, "kind": "movie", "tmdb_id": i + 1},
            }
            await e.accept("completed", event, f"done-{i}", iso_time(self.now))
            await e.drain(0)
        ledger = await e.ledger(0)
        self.assertLessEqual(len(ledger["queue"]), 64)
        self.assertLessEqual(len(json.dumps(ledger, ensure_ascii=False).encode()), 60000)
        self.assertGreater(ledger["dropped"], 0)
        self.assertIn("未同步", (await e.status(0))["warning"])
        self.assertIn("HTTP 503", (await e.status(0))["last_error"])
        self.handler = None
        await e.set(2, "account", self.account, secret=True)
        await e.accept("completed", {**self.event, "member_id": 2}, "other", iso_time(self.now))
        await e.drain(2)
        self.assertEqual((await e.status(2))["pending"], 0)

    async def test_capacity_discards_start_before_completed_watch(self):
        e = self.engine
        await e.accept("started", self.event, "start", iso_time(self.now))
        for i in range(64):
            await e.accept(
                "completed",
                {
                    **self.event,
                    "media": {"id": i + 20, "kind": "movie", "tmdb_id": i + 20},
                },
                f"done-{i}",
                iso_time(self.now),
            )
        ledger = await e.ledger(0)
        self.assertEqual(len(ledger["queue"]), 64)
        self.assertTrue(all(e.finished(j) for j in ledger["queue"]))
        self.assertEqual(ledger.get("dropped", 0), 0)
        # Expire a pending live start without discarding the old completed watch.
        self.now += 400
        await e.save_ledger(0, ledger)
        self.assertEqual(len((await e.ledger(0))["queue"]), 64)

    async def test_oversized_item_is_skipped_without_raising(self):
        e = self.engine
        event = {
            **self.event,
            "media": {"id": 8, "kind": "movie", "imdb_id": "tt" + "9" * 70000},
        }
        await e.accept("completed", event, "large", iso_time(self.now))
        ledger = await e.ledger(0)
        self.assertLessEqual(len(json.dumps(ledger).encode()), 60000)
        self.assertEqual(ledger["queue"], [])
        row = (await e.activity(0))["records"][0]
        self.assertEqual(row["state"], "skipped")
        self.assertIn("未同步", row["reason"])

    async def test_heartbeats_do_not_displace_completed_activity(self):
        e = self.engine
        await e.accept("completed", self.event, "watch", iso_time(self.now))
        await e.drain(0)
        for i in range(90):
            self.now += 61
            event = {
                **self.event,
                "media": {"id": i + 20, "kind": "movie", "tmdb_id": i + 20},
            }
            await e.accept("started", event, f"heartbeat-{i}", iso_time(self.now))
            await e.drain(0)
        rows = (await e.activity(0))["records"]
        self.assertEqual([r["id"] for r in rows], ["watch"])
        self.assertEqual(rows[0]["state"], "sent")

    async def test_401_refresh_rotation_persisted_before_replay_without_secret(self):
        e = self.engine
        e.client.client_secret = ""

        def handler(r):
            if r.url.path == "/oauth/token":
                self.assertNotIn("client_secret", json.loads(r.content))
                return httpx.Response(
                    200,
                    json={
                        "access_token": "rotated-access",
                        "refresh_token": "rotated-refresh",
                        "expires_in": 6000,
                    },
                )
            if r.headers.get("authorization") == "Bearer fixture-access":
                return httpx.Response(401)
            self.assertEqual(
                e.store.data["member:0", "account"]["refresh_token"], "rotated-refresh"
            )
            return None

        self.handler = handler
        await e.accept("completed", self.event, "refresh", iso_time(self.now))
        await e.drain(0)
        self.assertEqual(
            [r.url.path for r in self.requests],
            ["/scrobble/stop", "/oauth/token", "/scrobble/stop"],
        )
        self.assertEqual((await e.get(0, "account"))["refresh_token"], "rotated-refresh")
        self.assertEqual((await e.status(0))["pending"], 0)
        self.assertTrue(self.requests[0].headers["user-agent"].endswith(VERSION))

    async def test_replay_rejection_does_not_overwrite_rotated_tokens(self):
        e = self.engine
        self.handler = lambda r: (
            httpx.Response(200, json={"access_token": "new", "refresh_token": "new-refresh"})
            if r.url.path == "/oauth/token"
            else httpx.Response(401)
        )
        await e.accept("completed", self.event, "bad-permissions", iso_time(self.now))
        await e.drain(0)
        account = await e.get(0, "account")
        self.assertTrue(account["blocked"])
        self.assertEqual(account["refresh_token"], "new-refresh")

    async def test_rotated_tokens_survive_transient_storage_failure(self):
        e = self.engine
        self.handler = lambda r: (
            httpx.Response(200, json={"access_token": "new", "refresh_token": "new-refresh"})
            if r.url.path == "/oauth/token"
            else httpx.Response(401)
            if r.headers.get("authorization") == "Bearer fixture-access"
            else None
        )
        await e.accept("completed", self.event, "storage", iso_time(self.now))
        e.store.fail_account_write = True
        with self.assertRaises(OSError):
            await e.drain(0)
        self.assertEqual(e.rotated[0]["refresh_token"], "new-refresh")
        e.store.fail_account_write = False
        await e.drain(0)
        self.assertEqual(sum(r.url.path == "/oauth/token" for r in self.requests), 1)
        self.assertEqual((await e.get(0, "account"))["refresh_token"], "new-refresh")
        self.assertEqual((await e.status(0))["pending"], 0)

    async def test_disconnect_success_failure_and_changed_application(self):
        e = self.engine
        e.client.client_secret = ""
        await e.accept("completed", self.event, "disconnect", iso_time(self.now))
        result = await e.disconnect(0)
        self.assertTrue(result["remote_revocation_confirmed"])
        self.assertEqual(
            json.loads(self.requests[-1].content),
            {"client_id": "fixture-client", "token": "fixture-access"},
        )
        for key in ("account", "ledger", "linked", "delivery", "activity"):
            self.assertIsNone(await e.get(0, key))
        await e.set(0, "account", self.account, secret=True)
        self.handler = lambda r: httpx.Response(503)
        self.assertFalse((await e.disconnect(0))["remote_revocation_confirmed"])
        self.assertFalse((await e.status(0))["has_account"])
        await e.set(0, "account", {**self.account, "client_id": "old-app"}, secret=True)
        count = len(self.requests)
        self.assertFalse((await e.disconnect(0))["remote_revocation_confirmed"])
        self.assertEqual(count, len(self.requests))

    async def test_poll_pending_rate_limit_denied_and_expired(self):
        e = self.engine

        async def pending():
            await e.set(
                1,
                "pending",
                {
                    "device_code": "fixture",
                    "expires_at": self.now + 1000,
                    "interval": 5,
                    "next_poll": self.now,
                },
                secret=True,
            )

        for code, state in (
            (400, "pending"),
            (429, "pending"),
            (418, "denied"),
            (404, "expired"),
            (410, "expired"),
        ):
            with self.subTest(code=code):
                await pending()
                self.handler = lambda r, code=code: httpx.Response(
                    code, headers={"Retry-After": "120"}
                )
                result = await e.poll(1)
                self.assertEqual(result["state"], state)
                count = len(self.requests)
                if state == "pending":
                    await e.poll(1)
                    self.assertEqual(count, len(self.requests))
                    if code == 429:
                        self.assertEqual((await e.get(1, "pending"))["next_poll"], self.now + 120)
                else:
                    self.assertIsNone(await e.get(1, "pending"))
        await pending()
        self.now += 1001
        count = len(self.requests)
        self.assertEqual((await e.poll(1))["state"], "expired")
        self.assertEqual(count, len(self.requests))

    async def test_media_match_failures_retry_without_blocking(self):
        e = self.engine
        for code in (404, 422):
            with self.subTest(code=code):
                await e.set(0, "ledger", {"queue": [], "seen": [], "sessions": {}})
                self.handler = lambda r, code=code: httpx.Response(code)
                await e.accept("completed", self.event, f"failed-{code}", iso_time(self.now))
                await e.drain(0)
                self.assertEqual((await e.status(0))["failed"], 1)
                self.assertEqual((await e.status(0))["pending"], 0)
                self.assertEqual((await e.retry_failed(0))["retried"], 1)
                self.handler = None
                await e.drain(0)
                self.assertEqual((await e.status(0))["failed"], 0)
                self.assertEqual((await e.status(0))["pending"], 0)

    async def test_short_stop_422_is_not_match_failure(self):
        e = self.engine
        self.handler = lambda r: httpx.Response(422)
        await e.accept("stopped", {**self.event, "position_ms": 0}, "short", iso_time(self.now))
        await e.drain(0)
        self.assertEqual((await e.status(0))["failed"], 0)
        self.assertEqual((await e.status(0))["pending"], 0)
        self.assertIn("短播放", (await e.activity(0))["records"][0]["reason"])

    async def test_host_timestamp_format_retains_microseconds_and_offset(self):
        e = self.engine
        when = "2026-10-22T01:02:03.123456+00:00"
        await e.accept("completed", self.event, "host-time", when)
        await e.drain(0)
        self.assertEqual(json.loads(self.requests[-1].content)["movies"][0]["watched_at"], when)

    async def test_scheduler_sleeps_when_idle_and_wakes_for_delivery(self):
        e = self.engine
        task = asyncio.create_task(e.run())
        try:
            for _ in range(10):
                await asyncio.sleep(0)
            scans = list(e.store.scans)
            await asyncio.sleep(0.02)
            self.assertEqual(e.store.scans, scans)
            self.assertEqual(scans.count("account"), 0)
            await e.accept("completed", self.event, "wake", iso_time(self.now))
            for _ in range(100):
                if (await e.status(0))["pending"] == 0:
                    break
                await asyncio.sleep(0.001)
            self.assertEqual((await e.status(0))["pending"], 0)
            self.assertEqual(len(self.requests), 1)
        finally:
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        reloaded = Engine(e.store, e.client, e.health, e.log, clock=e.clock)
        await reloaded.initialize()
        self.assertEqual(e.store.scans.count("account"), 0)

    async def test_legacy_rejected_queue_migrates_to_reconnect_state(self):
        e = self.engine
        await e.accept("completed", self.event, "legacy", iso_time(self.now))
        ledger = await e.ledger(0)
        ledger["queue"][0]["error_status"] = 401
        await e.set(0, "ledger", ledger)
        await e.initialize()
        self.assertTrue((await e.status(0))["needs_reconnect"])
        self.assertIsNone(await e.get(0, "delivery"))
        self.assertEqual((await e.status(0))["pending"], 0)

    async def test_market_application_configured_and_empty_override_keeps_secret_isolated(
        self,
    ):
        self.assertEqual(DEFAULT_CLIENT_ID, "aPq49pAxnq5HM6qVYY1vmKfHl6bV6S10aotXT5crbH0")
        self.engine.client.client_id = ""
        with self.assertRaisesRegex(ValueError, "配置不可用"):
            await self.engine.begin(2)
        self.assertEqual(self.requests, [])

    async def test_expired_live_jobs_removed_but_completed_jobs_preserved(self):
        e = self.engine
        await e.accept("started", self.event, "live", iso_time(self.now))
        await e.accept(
            "completed",
            {**self.event, "media": {"id": 9, "kind": "movie", "tmdb_id": 9}},
            "done",
            iso_time(self.now),
        )
        self.now += 301
        await e.save_ledger(0, await e.ledger(0))
        self.assertEqual([j["id"] for j in (await e.ledger(0))["queue"]], ["done"])

    async def test_poll_success_keeps_encrypted_credentials_if_profile_unavailable(
        self,
    ):
        e = self.engine
        await e.set(
            1,
            "pending",
            {
                "device_code": "fixture",
                "expires_at": self.now + 1000,
                "interval": 5,
                "next_poll": self.now,
            },
            secret=True,
        )
        self.handler = lambda r: (
            httpx.Response(503)
            if r.url.path == "/users/settings"
            else httpx.Response(
                200,
                json={
                    "access_token": "member-access",
                    "refresh_token": "member-refresh",
                },
            )
        )
        self.assertEqual((await e.poll(1))["state"], "connected")
        self.assertEqual((await e.get(1, "account"))["refresh_token"], "member-refresh")
        self.assertTrue(await e.get(1, "linked"))
        self.assertIsNone(await e.get(1, "pending"))
        self.assertEqual((await e.get(0, "account"))["refresh_token"], "fixture-refresh")

    async def test_marker_failure_cannot_persist_an_invisible_queue(self):
        e = self.engine
        original = e.store.set

        async def fail_marker(key, value, **kwargs):
            if key == "delivery":
                raise OSError("fixture marker failure")
            await original(key, value, **kwargs)

        e.store.set = fail_marker
        with self.assertRaises(OSError):
            await e.accept("completed", self.event, "marker", iso_time(self.now))
        self.assertEqual((await e.ledger(0))["queue"], [])
        e.store.set = original
        await e.accept("completed", self.event, "marker", iso_time(self.now))
        self.assertIsNotNone(await e.get(0, "delivery"))
        await e.drain(0)
        self.assertEqual((await e.status(0))["pending"], 0)

    async def test_stale_delivery_marker_is_removed(self):
        e = self.engine
        await e.set(0, "delivery", {"next_at": self.now})
        await e.drain(0)
        self.assertIsNone(await e.get(0, "delivery"))
        self.assertEqual(self.requests, [])

    async def test_cleared_stop_then_completion_delivers_after_pacing_delay(self):
        e = self.engine
        payload = {**self.event, "position_ms": 0}
        await e.accept("stopped", payload, "cleared-stop", iso_time(self.now))
        await e.accept("completed", payload, "cleared-done", iso_time(self.now))
        await e.drain(0)
        await e.drain(0)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual((await e.status(0))["pending"], 1)
        self.now += 1
        await e.drain(0)
        self.assertEqual(json.loads(self.requests[-1].content)["progress"], 100)
        self.assertEqual((await e.status(0))["pending"], 0)

    async def test_optional_distinct_rewatch_events_keep_original_times(self):
        e = self.engine
        for i in range(2):
            payload = {**self.event, "viewing_id": f"optional-{i}", "position_ms": 0}
            when = iso_time(self.now - 86400 + i * 3600)
            for _ in range(2):
                await e.accept("completed", payload, f"rewatch-{i}", when)
        await e.drain(0)
        self.now += 1
        await e.drain(0)
        sent = [
            json.loads(r.content)["movies"][0]["watched_at"]
            for r in self.requests
            if r.url.path == "/sync/history"
        ]
        self.assertEqual(sent, [iso_time(self.now - 1 - 86400 + i * 3600) for i in range(2)])
        self.assertEqual((await e.status(0))["pending"], 0)

    async def test_disconnect_revokes_rotated_token_after_failed_persistence(self):
        e = self.engine
        e.rotated[0] = {
            **self.account,
            "access_token": "newest",
            "refresh_token": "newest-refresh",
        }
        self.assertTrue((await e.disconnect(0))["remote_revocation_confirmed"])
        self.assertEqual(json.loads(self.requests[-1].content)["token"], "newest")
        self.assertNotIn(0, e.rotated)

    async def test_write_400_is_item_failure_and_next_member_job_can_continue(self):
        e = self.engine
        self.handler = lambda r: httpx.Response(400)
        await e.accept("completed", self.event, "bad-body", iso_time(self.now))
        await e.drain(0)
        self.assertTrue((await e.status(0))["connected"])
        self.assertFalse((await e.status(0))["needs_reconnect"])
        self.assertEqual((await e.status(0))["failed"], 1)
        self.assertEqual((await e.activity(0))["records"][0]["state"], "failed")
        self.handler = None
        await e.accept(
            "completed",
            {**self.event, "media": {"id": 10, "kind": "movie", "tmdb_id": 10}},
            "next",
            iso_time(self.now),
        )
        await e.drain(0)
        self.assertEqual((await e.status(0))["pending"], 0)
        self.assertEqual((await e.status(0))["failed"], 1)

    async def test_legacy_write_400_is_not_migrated_into_blocked_auth(self):
        e = self.engine
        await e.accept("completed", self.event, "old-400", iso_time(self.now))
        ledger = await e.ledger(0)
        ledger["queue"][0]["error_status"] = 400
        await e.set(0, "ledger", ledger)
        await e.initialize()
        await e.accept(
            "started",
            {**self.event, "media": {"id": 8, "kind": "movie", "tmdb_id": 8}},
            "allowed",
            iso_time(self.now),
        )
        self.assertTrue((await e.status(0))["connected"])
        self.assertEqual((await e.status(0))["pending"], 2)

    async def test_auth_source_is_distinguished_from_item_400(self):
        self.handler = lambda r: httpx.Response(400)
        with self.assertRaises(TraktError) as token:
            await self.engine.client.refresh("fixture")
        self.assertTrue(token.exception.auth_error)
        with self.assertRaises(TraktError) as item:
            await self.engine.client.request("POST", "/sync/history", {})
        self.assertFalse(item.exception.auth_error)
        for code in (401, 403):
            self.assertTrue(TraktError(code).auth_error)

    async def test_loss_acknowledgement_keeps_totals_queue_and_future_warnings(self):
        e = self.engine
        e.health.ok = AsyncMock()
        e.health.degraded = AsyncMock()
        await e.accept("completed", self.event, "kept", iso_time(self.now))
        ledger = await e.ledger(0)
        ledger["dropped"] = 4
        await e.save_ledger(0, ledger)
        await e.drain(0)
        self.assertIn("4", (await e.status(0))["warning"])
        self.assertIsNone((await e.status(0))["last_error"])
        e.health.degraded.assert_not_awaited()
        await e.accept(
            "completed",
            {**self.event, "media": {"id": 9, "kind": "movie", "tmdb_id": 9}},
            "pending",
            iso_time(self.now),
        )
        self.assertEqual((await e.acknowledge_dropped(0))["acknowledged"], 4)
        status = await e.status(0)
        self.assertEqual(status["dropped"], 4)
        self.assertEqual(status["unacknowledged_dropped"], 0)
        self.assertEqual(status["pending"], 1)
        self.assertIsNone(status["warning"])
        ledger = await e.ledger(0)
        ledger["dropped"] = 6
        await e.save_ledger(0, ledger)
        self.assertEqual((await e.status(0))["unacknowledged_dropped"], 2)
        self.assertIn("2", (await e.status(0))["warning"])

    async def test_acknowledgement_does_not_clear_real_failure(self):
        e = self.engine
        ledger = await e.ledger(0)
        ledger.update(dropped=2, last_error="真实网络故障")
        await e.save_ledger(0, ledger)
        e.health.ok = AsyncMock()
        await e.acknowledge_dropped(0)
        self.assertEqual((await e.status(0))["last_error"], "真实网络故障")
        e.health.ok.assert_not_awaited()

    async def test_corrupt_account_cannot_stop_migration_or_other_member_delivery(self):
        e = self.engine
        await e.accept("completed", self.event, "broken", iso_time(self.now))
        await e.set(1, "account", {**self.account, "access_token": "healthy"}, secret=True)
        await e.accept("completed", {**self.event, "member_id": 1}, "healthy", iso_time(self.now))
        get = e.store.get

        async def broken_get(key, *, scope="global", default=None):
            if scope == "member:0" and key == "account":
                raise ValueError("fixture corrupt encrypted value")
            return await get(key, scope=scope, default=default)

        e.store.get = broken_get
        await e.initialize()
        self.assertTrue(await e.store.get("delivery-index-v3"))
        self.assertNotIn("account", e.store.scans)
        await e.deliver_ready(await e.store.scopes("delivery", entity="member"))
        self.assertEqual((await e.status(1))["pending"], 0)
        self.assertTrue((await e.status(0))["needs_reconnect"])
        self.assertEqual(len((await e.accounts())["accounts"]), 2)
        count = len(self.requests)
        for i in range(80):
            await e.accept("completed", self.event, f"unreadable-{i}", iso_time(self.now))
        self.assertEqual(count, len(self.requests))
        self.assertFalse((await e.disconnect(0))["remote_revocation_confirmed"])
        self.assertIsNone(e.store.data.get(("member:0", "account")))

    async def test_unexpected_member_errors_back_off_without_delaying_others(self):
        e = self.engine
        original = e.drain
        calls = []

        async def failing(member):
            calls.append(member)
            if member == 0:
                raise OSError("fixture")
            await original(member)

        e.drain = failing
        work = {"member:0": {"next_at": self.now}, "member:1": {"next_at": self.now}}
        with self.assertLogs("test", level="WARNING") as logs:
            await e.deliver_ready(work)
            self.assertEqual(e.delivery_failures[0][1], self.now + 10)
            for _ in range(8):
                await e.deliver_ready(work)
            self.assertEqual(calls.count(0), 1)
            self.assertEqual(calls.count(1), 9)
            self.now += 10
            await e.deliver_ready(work)
            self.assertEqual(e.delivery_failures[0][1], self.now + 20)
        self.assertEqual(len(logs.output), 2)
        self.assertNotIn("fixture", " ".join(logs.output))

    async def test_cancelled_delivery_is_not_swallowed_or_backed_off(self):
        e = self.engine
        e.drain = AsyncMock(side_effect=asyncio.CancelledError)
        with self.assertRaises(asyncio.CancelledError):
            await e.deliver_ready({"member:0": {"next_at": self.now}})
        self.assertEqual(e.delivery_failures, {})

    async def test_secretless_refresh_contains_oob_redirect_and_rotates(self):
        self.engine.client.client_secret = ""
        self.handler = lambda r: httpx.Response(
            200, json={"access_token": "new", "refresh_token": "new-r"}
        )
        await self.engine.client.refresh("old-r")
        self.assertEqual(
            json.loads(self.requests[-1].content),
            {
                "client_id": "fixture-client",
                "refresh_token": "old-r",
                "grant_type": "refresh_token",
                "redirect_uri": "urn:ietf:wg:oauth:2.0:oob",
            },
        )

    async def test_explicit_official_application_never_sends_stale_custom_secret(self):
        self.assertEqual(
            application_credentials(DEFAULT_CLIENT_ID, "stale"), (DEFAULT_CLIENT_ID, "")
        )

    async def test_legacy_account_without_index_is_discovered_by_status_or_playback(
        self,
    ):
        e = self.engine
        e.store.data["member:3", "account"] = copy.deepcopy(self.account)
        await e.initialize()
        self.assertIsNone(await e.get(3, "linked"))
        self.assertTrue((await e.status(3))["connected"])
        self.assertTrue(await e.get(3, "linked"))
        e.store.data["member:4", "account"] = copy.deepcopy(self.account)
        await e.accept(
            "completed",
            {**self.event, "member_id": 4},
            "legacy-discovery",
            iso_time(self.now),
        )
        self.assertTrue(await e.get(4, "linked"))
        self.assertIsNotNone(await e.get(4, "delivery"))

    async def test_disconnect_clears_stale_member_health(self):
        self.engine.health.ok = AsyncMock()
        await self.engine.disconnect(0)
        self.engine.health.ok.assert_awaited_once_with("member-0")
