"""本人三平台已看对齐：只读差异计划、明确确认、单项核验回写与失败重试。"""

import asyncio
import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Literal

from fastapi import HTTPException, Query, Request
from pydantic import BaseModel, Field

from .display_policy import work_keys
from .external_alignment import inspect_item
from .external_watch import canonical
from .media import sanitize_media
from .peers import PeerError
from .task_center import authorize_execution, begin_attempt, execution_view, finish_attempt


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def source_pin(source):
    return fingerprint([source.get(k) for k in ("platform", "origin", "secret", "account_id")])


def sync_key(media):
    """第 0 集不是可回写的真实单集，禁止触发宿主整季哨兵或平台级联。"""
    key = canonical(media)
    return (
        key
        if media.get("kind") == "movie"
        or (type(media.get("episode_number")) is int and media["episode_number"] > 0)
        else None
    )


class Alignment:
    """计划与目标只存私有库。浏览器不给外部地址、令牌或可任意写入的目标编号。"""

    def __init__(self, store, archive):
        self.store, self.archive, self.locks = store, archive, {}
        store.db.executescript("""
            CREATE TABLE IF NOT EXISTS alignment_jobs(
                id TEXT PRIMARY KEY,user_id TEXT NOT NULL,status TEXT NOT NULL,
                config TEXT NOT NULL,data TEXT NOT NULL DEFAULT '[]',error TEXT,
                progress INTEGER NOT NULL DEFAULT 0,created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS alignment_targets(
                id INTEGER PRIMARY KEY AUTOINCREMENT,job_id TEXT NOT NULL,
                source_id TEXT NOT NULL,canonical_key TEXT NOT NULL,media TEXT NOT NULL,
                destination TEXT,state TEXT NOT NULL,message TEXT NOT NULL DEFAULT '',
                attempted INTEGER NOT NULL DEFAULT 0,
                UNIQUE(job_id,source_id,canonical_key)
            );
            CREATE INDEX IF NOT EXISTS alignment_target_job ON alignment_targets(job_id,id);
            UPDATE alignment_jobs SET status='failed',error='插件更新中断了预览，请重新开始'
                WHERE status='reading';
            UPDATE alignment_targets SET state='failed',message='上次结果未确认，请重试核验'
                WHERE state='writing';
        """)
        if "attempted" not in {
            r["name"] for r in store.all("PRAGMA table_info(alignment_targets)")
        }:
            store.execute(
                "ALTER TABLE alignment_targets ADD COLUMN attempted INTEGER NOT NULL DEFAULT 0"
            )

    def scope(self, owner):
        return self.store.all(
            "SELECT source_id,library_id,excluded FROM display_libraries WHERE user_id=? "
            "ORDER BY source_id,library_id",
            (owner,),
        )

    def job(self, number, owner, *, ready=False, historical=False):
        row = self.store.one(
            "SELECT * FROM alignment_jobs WHERE id=? AND user_id=?", (number, owner)
        )
        if not row:
            raise HTTPException(404, "没有这次本人的对齐预览")
        if not historical and datetime.fromisoformat(row["created_at"]) < datetime.now(
            UTC
        ) - timedelta(hours=1):
            raise HTTPException(409, "预览已超过一小时，请重新核对")
        if ready and row["status"] != "ready":
            raise HTTPException(409, "请先完成差异预览")
        return row

    def validate(self, job):
        config = json.loads(job["config"])
        if config.get("version") != 1:
            raise HTTPException(409, "对齐规则已更新，请重新预览")
        if fingerprint(self.scope(job["user_id"])) != config["scope"]:
            raise HTTPException(409, "媒体库范围已变化，请重新预览")
        for key, pin in config["sources"].items():
            source = self.archive.source_for(key, job["user_id"])
            if source_pin(source) != pin:
                raise HTTPException(409, "连接账号已变化，请重新预览")
        return config

    def target(self, job, source, key, media, state, destination=None, message=""):
        self.store.execute(
            "INSERT INTO alignment_targets(job_id,source_id,canonical_key,media,destination,"
            "state,message) VALUES(?,?,?,?,?,?,?) ON CONFLICT(job_id,source_id,canonical_key) "
            "DO UPDATE SET destination=excluded.destination,state=excluded.state,"
            "message=excluded.message",
            (
                job,
                source,
                key,
                json.dumps(sanitize_media(media), ensure_ascii=False),
                json.dumps(destination) if destination is not None else None,
                state,
                message,
            ),
        )

    def view(self, job):
        counts = self.store.all(
            "SELECT source_id,state,COUNT(*) AS count FROM alignment_targets WHERE job_id=? "
            "GROUP BY source_id,state",
            (job["id"],),
        )
        return {
            "id": job["id"],
            "status": job["status"],
            "error": job["error"],
            "progress": job["progress"],
            "counts": counts,
            "union_count": len(json.loads(job["data"])),
            "created_at": job["created_at"],
            "phase": json.loads(job.get("phase") or "null"),
            "execution": execution_view(self.store, job),
        }

    async def build(self, number, user, source_ids, native_records, include_native, requested=None):
        owner = user["id"]
        try:
            job = self.job(number, owner)
            inventories, entries, completed_reads = {}, [], 0
            selection = json.loads(job["config"]).get("selection")
            for source_id in source_ids:
                source = self.archive.source_for(source_id, owner)

                def progress(count, prior=completed_reads, current_source=source_id):
                    self.validate(self.job(number, owner))
                    self.store.execute(
                        "UPDATE alignment_jobs SET progress=?,phase=? WHERE id=?",
                        (
                            prior + count,
                            json.dumps(
                                {
                                    "stage": "reading",
                                    "source_id": current_source,
                                    "processed": count,
                                    "total": None,
                                }
                            ),
                            number,
                        ),
                    )

                progress(0)

                result = await self.archive.external.read(
                    source["platform"],
                    self.store.decrypt(source["origin"]),
                    self.store.decrypt(source["secret"]),
                    progress,
                    account=source["account_id"],
                    include_libraries=True,
                    only_watched=False,
                    excluded_libraries=[
                        row["library_id"]
                        for row in self.scope(owner)
                        if row["source_id"] == source_id and row["excluded"]
                    ],
                )
                if result["account_id"] != source["account_id"]:
                    raise PeerError("外部账号已变化，请重新连接本人账号")
                for row in result["records"]:
                    row["media"] = sanitize_media(row["media"])
                    row["media"].pop("media_item_id", None)
                    row["media"].pop("demo_id", None)
                self.validate(job)
                completed_reads += len(result["records"])
                inventories[source_id] = result["records"]
                entries.extend((source_id, row) for row in result["records"])
            entries.extend(("movieclaw", row) for row in native_records)
            scope = self.scope(owner)
            known = {(r["source_id"], r["library_id"]) for r in scope}
            excluded = {(r["source_id"], r["library_id"]) for r in scope if r["excluded"]}
            denied = set(self.store.display_policy.denied_keys(owner))
            for source, row in entries:
                if {(source, str(lib)) for lib in row.get("library_ids", [])} & excluded:
                    denied.update(work_keys(row))

            def allowed(source, row):
                libs = {(source, str(lib)) for lib in row.get("library_ids", [])}
                return (
                    not (set(work_keys(row)) & denied)
                    and not (libs & excluded)
                    and (not excluded or bool(libs) and libs <= known)
                )

            union = {}
            for row in self.archive.records(owner, private=False):
                key = sync_key(row["media"])
                if key and not (set(work_keys(row)) & denied):
                    union[key] = {"canonical_key": key, "media": sanitize_media(row["media"])}
            for source, row in entries:
                key = sync_key(row["media"])
                if key and row.get("played") is True and allowed(source, row):
                    union.setdefault(
                        key, {"canonical_key": key, "media": sanitize_media(row["media"])}
                    )
            if requested is not None:
                guard = self.store.display_policy.guard(owner)
                requested_keys = {r["canonical_key"] for r in requested}
                for record in requested:
                    if sync_key(record["media"]) == record["canonical_key"] and guard(
                        record["media"]
                    ):
                        union.setdefault(record["canonical_key"], record)
                union = {key: record for key, record in union.items() if key in requested_keys}
            matching_total = sum(
                len(selection.get(source, [])) if selection else len(union)
                for source in inventories
            )
            matching_done = 0
            self.store.execute(
                "UPDATE alignment_jobs SET phase=? WHERE id=?",
                (
                    json.dumps({"stage": "matching", "processed": 0, "total": matching_total}),
                    number,
                ),
            )
            if len(union) > 100000:
                raise PeerError("已看内容超过十万项，请缩小参与范围")
            for source, rows in inventories.items():
                index = {}
                for row in rows:
                    key = sync_key(row["media"])
                    if key:
                        index.setdefault(key, []).append(row)
                for key, record in union.items():
                    if selection and key not in selection.get(source, []):
                        continue
                    candidates = index.get(key, [])
                    state, destination = "missing", None
                    if len(candidates) > 1:
                        state = "ambiguous"
                    elif candidates:
                        candidate = candidates[0]
                        if not allowed(source, candidate):
                            state = "excluded"
                        elif candidate.get("played") is True:
                            state = "skipped"
                        elif candidate.get("played") is False:
                            state, destination = (
                                "pending",
                                {
                                    "external_id": candidate["external_id"],
                                    "library_ids": candidate.get("library_ids", []),
                                },
                            )
                        else:
                            state = "unavailable"
                    self.target(number, source, key, record["media"], state, destination)
                    matching_done += 1
                    if matching_done % 100 == 0:
                        self.store.execute(
                            "UPDATE alignment_jobs SET phase=? WHERE id=?",
                            (
                                json.dumps(
                                    {
                                        "stage": "matching",
                                        "processed": matching_done,
                                        "total": matching_total,
                                    }
                                ),
                                number,
                            ),
                        )
                        await asyncio.sleep(0)
            self.validate(job)
            self.store.execute(
                "UPDATE alignment_jobs SET phase=? WHERE id=?",
                (
                    json.dumps(
                        {"stage": "matching", "processed": matching_done, "total": matching_total}
                    ),
                    number,
                ),
            )
            self.store.execute(
                "UPDATE alignment_jobs SET data=?,status=? WHERE id=?",
                (
                    json.dumps(list(union.values()), ensure_ascii=False),
                    "native" if include_native else "ready",
                    number,
                ),
            )
        except asyncio.CancelledError:
            self.store.execute(
                "UPDATE alignment_jobs SET status='failed',error='预览已停止' WHERE id=?", (number,)
            )
            raise
        except Exception as exc:
            message = (
                str(exc)
                if isinstance(exc, PeerError)
                else (
                    exc.detail if isinstance(exc, HTTPException) else "预览未完成，请检查连接后重试"
                )
            )
            self.store.execute(
                "UPDATE alignment_jobs SET status='failed',error=? WHERE id=?", (message, number)
            )


