# SPDX-License-Identifier: GPL-3.0-or-later
"""115 后端对着仿 115 跑全流程：登录、纳管、核验、取直链、上传（秒传 / 区间校验 / 分片 / 续传）、删除、错误归类。"""

from __future__ import annotations

import base64
import hashlib
import unittest
from unittest import mock

from cloud_115.api import Api115
from cloud_115.backend import Pan115

from movieclaw_sdk import drives as dv

from .fake115 import COOKIES, Fake115


def _fingerprint(data: bytes) -> dv.Fingerprint:
    return dv.Fingerprint(
        size=len(data),
        sha1=hashlib.sha1(data).hexdigest().upper(),
        md5=hashlib.md5(data).hexdigest().upper(),
        head_sha1=hashlib.sha1(data[: dv.HEAD_BYTES]).hexdigest().upper(),
    )


class Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.fake = Fake115()
        self.backend = Pan115(Api115(self.fake.transport()))
        self.account = dv.DriveAccount(user_id="10001", credentials={**COOKIES, "app": "alipaymini"})
        media = self.fake.mkdir(0, "影视")
        self.movies = self.fake.mkdir(media, "电影")
        dune = self.fake.mkdir(self.movies, "沙丘2 (2024)")
        self.dune = self.fake.put(dune, "Dune.Part.Two.2024.mkv", b"dune" * 100)
        self.dune_srt = self.fake.put(dune, "Dune.Part.Two.2024.chs.srt", b"subs")
        nolan = self.fake.mkdir(self.movies, "诺兰 & 合集")
        inception = self.fake.mkdir(nolan, "盗梦空间 (2010)")
        self.inception = self.fake.put(inception, "Inception.mkv", b"inception" * 50)
        self.elsewhere = self.fake.put(media, "readme.txt", b"x")


class LoginTest(Base):
    async def test_scan_login_walks_through_states(self) -> None:
        flow = await self.backend.begin_login({"app": "alipaymini"})
        self.assertEqual(flow.status, "pending")
        self.assertEqual(flow.qr, "https://115.com/scan/dg-QRUID")
        self.assertEqual((await self.backend.login_state(flow.flow_id)).status, "pending")
        self.assertEqual((await self.backend.login_state(flow.flow_id)).status, "scanned")
        done = await self.backend.login_state(flow.flow_id)
        self.assertEqual(done.status, "confirmed")
        assert done.result is not None
        self.assertEqual(done.result.user_id, "10001")
        self.assertEqual(done.result.display_name, "影视仓库")
        self.assertEqual(done.result.credentials, {**COOKIES, "app": "alipaymini"})
        # 结束后的流程不再可用
        self.assertEqual((await self.backend.login_state(flow.flow_id)).status, "failed")

    async def test_expired_and_cancelled_codes(self) -> None:
        self.fake.qr_statuses = [-1]
        flow = await self.backend.begin_login({})
        self.assertEqual((await self.backend.login_state(flow.flow_id)).status, "expired")
        self.fake.qr_statuses = [-2]
        flow = await self.backend.begin_login({})
        state = await self.backend.login_state(flow.flow_id)
        self.assertEqual((state.status, state.message), ("failed", "已在手机上取消登录"))

    async def test_unknown_login_end_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            await self.backend.begin_login({"app": "nonsense"})

    async def test_cookie_import_picks_the_four_cookies(self) -> None:
        header = "foo=1; " + "; ".join(f"{k}={v}" for k, v in COOKIES.items()) + "; bar=2"
        result = await self.backend.login_with_cookie(header)
        self.assertEqual(result.credentials, {**COOKIES, "app": "web"})
        with self.assertRaises(ValueError):
            await self.backend.login_with_cookie("UID=1; CID=2")
        self.fake.logged_in = False
        with self.assertRaises(dv.AuthExpired):
            await self.backend.login_with_cookie(header)

    async def test_account_info_and_expired_login(self) -> None:
        info = await self.backend.account_info(self.account)
        self.assertEqual((info.display_name, info.vip), ("影视仓库", "会员"))
        self.assertEqual((info.quota_total, info.quota_used), (60 * 2**40, 41 * 2**40))
        self.fake.logged_in = False
        with self.assertRaises(dv.AuthExpired):
            await self.backend.account_info(self.account)


