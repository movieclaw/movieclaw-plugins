"""Private capability profiles, repeated taste tests and real-source selection."""
import asyncio
import copy
import gc
import hashlib
import importlib.util
import json
import math
import random
import re
import time
import unittest
from pathlib import Path
from unittest.mock import patch


module_path = Path(__file__).parents[1] / "platform_discovery" / "personal.py"
spec = importlib.util.spec_from_file_location("platform_discovery_personal_tests", module_path)
personal = importlib.util.module_from_spec(spec)
spec.loader.exec_module(personal)


class FakePrivateSDK:
    SCOPE = re.compile(r"^(global|[a-z_]+:[A-Za-z0-9_.\-]{1,100})$")

    def __init__(self):
        self.values, self.writes, self.fail = {}, [], False

    @classmethod
    def check(cls, scope, key):
        # Match the v0.39 PluginStore contract, including the entity-name syntax.
        if not cls.SCOPE.match(scope):
            raise ValueError("Invalid SDK scope")
        if not key or len(key) > 200:
            raise ValueError("Invalid SDK key")

    async def get(self, key, *, scope, default=None):
        self.check(scope, key)
        if self.fail:
            raise RuntimeError("PRIVATE storage diagnostic")
        return copy.deepcopy(self.values.get((scope, key), default))

    async def set(self, key, value, *, scope, secret=False):
        self.check(scope, key)
        if self.fail:
            raise RuntimeError("PRIVATE storage diagnostic")
        if len(json.dumps(value, ensure_ascii=False).encode()) > 64 * 1024:
            raise ValueError("SDK per-value limit")
        self.writes.append({"scope": scope, "key": key, "secret": secret})
        self.values[scope, key] = copy.deepcopy(value)

    async def delete(self, key, *, scope):
        self.check(scope, key)
        self.values.pop((scope, key), None)


class FakePublicStorage:
    def __init__(self):
        self.values, self.fail = {}, False

    async def get(self, key):
        if self.fail:
            raise RuntimeError("PRIVATE public-cache storage diagnostic")
        return copy.deepcopy(self.values.get(key))

    async def set(self, key, value, ttl_seconds):
        if self.fail:
            raise RuntimeError("PRIVATE public-cache storage diagnostic")
        self.values[key] = copy.deepcopy(value)


def raw_row(kind, identity, year=1985, country="US", genre=18, **changes):
    return {"id": identity, "title": "真实来源电影 " + str(identity), "name": "真实来源剧集 " + str(identity),
            "original_title": "Original movie", "original_name": "Original TV", "origin_country": [country],
            "original_language": {"US": "en", "GB": "en", "CN": "zh", "TW": "zh", "HK": "cn", "KR": "ko", "JP": "ja"}.get(country, "th"),
            "genre_ids": [genre], "vote_average": 8.0, "vote_count": 500,
            "release_date": str(year) + "-01-01", "first_air_date": str(year) + "-01-01",
            "poster_path": "/poster" + str(identity) + ".jpg", "backdrop_path": "/backdrop.jpg", "overview": "公开简介", **changes}


class FakeDiscovery:
    def __init__(self, token="PRIVATE-TMDB-TOKEN"):
        self._token, self.calls, self.fail, self.no_posters = token, [], False, False

    async def _json(self, url, params, tmdb=False):
        self.calls.append((url, copy.deepcopy(params), tmdb))
        if self.fail:
            raise RuntimeError("PRIVATE upstream secret")
        kind = url.rsplit("/", 1)[-1]
        base = int(hashlib.sha256((url + json.dumps(params, sort_keys=True)).encode()).hexdigest()[:7], 16)
        lane = "classic" if any(value == "1989-12-31" for value in params.values()) else "middle" if any(value == "2009-12-31" for value in params.values()) else "modern"
        year = {"classic": 1960, "middle": 1990, "modern": 2010}[lane]
        date_prefix = "primary_release_date" if kind == "movie" else "first_air_date"
        low = params.get(date_prefix + ".gte")
        high = params.get(date_prefix + ".lte")
        if low: year = int(low[:4])
        if high and year > int(high[:4]): year = max(1880, int(high[:4]) - 14)
        span = max(1, min(15, int(high[:4]) - year + 1)) if high else 15
        countries = ["US", "GB", "CN", "KR", "JP", "TW", "HK", "TH"]
        rows = [raw_row(kind, base + index, year + index % span, params.get("with_origin_country") or countries[index % len(countries)],
                        int(params["with_genres"]) if params.get("with_genres") else [18, 35, 80, 9648, 878, 14, 12, 16][index % 8],
                        poster_path=None if self.no_posters else "/poster" + str(base + index) + ".jpg") for index in range(20)]
        return {"results": rows, "total_pages": 10}


class FakeOps:
    def __init__(self):
        self.calls, self.fail = [], False

    async def call(self, operation, params):
        self.calls.append((operation, copy.deepcopy(params)))
        if self.fail:
            raise RuntimeError("PRIVATE host ops diagnostic")
        kind = params["media_type"]
        base = int(hashlib.sha256(json.dumps(params, sort_keys=True).encode()).hexdigest()[:7], 16)
        return {"titles": [{"title_ref": f"tmdb:{kind}:{base + index}", "provider": "tmdb", "media_type": kind,
            "title": "宿主公开作品 " + str(base + index), "provider_rating": 8, "release_year": [1980, 2000, 2020][index % 3],
            "genres": ["剧情"], "poster_url": "https://image.tmdb.org/t/p/w500/poster.jpg", "overview": "公开简介",
            "library_status": {"private_path": "PRIVATE-LIBRARY"}, "library_links": [{"secret": "PRIVATE-ACCOUNT"}]} for index in range(20)], "total_pages": 9}


