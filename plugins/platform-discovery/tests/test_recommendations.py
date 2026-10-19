"""真实剧库抽样、地区归类与成员数据边界；不请求外部接口。"""

import asyncio
import copy
import importlib.util
import json
import time
import unittest
from pathlib import Path

module_path = Path(__file__).parents[1] / "platform_discovery" / "recommendations.py"
spec = importlib.util.spec_from_file_location("platform_discovery_recommendation_tests", module_path)
recommendations = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recommendations)

COUNTRY_BASE = {"KR": 100000, "CN": 200000, "TW": 300000, "HK": 400000, "JP": 500000, "US": 600000, "GB": 700000, "TH": 800000}


def direct_row(country="KR", language="ko", identity=1, **extra):
    return {"id": identity, "name": "经典剧 " + str(identity), "original_name": "Original " + str(identity),
            "origin_country": [country], "original_language": language, "genre_ids": [18],
            "vote_average": 8.0, "vote_count": 300, "first_air_date": "1985-01-01",
            "poster_path": "/poster.jpg", "backdrop_path": "/backdrop.jpg", "overview": "公开简介", **extra}


class FakeDiscovery:
    def __init__(self, token="test-token"):
        self._token, self.ttl, self.calls, self.fail = token, 3600, [], False

    async def _json(self, url, params, tmdb=False):
        self.calls.append((url, copy.deepcopy(params), tmdb))
        if self.fail:
            raise ValueError("proxy=http://user:PRIVATE-PASSWORD@proxy.invalid:7890 token=PRIVATE-TOKEN")
        country, language, page = params["with_origin_country"], params["with_original_language"], params["page"]
        base = COUNTRY_BASE[country] + page * 100 + (40 if language == "cn" else 0)
        rows = [direct_row(country, language, base + index) for index in range(20)]
        return {"results": rows, "total_pages": 8}


class FakeOps:
    def __init__(self):
        self.calls, self.fail = [], False

    async def call(self, operation, arguments):
        self.calls.append((operation, copy.deepcopy(arguments)))
        if self.fail:
            raise RuntimeError("credential=PRIVATE-HOST-SECRET")
        country, page = arguments["origin_country"], arguments["page"]
        rows = [{"title_ref": "tmdb:tv:" + str(COUNTRY_BASE[country] + page * 100 + index), "provider": "tmdb", "media_type": "tv",
                 "title": "宿主经典剧 " + str(index), "provider_rating": 8.0, "release_year": 1970,
                 "genres": ["剧情"], "poster_url": "https://image.tmdb.org/t/p/w500/poster.jpg",
                 "overview": "公开剧情", "library_status": {"library_id": 42, "path": "PRIVATE-LIBRARY-PATH"},
                 "private_account": "PRIVATE-ACCOUNT"} for index in range(20)]
        return {"titles": rows, "total_pages": 6}


class FakeStorage:
    def __init__(self):
        self.values = {}

    async def get(self, key):
        return copy.deepcopy(self.values.get(key))

    async def set(self, key, value, ttl_seconds):
        self.values[key] = copy.deepcopy(value)


