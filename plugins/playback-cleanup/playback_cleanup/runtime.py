"""续播进度清理的全部逻辑：选择、播放结束后的延迟清理、兜底扫描、清理记录与撤销。

只经宿主操作 ``playback.sync.state`` 写 ``position_ms → 0``（带 compare-and-set 的
``expected_updated_at``），不碰已看 / 收藏 / 播放次数 / 最近播放时间 / 播放流水。
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from .settings import ADMIN_MEMBER_ID, Settings

try:  # 宿主环境里用 SDK 的异常；纯单元测试没有 SDK 时用同构的替身
    from movieclaw_sdk.ops import OpsError
except ImportError:  # pragma: no cover - 只在没有 SDK 的环境里走到

    class OpsError(Exception):  # type: ignore[no-redef]
        def __init__(self, status: int, code: str, message: str, details=None) -> None:
            super().__init__(f"{code}（{status}）：{message}")
            self.status, self.code, self.message, self.details = status, code, message, details


SETTINGS_KEY = "settings"
HISTORY_KEY = "history"
RECENT_KEY = "recent"
LAST_RUN_KEY = "last_run"
PENDING_KEY = "pending"

SWEEP_TASK = "playback-cleanup.sweep"
SWEEP_INTERVAL = 3600

HISTORY_LIMIT = 120  # 插件数据单值上限 64KB
RECENT_LIMIT = 60
RUN_ITEMS_LIMIT = 40
CATALOG_PAGE = 100
#: 宿主 catalog 默认只给 movie / tv；「其他」库的 video 须显式要。旧宿主（0.46.0）的操作目录里
#: 没有 ``kinds``，带上会在发请求前就报 INVALID_ARGUMENTS——那时退回不带，只看影视。
CATALOG_KINDS = ["movie", "tv", "video"]
SYNC_KINDS = ("movie", "tv", "video")
ITEMS_PAGE = 200
STATE_BATCH = 100  # sync.state 单次最多 100 条
ITEMS_TTL = 600.0  # 库内条目缓存 10 分钟
ITEMS_MISS_REFRESH = 30.0  # 事件里的条目不在缓存时，缓存旧于 30 秒就重读一次
_FAR = 2_147_483_647  # 游标里「比任何季 / 集号都大」的数

Unit = tuple[int, int, int, int]  # (member_id, media_item_id, season, episode)；电影季集为 0


class SkipItem(Exception):
    """给人看的失败原因（路由转成 4xx/5xx）。"""


def _now_iso(clock: Callable[[], float]) -> str:
    return datetime.fromtimestamp(clock(), UTC).isoformat()


def aware_iso(value: Any) -> str | None:
    """宿主时间统一成带时区的 UTC ISO 字符串；不带时区的按 UTC 理解（数据库存的就是 UTC）。"""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None or dt.utcoffset() is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat()


def same_time(a: Any, b: Any) -> bool:
    x, y = aware_iso(a), aware_iso(b)
    return x is not None and x == y


def unit_key(unit: Unit) -> str:
    return ":".join(map(str, unit))


def parse_key(text: str) -> Unit | None:
    try:
        parts = tuple(int(p) for p in text.split(":"))
    except ValueError:
        return None
    return parts if len(parts) == 4 else None  # type: ignore[return-value]


def seek_cursor(media_item_id: int, season: int, episode: int) -> str:
    """catalog 按 (条目, 季, 集) 升序、游标「之后」翻页：给出恰好排在目标单元前面的游标。"""
    if episode > 0:
        return f"{media_item_id}:{season}:{episode - 1}"
    if season > 0:
        return f"{media_item_id}:{season - 1}:{_FAR}"
    return f"{media_item_id - 1}:{_FAR}:{_FAR}"


def catalog_unit(member_id: int, row: dict) -> Unit | None:
    media = row.get("media") if isinstance(row.get("media"), dict) else None
    if not media or media.get("id") is None:
        return None
    return (
        int(member_id),
        int(media["id"]),
        int(row.get("season") or 0),
        int(row.get("episode") or 0),
    )


def has_external_ids(media: dict) -> bool:
    return bool(media.get("tmdb_id") or media.get("imdb_id"))


def identity_token(media: dict) -> str | None:
    """catalog 下发的不可复用身份令牌（宿主 #1203 起 ``media.identity`` = ``external_id``）。

    旧宿主（0.46–0.48）的 catalog 没有这个字段，返回 None。
    """
    token = media.get("identity")
    return token if isinstance(token, str) and token else None


def write_token(media: dict) -> str | None:
    """写回时要带的令牌：只有两个外部编号都没有、且 catalog 给了非空令牌时才带。

    有 TMDB / IMDb 的条目宿主只核编号，不带令牌（请求形状与旧版一致）；旧宿主没给令牌时也不带
    ——旧宿主的 ``SyncChangeIn`` 是 ``extra=forbid``，多送 ``identity`` 会让整批 422。
    """
    if has_external_ids(media):
        return None
    return identity_token(media)


def build_change(row: dict, position_ms: int, expected: str | None) -> dict[str, Any]:
    """一个单元的写回：只带身份字段、``expected_updated_at`` 和 ``position_ms``。

    故意不带 ``played`` / ``is_favorite`` / ``last_played_at``：宿主对省略的字段一律不改。
    身份字段：有外部编号的带吻合的编号；没有的带 catalog 当时下发的 ``identity`` 令牌
    （没有令牌就不带，旧宿主照实回 ``mismatch``）。
    """
    media = row["media"]
    change: dict[str, Any] = {
        "media_item_id": int(media["id"]),
        "kind": media["kind"],
        "expected_updated_at": expected,
        "position_ms": int(position_ms),
    }
    if media.get("tmdb_id"):
        change["tmdb_id"] = int(media["tmdb_id"])
    if media.get("imdb_id"):
        change["imdb_id"] = str(media["imdb_id"])
    token = write_token(media)
    if token is not None:
        change["identity"] = token
    if media["kind"] == "tv":
        change["season"] = int(row.get("season") or 0)
        change["episode"] = int(row.get("episode") or 0)
    return change


def has_identity(row: dict) -> bool:
    """能写回的单元：有条目 id、类型是 movie / tv / video。

    不要求 TMDB / IMDb：两个外部编号都没有的本地条目由 ``build_change`` 带上 catalog 下发的
    ``identity`` 令牌（请求不带编号）；有编号的条目带上吻合的编号。旧宿主没有令牌，对无编号
    条目会回 ``mismatch``，照实显示。
    """
    media = row.get("media") or {}
    return media.get("id") is not None and media.get("kind") in SYNC_KINDS


def same_target(before: dict, after: dict) -> bool:
    """CAS 冲突重读后，还是不是当初那个条目：类型、外部编号、（无编号时）身份令牌都得一致。

    删除 A、再建 B 复用数字 id 时令牌会变；这时不能改用 B 的新令牌再写。
    """
    a, b = before.get("media") or {}, after.get("media") or {}
    if a.get("kind") != b.get("kind"):
        return False
    if (a.get("tmdb_id") or None, a.get("imdb_id") or None) != (
        b.get("tmdb_id") or None,
        b.get("imdb_id") or None,
    ):
        return False
    if has_external_ids(a):
        return True
    return identity_token(a) == identity_token(b)


def label(row: dict) -> str:
    media = row.get("media") or {}
    title = str(media.get("title") or f"条目 {media.get('id')}")
    if media.get("kind") == "tv":
        return f"{title} S{int(row.get('season') or 0):02d}E{int(row.get('episode') or 0):02d}"
    return title


class Runtime:
    def __init__(
        self,
        *,
        ops: Any,
        store: Any,
        logger: logging.Logger | Any = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    ) -> None:
        self.ops = ops
        self.store = store
        self.logger = logger or logging.getLogger("playback-cleanup")
        self.clock = clock
        self.sleep = sleep
        self._timers: dict[Unit, asyncio.Task] = {}
        self._firing: set[asyncio.Task] = set()
        self._items: dict[str, Any] = {"library_id": None, "titles": {}, "at": 0.0}
        self._write = asyncio.Lock()
        self._store_lock = asyncio.Lock()
        self._sweep: asyncio.Task | None = None
        self._sweep_info: dict[str, Any] | None = None
        self._kinds_ok = True  # 宿主 catalog 认不认 kinds；第一次被拒后不再带

    # ------------------------------------------------------------------ 设置与选择
    async def settings(self) -> Settings:
        return Settings.load(await self.store.get(SETTINGS_KEY, default=None))

    async def _library_rows(self) -> list[dict]:
        try:
            rows = await self.ops.call("library.list", {})
        except OpsError as exc:
            raise SkipItem(f"读取媒体库失败：{exc.message}") from exc
        return [r for r in (rows or []) if isinstance(r, dict) and r.get("id") is not None]

    async def _member_rows(self) -> list[dict]:
        """宿主成员列表 + 超管（成员 id 0，很多安装只有超管）。"""
        try:
            rows = await self.ops.call("members.list", {})
        except OpsError as exc:
            raise SkipItem(f"读取成员失败：{exc.message}") from exc
        out = [
            {"id": ADMIN_MEMBER_ID, "name": "超管", "username": "admin", "active": True}
        ]
        for r in rows or []:
            if not isinstance(r, dict) or r.get("id") is None or int(r["id"]) == ADMIN_MEMBER_ID:
                continue
            out.append(
                {
                    "id": int(r["id"]),
                    "name": str(r.get("nickname") or r.get("username") or f"成员 {r['id']}"),
                    "username": r.get("username"),
                    "active": (r.get("status") or "active") == "active",
                }
            )
        return out

    @staticmethod
    def eligible(row: dict) -> tuple[bool, str | None]:
        """宿主的观看同步覆盖电影 / 剧集 / 「其他」视频条目。"""
        if row.get("kind") in SYNC_KINDS:
            return True, None
        return False, "宿主观看同步只覆盖电影 / 剧集 / 视频；这个库里大概率没有能清的续播进度"

    async def choices(self) -> dict[str, Any]:
        settings = await self.settings()
        notes: list[str] = []
        libraries = []
        for row in await self._library_rows():
            ok, reason = self.eligible(row)
            libraries.append(
                {
                    "id": int(row["id"]),
                    "name": str(row.get("name") or ""),
                    "kind": row.get("kind"),
                    "source": row.get("source"),
                    "eligible": ok,
                    "reason": reason,
                }
            )
        if settings.library_id is not None and settings.library_id not in {
            lib["id"] for lib in libraries
        }:
            notes.append(f"已选的媒体库 {settings.library_id} 不存在了，请重新选择")
        members = await self._member_rows()
        known = {m["id"] for m in members}
        missing = [m for m in settings.member_ids if m not in known]
        if missing:
            notes.append("已选的成员不存在了：" + "、".join(map(str, missing)))
        return {
            "settings": settings.model_dump(),
            "libraries": libraries,
            "members": members,
            "notes": notes,
        }

    async def save_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            settings = Settings.model_validate(payload)
        except ValueError as exc:
            raise SkipItem(f"设置不对：{exc}") from exc
        if settings.library_id is not None:
            ids = {int(r["id"]) for r in await self._library_rows()}
            if settings.library_id not in ids:
                raise SkipItem(f"媒体库 {settings.library_id} 不存在")
        if settings.member_ids:
            known = {m["id"] for m in await self._member_rows()}
            bad = [m for m in settings.member_ids if m not in known]
            if bad:
                raise SkipItem("成员不存在：" + "、".join(map(str, bad)))
        await self.store.set(SETTINGS_KEY, settings.model_dump())
        if settings.library_id != self._items.get("library_id"):
            self._items = {"library_id": None, "titles": {}, "at": 0.0}
        if settings.selection_state() != "ready":
            # 关掉 / 取消选择后，等着的延迟清理一并作废
            await self.cancel_pending()
        return await self.choices()

    # ------------------------------------------------------------------ 库内条目
    async def library_titles(self, library_id: int, *, force: bool = False) -> dict[int, str]:
        """选中库里的条目 id → 标题（catalog 没有库过滤，只能客户端按这个集合筛）。"""
        cache = self._items
        fresh = self.clock() - float(cache.get("at") or 0) < ITEMS_TTL
        if not force and cache.get("library_id") == library_id and fresh:
            return cache["titles"]
        titles: dict[int, str] = {}
        offset = 0
        while True:
            try:
                rows = await self.ops.call(
                    "library.items.list",
                    {"library_id": library_id, "limit": ITEMS_PAGE, "offset": offset},
                )
            except OpsError as exc:
                raise SkipItem(f"读取媒体库条目失败：{exc.message}") from exc
            rows = [r for r in (rows or []) if isinstance(r, dict)]
            for row in rows:
                if row.get("media_item_id") is not None:
                    titles[int(row["media_item_id"])] = str(row.get("title") or "")
            if len(rows) < ITEMS_PAGE:
                break
            offset += len(rows)
        self._items = {"library_id": library_id, "titles": titles, "at": self.clock()}
        return titles

    async def in_library(self, library_id: int, media_item_id: int) -> bool:
        titles = await self.library_titles(library_id)
        if media_item_id in titles:
            return True
        if self.clock() - float(self._items.get("at") or 0) >= ITEMS_MISS_REFRESH:
            titles = await self.library_titles(library_id, force=True)  # 可能是刚入库的
        return media_item_id in titles

    # ------------------------------------------------------------------ 读 catalog
    async def catalog(self, member_id: int):
        """按成员翻完整个 catalog（只含成员和插件都能看到的库）。"""
        cursor: str | None = None
        while True:
            params: dict[str, Any] = {"member_id": member_id, "limit": CATALOG_PAGE}
            if cursor:
                params["cursor"] = cursor
            page = await self._catalog_page(params)
            for row in (page or {}).get("items") or []:
                if isinstance(row, dict):
                    yield row
            cursor = (page or {}).get("next_cursor")
            if not cursor:
                return

    async def _catalog_page(self, params: dict[str, Any]) -> Any:
        """读一页 catalog，带上 ``kinds``（含 video）；旧宿主不认这个参数时退回不带。"""
        if self._kinds_ok:
            try:
                return await self.ops.call(
                    "playback.sync.catalog", {**params, "kinds": list(CATALOG_KINDS)}
                )
            except OpsError as exc:
                if exc.code != "INVALID_ARGUMENTS" or "kinds" not in str(exc.message):
                    raise
                self._kinds_ok = False
                self.logger.info("playback-cleanup：宿主 catalog 不支持 kinds，只看电影 / 剧集")
        return await self.ops.call("playback.sync.catalog", params)

    async def lookup(self, unit: Unit) -> dict | None:
        """直接定位一个单元的当前状态：游标跳到它前面，读一小页。"""
        member_id, media_item_id, season, episode = unit
        page = await self._catalog_page(
            {
                "member_id": member_id,
                "cursor": seek_cursor(media_item_id, season, episode),
                "limit": 5,
            }
        )
        for row in (page or {}).get("items") or []:
            if isinstance(row, dict) and catalog_unit(member_id, row) == unit:
                return row
        return None

    # ------------------------------------------------------------------ 写入（CAS + 冲突重试一次）
    async def _state(self, member_id: int, changes: list[dict]) -> list[str | dict]:
        result = await self.ops.call(
            "playback.sync.state", {"member_id": member_id, "changes": changes}
        )
        outcomes = (result or {}).get("outcomes") or []
        return [o if isinstance(o, dict) else {"status": "failed"} for o in outcomes]

    async def clear(self, member_id: int, rows: list[dict], *, source: str) -> list[dict]:
        """把这些单元的续播进度清零。返回每个单元的结果（cleared / conflict / unchanged / ...）。"""
        results: list[dict] = []
        todo: list[dict] = []
        for row in rows:
            if not has_identity(row):
                results.append(self._result(member_id, row, "no_identity"))
            elif int(row.get("position_ms") or 0) <= 0:
                results.append(self._result(member_id, row, "already_zero"))
            else:
                todo.append(row)
        async with self._write:
            retry: list[dict] = []
            for status, row, outcome in await self._apply(member_id, todo):
                if status == "conflict":
                    retry.append(row)
                else:
                    results.append(await self._finish(member_id, row, status, outcome, source))
            fresh: list[dict] = []
            for row in retry:
                unit = catalog_unit(member_id, row)
                again = await self.lookup(unit) if unit else None
                if again is None:
                    results.append(self._result(member_id, row, "not_found"))
                elif not same_target(row, again):
                    # 条目被删了重建（数字 id 复用）或改了识别：不拿新令牌 / 新编号再写
                    results.append(
                        self._result(
                            member_id, row, "mismatch", error="重读后条目身份变了，没有写回"
                        )
                    )
                elif int(again.get("position_ms") or 0) <= 0:
                    results.append(self._result(member_id, again, "already_zero"))
                else:
                    fresh.append(again)
            for status, row, outcome in await self._apply(member_id, fresh):
                results.append(await self._finish(member_id, row, status, outcome, source))
        return results

    async def _apply(self, member_id: int, rows: list[dict]) -> list[tuple[str, dict, dict]]:
        out: list[tuple[str, dict, dict]] = []
        for start in range(0, len(rows), STATE_BATCH):
            chunk = rows[start : start + STATE_BATCH]
            changes = [build_change(r, 0, aware_iso(r.get("updated_at"))) for r in chunk]
            try:
                outcomes = await self._state(member_id, changes)
            except OpsError as exc:
                self.logger.warning("playback-cleanup：写回失败：%s", exc.message)
                out.extend(("failed", r, {"error": exc.message}) for r in chunk)
                continue
            for i, row in enumerate(chunk):
                outcome = outcomes[i] if i < len(outcomes) else {"status": "failed"}
                out.append((str(outcome.get("status") or "failed"), row, outcome))
        return out

    def _result(self, member_id: int, row: dict, status: str, **extra: Any) -> dict:
        media = row.get("media") or {}
        return {
            "member_id": member_id,
            "media_item_id": media.get("id"),
            "kind": media.get("kind"),
            "season": int(row.get("season") or 0),
            "episode": int(row.get("episode") or 0),
            "title": label(row),
            "position_ms": int(row.get("position_ms") or 0),
            "status": status,
            **extra,
        }

    async def _finish(
        self, member_id: int, row: dict, status: str, outcome: dict, source: str
    ) -> dict:
        if status != "applied":
            return self._result(member_id, row, status, error=outcome.get("error"))
        media = row["media"]
        entry = {
            "id": uuid.uuid4().hex[:12],
            "at": _now_iso(self.clock),
            "source": source,
            "member_id": member_id,
            "media_item_id": int(media["id"]),
            "kind": media["kind"],
            "tmdb_id": media.get("tmdb_id"),
            "imdb_id": media.get("imdb_id"),
            # 这次写回实际用的令牌（有外部编号 / 旧宿主时为 None）；撤销只回放它，不再查 catalog
            "identity": write_token(media),
            "season": int(row.get("season") or 0),
            "episode": int(row.get("episode") or 0),
            "title": label(row),
            "prior_position_ms": int(row.get("position_ms") or 0),
            "expected_updated_at": aware_iso(row.get("updated_at")),
            "state_version": aware_iso(outcome.get("state_version")),
            "undone_at": None,
            "undo_status": None,
            "undo_version": None,
        }
        await self._push(HISTORY_KEY, entry, HISTORY_LIMIT)
        return self._result(member_id, row, "cleared", history_id=entry["id"])

    async def _push(self, key: str, entry: dict, limit: int) -> None:
        async with self._store_lock:
            rows = await self.store.get(key, default=None)
            rows = rows if isinstance(rows, list) else []
            rows.insert(0, entry)
            await self.store.set(key, rows[:limit])

    async def _note(self, source: str, result: dict, dry_run: bool) -> None:
        await self._push(
            RECENT_KEY,
            {"at": _now_iso(self.clock), "source": source, "dry_run": dry_run, **result},
            RECENT_LIMIT,
        )

    # ------------------------------------------------------------------ 事件路径
    async def on_playback(self, payload: Any) -> str:
        """``playback.stopped`` / ``playback.completed``：命中就排一个延迟清理。返回处理结果。"""
        settings = await self.settings()
        state = settings.selection_state()
        if state != "ready":
            return state
        member_id = getattr(payload, "member_id", None)
        media = getattr(payload, "media", None)
        if member_id is None or media is None or getattr(media, "id", None) is None:
            return "ignored"
        if int(member_id) not in settings.member_ids:
            return "other_member"
        if getattr(media, "kind", None) not in SYNC_KINDS:
            return "other_kind"
        try:
            if not await self.in_library(settings.library_id, int(media.id)):
                return "other_library"
        except SkipItem as exc:
            self.logger.warning("playback-cleanup：%s", exc)
            return "failed"
        # 电影 / 视频只有一个 0/0 单元，只有剧集看事件里的季集号
        unit_ref = getattr(payload, "unit", None) if media.kind == "tv" else None
        season = int(getattr(unit_ref, "season", 0) or 0) if unit_ref else 0
        episode = int(getattr(unit_ref, "episode", 0) or 0) if unit_ref else 0
        unit: Unit = (int(member_id), int(media.id), season, episode)
        await self.schedule(unit, settings.delay_seconds)
        return "scheduled"

    async def schedule(self, unit: Unit, delay: float) -> None:
        """同一单元只留一个等待中的清理：再来事件就重新计时（stopped 与 completed 常成对到）。"""
        old = self._timers.pop(unit, None)
        if old is not None and not old.done() and old not in self._firing:
            old.cancel()  # 还在等：重新计时；已经在清的让它清完
        async with self._store_lock:
            pending = await self.store.get(PENDING_KEY, default=None)
            pending = pending if isinstance(pending, dict) else {}
            pending[unit_key(unit)] = self.clock() + delay
            await self.store.set(PENDING_KEY, pending)
        self._timers[unit] = asyncio.create_task(self._delayed(unit, delay))

    async def _delayed(self, unit: Unit, delay: float) -> None:
        try:
            await self.sleep(max(0.0, delay))
        except asyncio.CancelledError:
            return
        me = asyncio.current_task()
        if me is not None:
            self._firing.add(me)
        try:
            await self.fire(unit)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 后台任务出错只记日志，兜底扫描会补上
            self.logger.warning("playback-cleanup：延迟清理出错：%s", exc)
        finally:
            self._firing.discard(me)
            if self._timers.get(unit) is me:
                self._timers.pop(unit, None)
                await self._drop_pending(unit)

    async def _drop_pending(self, unit: Unit) -> None:
        async with self._store_lock:
            pending = await self.store.get(PENDING_KEY, default=None)
            if isinstance(pending, dict) and pending.pop(unit_key(unit), None) is not None:
                await self.store.set(PENDING_KEY, pending)

    async def fire(self, unit: Unit) -> dict | None:
        """延迟到点：重新读当前状态，仍有续播进度就清（只预览时只记录）。"""
        settings = await self.settings()
        member_id = unit[0]
        if settings.selection_state() != "ready" or member_id not in settings.member_ids:
            return None
        try:
            if not await self.in_library(settings.library_id, unit[1]):
                return None
            row = await self.lookup(unit)
        except (OpsError, SkipItem) as exc:
            self.logger.warning("playback-cleanup：读取播放状态失败：%s", getattr(exc, "message", exc))
            return None
        if row is None or int(row.get("position_ms") or 0) <= 0:
            return None  # 已经是 0（看完了 / 别处清过）：什么都不做
        if settings.dry_run:
            result = self._result(member_id, row, "preview")
        else:
            result = (await self.clear(member_id, [row], source="event"))[0]
        await self._note("event", result, settings.dry_run)
        return result

    async def restore_pending(self) -> int:
        """插件重启后，把还没到点的延迟清理接着排上（过期的尽快清）。"""
        pending = await self.store.get(PENDING_KEY, default=None)
        if not isinstance(pending, dict):
            return 0
        n = 0
        for key, due in list(pending.items()):
            unit = parse_key(key)
            if unit is None:
                continue
            delay = max(1.0, float(due or 0) - self.clock())
            self._timers[unit] = asyncio.create_task(self._delayed(unit, delay))
            n += 1
        return n

    async def cancel_pending(self) -> None:
        for task in self._timers.values():
            task.cancel()
        self._timers.clear()
        await self.store.set(PENDING_KEY, {})

    async def drain(self) -> None:
        """等所有等待中的延迟清理跑完（测试用）。"""
        while self._timers or self._firing:
            tasks = [*self._timers.values(), *self._firing]
            await asyncio.gather(*tasks, return_exceptions=True)

    async def close(self) -> None:
        # 只取消任务，不清 PENDING_KEY：下次启动接着排
        for task in self._timers.values():
            task.cancel()
        self._timers.clear()
        if self._sweep is not None and not self._sweep.done():
            self._sweep.cancel()

    # ------------------------------------------------------------------ 扫描路径
    async def sweep(self, *, reason: str, dry_run: bool) -> dict[str, Any]:
        """扫一遍选中库、选中成员的残留续播进度；只预览时只列出来。"""
        settings = await self.settings()
        state = settings.selection_state()
        if reason == "schedule" and state == "ready" and not settings.sweep_enabled:
            state = "sweep_off"
        # 手动运行不看「启用」开关（关掉的是自动的那两条路），但库和成员必须选
        if state in ("no_selection", "no_members") or (
            state != "ready" and reason == "schedule"
        ):
            self.logger.info("playback-cleanup：%s，跳过（%s）", state, reason)
            return {"status": state, "reason": reason}
        started = _now_iso(self.clock)
        counts = {"candidates": 0, "cleared": 0, "previewed": 0, "conflicts": 0, "skipped": 0, "failed": 0}
        items: list[dict] = []
        notes: list[str] = []
        status = "ok"
        try:
            titles = await self.library_titles(settings.library_id, force=True)
        except SkipItem as exc:
            return await self._save_run(
                {"status": "failed", "reason": reason, "dry_run": dry_run, "started_at": started,
                 "finished_at": _now_iso(self.clock), "counts": counts, "items": [], "notes": [str(exc)]}
            )
        kept = await self._undo_guards()
        for member_id in settings.member_ids:
            candidates: list[dict] = []
            try:
                async for row in self.catalog(member_id):
                    unit = catalog_unit(member_id, row)
                    if unit is None or unit[1] not in titles:
                        continue
                    if int(row.get("position_ms") or 0) <= 0:
                        continue
                    if any(same_time(row.get("updated_at"), v) for v in kept.get(unit, ())):
                        counts["skipped"] += 1  # 撤销过、之后没再动：别又清掉
                        continue
                    candidates.append(row)
            except OpsError as exc:
                notes.append(f"成员 {member_id}：读取观看状态失败：{exc.message}")
                status = "partial"
                continue
            counts["candidates"] += len(candidates)
            if dry_run:
                results = [self._result(member_id, r, "preview") for r in candidates]
            else:
                results = await self.clear(member_id, candidates, source=reason)
            for result in results:
                st = result["status"]
                if st == "preview":
                    counts["previewed"] += 1
                elif st == "cleared":
                    counts["cleared"] += 1
                elif st == "conflict":
                    counts["conflicts"] += 1
                elif st == "failed":
                    counts["failed"] += 1
                else:
                    counts["skipped"] += 1
                if len(items) < RUN_ITEMS_LIMIT:
                    items.append(result)
        if counts["failed"] and status == "ok":
            status = "partial"
        return await self._save_run(
            {
                "status": status,
                "reason": reason,
                "dry_run": dry_run,
                "started_at": started,
                "finished_at": _now_iso(self.clock),
                "counts": counts,
                "items": items,
                "notes": notes,
            }
        )

    async def _save_run(self, run: dict) -> dict:
        await self.store.set(LAST_RUN_KEY, run)
        return run

    async def _undo_guards(self) -> dict[Unit, list[str]]:
        guards: dict[Unit, list[str]] = {}
        for row in await self.history():
            if row.get("undo_status") == "applied" and row.get("undo_version"):
                unit = (row["member_id"], row["media_item_id"], row["season"], row["episode"])
                guards.setdefault(unit, []).append(row["undo_version"])
        return guards

    def start_sweep(self, *, reason: str, dry_run: bool) -> bool:
        """页面上的「立即预览 / 立即清理一次」：放到后台跑，页面轮询报告。正在跑时不重复开。"""
        if self._sweep is not None and not self._sweep.done():
            return False
        self._sweep_info = {"reason": reason, "dry_run": dry_run, "started_at": _now_iso(self.clock)}

        async def run() -> None:
            try:
                await self.sweep(reason=reason, dry_run=dry_run)
            except Exception as exc:  # noqa: BLE001
                self.logger.warning("playback-cleanup：扫描出错：%s", exc)
                await self._save_run(
                    {"status": "failed", "reason": reason, "dry_run": dry_run,
                     "started_at": self._sweep_info["started_at"], "finished_at": _now_iso(self.clock),
                     "counts": {}, "items": [], "notes": [f"扫描出错：{exc}"]}
                )

        self._sweep = asyncio.create_task(run())
        return True

    def running(self) -> dict | None:
        if self._sweep is not None and not self._sweep.done():
            return self._sweep_info
        return None

    # ------------------------------------------------------------------ 记录与撤销
    async def history(self) -> list[dict]:
        rows = await self.store.get(HISTORY_KEY, default=None)
        return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []

    async def history_rows(self) -> list[dict]:
        return [
            {**r, "undoable": self.undoable(r), "undo_blocked": self.undo_blocked(r)}
            for r in await self.history()
        ]

    @staticmethod
    def undo_blocked(row: dict) -> str | None:
        """不能安全撤销的原因（给人看）；能撤销 / 已撤销时为 None。

        两个外部编号都没有、记录里又没有令牌的旧记录（0.1.1 及以前写的）：宿主 #1203 起必须带
        令牌，而当时的令牌没存下来。不能再查 catalog 补——数字 id 可能已被别的条目复用，
        拿到的是新条目的令牌，会把 A 的进度写到 B 上。
        """
        if row.get("undo_status"):
            return None
        if not has_external_ids(row) and not identity_token(row):
            return "旧记录没有保存条目身份令牌，无法安全撤销"
        return None

    @classmethod
    def undoable(cls, row: dict) -> bool:
        return (
            not row.get("undo_status")
            and bool(row.get("state_version"))
            and int(row.get("prior_position_ms") or 0) > 0
            and cls.undo_blocked(row) is None
        )

    async def undo(self, entry_id: str) -> dict:
        """把清掉的续播进度写回去（宿主自那次清理后没再变过才行，否则冲突）。"""
        async with self._write:
            rows = await self.history()
            row = next((r for r in rows if r.get("id") == entry_id), None)
            if row is None:
                raise SkipItem("找不到这条清理记录")
            blocked = self.undo_blocked(row)
            if blocked:
                raise SkipItem(blocked)
            if not self.undoable(row):
                raise SkipItem("这条记录已经撤销过，或不能撤销")
            # 只回放记录里的身份：编号 + 当时用的令牌；不查 catalog 取「现在的」令牌
            fake = {
                "media": {
                    "id": row["media_item_id"],
                    "kind": row["kind"],
                    "tmdb_id": row.get("tmdb_id"),
                    "imdb_id": row.get("imdb_id"),
                    "identity": row.get("identity"),
                },
                "season": row["season"],
                "episode": row["episode"],
            }
            change = build_change(fake, int(row["prior_position_ms"]), row["state_version"])
            try:
                outcomes = await self._state(int(row["member_id"]), [change])
            except OpsError as exc:
                raise SkipItem(f"撤销失败：{exc.message}") from exc
            outcome = outcomes[0] if outcomes else {"status": "failed"}
            status = str(outcome.get("status") or "failed")
            if status == "failed":
                raise SkipItem("撤销失败：宿主没有返回结果")
            async with self._store_lock:
                rows = await self.history()
                for r in rows:
                    if r.get("id") == entry_id:
                        r["undo_status"] = status
                        r["undone_at"] = _now_iso(self.clock)
                        r["undo_version"] = aware_iso(outcome.get("state_version"))
                        row = r
                await self.store.set(HISTORY_KEY, rows)
        return {**row, "undoable": self.undoable(row), "undo_blocked": self.undo_blocked(row)}

    # ------------------------------------------------------------------ 报告
    async def report(self) -> dict[str, Any]:
        settings = await self.settings()
        recent = await self.store.get(RECENT_KEY, default=None)
        pending = await self.store.get(PENDING_KEY, default=None)
        history = await self.history()
        return {
            "state": settings.selection_state(),
            "settings": settings.model_dump(),
            "last_run": await self.store.get(LAST_RUN_KEY, default=None),
            "running": self.running(),
            "pending": len(pending) if isinstance(pending, dict) else 0,
            "recent": recent if isinstance(recent, list) else [],
            "history_total": len(history),
            "undoable": sum(1 for r in history if self.undoable(r)),
        }
