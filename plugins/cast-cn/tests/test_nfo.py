from __future__ import annotations

import unittest

from cast_cn.matching import CastEntry
from cast_cn.nfo import (
    NfoError,
    NfoIdentity,
    build_identity_root,
    ensure_identity,
    merge_actors,
    parse_nfo,
    read_tmdb_id,
    serialize_nfo,
)

IDENTITY = NfoIdentity(
    kind="movie", tmdb_id=603, title="黑客帝国", original_title="The Matrix", year=1999
)


class NfoTest(unittest.TestCase):
    def test_build_and_roundtrip(self) -> None:
        root = build_identity_root(IDENTITY)
        merge_actors(
            root,
            [
                CastEntry(
                    name="基努·里维斯",
                    role="尼奥",
                    thumb_url="https://image.tmdb.org/t/p/w185/a.jpg",
                    tmdb_person_id=6384,
                    order=0,
                )
            ],
        )
        text = serialize_nfo(root)
        self.assertIn('standalone="yes"', text)
        self.assertIn("<name>基努·里维斯</name>", text)
        self.assertIn("<role>尼奥</role>", text)
        self.assertIn("<tmdbid>6384</tmdbid>", text)

        parsed = parse_nfo(text)
        self.assertEqual(read_tmdb_id(parsed), 603)
        actor = parsed.find("actor")
        self.assertIsNotNone(actor)
        assert actor is not None
        self.assertEqual(actor.findtext("thumb"), "https://image.tmdb.org/t/p/w185/a.jpg")

    def test_identity_mismatch_is_rejected(self) -> None:
        root = build_identity_root(IDENTITY)
        other = NfoIdentity(
            kind="movie", tmdb_id=604, title="x", original_title=None, year=None
        )
        with self.assertRaises(NfoError):
            ensure_identity(root, other)

    def test_merge_preserves_other_nodes_and_comments(self) -> None:
        text = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<movie>
  <title>黑客帝国</title>
  <plot>不要动我</plot>
  <!-- 用户注释 -->
  <actor><name>旧演员</name><role>旧角色</role></actor>
  <actor><name>另一个旧演员</name><role>另一个角色</role></actor>
</movie>
"""
        root = parse_nfo(text)
        changed = merge_actors(
            root,
            [CastEntry(name="基努·里维斯", role="尼奥", tmdb_person_id=6384, order=0)],
        )
        self.assertTrue(changed)
        out = serialize_nfo(root)
        self.assertIn("<plot>不要动我</plot>", out)
        self.assertIn("<!-- 用户注释 -->", out)
        self.assertNotIn("旧演员", out)
        self.assertIn("<name>基努·里维斯</name>", out)
        self.assertEqual(len(root.findall("actor")), 1)

    def test_merge_is_noop_when_identical(self) -> None:
        root = build_identity_root(IDENTITY)
        actors = [
            CastEntry(name="基努·里维斯", role="尼奥", tmdb_person_id=6384, order=0)
        ]
        self.assertTrue(merge_actors(root, actors))
        before = serialize_nfo(root)
        self.assertFalse(merge_actors(root, actors))
        self.assertEqual(serialize_nfo(root), before)

    def test_relative_thumb_is_not_written(self) -> None:
        root = build_identity_root(IDENTITY)
        merge_actors(
            root,
            [
                CastEntry(
                    name="某人",
                    role="某角",
                    thumb_url="/images/people/a.jpg",
                    order=0,
                )
            ],
        )
        actor = root.find("actor")
        self.assertIsNotNone(actor)
        assert actor is not None
        self.assertIsNone(actor.find("thumb"))

    def test_tmdbid_uniqueid_fallback(self) -> None:
        text = """<movie><uniqueid type="tmdb">603</uniqueid></movie>"""
        root = parse_nfo(text)
        self.assertEqual(read_tmdb_id(root), 603)


if __name__ == "__main__":
    unittest.main()
