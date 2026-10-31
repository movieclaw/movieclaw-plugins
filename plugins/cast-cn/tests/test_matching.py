from __future__ import annotations

import unittest

from cast_cn.matching import (
    CastEntry,
    DoubanActor,
    clean_role,
    has_chinese,
    is_broken_thumb,
    match_actor,
    merge_cast,
    normalize_name,
    tmdb_profile_url,
)
from cast_cn.settings import AvatarPolicy, Condition


class MatchingTest(unittest.TestCase):
    def test_has_chinese(self) -> None:
        self.assertTrue(has_chinese("丹尼尔·克雷格"))
        self.assertTrue(has_chinese("丹尼尔 Daniel"))
        self.assertFalse(has_chinese("Daniel Craig"))
        self.assertFalse(has_chinese(""))
        self.assertFalse(has_chinese(None))

    def test_normalize_name(self) -> None:
        self.assertEqual(normalize_name("Daniel Craig"), normalize_name("daniel  craig"))
        self.assertEqual(normalize_name("Daniel-Craig"), "danielcraig")
        self.assertEqual(normalize_name("José"), "jose")
        self.assertEqual(normalize_name("丹尼尔·克雷格"), "丹尼尔克雷格")

    def test_clean_role(self) -> None:
        self.assertEqual(clean_role("饰 詹姆斯·邦德 James Bond 007"), "詹姆斯·邦德")
        self.assertEqual(clean_role("饰演 老东家"), "老东家")
        self.assertEqual(clean_role("James Bond"), "James Bond")
        self.assertIsNone(clean_role(""))

    def test_match_by_latin_name(self) -> None:
        actor = CastEntry(name="Daniel Craig", role="James Bond")
        douban = [
            DoubanActor(
                name="丹尼尔·克雷格",
                latin_name="Daniel Craig",
                character="饰 詹姆斯·邦德 James Bond",
            ),
            DoubanActor(name="其他演员", latin_name="Other Actor", character="饰 某人"),
        ]
        matched = match_actor(actor, douban)
        self.assertIsNotNone(matched)
        assert matched is not None
        self.assertEqual(matched.name, "丹尼尔·克雷格")

    def test_match_by_chinese_name(self) -> None:
        actor = CastEntry(name="丹尼尔·克雷格", role="James Bond")
        douban = [
            DoubanActor(
                name="丹尼尔·克雷格",
                latin_name="Daniel Craig",
                character="饰 詹姆斯·邦德",
            )
        ]
        self.assertIsNotNone(match_actor(actor, douban))

    def test_ambiguous_match_is_rejected(self) -> None:
        actor = CastEntry(name="Daniel Craig", role="James Bond")
        douban = [
            DoubanActor(
                name="丹尼尔·克雷格",
                latin_name="Daniel Craig",
                character="饰 詹姆斯·邦德",
            ),
            DoubanActor(
                name="另一个丹尼尔",
                latin_name="Daniel Craig",
                character="饰 另一个角色",
            ),
        ]
        self.assertIsNone(match_actor(actor, douban))

    def test_merge_name_and_role(self) -> None:
        actors = [
            CastEntry(
                name="Daniel Craig",
                role="James Bond",
                thumb_url="https://image.tmdb.org/t/p/w185/a.jpg",
                tmdb_person_id=8784,
            )
        ]
        douban = [
            DoubanActor(
                name="丹尼尔·克雷格",
                latin_name="Daniel Craig",
                character="饰 詹姆斯·邦德 James Bond",
                avatar_url="https://img.douban.com/a.jpg",
            )
        ]
        result = merge_cast(actors, douban, condition=Condition.ALL)
        self.assertTrue(result.changed)
        self.assertEqual(result.actors[0].name, "丹尼尔·克雷格")
        self.assertEqual(result.actors[0].role, "詹姆斯·邦德")
        self.assertEqual(result.actors[0].tmdb_person_id, 8784)
        # tmdb_first：已有 TMDB 头像不被豆瓣替换
        self.assertEqual(
            result.actors[0].thumb_url, "https://image.tmdb.org/t/p/w185/a.jpg"
        )

    def test_merge_fills_missing_avatar_from_douban(self) -> None:
        actors = [CastEntry(name="Daniel Craig", role="James Bond", tmdb_person_id=8784)]
        douban = [
            DoubanActor(
                name="丹尼尔·克雷格",
                latin_name="Daniel Craig",
                character="饰 詹姆斯·邦德",
                avatar_url="https://img.douban.com/a.jpg",
            )
        ]
        result = merge_cast(actors, douban, avatar_policy=AvatarPolicy.TMDB_FIRST)
        self.assertEqual(result.actors[0].thumb_url, "https://img.douban.com/a.jpg")

    def test_merge_renames_only_with_absolute_thumb(self) -> None:
        actors = [
            CastEntry(
                name="Daniel Craig",
                role="James Bond",
                thumb_url="/images/people/w185/a.jpg",
                tmdb_person_id=8784,
            )
        ]
        douban = [
            DoubanActor(
                name="丹尼尔·克雷格",
                latin_name="Daniel Craig",
                character="饰 詹姆斯·邦德",
            )
        ]
        # 没有外部地址：保留原名，只改角色，避免头像丢失
        result = merge_cast(actors, douban, base_url=None)
        self.assertEqual(result.actors[0].name, "Daniel Craig")
        self.assertEqual(result.actors[0].role, "詹姆斯·邦德")
        self.assertTrue(result.warnings)

        # 给了外部地址：可以改名，并把相对头像写成绝对 URL
        result = merge_cast(actors, douban, base_url="https://movieclaw.example.com")
        self.assertEqual(result.actors[0].name, "丹尼尔·克雷格")
        self.assertEqual(
            result.actors[0].thumb_url,
            "https://movieclaw.example.com/images/people/w185/a.jpg",
        )

    def test_keep_person_links_false_drops_link_after_rename(self) -> None:
        actors = [CastEntry(name="Daniel Craig", role="James Bond", tmdb_person_id=8784)]
        douban = [
            DoubanActor(
                name="丹尼尔·克雷格",
                latin_name="Daniel Craig",
                character="饰 詹姆斯·邦德",
            )
        ]
        result = merge_cast(actors, douban, keep_person_links=False)
        self.assertEqual(result.actors[0].name, "丹尼尔·克雷格")
        self.assertIsNone(result.actors[0].tmdb_person_id)

    def test_remove_non_chinese(self) -> None:
        actors = [
            CastEntry(name="Daniel Craig", role="James Bond"),
            CastEntry(name="Unknown Actor", role="Someone"),
        ]
        douban = [
            DoubanActor(
                name="丹尼尔·克雷格",
                latin_name="Daniel Craig",
                character="饰 詹姆斯·邦德",
            )
        ]
        result = merge_cast(actors, douban, remove_non_chinese=True)
        self.assertEqual([a.name for a in result.actors], ["丹尼尔·克雷格"])
        self.assertEqual(result.dropped, ["Unknown Actor"])
        self.assertTrue(result.changed)


