"""Official domestic source contracts, recorded samples and failure semantics."""
import asyncio
import copy
import importlib.util
import json
import sys
import types
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx


package_dir = Path(__file__).parents[1] / "platform_discovery"
package = types.ModuleType("platform_discovery_domestic_tests")
package.__path__ = [str(package_dir)]
sys.modules[package.__name__] = package
spec = importlib.util.spec_from_file_location(package.__name__ + ".sources", package_dir / "sources.py")
sources = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = sources
spec.loader.exec_module(sources)
adapters = sys.modules[package.__name__ + ".domestic_sources"]
FIXTURE = json.loads((Path(__file__).parent / "fixtures/domestic-2026-10-18.json").read_text())
SAMPLE_DAY = date(2026, 10, 18)


def current_bilibili(kind, count=None):
    payload = copy.deepcopy(FIXTURE["bilibili"][kind]["response"])
    today = datetime.now(timezone(timedelta(hours=8))).date()
    start = today - timedelta(days=3)
    payload["data"]["note"] = f"『{adapters.KIND_NAMES[kind]}』投稿在 {start:%Y年%m月%d日} - {today:%Y年%m月%d日} 的数据综合得分，每小时更新一次"
    if count is not None:
        sample = payload["data"]["list"][0]
        payload["data"]["list"] = [dict(sample, title=f"{kind} 作品 {i}", rank=i, season_id=1000 + i) for i in range(1, count + 1)]
    return payload


