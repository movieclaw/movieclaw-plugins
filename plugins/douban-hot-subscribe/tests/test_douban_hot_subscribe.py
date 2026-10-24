"""只测纯函数：不启动宿主，不发网络请求。

榜单条目的排序合并、排除词匹配、季号推断、已有订阅的比对键都在模块顶层的纯函数里，
改榜单口径时最容易坏的就是这几处，所以钉在这里。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _bootstrap  # noqa: F401,E402 -- 先把桩模块装好，下面才能导入
from douban_hot_subscribe import (  # noqa: E402
    is_permanent_error,
    keyword_hit,
    interleave,
    normalize_collections,
    pick_candidates,
    season_plan,
    seasons_missing,
    select_titles,
    title_key,
)
from movieclaw_sdk.ops import OpsError


class NormalizeCollectionsTest(unittest.TestCase):
    def test_alias_refs_pass_through(self):
        refs, bad = normalize_collections(["douban:movie:123", "tmdb:tv:9"])
        self.assertEqual(refs, ["douban:movie:123", "tmdb:tv:9"])
        self.assertEqual(bad, [])

    def test_split_on_comma_and_semicolon(self):
        refs, _ = normalize_collections("douban:movie:1，tmdb:movie:2；douban:tv:3")
        self.assertEqual(refs, ["douban:movie:1", "tmdb:movie:2", "douban:tv:3"])

    def test_dedupe_keeps_order(self):
        refs, _ = normalize_collections(["tmdb:movie:2", "douban:movie:1", "tmdb:movie:2"])
        self.assertEqual(refs, ["tmdb:movie:2", "douban:movie:1"])

    def test_unknown_entry_reported(self):
        refs, bad = normalize_collections(["乱七八糟"])
        self.assertEqual(refs, [])
        self.assertEqual(bad, ["乱七八糟"])

    def test_empty(self):
        self.assertEqual(normalize_collections(None), ([], []))
        self.assertEqual(normalize_collections([]), ([], []))


class KeywordHitTest(unittest.TestCase):
    def test_chinese_substring(self):
        self.assertEqual(keyword_hit("某综艺节目", ["综艺"]), "综艺")

    def test_english_word_boundary(self):
        self.assertEqual(keyword_hit("Some TS Show", ["TS"]), "TS")
        self.assertIsNone(keyword_hit("Hits 2024", ["ts"]))
        self.assertIsNone(keyword_hit("TCA 2024", ["TC"]))

    def test_no_keywords(self):
        self.assertIsNone(keyword_hit("随便什么", []))


class InterleaveTest(unittest.TestCase):
    def test_round_robin_between_collections(self):
        rows = [
            {"collection": "a", "n": 1},
            {"collection": "a", "n": 2},
            {"collection": "b", "n": 3},
        ]
        self.assertEqual([row["n"] for row in interleave(rows)], [1, 3, 2])

    def test_single_collection_keeps_order(self):
        rows = [{"collection": "a", "n": 1}, {"collection": "a", "n": 2}]
        self.assertEqual([row["n"] for row in interleave(rows)], [1, 2])


class SeasonPlanTest(unittest.TestCase):
    def test_movie(self):
        self.assertEqual(season_plan("某种物质", "movie", True), ([], False, "电影"))

    def test_tv_with_season_in_title(self):
        self.assertEqual(season_plan("相对宇宙 第二季", "tv", True), ([2], True, "按第 2 季订阅"))

    def test_tv_without_season_defaults_to_first(self):
        self.assertEqual(season_plan("某部剧", "tv", True), ([1], True, "标题没写第几季，按第 1 季订阅"))

    def test_tv_without_backfill_only_follows_new(self):
        self.assertEqual(season_plan("相对宇宙 第二季", "tv", False), ([], True, "只追订阅之后播出的新集"))


class TitleKeyTest(unittest.TestCase):
    def test_strips_whitespace_and_lowercases(self):
        self.assertEqual(title_key("The  Expanse", 2020), "theexpanse|2020")

    def test_year_must_be_int(self):
        self.assertEqual(title_key("某剧", "2020"), "某剧|")
        self.assertEqual(title_key("某剧", None), "某剧|")


class PickCandidatesTest(unittest.TestCase):
    def test_keeps_only_entries_with_title_ref(self):
        details = [
            {"title_ref": "tmdb:movie:1", "title": "甲", "year": 2020},
            {"title": "没有 ref"},
            "不是字典",
        ]
        self.assertEqual(
            pick_candidates(details), [{"title_ref": "tmdb:movie:1", "title": "甲", "year": 2020}]
        )

    def test_none(self):
        self.assertEqual(pick_candidates(None), [])


def _row(ref, title, rank, rating=8.0, collection="a", **extra):
    return {
        "title_ref": ref,
        "title": title,
        "rank": rank,
        "provider_rating": rating,
        "collection": collection,
        **extra,
    }


class SelectTitlesTest(unittest.TestCase):
    def test_plain_queue(self):
        rows = [_row("douban:movie:1", "甲", 1), _row("douban:movie:2", "乙", 2)]
        queue, skipped = select_titles(rows)
        self.assertEqual([row["title_ref"] for row in queue], ["douban:movie:1", "douban:movie:2"])
        self.assertEqual(skipped, [])

    def test_drops_entries_already_done(self):
        rows = [_row("douban:movie:1", "甲", 1), _row("douban:movie:2", "乙", 2)]
        queue, skipped = select_titles(rows, done={"douban:movie:1"})
        self.assertEqual([row["title_ref"] for row in queue], ["douban:movie:2"])
        # 已处理的不算「跳过」，不该在状态里刷一屏
        self.assertEqual(skipped, [])

    def test_drops_entries_without_ref_or_title(self):
        rows = [
            {"title_ref": "", "title": "没有引用", "rank": 1},
            {"title_ref": "douban:movie:9", "title": "  ", "rank": 2},
            _row("douban:movie:1", "甲", 3),
        ]
        queue, _ = select_titles(rows)
        self.assertEqual([row["title_ref"] for row in queue], ["douban:movie:1"])

    def test_dedupes_same_ref_across_collections(self):
        rows = [
            _row("douban:movie:1", "甲", 1, collection="a"),
            _row("douban:movie:1", "甲", 1, collection="b"),
        ]
        queue, _ = select_titles(rows)
        self.assertEqual(len(queue), 1)
        self.assertEqual(queue[0]["collection"], "a")

    def test_max_rank_with_reason(self):
        rows = [_row("douban:movie:1", "甲", 1), _row("douban:movie:2", "乙", 5)]
        queue, skipped = select_titles(rows, max_rank=3)
        self.assertEqual([row["title_ref"] for row in queue], ["douban:movie:1"])
        self.assertEqual(len(skipped), 1)
        self.assertIn("第 5", skipped[0]["reason"])

    def test_min_rating(self):
        rows = [_row("douban:movie:1", "甲", 1, rating=8.5), _row("douban:movie:2", "乙", 2, rating=5.0)]
        queue, skipped = select_titles(rows, min_rating=6.0)
        self.assertEqual([row["title_ref"] for row in queue], ["douban:movie:1"])
        self.assertIn("低于下限", skipped[0]["reason"])

    def test_unrated_kept_by_default_dropped_on_request(self):
        rows = [_row("douban:movie:1", "甲", 1, rating=0)]
        self.assertEqual(len(select_titles(rows)[0]), 1)
        queue, skipped = select_titles(rows, skip_unrated=True)
        self.assertEqual(queue, [])
        self.assertIn("评分", skipped[0]["reason"])

    def test_exclude_keywords(self):
        rows = [_row("douban:tv:1", "某综艺", 1), _row("douban:tv:2", "某剧", 2)]
        queue, skipped = select_titles(rows, exclude_keywords=["综艺"])
        self.assertEqual([row["title_ref"] for row in queue], ["douban:tv:2"])
        self.assertIn("综艺", skipped[0]["reason"])

    def test_owned_in_library_skipped(self):
        rows = [
            _row("douban:movie:1", "甲", 1, library_status={"file_count": 1}),
            _row("douban:movie:2", "乙", 2, library_status={"file_count": 0}),
        ]
        queue, skipped = select_titles(rows, skip_owned=True)
        self.assertEqual([row["title_ref"] for row in queue], ["douban:movie:2"])
        self.assertIn("媒体库", skipped[0]["reason"])
        # 关掉这项就都订
        self.assertEqual(len(select_titles(rows, skip_owned=False)[0]), 2)

    def test_rank_defaults_to_position(self):
        rows = [{"title_ref": "douban:movie:1", "title": "甲", "provider_rating": 8}]
        queue, _ = select_titles(rows)
        self.assertEqual(queue[0]["rank"], 1)


class IsPermanentErrorTest(unittest.TestCase):
    def exc(self, status):
        err = OpsError("op", "boom")
        err.status = status
        return err

    def test_client_errors_are_permanent(self):
        for status in (400, 404, 409, 422):
            self.assertTrue(is_permanent_error(self.exc(status)), status)

    def test_transient_errors_are_not_permanent(self):
        for status in (401, 403, 408, 425, 429, 500, 502, 503):
            self.assertFalse(is_permanent_error(self.exc(status)), status)

    def test_missing_status_is_not_permanent(self):
        self.assertFalse(is_permanent_error(OpsError("op", "boom")))
        self.assertFalse(is_permanent_error(self.exc(None)))


class SeasonsMissingTest(unittest.TestCase):
    def test_all_selected(self):
        self.assertEqual(seasons_missing([1, 2], [1, 2]), [])
        self.assertEqual(seasons_missing([1], [1, 2]), [])

    def test_some_missing(self):
        self.assertEqual(seasons_missing([1, 2, 3], [1, 3]), [2])

    def test_string_seasons_from_host(self):
        self.assertEqual(seasons_missing([1, 2], ["1"]), [2])

    def test_unusable_reply_is_not_reported(self):
        # 宿主没回这个字段时不该报「全丢了」，否则每轮都误报
        self.assertEqual(seasons_missing([1, 2], None), [])
        self.assertEqual(seasons_missing([1, 2], []), [1, 2])
        self.assertEqual(seasons_missing([], [1]), [])

    def test_garbage_entries_are_ignored(self):
        self.assertEqual(seasons_missing([1], [None, "x", 1]), [])


if __name__ == "__main__":
    unittest.main()
