import unittest
import httpx
from parsel import Selector
from movieclaw_tracker.selectors import NexusPHPSelectors
from pt_pack import official_conflict, Settings
from movieclaw_sdk.config_schema import config_schema
from pt_pack.adapters import PackNexusSite, GreatPosterWallSite, ZhuqueSite, safe_url, ensure_torrent
from pt_pack.identity import validate_user_agent, with_identity
from movieclaw_tracker.exceptions import TrackerParseError
from movieclaw_tracker.models import TorrentCategory
from movieclaw_tracker.http import HttpClient
from movieclaw_tracker.models import SearchQuery
from pt_pack.diagnostics import issue_ticket, REPORTS, TICKETS, torrent_metadata

VALID_TORRENT = b"d4:infod6:lengthi1e4:name1:x12:piece lengthi16384e6:pieces20:12345678901234567890ee"


def make(cls, **kwargs):
    return cls(site_id="test", base_url="https://example.org", client=None, auth_manager=None, **kwargs)


class Parsers(unittest.TestCase):
    def test_browser_identity_header_and_legacy_menu_migration(self):
        for value in ("test\r\nCookie: injected", "test\n", "中文", "x" * 513):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_user_agent(value)
        self.assertEqual(validate_user_agent(""), "")
        schema = config_schema(Settings)
        self.assertEqual(schema["properties"], {})
        self.assertEqual(Settings(management_url="legacy", hitpt_user_agent="browser").hitpt_user_agent, "browser")

    def test_builtin_skip_id_and_host(self):
        c = {"site_id": "x", "base_url": "https://www.example.org"}
        self.assertTrue(official_conflict(c, [{"site_id": "x"}]))
        self.assertTrue(official_conflict(c, [{"site_id": "renamed", "base_url": "https://example.org"}]))
        self.assertFalse(official_conflict(c, [{"site_id": "different", "base_url": "https://other.org"}]))

    def test_url_boundary_and_tokens(self):
        for url in ["https://evil.org/dl", "//evil.org/dl", "http://example.org/dl", "https://u:p@example.org/dl", "/dl?usetoken=1", "https://example.org:8443/dl"]:
            with self.assertRaises(TrackerParseError):
                safe_url("https://example.org", url)
        self.assertEqual(safe_url("https://example.org", "\\torrents.php?id=2"), "https://example.org/torrents.php?id=2")

    def test_no_html_torrent(self):
        with self.assertRaises(TrackerParseError):
            ensure_torrent(b"<html>login</html>")
        self.assertEqual(ensure_torrent(VALID_TORRENT), VALID_TORRENT)
        for data in (b"d4:infodee", VALID_TORRENT + b"garbage", b"d4:infoi1ee"):
            with self.assertRaises(TrackerParseError):
                ensure_torrent(data)
        self.assertEqual(torrent_metadata(VALID_TORRENT)["file_count"], 1)
        self.assertEqual(torrent_metadata(VALID_TORRENT)["content_bytes"], 1)

    def test_gpw_groups_not_torrents_or_free_token(self):
        site = make(GreatPosterWallSite)
        html = '''<table id="torrent_table"><tr class="TableTorrent-rowMovieInfo"><td>
        <div class="TableTorrent-movieInfoSubtitle">Sample Film</div>
        <span class="TableTorrent-movieInfoTitle"><a>示例</a></span><span class="TableTorrent-movieInfoYear">(2024)</span>
        </td></tr><tr class="TableTorrent-rowTitle"><td>
        <a href="torrents.php?action=download&amp;id=42&amp;usetoken=1">FL</a>
        <a href="torrents.php?action=download&amp;id=42">DL</a>
        <a href="torrents.php?id=10&amp;torrentid=42#torrent42">1080p / x264</a>
        </td><td>now</td><td>2.5 GiB</td><td>10</td><td>4</td><td>0</td></tr></table>'''
        items, _ = site.parse_list(html)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].torrent_id, "42")
        self.assertEqual(items[0].size_bytes, 2684354560)
        self.assertNotIn("usetoken", items[0].download_url)
        self.assertEqual(items[0].title, "Sample Film 2024 1080p / x264")
        self.assertFalse(items[0].free)

    def test_nexus_nested_cells_and_subtitle(self):
        site = make(PackNexusSite, selectors=NexusPHPSelectors().replace(
            torrent_category_css="td:first-child a[href*='cat=']::attr(href)"),
            category_map={TorrentCategory.TV: ["402"]})
        row = Selector(text='''<table><tr><td><a href="?cat=402">TV</a></td><td><table class="torrentname"><tr>
        <td class="embedded"><a href="details.php?id=9">Sample.2026.S01E01</a><img class="pro_free"/><br/>示例 第一集</td>
        </tr></table></td><td>1</td><td><span title="2026-10-13 11:00:00">now</span></td><td>1 GiB</td><td>7</td><td>2</td><td>3</td><td>u</td></tr></table>''').css("table > tr")[0]
        item = site._parse_torrent_row(row)
        self.assertEqual(item.torrent_id, "9")
        self.assertEqual(item.category, TorrentCategory.TV)
        self.assertEqual(item.size_bytes, 1073741824)
        self.assertEqual(item.subtitle, "示例 第一集")
        self.assertTrue(item.free)

    def test_gpw_empty_search_is_not_expired_session(self):
        site = make(GreatPosterWallSite)
        html = '<a id="header-username-value" href="user.php?id=1">test</a><h2>你的搜索与任何内容都不匹配。</h2>'
        self.assertEqual(site.parse_list(html), ([], False))
        for invalid in ('<h2>你的搜索与任何内容都不匹配。</h2>', '<a id="header-username-value">test</a><h2>服务器错误</h2>'):
            with self.assertRaises(TrackerParseError):
                site.parse_list(invalid)

    def test_zhuque_fields_and_defaults(self):
        site = make(ZhuqueSite)
        item = site.parse_item({"id": 8, "title": "Sample", "size": 123, "seeding": 3,
                                "downloadRate": 0, "uploadRate": 2, "uploadTime": 1700000000}, "example-key")
        self.assertTrue(item.free)
        self.assertEqual(item.seeders, 3)
        self.assertEqual(item.upload_volume_factor, 2)
        self.assertIn("/download/8/example-key", item.download_url)
        self.assertFalse(site.parse_item({"id": 9, "title": "Sample"}).free)
        with self.assertRaises(TrackerParseError):
            site.parse_item({"id": 8})


