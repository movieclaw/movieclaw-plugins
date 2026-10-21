"""本人导入与手动对齐任务：持久结果、执行进度和只读重核，不创建后台写入循环。"""

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Literal

from fastapi import HTTPException, Query, Request
from pydantic import BaseModel, Field


def stamp():
    return datetime.now(UTC).isoformat()


def execution_view(store, job):
    execution = json.loads(job.get("execution") or "null")
    if not execution:
        return None
    result = dict(execution)
    if result["status"] == "running" and datetime.fromisoformat(
        result["updated_at"]
    ) < datetime.now(UTC) - timedelta(minutes=2):
        result["status"] = "interrupted"
    rows = store.all(
        "SELECT source_id,execution_state,COUNT(*) AS count FROM alignment_targets "
        "WHERE job_id=? AND execution_id=? GROUP BY source_id,execution_state",
        (job["id"], result["id"]),
    )
    result["counts"] = rows
    result["processed"] = sum(
        r["count"] for r in rows if r["execution_state"] in {"done", "skipped", "failed"}
    )
    result["remaining"] = max(0, result["total"] - result["processed"])
    result["uncertain"] = sum(
        r["count"]
        for r in rows
        if r["execution_state"] == "writing" and result["status"] != "running"
    )
    current = store.one(
        "SELECT media,source_id FROM alignment_targets WHERE job_id=? AND "
        "execution_id=? AND "
        "execution_state='writing' ORDER BY id LIMIT 1",
        (job["id"], result["id"]),
    )
    result["current"] = (
        {"media": json.loads(current["media"]), "source_id": current["source_id"]}
        if current and result["status"] == "running"
        else None
    )
    return result


def authorize_execution(store, job, execution_id, target_id):
    """把每一项提交绑定到已确认的本轮选项；旧客户端不能插入其他窗口的执行。"""
    execution = execution_view(store, job)
    if execution_id is None:
        if execution and execution["status"] == "running":
            raise HTTPException(409, "本任务正在其他执行窗口处理")
        return
    if not execution or execution["id"] != execution_id or execution["status"] != "running":
        raise HTTPException(409, "本轮执行已停止，请重新选择并确认")
    if not store.one(
        "SELECT 1 FROM alignment_targets WHERE job_id=? AND id=? AND execution_id=?",
        (job["id"], target_id, execution_id),
    ):
        raise HTTPException(422, "该条目未包含在本轮确认中")


def begin_attempt(store, job, execution_id, target_id):
    authorize_execution(store, job, execution_id, target_id)
    store.execute(
        "UPDATE alignment_targets SET "
        "attempts=attempts+1,last_started_at=?,execution_state=CASE WHEN "
        "execution_id=? THEN "
        "'writing' ELSE execution_state END WHERE id=? AND job_id=?",
        (stamp(), execution_id, target_id, job["id"]),
    )


def finish_attempt(store, job, execution_id, target_id, state):
    store.execute(
        "UPDATE alignment_targets SET "
        "last_checked_at=?,last_finished_at=?,execution_state=CASE WHEN "
        "execution_id=? THEN ? "
        "ELSE execution_state END WHERE id=? AND job_id=?",
        (stamp(), stamp(), execution_id, state, target_id, job["id"]),
    )


def collapse_imports(jobs, archive, owner):
    """按本人、连接和完整来源内容合并重复核对；保留首次、实际变更和异常任务。

    旧日志不能倒推实际更新量，只在相邻成功读取的完整内容完全一致时折叠。
    分页之前合并；原始任务及明细保留，可随时展开。
    """
    imported = {
        r["id"]: r for r in archive.store.all("SELECT * FROM watch_jobs WHERE user_id=?", (owner,))
    }
    result, previous = [], {}
    for row in sorted(jobs, key=lambda row: (row["created_at"], row["id"])):
        if row["kind"] != "import":
            result.append(dict(row))
            continue
        job = imported[row["id"]]
        key = (job["source_id"], job["platform"])
        if job["status"] != "complete":
            previous.pop(key, None)
            result.append(dict(row))
            continue
        digest = archive.content_hash(job)
        outcome = json.loads(job["result"] or "null")
        actual_change = (
            outcome
            and "unchanged_records" in outcome
            and (outcome["new_records"] or outcome["updated_records"])
        )
        last = previous.get(key)
        if last and digest == last["hash"] and not actual_change:
            group = last.get("group")
            if group is None:
                group = {
                    **row,
                    "repeat_count": 0,
                    "first_repeated_at": row["created_at"],
                    "same_as_previous": True,
                }
                last["group"] = group
                result.append(group)
            group.update(row)
            group["repeat_count"] += 1
        else:
            previous[key] = {"hash": digest}
            result.append(dict(row))
    return sorted(
        result,
        key=lambda row: (bool(row.get("active")), row["created_at"], row["id"]),
        reverse=True,
    )


