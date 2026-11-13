"""片头片尾：把 MovieClaw 的识别结论与飞牛现状摆在一起，按季对比、按季写回。

飞牛的片头片尾**一季只存一套秒数**（写季 guid，整季每集都读回同一个值），
所以这里的单位是「库 + TMDB + 季」：

- **MC 侧**：主线扫描/识别出的每个文件片段（``library.items.segments``），
  一季内取片头结束、片尾开始时长的中位数（与自动写回同口径）；
- **飞牛侧**：登录账号名下这一季的 ``play_config``（``play/info``）；
- 季集合取两边并集——飞牛设了、MC 还没识别的季也要显示出来。

飞牛侧每季一次请求，量大的库要几百次，所以：并发限流、结果缓存 5 分钟、
页面点「刷新对比」才强刷。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from .sync import median, skip_values_from_segments
from .trim_api import CATEGORY_TV, TYPE_TV, TrimClient, TrimError

#: 飞牛季索引缓存时长（秒）：库结构变化不频繁
SEASON_INDEX_TTL = 1800.0
#: 读飞牛配置的并发数：家用 NAS，别一次打满
READ_CONCURRENCY = 6
#: 单次总览最多对比多少季（再多就在页面上分批看，避免一次几百个请求）
MAX_SEASONS = 400
#: 总览结果缓存（页面反复打开不重复打飞牛）
OVERVIEW_TTL = 300.0

#: ``library_guid`` → (建索引时刻, {(tmdb, 季): {"guid", "title"}})
_SEASON_INDEX: dict[str, tuple[float, dict[tuple[int, int], dict[str, Any]]]] = {}


# ---------------------------------------------------------------- 飞牛侧索引
async def season_index(
    trim: TrimClient, library_guid: str, *, log: Any, max_age: float = SEASON_INDEX_TTL
) -> dict[tuple[int, int], dict[str, Any]]:
    """``(tmdb, 季) → {guid, title}``：飞牛这个库里的全部季。

    飞牛的季 guid 是写片头片尾的唯一入口，所以要先把「哪部剧的第几季」翻成
    季 guid。索引建失败（登录失效、网络抖）**不写缓存**，下次再来。
    """
    now = time.monotonic()
    cached = _SEASON_INDEX.get(library_guid)
    if cached is not None and now - cached[0] < max_age:
        return cached[1]

    index: dict[tuple[int, int], dict[str, Any]] = {}
    try:
        top = await trim.all_items(library_guid)
    except TrimError as exc:
        log.warning("读飞牛库条目失败（片头片尾索引）：%s", exc)
        return cached[1] if cached else index

    for tv in [i for i in top if i.type == TYPE_TV and i.guid and i.tmdb_id]:
        try:
            seasons = await trim.seasons(tv.guid)
        except TrimError:
            continue  # 单部剧失败不影响其他
        for season in seasons:
            if not season.guid or season.season_number is None:
                continue
            key = (int(tv.tmdb_id), int(season.season_number))
            index[key] = {
                "guid": str(season.guid),
                "title": season.tv_title or season.parent_title or tv.title or "",
            }
    if not index and cached:
        return cached[1]
    _SEASON_INDEX[library_guid] = (now, index)
    log.info("飞牛季索引已建立：库 %s，%d 季", library_guid, len(index))
    return index


def forget_index(library_guid: str | None = None) -> None:
    if library_guid is None:
        _SEASON_INDEX.clear()
    else:
        _SEASON_INDEX.pop(library_guid, None)


# ---------------------------------------------------------------- MC 侧聚合
async def _all_items(ops: Any, library_id: int, *, page: int = 200) -> list[dict[str, Any]]:
    """按页拉全库条目（上限 5000，防止异常情况下无限翻页）。"""
    out: list[dict[str, Any]] = []
    offset = 0
    while offset < 5000:
        rows = await ops.call(
            "library.items.list",
            {"library_id": int(library_id), "limit": page, "offset": offset},
        )
        rows = rows or []
        out.extend(row for row in rows if isinstance(row, dict))
        if len(rows) < page:
            break
        offset += page
    return out


async def mc_season_rows(
    ops: Any, *, library_id: int, log: Any, only: set[tuple[int, int]] | None = None
) -> dict[tuple[int, int], dict[str, Any]]:
    """MC 侧按 ``(tmdb, 季)`` 聚合的识别值：片头片尾取中位数，附带集数。

    ``only`` 给了就只算这些季（省掉飞牛侧根本不存在的季的请求）。
    """
    rows: dict[tuple[int, int], list[tuple[int, int]]] = {}
    titles: dict[tuple[int, int], str] = {}
    items = await _all_items(ops, library_id)
    tv_items = [
        item
        for item in items
        if str(item.get("kind") or "") == "tv" and item.get("tmdb_id")
    ]

    sem = asyncio.Semaphore(8)

    async def one(item: dict[str, Any], season: int) -> tuple[tuple[int, int], list[Any]]:
        async with sem:
            try:
                segs = await ops.call(
                    "library.items.segments",
                    {
                        "library_id": int(library_id),
                        "media_item_id": int(item["media_item_id"]),
                        "season": int(season),
                    },
                )
            except Exception as exc:  # noqa: BLE001 -- 单个条目失败不影响整表
                log.debug("读条目片头片尾失败（%s）：%s", item.get("title"), exc)
                return (int(item["tmdb_id"]), int(season)), []
            return (int(item["tmdb_id"]), int(season)), segs or []

    jobs = []
    for item in tv_items:
        for season in item.get("seasons") or []:
            key = (int(item["tmdb_id"]), int(season))
            if only is not None and key not in only:
                continue
            titles.setdefault(key, str(item.get("title") or ""))
            jobs.append(one(item, int(season)))
    for key, segs in await asyncio.gather(*jobs):
        for row in segs:
            values = skip_values_from_segments(row.get("segments"), row.get("duration_seconds"))
            if values is None:
                continue
            rows.setdefault(key, []).append(values)

    out: dict[tuple[int, int], dict[str, Any]] = {}
    for key, values in rows.items():
        openings = sorted(v[0] for v in values)
        endings = sorted(v[1] for v in values)
        out[key] = {
            "tmdb_id": key[0],
            "season": key[1],
            "title": titles.get(key, ""),
            "mc_opening": median(openings),
            "mc_ending": median(endings),
            "episodes": len(values),
        }
    return out


# ---------------------------------------------------------------- 总览
def _close(left: int | None, right: int | None) -> bool:
    return left is not None and right is not None and int(left) == int(right)


def compare_state(mc: dict[str, Any] | None, trim_cfg: dict[str, Any]) -> str:
    """两边值 → 状态。抽成纯函数，便于单独核对。"""
    t_o = trim_cfg.get("skip_opening")
    t_e = trim_cfg.get("skip_ending")
    if t_o is None and t_e is None:
        return "unset"
    if mc is None or mc.get("mc_opening") is None:
        return "trim_only"
    if _close(t_o, mc.get("mc_opening")) and _close(t_e, mc.get("mc_ending")):
        return "same"
    return "diff"


async def read_trim_configs(
    trim: TrimClient, season_guids: list[str], *, log: Any
) -> dict[str, dict[str, Any]]:
    """并发读一批季的飞牛配置：``季 guid → {skip_opening, skip_ending}``。"""
    sem = asyncio.Semaphore(READ_CONCURRENCY)
    out: dict[str, dict[str, Any]] = {}

    async def one(guid: str) -> None:
        async with sem:
            try:
                out[guid] = await trim.read_play_config(guid)
            except TrimError:
                out[guid] = {"skip_opening": None, "skip_ending": None}

    await asyncio.gather(*(one(g) for g in season_guids))
    return out


async def overview(
    trim: TrimClient,
    ops: Any,
    *,
    libraries: list[dict[str, Any]],
    library_id: int | None,
    log: Any,
) -> dict[str, Any]:
    """片头片尾总览：``{"seasons": [...], "checked_at": ..., "truncated": bool}``。

    ``libraries`` 是插件台账里已接入的库（``{id, name, kind, fn_guid}``），
    只处理剧集库——电影没有季，飞牛的跳过配置也是按季存在剧上的。
    """
    out: list[dict[str, Any]] = []
    truncated = False
    for lib in libraries:
        if library_id is not None and int(lib["id"]) != int(library_id):
            continue
        if str(lib.get("kind") or "") != "tv":
            continue
        guid = str(lib.get("fn_guid") or "")
        if not guid:
            continue
        index = await season_index(trim, guid, log=log)
        if not index:
            continue
        keys = list(index.keys())
        if len(keys) > MAX_SEASONS:
            keys = keys[:MAX_SEASONS]
            truncated = True
        mc_rows = await mc_season_rows(ops, library_id=int(lib["id"]), log=log, only=set(keys))
        configs = await read_trim_configs(
            trim, [str(index[key]["guid"]) for key in keys], log=log
        )
        for key in keys:
            season_guid = str(index[key]["guid"])
            cfg = configs.get(season_guid) or {}
            mc = mc_rows.get(key)
            out.append(
                {
                    "library_id": lib["id"],
                    "library": lib.get("name") or "",
                    "title": (mc or {}).get("title") or index[key].get("title") or "",
                    "tmdb_id": key[0],
                    "season": key[1],
                    "episodes": (mc or {}).get("episodes") or 0,
                    "mc_opening": (mc or {}).get("mc_opening"),
                    "mc_ending": (mc or {}).get("mc_ending"),
                    "trim_opening": cfg.get("skip_opening"),
                    "trim_ending": cfg.get("skip_ending"),
                    "state": compare_state(mc, cfg),
                    "season_guid": season_guid,
                }
            )
    return {"seasons": out, "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "truncated": truncated}


# ---------------------------------------------------------------- 单季读写
def find_season_guid(
    index: dict[tuple[int, int], dict[str, Any]], tmdb_id: int, season: int
) -> str | None:
    row = index.get((int(tmdb_id), int(season)))
    return str(row["guid"]) if row else None


async def write_season(
    trim: TrimClient,
    season_guid: str,
    *,
    opening: int,
    ending: int,
) -> dict[str, Any]:
    """写一季的片头片尾（飞牛不接受 0 清除，0 会被当成「未设置」保留旧值）。"""
    await trim.set_play_config(season_guid, int(opening), int(ending))
    return {
        "season_guid": season_guid,
        "skip_opening": int(opening),
        "skip_ending": int(ending),
    }


async def read_season(trim: TrimClient, season_guid: str) -> dict[str, Any]:
    cfg = await trim.read_play_config(season_guid)
    return {"season_guid": season_guid, **cfg}


__all__ = [
    "CATEGORY_TV",
    "compare_state",
    "find_season_guid",
    "forget_index",
    "mc_season_rows",
    "overview",
    "read_season",
    "read_trim_configs",
    "season_index",
    "write_season",
]


async def season_values(
    ops: Any, *, library_id: int, tmdb_id: int, season: int, log: Any
) -> tuple[int, int] | None:
    """MC 侧某一季的识别结论（片头秒数, 片尾秒数）；没结论返回 None。"""
    key = (int(tmdb_id), int(season))
    rows = await mc_season_rows(ops, library_id=int(library_id), log=log, only={key})
    row = rows.get(key)
    if not row or row.get("mc_opening") is None:
        return None
    return int(row["mc_opening"]), int(row.get("mc_ending") or 0)
