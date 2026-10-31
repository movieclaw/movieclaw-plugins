"""只读识别回归：真实本体名称模型 + 模拟宿主接口；不登录、不转存、不删文件。"""

import asyncio
import logging
import os
import unittest
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import p115recognition as r
import p115_import as plugin
from movieclaw_sdk.ops import OpsError


def body(**kw):
    return SimpleNamespace(kind=kw.get("kind"), title=kw.get("title"), year=kw.get("year"),
                           tmdb_id=kw.get("tmdb_id"))


def hit(id=290193, title="鼠惑", year=2026, kind="tv"):
    return {"status": "ready", "tmdb_id": id, "title": title, "year": year,
            "kind": kind, "library_id": 12, "library_name": "日韩剧"}


def ambiguous(*rows):
    return {"status": "ambiguous", "candidates": list(rows)}


class FakeOps:
    def __init__(self, answers=None, details=None):
        self.answers = answers or {}
        self.details = details or {}
        self.calls = []

    async def call(self, name, payload):
        self.calls.append((name, dict(payload)))
        if name == "discover.get-title-details":
            return {"title": self.details.get(payload["title_ref"], {})}
        if name != "dl.resolve-target":
            raise AssertionError("识别不应该调用写操作：" + name)
        key = (payload["title"], payload.get("year"), payload["kind"])
        result = deepcopy(self.answers.get(key, {"status": "not_found", "candidates": []}))
        selected = payload.get("selected_tmdb_id")
        if selected and result.get("tmdb_id") != selected:
            for candidate in result.get("candidates", []):
                if candidate.get("tmdb_id") == selected:
                    return {**candidate, "status": "ready", "library_id": 12}
            raise OpsError(400, "BAD_REQUEST", "确认的编号不在候选里")
        return result


