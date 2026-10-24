"""只测纯函数：不启动宿主，不发网络请求。

``apply`` 里那些闭包要靠宿主上下文才能跑，这里不碰。选季、时间窗口、豆瓣返回的解析
全在模块顶层的纯函数里，界面改版时最容易坏的就是这几处，所以钉在这里。
"""

import os
import sys
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _bootstrap  # noqa: F401,E402 -- 先把桩模块装好，下面才能导入
from douban_wish import (  # noqa: E402
    CST,
    baseline_string,
    cutoff_string,
    cjk_number,
    in_window,
    is_permanent_error,
    normalize_user,
    normalize_users,
    parse_wishes,
    pick_candidates,
    plan_for,
    season_marker,
    seasons_missing,
)
from movieclaw_sdk.ops import OpsError


class NormalizeUserTest(unittest.TestCase):
    def test_bare_id(self):
        self.assertEqual(normalize_user("254458098"), "254458098")

    def test_profile_url(self):
        self.assertEqual(
            normalize_user("https://www.douban.com/people/254458098/"), "254458098"
        )

    def test_mobile_url_with_query(self):
        self.assertEqual(
            normalize_user("https://m.douban.com/people/abc_1.2/?tab=wish"), "abc_1.2"
        )

    def test_url_without_scheme(self):
        self.assertEqual(normalize_user("www.douban.com/people/x9/"), "x9")

    def test_subject_url_is_not_a_user(self):
        self.assertEqual(normalize_user("https://movie.douban.com/subject/123/"), "")

    def test_blank_and_garbage(self):
        self.assertEqual(normalize_user(""), "")
        self.assertEqual(normalize_user("   "), "")
        self.assertEqual(normalize_user("not a valid id!"), "")


class NormalizeUsersTest(unittest.TestCase):
    def test_split_on_comma_semicolon_and_newline(self):
        users, bad = normalize_users(["a1,b2；c3、d4\ne5"])
        self.assertEqual(users, ["a1", "b2", "c3", "d4", "e5"])
        self.assertEqual(bad, [])

    def test_plain_string_input(self):
        users, bad = normalize_users("a1, b2")
        self.assertEqual(users, ["a1", "b2"])
        self.assertEqual(bad, [])

    def test_dedupe_keeps_first_order(self):
        users, _ = normalize_users(["b2", "a1", "b2"])
        self.assertEqual(users, ["b2", "a1"])

    def test_unparsable_entries_reported(self):
        users, bad = normalize_users(["https://www.douban.com/people/ok1/", "bad id"])
        self.assertEqual(users, ["ok1"])
        self.assertEqual(bad, ["bad id"])

    def test_empty_input(self):
        self.assertEqual(normalize_users(None), ([], []))
        self.assertEqual(normalize_users([]), ([], []))


class ParseWishesTest(unittest.TestCase):
    def payload(self):
        return {
            "interests": [
                {
                    "status": "mark",
                    "create_time": "2026-10-20 21:00:00",
                    "subject": {"id": "36753674", "title": "某种物质", "type": "movie"},
                },
                {
                    "status": "mark",
                    "create_time": "2026-10-19 10:00:00",
                    "subject": {"id": "35623817", "title": "相对宇宙 第一季", "type": "tv"},
                },
                {
                    "status": "done",
                    "create_time": "2026-10-18 10:00:00",
                    "subject": {"id": "1", "title": "看过的", "type": "movie"},
                },
                {
                    "status": "mark",
                    "create_time": "2026-10-18 10:00:00",
                    "subject": {"id": "2", "title": "一本书", "type": "book"},
                },
                {
                    "status": "mark",
                    "create_time": "",
                    "subject": {"id": "3", "title": "没有收藏时间", "type": "movie"},
                },
                {
                    "status": "mark",
                    "create_time": "2026-10-18 10:00:00",
                    "subject": {"id": "", "title": "没有 id", "type": "movie"},
                },
                "不是字典",
            ]
        }

    def test_only_wanted_movie_and_tv(self):
        wishes = parse_wishes(self.payload())
        self.assertEqual([wish.douban_id for wish in wishes], ["36753674", "35623817"])
        self.assertEqual([wish.kind for wish in wishes], ["movie", "tv"])

    def test_empty_payload(self):
        self.assertEqual(parse_wishes({}), [])
        self.assertEqual(parse_wishes({"interests": None}), [])


