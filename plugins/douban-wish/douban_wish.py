"""豆瓣想看同步：豆瓣上点「想看」，MovieClaw 里自动订阅。

数据来源是豆瓣移动端的「我的收藏」接口（网页和 App 里「想看 / 看过 / 在看」列表用的同一个），
只读公开可见的收藏，**不需要也不接受账号密码**：

    GET {base}/user/{用户}/interests?start=0&count=50&for_mobile=1

返回按收藏时间倒序，混着电影 / 剧集 / 书 / 游戏，每条自带状态与收藏时间：
``mark`` = 想看、``done`` = 看过、``doing`` = 在看。本插件只处理 ``mark`` 且类型是电影 / 剧集的条目。

一轮同步（可以填多个豆瓣 ID，一个个跑）：
1. 从最新往回翻页，只收「收藏时间在最近 N 天以内」的想看（N 在设置里，0 = 不限）；
   处理过的豆瓣条目 id 记在插件数据里，不会重复订；
2. 每条转成订阅：电影直接订；剧集按标题里的「第 N 季 / Season N」选季（没写就按第 1 季），
   并按设置决定是否打开自动续订；已经有订阅的跳过；
3. 对不上的（豆瓣条目在 TMDB 找不到、或对应多部作品）记进「待确认」，用
   ``mclaw plugins douban-wish pending`` 看、``resolve`` 定，不自己瞎猜。

第一次同步默认只记基线（把窗口内已有的想看当历史，不订阅），免得一装上就把老想看图订一遍；
想连已有的也订，把设置里的「第一次同步」改成「连已有的也订」。
"""

import asyncio
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Literal

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from movieclaw_api.plugins.keys import PLUGIN_DATA, PLUGIN_ROUTES
from movieclaw_scheduler import SCHEDULED_TASKS, TaskDefinition, TriggerType
from movieclaw_sdk import Context, net, plugin
from movieclaw_sdk.events import PLUGIN_EVENTS, Publisher, plugin_event
from movieclaw_sdk.ops import HOST_OPS, OpsError

PLUGIN_ID = "douban-wish"
SYNC_TASK = f"{PLUGIN_ID}.sync"

DEFAULT_API_BASE = "https://m.douban.com/rexxar/api/v2"
PAGE_SIZE = 50          # 接口单页上限
MAX_PAGES = 6           # 每个豆瓣 ID 一轮最多翻 6 页（300 条）
DONE_LIMIT = 800        # 记多少条处理过的豆瓣条目（插件数据单个值有 64 KB 上限）
PENDING_LIMIT = 50
RECENT_SIZE = 60

FETCH_RETRIES = 3       # 403 / 429 / 5xx 最多再试几次（风控限流多半等一会儿就过）
FETCH_BACKOFF = 2.0     # 第一次退避 2 秒，之后 4、8 秒

DONE_KEY = "done"             # [豆瓣条目 id, …]，最近处理的在前
PENDING_KEY = "pending"       # {豆瓣条目 id: {title, kind, reason, candidates, user, at}}
STATS_KEY = "stats"           # {"runs", "subscribed", "skipped", "failed", "waiting"}
RECENT_KEY = "recent"         # [{at, line}] 最近做了什么（接口与排查用）
BASELINE_KEY = "baseline"     # {豆瓣 ID: 建立基线的时间戳}

MOBILE_UA = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15"
WISH_STATUS = "mark"          # 豆瓣的「想看」
WANTED_KINDS = ("movie", "tv")
SKIPPED = "已经有订阅，跳过"
TEMPORARY_PREFIX = "暂时失败"   # 临时故障（5xx / 超时 / 限流）：不记账，下一轮重试
# 豆瓣的收藏时间按北京时间给，比较时也按北京时间算窗口
CST = timezone(timedelta(hours=8))

