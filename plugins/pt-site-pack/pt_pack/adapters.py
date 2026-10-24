"""Site-specific adapters using MovieClaw's authenticated, rate-limited client."""
import math
import re
from datetime import datetime, timezone
from urllib.parse import parse_qs, urljoin, urlparse

from parsel import Selector
from movieclaw_tracker.base import BaseSite
from movieclaw_tracker.exceptions import TrackerAuthError, TrackerParseError
from movieclaw_tracker.frameworks.nexusphp import NexusPHPSite
from movieclaw_tracker.models import (
    TorrentCategory, TorrentListItem, TorrentListPage, TorrentDetail,
    SearchQuery, SearchResult, UserProfile,
)
from .diagnostics import ProbeMixin, torrent_metadata
from .profiles import AUTH_CHECK


def text(node, css):
    return " ".join(" ".join(node.css(css).xpath("string(.)").getall()).split())


def integer(value):
    return int(re.sub(r"[^\d]", "", str(value)) or 0)


def size_bytes(value):
    m = re.search(r"([\d.,]+)\s*([KMGTPE]i?B|B)\b", str(value), re.I)
    if not m:
        return 0
    power = "BKMGTPE".index(m[2][0].upper())
    return int(float(m[1].replace(",", "")) * 1024 ** power)


def safe_url(base, value):
    url = urljoin(base + "/", value.replace("\\", "/"))
    a, b = urlparse(base), urlparse(url)
    if b.scheme != "https" or b.hostname != a.hostname or (b.port or 443) != (a.port or 443) or b.username or b.password:
        raise TrackerParseError("站点返回了跨域或不安全链接")
    if "usetoken" in parse_qs(b.query):
        raise TrackerParseError("不自动使用免费令牌")
    return url


def ensure_torrent(data):
    torrent_metadata(data)
    return data


class PackNexusSite(ProbeMixin, NexusPHPSite):
    """YAML owns the selectors; only shared uid/subtitle normalization is code."""
    async def _search_plain(self, query):
        return await NexusPHPSite.search(self, query)

    async def get_user_profile(self, user_id=None):
        if not user_id:
            response = await self.client.get(self._url("index.php"))
            doc = Selector(text=response.text)
            href = doc.css(self.selectors.profile_uid_css).get()
            match = re.search(r"[?&]id=(\d+)", href or "")
            if not match:
                raise TrackerAuthError("站点会话失效，请通过扩展重新同步 Cookie")
            user_id = match[1]
        return await super().get_user_profile(user_id)

    def _parse_torrent_row(self, row):
        item = super()._parse_torrent_row(row)
        if item:
            cells = row.css("td.embedded").xpath("./a[contains(@href,'details.php')]/following-sibling::br/following-sibling::text()")
            subtitle = " ".join(x.strip() for x in cells.getall() if x.strip())
            if subtitle:
                item.subtitle = subtitle
        return item

    async def check_auth(self):
        try:
            await self.get_user_profile()
            return True
        except TrackerAuthError:
            return False

    async def get_torrent_detail(self, url):
        detail = await super().get_torrent_detail(safe_url(self.base_url, url))
        if detail.size:
            match = re.search(r"[\d.,]+\s*[KMGTPE]i?B\b", detail.size, re.I)
            if match:
                detail.size = match[0]
                detail.size_bytes = size_bytes(detail.size)
        if detail.download_url:
            detail.download_url = safe_url(self.base_url, detail.download_url)
        return detail

    async def download_torrent(self, url):
        return ensure_torrent(await self.client.download(safe_url(self.base_url, url)))


