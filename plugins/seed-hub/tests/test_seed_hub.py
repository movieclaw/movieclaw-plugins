"""只测纯函数：不启动宿主，不发网络请求，不碰真实下载器。

路径换算、判重口径、死种判定、标题匹配这几处一错就会误删种子、
或者该转的永远转不动，所以钉在这里。每处都配正例和反例。
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _bootstrap  # noqa: F401,E402  -- 注册 AST 桩模块（无运行时时）
import seed_hub as sh  # 真实模块或 tests/_bootstrap.py 注册的桩


class SiteNicknames(unittest.TestCase):
    def test_fill_and_overwrite(self):
        sh._SITE_NICKNAMES.clear()
        sh.remember_site_nicknames({"3": {"id": 3, "nickname": "馒头"}, "9": {"id": 9, "nickname": ""}})
        self.assertEqual(sh._SITE_NICKNAMES, {"3": "馒头"})  # 空昵称不入表
        sh.remember_site_nicknames({"3": {"id": 3, "nickname": "M-Team"}})
        self.assertEqual(sh._SITE_NICKNAMES["3"], "M-Team")  # 新值覆盖


class Paths(unittest.TestCase):
    def test_convert_save_path_longest_prefix(self):
        pairs = [("/data", "/mnt/qb"), ("/data/media", "/mnt/nas/media")]
        self.assertEqual(sh.convert_save_path("/data/media/movies/a", pairs), "/mnt/nas/media/movies/a")
        self.assertEqual(sh.convert_save_path("/data/other", pairs), "/mnt/qb/other")
        self.assertEqual(sh.convert_save_path("/opt/elsewhere", pairs), "/opt/elsewhere")  # 未命中原样
        self.assertEqual(sh.convert_save_path("/data", pairs), "/mnt/qb")  # 恰好等于前缀
        self.assertEqual(sh.convert_save_path("/datax/y", pairs), "/datax/y")  # 目录边界：/datax 不算 /data

    def test_convert_no_pairs(self):
        self.assertEqual(sh.convert_save_path("/a/b", []), "/a/b")

    def test_is_excluded_path(self):
        roots = ["/downloads/keep"]
        self.assertTrue(sh.is_excluded_path("/downloads/keep/x", roots))
        self.assertTrue(sh.is_excluded_path("/downloads/keep", roots))
        self.assertFalse(sh.is_excluded_path("/downloads/keepx/y", roots))  # 目录边界
        self.assertFalse(sh.is_excluded_path("/other/x", roots))

    def test_torrent_file_candidates(self):
        self.assertEqual(
            sh.torrent_file_candidates("ABC123", ["/qb/bt", "/tr/t"]),
            ["/qb/bt/abc123.torrent", "/tr/t/abc123.torrent"],  # hash 统一小写
        )


class Completion(unittest.TestCase):
    def test_is_completed(self):
        self.assertTrue(sh.is_completed({"state": "completed"}))
        self.assertTrue(sh.is_completed({"state": "stalledUP", "progress": 1.0}))  # 旧快照兜底
        self.assertFalse(sh.is_completed({"state": "stalledUP", "progress": 0.9}))
        self.assertFalse(sh.is_completed({"state": "error", "progress": 1.0}))  # error 不算
        self.assertFalse(sh.is_completed({"state": None}))

    def test_qb_seeding_paused(self):
        self.assertTrue(sh.qb_seeding_paused("pausedUP"))
        self.assertTrue(sh.qb_seeding_paused("stoppedUP"))  # qB 4.4+
        self.assertFalse(sh.qb_seeding_paused("checkingUP"))

    def test_tr_verify_done(self):
        self.assertTrue(sh.tr_verify_done({"status": 0, "percentDone": 1.0}))
        self.assertFalse(sh.tr_verify_done({"status": 4, "percentDone": 1.0}))  # 还在校验
        self.assertFalse(sh.tr_verify_done({"status": 0, "percentDone": 0.7}))
        self.assertFalse(sh.tr_verify_done({}))  # 脏数据不炸


class IyuuProtocol(unittest.TestCase):
    def test_compact_hash_json_sorted_lower(self):
        self.assertEqual(sh.compact_hash_json(["BB", "aa"]), '["aa","bb"]')

    def test_parse_sites(self):
        data = {"sites": [{"id": 3, "nickname": "馒头"}, {"nickname": "无 id 的忽略"}, "垃圾"]}
        self.assertEqual(set(sh.parse_sites(data)), {"3"})

    def test_parse_matches(self):
        data = {
            "HASH1": {"torrent": [{"sid": 3, "torrent_id": 100, "info_hash": "hash1"}]},
            "HASH2": {"torrent": {"sid": 9, "torrent_id": 5}},  # 单对象也能吃
            "HASH3": {"torrent": [{"sid": "", "torrent_id": 5}]},  # 缺 sid 丢弃
            "HASH4": {"other": 1},  # 没有 torrent 丢弃
            "垃圾": "不是字典",
        }
        out = sh.parse_matches(data)
        self.assertEqual(set(out), {"hash1", "hash2"})
        self.assertEqual(out["hash2"], [{"sid": 9, "torrent_id": 5}])

    def test_extract_credentials(self):
        self.assertEqual(sh.extract_credentials("https://x/dl?a=1&passkey=abc123def"), {"passkey": "abc123def"})
        self.assertEqual(sh.extract_credentials("https://x/download.php?id=5"), {})
        self.assertEqual(sh.extract_credentials(""), {})

    def test_apply_credentials_fill_and_strip(self):
        url = "https://x/dl/{id}?passkey={passkey}&uid={uid}"
        self.assertEqual(sh.apply_credentials(url, {"passkey": "PK"}), "https://x/dl/{id}?passkey=PK")
        self.assertEqual(sh.apply_credentials(url, {}), "https://x/dl/{id}")  # 没凭据剔占位符
        self.assertEqual(sh.apply_credentials("", {"passkey": "PK"}), "")

    def test_fill_download_template(self):
        self.assertEqual(sh.fill_download_template("download.php?id={}&hit=1", 42), "download.php?id=42&hit=1")
        self.assertEqual(sh.fill_download_template("dl/{}?passkey={passkey}", 7), "dl/7")

    def test_build_download_url(self):
        self.assertEqual(sh.build_download_url("https://x.org", 5), "https://x.org/download.php?id=5")
        self.assertEqual(sh.build_download_url("x.org", 5, is_https="0"), "http://x.org/download.php?id=5")
        self.assertEqual(sh.build_download_url("https://x.org", "raw-id", api_style=True), "raw-id")
        self.assertEqual(
            sh.build_download_url("https://x.org", 5, template="dl/{}.torrent"),
            "https://x.org/dl/5.torrent",
        )

    def test_domain_of(self):
        self.assertEqual(sh.domain_of("https://Tracker.X.org:443/a"), "tracker.x.org")
        self.assertEqual(sh.domain_of("tracker.x.org"), "tracker.x.org")


class TitleMatch(unittest.TestCase):
    def test_same_release(self):
        self.assertTrue(sh._title_match("A.Better.Place.2020.1080p.WEB-DL.x265.Group", "A Better Place 2020 GROUP"))
        self.assertTrue(sh._title_match("凡人修仙传年番4", "凡人修仙传年番4 H.265 1080p"))

    def test_different_release(self):
        self.assertFalse(sh._title_match("Dark.the.Light.2020", "Light.the.Dark.2020"))  # 顺序不同不认
        self.assertFalse(sh._title_match("凡人修仙传", "凡人修仙传年番"))  # 中文名长度差>2 不同源

    def test_empty(self):
        self.assertFalse(sh._title_match("", "x"))
        self.assertFalse(sh._title_match("。、", "。、"))


class DeadTorrentVerdict(unittest.TestCase):
    def test_tracker_dead_reason(self):
        self.assertEqual(sh.tracker_dead_reason("Torrent NOT REGISTERED with this tracker"), "not registered with this tracker")
        self.assertIsNone(sh.tracker_dead_reason("connection timed out"))
        self.assertIsNone(sh.tracker_dead_reason(""))

    def test_tr_verdict(self):
        dead_tracker = [{"isBackup": False, "lastAnnounceResult": "Torrent NOT registered with this tracker"}]
        reason, note = sh.tr_torrent_verdict(
            error=sh._TR_ERROR_LOCAL, error_string="No data found!",
            tracker_stats=dead_tracker, labels=[], protected_labels=set(),
        )
        self.assertEqual(reason, "tracker_deleted")  # tracker 判据优先于 no_data
        reason, _ = sh.tr_torrent_verdict(
            error=sh._TR_ERROR_LOCAL, error_string="No data found!",
            tracker_stats=[], labels=[], protected_labels=set(),
        )
        self.assertEqual(reason, "no_data")
        reason, _ = sh.tr_torrent_verdict(
            error=sh._TR_ERROR_LOCAL, error_string="No data found!",
            tracker_stats=[], labels=["留"], protected_labels={"留"},
        )
        self.assertIsNone(reason)  # 保护标签放过
        # 校验完好的 No data found 不判死（多半是路径变了）——本函数不校验进度，
        # 只看 error+error_string，这是调用方 _classify_tr 的职责，这里只钉口径
        reason, _ = sh.tr_torrent_verdict(
            error=sh._TR_ERROR_TRACKER, error_string="tracker down",
            tracker_stats=[], labels=[], protected_labels=set(),
        )
        self.assertIsNone(reason)  # tracker 警告（error=2）不算
        # isBackup 的 tracker 行不参与判断
        reason, _ = sh.tr_torrent_verdict(
            error=0, error_string="",
            tracker_stats=[{"isBackup": True, "lastAnnounceResult": "torrent banned"}],
            labels=[], protected_labels=set(),
        )
        self.assertIsNone(reason)

    def test_qb_verdict(self):
        dead = [
            {"url": "** [DHT] x", "tier": -1, "status": 4, "msg": ""},  # 伪条目，不参与
            {"url": "https://t1/announce", "tier": 0, "status": 4, "msg": "torrent not registered"},
            {"url": "https://t2/announce", "tier": 1, "status": 4, "msg": "torrent banned"},
        ]
        alive = [
            {"url": "https://t1/announce", "tier": 0, "status": 4, "msg": "torrent not registered"},
            {"url": "https://t2/announce", "tier": 1, "status": 2, "msg": ""},  # 有一个活着
        ]
        private_hint = [
            {"url": "https://t1/announce", "tier": 0, "status": 4, "msg": "私有 torrent"},
        ]
        reason, _ = sh.qb_torrent_verdict(trackers=dead, state="", protected_labels=set(), labels=[])
        self.assertEqual(reason, "tracker_deleted")
        reason, _ = sh.qb_torrent_verdict(trackers=alive, state="", protected_labels=set(), labels=[])
        self.assertIsNone(reason)
        reason, _ = sh.qb_torrent_verdict(trackers=private_hint, state="", protected_labels=set(), labels=[])
        self.assertIsNone(reason)
        reason, _ = sh.qb_torrent_verdict(trackers=dead, state="", protected_labels={"留"}, labels=["留"])
        self.assertIsNone(reason)

    def test_sitename_from_labels(self):
        sh._SITE_NICKNAMES.clear()
        self.assertEqual(sh.sitename_from_labels(["站点/天空"]), "天空")
        sh.remember_site_nicknames({"3": {"nickname": "馒头"}})
        self.assertEqual(sh.sitename_from_labels(["馒头"]), "馒头")  # 命中昵称表，返回昵称本身
        self.assertIsNone(sh.sitename_from_labels(["随便"]))


class SiteTag(unittest.TestCase):
    def test_known_domain(self):
        self.assertEqual(sh.site_tag_from_trackers(["https://tracker.m-team.cc/x"]), "站点/馒头")
        self.assertEqual(sh.site_tag_from_trackers(["https://t.hdsky.me/x"]), "站点/天空")

    def test_unknown_domain_uses_body(self):
        self.assertEqual(sh.site_tag_from_trackers(["https://tracker.somept.org/x"]), "站点/somept")
        self.assertEqual(sh.site_tag_from_trackers(["https://pt.mytracker.cn/x"]), "站点/mytracker")

    def test_empty(self):
        self.assertIsNone(sh.site_tag_from_trackers([]))


class Bencode(unittest.TestCase):
    @staticmethod
    def _sample() -> bytes:
        from bencode import bencode
        return bencode({b"info": {b"name": b"hello", b"length": 5}})

    def test_info_hash_of(self):
        import hashlib
        from bencode import bencode, bdecode
        raw = self._sample()
        data = bdecode(raw)
        self.assertEqual(sh.info_hash_of(raw), hashlib.sha1(bencode(data[b"info"])).hexdigest())
        self.assertIsNone(sh.info_hash_of(b"junk"))
        self.assertIsNone(sh.info_hash_of(b"de"))  # 没有 info 段

    def test_patch_announce(self):
        raw = self._sample()
        self.assertTrue(sh.needs_trackers(raw))  # 缺 announce
        patched = sh.patch_announce(raw, [["https://a/announce", "https://b/announce"]])
        self.assertFalse(sh.needs_trackers(patched))
        self.assertEqual(sh.patch_announce(patched, [["https://a/announce"]]), patched)  # 已有不再补
        with self.assertRaises(Exception):
            sh.patch_announce(b"d3:foo", [["x"]])  # 顶层不是字典

    def test_announce_urls(self):
        from bencode import bencode
        raw = bencode({
            b"announce": b"https://a/announce",
            b"announce-list": [[b"https://a/announce"], [b"https://b/announce"]],
            b"info": {},
        })
        self.assertEqual(sh._announce_urls(raw), ["https://a/announce", "https://a/announce", "https://b/announce"])
        self.assertEqual(sh._announce_urls(b"junk"), [])
        self.assertEqual(sh._announce_urls(b"de"), [])


class Misc(unittest.TestCase):
    def test_split_helpers(self):
        self.assertEqual(sh.split_lines("a\n b \n\n"), ["a", "b"])
        self.assertEqual(sh.split_tags("a, b,,c"), ["a", "b", "c"])
        self.assertEqual(sh.qb_tags({"tags": "x, y"}), ["x", "y"])

    def test_slug_aliases(self):
        self.assertEqual(sh.slug_of({"site": "m-team"}), "mteam")
        self.assertEqual(sh.slug_of({"site": "hdsky"}), "hdsky")


if __name__ == "__main__":
    unittest.main()
