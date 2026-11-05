"""运行时：选库 → 逐条目匹配 → 改写 NFO → 刷新 → 复核；台账、撤销、待确认。"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import ipaddress
import json
import os
import time
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from movieclaw_sdk.jobs import JobContext
from movieclaw_sdk.ops import OpsError

from . import nfo
from .ai import PER_RUN, AiAdvisor, AiError
from .aliases import NameTable, NameTableCache, NameTableError
from .filetree import FiletreeCache, FiletreeError, image_url
from .junk import junk_reason
from .matching import Avatar, AvatarIndex, Match, fold
from .search import HOW_LABEL, SOURCE_LABEL, Blocked, Candidate, Searcher, item_num, normalize_num
from .settings import Settings, ThumbMode

RUN_JOB = "actor-avatar:run"
UNDO_JOB = "actor-avatar:undo"
SEARCH_JOB = "actor-avatar:search"
SWEEP_TASK = "actor-avatar.sweep"

LEDGER_SHARDS = "ledger:shards"
EVENTS_KEY = "events"
RUNS_KEY = "runs"
PENDING_KEY = "pending"
ALIASES_KEY = "aliases"
REJECTED_KEY = "rejected"
CURSOR_KEY = "sweep:cursor"
HOST_KEY = "host:display"
#: 插件页面勾选的目标库：``{"ids": [9], "at": …, "migrated_from": [...]}``
SELECTION_KEY = "libraries:selected"
#: 自动写入的「解锁」：设置关掉只预览后，还要在插件页面亲手点一次「写入」
ARMED_KEY = "write:armed"
#: 自动触发的运行（不是人点的）
AUTO_REASONS = ("arrived", "schedule")
#: 不会「解锁」自动写入的运行：自动触发的，加上确认 / 手动设置之后顺带重跑的
#: （点「确认」「保存别名」不等于同意改 NFO；没解锁时这些重跑只预览）
GATED_REASONS = (*AUTO_REASONS, "confirm", "manual-entry", "search")
#: 手动设置：``{演员名: {"kind": "alias", "target": gfriends 名} | {"kind": "upload", "file": …}}``
MANUAL_KEY = "manual"
#: 最近没配上的演员（给页面列、给 AI 推荐用）：``{演员名: {items, titles, junk, …}}``
UNMATCHED_KEY = "unmatched"
#: 搜索补头像：``{演员名: {"at", "status", "num", "found", "blocked"}}``（冷却用）
SEARCH_TRIED_KEY = "search:tried"
#: 页面上点「搜索头像」后的进度：``{演员名: {"state": queued|running|done|failed, …}}``
SEARCH_STATUS_KEY = "search:status"
_SEARCH_TRIED_LIMIT = 1500
_SEARCH_STATUS_LIMIT = 40
REMOTE_GONE_NOTE = "搜索来的头像图片已失效（不再是 200 图片），这次没写；可以再搜一次"
UPLOAD_NO_URL_NOTE = "上传的头像要用外部访问地址（设置 → 应用，或本插件设置）才能写进 NFO"
NOT_ARMED_NOTE = (
    "「只预览不写入」已关闭，但还没在插件页面亲手点过「写入」：自动运行先按预览处理。"
    "在插件页面点一次「写入」后，入库与定时任务才会自动写"
)
NO_SELECTION_NOTE = "还没选媒体库：到插件页面「设置」里勾选要补头像的库并保存，插件才会开始处理"
HOST_GAP_NOTE = (
    "宿主刷新后没显示 NFO 里的演员头像（MovieClaw 本身的问题，需要宿主修复）；"
    "已停在探路的第一部，没再改别的 NFO。宿主修好后再运行会自动接着写，并把之前写过的条目重新刷新"
)

_EVENTS_LIMIT = 150
_RUNS_LIMIT = 20
_PENDING_LIMIT = 300
_UNMATCHED_LIMIT = 400
_MANUAL_LIMIT = 1000
#: 单个存储值的预算（宿主上限 64 KB，留余量）
_STORE_BUDGET = 48 * 1024
_SHARD = 4000
_PAGE = 50
_REFRESH_WAIT_S = 120
_TERMINAL = {"succeeded", "failed", "cancelled", "blocked", "partial"}
_DISC = {"iso", "bdmv", "video_ts", "ifo"}
_MAX_IMAGE = 5 * 1024 * 1024
_MAGIC = ((b"\xff\xd8\xff", "jpg"), (b"\x89PNG\r\n\x1a\n", "png"), (b"RIFF", "webp"))


class SkipItem(Exception):
    """条目不适合处理（不是失败）。"""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def manual_entry(manual: dict[str, dict], name: str) -> dict | None:
    """手动设置按原名查，查不到再按折叠键（全半角 / 大小写不同也算同一个人）。"""
    raw = (name or "").strip()
    entry = manual.get(raw)
    if entry is None:
        key = fold(raw)
        entry = next((v for k, v in manual.items() if fold(k) == key), None) if key else None
    return entry if isinstance(entry, dict) else None


def _remote_entry(row: dict[str, Any], *, auto: bool = False) -> dict[str, Any]:
    """待确认里的搜索候选 → 手动设置条目（kind=remote，NFO 直接写来源图片地址）。"""
    entry = {
        "kind": "remote",
        "url": row.get("url"),
        "source": row.get("source"),
        "source_label": row.get("source_label"),
        "page": row.get("page"),
        "num": row.get("num"),
        "how": row.get("how"),
        "how_label": row.get("how_label"),
        "target": row.get("actor_name"),
    }
    if auto:
        entry["auto"] = True
    return entry


def image_ext(body: bytes) -> str | None:
    if body.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if body.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if body.startswith(b"RIFF") and body[8:12] == b"WEBP":
        return "webp"
    return None


def _fits(value: Any) -> bool:
    return len(json.dumps(value, ensure_ascii=False).encode("utf-8")) <= _STORE_BUDGET


def _fit_list(values: list) -> list:
    """从最旧的开始丢，直到能放进一个存储值。"""
    values = list(values)
    while len(values) > 1 and not _fits(values):
        values = values[max(1, len(values) // 10) :]
    return values


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _job_status(data: Any) -> str:
    job = (data or {}).get("job") if isinstance(data, dict) else None
    job = job or data or {}
    return str(job.get("status") or "") if isinstance(job, dict) else ""


def thumb_url_warning(base_url: str) -> str | None:
    """托管模式的外部地址能不能被主程序的图片代理接受（公网域名 + 标准端口）。"""
    if not base_url:
        return "没有外部访问地址：托管模式无法生成头像链接"
    parts = urlsplit(base_url)
    host = parts.hostname or ""
    try:
        ipaddress.ip_address(host)
        return "外部访问地址是 IP：网页端图片代理会拒绝 IP 直连，头像显示不出来"
    except ValueError:
        pass
    if parts.port and parts.port not in (80, 443):
        return "外部访问地址带非标准端口：网页端图片代理只接受 80/443"
    return None


class Runtime:
    def __init__(
        self,
        *,
        settings: Settings,
        ops: Any,
        store: Any,
        files: Any,
        jobs: Any,
        filetree: FiletreeCache,
        http: Any,
        names: NameTableCache | None = None,
        logger: Any,
        sign: Any = None,
        base_url: str = "",
        sleep: Any = asyncio.sleep,
        ai: AiAdvisor | None = None,
        searcher: Searcher | None = None,
    ) -> None:
        self.settings = settings
        self.ai = ai
        self.searcher = searcher
        self.ops = ops
        self.store = store
        self.files = files
        self.jobs = jobs
        self.filetree = filetree
        self.names = names
        self.http = http
        self.logger = logger
        self.sign = sign
        self.base_url = (base_url or "").rstrip("/")
        self.sleep = sleep
        self._last_request = 0.0

    # ------------------------------------------------------------------ 排任务
    async def enqueue_run(
        self,
        *,
        reason: str,
        library_id: int | None = None,
        media_item_ids: list[int] | None = None,
        dry_run: bool | None = None,
    ) -> Any:
        ids = sorted({int(i) for i in media_item_ids or []})
        scope = "items:" + ",".join(map(str, ids[:20])) if ids else f"lib:{library_id or 'all'}"
        return await self.jobs.create(
            RUN_JOB,
            input={
                "reason": reason,
                "library_id": library_id,
                "media_item_ids": ids,
                "dry_run": dry_run,
            },
            priority=-20,
            dedupe_key=f"run:{scope}",
        )

    async def enqueue_undo(self, media_item_ids: list[int] | None) -> Any:
        ids = sorted({int(i) for i in media_item_ids or []})
        return await self.jobs.create(
            UNDO_JOB,
            input={"media_item_ids": ids, "all": not ids},
            priority=-10,
            dedupe_key="undo:" + (",".join(map(str, ids[:20])) or "all"),
        )

    # ------------------------------------------------------------------ 选库
    @staticmethod
    def eligible(row: dict) -> bool:
        return row.get("kind") == "video" and row.get("source") == "local"

    async def _library_rows(self) -> list[dict]:
        try:
            rows = await self.ops.call("library.list", {})
        except OpsError as exc:
            raise SkipItem(f"读取媒体库失败：{exc.message}") from exc
        return [r for r in (rows or []) if isinstance(r, dict) and r.get("id") is not None]

    async def selection(self, rows: list[dict] | None = None) -> tuple[list[int], str, list[str]]:
        """目标库 id、来源（panel / legacy / none）与提示。

        插件页面勾选过就用勾选的；否则读 0.1.x 设置里的库名 / 数字 id，解析成 id 后存成勾选
        结果（只迁移一次）；都没有时不选任何库（来源 none），要在插件页面勾选。
        """
        stored = await self.store.get(SELECTION_KEY, default=None)
        if isinstance(stored, dict) and isinstance(stored.get("ids"), list):
            return [int(i) for i in stored["ids"]], "panel", []
        legacy = self.settings.legacy_library_refs()
        if not legacy:
            return [], "none", []
        rows = await self._library_rows() if rows is None else rows
        ids: list[int] = []
        notes: list[str] = []
        for ref in legacy:
            hit = next(
                (r for r in rows if str(r.get("name")) == ref or str(r.get("id")) == ref), None
            )
            if hit is None:
                notes.append(f"找不到媒体库「{ref}」")
            elif int(hit["id"]) not in ids:
                ids.append(int(hit["id"]))
        if legacy and ids:
            await self.store.set(
                SELECTION_KEY, {"ids": ids, "at": _now(), "migrated_from": legacy[:20]}
            )
            return ids, "panel", notes
        return ids, "legacy", notes

    async def has_selection(self) -> bool:
        """勾选过（哪怕一个都没勾）或有 0.1.x 旧设置；都没有时插件什么都不做。"""
        stored = await self.store.get(SELECTION_KEY, default=None)
        if isinstance(stored, dict) and isinstance(stored.get("ids"), list):
            return True
        return bool(self.settings.legacy_library_refs())

    async def library_choices(self) -> dict[str, Any]:
        """插件页面的库选择器：服务器上全部库，标出能不能选、选没选。"""
        rows = await self._library_rows()
        ids, source, notes = await self.selection(rows)
        chosen = set(ids)
        items = []
        for row in rows:
            ok = self.eligible(row)
            items.append(
                {
                    "id": int(row["id"]),
                    "name": str(row.get("name") or ""),
                    "kind": row.get("kind"),
                    "source": row.get("source"),
                    "eligible": ok,
                    "reason": None if ok else "只支持「其他视频 + 本地目录」的库",
                    "selected": int(row["id"]) in chosen,
                }
            )
        known = {i["id"] for i in items}
        missing = [i for i in ids if i not in known]
        if missing:
            notes.append("已选的库不存在了：" + "、".join(map(str, missing)))
        return {"items": items, "selected": ids, "source": source, "notes": notes}

    async def save_selection(self, library_ids: list[int]) -> dict[str, Any]:
        rows = {int(r["id"]): r for r in await self._library_rows()}
        ids: list[int] = []
        for raw in library_ids:
            lid = int(raw)
            row = rows.get(lid)
            if row is None:
                raise SkipItem(f"媒体库 {lid} 不存在")
            if not self.eligible(row):
                raise SkipItem(f"「{row.get('name')}」不是本地视频库，不能选")
            if lid not in ids:
                ids.append(lid)
        await self.store.set(SELECTION_KEY, {"ids": ids, "at": _now()})
        return await self.library_choices()

    async def target_libraries(self) -> tuple[list[dict], list[str]]:
        """要处理的库：勾选的、仍存在且是本地视频库的。返回 (库列表, 提示)。"""
        rows = await self._library_rows()
        ids, _source, notes = await self.selection(rows)
        by_id = {int(r["id"]): r for r in rows}
        chosen: list[dict] = []
        for lid in ids:
            hit = by_id.get(lid)
            if hit is None:
                notes.append(f"找不到媒体库 {lid}")
                continue
            if not self.eligible(hit):
                notes.append(
                    f"「{hit.get('name')}」不是本地视频库（{hit.get('kind')}/{hit.get('source')}），已跳过"
                )
                continue
            chosen.append(hit)
        return chosen, notes

    # ------------------------------------------------------------------ 写入解锁
    async def armed(self) -> bool:
        value = await self.store.get(ARMED_KEY, default=None)
        return isinstance(value, dict) and bool(value.get("at"))

    async def arm(self) -> None:
        if not await self.armed():
            await self.store.set(ARMED_KEY, {"at": _now()})

    async def disarm(self) -> None:
        await self.store.delete(ARMED_KEY)

    async def name_table(self) -> tuple[NameTable | None, str | None]:
        if self.names is None or not self.settings.name_table:
            return None, None
        try:
            table = await self.names.table(
                self.settings.name_table_url(), self.settings.filetree_ttl_hours
            )
        except NameTableError as exc:
            return None, f"中文译名表不可用，本轮只按原名匹配：{exc}"
        return table, None

    # ------------------------------------------------------------------ 一轮
    async def run(self, context: JobContext | None, data: dict[str, Any]) -> dict[str, Any]:
        started = time.time()
        dry_run = self.settings.dry_run if data.get("dry_run") is None else bool(data["dry_run"])
        reason = data.get("reason") or "manual"
        if not await self.has_selection():
            # 没选库：不读条目、不写任何东西（连运行记录也不记），页面靠报告里的 library_source 提示
            self.logger.info("actor-avatar：还没选媒体库，跳过本轮（%s）", reason)
            return {
                "at": _now(),
                "reason": reason,
                "dry_run": dry_run,
                "libraries": [],
                "notes": [NO_SELECTION_NOTE],
                "status": "no_selection",
            }
        gated = False
        if not dry_run:
            if reason in GATED_REASONS:
                # 自动运行要先「解锁」：关掉只预览不等于同意无人值守地改 NFO
                if not await self.armed():
                    dry_run, gated = True, True
            else:
                await self.arm()
        libraries, notes = await self.target_libraries()
        if gated:
            notes.append(NOT_ARMED_NOTE)
        if data.get("library_id") is not None:
            libraries = [lib for lib in libraries if int(lib["id"]) == int(data["library_id"])]
            if not libraries:
                notes.append(f"媒体库 {data['library_id']} 不在目标库里")
        summary: dict[str, Any] = {
            "at": _now(),
            "reason": reason,
            "dry_run": dry_run,
            "libraries": [lib.get("name") for lib in libraries],
            "notes": notes,
            "seen": 0,
            "written": 0,
            "avatars": 0,
            "previewed": 0,
            "noop": 0,
            "skipped": 0,
            "failed": 0,
            "pending": 0,
        }
        if not libraries:
            summary["status"] = "no_library"
            await self._push_run(summary)
            return summary
        base = self.settings.source_base()
        try:
            index = await self.filetree.index(base, self.settings.filetree_ttl_hours)
        except FiletreeError as exc:
            summary["status"] = "failed"
            summary["notes"].append(str(exc))
            await self._push_run(summary)
            return summary
        aliases = await self._dict(ALIASES_KEY)
        manual = await self._dict(MANUAL_KEY)
        rejected = set(await self._list(REJECTED_KEY))
        table, table_note = await self.name_table()
        if table_note:
            summary["notes"].append(table_note)
        refreshes: list[tuple[int, int, str, dict[str, str]]] = []
        host = await self._dict(HOST_KEY)
        if not dry_run and host.get("state") == "hidden" and summary["reason"] == "arrived":
            # 已知宿主不显示 NFO 头像：入库触发的小批量不再写，等每日任务 / 手动运行再探一次
            summary["status"] = "host_gap"
            summary["notes"].append(HOST_GAP_NOTE)
            await self._push_run(summary)
            return summary
        # 写入模式下先拿第一条写入的条目探路：宿主不显示就停手，不再改别的 NFO
        probe = not dry_run and host.get("state") != "shown"
        stopped = False

        cursor_before = await self._dict(CURSOR_KEY)
        seen_names: list[tuple[int, int, dict[str, Any]]] = []
        for library_id, item_id in await self._targets(libraries, data):
            if context is not None:
                await context.raise_if_cancelled()
            summary["seen"] += 1
            try:
                result = await self.process_item(
                    library_id,
                    item_id,
                    index,
                    aliases=aliases,
                    rejected=rejected,
                    dry_run=dry_run,
                    table=table,
                    manual=manual,
                )
            except SkipItem as exc:
                summary["skipped"] += 1
                await self._event(item_id, library_id, "skipped", str(exc))
                continue
            except (nfo.NfoError, OpsError, OSError, ValueError) as exc:
                summary["failed"] += 1
                message = getattr(exc, "message", None) or str(exc)
                await self._event(item_id, library_id, "failed", message[:300])
                continue
            summary["pending"] += result.get("pending", 0)
            seen_names.append((library_id, item_id, result))
            for note in result.get("notes") or ():
                if note not in summary["notes"]:
                    summary["notes"].append(note)
            status = result["status"]
            if status == "written":
                summary["written"] += 1
                summary["avatars"] += len(result["written"])
                if result.get("refresh_job_id"):
                    entry = (library_id, item_id, result["refresh_job_id"], result["written"])
                    if probe:
                        probe = False
                        shown, hidden = await self._verify([entry])
                        if hidden and not shown:
                            await self._set_host("hidden", shown, hidden)
                            summary["shown"], summary["not_shown"] = shown, hidden
                            summary["notes"].append(HOST_GAP_NOTE)
                            stopped = True
                            break
                        await self._set_host("shown", shown, hidden)
                        summary["redisplayed"] = await self._redisplay_hidden()
                        summary["shown"] = shown
                        summary["not_shown"] = hidden
                    else:
                        refreshes.append(entry)
            elif status == "preview":
                summary["previewed"] += 1
                summary["avatars"] += len(result["wanted"])
            else:
                summary["noop"] += 1
            if context is not None and summary["seen"] % 10 == 0:
                with contextlib.suppress(Exception):
                    await context.update_progress(
                        mode="indeterminate", phase="run", message=f"已看 {summary['seen']} 部"
                    )

        if refreshes:
            shown, hidden = await self._verify(refreshes)
            summary["shown"] = summary.get("shown", 0) + shown
            summary["not_shown"] = summary.get("not_shown", 0) + hidden
            if shown or hidden:
                await self._set_host("shown" if shown else "hidden", shown, hidden)
        if stopped and not data.get("media_item_ids"):
            await self.store.set(CURSOR_KEY, cursor_before)  # 没处理的条目下次接着来
        summary["junk"] = await self._note_unmatched(seen_names)
        if self.settings.ai_suggest_on_run and self.ai is not None and self.ai.enabled:
            summary["ai"] = await self._ai_after_run(index, table, rejected)
        # 搜索补头像只挂在定时任务 / 新片入库的那一轮后面（确认、预览、手动设置后的重跑都不搜）
        if reason in AUTO_REASONS and self.settings.search_enabled and self.searcher is not None:
            summary["search"] = await self._search_after_run(
                index, table, aliases, rejected, can_write=not dry_run
            )
        summary["status"] = "host_gap" if stopped else "ok"
        summary["seconds"] = round(time.time() - started, 1)
        await self._push_run(summary)
        return summary

    async def _targets(self, libraries: list[dict], data: dict[str, Any]) -> list[tuple[int, int]]:
        ids = [int(i) for i in data.get("media_item_ids") or []]
        allowed = {int(lib["id"]) for lib in libraries}
        if ids:
            lib = data.get("library_id")
            if lib is not None:
                return [(int(lib), i) for i in ids if int(lib) in allowed]
            # 入库事件之外的调用只给了条目 id：逐个库试（库少，成本可忽略）
            return (
                [(int(libraries[0]["id"]), i) for i in ids]
                if len(libraries) == 1
                else [(int(lib["id"]), i) for lib in libraries for i in ids]
            )
        limit = self.settings.max_items_per_run
        cursor = await self._dict(CURSOR_KEY)
        lib_index = int(cursor.get("library_index") or 0)
        offset = int(cursor.get("offset") or 0)
        if lib_index >= len(libraries):
            lib_index, offset = 0, 0
        picked: list[tuple[int, int]] = []
        while lib_index < len(libraries) and len(picked) < limit:
            library_id = int(libraries[lib_index]["id"])
            rows = await self.ops.call(
                "library.items.list",
                {"library_id": library_id, "limit": _PAGE, "offset": offset},
            )
            rows = [r for r in (rows or []) if isinstance(r, dict)]
            for row in rows:
                if len(picked) >= limit:
                    break
                offset += 1
                if (
                    row.get("kind") == "video"
                    and row.get("source") == "local"
                    and row.get("media_item_id")
                ):
                    picked.append((library_id, int(row["media_item_id"])))
            if (len(rows) < _PAGE and len(picked) < limit) or not rows:
                lib_index += 1
                offset = 0
        done = lib_index >= len(libraries)
        await self.store.set(
            CURSOR_KEY, {"library_index": 0 if done else lib_index, "offset": 0 if done else offset}
        )
        return picked

    # ------------------------------------------------------------------ 单条目
    async def process_item(
        self,
        library_id: int,
        item_id: int,
        index: AvatarIndex,
        *,
        aliases: dict[str, str],
        rejected: set[str],
        dry_run: bool,
        table: NameTable | None = None,
        manual: dict[str, dict] | None = None,
    ) -> dict[str, Any]:
        manual = manual or {}
        try:
            item = await self.ops.call(
                "library.items.get", {"library_id": library_id, "media_item_id": item_id}
            )
        except OpsError as exc:
            if exc.status == 404:
                raise SkipItem("条目不存在或不在这个库里") from exc
            raise
        if not isinstance(item, dict):
            raise SkipItem("条目详情为空")
        if item.get("kind") != "video" or item.get("source") != "local":
            raise SkipItem("不是本地视频条目")
        title = str(item.get("title") or "")
        nfo_path = self._nfo_path(item)
        ledger = await self._ledger(item_id)
        owned: dict[str, str] = dict(ledger.get("owned") or {})

        text = await self._read_text(nfo_path) if await self.files.exists(nfo_path) else None
        nfo_thumbs = nfo.actor_names(text)
        db_actors = [
            {
                "name": str(a.get("name") or "").strip(),
                "role": a.get("role"),
                "thumb": a.get("thumb_url"),
            }
            for a in ((item.get("local_meta") or {}).get("actors") or [])
            if isinstance(a, dict) and str(a.get("name") or "").strip()
        ]
        names = list(nfo_thumbs) if nfo_thumbs else [a["name"] for a in db_actors]
        num = item_num(
            text,
            title,
            str(item.get("original_title") or ""),
            *(os.path.basename(str(f.get("file_path") or "")) for f in item.get("files") or []),
        )
        db_thumb = {a["name"]: a.get("thumb") for a in db_actors}
        if not names:
            await self._event(item_id, library_id, "noop", "没有演员", title=title)
            return {"status": "noop", "title": title}

        wanted: dict[str, str] = {}
        matches: dict[str, Match] = {}
        has_avatar: list[str] = []
        unmatched: list[str] = []
        junk: dict[str, str] = {}
        uploads: dict[str, str] = {}
        remotes: dict[str, dict] = {}
        notes: list[str] = []
        for name in names:
            current = nfo_thumbs.get(name) or db_thumb.get(name)
            if current and current != owned.get(name):
                has_avatar.append(name)
                continue
            entry = manual_entry(manual, name)
            # 手动设置优先于一切自动匹配
            if entry and entry.get("kind") == "upload" and entry.get("file"):
                uploads[name] = str(entry["file"])
                continue
            if entry and entry.get("kind") == "remote" and entry.get("url"):
                remotes[name] = entry
                continue
            if entry and entry.get("kind") == "alias":
                hit = index.by_name(str(entry.get("target") or ""))
                if hit is not None:
                    matches[name] = Match(hit, "manual", True, name)
                    continue
            if self.settings.skip_junk_names:
                why = junk_reason(name)
                if why:
                    junk[name] = why
                    continue
            match = index.lookup(name, aliases, table)
            if match is None:
                unmatched.append(name)
                continue
            matches[name] = match
        names_info = {
            "title": title,
            "unmatched": unmatched,
            "junk": junk,
            "matched": [*matches, *uploads, *remotes],
            "num": num,
        }
        if uploads and not dry_run and not self._can_link():
            notes.append(UPLOAD_NO_URL_NOTE)
            for name in uploads:
                unmatched.append(name)
            uploads = {}

        if remotes and not dry_run:
            # 搜索来的来源图片：写前再确认一次还是 200 的真图（已经写进去的同一地址不再请求）
            for name, entry in list(remotes.items()):
                url = str(entry["url"])
                if nfo_thumbs.get(name) == url:
                    continue
                ok = False
                if self.searcher is not None:
                    try:
                        ok = await self.searcher.image_ok(url)
                    except Blocked:
                        ok = False
                if not ok:
                    remotes.pop(name)
                    unmatched.append(name)
                    if REMOTE_GONE_NOTE not in notes:
                        notes.append(REMOTE_GONE_NOTE)

        pending_added = 0
        if self.settings.infer_from_title and len(unmatched) == 1 and unmatched[0] not in rejected:
            taken = {fold(m.avatar.name) for m in matches.values()} | {fold(n) for n in names}
            candidates = index.infer_from_title(title, exclude=taken)
            if candidates:
                pending_added = await self._add_pending(unmatched[0], candidates, item_id, title)

        if not matches and not uploads and not remotes:
            reason = "已有头像" if has_avatar and not unmatched else "头像库里找不到"
            await self._event(
                item_id,
                library_id,
                "noop",
                reason,
                title=title,
                unmatched=unmatched,
                has_avatar=has_avatar,
                junk=junk,
            )
            return {"status": "noop", "pending": pending_added, "notes": notes, **names_info}

        if dry_run:
            preview = {
                n: f"{m.avatar.company}/{m.avatar.pick(self.settings.ai_fix)}"
                for n, m in matches.items()
            }
            preview.update({n: f"上传/{f}" for n, f in uploads.items()})
            preview.update(
                {
                    n: f"{SOURCE_LABEL.get(e.get('source'), '搜索')}/{e['url']}"
                    for n, e in remotes.items()
                }
            )
            if uploads and not self._can_link():
                notes.append(UPLOAD_NO_URL_NOTE)
            await self._event(
                item_id,
                library_id,
                "preview",
                "",
                title=title,
                matched={
                    **{n: f"{m.how}:{m.avatar.name}" for n, m in matches.items()},
                    **{n: "upload" for n in uploads},
                    **{n: f"search:{e.get('source')}" for n, e in remotes.items()},
                },
                files=preview,
                unmatched=unmatched,
                has_avatar=has_avatar,
                junk=junk,
                nfo="将新建" if text is None else "将改写",
            )
            return {
                "status": "preview",
                "wanted": preview,
                "pending": pending_added,
                "notes": notes,
                **names_info,
            }

        for name, match in matches.items():
            wanted[name] = await self._thumb_for(match)
        for name, file in uploads.items():
            wanted[name] = await self._link(file)
        for name, entry in remotes.items():
            wanted[name] = str(entry["url"])  # 来源图片地址直接写，和镜像地址写法一样

        edit = nfo.apply_thumbs(
            text,
            wanted,
            owned=owned,
            db_actors=[{"name": a["name"], "role": a.get("role")} for a in db_actors],
            title=title or None,
        )
        if not edit.changed:
            await self._event(item_id, library_id, "noop", "NFO 已是最新", title=title)
            return {"status": "noop", "pending": pending_added, "notes": notes, **names_info}

        backup = None
        if text is not None:
            backup = await self._backup(item_id, text)
        await self._atomic_write(nfo_path, edit.text)
        owned.update(edit.written)
        created = bool(ledger.get("created")) or edit.created
        base_sha = ledger.get("base_sha")
        if created:
            # 记下「去掉本插件头像后」的指纹：撤销时只有文件仍是这个样子（用户没动过）才删
            base_sha = _sha(nfo.strip_thumbs(edit.text, owned)[0])
        ledger.update(
            {
                "media_item_id": item_id,
                "library_id": library_id,
                "title": title,
                "nfo_path": nfo_path,
                # 只要第一次是我们新建的，就一直记为新建（撤销时整份删掉）
                "created": created,
                "base_sha": base_sha,
                "backup": ledger.get("backup") or backup,
                "owned": owned,
                "updated_at": _now(),
                "status": "written",
            }
        )
        await self._save_ledger(item_id, ledger)
        job_id = await self._refresh(library_id, item_id)
        await self._event(
            item_id,
            library_id,
            "written",
            "",
            title=title,
            written=list(edit.written),
            kept=edit.kept,
            unmatched=unmatched,
            has_avatar=has_avatar,
            junk=junk,
            nfo="新建" if edit.created else "改写",
        )
        return {
            "status": "written",
            "written": edit.written,
            "refresh_job_id": job_id,
            "pending": pending_added,
            "notes": notes,
            **names_info,
        }

    def _nfo_path(self, item: dict[str, Any]) -> str:
        for row in item.get("files") or []:
            if not isinstance(row, dict) or row.get("missing"):
                continue
            if row.get("state") not in (None, "in_place"):
                continue
            path = str(row.get("file_path") or "")
            if not path or (row.get("container") or "").lower() in _DISC:
                continue
            return os.path.splitext(path)[0] + ".nfo"
        raise SkipItem("没有在位的视频文件")

    async def _thumb_for(self, match: Match) -> str:
        file = match.avatar.pick(self.settings.ai_fix)
        url = image_url(self.settings.source_base(), match.avatar.company, file)
        if self.settings.thumb_mode != ThumbMode.HOSTED:
            return url
        name = await self._download(url, f"{match.avatar.company}/{file}")
        return await self._link(name)

    def _can_link(self) -> bool:
        return self.sign is not None and bool(self.base_url)

    async def _link(self, name: str) -> str:
        """插件托管的头像（下载的或用户上传的）→ 写进 NFO 的长期签名地址。"""
        if self.sign is None:
            raise SkipItem("托管头像需要签名链接服务")
        path = await self.sign(f"/avatar/{name}")
        if path.startswith(("http://", "https://")):
            return path
        if not self.base_url:
            raise SkipItem("托管头像需要外部访问地址（设置 → 应用，或本插件设置）")
        return self.base_url + path

    async def _download(self, url: str, key: str) -> str:
        digest = hashlib.sha1(key.encode("utf-8")).hexdigest()
        for _magic, ext in _MAGIC:
            existing = self.files.path("plugin", "avatars", f"{digest}.{ext}")
            if await self.files.exists(existing):
                return f"{digest}.{ext}"
        await self._pace()
        resp = await self.http.get(url)
        if resp.status_code != 200:
            raise SkipItem(f"下载头像失败：HTTP {resp.status_code}")
        body = resp.content
        if not body or len(body) > _MAX_IMAGE:
            raise SkipItem("头像文件大小异常")
        ext = next((e for magic, e in _MAGIC if body.startswith(magic)), None)
        if ext is None:
            raise SkipItem("下载到的不是图片")
        name = f"{digest}.{ext}"
        directory = self.files.path("plugin", "avatars")
        await self.files.makedirs(directory)
        await self._write_bytes(os.path.join(directory, name), body, atomic=True)
        return name

    async def _pace(self) -> None:
        gap = self.settings.request_interval_ms / 1000
        wait = self._last_request + gap - time.monotonic()
        if wait > 0:
            await self.sleep(wait)
        self._last_request = time.monotonic()

    async def _refresh(self, library_id: int, item_id: int) -> str | None:
        await self._pace()
        try:
            data = await self.ops.call(
                "library.items.refresh-metadata",
                {"library_id": library_id, "media_item_id": item_id},
            )
        except OpsError as exc:
            self.logger.warning("actor-avatar：触发刷新失败 %s：%s", item_id, exc.message)
            return None
        job_id = (data or {}).get("job_id") if isinstance(data, dict) else None
        return str(job_id) if job_id else None

    async def _wait_jobs(self, job_ids: list[str], timeout: float) -> None:
        pending = [j for j in job_ids if j]
        deadline = time.monotonic() + timeout
        while pending and time.monotonic() < deadline:
            still = []
            for jid in pending:
                try:
                    status = _job_status(await self.ops.call("jobs.show", {"job_id": jid}))
                except OpsError:
                    continue
                if status not in _TERMINAL:
                    still.append(jid)
            pending = still
            if pending:
                await self.sleep(3)

    async def _set_host(self, state: str, shown: int, hidden: int) -> None:
        await self.store.set(
            HOST_KEY,
            {"state": state, "checked_at": _now(), "shown": shown, "not_shown": hidden},
        )

    async def _redisplay_hidden(self) -> int:
        """宿主已能显示：把以前写了但没显示出来的条目再刷新一次（每轮最多一批）。"""
        count = 0
        for item_id in await self._ledger_ids():
            if count >= self.settings.max_items_per_run:
                break
            entry = await self._ledger(item_id)
            if entry.get("shown") is not False or not entry.get("owned"):
                continue
            job = await self._refresh(int(entry.get("library_id") or 0), item_id)
            if job:
                entry["shown"] = None
                await self._save_ledger(item_id, entry)
                count += 1
        return count

    async def _verify(
        self, refreshes: list[tuple[int, int, str, dict[str, str]]]
    ) -> tuple[int, int]:
        """刷新完成后看详情页演员是否真的带上了头像地址（结果记进台账的 shown）。"""
        await self._wait_jobs([r[2] for r in refreshes], _REFRESH_WAIT_S)
        shown = hidden = 0
        for library_id, item_id, _job, written in refreshes[:50]:
            try:
                item = await self.ops.call(
                    "library.items.get", {"library_id": library_id, "media_item_id": item_id}
                )
            except OpsError:
                continue
            actors = {
                str(a.get("name") or ""): a.get("thumb_url")
                for a in ((item or {}).get("local_meta") or {}).get("actors") or []
                if isinstance(a, dict)
            }
            ok = all(actors.get(name) == url for name, url in written.items())
            for name, url in written.items():
                if actors.get(name) == url:
                    shown += 1
                else:
                    hidden += 1
            entry = await self._ledger(item_id)
            if entry:
                entry["shown"] = ok
                await self._save_ledger(item_id, entry)
        return shown, hidden

    # ------------------------------------------------------------------ 撤销
    async def undo(self, context: JobContext | None, data: dict[str, Any]) -> dict[str, Any]:
        ids = [int(i) for i in data.get("media_item_ids") or []]
        if not ids and data.get("all"):
            ids = await self._ledger_ids()
        result = {"undone": 0, "deleted": 0, "skipped": 0, "items": []}
        to_delete: list[tuple[str, str, str | None]] = []
        for item_id in ids:
            if context is not None:
                await context.raise_if_cancelled()
            ledger = await self._ledger(item_id)
            owned = dict(ledger.get("owned") or {})
            path = ledger.get("nfo_path")
            if not path or not owned:
                result["skipped"] += 1
                continue
            if not await self.files.exists(path):
                ledger.update({"owned": {}, "status": "undone", "updated_at": _now()})
                await self._save_ledger(item_id, ledger)
                result["skipped"] += 1
                continue
            text = await self._read_text(path)
            stripped, removed = nfo.strip_thumbs(text, owned)
            if removed:
                await self._atomic_write(path, stripped)
            job_id = await self._refresh(int(ledger["library_id"]), item_id)
            if ledger.get("created") and ledger.get("base_sha") == _sha(stripped):
                to_delete.append((path, stripped, job_id))
            ledger.update({"owned": {}, "status": "undone", "updated_at": _now()})
            await self._save_ledger(item_id, ledger)
            await self._event(
                item_id,
                ledger.get("library_id"),
                "undone",
                "",
                title=ledger.get("title"),
                removed=removed,
            )
            result["undone"] += 1
            result["items"].append(item_id)
        if to_delete:
            # 新建的 NFO：先让主程序把去掉头像的版本吸收进档案，再删文件；
            # 直接删的话档案里会留着旧头像地址（刷新时找不到 NFO 就什么都不改）
            await self._wait_jobs([j for _p, _t, j in to_delete if j], _REFRESH_WAIT_S)
            for path, stripped, _job in to_delete:
                with contextlib.suppress(Exception):
                    if await self._read_text(path) == stripped:
                        await self.files.remove(path)
                        result["deleted"] += 1
        return result

    # ------------------------------------------------------------------ 待确认
    async def _add_pending(
        self,
        actor: str,
        candidates: list,
        item_id: int | None,
        title: str,
        *,
        via: str | None = None,
    ) -> int:
        pending = await self._dict(PENDING_KEY)
        entry = pending.get(actor) or {
            "actor": actor,
            "candidates": [],
            "items": [],
            "first_seen": _now(),
        }
        known = {c["name"] for c in entry["candidates"]}
        for cand in candidates[:3]:
            if cand.name not in known:
                row = {"name": cand.name, "company": cand.company, "file": cand.file}
                if via:
                    row["via"] = via
                entry["candidates"].append(row)
        if item_id is not None and item_id not in entry["items"]:
            entry["items"] = (entry["items"] + [item_id])[-10:]
            entry["title"] = title[:120]
        new = actor not in pending
        pending[actor] = entry
        oldest = sorted(pending, key=lambda k: pending[k].get("first_seen") or "")
        while oldest and (len(pending) > _PENDING_LIMIT or not _fits(pending)):
            pending.pop(oldest.pop(0), None)
        await self.store.set(PENDING_KEY, pending)
        return 1 if new else 0

    async def pending(self) -> list[dict]:
        pending = await self._dict(PENDING_KEY)
        base = self.settings.source_base()
        rows = []
        for entry in pending.values():
            row = dict(entry)
            row["candidates"] = [
                {
                    **c,
                    "preview": c.get("url")
                    if c.get("kind") == "remote"
                    else image_url(base, c.get("company") or "", c.get("file") or ""),
                }
                for c in entry.get("candidates") or []
            ]
            rows.append(row)
        return sorted(rows, key=lambda r: r.get("first_seen") or "", reverse=True)

    async def confirm(self, actor: str, candidate: str) -> dict[str, Any]:
        pending = await self._dict(PENDING_KEY)
        entry = pending.get(actor)
        row = next(
            (c for c in (entry or {}).get("candidates") or [] if c["name"] == candidate), None
        )
        if entry is None or row is None:
            raise SkipItem("待确认列表里没有这个候选")
        if row.get("kind") == "remote" and row.get("url"):
            # 搜索来的来源图片：记成手动设置（写入时再验证一次 200），台账照常、可撤销
            items = list(entry.get("items") or [])
            for item_id in await self._save_manual(actor, _remote_entry(row)):
                if item_id not in items:
                    items.append(item_id)
            return {"actor": actor, "candidate": candidate, "items": items}
        aliases = await self._dict(ALIASES_KEY)
        aliases[actor] = candidate
        await self.store.set(ALIASES_KEY, aliases)
        pending.pop(actor, None)
        await self.store.set(PENDING_KEY, pending)
        return {"actor": actor, "candidate": candidate, "items": entry.get("items") or []}

    async def reject(self, actor: str) -> dict[str, Any]:
        pending = await self._dict(PENDING_KEY)
        entry = pending.pop(actor, None)
        await self.store.set(PENDING_KEY, pending)
        rejected = await self._list(REJECTED_KEY)
        if actor not in rejected:
            rejected = (rejected + [actor])[-2000:]
            await self.store.set(REJECTED_KEY, rejected)
        return {"actor": actor, "removed": entry is not None}

    async def forget_alias(self, actor: str) -> dict[str, Any]:
        aliases = await self._dict(ALIASES_KEY)
        removed = aliases.pop(actor, None)
        await self.store.set(ALIASES_KEY, aliases)
        return {"actor": actor, "removed": removed is not None}

    # ------------------------------------------------------------------ 没配上的演员
    async def _note_unmatched(self, seen: list[tuple[int, int, dict[str, Any]]]) -> int:
        """一轮跑完后一次性更新「没配上的演员」表；返回这轮见到的刮削垃圾名个数。"""
        if not seen:
            return 0
        table = await self._dict(UNMATCHED_KEY)
        junk_names: set[str] = set()
        changed = False
        for library_id, item_id, result in seen:
            for name in result.get("matched") or ():
                changed |= table.pop(name, None) is not None
            title = str(result.get("title") or "")[:120]
            rows = [(n, None) for n in result.get("unmatched") or ()]
            rows += list((result.get("junk") or {}).items())
            for name, why in rows:
                if why:
                    junk_names.add(name)
                entry = table.get(name) or {"actor": name, "items": [], "titles": []}
                if item_id not in entry["items"]:
                    entry["items"] = (entry["items"] + [item_id])[-10:]
                    if title and title not in entry["titles"]:
                        entry["titles"] = (entry["titles"] + [title])[-3:]
                entry["library_id"] = library_id
                entry["junk"] = why
                num = result.get("num")
                if num:
                    nums = [n for n in entry.get("nums") or [] if n != num]
                    entry["nums"] = (nums + [num])[-3:]
                entry["last_seen"] = _now()
                table.pop(name, None)
                table[name] = entry  # 最近见到的排在后面
                changed = True
        while table and (len(table) > _UNMATCHED_LIMIT or not _fits(table)):
            table.pop(next(iter(table)))
        if changed:
            await self.store.set(UNMATCHED_KEY, table)
        return len(junk_names)

    async def _unmatched_counts(self) -> dict[str, int]:
        table = await self._dict(UNMATCHED_KEY)
        junk = sum(1 for e in table.values() if isinstance(e, dict) and e.get("junk"))
        return {"actors": len(table) - junk, "junk": junk}

    async def unmatched(self) -> dict[str, Any]:
        table = await self._dict(UNMATCHED_KEY)
        manual = await self._dict(MANUAL_KEY)
        pending = await self._dict(PENDING_KEY)
        tried = await self.store.get("ai:tried", default={}) or {}
        searched = await self._dict(SEARCH_TRIED_KEY)
        actors, junk = [], []
        for entry in reversed(list(table.values())):
            name = entry.get("actor") or ""
            row = {
                **entry,
                "count": len(entry.get("items") or []),
                "manual": manual_entry(manual, name),
                "pending": name in pending,
                "ai_tried": tried.get(name),
                "num": self.entry_num(entry),
                "search_tried": searched.get(name),
            }
            (junk if entry.get("junk") else actors).append(row)
        return {"actors": actors, "junk": junk}

    # ------------------------------------------------------------------ 手动设置
    async def _index(self) -> AvatarIndex:
        try:
            return await self.filetree.index(
                self.settings.source_base(), self.settings.filetree_ttl_hours
            )
        except FiletreeError as exc:
            raise SkipItem(str(exc)) from exc

    async def search(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        """自动补全：gfriends 名字 + 中文译名表（译名 → 日文名，日文名必须在 gfriends 里）。"""
        query = (query or "").strip()
        if not query:
            return []
        index = await self._index()
        base = self.settings.source_base()
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()

        def add(avatar: Avatar, via: str) -> None:
            if avatar.name in seen or len(rows) >= limit:
                return
            seen.add(avatar.name)
            rows.append(
                {
                    "name": avatar.name,
                    "company": avatar.company,
                    "via": via,
                    "preview": image_url(base, avatar.company, avatar.file),
                }
            )

        table, _ = await self.name_table()
        exact = index.lookup(query, None, table)
        if exact is not None:
            add(exact.avatar, exact.how)
        for avatar in index.search(query, limit):
            add(avatar, "gfriends")
        if table is not None:
            for alias, target in table.search(query, limit):
                hit = index.by_name(target)
                if hit is not None:
                    add(hit, f"译名表：{alias}")
        return rows

    async def manual_rows(self) -> list[dict[str, Any]]:
        manual = await self._dict(MANUAL_KEY)
        base = self.settings.source_base()
        rows = []
        index = None
        for actor, entry in manual.items():
            row = {"actor": actor, **entry}
            if entry.get("kind") == "alias":
                if index is None:
                    try:
                        index = await self._index()
                    except SkipItem:
                        index = AvatarIndex({})
                hit = index.by_name(str(entry.get("target") or ""))
                row["preview"] = image_url(base, hit.company, hit.file) if hit else None
                row["missing"] = hit is None
            elif entry.get("kind") == "remote":
                row["preview"] = entry.get("url")
            rows.append(row)
        return sorted(rows, key=lambda r: r.get("at") or "", reverse=True)

    async def _save_manual(self, actor: str, entry: dict[str, Any]) -> list[int]:
        actor = actor.strip()
        if not actor:
            raise SkipItem("演员名不能为空")
        manual = await self._dict(MANUAL_KEY)
        manual.pop(actor, None)
        manual[actor] = {**entry, "at": _now()}
        while manual and (len(manual) > _MANUAL_LIMIT or not _fits(manual)):
            manual.pop(next(iter(manual)))
        await self.store.set(MANUAL_KEY, manual)
        # 手动设置后，待确认 / 拒绝里的同名条目就没用了
        pending = await self._dict(PENDING_KEY)
        items: list[int] = list((pending.pop(actor, None) or {}).get("items") or [])
        await self.store.set(PENDING_KEY, pending)
        rejected = await self._list(REJECTED_KEY)
        if actor in rejected:
            await self.store.set(REJECTED_KEY, [r for r in rejected if r != actor])
        seen = (await self._dict(UNMATCHED_KEY)).get(actor) or {}
        for item_id in seen.get("items") or []:
            if item_id not in items:
                items.append(item_id)
        return items

    async def set_manual_alias(self, actor: str, target: str) -> dict[str, Any]:
        index = await self._index()
        hit = index.by_name(target)
        if hit is None:
            raise SkipItem(f"gfriends 里没有「{target}」：请从搜索结果里选")
        items = await self._save_manual(actor, {"kind": "alias", "target": hit.name})
        return {"actor": actor.strip(), "target": hit.name, "items": items}

    async def set_manual_upload(self, actor: str, body: bytes) -> dict[str, Any]:
        if not body or len(body) > _MAX_IMAGE:
            raise SkipItem("图片为空或超过 5 MB")
        ext = image_ext(body)
        if ext is None:
            raise SkipItem("只支持 JPG / PNG / WebP 图片")
        name = f"{hashlib.sha1(body).hexdigest()}.{ext}"
        directory = self.files.path("plugin", "avatars")
        await self.files.makedirs(directory)
        path = os.path.join(directory, name)
        if not await self.files.exists(path):
            await self._write_bytes(path, body, atomic=True)
        items = await self._save_manual(actor, {"kind": "upload", "file": name, "bytes": len(body)})
        return {
            "actor": actor.strip(),
            "file": name,
            "items": items,
            "linkable": self._can_link(),
            "note": None if self._can_link() else UPLOAD_NO_URL_NOTE,
        }

    async def remove_manual(self, actor: str) -> dict[str, Any]:
        """删掉手动设置（已经写进 NFO 的头像不动；要撤掉用「撤销」）。"""
        manual = await self._dict(MANUAL_KEY)
        entry = manual.pop(actor, None)
        await self.store.set(MANUAL_KEY, manual)
        return {"actor": actor, "removed": entry is not None}

    async def upload_path(self, name: str) -> str | None:
        path = self.files.path("plugin", "avatars", name)
        return path if await self.files.exists(path) else None

    # ------------------------------------------------------------------ AI 推荐
    async def ai_status(self) -> dict[str, Any]:
        if self.ai is None:
            return {
                "available": False,
                "reason": (
                    "没有拿到 AI 助手（需要 MovieClaw 支持插件调用 AI，并在安装时批准 agent:none）"
                ),
                "per_run": self.settings.ai_suggest_on_run,
            }
        return {**await self.ai.status(), "per_run": self.settings.ai_suggest_on_run}

    async def ai_suggest(self, actor: str) -> dict[str, Any]:
        """页面上点「AI 推荐别名」：问一次，查得到的候选放进「待确认」。"""
        if self.ai is None:
            raise AiError("没有拿到 AI 助手（需要在安装时批准 agent:none）")
        if not await self.has_selection():
            raise AiError(NO_SELECTION_NOTE)
        index = await self._index()
        table, _ = await self.name_table()
        seen = (await self._dict(UNMATCHED_KEY)).get(actor) or {}
        return await self._ai_one(actor, seen, index, table)

    async def _ai_one(
        self, actor: str, seen: dict[str, Any], index: AvatarIndex, table: Any
    ) -> dict[str, Any]:
        assert self.ai is not None
        titles = list(seen.get("titles") or [])
        result = await self.ai.suggest(actor, titles, index, table)
        accepted: list[Avatar] = result["accepted"]
        added = 0
        if accepted:
            items = list(seen.get("items") or [])
            added = await self._add_pending(
                actor,
                accepted,
                items[-1] if items else None,
                titles[-1] if titles else "",
                via="ai",
            )
            if len(items) > 1:
                pending = await self._dict(PENDING_KEY)
                if actor in pending:
                    merged = list(pending[actor].get("items") or [])
                    for item_id in items:
                        if item_id not in merged:
                            merged.append(item_id)
                    pending[actor]["items"] = merged[-10:]
                    await self.store.set(PENDING_KEY, pending)
        return {
            "actor": actor,
            "asked": result["asked"],
            "accepted": [a.name for a in accepted],
            "pending_added": added,
            "usage": result["usage"],
        }

    async def _ai_after_run(self, index: AvatarIndex, table: Any, rejected: set[str]) -> dict:
        """每轮运行后的自动推荐（设置里开了才跑）：最多 PER_RUN 位，跳过问过的 / 有手动设置的。"""
        assert self.ai is not None
        table_seen = await self._dict(UNMATCHED_KEY)
        manual = await self._dict(MANUAL_KEY)
        pending = await self._dict(PENDING_KEY)
        done: dict[str, Any] = {"asked": 0, "accepted": 0, "errors": []}
        for entry in reversed(list(table_seen.values())):
            if done["asked"] >= PER_RUN:
                break
            actor = entry.get("actor") or ""
            if (
                not actor
                or entry.get("junk")
                or actor in rejected
                or actor in pending
                or manual_entry(manual, actor)
                or await self.ai.recently_tried(actor)
            ):
                continue
            try:
                result = await self._ai_one(actor, entry, index, table)
            except AiError as exc:
                done["errors"].append(str(exc))
                break
            done["asked"] += 1
            done["accepted"] += len(result["accepted"])
        return done

    # ------------------------------------------------------------------ 搜索补头像
    @staticmethod
    def entry_num(entry: dict[str, Any]) -> str | None:
        """「没配上的演员」表里的番号：运行时记下的优先，旧条目从片名里提取。"""
        for num in reversed(entry.get("nums") or []):
            if num:
                return str(num)
        for title in reversed(entry.get("titles") or []):
            found = normalize_num(str(title))
            if found:
                return found
        return None

    async def _cooling(self, actor: str, tried: dict[str, Any]) -> bool:
        """自动轮次的冷却：上次没搜到（且不是被拦）就 search_cooldown_days 天内不再搜。"""
        last = tried.get(actor)
        if not isinstance(last, dict) or last.get("status") != "none":
            return False
        try:
            at = datetime.fromisoformat(str(last.get("at")))
        except ValueError:
            return False
        age = (datetime.now(UTC) - at).total_seconds()
        return age < self.settings.search_cooldown_days * 86400

    async def enqueue_search(self, actor: str) -> Any:
        """页面上点「搜索头像」：排一个后台任务（只搜这一位），页面轮询状态。"""
        actor = actor.strip()
        if not await self.has_selection():
            raise SkipItem(NO_SELECTION_NOTE)
        if not self.settings.search_enabled:
            raise SkipItem("搜索补头像已在设置里关闭")
        if self.searcher is None or not self.searcher.enabled_sources():
            raise SkipItem("没有开启任何搜索来源")
        entry = (await self._dict(UNMATCHED_KEY)).get(actor)
        if not isinstance(entry, dict):
            raise SkipItem("「没配上的演员」里没有这位")
        if not self.entry_num(entry):
            raise SkipItem("她的片子里找不到番号（标题 / 文件名 / NFO 都没有），没法搜索")
        await self._set_search_status(actor, {"state": "queued"})
        return await self.jobs.create(
            SEARCH_JOB,
            input={"actor": actor},
            priority=-15,
            dedupe_key=f"search:{actor[:80]}",
        )

    async def search_job(self, context: JobContext | None, data: dict[str, Any]) -> dict[str, Any]:
        """后台任务：手动搜一位演员（无视冷却；结果只进待确认）。"""
        actor = str(data.get("actor") or "").strip()
        await self._set_search_status(actor, {"state": "running"})
        try:
            if self.searcher is None:
                raise SkipItem("搜索不可用")
            entry = (await self._dict(UNMATCHED_KEY)).get(actor) or {}
            num = self.entry_num(entry)
            if not num:
                raise SkipItem("找不到番号")
            index = await self._index()
            table, _ = await self.name_table()
            aliases = await self._dict(ALIASES_KEY)
            result = await self._search_one(
                actor, entry, num, index, table, aliases, set(), auto=False, can_write=False
            )
        except SkipItem as exc:
            await self._set_search_status(actor, {"state": "failed", "error": str(exc)[:200]})
            return {"actor": actor, "status": "failed", "error": str(exc)[:200]}
        await self._set_search_status(actor, {"state": "done", **result})
        return result

    async def search_status(self, actor: str | None = None) -> dict[str, Any]:
        table = await self._dict(SEARCH_STATUS_KEY)
        if actor is not None:
            return {"actor": actor, **(table.get(actor) or {"state": "idle"})}
        return {"items": table}

    async def _set_search_status(self, actor: str, value: dict[str, Any]) -> None:
        if not actor:
            return
        table = await self._dict(SEARCH_STATUS_KEY)
        table.pop(actor, None)
        table[actor] = {**value, "at": _now()}
        while len(table) > _SEARCH_STATUS_LIMIT or not _fits(table):
            table.pop(next(iter(table)))
        await self.store.set(SEARCH_STATUS_KEY, table)

    async def _search_after_run(
        self,
        index: AvatarIndex,
        table: Any,
        aliases: dict[str, str],
        rejected: set[str],
        *,
        can_write: bool,
    ) -> dict[str, Any]:
        """一轮（定时 / 入库）跑完后：给「没配上的演员」按番号搜索，每轮最多 search_per_run 位。"""
        assert self.searcher is not None
        done: dict[str, Any] = {"searched": 0, "candidates": 0, "blocked": [], "auto_written": 0}
        sources = set(self.searcher.enabled_sources())
        if not sources:
            return done
        seen = await self._dict(UNMATCHED_KEY)
        manual = await self._dict(MANUAL_KEY)
        pending = await self._dict(PENDING_KEY)
        tried = await self._dict(SEARCH_TRIED_KEY)
        paused: set[str] = set()
        for entry in reversed(list(seen.values())):
            if done["searched"] >= self.settings.search_per_run or sources <= paused:
                break
            actor = str(entry.get("actor") or "")
            if (
                not actor
                or entry.get("junk")
                or actor in rejected
                or actor in pending
                or manual_entry(manual, actor)
                or await self._cooling(actor, tried)
            ):
                continue
            num = self.entry_num(entry)
            if not num:
                continue
            result = await self._search_one(
                actor, entry, num, index, table, aliases, paused, auto=True, can_write=can_write
            )
            done["searched"] += 1
            done["candidates"] += result["candidates"]
            done["auto_written"] += 1 if result.get("auto_written") else 0
            for label in result["blocked"]:
                if label not in done["blocked"]:
                    done["blocked"].append(label)
        return done

    async def _search_one(
        self,
        actor: str,
        entry: dict[str, Any],
        num: str,
        index: AvatarIndex,
        table: Any,
        aliases: dict[str, str],
        paused: set[str],
        *,
        auto: bool,
        can_write: bool,
    ) -> dict[str, Any]:
        assert self.searcher is not None
        before = set(paused)
        outcome = await self.searcher.search(
            actor, num, index=index, aliases=aliases, table=table, paused=paused
        )
        items = [int(i) for i in entry.get("items") or []]
        item_id = items[-1] if items else None
        library_id = entry.get("library_id")
        label = f"{actor}（{num}）"
        for source in sorted(paused - before):
            name = SOURCE_LABEL.get(source, source)
            await self._event(
                item_id,
                library_id,
                "search",
                self.searcher.scrub(f"{name} 拦截了请求，本轮暂停这个来源（不算这位演员没搜到）"),
                title=label,
            )
        result = outcome.summary()
        result["auto_written"] = False
        cands = outcome.candidates
        if cands:
            first = cands[0]
            if (
                auto
                and can_write
                and self.settings.search_auto_write
                and first.auto_ok
                and await self.armed()
            ):
                await self._save_manual(actor, _remote_entry(first.row(), auto=True))
                if items:
                    await self.enqueue_run(reason="search", media_item_ids=items)
                result["auto_written"] = True
            else:
                await self._add_search_pending(actor, cands, items, entry)
        how = "、".join(
            f"{SOURCE_LABEL.get(c.source, c.source)}·{HOW_LABEL.get(c.how, c.how)}" for c in cands
        )
        reason = (
            f"自动写入：{how}"
            if result["auto_written"]
            else f"找到 {len(cands)} 个候选，已放进待确认：{how}"
            if cands
            else "被拦，没搜完"
            if outcome.blocked
            else "没找到"
        )
        await self._event(
            item_id,
            library_id,
            "search",
            self.searcher.scrub(reason),
            title=label,
            tried=outcome.tried,
            blocked=outcome.blocked,
        )
        tried = await self._dict(SEARCH_TRIED_KEY)
        tried.pop(actor, None)
        tried[actor] = {
            "at": _now(),
            "status": outcome.status,
            "num": num,
            "found": len(cands),
            "blocked": outcome.blocked,
            "manual": not auto,
        }
        while len(tried) > _SEARCH_TRIED_LIMIT or not _fits(tried):
            tried.pop(next(iter(tried)))
        await self.store.set(SEARCH_TRIED_KEY, tried)
        return result

    async def _add_search_pending(
        self, actor: str, cands: list[Candidate], items: list[int], seen: dict[str, Any]
    ) -> None:
        pending = await self._dict(PENDING_KEY)
        titles = list(seen.get("titles") or [])
        entry = pending.get(actor) or {
            "actor": actor,
            "candidates": [],
            "items": [],
            "first_seen": _now(),
        }
        known = {c["name"] for c in entry["candidates"]}
        for cand in cands[:3]:
            row = cand.row()
            if row["name"] not in known:
                entry["candidates"].append(row)
                known.add(row["name"])
        merged = list(entry.get("items") or [])
        for item_id in items:
            if item_id not in merged:
                merged.append(item_id)
        entry["items"] = merged[-10:]
        if titles and not entry.get("title"):
            entry["title"] = str(titles[-1])[:120]
        pending[actor] = entry
        oldest = sorted(pending, key=lambda k: pending[k].get("first_seen") or "")
        while oldest and (len(pending) > _PENDING_LIMIT or not _fits(pending)):
            pending.pop(oldest.pop(0), None)
        await self.store.set(PENDING_KEY, pending)

    # ------------------------------------------------------------------ 报告
    async def report(self) -> dict[str, Any]:
        meta = await self.filetree.meta()
        ledger_ids = await self._ledger_ids()
        written_items = 0
        written_avatars = 0
        for item_id in ledger_ids[-2000:]:
            entry = await self._ledger(int(item_id))
            if entry.get("owned"):
                written_items += 1
                written_avatars += len(entry["owned"])
        warning = None
        uploads = any(
            isinstance(v, dict) and v.get("kind") == "upload"
            for v in (await self._dict(MANUAL_KEY)).values()
        )
        link_warning = thumb_url_warning(self.base_url)
        hosted = self.settings.thumb_mode == ThumbMode.HOSTED
        if hosted or uploads:
            warning = link_warning
        library_source = None
        try:
            choices = await self.library_choices()
            libraries = [i["name"] for i in choices["items"] if i["selected"]]
            library_source = choices["source"]
        except SkipItem:
            libraries = []
        names_meta = await self.names.meta() if self.names is not None else {}
        return {
            "settings": {
                "enabled": self.settings.enabled,
                "libraries": libraries,
                # none = 从没勾选过（也没有 0.1.x 旧设置）：页面提示先去选库
                "library_source": library_source,
                "dry_run": self.settings.dry_run,
                "write_armed": await self.armed(),
                "name_table": self.settings.name_table,
                "mirror": self.settings.mirror.value,
                "source_base": self.settings.source_base(),
                "ai_fix": self.settings.ai_fix,
                "thumb_mode": self.settings.thumb_mode.value,
                "infer_from_title": self.settings.infer_from_title,
                "max_items_per_run": self.settings.max_items_per_run,
                "skip_junk_names": self.settings.skip_junk_names,
                "ai_suggest_on_run": self.settings.ai_suggest_on_run,
                "ai_daily_limit": self.settings.ai_daily_limit,
                "process_arrivals": self.settings.process_arrivals,
                "search_enabled": self.settings.search_enabled,
                "search_sources": [
                    SOURCE_LABEL[k]
                    for k in (self.searcher.enabled_sources() if self.searcher else [])
                ],
                "search_auto_write": self.settings.search_auto_write,
                "search_per_run": self.settings.search_per_run,
                # 只说配没配，绝不回传 DMM 密钥本身
                "dmm_api": bool(self.searcher and self.searcher.api_configured()),
            },
            "thumb_warning": warning,
            # 0.4.0 页面按位置显示：托管模式提醒只放在设置里「头像地址写法」旁边与上传处，
            # 不再作为顶部横幅；hosted / uploads 让页面判断哪里需要提示
            "link": {
                "warning": link_warning,
                "hosted": hosted,
                "uploads": uploads,
                "own_url": bool((self.settings.public_base_url or "").strip()),
            },
            "filetree": {
                k: meta.get(k)
                for k in (
                    "url",
                    "etag",
                    "fetched_at",
                    "checked_at",
                    "total",
                    "timestamp",
                    "companies",
                    "bytes",
                    "last_status",
                    "last_error",
                )
            },
            "name_table": {
                k: names_meta.get(k)
                for k in ("url", "etag", "fetched_at", "checked_at", "keys", "last_error")
            },
            "host_display": await self._dict(HOST_KEY),
            "ledger": {"items": written_items, "avatars": written_avatars},
            "runs": (await self._list(RUNS_KEY))[-10:][::-1],
            "events": (await self._list(EVENTS_KEY))[-80:][::-1],
            "pending": len(await self._dict(PENDING_KEY)),
            "aliases": await self._dict(ALIASES_KEY),
            "manual": len(await self._dict(MANUAL_KEY)),
            "unmatched": await self._unmatched_counts(),
            "can_link_uploads": self._can_link(),
        }

    async def ledger_rows(self) -> list[dict]:
        rows = []
        for item_id in (await self._ledger_ids())[-500:][::-1]:
            entry = await self._ledger(int(item_id))
            if entry:
                rows.append(
                    {
                        k: entry.get(k)
                        for k in (
                            "media_item_id",
                            "library_id",
                            "title",
                            "nfo_path",
                            "created",
                            "owned",
                            "status",
                            "updated_at",
                        )
                    }
                )
        return rows

    # ------------------------------------------------------------------ 存储
    async def _dict(self, key: str) -> dict:
        value = await self.store.get(key, default={})
        return value if isinstance(value, dict) else {}

    async def _list(self, key: str) -> list:
        value = await self.store.get(key, default=[])
        return value if isinstance(value, list) else []

    async def _ledger(self, item_id: int) -> dict:
        return await self._dict(f"ledger:{item_id}")

    async def _save_ledger(self, item_id: int, entry: dict) -> None:
        await self.store.set(f"ledger:{item_id}", entry)
        shard = int(item_id) // _SHARD
        key = f"ledger:index:{shard}"
        ids = await self._list(key)
        if item_id not in ids:
            ids.append(item_id)
            await self.store.set(key, ids)
            shards = await self._list(LEDGER_SHARDS)
            if shard not in shards:
                await self.store.set(LEDGER_SHARDS, sorted([*shards, shard]))

    async def _ledger_ids(self) -> list[int]:
        """台账里的条目 id（按 id 分片存，每片 ≤ 4000 个，单值不超上限）。"""
        ids: list[int] = []
        for shard in await self._list(LEDGER_SHARDS):
            ids.extend(int(i) for i in await self._list(f"ledger:index:{int(shard)}"))
        return ids

    async def _event(
        self, item_id: int, library_id: Any, status: str, reason: str, **extra: Any
    ) -> None:
        event = {
            "media_item_id": item_id,
            "library_id": library_id,
            "status": status,
            "reason": reason,
            "at": _now(),
        }
        for key, value in extra.items():
            if isinstance(value, list):
                value = value[:12]
            elif isinstance(value, dict):
                value = dict(list(value.items())[:12])
            event[key] = value
        events = await self._list(EVENTS_KEY)
        events.append(event)
        await self.store.set(EVENTS_KEY, _fit_list(events[-_EVENTS_LIMIT:]))

    async def _push_run(self, summary: dict) -> None:
        runs = await self._list(RUNS_KEY)
        summary["notes"] = [str(n)[:300] for n in summary.get("notes") or []][:10]
        runs.append(summary)
        await self.store.set(RUNS_KEY, _fit_list(runs[-_RUNS_LIMIT:]))

    # ------------------------------------------------------------------ 文件
    async def _read_text(self, path: str) -> str:
        handle = await self.files.open(path, "rb")
        try:
            data = await asyncio.to_thread(handle.read)
        finally:
            await asyncio.to_thread(handle.close)
        return nfo.decode(data)

    async def _write_bytes(self, path: str, data: bytes, *, atomic: bool) -> None:
        target = f"{path}.actor-avatar.tmp" if atomic else path
        handle = await self.files.open(target, "wb")
        try:
            await asyncio.to_thread(handle.write, data)
        finally:
            await asyncio.to_thread(handle.close)
        if atomic:
            try:
                await self.files.rename(target, path)
            except Exception:
                with contextlib.suppress(Exception):
                    await self.files.remove(target)
                raise

    async def _atomic_write(self, path: str, text: str) -> None:
        await self._write_bytes(path, text.encode("utf-8"), atomic=True)

    async def _backup(self, item_id: int, text: str) -> str:
        """改写前的原文存进插件私有目录（不往媒体目录里丢 .bak）。只保留第一次的原文。"""
        directory = self.files.path("plugin", "backups")
        await self.files.makedirs(directory)
        path = os.path.join(directory, f"{item_id}.nfo")
        if not await self.files.exists(path):
            await self._write_bytes(path, text.encode("utf-8"), atomic=True)
        return path
