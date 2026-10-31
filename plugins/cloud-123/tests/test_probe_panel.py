"""本地 HTTP 与模拟网盘测试，不连接真实账号。"""

import asyncio
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock
import urllib.error
import urllib.request

import httpx

from tools import probe_panel as panel_module
from tools import probe_upload as probe


class PanelFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name).resolve()
        self.state = {"salt": "synthetic", "parent": "100", "name": "test.bin", "size": 3,
                      "md5": hashlib.md5(b"abc").hexdigest(), "next_part": 0,
                      "upload": {"FileId": 200, "Reuse": False, "SliceSize": 6,
                                 "Bucket": "private-bucket", "Key": "private-key",
                                 "StorageNode": "private-node", "UploadId": "private-session"}}
        probe.save(self.directory / "private.json", self.state)
        (self.directory / "sample.bin").write_bytes(b"abc")
        self.panel = panel_module.Panel(self.directory)

    def execute(self, action, **extra):
        self.panel.execute({"action": action, "revision": self.panel.revision, **extra})


class PanelTest(PanelFixture):
    def test_local_view_excludes_private_state_and_identity_report(self):
        rows = [{"operation": "identity", "response": {"Passport": 19900000000}},
                {"operation": "file_info", "response": {"data": {"infoList": []}}}]
        (self.directory / "samples.jsonl").write_text("\n".join(map(json.dumps, rows)))
        with mock.patch.object(probe, "run") as run:
            self.execute("open", directory=str(self.directory))
            self.execute("token", token="Bearer synthetic-token")
            view = self.panel.view()
            run.assert_not_called()
        self.assertTrue(view["token_ready"])
        self.assertEqual(view["allowed"], ["inspect", "put"])
        serialized = json.dumps(view)
        for secret in ("19900000000", "identity", "private-bucket", "private-key", "private-session", "synthetic-token"):
            self.assertNotIn(secret, serialized)
        self.assertEqual(json.loads(view["report"]), rows[1])
        self.execute("forget")
        self.assertEqual(self.panel.view()["allowed"], [])
        self.assertEqual(self.panel.token, "")

    def test_pending_completed_reuse_and_invalid_session_disable_writes(self):
        self.execute("token", token="synthetic-token")
        for change in ({"pending": "put:0"}, {"complete_sent": True},
                       {"upload": {**self.state["upload"], "Reuse": True}},
                       {"upload": {**self.state["upload"], "FileId": None}}):
            with self.subTest(change=change):
                probe.save(self.directory / "private.json", {**self.state, **change})
                self.assertEqual(self.panel.view()["allowed"], ["inspect"])
                with mock.patch.object(probe, "run") as run:
                    for action in ("put", "complete"):
                        with self.assertRaises(probe.ProbeError):
                            self.execute(action)
                    run.assert_not_called()

    def test_missing_state_and_broken_report_leave_local_controls_available(self):
        self.execute("token", token="synthetic-token")
        (self.directory / "private.json").unlink()
        view = self.panel.view()
        self.assertEqual(view["allowed"], [])
        self.assertTrue(view["token_ready"])
        self.assertIn("本地状态缺失", view["reason"])
        probe.save(self.directory / "private.json", self.state)
        (self.directory / "samples.jsonl").write_text('{"unfinished":')
        view = self.panel.view()
        self.assertEqual(view["allowed"], ["inspect"])
        self.assertEqual(view["report"], "")
        self.assertIn("写操作已禁用", view["reason"])

    def test_stale_request_and_missing_token_never_run(self):
        with mock.patch.object(probe, "run") as run:
            with self.assertRaises(probe.ProbeError):
                self.execute("put")
            self.execute("token", token="synthetic-token")
            with self.assertRaises(probe.ProbeError):
                self.panel.execute({"action": "put", "revision": self.panel.revision - 1})
            run.assert_not_called()

    def test_real_runner_upload_and_complete_use_mock_http_without_getpass(self):
        requests = []
        def handler(request):
            requests.append(request)
            if request.method == "PUT":
                return httpx.Response(200, headers={"etag": self.state["md5"]})
            if request.url.path.endswith("user/info"):
                data = {"UID": 1}
            elif request.url.path.endswith("s3_repare_upload_parts_batch"):
                self.fail("single part must use auth")
            elif request.url.path.endswith("s3_upload_object/auth"):
                data = {"presignedUrls": {"1": "https://storage.test/part"}}
            else:
                data = {}
            return httpx.Response(200, json={"code": 0, "data": data})
        real_client = httpx.AsyncClient
        self.execute("token", token="synthetic-token")
        with mock.patch.object(probe.httpx, "AsyncClient", side_effect=lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw)), \
             mock.patch.object(probe.getpass, "getpass") as getpass:
            self.execute("put")
            self.assertEqual(self.panel.view()["allowed"], ["inspect", "complete"])
            self.execute("complete")
            self.assertEqual(self.panel.view()["allowed"], ["inspect"])
            getpass.assert_not_called()
        self.assertEqual(sum(r.method == "PUT" for r in requests), 1)
        for path in self.directory.iterdir():
            if path.is_file():
                self.assertNotIn(b"synthetic-token", path.read_bytes())

    def test_failed_put_preserves_pending_and_disallows_replay(self):
        real_client = httpx.AsyncClient
        self.execute("token", token="synthetic-token")
        for error in (httpx.ReadTimeout("private-network-detail"), asyncio.CancelledError("stop")):
            def handler(request):
                if request.url.path.endswith("user/info"):
                    return httpx.Response(200, json={"code": 0, "data": {"UID": 1}})
                raise error
            probe.save(self.directory / "private.json", self.state)
            with mock.patch.object(probe.httpx, "AsyncClient", side_effect=lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw)):
                with self.assertRaises(type(error)) as caught:
                    self.execute("put")
                self.assertIs(caught.exception, error)
            self.assertEqual(self.panel.state()["pending"], "put:0")
            self.assertEqual(self.panel.view()["allowed"], ["inspect"])

    def test_new_sample_reuses_bytes_and_retains_failed_directory(self):
        self.execute("token", token="synthetic-token")
        previous = self.directory
        async def failed_run(args, token):
            self.assertEqual(token, "synthetic-token")
            self.assertEqual(args.source, previous / "sample.bin")
            self.assertEqual(args.parent, "123")
            args.workdir.mkdir()
            probe.save(args.workdir / "private.json", {**self.state, "pending": "initialize"})
            raise probe.ProbeError("synthetic error")
        with mock.patch.object(panel_module.tempfile, "gettempdir", return_value=str(self.directory)), \
             mock.patch.object(probe, "run", side_effect=failed_run):
            with self.assertRaises(probe.ProbeError):
                self.execute("start", parent="123", bytes=0, reuse=True)
        self.assertNotEqual(self.panel.directory, previous)
        self.assertTrue((previous / "private.json").exists())
        self.assertEqual(self.panel.view()["allowed"], ["inspect"])