class OverlappingPagedDiscovery(FakeDiscovery):
    """One catalog, not new IDs invented for every filter/page combination.

    Like a real discover endpoint, a genre page overlaps the global/era pages;
    different pages from the same query have stable non-overlapping contents.
    """
    def __init__(self):
        super().__init__()
        self.catalog = {}
        for kind, base in (("movie", 10000), ("tv", 20000)):
            self.catalog[kind] = [raw_row(kind, base + index, [1970, 2000, 2020][index // 60],
                ["US", "GB", "CN", "KR", "JP"][index % 5], 18 if index % 2 else 35,
                vote_average=9.4 - index % 12 / 10) for index in range(180)]

    async def _json(self, url, params, tmdb=False):
        self.calls.append((url, copy.deepcopy(params), tmdb))
        kind = url.rsplit("/", 1)[-1]
        date_key = "release_date" if kind == "movie" else "first_air_date"
        query_key = "primary_release_date" if kind == "movie" else "first_air_date"
        rows = [row for row in self.catalog[kind]
            if row[date_key] >= params.get(query_key + ".gte", "0000")
            and row[date_key] <= params.get(query_key + ".lte", "9999")
            and (not params.get("with_genres") or int(params["with_genres"]) in row["genre_ids"])
            and (not params.get("with_origin_country") or params["with_origin_country"] in row["origin_country"])]
        rows.sort(key=lambda row: (-row["vote_average"], row["id"]))
        start = (params["page"] - 1) * 20
        return {"results": copy.deepcopy(rows[start:start + 20]), "total_pages": max(1, math.ceil(len(rows) / 20))}


class PersonalTests(unittest.IsolatedAsyncioTestCase):
    async def test_behavior_feedback_changes_ranking_and_is_reversible(self):
        item = personal._public_item(raw_row("movie", 98765, genre=80), "movie")
        pool = {"items": [item]}
        profile = await self.service._profile(self.service._scope(self.token))
        baseline = self.service._rank(item, {}, personal.DEFAULT_ANSWERS, random.Random(2), set(), ({}, {}, {}))[0]
        await self.service.feedback(self.token, "save", item)
        saved_profile = await self.service._profile(self.service._scope(self.token))
        positive = personal._behavior(saved_profile, pool)
        self.assertAlmostEqual(positive[0]["genre:80"], 1)
        liked = self.service._rank(item, {}, personal.DEFAULT_ANSWERS, random.Random(2), set(), positive)[0]
        self.assertGreater(liked, baseline)
        await self.service.feedback(self.token, "hide", item)
        hidden_saved = await self.service._profile(self.service._scope(self.token))
        self.assertEqual(personal._behavior(hidden_saved, pool)[0], {}, "latest disinterest overrides saved positive evidence")
        await self.service.feedback(self.token, "remove", {"detail_ref": item["detail_ref"]})
        hidden_profile = await self.service._profile(self.service._scope(self.token))
        negative = personal._behavior(hidden_profile, {"items": []})
        self.assertAlmostEqual(negative[1]["genre:80"], 1)
        self.assertLess(self.service._rank(item, {}, personal.DEFAULT_ANSWERS, random.Random(2), set(), negative)[0], baseline)
        await self.service.feedback(self.token, "unhide", {"detail_ref": item["detail_ref"]})
        cleared = await self.service._profile(self.service._scope(self.token))
        self.assertEqual(personal._behavior(cleared, pool)[:2], ({}, {}))
        self.assertEqual(profile["answers"], cleared["answers"])

    async def test_subscription_learning_dedup_exclusion_restart_and_isolation(self):
        first = await self.service.recommend(self.token, "sub-before", limit=6)
        item = first["items"][0]
        before_answers = (await self.service.get_profile(self.token))["answers"]
        view = await self.service.sync_subscriptions(self.token, [item["detail_ref"], item["detail_ref"]])
        self.assertEqual(view["learning"]["subscriptions"], 1)
        self.assertEqual(view["learning"]["subscription_features"], 1)
        revision = view["revision"]
        again = await self.service.sync_subscriptions(self.token, [item["detail_ref"]])
        self.assertEqual(again["revision"], revision, "unchanged synchronization must not invalidate batches")
        after = await self.service.recommend(self.token, "sub-after", limit=6)
        self.assertNotIn(item["detail_ref"], [row["detail_ref"] for row in after["items"]])
        saved = await self.service.feedback(self.token, "save", after["items"][0])
        third = await self.service.recommend(self.token, "saved-after", limit=6)
        self.assertNotIn(saved["saved"][0]["item"]["detail_ref"], [row["detail_ref"] for row in third["items"]])
        other = await self.service.create_profile()
        self.assertEqual(other["profile"]["learning"]["subscriptions"], 0)
        restarted = personal.PersonalRecommendations(self.discovery, private_store=self.store)
        restored = await restarted.get_profile(self.token)
        self.assertEqual(restored["learning"]["subscriptions"], 1)
        self.assertEqual(restored["answers"], before_answers)
        await restarted.close()
        self.assertTrue(all(row["secret"] for row in self.store.writes))
        await self.service.sync_subscriptions(self.token, [])
        self.assertEqual((await self.service.get_profile(self.token))["learning"]["subscriptions"], 0)

    async def test_subscription_enrichment_projects_only_public_features_and_bounds(self):
        async def details(url, params, tmdb=False):
            return {"id": int(url.rsplit("/", 1)[-1]), "genres": [{"id": 80}], "origin_country": ["KR"],
                    "original_language": "ko", "first_air_date": "2020-01-01", "library_status": {"path": "PRIVATE-SECRET"}, "token": "PRIVATE-TOKEN"}
        self.discovery._json = details
        refs = ["tmdb:tv:" + str(i) for i in range(1, 21)]
        result = await self.service.sync_subscriptions(self.token, refs)
        self.assertEqual(result["learning"]["subscription_features"], 12)
        record = await self.service._profile(self.service._scope(self.token))
        self.assertNotIn("PRIVATE-", json.dumps(record))
        self.assertEqual(record["subscriptions"]["features"][refs[0]]["features"]["country:KR"], 1)
        with self.assertRaises(personal.PersonalProfileError):
            await self.service.sync_subscriptions(self.token, ["https://evil.example/"])
        with self.assertRaises(personal.PersonalProfileError):
            await self.service.sync_subscriptions(self.token, refs * 51)

    async def test_disable_behavior_keeps_explicit_exclusions_and_answers(self):
        first = await self.service.recommend(self.token, "learning-start", limit=6)
        item = first["items"][0]
        await self.service.feedback(self.token, "hide", item)
        view = await self.service.learning(self.token, False)
        profile = await self.service._profile(self.service._scope(self.token))
        self.assertFalse(view["learning"]["enabled"])
        self.assertEqual(personal._behavior(profile, {"items": []})[:2], ({}, {}))
        result = await self.service.recommend(self.token, "learning-off", limit=6)
        self.assertNotIn(item["detail_ref"], [row["detail_ref"] for row in result["items"]])
        with self.assertRaises(personal.PersonalProfileError):
            await self.service.learning(self.token, "false")

    async def test_immediate_previous_wall_is_skipped_when_alternatives_exist(self):
        discovery = OverlappingPagedDiscovery()
        service = personal.PersonalRecommendations(discovery, private_store=self.store)
        first = await service.recommend(self.token, "avoid-last", limit=6)
        scope = service._scope(self.token)
        profile = await service._profile(scope)
        # Exhaust the long window while leaving alternatives outside the last wall.
        pool = next(iter(service._pools.values()))
        last = [item["id"] for item in first["items"]]
        profile["recent"] = [[item["id"] for item in pool["items"]], last]
        await service._write(scope, "profile", profile)
        second = await service.recommend(self.token, "avoid-last-next", limit=6)
        self.assertEqual(len(second["items"]), 6)
        self.assertFalse(set(last).intersection(item["id"] for item in second["items"]))
        await service.close()

    async def test_private_shelf_persists_isolated_and_hidden_titles_stay_excluded(self):
        first = await self.service.recommend(self.token, "feedback", limit=6)
        item = first["items"][0]
        await self.service.feedback(self.token, "save", item)
        other = await self.service.create_profile()
        self.assertEqual((await self.service.shelf(other["profile_token"]))["saved"], [])
        restarted = personal.PersonalRecommendations(self.discovery, private_store=self.store)
        self.assertEqual((await restarted.shelf(self.token))["saved"][0]["item"]["detail_ref"], item["detail_ref"])
        await self.service.feedback(self.token, "hide", item)
        fresh = await self.service.recommend(self.token, "feedback", limit=6)
        self.assertNotIn(item["detail_ref"], [row["detail_ref"] for row in fresh["items"]])
        self.assertNotEqual(first["batch_id"], fresh["batch_id"])
        await self.service.feedback(self.token, "unhide", {"detail_ref": item["detail_ref"]})
        await self.service.feedback(self.token, "remove", {"detail_ref": item["detail_ref"]})
        self.assertEqual(await restarted.shelf(self.token), {"saved": [], "dismissed": [], "watched": [], "liked": []})
        self.assertTrue(all(row["secret"] for row in self.store.writes))
        with self.assertRaises(personal.PersonalProfileError):
            await self.service.feedback(self.token, "save", {"detail_ref": "tmdb:movie:1", "title": "unverified"})
        await restarted.close()

    async def test_wall_filters_are_hard_constraints_and_do_not_edit_taste(self):
        before = await self.service.get_profile(self.token)
        for kind, era in [("movie", "classic"), ("tv", "recent")]:
            result = await self.service.recommend(self.token, "same-filter-nonce", limit=6, media_type=kind, era=era)
            self.assertEqual(len(result["items"]), 6)
            self.assertTrue(all(item["media_type"] == kind for item in result["items"]))
            self.assertTrue(all(item["year"] < 2000 if era == "classic" else item["year"] >= 2010 for item in result["items"]))
        after = await self.service.get_profile(self.token)
        self.assertEqual(before, after)
        with self.assertRaises(personal.PersonalProfileError):
            await self.service.recommend(self.token, "bad", media_type="invalid")

    async def asyncSetUp(self):
        self.store, self.discovery = FakePrivateSDK(), FakeDiscovery()
        self.service = personal.PersonalRecommendations(self.discovery, private_store=self.store)
        created = await self.service.create_profile()
        self.token = created["profile_token"]

    async def asyncTearDown(self):
        await self.service.close()

    async def test_eight_meaningful_questions_and_answer_validation(self):
        schema = await self.service.questionnaire()
        self.assertEqual([question["id"] for question in schema["questions"]],
                         ["themes", "pace", "mood", "regions", "era", "format", "exploration", "avoid"])
        self.assertEqual(len(schema["questions"]), 8)
        for change in ({"extra": "bad"}, {"pace": "unknown"}, {"themes": ["human"] * 2}, {"themes": ["human", "mystery", "fun", "animation"]},
                       {"regions": ["global", "korean"]}, {"avoid": ["none", "horror"]}, {"themes": ["animation"], "avoid": ["animation"]}):
            answers = {**copy.deepcopy(personal.DEFAULT_ANSWERS), **change}
            with self.subTest(change=change), self.assertRaises(personal.PersonalProfileError):
                await self.service.submit(self.token, answers)
        with self.assertRaises(personal.PersonalProfileError):
            await self.service.submit(self.token, {})
        schema["questions"][0]["choices"].clear()
        self.assertTrue((await self.service.questionnaire())["questions"][0]["choices"])

    async def test_capabilities_isolate_profiles_and_all_sdk_values_are_secret(self):
        second = await self.service.create_profile()
        answers = {**copy.deepcopy(personal.DEFAULT_ANSWERS), "themes": ["fantasy"]}
        await self.service.submit(self.token, answers)
        self.assertTrue((await self.service.get_profile(self.token))["has_test"])
        self.assertFalse((await self.service.get_profile(second["profile_token"]))["has_test"])
        self.assertTrue(all(row["secret"] for row in self.store.writes))
        self.assertTrue(all(row["scope"].startswith("taste:") for row in self.store.writes))
        serialized = json.dumps({str(key): value for key, value in self.store.values.items()})
        self.assertNotIn(self.token, serialized)
        self.assertNotIn(second["profile_token"], serialized)
        self.assertNotIn("PRIVATE-TMDB-TOKEN", serialized)
        self.assertNotIn("cache:", serialized)
        for invalid in ("a" * 43, "", "../profile", None):
            with self.subTest(token=invalid), self.assertRaises(personal.PersonalProfileError):
                await self.service.get_profile(invalid)

    async def test_profile_creation_uses_host_valid_scope_and_survives_restart(self):
        expected = "taste:" + hashlib.sha256(self.token.encode()).hexdigest()[:32]
        self.assertEqual(self.service._scope(self.token), expected)
        self.assertTrue(FakePrivateSDK.SCOPE.match(expected))
        restarted = personal.PersonalRecommendations(self.discovery, private_store=self.store)
        self.assertFalse((await restarted.get_profile(self.token))["has_test"])
        await restarted.submit(self.token, copy.deepcopy(personal.DEFAULT_ANSWERS))
        self.assertEqual((await restarted.get_profile(self.token))["history_count"], 1)
        await restarted.close()

    async def test_fake_store_rejects_the_same_scope_key_and_value_limits_as_host(self):
        for scope, key in (("taste-v1:" + "a" * 32, "profile"), ("taste:" + "a" * 101, "profile"),
                           ("taste:ok", ""), ("taste:ok", "x" * 201)):
            for operation in ("get", "set", "delete"):
                with self.subTest(scope=scope, key=key, operation=operation), self.assertRaises(ValueError):
                    if operation == "set":
                        await self.store.set(key, {}, scope=scope, secret=True)
                    else:
                        await getattr(self.store, operation)(key, scope=scope)
        with self.assertRaises(ValueError):
            await self.store.set("large", "x" * (64 * 1024), scope="taste:ok", secret=True)

    async def test_retests_weight_current_more_and_retain_bounded_previous_history(self):
        previous = {**copy.deepcopy(personal.DEFAULT_ANSWERS), "themes": ["fantasy"]}
        current = {**copy.deepcopy(personal.DEFAULT_ANSWERS), "themes": ["mystery"]}
        await self.service.submit(self.token, previous)
        await self.service.submit(self.token, current)
        record = await self.service._profile(self.service._scope(self.token))
        self.assertAlmostEqual(record["vector"]["genre:9648"], .72)
        self.assertAlmostEqual(record["vector"]["genre:878"], .28)
        self.assertEqual(record["answers"]["themes"], ["mystery"])
        for _ in range(10):
            await self.service.submit(self.token, current)
        profile = await self.service.get_profile(self.token)
        self.assertEqual(profile["history_count"], 12)
        self.assertEqual(len(profile["history"]), personal.MAX_HISTORY)
        self.assertEqual(profile["answers"], current)
        self.assertNotIn("vector", profile)
        self.assertTrue(profile["summary"]["axes"])

    async def test_untested_exploration_has_48_real_posters_both_formats_and_three_eras(self):
        result = await self.service.recommend(self.token, "first")
        self.assertEqual(len(result["items"]), 48)
        self.assertEqual(len({item["id"] for item in result["items"]}), 48)
        self.assertFalse(result["personalization"]["tested"])
        self.assertIn("还没有测试", result["message"])
        self.assertEqual({item["media_type"] for item in result["items"]}, {"movie", "tv"})
        self.assertEqual({personal._era(item["year"]) for item in result["items"]}, {"classic", "middle", "modern"})
        for item in result["items"]:
            self.assertTrue(item["poster_url"].startswith("https://image.tmdb.org/"))
            self.assertGreaterEqual(item["rating"], personal.MIN_RATING)
            self.assertTrue(item["recommendation_reasons"])
            self.assertNotIn("match_score", item)
            self.assertIn("is_exploration", item["metadata"])
        self.assertLessEqual(len(self.discovery.calls), 14 + personal.EXPANSION_QUERIES)
        self.assertTrue(all(tmdb for _, _, tmdb in self.discovery.calls))
        dates = [value for _, params, _ in self.discovery.calls for key, value in params.items() if "date" in key]
        self.assertIn("1989-12-31", dates)
        self.assertNotIn("2026-01-01", dates)
        self.assertFalse(any("偏好" in reason or "题材相近" in reason for item in result["items"] for reason in item["recommendation_reasons"]))

    async def test_stable_nonce_survives_restart_and_retest_invalidates_batch(self):
        first = await self.service.recommend(self.token, "stable")
        count = len(self.discovery.calls)
        again = await self.service.recommend(self.token, "stable")
        self.assertEqual(first, again)
        restarted = personal.PersonalRecommendations(self.discovery, private_store=self.store)
        self.assertEqual(first, await restarted.recommend(self.token, "stable"))
        self.assertEqual(len(self.discovery.calls), count)
        answers = {**copy.deepcopy(personal.DEFAULT_ANSWERS), "themes": ["fantasy"]}
        await restarted.submit(self.token, answers)
        changed = await restarted.recommend(self.token, "stable")
        self.assertNotEqual(first["batch_id"], changed["batch_id"])
        self.assertTrue(changed["personalization"]["tested"])
        await restarted.close()

    async def test_multiple_batches_expand_candidate_pool_and_avoid_previous_when_possible(self):
        batches = []
        for index in range(5):
            result = await self.service.recommend(self.token, "batch" + str(index))
            ids = {item["id"] for item in result["items"]}
            self.assertEqual(len(ids), 48)
            self.assertFalse(ids.intersection(set().union(*batches)))
            self.assertFalse(result["personalization"]["repeated"])
            batches.append(ids)
        record = await self.service._profile(self.service._scope(self.token))
        self.assertEqual(len(record["recent"]), min(len(batches), personal.RECENT_BATCHES))
        self.assertEqual(len(record["batches"]), personal.MAX_BATCHES)
        self.assertGreater(len(self.discovery.calls), 8)
        metas = [key for scope, key in self.store.values if key.startswith("m:batch:")]
        self.assertEqual(len(metas), personal.MAX_BATCHES)

    async def test_realistic_overlapping_filters_and_pages_give_seven_fresh_walls(self):
        discovery = OverlappingPagedDiscovery()
        service = personal.PersonalRecommendations(discovery, private_store=self.store)
        batches = []
        for index in range(7):
            result = await service.recommend(self.token, "overlap" + str(index), limit=48)
            ids = {item["id"] for item in result["items"]}
            recent = set().union(*batches[-personal.RECENT_BATCHES:])
            self.assertEqual(len(ids), 48)
            self.assertFalse(ids.intersection(recent))
            self.assertEqual(result["freshness"]["new_count"], 48)
            self.assertEqual(result["freshness"]["repeated_count"], 0)
            self.assertFalse(result["freshness"]["exhausted"])
            self.assertTrue(all(item["metadata"]["is_new_to_recent"] for item in result["items"]))
            if index == 0:
                self.assertTrue(result["freshness"]["expanded"])
                self.assertGreaterEqual(result["freshness"]["pool_count"], 48 * 7)
            batches.append(ids)
        queries = [(url, json.dumps(params, sort_keys=True)) for url, params, _ in discovery.calls]
        self.assertEqual(len(queries), len(set(queries)))
        self.assertGreater(len(discovery.calls), 8 + 6)
        self.assertTrue(all(params["vote_average.gte"] == 6.5 for _, params, _ in discovery.calls))
        await service.close()

    async def test_old_era_seeds_and_format_preferences_cannot_displace_unseen_candidates(self):
        old = [personal._public_item(raw_row("movie", 100 + index, 1970, genre=18, vote_average=9.9), "movie") for index in range(24)]
        fresh = [personal._public_item(raw_row("tv", 1000 + index, 2020, genre=35, vote_average=6.5, vote_count=50), "tv") for index in range(24)]
        answers = {**copy.deepcopy(personal.DEFAULT_ANSWERS), "format": "movie", "era": "classic", "themes": ["human"], "pace": "quiet"}
        profile = {"answers": answers, "vector": personal._vector(answers), "history": [{"vector": personal._vector(answers)}],
                   "revision": "stable", "recent": [[item["id"] for item in old]]}
        selected = self.service._select({"items": old + fresh}, profile, "hard-exclude", 12)
        self.assertEqual(len(selected), 12)
        self.assertTrue(all(item["id"] in {value["id"] for value in fresh} for item in selected))
        self.assertTrue(all(item["metadata"]["is_new_to_recent"] for item in selected))

    async def test_exhausted_small_pool_uses_every_unseen_before_older_repeats(self):
        items = [personal._public_item(raw_row("movie", index + 1, 1980), "movie") for index in range(16)]
        old_ids = [item["id"] for item in items[:12]]
        profile = {"answers": {}, "vector": {}, "history": [], "revision": "small", "recent": [old_ids[:6], old_ids[6:]]}
        result = self.service._select({"items": items}, profile, "all-fresh-first", 10)
        selected_ids = {item["id"] for item in result}
        self.assertTrue({item["id"] for item in items[12:]}.issubset(selected_ids))
        self.assertTrue(set(old_ids[:6]).issubset(selected_ids))
        self.assertEqual(sum(item["metadata"]["is_new_to_recent"] for item in result), 4)

    async def test_viewport_limits_and_individual_freshness_flags_are_exact(self):
        for limit in (3, 6, 9, 12, 44, 96):
            result = await self.service.recommend(self.token, "viewport" + str(limit), limit=limit)
            self.assertEqual(len(result["items"]), limit)
            self.assertEqual(len({item["id"] for item in result["items"]}), limit)
            new_count = sum(item["metadata"]["is_new_to_recent"] for item in result["items"])
            self.assertEqual(new_count, result["freshness"]["new_count"])
            self.assertEqual(limit - new_count, result["freshness"]["repeated_count"])
            self.assertEqual(result["freshness"]["window_batches"], personal.RECENT_BATCHES)
            self.assertLessEqual(result["freshness"]["pool_count"], personal.MAX_POOL_ITEMS)

    async def test_slow_expansion_has_a_total_deadline_and_keeps_real_fresh_candidates(self):
        original_query = self.discovery._json
        async def slow_next_page(url, params, tmdb=False):
            if params["page"] > 1:
                self.discovery.calls.append((url, copy.deepcopy(params), tmdb))
                await asyncio.Event().wait()
            return await original_query(url, params, tmdb)
        self.discovery._json = slow_next_page
        started = time.monotonic()
        with patch.object(personal, "EXPANSION_SECONDS", .02):
            result = await asyncio.wait_for(self.service.recommend(self.token, "slow-pages"), 1)
        self.assertLess(time.monotonic() - started, 1)
        self.assertEqual(len(result["items"]), 48)
        self.assertEqual(result["freshness"]["new_count"], 48)
        self.assertEqual(result["freshness"]["repeated_count"], 0)
        self.assertTrue(result["freshness"]["expanded"])
        self.assertEqual(result["status"], "stale")
        self.assertLessEqual(len(self.discovery.calls), 8 + 6)

    async def test_format_preference_has_expected_ratio_and_region_targeting(self):
        answers = {**copy.deepcopy(personal.DEFAULT_ANSWERS), "format": "movie", "regions": ["korean"], "themes": ["mystery"]}
        await self.service.submit(self.token, answers)
        result = await self.service.recommend(self.token, "region")
        self.assertEqual(sum(item["media_type"] == "movie" for item in result["items"]), 36)
        self.assertTrue(any(params.get("with_origin_country") == "KR" for _, params, _ in self.discovery.calls))
        self.assertTrue(any("偏好" in reason for item in result["items"] for reason in item["recommendation_reasons"]))

    async def test_explicit_avoid_uses_verified_genre_tags(self):
        answers = {**copy.deepcopy(personal.DEFAULT_ANSWERS), "avoid": ["animation"]}
        await self.service.submit(self.token, answers)
        result = await self.service.recommend(self.token, "skip-animation")
        self.assertTrue(result["items"])
        self.assertFalse(any(16 in item["metadata"]["genre_ids"] for item in result["items"]))

    async def test_missing_posters_and_total_failure_return_honest_empty_without_fixture(self):
        self.discovery.no_posters = True
        result = await self.service.recommend(self.token, "no-posters")
        self.assertEqual(result["items"], [])
        self.assertIn("暂不凑满", result["message"])
        self.discovery.fail = True
        self.service._pools.clear()
        result = await self.service.recommend(self.token, "no-network")
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["items"], [])
        self.assertNotIn("PRIVATE", json.dumps(result))

    async def test_quality_validation_filters_low_votes_bad_posters_and_private_fields(self):
        for change in ({"vote_average": 6.4}, {"vote_count": 99}, {"poster_path": None}, {"adult": True}, {"genre_ids": [10764]}, {"genre_ids": [{"id": 18}]}):
            with self.subTest(change=change):
                self.assertIsNone(personal._public_item(raw_row("movie", 1, **change), "movie"))
        movie = raw_row("movie", 2, library_status={"path": "PRIVATE"}, library_links=["PRIVATE"])
        movie.pop("origin_country")
        item = personal._public_item(movie, "movie", "KR")
        self.assertEqual(item["metadata"]["origin_country"], ["KR"])
        self.assertNotIn("PRIVATE", json.dumps(item))

    async def test_host_fallback_uses_readonly_ops_and_projects_public_fields(self):
        ops = FakeOps()
        async def get_ops():
            return ops
        service = personal.PersonalRecommendations(FakeDiscovery(token=""), get_ops, self.store)
        result = await service.recommend(self.token, "host")
        self.assertEqual(len(result["items"]), 48)
        self.assertTrue(all(operation == "discover.filter-titles" for operation, _ in ops.calls))
        self.assertTrue(all(params["rating_gte"] == 6.5 and params["sort"] == "rating" for _, params in ops.calls))
        self.assertNotIn("PRIVATE", json.dumps(result))
        self.assertTrue(all(item["metadata"]["vote_count"] is None for item in result["items"]))
        self.assertIn("字段有限", result["message"])
        await service.close()

    async def test_direct_failure_falls_back_to_host_without_repeating_failed_network(self):
        ops = FakeOps()
        async def get_ops():
            return ops
        self.discovery.fail = True
        service = personal.PersonalRecommendations(self.discovery, get_ops, self.store)
        first = await service.recommend(self.token, "fallback1")
        direct_calls = len(self.discovery.calls)
        self.assertEqual(len(first["items"]), 48)
        self.assertIn("宿主公开剧库", first["message"])
        self.assertNotIn("PRIVATE", json.dumps(first))
        await service.recommend(self.token, "fallback2")
        self.assertEqual(len(self.discovery.calls), direct_calls)
        await service.close()

    async def test_current_taste_ranks_above_previous_taste_with_same_quality(self):
        old = personal._vector({**copy.deepcopy(personal.DEFAULT_ANSWERS), "themes": ["fantasy"]})
        answers = {**copy.deepcopy(personal.DEFAULT_ANSWERS), "themes": ["mystery"]}
        vector = personal._combined(personal._vector(answers), [{"vector": old}])
        mystery = personal._public_item(raw_row("movie", 1, genre=9648), "movie")
        fantasy = personal._public_item(raw_row("movie", 2, genre=878), "movie")
        mystery_score = self.service._rank(mystery, vector, answers, random.Random(1), set())[0]
        fantasy_score = self.service._rank(fantasy, vector, answers, random.Random(1), set())[0]
        self.assertGreater(mystery_score, fantasy_score)
        self.assertGreater(vector["genre:878"], 0)

    async def test_small_pool_cycles_honestly_without_fabricating_more_posters(self):
        async def small_query(url, params, tmdb=False):
            kind = url.rsplit("/", 1)[-1]
            base = 1000 if kind == "movie" else 2000
            return {"results": [raw_row(kind, base + index, [1980, 2000, 2020][index % 3]) for index in range(20)], "total_pages": 1}
        self.discovery._json = small_query
        first = await self.service.recommend(self.token, "small1")
        second = await self.service.recommend(self.token, "small2")
        self.assertEqual(len(first["items"]), 40)
        self.assertEqual(len(second["items"]), 40)
        self.assertEqual(second["status"], "ready")
        self.assertTrue(second["personalization"]["repeated"])
        self.assertEqual(second["freshness"]["new_count"], 0)
        self.assertEqual(second["freshness"]["repeated_count"], 40)
        self.assertTrue(second["freshness"]["exhausted"])
        self.assertIn("暂不凑满", second["message"])
        self.assertIn("循环补充", second["message"])

    async def test_expired_public_candidates_survive_source_failure_as_stale(self):
        await self.service.recommend(self.token, "warm")
        for pool in self.service._pools.values():
            pool["expires"] = time.monotonic() - 1
        self.discovery.fail = True
        result = await self.service.recommend(self.token, "stale")
        self.assertEqual(len(result["items"]), 48)
        self.assertEqual(result["status"], "stale")
        self.assertIn("上次成功", result["message"])
        self.assertNotIn("PRIVATE", json.dumps(result))

    async def test_unknown_capabilities_do_not_accumulate_locks(self):
        for index in range(80):
            with self.assertRaises(personal.PersonalProfileError) as error:
                await self.service.get_profile(str(index).rjust(43, "x"))
            self.assertEqual(error.exception.status, 404)
        gc.collect()
        self.assertEqual(len(self.service._locks), 0)

    async def test_host_nonfiction_and_conflicting_types_are_not_movie_drama(self):
        base = {"title_ref": "tmdb:tv:42", "provider": "tmdb", "media_type": "tv", "title": "公开作品",
                "provider_rating": 8, "release_year": 2000, "poster_url": "https://image.tmdb.org/poster.jpg"}
        for genres in (["Reality"], ["纪录"], ["News"], ["Talk"], [{"id": 18}]):
            self.assertIsNone(personal._public_item({**base, "genres": genres}, "tv", host=True))
        self.assertIsNone(personal._public_item({**base, "media_type": "movie", "genres": ["Drama"]}, "tv", host=True))
        item = personal._public_item({**base, "genres": ["Horror"]}, "tv", host=True)
        self.assertEqual(item["metadata"]["genre_ids"], [27])

    async def test_concurrent_same_profile_nonce_is_single_batch(self):
        results = await asyncio.gather(*(self.service.recommend(self.token, "parallel") for _ in range(5)))
        self.assertTrue(all(result == results[0] for result in results))
        record = await self.service._profile(self.service._scope(self.token))
        self.assertEqual(len(record["recent"]), 1)

    async def test_private_storage_failures_are_safe_and_saved_history_survives_close(self):
        answers = copy.deepcopy(personal.DEFAULT_ANSWERS)
        await self.service.submit(self.token, answers)
        self.store.fail = True
        with self.assertRaises(personal.PersonalProfileError) as error:
            await self.service.get_profile(self.token)
        self.assertNotIn("PRIVATE", str(error.exception))
        self.assertEqual(error.exception.status, 503)
        self.store.fail = False
        await self.service.close()
        restarted = personal.PersonalRecommendations(self.discovery, private_store=self.store)
        self.assertEqual((await restarted.get_profile(self.token))["answers"], answers)
        await restarted.close()

    async def test_public_candidates_persist_without_any_profile_or_private_annotations(self):
        storage = FakePublicStorage()
        service = personal.PersonalRecommendations(self.discovery, private_store=self.store, storage=storage)
        answers = {**copy.deepcopy(personal.DEFAULT_ANSWERS), "regions": ["korean"], "themes": ["mystery"]}
        await service.submit(self.token, answers)
        result = await service.recommend(self.token, "persist-public", limit=12)
        self.assertEqual(len(result["items"]), 12)
        self.assertEqual(set(storage.values), {personal.PUBLIC_CATALOG_KEY})
        record = storage.values[personal.PUBLIC_CATALOG_KEY]
        self.assertGreater(len(record["items"]), 0)
        self.assertLessEqual(len(record["items"]), personal.MAX_POOL_ITEMS)
        serialized = json.dumps(record)
        for forbidden in (self.token, "PRIVATE", "answers", "history", "profile", "revision", "recommendation_reasons", "is_new_to_recent", "nonce", "vector"):
            self.assertNotIn(forbidden, serialized)
        self.assertTrue(all(set(item["metadata"]) == {"genre_ids", "genres", "origin_country", "original_language", "vote_count", "minimum_votes", "language_verified"} for item in record["items"]))
        await service.close()

    async def test_no_plugin_token_can_persist_verified_host_public_candidates_and_restore_them(self):
        ops, storage = FakeOps(), FakePublicStorage()
        async def get_ops():
            return ops
        service = personal.PersonalRecommendations(FakeDiscovery(token=""), get_ops, self.store, storage)
        # Cold storage and a profile without any successful wall must still work online.
        online = await service.recommend(self.token, "host-cold", limit=12)
        self.assertEqual(online["status"], "ready")
        self.assertEqual(len(online["items"]), 12)
        record = storage.values[personal.PUBLIC_CATALOG_KEY]
        self.assertEqual(record["backend"], "host")
        self.assertTrue(record["items"])
        self.assertTrue(all(item["metadata"]["minimum_votes"] == 50 for item in record["items"]))
        self.assertTrue(all(item["metadata"]["vote_count"] is None and not item["metadata"]["language_verified"] for item in record["items"]))
        self.assertTrue(all(params["sort"] == "rating" and params["rating_gte"] == 6.5 for _, params in ops.calls))
        await service.close()
        ops.fail = True
        restored = personal.PersonalRecommendations(FakeDiscovery(token=""), get_ops, self.store, storage)
        other = await restored.create_profile()
        offline = await asyncio.wait_for(restored.recommend(other["profile_token"], "host-reload", limit=12), .3)
        self.assertEqual(offline["status"], "stale")
        self.assertEqual(len(offline["items"]), 12)
        self.assertFalse(offline["cache"]["private_batch"])
        self.assertIn("宿主公开剧库", offline["message"])
        await restored.close()

    async def test_restart_with_public_pool_returns_immediate_stale_while_refresh_waits(self):
        storage = FakePublicStorage()
        source = personal.PersonalRecommendations(self.discovery, private_store=self.store, storage=storage)
        await source.recommend(self.token, "warm-public", limit=12)
        await source.close()
        entered, release = asyncio.Event(), asyncio.Event()
        async def blocked_query(url, params, tmdb=False):
            entered.set()
            await release.wait()
            raise RuntimeError("PRIVATE custom proxy unreachable")
        failed = FakeDiscovery()
        failed._json = blocked_query
        restarted = personal.PersonalRecommendations(failed, private_store=self.store, storage=storage)
        await restarted.initialize()
        result = await asyncio.wait_for(restarted.recommend(self.token, "after-reload", limit=12), .3)
        self.assertEqual(len(result["items"]), 12)
        self.assertEqual(result["status"], "stale")
        self.assertTrue(result["cache"]["restored"])
        self.assertFalse(result["cache"]["private_batch"])
        self.assertTrue(result["cache"]["refreshing"])
        self.assertIn("持久保存的公开候选", result["message"])
        await asyncio.wait_for(entered.wait(), .3)
        release.set()
        await restarted._public_task
        self.assertEqual(len((await restarted.recommend(self.token, "after-outage", limit=12))["items"]), 12)
        self.assertNotIn("PRIVATE", json.dumps(result))
        await restarted.close()

    async def test_only_same_verified_capability_can_recover_own_successful_batch_after_restart(self):
        # Simulate old releases: no shared public pool was ever persisted.
        first = await self.service.recommend(self.token, "legacy-success", limit=12)
        scope = self.service._scope(self.token)
        record = await self.service._profile(scope)
        record.pop("last_success_batch", None)
        await self.service._write(scope, "profile", record)
        await self.service.close()
        failed = FakeDiscovery()
        failed.fail = True
        storage = FakePublicStorage()
        restarted = personal.PersonalRecommendations(failed, private_store=self.store, storage=storage)
        recovered = await asyncio.wait_for(restarted.recommend(self.token, "new-after-failure", limit=12), .3)
        self.assertEqual(recovered["status"], "stale")
        self.assertEqual({item["id"] for item in first["items"]}, {item["id"] for item in recovered["items"]})
        self.assertTrue(recovered["cache"]["private_batch"])
        self.assertIn("此私人口味档案", recovered["message"])
        if restarted._public_task:
            await restarted._public_task
        self.assertFalse(storage.values)  # Never promote private batch membership into shared cache.
        other = await restarted.create_profile()
        result = await restarted.recommend(other["profile_token"], "other-profile", limit=12)
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["items"], [])
        self.assertIn("代理或服务器网络", result["message"])
        self.assertIn("不表示作品已下线", result["message"])
        with self.assertRaises(personal.PersonalProfileError) as error:
            await restarted.recommend("x" * 43, "forged-token", limit=12)
        self.assertEqual(error.exception.status, 404)
        await restarted.close()

    async def test_offline_shuffles_keep_original_success_even_when_three_batch_ring_rolls(self):
        original = await self.service.recommend(self.token, "last-real-success", limit=9)
        success_key = "batch:" + original["batch_id"]
        await self.service.close()
        failed = FakeDiscovery()
        failed.fail = True
        service = personal.PersonalRecommendations(failed, private_store=self.store)
        for index in range(6):
            result = await service.recommend(self.token, "offline" + str(index), limit=9)
            self.assertEqual(result["status"], "stale")
            self.assertEqual(len(result["items"]), 9)
        profile = await service._profile(service._scope(self.token))
        self.assertEqual(profile["last_success_batch"], success_key)
        self.assertNotIn(success_key, profile["batches"])
        self.assertEqual((await service._read(service._scope(self.token), success_key))["status"], "ready")
        self.assertLessEqual(sum(key.startswith("m:batch:") for scope, key in self.store.values if scope == service._scope(self.token)), personal.MAX_BATCHES + 1)
        await service.close()

    async def test_public_cache_failures_and_corruption_do_not_hide_private_success(self):
        await self.service.recommend(self.token, "own-success", limit=6)
        await self.service.close()
        failed = FakeDiscovery()
        failed.fail = True
        storage = FakePublicStorage()
        storage.fail = True
        service = personal.PersonalRecommendations(failed, private_store=self.store, storage=storage)
        result = await service.recommend(self.token, "storage-failure", limit=6)
        self.assertEqual(len(result["items"]), 6)
        self.assertEqual(result["status"], "stale")
        self.assertNotIn("PRIVATE", result["message"])
        await service.close()

    async def test_public_catalog_projection_rejects_bad_quality_and_private_extras(self):
        item = personal._public_item(raw_row("movie", 1), "movie")
        item["answers"] = {"secret": "PRIVATE"}
        item["recommendation_reasons"] = ["PRIVATE preference"]
        item["metadata"]["private_path"] = "PRIVATE library"
        cleaned = self.service._catalog_item(item)
        self.assertNotIn("PRIVATE", json.dumps(cleaned))
        for changed in ({"rating": 6.4}, {"poster_url": "http://bad.invalid/poster"}, {"detail_ref": "untrusted:42"}):
            self.assertIsNone(self.service._catalog_item({**item, **changed}))
        host = copy.deepcopy(item)
        host["metadata"].update(language_verified=False, minimum_votes=None)
        self.assertIsNone(self.service._catalog_item(host))

    async def test_public_refresh_is_shared_and_recovers_new_live_candidates(self):
        storage = FakePublicStorage()
        warm = personal.PersonalRecommendations(self.discovery, private_store=self.store, storage=storage)
        await warm.recommend(self.token, "seed", limit=6)
        await warm.close()
        fresh = FakeDiscovery()
        service = personal.PersonalRecommendations(fresh, private_store=self.store, storage=storage)
        other = await service.create_profile()
        one, two = await asyncio.gather(service.recommend(self.token, "refresh-one", limit=6), service.recommend(other["profile_token"], "refresh-two", limit=6))
        self.assertEqual(one["status"], "stale")
        self.assertEqual(two["status"], "stale")
        refresh_task = service._public_task
        self.assertIsNotNone(refresh_task)
        await refresh_task
        result = await service.recommend(self.token, "live-again", limit=6)
        self.assertEqual(result["status"], "ready")
        self.assertEqual(len(result["items"]), 6)
        self.assertFalse(result["cache"]["restored"])
        # One refresh queried its eight fixed public lanes, not one per profile.
        self.assertEqual(len(fresh.calls), 8)
        await service.close()

    async def test_manifest_committed_then_rpc_failed_remains_readable(self):
        original_set = self.store.set
        failed = False
        async def uncertain_set(key, value, *, scope, secret=False):
            nonlocal failed
            await original_set(key, value, scope=scope, secret=secret)
            if key == "m:profile" and not failed:
                failed = True
                raise RuntimeError("PRIVATE remote commit succeeded but reply lost")
        self.store.set = uncertain_set
        result = await self.service.submit(self.token, copy.deepcopy(personal.DEFAULT_ANSWERS))
        self.assertEqual(result["history_count"], 1)
        self.assertEqual((await self.service.get_profile(self.token))["history_count"], 1)

    async def test_unknown_manifest_outcome_never_deletes_potentially_live_chunks(self):
        original_set, original_get = self.store.set, self.store.get
        outcome_unknown = False
        async def uncertain_set(key, value, *, scope, secret=False):
            nonlocal outcome_unknown
            await original_set(key, value, scope=scope, secret=secret)
            if key == "m:profile":
                outcome_unknown = True
                raise RuntimeError("PRIVATE reply lost")
        async def uncertain_get(key, *, scope, default=None):
            if outcome_unknown:
                raise RuntimeError("PRIVATE cannot confirm metadata")
            return await original_get(key, scope=scope, default=default)
        self.store.set, self.store.get = uncertain_set, uncertain_get
        with self.assertRaises(personal.PersonalProfileError) as error:
            await self.service.submit(self.token, copy.deepcopy(personal.DEFAULT_ANSWERS))
        self.assertEqual(error.exception.status, 503)
        self.store.set, self.store.get = original_set, original_get
        recovered = await self.service.get_profile(self.token)
        self.assertEqual(recovered["history_count"], 1)

    async def test_failed_manifest_before_commit_keeps_old_profile(self):
        original_set = self.store.set
        async def failed_set(key, value, *, scope, secret=False):
            if key == "m:profile":
                raise RuntimeError("PRIVATE metadata write rejected")
            await original_set(key, value, scope=scope, secret=secret)
        self.store.set = failed_set
        original_chunks = {key for key in self.store.values if key[1].startswith("b:profile:")}
        with self.assertRaises(personal.PersonalProfileError):
            await self.service.submit(self.token, copy.deepcopy(personal.DEFAULT_ANSWERS))
        self.assertFalse((await self.service.get_profile(self.token))["has_test"])
        self.assertEqual({key for key in self.store.values if key[1].startswith("b:profile:")}, original_chunks)

    async def test_read_retries_if_other_process_replaced_manifest_and_deleted_old_chunks(self):
        old_values = copy.deepcopy(self.store.values)
        submitted = await self.service.submit(self.token, copy.deepcopy(personal.DEFAULT_ANSWERS))
        new_values = copy.deepcopy(self.store.values)
        self.store.values = old_values
        original_get = self.store.get
        switched = False
        async def racing_get(key, *, scope, default=None):
            nonlocal switched
            if key.startswith("b:profile:") and not switched:
                switched = True
                self.store.values = new_values
                return None
            return await original_get(key, scope=scope, default=default)
        self.store.get = racing_get
        self.assertEqual((await self.service.get_profile(self.token))["revision"], submitted["revision"])

    async def test_get_profile_waits_for_inflight_same_profile_write(self):
        original_set = self.store.set
        entered, release = asyncio.Event(), asyncio.Event()
        async def waiting_set(key, value, *, scope, secret=False):
            if key == "m:profile":
                entered.set()
                await release.wait()
            await original_set(key, value, scope=scope, secret=secret)
        self.store.set = waiting_set
        submit_task = asyncio.create_task(self.service.submit(self.token, copy.deepcopy(personal.DEFAULT_ANSWERS)))
        await entered.wait()
        read_task = asyncio.create_task(self.service.get_profile(self.token))
        await asyncio.sleep(0)
        self.assertFalse(read_task.done())
        release.set()
        saved, read = await asyncio.gather(submit_task, read_task)
        self.assertEqual(saved["revision"], read["revision"])

    async def test_large_private_records_are_encrypted_chunked_and_round_trip(self):
        scope = self.service._scope(self.token)
        record = {"payload": random.Random(8).randbytes(180000).hex()}
        await self.service._write(scope, "large", record)
        self.assertEqual(await self.service._read(scope, "large"), record)
        self.assertGreater(sum(key.startswith("b:large:") for _, key in self.store.values), 1)
        self.assertTrue(all(row["secret"] for row in self.store.writes))

    async def test_invalid_limits_and_nonce_are_rejected(self):
        for limit, nonce in ((2, "x"), (97, "x"), (48, ""), (48, "x" * 97), (True, "x")):
            with self.subTest(limit=limit, nonce=nonce), self.assertRaises(personal.PersonalProfileError):
                await self.service.recommend(self.token, nonce, limit)


if __name__ == "__main__":
    unittest.main()