def register_alignment(app, store, user_for, archive, media_model):
    service = Alignment(store, archive)
    app.state.alignment = service

    class NativeRecord(BaseModel):
        media: media_model
        played: bool = Field(strict=True)
        library_ids: list[str] = Field(default_factory=list, max_length=20)

    class Preview(BaseModel):
        source_ids: list[str] = Field(default_factory=list, max_length=10)
        include_native: bool = Field(default=True, strict=True)
        native_records: list[NativeRecord] = Field(default_factory=list, max_length=100000)

    class NativeTarget(BaseModel):
        canonical_key: str = Field(max_length=160)
        state: Literal["pending", "skipped", "missing", "ambiguous", "unavailable"]
        media_item_id: int | None = Field(default=None, ge=1, lt=2**53, strict=True)

    class NativeBatch(BaseModel):
        targets: list[NativeTarget] = Field(max_length=200)

    class Consent(BaseModel):
        consent: bool = Field(strict=True)
        execution_id: str | None = Field(default=None, max_length=40)

    class NativeResult(Consent):
        success: bool = Field(strict=True)
        already_played: bool = Field(default=False, strict=True)

    class NativePrepare(Consent):
        begin_write: bool = Field(default=False, strict=True)
        begin_attempt: bool = Field(default=False, strict=True)

    @app.get("/api/watch/alignment/config")
    async def alignment_config(request: Request):
        owner = user_for(request)["id"]
        recent = store.one(
            "SELECT id FROM alignment_jobs WHERE user_id=? AND status='ready' "
            "ORDER BY created_at DESC LIMIT 1",
            (owner,),
        )
        return {
            "recent_id": recent["id"] if recent else None,
            "sources": store.all(
                "SELECT id,platform,account_name FROM watch_sources WHERE user_id=? "
                "AND active=1 AND account_id IS NOT NULL",
                (owner,),
            ),
            "excluded_native": [
                row["library_id"]
                for row in service.scope(owner)
                if row["source_id"] == "movieclaw" and row["excluded"]
            ],
        }

    @app.post("/api/watch/alignment/preview", status_code=202)
    async def preview_alignment(payload: Preview, request: Request):
        user = user_for(request)
        sources = {
            key: source_pin(archive.source_for(key, user["id"])) for key in payload.source_ids
        }
        if not sources and not payload.include_native:
            raise HTTPException(422, "请至少选择一个参与平台")
        number = str(uuid.uuid4())
        store.execute(
            "INSERT INTO alignment_jobs(id,user_id,status,config,created_at) VALUES(?,?,?,?,?)",
            (
                number,
                user["id"],
                "reading",
                json.dumps(
                    {
                        "sources": sources,
                        "version": 1,
                        "native": payload.include_native,
                        "scope": fingerprint(service.scope(user["id"])),
                    }
                ),
                datetime.now(UTC).isoformat(),
            ),
        )
        key = "alignment:" + number
        task = archive.task_factory(
            service.build(
                number,
                user,
                list(sources),
                [r.model_dump(mode="json") for r in payload.native_records],
                payload.include_native,
            )
        )
        archive.tasks[key] = task
        task.add_done_callback(lambda _: archive.tasks.pop(key, None))
        return {"id": number}

    @app.get("/api/watch/alignment/{number}")
    async def alignment_status(number: str, request: Request):
        job = service.job(number, user_for(request)["id"])
        service.validate(job)
        return service.view(job)

    @app.get("/api/watch/alignment/{number}/records")
    async def alignment_union(number: str, request: Request, offset: int = Query(0, ge=0)):
        job = service.job(number, user_for(request)["id"], historical=True)
        data = json.loads(job["data"])
        selection = json.loads(job["config"]).get("selection")
        if selection is not None:
            data = [r for r in data if r["canonical_key"] in selection.get("movieclaw", [])]
        return {
            "records": data[offset : offset + 200],
            "next_offset": offset + 200 if len(data) > offset + 200 else None,
        }

    @app.post("/api/watch/alignment/{number}/native-targets")
    async def alignment_native_targets(number: str, payload: NativeBatch, request: Request):
        job = service.job(number, user_for(request)["id"])
        if job["status"] != "native" or not service.validate(job)["native"]:
            raise HTTPException(409, "这份预览不接受主程序目标")
        selection = json.loads(job["config"]).get("selection")
        union = {
            row["canonical_key"]: row["media"]
            for row in json.loads(job["data"])
            if selection is None or row["canonical_key"] in selection.get("movieclaw", [])
        }
        for item in payload.targets:
            if (
                item.canonical_key not in union
                or item.state == "pending"
                and not item.media_item_id
            ):
                raise HTTPException(422, "目标不属于本次已看预览")
        for item in payload.targets:
            media = union[item.canonical_key]
            target = {"media_item_id": item.media_item_id}
            if media["kind"] == "tv":
                target.update(
                    season_number=media["season_number"], episode_number=media["episode_number"]
                )
            service.target(number, "movieclaw", item.canonical_key, media, item.state, target)
        return {"ok": True}

    @app.post("/api/watch/alignment/{number}/ready")
    async def alignment_ready(number: str, request: Request):
        job = service.job(number, user_for(request)["id"])
        config = service.validate(job)
        count = store.one(
            "SELECT COUNT(*) AS n FROM alignment_targets WHERE job_id=? AND source_id='movieclaw'",
            (number,),
        )["n"]
        if (
            job["status"] != "native"
            or not config["native"]
            or count
            != (
                sum(
                    r["canonical_key"] in config["selection"].get("movieclaw", [])
                    for r in json.loads(job["data"])
                )
                if "selection" in config
                else len(json.loads(job["data"]))
            )
        ):
            raise HTTPException(409, "主程序差异尚未核对完整")
        store.execute("UPDATE alignment_jobs SET status='ready' WHERE id=?", (number,))
        return service.view(service.job(number, job["user_id"]))

    @app.get("/api/watch/alignment/{number}/targets")
    async def alignment_targets(
        number: str, request: Request, offset: int = Query(0, ge=0), source_id: str | None = None
    ):
        service.job(number, user_for(request)["id"], historical=True)
        rows = store.all(
            "SELECT * FROM alignment_targets WHERE job_id=? AND (? IS NULL OR source_id=?) "
            "ORDER BY id LIMIT 201 OFFSET ?",
            (number, source_id, source_id, offset),
        )
        return {
            "targets": [
                {
                    "id": r["id"],
                    "source_id": r["source_id"],
                    "media": json.loads(r["media"]),
                    "state": r["state"],
                    "message": r["message"],
                    "attempts": r.get("attempts", 0),
                    "last_checked_at": r.get("last_checked_at"),
                    "execution_state": r.get("execution_state"),
                    **(
                        {"native_target": json.loads(r["destination"])}
                        if r["source_id"] == "movieclaw"
                        else {}
                    ),
                }
                for r in rows[:200]
            ],
            "next_offset": offset + 200 if len(rows) > 200 else None,
        }

    @app.post("/api/watch/alignment/{number}/apply/{target_id}")
    async def apply_alignment(number: str, target_id: int, payload: Consent, request: Request):
        owner = user_for(request)["id"]
        job = service.job(number, owner, ready=True)
        if not payload.consent:
            raise HTTPException(422, "请先确认本次对齐的目标与数量")
        async with service.locks.setdefault(number, asyncio.Lock()):
            service.validate(job)
            row = store.one(
                "SELECT * FROM alignment_targets WHERE id=? AND job_id=?", (target_id, number)
            )
            if not row or row["source_id"] == "movieclaw":
                raise HTTPException(404, "没有这条外部同步目标")
            authorize_execution(store, service.job(number, owner), payload.execution_id, target_id)
            if row["state"] in {"done", "skipped"}:
                return {"state": row["state"]}
            if row["state"] not in {"pending", "failed"}:
                raise HTTPException(409, "该条目不能自动同步，请重新核对")
            source = archive.source_for(row["source_id"], owner)
            base, token = store.decrypt(source["origin"]), store.decrypt(source["secret"])
            media, target = json.loads(row["media"]), json.loads(row["destination"])
            async with archive.write_lock(source, target["external_id"]):
                begin_attempt(store, service.job(number, owner), payload.execution_id, target_id)
                store.execute(
                    "UPDATE alignment_targets SET state='writing' WHERE id=?", (target_id,)
                )
                try:
                    current = await inspect_item(
                        archive.external, source, base, token, target["external_id"], media
                    )
                    service.validate(job)
                    if set(work_keys(media)) & store.display_policy.denied_keys(owner):
                        raise PeerError("作品已被媒体库排除，请重新预览")
                    excluded = {
                        r["library_id"]
                        for r in service.scope(owner)
                        if r["source_id"] == source["id"] and r["excluded"]
                    }
                    if current["library_ids"] & excluded:
                        raise PeerError("条目已进入排除的媒体库，请重新预览")
                    if not current["library_ids"] & set(target["library_ids"]):
                        raise PeerError("条目所属媒体库已变化，请重新预览")
                    if current["played"]:
                        state = "skipped"
                    else:
                        if row["attempted"]:
                            raise PeerError(
                                "上次提交结果仍未确认，本次仅核验；请重新预览后再决定是否补标"
                            )
                        # 写前持久化意图；响应丢失时只复核状态，不盲目重放并增加平台播放次数。
                        store.execute(
                            "UPDATE alignment_targets SET attempted=1 WHERE id=?", (target_id,)
                        )
                        await archive.external.mark_played(
                            base,
                            token,
                            source["platform"],
                            source["account_id"],
                            target["external_id"],
                        )
                        checked = await inspect_item(
                            archive.external, source, base, token, target["external_id"], media
                        )
                        if not checked["played"]:
                            raise PeerError("外部服务器尚未确认已看状态，请重试核验")
                        state = "done"
                    message = ""
                except asyncio.CancelledError:
                    store.execute(
                        "UPDATE alignment_targets SET state='failed',"
                        "message='结果未确认，请重试核验' "
                        "WHERE id=?",
                        (target_id,),
                    )
                    finish_attempt(store, job, payload.execution_id, target_id, "failed")
                    raise
                except Exception as exc:
                    state = "failed"
                    message = (
                        str(exc) if isinstance(exc, PeerError) else "同步结果未确认，请重试核验"
                    )
                store.execute(
                    "UPDATE alignment_targets SET state=?,message=? WHERE id=?",
                    (state, message, target_id),
                )
                finish_attempt(store, job, payload.execution_id, target_id, state)
                return {"state": state, "message": message}

    @app.post("/api/watch/alignment/{number}/native-prepare/{target_id}")
    async def alignment_native_prepare(
        number: str, target_id: int, payload: NativePrepare, request: Request
    ):
        job = service.job(number, user_for(request)["id"], ready=True)
        service.validate(job)
        row = store.one(
            "SELECT * FROM alignment_targets WHERE id=? AND job_id=? AND source_id='movieclaw'",
            (target_id, number),
        )
        authorize_execution(
            store, service.job(number, job["user_id"]), payload.execution_id, target_id
        )
        if not payload.consent or not row or row["state"] not in {"pending", "failed", "writing"}:
            raise HTTPException(409, "请核对待同步目标并确认")
        if set(work_keys(json.loads(row["media"]))) & store.display_policy.denied_keys(
            job["user_id"]
        ):
            raise HTTPException(409, "作品已被媒体库排除，请重新预览")
        if row["state"] == "writing" and not payload.execution_id:
            raise HTTPException(409, "本条目已有执行窗口")
        if payload.begin_attempt:
            begin_attempt(store, job, payload.execution_id, target_id)
            if payload.execution_id:
                store.execute(
                    "UPDATE alignment_targets SET state='writing' WHERE id=?", (target_id,)
                )
        if payload.begin_write:
            if row["attempted"]:
                raise HTTPException(409, "上次提交结果仍未确认，请重新预览后再决定是否补标")
            store.execute("UPDATE alignment_targets SET attempted=1 WHERE id=?", (target_id,))
        return {"native_target": json.loads(row["destination"]), "media": json.loads(row["media"])}

    @app.post("/api/watch/alignment/{number}/native-result/{target_id}")
    async def alignment_native_result(
        number: str, target_id: int, payload: NativeResult, request: Request
    ):
        job = service.job(number, user_for(request)["id"], ready=True)
        service.validate(job)
        if not payload.consent:
            raise HTTPException(422, "请先确认同步")
        row = store.one(
            "SELECT * FROM alignment_targets WHERE id=? AND job_id=? AND source_id='movieclaw'",
            (target_id, number),
        )
        if payload.execution_id is None:
            authorize_execution(store, job, None, target_id)
        if payload.execution_id and row and row["execution_id"] != payload.execution_id:
            raise HTTPException(409, "执行窗口已变化，请重新核对")
        if not row or row["state"] not in {"pending", "failed", "done", "writing"}:
            raise HTTPException(409, "没有待同步的主程序目标")
        state = ("skipped" if payload.already_played else "done") if payload.success else "failed"
        message = "主程序未确认已看状态，请重新核对" if state == "failed" else ""
        store.execute(
            "UPDATE alignment_targets SET state=?,message=? WHERE id=?",
            (state, message, target_id),
        )
        finish_attempt(store, job, payload.execution_id, target_id, state)
        return {"state": state, "message": message}

    return service
