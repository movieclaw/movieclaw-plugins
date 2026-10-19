"""Historical-week alignment, safe diagnostics and public artwork restoration."""
import asyncio
import copy
import importlib.util
import json
import sys
import time
import unittest
from datetime import date
from pathlib import Path

import httpx


spec = importlib.util.spec_from_file_location("netflix_artwork_history_tests", Path(__file__).parents[1] / "platform_discovery" / "netflix_artwork.py")
artwork = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = artwork
spec.loader.exec_module(artwork)


class Node:
    """The helper's HTML-tree protocol; real HTML integration has source tests."""
    def __init__(self, tag="div", attrs=None, children=None):
        self.tag, self.attrs, self.children = tag, attrs or {}, children or []

    def nodes(self):
        yield self
        for child in self.children:
            if isinstance(child, Node):
                yield from child.nodes()

    def text(self):
        return "".join(child.text() if isinstance(child, Node) else child for child in self.children)


def page(week, titles):
    finish = date.fromisoformat(week).strftime("%m/%d/%y")
    nodes = [Node("a", {"data-uia": "top10-filters-week-select-option", "aria-selected": "true"}, ["9/28/26 - " + finish])]
    for index, title in enumerate(titles, 1):
        nodes.append(Node("div", {"data-uia": "top10-card", "style": f"background-image:url(https://dnm.nflximg.net/work-{index}.jpg)"}, [
            Node("div", {"data-uia": "top10-card-logo"}, [Node("img", {"alt": title, "src": "https://dnm.nflximg.net/title-logo.png"})]),
            Node("a", {"data-uia": "play-on-netflix-link", "href": f"https://www.netflix.com/watch/{10000000 + index}?trackId=1"}),
        ]))
        nodes.append(Node("td", {"data-uia": "top10-table-row-title"}, [
            Node("img", {"src": f"https://dnm.nflximg.net/thumbnail-{index}.jpg"}), Node("button", children=[title]),
        ]))
    return Node(children=nodes)


def item(title, category="Films (English)", season="N/A"):
    return {"title": title, "media_type": "movie" if category.startswith("Films") else "tv", "poster_url": None,
            "backdrop_url": None, "detail_ref": None, "metadata": {"rank_scope": category, "season_title": season}}


