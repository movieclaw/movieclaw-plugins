"""私人观影档案与导入预览。统计按作品身份去重，不读取广场快照或冒充播放流水。"""

import asyncio
import base64
import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Literal

from fastapi import HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field, SecretStr

from .external_watch import ExternalWatchClient, canonical, timestamp
from .media import safe_poster_path, sanitize_media
from .peers import PeerError
from .timeline import order_key
from .upstream import UpstreamError


def now():
    return datetime.now(UTC).isoformat()


def preserve_verified_media(media, previous):
    """同一来源条目暂缺元数据时保留已验证身份；作品或季集变化不能借用旧匹配。"""
    identity = ("kind", "title", "year", "season_number", "episode_number")
    if (
        not previous
        or not canonical(previous)
        or any(media.get(key) != previous.get(key) for key in identity)
    ):
        return media
    result = dict(media)
    if not result.get("tmdb_id"):
        result["tmdb_id"] = previous["tmdb_id"]
    if canonical(result) == canonical(previous) and not result.get("poster_path"):
        result["poster_path"] = safe_poster_path(previous.get("poster_path"))
    return result


def safe_avatar(value):
    """只接受有限大小的 JPEG 缩略图；不接受 URL、SVG 或携带凭据的图片地址。"""
    if (
        not isinstance(value, str)
        or not value.startswith("data:image/jpeg;base64,")
        or len(value) > 90000
    ):
        return None
    try:
        raw = base64.b64decode(value.split(",", 1)[1], validate=True)
        return (
            value
            if 4 < len(raw) <= 64000 and raw[:3] == b"\xff\xd8\xff" and raw[-2:] == b"\xff\xd9"
            else None
        )
    except (ValueError, TypeError):
        return None


