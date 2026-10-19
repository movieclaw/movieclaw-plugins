"""Read-only discovery sources. Public pages are parsed as data, never executed."""
from __future__ import annotations

import asyncio
import copy
import csv
import hashlib
import io
import json
import math
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from urllib.parse import urlencode, urlsplit
from .preferences import migrate_platform_settings
from .netflix_artwork import NetflixArtwork, OFFICIAL_PAGES, parse_official_board
from .domestic_sources import (
    BILIBILI_RANK_API, BILIBILI_TYPES, KIND_NAMES, MANGO_CHANNEL_BASE,
    MANGO_CHANNELS, MANGO_PAGES, DomesticSourceError,
    parse_bilibili_rank, parse_mango_channel,
)
from .cache import PersistentCache

import httpx


PLATFORMS = [
    {"id": "iqiyi", "name": "爱奇艺", "wordmark": "iQIYI", "color": "#00cc5b", "scope": "domestic", "description": "风云榜 · 实时热度", "url": "https://www.iqiyi.com/ranks1/-1/0", "kinds": ["all", "tv", "variety", "movie"], "source_label": "官方风云榜"},
    {"id": "tencent", "name": "腾讯视频", "wordmark": "腾讯视频", "color": "#ff9d00", "scope": "domestic", "description": "官方热搜榜 · 按搜索热度排行", "url": "https://v.qq.com/biu/ranks/", "kinds": ["all", "tv", "variety", "movie"], "source_label": "官方热搜榜"},
    {"id": "youku", "name": "优酷", "wordmark": "YOUKU", "color": "#13b8f7", "scope": "domestic", "description": "频道公开榜单 · 标注原榜名称", "url": "https://www.youku.com/channel/webtv/list", "kinds": ["all", "tv", "variety", "movie"], "source_label": "官方频道榜单"},
    {"id": "mango", "name": "芒果 TV", "wordmark": "芒果TV", "color": "#ff8b00", "scope": "domestic", "description": "官方频道推荐 · 保留栏目名称", "url": "https://www.mgtv.com/", "kinds": ["all", "tv", "variety", "movie"], "source_label": "官方频道推荐"},
    {"id": "bilibili", "name": "哔哩哔哩", "wordmark": "bilibili", "color": "#fb7299", "scope": "domestic", "description": "影视榜 · 自动检查榜单日期", "url": "https://www.bilibili.com/v/popular/rank/movie", "kinds": ["all", "tv", "variety", "movie"], "source_label": "官方影视榜"},
    {"id": "netflix", "name": "Netflix", "wordmark": "NETFLIX", "color": "#e50914", "scope": "global", "description": "Tudum 全球周榜 · 官方观看数据", "url": "https://www.netflix.com/tudum/top10/", "kinds": ["all", "tv", "movie"], "source_label": "官方全球周榜"},
    {"id": "hbo", "name": "HBO Max", "wordmark": "HBO max", "color": "#6366f1", "scope": "global", "description": "所选地区可看 · 按 TMDB 热门排序", "url": "https://www.hbomax.com/", "kinds": ["all", "tv", "variety", "movie"], "source_label": "平台可看 · TMDB 热门"},
    {"id": "disney", "name": "Disney+", "wordmark": "Disney+", "color": "#47c6f9", "scope": "global", "description": "所选地区可看 · 按 TMDB 热门排序", "url": "https://www.disneyplus.com/explore", "kinds": ["all", "tv", "variety", "movie"], "source_label": "平台可看 · TMDB 热门"},
]
PLATFORM_BY_ID = {p["id"]: p for p in PLATFORMS}
TMDB_BASE = "https://api.themoviedb.org/3"
NETFLIX_DATA = "https://www.netflix.com/tudum/top10/data/all-weeks-global.tsv"
NETFLIX_SCHEMA = "official-pages-v3"
NETFLIX_SNAPSHOT_KEY = ("netflix-chart", NETFLIX_SCHEMA, "global")
TENCENT_API = "https://pbaccess.video.qq.com/trpc.tencentvideohotlistdata.hotlistdata.DoSearchHotListHttp/getSearchHotListHttp"
ALLOWED_HOSTS = {"api.themoviedb.org", "www.netflix.com", "www.iqiyi.com", "www.youku.com", "pbaccess.video.qq.com", "api.bilibili.com", "dc.bz.mgtv.com"}
PAGE_SIZE = 20
KINDS = {"all", "tv", "variety", "movie"}
PROXY_MODES = {"host", "direct", "custom"}
PROXY_SCHEMES = {"http", "https", "socks5", "socks5h"}


class _SourceError(ValueError):
    """Deliberately safe public messages, distinct from transport diagnostics."""


def _secret_value(value):
    return value.get_secret_value().strip() if hasattr(value, "get_secret_value") else str(value or "").strip()


def _validated_proxy(value):
    """Validate the administrator's proxy without echoing its credentials."""
    raw = _secret_value(value)
    try:
        parts = urlsplit(raw)
        if (parts.scheme not in PROXY_SCHEMES or not parts.hostname or parts.query or parts.fragment
                or parts.path not in {"", "/"} or any(char.isspace() for char in raw)):
            raise ValueError
        if parts.port is not None and not 1 <= parts.port <= 65535:
            raise ValueError
    except (ValueError, TypeError):
        raise _SourceError("请配置有效的 HTTP、HTTPS 或 SOCKS 自定义代理地址。") from None
    return raw


