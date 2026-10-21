"""本人 Emby/Plex 后台补标：独立库白名单、可撤销授权与持久写入意图。"""

import asyncio
import json
import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Literal

from fastapi import HTTPException, Query, Request
from pydantic import BaseModel, Field

from .external_alignment import inspect_item
from .inventory_reads import InventoryReads
from .media import sanitize_media
from .peers import PeerError
from .watch_alignment import source_pin, sync_key

log = logging.getLogger(__name__)


def stamp():
    return datetime.now(UTC).isoformat()


class SyncSource(BaseModel):
    source_id: str = Field(min_length=1, max_length=64)
    direction: Literal["read", "write", "both"] = "both"
    library_ids: list[str] = Field(min_length=1, max_length=200)


class SyncSettings(BaseModel):
    enabled: bool = Field(default=False, strict=True)
    consent: bool = Field(default=False, strict=True)
    interval_minutes: Literal[15, 30, 60] = 15
    sources: list[SyncSource] = Field(default_factory=list, max_length=20)


class Stopped(Exception):
    """授权、账号或范围在执行中变化时，停止余下的写入。"""


class AutoSync:
    """后台只读实时库存作为已看证据，不用旧导入足迹或同步日志制造新观看。

    ledger 在发出写请求前落盘。响应丢失或重载后只回读核验，不自动再次补标；
    已成功写入后用户自行取消已看，也保留其决定，交由手动对齐再次确认。
    """

    def __init__(self, store, archive):
        self.store, self.archive = store, archive
        self.running, self.poller = {}, None
        self.logger = log
        self.inventory_reads = InventoryReads(archive.task_factory)
        store.db.executescript("""
            CREATE TABLE IF NOT EXISTS auto_sync_settings(
                user_id TEXT PRIMARY KEY,enabled INTEGER NOT NULL DEFAULT 0,
                revision INTEGER NOT NULL DEFAULT 0,config TEXT NOT NULL,
                next_at TEXT,last_checked_at TEXT,error TEXT
            );
            CREATE TABLE IF NOT EXISTS auto_sync_runs(
                id TEXT PRIMARY KEY,user_id TEXT NOT NULL,status TEXT NOT NULL,
                stage TEXT NOT NULL,source_id TEXT,read_count INTEGER NOT NULL DEFAULT 0,
                total INTEGER NOT NULL DEFAULT 0,processed INTEGER NOT NULL DEFAULT 0,
                summary TEXT NOT NULL DEFAULT '{}',error TEXT,created_at TEXT NOT NULL,
                finished_at TEXT
            );
            CREATE INDEX IF NOT EXISTS auto_sync_owner ON auto_sync_runs(user_id,created_at);
            CREATE TABLE IF NOT EXISTS auto_sync_targets(
                id INTEGER PRIMARY KEY AUTOINCREMENT,run_id TEXT NOT NULL,
                source_id TEXT NOT NULL,canonical_key TEXT NOT NULL,media TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'pending',message TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS auto_sync_run ON auto_sync_targets(run_id,id);
            CREATE TABLE IF NOT EXISTS auto_sync_ledger(
                user_id TEXT NOT NULL,source_id TEXT NOT NULL,pin TEXT NOT NULL,
                external_id TEXT NOT NULL,canonical_key TEXT NOT NULL,state TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(user_id,source_id,pin,external_id,canonical_key)
            );
            UPDATE auto_sync_runs SET status='interrupted',stage='finished',
                finished_at=strftime('%Y-%m-%dT%H:%M:%f+00:00','now'),
                error='运行环境重载中断了本轮；已提交项目会先回读核验'
                WHERE status='running';
            UPDATE auto_sync_targets SET state='uncertain',message='上次结果未确认，等待回读核验'
                WHERE state='writing';
        """)

    def start(self):
        if self.poller is None or self.poller.done():
            self.poller = self.archive.task_factory(self.poll())

    async def close(self):
        tasks = [*self.running.values(), *([self.poller] if self.poller else [])]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.poller = None
        await self.inventory_reads.close()

    def source_for(self, source_id, owner):
        if source_id == "movieclaw":
            if not self.native.settings.native:
                raise HTTPException(404, "此连接不可用")
            return self.native.source_for(owner)
        return self.archive.source_for(source_id, owner)

    def settings(self, owner):
        return self.store.one("SELECT * FROM auto_sync_settings WHERE user_id=?", (owner,))

    def guard(self, owner, revision):
        setting = self.settings(owner)
        if not setting or not setting["enabled"] or setting["revision"] != revision:
            raise Stopped()
        config = json.loads(setting["config"])
        for selection in config["sources"]:
            try:
                source = self.source_for(selection["source_id"], owner)
            except HTTPException:
                raise Stopped() from None
            if (
                source["platform"] not in {"emby", "plex", "movieclaw"}
                or source_pin(source) != selection["pin"]
            ):
                raise Stopped()
        return config

    async def identity(self, owner, revision):
        user = self.store.one("SELECT * FROM users WHERE id=?", (owner,))
        if not user:
            raise Stopped()
        # 不让已停用、删除或复用编号的宿主成员继续使用旧的后台授权。
        identity = await self.archive.source.identity(self.store.decrypt(user["token"]))
        if (identity.get("username"), identity.get("role"), identity.get("member_id")) != (
            user["username"],
            user["role"],
            user["member_id"],
        ):
            raise Stopped()
        return self.guard(owner, revision)

    def view(self, owner):
        setting = self.settings(owner)
        config = (
            json.loads(setting["config"]) if setting else {"sources": [], "interval_minutes": 15}
        )
        connections = []
        for source in self.store.all(
            "SELECT * FROM watch_sources WHERE user_id=? AND active=1 ORDER BY created_at", (owner,)
        ):
            if source["platform"] not in {"emby", "plex", "movieclaw"}:
                continue
            connections.append(
                {
                    "id": source["id"],
                    "platform": source["platform"],
                    "account_name": source["account_name"],
                    "bound": bool(source["account_id"]),
                    "libraries": self.store.all(
                        "SELECT library_id AS id,name FROM display_libraries "
                        "WHERE user_id=? AND source_id=? ORDER BY name",
                        (owner, source["id"]),
                    ),
                }
            )
        if hasattr(self, "native") and self.native.settings.native:
            grant = self.store.one(
                "SELECT username FROM native_sync_grants WHERE user_id=?", (owner,)
            )
            connections.append(
                {
                    "id": "movieclaw",
                    "platform": "movieclaw",
                    "account_name": grant["username"] if grant else "本人账号未配对",
                    "bound": bool(grant),
                    "libraries": self.store.all(
                        (
                            "SELECT library_id AS id,name FROM display_libraries "
                            "WHERE user_id=? AND "
                            "source_id='movieclaw' ORDER BY name"
                        ),
                        (owner,),
                    ),
                }
            )
        runs = self.history(owner)["runs"]
        return {
            "enabled": bool(setting and setting["enabled"]),
            "interval_minutes": config["interval_minutes"],
            "sources": [{k: v for k, v in s.items() if k != "pin"} for s in config["sources"]],
            "connections": connections,
            "runs": runs,
            "next_at": setting["next_at"] if setting else None,
            "last_checked_at": setting["last_checked_at"] if setting else None,
            "error": setting["error"] if setting else None,
        }

    def history(self, owner, offset=0, limit=10):
        rows = self.store.all(
            "SELECT * FROM auto_sync_runs WHERE user_id=? ORDER BY created_at DESC,id DESC "
            "LIMIT ? OFFSET ?",
            (owner, limit + 1, offset),
        )
        return {
            "runs": [self.run_view(run) for run in rows[:limit]],
            "total": self.store.one(
                "SELECT COUNT(*) AS n FROM auto_sync_runs WHERE user_id=?", (owner,)
            )["n"],
            "next_offset": offset + limit if len(rows) > limit else None,
        }

    def run_view(self, run):
        run = dict(run)
        run["summary"] = json.loads(run["summary"])
        run["remaining"] = max(0, run["total"] - run["processed"])
        run["counts"] = self.store.all(
            "SELECT source_id,state,COUNT(*) AS count FROM auto_sync_targets WHERE "
            "run_id=? GROUP BY source_id,state",
            (run["id"],),
        )
        current = (
            self.store.one(
                "SELECT media FROM auto_sync_targets WHERE run_id=? AND state='writing' "
                "ORDER BY id LIMIT 1",
                (run["id"],),
            )
            if run["status"] == "running" and run["stage"] == "writing"
            else None
        )
        run["current_media"] = json.loads(current["media"]) if current else None
        run.pop("user_id")
        return run

    async def stop(self, owner):
        self.store.execute(
            "UPDATE auto_sync_settings SET enabled=0,revision=revision+1,next_at=NULL "
            "WHERE user_id=?",
            (owner,),
        )
        task = self.running.get(owner)
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def save(self, owner, payload):
        if payload.enabled and not payload.consent:
            raise HTTPException(422, "请确认自动补标范围，开启后也会核对范围内已有的已看内容")
        ids, selections, bindings = set(), [], set()
        for choice in payload.sources:
            source = self.source_for(choice.source_id, owner)
            if choice.source_id in ids or source["platform"] not in {"emby", "plex", "movieclaw"}:
                raise HTTPException(422, "连接重复或平台不兼容")
            if not source["account_id"]:
                raise HTTPException(422, "请先验证并绑定本人账号")
            binding = (
                source["platform"],
                self.store.decrypt(source["origin"]),
                source["account_id"],
            )
            if binding in bindings:
                raise HTTPException(422, "同一服务器账号请只选择一个连接")
            known = {
                r["library_id"]
                for r in self.store.all(
                    "SELECT library_id FROM display_libraries WHERE user_id=? AND source_id=?",
                    (owner, choice.source_id),
                )
            }
            libraries = sorted(set(choice.library_ids))
            if not set(libraries) <= known:
                raise HTTPException(422, "媒体库尚未核实，请先在展示范围中刷新媒体库")
            ids.add(choice.source_id)
            bindings.add(binding)
            selections.append(
                {**choice.model_dump(), "library_ids": libraries, "pin": source_pin(source)}
            )
        if payload.enabled and (
            len(selections) < 2
            or not any(
                a["source_id"] != b["source_id"]
                and a["direction"] in {"read", "both"}
                and b["direction"] in {"write", "both"}
                for a in selections
                for b in selections
            )
        ):
            raise HTTPException(422, "至少选择两个本人连接，并设置可互通的读取与接收方向")
        await self.stop(owner)
        config = {"interval_minutes": payload.interval_minutes, "sources": selections}
        self.store.execute(
            "INSERT INTO auto_sync_settings(user_id,enabled,config,next_at) "
            "VALUES(?,?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET "
            "enabled=excluded.enabled,config=excluded.config,"
            "revision=auto_sync_settings.revision+1,next_at=excluded.next_at,error=NULL",
            (owner, int(payload.enabled), json.dumps(config), stamp() if payload.enabled else None),
        )
        return self.view(owner)

    def kick(self, owner):
        setting = self.settings(owner)
        if not setting or not setting["enabled"]:
            raise HTTPException(409, "请先在设置中开启自动已看同步")
        if owner in self.running and not self.running[owner].done():
            return
        task = self.archive.task_factory(self.run(owner, setting["revision"]))
        self.running[owner] = task
        task.add_done_callback(
            lambda finished: (
                self.running.pop(owner, None) if self.running.get(owner) is finished else None
            )
        )

    async def poll(self):
        while True:
            try:
                for row in self.store.all(
                    "SELECT user_id FROM auto_sync_settings WHERE enabled=1 AND (next_at IS "
                    "NULL OR next_at<=?)",
                    (stamp(),),
                ):
                    self.kick(row["user_id"])
            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger.warning("后台已看同步调度暂时失败，稍后重试")
            await asyncio.sleep(10)

    def update(self, run, **values):
        self.store.execute(
            "UPDATE auto_sync_runs SET " + ",".join(f"{k}=?" for k in values) + " WHERE id=?",
            (*values.values(), run),
        )

    @staticmethod
    def allowed(row, selection):
        libraries = set(map(str, row.get("library_ids", [])))
        return bool(libraries) and libraries <= set(selection["library_ids"])

    async def check_item(self, owner, revision, selection, row):
        self.guard(owner, revision)
        source = self.source_for(selection["source_id"], owner)
        if source["platform"] == "movieclaw":
            result = await self.native.inspect(owner, row)
        else:
            result = await inspect_item(
                self.archive.external,
                source,
                self.store.decrypt(source["origin"]),
                self.store.decrypt(source["secret"]),
                row["external_id"],
                row["media"],
            )
        self.guard(owner, revision)
        # Emby Ancestors 还包含普通文件夹；只把已核实的媒体库根作为范围边界。
        selected = set(selection["library_ids"])
        known = {
            r["library_id"]
            for r in self.store.all(
                "SELECT library_id FROM display_libraries WHERE user_id=? AND source_id=?",
                (owner, selection["source_id"]),
            )
        } | selected
        if not (result["library_ids"] & selected) or (result["library_ids"] & known) - selected:
            raise PeerError("条目不再属于所选同步媒体库")
        return result

    async def transfer(self, owner, revision, selection, row, evidence, run, target):
        source = self.source_for(selection["source_id"], owner)
        key = sync_key(row["media"])
        ledger_key = (owner, source["id"], source_pin(source), row["external_id"], key)
        async with self.archive.write_lock(source, row["external_id"]):
            await self.identity(owner, revision)
            current = await self.check_item(owner, revision, selection, row)
            ledger = self.store.one(
                "SELECT state FROM auto_sync_ledger WHERE user_id=? AND source_id=? AND pin=? "
                "AND external_id=? AND canonical_key=?",
                ledger_key,
            )
            if current["played"]:
                if ledger:
                    self.store.execute(
                        "UPDATE auto_sync_ledger SET state='done',updated_at=? WHERE user_id=? "
                        "AND source_id=? AND pin=? AND external_id=? AND canonical_key=?",
                        (stamp(), *ledger_key),
                    )
                return "skipped", "平台已确认已看"
            # 跨轮次和重载保持写入意图；手动对齐曾提交的条目也不能被后台盲目重放。
            manual = self.store.one(
                "SELECT 1 FROM alignment_targets t JOIN alignment_jobs j ON j.id=t.job_id "
                "WHERE j.user_id=? AND t.source_id=? AND t.canonical_key=? AND t.attempted=1 "
                "AND (json_extract(t.destination,'$.external_id')=? OR "
                "(?='movieclaw' AND json_extract(t.destination,'$.media_item_id')=?)) LIMIT 1",
                (
                    owner,
                    source["id"],
                    key,
                    row["external_id"],
                    source["id"],
                    row.get("target", {}).get("media_item_id"),
                ),
            )
            if ledger or manual:
                return "uncertain", "此条目已有提交记录；当前未标已看，请使用确认同步重新核对"
            witness_selection, witness = evidence
            if not (await self.check_item(owner, revision, witness_selection, witness))["played"]:
                return "skipped", "来源已看状态已变化，本次不补标"
            await self.identity(owner, revision)
            # 先持久化，再发请求；取消或丢失响应后不得再次自动发出写请求。
            self.store.execute(
                "INSERT INTO auto_sync_ledger VALUES(?,?,?,?,?,?,?)",
                (*ledger_key, "submitted", stamp()),
            )
            self.store.execute("UPDATE auto_sync_targets SET state='writing' WHERE id=?", (target,))
            if source["platform"] == "movieclaw":
                await self.native.mark(owner, row)
            else:
                await self.archive.external.mark_played(
                    self.store.decrypt(source["origin"]),
                    self.store.decrypt(source["secret"]),
                    source["platform"],
                    source["account_id"],
                    row["external_id"],
                )
            if not (await self.check_item(owner, revision, selection, row))["played"]:
                return "uncertain", "平台尚未确认结果，后续只回读核验"
            self.store.execute(
                "UPDATE auto_sync_ledger SET state='done',updated_at=? WHERE user_id=? "
                "AND source_id=? AND pin=? AND external_id=? AND canonical_key=?",
                (stamp(), *ledger_key),
            )
            return "done", "已回读确认"

    async def reconcile(self, owner, revision, selection, inventory):
        """对丢失响应的已提交意图只回读，补全原任务结果，不产生新的写入。"""
        for ledger in self.store.all(
            "SELECT * FROM auto_sync_ledger WHERE user_id=? AND source_id=? AND pin=? "
            "AND state='submitted'",
            (owner, selection["source_id"], selection["pin"]),
        ):
            matches = inventory.get(ledger["canonical_key"], [])
            if len(matches) != 1 or matches[0]["external_id"] != ledger["external_id"]:
                continue
            if not matches[0]["played"]:
                continue
            async with self.archive.write_lock(
                self.source_for(selection["source_id"], owner), ledger["external_id"]
            ):
                if not (await self.check_item(owner, revision, selection, matches[0]))["played"]:
                    continue
                self.store.execute(
                    "UPDATE auto_sync_ledger SET state='done',updated_at=? WHERE user_id=? "
                    "AND source_id=? AND pin=? AND external_id=? AND canonical_key=?",
                    (
                        stamp(),
                        owner,
                        selection["source_id"],
                        selection["pin"],
                        ledger["external_id"],
                        ledger["canonical_key"],
                    ),
                )
                self.store.execute(
                    "UPDATE auto_sync_targets SET state='done',message='后续回读已确认' "
                    "WHERE source_id=? AND canonical_key=? AND state='uncertain' "
                    "AND run_id IN (SELECT id FROM auto_sync_runs WHERE user_id=?)",
                    (selection["source_id"], ledger["canonical_key"], owner),
                )
        # 回读会改变旧条目结果，旧任务的进度也必须同步；未提交的取消项仍保留为剩余。
        self.store.execute(
            "UPDATE auto_sync_runs SET processed=(SELECT COUNT(*) FROM auto_sync_targets "
            "WHERE run_id=auto_sync_runs.id AND state IN "
            "('done','skipped','failed','uncertain')) "
            "WHERE user_id=? AND status!='running'",
            (owner,),
        )
        self.store.execute(
            "UPDATE auto_sync_runs SET status='done',error=NULL WHERE user_id=? "
            "AND status IN ('attention','interrupted','paused') AND total>0 "
            "AND total=(SELECT COUNT(*) FROM auto_sync_targets WHERE "
            "run_id=auto_sync_runs.id "
            "AND state IN ('done','skipped')) "
            "AND NOT EXISTS (SELECT 1 FROM json_each(auto_sync_runs.summary) "
            "WHERE json_extract(value,'$.error') IS NOT NULL)",
            (owner,),
        )

    async def read_inventory(self, owner, source, selection, progress):
        """已看与续播只合并完全相同的在途读取；不同账号、授权和范围始终隔离。"""
        selected = set(selection["library_ids"])
        known = {
            r["library_id"]
            for r in self.store.all(
                "SELECT library_id FROM display_libraries WHERE user_id=? AND source_id=?",
                (owner, source["id"]),
            )
        }
        key = (owner, source["id"], source_pin(source), tuple(sorted(selected)))

        async def load(notify):
            if source["platform"] == "movieclaw":
                return await self.native.read(owner, sorted(selected), notify)
            return await self.archive.external.read(
                source["platform"],
                self.store.decrypt(source["origin"]),
                self.store.decrypt(source["secret"]),
                notify,
                account=source["account_id"],
                include_libraries=True,
                only_watched=False,
                excluded_libraries=known - selected,
                included_libraries=sorted(selected),
            )

        return await self.inventory_reads.read(key, load, progress)

    async def run(self, owner, revision):
        run = str(uuid.uuid4())
        self.store.execute(
            "INSERT INTO auto_sync_runs(id,user_id,status,stage,created_at) "
            "VALUES(?,?,'running','reading',?)",
            (run, owner, stamp()),
        )
        status, error, summary = "done", None, {}
        try:
            config = await self.identity(owner, revision)
            inventories, witnesses = {}, {}
            for selection in config["sources"]:
                source = self.source_for(selection["source_id"], owner)
                self.update(run, source_id=source["id"], read_count=0, stage="reading")

                def progress(count):
                    self.guard(owner, revision)
                    self.update(run, read_count=count)

                try:
                    result = await self.read_inventory(owner, source, selection, progress)
                except (Stopped, asyncio.CancelledError):
                    raise
                except Exception as exc:
                    # 不使用失败分页的半份数据，也不让一台离线服务器阻塞其他连接。
                    summary[source["id"]] = {
                        "read": 0,
                        "already": 0,
                        "missing": 0,
                        "ambiguous": 0,
                        "unmatched": 0,
                        "error": str(exc)
                        if isinstance(exc, PeerError)
                        else "连接读取失败，下一轮重试",
                    }
                    self.update(run, summary=json.dumps(summary))
                    continue
                self.guard(owner, revision)
                if result["account_id"] != source["account_id"]:
                    raise PeerError("连接账号已变化，请重新保存自动同步设置")
                # 多版本/重复编号都不自动选取；未核实来源和新库也不进入同步。
                self.update(run, stage="matching")
                inventory = {}
                for index, row in enumerate(result["records"]):
                    if index % 100 == 0:
                        self.guard(owner, revision)
                        await asyncio.sleep(0)
                    key = sync_key(row["media"])
                    if key and self.allowed(row, selection) and type(row.get("played")) is bool:
                        inventory.setdefault(key, []).append(row)
                await self.reconcile(owner, revision, selection, inventory)
                inventories[source["id"]] = inventory
                summary[source["id"]] = {
                    "read": len(result["records"]),
                    "already": 0,
                    "missing": 0,
                    "ambiguous": 0,
                    "unmatched": len(result["records"]) - sum(map(len, inventory.values())),
                }
                if selection["direction"] in {"read", "both"}:
                    for key, rows in inventory.items():
                        if len(rows) == 1 and rows[0]["played"]:
                            witnesses.setdefault(key, []).append((selection, rows[0]))
            work = []
            for selection in config["sources"]:
                if selection["direction"] == "read":
                    continue
                if selection["source_id"] not in inventories:
                    continue
                inventory = inventories[selection["source_id"]]
                counts = summary[selection["source_id"]]
                for key, evidence in witnesses.items():
                    evidence = [e for e in evidence if e[0]["source_id"] != selection["source_id"]]
                    if not evidence:
                        continue
                    matches = inventory.get(key, [])
                    if len(matches) != 1:
                        counts["ambiguous" if matches else "missing"] += 1
                    elif matches[0]["played"]:
                        counts["already"] += 1
                    else:
                        row = matches[0]
                        self.store.execute(
                            "INSERT INTO auto_sync_targets(run_id,source_id,canonical_key,media) "
                            "VALUES(?,?,?,?)",
                            (
                                run,
                                selection["source_id"],
                                key,
                                json.dumps(sanitize_media(row["media"])),
                            ),
                        )
                        target = self.store.one("SELECT last_insert_rowid() AS id")["id"]
                        work.append((selection, row, evidence[0], target))
            self.update(
                run, stage="writing", source_id=None, total=len(work), summary=json.dumps(summary)
            )
            for index, (selection, row, evidence, target) in enumerate(work):
                self.guard(owner, revision)
                self.update(run, source_id=selection["source_id"])
                try:
                    state, message = await self.transfer(
                        owner, revision, selection, row, evidence, run, target
                    )
                except (Stopped, asyncio.CancelledError):
                    raise
                except Exception as exc:
                    submitted = (
                        self.store.one("SELECT state FROM auto_sync_targets WHERE id=?", (target,))[
                            "state"
                        ]
                        == "writing"
                    )
                    state = "uncertain" if submitted else "failed"
                    message = (
                        str(exc)
                        if isinstance(exc, PeerError)
                        else "同步未完成，请检查连接后重试核验"
                    )
                self.store.execute(
                    "UPDATE auto_sync_targets SET state=?,message=? WHERE id=?",
                    (state, message, target),
                )
                self.update(run, processed=index + 1)
                # 让暂停请求和其他账号任务有机会运行。
                await asyncio.sleep(0)
            if any(value.get("error") for value in summary.values()) or self.store.one(
                "SELECT 1 FROM auto_sync_targets WHERE run_id=? AND state IN "
                "('failed','uncertain')",
                (run,),
            ):
                status = "attention"
        except (Stopped, asyncio.CancelledError):
            status, error = "paused", "本轮已停止；已发出的请求会在下次核验"
            raise_cancel = asyncio.current_task().cancelling()
            if raise_cancel:
                raise
        except Exception as exc:
            status = "failed"
            error = (
                str(exc) if isinstance(exc, PeerError) else "读取或账号核验失败，请检查连接后重试"
            )
        finally:
            self.store.execute(
                "UPDATE auto_sync_targets SET state='uncertain',"
                "message='结果未确认，后续仅回读核验' "
                "WHERE run_id=? AND state='writing'",
                (run,),
            )
            self.store.execute(
                "UPDATE auto_sync_targets SET state='cancelled',message='本轮停止，未提交' WHERE "
                "run_id=? AND state='pending'",
                (run,),
            )
            self.update(
                run,
                status=status,
                stage="finished",
                error=error,
                finished_at=stamp(),
                summary=json.dumps(summary),
            )
            setting = self.settings(owner)
            if setting and setting["revision"] == revision:
                config = json.loads(setting["config"])
                next_at = (
                    (datetime.now(UTC) + timedelta(minutes=config["interval_minutes"])).isoformat()
                    if setting["enabled"]
                    else None
                )
                self.store.execute(
                    "UPDATE auto_sync_settings SET last_checked_at=?,next_at=?,error=? WHERE "
                    "user_id=?",
                    (stamp(), next_at, error, owner),
                )
            # 连续无补标的成功检查只保留最新一次，避免任务历史被空轮询刷屏。
            if status == "done" and not self.store.one(
                "SELECT 1 FROM auto_sync_targets WHERE run_id=?", (run,)
            ):
                self.store.execute(
                    "DELETE FROM auto_sync_runs WHERE user_id=? AND id<>? AND status='done' AND "
                    "total=0",
                    (owner, run),
                )


