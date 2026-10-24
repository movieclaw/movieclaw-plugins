"""首次安装和反向代理下的本人配对；全部请求使用注入的传输，不访问真实服务器。"""

import json
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from movieclaw_friends.config import Settings
from movieclaw_friends.native import Settings as NativeSettings
from movieclaw_friends.native_sync import NativeSync
from movieclaw_friends.peers import PeerError
from movieclaw_friends.store import Store
from pydantic import ValidationError


class PairingConnectionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = Store(Path(self.directory.name))
        self.addCleanup(self.store.close)
        self.wire = []
        self.handler = lambda request: httpx.Response(
            200, json={"success": True, "data": {"ok": True}}
        )

        def handle(request):
            self.wire.append(request)
            return self.handler(request)

        self.sync = NativeSync(
            None,
            SimpleNamespace(store=self.store),
            Settings("", "https://host.test", Path(self.directory.name), native=True),
            None,
            transport=httpx.MockTransport(handle),
        )

    async def test_missing_external_address_never_resolves_placeholder(self):
        self.sync.settings = replace(self.sync.settings, public_url="http://movieclaw.invalid")
        with self.assertRaises(HTTPException) as caught:
            await self.sync.request("POST", "/auth/device/authorize")
        self.assertEqual(caught.exception.status_code, 409)
        self.assertIn("地址", caught.exception.detail)
        self.assertEqual(self.wire, [])

    async def test_explicit_local_api_works_without_public_address(self):
        self.sync.settings = replace(
            self.sync.settings,
            public_url="http://movieclaw.invalid",
            upstream_url="http://127.0.0.1:8090",
        )
        self.assertEqual(await self.sync.request("GET", "/auth/me", "fixture-secret"), {"ok": True})
        self.assertEqual(str(self.wire[0].url), "http://127.0.0.1:8090/api/v1/auth/me")
        self.assertEqual(self.wire[0].headers["authorization"], "Bearer fixture-secret")

    async def test_existing_public_origin_remains_default(self):
        await self.sync.request("GET", "/auth/me")
        self.assertEqual(str(self.wire[0].url), "https://host.test/api/v1/auth/me")

    async def test_transport_errors_are_specific_and_do_not_disclose_secrets(self):
        cases = [
            (httpx.ConnectTimeout("private.test token=fixture-secret"), "超时"),
            (httpx.ReadTimeout("private.test token=fixture-secret"), "超时"),
            (httpx.ProxyError("private.test token=fixture-secret"), "代理"),
            (ValueError("private.test token=fixture-secret"), "配置"),
            (httpx.ConnectError("certificate verify failed private.test fixture-secret"), "证书"),
            (httpx.ConnectError("private.test token=fixture-secret"), "连接"),
            (
                httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] private.test fixture-secret"),
                "证书",
            ),
        ]
        for error, expected in cases:
            with self.subTest(error=type(error).__name__, expected=expected):

                def fail(request, failure=error):
                    raise failure

                self.handler = fail
                with self.assertRaises(PeerError) as caught:
                    await self.sync.request(
                        "POST",
                        "/auth/device/authorize",
                        body={"private": "fixture-secret"},
                    )
                message = str(caught.exception)
                self.assertIn(expected, message)
                self.assertNotIn("fixture-secret", message)
                self.assertNotIn("private.test", message)

    async def test_redirects_are_not_followed_or_given_credentials(self):
        self.handler = lambda request: httpx.Response(
            302, headers={"location": "https://private.test/?token=fixture-secret"}
        )
        with self.assertRaises(PeerError) as caught:
            await self.sync.request("GET", "/auth/me", "fixture-secret")
        self.assertIn("重定向", str(caught.exception))
        self.assertNotIn("private.test", str(caught.exception))
        self.assertEqual(len(self.wire), 1)

    async def test_html_auth_gateway_has_specific_error(self):
        self.handler = lambda request: httpx.Response(
            200,
            text="<html>fixture-secret</html>",
            headers={"content-type": "text/html"},
        )
        with self.assertRaises(PeerError) as caught:
            await self.sync.request("POST", "/auth/device/authorize")
        self.assertIn("JSON", str(caught.exception))
        self.assertNotIn("fixture-secret", str(caught.exception))

    async def test_incompatible_json_shapes_do_not_raise_internal_error(self):
        for payload in (
            [],
            None,
            {"success": False, "message": "fixture-secret"},
            {"success": True},
        ):
            with self.subTest(payload=type(payload).__name__):
                self.handler = lambda request, value=payload: httpx.Response(
                    200,
                    content=json.dumps(value),
                    headers={"content-type": "application/json"},
                )
                with self.assertRaises(PeerError) as caught:
                    await self.sync.request("GET", "/auth/me")
                self.assertIn("格式", str(caught.exception))
                self.assertNotIn("fixture-secret", str(caught.exception))

    async def test_missing_interface_reports_version_or_address(self):
        self.handler = lambda request: httpx.Response(404, json={"detail": "fixture-secret"})
        with self.assertRaises(PeerError) as caught:
            await self.sync.request("POST", "/auth/device/authorize")
        self.assertIn("404", str(caught.exception))
        self.assertIn("版本", str(caught.exception))
        self.assertNotIn("fixture-secret", str(caught.exception))

    async def test_pending_authorization_and_delete_empty_response_still_work(self):
        self.handler = lambda request: httpx.Response(202, json={"success": True, "data": None})
        self.assertIsNone(await self.sync.request("POST", "/auth/device/token"))
        self.handler = lambda request: httpx.Response(200, json={"success": True, "data": None})
        self.assertIsNone(await self.sync.request("DELETE", "/auth/devices/current"))

    def test_local_api_setting_canonicalizes_and_rejects_embedded_credentials(self):
        self.assertEqual(
            NativeSettings(native_api_url="http://LOCALHOST:80/").native_api_url,
            "http://localhost",
        )
        for value in (
            "https://user:password@host.test",
            "https://host.test/api/v1",
            "file:///etc/passwd",
            "https://host.test?token=secret",
        ):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                NativeSettings(native_api_url=value)
        self.assertEqual(NativeSettings().native_api_url, "")

    def registered_app(self):
        app = FastAPI()
        self.sync.app = app
        self.sync.user_for = lambda request: {
            "id": "owner",
            "username": "alice",
            "role": "member",
        }

        @app.middleware("http")
        async def authenticated_test_account(request, call_next):
            request.state.friends_login_name = "alice"
            return await call_next(request)

        @app.exception_handler(PeerError)
        async def peer_error(request, exc):
            return JSONResponse({"detail": str(exc)}, status_code=502)

        self.sync.register()
        return app

    async def test_local_pairing_opens_current_browser_origin_without_exposing_backend(
        self,
    ):
        self.sync.settings = replace(
            self.sync.settings,
            public_url="http://movieclaw.invalid",
            upstream_url="http://127.0.0.1:8090",
        )
        self.handler = lambda request: httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "user_code": "ABCD",
                    "device_code": "private-code",
                    "expires_in": 300,
                    "interval": 5,
                },
            },
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.registered_app()),
            base_url="https://browser.test",
        ) as client:
            response = await client.post("/api/watch/native-sync/pair")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["verification_url"], "/activate?code=ABCD")
        self.assertNotIn("127.0.0.1", response.text)
        self.assertNotIn("private-code", response.text)
        self.assertEqual(self.sync.pending["owner"]["origin"], "http://127.0.0.1:8090")

    async def test_changed_endpoint_does_not_receive_old_credential_on_revoke(self):
        self.store.execute(
            "INSERT INTO native_sync_grants(user_id,username,role,account,secret,origin) VALUES(?,?,?,?,?,?)",
            (
                "owner",
                "alice",
                "member",
                "account",
                self.store.encrypt("old-secret"),
                self.store.encrypt("https://host.test"),
            ),
        )
        self.sync.settings = replace(self.sync.settings, upstream_url="https://different.test")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.registered_app()),
            base_url="https://browser.test",
        ) as client:
            status = await client.get("/api/watch/native-sync")
            self.assertFalse(status.json()["bound"])
            response = await client.delete("/api/watch/native-sync")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.wire, [])
        self.assertIsNone(self.store.one("SELECT * FROM native_sync_grants"))

    async def test_changed_endpoint_never_receives_old_pairing_challenge(self):
        self.sync.pending["owner"] = {
            "origin": "https://previous.test",
            "code": "private-code",
            "expires": time.time() + 300,
        }
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.registered_app()),
            base_url="https://browser.test",
        ) as client:
            response = await client.post("/api/watch/native-sync/pair/check")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.wire, [])

    async def test_malformed_pairing_reply_is_controlled_failure(self):
        valid = {
            "user_code": "ABCD",
            "device_code": "private-code",
            "interval": 5,
            "expires_in": 300,
        }
        cases = [
            None,
            [],
            {},
            {**valid, "interval": "5"},
            {**valid, "device_code": None},
            {**valid, "expires_in": False},
        ]
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.registered_app()),
            base_url="https://browser.test",
        ) as client:
            for payload in cases:
                with self.subTest(payload=type(payload).__name__):
                    self.handler = lambda request, value=payload: httpx.Response(
                        200, json={"success": True, "data": value}
                    )
                    response = await client.post("/api/watch/native-sync/pair")
                    self.assertEqual(response.status_code, 502)
                    self.assertIn("格式", response.json()["detail"])
                    self.assertNotIn("private-code", response.text)
                    self.assertEqual(self.sync.pending, {})