class DomesticParserTests(unittest.TestCase):
    def test_recorded_bilibili_licensed_categories_dates_and_string_ratings(self):
        for kind in ("tv", "movie", "variety"):
            with self.subTest(kind=kind):
                rows, note, updated = adapters.parse_bilibili_rank(FIXTURE["bilibili"][kind]["response"], kind, today=SAMPLE_DAY)
                self.assertEqual(len(rows), 3)
                self.assertEqual([row["rank"] for row in rows], [1, 2, 3])
                self.assertEqual(updated, "2026-10-18")
                self.assertIn("2026年10月15日", note)
                self.assertEqual(rows[0]["metadata"]["rank_scope"], adapters.KIND_NAMES[kind] + "榜")
                if kind == "tv":
                    self.assertEqual(rows[0]["rating"], 9.9)
                if kind == "variety":
                    self.assertIsNone(rows[0]["rating"])

    def test_bilibili_accepts_legacy_rating_shape_without_reusing_untrusted_links(self):
        payload = copy.deepcopy(FIXTURE["bilibili"]["movie"]["response"])
        payload["result"] = payload.pop("data")
        payload["result"]["list"][0].update(rating={"score": 8.6}, url="https://private.invalid/arbitrary")
        rows, _, _ = adapters.parse_bilibili_rank(payload, "movie", today=SAMPLE_DAY)
        self.assertEqual(rows[0]["rating"], 8.6)
        self.assertEqual(rows[0]["source_url"], "https://www.bilibili.com/bangumi/play/ss" + rows[0]["native_id"])

    def test_bilibili_rejects_historical_future_malformed_and_reversed_periods(self):
        notes = [
            "电影投稿在 2025年07月19日 - 2025年07月22日 的数据综合得分",
            "电影投稿在 2027年10月15日 - 2027年10月18日 的数据综合得分",
            "电影投稿在 2026年10月19日 - 2026年10月18日 的数据综合得分",
            "电影投稿在 2026年02月31日 - 2026年10月18日 的数据综合得分",
            "电影投稿在 2020年01月01日 - 2026年10月18日 的数据综合得分",
            "电影投稿近日的数据综合得分",
        ]
        for note in notes:
            payload = copy.deepcopy(FIXTURE["bilibili"]["movie"]["response"])
            payload["data"]["note"] = note
            with self.subTest(note=note), self.assertRaises(adapters.DomesticSourceError):
                adapters.parse_bilibili_rank(payload, "movie", today=SAMPLE_DAY)

    def test_bilibili_rejects_animation_and_cross_category_response(self):
        payload = copy.deepcopy(FIXTURE["bilibili"]["tv"]["response"])
        payload["data"]["season_type"] = 4
        with self.assertRaises(adapters.DomesticSourceError):
            adapters.parse_bilibili_rank(payload, "tv", today=SAMPLE_DAY)
        payload = copy.deepcopy(FIXTURE["bilibili"]["movie"]["response"])
        with self.assertRaises(adapters.DomesticSourceError):
            adapters.parse_bilibili_rank(payload, "tv", today=SAMPLE_DAY)

    def test_bilibili_skips_ugc_missing_identifiers_invalid_ranks_and_duplicates(self):
        payload = copy.deepcopy(FIXTURE["bilibili"]["movie"]["response"])
        original = payload["data"]["list"][0]
        payload["data"]["list"] = [original, dict(original), {"title": "UP 主影视解说", "bvid": "BVsample", "rank": 4}, dict(original, season_id=3, rank=-1)]
        rows, _, _ = adapters.parse_bilibili_rank(payload, "movie", today=SAMPLE_DAY)
        self.assertEqual(len(rows), 1)

    def test_recorded_mango_modules_are_editorial_selections_without_fabricated_dates_or_ranks(self):
        for kind in ("tv", "movie", "variety"):
            with self.subTest(kind=kind):
                rows, label, updated = adapters.parse_mango_channel(FIXTURE["mango"][kind]["response"], kind, today=SAMPLE_DAY)
                self.assertEqual(len(rows), 3)
                self.assertEqual(label, adapters.MANGO_MODULES[kind])
                self.assertIsNone(updated)
                self.assertTrue(all("rank" not in row for row in rows))
                self.assertTrue(all(row["metadata"]["selection_scope"] == label for row in rows))
                self.assertNotIn("updatetime", rows[0])

    def test_mango_ignores_ads_playlist_cards_external_links_and_duplicate_titles(self):
        payload = copy.deepcopy(FIXTURE["mango"]["tv"]["response"])
        rows = payload["data"][0]["DSLList"][0]["data"]["items"]
        original = rows[0]
        rows[:] = [original, dict(original), dict(original, jumpId="2", jumpKind=2, videoUrl="https://www.mgtv.com/l/2.html"),
                   dict(original, jumpId="3", videoUrl="https://private.invalid/b/3/4.html"),
                   dict(original, jumpId="4", videoUrl="https://user:password@www.mgtv.com/b/4/5.html"),
                   dict(original, jumpId="5", videoUrl="javascript:alert(1)")]
        parsed, _, _ = adapters.parse_mango_channel(payload, "tv", today=SAMPLE_DAY)
        self.assertEqual(len(parsed), 1)

    def test_mango_movies_exclude_promotional_snippets_and_extras(self):
        payload = copy.deepcopy(FIXTURE["mango"]["movie"]["response"])
        rows = payload["data"][0]["DSLList"][0]["data"]["items"]
        original = rows[0]
        rows[:] = [original, dict(original, jumpId="2", videoUrl="https://www.mgtv.com/b/2/3.html", durationTopic=82),
                   dict(original, jumpId="3", videoUrl="https://www.mgtv.com/b/3/4.html", name="真实作品 日常番外", durationTopic=0)]
        parsed, _, _ = adapters.parse_mango_channel(payload, "movie", today=SAMPLE_DAY)
        self.assertEqual(len(parsed), 1)

    def test_mango_rejects_wrong_channel_and_unverified_module(self):
        for wrong_channel in (True, False):
            payload = copy.deepcopy(FIXTURE["mango"]["tv"]["response"])
            if wrong_channel:
                payload["channel"].update(title="动漫", vclassId=8)
            else:
                payload["data"][0]["moduleTitle"] = "广告精选"
            with self.subTest(wrong_channel=wrong_channel), self.assertRaises(adapters.DomesticSourceError):
                adapters.parse_mango_channel(payload, "tv", today=SAMPLE_DAY)

    def test_mango_rejects_expired_response_dates_without_rejecting_classic_programs(self):
        payload = copy.deepcopy(FIXTURE["mango"]["tv"]["response"])
        payload["serverTime"] = int(datetime(2020, 1, 1, tzinfo=timezone.utc).timestamp())
        with self.assertRaises(adapters.DomesticSourceError):
            adapters.parse_mango_channel(payload, "tv", today=SAMPLE_DAY)
        payload["serverTime"] = 0
        payload["data"][0]["DSLList"][0]["data"]["items"][0]["updatetime"] = 1
        self.assertEqual(len(adapters.parse_mango_channel(payload, "tv", today=SAMPLE_DAY)[0]), 3)

    def test_mango_response_timestamp_does_not_become_recommendation_generation_date(self):
        payload = copy.deepcopy(FIXTURE["mango"]["tv"]["response"])
        payload["serverTime"] = int(datetime(2026, 10, 18, tzinfo=timezone.utc).timestamp())
        _, _, updated = adapters.parse_mango_channel(payload, "tv", today=SAMPLE_DAY)
        self.assertIsNone(updated)


class DomesticFeedTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.instances = []

    async def asyncTearDown(self):
        await asyncio.gather(*(instance.close() for instance in self.instances))

    def make(self, handler):
        instance = sources.DiscoverySources({}, transport=httpx.MockTransport(handler))
        self.instances.append(instance)
        return instance

    async def test_bilibili_uses_current_official_endpoint_and_projects_public_data(self):
        def handler(request):
            self.assertEqual(request.url.path, "/pgc/season/rank/web/list")
            self.assertEqual(dict(request.url.params), {"day": "3", "season_type": "5"})
            return httpx.Response(200, json=current_bilibili("tv"))
        result = await self.make(handler).feed("bilibili", "tv")
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["items"][0]["rating"], 9.9)
        self.assertTrue(result["items"][0]["poster_url"].startswith("https://"))
        self.assertEqual(result["source"]["url"], "https://www.bilibili.com/v/popular/rank/tv/")
        self.assertIn("电视剧榜", result["source"]["label"])

    async def test_bilibili_aggregate_paginates_all_three_independent_rankings(self):
        def handler(request):
            kind = {"2": "movie", "5": "tv", "7": "variety"}[request.url.params["season_type"]]
            return httpx.Response(200, json=current_bilibili(kind, count=30))
        instance = self.make(handler)
        first, fourth = await instance.feed("bilibili", "all"), await instance.feed("bilibili", "all", 4)
        self.assertEqual(len(first["items"]), 20)
        self.assertEqual(len(fourth["items"]), 20)
        self.assertTrue(fourth["has_more"])
        self.assertEqual([i["rank"] for i in first["items"][:3]], [1, 1, 1])
        self.assertEqual({i["media_type"] for i in first["items"][:3]}, {"movie", "tv", "variety"})

    async def test_bilibili_partial_category_failure_is_explicit_and_never_exposes_diagnostics(self):
        def handler(request):
            if request.url.params["season_type"] == "5":
                raise ValueError("private-proxy-password 127.0.0.1")
            kind = {"2": "movie", "7": "variety"}[request.url.params["season_type"]]
            return httpx.Response(200, json=current_bilibili(kind))
        result = await self.make(handler).feed("bilibili", "all")
        self.assertEqual(result["status"], "ready")
        self.assertIn("部分分类暂不可用", result["message"])
        self.assertNotIn("private-proxy-password", json.dumps(result))

    async def test_mango_official_modules_are_labeled_as_selection_and_not_rank(self):
        def handler(request):
            self.assertEqual(request.url.host, "dc.bz.mgtv.com")
            self.assertEqual(request.url.params["osType"], "windows")
            kind = {"4": "tv", "6": "variety", "9": "movie"}[request.url.path.rsplit("/", 1)[1]]
            return httpx.Response(200, json=FIXTURE["mango"][kind]["response"])
        instance = self.make(handler)
        for kind in ("all", "tv", "movie", "variety"):
            with self.subTest(kind=kind):
                result = await instance.feed("mango", kind)
                self.assertEqual(result["status"], "ready")
                self.assertEqual(result["source"]["kind"], "official_selection")
                self.assertIn("源未提供推荐生成时间", result["source"]["period"])
                self.assertTrue(all("rank" not in item for item in result["items"]))

    async def test_changed_mango_module_returns_unavailable_instead_of_using_other_recommendations(self):
        payload = copy.deepcopy(FIXTURE["mango"]["movie"]["response"])
        payload["data"][0]["moduleTitle"] = "广告"
        result = await self.make(lambda request: httpx.Response(200, json=payload)).feed("mango", "movie")
        self.assertEqual((result["status"], result["items"]), ("unavailable", []))

    async def test_mango_fallback_host_allowlist_does_not_allow_other_subdomains(self):
        self.assertIn("dc.bz.mgtv.com", sources.ALLOWED_HOSTS)
        instance = self.make(lambda request: self.fail("Disallowed host must not be fetched"))
        with self.assertRaises(ValueError):
            await instance._fetch("https://arbitrary.mgtv.com/private")


if __name__ == "__main__":
    unittest.main()
