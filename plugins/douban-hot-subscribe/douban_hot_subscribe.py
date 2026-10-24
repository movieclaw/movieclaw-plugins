"""豆瓣热门自动订阅：豆瓣热门榜单里新冒出来的电影 / 剧集，自动变成 MovieClaw 订阅。

数据来源是 MovieClaw 自己的「发现」片单（豆瓣榜单），插件不直接连豆瓣、不需要 Cookie，
只调宿主操作 ``discover.browse-collection`` 读榜、``subscriptions.create`` 建订阅。

一轮同步：
1. 按设置里的片单列表（默认「实时热门电影」「实时热门剧集」）逐个读榜；
2. 逐条过筛：只取前 N 名、评分下限、标题排除词、库里已有就跳过、处理过的跳过；
3. 剩下的按「每轮上限」（默认 5 条）一条条建订阅——电影直接订；剧集按标题里的季号
   （没有就第 1 季）订，并按设置决定补不补已播出的集、要不要自动续订；
   这一轮没订完的下一轮接着订，榜单再长也会自己咬完；
4. 从一开始就分不清的（豆瓣条目对应多部作品、TMDB 还没收录）记进「待确认」，
   用 ``mclaw plugins douban-hot-subscribe pending`` 看、``resolve`` 定，不自己瞎猜。

默认口径是「榜单里的全要」：不看评分（新片常常还没出分）、不限名次、不限排除词；
只靠「每轮 5 条」控节奏，避免一下子给下载器排几十个种子。想要更挑一点，
去插件设置里调评分下限 / 只取前几名 / 排除词。
"""

import re
import time
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from movieclaw_api.plugins.keys import PLUGIN_DATA, PLUGIN_ROUTES
from movieclaw_scheduler import SCHEDULED_TASKS, TaskDefinition, TriggerType
from movieclaw_sdk import Context, plugin
from movieclaw_sdk.events import PLUGIN_EVENTS, Publisher, plugin_event
from movieclaw_sdk.ops import HOST_OPS, OpsError

PLUGIN_ID = "douban-hot-subscribe"
SYNC_TASK = f"{PLUGIN_ID}.sync"

DEFAULT_COLLECTIONS = ("实时热门电影", "实时热门剧集")

#: 片单别名 → 豆瓣片单引用。别名是给设置表单用的：片单引用太长，用户记不住。
PRESETS: dict[str, str] = {
    # 电影
    "实时热门电影": "douban:movie:movie_real_time_hotest",
    "热门电影": "douban:movie:movie_hot_gaia",
    "影院热映": "douban:movie:movie_showing",
    "即将上映": "douban:movie:movie_soon",
    "一周口碑电影": "douban:movie:movie_weekly_best",
    "高分电影": "douban:movie:movie_high_score",
    "Top250": "douban:movie:movie_top250",
    "经典电影": "douban:movie:movie_classic",
    "科幻片": "douban:movie:movie_scifi",
    "喜剧片": "douban:movie:movie_comedy",
    "动作片": "douban:movie:movie_action",
    "爱情片": "douban:movie:movie_love",
    # 剧集
    "实时热门剧集": "douban:tv:tv_real_time_hotest",
    "近期热门剧集": "douban:tv:tv_hot",
    "华语口碑剧集": "douban:tv:tv_chinese_best_weekly",
    "全球口碑剧集": "douban:tv:tv_global_best_weekly",
    "热门国产剧": "douban:tv:tv_domestic",
    "热门美剧": "douban:tv:tv_american",
    "热门日剧": "douban:tv:tv_japanese",
    "热门韩剧": "douban:tv:tv_korean",
    "热门动画": "douban:tv:tv_animation",
    "国内综艺": "douban:tv:show_domestic",
    "国外综艺": "douban:tv:show_foreign",
}

DONE_KEY = "done"            # [片单条目引用, …]，最近处理的在前
PENDING_KEY = "pending"      # {引用: {title, kind, reason, candidates, collection, at}}
STATS_KEY = "stats"          # {"runs", "subscribed", "skipped", "failed", "waiting"}
RECENT_KEY = "recent"        # [{at, line}] 最近做了什么
BASELINE_KEY = "baseline"    # {"at": 时间戳, "count": 条数}，第一次同步只记基线的标记

DONE_LIMIT = 1000            # 记多少条处理过的（插件数据单个值有 64 KB 上限）
PENDING_LIMIT = 50
RECENT_SIZE = 60
DEFAULT_FETCH_LIMIT = 30     # 一个片单一轮读多少条（设置里限了前 N 名时按 N 读）
MAX_PER_RUN_CEILING = 50

# 结果文案前缀：临时失败的条目不记 done、不占本轮预算，下一轮重试
TEMPORARY_PREFIX = "暂时失败"
SKIPPED = "已经有订阅，跳过"