class PureTests(unittest.TestCase):
    def test_prefix_and_pin_are_removed_before_parsing(self):
        clues = r.source_clues("S- 鼠惑 (2026) {tmdbid-290193}", "tv", "目录")
        self.assertEqual((clues[0].title, clues[0].year), ("鼠惑", 2026))

    def test_year_in_title_preserved(self):
        clues = r.source_clues("Blade.Runner.2049.2017.1080p.mkv", "movie", "文件")
        self.assertEqual((clues[0].title, clues[0].year), ("Blade Runner 2049", 2017))

    def test_number_only_movie_with_explicit_release_year(self):
        clues = r.source_clues("1917.2019.1080p.mkv", "movie", "文件")
        self.assertEqual((clues[0].title, clues[0].year), ("1917", 2019))

    def test_leading_year_in_title_not_used_as_release_year(self):
        clues = r.source_clues("2001.A.Space.Odyssey.1968.1080p.mkv", "movie", "文件")
        self.assertEqual((clues[0].title, clues[0].year), ("2001 A Space Odyssey", 1968))

    def test_category_bracket_is_not_part_of_title(self):
        clues = r.source_clues("【韩剧】鼠惑 (2026)", "tv", "目录")
        self.assertEqual((clues[0].title, clues[0].year), ("鼠惑", 2026))

    def test_tv_total_episode_label_without_episode_filename(self):
        evidence = r.collect_evidence("鼠惑 2026 全10集", ["鼠惑 2026 全10集/01.mkv"], body())
        self.assertEqual(evidence["kind"], "tv")

    def test_conventional_name_overrides_model_truncation(self):
        clues = r.source_clues("知否知否应是绿肥红瘦 (2018)", "tv", "目录")
        self.assertEqual(clues[0].title, "知否知否应是绿肥红瘦")

    def test_punctuation_title_preserved(self):
        clues = r.source_clues("E.T.The.Extra-Terrestrial.1982.1080p.mkv", "movie", "文件")
        self.assertEqual(clues[0].title, "E T The Extra-Terrestrial")
        self.assertEqual(clues[0].year, 1982)

    def test_noise_advertising(self):
        clues = r.source_clues("【公众号xxx】鼠惑.2026.S01E01.2160p.mkv", "tv", "文件")
        self.assertEqual(clues[0].title, "鼠惑")
        self.assertEqual(clues[0].year, 2026)

    def test_numeric_or_hash_or_category_not_title(self):
        for value in ("01.mkv", "Season 1", "S01E02.mkv", "影剧资源", "电影", "待整理", "abcdef1234567890abcdef.mkv", ""):
            with self.subTest(value=value):
                self.assertEqual(r.source_clues(value, "tv", "目录"), [])

    def test_title_containing_short_technical_letters(self):
        self.assertEqual(r.source_clues("Hits (2023)", "movie", "file")[0].title, "Hits")
        self.assertEqual(r.source_clues("Cameron (2024)", "movie", "file")[0].title, "Cameron")
        self.assertEqual(r.source_clues("The Web (1947)", "movie", "file")[0].title, "The Web")

    def test_filename_tv_beats_title_movie_guess(self):
        e = r.collect_evidence("鼠惑 (2026)", ["鼠惑 (2026)/Season 1/01.mkv"], body())
        self.assertEqual(e["kind"], "tv")
        self.assertTrue(e["groups"][0][1])

    def test_normalized_chinese_parentheses(self):
        self.assertEqual(r.source_clues("鼠惑（2026）", "tv", "目录")[0].title, "鼠惑")

    def test_user_kind_validated(self):
        with self.assertRaises(ValueError):
            r.collect_evidence("a", ["a.mkv"], body(kind="documentary"))

    def test_pin_forms_and_deduplication(self):
        e = r.collect_evidence("鼠惑 [tmdb=290193]", ["鼠惑 {tmdbid-290193}/Season 1/01.mkv"], body())
        self.assertEqual(e["pins"], [290193])

    def test_two_directories_kept(self):
        e = r.collect_evidence("合集", ["A (2020)/A.2020.mkv", "B (2021)/B.2021.mkv"], body())
        self.assertEqual(len(e["groups"]), 2)

    def test_candidate_dedup_keeps_movie_tv_collision(self):
        self.assertEqual(len(r.merge_candidates([{"kind":"tv","tmdb_id":5}, {"kind":"tv","tmdb_id":5}, {"kind":"movie","tmdb_id":5}])), 2)

    def test_parse_cache_copies_not_changed_by_caller(self):
        e1 = r.collect_evidence("鼠惑 (2026)", ["Mousetrap.2026.S01E01.mkv"], body())
        e2 = r.collect_evidence("鼠惑 (2026)", ["Mousetrap.2026.S01E02.mkv"], body())
        self.assertEqual(e1["share"][0].title, e2["share"][0].title)


