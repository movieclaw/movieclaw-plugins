from __future__ import annotations

import unittest

from cast_cn.matching import (
    CastEntry,
    DoubanActor,
    clean_role,
    has_chinese,
    match_actor,
    merge_cast,
    normalize_name,
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


if __name__ == "__main__":
    unittest.main()