class PanelHTTPTest(PanelFixture):
    def setUp(self):
        super().setUp()
        self.server = panel_module.Server(self.panel)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .01})
        self.thread.start()
        self.addCleanup(self.stop)

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def request(self, data=None, headers=None, path="/api"):
        defaults = {"X-Panel-Key": self.server.key, "Origin": self.server.origin,
                    "Content-Type": "application/json"}
        defaults.update(headers or {})
        request = urllib.request.Request(self.server.origin + path, data=json.dumps(data).encode() if data is not None else None, headers=defaults)
        try:
            return urllib.request.urlopen(request, timeout=3)
        except urllib.error.HTTPError as response:
            return response

    def test_host_origin_key_and_busy_are_enforced(self):
        for headers in ({"Host": "attacker.test"}, {"Origin": "https://attacker.test"}, {"X-Panel-Key": "wrong"}):
            with self.request({"action": "token", "token": "secret", "revision": 0}, headers) as response:
                self.assertEqual(response.status, 403)
        self.assertEqual(self.panel.token, "")
        with self.panel.lock:
            with self.request() as response:
                self.assertEqual(response.status, 409)
        with self.request(path="/") as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])
            self.assertNotIn("__PANEL_KEY__", response.read().decode())

    def test_http_error_hides_original_message_and_report_uses_text(self):
        self.execute("token", token="synthetic-token")
        with mock.patch.object(probe, "run", side_effect=OSError("synthetic-token private-network-detail")):
            with self.request({"action": "put", "revision": self.panel.revision}) as response:
                self.assertEqual(response.status, 400)
                body = response.read().decode()
                self.assertIn("OSError", body)
                self.assertNotIn("synthetic-token", body)
                self.assertNotIn("private-network-detail", body)
        with self.request() as response:
            self.assertEqual(response.status, 200)
            self.assertTrue(json.load(response)["token_ready"])


if __name__ == "__main__":
    unittest.main()