def _setting(config, name, default):
    return config.get(name, default) if isinstance(config, dict) else getattr(config, name, default)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _url(value):
    value = str(value or "").strip()
    if value.startswith("//"):
        value = "https:" + value
    parts = urlsplit(value)
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
        return None
    # Output links are never fetched by the server. Upgrade source CDN URLs.
    return "https://" + value.split("://", 1)[1]


def _search_url(title):
    return "/search?" + urlencode({"q": title, "tab": "media"})


def _item(platform, title, media_type, native_id="", **values):
    title = str(title or "").strip()[:300]
    native_id = str(native_id or hashlib.sha256(title.encode()).hexdigest()[:16])
    return {"id": f"{platform}:{native_id}", "title": title, "media_type": media_type,
            "search_url": _search_url(title), "detail_ref": None, "poster_url": None,
            "backdrop_url": None, **values}


def _paginate(items, page):
    start = (page - 1) * PAGE_SIZE
    return items[start:start + PAGE_SIZE], len(items) > start + PAGE_SIZE


@dataclass
class _Node:
    tag: str
    attrs: dict = field(default_factory=dict)
    children: list = field(default_factory=list)

    def nodes(self):
        yield self
        for child in self.children:
            if isinstance(child, _Node):
                yield from child.nodes()

    def text(self):
        return "".join(c.text() if isinstance(c, _Node) else c for c in self.children)

    def has_class(self, name):
        return name in self.attrs.get("class", "").split()

    def by_class(self, name):
        return next((n for n in self.nodes() if n.has_class(name)), None)


class _HTML(HTMLParser):
    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self, document):
        super().__init__(convert_charrefs=True)
        self.root = _Node("root")
        self.stack = [self.root]
        self.feed(document)

    def handle_starttag(self, tag, attrs):
        node = _Node(tag, dict(attrs))
        self.stack[-1].children.append(node)
        if tag not in self.VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.stack[-1].children.append(_Node(tag, dict(attrs)))

    def handle_endtag(self, tag):
        for pos in range(len(self.stack) - 1, 0, -1):
            if self.stack[pos].tag == tag:
                del self.stack[pos:]
                break

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def parse_iqiyi(document):
    """Only extract visible SSR cards; never evaluate the Nuxt script."""
    root = _HTML(document).root
    items = []
    types = {"电视剧": "tv", "中剧": "tv", "短剧": "tv", "综艺": "variety", "电影": "movie"}
    for card in root.nodes():
        if card.tag != "a" or not card.has_class("rvi__box"):
            continue
        title_node, type_node = card.by_class("rvi__tit1"), card.by_class("rvi__type1")
        if title_node is None or type_node is None:
            continue
        type_text = type_node.attrs.get("title") or type_node.text().strip()
        media_type = types.get(type_text.split("/")[0].strip())
        if not media_type:
            continue
        title = title_node.attrs.get("title") or title_node.text().strip()
        image = next((n for n in card.nodes() if n.tag == "img" and n.has_class("i71-img")), None)
        rank_node, heat_node = card.by_class("rvi__No__txt"), card.by_class("rvi__index__num")
        desc = card.by_class("rvi__des2")
        year = re.search(r"(?:^|/)\s*((?:19|20)\d{2})\s*(?:/|$)", type_text)
        link = _url(card.attrs.get("href"))
        items.append(_item("iqiyi", title, media_type, card.attrs.get("data-pb-v2", ""),
                           rank=int(rank_node.text()) if rank_node and rank_node.text().isdigit() else None,
                           year=int(year.group(1)) if year else None, subtitle=type_text,
                           overview=(desc.attrs.get("title") or desc.text()).strip() if desc else "",
                           poster_url=_url(image.attrs.get("src")) if image else None,
                           source_url=link, metadata={"heat": heat_node.text().strip() if heat_node else None, "rank_scope": "总榜"}))
    meta = root.by_class("erji__meta__txt")
    return items, meta.text().strip() if meta else ""


def parse_youku(document, media_type):
    """Read a JSON-like data assignment without executing JavaScript."""
    match = re.search(r"window\.__INITIAL_DATA__\s*=\s*(\{.*?)(?:</script>)", document, re.S)
    if not match:
        raise _SourceError("优酷公开页面未包含榜单数据")
    payload = match.group(1).strip().rstrip(";").strip()
    # Youku serializes absent values as the literal undefined. Token replacement
    # is limited to value positions, never arbitrary script execution.
    payload = re.sub(r"(?<=[:\[,])\s*undefined(?=\s*[,}\]])", "null", payload)
    data = json.loads(payload)
    for module in data.get("moduleList", []):
        headings = [str(v.get("title", "")) for v in module.get("mainTitleLinks", [])]
        if not any("榜" in heading for heading in headings):
            continue
        components, tabs = module.get("components", []), module.get("tabTitle", [])
        # Prefer the platform heat chart when shipped in this page. Otherwise
        # preserve the name of the first public chart (e.g. 飙升榜), never rename it.
        index = next((i for i, tab in enumerate(tabs) if tab.get("name") == "热度榜"), 0)
        if index >= len(components) or not components[index].get("isRank"):
            continue
        label = str(tabs[index].get("name", "频道榜单")) if tabs else "频道榜单"
        items, server_time = [], None
        for rank, row in enumerate(components[index].get("itemList", []), 1):
            if not row.get("title"):
                continue
            track = row.get("trackInfo") or {}
            server_time = track.get("servertime") or server_time
            items.append(_item("youku", row["title"], media_type, row.get("videoId") or row.get("videoLink"),
                               rank=rank, subtitle=row.get("subTitle") or row.get("summary"),
                               poster_url=_url(row.get("img")), source_url=_url(row.get("videoLink") or row.get("link")),
                               metadata={"rank_scope": label, "update_status": row.get("summary")}))
        if items:
            stamp = datetime.fromtimestamp(float(server_time) / 1000, timezone.utc).isoformat() if server_time else None
            return items, label, stamp
    raise _SourceError("优酷公开页面当前没有可解析的频道榜单")