class ResolveTests(unittest.IsolatedAsyncioTestCase):
    async def resolve(self, title, paths, b=None, ops=None):
        b = b or body()
        e = r.collect_evidence(title, paths, b)
        return await r.recognize(ops or FakeOps(), e, b)

    async def test_no_tmdb_id_filename_fallback(self):
        ops = FakeOps({("Mousetrap",2026,"tv"): hit()})
        result = await self.resolve("影剧资源", ["Mousetrap.2026.S01E01.2160p.mkv"], ops=ops)
        self.assertEqual(result["tmdb_id"], 290193)
        self.assertEqual(result["status"], "ready")

    async def test_original_failed_case_automatic(self):
        ops = FakeOps({("鼠惑",2026,"tv"): hit(), ("Mousetrap",2026,"tv"):hit()},
                      {"tmdb:tv:290193": {"title":"鼠惑","release_year":2026}})
        result = await self.resolve("S- 鼠惑 (2026) {tmdbid-290193}",
                                    ["S- 鼠惑 (2026) {tmdbid-290193}/Season 1/Mousetrap.2026.S01E01.mkv"], ops=ops)
        self.assertEqual(result["status"], "ready")
        self.assertTrue(any(row["origin"] == "编号资料核验" for row in result["recognition"]))

    async def test_wrong_share_title_cannot_override_filename(self):
        ops = FakeOps({("其他片",2026,"tv"):hit(111,"其他片"), ("Mousetrap",2026,"tv"):hit()})
        result = await self.resolve("其他片 (2026)",["Mousetrap.2026.S01E01.mkv"],ops=ops)
        self.assertEqual(result["status"],"ambiguous")
        self.assertIn("不同作品",result["message"])

    async def test_directory_resolves_numeric_episodes(self):
        ops = FakeOps({("鼠惑",2026,"tv"):hit()})
        result = await self.resolve("影剧资源",["鼠惑 (2026)/Season 1/01.mkv","鼠惑 (2026)/Season 1/02.mkv"],ops=ops)
        self.assertEqual(result["status"],"ready")

    async def test_completely_unknown_names_no_fake_candidate_message(self):
        result = await self.resolve("影剧资源",["Season 1/01.mkv"])
        self.assertEqual(result["status"],"not_found")
        self.assertEqual(result["candidates"],[])
        self.assertIn("不需要 TMDB 编号",result["message"])
        self.assertNotIn("候选",result["message"])

    async def test_ambiguous_same_title_does_not_choose_first(self):
        rows=[{"kind":"movie","tmdb_id":1,"title":"重名","year":2020},
              {"kind":"movie","tmdb_id":2,"title":"重名","year":2020}]
        result=await self.resolve("分享",["重名.2020.1080p.mkv"],ops=FakeOps({("重名",2020,"movie"):ambiguous(*rows)}))
        self.assertEqual(result["status"],"ambiguous")
        self.assertEqual(len(result["candidates"]),2)

    async def test_no_year_unique_candidate_reverified_by_host(self):
        rows=[{"kind":"tv","tmdb_id":290193,"title":"鼠惑","year":2026}]
        ops=FakeOps({("鼠惑",None,"tv"):ambiguous(*rows),("鼠惑",2026,"tv"):hit()})
        result=await self.resolve("影剧资源",["鼠惑/Season 1/01.mkv"],ops=ops)
        self.assertEqual(result["status"],"ready")
        self.assertTrue(any("无年份候选复核" in x["origin"] for x in result["recognition"]))

    async def test_no_year_remake_stays_ambiguous(self):
        rows=[{"kind":"tv","tmdb_id":10,"title":"同名","year":2000}, {"kind":"tv","tmdb_id":11,"title":"同名","year":2020}]
        ops=FakeOps({("同名",None,"tv"):ambiguous(*rows),("同名",2000,"tv"):hit(10,"同名",2000),("同名",2020,"tv"):hit(11,"同名",2020)})
        result=await self.resolve("分享",["同名/Season 1/01.mkv"],ops=ops)
        self.assertEqual(result["status"],"ambiguous")

    async def test_no_year_one_unique_and_another_ambiguous_stays_ambiguous(self):
        rows=[{"kind":"tv","tmdb_id":10,"title":"同名","year":2000},
              {"kind":"tv","tmdb_id":11,"title":"同名","year":2020},
              {"kind":"tv","tmdb_id":12,"title":"同名","year":2020}]
        ops=FakeOps({("同名",None,"tv"):ambiguous(*rows),
                     ("同名",2000,"tv"):hit(10,"同名",2000),
                     ("同名",2020,"tv"):ambiguous(*rows[1:])})
        result=await self.resolve("分享",["同名/Season 1/01.mkv"],ops=ops)
        self.assertEqual(result["status"],"ambiguous")

    async def test_multi_movie_share_stopped(self):
        ops=FakeOps({("The Matrix",1999,"movie"):hit(603,"黑客帝国",1999,"movie"),
                     ("Inception",2010,"movie"):hit(27205,"盗梦空间",2010,"movie")})
        result=await self.resolve("合集",["The.Matrix.1999.1080p.mkv","Inception.2010.1080p.mkv"],ops=ops)
        self.assertEqual(result["status"],"ambiguous")
        self.assertEqual(len(result["candidates"]),2)

    async def test_unrecognized_second_movie_cannot_be_forced_to_first(self):
        ops=FakeOps({("The Matrix",1999,"movie"):hit(603,"黑客帝国",1999,"movie")})
        result=await self.resolve("合集",["The.Matrix.1999.1080p.mkv","Unrecognized.2020.1080p.mkv"],ops=ops)
        self.assertNotEqual(result["status"],"ready")

    async def test_same_series_many_episodes_cached(self):
        ops=FakeOps({("Mousetrap",2026,"tv"):hit()})
        result=await self.resolve("分享",[f"Mousetrap.2026.S01E{i:02}.2160p.mkv" for i in range(1,101)],ops=ops)
        self.assertEqual(result["status"],"ready")
        self.assertLessEqual(len(ops.calls),2)

    async def test_budget_exhaustion_fails_closed(self):
        names=[f"UnknownTitle{i}.2020.1080p.mkv" for i in range(30)]
        result=await self.resolve("合集",names)
        self.assertEqual(result["status"],"ambiguous")
        self.assertIn("线索过多",result["message"])

    async def test_conflicting_tmdb_markers_fail_closed(self):
        result=await self.resolve("分享",["A {tmdbid-10}/A.2020.mkv","B {tmdbid-11}/B.2020.mkv"])
        self.assertEqual(result["status"],"ambiguous")
        self.assertIn("标记互相冲突",result["message"])

    async def test_original_source_sent_as_subtitle(self):
        ops=FakeOps({("Mousetrap",2026,"tv"):hit()})
        original="Mousetrap.2026.S01E01.2160p.NF.WEB-DL.mkv"
        await self.resolve("影剧资源",[original],ops=ops)
        self.assertEqual(ops.calls[0][1]["subtitle"],original)

    async def test_host_route_failure_does_not_start_transfer(self):
        blocked = dict(hit(), ok=False, warning="没有监听规则")
        with self.assertRaises(RuntimeError):
            await self.resolve("分享",["Mousetrap.2026.S01E01.mkv"],
                               ops=FakeOps({("Mousetrap",2026,"tv"):blocked}))

    async def test_explicit_id_for_unparsed_source_still_uses_host(self):
        ops=FakeOps({("鼠惑",2026,"tv"):hit()}, {"tmdb:tv:290193":{"title":"鼠惑","release_year":2026}})
        result=await self.resolve("未知分享描述",["完全未知资源名/Season 1/01.mkv"],
                                 body(tmdb_id=290193,kind="tv"),ops=ops)
        self.assertEqual(result["status"],"ready")
        self.assertTrue(all(name in ("discover.get-title-details","dl.resolve-target") for name,_ in ops.calls))

    async def test_wrong_pin_with_conflicting_filename_stopped(self):
        ops=FakeOps({("鼠惑",2026,"tv"):hit(), ("Other Show",2026,"tv"):hit(91,"Other Show")},
                    {"tmdb:tv:290193":{"title":"鼠惑","release_year":2026}})
        result=await self.resolve("鼠惑 {tmdbid-290193}",["Other.Show.2026.S01E01.mkv"],ops=ops)
        self.assertEqual(result["status"],"ambiguous")

    async def test_host_network_failure_not_misreported_as_not_found(self):
        ops=FakeOps()
        ops.call=AsyncMock(side_effect=OpsError(502,"UPSTREAM_ERROR","TMDB 暂时不可用"))
        with self.assertRaises(OpsError):
            await self.resolve("鼠惑 (2026)",["Mousetrap.2026.S01E01.mkv"],ops=ops)

    async def test_id_never_bypasses_host_routing_validation(self):
        ops=FakeOps(details={"tmdb:tv:290193":{"title":"鼠惑","release_year":2026}})
        result=await self.resolve("分享",["Season 1/01.mkv"],body(tmdb_id=290193,kind="tv"),ops=ops)
        self.assertNotEqual(result["status"],"ready")

    async def test_id_verified_before_using_canonical_title(self):
        ops=FakeOps({("鼠惑",2026,"tv"):hit()}, {"tmdb:tv:290193":{"title":"鼠惑","release_year":2026}})
        result=await self.resolve("无意义分享名",["Season 1/01.mkv"],body(tmdb_id=290193,kind="tv"),ops=ops)
        self.assertEqual(result["status"],"ready")
        self.assertEqual(ops.calls[0][0],"discover.get-title-details")
        self.assertEqual(ops.calls[1][0],"dl.resolve-target")


