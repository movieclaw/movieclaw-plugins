"""插件运行时：取条目、查豆瓣、合并、写 NFO、刷新、复核、报告。"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import time
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

from movieclaw_sdk.jobs import (
    JobCancelled,
    JobContext,
    JobFailed,
    JobRetry,
    ResourceRef,
)
from movieclaw_sdk.ops import OpsError

from .douban import DoubanClient, DoubanError
from .matching import (
    CastEntry,
    DoubanActor,
    merge_cast,
    normalize_name,
)
from .nfo import (
    NfoError,
    NfoIdentity,
    build_identity_root,
    ensure_identity,
    merge_actors,
    nfo_filename,
    parse_nfo,
    serialize_nfo,
)
from .settings import Settings

ITEM_JOB = "cast-cn:item"
SWEEP_JOB = "cast-cn:sweep"
SWEEP_TASK = "cast-cn.sweep"
_DOUBAN_TTL = 30 * 24 * 3600
_DOUBAN_EMPTY_TTL = 3600
_SEARCH_TTL = 7 * 24 * 3600
# 匹配逻辑变化时递增，旧的负缓存自然失效
_SEARCH_CACHE_VERSION = "v2"
_REPORT_LIMIT = 200
_REFRESH_TIMEOUT_S = 600
_VERIFY_ATTEMPTS = 2
# 批次窗口：轮询间隔与单批等待上限
_BATCH_POLL_S = 12
_BATCH_WAIT_TIMEOUT_S = 45 * 60
# 视为处理完成的任务状态（等待中的 queued/running/retry_wait 等继续等）
_TERMINAL_JOB_STATUSES = {"succeeded", "failed", "cancelled", "blocked"}


class SkipItem(Exception):
    """这条条目不适合处理：不是失败，不重试。"""


class RetryLater(Exception):
    """暂时做不了：元数据没刮完、豆瓣没匹配到等，稍后重试。"""


class Runtime:
    def __init__(
        self,
        *,
        ctx: Any,
        settings: Settings,
        ops: Any,
        store: Any,
        files: Any,
        health: Any,
        jobs: Any,
        douban: DoubanClient,
        base_url: str,
    ) -> None:
        self.ctx = ctx
        self.settings = settings
        self.ops = ops
        self.store = store
        self.files = files
        self.health = health
        self.jobs = jobs
        self.douban = douban
        self.base_url = (base_url or "").rstrip("/")
        self.logger = ctx.logger
        self._refresh_sem = asyncio.Semaphore(1)
        self._libraries: list[dict[str, Any]] | None = None
        self._libraries_at = 0.0

    # ------------------------------------------------------------------ 任务
    async def enqueue_item(
        self,
        media_item_id: int,
        library_id: int,
        *,
        reason: str,
        manual: bool = False,
        dry_run: bool | None = None,
    ) -> Any:
        ref = await self.jobs.create(
            ITEM_JOB,
            input={
                "media_item_id": int(media_item_id),
                "library_id": int(library_id),
                "reason": reason,
                "manual": bool(manual),
                "dry_run": self.settings.dry_run if dry_run is None else bool(dry_run),
            },
            priority=-10 if not manual else 0,
            dedupe_key=f"item:{media_item_id}",
            # 只做归属，不做 target 独占：我们随后要等主程序的
            # library.items.refresh-metadata，它也需要 media_item 的 target 锁；
            # 两边都拿 target 会互相等到超时。
            resources=[ResourceRef("media_item", int(media_item_id), "context")],
        )
        if not ref.created:
            self.logger.info("cast-cn：条目 %s 已有在跑的任务，复用 %s", media_item_id, ref.id)
        return ref

    async def enqueue_sweep(
        self,
        *,
        reason: str,
        library_id: int | None = None,
        manual: bool = False,
        dry_run: bool | None = None,
    ) -> Any:
        scope = str(library_id) if library_id is not None else "all"
        ref = await self.jobs.create(
            SWEEP_JOB,
            input={
                "reason": reason,
                "library_id": int(library_id) if library_id is not None else None,
                "manual": bool(manual),
                "dry_run": dry_run,
            },
            priority=-20,
            dedupe_key=f"sweep:{scope}",
        )
        if not ref.created:
            self.logger.info("cast-cn：补跑任务已在跑，复用 %s", ref.id)
        return ref

    # ------------------------------------------------------------------ 单条目
    async def process_item(self, context: JobContext, data: dict[str, Any]) -> dict[str, Any]:
        media_item_id = int(data["media_item_id"])
        library_id = int(data["library_id"])
        manual = bool(data.get("manual"))
        dry_run = bool(data.get("dry_run", self.settings.dry_run))

        if not self.settings.enabled and not manual:
            return {"status": "disabled"}

        if self.settings.delay_seconds and not manual:
            await self._sleep_cancellable(context, self.settings.delay_seconds)

        state = await self._item_state(media_item_id)
        # 上次已经写完 NFO、但刷新/复核没走完：直接续跑，不重复改 NFO
        if state.get("pending_refresh") and state.get("nfo_path"):
            if await self.files.exists(state["nfo_path"]):
                return await self._refresh_and_verify(context, media_item_id, library_id, state)
            state["pending_refresh"] = False
            await self._save_state(media_item_id, state)

        try:
            await context.update_progress(
                mode="indeterminate",
                phase="fetch",
                message="读取条目",
            )
            item = await self._fetch_item(library_id, media_item_id)
            await self._validate_item(item, library_id)
            title = str(item.get("title") or "")
            actors = [
                CastEntry(
                    name=str(row.get("name") or ""),
                    role=(str(row.get("role")) if row.get("role") else None),
                    thumb_url=(str(row.get("thumb_url")) if row.get("thumb_url") else None),
                    tmdb_person_id=(
                        int(row["tmdb_person_id"])
                        if row.get("tmdb_person_id") is not None
                        else None
                    ),
                    order=index,
                )
                for index, row in enumerate(item["local_meta"].get("actors") or [])
                if (row.get("name") or "").strip()
            ]
            if not actors:
                raise RetryLater("演员表为空（元数据可能还没刮完）")

            await context.update_progress(
                mode="indeterminate",
                phase="douban",
                message=f"匹配豆瓣：《{title}》",
            )
            douban_ids = await self._find_douban_ids(item)
            if not douban_ids:
                raise RetryLater("没有匹配到豆瓣条目")

            await context.update_progress(
                mode="indeterminate",
                phase="merge",
                message="合并演员表",
            )
            # 合并多个豆瓣条目的演员表：剧集常按季拆条目，同一部作品各季/总条目
            # 的演员互有出入（TMDB 的条目级演员表横跨全季），取第一个只会覆盖一部分。
            # 去重后统一匹配，能覆盖的演员明显更多。
            combined: list[DoubanActor] = []
            seen_names: set[str] = set()
            for douban_id in douban_ids[:5]:
                for actor in await self._get_douban_cast(douban_id):
                    key = normalize_name(actor.name)
                    if not key or key in seen_names:
                        continue
                    seen_names.add(key)
                    combined.append(actor)
            if not combined:
                raise RetryLater("豆瓣没有返回演员表")
            merged = merge_cast(
                actors,
                combined,
                condition=self.settings.condition,
                force_name=self.settings.force_name,
                force_role=self.settings.force_role,
                remove_non_chinese=self.settings.remove_non_chinese,
                remove_no_avatar=self.settings.remove_no_avatar,
                avatar_policy=self.settings.avatar_policy,
                base_url=self.base_url,
                keep_person_links=self.settings.keep_person_links,
            )
            if merged.matched == 0:
                # 豆瓣条目找到了、但演员表对不上：这是数据源不匹配，不是临时故障。
                # 重试三次只会把一条“跳过”记成“失败”，直接跳过并留下原因。
                raise SkipItem("豆瓣演员表与条目没有匹配")
            if not merged.changed:
                await self._clear_retry(media_item_id)
                await self._record(
                    media_item_id,
                    status="noop",
                    title=title,
                    library_id=library_id,
                    matched=merged.matched,
                    unresolved=merged.unresolved,
                    warnings=merged.warnings,
                )
                return {
                    "status": "noop",
                    "matched": merged.matched,
                    "unresolved": len(merged.unresolved),
                }

            if dry_run:
                await self._record(
                    media_item_id,
                    status="dry_run",
                    title=title,
                    library_id=library_id,
                    matched=merged.matched,
                    changes=merged.changes,
                    unresolved=merged.unresolved,
                    warnings=merged.warnings,
                    dropped=merged.dropped,
                )
                return {
                    "status": "dry_run",
                    "matched": merged.matched,
                    "changes": [asdict(change) for change in merged.changes],
                    "dropped": merged.dropped,
                }

            nfo_path = await self._pick_nfo_path(item)
            identity = NfoIdentity(
                kind=str(item["kind"]),
                tmdb_id=int(item["tmdb_id"]),
                title=title,
                original_title=(str(item.get("original_title")) or None),
                year=(int(item["year"]) if item.get("year") is not None else None),
            )
            existing_text, root = await self._read_nfo(nfo_path)
            if root is None:
                root = build_identity_root(identity)
            else:
                ensure_identity(root, identity)
            merge_actors(root, merged.actors)
            new_text = serialize_nfo(root)
            if existing_text is not None and new_text == existing_text:
                await self._clear_retry(media_item_id)
                await self._record(
                    media_item_id,
                    status="noop",
                    title=title,
                    library_id=library_id,
                    matched=merged.matched,
                    warnings=merged.warnings,
                )
                return {"status": "noop", "reason": "NFO 内容未变化"}

            backup = await self._backup_and_write(nfo_path, existing_text, new_text)
            state = await self._item_state(media_item_id)
            state.update(
                {
                    "status": "nfo_written",
                    "title": title,
                    "library_id": library_id,
                    "nfo_path": nfo_path,
                    "backup": backup,
                    "pending_refresh": True,
                    "verify_attempts": 0,
                    "expected": [
                        {
                            "name": change.name_after,
                            "role": change.role_after,
                            "thumb": change.thumb_after,
                            "name_changed": change.name_changed,
                            "role_changed": change.role_changed,
                        }
                        for change in merged.changes
                        if change.matched
                        and (change.name_changed or change.role_changed or change.thumb_changed)
                        and normalize_name(change.name_after)
                        in {normalize_name(actor.name) for actor in merged.actors}
                    ],
                    "warnings": merged.warnings,
                    "matched": merged.matched,
                    "dropped": merged.dropped[:40],
                    "updated_at": _now(),
                }
            )
            await self._save_state(media_item_id, state)
            self.logger.info(
                "cast-cn：《%s》写入 NFO %s（备份 %s），开始刷新元数据",
                title,
                nfo_path,
                backup or "无（原文件不存在）",
            )
            return await self._refresh_and_verify(context, media_item_id, library_id, state)

        except SkipItem as exc:
            await self._clear_retry(media_item_id)
            await self._record(
                media_item_id,
                status="skipped",
                library_id=library_id,
                reason=str(exc),
            )
            self.logger.info("cast-cn：条目 %s 跳过：%s", media_item_id, exc)
            return {"status": "skipped", "reason": str(exc)}
        except RetryLater as exc:
            await self._retry_or_fail(media_item_id, library_id, str(exc))
            raise
        except NfoError as exc:
            await self._fail(media_item_id, library_id, str(exc))
            raise JobFailed(f"NFO 未写入：{exc}", code="CAST_CN_NFO") from exc
        except OpsError as exc:
            if exc.status in (403, 404):
                await self._fail(media_item_id, library_id, exc.message)
                raise JobFailed(f"宿主操作失败：{exc.message}", code=exc.code) from exc
            await self._retry_or_fail(media_item_id, library_id, exc.message)
            raise
        except DoubanError as exc:
            await self.health.degraded("douban", str(exc), action_href="/settings/plugins")
            await self._retry_or_fail(media_item_id, library_id, str(exc))
            raise
        except (JobRetry, JobFailed):
            raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 任务失败要落到报告，不吞异常
            self.logger.exception("cast-cn：条目 %s 处理失败", media_item_id)
            await self._fail(media_item_id, library_id, str(exc))
            raise JobFailed(f"处理失败：{exc}", code="CAST_CN_ERROR") from exc

    # ------------------------------------------------------------------ 刷新/复核
    async def _refresh_and_verify(
        self,
        context: JobContext,
        media_item_id: int,
        library_id: int,
        state: dict[str, Any],
    ) -> dict[str, Any]:
        await context.update_progress(
            mode="indeterminate",
            phase="refresh",
            message="等待主程序重新吸收 NFO",
        )
        await self._refresh_item(library_id, media_item_id)
        await context.update_progress(
            mode="indeterminate",
            phase="verify",
            message="复核详情页演员表",
        )
        verify = await self._verify(media_item_id, state.get("expected") or [])
        if not verify["ok"]:
            attempts = int(state.get("verify_attempts", 0)) + 1
            state["verify_attempts"] = attempts
            # 关键：复核失败说明主程序刷新时没有重吸收我们的 NFO（保鲜刷新的
            # 指纹守卫把吸收跳过了）。只重新刷新没用——必须让重试走完整流程
            # （重新合并 + 重写 NFO + 再刷新），重写会打破指纹守卫。
            state["pending_refresh"] = False
            await self._save_state(media_item_id, state)
            if attempts < _VERIFY_ATTEMPTS:
                raise JobRetry(
                    f"刷新后演员表还没生效（{', '.join(verify['missing'][:3])}），"
                    "将重写 NFO 后重试",
                    delay_seconds=120,
                )
            reason = f"刷新后演员表仍未生效：{', '.join(verify['missing'][:5])}"
            await self._fail(media_item_id, library_id, reason)
            raise JobFailed(reason, code="CAST_CN_VERIFY")

        state.update(
            {
                "status": "ok",
                "pending_refresh": False,
                "verify_attempts": 0,
                "finished_at": _now(),
                "thumb_missing": verify["thumb_missing"],
            }
        )
        await self._save_state(media_item_id, state)
        await self._clear_retry(media_item_id)
        dropped = state.get("dropped") or []
        await self._record(
            media_item_id,
            status="ok",
            title=str(state.get("title") or ""),
            library_id=library_id,
            matched=int(state.get("matched") or 0),
            dropped=dropped,
            reason=(
                f"已移除 {len(dropped)} 位演员（无头像/非中文）" if dropped else ""
            ),
            warnings=state.get("warnings") or [],
            thumb_missing=verify["thumb_missing"],
        )
        return {
            "status": "ok",
            "nfo": state.get("nfo_path"),
            "backup": state.get("backup"),
            "thumb_missing": verify["thumb_missing"],
        }

    async def _refresh_item(self, library_id: int, media_item_id: int) -> None:
        async with self._refresh_sem:
            data = await self.ops.call(
                "library.items.refresh-metadata",
                {"library_id": library_id, "media_item_id": media_item_id},
            )
            job_id = (data or {}).get("job_id")
            if not job_id:
                raise JobRetry("主程序没有返回刷新任务 id，稍后重试", delay_seconds=60)
            await self._wait_job(str(job_id))

    async def _wait_job(self, job_id: str) -> None:
        deadline = time.monotonic() + _REFRESH_TIMEOUT_S
        revision = 0
        while time.monotonic() < deadline:
            try:
                data = await self.ops.call(
                    "jobs.wait",
                    {
                        "job_id": job_id,
                        "after_revision": revision,
                        "wait_seconds": 30,
                    },
                )
            except OpsError as exc:
                if exc.status == 404:
                    raise JobFailed("刷新任务已不存在", code="CAST_CN_JOB_GONE") from exc
                raise JobRetry(f"等待刷新任务失败：{exc.message}", delay_seconds=60) from exc
            job = (data or {}).get("job") or {}
            revision = int(job.get("revision") or revision)
            status = str(job.get("status") or "")
            if status == "succeeded":
                return
            if status in {"failed", "cancelled", "blocked"}:
                error = job.get("error") or {}
                message = str(error.get("message") or status)
                raise JobRetry(f"刷新元数据未成功：{message}", delay_seconds=300)
            if status in {"queued", "running", "retry_wait", "waiting", "cancelling"}:
                continue
        raise JobRetry("等待刷新超时，稍后重试", delay_seconds=300)

    async def _verify(self, media_item_id: int, expected: list[dict[str, Any]]) -> dict[str, Any]:
        item = await self._fetch_item_by_id(media_item_id)
        rows = ((item.get("local_meta") or {}).get("actors") or []) if item else []
        by_name = {
            normalize_name(str(row.get("name") or "")): row for row in rows if row.get("name")
        }
        missing: list[str] = []
        thumb_missing: list[str] = []
        for want in expected:
            name = str(want.get("name") or "")
            row = by_name.get(normalize_name(name))
            if row is None:
                missing.append(name)
                continue
            if want.get("name_changed") and str(row.get("name") or "") != name:
                missing.append(name)
                continue
            if want.get("role_changed") and (row.get("role") or None) != want.get("role"):
                missing.append(name)
                continue
            thumb = str(want.get("thumb") or "")
            if thumb.startswith(("http://", "https://")) and not row.get("thumb_url"):
                thumb_missing.append(name)
        return {"ok": not missing, "missing": missing, "thumb_missing": thumb_missing}

    async def _fetch_item_by_id(self, media_item_id: int) -> dict[str, Any] | None:
        state = await self._item_state(media_item_id)
        library_id = state.get("library_id")
        if library_id is None:
            return None
        return await self._fetch_item(int(library_id), media_item_id)

    # ------------------------------------------------------------------ 宿主数据
    async def _fetch_item(self, library_id: int, media_item_id: int) -> dict[str, Any]:
        try:
            data = await self.ops.call(
                "library.items.get",
                {"library_id": library_id, "media_item_id": media_item_id},
            )
        except OpsError as exc:
            if exc.status == 404:
                raise SkipItem("条目不存在或已删除") from exc
            raise
        if not isinstance(data, dict):
            raise RetryLater("条目详情返回为空")
        return data

    async def _validate_item(self, item: dict[str, Any], library_id: int) -> None:
        wanted = await self._resolved_library_ids()
        if wanted is not None and library_id not in wanted:
            raise SkipItem("不在配置的媒体库范围内")
        if item.get("kind") not in ("movie", "tv"):
            raise SkipItem("只处理电影/剧集")
        if item.get("source") != "tmdb" or not item.get("tmdb_id"):
            raise SkipItem("不是 TMDB 识别的条目")
        if not item.get("entry_dirs"):
            raise SkipItem("没有本地条目目录（网盘-only / .strm / 未挂载）")
        if not item.get("local_meta"):
            raise RetryLater("元数据还没刮完")
        await self.list_libraries()
        lib = self._library_meta_sync(library_id)
        if lib is not None:
            if (lib.get("capabilities") or {}).get("write_nfo") is False:
                raise SkipItem("该媒体库未开启写 NFO")
            if lib.get("source") == "local":
                raise SkipItem("本地内容库不处理")

    async def _find_douban_ids(self, item: dict[str, Any]) -> list[str]:
        existing = item.get("douban_id")
        if existing:
            value = str(existing).strip()
            return [value] if value else []

        title = str(item.get("title") or "").strip()
        year = item.get("year")
        kind = item.get("kind")
        if not title:
            return []
        cache_key = f"search:{_SEARCH_CACHE_VERSION}:{normalize_name(title)}:{year or ''}"
        cached = await self._cache_get(cache_key)
        if isinstance(cached, list):
            return [str(value) for value in cached if str(value).strip()]

        queries: list[str] = [title]
        for key in ("original_title", "english_title"):
            value = str(item.get(key) or "").strip()
            if value and value != title and value not in queries:
                queries.append(value)
        for query in queries[:2]:
            try:
                data = await self.ops.call(
                    "search.titles",
                    {"query": query, "provider": "douban", "save_history": False},
                )
            except OpsError as exc:
                raise RetryLater(f"豆瓣搜索失败：{exc.message}") from exc
            found = self._pick_douban_titles(
                (data or {}).get("titles") or [],
                title=title,
                query=query,
                year=year,
                kind=kind,
            )
            if found:
                await self._cache_set(cache_key, found[:5], _SEARCH_TTL)
                return found[:5]
        await self._cache_set(cache_key, [], _SEARCH_TTL)
        return []

    @staticmethod
    def _pick_douban_titles(
        titles: list[dict[str, Any]], *, title: str, query: str, year: Any, kind: Any
    ) -> list[str]:
        """按可信度返回候选豆瓣 id。

        剧集优先季条目：豆瓣常把剧集拆成「第一季 / 第二季…」，总条目有时只是一个
        占位页（例如 Stranger Things 总条目只有 1 位演员）。季条目按季号排序，
        同季再按标题贴近度；总条目排在季条目之后作为兜底。
        """
        targets = {normalize_name(title), normalize_name(query)} - {""}
        scored: list[tuple[int, int, int | None, int, str]] = []
        for row in titles:
            if str(row.get("provider") or "") != "douban":
                continue
            ext = str(row.get("external_id") or "").strip()
            if not ext:
                continue
            media_type = row.get("media_type")
            if media_type and kind and media_type != kind:
                continue
            title_text = str(row.get("title") or "")
            candidate = normalize_name(title_text)
            if not candidate:
                continue
            if candidate in targets:
                score = 100
            elif any(target in candidate or candidate in target for target in targets):
                score = 70
            else:
                continue
            year_penalty = 0
            candidate_year = row.get("release_year")
            if year and candidate_year:
                try:
                    diff = abs(int(candidate_year) - int(year))
                except (TypeError, ValueError):
                    diff = 99
                if diff > 1:
                    continue
                year_penalty = diff * 5
            season = Runtime._season_number(title_text)
            if kind == "movie" and season is not None:
                # 电影不要拿剧集的季条目当演员来源
                continue
            scored.append((score, year_penalty, season, len(title_text), ext))
        if not scored:
            return []

        if kind == "tv":
            seasons = [row for row in scored if row[2] is not None]
            bases = [row for row in scored if row[2] is None]
            seasons.sort(key=lambda row: (row[2], -row[0], row[1], row[3]))
            bases.sort(key=lambda row: (-row[0], row[1], row[3]))
            ordered = seasons + bases
        else:
            ordered = sorted(scored, key=lambda row: (-row[0], row[1], row[3]))

        result: list[str] = []
        for row in ordered:
            if row[4] not in result:
                result.append(row[4])
        return result

    @staticmethod
    def _season_number(title: str) -> int | None:
        match = re.search(r"第\s*([一二三四五六七八九十两\d]+)\s*季", title)
        if match:
            return _chinese_number(match.group(1))
        match = re.search(r"\bSeason\s*(\d+)", title, re.IGNORECASE)
        if match:
            return int(match.group(1))
        return None

    async def _get_douban_cast(self, douban_id: str) -> list[DoubanActor]:
        cache_key = f"douban:{douban_id}"
        cached = await self._cache_get(cache_key)
        if isinstance(cached, list):
            return [
                DoubanActor(**row) for row in cached if isinstance(row, dict) and row.get("name")
            ]
        try:
            actors = await self.douban.celebrities(douban_id)
        except DoubanError:
            raise
        await self.health.ok("douban")
        ttl = _DOUBAN_TTL if actors else _DOUBAN_EMPTY_TTL
        await self._cache_set(cache_key, [asdict(actor) for actor in actors], ttl)
        return actors

    async def list_libraries(self, *, force: bool = False) -> list[dict[str, Any]]:
        now = time.monotonic()
        if not force and self._libraries is not None and now - self._libraries_at < 60:
            return self._libraries
        try:
            data = await self.ops.call("library.list", {"scope": "all"})
        except OpsError as exc:
            raise RetryLater(f"读取媒体库失败：{exc.message}") from exc
        self._libraries = [row for row in (data or []) if isinstance(row, dict)]
        self._libraries_at = now
        return self._libraries

    def _library_meta_sync(self, library_id: int) -> dict[str, Any] | None:
        if self._libraries is None:
            return None
        for row in self._libraries:
            if int(row.get("id") or 0) == library_id:
                return row
        return None

    async def _resolved_library_ids(self) -> set[int] | None:
        """配置的媒体库（名称或 id）→ 实际库 id 集合；未配置返回 None（全部）。

        名称按 `library.list` 的 name 精确匹配，纯数字同时兼容 id；写错的名称
        记警告并列出可用库，绝不静默跳过。
        """
        entries = self.settings.library_names()
        if not entries:
            return None
        libraries = await self.list_libraries(force=True)
        by_name: dict[str, int] = {}
        by_id: dict[str, int] = {}
        for row in libraries:
            lid = int(row.get("id") or 0)
            if lid <= 0:
                continue
            by_name[str(row.get("name") or "").strip()] = lid
            by_id[str(lid)] = lid
        resolved: set[int] = set()
        unknown: list[str] = []
        for entry in entries:
            if entry in by_name:
                resolved.add(by_name[entry])
            elif entry in by_id:
                resolved.add(by_id[entry])
            else:
                unknown.append(entry)
        if unknown:
            available = "、".join(
                f"{row.get('name')}(id {row.get('id')})" for row in libraries
            )
            self.logger.warning(
                "cast-cn：这些媒体库没找到：%s。可用：%s", "、".join(unknown), available
            )
        return resolved

    # ------------------------------------------------------------------ 全库补跑
    async def sweep(self, context: JobContext, data: dict[str, Any]) -> dict[str, Any]:
        if not self.settings.enabled and not data.get("manual"):
            return {"status": "disabled"}

        libraries = await self.list_libraries(force=True)
        only = data.get("library_id")
        if only is not None:
            libraries = [row for row in libraries if int(row.get("id") or 0) == int(only)]
        wanted = await self._resolved_library_ids()
        libraries = [
            row
            for row in libraries
            if (wanted is None or int(row.get("id") or 0) in wanted)
            and (row.get("capabilities") or {}).get("write_nfo") is not False
            and row.get("source") != "local"
        ]
        if not libraries:
            return {"status": "skipped", "reason": "没有可处理的媒体库"}

        cursor = await self.store.get("sweep:cursor", default={})
        if not isinstance(cursor, dict):
            cursor = {}
        lib_index = max(0, int(cursor.get("library_index") or 0))
        offset = max(0, int(cursor.get("offset") or 0))
        if lib_index >= len(libraries):
            lib_index, offset = 0, 0

        limit = self.settings.max_items_per_run
        batch_size = max(1, self.settings.batch_size)
        queued = 0
        pages = 0
        dry_run = data.get("dry_run")
        inflight: list[str] = []
        while lib_index < len(libraries) and queued < limit and pages < 100:
            await context.raise_if_cancelled()
            library = libraries[lib_index]
            library_id = int(library["id"])
            try:
                rows = await self.ops.call(
                    "library.items.list",
                    {
                        "library_id": library_id,
                        "limit": 50,
                        "offset": offset,
                        "identity": "confirmed",
                    },
                )
            except OpsError as exc:
                raise RetryLater(f"读取库 {library_id} 条目失败：{exc.message}") from exc
            rows = [row for row in (rows or []) if isinstance(row, dict)]
            pages += 1
            if not rows:
                lib_index += 1
                offset = 0
                continue
            consumed = 0
            for row in rows:
                await context.raise_if_cancelled()
                if queued >= limit:
                    break
                consumed += 1
                if row.get("kind") not in ("movie", "tv"):
                    continue
                if row.get("source") != "tmdb" or not row.get("tmdb_id"):
                    continue
                media_item_id = row.get("media_item_id")
                if not media_item_id:
                    continue
                ref = await self.enqueue_item(
                    int(media_item_id),
                    library_id,
                    reason="sweep",
                    dry_run=dry_run,
                )
                inflight.append(ref.id)
                queued += 1
                if len(inflight) >= batch_size:
                    # 批次窗口：等这一批全部处理完再排下一批，
                    # 活动中心同时只显示 batch_size 个任务
                    await context.update_progress(
                        mode="determinate",
                        phase="sweep",
                        message=f"已排 {queued} 条，等待当前 {len(inflight)} 条完成",
                        current=queued,
                        total=limit,
                    )
                    await self._wait_batch(context, inflight)
                    inflight = []
                    await context.update_progress(
                        mode="determinate",
                        phase="sweep",
                        message=f"已处理 {queued} 条",
                        current=queued,
                        total=limit,
                    )
            offset += consumed
            if queued >= limit:
                break
            if len(rows) < 50:
                lib_index += 1
                offset = 0

        if inflight:
            await self._wait_batch(context, inflight)
            inflight = []

        done = lib_index >= len(libraries)
        await self.store.set(
            "sweep:cursor",
            {"library_index": 0 if done else lib_index, "offset": 0 if done else offset},
        )
        return {
            "status": "ok",
            "queued": queued,
            "done": done,
            "next_library": None if done else int(libraries[lib_index]["id"]),
        }

    async def _wait_batch(self, context: JobContext, job_ids: list[str]) -> None:
        """等待一批任务全部进入终态；活动中心因此同时只显示一批。

        - 每 12 秒轮询一次状态；`failed` 的自动忽略（记录都在插件报告里），
          保持活动中心干净
        - 补跑任务被取消时，连带取消批内未完成的任务
        - 单批等待上限 45 分钟：超时的任务放它自己跑完，不再阻塞后续批次
        """
        pending = list(dict.fromkeys(job_ids))
        deadline = time.monotonic() + _BATCH_WAIT_TIMEOUT_S
        total = len(pending)
        while pending:
            if await context.cancel_requested():
                for jid in pending:
                    with contextlib.suppress(Exception):
                        await self.ops.call("jobs.cancel", {"job_id": jid})
                raise JobCancelled()
            await asyncio.sleep(_BATCH_POLL_S)
            still: list[str] = []
            done = 0
            for jid in pending:
                try:
                    data = await self.ops.call("jobs.show", {"job_id": jid})
                except OpsError as exc:
                    if exc.status == 404:
                        continue  # 任务记录已不存在，视为结束
                    still.append(jid)
                    continue
                status = str((data or {}).get("status") or "")
                if status in _TERMINAL_JOB_STATUSES:
                    done += 1
                    if status == "failed":
                        with contextlib.suppress(Exception):
                            await self.ops.call("jobs.dismiss", {"job_id": jid})
                else:
                    still.append(jid)
            if not still:
                return
            with contextlib.suppress(Exception):
                await context.update_progress(
                    mode="determinate",
                    phase="sweep",
                    message=f"等待本批完成 {done}/{total}",
                    current=done,
                    total=total,
                )
            if time.monotonic() > deadline:
                self.logger.warning(
                    "cast-cn：等待批次超时（%d 条仍未结束），继续处理后续批次",
                    len(still),
                )
                return
            pending = still

    # ------------------------------------------------------------------ NFO
    async def _pick_nfo_path(self, item: dict[str, Any]) -> str:
        dirs: list[str] = []
        for raw in item.get("entry_dirs") or []:
            value = str(raw or "").strip()
            if value and value not in dirs:
                dirs.append(value)
        if not dirs:
            raise SkipItem("没有本地条目目录")
        filename = nfo_filename(str(item["kind"]))
        for directory in dirs:
            candidate = os.path.normpath(os.path.join(directory, filename))
            if await self.files.exists(candidate):
                # 进程外文件的写入授权由 open(..., "wb") / rename 在宿主侧检查；
                # 远程文件代理没有 check()，不能在这里预检。
                return candidate
        return os.path.normpath(os.path.join(dirs[0], filename))

    async def _read_nfo(self, path: str) -> tuple[str | None, Any | None]:
        if not await self.files.exists(path):
            return None, None
        text = await self._read_text(path)
        if not text.strip():
            raise NfoError("NFO 是空文件，拒绝覆盖")
        return text, parse_nfo(text)

    async def _read_text(self, path: str) -> str:
        handle = await self.files.open(path, "rb")
        try:
            data = await asyncio.to_thread(handle.read)
        finally:
            await asyncio.to_thread(handle.close)
        for encoding in ("utf-8-sig", "utf-8", "gb18030"):
            try:
                return data.decode(encoding)
            except UnicodeDecodeError:
                continue
        raise NfoError("NFO 编码无法识别（不是 UTF-8/GB18030）")

    async def _backup_and_write(
        self, path: str, existing_text: str | None, new_text: str
    ) -> str | None:
        backup: str | None = None
        if existing_text is not None:
            stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
            backup = f"{path}.cast-cn.{stamp}.bak"
            try:
                await self._write_text(backup, existing_text)
            except Exception as exc:  # noqa: BLE001
                raise NfoError(f"备份 NFO 失败，已中止写入：{exc}") from exc
            await self._prune_backups(path)

        temp = f"{path}.cast-cn.tmp.{os.getpid()}.{int(time.time() * 1000)}"
        try:
            await self._write_text(temp, new_text)
            await self.files.rename(temp, path)
        except Exception:
            with contextlib.suppress(Exception):
                await self.files.remove(temp)
            raise
        return backup

    async def _atomic_write(self, path: str, text: str) -> None:
        temp = f"{path}.cast-cn.tmp.{os.getpid()}.{int(time.time() * 1000)}"
        try:
            await self._write_text(temp, text)
            await self.files.rename(temp, path)
        except Exception:
            with contextlib.suppress(Exception):
                await self.files.remove(temp)
            raise

    async def _write_text(self, path: str, text: str) -> None:
        handle = await self.files.open(path, "wb")
        try:
            await asyncio.to_thread(handle.write, text.encode("utf-8"))
        finally:
            await asyncio.to_thread(handle.close)

    async def _prune_backups(self, nfo_path: str) -> None:
        directory = os.path.dirname(nfo_path)
        prefix = os.path.basename(nfo_path) + ".cast-cn."
        try:
            names = await self.files.listdir(directory)
        except Exception:  # noqa: BLE001
            return
        backups = sorted(
            name for name in names if name.startswith(prefix) and name.endswith(".bak")
        )
        for name in backups[:-3]:
            try:
                await self.files.remove(os.path.join(directory, name))
            except Exception:  # noqa: BLE001
                self.logger.warning("cast-cn：清理旧备份失败：%s", name)

    async def restore(self, media_item_id: int) -> dict[str, Any]:
        state = await self._item_state(media_item_id)
        nfo_path = state.get("nfo_path")
        backup = state.get("backup")
        if not nfo_path or not backup:
            raise SkipItem("这条条目没有可恢复的 NFO 备份")
        if not await self.files.exists(backup):
            raise SkipItem("备份文件已不存在")
        text = await self._read_text(backup)
        await self._atomic_write(nfo_path, text)
        state.update({"status": "restored", "pending_refresh": False, "restored_at": _now()})
        await self._save_state(media_item_id, state)

        # 备份恢复后必须让主程序重新吸收，否则详情页还是旧数据。
        # 这里只发起刷新、不等结果；调用方可以看任务中心或 report。
        job_id: str | None = None
        library_id = state.get("library_id")
        if library_id is not None:
            try:
                data = await self.ops.call(
                    "library.items.refresh-metadata",
                    {"library_id": int(library_id), "media_item_id": media_item_id},
                )
                job_id = (data or {}).get("job_id")
            except OpsError as exc:
                self.logger.warning(
                    "cast-cn：恢复 NFO 后发起刷新失败（条目 %s）：%s",
                    media_item_id,
                    exc.message,
                )
        return {
            "status": "restored",
            "nfo": nfo_path,
            "backup": backup,
            "refresh_started": bool(job_id),
            "job_id": job_id,
        }

    # ------------------------------------------------------------------ 状态/报告
    async def report(self) -> dict[str, Any]:
        items = await self.store.get("report:items", default=[])
        stats = await self.store.get("stats", default={})
        return {
            "settings": {
                "enabled": self.settings.enabled,
                "libraries": self.settings.libraries,
                "condition": self.settings.condition.value,
                "avatar_policy": self.settings.avatar_policy.value,
                "dry_run": self.settings.dry_run,
                "keep_person_links": self.settings.keep_person_links,
                "base_url_set": bool(self.base_url),
            },
            "stats": stats if isinstance(stats, dict) else {},
            "items": items[-50:] if isinstance(items, list) else [],
        }

    async def _record(
        self,
        media_item_id: int,
        *,
        status: str,
        title: str = "",
        library_id: int | None = None,
        reason: str = "",
        matched: int = 0,
        unresolved: list[str] | None = None,
        changes: list[Any] | None = None,
        warnings: list[str] | None = None,
        thumb_missing: list[str] | None = None,
        dropped: list[str] | None = None,
    ) -> None:
        now = _now()
        state = await self._item_state(media_item_id)
        state.update(
            {
                "media_item_id": media_item_id,
                "library_id": library_id if library_id is not None else state.get("library_id"),
                "title": title or state.get("title") or "",
                "status": status,
                "reason": reason,
                "matched": matched,
                "unresolved": (unresolved or [])[:40],
                "dropped": (dropped or [])[:40],
                "warnings": (warnings or [])[:20],
                "thumb_missing": (thumb_missing or [])[:20],
                "updated_at": now,
            }
        )
        if changes is not None:
            state["changes"] = [asdict(change) for change in changes][:40]
        if status in {"ok", "noop", "skipped", "dry_run", "restored"}:
            state["pending_refresh"] = False
        await self._save_state(media_item_id, state)

        summary = {
            "media_item_id": media_item_id,
            "library_id": state.get("library_id"),
            "title": state.get("title"),
            "status": status,
            "reason": reason,
            "matched": matched,
            "dropped": (dropped or [])[:20],
            "at": now,
        }
        items = await self.store.get("report:items", default=[])
        if not isinstance(items, list):
            items = []
        items.append(summary)
        await self.store.set("report:items", items[-_REPORT_LIMIT:])

        stats = await self.store.get("stats", default={})
        if not isinstance(stats, dict):
            stats = {}
        stats[status] = int(stats.get(status) or 0) + 1
        stats["last_run"] = now
        await self.store.set("stats", stats)

    async def _fail(self, media_item_id: int, library_id: int, reason: str) -> None:
        await self._record(
            media_item_id,
            status="failed",
            library_id=library_id,
            reason=reason,
        )

    async def _retry_or_fail(self, media_item_id: int, library_id: int, reason: str) -> None:
        key = f"retry:{media_item_id}"
        attempts = int(await self.store.get(key, default=0) or 0) + 1
        await self.store.set(key, attempts)
        if attempts >= 3:
            await self.store.delete(key)
            await self._fail(media_item_id, library_id, reason)
            raise JobFailed(
                f"{reason}（已尝试 {attempts} 次）",
                code="CAST_CN_RETRY_EXHAUSTED",
            )
        raise JobRetry(f"{reason}（第 {attempts} 次）", delay_seconds=60 * attempts)

    async def _clear_retry(self, media_item_id: int) -> None:
        with contextlib.suppress(Exception):
            await self.store.delete(f"retry:{media_item_id}")

    async def _item_state(self, media_item_id: int) -> dict[str, Any]:
        value = await self.store.get(f"item:{media_item_id}", default={})
        return value if isinstance(value, dict) else {}

    async def _save_state(self, media_item_id: int, state: dict[str, Any]) -> None:
        try:
            await self.store.set(f"item:{media_item_id}", state)
        except ValueError:
            # 单值 64 KB 上限：去掉大字段后重试
            state = dict(state)
            state["changes"] = (state.get("changes") or [])[:10]
            state["expected"] = (state.get("expected") or [])[:20]
            await self.store.set(f"item:{media_item_id}", state)

    async def _cache_get(self, key: str) -> Any:
        row = await self.store.get(key, default=None)
        if not isinstance(row, dict):
            return None
        if float(row.get("expires_at") or 0) <= time.time():
            return None
        return row.get("value")

    async def _cache_set(self, key: str, value: Any, ttl: int) -> None:
        try:
            await self.store.set(key, {"expires_at": time.time() + ttl, "value": value})
        except ValueError:
            self.logger.warning("cast-cn：缓存 %s 超过插件数据上限，跳过", key)

    async def _sleep_cancellable(self, context: JobContext, seconds: int) -> None:
        remaining = max(0, int(seconds))
        while remaining > 0:
            await context.raise_if_cancelled()
            step = min(5, remaining)
            await asyncio.sleep(step)
            remaining -= step


def _now() -> str:
    return datetime.now(UTC).isoformat()


_CN_DIGITS = {
    "零": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}


def _chinese_number(text: str) -> int | None:
    """「一」～「九十九」或阿拉伯数字 → int；解析不了返回 None。"""
    text = (text or "").strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    if text == "十":
        return 10
    if "十" in text:
        left, _, right = text.partition("十")
        tens = _CN_DIGITS.get(left, 1) if left else 1
        ones = _CN_DIGITS.get(right, 0) if right else 0
        return tens * 10 + ones
    return _CN_DIGITS.get(text)
