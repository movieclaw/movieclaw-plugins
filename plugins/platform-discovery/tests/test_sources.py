"""Source semantics, failure recovery and network boundaries; no live API calls."""
import asyncio
import importlib.util
import json
import sys
import time
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import httpx
from pydantic import SecretStr


package_dir = Path(__file__).parents[1] / "platform_discovery"
package = types.ModuleType("platform_discovery_source_tests")
package.__path__ = [str(package_dir)]
sys.modules[package.__name__] = package
spec = importlib.util.spec_from_file_location(package.__name__ + ".sources", package_dir / "sources.py")
sources = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = sources
spec.loader.exec_module(sources)


class SourceParsingTests(unittest.TestCase):
    def test_iqiyi_preserves_native_type_rank_and_source(self):
        page = '''<span class="erji__meta__txt">按实时热度排行</span>
          <a class="rvi__box" href="//www.iqiyi.com/v_sample.html" data-pb-v2="aid=42">
          <img class="i71-img" src="//pic1.iqiyipic.com/poster.jpg">
          <span class="rvi__No__txt">7</span><div class="rvi__tit1" title="样例 &amp; 剧">7样例</div>
          <div class="rvi__type1">电视剧 / 2026 / 悬疑</div><p class="rvi__des2">故事</p>
          <span class="rvi__index__num">5000</span></a>
          <a class="rvi__box"><div class="rvi__tit1" title="不支持的音乐"></div>
          <div class="rvi__type1">音乐 / 2026</div></a>'''
        items, note = sources.parse_iqiyi(page)
        self.assertEqual(len(items), 1)
        self.assertEqual((items[0]["title"], items[0]["media_type"], items[0]["rank"]), ("样例 & 剧", "tv", 7))
        self.assertEqual(items[0]["metadata"]["heat"], "5000")
        self.assertIsNone(items[0]["detail_ref"])
        self.assertIn("tab=media", items[0]["search_url"])
        self.assertEqual(items[0]["source_url"], "https://www.iqiyi.com/v_sample.html")
        self.assertEqual(note, "按实时热度排行")

    def test_youku_never_executes_javascript_and_keeps_chart_name(self):
        payload = {"moduleList": [{"mainTitleLinks": [{"title": "今日排行榜"}], "tabTitle": [{"name": "飙升榜"}], "components": [{"isRank": True, "itemList": [{"title": "样例剧", "img": "//m.ykimg.com/test", "videoLink": "//v.youku.com/id_test", "summary": "20集全"}]}]}]}
        page = "<script>window.__INITIAL_DATA__ = " + json.dumps(payload) + ";</script><script>throw new Error('must not execute')</script>"
        items, label, stamp = sources.parse_youku(page, "tv")
        self.assertEqual(label, "飙升榜")
        self.assertEqual(items[0]["metadata"]["rank_scope"], "飙升榜")
        self.assertIsNone(stamp)
        with self.assertRaises(ValueError):
            sources.parse_youku("<script>window.__INITIAL_DATA__ = fetch('https://evil.invalid');</script>", "tv")


class SourcesTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.instances = []

    async def asyncTearDown(self):
        for instance in self.instances:
            await instance.close()

    def make(self, handler, **config):
        instance = sources.DiscoverySources(config, transport=httpx.MockTransport(handler))
        self.instances.append(instance)
        return instance

    async def test_platform_switches_default_to_all_eight_platforms(self):
        def handler(request):
            self.fail("Reading the platform catalog must not contact an upstream")
        catalog = await self.make(handler).catalog()
        self.assertEqual([p["id"] for p in catalog["platforms"]],
                         ["iqiyi", "tencent", "youku", "mango", "bilibili", "netflix", "hbo", "disney"])

    async def test_each_platform_switch_filters_exactly_its_platform(self):
        platform_ids = ["iqiyi", "tencent", "youku", "mango", "bilibili", "netflix", "hbo", "disney"]
        def handler(request):
            self.fail("Reading the platform catalog must not contact an upstream")
        for enabled in platform_ids:
            with self.subTest(enabled=enabled):
                config = {"enable_" + platform: platform == enabled for platform in platform_ids}
                catalog = await self.make(handler, **config).catalog()
                self.assertEqual([p["id"] for p in catalog["platforms"]], [enabled])

    async def test_platform_switches_preserve_order_for_mixed_selection(self):
        enabled = {"iqiyi", "youku", "netflix", "disney"}
        config = {"enable_" + platform: platform in enabled for platform in sources.PLATFORM_BY_ID}
        catalog = await self.make(lambda request: self.fail("Catalog is local"), **config).catalog()
        self.assertEqual([p["id"] for p in catalog["platforms"]], ["iqiyi", "youku", "netflix", "disney"])

    async def test_all_platform_switches_off_blocks_feeds_without_upstream_requests(self):
        config = {"enable_" + platform: False for platform in sources.PLATFORM_BY_ID}
        # Explicit new switches must also win when the old selection was all on.
        config.update(enabled_platforms=list(sources.PLATFORM_BY_ID), enable_domestic=True)
        instance = self.make(lambda request: self.fail("Disabled platforms must not request upstreams"), **config)
        self.assertEqual((await instance.catalog())["platforms"], [])
        for platform in sources.PLATFORM_BY_ID:
            with self.subTest(platform=platform):
                result = await instance.feed(platform)
                self.assertEqual((result["status"], result["items"], result["has_more"]), ("unavailable", [], False))

    async def test_legacy_dict_selection_and_domestic_switch_remain_compatible(self):
        selections = [
            ["iqiyi", "netflix", "disney"],
            "iqiyi,netflix,disney",
            "iqiyi、netflix、disney",
            "iqiyi\nnetflix\ndisney",
            "iqiyi,netflix、disney\n",
        ]
        for selection in selections:
            with self.subTest(selection_type=type(selection).__name__):
                instance = self.make(lambda request: self.fail("Catalog is local"), enabled_platforms=selection, enable_domestic=False)
                self.assertEqual([p["id"] for p in (await instance.catalog())["platforms"]], ["netflix", "disney"])

    async def test_explicit_new_switches_override_legacy_selection_and_domestic_false(self):
        instance = self.make(lambda request: self.fail("Catalog is local"),
                             enabled_platforms=["netflix", "hbo"], enable_domestic=False,
                             enable_iqiyi=True, enable_netflix=False, enable_disney=True)
        self.assertEqual([p["id"] for p in (await instance.catalog())["platforms"]], ["iqiyi", "hbo", "disney"])

    async def test_legacy_domestic_switch_accepts_false_string_and_numeric_values(self):
        for legacy_off in (False, 0, "false", " False ", "0", "no", "n", "off", "f"):
            with self.subTest(legacy_off=legacy_off):
                instance = self.make(lambda request: self.fail("Catalog is local"), enable_domestic=legacy_off)
                self.assertEqual([p["id"] for p in (await instance.catalog())["platforms"]], ["netflix", "hbo", "disney"])

    async def test_platform_selection_does_not_change_proxy_region_token_or_connection_test(self):
        def handler(request):
            self.assertEqual(str(request.url), sources.NETFLIX_DATA)
            self.assertNotIn("Authorization", request.headers)
            return httpx.Response(200, text="week\tcategory\tweekly_rank\tshow_title\n")
        config = {"enable_" + platform: False for platform in sources.PLATFORM_BY_ID}
        instance = self.make(handler, **config, watch_region="gb", tmdb_token=SecretStr("private-tmdb-token"),
                             proxy_mode="custom", proxy_url=SecretStr("http://user:private-proxy-password@127.0.0.1:7890"))
        catalog = await instance.catalog()
        self.assertEqual(catalog["platforms"], [])
        self.assertEqual((catalog["region"], catalog["proxy_mode"], catalog["tmdb_configured"]), ("GB", "custom", True))
        self.assertNotIn("private-", json.dumps(catalog))
        connection = await instance.connection_test()
        self.assertEqual((connection["ok"], connection["mode"]), (True, "custom"))

    async def test_netflix_official_period_and_independent_category_ranks(self):
        today = datetime.now(timezone.utc).date()
        week = (today - timedelta(days=today.weekday() + 1)).isoformat()
        tsv = "week\tcategory\tweekly_rank\tshow_title\tseason_title\tweekly_views\tweekly_hours_viewed\truntime\n"
        tsv += "".join(f"{week}\t{category}\t{rank}\tSample {rank}\tN/A\t100\t200\t2.0\n"
                       for category in sources.OFFICIAL_PAGES for rank in range(1, 11))
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(200, text=tsv)
        instance = self.make(handler)
        result = await instance.feed("netflix")
        self.assertEqual(result["status"], "stale")
        self.assertEqual(result["source"]["data_source"], "tsv_fallback")
        self.assertEqual(result["source"]["kind"], "official_rank")
        self.assertEqual(result["source"]["updated_at"], week)
        self.assertEqual([i["rank"] for i in result["items"][:4]], [1, 1, 1, 1])
        self.assertNotEqual(result["items"][0]["metadata"]["rank_scope"], result["items"][1]["metadata"]["rank_scope"])
        self.assertTrue(all(i["detail_ref"] is None for i in result["items"]))
        variety = await instance.feed("netflix", "variety")
        self.assertEqual((variety["status"], variety["items"]), ("unavailable", []))
        self.assertEqual(sum(str(call.url) == sources.NETFLIX_DATA for call in calls), 1)
        self.assertTrue(all(call.url.host == "www.netflix.com" for call in calls))

    @staticmethod
    def official_netflix_page(week, title="Sample: Season 2"):
        finish = datetime.fromisoformat(week).strftime("%m/%d/%y")
        return f'''<a data-uia="top10-filters-week-select-option" aria-selected="true"><span>1/1/26 - {finish}</span></a>
          <div data-uia="top10-card" style="background-image:url(https://dnm.nflximg.net/real-scene.jpg)">
            <div data-uia="top10-card-logo"><img src="https://dnm.nflximg.net/logo.png" alt="{title}"></div>
            <a data-uia="play-on-netflix-link" href="https://www.netflix.com/watch/12345678?trackId=1">Watch</a>
          </div>
          <td data-uia="top10-table-row-title"><img src="https://dnm.nflximg.net/real-thumb.jpg"><button>{title}</button></td>
          <img src="https://dnm.nflximg.net/unrelated-article.jpg" alt="{title}">'''

    async def test_netflix_uses_same_week_first_party_picture_without_token_and_skips_logo(self):
        week = (datetime.now(timezone.utc).date() - timedelta(days=3)).isoformat()
        def handler(request):
            self.assertEqual(request.url.host, "www.netflix.com")
            self.assertNotIn("Authorization", request.headers)
            return httpx.Response(200, text=self.official_netflix_page(week))
        item = sources._item("netflix", "Sample", "tv", metadata={"rank_scope": "TV (English)", "season_title": "Sample: Season 2"})
        await self.make(handler)._netflix_artwork().official_items([item], week)
        self.assertEqual(item["poster_url"], "https://dnm.nflximg.net/real-scene.jpg")
        self.assertEqual(item["poster_fallback_urls"], ["https://dnm.nflximg.net/real-thumb.jpg"])
        self.assertEqual(item["metadata"]["poster_source"], "Netflix Tudum")
        self.assertEqual(item["metadata"]["poster_aspect"], "landscape")
        self.assertEqual(item["source_url"], "https://www.netflix.com/watch/12345678")
        self.assertIsNone(item["detail_ref"])
        self.assertNotIn("logo.png", json.dumps(item))
        self.assertNotIn("unrelated-article", json.dumps(item))

    async def test_netflix_rejects_different_week_or_different_season_first_party_art(self):
        week = (datetime.now(timezone.utc).date() - timedelta(days=3)).isoformat()
        for page_week, page_title in [("2020-01-01", "Sample: Season 2"), (week, "Sample: Season 1")]:
            with self.subTest(matching_week=page_week == week):
                instance = self.make(lambda request: httpx.Response(200, text=self.official_netflix_page(page_week, page_title)))
                item = sources._item("netflix", "Sample", "tv", metadata={"rank_scope": "TV (English)", "season_title": "Sample: Season 2"})
                await instance._netflix_artwork().official_items([item], week)
                self.assertIsNone(item["poster_url"])

    async def test_netflix_wwe_duplicate_year_is_normalized_but_episode_date_must_match(self):
        week = (datetime.now(timezone.utc).date() - timedelta(days=3)).isoformat()
        instance = self.make(lambda request: httpx.Response(200, text=self.official_netflix_page(week, "Raw: 2026 - September 28, 2026")))
        for episode_date, matched in [("September 28, 2026", True), ("September 29, 2026", False)]:
            with self.subTest(episode_date=episode_date):
                item = sources._item("netflix", "Raw", "tv", metadata={"rank_scope": "TV (English)", "season_title": "Raw: " + episode_date})
                await instance._netflix_artwork().official_items([item], week)
                self.assertEqual(bool(item["poster_url"]), matched)

    async def test_netflix_official_art_cache_survives_temporary_refresh_failure(self):
        week = (datetime.now(timezone.utc).date() - timedelta(days=3)).isoformat()
        calls = 0
        def handler(request):
            nonlocal calls
            calls += 1
            if calls > 1:
                raise httpx.ConnectError("temporary failure", request=request)
            return httpx.Response(200, text=self.official_netflix_page(week))
        instance = self.make(handler)
        resolver = instance._netflix_artwork()
        def fresh():
            return sources._item("netflix", "Sample", "tv", metadata={"rank_scope": "TV (English)", "season_title": "Sample: Season 2"})
        first = fresh()
        await resolver.official_items([first], week)
        key = ("netflix-artwork", "TV (English)", week)
        instance._cache[key] = (time.monotonic() - 1, instance._cache[key][1])
        second = fresh()
        await resolver.official_items([second], week)
        self.assertEqual(second["poster_url"], first["poster_url"])
        self.assertEqual(calls, 2)

    async def test_netflix_multilingual_exact_match_strips_season_and_keeps_official_backup(self):
        calls = []
        def handler(request):
            calls.append(request)
            self.assertEqual(request.url.params["query"], "Squid Game")
            english = request.url.params["language"] == "en-US"
            return httpx.Response(200, json={"results": [{"id": 93405, "name": "Squid Game" if english else "鱿鱼游戏", "original_name": "오징어 게임", "poster_path": "/valid.jpg", "first_air_date": "2021-09-17", "vote_average": 8.0}]})
        instance = self.make(handler, tmdb_token="test-token")
        item = sources._item("netflix", "Squid Game: Season 3", "tv", poster_url="https://dnm.nflximg.net/official.jpg", metadata={})
        await instance._enrich_netflix(item)
        self.assertEqual(item["detail_ref"], "tmdb:tv:93405")
        self.assertEqual(item["poster_url"], "https://image.tmdb.org/t/p/w500/valid.jpg")
        self.assertIn("https://dnm.nflximg.net/official.jpg", item["poster_fallback_urls"])
        self.assertEqual(item["rating_source"], "TMDB")
        self.assertEqual(len(calls), 2)
        cached_item = sources._item("netflix", "Squid Game: Season 3", "tv", metadata={})
        await instance._enrich_netflix(cached_item)
        self.assertEqual(cached_item["poster_url"], item["poster_url"])
        self.assertEqual(len(calls), 2)

    async def test_netflix_matching_rejects_ambiguous_ids_even_when_only_one_has_poster(self):
        def handler(request):
            return httpx.Response(200, json={"results": [
                {"id": 1, "title": "Same Movie", "release_date": "2021-01-01", "poster_path": "/first.jpg"},
                {"id": 2, "title": "Same Movie", "release_date": "1980-01-01", "poster_path": None},
            ]})
        instance = self.make(handler, tmdb_token="test-token")
        item = sources._item("netflix", "Same Movie", "movie", metadata={})
        await instance._enrich_netflix(item)
        self.assertIsNone(item["poster_url"])
        self.assertIsNone(item["detail_ref"])

    async def test_netflix_matching_rejects_release_year_conflicts(self):
        def handler(request):
            return httpx.Response(200, json={"results": [{"id": 1, "title": "Same Movie", "release_date": "1980-01-01", "poster_path": "/wrong.jpg"}]})
        instance = self.make(handler, tmdb_token="test-token")
        item = sources._item("netflix", "Same Movie (2021)", "movie", metadata={})
        await instance._enrich_netflix(item)
        self.assertIsNone(item["poster_url"])
        self.assertIsNone(item["detail_ref"])

    async def test_netflix_known_tmdb_alias_is_accepted_but_substring_is_not(self):
        for approved in (True, False):
            with self.subTest(approved_alias=approved):
                def handler(request):
                    if request.url.path.endswith("alternative_titles"):
                        return httpx.Response(200, json={"results": [{"title": "Raw" if approved else "Raw Cut"}]})
                    return httpx.Response(200, json={"results": [{"id": 4656, "name": "WWE Raw", "original_name": "WWE Raw", "poster_path": "/wwe.jpg", "first_air_date": "1993-01-11"}]})
                instance = self.make(handler, tmdb_token="test-token")
                item = sources._item("netflix", "Raw", "tv", metadata={"season_title": "Raw: 2026 - October 12, 2026"})
                await instance._enrich_netflix(item)
                self.assertEqual(item["detail_ref"], "tmdb:tv:4656" if approved else None)

    async def test_netflix_successful_tmdb_art_cache_survives_search_failure(self):
        healthy = True
        def handler(request):
            if not healthy:
                raise httpx.ConnectError("temporary failure", request=request)
            return httpx.Response(200, json={"results": [{"id": 10, "name": "Sample", "original_name": "Sample", "poster_path": "/stable.jpg", "first_air_date": "2021-01-01"}]})
        instance = self.make(handler, tmdb_token="test-token")
        first = sources._item("netflix", "Sample", "tv", metadata={})
        await instance._enrich_netflix(first)
        key = next(key for key in instance._cache if key[0] == "netflix-match")
        instance._cache[key] = (time.monotonic() - 1, instance._cache[key][1])
        healthy = False
        second = sources._item("netflix", "Sample", "tv", metadata={})
        await instance._enrich_netflix(second)
        self.assertEqual(second["poster_url"], first["poster_url"])
        self.assertEqual(second["detail_ref"], first["detail_ref"])

    async def test_cache_deduplicates_concurrent_requests_and_preserves_last_success(self):
        calls = 0
        async def handler(request):
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.01)
            if calls > 1:
                raise httpx.ConnectError("private upstream diagnostic", request=request)
            return httpx.Response(200, json={"data": {"ErrCode": 0, "Results": [{"ChannelID": "2", "Result": {"TotalNum": 1, "ListInfo": [{"ID": "mzc00200sample", "Query": "真实片名", "VideoType": "2", "HzImgURL": "https://puui.qpic.cn/sample"}]}}]}})
        instance = self.make(handler)
        a, b = await asyncio.gather(instance.feed("tencent", "tv"), instance.feed("tencent", "tv"))
        self.assertEqual(calls, 1)
        self.assertEqual(a, b)
        a["items"][0]["title"] = "consumer mutation"
        self.assertEqual((await instance.feed("tencent", "tv"))["items"][0]["title"], "真实片名")
        key = ("tencent", "tv", 1)
        instance._cache[key] = (time.monotonic() - 1, instance._cache[key][1])
        stale = await instance.feed("tencent", "tv")
        self.assertEqual(stale["status"], "stale")
        self.assertEqual(stale["items"][0]["title"], "真实片名")
        self.assertNotIn("private", stale["message"])
        self.assertIn("热搜榜", stale["source"]["label"])

    async def test_bilibili_rejects_successful_but_old_rankings(self):
        def handler(request):
            return httpx.Response(200, json={"code": 0, "result": {"note": "电影投稿在 2020年01月01日 - 2020年01月03日 的数据综合得分", "list": [{"title": "旧片", "rank": 1}]}})
        result = await self.make(handler).feed("bilibili", "movie")
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["items"], [])
        self.assertIn("2020-01-03", result["message"])

    async def test_missing_token_does_not_call_or_fabricate_hbo_catalog(self):
        def handler(request):
            self.fail("No request should be made without the optional TMDB token")
        result = await self.make(handler).feed("hbo")
        self.assertEqual((result["status"], result["items"]), ("needs_config", []))

    async def test_provider_filter_region_attribution_and_no_official_rank(self):
        calls = []
        def handler(request):
            calls.append(request)
            self.assertEqual(request.headers["Authorization"], "Bearer test-secret")
            if "/watch/providers/" in request.url.path:
                return httpx.Response(200, json={"results": [{"provider_id": 999, "provider_name": "HBO Max"}]})
            self.assertEqual(request.url.params["watch_region"], "HK")
            self.assertEqual(request.url.params["with_watch_providers"], "999")
            self.assertEqual(request.url.params["sort_by"], "popularity.desc")
            return httpx.Response(200, json={"total_pages": 2, "results": [{"id": 17, "name": "海外样例", "genre_ids": [], "popularity": 3, "vote_average": 8.1, "first_air_date": "2024-01-01", "poster_path": "/sample.jpg"}]})
        result = await self.make(handler, tmdb_token="test-secret", watch_region="HK").feed("hbo", "tv")
        self.assertEqual(result["source"]["kind"], "provider_popular")
        self.assertIn("JustWatch", result["source"]["attribution"])
        self.assertEqual(result["items"][0]["detail_ref"], "tmdb:tv:17")
        self.assertEqual(result["items"][0]["rating_source"], "TMDB")
        self.assertNotIn("rank", result["items"][0])
        self.assertEqual(len(calls), 2)

    async def test_fixed_hosts_redirects_and_response_size_boundaries(self):
        calls = []
        def handler(request):
            calls.append(request)
            if request.url.path.endswith("redirect"):
                return httpx.Response(302, headers={"location": "https://127.0.0.1/"})
            return httpx.Response(200, content=b"x" * 4_000_001)
        instance = self.make(handler)
        with self.assertRaises(ValueError):
            await instance._fetch("https://127.0.0.1/private")
        with self.assertRaises(ValueError):
            await instance._fetch("http://www.netflix.com/unsafe")
        self.assertEqual(calls, [])
        with self.assertRaises(ValueError):
            await instance._fetch("https://www.netflix.com/redirect")
        with self.assertRaises(ValueError):
            await instance._fetch("https://www.netflix.com/large")
        self.assertEqual(len(calls), 2)

    async def test_default_host_uses_only_own_sdk_service(self):
        transport = httpx.MockTransport(lambda request: httpx.Response(200))
        factory = Mock(return_value=transport)
        sdk = types.ModuleType("movieclaw_sdk")
        sdk.net = types.SimpleNamespace(http_transport=factory)
        with patch.dict(sys.modules, {"movieclaw_sdk": sdk}):
            instance = sources.DiscoverySources({}, entry_id="platform-discovery")
        self.instances.append(instance)
        factory.assert_called_once_with("platform-discovery")
        self.assertEqual(instance.proxy_mode, "host")
        self.assertFalse(instance._client._trust_env)

    async def test_direct_and_custom_disable_ambient_proxy_environment(self):
        proxy = "http://proxy-user:proxy-secret@127.0.0.1:7890"
        for mode, expected in [("direct", {"trust_env": False}), ("custom", {"proxy": proxy, "trust_env": False})]:
            with self.subTest(mode=mode):
                transport = httpx.MockTransport(lambda request: httpx.Response(200))
                sdk = types.ModuleType("movieclaw_sdk")
                sdk.net = types.SimpleNamespace(http_transport=Mock(side_effect=AssertionError("Host transport must not be selected")))
                with patch.dict(sys.modules, {"movieclaw_sdk": sdk}), patch.object(sources.httpx, "AsyncHTTPTransport", return_value=transport) as factory:
                    instance = sources.DiscoverySources({"proxy_mode": mode, "proxy_url": SecretStr(proxy)})
                self.instances.append(instance)
                factory.assert_called_once_with(**expected)
                self.assertFalse(instance._client._trust_env)
                self.assertEqual((await instance.catalog())["proxy_mode"], mode)
                self.assertNotIn("proxy-secret", json.dumps(await instance.catalog()))

    async def test_plain_proxy_address_takes_priority_over_legacy_secret_proxy(self):
        ordinary = "http://127.0.0.1:7891"
        transport = httpx.MockTransport(lambda request: httpx.Response(200))
        with patch.object(sources.httpx, "AsyncHTTPTransport", return_value=transport) as factory:
            instance = sources.DiscoverySources({
                "proxy_mode": "custom", "proxy_address": "  " + ordinary + "  ",
                "proxy_url": SecretStr("http://old-user:old-private-password@127.0.0.1:7890"),
            })
        self.instances.append(instance)
        factory.assert_called_once_with(proxy=ordinary, trust_env=False)
        self.assertNotIn("old-private-password", json.dumps(await instance.catalog()))

    async def test_blank_plain_proxy_address_keeps_legacy_secret_proxy(self):
        secret = "http://legacy-user:legacy-private-password@127.0.0.1:7890"
        for ordinary in ("", "  "):
            with self.subTest(ordinary_is_whitespace=bool(ordinary)):
                transport = httpx.MockTransport(lambda request: httpx.Response(200))
                with patch.object(sources.httpx, "AsyncHTTPTransport", return_value=transport) as factory:
                    instance = sources.DiscoverySources({"proxy_mode": "custom", "proxy_address": ordinary, "proxy_url": SecretStr(secret)})
                self.instances.append(instance)
                factory.assert_called_once_with(proxy=secret, trust_env=False)
                self.assertNotIn("legacy-private-password", json.dumps(await instance.catalog()))

    async def test_plain_proxy_address_rejects_all_userinfo_without_disclosing_values(self):
        ordinary_addresses = [
            "http://private-user:private-password@127.0.0.1:7891",
            "http://private-user@127.0.0.1:7891",
            "http://@127.0.0.1:7891",
        ]
        for ordinary in ordinary_addresses:
            with self.subTest(userinfo_format=ordinary.split("://")[1].split("@")[0][:1]):
                with patch.object(sources.httpx, "AsyncHTTPTransport") as factory:
                    with self.assertRaises(ValueError) as failure:
                        sources.DiscoverySources({
                            "proxy_mode": "custom", "proxy_address": ordinary,
                            "proxy_url": SecretStr("http://fallback-user:fallback-password@127.0.0.1:7890"),
                        })
                factory.assert_not_called()
                message = str(failure.exception)
                for private in ("private-user", "private-password", "127.0.0.1", "fallback-password"):
                    self.assertNotIn(private, message)

    async def test_supported_proxy_schemes_construct_real_transports(self):
        # The production host declares httpx[socks]; test HTTP and SOCKS URLs
        # without opening any network connection.
        for scheme in ("http", "https", "socks5", "socks5h"):
            with self.subTest(scheme=scheme):
                instance = sources.DiscoverySources({"proxy_mode": "custom", "proxy_url": SecretStr(f"{scheme}://user:secret@127.0.0.1:7890")})
                self.instances.append(instance)
                self.assertEqual(instance.proxy_mode, "custom")

    async def test_invalid_proxy_configuration_errors_never_include_address_or_password(self):
        bad_urls = [None, "file:///etc/passwd", "http://user:private-password@127.0.0.1:99999", "http://user:private-password@127.0.0.1:7890/path", "http://user:private-password@127.0.0.1:7890?query=secret", "http://user:private-password@127.0.0.1:7890#fragment"]
        for value in bad_urls:
            with self.subTest(value_present=value is not None):
                with self.assertRaises(ValueError) as failure:
                    sources.DiscoverySources({"proxy_mode": "custom", "proxy_url": SecretStr(value) if value else None})
                self.assertNotIn("private-password", str(failure.exception))
                self.assertNotIn("127.0.0.1", str(failure.exception))
        with self.assertRaises(ValueError) as failure:
            sources.DiscoverySources({"proxy_mode": "http://user:private-password@127.0.0.1"})
        self.assertNotIn("private-password", str(failure.exception))
        with patch.object(sources.httpx, "AsyncHTTPTransport", side_effect=ValueError("proxy initialization failed: http://user:private-password@127.0.0.1:7890")):
            with self.assertRaises(ValueError) as failure:
                sources.DiscoverySources({"proxy_mode": "custom", "proxy_url": SecretStr("http://user:private-password@127.0.0.1:7890")})
        self.assertNotIn("private-password", str(failure.exception))

    async def test_connection_test_uses_fixed_endpoint_and_does_not_add_tmdb_token(self):
        calls = []
        def handler(request):
            calls.append(request)
            self.assertEqual(str(request.url), sources.NETFLIX_DATA)
            self.assertNotIn("Authorization", request.headers)
            return httpx.Response(200, text="week\tcategory\tweekly_rank\tshow_title\n")
        instance = self.make(handler, proxy_mode="custom", proxy_url=SecretStr("http://user:secret@127.0.0.1:7890"), tmdb_token="tmdb-secret")
        result = await instance.connection_test()
        self.assertEqual((result["ok"], result["mode"]), (True, "custom"))
        self.assertIsInstance(result["latency_ms"], int)
        self.assertGreaterEqual(result["latency_ms"], 0)
        self.assertEqual(len(calls), 1)
        self.assertNotIn("secret", json.dumps(result))

    async def test_connection_test_rejects_block_pages_and_does_not_blame_proxy_for_denial(self):
        for status, body in [(200, "<html>blocked login page</html>"), (401, "denied"), (403, "forbidden"), (429, "throttled"), (503, "unavailable")]:
            with self.subTest(status=status):
                instance = self.make(lambda request: httpx.Response(status, text=body))
                result = await instance.connection_test()
                self.assertFalse(result["ok"])
                if status in {401, 403}:
                    self.assertIn("不能单独判断代理", result["message"])
                self.assertNotIn(body, result["message"])

    async def test_transport_errors_are_redacted_in_connection_and_aggregated_feeds(self):
        secret_diagnostic = "http://user:private-proxy-password@127.0.0.1:7890"
        def handler(request):
            raise ValueError("runtime proxy failure at " + secret_diagnostic)
        instance = self.make(handler, proxy_mode="custom", proxy_url=SecretStr(secret_diagnostic))
        for result in [await instance.connection_test(), await instance.feed("netflix"), await instance.feed("bilibili")]:
            serialized = json.dumps(result)
            self.assertNotIn("private-proxy-password", serialized)
            self.assertNotIn("127.0.0.1", serialized)
        await instance.close()
        result = await instance.connection_test()
        self.assertFalse(result["ok"])


if __name__ == "__main__":
    unittest.main()
