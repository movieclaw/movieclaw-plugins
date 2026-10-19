"""观看与明确喜欢：真实排序、离线排除、撤销、持久化和账号隔离。"""
import copy
import random
import unittest
from test_personal import personal, FakeDiscovery, FakePrivateSDK, raw_row


def item(identity, genre=80):
    return personal._public_item(raw_row("movie", identity, genre=genre), "movie")


class WatchLikeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = FakePrivateSDK()
        self.service = personal.PersonalRecommendations(FakeDiscovery(), private_store=self.store)
        self.token = (await self.service.create_profile())["profile_token"]

    async def asyncTearDown(self):
        await self.service.close()

    async def test_independent_flags_idempotent_undo_persist_and_no_public_annotations(self):
        value = item(911)
        await self.service.feedback(self.token, "like", value)
        await self.service.feedback(self.token, "watch", value)
        shelf = await self.service.feedback(self.token, "watch", value)
        self.assertEqual(len(shelf["liked"]), 1)
        self.assertEqual(len(shelf["watched"]), 1)
        restarted = personal.PersonalRecommendations(FakeDiscovery(), private_store=self.store)
        try:
            self.assertEqual(await restarted.shelf(self.token), shelf)
            other = (await restarted.create_profile())["profile_token"]
            self.assertEqual((await restarted.shelf(other))["liked"], [])
            await restarted.feedback(self.token, "unwatch", {"detail_ref": value["detail_ref"]})
            self.assertEqual(len((await restarted.shelf(self.token))["liked"]), 1)
            await restarted.feedback(self.token, "unlike", {"detail_ref": value["detail_ref"]})
            view = await restarted.get_profile(self.token)
            self.assertEqual(view["learning"]["watched"], 0)
            self.assertEqual(view["learning"]["liked"], 0)
            self.assertTrue(all(write["secret"] for write in self.store.writes))
            self.assertNotIn("watched", value)
            self.assertNotIn("liked", value)
        finally:
            await restarted.close()

    async def test_explicit_like_outweighs_passive_watching_and_negative_overrides(self):
        watched, liked = item(912, 35), item(913, 80)
        await self.service.feedback(self.token, "watch", watched)
        scope = self.service._scope(self.token)
        weak = personal._behavior(await self.service._profile(scope), {"items": []})
        self.assertLessEqual(weak[0]["genre:35"], .2)
        await self.service.feedback(self.token, "like", liked)
        both = personal._behavior(await self.service._profile(scope), {"items": []})
        self.assertGreater(both[0]["genre:80"], both[0]["genre:35"] * 10)
        baseline = self.service._rank(liked, {}, personal.DEFAULT_ANSWERS, random.Random(2), set(), ({}, {}, {}))[0]
        ranked = self.service._rank(liked, {}, personal.DEFAULT_ANSWERS, random.Random(2), set(), both)[0]
        self.assertGreater(ranked, baseline)
        await self.service.feedback(self.token, "hide", liked)
        hidden = personal._behavior(await self.service._profile(scope), {"items": []})
        self.assertNotIn("genre:80", hidden[0])
        self.assertIn("genre:80", hidden[1])
        await self.service.feedback(self.token, "unhide", {"detail_ref": liked["detail_ref"]})
        self.assertIn("genre:80", personal._behavior(await self.service._profile(scope), {"items": []})[0])
        await self.service.feedback(self.token, "unlike", {"detail_ref": liked["detail_ref"]})
        self.assertNotIn("genre:80", personal._behavior(await self.service._profile(scope), {"items": []})[0])

    async def test_watched_excluded_with_same_nonce_learning_off_and_private_offline_fallback(self):
        first = await self.service.recommend(self.token, "same", limit=6)
        watched = first["items"][0]
        await self.service.feedback(self.token, "like", watched)
        await self.service.feedback(self.token, "watch", watched)
        fresh = await self.service.recommend(self.token, "same", limit=6)
        self.assertNotIn(watched["detail_ref"], [row["detail_ref"] for row in fresh["items"]])
        await self.service.learning(self.token, False)
        scope = self.service._scope(self.token)
        self.assertEqual(personal._behavior(await self.service._profile(scope), {"items": []})[:2], ({}, {}))
        self.service._pools.clear()
        self.service._public_snapshot = None
        self.service._public_loaded = True
        async def no_pool(*args, **kwargs): return None
        self.service._pool = no_pool
        fallback = await self.service.recommend(self.token, "offline", limit=6)
        self.assertNotIn(watched["detail_ref"], [row["detail_ref"] for row in fallback["items"]])
        await self.service.feedback(self.token, "unwatch", {"detail_ref": watched["detail_ref"]})
        profile = await self.service._profile(scope)
        profile["recent"] = []
        await self.service._write(scope, "profile", profile)
        # Force a one-title public pool: undo must allow the watched film back.
        pool = {"items": [watched], "backend": "tmdb", "status": "ready", "next_queries": [], "partial": False}
        async def one_pool(*args, **kwargs): return copy.deepcopy(pool)
        self.service._pool = one_pool
        self.service._public_snapshot = {"items": [watched]}
        restored = await self.service.recommend(self.token, "restored", limit=3)
        self.assertEqual([row["detail_ref"] for row in restored["items"]], [watched["detail_ref"]])

    async def test_duplicate_positive_title_is_not_counted_as_three_observations(self):
        value = item(914)
        await self.service.feedback(self.token, "like", value)
        before = personal._behavior(await self.service._profile(self.service._scope(self.token)), {"items": []})[:2]
        await self.service.feedback(self.token, "watch", value)
        await self.service.feedback(self.token, "save", value)
        after = personal._behavior(await self.service._profile(self.service._scope(self.token)), {"items": []})[:2]
        self.assertAlmostEqual(before[0]["genre:80"], after[0]["genre:80"], places=5)

    async def test_invalid_feedback_cannot_modify_profile_and_capacity_keeps_old_flags(self):
        original = await self.service.get_profile(self.token)
        for action, value in [("like", {"detail_ref": "https://bad.test/1"}), ("watch", {"detail_ref": "tmdb:movie:915"}), ("watched", item(915))]:
            with self.assertRaises(personal.PersonalProfileError):
                await self.service.feedback(self.token, action, value)
        self.assertEqual((await self.service.get_profile(self.token))["revision"], original["revision"])
        scope = self.service._scope(self.token)
        profile = await self.service._profile(scope)
        profile["liked"] = [{"item": {"detail_ref": "tmdb:movie:" + str(index)}, "liked_at": "2026-10-19T00:00:00+00:00"} for index in range(1, 301)]
        await self.service._write(scope, "profile", profile)
        with self.assertRaisesRegex(personal.PersonalProfileError, "已满"):
            await self.service.feedback(self.token, "like", item(915))
        self.assertEqual(len((await self.service.shelf(self.token))["liked"]), 300)
        await self.service.feedback(self.token, "like", item(1))
        self.assertEqual(len((await self.service.shelf(self.token))["liked"]), 300)
