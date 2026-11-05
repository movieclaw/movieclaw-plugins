"""按番号搜索补头像：av-wiki → javdb → DMM（纯解析 + 认人规则 + 按站点串行限速的请求器）。

认人规则：演员必须出现在**该番号**的演员表里。

- 演员表里有完全同名（按 :func:`matching.fold` 折叠后相等）→ ``exact``（同名）；
- av-wiki 演员表只有 1 人且不同名 → ``alias``（别名，正名 = 那个人）：先用正名查 gfriends，
  命中就用 gfriends 的图；否则带着正名继续查后面的来源；
- 其它（多人无同名、空表）→ 这个来源判定不了，继续下一个。

DMM 的图片地址是按 av-wiki 的罗马字 slug 猜的（``kuroki_nami`` 可能是同名的老演员），
所以只算 ``guess``（猜测），永远只进待确认。配了 DMM 官方 API 时按番号查演员表，同名才用。

只接受真实图片：HTTP 200、``image/*``、不跟随跳转（3xx = 没图）、大小合理、不是 DMM 的
「now printing」占位图。被反爬（403 / 429 / 挑战页 / 连不上）的来源本轮暂停，不算演员失败。

密钥（DMM api_id / affiliate_id）只出现在发给 api.dmm.com 的查询参数里；出错时只记站点名与
异常类型，不记地址、不记异常原文。
"""

from __future__ import annotations

import asyncio
import hashlib
import html as htmllib
import re
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote, urlsplit

import httpx

from .matching import Avatar, fold

AVWIKI = "avwiki"
JAVDB = "javdb"
DMM = "dmm"
DMM_API = "dmm_api"
GFRIENDS = "gfriends"

SOURCE_LABEL = {
    AVWIKI: "av-wiki",
    JAVDB: "javdb",
    DMM: "DMM",
    DMM_API: "DMM API",
    GFRIENDS: "gfriends",
}
HOW_LABEL = {"exact": "同名", "alias": "别名", "guess": "猜测"}
#: 「交叉验证通过自动写入」只认这些来源的同名结果
AUTO_SOURCES = frozenset({AVWIKI, JAVDB, DMM_API})

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)
BROWSER_HEADERS = {
    "User-Agent": BROWSER_UA,
    "Accept-Language": "ja,zh-CN;q=0.8,zh;q=0.7,en;q=0.5",
}
JAVDB_COOKIE = "over18=1; locale=zh"
DMM_API_BASE = "https://api.dmm.com/affiliate/v3/"
#: DMM 缺图时跳到的占位图（按跳转目标认，200 直接给出时按内容哈希认）
DMM_NOIMAGE_HINTS = ("now_printing", "/noimage/")
DMM_NOIMAGE_SHA1 = frozenset({"97d573a5b0cc474eb1e95265960b4a066f3aa4b7"})
MIN_IMAGE = 512
MAX_IMAGE = 5 * 1024 * 1024
CAST_EXCERPT = 6