class BuildPlanTests(unittest.IsolatedAsyncioTestCase):
    def setup_context(self):
        store = SimpleNamespace()
        ops = FakeOps({("鼠惑",2026,"tv"):hit(), ("Mousetrap",2026,"tv"):hit()},
                      {"tmdb:tv:290193":{"title":"鼠惑","release_year":2026}})
        ctx = SimpleNamespace(config=plugin.Settings(), logger=logging.getLogger("test"))
        def use(key):
            if key is plugin.PLUGIN_DATA:
                return SimpleNamespace(scoped=lambda c:store)
            if key is plugin.HOST_OPS:
                return SimpleNamespace(client=AsyncMock(return_value=ops))
            raise AssertionError(str(key))
        ctx.use = use
        return ctx, ops

    def pan(self, title="影剧资源", videos=None):
        if videos is None:
            videos = [plugin.api.ShareFile(share_id="123", name="Mousetrap.2026.S01E01.2160p.mkv", is_dir=False,
                      size=1000,rel_path="Mousetrap.2026.S01E01.2160p.mkv")]
        return SimpleNamespace(share_meta=AsyncMock(return_value={"title":title,"user":"test"}),
                               share_tree=AsyncMock(return_value=videos))

    def target(self, existing=None):
        return {**hit(), "media_item_id":None,"templates":plugin.naming.Templates(),
                "cloud_account_id":1,"cloud_root_id":"456","cloud_root_path":"日韩剧",
                "entry_dir_name":"鼠惑 (2026)","existing":existing or []}

    async def test_share_plan_uses_core_recognition_without_mutations(self):
        ctx, ops = self.setup_context()
        parser = SimpleNamespace(ensure=AsyncMock())
        pan = self.pan()
        with patch.object(plugin,"_core_parser",parser), patch.object(plugin.api,"Pan",return_value=pan), \
             patch.object(plugin,"_target",new=AsyncMock(return_value=self.target())) as target:
            result = await plugin.build_plan(ctx,plugin.PlanBody(link="https://115.com/s/abcdef?password=test"))
        self.assertEqual(result["status"],"ready")
        self.assertEqual(result["tmdb_id"],290193)
        self.assertEqual(result["total_new_bytes"],1000)
        self.assertEqual(len(result["recognition"]),1)
        self.assertEqual(target.call_args.kwargs["resolved"]["tmdb_id"],290193)
        self.assertEqual([x[0] for x in ops.calls],["dl.resolve-target"])

    async def test_empty_candidates_returns_not_found_and_zero_transfer(self):
        ctx, ops = self.setup_context()
        pan=self.pan("影剧资源",[plugin.api.ShareFile(share_id="123",name="01.mkv",is_dir=False,size=1000,rel_path="Season 1/01.mkv")])
        with patch.object(plugin,"_core_parser",SimpleNamespace(ensure=AsyncMock())), \
             patch.object(plugin.api,"Pan",return_value=pan):
            result=await plugin.build_plan(ctx,plugin.PlanBody(link="https://115.com/s/abcdef?password=test"))
        self.assertEqual(result["status"],"not_found")
        self.assertEqual(result["receive_ids"],[])
        self.assertEqual(result["candidates"],[])
        self.assertNotIn("候选",result["message"])
        self.assertEqual(ops.calls,[])

    async def test_existing_larger_file_is_still_skipped(self):
        ctx, ops = self.setup_context()
        existing=[{"file_id":9,"name":"old.mkv","size":2000,"season":1,"episode":1,
                   "remote_path":"日韩剧/鼠惑 (2026)/Season 01/old.mkv","missing":False}]
        with patch.object(plugin,"_core_parser",SimpleNamespace(ensure=AsyncMock())), \
             patch.object(plugin.api,"Pan",return_value=self.pan()), \
             patch.object(plugin,"_target",new=AsyncMock(return_value=self.target(existing))):
            result=await plugin.build_plan(ctx,plugin.PlanBody(link="https://115.com/s/abcdef?password=test"))
        self.assertEqual(result["status"],"nothing_to_do")
        self.assertEqual(result["receive_ids"],[])
        self.assertEqual(result["old_files"],[])


    async def test_ed2k_uses_same_core_recognition_and_does_not_submit_download(self):
        ctx, ops = self.setup_context()
        pan=SimpleNamespace(open_login=AsyncMock(return_value={"authorized":True}))
        offline=[plugin.api.OfflineItem(url="ed2k://|file|Mousetrap.2026.S01E01.2160p.mkv|1000|0123456789abcdef0123456789abcdef|/",
                  name="Mousetrap.2026.S01E01.2160p.mkv",size=1000,info_hash="a"*32)]
        with patch.object(plugin,"_core_parser",SimpleNamespace(ensure=AsyncMock())), \
             patch.object(plugin,"_target",new=AsyncMock(return_value=self.target())):
            result=await plugin._offline_plan(ctx,pan,ops,offline,plugin.PlanBody(link=offline[0].url))
        self.assertEqual(result["status"],"ready")
        self.assertEqual(result["tmdb_id"],290193)
        self.assertEqual(result["total_new_bytes"],1000)
        self.assertEqual(result["items"][0]["source"],"ed2k")
        self.assertEqual([name for name,_ in ops.calls],["dl.resolve-target"])

    async def test_bare_magnet_needs_user_identity_not_hallucination(self):
        ctx, ops = self.setup_context()
        pan=SimpleNamespace(open_login=AsyncMock(return_value={"authorized":True}))
        offline=[plugin.api.OfflineItem(url="magnet:?xt=urn:btih:"+"a"*40,info_hash="a"*40,kind="magnet")]
        with patch.object(plugin,"_core_parser",SimpleNamespace(ensure=AsyncMock())):
            result=await plugin._offline_plan(ctx,pan,ops,offline,plugin.PlanBody(link=offline[0].url))
        self.assertEqual(result["status"],"not_found")
        self.assertEqual(result["receive_ids"],[])
        self.assertEqual(ops.calls,[])

    async def test_recognize_path_cannot_be_absolute_read_target(self):
        ctx, ops = self.setup_context()
        with patch.object(plugin,"_core_parser",SimpleNamespace(ensure=AsyncMock())):
            evidence,result=await plugin._recognize_input(ctx,ops,"",["/etc/passwd"],body())
        self.assertNotEqual(result["status"],"ready")
        self.assertTrue(all(name=="dl.resolve-target" for name,_ in ops.calls))