class DiscoverySources:
    def __init__(self, config, ops_getter=None, *, entry_id="platform-discovery", transport=None, storage=None):
        self.config = config
        self.storage = storage
        self._cache = PersistentCache(storage) if storage is not None else {}
        self._locks = {}
        self._refresh_tasks = {}
        self._slots = asyncio.Semaphore(4)
        self._closed = False
        token = _setting(config, "tmdb_token", "")
        self._token = _secret_value(token)
        self.proxy_mode = str(_setting(config, "proxy_mode", "host")).strip().lower()
        if self.proxy_mode not in PROXY_MODES:
            raise _SourceError("代理模式无效，请选择沿用宿主、直接连接或自定义代理。")
        region = str(_setting(config, "watch_region", "US")).upper()
        self.region = region if re.fullmatch("[A-Z]{2}", region) else "US"
        self.timeout = max(3, min(30, int(_setting(config, "request_timeout_seconds", 12))))
        self.ttl = max(5, min(1440, int(_setting(config, "cache_minutes", 60)))) * 60
        if transport is None:
            try:
                if self.proxy_mode == "host":
                    from movieclaw_sdk import net
                    transport = net.http_transport(entry_id)
                elif self.proxy_mode == "direct":
                    transport = httpx.AsyncHTTPTransport(trust_env=False)
                else:
                    ordinary = _setting(config, "proxy_address", "").strip()
                    if ordinary:
                        parts = urlsplit(ordinary)
                        if parts.username is not None or parts.password is not None:
                            raise _SourceError("带账号密码的链接请使用安全字段")
                    proxy = _validated_proxy(ordinary or _setting(config, "proxy_url", None))
                    transport = httpx.AsyncHTTPTransport(proxy=proxy, trust_env=False)
            except _SourceError:
                raise
            except Exception:
                raise _SourceError("无法初始化插件网络连接，请检查代理设置。") from None
        self._client = httpx.AsyncClient(transport=transport, timeout=self.timeout, follow_redirects=False, trust_env=False)

    async def initialize(self):
        if isinstance(self._cache, PersistentCache):
            await self._cache.hydrate()

    def cache_status(self):
        status = self.storage.status() if self.storage is not None else {"persistent": False}
        return {**status, "memory_entries": len(self._cache),
                "restored_entries": getattr(self._cache, "hydrated", 0),
                "background_refreshes": len(self._refresh_tasks)}

    async def close(self):
        self._closed = True
        tasks = list(self._refresh_tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if isinstance(self._cache, PersistentCache):
            await self._cache.close()
        self._cache.clear()
        await self._client.aclose()

    async def catalog(self):
        settings = migrate_platform_settings(self.config) if isinstance(self.config, dict) else self.config
        platforms = [copy.deepcopy(p) for p in PLATFORMS if _setting(settings, "enable_" + p["id"], True)]
        return {"platforms": platforms, "region": self.region, "tmdb_configured": bool(self._token), "proxy_mode": self.proxy_mode}

    async def connection_test(self):
        """Probe one fixed public data source through the selected transport."""
        started = time.monotonic()
        try:
            document = await self._fetch(NETFLIX_DATA)
            header = document.split("\n", 1)[0].strip().split("\t")
            if not {"week", "category", "weekly_rank", "show_title"}.issubset(header):
                raise _SourceError("上游响应未通过数据格式检查。")
            ok, message = True, "Netflix 官方周榜数据连接正常。"
        except httpx.HTTPStatusError as exc:
            ok = False
            if exc.response.status_code in {401, 403}:
                message = "上游拒绝了此请求，连接测试未通过；该结果不能单独判断代理是否可用。"
            elif exc.response.status_code == 429:
                message = "上游暂时限制了请求频率，请稍后重试。"
            elif exc.response.status_code >= 500:
                message = "上游服务暂时异常，请稍后重试。"
            else:
                message = "上游返回异常响应，连接测试未通过，请稍后重试。"
        except Exception:
            ok, message = False, "连接测试未通过，请检查网络或代理设置后重试。"
        return {"ok": ok, "mode": self.proxy_mode, "message": message,
                "latency_ms": max(0, round((time.monotonic() - started) * 1000))}

    async def _fetch(self, url, params=None, tmdb=False):
        if self._closed:
            raise _SourceError("插件已停止")
        parts = urlsplit(url)
        if parts.scheme != "https" or parts.hostname not in ALLOWED_HOSTS or parts.username or parts.password:
            raise _SourceError("不支持的源地址")
        headers = {"User-Agent": "MovieClaw-PlatformDiscovery/0.1", "Accept": "application/json,text/html,text/tab-separated-values"}
        if tmdb:
            if not self._token:
                raise _SourceError("请配置 TMDB API Read Access Token")
            headers["Authorization"] = "Bearer " + self._token

        async with self._slots:
            async with self._client.stream("GET", url, params=params, headers=headers) as response:
                if 300 <= response.status_code < 400:
                    raise _SourceError("源站返回跳转，未继续请求")
                response.raise_for_status()
                raw = bytearray()
                async for block in response.aiter_bytes():
                    raw.extend(block)
                    if len(raw) > 4_000_000:
                        raise _SourceError("源数据超过大小限制")
                return raw.decode("utf-8-sig", "replace")

    async def _json(self, url, params=None, tmdb=False):
        return json.loads(await self._fetch(url, params, tmdb))

    def _source(self, platform, kind="official_rank", label=None, **extra):
        p = PLATFORM_BY_ID[platform]
        return {"label": label or p["source_label"], "kind": kind, "url": p["url"], **extra}

    def _empty(self, platform, kind, status, message):
        return {"platform_id": platform, "kind": kind, "items": [], "source": self._source(platform, "official_selection" if platform == "mango" else "provider_popular" if platform in {"hbo", "disney"} else "official_rank"),
                "status": status, "message": message, "has_more": False}

    async def feed(self, platform, kind="all", page=1):
        if platform not in PLATFORM_BY_ID or kind not in KINDS or not isinstance(page, int) or not 1 <= page <= 100:
            raise _SourceError("平台、类型或页码无效")
        if platform not in {p["id"] for p in (await self.catalog())["platforms"]}:
            return self._empty(platform, kind, "unavailable", "此平台已在插件设置中关闭")
        key = (platform, kind, page)
        cached = self._cache.get(key)
        if platform == "netflix" and cached:
            source = cached[1].get("source", {})
            snapshot = self._cache.get(NETFLIX_SNAPSHOT_KEY)
            # v0.2 persisted TSV feeds must recheck the newer official pages
            # immediately. Keep their dated contents usable during migration.
            if (source.get("schema") != NETFLIX_SCHEMA or snapshot and
                    source.get("snapshot_checked_at") != snapshot[1].get("checked_at")):
                cached = (0, cached[1])
                self._cache[key] = cached
        if cached and time.monotonic() < cached[0]:
            result = copy.deepcopy(cached[1])
            result["cache"] = {"refreshing": False, "persistent": self.storage is not None}
            return result
        if cached and cached[1].get("items"):
            self._schedule_refresh(platform, kind, page, key)
            result = copy.deepcopy(cached[1])
            result.update(status="stale", message="先展示已保存的片单，正在后台更新；请留意来源日期。")
            result["cache"] = {"refreshing": True, "persistent": self.storage is not None}
            return result
        return await self._refresh_feed(platform, kind, page, key)

    def _schedule_refresh(self, platform, kind, page, key):
        if self._closed:
            return None
        if key in self._refresh_tasks:
            return self._refresh_tasks[key]
        task = asyncio.create_task(self._refresh_feed(platform, kind, page, key))
        self._refresh_tasks[key] = task
        task.add_done_callback(lambda finished: self._refresh_tasks.pop(key, None))
        return task

    async def refresh(self, platform, kind="all", page=1):
        """Force a checked refresh, sharing active work and preserving failures.

        Adjacent Netflix variants share one four-board check within 30 seconds,
        so a scheduler batch does not download the same 13 MB four times.
        """
        if platform not in PLATFORM_BY_ID or kind not in KINDS or not isinstance(page, int) or not 1 <= page <= 100:
            raise _SourceError("平台、类型或页码无效")
        if platform not in {p["id"] for p in (await self.catalog())["platforms"]}:
            return self._empty(platform, kind, "unavailable", "此平台已在插件设置中关闭")
        key = (platform, kind, page)
        active = self._refresh_tasks.get(key)
        if active is not None:
            return copy.deepcopy(await asyncio.shield(active))
        if platform == "netflix" and time.monotonic() - getattr(self, "_netflix_last_check", 0) >= 30:
            snapshot = self._cache.get(NETFLIX_SNAPSHOT_KEY)
            if snapshot:
                self._cache[NETFLIX_SNAPSHOT_KEY] = (0, snapshot[1])
        cached = self._cache.get(key)
        if cached:
            self._cache[key] = (0, cached[1])
        task = self._schedule_refresh(platform, kind, page, key)
        if task is None:
            raise _SourceError("插件正在关闭，请稍后重试。")
        return copy.deepcopy(await asyncio.shield(task))

    async def _refresh_feed(self, platform, kind, page, key):
        async with self._locks.setdefault(key, asyncio.Lock()):
            cached = self._cache.get(key)
            if cached and time.monotonic() < cached[0]:
                return copy.deepcopy(cached[1])
            try:
                result = await getattr(self, "_" + platform)(kind, page)
                result.update({"platform_id": platform, "kind": kind, "page": page})
                result.setdefault("status", "ready")
                result.setdefault("message", "")
                result.setdefault("has_more", False)
                result.setdefault("fetched_at", _now())
                result["cache"] = {"refreshing": False, "persistent": self.storage is not None}
                result["source"].setdefault("fetched_at", result["fetched_at"])
                ttl = self.ttl if result["status"] == "ready" else 120
                if result.get("artwork_diagnostics", {}).get("missing", 0):
                    ttl = min(ttl, 120)
                self._cache[key] = (time.monotonic() + ttl, copy.deepcopy(result))
                return result
            except Exception as exc:
                # A network error does not discard the most recent usable data.
                if cached and cached[1].get("items"):
                    result = copy.deepcopy(cached[1])
                    result.update(status="stale", message="刷新失败，正在展示上次成功获取的榜单；请留意榜单日期。")
                    result["cache"] = {"refreshing": False, "persistent": self.storage is not None}
                    self._cache[key] = (time.monotonic() + 120, copy.deepcopy(result))
                    return result
                message = str(exc) if isinstance(exc, _SourceError) else "暂时无法获取源平台数据，可打开官方榜单查看。"
                result = self._empty(platform, kind, "unavailable", message)
                result["page"] = page
                self._cache[key] = (time.monotonic() + 120, result)
                return copy.deepcopy(result)

    async def _iqiyi(self, kind, page):
        # The SSR aggregate page is more consistently available than channels.
        document = await self._fetch("https://www.iqiyi.com/ranks1/-1/0")
        items, period = parse_iqiyi(document)
        if not items:
            raise _SourceError("爱奇艺公开页面未返回可用的风云榜")
        stamp = None
        clock = re.search(r"最近更新\s*(\d{2})-(\d{2})\s+(\d{2}):(\d{2})", period)
        if clock:
            now = datetime.now(timezone(timedelta(hours=8)))
            month, day, hour, minute = map(int, clock.groups())
            candidates = [datetime(year, month, day, hour, minute, tzinfo=now.tzinfo) for year in (now.year - 1, now.year, now.year + 1)]
            updated = min(candidates, key=lambda v: abs((now - v).total_seconds()))
            if updated < now - timedelta(days=7) or updated > now + timedelta(days=1):
                raise _SourceError("爱奇艺榜源日期不符合当前榜单时效，暂不展示为热播。")
            stamp = updated.isoformat()
        filtered = [i for i in items if kind == "all" or i["media_type"] == kind]
        items, more = _paginate(filtered, page)
        return {"items": items, "has_more": more, "source": self._source("iqiyi", label="爱奇艺官方风云榜 · 总榜筛选", period=period, updated_at=stamp),
                "message": "按官方总榜筛选类型，名次保留总榜原始名次。"}

    async def _tencent(self, kind, page):
        channel = {"all": "0", "movie": "1", "tv": "2", "variety": "10"}[kind]
        data = await self._json(TENCENT_API, {"ChannelID": channel, "PageSize": PAGE_SIZE, "PageNum": page - 1, "SceneID": 2})
        data = data.get("data", data)
        if data.get("ErrCode", 0) != 0:
            raise _SourceError("腾讯视频热搜榜暂时不可用")
        block = next((v.get("Result", {}) for v in data.get("Results", []) if str(v.get("ChannelID")) == channel), {})
        rows, items = block.get("ListInfo", []), []
        for position, row in enumerate(rows, (page - 1) * PAGE_SIZE + 1):
            media_type = {"1": "movie", "2": "tv", "10": "variety"}.get(str(row.get("VideoType")))
            if not media_type or not row.get("Query") or (kind != "all" and media_type != kind):
                continue
            native_id = str(row.get("ID", ""))
            direct = f"https://v.qq.com/x/cover/{native_id}.html" if re.fullmatch(r"[A-Za-z0-9]{10,30}", native_id) else None
            image = _url(row.get("HzImgURL"))
            items.append(_item("tencent", row["Query"], media_type, native_id,
                               rank=position, subtitle=row.get("SecondTitle", ""), backdrop_url=image, poster_url=image,
                               source_url=direct or ("https://v.qq.com/x/search/?" + urlencode({"q": row["Query"]})),
                               metadata={"heat": row.get("HotLevel"), "trend": row.get("Trend"), "rank_scope": "官方热搜榜", "poster_aspect": "landscape"}))
        if not items and page == 1:
            raise _SourceError("腾讯视频官方热搜榜当前未返回可用影片")
        return {"items": items, "has_more": int(block.get("TotalNum") or 0) > page * PAGE_SIZE,
                "source": self._source("tencent", label="腾讯视频官方热搜榜", period="源接口未提供榜单生成时间"),
                "message": "这是平台公开热搜榜，热度反映搜索关注；不是播放量排名。"}

    async def _youku(self, kind, page):
        if kind == "all":
            results = await asyncio.gather(*(self._youku(k, 1) for k in ("tv", "movie", "variety")), return_exceptions=True)
            valid = [r for r in results if isinstance(r, dict) and r.get("items")]
            if not valid:
                raise _SourceError("优酷公开频道榜单暂时不可用")
            items, more = _paginate([i for r in valid for i in r["items"]], page)
            missing = len(valid) < 3
            return {"items": items, "has_more": more, "source": self._source("youku", label="优酷公开频道榜单", period="各类别独立排行"),
                    "message": "保留每条内容所属原榜名次。" + (" 部分分类暂不可用。" if missing else "")}
        channel = {"tv": "webtv", "movie": "webmovie", "variety": "webzy"}[kind]
        url = f"https://www.youku.com/channel/{channel}/list"
        items, label, updated = parse_youku(await self._fetch(url), kind)
        if updated:
            stamp, now = datetime.fromisoformat(updated), datetime.now(timezone.utc)
            if stamp < now - timedelta(days=14) or stamp > now + timedelta(days=1):
                raise _SourceError("优酷榜源日期不符合当前榜单时效，暂不展示为热播。")
        items, more = _paginate(items, page)
        return {"items": items, "has_more": more, "source": self._source("youku", label="优酷官方" + label, url=url, updated_at=updated),
                "message": "名次来自当前公开频道的“" + label + "”。"}

    async def _mango(self, kind, page):
        if kind == "all":
            results = await asyncio.gather(*(self._mango_category(k) for k in ("tv", "movie", "variety")), return_exceptions=True)
            valid = [r for r in results if isinstance(r, dict) and r.get("items")]
            if not valid:
                raise _SourceError("芒果 TV 官方频道推荐当前暂不可用，可前往平台查看。")
            combined = [item for result in valid for item in result["items"]]
            combined.sort(key=lambda i: (i["metadata"]["selection_order"], i["media_type"]))
            items, more = _paginate(combined, page)
            return {"items": items, "has_more": more,
                    "source": self._source("mango", "official_selection", label="芒果 TV 官方频道推荐", period="各频道独立推荐；源未提供推荐生成时间"),
                    "message": "这是平台频道选片，保留栏目顺序；不是播放量或热度排名。" + (" 部分频道暂不可用。" if len(valid) < 3 else "")}
        result = await self._mango_category(kind)
        result["items"], result["has_more"] = _paginate(result["items"], page)
        return result

    async def _mango_category(self, kind):
        try:
            rows, label, updated = parse_mango_channel(await self._json(MANGO_CHANNEL_BASE + MANGO_CHANNELS[kind], {"osType": "windows"}), kind)
        except DomesticSourceError as exc:
            raise _SourceError(str(exc)) from None
        items = []
        for row in rows:
            row = dict(row)
            title, native_id = row.pop("title"), row.pop("native_id")
            for key in ("poster_url", "backdrop_url", "source_url"):
                row[key] = _url(row.get(key))
            items.append(_item("mango", title, kind, native_id, **row))
        return {"items": items,
                "source": self._source("mango", "official_selection", label="芒果 TV 官方频道推荐 · " + label, url=MANGO_PAGES[kind],
                                       period="栏目：" + label + "；源未提供推荐生成时间", updated_at=updated),
                "message": "保留官方“" + label + "”栏目推荐顺序；不是播放量或热度排名。"}

    async def _bilibili(self, kind, page):
        if kind == "all":
            results = await asyncio.gather(*(self._bilibili_category(k) for k in ("tv", "movie", "variety")), return_exceptions=True)
            valid = [r for r in results if isinstance(r, dict) and r.get("items")]
            if not valid:
                messages = [str(r) for r in results if isinstance(r, _SourceError)]
                raise _SourceError(messages[0] if messages else "哔哩哔哩公开影视榜当前没有可用数据")
            combined = [item for result in valid for item in result["items"]]
            combined.sort(key=lambda i: (i["rank"], i["media_type"]))
            items, more = _paginate(combined, page)
            periods = "；".join(r["source"]["period"] for r in valid)
            return {"items": items, "has_more": more,
                    "source": self._source("bilibili", period=periods, updated_at=max(r["source"]["updated_at"] for r in valid)),
                    "message": "电影、电视剧、综艺为独立榜单，保留原榜名次；仅展示通过日期和分类检查的正版影视条目。" + (" 部分分类暂不可用。" if len(valid) < 3 else "")}
        result = await self._bilibili_category(kind)
        result["items"], result["has_more"] = _paginate(result["items"], page)
        return result

    async def _bilibili_category(self, kind):
        try:
            rows, period, updated = parse_bilibili_rank(await self._json(BILIBILI_RANK_API, {"day": 3, "season_type": BILIBILI_TYPES[kind]}), kind)
        except DomesticSourceError as exc:
            raise _SourceError(str(exc)) from None
        items = []
        for row in rows:
            row = dict(row)
            title, native_id = row.pop("title"), row.pop("native_id")
            for key in ("poster_url", "backdrop_url", "source_url"):
                row[key] = _url(row.get(key))
            items.append(_item("bilibili", title, kind, native_id, **row))
        return {"items": items, "source": self._source("bilibili", label="哔哩哔哩官方" + KIND_NAMES[kind] + "榜",
                url="https://www.bilibili.com/v/popular/rank/" + kind + "/", period=period, updated_at=updated),
                "message": "名次来自平台正版影视分类榜，按近期综合得分排行；不是全站投稿或影视解说榜。"}

    async def _netflix(self, kind, page):
        if kind == "variety":
            return self._empty("netflix", kind, "unavailable", "Netflix 官方周榜没有独立综艺分类；综艺节目包含在 TV 榜中。")
        snapshot = await self._netflix_snapshot()
        week, rows = snapshot["week"], snapshot["rows"]
        latest = date.fromisoformat(week)
        items = []
        for row in rows:
            media_type = "movie" if row["category"].startswith("Films") else "tv"
            if kind != "all" and media_type != kind:
                continue
            season, category = row.get("season_title", "N/A"), row["category"]
            items.append(_item("netflix", row["show_title"], media_type, f"{week}:{category}:{row['weekly_rank']}",
                               rank=row["weekly_rank"], subtitle=season if season not in {"", "N/A"} else category,
                               source_url=OFFICIAL_PAGES[category],
                               metadata={"rank_scope": category, "season_title": season, "weekly_views": row["weekly_views"],
                                         "weekly_hours_viewed": row["weekly_hours_viewed"], "runtime": row["runtime"],
                                         "runtime_display": row.get("runtime_display"), "runtime_minutes": row.get("runtime_minutes"),
                                         "weeks_in_top_10": row.get("weeks_in_top_10"), "week": week,
                                         "metric_units": {"weekly_views": "views", "weekly_hours_viewed": "hours", "runtime": "hours", "runtime_minutes": "minutes"}}))
        # Independent ranks remain independent; this is not a combined Top 40.
        items.sort(key=lambda i: (i["rank"], i["metadata"]["rank_scope"]))
        items, more = _paginate(items, page)
        resolver = self._netflix_artwork()
        for category, records in snapshot.get("artwork", {}).items():
            resolver.prime(category, week, records)
        artwork_diagnostics = await resolver.official_items(items, week)
        if self._token:
            await asyncio.gather(*(self._enrich_netflix(item) for item in items))
        age = (datetime.now(timezone.utc).date() - latest).days
        freshness = f" 此统计周距今 {age} 天，请以标注日期为准。" if age > 10 else ""
        fallback = snapshot.get("fallback", False)
        return {"items": items, "has_more": more, "status": "stale" if fallback else "ready",
                "artwork_diagnostics": artwork_diagnostics or {},
                "source": self._source("netflix", label="Netflix 官方全球周榜" + (" · 上次数据" if fallback else ""),
                    period=snapshot["week_start"] + " — " + week, week_start=snapshot["week_start"], week_end=week,
                    updated_at=week, data_url=NETFLIX_DATA if snapshot["data_source"] == "tsv_fallback" else OFFICIAL_PAGES["Films (English)"],
                    attribution="Netflix Tudum", scope="global", board_count=4, schema=NETFLIX_SCHEMA,
                    data_source=snapshot["data_source"], snapshot_checked_at=snapshot["checked_at"],
                    diagnostics=snapshot.get("diagnostics", {"state": "ready"})),
                "message": ("官网最新榜暂未取得，正在展示标注日期的旧榜；这不是当前周榜。" if fallback else
                    "官网默认页四分类已核对同一统计周。") + "电影 / TV、英语 / 非英语为独立榜单，保留原榜名次；全球周榜不代表地区片库。观看次数单位为次，观看时长单位为小时，片长为小时:分钟。" + freshness}

    @staticmethod
    def _netflix_validate_week(week):
        try:
            latest = date.fromisoformat(week)
        except (ValueError, TypeError):
            raise _SourceError("Netflix 官方周榜没有可验证日期") from None
        if latest.weekday() != 6:
            raise _SourceError("Netflix 官方周榜不是完整的周统计区间")
        if latest < datetime.now(timezone.utc).date() - timedelta(days=21):
            raise _SourceError("Netflix 周榜已超过三周未更新，暂不展示为当前热播。")
        if latest > datetime.now(timezone.utc).date() + timedelta(days=1):
            raise _SourceError("Netflix 官方周榜日期异常，暂不展示为当前热播。")
        return latest

    async def _netflix_tsv_fallback(self):
        """The download can lag behind default pages; it is never called live."""
        rows = list(csv.DictReader(io.StringIO(await self._fetch(NETFLIX_DATA)), delimiter="\t"))
        if not rows or not {"week", "category", "weekly_rank", "show_title", "weekly_views", "weekly_hours_viewed", "runtime"}.issubset(rows[0]):
            raise _SourceError("Netflix 官方备用周榜格式暂时不可用")
        week = max(r.get("week", "") for r in rows)
        latest = self._netflix_validate_week(week)
        selected = []
        for row in rows:
            if row.get("week") != week:
                continue
            try:
                row = dict(row)
                row["weekly_rank"] = int(row["weekly_rank"])
                row["weekly_views"] = int(row["weekly_views"])
                row["weekly_hours_viewed"] = int(row["weekly_hours_viewed"])
                row["runtime"] = float(row["runtime"])
                if row["category"] not in OFFICIAL_PAGES or not row["show_title"].strip() or not math.isfinite(row["runtime"]) or min(row["weekly_views"], row["weekly_hours_viewed"], row["runtime"]) <= 0:
                    raise ValueError
            except (ValueError, TypeError, KeyError):
                raise _SourceError("Netflix 官方备用周榜含有无法核对的条目") from None
            selected.append(row)
        if len(selected) != 40 or any(sorted(r["weekly_rank"] for r in selected if r["category"] == category) != list(range(1, 11)) for category in OFFICIAL_PAGES):
            raise _SourceError("Netflix 官方备用周榜四分类不完整，暂不拼接其他周榜。")
        for category in OFFICIAL_PAGES:
            identities = [(row.get("season_title") or row["show_title"]) if row.get("season_title") not in {None, "", "N/A"} else row["show_title"]
                          for row in selected if row["category"] == category]
            if len(set(identities)) != 10:
                raise _SourceError("Netflix 官方备用周榜作品重复，暂不展示未经核对的排名。")
        return {"week": week, "week_start": (latest - timedelta(days=6)).isoformat(), "rows": selected,
                "artwork": {}, "data_source": "tsv_fallback", "fallback": True, "checked_at": _now()}

    async def _netflix_snapshot(self):
        key = NETFLIX_SNAPSHOT_KEY
        cached = self._cache.get(key)
        if cached and cached[0] > time.monotonic():
            return copy.deepcopy(cached[1])
        async with self._locks.setdefault(key, asyncio.Lock()):
            cached = self._cache.get(key)
            if cached and cached[0] > time.monotonic():
                return copy.deepcopy(cached[1])
            async def board(category, url):
                return parse_official_board(_HTML(await self._fetch(url)).root, category)
            boards = await asyncio.gather(*(board(c, u) for c, u in OFFICIAL_PAGES.items()), return_exceptions=True)
            cause = "request_or_parse_error"
            if all(isinstance(b, dict) for b in boards):
                weeks = {b["week"] for b in boards}
                if len(weeks) == 1:
                    week = next(iter(weeks))
                    try:
                        self._netflix_validate_week(week)
                        if cached and week < cached[1]["week"]:
                            raise _SourceError("官网返回较早的统计周")
                    except _SourceError:
                        cause = "invalid_or_older_week"
                    else:
                        snapshot = {"week": week, "week_start": boards[0]["week_start"],
                                    "rows": [row for b in boards for row in b["rows"]],
                                    "artwork": {c: b["artwork"] for c, b in zip(OFFICIAL_PAGES, boards)},
                                    "data_source": "official_pages", "fallback": False, "checked_at": _now(),
                                    "diagnostics": {"state": "ready", "boards": 4, "week": week}}
                        self._netflix_last_check = time.monotonic()
                        self._cache[key] = (time.monotonic() + self.ttl, copy.deepcopy(snapshot))
                        return snapshot
                else:
                    cause = "inconsistent_weeks"
            self._netflix_last_check = time.monotonic()
            if cached and cached[1].get("rows"):
                # Never replace a coherent saved week with partial/mixed newer
                # boards or a downloadable file which is older still.
                self._netflix_validate_week(cached[1]["week"])
                snapshot = copy.deepcopy(cached[1])
                snapshot["fallback"] = True
            else:
                snapshot = await self._netflix_tsv_fallback()
            snapshot["diagnostics"] = {"state": "stale", "cause": cause, "boards": 4,
                                       "message": "未取得完整且同周的官网最新四榜，保留来源原始日期。"}
            self._cache[key] = (time.monotonic() + 120, copy.deepcopy(snapshot))
            return snapshot

    def _netflix_artwork(self):
        if not hasattr(self, "_netflix_artwork_resolver"):
            self._netflix_artwork_resolver = NetflixArtwork(
                self._fetch, self._json, self._tmdb_item, lambda document: _HTML(document).root,
                self._cache, self._locks, self.ttl, self.timeout, TMDB_BASE)
        return self._netflix_artwork_resolver

    async def _enrich_netflix(self, item):
        await self._netflix_artwork().enrich(item)

    def _tmdb_item(self, platform, row, media_type):
        actual_type = "variety" if media_type == "tv" and {10764, 10767}.intersection(row.get("genre_ids", [])) else media_type
        title = row.get("title") or row.get("name") or ""
        poster = "https://image.tmdb.org/t/p/w500" + row["poster_path"] if row.get("poster_path") else None
        backdrop = "https://image.tmdb.org/t/p/w1280" + row["backdrop_path"] if row.get("backdrop_path") else None
        release = row.get("release_date") or row.get("first_air_date") or ""
        identity = int(row.get("id") or 0)
        if identity <= 0:
            raise _SourceError("TMDB 未返回有效影片 ID")
        return _item(platform, title, actual_type, f"{media_type}:{identity}", detail_ref=f"tmdb:{media_type}:{identity}",
                     poster_url=poster, backdrop_url=backdrop, overview=row.get("overview", ""), rating=row.get("vote_average"), rating_source="TMDB",
                     year=int(release[:4]) if re.match(r"\d{4}", release) else None,
                     source_url=f"https://www.themoviedb.org/{media_type}/{identity}/watch?" + urlencode({"locale": self.region}),
                     metadata={"popularity": row.get("popularity"), "region": self.region})

    async def _provider(self, platform, kind, page):
        if not self._token:
            return self._empty(platform, kind, "needs_config", "请在插件设置中填写 TMDB API Read Access Token，保存后即可查看此地区的平台可看片单。")
        aliases = {"hbo": {"hbo max", "max"}, "disney": {"disney plus", "disney+"}}[platform]

        async def discover(media_type):
            providers = await self._json(TMDB_BASE + "/watch/providers/" + media_type, {"watch_region": self.region, "language": "en-US"}, tmdb=True)
            provider = next((p for p in providers.get("results", []) if str(p.get("provider_name", "")).casefold() in aliases), None)
            if not provider:
                return {"results": [], "total_pages": 0}
            params = {"watch_region": self.region, "with_watch_providers": provider["provider_id"], "with_watch_monetization_types": "flatrate", "sort_by": "popularity.desc", "language": "zh-CN", "include_adult": "false", "page": page}
            if kind == "variety":
                params["with_genres"] = "10764|10767"
            return await self._json(TMDB_BASE + "/discover/" + media_type, params, tmdb=True)

        media_types = ("movie", "tv") if kind == "all" else ("movie" if kind == "movie" else "tv",)
        data = await asyncio.gather(*(discover(t) for t in media_types))
        items = [self._tmdb_item(platform, row, media_type) for media_type, result in zip(media_types, data) for row in result.get("results", [])]
        if kind == "tv":
            items = [i for i in items if i["media_type"] == "tv"]
        if kind == "all":
            items.sort(key=lambda i: i["metadata"].get("popularity") or 0, reverse=True)
        return {"items": items, "has_more": any(r.get("total_pages", 0) > page for r in data),
                "source": self._source(platform, "provider_popular", label=f"{PLATFORM_BY_ID[platform]['name']} 可看 · TMDB 热门", region=self.region,
                                       attribution="TMDB · Availability data by JustWatch", updated_at=None),
                "message": f"地区 {self.region} 的订阅可看片库，按 TMDB 站外热门排序；不是平台官方播放榜。" + (" 此地区暂无对应数据。" if not items else "")}

    async def _hbo(self, kind, page):
        return await self._provider("hbo", kind, page)

    async def _disney(self, kind, page):
        return await self._provider("disney", kind, page)
