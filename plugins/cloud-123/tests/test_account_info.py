"""真实 SDK + P123Client + HTTP 模拟；不访问真实账号。"""

from __future__ import annotations

import asyncio
import base64
import json
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlsplit

import httpx
from cloud_123.backend import LOGIN_TTL_S, Pan123
from movieclaw_sdk import drives as dv
from movieclaw_sdk import net


def token(label: str) -> str:
    # JWT 内容只是构造器输入，身份必须从 user/info 响应验证。
    payload = base64.urlsafe_b64encode(json.dumps({"id": "untrusted", "username": label}).encode())
    return f"header.{payload.decode().rstrip('=')}.signature"


def tree_entry(file_id, name, kind=0, **fields):
    return {"FileId": file_id, "FileName": name, "Type": kind,
            "Size": 0, "Etag": "a" * 32, **fields}


class AccountContractsTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.backend = Pan123()
        self.account = dv.DriveAccount("12345", {"token": token("first")})
        self.data = {"UID": 12345, "Nickname": "测试用户", "Vip": True, "SpaceUsed": 1024}
        self.payload = {"code": 0, "data": self.data}
        self.status = 200
        self.headers = {}
        self.requests = []
        self.closed = 0
        self.paced = 0
        self.raw = None
        self.failure = None
        self.close_failure = None
        self.gate = None
        self.entered = asyncio.Event()
        self.origins = []
        self.reply = None

        test = self

        class Transport(httpx.MockTransport):
            async def aclose(self):
                test.closed += 1
                await super().aclose()
                if test.close_failure is not None:
                    raise test.close_failure

        async def handler(request):
            self.requests.append(request)
            self.entered.set()
            if self.gate is not None:
                await self.gate.wait()
            if self.failure is not None:
                raise self.failure
            if self.reply is not None:
                return await self.reply(request)
            if self.raw is not None:
                return httpx.Response(self.status, content=self.raw, headers=self.headers)
            return httpx.Response(self.status, json=self.payload, headers=self.headers)

        def transport(service):
            self.origins.append(service)
            return net._Paced(Transport(handler))

        async def pace():
            self.paced += 1

        self.patch = mock.patch.object(net, "http_transport", side_effect=transport)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        pacer_token = net.pacer.set(pace)
        self.addCleanup(net.pacer.reset, pacer_token)

    async def _begin_qr(self):
        self.payload = {"code": 0, "data": {
            "uniID": "qr-id&other=1", "url": "https://www.123pan.com/qr-login",
        }}
        return await self.backend.begin_login({})

    async def test_ensure_dir_creates_levels_and_verifies_exact_names(self):
        graph = {"0": [tree_entry(1, "剧集", 1)], "1": []}
        next_id = 2

        async def reply(request):
            nonlocal next_id
            if request.method == "POST":
                body = json.loads(request.content)
                parent, name = body["parentfileid"], body["filename"]
                graph[parent].append(tree_entry(next_id, name, 1))
                graph[str(next_id)] = []
                next_id += 1
                # 不信任创建响应中的 id；必须重新精确查询。
                return httpx.Response(200, json={"code": 0, "data": {"Info": {"FileId": 999}}})
            return httpx.Response(200, json={"code": 0, "data": {
                "InfoList": graph[request.url.params["parentfileid"]], "Next": -1,
            }})

        self.reply = reply
        result = await self.backend.ensure_dir(self.account, "", "剧集/第一季/花絮 #1")
        self.assertEqual(result, "3")
        self.assertEqual([r.method for r in self.requests], ["GET", "GET", "POST", "GET", "GET", "POST", "GET"])
        posts = [r for r in self.requests if r.method == "POST"]
        for request, parent, name in zip(posts, ("1", "2"), ("第一季", "花絮 #1")):
            self.assertEqual(str(request.url), "https://123pan.com/b/api/file/upload_request")
            self.assertEqual(json.loads(request.content), {
                "filename": name, "parentfileid": parent, "driveid": 0,
                "duplicate": 0, "etag": "", "size": 0, "type": 1, "notreuse": False,
            })
            self.assertEqual(request.headers["authorization"], "Bearer " + token("first"))
        self.assertEqual((self.paced, self.closed), (7, 7))
        self.assertEqual(self.origins, ["cloud-123"] * 7)
        self.assertEqual(await self.backend.ensure_dir(self.account, "", "剧集/第一季/花絮 #1"), "3")
        self.assertEqual(sum(r.method == "POST" for r in self.requests), 2)

    async def test_ensure_dir_empty_path_returns_parent_without_network_or_credentials(self):
        self.account.credentials = {}
        for parent in ("", "0", "0037", "37"):
            self.assertEqual(await self.backend.ensure_dir(self.account, parent, ""), parent)
        self.assertEqual(self.requests, [])

    async def test_ensure_dir_validates_entire_path_before_creating_any_level(self):
        for path in (None, 3, "/abs", "a/", "a//b", "a/.", "a/..", "a/b\\c", "a/b\x00c"):
            with self.subTest(path=path), self.assertRaises(dv.Conflict):
                await self.backend.ensure_dir(self.account, "", path)
        with self.assertRaises(dv.NotFound):
            await self.backend.ensure_dir(self.account, "abc", "dir")
        self.account.credentials = {}
        with self.assertRaises(dv.AuthExpired):
            await self.backend.ensure_dir(self.account, "", "dir")
        self.assertEqual(self.requests, [])

    async def test_ensure_dir_scans_all_pages_before_reuse_or_creation(self):
        for last, error in ((tree_entry(1, "目标", 1), None),
                            (tree_entry(2, "目标", 1), dv.Conflict),
                            (tree_entry(2, "目标"), dv.Conflict),
                            (tree_entry(1, "改名", 1), dv.Transient)):
            async def reply(request):
                first = request.url.params["next"] == "0"
                return httpx.Response(200, json={"code": 0, "data": {
                    "InfoList": [tree_entry(1, "目标", 1)] if first else [last],
                    "Next": 1 if first else -1, "Total": 0,
                }})
            self.reply = reply
            sent = len(self.requests)
            if error:
                with self.assertRaises(error):
                    await self.backend.ensure_dir(self.account, "", "目标")
            else:
                self.assertEqual(await self.backend.ensure_dir(self.account, "", "目标"), "1")
            self.assertEqual(len(self.requests), sent + 2)
            self.assertTrue(all(r.method == "GET" for r in self.requests))

    async def test_ensure_dir_later_page_failure_never_creates_or_returns_early(self):
        for initial in ([tree_entry(1, "目标", 1)], [tree_entry(1, "其他", 1)]):
            async def reply(request):
                first = request.url.params["next"] == "0"
                return httpx.Response(200, json={"code": 0, "data": {
                    "InfoList": initial if first else [], "Next": 1 if first else 2,
                }})
            self.reply = reply
            with self.assertRaises(dv.ApiChanged):
                await self.backend.ensure_dir(self.account, "", "目标")
        self.assertTrue(all(r.method == "GET" for r in self.requests))

    async def test_ensure_dir_creation_race_requires_exact_unambiguous_directory(self):
        for items, error in (([tree_entry(2, "目标", 1)], None),
                             ([tree_entry(2, "目标")], dv.Conflict),
                             ([tree_entry(2, "目标", 1), tree_entry(3, "目标", 1)], dv.Conflict),
                             ([tree_entry(2, "目标 (1)", 1)], dv.Transient),
                             ([], dv.Transient)):
            created = False

            async def reply(request):
                nonlocal created
                if request.method == "POST":
                    created = True
                    return httpx.Response(200, json={"code": 0, "data": {"Reuse": True}})
                return httpx.Response(200, json={"code": 0, "data": {
                    "InfoList": items if created else [], "Next": -1,
                }})
            self.reply = reply
            sent = len(self.requests)
            if error:
                with self.assertRaises(error):
                    await self.backend.ensure_dir(self.account, "", "目标")
            else:
                self.assertEqual(await self.backend.ensure_dir(self.account, "", "目标"), "2")
            self.assertEqual([r.method for r in self.requests[sent:]], ["GET", "POST", "GET"])

    async def test_ensure_dir_timeout_or_close_failure_recovers_by_lookup_on_next_call(self):
        for timeout in (True, False):
            created = False

            async def reply(request):
                nonlocal created
                if request.method == "POST":
                    created = True
                    self.close_failure = OSError("secret-close")
                    if timeout:
                        raise httpx.ReadTimeout("secret-timeout")
                    return httpx.Response(200, json={"code": 0, "data": {}})
                return httpx.Response(200, json={"code": 0, "data": {
                    "InfoList": [tree_entry(1, "目标", 1)] if created else [], "Next": -1,
                }})
            self.reply = reply
            sent = len(self.requests)
            with self.assertRaises(dv.Transient) as caught:
                await self.backend.ensure_dir(self.account, "", "目标")
            self.assertNotIn("secret", str(caught.exception))
            if timeout:
                self.assertIn("连接失败", str(caught.exception))
            self.close_failure = None
            self.assertEqual(await self.backend.ensure_dir(self.account, "", "目标"), "1")
            self.assertEqual([r.method for r in self.requests[sent:]], ["GET", "POST", "GET"])
            self.assertEqual(self.closed, len(self.requests))

    async def test_ensure_dir_post_errors_preserve_categories_and_do_not_retry(self):
        for code, error in ((401, dv.AuthExpired), (429, dv.RateLimited), (5066, dv.NotFound),
                            (1, dv.Transient), (None, dv.ApiChanged)):
            self.close_failure = None

            async def reply(request):
                if request.method == "POST":
                    self.close_failure = OSError("secret-close")
                    return httpx.Response(200, json={"code": code, "message": "secret"})
                return httpx.Response(200, json={"code": 0, "data": {"InfoList": [], "Next": -1}})
            self.reply = reply
            sent = len(self.requests)
            with self.assertRaises(error) as caught:
                await self.backend.ensure_dir(self.account, "", "目标/下一层")
            self.assertNotIn("secret", str(caught.exception))
            self.assertEqual([r.method for r in self.requests[sent:]], ["GET", "POST"])
            self.assertEqual(self.closed, len(self.requests))

    async def test_ensure_dir_cancelled_creation_is_not_retried_and_recovers_next_call(self):
        entered, release = asyncio.Event(), asyncio.Event()
        created = False

        async def reply(request):
            nonlocal created
            if request.method == "POST":
                created = True
                self.close_failure = OSError("secret-close")
                entered.set()
                await release.wait()
            return httpx.Response(200, json={"code": 0, "data": {
                "InfoList": [tree_entry(1, "目标", 1)] if created else [], "Next": -1,
            }})
        self.reply = reply
        task = asyncio.create_task(self.backend.ensure_dir(self.account, "", "目标"))
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel("ensure-stop")
        with self.assertRaises(asyncio.CancelledError) as caught:
            await task
        self.assertEqual(caught.exception.args, ("ensure-stop",))
        self.assertTrue(task.cancelled())
        self.close_failure = None
        self.assertEqual(await self.backend.ensure_dir(self.account, "", "目标"), "1")
        self.assertEqual([r.method for r in self.requests], ["GET", "POST", "GET"])
        self.assertEqual(self.closed, 3)

    async def test_ensure_dir_budget_can_block_creation_before_sending_post(self):
        self.payload = {"code": 0, "data": {"InfoList": [], "Next": -1}}

        async def deny_write():
            self.paced += 1
            if self.paced == 2:
                raise dv.RateLimited("budget exhausted")
        context = net.pacer.set(deny_write)
        try:
            with self.assertRaises(dv.RateLimited):
                await self.backend.ensure_dir(self.account, "", "目标")
        finally:
            net.pacer.reset(context)
        self.assertEqual([r.method for r in self.requests], ["GET"])
        self.assertEqual((self.paced, self.closed), (2, 2))

    async def test_ensure_dir_verification_failure_never_returns_created_id(self):
        posted = False

        async def reply(request):
            nonlocal posted
            if request.method == "POST":
                posted = True
                return httpx.Response(200, json={"code": 0, "data": {"Info": {"FileId": 1}}})
            if posted:
                return httpx.Response(503, text="secret")
            return httpx.Response(200, json={"code": 0, "data": {"InfoList": [], "Next": -1}})
        self.reply = reply
        with self.assertRaises(dv.Transient):
            await self.backend.ensure_dir(self.account, "", "目标/下一层")
        self.assertEqual([r.method for r in self.requests], ["GET", "POST", "GET"])

    async def test_ensure_dir_rejects_ancestor_cycle_without_creating(self):
        async def reply(request):
            root = request.url.params["parentfileid"] == "37"
            return httpx.Response(200, json={"code": 0, "data": {
                "InfoList": [tree_entry(1 if root else 37, "A" if root else "B", 1)], "Next": -1,
            }})
        self.reply = reply
        with self.assertRaises(dv.Transient):
            await self.backend.ensure_dir(self.account, "37", "A/B/C")
        self.assertEqual([r.method for r in self.requests], ["GET", "GET"])

    async def test_list_tree_traverses_all_pages_and_preserves_relative_paths(self):
        pages = {
            ("37", "0"): ([tree_entry(1, "剧集", 1), tree_entry(2, "说明.txt", Size=12)], "2"),
            ("37", "2"): ([tree_entry(3, "空目录", 1)], -1),
            ("1", "0"): ([tree_entry(4, "第一季", 1), tree_entry(5, "海报.jpg", Size="20")], 5),
            ("1", "5"): ([tree_entry(6, "字幕.srt", Size=30)], "-1"),
            ("3", "0"): ([], -1),
            ("4", "0"): ([tree_entry(7, "片名 #1.mkv", Size=2**40)], -1),
        }

        async def reply(request):
            parent, cursor = request.url.params["parentfileid"], request.url.params["next"]
            items, next_cursor = pages[parent, cursor]
            return httpx.Response(200, json={"code": 0, "data": {
                "InfoList": [{**item, "ParentFileId": parent} for item in items],
                "Next": next_cursor, "Total": 0,
            }})

        self.reply = reply
        result = await self.backend.list_tree(self.account, "0037")
        self.assertEqual(result, dv.RemoteTree(files=(
            dv.RemoteFile("2", "说明.txt", 12, parent_id="37", rel_path="说明.txt"),
            dv.RemoteFile("5", "海报.jpg", 20, parent_id="1", rel_path="剧集/海报.jpg"),
            dv.RemoteFile("6", "字幕.srt", 30, parent_id="1", rel_path="剧集/字幕.srt"),
            dv.RemoteFile("7", "片名 #1.mkv", 2**40, parent_id="4", rel_path="剧集/第一季/片名 #1.mkv"),
        ), dir_ids=("1", "3", "4")))
        encoded = json.loads(json.dumps(dv.tree_dict(result)))
        self.assertEqual(dv.tree_from(encoded), result)
        self.assertTrue(all(file["sha1"] is None and "md5" not in file for file in encoded["files"]))
        self.assertEqual([(r.url.params["parentfileid"], r.url.params["next"])
                          for r in self.requests], list(pages))
        self.assertEqual((self.paced, self.closed), (6, 6))
        self.assertEqual(self.origins, ["cloud-123"] * 6)

    async def test_list_tree_empty_root_and_invalid_root(self):
        self.payload = {"code": 0, "data": {"InfoList": [], "Next": -1}}
        self.assertEqual(await self.backend.list_tree(self.account, ""), dv.RemoteTree())
        self.assertEqual(self.requests[0].url.params["parentfileid"], "0")
        for value in (None, 3, "abc", "../1", "１２", "-1"):
            with self.subTest(value=value), self.assertRaises(dv.NotFound):
                await self.backend.list_tree(self.account, value)
        self.assertEqual(len(self.requests), 1)

    async def test_list_tree_deduplicates_files_and_only_visits_each_directory_once(self):
        async def reply(request):
            parent, cursor = request.url.params["parentfileid"], request.url.params["next"]
            if parent == "0":
                items = [tree_entry(1, "目录", 1), tree_entry(2, "零字节", Etag="A" * 32)]
                if cursor != "0":
                    items = [tree_entry("1", "目录", 1), tree_entry("2", "零字节", Size="0")]
                next_cursor = 2 if cursor == "0" else -1
            else:
                items, next_cursor = [], -1
            return httpx.Response(200, json={"code": 0, "data": {"InfoList": items, "Next": next_cursor}})

        self.reply = reply
        result = await self.backend.list_tree(self.account, "")
        self.assertEqual(result, dv.RemoteTree(
            files=(dv.RemoteFile("2", "零字节", 0, parent_id="0", rel_path="零字节"),), dir_ids=("1",)))
        self.assertEqual([r.url.params["parentfileid"] for r in self.requests], ["0", "0", "1"])

    async def test_list_tree_rejects_conflicting_file_metadata(self):
        for changes in ({"Size": 1}, {"FileName": "改名"}, {"Type": 1}):
            with self.subTest(changes=changes):
                self.payload = {"code": 0, "data": {
                    "InfoList": [tree_entry(1, "文件"), tree_entry(1, "文件", **changes)], "Next": -1,
                }}
                with self.assertRaises(dv.Transient):
                    await self.backend.list_tree(self.account, "")

    async def test_list_tree_rejects_cycles_and_cross_parent_duplicates(self):
        # 根 -> A -> 根、根 -> A -> B -> A，以及同一文件出现在两个父目录。
        for graph in (
            {"37": [tree_entry(1, "A", 1)], "1": [tree_entry(37, "根", 1)]},
            {"37": [tree_entry(1, "A", 1)], "1": [tree_entry(2, "B", 1)], "2": [tree_entry(1, "A", 1)]},
            {"37": [tree_entry(1, "A", 1), tree_entry(2, "文件")], "1": [tree_entry(2, "文件")]},
            {"37": [tree_entry(1, "A", 1), tree_entry(2, "B", 1)], "1": [tree_entry(2, "B", 1)]},
        ):
            async def reply(request):
                items = graph[request.url.params["parentfileid"]]
                return httpx.Response(200, json={"code": 0, "data": {"InfoList": items, "Next": -1}})
            self.reply = reply
            with self.subTest(graph=graph), self.assertRaises(dv.Transient):
                await self.backend.list_tree(self.account, "37")

    async def test_list_tree_rejects_ambiguous_paths_and_unsafe_names(self):
        for kind in (0, 1):
            self.payload = {"code": 0, "data": {
                "InfoList": [tree_entry(1, "同名", kind), tree_entry(2, "同名")], "Next": -1,
            }}
            with self.assertRaises(dv.Transient):
                await self.backend.list_tree(self.account, "")
            for name in ("..", ".", "../escape", "/absolute", "a\\b", "a\x00b"):
                self.payload["data"]["InfoList"] = [tree_entry(1, name, kind)]
                with self.subTest(kind=kind, name=name), self.assertRaises(dv.ApiChanged):
                    await self.backend.list_tree(self.account, "")

    async def test_list_tree_validates_size_but_ignores_unused_etag(self):
        for value in (None, True, -1, "-1", "bad", 1.5):
            self.payload = {"code": 0, "data": {"InfoList": [tree_entry(1, "文件", Size=value)], "Next": -1}}
            with self.subTest(size=value), self.assertRaises(dv.ApiChanged):
                await self.backend.list_tree(self.account, "")
        items = [{"FileId": 1, "Type": 0, "FileName": "文件", "Size": 0}]
        items += [tree_entry(1, "文件", Etag=value)
                  for value in (None, True, "", "a" * 31, "g" * 32, "b" * 32, {})]
        self.payload["data"]["InfoList"] = items
        result = await self.backend.list_tree(self.account, "")
        self.assertEqual(result.files, (dv.RemoteFile("1", "文件", 0, parent_id="0", rel_path="文件"),))
        self.assertIsNone(result.files[0].sha1)

    async def test_list_tree_rejects_bad_child_page_without_returning_accumulated_files(self):
        cases = [
            ({"InfoList": [], "Next": 1}, dv.ApiChanged),
            ({"InfoList": [tree_entry(2, "bad", Trashed=True)], "Next": -1}, dv.Transient),
            ({"InfoList": [tree_entry(2, "bad", ParentFileId=99)], "Next": -1}, dv.Transient),
        ]
        for data, error in cases:
            async def reply(request):
                child = request.url.params["parentfileid"] == "1"
                return httpx.Response(200, json={"code": 0, "data": data if child else {
                    "InfoList": [tree_entry(1, "目录", 1), tree_entry(3, "已读取")], "Next": -1,
                }})
            self.reply = reply
            with self.subTest(data=data), self.assertRaises(error):
                await self.backend.list_tree(self.account, "")

    async def test_list_tree_child_api_failure_is_not_a_partial_success(self):
        for code, error in ((5066, dv.NotFound), (401, dv.AuthExpired), (429, dv.RateLimited), (1, dv.Transient)):
            self.close_failure = None
            sent = len(self.requests)

            async def reply(request):
                if request.url.params["parentfileid"] == "0":
                    return httpx.Response(200, json={"code": 0, "data": {
                        "InfoList": [tree_entry(1, "目录", 1), tree_entry(2, "已读取")], "Next": -1,
                    }})
                self.close_failure = OSError("secret-close")
                return httpx.Response(200, json={"code": code, "message": "secret"})

            self.reply = reply
            with self.subTest(code=code), self.assertRaises(error) as caught:
                await self.backend.list_tree(self.account, "")
            self.assertNotIn("secret", str(caught.exception))
            self.assertEqual(len(self.requests), sent + 2)
            self.assertEqual(self.closed, len(self.requests))

    async def test_list_tree_budget_interruption_restarts_entire_tree(self):
        async def reply(request):
            items = [tree_entry(1, "目录", 1), tree_entry(2, "已读取")] if request.url.params["parentfileid"] == "0" else []
            return httpx.Response(200, json={"code": 0, "data": {"InfoList": items, "Next": -1}})

        async def deny_child():
            self.paced += 1
            if self.paced == 2:
                raise dv.RateLimited("budget exhausted")

        self.reply = reply
        context = net.pacer.set(deny_child)
        try:
            with self.assertRaises(dv.RateLimited):
                await self.backend.list_tree(self.account, "")
            result = await self.backend.list_tree(self.account, "")
        finally:
            net.pacer.reset(context)
        self.assertEqual([file.remote_id for file in result.files], ["2"])
        self.assertEqual(result.dir_ids, ("1",))
        self.assertEqual([r.url.params["parentfileid"] for r in self.requests], ["0", "0", "1"])
        self.assertEqual((self.paced, self.closed), (4, 4))

    async def test_list_tree_cancel_survives_close_failure_and_next_call_restarts(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def reply(request):
            child = request.url.params["parentfileid"] == "1"
            if child:
                self.close_failure = OSError("secret-close")
                entered.set()
                await release.wait()
            return httpx.Response(200, json={"code": 0, "data": {
                "InfoList": [] if child else [tree_entry(1, "目录", 1), tree_entry(2, "已读取")], "Next": -1,
            }})

        self.reply = reply
        task = asyncio.create_task(self.backend.list_tree(self.account, ""))
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel("tree-stop")
        with self.assertRaises(asyncio.CancelledError) as caught:
            await task
        self.assertEqual(caught.exception.args, ("tree-stop",))
        self.assertTrue(task.cancelled())
        self.assertEqual((len(self.requests), self.closed), (2, 2))
        self.reply, self.close_failure = None, None
        self.payload = {"code": 0, "data": {"InfoList": [], "Next": -1}}
        self.assertEqual(await self.backend.list_tree(self.account, ""), dv.RemoteTree())
        self.assertEqual(self.requests[-1].url.params["parentfileid"], "0")

    async def test_list_tree_iterates_beyond_python_recursion_depth(self):
        depth = 1100

        async def reply(request):
            parent = int(request.url.params["parentfileid"])
            entry = tree_entry(parent + 1, "d", 1) if parent < depth else tree_entry(parent + 1, "f")
            return httpx.Response(200, json={"code": 0, "data": {"InfoList": [entry], "Next": -1}})

        self.reply = reply
        result = await self.backend.list_tree(self.account, "")
        self.assertEqual(len(result.dir_ids), depth)
        self.assertEqual(result.files[0].rel_path, "d/" * depth + "f")
        self.assertEqual(result.files[0].parent_id, str(depth))
        self.assertEqual((self.paced, self.closed), (depth + 1, depth + 1))

    async def test_list_tree_long_relative_paths_exceed_old_estimated_budget(self):
        long_name = "目" * 200

        async def reply(request):
            parent = int(request.url.params["parentfileid"])
            cursor = int(request.url.params["next"])
            if parent < 100:
                items, next_cursor = [tree_entry(parent + 1, long_name, 1)], -1
            else:
                items = [tree_entry(101 + i, f"影片{i}.mkv") for i in range(cursor, cursor + 100)]
                next_cursor = -1
            return httpx.Response(200, json={"code": 0, "data": {"InfoList": items, "Next": next_cursor}})

        self.reply = reply
        result = await self.backend.list_tree(self.account, "")
        self.assertEqual(len(result.files), 100)
        self.assertEqual(result.files[-1].rel_path, (long_name + "/") * 100 + "影片99.mkv")
        encoded = json.dumps(dv.tree_dict(result), ensure_ascii=True).encode("ascii")
        self.assertGreater(len(encoded), 8 * 1024 * 1024)
        self.assertEqual(dv.tree_from(json.loads(encoded)), result)
        self.assertEqual((len(self.requests), self.closed), (101, 101))

    async def test_list_tree_more_than_ten_thousand_entries_round_trip(self):
        async def reply(request):
            cursor = int(request.url.params["next"])
            items = [tree_entry(i, f"影片{i}.mkv", Size=i) for i in range(cursor + 1, cursor + 101)]
            return httpx.Response(200, json={"code": 0, "data": {
                "InfoList": items, "Next": cursor + 100 if cursor < 10000 else -1, "Total": 0,
            }})

        self.reply = reply
        result = await self.backend.list_tree(self.account, "")
        self.assertEqual(len(result.files), 10_100)
        self.assertEqual(result.files[-1].remote_id, "10100")
        encoded = json.dumps(dv.tree_dict(result), ensure_ascii=True).encode("ascii")
        self.assertEqual(dv.tree_from(json.loads(encoded)), result)
        self.assertEqual((self.paced, self.closed), (101, 101))

    async def test_list_tree_deep_wide_empty_directories_are_all_visited(self):
        async def reply(request):
            parent = int(request.url.params["parentfileid"])
            cursor = int(request.url.params["next"])
            if parent < 100:
                items, next_cursor = [tree_entry(parent + 1, "d" * 100, 1)], -1
            elif parent == 100:
                start = 101 if cursor == 0 else cursor + 1
                end = min(start + 100, 1001)
                items = [tree_entry(i, f"child{i}", 1) for i in range(start, end)]
                next_cursor = end - 1 if end < 1001 else -1
            else:
                items, next_cursor = [], -1
            return httpx.Response(200, json={"code": 0, "data": {"InfoList": items, "Next": next_cursor}})

        self.reply = reply
        result = await self.backend.list_tree(self.account, "")
        self.assertEqual(result.files, ())
        self.assertEqual(set(result.dir_ids), {str(i) for i in range(1, 1001)})
        # 900 个空叶目录的完整路径累计超过原 8 MiB 预算，但都应完成遍历。
        self.assertEqual({int(r.url.params["parentfileid"]) for r in self.requests}, set(range(1001)))
        self.assertEqual((len(self.requests), self.closed), (1009, 1009))

    async def test_list_dir_exhausts_cursor_pages_and_only_returns_directories(self):
        pages = {
            "0": {"InfoList": [
                {"FileId": fid, "FileName": f"条目{fid}", "Type": int(fid == 2),
                 "ParentFileId": 0, "Trashed": False, "Status": 0}
                for fid in range(1, 101)
            ], "Next": "100", "Total": 0},
            "100": {"InfoList": [
                {"FileId": "101", "FileName": "短页目录", "Type": 1, "ParentFileId": "0"},
            ], "Next": 101, "Total": 0},
            "101": {"InfoList": [
                {"FileId": 102, "FileName": "末页目录", "Type": 1},
            ], "Next": "-1", "Total": 0},
        }

        async def reply(request):
            return httpx.Response(200, json={"code": 0, "data": pages[request.url.params["next"]]})

        self.reply = reply
        result = await self.backend.list_dir(self.account, "")
        self.assertEqual(result, [dv.RemoteDir("2", "条目2"), dv.RemoteDir("101", "短页目录"),
                                  dv.RemoteDir("102", "末页目录")])
        self.assertEqual([r.url.params["next"] for r in self.requests], ["0", "100", "101"])
        for request in self.requests:
            self.assertEqual(str(request.url).split("?")[0], "https://123pan.com/b/api/file/list")
            self.assertEqual(request.method, "GET")
            self.assertEqual(dict(request.url.params), {
                "parentfileid": "0", "limit": "100", "next": request.url.params["next"],
                "orderby": "file_id", "orderdirection": "asc", "driveid": "0",
                "trashed": "false", "indirectspace": "false", "event": "homeListFile",
            })
            self.assertEqual(request.headers["authorization"], "Bearer " + token("first"))
        self.assertEqual((self.paced, self.closed), (3, 3))
        self.assertEqual(self.origins, ["cloud-123"] * 3)
        self.assertEqual([dv.remote_dir_from(json.loads(json.dumps(dv.remote_dir_dict(d))))
                          for d in result], result)

    async def test_list_dir_accepts_empty_final_page_and_explicit_parent(self):
        self.payload = {"code": 200, "data": {"InfoList": [], "Next": -1}}
        self.assertEqual(await self.backend.list_dir(self.account, "0037"), [])
        self.assertEqual(self.requests[-1].url.params["parentfileid"], "37")
        self.assertEqual(len(self.requests), 1)

    async def test_list_dir_continues_file_only_page_until_empty_final_page(self):
        async def reply(request):
            first = request.url.params["next"] == "0"
            return httpx.Response(200, json={"code": 0, "data": {
                "InfoList": [{"FileId": 1, "FileName": "电影.mkv", "Type": 0}] if first else [],
                "Next": 1 if first else -1,
            }})

        self.reply = reply
        self.assertEqual(await self.backend.list_dir(self.account, ""), [])
        self.assertEqual([r.url.params["next"] for r in self.requests], ["0", "1"])
        self.assertEqual((self.paced, self.closed), (2, 2))

    async def test_list_dir_rejects_invalid_input_before_request(self):
        for dir_id in (None, 3, "abc", "-1", "../1", "１２", " 1"):
            with self.subTest(dir_id=dir_id):
                with self.assertRaises(dv.NotFound) as caught:
                    await self.backend.list_dir(self.account, dir_id)
                encoded = json.loads(json.dumps(dv.error_dict(caught.exception)))
                self.assertEqual(encoded, {
                    "kind": "NotFound", "message": "123 目录 id 格式不正确", "retry_after": None,
                })
                restored = dv.error_from(encoded)
                self.assertIsInstance(restored, dv.NotFound)
                self.assertEqual(restored.message, caught.exception.message)
        for value, error in ((None, dv.AuthExpired), ("", dv.AuthExpired),
                             ("abc\r\nsecret", dv.Conflict), ("中文", dv.Conflict),
                             ("a.%%%bad.signature", dv.Conflict)):
            self.account.credentials = {"token": value}
            with self.subTest(token=value), self.assertRaises(error):
                await self.backend.list_dir(self.account, "")
        self.assertEqual(self.requests, [])

    async def test_list_dir_requires_valid_list_and_progress_even_on_short_pages(self):
        item = {"FileId": 1, "FileName": "目录", "Type": 1}
        cases = [{"InfoList": [], "Next": 1}, {"InfoList": [item], "Next": 0},
                 {"InfoList": None, "Next": -1}, {"Next": -1},
                 {"InfoList": {}, "Next": -1}]
        cases += [{"InfoList": [item], "Next": cursor}
                  for cursor in (None, True, 1.5, "oops", "１", -2, "", "9" * 5000)]
        for data in cases:
            with self.subTest(data=str(data)[:100]):
                self.payload = {"code": 0, "data": data}
                sent = len(self.requests)
                with self.assertRaises(dv.ApiChanged):
                    await self.backend.list_dir(self.account, "")
                self.assertEqual(len(self.requests), sent + 1)

    async def test_list_dir_rejects_repeated_or_backwards_cursor(self):
        for next_cursor in (10, "10", 0, 5):
            with self.subTest(next_cursor=next_cursor):
                sent = len(self.requests)

                async def reply(request):
                    first = request.url.params["next"] == "0"
                    return httpx.Response(200, json={"code": 0, "data": {
                        "InfoList": [{"FileId": 10 if first else 11, "FileName": "目录", "Type": 1}],
                        "Next": 10 if first else next_cursor,
                    }})

                self.reply = reply
                with self.assertRaises(dv.ApiChanged):
                    await self.backend.list_dir(self.account, "")
                self.assertEqual(len(self.requests), sent + 2)

    async def test_list_dir_deduplicates_same_id_but_rejects_conflicting_name_or_type(self):
        first = {"FileId": 1, "FileName": "目录", "Type": 1}
        for change in ({}, {"FileName": "已移动"}, {"Type": 0}):
            with self.subTest(change=change):
                async def reply(request):
                    initial = request.url.params["next"] == "0"
                    return httpx.Response(200, json={"code": 0, "data": {
                        "InfoList": [first if initial else {**first, "FileId": "1", **change}],
                        "Next": 1 if initial else -1,
                    }})
                self.reply = reply
                if change:
                    with self.assertRaises(dv.Transient):
                        await self.backend.list_dir(self.account, "")
                else:
                    self.assertEqual(await self.backend.list_dir(self.account, ""), [dv.RemoteDir("1", "目录")])

    async def test_list_dir_rejects_malformed_entries_without_leaking_names(self):
        valid = {"FileId": 1, "FileName": "目录", "Type": 1}
        changes = [{"FileId": value} for value in (None, True, 0, -1, 1.5, "secret")]
        changes += [{"Type": value} for value in (None, True, "1", 2)]
        changes += [{"FileName": value} for value in (None, "", ".", "..", "../secret", "a\\secret", "\x00secret")]
        changes += [{"Trashed": "false"}, {"ParentFileId": None}]
        for item in (None, [], *({**valid, **change} for change in changes)):
            with self.subTest(item=item):
                self.payload = {"code": 0, "data": {"InfoList": [item], "Next": -1}}
                with self.assertRaises(dv.ApiChanged) as caught:
                    await self.backend.list_dir(self.account, "")
                self.assertNotIn("secret", str(caught.exception))

    async def test_list_dir_does_not_hide_recycled_or_moved_items(self):
        for kind in (0, 1):
            for change in ({"Trashed": True}, {"Trashed": 1}, {"ParentFileId": 2}, {"FileId": 37}):
                with self.subTest(kind=kind, change=change):
                    self.payload = {"code": 0, "data": {"InfoList": [
                        {"FileId": 1, "FileName": "secret", "Type": kind, **change},
                    ], "Next": -1}}
                    with self.assertRaises(dv.Transient) as caught:
                        await self.backend.list_dir(self.account, "37")
                    self.assertNotIn("secret", str(caught.exception))

    async def test_restricted_siblings_do_not_block_listing_or_directory_creation(self):
        created = False

        async def reply(request):
            nonlocal created
            if request.method == "POST":
                self.assertTrue(request.url.path.endswith("/file/upload_request"))
                created = True
                return httpx.Response(200, json={"code": 0, "data": {"Info": {"FileId": 3}}})
            items = []
            if request.url.params["parentfileid"] == "0":
                items = [tree_entry(1, "受限文件", Status=1, PunishFlag=2),
                         tree_entry(2, "已有目录", 1, Status="0", PunishFlag=None)]
                if created:
                    items.append(tree_entry(3, "新目录", 1))
            return httpx.Response(200, json={"code": 0, "data": {"InfoList": items, "Next": -1}})

        self.reply = reply
        self.assertEqual(await self.backend.list_dir(self.account, ""), [dv.RemoteDir("2", "已有目录")])
        tree = await self.backend.list_tree(self.account, "")
        self.assertEqual(tree.files, (dv.RemoteFile("1", "受限文件", 0, parent_id="0", rel_path="受限文件"),))
        self.assertEqual(tree.dir_ids, ("2",))
        self.assertEqual(await self.backend.ensure_dir(self.account, "0", "已有目录"), "2")
        self.assertFalse(created)
        self.assertEqual(await self.backend.ensure_dir(self.account, "0", "新目录"), "3")
        self.assertEqual(sum(r.method == "POST" for r in self.requests), 1)

    async def test_list_dir_later_page_errors_survive_close_failure_and_do_not_retry(self):
        cases = [(200, 401, dv.AuthExpired), (200, 429, dv.RateLimited),
                 (200, 5066, dv.NotFound), (200, 1, dv.Transient), (200, 98765, dv.Transient),
                 (401, 0, dv.AuthExpired), (429, 0, dv.RateLimited), (503, 0, dv.Transient),
                 (302, 0, dv.Transient), (200, None, dv.ApiChanged)]
        for status, code, error in cases:
            with self.subTest(status=status, code=code):
                self.close_failure = None
                sent = len(self.requests)

                async def reply(request):
                    if request.url.params["next"] == "0":
                        return httpx.Response(200, json={"code": 0, "data": {
                            "InfoList": [{"FileId": 1, "FileName": "目录", "Type": 1}], "Next": 1,
                        }})
                    self.close_failure = OSError("secret-close")
                    return httpx.Response(status, json={"code": code, "message": "secret-token"},
                                          headers={"Location": "https://example.com/secret"})

                self.reply = reply
                with self.assertRaises(error) as caught:
                    await self.backend.list_dir(self.account, "")
                self.assertNotIn("secret", str(caught.exception))
                self.assertEqual(len(self.requests), sent + 2)
                self.assertEqual(self.closed, len(self.requests))

    async def test_list_dir_budget_interruption_restarts_from_first_page(self):
        async def reply(request):
            return httpx.Response(200, json={"code": 0, "data": {
                "InfoList": [{"FileId": 1, "FileName": "目录", "Type": 1}],
                "Next": 1 if request.url.params["next"] == "0" else -1,
            }})

        async def deny_second_request():
            self.paced += 1
            if self.paced == 2:
                raise dv.RateLimited("budget exhausted")

        self.reply = reply
        context = net.pacer.set(deny_second_request)
        try:
            with self.assertRaises(dv.RateLimited):
                await self.backend.list_dir(self.account, "")
            result = await self.backend.list_dir(self.account, "")
        finally:
            net.pacer.reset(context)
        self.assertEqual(result, [dv.RemoteDir("1", "目录")])
        self.assertEqual([r.url.params["next"] for r in self.requests], ["0", "0", "1"])
        self.assertEqual((self.paced, self.closed), (4, 4))

    async def test_list_dir_cancellation_on_later_page_survives_close_failure(self):
        second_entered = asyncio.Event()
        gate = asyncio.Event()

        async def reply(request):
            if request.url.params["next"] != "0":
                self.close_failure = OSError("secret-close")
                second_entered.set()
                await gate.wait()
            return httpx.Response(200, json={"code": 0, "data": {
                "InfoList": [{"FileId": 1, "FileName": "目录", "Type": 1}], "Next": 1,
            }})

        self.reply = reply
        task = asyncio.create_task(self.backend.list_dir(self.account, ""))
        await asyncio.wait_for(second_entered.wait(), 2)
        task.cancel("list-stop")
        with self.assertRaises(asyncio.CancelledError) as caught:
            await task
        self.assertEqual(caught.exception.args, ("list-stop",))
        self.assertTrue(task.cancelled())
        self.assertEqual((len(self.requests), self.closed), (2, 2))

    async def test_list_dir_transport_parse_and_close_errors_are_sanitized(self):
        self.payload = {"code": 0, "data": {"InfoList": [], "Next": -1}}
        for failure, raw, close, error in (
            (httpx.ReadTimeout("secret"), None, OSError("secret"), dv.Transient),
            (None, b"secret-not-json", OSError("secret"), dv.ApiChanged),
            (None, None, OSError("secret"), dv.Transient),
        ):
            self.failure, self.raw, self.close_failure = failure, raw, close
            with self.assertRaises(error) as caught:
                await self.backend.list_dir(self.account, "")
            self.assertNotIn("secret", str(caught.exception))
        self.assertEqual((len(self.requests), self.closed), (3, 3))

    async def test_cancel_login_is_local_idempotent_and_isolated(self):
        for status in (0, 1):
            initial = await self._begin_qr()
            other = await self._begin_qr()
            self.payload = {"code": 0, "data": {"loginStatus": status}}
            await self.backend.login_state(initial.flow_id)
            deadline = self.backend._flows[initial.flow_id].expires_at
            sent, closed = len(self.requests), self.closed
            with mock.patch.object(net, "http_transport", side_effect=AssertionError("取消不得联网")):
                self.assertIsNone(await self.backend.cancel_login(initial.flow_id))
                cancelled = await self.backend.login_state(initial.flow_id)
                self.assertEqual(cancelled, dv.LoginFlow(initial.flow_id, "failed", "已取消登录"))
                self.assertIsNone(await self.backend.cancel_login(initial.flow_id))
                self.assertEqual(await self.backend.login_state(initial.flow_id), cancelled)
            self.assertEqual(self.backend._flows[other.flow_id].flow, other)
            self.assertEqual(self.backend._flows[initial.flow_id].expires_at, deadline)
            self.assertEqual((len(self.requests), self.closed), (sent, closed))
            self.assertEqual(dv.flow_from(json.loads(json.dumps(dv.flow_dict(cancelled)))), cancelled)

    async def test_cancel_login_missing_or_restarted_flow_is_noop(self):
        self.assertIsNone(await self.backend.cancel_login("unknown"))
        self.assertEqual(self.backend._flows, {})
        initial = await self._begin_qr()
        restarted = Pan123()
        self.assertIsNone(await restarted.cancel_login(initial.flow_id))
        self.assertEqual(restarted._flows, {})
        self.assertEqual(self.backend._flows[initial.flow_id].flow, initial)
        self.assertEqual(len(self.requests), 1)

    async def test_cancel_login_preserves_remote_terminal_and_local_expiry(self):
        for status in (2, 4):
            initial = await self._begin_qr()
            self.payload = {"code": 0, "data": {"loginStatus": status}}
            terminal = await self.backend.login_state(initial.flow_id)
            sent = len(self.requests)
            await self.backend.cancel_login(initial.flow_id)
            self.assertEqual(await self.backend.login_state(initial.flow_id), terminal)
            self.assertEqual(len(self.requests), sent)
        initial = await self._begin_qr()
        sent = len(self.requests)
        with mock.patch("cloud_123.backend.time") as clock:
            clock.monotonic.return_value = self.backend._flows[initial.flow_id].expires_at
            await self.backend.cancel_login(initial.flow_id)
            expired = await self.backend.login_state(initial.flow_id)
            self.assertEqual(expired.status, "expired")
            await self.backend.cancel_login(initial.flow_id)
            self.assertEqual(await self.backend.login_state(initial.flow_id), expired)
        self.assertEqual(len(self.requests), sent)

    async def test_cancel_login_rejects_late_results_and_errors_from_either_network_stage(self):
        for stage in ("/result", "/info"):
            for status, close_fails in ((200, False), (503, False), (200, True), (503, True)):
                with self.subTest(stage=stage, status=status, close_fails=close_fails):
                    initial = await self._begin_qr()
                    entered, release = asyncio.Event(), asyncio.Event()

                    async def reply(request):
                        if request.url.path.endswith(stage):
                            entered.set()
                            await release.wait()
                            self.close_failure = OSError("secret-close") if close_fails else None
                            code = status
                        else:
                            code = 200
                        data = ({"token": token("late")} if request.url.path.endswith("/result")
                                else self.data)
                        return httpx.Response(code, json={"code": 200, "data": data})

                    self.reply = reply
                    task = asyncio.create_task(self.backend.login_state(initial.flow_id))
                    await asyncio.wait_for(entered.wait(), 2)
                    sent = len(self.requests)
                    self.assertIsNone(await self.backend.cancel_login(initial.flow_id))
                    cancelled = await self.backend.login_state(initial.flow_id)
                    self.assertEqual(cancelled, dv.LoginFlow(initial.flow_id, "failed", "已取消登录"))
                    release.set()
                    self.assertEqual(await task, cancelled)
                    self.assertEqual(await self.backend.login_state(initial.flow_id), cancelled)
                    self.assertEqual(len(self.requests), sent)
                    self.assertEqual(self.closed, len(self.requests))
                    self.reply, self.close_failure = None, None

    async def test_cancel_login_does_not_swallow_task_cancellation(self):
        for stage in ("/result", "/info"):
            initial = await self._begin_qr()
            entered, release = asyncio.Event(), asyncio.Event()

            async def reply(request):
                if request.url.path.endswith(stage):
                    self.close_failure = OSError("secret-close")
                    entered.set()
                    await release.wait()
                return httpx.Response(200, json={"code": 200, "data": {"token": token("late")}})

            self.reply = reply
            task = asyncio.create_task(self.backend.login_state(initial.flow_id))
            await asyncio.wait_for(entered.wait(), 2)
            await self.backend.cancel_login(initial.flow_id)
            task.cancel("rpc-stop")
            with self.assertRaises(asyncio.CancelledError) as caught:
                await task
            self.assertEqual(caught.exception.args, ("rpc-stop",))
            self.assertTrue(task.cancelled())
            self.assertEqual((await self.backend.login_state(initial.flow_id)).status, "failed")
            self.assertEqual(self.closed, len(self.requests))
            self.reply, self.close_failure = None, None

    async def test_cancelled_session_is_cleaned_up_without_extending_its_deadline(self):
        initial = await self._begin_qr()
        deadline = self.backend._flows[initial.flow_id].expires_at
        await self.backend.cancel_login(initial.flow_id)
        with mock.patch("cloud_123.backend.time") as clock:
            clock.monotonic.return_value = deadline
            new = await self._begin_qr()
        self.assertEqual(set(self.backend._flows), {new.flow_id})
        await self.backend.cancel_login(initial.flow_id)
        self.assertEqual((await self.backend.login_state(initial.flow_id)).status, "expired")
        self.assertEqual(set(self.backend._flows), {new.flow_id})
        self.assertEqual(len(self.requests), 2)

    async def test_login_state_maps_nonterminal_states_without_regression_or_poll_loop(self):
        initial = await self._begin_qr()
        for status, expected in ((0, "pending"), (1, "scanned"), (0, "scanned"), (3, "scanned")):
            self.payload = {"code": 0, "data": {"loginStatus": status, "token": token("ignored")}}
            sent = len(self.requests)
            flow = await self.backend.login_state(initial.flow_id)
            self.assertEqual(flow.status, expected)
            self.assertEqual(flow.qr, initial.qr)
            self.assertIsNone(flow.result)
            self.assertEqual(len(self.requests), sent + 1)
            request = self.requests[-1]
            self.assertEqual(str(request.url.copy_with(query=None)),
                             "https://login.123pan.com/api/user/qr-code/result")
            self.assertEqual(dict(request.url.params), {"uniID": "qr-id&other=1"})
            self.assertNotIn("authorization", request.headers)
            self.assertNotIn("cookie", request.headers)
        self.assertEqual((self.closed, self.paced), (5, 5))

    async def test_login_state_cancelled_and_expired_are_cached_terminals(self):
        for status, expected in ((2, "failed"), (4, "expired")):
            initial = await self._begin_qr()
            self.payload = {"code": 0, "data": {"loginStatus": status}}
            flow = await self.backend.login_state(initial.flow_id)
            self.assertEqual(flow.status, expected)
            self.assertIsNone(flow.qr)
            self.assertIsNone(flow.result)
            sent = len(self.requests)
            self.payload = {"code": 200, "data": {"token": token("late")}}
            self.assertEqual(await self.backend.login_state(initial.flow_id), flow)
            self.assertEqual(len(self.requests), sent)

    async def test_login_state_confirms_only_after_live_identity_and_caches_result(self):
        initial = await self._begin_qr()
        secret = token("untrusted")

        async def reply(request):
            if request.url.path.endswith("/result"):
                return httpx.Response(200, json={"code": 200, "data": {"token": "Bearer " + secret}})
            return httpx.Response(200, json={"code": 0, "data": self.data})

        self.reply = reply
        flow = await self.backend.login_state(initial.flow_id)
        self.assertEqual(flow.status, "confirmed")
        self.assertEqual(flow.result, dv.LoginResult(
            "12345", "测试用户", {"token": secret, "login_method": "scan"},
        ))
        self.assertIsNone(flow.qr)
        self.assertEqual(dv.flow_from(json.loads(json.dumps(dv.flow_dict(flow)))), flow)
        self.assertEqual(str(self.requests[-1].url), "https://123pan.com/b/api/user/info")
        self.assertEqual(self.requests[-1].headers["authorization"], "Bearer " + secret)
        self.assertNotIn("authorization", self.requests[-2].headers)
        await self.backend.cancel_login(initial.flow_id)
        self.assertEqual(await self.backend.login_state(initial.flow_id), flow)
        with mock.patch("cloud_123.backend.time") as clock:
            clock.monotonic.return_value = self.backend._flows[initial.flow_id].expires_at + 1
            self.assertEqual(await self.backend.login_state(initial.flow_id), flow)
        self.assertEqual((len(self.requests), self.closed, self.paced), (3, 3, 3))

    async def test_login_state_keeps_two_flows_and_verified_accounts_separate(self):
        first = await self._begin_qr()
        self.payload["data"]["uniID"] = "second-qr"
        second = await self.backend.begin_login({})

        async def reply(request):
            if request.url.path.endswith("/result"):
                label = "first" if request.url.params["uniID"] == "qr-id&other=1" else "second"
                return httpx.Response(200, json={"code": 200, "data": {"token": token(label)}})
            uid = 12345 if request.headers["authorization"] == "Bearer " + token("first") else 67890
            return httpx.Response(200, json={"code": 0, "data": {"UID": uid}})

        self.reply = reply
        one, two = await asyncio.gather(
            self.backend.login_state(first.flow_id), self.backend.login_state(second.flow_id),
        )
        self.assertEqual((one.result.user_id, two.result.user_id), ("12345", "67890"))
        self.assertEqual(one.result.credentials["token"], token("first"))
        self.assertEqual(two.result.credentials["token"], token("second"))
        self.assertEqual(self.backend._flows[first.flow_id].flow, one)
        self.assertEqual(self.backend._flows[second.flow_id].flow, two)

    async def test_login_state_rejects_unknown_states_and_malformed_success_tokens(self):
        initial = await self._begin_qr()
        payloads = [{"code": 0, "data": {"loginStatus": value}}
                    for value in (None, True, False, "1", -1, 5, [], {})]
        payloads += [{"code": 200, "data": {"token": value}}
                     for value in (None, "", 1, "secret\r\nvalue", "a.%%%bad.signature")]
        for payload in payloads:
            with self.subTest(payload=payload):
                self.payload = payload
                sent = len(self.requests)
                with self.assertRaises(dv.ApiChanged) as caught:
                    await self.backend.login_state(initial.flow_id)
                self.assertNotIn("secret", str(caught.exception))
                self.assertEqual(len(self.requests), sent + 1)
                self.assertEqual(self.backend._flows[initial.flow_id].flow, initial)

    async def test_login_state_identity_failure_does_not_confirm_and_can_retry(self):
        initial = await self._begin_qr()
        user_response = None

        async def reply(request):
            if request.url.path.endswith("/result"):
                return httpx.Response(200, json={"code": 200, "data": {"token": token("scan")}})
            return user_response

        self.reply = reply
        for status, payload, error in (
            (401, {}, dv.AuthExpired), (429, {}, dv.RateLimited), (503, {}, dv.Transient),
            (200, {"code": 0, "data": {"UID": False}}, dv.ApiChanged),
        ):
            user_response = httpx.Response(status, json=payload)
            with self.assertRaises(error):
                await self.backend.login_state(initial.flow_id)
            self.assertEqual(self.backend._flows[initial.flow_id].flow, initial)
        user_response = httpx.Response(200, json={"code": 0, "data": self.data})
        self.assertEqual((await self.backend.login_state(initial.flow_id)).status, "confirmed")

    async def test_login_state_missing_and_locally_expired_flows_do_not_send_requests(self):
        self.assertEqual((await self.backend.login_state("unknown")).status, "expired")
        initial = await self._begin_qr()
        self.assertEqual((await Pan123().login_state(initial.flow_id)).status, "expired")
        deadline = self.backend._flows[initial.flow_id].expires_at
        with mock.patch("cloud_123.backend.time") as clock:
            clock.monotonic.return_value = deadline
            expired = await self.backend.login_state(initial.flow_id)
            self.assertEqual(expired.status, "expired")
            self.assertEqual(await self.backend.login_state(initial.flow_id), expired)
        self.assertEqual(len(self.requests), 1)

    async def test_login_state_checks_expiry_after_both_network_stages(self):
        for expire_at in ("/result", "/info"):
            initial = await self._begin_qr()
            deadline = self.backend._flows[initial.flow_id].expires_at
            with mock.patch("cloud_123.backend.time") as clock:
                clock.monotonic.return_value = deadline - 1

                async def reply(request):
                    if request.url.path.endswith(expire_at):
                        clock.monotonic.return_value = deadline
                    if request.url.path.endswith("/result"):
                        return httpx.Response(200, json={"code": 200, "data": {"token": token("late")}})
                    return httpx.Response(200, json={"code": 0, "data": self.data})

                self.reply = reply
                sent = len(self.requests)
                flow = await self.backend.login_state(initial.flow_id)
                self.assertEqual(flow.status, "expired")
                self.assertIsNone(flow.result)
                self.assertEqual(len(self.requests) - sent, 1 if expire_at == "/result" else 2)
            self.reply = None

    async def test_login_state_discards_late_pending_after_concurrent_scanned(self):
        initial = await self._begin_qr()
        entered, release = asyncio.Event(), asyncio.Event()
        calls = 0

        async def reply(request):
            nonlocal calls
            calls += 1
            if calls == 1:
                entered.set()
                await release.wait()
                return httpx.Response(200, json={"code": 0, "data": {"loginStatus": 0}})
            return httpx.Response(200, json={"code": 0, "data": {"loginStatus": 1}})

        self.reply = reply
        task = asyncio.create_task(self.backend.login_state(initial.flow_id))
        await asyncio.wait_for(entered.wait(), 2)
        scanned = await self.backend.login_state(initial.flow_id)
        release.set()
        self.assertEqual(await task, scanned)
        self.assertEqual(scanned.status, "scanned")

    async def test_login_state_discards_late_identity_success_or_error_after_remote_cancel(self):
        for identity_status in (200, 503):
            initial = await self._begin_qr()
            entered, release = asyncio.Event(), asyncio.Event()

            async def reply(request):
                if request.url.path.endswith("/info"):
                    entered.set()
                    await release.wait()
                    return httpx.Response(identity_status, json={"code": 0, "data": self.data})
                payload = ({"code": 0, "data": {"loginStatus": 2}} if entered.is_set()
                           else {"code": 200, "data": {"token": token("late")}})
                return httpx.Response(200, json=payload)

            self.reply = reply
            task = asyncio.create_task(self.backend.login_state(initial.flow_id))
            await asyncio.wait_for(entered.wait(), 2)
            cancelled = await self.backend.login_state(initial.flow_id)
            release.set()
            self.assertEqual(await task, cancelled)
            self.assertEqual(cancelled.status, "failed")
            self.assertIsNone(cancelled.result)
            self.reply = None

    async def test_login_state_does_not_restore_session_removed_during_request(self):
        initial = await self._begin_qr()
        entered, release = asyncio.Event(), asyncio.Event()

        async def reply(request):
            entered.set()
            await release.wait()
            return httpx.Response(200, json={"code": 200, "data": {"token": token("late")}})

        self.reply = reply
        task = asyncio.create_task(self.backend.login_state(initial.flow_id))
        await asyncio.wait_for(entered.wait(), 2)
        self.reply = None
        deadline = self.backend._flows[initial.flow_id].expires_at
        with mock.patch("cloud_123.backend.time") as clock:
            clock.monotonic.return_value = deadline
            new = await self._begin_qr()
        release.set()
        self.assertEqual((await task).status, "expired")
        self.assertEqual(set(self.backend._flows), {new.flow_id})
        self.assertEqual(len(self.requests), 3)

    async def test_login_state_status_errors_remain_classified_even_when_close_fails(self):
        initial = await self._begin_qr()
        self.close_failure = OSError("secret-close")
        for status, payload, error in (
            (401, {}, dv.Transient), (200, {"code": 401}, dv.Transient),
            (429, {}, dv.RateLimited), (200, {"code": 429}, dv.RateLimited),
            (503, {}, dv.Transient), (200, {"code": 1}, dv.Transient),
            (200, {"code": 0, "data": []}, dv.ApiChanged),
        ):
            self.status, self.payload = status, payload
            sent = len(self.requests)
            with self.assertRaises(error) as caught:
                await self.backend.login_state(initial.flow_id)
            self.assertNotIn("secret", str(caught.exception))
            self.assertNotIn("关闭失败", str(caught.exception))
            self.assertEqual(len(self.requests), sent + 1)
            self.assertEqual(self.backend._flows[initial.flow_id].flow, initial)

    async def test_login_state_cancel_during_either_network_stage_survives_close_failure(self):
        for cancel_at in ("/result", "/info"):
            initial = await self._begin_qr()
            entered, release = asyncio.Event(), asyncio.Event()

            async def reply(request):
                if request.url.path.endswith(cancel_at):
                    self.close_failure = OSError("secret-close")
                    entered.set()
                    await release.wait()
                return httpx.Response(200, json={"code": 200, "data": {"token": token("scan")}})

            self.reply = reply
            task = asyncio.create_task(self.backend.login_state(initial.flow_id))
            await asyncio.wait_for(entered.wait(), 2)
            task.cancel("poll-stop")
            with self.assertRaises(asyncio.CancelledError) as caught:
                await task
            self.assertEqual(caught.exception.args, ("poll-stop",))
            self.assertTrue(task.cancelled())
            self.assertEqual(self.backend._flows[initial.flow_id].flow, initial)
            self.assertEqual(len(self.requests), self.closed)
            self.reply, self.close_failure = None, None

    async def test_begin_login_uses_static_api_without_credentials_and_returns_pending(self):
        self.payload = {"code": 200, "data": {
            "uniID": "qr-id&source=other/+", "url": "https://www.123pan.com/qr-login",
        }}
        with mock.patch("p123client.P123Client.__init__", side_effect=AssertionError("不应创建客户端")):
            flow = await self.backend.begin_login({})
        self.assertEqual(flow.status, "pending")
        self.assertFalse(flow.terminal)
        self.assertIsNone(flow.result)
        self.assertNotEqual(flow.flow_id, self.payload["data"]["uniID"])
        self.assertEqual(dv.flow_from(json.loads(json.dumps(dv.flow_dict(flow)))), flow)
        parsed = urlsplit(flow.qr)
        self.assertEqual((parsed.scheme, parsed.netloc, parsed.path),
                         ("https", "www.123pan.com", "/qr-login"))
        self.assertEqual(parse_qs(parsed.query), {
            "env": ["production"], "uniID": ["qr-id&source=other/+"],
            "source": ["123pan"], "type": ["login"],
        })
        request, = self.requests
        self.assertEqual(str(request.url), "https://login.123pan.com/api/user/qr-code/generate")
        self.assertEqual(request.method, "GET")
        self.assertNotIn("authorization", request.headers)
        self.assertNotIn("cookie", request.headers)
        self.assertEqual(self.origins, ["cloud-123"])
        self.assertEqual((self.paced, self.closed), (1, 1))
        session = self.backend._flows[flow.flow_id]
        self.assertEqual(session.uni_id, self.payload["data"]["uniID"])
        self.assertEqual(session.flow, flow)

    async def test_begin_login_normalizes_empty_url_delimiters(self):
        base_url = "https://www.123pan.com/qr-login"
        for suffix in ("", "?", "#", "?#"):
            with self.subTest(suffix=suffix):
                self.payload = {"code": 0, "data": {
                    "uniID": "qr-id&source=other/+", "url": base_url + suffix,
                }}
                flow = await self.backend.begin_login({})
                parsed = urlsplit(flow.qr)
                self.assertEqual(parsed._replace(query="").geturl(), base_url)
                self.assertEqual(parse_qs(parsed.query), {
                    "env": ["production"], "uniID": ["qr-id&source=other/+"],
                    "source": ["123pan"], "type": ["login"],
                })
                self.assertEqual(parsed.fragment, "")
                self.assertEqual(flow.status, "pending")
                self.assertEqual(self.backend._flows[flow.flow_id].flow, flow)

    async def test_begin_login_isolated_from_account_cookies_and_other_instances(self):
        self.headers = {"Set-Cookie": "session=account-secret; Path=/"}
        await self.backend.account_info(self.account)
        self.payload = {"code": 0, "data": {
            "uniID": "qr-id", "url": "https://www.123pan.com/qr-login",
        }}
        other = Pan123()
        first, second, third = await asyncio.gather(
            self.backend.begin_login({}), self.backend.begin_login({}), other.begin_login({}),
        )
        self.assertEqual(len({first.flow_id, second.flow_id, third.flow_id}), 3)
        self.assertEqual(set(self.backend._flows), {first.flow_id, second.flow_id})
        self.assertEqual(set(other._flows), {third.flow_id})
        self.assertEqual(Pan123()._flows, {})
        for request in self.requests[1:]:
            self.assertNotIn("authorization", request.headers)
            self.assertNotIn("cookie", request.headers)
        self.assertEqual((len(self.requests), self.closed), (4, 4))

    async def test_begin_login_prunes_expired_sessions_without_extending_active_deadline(self):
        self.payload = {"code": 0, "data": {
            "uniID": "qr-id", "url": "https://www.123pan.com/qr-login",
        }}
        with mock.patch("cloud_123.backend.time") as clock:
            clock.monotonic.return_value = 1000.0
            first = await self.backend.begin_login({})
            self.assertEqual(self.backend._flows[first.flow_id].expires_at, 1000 + LOGIN_TTL_S)
            clock.monotonic.return_value = 1001.0
            second = await self.backend.begin_login({})
            clock.monotonic.return_value = 1000.0 + LOGIN_TTL_S
            third = await self.backend.begin_login({})
        self.assertEqual(set(self.backend._flows), {second.flow_id, third.flow_id})
        self.assertEqual(self.backend._flows[second.flow_id].expires_at, 1001 + LOGIN_TTL_S)

    async def test_begin_login_rejects_undeclared_options_before_request(self):
        with self.assertRaises(ValueError) as caught:
            await self.backend.begin_login({"token": "secret-token"})
        self.assertNotIn("secret-token", str(caught.exception))
        self.assertEqual(self.requests, [])
        self.assertEqual(self.backend._flows, {})

    async def test_begin_login_rejects_malformed_qr_data_without_creating_session(self):
        valid_url = "https://www.123pan.com/qr-login"
        invalid_data = [{}, [], {"url": valid_url}]
        invalid_data.extend({"uniID": value, "url": valid_url}
                            for value in (None, "", " \n", 123, True, []))
        invalid_data.extend({"uniID": "secret-qr-id", "url": value} for value in (
            None, "", 123, "not-a-url", "http://www.123pan.com/login", "https://[bad",
            "https://user:secret@www.123pan.com/login", "https://@www.123pan.com/login",
            valid_url + "?secret=1", valid_url + "#secret", valid_url + "\n", valid_url + "\x00",
        ))
        for data in invalid_data:
            with self.subTest(data=data):
                self.payload = {"code": 0, "data": data}
                with self.assertRaises(dv.ApiChanged) as caught:
                    await self.backend.begin_login({})
                self.assertNotIn("secret", str(caught.exception))
                self.assertEqual(self.backend._flows, {})
        self.assertEqual(len(self.requests), self.closed)

    async def test_begin_login_classifies_errors_and_refuses_redirect_without_retry(self):
        for status, payload, error in (
            (401, {}, dv.Transient), (429, {}, dv.RateLimited), (503, {}, dv.Transient),
            (302, {}, dv.Transient), (200, {"code": 401}, dv.Transient),
            (200, {"code": 429}, dv.RateLimited), (200, {"code": 1}, dv.Transient),
            (200, {"code": "0"}, dv.ApiChanged), (200, [], dv.ApiChanged),
        ):
            with self.subTest(status=status, payload=payload):
                self.status, self.payload = status, payload
                self.headers = {"Location": "https://unexpected.invalid/secret-token"}
                sent, closed = len(self.requests), self.closed
                with self.assertRaises(error) as caught:
                    await self.backend.begin_login({})
                self.assertNotIn("secret-token", str(caught.exception))
                self.assertEqual((len(self.requests), self.closed), (sent + 1, closed + 1))
                self.assertEqual(self.backend._flows, {})

    async def test_begin_login_401_is_sanitized_transient_even_when_close_fails(self):
        for status in (401, 200):
            for close_failure in (None, OSError("secret-close")):
                with self.subTest(status=status, close_fails=close_failure is not None):
                    self.status = status
                    self.payload = {"code": 401, "message": "secret-response"}
                    self.close_failure = close_failure
                    sent, closed = len(self.requests), self.closed
                    with self.assertRaises(dv.Transient) as caught:
                        await self.backend.begin_login({})
                    self.assertEqual(str(caught.exception), "123 二维码生成暂时不可用，请稍后重试")
                    self.assertIsNone(caught.exception.__cause__)
                    self.assertTrue(caught.exception.__suppress_context__)
                    self.assertNotIn("authorization", self.requests[-1].headers)
                    self.assertEqual((len(self.requests), self.closed), (sent + 1, closed + 1))
                    self.assertEqual(self.backend._flows, {})

    async def test_begin_login_parse_and_network_errors_survive_close_failure(self):
        self.close_failure = OSError("secret-close")
        self.raw = b"<html>secret-response</html>"
        with self.assertRaises(dv.ApiChanged) as caught:
            await self.backend.begin_login({})
        self.assertEqual(str(caught.exception), "123 二维码生成返回了无法解析的响应")
        self.failure = httpx.ReadTimeout("secret-network")
        with self.assertRaises(dv.Transient) as caught:
            await self.backend.begin_login({})
        self.assertEqual(str(caught.exception), "123 二维码生成连接失败，请稍后重试")
        self.payload, self.raw, self.failure = {"code": 429}, None, None
        with self.assertRaises(dv.RateLimited):
            await self.backend.begin_login({})
        self.assertEqual((len(self.requests), self.closed), (3, 3))
        self.assertEqual(self.backend._flows, {})

    async def test_begin_login_close_failure_or_close_cancellation_does_not_create_session(self):
        self.payload = {"code": 0, "data": {
            "uniID": "qr-id", "url": "https://www.123pan.com/qr-login",
        }}
        self.close_failure = OSError("secret-token")
        with self.assertRaises(dv.Transient) as caught:
            await self.backend.begin_login({})
        self.assertEqual(str(caught.exception), "123 二维码生成连接关闭失败，请稍后重试")
        self.close_failure = asyncio.CancelledError("close-stop")
        with self.assertRaises(asyncio.CancelledError) as caught:
            await self.backend.begin_login({})
        self.assertEqual(caught.exception.args, ("close-stop",))
        self.assertEqual(self.backend._flows, {})
        self.assertEqual((len(self.requests), self.closed), (2, 2))

    async def test_begin_login_cancel_survives_close_failure_without_creating_session(self):
        self.gate = asyncio.Event()
        self.close_failure = OSError("secret-token")
        task = asyncio.create_task(self.backend.begin_login({}))
        await asyncio.wait_for(self.entered.wait(), timeout=2)
        task.cancel("qr-stop")
        with self.assertRaises(asyncio.CancelledError) as caught:
            await task
        self.assertEqual(caught.exception.args, ("qr-stop",))
        self.assertTrue(task.cancelled())
        self.assertEqual(self.backend._flows, {})
        self.assertEqual((len(self.requests), self.closed), (1, 1))

    def test_login_form_is_one_required_secret_field_and_survives_sdk_serialization(self):
        spec = dv.backend_spec(self.backend)
        _, login = dv.spec_parts(json.loads(json.dumps(spec)))
        schema = dv.form_schema(login)
        self.assertEqual(schema["required"], ["token"])
        self.assertEqual(set(schema["properties"]), {"token"})
        field = schema["properties"]["token"]
        self.assertEqual(field["type"], "string")
        self.assertEqual(field["format"], "password")
        self.assertTrue(field["writeOnly"])
        self.assertNotIn("default", field)
        self.assertTrue(login.scan)
        self.assertEqual(login.cookie_domains, ())

    async def test_form_login_normalizes_token_and_returns_verified_identity(self):
        secret = token("untrusted-name")
        self.data["Nickname"] = " 测试用户 "
        for value in (secret, f" \n{secret}\t", f" Bearer {secret}\n"):
            with self.subTest(value=value):
                values = {"token": value}
                result = await self.backend.login_with_form(values)
                self.assertEqual(values, {"token": value})
                self.assertEqual(result, dv.LoginResult(
                    "12345", "测试用户", {"token": secret, "login_method": "token"},
                ))
                self.assertEqual(dv.result_from(json.loads(json.dumps(dv.result_dict(result)))), result)
                request = self.requests[-1]
                self.assertEqual(str(request.url), "https://123pan.com/b/api/user/info")
                self.assertEqual(request.method, "GET")
                self.assertEqual(request.headers["authorization"], f"Bearer {secret}")
        self.assertEqual(self.origins, ["cloud-123"] * 3)
        self.assertEqual((len(self.requests), self.paced, self.closed), (3, 3, 3))
        account = dv.DriveAccount(result.user_id, result.credentials)
        self.assertEqual((await self.backend.account_info(account)).display_name, result.display_name)

    async def test_form_login_rejects_bad_input_without_request_or_secret_in_error(self):
        for values in ({}, {"token": None}, {"token": {"secret-token": 1}},
                       {"token": 123}, {"token": ""}, {"token": " \n"},
                       {"token": "Bearer "}, {"token": "Bearer"},
                       {"token": "secret-token\r\ninjected"},
                       {"token": "secret-token\x00"}, {"token": "secret-token\x7f"},
                       {"token": "中文-secret-token"}, {"token": "a.%%%bad.signature"}):
            with self.subTest(values=values):
                with self.assertRaises(ValueError) as caught:
                    await self.backend.login_with_form(values)
                self.assertNotIn("secret-token", str(caught.exception))
        self.assertEqual(self.requests, [])

    async def test_form_login_requires_remote_uid_and_uses_uid_when_name_absent(self):
        for uid in (None, True, 0, -1, "", "unknown", [], "１２３４５"):
            with self.subTest(uid=uid):
                self.data["UID"] = uid
                with self.assertRaises(dv.ApiChanged):
                    await self.backend.login_with_form(self.account.credentials)
        self.payload = {"code": 200, "data": {"UID": "67890"}}
        result = await self.backend.login_with_form(self.account.credentials)
        self.assertEqual((result.user_id, result.display_name), ("67890", "123 用户 67890"))
        self.assertEqual(set(result.credentials), {"token", "login_method"})

    async def test_form_login_preserves_upstream_error_categories_even_when_close_fails(self):
        self.close_failure = OSError("secret-close-token")
        for status, payload, error in (
            (401, {}, dv.AuthExpired), (429, {}, dv.RateLimited), (503, {}, dv.Transient),
            (200, {"code": 401}, dv.AuthExpired), (200, {"code": 429}, dv.RateLimited),
            (200, {"code": 1}, dv.Transient), (200, {"code": 0, "data": []}, dv.ApiChanged),
        ):
            with self.subTest(status=status, payload=payload):
                self.status, self.payload = status, payload
                sent, closed = len(self.requests), self.closed
                with self.assertRaises(error) as caught:
                    await self.backend.login_with_form(self.account.credentials)
                self.assertNotIn("secret", str(caught.exception))
                self.assertNotIn("关闭失败", str(caught.exception))
                self.assertEqual((len(self.requests), self.closed), (sent + 1, closed + 1))

    async def test_form_login_network_failure_is_transient(self):
        self.failure = httpx.ReadTimeout("secret-token")
        with self.assertRaises(dv.Transient) as caught:
            await self.backend.login_with_form(self.account.credentials)
        self.assertNotIn("secret-token", str(caught.exception))
        self.assertEqual((len(self.requests), self.closed), (1, 1))

    async def test_form_login_close_failure_does_not_return_success(self):
        self.close_failure = OSError("secret-token")
        with self.assertRaises(dv.Transient) as caught:
            await self.backend.login_with_form(self.account.credentials)
        self.assertEqual(str(caught.exception), "123 账号查询连接关闭失败，请稍后重试")
        self.assertEqual((len(self.requests), self.closed), (1, 1))

    async def test_form_login_cancel_survives_close_failure(self):
        self.gate = asyncio.Event()
        self.close_failure = OSError("secret-token")
        task = asyncio.create_task(self.backend.login_with_form(self.account.credentials))
        await asyncio.wait_for(self.entered.wait(), timeout=2)
        task.cancel("login-stop")
        with self.assertRaises(asyncio.CancelledError) as caught:
            await task
        self.assertEqual(caught.exception.args, ("login-stop",))
        self.assertTrue(task.cancelled())
        self.assertEqual((len(self.requests), self.closed), (1, 1))

    async def test_account_info_uses_web_endpoint_sdk_pacing_and_live_uid(self):
        info = await self.backend.account_info(self.account)
        self.assertEqual(info, dv.AccountInfo("测试用户", vip="会员", quota_used=1024))
        request, = self.requests
        self.assertEqual(str(request.url), "https://123pan.com/b/api/user/info")
        self.assertEqual(request.method, "GET")
        self.assertEqual(request.headers["authorization"], "Bearer " + self.account.credentials["token"])
        self.assertEqual(self.origins, ["cloud-123"])
        self.assertEqual((self.paced, self.closed), (1, 1))
        self.assertTrue(all(value > 0 for value in request.extensions["timeout"].values()))
        self.assertEqual(dv.info_from(dv.info_dict(info)), info)

    async def test_optional_fields_do_not_invent_capacity_or_membership(self):
        for value in (None, False, -1, "1024", 1.5):
            with self.subTest(value=value):
                self.payload = {"code": 200, "data": {
                    "UID": "12345", "Nickname": "", "Vip": "false",
                    "SpaceUsed": value, "SpacePermanent": 2048, "SpaceTemp": 9999,
                }}
                info = await self.backend.account_info(self.account)
                self.assertEqual(info, dv.AccountInfo("123 用户 12345", quota_total=12047))
        self.data.update(Vip=False, SpaceUsed=0)
        self.payload = {"code": 0, "data": self.data}
        self.assertEqual((await self.backend.account_info(self.account)).quota_used, 0)

    async def test_capacity_uses_current_permanent_and_temporary_bytes(self):
        for permanent, temporary in ((2048, 9999), (2048, 0), (0, 0), (2**50, 2**40)):
            with self.subTest(permanent=permanent, temporary=temporary):
                self.data.update(SpacePermanent=permanent, SpaceTemp=temporary)
                info = await self.backend.account_info(self.account)
                self.assertEqual(info.quota_total, permanent + temporary)
                self.assertEqual(info.quota_used, 1024)
                self.assertEqual(dv.info_from(dv.info_dict(info)), info)

    async def test_capacity_missing_or_invalid_component_is_unknown(self):
        for key in ("SpacePermanent", "SpaceTemp"):
            for value in (None, False, True, -1, "2048", 1.5, []):
                with self.subTest(key=key, value=value):
                    self.data.update(SpacePermanent=2048, SpaceTemp=0)
                    self.data[key] = value
                    info = await self.backend.account_info(self.account)
                    self.assertIsNone(info.quota_total)
                    self.assertEqual(info.quota_used, 1024)
            self.data.update(SpacePermanent=2048, SpaceTemp=0)
            del self.data[key]
            self.assertIsNone((await self.backend.account_info(self.account)).quota_total)

    async def test_invalid_or_missing_uid_is_protocol_error(self):
        for uid in (None, True, 0, -1, "", "unknown", [], "１２３４５"):
            with self.subTest(uid=uid):
                self.data["UID"] = uid
                with self.assertRaises(dv.ApiChanged):
                    await self.backend.account_info(self.account)

    async def test_valid_token_for_another_account_does_not_replace_identity(self):
        self.data["UID"] = 99999
        original = dict(self.account.credentials)
        with self.assertRaises(dv.Conflict):
            await self.backend.account_info(self.account)
        self.assertEqual(self.account.credentials, original)

    async def test_two_accounts_and_relogin_use_current_credentials_without_cookies(self):
        self.headers = {"Set-Cookie": "session=first-account; Path=/"}
        await self.backend.account_info(self.account)
        other = dv.DriveAccount("67890", {"token": token("second")})
        self.data["UID"] = 67890
        await self.backend.account_info(other)
        self.data["UID"] = 12345
        self.account.credentials = {"token": token("new-login")}
        await self.backend.account_info(self.account)
        self.assertEqual([r.headers["authorization"] for r in self.requests], [
            "Bearer " + token("first"), "Bearer " + token("second"), "Bearer " + token("new-login"),
        ])
        self.assertTrue(all("cookie" not in r.headers for r in self.requests))
        self.assertEqual(self.closed, 3)

    async def test_missing_and_unsafe_tokens_do_not_send_requests(self):
        for value, error in ((None, dv.AuthExpired), ("", dv.AuthExpired),
                             ("abc\r\nsecret", dv.Conflict), ("中文", dv.Conflict),
                             ("a.%%%bad.signature", dv.Conflict)):
            with self.subTest(value=value):
                self.account.credentials = {"token": value}
                with self.assertRaises(error):
                    await self.backend.account_info(self.account)
        self.assertEqual(self.requests, [])

    async def test_business_errors_are_classified_without_relogin_or_leaking_response(self):
        for code, error in ((401, dv.AuthExpired), (429, dv.RateLimited),
                            (1, dv.Transient), (5113, dv.Transient), (98765, dv.Transient)):
            with self.subTest(code=code):
                self.payload = {"code": code, "message": "secret-token", "data": {"token": "secret-token"}}
                count = len(self.requests)
                with self.assertRaises(error) as caught:
                    await self.backend.account_info(self.account)
                self.assertNotIn("secret-token", str(caught.exception))
                self.assertEqual(len(self.requests), count + 1)

    async def test_http_errors_and_redirect_do_not_follow_or_expose_body(self):
        for status, error in ((401, dv.AuthExpired), (429, dv.RateLimited),
                              (403, dv.Transient), (503, dv.Transient), (302, dv.Transient)):
            with self.subTest(status=status):
                self.status = status
                self.raw = b"secret-token"
                self.headers = {"Location": "https://unexpected.invalid/"}
                count = len(self.requests)
                with self.assertRaises(error) as caught:
                    await self.backend.account_info(self.account)
                self.assertNotIn("secret-token", str(caught.exception))
                self.assertEqual(len(self.requests), count + 1)

    async def test_malformed_success_is_not_silently_accepted(self):
        for payload in ([], {}, {"code": False}, {"code": "0"}, {"code": 0, "data": []}):
            with self.subTest(payload=payload):
                self.payload = payload
                with self.assertRaises(dv.ApiChanged):
                    await self.backend.account_info(self.account)
        self.raw = b"<html>secret-token</html>"
        with self.assertRaises(dv.ApiChanged) as caught:
            await self.backend.account_info(self.account)
        self.assertNotIn("secret-token", str(caught.exception))

    async def test_network_errors_are_transient_and_close_transport(self):
        for failure in (httpx.ReadTimeout("secret-token"), OSError("secret-token")):
            with self.subTest(failure=type(failure).__name__):
                self.failure = failure
                closed = self.closed
                with self.assertRaises(dv.Transient) as caught:
                    await self.backend.account_info(self.account)
                self.assertNotIn("secret-token", str(caught.exception))
                self.assertEqual(self.closed, closed + 1)

    async def test_host_budget_rejection_stops_http_before_send(self):
        async def denied():
            raise dv.RateLimited("宿主预算已用尽")
        pacer_token = net.pacer.set(denied)
        try:
            with self.assertRaises(dv.RateLimited):
                await self.backend.account_info(self.account)
        finally:
            net.pacer.reset(pacer_token)
        self.assertEqual(self.requests, [])
        self.assertEqual(self.closed, 1)

    async def test_cancel_propagates_and_closes_without_retry(self):
        self.gate = asyncio.Event()
        task = asyncio.create_task(self.backend.account_info(self.account))
        await asyncio.wait_for(self.entered.wait(), timeout=2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual((len(self.requests), self.closed), (1, 1))

    async def test_close_failure_alone_is_sanitized_transient(self):
        for failure in (OSError("secret-token"), httpx.ReadError("secret-token"),
                        RuntimeError("secret-token")):
            with self.subTest(failure=type(failure).__name__):
                self.close_failure = failure
                closed, sent = self.closed, len(self.requests)
                with self.assertRaises(dv.Transient) as caught:
                    await self.backend.account_info(self.account)
                self.assertEqual(str(caught.exception), "123 账号查询连接关闭失败，请稍后重试")
                self.assertIsNone(caught.exception.__cause__)
                self.assertTrue(caught.exception.__suppress_context__)
                self.assertEqual((self.closed, len(self.requests)), (closed + 1, sent + 1))

    async def test_close_failure_preserves_http_and_business_errors(self):
        self.close_failure = OSError("secret-close-token")
        for status, payload, error, message in (
            (401, {}, dv.AuthExpired, "123 登录已失效，请重新登录"),
            (429, {}, dv.RateLimited, "123 请求过于频繁，请稍后重试"),
            (200, {"code": 401}, dv.AuthExpired, "123 登录已失效，请重新登录"),
            (200, {"code": 429}, dv.RateLimited, "123 请求过于频繁，请稍后重试"),
            (200, {"code": 1}, dv.Transient, "123 账号查询暂时失败（业务码 1）"),
            (200, {"code": 0, "data": []}, dv.ApiChanged, "123 账号查询缺少有效数据"),
        ):
            with self.subTest(status=status, payload=payload):
                self.status, self.payload = status, payload
                closed, sent = self.closed, len(self.requests)
                with self.assertRaises(error) as caught:
                    await self.backend.account_info(self.account)
                self.assertEqual(str(caught.exception), message)
                self.assertEqual((self.closed, len(self.requests)), (closed + 1, sent + 1))

    async def test_close_failure_preserves_request_error(self):
        self.failure = httpx.ReadTimeout("secret-request-token")
        self.close_failure = RuntimeError("secret-close-token")
        with self.assertRaises(dv.Transient) as caught:
            await self.backend.account_info(self.account)
        self.assertEqual(str(caught.exception), "123 账号查询连接失败，请稍后重试")
        self.assertEqual((len(self.requests), self.closed), (1, 1))

    async def test_close_failure_does_not_replace_cancellation(self):
        self.gate = asyncio.Event()
        self.close_failure = OSError("secret-close-token")
        task = asyncio.create_task(self.backend.account_info(self.account))
        await asyncio.wait_for(self.entered.wait(), timeout=2)
        task.cancel("review-stop")
        with self.assertRaises(asyncio.CancelledError) as caught:
            await task
        self.assertEqual(caught.exception.args, ("review-stop",))
        self.assertTrue(task.cancelled())
        self.assertEqual((len(self.requests), self.closed), (1, 1))

    async def test_cancellation_from_close_is_not_converted_to_transient(self):
        self.close_failure = asyncio.CancelledError("close-stop")
        with self.assertRaises(asyncio.CancelledError) as caught:
            await self.backend.account_info(self.account)
        self.assertEqual(caught.exception.args, ("close-stop",))
        self.assertEqual((len(self.requests), self.closed), (1, 1))