class ParserPermissionsTests(unittest.IsolatedAsyncioTestCase):
    @unittest.skipUnless(os.path.isdir(r.MODEL_DIRECTORY), "没有宿主 NER 模型目录（运行环境未带模型），跳过真实模型加载")
    async def test_model_is_read_only_via_granted_file_interface(self):
        class Files:
            def __init__(self): self.calls=[]
            async def open(self,path,mode):
                self.calls.append((path,mode))
                return open(path,mode)
        files=Files()
        parser=r.CoreParser(files,logging.getLogger("test"))
        before=os.environ.get("MOVIECLAW_NER_DIR")
        try:
            await parser.ensure()
            await parser.ensure()
            self.assertEqual(len(files.calls),3)
            self.assertTrue(all(mode=="rb" for _,mode in files.calls))
            self.assertTrue(parser.ready)
        finally:
            parser.close()
            if before: os.environ["MOVIECLAW_NER_DIR"]=before

    async def test_ungranted_model_path_fails_closed(self):
        files=SimpleNamespace(open=AsyncMock(side_effect=PermissionError("未授权")))
        parser=r.CoreParser(files,logging.getLogger("test"))
        with self.assertRaises(PermissionError):
            await parser.ensure()
        self.assertFalse(parser.ready)
        self.assertIsNone(parser.temp)


if __name__ == "__main__":
    unittest.main(verbosity=2)
