"""本人续播同步：首次只建基线，按真实位置变化判断新旧，保护活跃播放和版本差异。"""

import asyncio
import json
import logging
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Literal

from fastapi import HTTPException, Query, Request
from pydantic import BaseModel, Field

from .auto_sync import Stopped
from .external_alignment import inspect_item
from .media import sanitize_media
from .peers import PeerError
from .watch_alignment import fingerprint, source_pin, sync_key


def stamp():
    return datetime.now(UTC).isoformat()


def state(value):
    return {k: value.get(k) for k in ("played", "position_ms", "duration_ms", "edition")}


def valid(value):
    return (
        value.get("played") is False
        and type(value.get("position_ms")) is int
        and type(value.get("duration_ms")) is int
        and 0 < value["position_ms"] < value["duration_ms"] * 0.85
        and value["duration_ms"] < 86400000
    )


def compatible(a, b):
    return (
        a.get("version_count", 1) in (None, 1)
        and b.get("version_count", 1) in (None, 1)
        and type(a.get("duration_ms")) is int
        and type(b.get("duration_ms")) is int
        and min(a["duration_ms"], b["duration_ms"]) > 0
        and abs(a["duration_ms"] - b["duration_ms"])
        <= min(5000, min(a["duration_ms"], b["duration_ms"]) * 0.01)
        and str(a.get("edition") or "").strip().casefold()
        == str(b.get("edition") or "").strip().casefold()
    )


class ResumeOptions(BaseModel):
    model_config = {"extra": "forbid"}
    enabled: bool = Field(default=False, strict=True)
    consent: bool = Field(default=False, strict=True)
    interval_minutes: Literal[1, 5, 15] = 5
    scope_token: str = Field(default="", max_length=64)