def register_auto_sync(app, store, user_for, archive):
    service = AutoSync(store, archive)
    app.state.auto_sync = service
    service.native = app.state.native_sync
    from .sync_control import register_sync_control

    register_sync_control(app, service, user_for, "auto-sync")

    @app.get("/api/watch/auto-sync")
    async def auto_sync_status(request: Request):
        return service.view(user_for(request)["id"])

    @app.put("/api/watch/auto-sync")
    async def auto_sync_settings(payload: SyncSettings, request: Request):
        return await service.save(user_for(request)["id"], payload)

    @app.post("/api/watch/auto-sync/pause")
    async def auto_sync_pause(request: Request):
        owner = user_for(request)["id"]
        await service.stop(owner)
        return service.view(owner)

    @app.post("/api/watch/auto-sync/check", status_code=202)
    async def auto_sync_check(request: Request):
        service.kick(user_for(request)["id"])
        return {"ok": True}

    @app.get("/api/watch/auto-sync/runs")
    async def auto_sync_history(
        request: Request, offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=50)
    ):
        return service.history(user_for(request)["id"], offset, limit)

    @app.get("/api/watch/auto-sync/runs/{number}")
    async def auto_sync_report(number: str, request: Request):
        row = store.one(
            "SELECT * FROM auto_sync_runs WHERE id=? AND user_id=?",
            (number, user_for(request)["id"]),
        )
        if not row:
            raise HTTPException(404, "没有这次本人的后台同步任务")
        return service.run_view(row)

    class Recheck(BaseModel):
        target_ids: list[int] = Field(min_length=1, max_length=100000)
        consent: bool = Field(strict=True)

    @app.post("/api/watch/auto-sync/runs/{number}/recheck", status_code=202)
    async def auto_sync_recheck(number: str, payload: Recheck, request: Request):
        from .watch_alignment import fingerprint

        user = user_for(request)
        run = store.one(
            "SELECT * FROM auto_sync_runs WHERE id=? AND user_id=?", (number, user["id"])
        )
        if not run:
            raise HTTPException(404, "没有这次本人的后台同步任务")
        if not payload.consent or run["status"] == "running":
            raise HTTPException(409, "请在本轮完成后选择并确认重新核对")
        ids = set(payload.target_ids)
        rows = [
            r
            for r in store.all("SELECT * FROM auto_sync_targets WHERE run_id=?", (number,))
            if r["id"] in ids
        ]
        if len(rows) != len(ids):
            raise HTTPException(422, "包含不属于本人的同步条目")
        selection, seeds, pins = {}, {}, {}
        for row in rows:
            selection.setdefault(row["source_id"], []).append(row["canonical_key"])
            seeds[row["canonical_key"]] = {
                "canonical_key": row["canonical_key"],
                "media": json.loads(row["media"]),
            }
            if row["source_id"] != "movieclaw":
                source = archive.source_for(row["source_id"], user["id"])
                pins[row["source_id"]] = source_pin(source)
        alignment = app.state.alignment
        config = {
            "sources": pins,
            "version": 1,
            "native": "movieclaw" in selection,
            "scope": fingerprint(alignment.scope(user["id"])),
            "selection": selection,
            "parent_id": number,
        }
        fresh = str(uuid.uuid4())
        store.execute(
            (
                "INSERT INTO alignment_jobs(id,user_id,status,config,created_at) "
                "VALUES(?,?,'reading',?,?)"
            ),
            (fresh, user["id"], json.dumps(config), stamp()),
        )
        task = archive.task_factory(
            alignment.build(
                fresh, user, list(pins), [], config["native"], requested=list(seeds.values())
            )
        )
        key = "alignment:" + fresh
        archive.tasks[key] = task
        task.add_done_callback(lambda _: archive.tasks.pop(key, None))
        return {"id": fresh}

    @app.get("/api/watch/auto-sync/runs/{number}/targets")
    async def auto_sync_targets(number: str, request: Request, offset: int = Query(0, ge=0)):
        if not store.one(
            "SELECT 1 FROM auto_sync_runs WHERE id=? AND user_id=?",
            (number, user_for(request)["id"]),
        ):
            raise HTTPException(404, "没有这次本人的后台同步任务")
        rows = store.all(
            "SELECT id,source_id,media,state,message FROM auto_sync_targets WHERE "
            "run_id=? ORDER BY id LIMIT 201 OFFSET ?",
            (number, offset),
        )
        return {
            "targets": [{**r, "media": json.loads(r["media"])} for r in rows[:200]],
            "next_offset": offset + 200 if len(rows) > 200 else None,
        }

    return service