class RecommendationTests(unittest.IsolatedAsyncioTestCase):
    async def test_categories_cover_six_regions_and_default_korean(self):
        source = recommendations.RecommendationSources(FakeDiscovery())
        catalog = await source.categories()
        self.assertEqual(catalog["default_category"], "korean")
        self.assertEqual([item["id"] for item in catalog["categories"]], ["korean", "chinese", "japanese", "american", "british", "thai"])
        self.assertEqual(catalog["categories"][1]["countries"], ["CN", "TW", "HK"])
        catalog["categories"][0]["countries"].append("US")
        self.assertEqual((await source.categories())["categories"][0]["countries"], ["KR"])
        await source.close()

    async def test_direct_query_uses_country_language_votes_and_no_year_limit(self):
        discovery = FakeDiscovery()
        source = recommendations.RecommendationSources(discovery)
        result = await source.recommend("korean", nonce="0")
        self.assertEqual(result["status"], "ready")
        self.assertEqual(len(result["items"]), 12)
        self.assertTrue(all(item["year"] == 1985 for item in result["items"]))
        self.assertEqual(result["source"]["kind"], "random_library")
        for url, params, token in discovery.calls:
            self.assertEqual(url, recommendations.TMDB_DISCOVER)
            self.assertTrue(token)
            self.assertEqual(params["with_origin_country"], "KR")
            self.assertEqual(params["with_original_language"], "ko")
            self.assertGreaterEqual(params["vote_count.gte"], 20)
            self.assertEqual(set(params["without_genres"].split(",")), {"16", "99", "10763", "10764", "10767"})
            self.assertFalse(any("date" in name or "year" in name for name in params))
            self.assertLessEqual(params["page"], 500)
        await source.close()

    async def test_direct_items_reject_wrong_region_language_nonfiction_and_low_votes(self):
        for change in ({"origin_country": ["JP"]}, {"original_language": "en"}, {"genre_ids": [18, 10764]},
                       {"genre_ids": [18, 10767]}, {"genre_ids": [16, 18]}, {"genre_ids": [99]},
                       {"genre_ids": [10763]}, {"vote_count": 1}, {"vote_average": 5.9}, {"adult": True},
                       {"genre_ids": [{"id": 18}]}, {"origin_country": "KR"}, {"name": ""}):
            with self.subTest(change=change):
                self.assertIsNone(recommendations._direct_item(direct_row(**change), "KR", ["ko"]))
        item = recommendations._direct_item(direct_row(), "KR", ["ko"])
        self.assertEqual(item["detail_ref"], "tmdb:tv:1")
        self.assertEqual(item["media_type"], "tv")

    async def test_nonce_is_stable_new_batch_avoids_previous_and_uses_cache(self):
        discovery = FakeDiscovery()
        source = recommendations.RecommendationSources(discovery)
        first = await source.recommend("korean", nonce="0")
        network_calls = len(discovery.calls)
        next_batch = await source.recommend("korean", nonce="1")
        again = await source.recommend("korean", nonce="0")
        self.assertEqual(first["items"], again["items"])
        self.assertFalse({item["id"] for item in first["items"]} & {item["id"] for item in next_batch["items"]})
        self.assertEqual(len(discovery.calls), network_calls)
        first["items"][0]["title"] = "不应修改缓存"
        self.assertNotEqual((await source.recommend("korean", nonce="0"))["items"][0]["title"], "不应修改缓存")
        await source.close()

    async def test_chinese_regions_are_separate_queries_and_balanced(self):
        discovery = FakeDiscovery()
        source = recommendations.RecommendationSources(discovery)
        result = await source.recommend("chinese", nonce="0")
        counts = {country: sum(country in item["metadata"]["origin_country"] for item in result["items"]) for country in ("CN", "TW", "HK")}
        self.assertEqual(counts, {"CN": 4, "TW": 4, "HK": 4})
        self.assertEqual({params["with_origin_country"] for _, params, _ in discovery.calls}, {"CN", "TW", "HK"})
        self.assertEqual({params["with_original_language"] for _, params, _ in discovery.calls}, {"zh", "cn"})
        await source.close()

    async def test_multiple_batches_refill_pool_without_recent_repeats(self):
        discovery = FakeDiscovery()
        source = recommendations.RecommendationSources(discovery)
        batches = []
        for index in range(7):
            result = await source.recommend("korean", nonce=str(index), session_id="browser-a")
            identities = {item["id"] for item in result["items"]}
            self.assertEqual(len(identities), 12)
            self.assertFalse(identities.intersection(set().union(*batches[-5:])))
            self.assertFalse(result["selection"]["repeated"])
            batches.append(identities)
            if index == 0:
                self.assertEqual(len(discovery.calls), 2)
            if index == 2:
                self.assertEqual(len(discovery.calls), 2)
        self.assertGreater(len(discovery.calls), 2)
        pages = [params["page"] for _, params, _ in discovery.calls]
        self.assertEqual(len(pages), len(set(pages)))
        self.assertEqual(len(source._sessions["browser-a"]["categories"]["korean"]["history"]), 5)
        await source.close()

    async def test_browser_sessions_do_not_consume_each_others_history(self):
        source = recommendations.RecommendationSources(FakeDiscovery())
        a_first = await source.recommend("korean", nonce="first", session_id="browser-a")
        a_next = await source.recommend("korean", nonce="next", session_id="browser-a")
        b_first = await source.recommend("korean", nonce="first", session_id="browser-b")
        self.assertEqual(a_first["items"], b_first["items"])
        self.assertFalse({item["id"] for item in a_first["items"]} & {item["id"] for item in a_next["items"]})
        self.assertNotIn("browser-a", json.dumps(a_first))
        await source.close()

    async def test_small_pool_reports_repeats_prioritizes_unseen_and_stable_nonce(self):
        discovery = FakeDiscovery()
        original_query = discovery._json
        async def small_query(url, params, tmdb=False):
            data = await original_query(url, params, tmdb)
            return {"results": data["results"][:16], "total_pages": 1}
        discovery._json = small_query
        source = recommendations.RecommendationSources(discovery)
        first = await source.recommend("korean", nonce="first", session_id="small")
        second = await source.recommend("korean", nonce="second", session_id="small")
        first_ids, second_ids = ({item["id"] for item in result["items"]} for result in (first, second))
        self.assertEqual(len(second_ids - first_ids), 4)
        self.assertTrue(second["selection"]["repeated"])
        self.assertIn("循环补足", second["message"])
        self.assertEqual(second, await source.recommend("korean", nonce="second", session_id="small"))
        self.assertEqual(len(discovery.calls), 1)
        await source.close()

    async def test_refill_failure_keeps_public_pool_and_retries_later(self):
        discovery = FakeDiscovery()
        source = recommendations.RecommendationSources(discovery)
        for index in range(3):
            await source.recommend("korean", nonce=str(index), session_id="refill")
        discovery.fail = True
        result = await source.recommend("korean", nonce="3", session_id="refill")
        self.assertEqual(len(result["items"]), 12)
        self.assertTrue(result["selection"]["repeated"])
        self.assertIn("部分地区或分页暂不可用", result["message"])
        self.assertNotIn("PRIVATE", json.dumps(result))
        discovery.fail = False
        result = await source.recommend("korean", nonce="4", session_id="refill")
        self.assertFalse(result["selection"]["repeated"])
        await source.close()

    async def test_persistent_pool_survives_restart_without_session_data(self):
        discovery, storage = FakeDiscovery(), FakeStorage()
        source = recommendations.RecommendationSources(discovery, storage=storage)
        first = await source.recommend("korean", nonce="first", session_id="never-persist-browser")
        count = len(discovery.calls)
        await source.close()
        restarted = recommendations.RecommendationSources(discovery, storage=storage)
        result = await restarted.recommend("korean", nonce="first", session_id="new-browser")
        self.assertEqual(first["items"], result["items"])
        self.assertEqual(len(discovery.calls), count)
        self.assertNotIn("never-persist-browser", json.dumps(storage.values))
        self.assertNotIn("test-token", json.dumps(storage.values))
        await restarted.close()

    async def test_persistent_host_pool_contains_only_projected_public_fields(self):
        storage, ops = FakeStorage(), FakeOps()
        async def get_ops():
            return ops
        source = recommendations.RecommendationSources(FakeDiscovery(token=""), get_ops, storage)
        await source.recommend("korean", session_id="browser-private")
        payload = json.dumps(storage.values)
        for forbidden in ("library_status", "PRIVATE", "private_account", "browser-private"):
            self.assertNotIn(forbidden, payload)
        await source.close()

    async def test_persistent_overviews_are_bounded_without_mutating_live_pool(self):
        discovery, storage = FakeDiscovery(), FakeStorage()
        original_query = discovery._json
        async def long_overview_query(url, params, tmdb=False):
            data = await original_query(url, params, tmdb)
            for row in data["results"]:
                row["overview"] = "长剧情" * 1000
            return data
        discovery._json = long_overview_query
        source = recommendations.RecommendationSources(discovery, storage=storage)
        result = await source.recommend("korean")
        self.assertEqual(len(result["items"][0]["overview"]), 3000)
        cached = next(iter(storage.values.values()))
        self.assertTrue(all(len(item["overview"]) == 1000 for item in cached["buckets"]["KR"]))
        self.assertEqual(len(source._cache["korean"]["buckets"]["KR"][0]["overview"]), 3000)
        await source.close()

    async def test_expired_persistent_pool_is_immediate_stale_while_refresh_waits(self):
        discovery, storage = FakeDiscovery(), FakeStorage()
        source = recommendations.RecommendationSources(discovery, storage=storage)
        await source.recommend("korean")
        await source.close()
        for value in storage.values.values():
            value["expires_wall"] = time.time() - 1
        entered, release = asyncio.Event(), asyncio.Event()
        original_query = discovery._json
        async def waiting_query(url, params, tmdb=False):
            entered.set()
            await release.wait()
            return await original_query(url, params, tmdb)
        discovery._json = waiting_query
        restarted = recommendations.RecommendationSources(discovery, storage=storage)
        result = await asyncio.wait_for(restarted.recommend("korean", nonce="new"), 0.2)
        self.assertEqual(result["status"], "stale")
        self.assertTrue(result["items"])
        await asyncio.wait_for(entered.wait(), 0.2)
        release.set()
        await asyncio.gather(*list(restarted._refreshes.values()))
        self.assertEqual((await restarted.recommend("korean", nonce="newer"))["status"], "ready")
        await restarted.close()

    async def test_session_and_nonce_memory_are_bounded_and_expired_sessions_drop(self):
        source = recommendations.RecommendationSources(FakeDiscovery())
        for index in range(recommendations.MAX_SESSIONS + 2):
            await source.recommend("korean", nonce="one", session_id="browser" + str(index))
        self.assertEqual(len(source._sessions), recommendations.MAX_SESSIONS)
        self.assertNotIn("browser0", source._sessions)
        source._sessions["browser2"]["last_seen"] = time.monotonic() - recommendations.SESSION_TTL - 1
        for index in range(recommendations.MAX_BATCHES + 1):
            await source.recommend("korean", nonce=str(index), session_id="steady")
        self.assertNotIn("browser2", source._sessions)
        session = source._sessions["steady"]["categories"]["korean"]
        self.assertEqual(len(session["batches"]), recommendations.MAX_BATCHES)
        self.assertEqual(len(session["history"]), recommendations.HISTORY_BATCHES)
        self.assertLessEqual(len(source._cache["korean"]["buckets"]["KR"]), recommendations.MAX_POOL_ITEMS)
        await source.close()

    async def test_no_plugin_token_uses_only_readonly_host_filter_and_public_projection(self):
        discovery, ops = FakeDiscovery(token=""), FakeOps()
        async def get_ops():
            return ops
        source = recommendations.RecommendationSources(discovery, get_ops)
        result = await source.recommend("korean", nonce="0")
        self.assertEqual(result["status"], "ready")
        self.assertEqual(len(result["items"]), 12)
        self.assertFalse(result["source"]["language_filtered"])
        self.assertFalse(discovery.calls)
        for operation, arguments in ops.calls:
            self.assertEqual(operation, "discover.filter-titles")
            self.assertEqual(arguments["origin_country"], "KR")
            self.assertEqual(arguments["genre_ids"], [18])
            self.assertEqual(arguments["sort"], "rating")
            self.assertNotIn("year", arguments)
        serialized = json.dumps(result)
        for forbidden in ("library_status", "PRIVATE-LIBRARY-PATH", "PRIVATE-ACCOUNT", "private_account"):
            self.assertNotIn(forbidden, serialized)
        await source.close()

    async def test_host_variety_is_not_presented_as_drama(self):
        row = {"title_ref": "tmdb:tv:1", "provider": "tmdb", "media_type": "tv", "title": "真人秀", "provider_rating": 8.0, "genres": ["真人秀"]}
        self.assertIsNone(recommendations._host_item(row, "KR"))

    async def test_host_chinese_fanout_preserves_region_scope(self):
        ops = FakeOps()
        async def get_ops():
            return ops
        source = recommendations.RecommendationSources(FakeDiscovery(token=""), get_ops)
        result = await source.recommend("chinese")
        self.assertEqual({args["origin_country"] for _, args in ops.calls}, {"CN", "TW", "HK"})
        self.assertIn("没有原始语言字段", result["message"])
        self.assertEqual({item["metadata"]["selection_country"] for item in result["items"]}, {"CN", "TW", "HK"})
        await source.close()

    async def test_direct_failure_can_use_host_without_exposing_secret(self):
        discovery, ops = FakeDiscovery(), FakeOps()
        discovery.fail = True
        async def get_ops():
            return ops
        source = recommendations.RecommendationSources(discovery, get_ops)
        result = await source.recommend("korean")
        self.assertEqual(result["status"], "ready")
        self.assertFalse(result["source"]["language_filtered"])
        self.assertNotIn("PRIVATE", json.dumps(result))
        await source.close()

    async def test_refresh_failure_keeps_previous_real_pool_as_stale(self):
        discovery = FakeDiscovery()
        source = recommendations.RecommendationSources(discovery)
        await source.recommend("korean", nonce="0")
        source._cache["korean"]["expires"] = time.monotonic() - 1
        discovery.fail = True
        result = await source.recommend("korean", nonce="1")
        self.assertEqual(result["status"], "stale")
        self.assertTrue(result["items"])
        self.assertNotIn("PRIVATE", json.dumps(result))
        await source.close()

    async def test_total_failure_returns_empty_real_data_not_demo(self):
        discovery, ops = FakeDiscovery(), FakeOps()
        discovery.fail = ops.fail = True
        async def get_ops():
            return ops
        source = recommendations.RecommendationSources(discovery, get_ops)
        result = await source.recommend("korean")
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["items"], [])
        self.assertNotIn("PRIVATE", json.dumps(result))
        await source.close()

    async def test_invalid_arguments_and_close(self):
        source = recommendations.RecommendationSources(FakeDiscovery())
        for category, nonce, limit in (("unknown", "0", 12), ("korean", "0", 25), ("korean", "x" * 129, 12)):
            with self.assertRaises(ValueError):
                await source.recommend(category, nonce=nonce, limit=limit)
        for invalid in ("", "contains space", "x" * 65, "path/../", None):
            with self.assertRaises(ValueError):
                await source.recommend("korean", session_id=invalid)
        await source.close()
        with self.assertRaises(ValueError):
            await source.recommend("korean")


if __name__ == "__main__":
    unittest.main()