class BrokenThumbTest(unittest.TestCase):
    """失效头像修复：老工具写进 NFO 的无扩展名 TMDB 地址（必然 404）。"""

    BROKEN = "https://image.tmdb.org/t/p/original/usWnHCzbADijULREZYSJ0qfM"
    FIXED = "https://image.tmdb.org/t/p/original/usWnHCzbADijULREZYSJ0qfM00y.jpg"

    def test_is_broken_thumb(self) -> None:
        self.assertTrue(is_broken_thumb(self.BROKEN))
        self.assertFalse(is_broken_thumb(self.FIXED))
        self.assertFalse(is_broken_thumb("https://img1.doubanio.com/view/photo/p1.jpg"))
        self.assertFalse(
            is_broken_thumb("/images/people/original/us/usWnHCzbADijULREZYSJ0qfM00y.jpg")
        )
        self.assertFalse(is_broken_thumb(None))
        self.assertFalse(is_broken_thumb(""))

    def test_tmdb_profile_url(self) -> None:
        self.assertEqual(
            tmdb_profile_url("/images/people/original/us/usWnHCzbADijULREZYSJ0qfM00y.jpg"),
            self.FIXED,
        )
        self.assertEqual(
            tmdb_profile_url("https://image.tmdb.org/t/p/w185/usWnHCzbADijULREZYSJ0qfM00y.jpg"),
            self.FIXED,
        )
        self.assertIsNone(tmdb_profile_url(self.BROKEN))
        self.assertIsNone(tmdb_profile_url("https://img1.doubanio.com/view/photo/p1.jpg"))
        self.assertIsNone(tmdb_profile_url(None))

    def test_repair_applied_even_without_gate(self) -> None:
        """修坏头像是纠错：姓名角色全中文（处理条件不命中）也要修。"""
        actor = CastEntry(name="全中文", role="全中文角色", thumb_url=self.BROKEN)
        result = merge_cast([actor], [], avatar_repairs={"全中文": self.FIXED})
        self.assertTrue(result.changed)
        self.assertEqual(result.actors[0].thumb_url, self.FIXED)

    def test_repair_prefers_person_over_douban(self) -> None:
        """人物表回填的地址比豆瓣头像更权威。"""
        actor = CastEntry(name="丹尼尔", role="邦德", thumb_url=self.BROKEN)
        douban = [
            DoubanActor(
                name="丹尼尔", character="詹姆斯·邦德", avatar_url="https://img1.doubanio.com/d.jpg"
            )
        ]
        result = merge_cast([actor], douban, avatar_repairs={"丹尼尔": self.FIXED})
        self.assertEqual(result.actors[0].thumb_url, self.FIXED)

    def test_broken_thumb_treated_as_missing_for_douban(self) -> None:
        """失效头像按缺失处理：人物表没查到时豆瓣照常能补。"""
        actor = CastEntry(name="丹尼尔", role="邦德", thumb_url=self.BROKEN)
        douban = [
            DoubanActor(
                name="丹尼尔", character="詹姆斯·邦德", avatar_url="https://img1.doubanio.com/d.jpg"
            )
        ]
        result = merge_cast([actor], douban)
        self.assertEqual(result.actors[0].thumb_url, "https://img1.doubanio.com/d.jpg")

    def test_broken_thumb_without_repair_kept(self) -> None:
        """没救回来的保持原样：不把坏头像删掉，等下一轮再修。"""
        actor = CastEntry(name="路人甲", role="路人", thumb_url=self.BROKEN)
        result = merge_cast([actor], [])
        self.assertEqual(result.actors[0].thumb_url, self.BROKEN)
        self.assertFalse(result.changed)

    def test_missing_thumb_filled_by_repair(self) -> None:
        """头像缺失且人物表有正确地址：直接回填（比等豆瓣更准）。"""
        actor = CastEntry(name="丹尼尔", role="邦德", thumb_url=None)
        result = merge_cast([actor], [], avatar_repairs={"丹尼尔": self.FIXED})
        self.assertEqual(result.actors[0].thumb_url, self.FIXED)

    def test_policy_none_skips_repair_at_runtime(self) -> None:
        """「不改动头像」时运行时不会传修复表：合并层尊重传入的空表。"""
        actor = CastEntry(name="全中文", role="全中文角色", thumb_url=self.BROKEN)
        result = merge_cast([actor], [], avatar_repairs=None)
        self.assertEqual(result.actors[0].thumb_url, self.BROKEN)
        self.assertFalse(result.changed)


if __name__ == "__main__":
    unittest.main()
