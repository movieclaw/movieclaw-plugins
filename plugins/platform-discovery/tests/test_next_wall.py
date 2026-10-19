"""Speculation is private, uncommitted, bounded and revalidated atomically."""
import copy
import time
import unittest
from test_personal import personal, FakePrivateSDK, FakeDiscovery


class NextWallTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = FakePrivateSDK()
        self.service = personal.PersonalRecommendations(FakeDiscovery(), private_store=self.store)
        self.token = (await self.service.create_profile())["profile_token"]
        self.scope = self.service._scope(self.token)

    async def profile(self):
        return copy.deepcopy(await self.service._profile(self.scope))

    async def test_preparation_does_not_mark_shown_and_commit_is_idempotent(self):
        await self.service.recommend(self.token, "first", limit=12)
        before = await self.profile()
        prepared = await self.service.prepare_wall(self.token, nonce="next", limit=12)
        self.assertEqual(await self.profile(), before)
        self.assertFalse({x["id"] for x in prepared["items"]} & set(before["recent"][-1]))
        self.assertNotIn("batch:" + prepared["batch_id"], before["batches"])
        shown = await self.service.commit_wall(self.token, prepared["prepared_ticket"])
        after = await self.profile()
        self.assertEqual(shown["items"], prepared["items"])
        self.assertEqual(len(after["recent"]), len(before["recent"]) + 1)
        self.assertEqual(after["recent"][-1], [x["id"] for x in shown["items"]])
        self.assertEqual(await self.service.commit_wall(self.token, prepared["prepared_ticket"]), shown)
        self.assertEqual(await self.profile(), after)

    async def test_latest_watched_hidden_saved_likes_learning_and_tests_invalidate(self):
        for action in ("watch", "hide", "save", "like"):
            prepared = await self.service.prepare_wall(self.token, nonce=action, limit=12)
            item = prepared["items"][0]
            await self.service.feedback(self.token, action, item)
            with self.assertRaises(personal.PersonalProfileError) as caught:
                await self.service.commit_wall(self.token, prepared["prepared_ticket"])
            self.assertEqual(caught.exception.status, 409)
            replacement = await self.service.prepare_wall(self.token, nonce=action+"-new", limit=12)
            if action != "like":
                self.assertNotIn(item["detail_ref"], [x["detail_ref"] for x in replacement["items"]])
        before = await self.profile()
        self.assertEqual(before["recent"], [])
        await self.service.learning(self.token, False)
        with self.assertRaises(personal.PersonalProfileError):
            await self.service.commit_wall(self.token, replacement["prepared_ticket"])
        replacement = await self.service.prepare_wall(self.token, nonce="test", limit=12)
        await self.service.submit(self.token, copy.deepcopy(personal.DEFAULT_ANSWERS))
        with self.assertRaises(personal.PersonalProfileError):
            await self.service.commit_wall(self.token, replacement["prepared_ticket"])

    async def test_other_device_shown_history_expiry_replacement_and_account_isolation(self):
        first = await self.service.prepare_wall(self.token, nonce="old", limit=12)
        second = await self.service.prepare_wall(self.token, nonce="new", limit=12, media_type="tv", era="classic")
        self.assertTrue(all(i["media_type"] == "tv" and i["year"] < 2000 for i in second["items"]))
        with self.assertRaises(personal.PersonalProfileError):
            await self.service.commit_wall(self.token, first["prepared_ticket"])
        other = (await self.service.create_profile())["profile_token"]
        with self.assertRaises(personal.PersonalProfileError):
            await self.service.commit_wall(other, second["prepared_ticket"])
        await self.service.recommend(self.token, "other-device", limit=12)
        with self.assertRaises(personal.PersonalProfileError):
            await self.service.commit_wall(self.token, second["prepared_ticket"])
        fresh = await self.service.prepare_wall(self.token, nonce="expiry", limit=12)
        stage = await self.service._read(self.scope, "next-wall")
        stage["queue"][0]["expires"] = time.time() - 1
        await self.service._write(self.scope, "next-wall", stage)
        with self.assertRaises(personal.PersonalProfileError):
            await self.service.commit_wall(self.token, fresh["prepared_ticket"])

    async def test_three_faces_are_distinct_unshown_and_consumed_in_order(self):
        await self.service.recommend(self.token, "current", limit=24)
        before = await self.profile()
        faces = []
        for index in range(3):
            faces.append(await self.service.prepare_wall(self.token, nonce="queue"+str(index), limit=24,
                after=faces[-1]["prepared_ticket"] if faces else ""))
        self.assertEqual(await self.profile(), before)
        ids = [set(x["id"] for x in face["items"]) for face in faces]
        self.assertEqual(len(set.union(*ids)), sum(map(len, ids)))
        self.assertFalse(set.union(*ids) & set(before["recent"][-1]))
        with self.assertRaises(personal.PersonalProfileError):
            await self.service.commit_wall(self.token, faces[1]["prepared_ticket"])
        recovered = await self.service.prepare_wall(self.token, nonce="recover", limit=24, after=faces[0]["prepared_ticket"])
        self.assertEqual(recovered["prepared_ticket"], faces[1]["prepared_ticket"], "cancelled request can recover same next ticket")
        capped = await self.service.prepare_wall(self.token, nonce="fourth", limit=24, after=faces[-1]["prepared_ticket"])
        self.assertEqual(capped["prepared_ticket"], faces[-1]["prepared_ticket"])
        for index, face in enumerate(faces):
            shown = await self.service.commit_wall(self.token, face["prepared_ticket"])
            self.assertEqual(shown["items"], face["items"])
            self.assertEqual(len((await self.profile())["recent"]), len(before["recent"])+index+1)
        self.assertEqual(len((await self.service._read(self.scope, "next-wall"))["queue"]), 0)

    async def test_watch_invalidates_all_three_and_refill_after_commit_excludes_pending(self):
        faces = []
        for index in range(3):
            faces.append(await self.service.prepare_wall(self.token, nonce="three"+str(index), limit=12))
        await self.service.commit_wall(self.token, faces[0]["prepared_ticket"])
        refill = await self.service.prepare_wall(self.token, nonce="refill", limit=12, after=faces[-1]["prepared_ticket"])
        self.assertFalse({i["id"] for i in refill["items"]} & {i["id"] for f in faces for i in f["items"]})
        watched = faces[2]["items"][0]
        await self.service.feedback(self.token, "watch", watched)
        for face in faces[1:]+[refill]:
            with self.assertRaises(personal.PersonalProfileError):
                await self.service.commit_wall(self.token, face["prepared_ticket"])
        fresh = await self.service.prepare_wall(self.token, nonce="new-basis", limit=12, after="")
        self.assertNotIn(watched["detail_ref"], [i["detail_ref"] for i in fresh["items"]])

    async def test_page_queues_are_isolated_bounded_and_all_respond_to_account_history(self):
        desktop = await self.service.prepare_wall(self.token, nonce="desktop", limit=24, queue_id="desktop", after="")
        mobile = await self.service.prepare_wall(self.token, nonce="mobile", limit=6, queue_id="phone", after="")
        again = await self.service.prepare_wall(self.token, nonce="retry", limit=24, queue_id="desktop", after="")
        self.assertEqual(again["prepared_ticket"], desktop["prepared_ticket"])
        with self.assertRaises(personal.PersonalProfileError):
            await self.service.commit_wall(self.token, desktop["prepared_ticket"], queue_id="phone")
        await self.service.commit_wall(self.token, desktop["prepared_ticket"], queue_id="desktop")
        with self.assertRaises(personal.PersonalProfileError):
            await self.service.commit_wall(self.token, mobile["prepared_ticket"], queue_id="phone")
        for i in range(6):
            await self.service.prepare_wall(self.token, nonce="bounded"+str(i), limit=6, queue_id="tab"+str(i), after="")
        index = await self.service._read(self.scope, "next-wall-index")
        self.assertEqual(len(index), 4)
        stage_metas = [key for scope,key in self.store.values if scope==self.scope and key.startswith("m:next-wall:")]
        self.assertEqual(len(stage_metas), 4)
        for i in range(2):
            old = await self.service._stage_key(self.scope, "tab"+str(i))
            self.assertIsNone(await self.service._read(self.scope, old))
