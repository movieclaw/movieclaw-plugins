"""失败任务恢复：重新读取元数据、核对历史；不读写媒体文件。"""

import copy

from movieclaw_sdk.ops import OpsError

from .policy import history_body, identity, timestamp


class LocalStateError(Exception):
    def __init__(self, reason, *, deleted=False):
        self.reason, self.deleted = reason, deleted
        super().__init__(reason)


class MetadataResolver:
    def __init__(self, ops):
        self.ops = ops

    async def __call__(self, source, old_item):
        libraries = await self.ops.call("library.list", {})
        libraries.sort(key=lambda lib: lib["id"] != source.get("library_id"))
        for library in libraries:
            try:
                item = await self.ops.call(
                    "library.items.get",
                    {"library_id": library["id"], "media_item_id": source["media_id"]},
                )
            except OpsError as exc:
                if exc.status == 404:
                    continue
                raise
            files = item.get("files", [])
            if source.get("file_id") is not None:
                files = [f for f in files if f["id"] == source["file_id"]]
                if not files:
                    raise LocalStateError("对应的本地文件记录已删除，停止此条同步", deleted=True)
            else:
                unit = old_item.get("episode")
                if unit:
                    files = [
                        f
                        for f in files
                        if f.get("season_number") == unit["season"]
                        and f.get("episode_number") == unit["number"]
                    ]
                if not files:
                    raise LocalStateError("原季集已不存在；缺少文件标识，请先修正此条季集或忽略")
            if not files or any(
                f.get("missing") or f.get("state", "in_place") != "in_place" for f in files
            ):
                raise LocalStateError("本地文件当前缺失或待回收，暂停此条同步")
            unit = None
            if item["kind"] == "tv":
                units = {(f.get("season_number"), f.get("episode_number")) for f in files}
                if len(units) != 1:
                    raise LocalStateError("当前文件对应多个季集，请人工确认")
                season, episode = units.pop()
                unit = {"season": season, "episode": episode}
            updated = identity({"media": item, "unit": unit})
            if not updated:
                raise LocalStateError("当前媒体元数据缺少有效外部编号或季集，请修正后重试")
            source["library_id"] = library["id"]
            return updated
        raise LocalStateError("本地媒体条目已删除，停止此条同步", deleted=True)


class Recovery:
    @staticmethod
    def recovery_delay(job):
        return min(21600, 600 * 2 ** min(job.get("repair_attempt", 0), 6))

    async def refresh_job(self, job):
        if not job.get("revalidate"):
            return
        if job.get("source") and self.resolver:
            job["item"] = await self.resolver(job["source"], job["item"])
        if self.finished(job):
            job["path"] = "/sync/history"
            job["body"] = history_body(job["item"], job["occurred_at"])
            job["uncertain"] = True
        else:
            job["body"] = {**job["item"], "progress": job["body"].get("progress", 0)}
        job.pop("revalidate", None)

    async def retire_failed(self, member, ledger):
        kept = []
        for job in ledger.get("failed", []):
            if not self.finished(job) and self.clock() - timestamp(job["occurred_at"]) > 300:
                await self.complete_activity(member, job, "expired")
            else:
                kept.append(job)
        ledger["failed"] = kept

    async def _retry_locked(self, member, delivery_id=None, *, correction=None, automatic=False):
        ledger = await self.ledger(member)
        if delivery_id is not None and not any(
            j["id"] == delivery_id for j in ledger.get("failed", [])
        ):
            raise ValueError("这条失败记录已处理或不存在，请刷新页面")
        await self.retire_failed(member, ledger)
        selected = [
            j
            for j in ledger.get("failed", [])
            if (delivery_id is None or j["id"] == delivery_id)
            and (not automatic or j.get("retry_at", self.clock() + 600) <= self.clock())
        ]
        if delivery_id is not None and not selected:
            await self.save_ledger(member, ledger)
            return {"retried": 0, "retired": True}
        count = 0
        for original in selected:
            if len(ledger["queue"]) >= 64:
                break
            job = copy.deepcopy(original)
            if correction:
                if "episode" not in job["item"]:
                    raise ValueError("电影不能修正季集")
                job["item"]["episode"] = {
                    "season": correction["season"],
                    "number": correction["episode"],
                }
            job.update(revalidate=True, next_at=self.clock(), uncertain=True)
            try:
                await self.refresh_job(job)
            except LocalStateError as exc:
                if exc.deleted:
                    ledger["failed"].remove(original)
                    await self.complete_activity(member, job, "local_deleted")
                else:
                    original["item"] = job["item"]
                    original["repair_reason"] = exc.reason
                    original["repair_attempt"] = original.get("repair_attempt", 0) + 1
                    original["retry_at"] = self.clock() + self.recovery_delay(original)
                continue
            except OpsError:
                original["item"] = job["item"]
                original["repair_reason"] = "暂时无法读取媒体元数据，稍后自动核对"
                original["retry_at"] = self.clock() + 600
                continue
            job.pop("error_status", None)
            job.pop("auth_error", None)
            job.pop("repair_reason", None)
            # 排队后元数据仍可能变化，正式发送前再核对一次。
            job["revalidate"] = True
            ledger["queue"].append(job)
            ledger["failed"].remove(original)
            # 更新展示元数据，避免已修正的任务仍显示旧 S00。
            unit = job["item"].get("episode", {})
            await self.record_activity(
                member,
                job["id"],
                season=unit.get("season"),
                episode=unit.get("number"),
                state="queued",
            )
            count += 1
        if not ledger.get("failed"):
            ledger["last_error"] = None
        await self.save_ledger(member, ledger)
        return {"retried": count}

    async def dismiss_failed(self, member, delivery_id):
        async with self.locks[member]:
            ledger = await self.ledger(member)
            jobs = [j for j in ledger.get("failed", []) if j["id"] == delivery_id]
            if not jobs:
                raise ValueError("记录已处理或不存在")
            for job in jobs:
                ledger["failed"].remove(job)
                await self.complete_activity(member, job, "dismissed")
            if not ledger["failed"]:
                ledger["last_error"] = None
                await self.health.ok(f"member-{member}")
            await self.save_ledger(member, ledger)
            return {"dismissed": len(jobs)}
