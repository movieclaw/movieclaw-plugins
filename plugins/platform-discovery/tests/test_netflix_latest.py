"""Current SSR ranking integrity, shared refresh and legacy-cache migration."""
import asyncio
import copy
import importlib.util
import json
import sys
import time
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

package_dir = Path(__file__).parents[1] / "platform_discovery"
package = types.ModuleType("platform_discovery_latest_tests")
package.__path__ = [str(package_dir)]
sys.modules[package.__name__] = package
spec = importlib.util.spec_from_file_location(package.__name__ + ".sources", package_dir / "sources.py")
sources = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = sources
spec.loader.exec_module(sources)
artwork = sys.modules[package.__name__ + ".netflix_artwork"]


def latest_week():
    today = datetime.now(timezone.utc).date()
    return today - timedelta(days=today.weekday() + 1)


def page(category, week=None, *, country="Global", count=10, runtime="1:48", views="23,800,000", hours="42,800,000"):
    end = week or latest_week()
    start = end - timedelta(days=6)
    controls = [("top10-country-select-option", country), ("top10-category-select-option", artwork.OFFICIAL_LABELS[category]),
                ("top10-filters-week-select-option", start.strftime("%m/%d/%y") + " - " + end.strftime("%m/%d/%y"))]
    html = "".join(f'<a data-uia="{uia}" aria-selected="true">{label}</a>' for uia, label in controls)
    html += '<div data-uia="top10-table"><table><thead><tr>'
    html += ''.join(f'<th data-uia="top10-table-header-{key}">{label}</th>' for key, label in
                    (("views", "Views"), ("runtime", "Runtime"), ("hours", "Hours Viewed")))
    html += '</tr></thead><tbody>'
    for rank in range(1, count + 1):
        title = f"{category} Example {rank}" + (": Season 2" if category.startswith("TV") else "")
        html += f'<tr><td data-uia="top10-table-row-title"><span class="rank">{rank:02d}</span><img src="https://dnm.nflximg.net/{rank}.jpg"><button>{title}</button></td>'
        html += ''.join(f'<td data-uia="top10-table-row-{key}">{value}</td>' for key, value in
                        (("weeks", "2"), ("views", views), ("runtime", runtime), ("hours", hours)))
        html += '</tr>'
    return html + '</tbody></table></div>'


class NetflixLatestTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.instances = []

    async def asyncTearDown(self):
        for instance in self.instances:
            await instance.close()

    def make(self, handler):
        instance = sources.DiscoverySources({}, transport=httpx.MockTransport(handler))
        self.instances.append(instance)
        return instance

    def handler(self, calls, overrides=None):
        by_url = {url: category for category, url in sources.OFFICIAL_PAGES.items()}
        def handle(request):
            calls.append(str(request.url))
            self.assertNotIn("Authorization", request.headers)
            self.assertNotIn("?", str(request.url))
            category = by_url[str(request.url)]
            return httpx.Response(200, text=page(category, **(overrides or {}).get(category, {})))
        return handle

    async def test_latest_default_four_boards_metrics_and_artwork_reused_for_all_variants(self):
        calls = []
        instance = self.make(self.handler(calls))
        all_one, television, movies, all_two = await asyncio.gather(instance.feed("netflix"), instance.feed("netflix", "tv"),
            instance.feed("netflix", "movie"), instance.feed("netflix", "all", 2))
        self.assertEqual(len(calls), 4)
        self.assertEqual(set(calls), set(sources.OFFICIAL_PAGES.values()))
        self.assertTrue(all_one["has_more"])
        self.assertFalse(all_two["has_more"])
        self.assertEqual(len({i["id"] for r in (all_one, all_two) for i in r["items"]}), 40)
        for result in (all_one, all_two, television, movies):
            self.assertEqual((result["status"], result["source"]["updated_at"], result["source"]["board_count"]), ("ready", latest_week().isoformat(), 4))
            self.assertEqual(result["source"]["data_source"], "official_pages")
            self.assertEqual(result["artwork_diagnostics"]["missing"], 0)
            self.assertEqual(len(result["items"]), 20)
            meta = result["items"][0]["metadata"]
            self.assertEqual((meta["weekly_views"], meta["weekly_hours_viewed"], meta["runtime_display"], meta["runtime_minutes"]), (23800000, 42800000, "1:48", 108))
            self.assertAlmostEqual(meta["runtime"], 1.8)
        self.assertEqual({i["media_type"] for i in television["items"]}, {"tv"})
        self.assertEqual({i["media_type"] for i in movies["items"]}, {"movie"})

    def test_parser_rejects_wrong_scope_category_week_units_abbreviation_and_incomplete_ranks(self):
        category = "Films (English)"
        valid = page(category)
        invalid = [page(category, country="United States"), page(category, count=9), page(category, runtime="1.8"),
                   page(category, views="23.8M"), page(category, hours="0"),
                   valid.replace("Movies | English", "Shows | English"),
                   valid.replace("Hours Viewed", "Minutes Viewed"),
                   page(category, latest_week() - timedelta(days=1)),
                   valid.replace('<span class="rank">10</span>', '<span class="rank">09</span>')]
        for document in invalid:
            with self.subTest(document=document[:100]):
                with self.assertRaises(ValueError):
                    artwork.parse_official_board(sources._HTML(document).root, category)

    async def test_inconsistent_four_weeks_preserve_full_cached_week_not_partial_newer_board(self):
        calls = []
        instance = self.make(self.handler(calls))
        first = await instance.feed("netflix")
        snapshot = copy.deepcopy(instance._cache[sources.NETFLIX_SNAPSHOT_KEY][1])
        instance._cache[sources.NETFLIX_SNAPSHOT_KEY] = (0, snapshot)
        instance._cache[("netflix", "all", 1)] = (0, first)
        instance._client._transport = httpx.MockTransport(self.handler(calls, {"TV (English)": {"week": latest_week() - timedelta(days=7)}}))
        instance._netflix_last_check = 0
        result = await instance.refresh("netflix")
        self.assertEqual(result["status"], "stale")
        self.assertEqual(result["source"]["diagnostics"]["cause"], "inconsistent_weeks")
        self.assertEqual([i["id"] for i in result["items"]], [i["id"] for i in first["items"]])
        self.assertIn("旧榜", result["message"])
        self.assertEqual(len(calls), 8)

    async def test_legacy_fresh_tsv_feed_expires_and_migrates_immediately(self):
        calls = []
        instance = self.make(self.handler(calls))
        key = ("netflix", "all", 1)
        old = {"items": [{"id": "legacy", "title": "Legacy dated item"}], "source": {"updated_at": "2026-10-04"}, "status": "ready"}
        instance._cache[key] = (time.monotonic() + 3600, old)
        result = await instance.feed("netflix")
        self.assertEqual((result["status"], result["items"][0]["id"], result["cache"]["refreshing"]), ("stale", "legacy", True))
        await asyncio.gather(*instance._refresh_tasks.values())
        result = await instance.feed("netflix")
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["source"]["schema"], sources.NETFLIX_SCHEMA)
        self.assertEqual(len(calls), 4)

    async def test_refresh_batch_awaits_active_key_and_downloads_only_once(self):
        calls = []
        instance = self.make(self.handler(calls))
        results = await asyncio.gather(*(instance.refresh("netflix") for _ in range(5)))
        self.assertTrue(all(r["status"] == "ready" for r in results))
        await instance.refresh("netflix", "tv")
        await instance.refresh("netflix", "movie")
        await instance.refresh("netflix", "all", 2)
        self.assertEqual(len(calls), 4)
        instance._netflix_last_check = 0
        await instance.refresh("netflix")
        self.assertEqual(len(calls), 8)

    async def test_failures_never_expose_proxy_or_auth_and_incomplete_tsv_cannot_look_current(self):
        def handle(request):
            if str(request.url) == sources.NETFLIX_DATA:
                return httpx.Response(200, text="week\tcategory\tweekly_rank\tshow_title\tweekly_views\tweekly_hours_viewed\truntime\n" +
                    f"{latest_week().isoformat()}\tFilms (English)\t1\tOnly title\t10\t20\t2\n")
            raise RuntimeError("private-user:private-pass@private-proxy Token private-token")
        result = await self.make(handle).feed("netflix")
        self.assertEqual((result["status"], result["items"]), ("unavailable", []))
        self.assertNotIn("private-", json.dumps(result))

    async def test_forced_non_netflix_refresh_deduplicates_and_preserves_last_success(self):
        calls, failing = 0, False
        instance = self.make(lambda request: self.fail("This test uses the adapter protocol"))
        async def adapter(kind, page):
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.01)
            if failing:
                raise RuntimeError("private proxy credentials")
            return {"items": [{"id": "native:1", "title": "Saved title"}], "source": {"updated_at": "today"}}
        instance._iqiyi = adapter
        await instance.feed("iqiyi")
        results = await asyncio.gather(*(instance.refresh("iqiyi") for _ in range(4)))
        self.assertEqual(calls, 2)
        self.assertTrue(all(result["status"] == "ready" for result in results))
        failing = True
        result = await instance.refresh("iqiyi")
        self.assertEqual((result["status"], result["items"][0]["id"]), ("stale", "native:1"))
        self.assertNotIn("credentials", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
