"""进度与片头片尾同步：两个方向 + 一条写回链。

**MC → 飞牛（事件驱动，近实时）**
    订阅 MC 的播放事件（进度/停止/看完），把位置写给飞牛的 ``play/record``。
    - 进度事件本身每集最多 30 秒一条，这里再加一道 20 秒节流：同一文件 20 秒内
      只写一次（飞牛是家用 NAS，不能按事件频率打）；
    - 停止 / 看完事件**不节流**——这两个是用户真正离开或看毕的时刻，必须准；
    - 事件 payload 自带 ``media.tmdb_id`` 与标题，**不需要查宿主的库**。

**飞牛 → MC（定时轮询）**
    拉飞牛的「继续观看」列表（十几条），逐条取条目详情的真实进度；本地身份用
    ``playback.sync.catalog?tmdb_id=`` 查（compare-and-set 需要 updated_at），
    写回走 **``playback.sync.state``**——不自己动观看状态：那个接口做了成员
    可见性、compare-and-set 与事件抑制，重复实现只会引入不一致。
    实测 ``/play/list`` 自带的 ``watched_ts`` 恒为 0，真实进度只在条目详情里。

**片头片尾（飞牛 ← MC）**
    订阅 ``library.segments.updated``（一季识别写回后的可靠事件），按
    ``library.items.segments`` 读结论，按季取中位数写进飞牛——飞牛按季存、
    整季生效（写单集 guid 读不回来，实测结论）。事件结果可能没变，要拿
    上次写入的状态比对，没变就不调接口。

映射关系：两侧靠 **(TMDB id, 季号, 集号)** 收敛，和文件在哪无关。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

#: 同一文件的进度写入最小间隔（秒）
PROGRESS_MIN_GAP = 20.0
#: (tmdb_id, season, episode) → 飞牛 guid 的解析缓存（按标题搜到后核对 TMDB 号）
_RESOLVED: dict[tuple[int, int, int], str | None] = {}
#: 每个飞牛 guid 上次写进度的时间（节流用）
_LAST_WRITE: dict[str, float] = {}
#: 「飞牛里刚看完 → MC 跟上」的加速窗口：定时任务按配置周期跑（默认 60 秒），
#: 但 MC 刚有播放活动时把间隔压到 5 秒补拉几轮，静默后停回常态。
FAST_INTERVAL = 5.0
FAST_WINDOW = 120.0
FAST_ROUNDS = 12  # 单次任务最多补 12 轮（约 1 分钟），下个周期再判断
_LAST_ACTIVITY = 0.0
#: 每轮只查前 N 条详情：play/list 按最近观看排序，正在看的就在最前面
DETAIL_LIMIT = 5


def note_activity() -> None:
    """登记「MC 这边刚有播放动作」，供定时任务决定要不要加速轮询。"""
    global _LAST_ACTIVITY
    _LAST_ACTIVITY = time.monotonic()


def activity_recent() -> bool:
    """最近 ``FAST_WINDOW`` 秒内有没有播放活动。"""
    return time.monotonic() - _LAST_ACTIVITY < FAST_WINDOW


# ---------------------------------------------------------------- MC → 飞牛
async def resolve_guid(
    trim: Any, *, tmdb_id: int, title: str, season: int = 0, episode: int = 0, log: Any
) -> str | None:
    """MC 的条目 → 飞牛条目 guid：按标题搜索，再用 TMDB 号核对。

    为什么不用「已接入库的条目索引」：用户看的片可能在某个**没接入的飞牛库**
    里（插件只接了配置了路径映射的库），索引里根本没有它。搜索接口不限库，
    按 trim_id 核对又足够严，是唯一不依赖库接入的定位方式。

    结果按 (TMDB, 季, 集) 缓存：命中过就不再搜。
    """
    key = (int(tmdb_id), int(season), int(episode))
    if key in _RESOLVED:
        return _RESOLVED[key]
    if not title:
        return None
    from .trim_api import TrimError, trim_tmdb_id

    try:
        rows = await trim.search(title)
    except TrimError as exc:
        log.debug("飞牛搜索失败（%s）：%s", title, exc)
        return None
    for row in rows:
        if trim_tmdb_id(row) != int(tmdb_id):
            continue  # 同名不同片：以 TMDB 号为准
        guid = row.get("guid")
        if not guid:
            continue
        if episode:
            # 剧集：搜到的是剧本体，还要按季集找到具体那一集
            try:
                seasons = await trim.seasons(str(guid))
                for s in seasons:
                    if int(s.season_number or 0) != int(season):
                        continue
                    for ep in await trim.episodes(s.guid):
                        if int(ep.episode_number or 0) == int(episode):
                            _RESOLVED[key] = ep.guid
                            return ep.guid
            except TrimError:
                return None
            return None
        if str(row.get("type") or "") in ("Movie", "Video"):
            _RESOLVED[key] = str(guid)
            return str(guid)
    _RESOLVED[key] = None
    return None


async def push_one(
    trim: Any, *, item_guid: str, position_ms: int, played: bool, force: bool = False, log: Any
) -> bool:
    """把一条进度写给飞牛；节流窗口内跳过（``force`` 用于停止/看完）。"""
    now = time.monotonic()
    last = _LAST_WRITE.get(item_guid, 0.0)
    if not force and now - last < PROGRESS_MIN_GAP:
        return False
    from .trim_api import TrimError

    try:
        await trim.record_progress(
            item_guid, ts=max(0, int(position_ms) // 1000), watched=1 if played else 0
        )
        _LAST_WRITE[item_guid] = now
        return True
    except TrimError as exc:
        log.warning("写飞牛观看进度失败（%s）：%s", item_guid, exc)
        return False


async def handle_playback_event(
    *, payload: Any, trim: Any, log: Any, force: bool, member_username: str
) -> int:
    """处理一次播放事件：定位到飞牛条目后写进度。

    :param force: 停止 / 看完事件不节流
    :param member_username: 这个飞牛账号对应的 MovieClaw 成员名（空 = 超管）。
        只有这个成员的事件会写进飞牛——飞牛的进度按飞牛账号存，把全家人的
        进度都写进同一个账号，家人看过的片会在映射账号那边显示已看。

    事件 payload 自带 ``media.tmdb_id`` / ``media.title``（MediaRef 快照），
    不需要回查宿主的库——离线场景（宿主忙）也不受影响。
    """
    from movieclaw_sdk.events import MediaRef

    who = int(getattr(payload, "member_id", 0) or 0)
    want_name = (member_username or "").strip().lower()
    # 成员名未配置 → 只收超管（member_id=0）；配置了 → 只收这个成员。
    # 成员 id 与名字的对照在入口模块解析（缓存），这里只按名字口径过滤。
    if want_name:
        expect_id = EXPECTED_MEMBER.get("id")
        if expect_id is not None and who != int(expect_id):
            return 0
    elif who != 0:
        return 0

    media = getattr(payload, "media", None)
    if media is None or not isinstance(media, MediaRef):
        return 0
    tmdb_id = getattr(media, "tmdb_id", None)
    if not tmdb_id:
        return 0
    unit = getattr(payload, "unit", None)
    # UnitRef 的字段名是 season / episode（不是 season_number / episode_number）。
    # 取错名字会一直拿到 0，剧集就被当成电影去飞牛里找，永远找不到 —— 实测踩过。
    season = int(getattr(unit, "season", 0) or 0) if unit is not None else 0
    episode = int(getattr(unit, "episode", 0) or 0) if unit is not None else 0
    position_ms = getattr(payload, "position_ms", None)
    if position_ms is None:
        return 0
    played = bool(getattr(payload, "played", False))
    title = str(getattr(media, "title", "") or "")

    guid = await resolve_guid(
        trim, tmdb_id=int(tmdb_id), title=title, season=season, episode=episode, log=log
    )
    if guid is None:
        log.info("飞牛里找不到这部（TMDB %s《%s》），跳过进度回写", tmdb_id, title)
        return 0
    if await push_one(
        trim, item_guid=guid, position_ms=int(position_ms), played=played, force=force, log=log
    ):
        log.info("已把进度写进飞牛：《%s》→ %ds%s", title, int(position_ms) // 1000,
                 "（看完）" if played else "")
        return 1
    return 0


#: 入口模块解析好的「映射成员名 → 成员 id」（供事件侧过滤用）
EXPECTED_MEMBER: dict[str, int | None] = {}


# ---------------------------------------------------------------- 飞牛 → MC
async def pull_tick(*, trim: Any, ops: Any, log: Any, member_id: int = 0) -> int:
    """飞牛 → MC：拉「继续观看」，把真实进度交给主程序的 ``playback.sync.state``。

    **不按「库是否接入」过滤**：飞牛条目自带 TMDB 编号（``trim_id``），用它查
    ``playback.sync.catalog``（按 TMDB 过滤、拿 updated_at）就够。也不自己写
    观看状态：宿主接口带 compare-and-set 与事件抑制，重复实现只会让两边版本打架。

    只在「MC 已看完、飞牛还没看完」时不让倒退——那是两边不同步的旧状态，
    写回去会把人拉回未看完。位置靠前靠后都以飞牛为准（飞牛是用户刚在用的播放器）。
    """
    from .trim_api import TrimError, trim_tmdb_id

    applied = 0
    try:
        rows = await trim.play_list()
    except TrimError as exc:
        log.warning("拉飞牛继续观看列表失败：%s", exc)
        return 0

    for row in rows[:DETAIL_LIMIT]:
        guid = row.get("guid")
        if not guid:
            continue
        try:
            detail = await trim.item_detail(str(guid))
        except TrimError:
            continue
        ts = int(detail.get("watched_ts") or 0)
        watched = bool(detail.get("is_watched"))
        if ts <= 0 and not watched:
            continue
        tmdb_id = trim_tmdb_id(detail)
        if not tmdb_id:
            continue
        kind = "movie" if str(detail.get("type") or "") == "Movie" else "tv"
        season = int(detail.get("season_number") or 0)
        episode = int(detail.get("episode_number") or 0)
        if kind == "tv" and episode <= 0:
            continue  # SyncChangeIn：剧集的集号不能是 0（特别篇也不行）
        # 本地身份与状态行：按 TMDB 过滤的 catalog 一页（50 条）绰绰有余
        local = await _catalog_unit(ops, member_id=member_id, tmdb_id=int(tmdb_id),
                                    season=season, episode=episode)
        if local is None:
            continue  # MC 库里没有这部：没有可写的状态行
        media_item_id, state = local
        if state is not None:
            st_played = bool(state.get("played"))
            st_position = int(state.get("position_ms") or 0)
            if st_played and not watched:
                continue
            if st_played == watched and abs(st_position - ts * 1000) <= 1000:
                continue  # 值没变就别发请求：省一次 compare-and-set
        change: dict[str, Any] = {
            "media_item_id": media_item_id,
            "kind": kind,
            "tmdb_id": int(tmdb_id),
            # compare-and-set 必填：catalog 预览时没有状态行就是 null
            "expected_updated_at": state.get("updated_at") if state else None,
            "played": watched,
        }
        if not watched and ts > 0:
            change["position_ms"] = ts * 1000
        # 观看时间只在新记录时给，**已有的必须原样保留**：每轮都写「现在」的话，
        # 按 last_played_at 排序的「最近观看」会被轮询反复重排（用户真的会看到
        # 列表在跳、记录像被挤没了）。飞牛不提供观看时刻，拿不到就沿用 MC 已有值。
        if state is not None and state.get("last_played_at"):
            change["last_played_at"] = state["last_played_at"]
        if kind == "movie":
            change.update(season=0, episode=0)
        else:
            change.update(season=season, episode=episode)
        # playback.sync.state 是「单个请求体」接口：整体挂 body 槽位
        result = await ops.call(
            "playback.sync.state", {"body": {"member_id": member_id, "changes": [change]}}
        )
        outcomes = (result or {}).get("outcomes") or []
        status = str(outcomes[0].get("status") or "") if outcomes else ""
        if status == "applied":
            applied += 1
        elif status and status != "unchanged":
            log.info("进度写回被跳过（%s）：可能是版本冲突或条目已变", status)
    if applied:
        log.info("从飞牛拉回观看进度：%d 条", applied)
    return applied


async def _catalog_unit(
    ops: Any, *, member_id: int, tmdb_id: int, season: int, episode: int
) -> tuple[int, dict[str, Any] | None] | None:
    """按 TMDB 查宿主目录：返回 (media_item_id, 该单元的状态行或 None)。

    ``playback.sync.catalog?tmdb_id=`` 只回这部作品的单元（一页 50 条足够；
    剧集可能同号但 kind 不同，按季集号收敛到唯一一行）。
    """
    data = await ops.call(
        "playback.sync.catalog", {"member_id": member_id, "tmdb_id": tmdb_id, "limit": 50}
    )
    for unit in (data or {}).get("items") or []:
        media = unit.get("media") or {}
        if int(media.get("tmdb_id") or 0) != int(tmdb_id):
            continue
        if int(unit.get("season") or 0) != int(season) or int(unit.get("episode") or 0) != int(
            episode
        ):
            continue
        return int(media["id"]), {
            "played": bool(unit.get("played")),
            "position_ms": int(unit.get("position_ms") or 0),
            "updated_at": unit.get("updated_at"),
            "last_played_at": unit.get("last_played_at"),
        }
    return None


# ---------------------------------------------------------------- 片头片尾
def skip_values_from_segments(segments: Any, duration_seconds: Any) -> tuple[int, int] | None:
    """MovieClaw 的片头片尾结论 → 飞牛的 (skip_opening, skip_ending) 秒。

    - ``skip_opening``：片头结束时刻（跳过多少秒）；
    - ``skip_ending``：片尾开始时刻距结尾还有多少秒。

    没有结论返回 None（不写，避免把飞牛已有配置覆盖成 0）；
    荒谬值（片头 >10 分钟、片尾 >15 分钟）当识别噪声丢掉。
    """
    import json as _json

    if not segments:
        return None
    try:
        items = _json.loads(segments) if isinstance(segments, str) else segments
    except (TypeError, ValueError):
        return None
    if not isinstance(items, (list, tuple)) or not items:
        return None
    intro_end_ms = None
    outro_start_ms = None
    for seg in items:
        if not isinstance(seg, dict):
            continue
        if seg.get("type") == "intro" and seg.get("end_ms"):
            intro_end_ms = int(seg["end_ms"])
        elif seg.get("type") == "outro" and seg.get("start_ms") is not None:
            outro_start_ms = int(seg["start_ms"])
    skip_opening = int(intro_end_ms // 1000) if intro_end_ms else 0
    skip_ending = 0
    if outro_start_ms is not None and duration_seconds:
        dur_ms = int(float(duration_seconds) * 1000)
        if 0 < outro_start_ms < dur_ms:
            skip_ending = int((dur_ms - outro_start_ms) // 1000)
    if skip_opening <= 0 and skip_ending <= 0:
        return None
    if skip_opening > 600 or skip_ending > 900:
        return None
    return skip_opening, skip_ending


def median(values: list[int]) -> int:
    """中位数（偶数个取偏右的那个，与统计口径无关紧要——秒级抖动可忽略）。"""
    ordered = sorted(values)
    return ordered[len(ordered) // 2]


async def push_markers(
    trim: Any, *, rows: list[tuple[str, int, int, str]], state: dict[str, Any], log: Any,
    max_writes: int = 400,
) -> dict[str, int]:
    """把 MC 识别出的片头片尾写进飞牛（``skip_opening`` / ``skip_ending`` 秒）。

    写**季 guid**：飞牛按季存配置，写在哪一季，整季每一集立刻生效（实测）。
    值没变就不调接口（``state`` 是插件侧记录的「已写过什么」）。
    """
    from .trim_api import TrimError

    written = skipped = failed = 0
    for guid, skip_opening, skip_ending, label in rows:
        if written >= max_writes:
            break
        prev = state.get(guid)
        if (
            isinstance(prev, (list, tuple))
            and len(prev) == 2
            and int(prev[0]) == skip_opening
            and int(prev[1]) == skip_ending
        ):
            skipped += 1
            continue
        try:
            await trim.set_play_config(guid, skip_opening, skip_ending)
            state[guid] = [skip_opening, skip_ending]
            written += 1
            log.info("飞牛片头片尾已写：%s（片头 %ds / 片尾 %ds）", label, skip_opening, skip_ending)
        except TrimError as exc:
            failed += 1
            log.warning("写飞牛片头片尾失败（%s）：%s", label, exc)
        await asyncio.sleep(0.12)  # 家用 NAS：别按接口上限打
    return {"written": written, "skipped": skipped, "failed": failed}