_NUM = re.compile(r"(?<![A-Za-z0-9])(\d{0,3}[A-Za-z]{2,6})[-_ ]?(\d{2,5})(?![0-9])")
_NFO_NUM = re.compile(r"<num>\s*([^<]{2,40}?)\s*</num>", re.IGNORECASE)
_TITLE = re.compile(r"<title>\s*([^<]*)", re.IGNORECASE)
_CHALLENGE = (
    "one moment, please",
    "just a moment",
    "attention required",
    "cf-browser-verification",
    "challenge-platform",
    "driver-verify",
)
_TAG = re.compile(r"<[^>]+>")
_AVWIKI_ACTRESS = re.compile(
    r'<a\s+href="https?://av-wiki\.net/av-actress/([^"/]+)/?"[^>]*>(.*?)</a>', re.S
)
_JAVDB_BOX = re.compile(r'<a\s+href="/v/([A-Za-z0-9]+)"\s+class="box"[^>]*>(.*?)</a>', re.S)
_STRONG = re.compile(r"<strong>\s*([^<]+?)\s*</strong>")
_JAVDB_FEMALE = (
    re.compile(r'<a\s+class="actor-female"\s+href="/actors/([A-Za-z0-9]+)"[^>]*>([^<]+)</a>'),
    re.compile(
        r'<a\s+href="/actors/([A-Za-z0-9]+)"[^>]*>([^<]+)</a>\s*<strong\s+class="symbol female"'
    ),
)
_ASCII_SLUG = re.compile(r"^[a-z][a-z-]*[a-z]$")
_KUNREI = (
    ("sha", "sya"),
    ("shu", "syu"),
    ("sho", "syo"),
    ("cha", "tya"),
    ("chu", "tyu"),
    ("cho", "tyo"),
    ("shi", "si"),
    ("chi", "ti"),
    ("tsu", "tu"),
    ("ja", "zya"),
    ("ju", "zyu"),
    ("jo", "zyo"),
    ("ji", "zi"),
    ("fu", "hu"),
)
_MAGIC = (b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n", b"GIF8")


# ---------------------------------------------------------------------- 番号
def normalize_num(text: str) -> str | None:
    """``hsoda113`` / ``HSODA_113`` / ``ebwh00189`` → ``HSODA-113`` / ``EBWH-189``。"""
    m = _NUM.search(text or "")
    if not m:
        return None
    prefix, digits = m.group(1).upper(), m.group(2)
    if not re.search(r"[A-Z]{2}", prefix):
        return None
    return f"{prefix}-{str(int(digits)).zfill(3)}"


def nfo_num(text: str | None) -> str | None:
    m = _NFO_NUM.search(text or "")
    return normalize_num(htmllib.unescape(m.group(1))) if m else None


def item_num(nfo_text: str | None, *texts: str) -> str | None:
    """条目的番号：NFO 的 ``<num>`` 优先，其次标题、文件名。拿不到返回 None。"""
    found = nfo_num(nfo_text)
    if found:
        return found
    for text in texts:
        found = normalize_num(text or "")
        if found:
            return found
    return None


# ---------------------------------------------------------------------- 解析
def _text(fragment: str) -> str:
    return htmllib.unescape(_TAG.sub("", fragment or "")).strip()


def is_challenge(body: str) -> bool:
    """反爬挑战页（HTTP 200 但不是正文）。"""
    head = (body or "")[:4000].lower()
    title = _TITLE.search(head)
    text = title.group(1) if title else ""
    return (
        any(key in text for key in _CHALLENGE)
        or text.strip().startswith("loader")
        or any(key in head for key in ("cf-browser-verification", "challenge-platform"))
    )


def avwiki_complete(body: str, num: str) -> bool:
    """av-wiki 作品页是否完整：含本番号（「品番：<番号>」或番号本身，不分大小写）或「AV女優名」。

    av-wiki 偶尔回 200 的加载页 / 半截页面，这种页面解析不出演员表，但不能当成「没演员表」。
    """
    body = body or ""
    return num.upper() in body.upper() or "AV女優名" in body


@dataclass(frozen=True)
class Member:
    """演员表里的一个人。``ref`` 是站点自己的标识（av-wiki slug / javdb 演员 id / DMM id）。"""

    name: str
    ref: str = ""


def parse_avwiki(body: str, num: str) -> list[Member]:
    """av-wiki 作品页的演员表：「品番：<番号>」那一块里「AV女優名：」后到 ``</li>`` 为止的链接。

    页面下面的相关作品也有「AV女優名：」，所以先找本番号的那一块；找不到再退回第一处。
    """
    body = body or ""
    block = None
    at = body.upper().find(f"品番：{num.upper()}")
    if at >= 0:
        end = body.find("</ul>", at)
        block = body[at : end if end > 0 else at + 4000]
    for chunk in (block, body):
        if not chunk:
            continue
        m = re.search(r"AV女優名：(.*?)</li>", chunk, re.S)
        if m:
            seen: dict[str, Member] = {}
            for slug, name in _AVWIKI_ACTRESS.findall(m.group(1)):
                clean = _text(name)
                if clean and clean not in seen:
                    seen[clean] = Member(clean, unquote(slug))
            return list(seen.values())
    return []


def parse_javdb_search(body: str, num: str) -> str | None:
    """javdb 搜索结果里番号**完全相等**的作品 id。"""
    want = num.upper()
    for vid, inner in _JAVDB_BOX.findall(body or ""):
        strong = _STRONG.search(inner)
        if strong and _text(strong.group(1)).upper() == want:
            return vid
    return None


def parse_javdb_work(body: str) -> list[Member]:
    """javdb 作品页的女演员。"""
    seen: dict[str, Member] = {}
    for pattern in _JAVDB_FEMALE:
        for aid, name in pattern.findall(body or ""):
            clean = _text(name)
            if clean and clean not in seen:
                seen[clean] = Member(clean, aid)
    return list(seen.values())


def javdb_avatar_url(aid: str) -> str:
    return f"https://c0.jdbstatic.com/avatars/{aid[:2].lower()}/{aid}.jpg"


def dmm_guess_slugs(avwiki_slug: str) -> list[str]:
    """av-wiki 的 slug（``kuroki-nami``）猜 DMM 头像文件名：原样、训令式、带 2 后缀。"""
    slug = (avwiki_slug or "").strip().lower()
    if not _ASCII_SLUG.match(slug):
        return []
    base = slug.replace("-", "_")
    kunrei = base
    for hepburn, kun in _KUNREI:
        kunrei = kunrei.replace(hepburn, kun)
    out = [base]
    if kunrei != base:
        out.append(kunrei)
    out.append(base + "2")
    return out


def dmm_actjpg_url(slug: str) -> str:
    return f"https://pics.dmm.co.jp/mono/actjpgs/{slug}.jpg"


def dmm_cid_num(cid: str) -> str | None:
    """DMM 的 content_id（``ebwh00189`` / ``1hsoda113`` / ``h_1234abc00012``）→ 番号。"""
    cid = (cid or "").lower()
    m = re.search(r"([a-z]{2,6})0*(\d{2,5})[a-z]?$", cid)
    if not m:
        return None
    return f"{m.group(1).upper()}-{str(int(m.group(2))).zfill(3)}"


def parse_dmm_items(data: Any, num: str) -> tuple[list[Member], str | None]:
    """ItemList 的结果里番号相等的那一部：(演员表, content_id)。"""
    items = (((data or {}).get("result") or {}).get("items")) if isinstance(data, dict) else None
    for item in items or []:
        if not isinstance(item, dict):
            continue
        cid = str(item.get("content_id") or item.get("product_id") or "")
        if dmm_cid_num(cid) != num.upper():
            continue
        actresses = ((item.get("iteminfo") or {}).get("actress")) or []
        cast = [
            Member(str(a.get("name") or "").strip(), str(a.get("id") or ""))
            for a in actresses
            if isinstance(a, dict) and str(a.get("name") or "").strip()
        ]
        return cast, cid
    return [], None


def parse_dmm_actress(data: Any) -> str | None:
    result = (data or {}).get("result") if isinstance(data, dict) else None
    for actress in (result or {}).get("actress") or []:
        urls = actress.get("imageURL") if isinstance(actress, dict) else None
        if isinstance(urls, dict):
            url = urls.get("large") or urls.get("small")
            if isinstance(url, str) and url.startswith("https://"):
                return url
    return None


def site_for(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    if host.endswith("av-wiki.net"):
        return AVWIKI
    if host.endswith("javdb.com") or host.endswith("jdbstatic.com"):
        return JAVDB
    if host == "api.dmm.com":
        return DMM_API
    if "dmm." in host:
        return DMM
    return host or "other"


# ---------------------------------------------------------------------- 结果
@dataclass
class Candidate:
    source: str  # avwiki / javdb / dmm / dmm_api / gfriends
    how: str  # exact / alias / guess
    name: str  # 演员表里的名字（gfriends 时是 gfriends 里的名字）
    num: str
    page: str
    cast: list[str]
    url: str | None = None  # 来源图片（gfriends 时为空，用 avatar）
    avatar: Avatar | None = None
    via: str | None = None  # gfriends 经别名时：哪个站给的正名

    @property
    def key(self) -> str:
        """待确认里的候选名（确认接口用它定位）。"""
        if self.source == GFRIENDS:
            return self.name
        if self.how == "guess" and self.url:
            return f"DMM 猜测：{self.url.rsplit('/', 1)[-1].rsplit('.', 1)[0]}"
        return f"{SOURCE_LABEL.get(self.source, self.source)}：{self.name}"

    @property
    def auto_ok(self) -> bool:
        return self.how == "exact" and self.source in AUTO_SOURCES and bool(self.url)

    def row(self) -> dict[str, Any]:
        row: dict[str, Any] = {
            "name": self.key,
            "via": "search",
            "kind": "gfriends" if self.source == GFRIENDS else "remote",
            "source": self.source,
            "source_label": SOURCE_LABEL.get(self.source, self.source),
            "how": self.how,
            "how_label": HOW_LABEL.get(self.how, self.how),
            "num": self.num,
            "page": self.page,
            "cast": list(self.cast[:CAST_EXCERPT]),
            "actor_name": self.name,
        }
        if self.avatar is not None:
            row.update(company=self.avatar.company, file=self.avatar.file)
            row["alias_from"] = SOURCE_LABEL.get(self.via or "", self.via)
        if self.url:
            row["url"] = self.url
        return row


@dataclass
class Outcome:
    actor: str
    num: str
    candidates: list[Candidate] = field(default_factory=list)
    blocked: list[str] = field(default_factory=list)  # 本次被拦的来源（显示名）
    tried: list[str] = field(default_factory=list)
    alias: str | None = None  # av-wiki 给出的正名

    @property
    def status(self) -> str:
        if self.candidates:
            return "found"
        if self.blocked:
            return "blocked"
        return "none"

    def summary(self) -> dict[str, Any]:
        return {
            "actor": self.actor,
            "num": self.num,
            "status": self.status,
            "candidates": len(self.candidates),
            "blocked": list(self.blocked),
            "tried": list(self.tried),
            "alias": self.alias,
        }


class Blocked(Exception):
    """来源拒绝了请求（反爬 / 限流 / 挑战页 / 连不上）：本轮暂停这个来源。"""

    def __init__(self, source: str, why: str) -> None:
        super().__init__(f"{SOURCE_LABEL.get(source, source)}：{why}")
        self.source = source
        self.why = why


# ---------------------------------------------------------------------- 请求器
class Searcher:
    """同一站点串行、相邻请求至少间隔 ``search_interval_s`` 秒；``sleep`` / ``clock`` 可注入。"""

    def __init__(
        self,
        *,
        http: Any,
        settings: Any,
        logger: Any = None,
        sleep: Any = asyncio.sleep,
        clock: Any = time.monotonic,
    ) -> None:
        self.http = http
        self.settings = settings
        self.logger = logger
        self.sleep = sleep
        self.clock = clock
        self._locks: dict[str, asyncio.Lock] = {}
        self._last: dict[str, float] = {}
        self.requests: int = 0

    # -------------------------------------------------------------- 配置
    def _secret(self, name: str) -> str:
        value = getattr(self.settings, name, None)
        getter = getattr(value, "get_secret_value", None)
        return (getter() if getter else str(value or "")).strip()

    def api_configured(self) -> bool:
        return bool(self._secret("dmm_api_id") and self._secret("dmm_affiliate_id"))

    def enabled_sources(self) -> list[str]:
        s = self.settings
        out = []
        if s.search_avwiki:
            out.append(AVWIKI)
        if s.search_javdb:
            out.append(JAVDB)
        if s.search_dmm:
            if self.api_configured():
                out.append(DMM_API)
            out.append(DMM)
        return out

    def scrub(self, text: str) -> str:
        """万一有字符串里混进了密钥，替换掉（事件 / 日志 / 状态写入前都过一遍）。"""
        for name in ("dmm_api_id", "dmm_affiliate_id"):
            secret = self._secret(name)
            if secret and len(secret) >= 3:
                text = text.replace(secret, "***")
        return text

    # -------------------------------------------------------------- 请求
    async def _get(
        self,
        site: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        lock = self._locks.setdefault(site, asyncio.Lock())
        async with lock:
            gap = float(self.settings.search_interval_s)
            last = self._last.get(site)
            if last is not None:
                wait = last + gap - self.clock()
                if wait > 0:
                    await self.sleep(wait)
            self._last[site] = self.clock()
            self.requests += 1
            try:
                return await self.http.get(
                    url,
                    params=params,
                    headers={**BROWSER_HEADERS, **(headers or {})},
                    follow_redirects=False,
                )
            except httpx.HTTPError as exc:
                # 不记异常原文：DMM API 的地址里带密钥
                if self.logger is not None:
                    self.logger.info(
                        "actor-avatar：搜索 %s 请求失败（%s）",
                        SOURCE_LABEL.get(site, site),
                        type(exc).__name__,
                    )
                raise Blocked(site, "连不上") from None

    async def _page(self, site: str, url: str, **kw: Any) -> str | None:
        """正文页：404 = 没有这一页（None）；403 / 429 / 5xx / 挑战页 = 被拦。"""
        resp = await self._get(site, url, **kw)
        if resp.status_code == 404:
            return None
        if resp.status_code in (403, 429, 503) or resp.status_code >= 500:
            raise Blocked(site, f"HTTP {resp.status_code}")
        if 300 <= resp.status_code < 400:
            raise Blocked(site, "被跳转到验证页")
        if resp.status_code != 200:
            return None
        body = resp.text
        if is_challenge(body):
            raise Blocked(site, "反爬挑战页")
        return body

    async def image_ok(self, url: str) -> bool:
        """是不是一张真实图片：200、image/*、不跳转、大小合理、不是占位图。"""
        if not isinstance(url, str) or not url.startswith("https://"):
            return False
        if any(hint in url for hint in DMM_NOIMAGE_HINTS):
            return False
        site = site_for(url)
        try:
            resp = await self._get(site, url)
        except Blocked:
            return False
        if resp.status_code == 429:
            raise Blocked(site, "HTTP 429")
        if resp.status_code != 200:
            return False  # 3xx（DMM 缺图跳 now_printing）、403（javdb 没头像）
        ctype = (resp.headers.get("content-type") or "").lower()
        if not ctype.startswith("image/"):
            return False
        body = resp.content
        if not (MIN_IMAGE <= len(body) <= MAX_IMAGE):
            return False
        if not body.startswith(_MAGIC) and not (body.startswith(b"RIFF") and body[8:12] == b"WEBP"):
            return False
        return hashlib.sha1(body).hexdigest() not in DMM_NOIMAGE_SHA1

    # -------------------------------------------------------------- 各来源
    async def avwiki_cast(self, num: str) -> tuple[list[Member], str]:
        """av-wiki 演员表。页面不完整（加载页 / 半截页）时隔一个请求间隔重试 1 次，仍不行算被拦。"""
        page = f"https://av-wiki.net/{num.lower()}/"
        for _attempt in range(2):
            body = await self._page(AVWIKI, page)  # 同站点相邻请求自带间隔
            if body is None:
                return [], page
            if avwiki_complete(body, num):
                # 含本番号但没演员链接 = 这片子确实没演员表，不是被拦
                return parse_avwiki(body, num), page
        raise Blocked(AVWIKI, "页面不完整")

    async def javdb_cast(self, num: str) -> tuple[list[Member], str | None]:
        cookie = {"Cookie": JAVDB_COOKIE}
        body = await self._page(
            JAVDB, "https://javdb.com/search", params={"q": num, "f": "all"}, headers=cookie
        )
        vid = parse_javdb_search(body or "", num)
        if not vid:
            return [], None
        page = f"https://javdb.com/v/{vid}"
        work = await self._page(JAVDB, page, headers=cookie)
        return parse_javdb_work(work or ""), page

    async def _dmm_api(self, endpoint: str, params: dict[str, Any]) -> Any:
        query = {
            "api_id": self._secret("dmm_api_id"),
            "affiliate_id": self._secret("dmm_affiliate_id"),
            "output": "json",
            **params,
        }
        resp = await self._get(DMM_API, DMM_API_BASE + endpoint, params=query)
        if resp.status_code in (400, 401, 403):
            raise Blocked(DMM_API, "接口拒绝（检查 api_id / affiliate_id）")
        if resp.status_code == 429 or resp.status_code >= 500:
            raise Blocked(DMM_API, f"HTTP {resp.status_code}")
        if resp.status_code != 200:
            return None
        try:
            data = resp.json()
        except ValueError:
            return None
        status = (
            ((data or {}).get("result") or {}).get("status") if isinstance(data, dict) else None
        )
        if status not in (None, 200, "200"):
            raise Blocked(DMM_API, "接口返回错误（检查 api_id / affiliate_id）")
        return data

    async def dmm_api_cast(self, num: str) -> tuple[list[Member], str | None]:
        for service in ({"service": "digital", "floor": "videoa"}, {"service": "mono"}):
            data = await self._dmm_api(
                "ItemList", {"site": "FANZA", **service, "keyword": num, "hits": 10}
            )
            cast, cid = parse_dmm_items(data, num)
            if cid:
                return cast, cid
        return [], None

    async def dmm_api_image(self, actress_id: str) -> str | None:
        if not actress_id:
            return None
        return parse_dmm_actress(await self._dmm_api("ActressSearch", {"actress_id": actress_id}))

    # -------------------------------------------------------------- 一位演员
    async def search(
        self,
        actor: str,
        num: str,
        *,
        index: Any,
        aliases: dict[str, str] | None = None,
        table: Any = None,
        paused: set[str] | None = None,
    ) -> Outcome:
        """按番号认人、找头像。``paused`` 是本轮被拦的来源（会被就地更新）。"""
        paused = paused if paused is not None else set()
        out = Outcome(actor=actor, num=num)
        wanted: dict[str, str] = {fold(actor): "exact"}
        slugs: list[str] = []
        s = self.settings

        def blocked(exc: Blocked) -> None:
            paused.add(exc.source)
            label = SOURCE_LABEL.get(exc.source, exc.source)
            if label not in out.blocked:
                out.blocked.append(label)

        # 1) av-wiki：认人、拿正名与罗马字 slug（它本身不给图）
        if s.search_avwiki and AVWIKI not in paused:
            try:
                cast, page = await self.avwiki_cast(num)
            except Blocked as exc:
                blocked(exc)
            else:
                out.tried.append(SOURCE_LABEL[AVWIKI])
                names = [m.name for m in cast]
                same = next((m for m in cast if fold(m.name) == fold(actor)), None)
                if same is not None:
                    slugs.append(same.ref)
                elif len(cast) == 1:
                    canon = cast[0]
                    out.alias = canon.name
                    slugs.append(canon.ref)
                    hit = index.lookup(canon.name, aliases, table) if index is not None else None
                    if hit is not None:
                        out.candidates.append(
                            Candidate(
                                source=GFRIENDS,
                                how="alias",
                                name=hit.avatar.name,
                                num=num,
                                page=page,
                                cast=names,
                                avatar=hit.avatar,
                                via=AVWIKI,
                            )
                        )
                        return out
                    wanted.setdefault(fold(canon.name), "alias")

        # 2) javdb：演员表 + 演员头像
        if s.search_javdb and JAVDB not in paused:
            try:
                cast, page = await self.javdb_cast(num)
                out.tried.append(SOURCE_LABEL[JAVDB])
                for member in cast:
                    how = wanted.get(fold(member.name))
                    if not how:
                        continue
                    url = javdb_avatar_url(member.ref)
                    if await self.image_ok(url):
                        out.candidates.append(
                            Candidate(
                                source=JAVDB,
                                how=how,
                                name=member.name,
                                num=num,
                                page=page or "",
                                cast=[m.name for m in cast],
                                url=url,
                            )
                        )
                        return out
            except Blocked as exc:
                blocked(exc)

        if not s.search_dmm:
            return out
        # 3a) DMM 官方 API（配了 api_id + affiliate_id 才用）
        if self.api_configured() and DMM_API not in paused:
            try:
                cast, cid = await self.dmm_api_cast(num)
                out.tried.append(SOURCE_LABEL[DMM_API])
                for member in cast:
                    how = wanted.get(fold(member.name))
                    if not how:
                        continue
                    url = await self.dmm_api_image(member.ref)
                    if url and await self.image_ok(url):
                        out.candidates.append(
                            Candidate(
                                source=DMM_API,
                                how=how,
                                name=member.name,
                                num=num,
                                page=f"https://www.dmm.co.jp/digital/videoa/-/detail/=/cid={cid}/",
                                cast=[m.name for m in cast],
                                url=url,
                            )
                        )
                        return out
            except Blocked as exc:
                blocked(exc)
        # 3b) DMM 按 slug 猜：只能进待确认，标「猜测」
        if DMM not in paused:
            tried_guess = False
            for slug in dict.fromkeys(g for ref in slugs for g in dmm_guess_slugs(ref)):
                tried_guess = True
                url = dmm_actjpg_url(slug)
                try:
                    ok = await self.image_ok(url)
                except Blocked as exc:
                    blocked(exc)
                    break
                if ok:
                    out.candidates.append(
                        Candidate(
                            source=DMM,
                            how="guess",
                            name=out.alias or actor,
                            num=num,
                            page=url,
                            cast=[out.alias or actor],
                            url=url,
                        )
                    )
            if tried_guess:
                out.tried.append(SOURCE_LABEL[DMM])
        return out
