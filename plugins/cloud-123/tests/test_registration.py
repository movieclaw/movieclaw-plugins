"""真实 SDK 注册/存储门面；数据 API 留在内存，不启动宿主或访问网盘。"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import socket
import subprocess
import sys
import tomllib
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from movieclaw_kernel import Plugin
from movieclaw_sdk import drives as dv
from movieclaw_sdk.runner import RemoteContext
from movieclaw_sdk.services import StoredValue

from cloud_123 import PLUGIN_DATA, apply
from cloud_123 import upload_store
from cloud_123.backend import Pan123
from cloud_123.upload import binding_for


class RegistrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        for name in ("connect", "connect_ex"):
            guard = patch.object(socket.socket, name, side_effect=AssertionError("network forbidden"))
            guard.start()
            self.addCleanup(guard.stop)
        self.api = SimpleNamespace(get=AsyncMock(return_value=StoredValue(found=False)), set=AsyncMock())
        self.runner = SimpleNamespace(service=Mock(return_value=self.api), declare=Mock())
        self.ctx = RemoteContext(self.runner, "cloud-123-test", "123 云盘", None,
                                 tuple(key.name for key in apply.inject))

    async def test_read_selfcheck_before_registration_and_reuse_scoped_store(self):
        async def get(**kwargs):
            self.runner.declare.assert_not_called()
            return StoredValue(found=False)
        self.api.get.side_effect = get
        await apply.apply(self.ctx)
        self.api.get.assert_awaited_once_with(key="upload_selfcheck", scope="global")
        self.api.set.assert_not_awaited()
        self.runner.service.assert_called_once_with("data")
        backend = self.ctx._drives["123"]
        self.assertIsInstance(backend, Pan123)
        self.assertEqual(backend._uploads.data.store.entry_id, self.ctx.entry_id)
        self.assertTrue(backend._uploads.data.checked)
        await backend._uploads.data.ready()
        self.assertEqual(self.api.get.await_count, 1)
        await backend._uploads.data.call("set", scope="upload_data:test", key="info", value={"test": True})
        self.api.set.assert_awaited_once_with(key="info", value={"test": True},
                                            scope="upload_data:test", secret=True)
        declaration = self.runner.declare.call_args.args
        self.assertEqual(declaration[0], "contributions")
        self.assertEqual(declaration[1]["registry"], "drive-backends")
        self.assertEqual(declaration[1]["id"], "123")
        caps, login = dv.spec_parts(json.loads(json.dumps(declaration[1]["item"])))
        self.assertEqual(caps, dv.Capabilities(writable=True, rapid_upload_hashes=("md5",),
                                             recycle_bin=True))
        self.assertTrue(login.scan)
        self.assertEqual(login.form["properties"]["token"]["format"], "password")

    async def test_unavailable_or_corrupt_store_does_not_register_and_redacts(self):
        for error, expected in ((LookupError("secret"), dv.Conflict),
                                (PermissionError("secret"), dv.Conflict),
                                (ValueError("secret"), dv.Conflict),
                                (RuntimeError("secret"), dv.Transient)):
            with self.subTest(error=type(error).__name__):
                self.api.get.side_effect = error
                with self.assertRaises(expected) as caught:
                    await apply.apply(self.ctx)
                self.assertNotIn("secret", str(caught.exception))
                self.assertEqual(self.ctx._drives, {})
                self.runner.declare.assert_not_called()
                self.api.set.assert_not_awaited()

    async def test_missing_injection_fails_before_store_access(self):
        self.ctx._inject = ()
        with self.assertRaises(LookupError):
            await apply.apply(self.ctx)
        self.runner.service.assert_not_called()
        self.runner.declare.assert_not_called()

    async def test_selfcheck_timeout_does_not_register(self):
        async def slow(**kwargs):
            await asyncio.Event().wait()
        self.api.get.side_effect = slow
        timeout = asyncio.timeout
        with patch.object(upload_store.asyncio, "timeout", side_effect=lambda _: timeout(0.01)):
            with self.assertRaises(dv.Transient):
                await apply.apply(self.ctx)
        self.runner.declare.assert_not_called()
        self.api.set.assert_not_awaited()

    async def test_cancel_during_selfcheck_does_not_register_or_write(self):
        entered = asyncio.Event()
        async def wait(**kwargs):
            entered.set()
            await asyncio.Event().wait()
        self.api.get.side_effect = wait
        task = asyncio.create_task(apply.apply(self.ctx))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.runner.declare.assert_not_called()
        self.api.set.assert_not_awaited()

    async def test_each_load_has_own_selfcheck_and_backend(self):
        await apply.apply(self.ctx)
        other = RemoteContext(self.runner, "second-entry", "123 云盘", None, (PLUGIN_DATA.name,))
        await apply.apply(other)
        first, second = self.ctx._drives["123"], other._drives["123"]
        self.assertIsNot(first, second)
        self.assertIsNot(first._uploads.data, second._uploads.data)
        self.assertEqual(second._uploads.data.store.entry_id, "second-entry")
        self.assertEqual(self.api.get.await_count, 2)

    async def test_file_size_is_not_limited_by_sample_size(self):
        await apply.apply(self.ctx)
        backend = self.ctx._drives["123"]
        self.assertIsNone(backend.capabilities.max_file_bytes)
        for size in (41 * 1024**2, 1024**3, 100 * 1024**3):
            binding = binding_for("1", "1", "large.bin", dv.Fingerprint(size, "", "b"*32, ""), None)
            self.assertEqual(binding["expected_size"], size)

    def test_manifest_matches_entry_and_canonical_service(self):
        from movieclaw_api.plugins.keys import PLUGIN_DATA as canonical
        from movieclaw_api.plugins.packages import Manifest, check_compat
        path = Path(__file__).resolve().parents[1] / "movieclaw-plugin.toml"
        manifest = Manifest.model_validate(tomllib.loads(path.read_text()))
        check_compat(manifest, reserved=set(), operations=set())
        self.assertIsInstance(apply, Plugin)
        self.assertEqual(manifest.plugin.id, apply.name)
        self.assertEqual(manifest.plugin.title, apply.title)
        self.assertEqual(manifest.plugin.entry, "cloud_123")
        self.assertEqual(manifest.plugin.runtime, "process")
        self.assertEqual(manifest.requires, {"drive-backends": "^1.5", "plugin-data": "^1.0"})
        self.assertEqual(apply.inject, (PLUGIN_DATA,))
        self.assertEqual((PLUGIN_DATA.name, PLUGIN_DATA.version, PLUGIN_DATA.stability),
                         (canonical.name, canonical.version, canonical.stability))
        self.assertTrue(manifest.permissions.network)
        self.assertFalse(manifest.permissions.operations)
        self.assertFalse(apply.permissions)
        self.assertEqual(manifest.dependencies.lock, "requirements.lock")

    def test_entry_discovery_does_not_import_cloud_client_or_host_modules(self):
        script = '''
import sys
sys.path.insert(0, sys.argv[1])
from movieclaw_sdk.runner import _find_plugin
entry = _find_plugin("cloud_123", "cloud-123")
assert [key.name for key in entry.inject] == ["plugin-data"]
assert "cloud_123.backend" not in sys.modules
assert "p123client" not in sys.modules
assert not any(name.startswith(("movieclaw_api", "movieclaw_db")) for name in sys.modules)
'''
        result = subprocess.run([sys.executable, "-B", "-c", script,
                                 str(Path(__file__).resolve().parents[1])],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