class GreatPosterWallSite(ProbeMixin, BaseSite):
    def __init__(self, category_map=None, **kwargs):
        super().__init__(**kwargs)

    def _upload_time(self, row):
        """Read Gazelle's exact tooltip, not the rendered relative time."""
        cells = row.css(".TableTorrent-cellStatTime")
        if not cells: return None
        cell = cells[0]
        values = [cell.attrib.get(k, "") for k in ("data-tooltip", "title", "datetime")]
        values += cell.css("[data-tooltip]::attr(data-tooltip), [title]::attr(title), time[datetime]::attr(datetime)").getall()
        values.append(cell.xpath("string(.)").get("").strip())
        months = dict(zip("Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split(), range(1, 13)))
        for raw in values:
            # Explicit English months avoid depending on the server's locale.
            match = re.fullmatch(r"([A-Za-z]{3})\s+(\d{1,2})\s+(\d{4}),\s+(\d{1,2}):(\d{2})(?::(\d{2}))?", raw.strip())
            try:
                if match:
                    parsed = datetime(int(match[3]), months[match[1].title()], int(match[2]), int(match[4]), int(match[5]), int(match[6] or 0))
                elif re.fullmatch(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:\d{2})?", raw.strip()):
                    parsed = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
                else: continue
                return self._to_utc(parsed)
            except (KeyError, ValueError): continue
        return None

    def parse_list(self, html):
        doc = Selector(text=html)
        if not doc.css("#torrent_table"):
            if doc.css("input[type='password']"):
                raise TrackerAuthError("海豹会话失效，请重新同步 Cookie")
            # A legitimate empty search omits the table entirely. Require both
            # its explicit empty-state text and the logged-in account header.
            if text(doc, "h2") == "你的搜索与任何内容都不匹配。" and doc.css("#header-username-value"):
                return [], False
            raise TrackerParseError("海豹种子表结构未识别")
        items, group = [], None
        for row in doc.css("#torrent_table > tr, #torrent_table > tbody > tr"):
            if "TableTorrent-rowMovieInfo" in row.attrib.get("class", ""):
                group = row
                continue
            if not group or "TableTorrent-rowTitle" not in row.attrib.get("class", ""):
                continue
            hrefs = row.css("a::attr(href)").getall()
            dl = next((h for h in hrefs if "action=download" in h and "usetoken" not in h), None)
            detail = next((h for h in hrefs if "torrentid=" in h), None)
            if not dl or not detail:
                continue
            tid = parse_qs(urlparse(dl).query).get("id", [""])[0]
            cells = row.xpath("./td")
            if not tid.isdigit() or len(cells) != 6:
                raise TrackerParseError("海豹种子行结构发生变化")
            english = text(group, ".TableTorrent-movieInfoSubtitle")
            chinese = text(group, ".TableTorrent-movieInfoTitle a")
            year = text(group, ".TableTorrent-movieInfoYear").strip("() ")
            specs = text(row, "a[href*='torrentid=']")
            free = bool(row.css(".freeleech, .torrent_freeleech, .torrent_discount.free"))
            size = cells[2].xpath("string(.)").get().strip()
            items.append(TorrentListItem(
                torrent_id=tid, title=f"{english or chinese} {year} {specs}".strip(),
                subtitle=chinese, category=TorrentCategory.MOVIE, site_category_id="movie",
                size=size, size_bytes=size_bytes(size), upload_time=self._upload_time(row), snatched=integer(text(row, "td:nth-child(4)")),
                seeders=integer(text(row, "td:nth-child(5)")), leechers=integer(text(row, "td:nth-child(6)")),
                free=free, download_volume_factor=0.0 if free else 1.0,
                detail_url=safe_url(self.base_url, detail), download_url=safe_url(self.base_url, dl),
            ))
        next_page = bool(doc.css("a[rel='next'], a#next_page"))
        # None rather than false when markup provides no authoritative next-page signal.
        return items, True if next_page else None

    async def _list(self, keyword, page, categories=None):
        if categories and TorrentCategory.MOVIE not in categories:
            return [], False
        response = await self.client.get(self._url("torrents.php"),
                                         params={"searchstr": keyword, "page": page, "group_results": "1"})
        return self.parse_list(response.text)

    async def list_torrents(self, *, categories=None, page=1):
        items, more = await self._list("", page, categories)
        return TorrentListPage(items=items, page=page, has_more=more)

    async def _search_plain(self, query):
        items, more = await self._list(query.keyword, query.page, query.categories)
        return SearchResult(items=items, page=query.page, has_more=more)

    async def get_torrent_detail(self, url):
        target = safe_url(self.base_url, url)
        tid = parse_qs(urlparse(target).query).get("torrentid", [""])[0]
        if not tid.isdigit():
            raise TrackerParseError("详情地址缺少 torrentid，不能使用影片分组 ID 代替种子 ID")
        response = await self.client.get(target)
        doc = Selector(text=response.text)
        link = next((h for h in doc.css("a::attr(href)").getall()
                     if "action=download" in h and "usetoken" not in h
                     and parse_qs(urlparse(h).query).get("id") == [tid]), None)
        if not link:
            raise TrackerAuthError("未找到此种子的普通下载链接，请检查海豹会话")
        row = doc.css("#torrent" + tid)
        english, chinese = text(doc, ".MovieInfo-subTitle"), text(doc, ".MovieInfo-title")
        year = text(doc, ".MovieInfo-year").strip("() ")
        titles = row.css(".TorrentTitle")
        # Desktop/mobile copies coexist in the HTML; don't concatenate both.
        specs = " ".join(titles[0].xpath("string(.)").get("").split()) if titles else text(row, "a[href*='torrentid=']")
        title = f"{english or chinese} {year} {specs}".strip()
        if not row or not (english or chinese) or not specs:
            raise TrackerParseError("海豹详情标题未识别")
        size = text(row, ".TableTorrent-cellSize")
        free = bool(row.css(".freeleech, .torrent_freeleech, .torrent_discount.free"))
        imdb = doc.css("#movie-intro a[href*='imdb.com/title/']::attr(href)").get("")
        douban = doc.css("#movie-intro a[href*='douban.com/subject/']::attr(href)").get("")
        imdb_id = re.search(r"tt\d+", imdb)
        douban_id = re.search(r"/subject/(\d+)", douban)
        return TorrentDetail(torrent_id=tid, title=title, subtitle=chinese, category=TorrentCategory.MOVIE,
                             size=size, size_bytes=size_bytes(size),
                             seeders=integer(text(row, ".TableTorrent-cellSeeders")),
                             leechers=integer(text(row, ".TableTorrent-cellLeechers")),
                             snatched=integer(text(row, ".TableTorrent-cellSnatches")),
                             free=free, download_volume_factor=0.0 if free else 1.0,
                             imdb_id=imdb_id[0] if imdb_id else None,
                             douban_id=douban_id[1] if douban_id else None,
                             description=text(doc, "#torrent_detail" + "_" + tid + " .is-description"),
                             download_url=safe_url(self.base_url, link))

    async def download_torrent(self, url):
        return ensure_torrent(await self.client.download(safe_url(self.base_url, url)))

    async def get_user_profile(self, user_id=None):
        response = await self.client.get(self._url("index.php"))
        doc = Selector(text=response.text)
        links = doc.css("a#header-username-value[href*='user.php?id=']") or doc.css("a[href^='user.php?id=']")
        if not links:
            raise TrackerAuthError("海豹未登录，请同步 Cookie")
        own = links[0]
        uid = parse_qs(urlparse(own.attrib["href"]).query).get("id", [""])[0]
        if user_id and user_id != uid:
            raise TrackerParseError("此适配器只读取当前登录用户资料")
        result = UserProfile(user_id=uid, username=own.xpath("string(.)").get().strip())
        # The Gazelle user API separates personal community activity from the
        # whole-site peers shown on index.php. Never read keys or credentials
        # from the returned profile into UserProfile.
        if uid.isdigit() and not AUTH_CHECK.get():
            try:
                await self.client.get(self._url("ajax.php"),params={"action":"user","id":uid})
            except Exception: pass
        return result

    async def check_auth(self):
        try:
            await self.get_user_profile()
            return True
        except TrackerAuthError:
            return False


class ZhuqueSite(ProbeMixin, BaseSite):
    def __init__(self, category_map=None, **kwargs):
        super().__init__(**kwargs)
        self._csrf = None
        self._category_names = None

    async def _categories(self):
        if self._category_names is None:
            raw = await self._api("api/torrent/option")
            if not isinstance(raw, dict) or not isinstance(raw.get("option"), list):
                raise TrackerParseError("朱雀分类选项结构发生变化")
            self._category_names = {str(o["id"]): o["name"] for o in raw["option"]
                                    if 500 < int(o["id"]) < 600}
        return self._category_names

    @staticmethod
    def category_for(name):
        for category, names in (
            (TorrentCategory.DOCUMENTARY, ("纪录", "documentary")),
            (TorrentCategory.ANIME, ("动漫", "动画", "anime", "animation")),
            (TorrentCategory.TV, ("剧集", "电视剧", "综艺", "tv")),
            (TorrentCategory.MOVIE, ("电影", "movie")),
            (TorrentCategory.MUSIC, ("音乐", "music")),
            (TorrentCategory.GAME, ("游戏", "game")),
        ):
            if any(n in name.lower() for n in names):
                return category
        return None

    async def _api(self, path, *, payload=None, params=None):
        if self._csrf is None:
            page = await self.client.get(self._url("index"))
            self._csrf = Selector(text=page.text).css("meta[name='x-csrf-token']::attr(content)").get()
            if not self._csrf:
                raise TrackerAuthError("朱雀未提供会话 CSRF 标记，请重新同步 Cookie")
        url = safe_url(self.base_url, path)
        kwargs = {"headers": {"x-csrf-token": self._csrf}}
        response = (await self.client.post(url, json=payload, **kwargs) if payload is not None
                    else await self.client.get(url, params=params, **kwargs))
        try:
            raw = response.json()
        except ValueError:
            raise TrackerParseError("朱雀 API 返回了非 JSON 响应") from None
        if not isinstance(raw, dict) or raw.get("status") != 200:
            if isinstance(raw, dict) and (raw.get("status") in (401, 403) or
                                        raw.get("code") in ("NOT_LOGGED_IN", "USER_NOT_LOGIN")):
                self._csrf = None
                raise TrackerAuthError("朱雀会话过期")
            raise TrackerParseError("朱雀 API 未返回成功状态，请检查站点维护或会话")
        return raw.get("data")

    def parse_item(self, raw, key=""):
        if not isinstance(raw, dict) or not raw.get("id") or not raw.get("title"):
            raise TrackerParseError("朱雀种子缺少 id/title")
        tid = str(raw["id"])
        rate = float(raw.get("downloadRate", 1))
        if rate < 0 or not math.isfinite(rate):
            raise TrackerParseError("朱雀下载系数无效")
        uploaded = raw.get("uploadTime", raw.get("upload_time"))
        time = datetime.fromtimestamp(float(uploaded), timezone.utc).replace(tzinfo=None) if uploaded else None
        key = raw.get("torrentKey") or key
        category_id = str(raw["category"]) if raw.get("category") else None
        category_name = (self._category_names or {}).get(category_id, "")
        return TorrentListItem(
            torrent_id=tid, title=raw["title"], subtitle=raw.get("subtitle") or "",
            site_category_id=category_id, site_category_name=category_name or None,
            category=self.category_for(category_name),
            size_bytes=int(raw.get("size", 0)), seeders=int(raw.get("seeding", 0)),
            leechers=int(raw.get("leeching", 0)), snatched=int(raw.get("complete", 0)),
            upload_time=time, free=rate == 0, download_volume_factor=rate,
            upload_volume_factor=float(raw.get("uploadRate", 1)),
            detail_url=self._url("torrent/info/" + tid),
            download_url=self._url(f"api/torrent/download/{tid}/{key}") if key else None,
        )

    async def _search(self, keyword, page, categories=None):
        # Site categories are obtained from its options API, never guessed numeric IDs.
        mapping = await self._categories()
        payload = {"page": page, "size": 20, "keyword": keyword, "type": "title", "order": "",
                   "tags": [], "category": [], "medium": [], "videoCoding": [], "resolution": [],
                   "connStatus": 0, "downloadStatus": 0, "more": False}
        if categories:
            payload["category"] = [int(cid) for cid, name in mapping.items()
                                   if self.category_for(name) in categories]
            if not payload["category"]:
                raise TrackerParseError("朱雀没有可验证的所选分类映射，请取消分类过滤")
        raw = await self._api("api/torrent/advancedSearch", payload=payload)
        if not isinstance(raw, dict) or not isinstance(raw.get("torrents"), list):
            raise TrackerParseError("朱雀搜索结构发生变化")
        items = [self.parse_item(x, raw.get("torrentKey", "")) for x in raw["torrents"]]
        total = int(raw["total"]) if raw.get("total") is not None else None
        return items, total

    async def list_torrents(self, *, categories=None, page=1):
        items, total = await self._search("", page, categories)
        return TorrentListPage(items=items, page=page,
            total_pages=math.ceil(total / 20) if total is not None else None,
            has_more=page * 20 < total if total is not None else None)

    async def _search_plain(self, query):
        items, total = await self._search(query.keyword, query.page, query.categories)
        return SearchResult(items=items, page=query.page, total_results=total,
            total_pages=math.ceil(total / 20) if total is not None else None,
            has_more=query.page * 20 < total if total is not None else None)

    async def get_torrent_detail(self, url):
        target = safe_url(self.base_url, url)
        m = re.search(r"/torrent/info/(\d+)$", urlparse(target).path)
        if not m:
            raise TrackerParseError("朱雀详情地址不正确")
        await self._categories()
        raw = await self._api("api/torrent/info", params={"id": m[1]})
        if not isinstance(raw, dict) or not isinstance(raw.get("torrent"), dict):
            raise TrackerParseError("朱雀详情结构发生变化")
        item = self.parse_item(raw["torrent"])
        if item.torrent_id != m[1]:
            raise TrackerParseError("朱雀详情返回了不同种子 ID")
        fields = item.model_dump()
        for field in ("site_category_id", "site_category_name", "detail_url", "hit_and_run"):
            fields.pop(field, None)
        content = raw.get("content") if isinstance(raw.get("content"), dict) else {}
        torrent = raw["torrent"]
        description = "\n\n".join(v for v in (content.get("overview"), torrent.get("description"),
                                    torrent.get("note"), torrent.get("mediainfo")) if isinstance(v, str) and v.strip())
        return TorrentDetail(**fields, description=description)

    async def download_torrent(self, url):
        target = safe_url(self.base_url, url)
        if not re.fullmatch(r"/api/torrent/download/\d+/[A-Za-z0-9_-]+", urlparse(target).path):
            raise TrackerParseError("朱雀下载地址不正确")
        return ensure_torrent(await self.client.download(target))

    async def get_user_profile(self, user_id=None):
        raw = await self._api("api/user/getInfo", params={"id": user_id} if user_id else {})
        if not isinstance(raw, dict) or not raw.get("id") or not raw.get("username"):
            raise TrackerAuthError("朱雀未返回登录用户资料")
        up, down = int(raw.get("upload", 0)), int(raw.get("download", 0))
        return UserProfile(user_id=str(raw["id"]), username=raw["username"],
            user_class=(raw.get("class") or {}).get("name", ""), uploaded_bytes=up,
            downloaded_bytes=down, ratio=up/down if down else None, bonus=raw.get("bonus"),
            seeding_count=int(raw.get("seeding", 0)), leeching_count=int(raw.get("leeching", 0)))

    async def check_auth(self):
        try:
            await self.get_user_profile()
            return True
        except TrackerAuthError:
            return False