def register_task_center(app, store, user_for, archive, alignment):
    """结构升级仅作用于插件私有表；历史新增数量不做推测，重启不重放写操作。"""
    for table, additions in {
        "alignment_jobs": {"execution": "TEXT", "phase": "TEXT"},
        "alignment_targets": {
            "execution_id": "TEXT",
            "execution_state": "TEXT",
            "attempts": "INTEGER NOT NULL DEFAULT 0",
            "last_checked_at": "TEXT",
            "last_started_at": "TEXT",
            "last_finished_at": "TEXT",
        },
    }.items():
        columns = {r["name"] for r in store.all(f"PRAGMA table_info({table})")}
        for name, definition in additions.items():
            if name not in columns:
                store.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
    for job in store.all("SELECT id,execution FROM alignment_jobs WHERE execution IS NOT NULL"):
        value = json.loads(job["execution"])
        if value.get("status") == "running":
            value.update(status="interrupted", finished_at=stamp())
            store.execute(
                "UPDATE alignment_jobs SET execution=? WHERE id=?", (json.dumps(value), job["id"])
            )

    class Selected(BaseModel):
        target_ids: list[int] = Field(min_length=1, max_length=100000)
        consent: bool = Field(strict=True)

    class ExecutionAction(BaseModel):
        action: Literal["heartbeat", "finish", "stop"]

    @app.get("/api/watch/tasks")
    async def watch_tasks(
        request: Request,
        kind: Literal["all", "import", "alignment", "auto_sync", "resume"] = "all",
        offset: int = Query(0, ge=0),
        limit: int = Query(20, ge=1, le=50),
        collapse: bool = True,
    ):
        owner = user_for(request)["id"]
        jobs = store.all(
            "SELECT id,'import' AS kind,created_at,status='reading' AS active "
            "FROM watch_jobs WHERE user_id=? "
            "UNION ALL "
            "SELECT id,'alignment' AS kind,created_at,(status='reading' OR "
            "COALESCE(json_extract(execution,'$.status')='running',0)) AS active "
            "FROM alignment_jobs WHERE "
            "user_id=? UNION ALL "
            "SELECT id,'auto_sync' AS kind,created_at,status='running' AS active "
            "FROM auto_sync_runs WHERE "
            "user_id=? UNION ALL "
            "SELECT id,'resume' AS kind,created_at,status='running' AS active "
            "FROM resume_runs WHERE user_id=? ORDER BY active DESC,created_at DESC,id DESC",
            (owner, owner, owner, owner),
        )
        jobs = [r for r in jobs if kind == "all" or r["kind"] == kind]
        original_count = len(jobs)
        if collapse:
            jobs = collapse_imports(jobs, archive, owner)
        result = []
        for row in jobs[offset : offset + limit]:
            if row["kind"] == "import":
                job = archive.job(row["id"], owner)
                summary = archive.preview_summary(job)
                source = (
                    store.one(
                        "SELECT account_name FROM watch_sources WHERE id=? AND user_id=?",
                        (job["source_id"], owner),
                    )
                    if job["source_id"]
                    else None
                )
                result.append(
                    {
                        **summary,
                        "kind": "import",
                        "repeat_count": row.get("repeat_count", 0),
                        "same_as_previous": row.get("same_as_previous", False),
                        "first_repeated_at": row.get("first_repeated_at"),
                        "created_at": job["created_at"],
                        "finished_at": job["finished_at"],
                        "account_name": source["account_name"] if source else None,
                    }
                )
            elif row["kind"] == "resume":
                run = store.one(
                    "SELECT * FROM resume_runs WHERE id=? AND user_id=?", (row["id"], owner)
                )
                result.append({**app.state.resume_sync.view_run(run), "kind": "resume"})
            elif row["kind"] == "auto_sync":
                run = store.one(
                    "SELECT * FROM auto_sync_runs WHERE id=? AND user_id=?", (row["id"], owner)
                )
                result.append({**app.state.auto_sync.run_view(run), "kind": "auto_sync"})
            else:
                job = alignment.job(row["id"], owner, historical=True)
                result.append(
                    {**alignment.view(job), "kind": "alignment", "created_at": job["created_at"]}
                )
        return {
            "tasks": result,
            "hidden_count": original_count - len(jobs),
            "total": len(jobs),
            "next_offset": offset + limit if len(jobs) > offset + limit else None,
        }

    @app.get("/api/watch/alignment/{number}/report")
    async def alignment_report(number: str, request: Request):
        job = alignment.job(number, user_for(request)["id"], historical=True)
        reason = None
        try:
            alignment.job(number, job["user_id"], ready=True)
            alignment.validate(job)
        except HTTPException as exc:
            reason = str(exc.detail)
        config = json.loads(job["config"])
        sources = []
        for key in config.get("sources", {}):
            source = store.one(
                "SELECT id,platform,account_name FROM watch_sources WHERE id=? AND user_id=?",
                (key, job["user_id"]),
            )
            if source:
                sources.append(source)
        return {
            **alignment.view(job),
            "created_at": job["created_at"],
            "writable": reason is None,
            "write_blocked_reason": reason,
            "sources": sources,
            "participants": list(config.get("sources", {}))
            + (["movieclaw"] if config.get("native") else []),
        }

    @app.post("/api/watch/alignment/{number}/execution")
    async def start_alignment_execution(number: str, payload: Selected, request: Request):
        job = alignment.job(number, user_for(request)["id"], ready=True)
        alignment.validate(job)
        if not payload.consent:
            raise HTTPException(422, "请确认本轮同步选项")
        execution = execution_view(store, job)
        if execution and execution["status"] == "running":
            raise HTTPException(409, "此任务已有正在执行的窗口")
        ids = set(payload.target_ids)
        rows = store.all("SELECT id,state FROM alignment_targets WHERE job_id=?", (number,))
        eligible = {r["id"] for r in rows if r["state"] in {"pending", "failed"}}
        if not ids <= eligible:
            raise HTTPException(422, "包含不可执行或不属于本任务的条目，请重新核对")
        execution = {
            "id": str(uuid.uuid4()),
            "total": len(ids),
            "status": "running",
            "started_at": stamp(),
            "updated_at": stamp(),
            "finished_at": None,
        }
        with store.lock:
            store.db.execute("BEGIN")
            try:
                store.db.execute(
                    "UPDATE alignment_jobs SET execution=? WHERE id=?",
                    (json.dumps(execution), number),
                )
                store.db.executemany(
                    "UPDATE alignment_targets SET "
                    "execution_id=?,execution_state='queued' WHERE job_id=? "
                    "AND id=?",
                    [(execution["id"], number, key) for key in ids],
                )
                store.db.commit()
            except BaseException:
                store.db.rollback()
                raise
        return execution_view(store, alignment.job(number, job["user_id"], historical=True))

    @app.post("/api/watch/alignment/{number}/execution/{execution_id}")
    async def update_alignment_execution(
        number: str, execution_id: str, payload: ExecutionAction, request: Request
    ):
        job = alignment.job(number, user_for(request)["id"], historical=True)
        execution = json.loads(job["execution"] or "null")
        if not execution or execution["id"] != execution_id:
            raise HTTPException(404, "没有本轮执行记录")
        if payload.action == "finish":
            if execution_view(store, job)["remaining"]:
                raise HTTPException(409, "仍有未处理条目，请核对结果或停止本轮")
            execution.update(status="completed", finished_at=stamp())
        elif payload.action == "stop":
            if execution["status"] == "running":
                execution.update(status="stopped", finished_at=stamp())
        elif execution["status"] != "running":
            raise HTTPException(409, "本轮执行已停止")
        execution["updated_at"] = stamp()
        store.execute(
            "UPDATE alignment_jobs SET execution=? WHERE id=?", (json.dumps(execution), number)
        )
        return execution_view(store, alignment.job(number, job["user_id"], historical=True))

    @app.post("/api/watch/alignment/{number}/recheck", status_code=202)
    async def recheck_alignment(number: str, payload: Selected, request: Request):
        # 只生成新预览，不回写。目标限定在本人旧任务中明确选择的项目。
        from .watch_alignment import fingerprint, source_pin

        old = alignment.job(number, user_for(request)["id"], historical=True)
        if not payload.consent:
            raise HTTPException(422, "请选择需要重新核对的条目")
        execution = execution_view(store, old)
        if execution and execution["status"] == "running":
            raise HTTPException(409, "本轮仍在执行，请完成或停止后再重新核对")
        ids = set(payload.target_ids)
        rows = [
            r
            for r in store.all("SELECT * FROM alignment_targets WHERE job_id=?", (number,))
            if r["id"] in ids
        ]
        if len(rows) != len(ids):
            raise HTTPException(422, "包含不属于本人任务的条目")
        selection, seeds, pins = {}, {}, {}
        previous = json.loads(old["config"])
        for row in rows:
            selection.setdefault(row["source_id"], []).append(row["canonical_key"])
            seeds[row["canonical_key"]] = {
                "canonical_key": row["canonical_key"],
                "media": json.loads(row["media"]),
            }
            if row["source_id"] != "movieclaw":
                source = archive.source_for(row["source_id"], old["user_id"])
                if previous.get("sources", {}).get(row["source_id"]) != source_pin(source):
                    raise HTTPException(409, "连接账号已变化，请新建完整预览")
                pins[row["source_id"]] = source_pin(source)
        config = {
            "sources": pins,
            "version": 1,
            "native": "movieclaw" in selection,
            "scope": fingerprint(alignment.scope(old["user_id"])),
            "selection": selection,
            "parent_id": number,
        }
        fresh = str(uuid.uuid4())
        store.execute(
            ("INSERT INTO alignment_jobs(id,user_id,status,config,created_at) VALUES(?,?,?,?,?)"),
            (fresh, old["user_id"], "reading", json.dumps(config), stamp()),
        )
        user = user_for(request)
        key = "alignment:" + fresh
        task = archive.task_factory(
            alignment.build(
                fresh, user, list(pins), [], config["native"], requested=list(seeds.values())
            )
        )
        archive.tasks[key] = task
        task.add_done_callback(lambda _: archive.tasks.pop(key, None))
        return {"id": fresh}