class WatchArchive:
    """每条来源事实保留原始身份；确认导入后才能进入本人档案，所有接口按用户隔离。

    后台任务通过宿主任务工厂运行，安装时不读取用户数据。请求结束不会丢失预览，
    插件卸载会取消任务；重载后将未完成任务标记中断，避免伪报导入成功。
    主程序写回由浏览器使用本人会话执行，本模块不借插件管理员权限写成员状态。
    """

    def __init__(self, store, source, settings, *, task_factory=None, external=None):
        self.store, self.source, self.settings = store, source, settings
        self.task_factory = task_factory or asyncio.create_task
        self.external = external or ExternalWatchClient(allow_private=settings.allow_private_peers)
        self.tasks = {}
        self.write_locks = {}
        with store.lock:
            store.db.executescript("""
                CREATE TABLE IF NOT EXISTS account_profiles (
                    user_id TEXT PRIMARY KEY REFERENCES users(id), server_name TEXT,
                    joined_at TEXT, avatar_data TEXT, avatar_version TEXT, updated_at TEXT
                );
                CREATE TABLE IF NOT EXISTS watch_sources (
                    id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
                    platform TEXT NOT NULL, origin TEXT NOT NULL, secret TEXT NOT NULL,
                    account_id TEXT, account_name TEXT, active INTEGER NOT NULL DEFAULT 1,
                    latest_job TEXT, synced_at TEXT, created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS watch_sources_owner ON watch_sources(user_id);
                CREATE TABLE IF NOT EXISTS watch_jobs (
                    id TEXT PRIMARY KEY,user_id TEXT NOT NULL REFERENCES users(id),source_id TEXT,
                    platform TEXT NOT NULL,status TEXT NOT NULL,
                    processed INTEGER NOT NULL DEFAULT 0,
                    error TEXT,created_at TEXT NOT NULL,finished_at TEXT
                );
                CREATE TABLE IF NOT EXISTS watch_preview (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL REFERENCES watch_jobs(id),
                    external_id TEXT NOT NULL,canonical_key TEXT,media TEXT NOT NULL,
                    watched_at TEXT,play_count INTEGER,UNIQUE(job_id,external_id)
                );
                CREATE INDEX IF NOT EXISTS watch_preview_job ON watch_preview(job_id,id);
                CREATE TABLE IF NOT EXISTS watch_facts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,user_id TEXT NOT NULL REFERENCES users(id),
                    source_id TEXT NOT NULL,platform TEXT NOT NULL,external_id TEXT NOT NULL,
                    canonical_key TEXT,media TEXT NOT NULL,watched_at TEXT,play_count INTEGER,
                    updated_at TEXT NOT NULL,UNIQUE(user_id,source_id,external_id)
                );
                CREATE TABLE IF NOT EXISTS watch_corrections (
                    user_id TEXT NOT NULL,source_id TEXT NOT NULL,external_id TEXT NOT NULL,
                    identity TEXT NOT NULL,tmdb_id INTEGER NOT NULL,poster_path TEXT,
                    PRIMARY KEY(user_id,source_id,external_id)
                );
                CREATE INDEX IF NOT EXISTS watch_facts_owner ON watch_facts(user_id,canonical_key);
            """)
            for table, column, definition in (
                ("watch_jobs", "result", "TEXT"),
                ("watch_jobs", "content_hash", "TEXT"),
                ("watch_preview", "change_kind", "TEXT"),
            ):
                if column not in {r[1] for r in store.db.execute(f"PRAGMA table_info({table})")}:
                    store.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
            columns = {row[1] for row in store.db.execute("PRAGMA table_info(watch_sources)")}
            if "account_selector" not in columns:
                store.db.execute(
                    "ALTER TABLE watch_sources ADD COLUMN account_selector TEXT NOT NULL DEFAULT ''"
                )
            store.execute(
                "UPDATE watch_jobs SET status='interrupted',error='插件更新中断了读取，请重新预览' "
                "WHERE status='reading'"
            )

    def write_lock(self, source, external_id):
        """手动与后台同步串行核验同一账号条目，避免同时补标增加播放次数。"""
        key = (
            source["platform"],
            self.store.decrypt(source["origin"]),
            source["account_id"],
            external_id,
        )
        return self.write_locks.setdefault(key, asyncio.Lock())

    async def close(self):
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def job(self, job_id, user_id):
        job = self.store.one("SELECT * FROM watch_jobs WHERE id=? AND user_id=?", (job_id, user_id))
        if not job:
            raise HTTPException(404, "没有这次本人的导入任务")
        return job

    def source_for(self, source_id, user_id):
        row = self.store.one(
            "SELECT * FROM watch_sources WHERE id=? AND user_id=?", (source_id, user_id)
        )
        if not row or not row["active"]:
            raise HTTPException(404, "这个数据连接已断开或不属于当前账号")
        return row

    def check_job(self, job):
        if job["source_id"]:
            source = self.source_for(job["source_id"], job["user_id"])
            if source["latest_job"] != job["id"]:
                raise HTTPException(409, "已有更新的读取任务，请使用最新预览")

    def corrected(self, owner, source, external_id, media):
        """只沿用本人对同一来源条目的确认；条目身份变化或来源已有编号时不覆盖。"""
        row = self.store.one(
            "SELECT * FROM watch_corrections WHERE user_id=? AND source_id=? AND external_id=?",
            (owner, source, str(external_id)),
        )
        identity = json.dumps([media.get(k) for k in ("kind", "title", "year")], ensure_ascii=False)
        if row and row["identity"] == identity and not media.get("tmdb_id"):
            return {**media, "tmdb_id": row["tmdb_id"], "poster_path": row["poster_path"]}
        return media

    def records(self, owner, *, private=True):
        facts = self.store.all("SELECT * FROM watch_facts WHERE user_id=?", (owner,))
        groups = {}
        for row in sorted(facts, key=order_key, reverse=True):
            key = row["canonical_key"] or (row["source_id"], row["external_id"])
            if key not in groups:
                groups[key] = {**row, "sources": []}
            elif not json.loads(groups[key]["media"]).get("poster_path"):
                previous_poster = safe_poster_path(json.loads(row["media"]).get("poster_path"))
                if previous_poster:
                    groups[key]["media"] = json.dumps(
                        {
                            **json.loads(groups[key]["media"]),
                            "poster_path": previous_poster,
                        },
                        ensure_ascii=False,
                    )
            if row["platform"] not in groups[key]["sources"]:
                groups[key]["sources"].append(row["platform"])
        policy = getattr(self.store, "display_policy", None)
        allowed = (
            policy.guard(owner)
            if policy and (not private or policy.private_hidden(owner))
            else lambda _: True
        )
        return [
            {
                "id": row["id"],
                "media": json.loads(row["media"]),
                "watched_at": row["watched_at"],
                "sources": row["sources"],
                "matched": bool(row["canonical_key"]),
                **(
                    {
                        "pending_series_key": hashlib.sha256(
                            json.dumps(
                                [
                                    owner,
                                    row["source_id"],
                                    *[
                                        json.loads(row["media"]).get(k)
                                        for k in ("kind", "title", "year")
                                    ],
                                ],
                                ensure_ascii=False,
                            ).encode()
                        ).hexdigest()
                    }
                    if private and not row["canonical_key"]
                    else {}
                ),
            }
            for row in groups.values()
            if allowed(json.loads(row["media"]))
        ]

    def shareable_history(self, owner):
        return [
            {
                "id": -row["id"],
                "media": row["media"],
                "started_at": None,
                "ended_at": row["watched_at"],
                "completed": True,
            }
            for row in self.records(owner, private=False)
        ]

    def stats(self, user_id):
        rows = self.store.all(
            "SELECT DISTINCT canonical_key,media FROM watch_facts WHERE user_id=? "
            "AND canonical_key IS NOT NULL",
            (user_id,),
        )
        policy = getattr(self.store, "display_policy", None)
        allowed = (
            policy.guard(user_id)
            if policy and policy.statistics_excluded(user_id)
            else lambda value: True
        )
        movies, episodes, shows = set(), set(), set()
        for row in rows:
            item = json.loads(row["media"])
            if not allowed(item):
                continue
            if item.get("kind") == "movie":
                movies.add(row["canonical_key"])
            else:
                episodes.add(row["canonical_key"])
                shows.add(item["tmdb_id"])
        return {
            "movies": len(movies),
            "shows": len(shows),
            "episodes": len(episodes),
            "pending": sum(
                allowed(json.loads(row["media"]))
                for row in self.store.all(
                    "SELECT media FROM watch_facts WHERE user_id=? AND canonical_key IS NULL",
                    (user_id,),
                )
            ),
        }

    def preview_summary(self, job):
        counts = self.store.one(
            "SELECT COUNT(*) AS total,COUNT(DISTINCT canonical_key) AS "
            "matched,SUM(canonical_key IS NULL) AS pending FROM watch_preview WHERE"
            " job_id=?",
            (job["id"],),
        )
        existing = self.store.one(
            "SELECT COUNT(DISTINCT p.canonical_key) AS n FROM watch_preview p WHERE"
            " job_id=? AND EXISTS(SELECT 1 FROM watch_facts f WHERE f.user_id=? AND"
            " f.canonical_key=p.canonical_key)",
            (job["id"], job["user_id"]),
        )["n"]
        saved = json.loads(job.get("result") or "null")
        can_commit = job["status"] == "preview"
        try:
            self.check_job(job)
        except HTTPException:
            can_commit = False
        return {
            "created_at": job["created_at"],
            "finished_at": job["finished_at"],
            "can_commit": can_commit,
            "result": saved,
            "result_available": saved is not None,
            "id": job["id"],
            "platform": job["platform"],
            "status": job["status"],
            "processed": job["processed"],
            "error": job["error"],
            "total": counts["total"],
            "matched": counts["matched"],
            "pending": counts["pending"] or 0,
            "already_known": saved["already_known"] if saved else existing,
            "new_unique": saved["new_unique"] if saved else counts["matched"] - existing,
        }

    def content_hash(self, job):
        """只比较来源内容，不包含任务编号、读取时间或 JSON 字段排列。旧任务懒加载缓存。"""
        if job.get("content_hash"):
            return job["content_hash"]
        rows = self.store.all(
            "SELECT external_id,canonical_key,media,watched_at,play_count FROM watch_preview "
            "WHERE job_id=? ORDER BY external_id",
            (job["id"],),
        )
        for row in rows:
            row["media"] = json.loads(row["media"])
        value = hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()
        if job["status"] == "complete":
            self.store.execute(
                "UPDATE watch_jobs SET content_hash=? WHERE id=?", (value, job["id"])
            )
        return value

    def commit(self, job):
        self.check_job(job)
        if job["status"] == "complete":
            return
        if job["status"] != "preview":
            raise HTTPException(409, "读取尚未完成，或这份预览已失效")
        with self.store.lock:
            # 在途预览也须沿用后来确认的匹配，避免旧预览覆盖本人修正。
            for row in self.store.all(
                "SELECT id,external_id,media FROM watch_preview WHERE job_id=? "
                "AND canonical_key IS NULL",
                (job["id"],),
            ):
                media = self.corrected(
                    job["user_id"],
                    job["source_id"] or "movieclaw",
                    row["external_id"],
                    json.loads(row["media"]),
                )
                self.store.db.execute(
                    "UPDATE watch_preview SET canonical_key=?,media=? WHERE id=?",
                    (
                        canonical(media),
                        json.dumps(media, ensure_ascii=False),
                        row["id"],
                    ),
                )
            # 提交时再核对最新事实，避免在途旧预览覆盖读取后确认的身份与海报。
            rows = self.store.all(
                "SELECT p.*,f.id AS fact_id,f.canonical_key AS old_key,f.media AS old_media,"
                "f.watched_at AS old_date,f.play_count AS old_count FROM watch_preview p "
                "LEFT JOIN watch_facts f ON f.user_id=? AND f.source_id=? "
                "AND f.external_id=p.external_id WHERE p.job_id=?",
                (job["user_id"], job["source_id"] or "movieclaw", job["id"]),
            )
            changes_by_id = []
            for row in rows:
                media = preserve_verified_media(
                    json.loads(row["media"]),
                    json.loads(row["old_media"]) if row["old_media"] else None,
                )
                if media != json.loads(row["media"]):
                    row.update(
                        media=json.dumps(media, ensure_ascii=False),
                        canonical_key=canonical(media),
                    )
                    self.store.db.execute(
                        "UPDATE watch_preview SET canonical_key=?,media=? WHERE id=?",
                        (row["canonical_key"], row["media"], row["id"]),
                    )
                change = "new" if row["fact_id"] is None else "updated"
                if row["fact_id"] is not None and (
                    row["canonical_key"] == row["old_key"]
                    and json.loads(row["media"]) == json.loads(row["old_media"])
                    and (row["watched_at"] or row["old_date"]) == row["old_date"]
                    and row["play_count"] == row["old_count"]
                ):
                    change = "unchanged"
                changes_by_id.append((change, row["id"]))
            self.store.db.executemany(
                "UPDATE watch_preview SET change_kind=? WHERE id=?", changes_by_id
            )
            changes = self.store.all(
                "SELECT change_kind,COUNT(*) AS n FROM watch_preview WHERE "
                "job_id=? GROUP BY change_kind",
                (job["id"],),
            )
            summary = self.preview_summary(self.job(job["id"], job["user_id"]))
            outcome = {
                k: summary[k]
                for k in ("total", "matched", "pending", "new_unique", "already_known")
            }
            outcome.update(
                new_records=sum(r["n"] for r in changes if r["change_kind"] == "new"),
                updated_records=sum(r["n"] for r in changes if r["change_kind"] == "updated"),
                unchanged_records=sum(r["n"] for r in changes if r["change_kind"] == "unchanged"),
            )
            # 相同来源条目只更新一次，跨来源相同 TMDB 作品在统计和列表层聚合。
            self.store.db.execute(
                """INSERT INTO watch_facts(user_id,source_id,platform,external_id,
                    canonical_key,media,watched_at,play_count,updated_at)
                SELECT ?,?,?,external_id,canonical_key,media,watched_at,play_count,?
                    FROM watch_preview WHERE job_id=? AND change_kind IN ('new','updated')
                ON CONFLICT(user_id,source_id,external_id)
                    DO UPDATE SET canonical_key=excluded.canonical_key,
                media=excluded.media,watched_at=COALESCE(excluded.watched_at,watch_facts.watched_at),play_count=excluded.play_count,updated_at=excluded.updated_at""",
                (
                    job["user_id"],
                    job["source_id"] or "movieclaw",
                    job["platform"],
                    now(),
                    job["id"],
                ),
            )
            self.store.db.execute(
                "UPDATE watch_jobs SET status='complete',finished_at=?,result=?,content_hash=? "
                "WHERE id=?",
                (now(), json.dumps(outcome), self.content_hash(job), job["id"]),
            )
            if job["source_id"]:
                self.store.db.execute(
                    "UPDATE watch_sources SET synced_at=? WHERE id=?",
                    (now(), job["source_id"]),
                )
            self.store.db.commit()

    def start(self, user, source_id=None, *, automatic=False):
        # 同一个账号同一个来源只允许一项在途读取，重复点击直接返回原任务。
        active = self.store.one(
            "SELECT * FROM watch_jobs WHERE user_id=? AND source_id IS ? AND status='reading'",
            (user["id"], source_id),
        )
        if active:
            return active["id"]
        if automatic and source_id is None:
            latest = self.store.one(
                "SELECT id FROM watch_jobs WHERE user_id=? AND source_id IS NULL "
                "AND status='complete' AND finished_at>=? ORDER BY finished_at DESC LIMIT 1",
                (user["id"], (datetime.now(UTC) - timedelta(minutes=5)).isoformat()),
            )
            if latest:
                return latest["id"]
        source = self.source_for(source_id, user["id"]) if source_id else None
        job_id = str(uuid.uuid4())
        self.store.execute(
            "INSERT INTO "
            "watch_jobs(id,user_id,source_id,platform,status,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (
                job_id,
                user["id"],
                source_id,
                source["platform"] if source else "movieclaw",
                "reading",
                now(),
            ),
        )
        if source:
            self.store.execute(
                "UPDATE watch_sources SET latest_job=? WHERE id=?", (job_id, source_id)
            )
        task = self.task_factory(self.read(job_id, user))
        self.tasks[job_id] = task
        task.add_done_callback(lambda _: self.tasks.pop(job_id, None))
        return job_id

    async def read(self, job_id, user):
        try:
            job = self.job(job_id, user["id"])
            if job["source_id"]:
                source = self.source_for(job["source_id"], user["id"])

                def progress(n):
                    self.check_job(job)
                    self.store.execute("UPDATE watch_jobs SET processed=? WHERE id=?", (n, job_id))

                result = await self.external.read(
                    source["platform"],
                    self.store.decrypt(source["origin"]),
                    self.store.decrypt(source["secret"]),
                    progress,
                    **(
                        {"account": source["account_id"] or source["account_selector"]}
                        if source["platform"] == "emby"
                        else {}
                    ),
                )
                self.check_job(job)
                if source["account_id"] and source["account_id"] != result["account_id"]:
                    raise PeerError("外部账号已变化，请新建连接，避免合并不同人的记录")
                self.store.execute(
                    "UPDATE watch_sources SET account_id=?,account_name=? WHERE id=?",
                    (result["account_id"], result["account_name"], source["id"]),
                )
                records = result["records"]
                for row in records:
                    row["media"] = self.corrected(
                        user["id"], source["id"], row["external_id"], row["media"]
                    )
                if getattr(self.store, "display_policy", None):
                    self.store.display_policy.remember(
                        user["id"], source["id"], records, result.get("libraries")
                    )
            else:
                records = await self.source.archive(self.store.decrypt(user["token"]))
                for row in records:
                    row["media"] = self.corrected(
                        user["id"], "movieclaw", row["external_id"], row["media"]
                    )
                if getattr(self.store, "display_policy", None):
                    self.store.display_policy.remember(user["id"], "movieclaw", records)
            self.check_job(job)
            with self.store.lock:
                previous = {
                    row["external_id"]: json.loads(row["media"])
                    for row in self.store.all(
                        "SELECT external_id,media FROM watch_facts WHERE user_id=? AND source_id=? "
                        "AND canonical_key IS NOT NULL",
                        (user["id"], job["source_id"] or "movieclaw"),
                    )
                }
                for row in records:
                    media = sanitize_media(row["media"])
                    media.pop("media_item_id", None)
                    media = preserve_verified_media(
                        media, previous.get(str(row["external_id"])[:160])
                    )
                    self.store.db.execute(
                        "INSERT OR REPLACE INTO watch_preview(job_id,external_id,canonical_key,"
                        "media,watched_at,play_count) VALUES(?,?,?,?,?,?)",
                        (
                            job_id,
                            str(row["external_id"])[:160],
                            canonical(media),
                            json.dumps(media, ensure_ascii=False),
                            timestamp(row.get("watched_at")),
                            row.get("play_count"),
                        ),
                    )
                self.store.db.execute(
                    "UPDATE watch_jobs SET status='preview',processed=? WHERE id=?",
                    (len(records), job_id),
                )
                self.store.db.commit()
            if not job["source_id"]:
                self.commit(self.job(job_id, user["id"]))
        except asyncio.CancelledError:
            self.store.execute(
                "UPDATE watch_jobs SET status='interrupted',error='读取已停止，可以重新开始' WHERE "
                "id=? AND status='reading'",
                (job_id,),
            )
            raise
        except (PeerError, UpstreamError, HTTPException) as exc:
            message = exc.detail if isinstance(exc, HTTPException) else str(exc)
            self.store.execute(
                "UPDATE watch_jobs SET status='failed',error=? WHERE id=?",
                (message[:300], job_id),
            )
        except Exception:
            self.store.execute(
                "UPDATE watch_jobs SET "
                "status='failed',error='数据格式不兼容，已停止读取且未导入，请检查来源版本' "
                "WHERE id=?",
                (job_id,),
            )