class ResumeSync:
    """每次写入前持久化来源事件和目标意图；响应丢失不自动重放同一事件。"""

    def __init__(self, auto, user_for):
        self.auto, self.store, self.archive, self.user_for = (
            auto,
            auto.store,
            auto.archive,
            user_for,
        )
        self.running, self.poller = {}, None
        self.logger = logging.getLogger(__name__)
        self.store.db.executescript("""
            CREATE TABLE IF NOT EXISTS resume_settings(user_id TEXT PRIMARY KEY,
                enabled INTEGER NOT NULL DEFAULT 0,revision INTEGER NOT NULL DEFAULT 0,
                config TEXT NOT NULL,next_at TEXT,error TEXT);
            CREATE TABLE IF NOT EXISTS resume_observations(user_id TEXT NOT NULL,
                source_id TEXT NOT NULL,pin TEXT NOT NULL,external_id TEXT NOT NULL,
                canonical_key TEXT NOT NULL,snapshot TEXT NOT NULL,changed REAL NOT NULL,
                event TEXT NOT NULL,PRIMARY KEY(user_id,source_id,pin,external_id));
            CREATE TABLE IF NOT EXISTS resume_ledger(user_id TEXT NOT NULL,
                source_id TEXT NOT NULL,pin TEXT NOT NULL,external_id TEXT NOT NULL,
                event TEXT NOT NULL,position INTEGER NOT NULL,state TEXT NOT NULL,
                canonical_key TEXT NOT NULL,
                PRIMARY KEY(user_id,source_id,pin,external_id,event));
            CREATE TABLE IF NOT EXISTS resume_runs(id TEXT PRIMARY KEY,user_id TEXT NOT NULL,
                status TEXT NOT NULL,stage TEXT NOT NULL,source_id TEXT,
                total INTEGER NOT NULL DEFAULT 0,processed INTEGER NOT NULL DEFAULT 0,
                read_count INTEGER NOT NULL DEFAULT 0,summary TEXT NOT NULL DEFAULT '{}',
                error TEXT,created_at TEXT NOT NULL,finished_at TEXT);
            CREATE TABLE IF NOT EXISTS resume_targets(id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,source_id TEXT NOT NULL,media TEXT NOT NULL,
                state TEXT NOT NULL,message TEXT NOT NULL DEFAULT '',
                external_id TEXT,event TEXT,pin TEXT);
            UPDATE resume_runs SET status='interrupted',stage='finished',
                finished_at=strftime('%Y-%m-%dT%H:%M:%f+00:00','now'),
                error='运行环境重载中断，已提交位置下轮只回读核验' WHERE status='running';
            UPDATE resume_targets SET state='uncertain' WHERE state='writing';
        """)

    def start(self):
        self.poller = self.archive.task_factory(self.poll())

    async def close(self):
        tasks = [*self.running.values(), *([self.poller] if self.poller else [])]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.poller = None

    def guard(self, owner, revision):
        row = self.store.one("SELECT * FROM resume_settings WHERE user_id=?", (owner,))
        if not row or not row["enabled"] or row["revision"] != revision:
            raise Stopped()
        config = json.loads(row["config"])
        for selected in config["sources"]:
            try:
                source = self.auto.source_for(selected["source_id"], owner)
            except HTTPException:
                raise Stopped() from None
            if source_pin(source) != selected["pin"]:
                raise Stopped()
        return config

    async def identity(self, owner, revision):
        user = self.store.one("SELECT * FROM users WHERE id=?", (owner,))
        current = await self.archive.source.identity(self.store.decrypt(user["token"]))
        if (current["username"], current["role"], current["member_id"]) != (
            user["username"],
            user["role"],
            user["member_id"],
        ):
            raise Stopped()
        return self.guard(owner, revision)

    def view_run(self, row):
        row = dict(row)
        row.pop("user_id")
        row["remaining"] = max(0, row["total"] - row["processed"])
        row["summary"] = json.loads(row["summary"])
        row["counts"] = self.store.all(
            (
                "SELECT source_id,state,COUNT(*) AS count FROM resume_targets WHERE "
                "run_id=? GROUP BY source_id,state"
            ),
            (row["id"],),
        )
        current = (
            self.store.one(
                "SELECT media FROM resume_targets WHERE run_id=? AND state='writing' "
                "ORDER BY id LIMIT 1",
                (row["id"],),
            )
            if row["status"] == "running" and row["stage"] == "writing"
            else None
        )
        row["current_media"] = json.loads(current["media"]) if current else None
        return row

    def view(self, owner):
        row = self.store.one("SELECT * FROM resume_settings WHERE user_id=?", (owner,))
        config = json.loads(row["config"]) if row else {"sources": [], "interval_minutes": 5}
        auto_setting = self.auto.settings(owner)
        candidate = json.loads(auto_setting["config"])["sources"] if auto_setting else []
        return {
            "scope_token": fingerprint(candidate),
            "candidate_sources": [{k: v for k, v in s.items() if k != "pin"} for s in candidate],
            "enabled": bool(row and row["enabled"]),
            "interval_minutes": config["interval_minutes"],
            "sources": [{k: v for k, v in s.items() if k != "pin"} for s in config["sources"]],
            "next_at": row["next_at"] if row else None,
            "error": row["error"] if row else None,
            "runs": [
                self.view_run(r)
                for r in self.store.all(
                    "SELECT * FROM resume_runs WHERE user_id=? ORDER BY created_at DESC LIMIT 5",
                    (owner,),
                )
            ],
        }

    async def save(self, owner, payload):
        setting = self.auto.settings(owner)
        selections = json.loads(setting["config"])["sources"] if setting else []
        if payload.enabled:
            if payload.scope_token != fingerprint(selections):
                raise HTTPException(409, "同步范围已改变，请刷新设置后核对并确认")
            if not payload.consent or len(selections) < 2:
                raise HTTPException(422, "请先保存至少两个同步连接及范围，并确认续播覆盖规则")
            if not any(
                a["source_id"] != b["source_id"]
                and a["direction"] in {"read", "both"}
                and b["direction"] in {"write", "both"}
                for a in selections
                for b in selections
            ):
                raise HTTPException(422, "没有可互通的读取及接收方向")
            for s in selections:
                if source_pin(self.auto.source_for(s["source_id"], owner)) != s["pin"]:
                    raise HTTPException(409, "账号绑定已改变，请先重新保存同步连接")
        task = self.running.get(owner)
        self.store.execute(
            "UPDATE resume_settings SET enabled=0,revision=revision+1 WHERE user_id=?", (owner,)
        )
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        config = {"sources": selections, "interval_minutes": payload.interval_minutes}
        self.store.execute(
            (
                "INSERT INTO resume_settings(user_id,enabled,config,next_at) "
                "VALUES(?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET "
                "enabled=excluded.enabled,config=excluded.config,next_at=excluded.next_at,revision=resume_settings.revision+1,error=NULL"
            ),
            (owner, payload.enabled, json.dumps(config), stamp() if payload.enabled else None),
        )
        # 每次明确保存后重新建立基线，不把停用期间旧位置猜作刚发生的观看。
        self.store.execute("DELETE FROM resume_observations WHERE user_id=?", (owner,))
        return self.view(owner)

    def kick(self, owner):
        row = self.store.one(
            "SELECT revision FROM resume_settings WHERE user_id=? AND enabled=1", (owner,)
        )
        if not row:
            raise HTTPException(409, "请先开启续播同步")
        if owner in self.running and not self.running[owner].done():
            return
        task = self.archive.task_factory(self.run(owner, row["revision"]))
        self.running[owner] = task
        task.add_done_callback(
            lambda t: self.running.pop(owner, None) if self.running.get(owner) is t else None
        )

    async def poll(self):
        while True:
            try:
                for row in self.store.all(
                    (
                        "SELECT user_id FROM resume_settings WHERE enabled=1 AND (next_at IS NULL "
                        "OR next_at<=?)"
                    ),
                    (stamp(),),
                ):
                    self.kick(row["user_id"])
            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger.warning("续播同步调度未完成，将稍后重试")
            await asyncio.sleep(10)

    def update(self, number, **values):
        self.store.execute(
            "UPDATE resume_runs SET " + ",".join(k + "=?" for k in values) + " WHERE id=?",
            (*values.values(), number),
        )

    async def inspect(self, owner, revision, selected, row):
        self.guard(owner, revision)
        source = self.auto.source_for(selected["source_id"], owner)
        if source["platform"] == "movieclaw":
            result = await self.auto.native.inspect(owner, row, include_resume=True)
        else:
            result = await inspect_item(
                self.archive.external,
                source,
                self.store.decrypt(source["origin"]),
                self.store.decrypt(source["secret"]),
                row["external_id"],
                row["media"],
                include_resume=True,
            )
        self.guard(owner, revision)
        known = {
            r["library_id"]
            for r in self.store.all(
                "SELECT library_id FROM display_libraries WHERE user_id=? AND source_id=?",
                (owner, source["id"]),
            )
        } | set(selected["library_ids"])
        if not result["library_ids"] & set(selected["library_ids"]) or (
            result["library_ids"] & known
        ) - set(selected["library_ids"]):
            raise PeerError("条目已不属于续播同步范围")
        return result

    async def active(self, owner, source):
        if source["platform"] == "movieclaw":
            return await self.auto.native.active_items(owner)
        return await self.archive.external.active_items(
            source, self.store.decrypt(source["origin"]), self.store.decrypt(source["secret"])
        )

    async def transfer(self, owner, revision, selected, row, evidence, target):
        witness_selection, witness, event = evidence
        source = self.auto.source_for(selected["source_id"], owner)
        ledger = (owner, source["id"], source_pin(source), row["external_id"], event)
        async with self.archive.write_lock(source, row["external_id"]):
            await self.identity(owner, revision)
            current = await self.inspect(owner, revision, selected, row)
            if current.get("played") is True:
                return "skipped", "目标已看，不覆盖"
            if not compatible(witness, current):
                return "skipped", "版本、时长或剪辑版不一致"
            if current.get("position_ms") == witness["position_ms"]:
                self.store.execute(
                    (
                        "UPDATE resume_ledger SET state='done' WHERE user_id=? AND source_id=? AND "
                        "pin=? AND external_id=? AND event=?"
                    ),
                    ledger,
                )
                return "skipped", "目标已是此续播位置"
            # 批量季集接口没有片长。未读取的片长不当成状态变化；真实片长及版本
            # 已由上方 compatible(witness, current) 的即时回读严格核对。
            observed = state(row)
            if any(
                current.get(k) != v
                for k, v in observed.items()
                if not (k == "duration_ms" and v is None)
            ):
                return "skipped", "目标位置在本轮读取后发生变化，下轮重新核对"
            if self.store.one(
                (
                    "SELECT 1 FROM resume_ledger WHERE user_id=? "
                    "AND source_id=? AND pin=? AND "
                    "external_id=? AND event=?"
                ),
                ledger,
            ):
                return "uncertain", "本次位置已有提交意图，仅回读核验，不重复覆盖"
            check = await self.inspect(owner, revision, witness_selection, witness)
            if not valid(check) or state(check) != state(witness) or not compatible(check, current):
                return "skipped", "来源位置已变化，下轮重新核对"
            if row["external_id"] in await self.active(owner, source):
                return "skipped", "目标正在播放或暂停中，保护当前会话"
            await self.identity(owner, revision)
            self.store.execute(
                "INSERT INTO resume_ledger VALUES(?,?,?,?,?,?,'submitted',?)",
                (*ledger, witness["position_ms"], sync_key(row["media"])),
            )
            self.store.execute("UPDATE resume_targets SET state='writing' WHERE id=?", (target,))
            if source["platform"] == "movieclaw":
                await self.auto.native.position(owner, row, witness["position_ms"])
            else:
                await self.archive.external.set_position(
                    source,
                    self.store.decrypt(source["origin"]),
                    self.store.decrypt(source["secret"]),
                    row["external_id"],
                    witness["position_ms"],
                    current["duration_ms"],
                    current,
                )
            after = await self.inspect(owner, revision, selected, row)
            if after.get("position_ms") != witness["position_ms"] or after.get("played") is True:
                return "uncertain", "平台未确认续播位置，下轮仅回读核验"
            self.store.execute(
                (
                    "UPDATE resume_ledger SET state='done' WHERE user_id=? AND source_id=? AND "
                    "pin=? AND external_id=? AND event=?"
                ),
                ledger,
            )
            # 记录自己的回写，下一轮不能把这个回声再当成新的观看事件。
            self.store.execute(
                (
                    "UPDATE resume_observations SET snapshot=? WHERE user_id=? AND source_id=? "
                    "AND pin=? AND external_id=?"
                ),
                (
                    json.dumps(state(after)),
                    owner,
                    source["id"],
                    source_pin(source),
                    row["external_id"],
                ),
            )
            return "done", f"已确认续播到 {witness['position_ms'] // 1000} 秒"

    async def reconcile(self, owner, revision, selected, row):
        source = self.auto.source_for(selected["source_id"], owner)
        bindings = (owner, source["id"], source_pin(source), row["external_id"])
        pending = self.store.all(
            "SELECT * FROM resume_ledger WHERE user_id=? AND "
            "source_id=? AND pin=? AND external_id=? AND "
            "canonical_key=? AND state='submitted' AND position=?",
            (*bindings, sync_key(row["media"]), row.get("position_ms")),
        )
        if not pending or row.get("played") is not False:
            return
        async with self.archive.write_lock(source, row["external_id"]):
            checked = await self.inspect(owner, revision, selected, row)
            if checked.get("played") is not False or checked.get("position_ms") != row.get(
                "position_ms"
            ):
                return
            for ledger in pending:
                self.store.execute(
                    "UPDATE resume_ledger SET state='done' WHERE "
                    "user_id=? AND source_id=? AND pin=? AND "
                    "external_id=? AND event=?",
                    (*bindings, ledger["event"]),
                )
                self.store.execute(
                    "UPDATE resume_targets SET "
                    "state='done',message='后续回读已确认续播位置' WHERE source_id=?"
                    " AND pin=? AND external_id=? AND event=? AND "
                    "state='uncertain' AND run_id IN (SELECT id FROM "
                    "resume_runs WHERE user_id=?)",
                    (source["id"], source_pin(source), row["external_id"], ledger["event"], owner),
                )
        self.store.execute(
            "UPDATE resume_runs SET processed=(SELECT COUNT(*) "
            "FROM resume_targets WHERE run_id=resume_runs.id AND "
            "state IN ('done','skipped','failed','uncertain')) "
            "WHERE user_id=? AND status!='running'",
            (owner,),
        )
        self.store.execute(
            "UPDATE resume_runs SET status='done',error=NULL "
            "WHERE user_id=? AND status IN "
            "('attention','interrupted','paused') AND total>0 AND"
            " total=(SELECT COUNT(*) FROM resume_targets WHERE "
            "run_id=resume_runs.id AND state IN "
            "('done','skipped')) AND NOT EXISTS (SELECT 1 FROM "
            "json_each(resume_runs.summary) WHERE "
            "json_extract(value,'$.error') IS NOT NULL)",
            (owner,),
        )
        return True

    def save_observations(self, rows):
        """每百条基线一次事务，避免逐条 fsync 阻塞前台；不把平台写请求放入事务。"""
        if not rows:
            return
        with self.store.lock:
            self.store.db.execute("BEGIN IMMEDIATE")
            try:
                self.store.db.executemany(
                    "INSERT INTO resume_observations VALUES(?,?,?,?,?,?,?,?) ON "
                    "CONFLICT(user_id,source_id,pin,external_id) DO UPDATE SET "
                    "canonical_key=excluded.canonical_key,snapshot=excluded.snapshot,"
                    "changed=excluded.changed,event=excluded.event",
                    rows,
                )
                self.store.db.execute("COMMIT")
            except Exception:
                self.store.db.execute("ROLLBACK")
                raise

    async def run(self, owner, revision):
        number = str(uuid.uuid4())
        summary = {}
        status, error = "done", None
        self.store.execute(
            (
                "INSERT INTO resume_runs(id,user_id,status,stage,created_at) "
                "VALUES(?,?,'running','reading',?)"
            ),
            (number, owner, stamp()),
        )
        try:
            config = await self.identity(owner, revision)
            observed = time.time()
            inventories = {}
            witnesses = {}
            for selected in config["sources"]:
                source = self.auto.source_for(selected["source_id"], owner)
                self.update(number, stage="reading", source_id=source["id"], read_count=0)

                def progress(count, source_id=source["id"]):
                    self.guard(owner, revision)
                    self.update(number, read_count=count, source_id=source_id)

                try:
                    result = await self.auto.read_inventory(owner, source, selected, progress)
                    if result["account_id"] != source["account_id"]:
                        raise PeerError("本人账号绑定发生变化")
                    self.guard(owner, revision)
                    self.update(number, stage="matching")
                    inventory = {}
                    for index, row in enumerate(result["records"]):
                        if index % 100 == 0:
                            self.guard(owner, revision)
                            await asyncio.sleep(0)
                        key = sync_key(row["media"])
                        if key and self.auto.allowed(row, selected):
                            inventory.setdefault(key, []).append(row)
                    summary[source["id"]] = {
                        "read": len(result["records"]),
                        "unmatched": len(result["records"]) - sum(map(len, inventory.values())),
                    }
                    inventories[source["id"]] = inventory
                    observations = []
                    for index, (key, rows) in enumerate(inventory.items()):
                        if index % 100 == 0:
                            self.save_observations(observations)
                            observations.clear()
                            self.guard(owner, revision)
                            await asyncio.sleep(0)
                        if len(rows) != 1:
                            continue
                        row = rows[0]
                        identity = (owner, source["id"], source_pin(source), row["external_id"])
                        old = self.store.one(
                            (
                                "SELECT * FROM resume_observations "
                                "WHERE user_id=? AND source_id=? AND "
                                "pin=? AND external_id=?"
                            ),
                            identity,
                        )
                        if old and old["canonical_key"] != key:
                            old = None
                        recovered = await self.reconcile(owner, revision, selected, row)
                        value = state(row)
                        if recovered and old:
                            old["snapshot"] = json.dumps(value)
                        changed = 0 if not old else old["changed"]
                        event = "" if not old else old["event"]
                        if old and json.loads(old["snapshot"]) != value:
                            # 未确认写入的回读也必须抑制为回声；真正不同的新位置才产生事件。
                            echo = self.store.one(
                                (
                                    "SELECT 1 FROM resume_ledger WHERE user_id=? "
                                    "AND source_id=? AND pin=? AND "
                                    "external_id=? AND position=? AND state='submitted'"
                                ),
                                (*identity, row.get("position_ms")),
                            )
                            if not echo:
                                changed, event = observed, str(uuid.uuid4())
                        observations.append((*identity, key, json.dumps(value), changed, event))
                        if selected["direction"] != "write" and changed and valid(row):
                            witnesses.setdefault(key, []).append((changed, selected, row, event))
                    self.save_observations(observations)
                except (Stopped, asyncio.CancelledError):
                    raise
                except Exception as exc:
                    summary[source["id"]] = {
                        "read": 0,
                        "error": str(exc)
                        if isinstance(exc, PeerError)
                        else "续播读取失败，请核对账号及接口权限",
                    }
                self.update(number, summary=json.dumps(summary))
            work = []
            for selected in config["sources"]:
                if selected["direction"] == "read":
                    continue
                for key, candidates in witnesses.items():
                    latest = max(r[0] for r in candidates)
                    best = [r for r in candidates if r[0] == latest]
                    if len(best) != 1:
                        continue
                    _, ws, witness, event = best[0]
                    matches = inventories.get(selected["source_id"], {}).get(key, [])
                    if (
                        ws["source_id"] == selected["source_id"]
                        or len(matches) != 1
                        or matches[0].get("played") is not False
                        or matches[0].get("position_ms") == witness["position_ms"]
                    ):
                        continue
                    row = matches[0]
                    self.store.execute(
                        (
                            "INSERT INTO "
                            "resume_targets(run_id,source_id,media,state,external_id,event,pin)"
                            " "
                            "VALUES(?,?,?,'pending',?,?,?)"
                        ),
                        (
                            number,
                            selected["source_id"],
                            json.dumps(sanitize_media(row["media"])),
                            row["external_id"],
                            event,
                            selected["pin"],
                        ),
                    )
                    target = self.store.one("SELECT last_insert_rowid() AS id")["id"]
                    work.append((selected, row, (ws, witness, event), target))
            self.update(number, stage="writing", total=len(work), source_id=None)
            for index, (selected, row, evidence, target) in enumerate(work):
                self.guard(owner, revision)
                self.update(number, source_id=selected["source_id"])
                try:
                    outcome, message = await self.transfer(
                        owner, revision, selected, row, evidence, target
                    )
                except (Stopped, asyncio.CancelledError):
                    raise
                except Exception as exc:
                    submitted = (
                        self.store.one("SELECT state FROM resume_targets WHERE id=?", (target,))[
                            "state"
                        ]
                        == "writing"
                    )
                    outcome = "uncertain" if submitted else "failed"
                    message = (
                        str(exc) if isinstance(exc, PeerError) else "续播结果未确认，请核对连接"
                    )
                if outcome in {"failed", "uncertain"}:
                    status = "attention"
                self.store.execute(
                    "UPDATE resume_targets SET state=?,message=? WHERE id=?",
                    (outcome, message, target),
                )
                self.update(number, processed=index + 1)
                await asyncio.sleep(0)
            if any(s.get("error") for s in summary.values()):
                status = "attention"
        except (Stopped, asyncio.CancelledError):
            status, error = "paused", "授权或账号发生变化，本轮已停止"
            if asyncio.current_task().cancelling():
                raise
        except Exception:
            status, error = "failed", "续播核验失败，请检查连接与本人授权"
        finally:
            self.store.execute(
                (
                    "UPDATE resume_targets SET state='uncertain',"
                    "message='结果未确认，下轮仅回读' WHERE "
                    "run_id=? AND state='writing'"
                ),
                (number,),
            )
            self.store.execute(
                (
                    "UPDATE resume_targets SET state='cancelled',message='未提交' WHERE run_id=? "
                    "AND state='pending'"
                ),
                (number,),
            )
            self.update(
                number,
                status=status,
                stage="finished",
                error=error,
                summary=json.dumps(summary),
                finished_at=stamp(),
            )
            setting = self.store.one("SELECT * FROM resume_settings WHERE user_id=?", (owner,))
            if setting and setting["revision"] == revision:
                minutes = json.loads(setting["config"])["interval_minutes"]
                self.store.execute(
                    "UPDATE resume_settings SET next_at=?,error=? WHERE user_id=?",
                    ((datetime.now(UTC) + timedelta(minutes=minutes)).isoformat(), error, owner),
                )
            if status == "done" and not self.store.one(
                "SELECT 1 FROM resume_targets WHERE run_id=?", (number,)
            ):
                self.store.execute(
                    (
                        "DELETE FROM resume_runs WHERE user_id=? AND id<>? AND status='done' AND "
                        "total=0"
                    ),
                    (owner, number),
                )

    def register(self, app):
        from .sync_control import register_sync_control

        register_sync_control(app, self, self.user_for, "resume-sync")

        @app.get("/api/watch/resume-sync")
        async def resume_sync_status(request: Request):
            return self.view(self.user_for(request)["id"])

        @app.put("/api/watch/resume-sync")
        async def resume_sync_settings(payload: ResumeOptions, request: Request):
            return await self.save(self.user_for(request)["id"], payload)

        @app.post("/api/watch/resume-sync/check", status_code=202)
        async def resume_sync_check(request: Request):
            self.kick(self.user_for(request)["id"])
            return {"ok": True}

        @app.get("/api/watch/resume-sync/runs/{number}")
        async def resume_sync_report(number: str, request: Request):
            row = self.store.one(
                "SELECT * FROM resume_runs WHERE id=? AND user_id=?",
                (number, self.user_for(request)["id"]),
            )
            if not row:
                raise HTTPException(404, "没有这次本人的续播任务")
            return self.view_run(row)

        @app.get("/api/watch/resume-sync/runs/{number}/targets")
        async def resume_sync_targets(number: str, request: Request, offset: int = Query(0, ge=0)):
            await resume_sync_report(number, request)
            rows = self.store.all(
                (
                    "SELECT id,source_id,media,state,message FROM resume_targets WHERE run_id=?"
                    " ORDER BY id LIMIT 201 OFFSET ?"
                ),
                (number, offset),
            )
            return {
                "targets": [{**r, "media": json.loads(r["media"])} for r in rows[:200]],
                "next_offset": offset + 200 if len(rows) > 200 else None,
            }