class NetflixArtworkHistoryTests(unittest.IsolatedAsyncioTestCase):
    def make(self, fetch, cache=None, json_fetch=None):
        async def no_tmdb(*args, **kwargs):
            raise AssertionError("Official fallback does not need a TMDB token")
        def tmdb_item(platform, row, media_type):
            return {"poster_url": "https://image.tmdb.org/t/p/w500" + row["poster_path"], "detail_ref": f"tmdb:{media_type}:{row['id']}"}
        return artwork.NetflixArtwork(fetch, json_fetch or no_tmdb, tmdb_item, lambda tree: tree,
                                       {} if cache is None else cache, {}, 3600, 3, "https://api.themoviedb.org/3")

    async def test_known_missing_titles_fetch_exact_historical_week_on_fixed_routes(self):
        calls = []
        movie_titles = ["UNABOMBER", "Doing Life", "The Breadwinner"]
        tv_titles = ["East of Eden: Limited Series", "A Different World: Season 1"]
        async def fetch(url, params=None):
            calls.append((url, params))
            self.assertIn(url, artwork.OFFICIAL_PAGES.values())
            self.assertEqual(params, {"week": "2026-10-04"})
            # The default page is now 10/11; the verified public week query
            # retrieves these actual titles from the 10/04 chart instead.
            return page("2026-10-04", tv_titles if url.endswith("/tv") else movie_titles)
        resolver = self.make(fetch)
        items = [item(title) for title in movie_titles] + [item("East of Eden", "TV (English)", tv_titles[0]), item("A Different World", "TV (English)", tv_titles[1])]
        summary = await resolver.official_items(items, "2026-10-04")
        self.assertEqual((summary["state"], summary["matched"], summary["missing"]), ("ready", 5, 0))
        self.assertEqual(summary["official_week"], "2026-10-04")
        self.assertEqual(len(calls), 2)
        for result in items:
            self.assertEqual(result["metadata"]["poster_source"], "Netflix Tudum")
            self.assertEqual(result["metadata"]["poster_aspect"], "landscape")
            self.assertTrue(result["poster_fallback_urls"])
            self.assertNotIn("logo.png", result["poster_url"])

    async def test_current_week_cannot_substitute_old_week_even_with_same_title(self):
        async def fetch(url, params=None):
            return page("2026-10-11", ["UNABOMBER"])
        target = item("UNABOMBER")
        summary = await self.make(fetch).official_items([target], "2026-10-04")
        self.assertIsNone(target["poster_url"])
        self.assertEqual(summary["official_week"], "2026-10-11")
        self.assertEqual(summary["expected_week"], "2026-10-04")
        self.assertEqual(target["metadata"]["netflix_artwork"]["state"], "week_mismatch")

    async def test_same_week_different_season_or_episode_is_not_matched(self):
        async def fetch(url, params=None):
            return page("2026-10-04", ["A Different World: Season 2", "Raw: 2026 - September 29, 2026"])
        targets = [item("A Different World", "TV (English)", "A Different World: Season 1"), item("Raw", "TV (English)", "Raw: September 28, 2026")]
        summary = await self.make(fetch).official_items(targets, "2026-10-04")
        self.assertEqual((summary["matched"], summary["missing"]), (0, 2))
        self.assertTrue(all(target["poster_url"] is None for target in targets))
        self.assertTrue(all(target["metadata"]["netflix_artwork"]["state"] == "missing_title" for target in targets))

    async def test_http_denial_and_proxy_failures_have_distinct_safe_diagnostics(self):
        secret = "http://private-user:private-password@127.0.0.1:7890"
        for denied in (True, False):
            with self.subTest(denied=denied):
                async def fetch(url, params=None):
                    if denied:
                        response = httpx.Response(403, request=httpx.Request("GET", url))
                        raise httpx.HTTPStatusError(secret, request=response.request, response=response)
                    raise ValueError("private proxy diagnostic " + secret)
                resolver = self.make(fetch)
                target = item("UNABOMBER")
                summary = await resolver.official_items([target], "2026-10-04")
                expected = "upstream_denied" if denied else "request_error"
                self.assertEqual(target["metadata"]["netflix_artwork"]["state"], expected)
                for result in (summary, target, list(resolver.cache.values())):
                    rendered = json.dumps(result)
                    for private in ("private-user", "private-password", "127.0.0.1", secret):
                        self.assertNotIn(private, rendered)

    async def test_unrecognized_html_format_is_reported_without_raw_content(self):
        async def fetch(url, params=None):
            return Node(children=["private upstream response text"])
        target = item("Doing Life")
        summary = await self.make(fetch).official_items([target], "2026-10-04")
        self.assertEqual(target["metadata"]["netflix_artwork"]["state"], "parse_error")
        self.assertNotIn("private upstream", json.dumps(summary))

    async def test_usable_artwork_restores_from_shared_dict_after_resolver_restart(self):
        async def fetch(url, params=None):
            return page("2026-10-04", ["The Breadwinner"])
        cache = {}
        first = item("The Breadwinner")
        await self.make(fetch, cache).official_items([first], "2026-10-04")
        restored = copy.deepcopy(cache)
        async def must_not_fetch(*args, **kwargs):
            self.fail("Restored, fresh public artwork must not require network access")
        second = item("The Breadwinner")
        summary = await self.make(must_not_fetch, restored).official_items([second], "2026-10-04")
        self.assertEqual(second["poster_url"], first["poster_url"])
        self.assertEqual(summary["matched"], 1)

    async def test_stale_verified_week_cache_survives_failures_with_retry_backoff(self):
        calls, failing = 0, False
        async def fetch(url, params=None):
            nonlocal calls
            calls += 1
            if failing:
                raise httpx.ReadTimeout("not public")
            return page("2026-10-04", ["UNABOMBER"])
        cache = {}
        resolver = self.make(fetch, cache)
        first = item("UNABOMBER")
        await resolver.official_items([first], "2026-10-04")
        key = ("netflix-artwork", "Films (English)", "2026-10-04")
        cache[key] = (time.monotonic() - 1, cache[key][1])
        failing = True
        second = item("UNABOMBER")
        summary = await resolver.official_items([second], "2026-10-04")
        state = second["metadata"]["netflix_artwork"]
        self.assertEqual((state["state"], state["cause"], state["image_week"]), ("stale_cache", "timeout", "2026-10-04"))
        self.assertEqual(second["poster_url"], first["poster_url"])
        self.assertEqual(summary["state"], "partial")
        await resolver.official_items([item("UNABOMBER")], "2026-10-04")
        self.assertEqual(calls, 2)

    async def test_reliable_tmdb_poster_stays_primary_when_official_backup_arrives(self):
        async def fetch(url, params=None):
            return page("2026-10-04", ["Doing Life"])
        target = item("Doing Life")
        target.update(poster_url="https://image.tmdb.org/t/p/w500/valid.jpg", detail_ref="tmdb:movie:123")
        target["metadata"].update(poster_source="TMDB", poster_aspect="portrait")
        await self.make(fetch).official_items([target], "2026-10-04")
        self.assertEqual(target["poster_url"], "https://image.tmdb.org/t/p/w500/valid.jpg")
        self.assertEqual(target["backdrop_url"], "https://dnm.nflximg.net/work-1.jpg")
        self.assertEqual(target["metadata"]["poster_source"], "TMDB")
        self.assertEqual(len(target["poster_fallback_urls"]), 2)

    async def test_invalid_week_does_not_make_requests_or_echo_input(self):
        async def must_not_fetch(*args, **kwargs):
            self.fail("An invalid date must not become a request parameter")
        target = item("UNABOMBER")
        summary = await self.make(must_not_fetch).official_items([target], "2026-10-04&token=private-token")
        self.assertEqual(target["metadata"]["netflix_artwork"]["state"], "invalid_week")
        self.assertNotIn("private-token", json.dumps(summary))


if __name__ == "__main__":
    unittest.main()