def register_watch_archive(
    app, store, user_for, source, settings, *, task_factory=None, external=None
):
    archive = WatchArchive(store, source, settings, task_factory=task_factory, external=external)
    app.state.watch_archive = archive

    class AvatarInput(BaseModel):
        data: str | None = Field(default=None, max_length=90000)
        version: str = Field(default="", max_length=100)

    class ConnectionInput(BaseModel):
        platform: Literal["emby", "plex"]
        url: str = Field(min_length=1, max_length=500)
        token: SecretStr = Field(min_length=1, max_length=4096)
        emby_user: str = Field(default="", max_length=80)

    class EmbyAccountInput(BaseModel):
        emby_user: str = Field(min_length=1, max_length=80)
        consent: bool = Field(strict=True)

    class ConsentInput(BaseModel):
        consent: bool = Field(strict=True)

    class MatchInput(BaseModel):
        tmdb_id: int = Field(ge=1, lt=2**53)
        kind: Literal["movie", "tv"]
        season_number: int | None = Field(default=None, ge=0, le=10000)
        episode_number: int | None = Field(default=None, ge=0, le=100000)

    class ArchiveMatchInput(BaseModel):
        tmdb_id: int = Field(strict=True, ge=1, lt=2**53)
        kind: Literal["movie", "tv"]
        poster_path: str | None = Field(default=None, max_length=220)
        all_episodes: bool = Field(default=False, strict=True)
        consent: bool = Field(strict=True)

    @app.put("/api/watch/records/{record_id}/match")
    async def match_archive_record(record_id: int, payload: ArchiveMatchInput, request: Request):
        """确认本人待匹配足迹；不写三平台已看状态，不改观看时间和原始季集号。"""
        user = user_for(request)
        if not payload.consent:
            raise HTTPException(422, "请先核对并确认作品身份")
        poster = safe_poster_path(payload.poster_path)
        if payload.poster_path is not None and poster is None:
            raise HTTPException(422, "海报必须是 TMDB 安全相对路径")
        row = store.one(
            "SELECT * FROM watch_facts WHERE id=? AND user_id=?", (record_id, user["id"])
        )
        if not row:
            raise HTTPException(404, "没有这条本人的观看足迹")
        if row["canonical_key"]:
            raise HTTPException(409, "这条记录已匹配，请刷新足迹")
        original = json.loads(row["media"])
        if payload.kind != original.get("kind"):
            raise HTTPException(422, "电影与剧集类型不一致")
        rows = [row]
        if payload.all_episodes and payload.kind == "tv":
            rows = store.all(
                "SELECT * FROM watch_facts WHERE user_id=? AND source_id=? "
                "AND canonical_key IS NULL",
                (user["id"], row["source_id"]),
            )
        identity = json.dumps(
            [original.get(k) for k in ("kind", "title", "year")], ensure_ascii=False
        )
        changes = []
        for item in rows:
            media = json.loads(item["media"])
            if [media.get(k) for k in ("kind", "title", "year")] != [
                original.get(k) for k in ("kind", "title", "year")
            ]:
                continue
            media.update(tmdb_id=payload.tmdb_id, poster_path=poster)
            key = canonical(media)
            if key is not None:
                changes.append((item, media, key))
        if not changes:
            raise HTTPException(422, "缺少有效季号或集号，不能确认单集身份")
        policy = getattr(store, "display_policy", None)
        with store.lock:
            store.db.execute("BEGIN")
            try:
                for item, media, key in changes:
                    store.db.execute(
                        "UPDATE watch_facts SET canonical_key=?,media=? WHERE id=?",
                        (key, json.dumps(media, ensure_ascii=False), item["id"]),
                    )
                    store.db.execute(
                        "INSERT OR REPLACE INTO watch_corrections VALUES(?,?,?,?,?,?)",
                        (
                            user["id"],
                            item["source_id"],
                            item["external_id"],
                            identity,
                            payload.tmdb_id,
                            poster,
                        ),
                    )
                    if policy:
                        member = store.one(
                            "SELECT libraries_json FROM display_memberships WHERE user_id=? "
                            "AND source_id=? AND external_id=?",
                            (user["id"], item["source_id"], item["external_id"]),
                        )
                        if member:
                            policy.remember(
                                user["id"],
                                item["source_id"],
                                [
                                    {
                                        "external_id": item["external_id"],
                                        "media": media,
                                        "library_ids": json.loads(member["libraries_json"]),
                                    }
                                ],
                            )
                store.db.commit()
            except BaseException:
                store.db.rollback()
                if policy:
                    policy._cache.pop(user["id"], None)
                raise
        return {"ok": True, "updated": len(changes), "stats": archive.stats(user["id"])}

    @app.get("/api/watch/profile")
    async def watch_profile(request: Request):
        user = user_for(request)
        profile = (
            store.one(
                "SELECT server_name,joined_at,avatar_version FROM account_profiles WHERE user_id=?",
                (user["id"],),
            )
            or {}
        )
        native = store.one(
            "SELECT finished_at,status,error FROM watch_jobs WHERE user_id=? AND "
            "platform='movieclaw' ORDER BY created_at DESC LIMIT 1",
            (user["id"],),
        )
        return {
            "profile": {
                "id": user["id"],
                "nickname": profile.get("server_name") or user["nickname"],
                "joined_at": profile.get("joined_at"),
                "plugin_joined_at": store.plugin_joined_at(user["id"]),
                "avatar_version": profile.get("avatar_version"),
            },
            "stats": archive.stats(user["id"]),
            "display_restricted": bool(
                getattr(store, "display_policy", None)
                and store.display_policy.configured(user["id"])
                and (
                    store.display_policy.private_hidden(user["id"])
                    or store.display_policy.statistics_excluded(user["id"])
                )
            ),
            "display_scope": {
                "history_hidden": store.display_policy.private_hidden(user["id"]),
                "statistics_excluded": store.display_policy.statistics_excluded(user["id"]),
            },
            "native_sync": native,
        }

    @app.put("/api/watch/avatar")
    async def sync_account_avatar(payload: AvatarInput, request: Request):
        user = user_for(request)
        data = safe_avatar(payload.data) if payload.data is not None else None
        if payload.data is not None and data is None:
            raise HTTPException(422, "请同步主程序的 JPEG 缩略头像，图片不能超过 64KB")
        version = hashlib.sha256(data.encode()).hexdigest()[:24] if data else ""
        store.execute(
            "INSERT INTO "
            "account_profiles(user_id,avatar_data,avatar_version,updated_at) "
            "VALUES(?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET avatar_data=exclude"
            "d.avatar_data,avatar_version=excluded.avatar_version,updated_at=exclud"
            "ed.updated_at",
            (user["id"], data, version, now()),
        )
        return {"avatar_version": version, "avatar_data": data}

    @app.get("/api/watch/avatar/{user_id}")
    async def read_account_avatar(user_id: str, request: Request):
        viewer = user_for(request)
        if not app.state.community.can_interact(viewer["id"], user_id):
            raise HTTPException(404, "没有可查看的头像")
        profile = store.one("SELECT avatar_data FROM account_profiles WHERE user_id=?", (user_id,))
        value = safe_avatar(profile["avatar_data"]) if profile else None
        if not value:
            raise HTTPException(404, "该用户尚未同步主程序头像")
        return Response(
            base64.b64decode(value.split(",", 1)[1]),
            media_type="image/jpeg",
            headers={"Cache-Control": "private, no-cache", "X-Content-Type-Options": "nosniff"},
        )

    @app.get("/api/watch/records")
    async def watch_records(
        request: Request,
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=30, ge=1, le=200),
    ):
        user = user_for(request)
        rows = archive.records(user["id"])[offset : offset + limit + 1]
        return {
            "records": rows[:limit],
            "next_offset": offset + limit if len(rows) > limit else None,
        }

    @app.post("/api/watch/refresh", status_code=202)
    async def refresh_watch_archive(request: Request, automatic: bool = False):
        return {"id": archive.start(user_for(request), automatic=automatic)}

    @app.get("/api/watch/connections")
    async def watch_connections(request: Request):
        user = user_for(request)
        return {
            "connections": store.all(
                "SELECT id,platform,account_name,synced_at,latest_job,"
                "(platform='emby' AND COALESCE(account_id,'')='') AS needs_account FROM "
                "watch_sources WHERE user_id=? AND active=1 ORDER BY created_at DESC",
                (user["id"],),
            )
        }

    @app.post("/api/watch/connections", status_code=202)
    async def connect_watch_source(payload: ConnectionInput, request: Request):
        user = user_for(request)
        url = archive.external.validate(payload.url)
        if any(ord(char) < 33 or ord(char) > 126 for char in payload.token.get_secret_value()):
            raise HTTPException(422, "访问令牌不应包含空格或换行")
        if payload.platform == "emby" and not payload.emby_user.strip():
            raise HTTPException(422, "Emby 连接需要填写本人用户名或用户 ID；API 密钥不绑定账号")
        source_id = str(uuid.uuid4())
        store.execute(
            "INSERT INTO "
            "watch_sources(id,user_id,platform,origin,secret,created_at,account_selector) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                source_id,
                user["id"],
                payload.platform,
                store.encrypt(url),
                store.encrypt(payload.token.get_secret_value()),
                now(),
                payload.emby_user.strip() if payload.platform == "emby" else "",
            ),
        )
        return {"id": archive.start(user, source_id), "source_id": source_id}

    @app.put("/api/watch/connections/{source_id}/account", status_code=202)
    async def bind_emby_account(source_id: str, payload: EmbyAccountInput, request: Request):
        user = user_for(request)
        source = archive.source_for(source_id, user["id"])
        if source["platform"] != "emby":
            raise HTTPException(422, "此功能只用于 Emby 账号绑定")
        if not payload.consent or not payload.emby_user.strip():
            raise HTTPException(422, "请确认要绑定的本人 Emby 账号")
        if source["account_id"] or source["synced_at"]:
            raise HTTPException(409, "此连接已绑定账号，请新建连接更换账号，避免混合不同人的记录")
        reading = store.one(
            "SELECT 1 FROM watch_jobs WHERE source_id=? AND status='reading'", (source_id,)
        )
        if reading:
            raise HTTPException(409, "读取尚在进行，请等本次结束后再指定账号")
        store.execute(
            "UPDATE watch_sources SET account_selector=? WHERE id=? AND user_id=?",
            (payload.emby_user.strip(), source_id, user["id"]),
        )
        return {"id": archive.start(user, source_id)}

    @app.post("/api/watch/connections/{source_id}/preview", status_code=202)
    async def preview_watch_source(source_id: str, request: Request):
        return {"id": archive.start(user_for(request), source_id)}

    @app.delete("/api/watch/connections/{source_id}")
    async def disconnect_watch_source(source_id: str, request: Request, erase: bool = False):
        user = user_for(request)
        archive.source_for(source_id, user["id"])
        store.execute(
            "UPDATE watch_sources SET active=0,secret='',origin='' WHERE id=?", (source_id,)
        )
        jobs = store.all(
            "SELECT id FROM watch_jobs WHERE source_id=? AND status='reading'", (source_id,)
        )
        for job in jobs:
            if job["id"] in archive.tasks:
                archive.tasks[job["id"]].cancel()
        if erase:
            store.execute(
                "DELETE FROM watch_corrections WHERE user_id=? AND source_id=?",
                (user["id"], source_id),
            )
            store.execute(
                "DELETE FROM watch_facts WHERE user_id=? AND source_id=?", (user["id"], source_id)
            )
        return {"ok": True}

    @app.get("/api/watch/jobs/{job_id}")
    async def watch_job_status(job_id: str, request: Request):
        return archive.preview_summary(archive.job(job_id, user_for(request)["id"]))

    @app.get("/api/watch/jobs/{job_id}/records")
    async def watch_job_records(
        job_id: str,
        request: Request,
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=30, ge=1, le=200),
        matched_only: bool = False,
        match_state: Literal["all", "matched", "pending"] = "all",
        change: Literal["all", "new", "updated", "unchanged"] = "all",
        query: str = Query(default="", max_length=100),
    ):
        job = archive.job(job_id, user_for(request)["id"])
        if job["source_id"]:
            archive.source_for(job["source_id"], job["user_id"])
        rows = store.all(
            "SELECT * FROM watch_preview WHERE job_id=? AND (?=0 OR canonical_key IS NOT NULL) "
            "AND (?='all' OR (?='matched' AND canonical_key IS NOT NULL) OR "
            "(?='pending' AND canonical_key IS NULL)) "
            "AND (?='all' OR change_kind=?) AND "
            "instr(lower(json_extract(media,'$.title')),lower(?))>0 "
            "ORDER BY id LIMIT ? OFFSET ?",
            (
                job_id,
                matched_only,
                match_state,
                match_state,
                match_state,
                change,
                change,
                query.strip(),
                limit + 1,
                offset,
            ),
        )
        return {
            "records": [
                {
                    "id": row["id"],
                    "media": json.loads(row["media"]),
                    "watched_at": row["watched_at"],
                    "matched": bool(row["canonical_key"]),
                    "change_kind": row["change_kind"],
                }
                for row in rows[:limit]
            ],
            "next_offset": offset + limit if len(rows) > limit else None,
        }

    @app.put("/api/watch/jobs/{job_id}/records/{record_id}/match")
    async def match_watch_record(
        job_id: str, record_id: int, payload: MatchInput, request: Request
    ):
        job = archive.job(job_id, user_for(request)["id"])
        archive.check_job(job)
        if job["status"] != "preview":
            raise HTTPException(409, "仅能校正尚未确认的导入预览")
        row = store.one(
            "SELECT media FROM watch_preview WHERE id=? AND job_id=?", (record_id, job_id)
        )
        if not row:
            raise HTTPException(404, "没有这条本人的预览记录")
        media = json.loads(row["media"])
        if payload.kind != media.get("kind"):
            raise HTTPException(422, "电影与剧集类型不一致，请重新选择")
        media["tmdb_id"] = payload.tmdb_id
        for key in ("season_number", "episode_number"):
            if getattr(payload, key) is not None:
                media[key] = getattr(payload, key)
        key = canonical(media)
        if key is None:
            raise HTTPException(422, "剧集需要确认电视剧身份、季号和集号")
        store.execute(
            "UPDATE watch_preview SET media=?,canonical_key=? WHERE id=?",
            (json.dumps(media, ensure_ascii=False), key, record_id),
        )
        return {"ok": True}

    @app.post("/api/watch/jobs/{job_id}/commit")
    async def commit_watch_import(job_id: str, payload: ConsentInput, request: Request):
        user = user_for(request)
        if not payload.consent:
            raise HTTPException(422, "请先确认导入到本人的私人观影档案")
        archive.commit(archive.job(job_id, user["id"]))
        return {"ok": True, "stats": archive.stats(user["id"])}

    return archive
