"""诊断工具的合成协议测试；不作为 R2/R3 的真实协议证据。"""

import asyncio
import argparse
import base64
import getpass
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import httpx

from tools.probe_upload import Probe, ProbeError, redact
from tools import probe_upload as module


class ProbeTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.content = b"0123456789"
        (self.directory / "sample.bin").write_bytes(self.content)
        self.state = {"salt": "test-salt", "parent": "100", "name": "probe.bin",
                      "size": 10, "md5": hashlib.md5(self.content).hexdigest(), "next_part": 0}
        jwt = base64.urlsafe_b64encode(b'{"id":"synthetic","username":"test"}').decode().rstrip("=")
        self.token = f"header.{jwt}.signature"
        self.requests = []
        self.failure = None
        self.empty = True
        self.account_id = 123
        self.upload = {"Info": {"FileId": 200}, "Reuse": False, "SliceSize": 6,
                       "Bucket": "bucket-secret", "Key": "key-secret",
                       "StorageNode": "node-secret", "UploadId": "upload-secret"}

    async def asyncSetUp(self):
        async def handler(request):
            self.requests.append(request)
            if self.failure:
                await self.failure(request)
            path = request.url.path
            if request.method == "PUT":
                return httpx.Response(200, headers={"etag": hashlib.md5(request.content).hexdigest()})
            if path.endswith("user/info"):
                data = {"UID": self.account_id}
            elif path.endswith("file/list"):
                data = {"InfoList": [] if self.empty else [{"FileId": 1}], "Next": -1}
            elif path.endswith("upload_request"):
                data = self.upload
            elif path.endswith("s3_repare_upload_parts_batch"):
                number = json.loads(request.content)["partNumberStart"]
                data = {"presignedUrls": {str(number): "https://storage.test/put?signature=secret-url"}}
            else:
                # 不假设成品状态和 upload_list 字段语义，仅保存原始结构。
                data = {"unknown_state": "server-value", "Etag": self.state["md5"]}
            return httpx.Response(200, json={"code": 0, "data": data},
                                  headers={"set-cookie": "secret=private-cookie; Domain=.storage.test"})
        self.http = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)
        self.addAsyncCleanup(self.http.aclose)
        self.probe = Probe(self.directory, self.state, self.token, self.http)

    async def start(self):
        await self.probe.identity()
        await self.probe.start()

    async def test_manual_stages_restart_and_no_implicit_success(self):
        await self.start()
        await self.probe.inspect()
        self.http.cookies.set("secret", "private-cookie", domain="storage.test")
        await self.probe.put()
        await self.probe.inspect()
        self.state = json.loads((self.directory / "private.json").read_text())
        self.probe = Probe(self.directory, self.state, self.token, self.http)
        await self.probe.identity()
        await self.probe.put()
        await self.probe.inspect()
        await self.probe.complete()
        await self.probe.inspect()
        puts = [r for r in self.requests if r.method == "PUT"]
        self.assertEqual([r.content for r in puts], [b"012345", b"6789"])
        self.assertTrue(all(not r.headers.get("authorization") and not r.headers.get("cookie") for r in puts))
        complete = [r for r in self.requests if r.url.path.endswith("upload_complete/v2")]
        self.assertEqual(len(complete), 1)
        self.assertIs(json.loads(complete[0].content)["ismultipart"], True)
        self.assertIs(self.state["complete_sent"], True)
        self.assertNotIn("verified", self.state)
        samples = (self.directory / "samples.jsonl").read_text()
        for secret in (self.token, "bucket-secret", "key-secret", "upload-secret", "secret-url", "private-cookie"):
            self.assertNotIn(secret, samples)
        self.assertIn(self.state["md5"], samples)
        self.assertEqual((self.directory / "private.json").stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.directory / "samples.jsonl").stat().st_mode & 0o777, 0o600)

    async def test_nonempty_target_has_no_initialization(self):
        self.empty = False
        with self.assertRaises(ProbeError):
            await self.start()
        self.assertFalse(any(r.url.path.endswith("upload_request") for r in self.requests))

    async def test_unknown_initialization_persists_intent_and_never_retries(self):
        async def fail(request):
            if request.url.path.endswith("upload_request"):
                raise httpx.ReadTimeout("secret")
        self.failure = fail
        with self.assertRaises(httpx.ReadTimeout):
            await self.start()
        state = json.loads((self.directory / "private.json").read_text())
        self.assertEqual(state["pending"], "initialize")
        self.probe = Probe(self.directory, state, self.token, self.http)
        with self.assertRaises(ProbeError):
            await self.probe.start()
        self.assertEqual(sum(r.url.path.endswith("upload_request") for r in self.requests), 1)

    async def test_cancelled_put_keeps_unknown_outcome_and_no_replay(self):
        await self.start()
        async def fail(request):
            if request.method == "PUT":
                raise asyncio.CancelledError("probe-stop")
        self.failure = fail
        with self.assertRaises(asyncio.CancelledError) as caught:
            await self.probe.put()
        self.assertEqual(caught.exception.args, ("probe-stop",))
        state = json.loads((self.directory / "private.json").read_text())
        self.assertEqual(state["pending"], "put:0")
        self.probe = Probe(self.directory, state, self.token, self.http)
        with self.assertRaises(ProbeError):
            await self.probe.put()
        self.assertEqual(sum(r.method == "PUT" for r in self.requests), 1)

    async def test_incomplete_or_repeated_merge_is_rejected(self):
        await self.start()
        with self.assertRaises(ProbeError):
            await self.probe.complete()
        await self.probe.put()
        await self.probe.put()
        await self.probe.complete()
        with self.assertRaises(ProbeError):
            await self.probe.complete()
        self.assertEqual(sum(r.url.path.endswith("upload_complete/v2") for r in self.requests), 1)

    async def test_changed_local_bytes_or_account_stops_writes(self):
        await self.start()
        self.account_id = 999
        with self.assertRaises(ProbeError):
            await self.probe.identity()
        (self.directory / "sample.bin").write_bytes(b"different")
        with self.assertRaises(ProbeError):
            await self.probe.put()
        self.assertFalse(any(r.method == "PUT" for r in self.requests))

    async def test_unknown_upload_shape_is_not_guessed(self):
        self.upload["Info"] = {"OtherId": 200}
        await self.start()
        with self.assertRaises(ProbeError):
            await self.probe.inspect()
        self.assertFalse(any(r.url.path.endswith("file/info") for r in self.requests))

    def test_redaction_preserves_hashes_and_protocol_shape(self):
        result = redact({"code": 0, "UID": 123, "Info": {"FileId": 200, "Size": "10"},
                         "Etag": '"' + "a" * 32 + '"', "presignedUrls": {"1": "https://secret"},
                         "message": "Bearer secret", "IsTruncated": False}, "salt")
        self.assertEqual(result["code"], 0)
        self.assertEqual(result["Info"]["Size"], "10")
        self.assertEqual(result["Etag"], '"' + "a" * 32 + '"')
        self.assertIs(result["IsTruncated"], False)
        self.assertNotIn("https://secret", json.dumps(result))
        self.assertNotIn("Bearer secret", json.dumps(result))
        self.assertNotEqual(result["UID"], 123)

    async def test_top_level_file_id_survives_restart_and_reaches_merge(self):
        self.upload["FileId"] = self.upload.pop("Info")["FileId"]
        await self.start()
        state = json.loads((self.directory / "private.json").read_text())
        self.assertEqual(state["upload"]["FileId"], 200)
        self.probe = Probe(self.directory, state, self.token, self.http)
        await self.probe.inspect()
        await self.probe.put()
        await self.probe.put()
        await self.probe.complete()
        info = next(r for r in self.requests if r.url.path.endswith("file/info"))
        self.assertEqual(json.loads(info.content), {"fileIdList": [{"FileId": "200"}]})
        merge = next(r for r in self.requests if r.url.path.endswith("upload_complete/v2"))
        self.assertEqual(json.loads(merge.content)["fileid"], "200")

    async def test_file_id_sources_must_be_valid_and_agree_before_put(self):
        await self.start()
        self.state["upload"].update(FileId=200, Info={"FileId": "00200"})
        self.assertEqual(self.probe.file_id(), "200")
        for invalid in (201, None, True, 0, "wrong"):
            with self.subTest(value=invalid):
                self.state["upload"]["FileId"] = invalid
                sent = len(self.requests)
                with self.assertRaises(ProbeError):
                    await self.probe.put()
                self.assertEqual(len(self.requests), sent)

    async def test_file_id_missing_nested_key_survives_save_and_rebuild(self):
        initial = dict(self.state)
        for info in ({}, {"FileName": "probe.bin"}, {"FileId": "00200"}):
            with self.subTest(info=info):
                self.state = dict(initial)
                self.probe = Probe(self.directory, self.state, self.token, self.http)
                self.upload.update(FileId=200, Info=info)
                await self.start()
                state = json.loads((self.directory / "private.json").read_text())
                self.assertEqual(state["upload"]["FileId"], 200)
                self.assertEqual("FileId" in state["upload"].get("Info", {}), "FileId" in info)
                restored = Probe(self.directory, state, self.token, self.http)
                self.assertEqual(restored.file_id(), "200")
                sent = len(self.requests)
                await restored.inspect()
                await restored.put()
                requests = self.requests[sent:]
                detail = next(r for r in requests if r.url.path.endswith("file/info"))
                self.assertEqual(json.loads(detail.content), {"fileIdList": [{"FileId": "200"}]})
                self.assertEqual(sum(r.method == "PUT" for r in requests), 1)

    async def test_invalid_nested_file_id_stays_rejected_after_save_and_rebuild(self):
        initial = dict(self.state)
        for invalid in (None, True, 0, -1, "wrong", 201):
            with self.subTest(value=invalid):
                self.state = dict(initial)
                self.probe = Probe(self.directory, self.state, self.token, self.http)
                self.upload.update(FileId=200, Info={"FileId": invalid})
                await self.start()
                state = json.loads((self.directory / "private.json").read_text())
                self.assertIn("FileId", state["upload"]["Info"])
                self.assertEqual(state["upload"]["Info"]["FileId"], invalid)
                restored = Probe(self.directory, state, self.token, self.http)
                sent = len(self.requests)
                with self.assertRaises(ProbeError):
                    await restored.put()
                with self.assertRaises(ProbeError):
                    await restored.complete()
                self.assertEqual(len(self.requests), sent)

    async def test_non_tty_fallback_stops_before_reading_token_or_network(self):
        module.save(self.directory / "private.json", self.state)
        incoming = io.StringIO("must-not-read\n")
        with mock.patch.object(getpass.os, "open", side_effect=OSError), \
             mock.patch.object(getpass.sys, "stdin", incoming), \
             mock.patch.object(getpass.sys, "stderr", io.StringIO()), \
             mock.patch.object(module.httpx, "AsyncClient") as client:
            with self.assertRaises(ProbeError):
                await module.run_locked(argparse.Namespace(command="inspect"), self.directory)
        self.assertEqual(incoming.tell(), 0)
        client.assert_not_called()

    async def test_run_locked_close_failure_preserves_business_network_and_cancel(self):
        module.save(self.directory / "private.json", self.state)
        real_client = httpx.AsyncClient

        class BadClose(httpx.MockTransport):
            async def aclose(self):
                raise OSError("secret-close")

        for original in (ProbeError("business"), httpx.ReadTimeout("network"),
                         asyncio.CancelledError("stop")):
            def factory(**kwargs):
                return real_client(transport=BadClose(lambda r: httpx.Response(200)), **kwargs)
            with mock.patch.object(module.httpx, "AsyncClient", side_effect=factory), \
                 mock.patch.object(getpass, "getpass", return_value=self.token), \
                 mock.patch.object(Probe, "identity", new=mock.AsyncMock(side_effect=original)), \
                 mock.patch.object(Probe, "inspect", new=mock.AsyncMock()) as inspect:
                with self.assertRaises(type(original)) as caught:
                    await module.run_locked(argparse.Namespace(command="inspect"), self.directory)
                self.assertIs(caught.exception, original)
                inspect.assert_not_awaited()

    async def test_run_locked_close_only_failure_is_sanitized_and_new_cancel_propagates(self):
        module.save(self.directory / "private.json", self.state)
        real_client = httpx.AsyncClient
        for close_error in (OSError("secret-close"), asyncio.CancelledError("close-stop")):
            class BadClose(httpx.MockTransport):
                async def aclose(self):
                    raise close_error
            def factory(**kwargs):
                return real_client(transport=BadClose(lambda r: httpx.Response(200)), **kwargs)
            expected = asyncio.CancelledError if isinstance(close_error, asyncio.CancelledError) else ProbeError
            with mock.patch.object(module.httpx, "AsyncClient", side_effect=factory), \
                 mock.patch.object(getpass, "getpass", return_value=self.token), \
                 mock.patch.object(Probe, "identity", new=mock.AsyncMock()), \
                 mock.patch.object(Probe, "inspect", new=mock.AsyncMock()):
                with self.assertRaises(expected) as caught:
                    await module.run_locked(argparse.Namespace(command="inspect"), self.directory)
                self.assertNotIn("secret-close", str(caught.exception))
                if expected is asyncio.CancelledError:
                    self.assertIs(caught.exception, close_error)

    def test_private_evidence_preserves_protocol_values_but_removes_credentials(self):
        etag = '"' + "a" * 32 + '-2"'
        body = {"Status": "completed", "ETag": etag, "NextMarker": "cursor-a",
                "Token": self.token, "cookie": "private-cookie", "signature": "signed-secret",
                "presignedUrls": {"1": "https://storage.test/put?sig=secret"},
                "detail": "echo " + self.token + " https://storage.test/?sig=secret",
                "other": "Bearer another-secret", self.token: "echoed-as-key"}
        self.probe.record("file_info", httpx.Response(200, json=body, headers={"etag": etag}))
        path = self.directory / "evidence.private.jsonl"
        evidence = json.loads(path.read_text())
        self.assertEqual(evidence["response"]["Status"], "completed")
        self.assertEqual(evidence["response"]["ETag"], etag)
        self.assertEqual(evidence["response"]["NextMarker"], "cursor-a")
        self.assertEqual(evidence["etag"], etag)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        report = json.loads((self.directory / "samples.jsonl").read_text())
        self.assertNotEqual(report["response"]["Status"], "completed")
        for secret in (self.token, "private-cookie", "signed-secret", "https://storage.test",
                       "another-secret"):
            self.assertNotIn(secret, path.read_text())
            self.assertNotIn(secret, (self.directory / "samples.jsonl").read_text())

    async def test_report_write_failure_after_put_preserves_unknown_outcome(self):
        await self.start()
        sent_put = False
        async def observe(request):
            nonlocal sent_put
            sent_put = request.method == "PUT"
        self.failure = observe
        real_open = Path.open
        def open_file(path, *args, **kwargs):
            if sent_put and path.name == "samples.jsonl":
                raise OSError("synthetic disk failure")
            return real_open(path, *args, **kwargs)
        with mock.patch.object(Path, "open", open_file):
            with self.assertRaises(OSError):
                await self.probe.put()
        state = json.loads((self.directory / "private.json").read_text())
        self.assertEqual(state["pending"], "put:0")
        self.assertEqual(state["next_part"], 0)
        last = json.loads((self.directory / "evidence.private.jsonl").read_text().splitlines()[-1])
        self.assertEqual(last["operation"], "put:0")
        self.probe = Probe(self.directory, state, self.token, self.http)
        with self.assertRaises(ProbeError):
            await self.probe.put()
        self.assertEqual(sum(r.method == "PUT" for r in self.requests), 1)


if __name__ == "__main__":
    unittest.main()