class BrowseTest(Base):
    async def test_list_dir_lists_subdirectories_across_pages(self) -> None:
        root = await self.backend.list_dir(self.account, "")
        self.assertEqual([d.name for d in root], ["影视"])
        movies = await self.backend.list_dir(self.account, str(self.movies))
        self.assertEqual(sorted(d.name for d in movies), ["沙丘2 (2024)", "诺兰 & 合集"])

    async def test_list_tree_gives_relative_paths_and_hashes(self) -> None:
        tree = await self.backend.list_tree(self.account, str(self.movies))
        files = {f.rel_path: f for f in tree.files}
        self.assertEqual(
            sorted(files),
            [
                "沙丘2 (2024)/Dune.Part.Two.2024.chs.srt",
                "沙丘2 (2024)/Dune.Part.Two.2024.mkv",
                "诺兰 & 合集/盗梦空间 (2010)/Inception.mkv",
            ],
        )
        dune = files["沙丘2 (2024)/Dune.Part.Two.2024.mkv"]
        self.assertEqual(dune.remote_id, str(self.dune))
        self.assertEqual(dune.size, 400)
        self.assertEqual(dune.sha1, hashlib.sha1(b"dune" * 100).hexdigest().upper())
        self.assertEqual(len(tree.dir_ids), 3)

    async def test_missing_directory_is_not_found(self) -> None:
        with self.assertRaises(dv.NotFound):
            await self.backend.list_tree(self.account, "999999")

    async def test_stat_and_direct_link_with_player_user_agent(self) -> None:
        stat = await self.backend.stat(self.account, str(self.dune))
        self.assertEqual((stat.name, stat.size), ("Dune.Part.Two.2024.mkv", 400))
        with mock.patch("p115cipher.rsa_decrypt", base64.b64decode):
            link = await self.backend.direct_link(self.account, str(self.dune), "TestPlayer/1.0")
        self.assertEqual(link.url, f"https://cdnfhnfile.115cdn.net/{self.dune}?t=4102444800&f=1")
        self.assertEqual(link.expires_in, 3600.0)
        with self.assertRaises(dv.NotFound):
            await self.backend.stat(self.account, "424242")

    async def test_errors_are_classified(self) -> None:
        self.fake.fail_errno = 590075
        with self.assertRaises(dv.RateLimited):
            await self.backend.list_dir(self.account, "")
        self.fake.fail_errno = 40101032
        with self.assertRaises(dv.AuthExpired):
            await self.backend.list_dir(self.account, "")
        self.fake.fail_errno = 12345
        with self.assertRaises(dv.DriveError) as ctx:
            await self.backend.list_dir(self.account, "")
        self.assertNotIsInstance(ctx.exception, (dv.AuthExpired, dv.RateLimited))


class ChangesTest(Base):
    async def test_cursor_starts_now_then_returns_new_events_oldest_first(self) -> None:
        self.fake.events = [{"id": 5, "type": 2, "file_id": 1, "parent_id": 2, "file_name": "old", "sha1": "AA"}]
        page = await self.backend.changes(self.account, None)
        self.assertEqual((page.cursor, page.changes), ("5", ()))
        self.fake.events += [
            {
                "id": 6,
                "type": 14,
                "file_id": 70,
                "parent_id": self.movies,
                "file_name": "新片.mkv",
                "sha1": "BB",
                "pick_code": "abc",
            },
            {"id": 7, "type": 8, "file_id": 70, "parent_id": self.movies, "file_name": "新片.mkv", "sha1": "BB"},
            {"id": 8, "type": 17, "file_id": 71, "parent_id": self.movies, "file_name": "新目录", "sha1": ""},
            {"id": 9, "type": 22, "file_id": 72, "parent_id": self.movies, "file_name": "删掉的.mkv", "sha1": "CC"},
        ]
        page = await self.backend.changes(self.account, "5")
        self.assertEqual(page.cursor, "9")
        self.assertEqual(
            [(c.kind, c.remote_id, c.is_dir, c.parent_id) for c in page.changes],
            [
                ("added", "70", False, str(self.movies)),
                ("dir_created", "71", True, str(self.movies)),
                ("removed", "72", False, str(self.movies)),
            ],
        )
        page = await self.backend.changes(self.account, "9")
        self.assertEqual((page.cursor, page.changes), ("9", ()))