_CN_DIGITS = {
    "零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}
_CN_SEASON = re.compile(r"第\s*([0-9]{1,2}|[零一二两三四五六七八九十]{1,3})\s*[季部]")
_EN_SEASON = re.compile(r"(?i)(?:season|series)\s*(\d{1,2})(?![0-9])")
_S_SEASON = re.compile(r"(?<![A-Za-z0-9])S(\d{1,2})(?![0-9])")
_PROFILE = re.compile(r"/people/([A-Za-z0-9_.\-%]+)")
_BAD_USER = re.compile(r"[^A-Za-z0-9_.\-]")
_SPLIT = re.compile(r"[,，;；、\r\n]+")


class DoubanUnavailable(Exception):
    """豆瓣读不到：ID 不对、收藏不公开、网络不通。信息可直接给用户看。"""


class Config(BaseModel):
    douban_users: list[str] = Field(
        default_factory=list,
        title="豆瓣 ID",
        description="可以填多个（一行一个，或用逗号隔开）：填主页地址里 people/ 后面那一段，也可直接粘整条主页链接",
    )
    within_days: int = Field(
        30,
        title="只同步最近几天的想看",
        description="豆瓣收藏时间在这么多天以内的才订阅，更老的不动；填 0 = 不限时间",
        ge=0,
        le=3650,
    )
    first_run: Literal["since_now", "history"] = Field(
        "since_now",
        title="第一次同步",
        description="since_now = 只订阅以后新点的想看（推荐）；history = 把窗口内现有的想看也订上",
    )
    tv_follow_future: bool = Field(
        True,
        title="剧集自动续订",
        description="订阅剧集时打开「自动续订」，以后播出的新集、新季自动追",
    )
    max_per_run: int = Field(
        5,
        title="每轮最多订阅几条",
        description="一次点了一堆想看时防止把任务压满，剩下的下一轮继续（1～50）",
        ge=1,
        le=50,
    )
    api_base: str = Field(
        "",
        title="豆瓣接口地址（可选）",
        description="留空用豆瓣官方地址；服务器访问不了豆瓣时填镜像或反代地址",
    )


class Wish(BaseModel):
    """豆瓣的一条「想看」。"""

    douban_id: str
    title: str
    kind: str            # movie / tv
    created: str         # 收藏时间 "YYYY-MM-DD HH:MM:SS"（北京时间），可直接按字符串比大小


class Subscribed(BaseModel):
    model_config = ConfigDict(frozen=True)

    douban_id: str
    title: str
    detail: str
    user: str = ""
    subscription_id: int | None = None


SUBSCRIBED = plugin_event(
    f"{PLUGIN_ID}.subscribed",
    Subscribed,
    title="豆瓣想看已转成订阅",
    doc="豆瓣点了想看、MovieClaw 里订阅成功时发出",
)


# --------------------------------------------------------------- 纯函数（可单独自测）


def normalize_user(raw: str) -> str:
    """豆瓣 ID 或主页链接 → ID；认不出返回空串。"""
    text = (raw or "").strip()
    if not text:
        return ""
    hit = _PROFILE.search(text)
    if hit:
        return hit.group(1)
    for prefix in ("https://", "http://"):
        if text.startswith(prefix):
            text = text[len(prefix):]
    text = text.split("?", 1)[0].split("#", 1)[0].strip().strip("/")
    if text.startswith(("www.douban.com/", "m.douban.com/", "movie.douban.com/")):
        parts = [part for part in text.split("/") if part]
        if len(parts) >= 3 and parts[1] == "people":
            return parts[2]
        return ""
    if "/" in text or _BAD_USER.search(text):
        return ""
    return text


def normalize_users(raw: list[str] | str | None) -> tuple[list[str], list[str]]:
    """设置里的多个豆瓣 ID → (认得出的 ID 列表, 认不出的原文)。去重、保序。"""
    if isinstance(raw, str):
        entries = _SPLIT.split(raw)
    else:
        entries = []
        for item in raw or []:
            entries.extend(_SPLIT.split(str(item)))
    # 只在逗号 / 分号 / 顿号 / 换行处分隔：豆瓣 ID 本身不含空格，整条链接也不会被拆坏
    users: list[str] = []
    bad: list[str] = []
    for entry in entries:
        entry = entry.strip()
        if not entry:
            continue
        user = normalize_user(entry)
        if not user:
            bad.append(entry)
        elif user not in users:
            users.append(user)
    return users, bad


def parse_wishes(payload: dict) -> list[Wish]:
    """接口返回 → 想看的电影 / 剧集（书、游戏、舞台剧都不算），带收藏时间。"""
    out: list[Wish] = []
    for row in payload.get("interests") or []:
        if not isinstance(row, dict) or row.get("status") != WISH_STATUS:
            continue
        subject = row.get("subject") or {}
        kind = subject.get("type") or subject.get("subtype")
        douban_id = str(subject.get("id") or "").strip()
        title = (subject.get("title") or "").strip()
        created = str(row.get("create_time") or "").strip()
        if kind not in WANTED_KINDS or not douban_id or not title or not created:
            continue
        out.append(Wish(douban_id=douban_id, title=title, kind=kind, created=created))
    return out


def cutoff_string(days: int, now: datetime | None = None) -> str | None:
    """「最近 N 天」的下界（北京时间字符串）；0 或负数 = 不限，返回 None。"""
    if not days or days <= 0:
        return None
    moment = (now or datetime.now(CST)) - timedelta(days=int(days))
    return moment.strftime("%Y-%m-%d %H:%M:%S")


def in_window(created: str, cutoff: str | None) -> bool:
    """豆瓣给的时间是 "YYYY-MM-DD HH:MM:SS"，同格式可直接按字符串比大小。"""
    return cutoff is None or created >= cutoff


def baseline_string(stamp) -> str | None:
    """基线时间戳 → 和豆瓣收藏时间同格式的字符串（北京时间）；没有基线返回 None。

    有了它才能判断「这条想看是不是基线之前就有的」：``done`` 名单只有 800 条，
    攒满后老条目会被挤掉，下次翻到就会被当成新的重新订阅一遍。
    """
    value = float(stamp or 0)
    if value <= 0:
        return None
    return datetime.fromtimestamp(value, CST).strftime("%Y-%m-%d %H:%M:%S")


def is_permanent_error(exc: OpsError) -> bool:
    """这次订阅失败是不是「重试也没用」。

    只有明确的客户端错误才算（参数不对、被拒绝）。5xx、超时、429、鉴权过期
    都是暂时的，下一轮该再试——写进 ``done`` 名单等于永久放弃这条想看。
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


def plan_for(title: str, kind: str) -> tuple[list[int], str]:
    """订阅参数里的选季 + 一句说明（纯规则，不发请求）。

    豆瓣把剧集按季拆条目（「相对宇宙 第一季」），标题里的季号就是用户要的那一季；
    没写季号的多半是单季剧，按第 1 季订。
    """
    if kind != "tv":
        return [], "电影"
    season = season_marker(title)
    if season is not None:
        return [season], f"按第 {season} 季订阅"
    return [1], "标题没写第几季，按第 1 季订阅"


def pick_candidates(details: list[dict] | None) -> list[dict]:
    """歧义候选 → 给用户看的清单（只留必要字段）。"""
    out = []
    for item in details or []:
        if not isinstance(item, dict):
            continue
        ref = item.get("title_ref") or ""
        if ref:
            out.append(
                {"title_ref": ref, "title": item.get("title") or "", "year": item.get("year")}
            )
    return out


# --------------------------------------------------------------- 插件本体


@plugin(
    PLUGIN_ID,
    title="豆瓣想看同步",
    inject=(HOST_OPS, PLUGIN_DATA, PLUGIN_ROUTES, PLUGIN_EVENTS),
    permissions=("subscriptions.create", "subscriptions.list"),
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

    # ------------------------------------------------------------ 豆瓣侧

    def api_base() -> str:
        return (config.api_base or DEFAULT_API_BASE).rstrip("/")

    async def fetch_page(user: str, start: int) -> dict:
        url = f"{api_base()}/user/{user}/interests"
        headers = {"User-Agent": MOBILE_UA, "Referer": "https://m.douban.com/"}
        # 服务名 = 条目 id：走用户在「设置 → 网络」里给这个插件配的代理（默认直连）
        async with httpx.AsyncClient(
            transport=net.http_transport(PLUGIN_ID), timeout=20, headers=headers
        ) as client:
            attempt = 0
            while True:
                response = await client.get(
                    url, params={"start": start, "count": PAGE_SIZE, "for_mobile": 1}
                )
                # 403 / 5xx 未必是「收藏不公开」，多半是豆瓣风控（翻页太快、IP 被限流）。
                # 退避重试几次，别一撞上就把整个账号判成读不到。
                retryable = response.status_code in (403, 429) or response.status_code >= 500
                if not retryable or attempt >= FETCH_RETRIES:
                    break
                attempt += 1
                await asyncio.sleep(FETCH_BACKOFF * (2 ** (attempt - 1)))
        if response.status_code == 404:
            raise DoubanUnavailable("豆瓣没有这个用户：ID 写错了，或者对方已经注销")
        if response.status_code == 403:
            raise DoubanUnavailable(
                "豆瓣拒绝读取这个账号的收藏：多半是收藏没公开；如果确认公开，"
                "那是豆瓣风控在限流，过一阵再试（也可以在设置里给这个插件配代理）"
            )
        if response.status_code == 401:
            raise DoubanUnavailable("这个豆瓣账号的收藏不公开，读不到想看列表")
        if response.status_code == 429:
            raise DoubanUnavailable("豆瓣在限流（HTTP 429），这一轮先跳过，过一阵再试")
        if response.status_code >= 500:
            raise DoubanUnavailable(f"豆瓣返回 HTTP {response.status_code}，稍后再试")
        if response.status_code != 200:
            raise DoubanUnavailable(f"豆瓣拒绝了这个请求（HTTP {response.status_code}）")
        try:
            return response.json()
        except ValueError as exc:
            raise DoubanUnavailable("豆瓣返回的不是 JSON，可能被拦截或接口改版了") from exc

    # ------------------------------------------------------------ 状态读写

    async def done_ids() -> list[str]:
        return list(await store.get(DONE_KEY, default=[]) or [])

    async def mark_done(douban_id: str) -> None:
        rows = await done_ids()
        rows = [douban_id, *(row for row in rows if row != douban_id)][:DONE_LIMIT]
        await store.set(DONE_KEY, rows)

    async def pending() -> dict:
        return await store.get(PENDING_KEY, default={}) or {}

    async def baselines() -> dict:
        data = await store.get(BASELINE_KEY, default={}) or {}
        return data if isinstance(data, dict) else {}

    async def recent(line: str) -> None:
        rows = await store.get(RECENT_KEY, default=[]) or []
        rows = [{"at": int(time.time()), "line": line}, *rows][:RECENT_SIZE]
        await store.set(RECENT_KEY, rows)

    async def bump(key: str) -> None:
        stats = await store.get(STATS_KEY, default={}) or {}
        stats[key] = int(stats.get(key, 0)) + 1
        await store.set(STATS_KEY, stats)

    async def hold(wish: Wish, reason: str, user: str, candidates: list[dict] | None = None) -> None:
        data = await pending()
        data[wish.douban_id] = {
            "title": wish.title,
            "kind": wish.kind,
            "reason": reason,
            "candidates": candidates or [],
            "user": user,
            "at": int(time.time()),
        }
        if len(data) > PENDING_LIMIT:
            keep = sorted(data.items(), key=lambda kv: kv[1].get("at", 0))[-PENDING_LIMIT:]
            data = dict(keep)
        await store.set(PENDING_KEY, data)
        await mark_done(wish.douban_id)
        await bump("waiting")
        await recent(f"{user} 的《{wish.title}》要你定一下：{reason}")
        log.info("《%s》(%s) 需人工确认：%s", wish.title, wish.douban_id, reason)

    # ------------------------------------------------------------ 订阅

    async def existing_douban_ids() -> set[str]:
        """已经有订阅的豆瓣条目 id（插件以超管身份调用，拿得到全部订阅）。"""
        try:
            rows = await ops.call("subscriptions.list", {})
        except OpsError as exc:
            log.warning("读订阅列表失败：%s", exc.message)
            return set()
        return {
            str(row["media"]["douban_id"])
            for row in rows or []
            if (row.get("media") or {}).get("douban_id")
        }

    def create_body(wish: Wish, title_ref: str) -> dict:
        seasons, _why = plan_for(wish.title, wish.kind)
        body: dict = {"title_ref": title_ref, "selected_seasons": seasons}
        if wish.kind == "tv":
            body["follow_future"] = bool(config.tv_follow_future)
        if title_ref.startswith("tmdb:"):
            # 从豆瓣歧义候选改选了 TMDB 条目：把豆瓣身份带回去，之后按豆瓣集数对账
            body["source_title_ref"] = f"douban:{wish.douban_id}"
        return body

    async def create(wish: Wish, title_ref: str) -> dict:
        """建订阅；剧集季号在 TMDB 不存在时退回「只追以后的新集」。

        两条路都要处理：宿主**明确报错**说没有这一季（400 带「季」），
        和宿主**只回成功却把 selected_seasons 丢掉**。只信自己的 ``plan_for``
        会给用户一个「按第 3 季订阅」的假回执，实际一集都不会下。
        """
        seasons, why = plan_for(wish.title, wish.kind)
        body = create_body(wish, title_ref)
        fell_back = False
        try:
            created = await ops.call("subscriptions.create", body)
        except OpsError as exc:
            no_such_season = wish.kind == "tv" and exc.status == 400 and "季" in (exc.message or "")
            if not no_such_season:
                raise
            fell_back = True
            # 只追以后的新集：季号留空，必须靠 follow_future 撑着，否则会落库成 0 工单
            body.update(selected_seasons=[], follow_future=True)
            created = await ops.call("subscriptions.create", body)
        sub = (created or {}).get("subscription") or {}
        if fell_back:
            wanted = seasons[0] if seasons else "?"
            return {
                "subscription": sub,
                "why": f"{why}，但 TMDB 里没有第 {wanted} 季，已改成只追以后播出的新集",
            }
        missing = seasons_missing(seasons, sub.get("selected_seasons"))
        if not missing:
            return {"subscription": sub, "why": why}
        # 宿主把这些季丢了：如实告知，别再假装订上了
        log.warning(
            "《%s》(%s) 订阅 %s 没建上第 %s 季：宿主返回的 selected_seasons=%r",
            wish.title,
            wish.douban_id,
            sub.get("id"),
            "、".join(str(s) for s in missing),
            sub.get("selected_seasons"),
        )
        return {
            "subscription": sub,
            "why": (
                f"{why}，但订阅 #{sub.get('id')} 实际没选上第 "
                f"{'、'.join(str(s) for s in missing)} 季（TMDB 里可能没有这一季），"
                "去订阅详情里手动确认一下"
            ),
            "seasons_missing": missing,
        }

    async def handle(wish: Wish, user: str, subscribed: set[str]) -> str:
        """一条想看 → 结果描述。"""
        if wish.douban_id in subscribed:
            await mark_done(wish.douban_id)
            await bump("skipped")
            return SKIPPED
        try:
            result = await create(wish, f"douban:{wish.douban_id}")
        except OpsError as exc:
            if exc.code == "SUBSCRIPTION_TARGET_AMBIGUOUS":
                await hold(wish, "豆瓣条目对应多部作品，要你挑一个", user, pick_candidates(exc.details))
                return "待确认（多部候选）"
            if exc.status == 404:
                await hold(wish, "TMDB 里还没收录这部，等收录后再来", user)
                return "待确认（TMDB 暂无）"
            # 5xx / 超时 / 429 是宿主或网络的临时故障：**不能写进 done**，
            # 否则这条想看就被永久放弃，等宿主恢复了也不会再订。
            if not is_permanent_error(exc):
                await bump("failed")
                await recent(f"《{wish.title}》订阅暂时失败，下一轮重试：{exc.message}")
                log.warning(
                    "《%s》(%s) 订阅暂时失败（HTTP %s），本轮不记账，下一轮重试：%s",
                    wish.title,
                    wish.douban_id,
                    exc.status,
                    exc.message,
                )
                return f"{TEMPORARY_PREFIX}（下一轮重试）：{exc.message}"
            await mark_done(wish.douban_id)
            await bump("failed")
            await recent(f"《{wish.title}》订阅失败：{exc.message}")
            log.warning("《%s》(%s) 订阅失败：%s", wish.title, wish.douban_id, exc.message)
            return f"失败：{exc.message}"
        sub = result["subscription"]
        sub_id = sub.get("id")
        detail = f"{result['why']}（订阅 #{sub_id}）"
        await mark_done(wish.douban_id)
        await bump("subscribed")
        subscribed.add(wish.douban_id)
        await recent(f"《{wish.title}》{detail}")
        log.info("《%s》(%s) %s", wish.title, wish.douban_id, detail)
        try:
            await events.publish(
                SUBSCRIBED,
                Subscribed(
                    douban_id=wish.douban_id,
                    title=wish.title,
                    detail=detail,
                    user=user,
                    subscription_id=sub_id if isinstance(sub_id, int) else None,
                ),
            )
        except Exception:  # noqa: BLE001 -- 通知发不出去不影响订阅本身
            log.warning("发「已订阅」事件失败：%s", wish.title, exc_info=True)
        return detail

    # ------------------------------------------------------------ 同步

    async def collect(user: str, cutoff: str | None, seen: set[str], baseline: bool) -> dict:
        """翻这个用户的收藏，收窗口内的想看。翻到窗口外 / 没有新条目就停。"""
        wishes: dict[str, Wish] = {}
        pages = 0
        total = 0
        page = 0
        while page < MAX_PAGES:
            payload = await fetch_page(user, page * PAGE_SIZE)
            pages += 1
            total = int(payload.get("total") or 0)
            batch = parse_wishes(payload)
            window = [wish for wish in batch if in_window(wish.created, cutoff)]
            for wish in window:
                wishes.setdefault(wish.douban_id, wish)
            hit_old = len(window) < len(batch)     # 这一页已经翻到窗口之外
            fresh = any(wish.douban_id not in seen for wish in window)
            if hit_old:
                break
            if page > 0 and not fresh and not baseline:
                break                               # 更老的页不会有没处理过的
            if len(payload.get("interests") or []) < PAGE_SIZE:
                break
            if pages * PAGE_SIZE >= total:
                break
            page += 1
        return {"wishes": list(wishes.values()), "pages": pages, "total": total}

    async def sync_user(user: str, cutoff: str | None, seen: set[str]) -> dict:
        marks = await baselines()
        baseline_run = user not in marks and config.first_run != "history"
        gathered = await collect(user, cutoff, seen, baseline_run)
        wishes = gathered["wishes"]

        if baseline_run:
            for wish in wishes:
                if wish.douban_id not in seen:
                    await mark_done(wish.douban_id)
            marks[user] = int(time.time())
            await store.set(BASELINE_KEY, marks)
            await recent(f"{user} 第一次同步：把窗口内 {len(wishes)} 条想看记为历史，之后新点的才订阅")
            log.info("%s 首次同步只记基线（%s 条）", user, len(wishes))
            return {
                "user": user,
                "pages": gathered["pages"],
                "baseline": len(wishes),
                "handled": 0,
                "results": [],
            }

        if user not in marks:
            # first_run=history：这一轮就是要把现有的都订一遍，所以只记下基线、
            # 不拿它当闸门（否则刚写的基线会把自己这轮该订的全挡掉）
            marks[user] = int(time.time())
            await store.set(BASELINE_KEY, marks)
            floor = None
        else:
            # done 名单只有 DONE_LIMIT 条，攒满后老条目会被挤掉。翻页时又可能翻到它们，
            # 于是被当成新的再订一遍。基线时间就是第二道闸：收藏时间早于基线的，根本没
            # 可能是「建立基线之后新点的」，一律不订。
            floor = baseline_string(marks.get(user))
        stale = [wish for wish in wishes if floor and wish.created < floor]
        queue = [
            wish
            for wish in wishes
            if wish.douban_id not in seen and not (floor and wish.created < floor)
        ]
        if stale:
            log.debug("%s：%d 条想看的收藏时间早于基线，跳过", user, len(stale))
        return {
            "user": user,
            "pages": gathered["pages"],
            "total_interests": gathered["total"],
            "in_window": len(wishes),
            "before_baseline": len(stale),
            "queue": queue,
            "handled": 0,
            "results": [],
        }

    async def sync_once() -> dict:
        users, bad = normalize_users(config.douban_users)
        if not users:
            raise DoubanUnavailable("还没填豆瓣 ID：去插件「设置」里填主页地址或 ID（可以填多个）")
        if busy["now"]:
            raise DoubanUnavailable("上一轮同步还没跑完，稍等")
        busy["now"] = True
        try:
            await bump("runs")
            cutoff = cutoff_string(config.within_days)
            seen = set(await done_ids())
            per_user: list[dict] = []
            errors: list[dict] = []
            for user in users:
                try:
                    per_user.append(await sync_user(user, cutoff, seen))
                except DoubanUnavailable as exc:
                    errors.append({"user": user, "error": str(exc)})
                    await recent(f"{user} 读取失败：{exc}")
                    log.warning("%s 读取失败：%s", user, exc)
                except Exception as exc:  # noqa: BLE001 -- 一个 ID 出错不影响其他 ID
                    errors.append({"user": user, "error": f"{type(exc).__name__}: {exc}"})
                    log.warning("%s 同步出错", user, exc_info=True)

            # 多个人一起排队，按设置的总量上限一条条处理
            budget = config.max_per_run
            subscribed = await existing_douban_ids() if budget else set()
            handled: list[dict] = []
            for row in per_user:
                if "queue" not in row:
                    continue
                left: list[Wish] = []
                for wish in row["queue"]:
                    if budget <= 0:
                        left.append(wish)
                        continue
                    result = await handle(wish, row["user"], subscribed)
                    handled.append(
                        {
                            "user": row["user"],
                            "title": wish.title,
                            "douban_id": wish.douban_id,
                            "result": result,
                        }
                    )
                    # 已经有订阅的等于没干活，不占这一轮的额度；暂时失败的也不算干活
                    # （这条想看没被记成 done，下一轮还会再来，额度留给真正订上的）
                    if result != SKIPPED and not result.startswith(TEMPORARY_PREFIX):
                        budget -= 1
                row["handled"] = len(row["queue"]) - len(left)
                row["remaining"] = len(left) + sum(
                    1
                    for item in handled
                    if item["user"] == row["user"]
                    and item["result"].startswith(TEMPORARY_PREFIX)
                )
                row.pop("queue", None)

            baseline = sum(row.get("baseline", 0) for row in per_user)
            return {
                "users": [row for row in per_user],
                "errors": errors,
                "invalid": bad,
                "within_days": config.within_days,
                "cutoff": cutoff,
                "new": sum(row.get("in_window", 0) for row in per_user),
                "before_baseline": sum(row.get("before_baseline", 0) for row in per_user),
                "handled": len(handled),
                "remaining": sum(row.get("remaining", 0) for row in per_user),
                "baseline": baseline,
                "results": handled,
                "message": (
                    "第一次同步只记了基线；之后你在豆瓣新点的想看会自动订阅"
                    if baseline and not handled
                    else ""
                ),
            }
        finally:
            busy["now"] = False

    async def scheduled_sync() -> None:
        """定时任务到点：没填 ID、豆瓣抽风都只记日志，不抛（抛了任务中心会记一次失败）。"""
        try:
            outcome = await sync_once()
        except DoubanUnavailable as exc:
            log.warning("豆瓣想看同步没跑成：%s", exc)
            await recent(f"同步失败：{exc}")
            return
        except Exception:  # noqa: BLE001 -- 网络抖动、豆瓣改版：下一轮再来
            log.warning("豆瓣想看同步出错，下一轮再试", exc_info=True)
            await recent("同步出错，下一轮再试")
            return
        if outcome.get("baseline") and not outcome.get("handled"):
            return
        log.info(
            "豆瓣想看同步：%s 个账号，窗口内 %s 条，处理 %s 条，还剩 %s 条%s",
            len(outcome.get("users") or []),
            outcome.get("new"),
            outcome.get("handled"),
            outcome.get("remaining", 0),
            f"，{len(outcome['errors'])} 个账号读取失败" if outcome.get("errors") else "",
        )

    # 定时任务：默认每 30 分钟一趟，周期在「设置 → 定时任务」里改
    ctx.contribute(
        SCHEDULED_TASKS,
        SYNC_TASK,
        TaskDefinition(
            key=SYNC_TASK,
            title="豆瓣想看同步",
            handler=scheduled_sync,
            default_trigger_type=TriggerType.INTERVAL,
            default_interval_seconds=1800,
            description="把豆瓣里新点的「想看」同步成 MovieClaw 订阅",
        ),
    )

    # ------------------------------------------------------------ 接口

    router = APIRouter()

    class ResolveBody(BaseModel):
        douban_id: str = Field(description="待确认条目的豆瓣 ID")
        action: Literal["subscribe", "ignore"] = Field(
            description="subscribe = 现在订阅；ignore = 不订了"
        )
        title_ref: str = Field("", description="选哪个候选作品（有多个候选时必填，如 tmdb:movie:123）")
        seasons: list[int] = Field(default_factory=list, description="剧集订哪几季；不填按标题推断")
        follow_future: bool | None = Field(None, description="是否自动续订；不填用插件设置")

    @router.get(
        "/status", operation_id=f"plugins.{PLUGIN_ID}.status", summary="豆瓣想看同步：状态"
    )
    async def status() -> dict:
        users, bad = normalize_users(config.douban_users)
        rows = await pending()
        return {
            "douban_users": users,
            "invalid": bad,
            "within_days": config.within_days,
            "cutoff": cutoff_string(config.within_days),
            "first_run": config.first_run,
            "baselined": sorted((await baselines()).keys()),
            "stats": await store.get(STATS_KEY, default={}),
            "pending": len(rows),
            "recent": (await store.get(RECENT_KEY, default=[]))[:10],
        }

    @router.post(
        "/check", operation_id=f"plugins.{PLUGIN_ID}.check", summary="豆瓣想看同步：立即同步一次"
    )
    async def check() -> dict:
        try:
            return await sync_once()
        except DoubanUnavailable as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @router.get(
        "/pending", operation_id=f"plugins.{PLUGIN_ID}.pending", summary="豆瓣想看同步：待确认的想看"
    )
    async def list_pending() -> dict:
        rows = await pending()
        items = [{"douban_id": key, **value} for key, value in rows.items()]
        items.sort(key=lambda item: item.get("at", 0), reverse=True)
        return {"items": items}

    @router.post(
        "/resolve", operation_id=f"plugins.{PLUGIN_ID}.resolve", summary="豆瓣想看同步：处理一条待确认"
    )
    async def resolve(body: ResolveBody) -> dict:
        rows = await pending()
        item = rows.get(body.douban_id)
        if item is None:
            raise HTTPException(status_code=404, detail=f"没有这条待确认：{body.douban_id}")
        if body.action == "ignore":
            rows.pop(body.douban_id, None)
            await store.set(PENDING_KEY, rows)
            await recent(f"《{item.get('title')}》已忽略")
            return {"ok": True, "ignored": body.douban_id, "title": item.get("title")}
        candidates = item.get("candidates") or []
        title_ref = body.title_ref.strip()
        if candidates and not title_ref:
            raise HTTPException(
                status_code=422,
                detail="这条有多个候选，请用 --title-ref 指定一个："
                + "；".join(f"{c['title_ref']}（{c['title']}）" for c in candidates),
            )
        if candidates and title_ref not in {c["title_ref"] for c in candidates}:
            raise HTTPException(
                status_code=422, detail="这个 title_ref 不在候选里，先看 pending 列出的候选"
            )
        title_ref = title_ref or f"douban:{body.douban_id}"
        kind = item.get("kind") or "movie"
        seasons = list(body.seasons)
        if kind == "tv" and not seasons:
            seasons, _why = plan_for(item.get("title") or "", kind)
        payload: dict = {"title_ref": title_ref, "selected_seasons": seasons}
        if kind == "tv":
            follow = config.tv_follow_future if body.follow_future is None else body.follow_future
            payload["follow_future"] = bool(follow)
        if title_ref.startswith("tmdb:"):
            payload["source_title_ref"] = f"douban:{body.douban_id}"
        try:
            try:
                created = await ops.call("subscriptions.create", payload)
            except OpsError as exc:
                if not (kind == "tv" and exc.status == 400 and "季" in (exc.message or "")):
                    raise
                payload.update(selected_seasons=[], follow_future=True)
                created = await ops.call("subscriptions.create", payload)
        except OpsError as exc:
            raise HTTPException(status_code=502, detail=f"订阅失败：{exc.message}") from exc
        sub = (created or {}).get("subscription") or {}
        rows.pop(body.douban_id, None)
        await store.set(PENDING_KEY, rows)
        await bump("subscribed")
        await recent(f"《{item.get('title')}》手动确认后已订阅 #{sub.get('id')}")
        # 和自动路径同一个口径：宿主把选季丢了就说出来，别给假回执
        missing = seasons_missing(seasons, sub.get("selected_seasons"))
        return {
            "ok": True,
            "subscription_id": sub.get("id"),
            "title": item.get("title"),
            "warning": (
                f"订阅建上了，但第 {'、'.join(str(s) for s in missing)} 季没选上"
                "（TMDB 里可能没有这一季）"
                if missing
                else ""
            ),
        }

    routes.mount(ctx, router)

    users, bad = normalize_users(config.douban_users)
    if bad:
        log.warning("%s：这些豆瓣 ID 认不出，已跳过：%s", PLUGIN_ID, "、".join(bad))
    log.info(
        "%s 已加载：%s 个豆瓣账号，只同步最近 %s 天的想看，每轮最多订阅 %s 条",
        PLUGIN_ID,
        len(users) or "（未填）",
        config.within_days or "不限时间",
        config.max_per_run,
    )