class HttpScenarios(unittest.IsolatedAsyncioTestCase):
    async def test_browser_identity_applies_to_every_site_and_preserves_cookie(self):
        browser_ua = "Mozilla/5.0 test-browser/155"
        cls = with_identity(PackNexusSite, lambda site_id: browser_ua)
        for site_id in ("hitpt", "btschool"):
            def handler(request):
                self.assertIn("uid=test-only", request.headers["cookie"])
                self.assertEqual(request.headers["user-agent"], browser_ua)
                if request.url.path == "/index.php":
                    return httpx.Response(200, text='<a href="userdetails.php?id=12">test-user</a>')
                return httpx.Response(200, text='<h1>test-user</h1>')
            async with HttpClient(cookies={"uid": "test-only"}, transport=httpx.MockTransport(handler)) as client:
                site = cls(site_id=site_id, base_url="https://example.org", client=client, auth_manager=None,
                    selectors=NexusPHPSelectors().replace(profile_uid_css="a[href*='userdetails.php?id=']::attr(href)", profile_username_css="h1"))
                self.assertEqual((await site.get_user_profile()).user_id, "12")

    async def test_zhuque_csrf_options_search_profile_and_download(self):
        calls = []

        def handler(request):
            calls.append(request.url.path)
            if request.url.path == "/index":
                return httpx.Response(200, text='<meta name="x-csrf-token" content="test-only">')
            if request.url.path == "/api/torrent/download/8/test-key":
                return httpx.Response(200, content=VALID_TORRENT)
            self.assertEqual(request.headers["x-csrf-token"], "test-only")
            if request.url.path == "/api/torrent/option":
                data = {"option": [{"id": 501, "name": "电影"}, {"id": 502, "name": "剧集"}]}
            elif request.url.path == "/api/torrent/advancedSearch":
                import json
                payload = json.loads(request.content)
                self.assertEqual(payload["type"], "title")
                self.assertEqual(payload["category"], [501])
                self.assertEqual(payload["keyword"], "Sample")
                data = {"torrents": [{"id": 8, "title": "Sample", "category": 501}], "total": 21, "torrentKey": "test-key"}
            elif request.url.path == "/api/user/getInfo":
                data = {"id": 1, "username": "test-user", "upload": 200, "download": 100}
            elif request.url.path == "/api/torrent/info":
                data = {"torrent": {"id": 8, "title": "Sample", "category": 501,
                                   "torrentKey": "test-key", "mediainfo": "General\nFile size: 1 B", "note": "Release note"},
                        "content": {"overview": "Film overview"}}
            else:
                self.fail(str(request.url))
            return httpx.Response(200, json={"status": 200, "data": data})

        async with HttpClient(transport=httpx.MockTransport(handler)) as client:
            site = ZhuqueSite(site_id="test", base_url="https://example.org", client=client, auth_manager=None)
            result = await site.search(SearchQuery(keyword="Sample", categories=[TorrentCategory.MOVIE]))
            self.assertEqual(result.total_pages, 2)
            self.assertTrue(result.has_more)
            self.assertEqual(result.items[0].category, TorrentCategory.MOVIE)
            self.assertTrue(await site.check_auth())
            self.assertEqual((await site.get_user_profile()).ratio, 2)
            detail = await site.get_torrent_detail(result.items[0].detail_url)
            self.assertEqual(detail.category, TorrentCategory.MOVIE)
            self.assertIn("Film overview", detail.description)
            self.assertIn("General", detail.description)
            self.assertIn("/download/8/test-key", detail.download_url)
            await site.download_torrent(result.items[0].download_url)
        self.assertEqual(calls.count("/index"), 1)

    async def test_nexus_profile_resolves_current_uid(self):
        def handler(request):
            if request.url.path == "/index.php":
                return httpx.Response(200, text='<a href="userdetails.php?id=12">test-user</a>')
            self.assertEqual(request.url.params["id"], "12")
            return httpx.Response(200, text='<h1><font size="4"><span><b>test-user</b></span></font></h1>')
        async with HttpClient(transport=httpx.MockTransport(handler)) as client:
            site = PackNexusSite(site_id="test", base_url="https://example.org", client=client, auth_manager=None,
                selectors=NexusPHPSelectors().replace(profile_uid_css="a[href*='userdetails.php?id=']::attr(href)", profile_username_css="h1"))
            self.assertEqual((await site.get_user_profile()).user_id, "12")
            self.assertEqual((await site.get_user_profile()).username, "test-user")

    async def test_expired_session_not_reported_as_authenticated(self):
        async with HttpClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, text='<form><input type="password"></form>'))) as client:
            site = ZhuqueSite(site_id="test", base_url="https://example.org", client=client, auth_manager=None)
            self.assertFalse(await site.check_auth())

    async def test_one_shot_probe_search_detail_and_file_only(self):
        calls = []
        def handler(request):
            calls.append(request.url.path)
            if request.url.path == "/torrents.php":
                self.assertEqual(request.url.params["search"], "Sample")
                return httpx.Response(200, text='''<table class="torrents"><tr><th>header</th></tr><tr><td>cat</td><td><a href="details.php?id=9">Sample</a><a href="download.php?id=9">DL</a></td><td>1</td><td>now</td><td>1 B</td><td>1</td><td>0</td><td>1</td><td>u</td></tr></table>''')
            if request.url.path == "/details.php":
                return httpx.Response(200, text='<h1 id="top">Sample</h1><div id="kdescr">Description</div><a href="download.php?id=9">download</a>')
            self.assertEqual(request.url.path, "/download.php")
            return httpx.Response(200, content=VALID_TORRENT)
        async with HttpClient(transport=httpx.MockTransport(handler)) as client:
            site = PackNexusSite(site_id="test", base_url="https://example.org", client=client, auth_manager=None, selectors=NexusPHPSelectors())
            token, marker = issue_ticket("test", "Sample")
            result = await site.search(SearchQuery(keyword=marker))
            self.assertEqual(len(result.items), 1)
            self.assertTrue(all(REPORTS["test"][x]["ok"] for x in ("search", "detail", "torrent")))
            self.assertEqual(REPORTS["test"]["download_tasks_created"], 0)
            self.assertNotIn(token, TICKETS)
            self.assertNotIn("download_url", REPORTS["test"]["detail"])
            self.assertNotIn("https://", str(REPORTS["test"]))
            with self.assertRaises(TrackerParseError):
                await site.search(SearchQuery(keyword=marker))
        self.assertEqual(calls, ["/torrents.php", "/details.php", "/download.php"])

    async def test_gpw_live_shaped_detail_without_h2_or_detail_href(self):
        def handler(request):
            return httpx.Response(200, text='''<div id="movie-intro"><span class="MovieInfo-title">示例</span><span class="MovieInfo-year">(2024)</span><div class="MovieInfo-subTitle">Sample Film</div><a href="https://imdb.com/title/tt1234567">IMDB</a></div><table><tr id="torrent42"><td><span class="TorrentTitle">1080p / x264 <span class="torrent_discount free">FREE</span></span><a href="torrents.php?action=download&amp;id=42">DL</a></td><td class="TableTorrent-cellSize">2 GiB</td><td class="TableTorrent-cellSeeders">3</td></tr><tr id="torrent_detail_42"><td><div class="is-description">release description</div></td></tr></table>''')
        async with HttpClient(transport=httpx.MockTransport(handler)) as client:
            site = GreatPosterWallSite(site_id="test", base_url="https://example.org", client=client, auth_manager=None)
            detail = await site.get_torrent_detail("torrents.php?id=10&torrentid=42")
            self.assertEqual(detail.title, "Sample Film 2024 1080p / x264 FREE")
            self.assertEqual(detail.description, "release description")
            self.assertEqual(detail.size_bytes, 2147483648)
            self.assertTrue(detail.free)
            self.assertEqual(detail.imdb_id, "tt1234567")

    async def test_nexus_basic_info_nested_size_and_subtitle(self):
        async with HttpClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, text='''<h1 id="top">Sample</h1><table><tr><td class="rowhead">副标题</td><td><b>Subtitle</b></td></tr><tr><td class="rowhead">基本信息</td><td>大小：<b>2 GiB</b> 类型：电影</td></tr></table><a href="download.php?id=9">DL</a>'''))) as client:
            site = PackNexusSite(site_id="test", base_url="https://example.org", client=client, auth_manager=None,
                selectors=NexusPHPSelectors().replace(detail_size_css="td.rowhead:contains('基本信息') + td", detail_subtitle_css="td.rowhead:contains('副标题') + td"))
            detail = await site.get_torrent_detail("details.php?id=9")
            self.assertEqual(detail.size, "2 GiB")
            self.assertEqual(detail.size_bytes, 2147483648)
            self.assertEqual(detail.subtitle, "Subtitle")


if __name__ == "__main__":
    unittest.main()