_CN_DIGITS = {
    "零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}
_CN_SEASON = re.compile(r"第\s*([0-9]{1,2}|[零一二两三四五六七八九十]{1,3})\s*[季部]")
_EN_SEASON = re.compile(r"(?i)(?:season|series)\s*(\d{1,2})(?![0-9])")
_S_SEASON = re.compile(r"(?<![A-Za-z0-9])S(\d{1,2})(?![0-9])")
_SPLIT = re.compile(r"[,，;；、\r\n]+")
_ALIAS_REF = re.compile(r"^(?:douban|tmdb):(?:movie|tv):[A-Za-z0-9_\-]+$")


class HotUnavailable(Exception):
    """这一轮读榜 / 建订阅没法进行：原因可直接给用户看。"""


class Config(BaseModel):
    collections: list[str] = Field(
        default_factory=lambda: list(DEFAULT_COLLECTIONS),
        title="盯哪些片单",
        description=(
            "可用的写法：实时热门电影、热门电影、影院热映、一周口碑电影、高分电影、Top250、"
            "实时热门剧集、近期热门剧集、华语口碑剧集、全球口碑剧集、热门国产剧、热门美剧、"
            "热门日剧、热门韩剧、热门动画、国内综艺…，也可以直接粘片单引用（douban:movie:xxx、"
            "tmdb:tv:xxx 这类，tmdb 的榜单也能盯；用 mclaw plugins douban-hot-subscribe "
            "collections 看全部）"
        ),
    )
    min_rating: float = Field(
        0,
        title="豆瓣评分下限",
        description="低于这个分的条目不订；默认 0 = 不看评分（新片常常还没有分，或者分数还没同步过来）",
        ge=0,
        le=10,
    )
    skip_unrated: bool = Field(
        False,
        title="暂无评分的不要",
        description="默认关：豆瓣还没出分（0 分）的照订",
    )
    max_rank: int = Field(
        0,
        title="每个片单只取前几名",
        description="0 = 整个片单都要（热门榜 10～30 条），填 5 就是只盯前 5 名",
        ge=0,
        le=250,
    )
    exclude_keywords: list[str] = Field(
        default_factory=list,
        title="标题含这些词就跳过",
        description="一行一个，如：综艺、短剧、预告、演唱会；英文词按整个单词匹配（TC 不会命中 TCA）",
    )
    max_per_run: int = Field(
        5,
        title="每轮最多订阅几条",
        description="一轮最多订这么多条，没订完的下一轮接着订（1～50）",
        ge=1,
        le=MAX_PER_RUN_CEILING,
    )
    first_run: Literal["since_now", "history"] = Field(
        "since_now",
        title="第一次同步",
        description=(
            "since_now = 只订以后新进榜的，现在的榜当历史（默认，装上一键就跑不会一次性订一堆）；"
            "history = 当前榜上的全都订（会分几轮订完）"
        ),
    )
    tv_backfill: bool = Field(
        True,
        title="剧集补已播出的集",
        description="开：订阅时把已经播出的集一起订；关：只从订阅之后播出的新集开始追",
    )
    tv_follow_future: bool = Field(
        True,
        title="剧集自动续订",
        description="以后播出的新集、新季自动纳入订阅",
    )
    skip_owned: bool = Field(
        True,
        title="库里已有就跳过",
        description="榜上的片媒体库里已经有了（在位的文件）就不再订阅",
    )


class Subscribed(BaseModel):
    model_config = ConfigDict(frozen=True)

    title: str
    kind: str
    collection: str
    detail: str
    subscription_id: int | None = None


SUBSCRIBED = plugin_event(
    f"{PLUGIN_ID}.subscribed",
    Subscribed,
    title="豆瓣热门已转成订阅",
    doc="豆瓣热门榜单里的新片自动订阅成功时发出",
)


# --------------------------------------------------------------- 纯函数（可单独自测）


def normalize_collections(values: list[str] | str | None) -> tuple[list[str], list[str]]:
    """设置里的片单 → (片单引用列表, 认不出的原文)。别名、引用都收，去重保序。"""
    if isinstance(values, str):
        entries = _SPLIT.split(values)
    else:
        entries = []
        for item in values or []:
            entries.extend(_SPLIT.split(str(item)))
    refs: list[str] = []
    bad: list[str] = []
    for entry in entries:
        text = entry.strip()
        if not text:
            continue
        ref = PRESETS.get(text, text if _ALIAS_REF.match(text) else "")
        if not ref:
            bad.append(text)
        elif ref not in refs:
            refs.append(ref)
    return refs, bad


def cjk_number(text: str) -> int | None:
    """「十」「十二」「二十一」这类中文数字 → 整数（只处理 0～99）。"""
    if text.isdigit():
        return int(text)
    if not text:
        return None
    if text == "十":
        return 10
    if text.startswith("十"):
        rest = _CN_DIGITS.get(text[1:])
        return 10 + rest if rest else None
    if "十" in text:
        head, _, tail = text.partition("十")
        tens = _CN_DIGITS.get(head)
        ones = _CN_DIGITS.get(tail) if tail else 0
        if tens is None or ones is None:
            return None
        return tens * 10 + ones
    return _CN_DIGITS.get(text)


def season_marker(title: str) -> int | None:
    """标题里写的季号：第 2 季 / 第二季 / Season 2 / S02；没写返回 None。"""
    for pattern in (_CN_SEASON, _EN_SEASON, _S_SEASON):
        hit = pattern.search(title or "")
        if not hit:
            continue
        number = cjk_number(hit.group(1))
        if number and 0 < number <= 99:
            return number
    return None


def season_plan(title: str, kind: str, backfill: bool) -> tuple[list[int], bool, str]:
    """订阅参数里的选季 + 是否自动续订 + 一句说明（纯规则，不发请求）。

    返回 ``(季号列表, follow_future, 说明)``。剧集关了「补已播出的集」时季号留空、
    改成追新——否则订阅会落库成 0 工单，一集都不会搜。
    """
    if kind != "tv":
        return [], False, "电影"
    follow = True
    if not backfill:
        return [], follow, "只追订阅之后播出的新集"
    season = season_marker(title)
    if season is not None:
        return [season], follow, f"按第 {season} 季订阅"
    return [1], follow, "标题没写第几季，按第 1 季订阅"


def is_permanent_error(exc: OpsError) -> bool:
    """这次订阅失败是不是「重试也没用」。

    只有明确的客户端错误才算（参数不对、被拒绝）。5xx、超时、429、鉴权过期都是
    暂时的，下一轮该再试——写进 ``done`` 名单等于永久放弃这条，而热门榜上的片子
    掉出榜再进榜是常态，误记一次就再也订不上了。
    """
    status = getattr(exc, "status", None)
    if not isinstance(status, int):
        return False
    if status >= 500 or status in (401, 403, 408, 425, 429):
        return False
    return 400 <= status < 500


def seasons_missing(wanted: list[int], got) -> list[int]:
    """宿主实际建上的季 vs 我们想要的季 → 没建上的那些。

    ``subscriptions.create`` 偶尔会「成功但把选季丢掉」（TMDB 里没这一季时），
    接口不报错、订阅却是空的。``got`` 不是列表时无从判断，返回空表示不追究。
    """
    if not isinstance(got, list):
        return []
    have = set()
    for value in got:
        try:
            have.add(int(value))
        except (TypeError, ValueError):
            continue
    return [int(season) for season in wanted or [] if int(season) not in have]


def keyword_hit(title: str, keywords: list[str] | tuple[str, ...]) -> str | None:
    """标题命中了哪个排除词；没命中返回 None。

    英文短词按词边界匹配（TC 不命中 TCA、TS 不命中 Hits），中文词按子串匹配。
    """
    text = (title or "").lower()
    for word in keywords or []:
        token = (word or "").strip()
        if not token:
            continue
        if token.isascii():
            pattern = r"(?<![A-Za-z0-9])" + re.escape(token.lower()) + r"(?![A-Za-z0-9])"
            if re.search(pattern, text):
                return token
        elif token.lower() in text:
            return token
    return None


def _owned(item: dict) -> bool:
    """榜上这条是不是已经在库里（接口给的库存摘要）。"""
    status = item.get("library_status")
    return bool(status) and bool(
        isinstance(status, dict) and (status.get("file_count") or status.get("library_count"))
    )


def select_titles(
    items: list[dict],
    *,
    max_rank: int = 0,
    min_rating: float = 0.0,
    skip_unrated: bool = False,
    exclude_keywords: list[str] | tuple[str, ...] = (),
    skip_owned: bool = True,
    done: set[str] | None = None,
) -> tuple[list[dict], list[dict]]:
    """榜单条目 → (要订的, 跳过的)。纯函数：不读配置、不发请求。

    跳过的每条带一句人话原因，记进日志与「最近动态」，用户在插件状态里看得到为什么没订。
    """
    done = done or set()
    queue: list[dict] = []
    skipped: list[dict] = []
    seen: set[str] = set()
    for index, item in enumerate(items):
        ref = str(item.get("title_ref") or "")
        title = (item.get("title") or "").strip()
        if not ref or not title or ref in seen:
            continue
        seen.add(ref)
        rank = int(item.get("rank") or index + 1)
        if ref in done:
            continue
        if max_rank and rank > max_rank:
            skipped.append({"title": title, "reason": f"只取前 {max_rank} 名，这条第 {rank}"})
            continue
        rating = float(item.get("provider_rating") or 0)
        if rating <= 0:
            if skip_unrated:
                skipped.append({"title": title, "reason": "豆瓣还没出评分"})
                continue
        elif min_rating and rating < min_rating:
            skipped.append({"title": title, "reason": f"豆瓣 {rating:g} 分，低于下限 {min_rating:g}"})
            continue
        hit = keyword_hit(title, exclude_keywords)
        if hit:
            skipped.append({"title": title, "reason": f"标题含排除词「{hit}」"})
            continue
        if skip_owned and _owned(item):
            skipped.append({"title": title, "reason": "媒体库里已经有这部"})
            continue
        queue.append({**item, "rank": rank})
    return queue, skipped


def interleave(rows: list[dict], key: str = "collection") -> list[dict]:
    """多榜单交错合并：轮流从每个榜单取一条（组内保序）。

    两个榜拼起来会变成「电影榜全部 → 剧集榜全部」：电影榜 10 条、每轮 5 条时，
    剧集要等两轮才轮到，看着像插件漏了剧集。交错之后两个榜齐头并进。
    """
    groups: dict[str, list[dict]] = {}
    for row in rows:
        groups.setdefault(str(row.get(key) or ""), []).append(row)
    out: list[dict] = []
    index = 0
    while True:
        added = False
        for group in groups.values():
            if index < len(group):
                out.append(group[index])
                added = True
        if not added:
            return out
        index += 1


def pick_candidates(details: list[dict] | None) -> list[dict]:
    """歧义候选 → 给用户看的清单（只留必要字段）。"""
    out = []
    for item in details or []:
        if not isinstance(item, dict):
            continue
        ref = item.get("title_ref") or ""
        if ref:
            out.append({"title_ref": ref, "title": item.get("title") or "", "year": item.get("year")})
    return out


def title_key(title: str, year: object) -> str:
    """已有订阅的比对键：片名（去掉空白、统一小写）+ 年份。"""
    text = re.sub(r"\s+", "", (title or "")).lower()
    return f"{text}|{year if isinstance(year, int) else ''}"


# --------------------------------------------------------------- 插件本体


@plugin(
    PLUGIN_ID,
    title="豆瓣热门自动订阅",
    inject=(HOST_OPS, PLUGIN_DATA, PLUGIN_ROUTES, PLUGIN_EVENTS),
    permissions=(
        "discover.list-collections",
        "discover.browse-collection",
        "subscriptions.create",
        "subscriptions.list",
    ),
    config=Config,
)
async def apply(ctx: Context) -> None:
    ops = await ctx.use(HOST_OPS).client(ctx)
    store = ctx.use(PLUGIN_DATA).scoped(ctx)
    routes = ctx.use(PLUGIN_ROUTES)
    log = ctx.logger
    config: Config = ctx.config
    events: Publisher = await ctx.use(PLUGIN_EVENTS).register(ctx, SUBSCRIBED)

    busy = {"now": False}

    # ------------------------------------------------------------ 状态读写

    async def done_refs() -> list[str]:
        return list(await store.get(DONE_KEY, default=[]) or [])

    async def mark_done(ref: str) -> None:
        rows = await done_refs()
        rows = [ref, *(row for row in rows if row != ref)][:DONE_LIMIT]
        await store.set(DONE_KEY, rows)

    async def mark_done_many(refs: list[str]) -> None:
        if not refs:
            return
        fresh = set(refs)
        rows = await done_refs()
        rows = [*refs, *(row for row in rows if row not in fresh)][:DONE_LIMIT]
        await store.set(DONE_KEY, rows)

    async def pending() -> dict:
        return await store.get(PENDING_KEY, default={}) or {}

    async def recent(line: str) -> None:
        rows = await store.get(RECENT_KEY, default=[]) or []
        rows = [{"at": int(time.time()), "line": line}, *rows][:RECENT_SIZE]
        await store.set(RECENT_KEY, rows)

    async def bump(key: str, step: int = 1) -> None:
        stats = await store.get(STATS_KEY, default={}) or {}
        stats[key] = int(stats.get(key, 0)) + step
        await store.set(STATS_KEY, stats)

    async def baseline() -> dict:
        data = await store.get(BASELINE_KEY, default={}) or {}
        return data if isinstance(data, dict) else {}

    async def hold(item: dict, reason: str, candidates: list[dict] | None = None) -> None:
        rows = await pending()
        rows[str(item["title_ref"])] = {
            "title": item.get("title") or "",
            "kind": item.get("media_type") or "movie",
            "reason": reason,
            "candidates": candidates or [],
            "collection": item.get("collection") or "",
            "at": int(time.time()),
        }
        if len(rows) > PENDING_LIMIT:
            keep = sorted(rows.items(), key=lambda kv: kv[1].get("at", 0))[-PENDING_LIMIT:]
            rows = dict(keep)
        await store.set(PENDING_KEY, rows)
        await mark_done(str(item["title_ref"]))
        await bump("waiting")
        await recent(f"《{item.get('title')}》要你定一下：{reason}")
        log.info("《%s》(%s) 需人工确认：%s", item.get("title"), item.get("title_ref"), reason)

    # ------------------------------------------------------------ 读榜

    async def collection_names(refs: list[str]) -> dict[str, str]:
        """片单引用 → 名称（读写榜时展示用；读不到就用引用）。豆瓣与 TMDB 的榜单目录都认。"""
        names: dict[str, str] = {}
        providers = {ref.split(":", 1)[0] for ref in refs} or {"douban"}
        for provider in sorted(providers):
            for media_type in ("movie", "tv"):
                try:
                    data = await ops.call(
                        "discover.list-collections",
                        {"provider": provider, "media_type": media_type},
                    )
                except OpsError as exc:
                    log.warning("读片单目录失败（%s/%s）：%s", provider, media_type, exc.message)
                    continue
                for row in (data or {}).get("collections") or []:
                    ref = row.get("collection_ref")
                    if ref:
                        names[ref] = row.get("name") or ref
        return names

    async def fetch_collection(ref: str, name: str, limit: int) -> list[dict]:
        """读一个片单，返回条目（附上 rank 与来源片单名）。"""
        data = await ops.call("discover.browse-collection", {"collection_ref": ref, "limit": limit})
        titles = (data or {}).get("titles") or []
        out = []
        for index, row in enumerate(titles):
            if not isinstance(row, dict) or not row.get("title_ref"):
                continue
            out.append({**row, "rank": index + 1, "collection": name or ref})
        return out

    # ------------------------------------------------------------ 订阅

    async def existing() -> tuple[set[str], set[str]]:
        """(已有订阅的豆瓣条目 id, 已有订阅的片名+年份)，用来跳过重复订阅。"""
        try:
            rows = await ops.call("subscriptions.list", {})
        except OpsError as exc:
            log.warning("读订阅列表失败：%s", exc.message)
            return set(), set()
        douban_ids: set[str] = set()
        keys: set[str] = set()
        for row in rows or []:
            media = row.get("media") or {}
            if media.get("douban_id"):
                douban_ids.add(str(media["douban_id"]))
            keys.add(title_key(media.get("title") or "", media.get("year")))
        return douban_ids, keys

    def already_subscribed(item: dict, douban_ids: set[str], keys: set[str]) -> bool:
        ref = str(item.get("title_ref") or "")
        if ref.startswith("douban:") and ref.split(":")[-1] in douban_ids:
            return True
        return title_key(item.get("title") or "", item.get("release_year")) in keys

    def create_body(item: dict, title_ref: str) -> tuple[dict, str]:
        kind = item.get("media_type") or "movie"
        seasons, _follow, why = season_plan(item.get("title") or "", kind, config.tv_backfill)
        body: dict = {"title_ref": title_ref, "selected_seasons": seasons}
        if kind == "tv":
            # 不补已播出的集时季号留空，必须靠追新撑着，否则订阅会落库成 0 工单
            body["follow_future"] = bool(config.tv_follow_future) if seasons else True
        if title_ref.startswith("tmdb:") and str(item.get("title_ref") or "").startswith("douban:"):
            # 从豆瓣歧义候选里改选了 TMDB 条目：把豆瓣身份带回去，之后按豆瓣集数对账。
            # 宿主只接受豆瓣引用，条目本身是 tmdb 时不能把 tmdb 引用塞进来（会 400）。
            body["source_title_ref"] = str(item["title_ref"])
        return body, why

    async def subscribe(item: dict) -> dict:
        """建订阅；剧集季号在 TMDB 里不存在时退回「只追以后的新集」。"""
        body, why = create_body(item, str(item["title_ref"]))
        try:
            created = await ops.call("subscriptions.create", body)
        except OpsError as exc:
            no_such_season = exc.status == 400 and "季" in (exc.message or "")
            if not no_such_season:
                raise
            body.update(selected_seasons=[], follow_future=True)
            created = await ops.call("subscriptions.create", body)
            why = f"{why}，但 TMDB 里没有这一季，已改成只追以后播出的新集"
        sub = (created or {}).get("subscription") or {}
        wanted = body.get("selected_seasons") or []
        missing = seasons_missing(wanted, sub.get("selected_seasons"))
        if missing:
            log.warning(
                "《%s》(%s) 订阅 %s 没建上第 %s 季：宿主返回的 selected_seasons=%r",
                item.get("title"), item.get("title_ref"), sub.get("id"),
                "、".join(str(s) for s in missing), sub.get("selected_seasons"),
            )
            why = (
                f"{why}，但订阅实际没选上第 {'、'.join(str(s) for s in missing)} 季"
                "（TMDB 里可能没有这一季），去订阅详情里手动确认一下"
            )
        return {"subscription": sub, "why": why}

    async def handle(item: dict, douban_ids: set[str], keys: set[str]) -> str:
        """榜上一条 → 结果描述。"""
        title = item.get("title") or ""
        if already_subscribed(item, douban_ids, keys):
            await mark_done(str(item["title_ref"]))
            await bump("skipped")
            return SKIPPED
        try:
            result = await subscribe(item)
        except OpsError as exc:
            if exc.code == "SUBSCRIPTION_TARGET_AMBIGUOUS":
                await hold(item, "豆瓣条目对应多部作品，要你挑一个", pick_candidates(exc.details))
                return "待确认（多部候选）"
            if exc.status == 404:
                await hold(item, "TMDB 里还没收录这部，等收录后再来", None)
                return "待确认（TMDB 暂无）"
            if not is_permanent_error(exc):
                # 临时失败：不写 done，下一轮还会再试（榜单会一直读到它）
                await bump("failed")
                await recent(f"《{title}》订阅暂时失败，下一轮重试：{exc.message}")
                log.warning(
                    "《%s》(%s) 订阅暂时失败，下一轮重试：%s", title, item.get("title_ref"), exc.message
                )
                return f"{TEMPORARY_PREFIX}（下一轮重试）：{exc.message}"
            await mark_done(str(item["title_ref"]))
            await bump("failed")
            await recent(f"《{title}》订阅失败：{exc.message}")
            log.warning("《%s》(%s) 订阅失败：%s", title, item.get("title_ref"), exc.message)
            return f"失败：{exc.message}"
        sub = result["subscription"]
        sub_id = sub.get("id")
        detail = f"{result['why']}（订阅 #{sub_id}）"
        ref = str(item["title_ref"])
        await mark_done(ref)
        if ref.startswith("douban:"):
            douban_ids.add(ref.split(":")[-1])
        keys.add(title_key(title, item.get("release_year")))
        await bump("subscribed")
        await recent(f"《{title}》{detail}")
        log.info("《%s》(%s) %s", title, ref, detail)
        try:
            await events.publish(
                SUBSCRIBED,
                Subscribed(
                    title=title,
                    kind=item.get("media_type") or "movie",
                    collection=item.get("collection") or "",
                    detail=detail,
                    subscription_id=sub_id if isinstance(sub_id, int) else None,
                ),
            )
        except Exception:  # noqa: BLE001 -- 通知发不出去不影响订阅本身
            log.warning("发「已订阅」事件失败：%s", title, exc_info=True)
        return detail

    # ------------------------------------------------------------ 同步

    async def sync_once(dry_run: bool = False) -> dict:
        refs, bad = normalize_collections(config.collections)
        if not refs:
            raise HotUnavailable(
                "还没配片单：去插件「设置」里选，如 实时热门电影、实时热门剧集"
                + (f"（认不出的写法：{'、'.join(bad)}）" if bad else "")
            )
        if busy["now"] and not dry_run:
            raise HotUnavailable("上一轮同步还没跑完，稍等")
        # 预览是只读的，不占同步互斥锁，免得把正在跑的定时任务的状态清掉
        if not dry_run:
            busy["now"] = True
        try:
            names = await collection_names(refs)
            fetch_limit = config.max_rank or DEFAULT_FETCH_LIMIT
            rows: list[dict] = []
            errors: list[dict] = []
            for ref in refs:
                name = names.get(ref) or ref
                try:
                    rows.extend(await fetch_collection(ref, name, fetch_limit))
                except OpsError as exc:
                    errors.append({"collection": name, "error": exc.message})
                    log.warning("读片单「%s」失败：%s", name, exc.message)

            done = set(await done_refs())
            # 先交错再筛：每轮从各榜轮流取，两个榜齐头并进
            queue, skipped = select_titles(
                interleave(rows),
                max_rank=config.max_rank,
                min_rating=config.min_rating,
                skip_unrated=config.skip_unrated,
                exclude_keywords=config.exclude_keywords,
                skip_owned=config.skip_owned,
                done=done,
            )

            marks = await baseline()
            first = not marks and config.first_run != "history"
            if first:
                if dry_run:
                    return {
                        "collections": [names.get(ref) or ref for ref in refs],
                        "fetched": len(rows),
                        "baseline": len(queue),
                        "dry_run": True,
                        "skipped": skipped[:30],
                        "errors": errors,
                        "invalid": bad,
                        "message": (
                            f"还没跑过第一次同步：真正跑的时候会把当前榜上这 {len(queue)} 条记为历史，"
                            "之后新进榜的才订阅（想连现在的也订，把设置改成「连当前的也订」）"
                        ),
                    }
                await mark_done_many([str(row["title_ref"]) for row in queue])
                await store.set(
                    BASELINE_KEY,
                    {"at": int(time.time()), "count": len(queue)},
                )
                await recent(f"第一次同步：把榜上 {len(queue)} 条记为历史，之后新进榜的才订阅")
                log.info("首次同步只记基线（%s 条）", len(queue))
                return {
                    "collections": [names.get(ref) or ref for ref in refs],
                    "fetched": len(rows),
                    "baseline": len(queue),
                    "handled": 0,
                    "subscribed": 0,
                    "remaining": 0,
                    "skipped": skipped[:30],
                    "results": [],
                    "errors": errors,
                    "invalid": bad,
                    "dry_run": dry_run,
                    "message": "第一次同步只记了基线；之后新进榜的会自动订阅",
                }
            if not marks:
                await store.set(BASELINE_KEY, {"at": int(time.time()), "count": 0})

            if dry_run:
                return {
                    "collections": [names.get(ref) or ref for ref in refs],
                    "fetched": len(rows),
                    "queue": [
                        {
                            "title": row.get("title"),
                            "kind": row.get("media_type"),
                            "rank": row.get("rank"),
                            "collection": row.get("collection"),
                            "rating": row.get("provider_rating"),
                        }
                        for row in queue[:50]
                    ],
                    "skipped": skipped[:30],
                    "errors": errors,
                    "invalid": bad,
                    "dry_run": True,
                }

            budget = max(1, min(config.max_per_run, MAX_PER_RUN_CEILING))
            douban_ids, keys = await existing()
            handled: list[dict] = []
            subscribed = 0
            retry = 0
            for item in queue:
                if budget <= 0:
                    break
                result = await handle(item, douban_ids, keys)
                # 已有订阅跳过、临时失败都不占预算：前者没花钱，后者下一轮还要再来
                if result == SKIPPED:
                    pass
                elif result.startswith(TEMPORARY_PREFIX):
                    retry += 1
                else:
                    budget -= 1
                    if "订阅 #" in result:
                        subscribed += 1
                handled.append(
                    {
                        "title": item.get("title"),
                        "collection": item.get("collection"),
                        "result": result,
                    }
                )
            return {
                "collections": [names.get(ref) or ref for ref in refs],
                "fetched": len(rows),
                "in_window": len(queue),
                "handled": len(handled),
                "subscribed": subscribed,
                # 没轮到的 + 这轮临时失败要重试的
                "remaining": max(len(queue) - len(handled), 0) + retry,
                "skipped": skipped[:30],
                "results": handled,
                "errors": errors,
                "invalid": bad,
                "dry_run": False,
            }
        finally:
            if not dry_run:
                busy["now"] = False

    async def scheduled_sync() -> None:
        """定时任务到点：没配片单、豆瓣抽风都只记日志，不抛（抛了任务中心会记一次失败）。"""
        try:
            outcome = await sync_once()
        except HotUnavailable as exc:
            log.warning("豆瓣热门自动订阅没跑成：%s", exc)
            return
        except Exception:  # noqa: BLE001 -- 网络抖动、接口改版：下一轮再来
            log.warning("豆瓣热门自动订阅出错，下一轮再试", exc_info=True)
            await recent("同步出错，下一轮再试")
            return
        if outcome.get("baseline"):
            return
        log.info(
            "豆瓣热门自动订阅：%s 个片单，读到 %s 条，待订 %s 条，处理 %s 条，还剩 %s 条%s",
            len(outcome.get("collections") or []),
            outcome.get("fetched"),
            outcome.get("in_window", 0),
            outcome.get("handled", 0),
            outcome.get("remaining", 0),
            f"，{len(outcome['errors'])} 个片单读取失败" if outcome.get("errors") else "",
        )

    # 定时任务：默认每 30 分钟一趟，周期在「设置 → 定时任务」里改
    # 每轮只订 max_per_run 条，没订完的下一轮接着订，所以榜单长了也能自己咬完
    ctx.contribute(
        SCHEDULED_TASKS,
        SYNC_TASK,
        TaskDefinition(
            key=SYNC_TASK,
            title="豆瓣热门自动订阅",
            handler=scheduled_sync,
            default_trigger_type=TriggerType.INTERVAL,
            default_interval_seconds=1800,
            description="读豆瓣热门榜单，把新进榜的电影、剧集自动订上",
        ),
    )

    # ------------------------------------------------------------ 接口

    router = APIRouter()

    class ResolveBody(BaseModel):
        title_ref: str = Field(description="待确认条目的引用（pending 里给出的 title_ref）")
        action: Literal["subscribe", "ignore"] = Field(
            description="subscribe = 现在订阅；ignore = 不订了"
        )
        pick: str = Field("", description="选哪个候选作品（有多个候选时必填，如 tmdb:movie:123）")
        seasons: list[int] = Field(default_factory=list, description="剧集订哪几季；不填按标题推断")
        follow_future: bool | None = Field(None, description="是否自动续订；不填用插件设置")

    @router.get("/status", operation_id=f"plugins.{PLUGIN_ID}.status", summary="豆瓣热门自动订阅：状态")
    async def status() -> dict:
        refs, bad = normalize_collections(config.collections)
        rows = await pending()
        return {
            "collections": refs,
            "invalid": bad,
            "min_rating": config.min_rating,
            "skip_unrated": config.skip_unrated,
            "max_rank": config.max_rank,
            "exclude_keywords": list(config.exclude_keywords),
            "max_per_run": config.max_per_run,
            "first_run": config.first_run,
            "tv_backfill": config.tv_backfill,
            "tv_follow_future": config.tv_follow_future,
            "skip_owned": config.skip_owned,
            "baseline": await baseline(),
            "stats": await store.get(STATS_KEY, default={}),
            "pending": len(rows),
            "recent": (await store.get(RECENT_KEY, default=[]))[:10],
        }

    @router.post("/check", operation_id=f"plugins.{PLUGIN_ID}.check", summary="豆瓣热门自动订阅：立即同步一次")
    async def check() -> dict:
        try:
            return await sync_once()
        except HotUnavailable as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @router.post("/preview", operation_id=f"plugins.{PLUGIN_ID}.preview", summary="豆瓣热门自动订阅：看看现在会订哪些（不真订）")
    async def preview() -> dict:
        try:
            return await sync_once(dry_run=True)
        except HotUnavailable as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @router.get("/collections", operation_id=f"plugins.{PLUGIN_ID}.collections", summary="豆瓣热门自动订阅：可用的片单")
    async def collections() -> dict:
        names = await collection_names(list(PRESETS.values()))
        watched, _bad = normalize_collections(config.collections)
        items = [
            {
                "alias": alias,
                "collection_ref": ref,
                "name": names.get(ref, ""),
                "watched": ref in set(watched),
            }
            for alias, ref in PRESETS.items()
        ]
        return {"items": items, "watched": watched}

    @router.get("/pending", operation_id=f"plugins.{PLUGIN_ID}.pending", summary="豆瓣热门自动订阅：待确认条目")
    async def list_pending() -> dict:
        rows = await pending()
        items = [{"title_ref": key, **value} for key, value in rows.items()]
        items.sort(key=lambda item: item.get("at", 0), reverse=True)
        return {"items": items}

    @router.post("/resolve", operation_id=f"plugins.{PLUGIN_ID}.resolve", summary="豆瓣热门自动订阅：处理一条待确认")
    async def resolve(body: ResolveBody) -> dict:
        rows = await pending()
        item = rows.get(body.title_ref)
        if item is None:
            raise HTTPException(status_code=404, detail=f"没有这条待确认：{body.title_ref}")
        if body.action == "ignore":
            rows.pop(body.title_ref, None)
            await store.set(PENDING_KEY, rows)
            await recent(f"《{item.get('title')}》已忽略")
            return {"ok": True, "ignored": body.title_ref, "title": item.get("title")}
        candidates = item.get("candidates") or []
        pick = body.pick.strip()
        if candidates and not pick:
            raise HTTPException(
                status_code=422,
                detail="这条有多个候选，请用 --pick 指定一个："
                + "；".join(f"{c['title_ref']}（{c['title']}）" for c in candidates),
            )
        if candidates and pick not in {c["title_ref"] for c in candidates}:
            raise HTTPException(status_code=422, detail="这个引用不在候选里，先看 pending 列出的候选")
        target = pick or body.title_ref
        kind = item.get("kind") or "movie"
        seasons = list(body.seasons)
        follow: bool | None = None
        if kind == "tv":
            if not seasons:
                seasons, _plan_follow, _why = season_plan(
                    item.get("title") or "", kind, config.tv_backfill
                )
            follow = config.tv_follow_future if body.follow_future is None else body.follow_future
            if not seasons:
                follow = True
        else:
            follow = None
        payload: dict = {"title_ref": target, "selected_seasons": seasons}
        if kind == "tv":
            payload["follow_future"] = bool(follow)
        if target.startswith("tmdb:") and body.title_ref.startswith("douban:"):
            # 只有「原条目是豆瓣、改选了 TMDB 候选」时才带原始豆瓣引用；
            # 宿主只接受豆瓣引用，原条目本身就是 tmdb 时带上会 400。
            payload["source_title_ref"] = body.title_ref
        try:
            try:
                created = await ops.call("subscriptions.create", payload)
            except OpsError as exc:
                if not (exc.status == 400 and "季" in (exc.message or "")):
                    raise
                payload.update(selected_seasons=[], follow_future=True)
                created = await ops.call("subscriptions.create", payload)
        except OpsError as exc:
            raise HTTPException(status_code=502, detail=f"订阅失败：{exc.message}") from exc
        sub = (created or {}).get("subscription") or {}
        rows.pop(body.title_ref, None)
        await store.set(PENDING_KEY, rows)
        await bump("subscribed")
        await recent(f"《{item.get('title')}》手动确认后已订阅 #{sub.get('id')}")
        return {"ok": True, "subscription_id": sub.get("id"), "title": item.get("title")}

    routes.mount(ctx, router)

    refs, bad = normalize_collections(config.collections)
    if bad:
        log.warning("%s：这些片单写法认不出，已跳过：%s", PLUGIN_ID, "、".join(bad))
    log.info(
        "%s 已加载：盯 %s 个片单（%s），每轮最多订阅 %s 条",
        PLUGIN_ID,
        len(refs) or "（未配）",
        "、".join(
            alias for alias, ref in PRESETS.items() if ref in refs
        ) or "未配",
        config.max_per_run,
    )