class WindowTest(unittest.TestCase):
    def test_zero_or_negative_means_unlimited(self):
        self.assertIsNone(cutoff_string(0))
        self.assertIsNone(cutoff_string(-3))

    def test_cutoff_is_beijing_time(self):
        now = datetime(2026, 10, 22, 12, 0, 0, tzinfo=CST)
        self.assertEqual(cutoff_string(7, now=now), "2026-10-15 12:00:00")

    def test_in_window_compares_strings(self):
        cutoff = "2026-10-15 12:00:00"
        self.assertTrue(in_window("2026-10-15 12:00:00", cutoff))
        self.assertTrue(in_window("2026-10-20 08:00:00", cutoff))
        self.assertFalse(in_window("2026-10-15 11:59:59", cutoff))

    def test_no_cutoff_keeps_everything(self):
        self.assertTrue(in_window("1999-01-01 00:00:00", None))


class CjkNumberTest(unittest.TestCase):
    def test_digits_pass_through(self):
        self.assertEqual(cjk_number("5"), 5)
        self.assertEqual(cjk_number("23"), 23)

    def test_ten_forms(self):
        self.assertEqual(cjk_number("十"), 10)
        self.assertEqual(cjk_number("十二"), 12)
        self.assertEqual(cjk_number("二十"), 20)
        self.assertEqual(cjk_number("二十一"), 21)

    def test_unknown(self):
        self.assertIsNone(cjk_number(""))
        self.assertIsNone(cjk_number("很多"))


class SeasonMarkerTest(unittest.TestCase):
    def test_chinese_season(self):
        self.assertEqual(season_marker("相对宇宙 第二季"), 2)
        self.assertEqual(season_marker("长安十二时辰 第一部"), 1)

    def test_english_season(self):
        self.assertEqual(season_marker("The Expanse Season 3"), 3)
        self.assertEqual(season_marker("Some Show series 2"), 2)

    def test_short_form(self):
        self.assertEqual(season_marker("Show S02"), 2)

    def test_no_marker(self):
        self.assertIsNone(season_marker("沙丘"))
        self.assertIsNone(season_marker(""))

    def test_s_inside_a_word_is_not_a_season(self):
        self.assertIsNone(season_marker("Hits 2024"))


class PlanForTest(unittest.TestCase):
    def test_movie(self):
        self.assertEqual(plan_for("某种物质", "movie"), ([], "电影"))

    def test_tv_with_season_in_title(self):
        self.assertEqual(plan_for("相对宇宙 第二季", "tv"), ([2], "按第 2 季订阅"))

    def test_tv_without_season_defaults_to_first(self):
        self.assertEqual(plan_for("某部剧", "tv"), ([1], "标题没写第几季，按第 1 季订阅"))


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


class BaselineStringTest(unittest.TestCase):
    def test_no_baseline(self):
        self.assertIsNone(baseline_string(None))
        self.assertIsNone(baseline_string(0))
        self.assertIsNone(baseline_string(""))
        self.assertIsNone(baseline_string(-1))

    def test_stamp_is_beijing_time(self):
        # 2026-10-22 04:00:00 UTC == 2026-10-22 12:00:00 北京时间
        stamp = datetime(2026, 10, 22, 4, 0, 0, tzinfo=timezone.utc).timestamp()
        self.assertEqual(baseline_string(stamp), "2026-10-22 12:00:00")

    def test_result_is_comparable_with_douban_time(self):
        # 关键性质：基线和豆瓣返回的 create_time 同格式，能直接按字符串比大小
        stamp = datetime(2026, 10, 22, 12, 0, 0, tzinfo=CST).timestamp()
        floor = baseline_string(stamp)
        self.assertEqual(floor, "2026-10-22 12:00:00")
        self.assertTrue(in_window("2026-10-22 12:00:00", floor))
        self.assertFalse(in_window("2026-10-22 11:59:59", floor))

    def test_accepts_string_of_number(self):
        # 从插件存储里读回来可能是字符串
        stamp = datetime(2026, 10, 22, 12, 0, 0, tzinfo=CST).timestamp()
        self.assertEqual(baseline_string(str(stamp)), "2026-10-22 12:00:00")


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
        # 拿不到状态码（网络层直接抛）时宁可下轮重试，也不要永久放弃
        self.assertFalse(is_permanent_error(OpsError("op", "boom")))
        self.assertFalse(is_permanent_error(self.exc("500")))
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
