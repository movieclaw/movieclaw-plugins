"""Real pinned SDK lifecycle, host admin guard and ASGI routes; Trakt HTTP is mocked."""

import importlib
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import APIRouter, FastAPI, Header
from fastapi.responses import JSONResponse
from movieclaw_api.api.deps import optional_login
from movieclaw_api.exceptions import AppException
from movieclaw_api.plugins.keys import PLUGIN_DATA, PLUGIN_HEALTH, PLUGIN_ROUTES
from movieclaw_api.services.auth import Principal
from movieclaw_api.services.plugin_routes import PluginRoutes
from movieclaw_kernel import DURABLE_EVENTS, DeliveryInfo, Origin
from movieclaw_kernel.observe import current_delivery
from movieclaw_kernel.testing import KernelHarness
from movieclaw_sdk.events import PLAYBACK_COMPLETED, PLAYBACK_PROGRESS, Playback
from movieclaw_sdk.ops import HOST_OPS

from trakt_sync.application import DEFAULT_CLIENT_ID
from trakt_sync.engine import Engine
from trakt_sync.plugin import apply
from trakt_sync.policy import iso_time

from .test_trakt_sync import Health, Store


class HostIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_host_guards_lifecycle_routes_and_event_delivery(self):
        entry = importlib.import_module("trakt_sync.plugin")
        routes_module = importlib.import_module("movieclaw_api.services.plugin_routes")
        for config, expected_id, expected_secret in (
            ({}, DEFAULT_CLIENT_ID, None),
            (
                {"client_id": DEFAULT_CLIENT_ID, "client_secret": "stale-secret"},
                DEFAULT_CLIENT_ID,
                None,
            ),
            (
                {"client_id": " \t", "client_secret": "stale-secret"},
                DEFAULT_CLIENT_ID,
                None,
            ),
            (
                {"client_id": " fixture-client ", "client_secret": "fixture-secret"},
                "fixture-client",
                "fixture-secret",
            ),
        ):
            with self.subTest(config=config):
                engines, requests, subscribers = [], [], {}
                store, app = Store(), FastAPI()
                host_router = APIRouter(prefix="/plugins")
                app.include_router(host_router, prefix="/api/v1")
                service = PluginRoutes(app, lambda _: store)

                class CaptureEngine(Engine):
                    def __init__(self, *args, **kwargs):
                        super().__init__(*args, **kwargs)
                        engines.append(self)

                async def identity(x_test_identity: str | None = Header(default=None)):
                    if x_test_identity is None:
                        return None
                    return Principal(
                        kind=x_test_identity,
                        name="fixture",
                        is_admin=x_test_identity == "admin",
                        member_id=1 if x_test_identity == "member" else None,
                    )

                async def app_error(request, exc):
                    return JSONResponse(
                        status_code=exc.status_code, content={"message": exc.message}
                    )

                app.dependency_overrides[optional_login] = identity
                app.add_exception_handler(AppException, app_error)

                def dispatch(request):
                    requests.append(request)
                    self.assertEqual(request.headers["trakt-api-key"], expected_id)
                    if request.url.path == "/oauth/device/code":
                        return httpx.Response(
                            200,
                            json={
                                "verification_url": "https://auth.trakt.tv/activate",
                                "device_code": "fixture",
                                "user_code": "FIXTURE",
                                "expires_in": 600,
                                "interval": 6,
                            },
                        )
                    if request.url.path == "/oauth/device/token":
                        self.assertEqual(
                            json.loads(request.content).get("client_secret"),
                            expected_secret,
                        )
                        return httpx.Response(
                            200,
                            json={
                                "access_token": "fixture-a",
                                "refresh_token": "fixture-r",
                                "expires_in": 604800,
                            },
                        )
                    if request.url.path == "/users/settings":
                        return httpx.Response(200, json={"user": {"username": "fixture-user"}})
                    return httpx.Response(201, json={"action": "scrobble"})

                def subscribe(event, *, entry_id, listener_id, handler):
                    key = event.name, entry_id, listener_id
                    subscribers[key] = handler
                    return lambda: subscribers.pop(key)

                # Keep the real host route mounting and admin dependencies. Only avoid its
                # unrelated OpenAPI hashing thread; all HTTP stays in process.
                with (
                    patch.object(entry, "Engine", CaptureEngine),
                    patch.object(
                        entry.net,
                        "http_transport",
                        lambda _: httpx.MockTransport(dispatch),
                    ),
                    patch.object(routes_module, "host_router", host_router),
                    patch.object(service, "_changed", lambda: None),
                ):
                    async with KernelHarness() as harness:
                        harness.provide(PLUGIN_DATA, SimpleNamespace(scoped=lambda _: store))
                        harness.provide(PLUGIN_HEALTH, SimpleNamespace(reporter=lambda _: Health()))
                        harness.provide(PLUGIN_ROUTES, service)
                        harness.provide(
                            HOST_OPS,
                            SimpleNamespace(
                                client=AsyncMock(
                                    return_value=SimpleNamespace(call=AsyncMock(return_value=[]))
                                )
                            ),
                        )
                        harness.provide(DURABLE_EVENTS, SimpleNamespace(subscribe=subscribe))
                        fiber = await harness.mount(apply, config=config)
                        self.assertEqual(fiber.state.value, "active")
                        self.assertEqual(len(subscribers), 3)
                        self.assertIs(
                            harness.events.owned_by("trakt-sync")[0].event,
                            PLAYBACK_PROGRESS,
                        )
                        prefix = "/api/v1/plugins/trakt-sync"
                        async with httpx.AsyncClient(
                            transport=httpx.ASGITransport(app),
                            base_url="http://fixture",
                        ) as web:
                            for method, path in (
                                ("GET", "/status"),
                                ("GET", "/accounts"),
                                ("GET", "/activity"),
                                ("POST", "/auth/begin"),
                                ("POST", "/auth/poll"),
                                ("POST", "/disconnect"),
                                ("POST", "/retry"),
                                ("POST", "/retry/missing"),
                                ("POST", "/dismiss/missing"),
                                ("POST", "/acknowledge-dropped"),
                            ):
                                self.assertEqual(
                                    (await web.request(method, prefix + path)).status_code,
                                    401,
                                )
                                self.assertEqual(
                                    (
                                        await web.request(
                                            method,
                                            prefix + path,
                                            headers={"x-test-identity": "member"},
                                        )
                                    ).status_code,
                                    403,
                                )
                            web.headers["x-test-identity"] = "admin"
                            self.assertEqual(
                                (
                                    await web.post(
                                        prefix + "/retry/missing", json={"season": -1, "episode": 0}
                                    )
                                ).status_code,
                                422,
                            )
                            self.assertTrue(
                                (await web.get(prefix + "/status")).json()["configured"]
                            )
                            self.assertEqual(
                                (await web.post(prefix + "/auth/begin")).status_code,
                                200,
                            )
                            self.assertEqual(
                                (await web.post(prefix + "/auth/poll")).json()["state"],
                                "connected",
                            )
                            payload = Playback.model_validate(
                                {
                                    "member_id": 0,
                                    "media": {
                                        "id": 7,
                                        "kind": "movie",
                                        "tmdb_id": 11,
                                        "title": "Fixture",
                                    },
                                    "unit": None,
                                    "position_ms": 90000,
                                    "duration_ms": 100000,
                                    "client": {"device_id": "fixture"},
                                }
                            )
                            delivery = DeliveryInfo(
                                "sdk-event",
                                PLAYBACK_COMPLETED.name,
                                iso_time(engines[0].clock()),
                                Origin(),
                            )
                            token = current_delivery.set(delivery)
                            try:
                                handler = subscribers[
                                    PLAYBACK_COMPLETED.name,
                                    "trakt-sync",
                                    "track-completed",
                                ]
                                await handler(payload)
                                await handler(payload)
                            finally:
                                current_delivery.reset(token)
                            await engines[0].drain(0)
                            status = (await web.get(prefix + "/status")).json()
                            self.assertTrue(status["connected"])
                            self.assertEqual(status["pending"], 0)
                            activity = (await web.get(prefix + "/activity")).json()
                            self.assertEqual(activity["records"][0]["state"], "sent")
                            self.assertNotIn("fixture-a", json.dumps(activity) + json.dumps(status))
                            ledger = await engines[0].ledger(0)
                            ledger["dropped"] = 2
                            await engines[0].save_ledger(0, ledger)
                            self.assertEqual(
                                (await web.post(prefix + "/acknowledge-dropped")).json()[
                                    "acknowledged"
                                ],
                                2,
                            )
                            self.assertIsNone((await web.get(prefix + "/status")).json()["warning"])
                            self.assertEqual(
                                (await web.post(prefix + "/retry/missing")).status_code,
                                409,
                            )
                            self.assertEqual(
                                (await web.get(prefix + "/status?member_id=-1")).status_code,
                                422,
                            )
                            self.assertEqual(
                                sum(r.url.path == "/scrobble/stop" for r in requests), 1
                            )
                            self.assertTrue(
                                (await web.post(prefix + "/disconnect")).json()["disconnected"]
                            )
                            await harness.unmount(fiber)
                            self.assertEqual((await web.get(prefix + "/status")).status_code, 404)
                        self.assertFalse(subscribers)
                        self.assertFalse(harness.events.owned_by("trakt-sync"))