class WriteTest(Base):
    async def test_ensure_dir_finds_or_creates_each_level(self) -> None:
        dune_dir = await self.backend.ensure_dir(self.account, str(self.movies), "沙丘2 (2024)")
        self.assertEqual(int(dune_dir), self.fake.nodes[self.dune].parent)
        new = await self.backend.ensure_dir(self.account, str(self.movies), "奥本海默 (2023)/Extras")
        node = self.fake.nodes[int(new)]
        self.assertEqual(node.name, "Extras")
        self.assertEqual(self.fake.nodes[node.parent].name, "奥本海默 (2023)")
        self.assertEqual(await self.backend.ensure_dir(self.account, str(self.movies), ""), str(self.movies))
        # 再来一次走缓存，不重复建
        before = len(self.fake.nodes)
        self.assertEqual(await self.backend.ensure_dir(self.account, str(self.movies), "奥本海默 (2023)/Extras"), new)
        self.assertEqual(len(self.fake.nodes), before)

    async def test_rapid_upload_answers_the_range_challenge(self) -> None:
        data = bytes(range(256)) * 20
        fp = _fingerprint(data)
        self.fake.rapid[fp.sha1] = data
        step = await self.backend.begin_upload(self.account, str(self.movies), "rapid.mkv", fp, None, None)
        self.assertEqual(step.need_range, (100, 100))
        start, length = step.need_range
        step = await self.backend.begin_upload(
            self.account, str(self.movies), "rapid.mkv", fp, step.state, data[start : start + length]
        )
        assert step.file is not None
        node = self.fake.nodes[int(step.file.remote_id)]
        self.assertEqual((node.name, node.data), ("rapid.mkv", data))
        stat = await self.backend.stat(self.account, step.file.remote_id)
        self.assertEqual((stat.size, stat.sha1), (fp.size, fp.sha1))

    async def test_real_upload_in_parts_and_resume(self) -> None:
        data = b"0123456789" * 5000
        fp = _fingerprint(data)
        step = await self.backend.begin_upload(self.account, str(self.movies), "new.mkv", fp, None, None)
        self.assertIsNone(step.file)
        self.assertEqual(step.next_part, 0)
        # 测试里把分片调小，切成多片
        state = {**step.state, "part_size": 12000}
        size = state["part_size"]
        chunks = [data[i : i + size] for i in range(0, len(data), size)]
        state = await self.backend.upload_part(self.account, state, 0, chunks[0])
        state = await self.backend.upload_part(self.account, state, 1, chunks[1])
        # 宿主重启：拿着保存的状态续传，从第 3 片接着传
        restarted = Pan115(Api115(self.fake.transport()))
        step = await restarted.begin_upload(self.account, str(self.movies), "new.mkv", fp, state, None)
        self.assertEqual(step.next_part, 2)
        state = step.state
        for index in range(2, len(chunks)):
            state = await restarted.upload_part(self.account, state, index, chunks[index])
        remote = await restarted.finish_upload(self.account, state)
        node = self.fake.nodes[int(remote.remote_id)]
        self.assertEqual((node.name, node.data), ("new.mkv", data))
        self.assertEqual(remote.sha1, fp.sha1)

    async def test_expired_oss_upload_starts_over(self) -> None:
        data = b"abc" * 1000
        fp = _fingerprint(data)
        step = await self.backend.begin_upload(self.account, str(self.movies), "gone.mkv", fp, None, None)
        self.fake.oss_uploads.clear()  # OSS 上的分片上传过期了
        with self.assertRaises(dv.Transient):
            await self.backend.upload_part(self.account, step.state, 0, data)
        again = await self.backend.begin_upload(self.account, str(self.movies), "gone.mkv", fp, step.state, None)
        self.assertEqual(again.next_part, 0)
        self.assertNotEqual(again.state["upload_id"], step.state["upload_id"])
        state = await self.backend.upload_part(self.account, again.state, 0, data)
        remote = await self.backend.finish_upload(self.account, state)
        self.assertEqual(self.fake.nodes[int(remote.remote_id)].data, data)

    async def test_delete_goes_to_recycle_bin_and_ignores_missing(self) -> None:
        await self.backend.delete(self.account, [str(self.dune), "424242", str(self.dune_srt)])
        self.assertEqual(sorted(self.fake.deleted), sorted([self.dune, self.dune_srt]))
        self.assertNotIn(self.dune, self.fake.nodes)


class PluginTest(unittest.TestCase):
    def test_declares_the_drive_contract(self) -> None:
        backend = Pan115()
        self.assertEqual(backend.type, "115")
        caps = backend.capabilities
        self.assertTrue(caps.writable and caps.range_challenge and caps.recycle_bin and caps.link_needs_ua)
        self.assertEqual(caps.rapid_upload_hashes, ("sha1",))
        spec = dv.backend_spec(backend)
        self.assertEqual(spec["login"]["options"][0]["default"], "alipaymini")


if __name__ == "__main__":
    unittest.main()
