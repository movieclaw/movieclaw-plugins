"""Foreground-only, confirmed history transfers with durable pages and idempotent receipts."""

import asyncio
import copy
import hashlib
import json
import uuid

from movieclaw_sdk.ops import OpsError

from .client import TraktError
from .policy import history_body, identity, iso_time, matches, timestamp, trakt_minute

CHUNK = 30
ACTIVE = {"scanning", "running"}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def unit_key(row):
    return f"unit_{row['media']['id']}_{row['season']}_{row['episode']}"


def item(row):
    return identity({"media": row["media"], "unit": row})


def fingerprint(direction, row):
    return digest([direction, item(row), row["watched_at"], row.get("source_id")])


def minute_key(row):
    return "minute_" + digest([item(row), trakt_minute(row["watched_at"])])


def source_ids(row):
    return [row["source_id"], *row.get("source_ids", [])]


def summary(task):
    return {k: v for k, v in task.items() if k not in {"generation", "remote_stamp", "signature"}}


class Transfers:
    """Only step() advances work. No timer, lifecycle hook or worker calls it."""

    def __init__(self, engine, ops):
        self.e, self.store, self.ops = engine, engine.store, ops

    def scope(self, task):
        return f"history:{task['id']}"

    async def save(self, task):
        await self.store.set("task", task, scope=self.scope(task))

    async def get(self, member, task_id):
        task = await self.store.get("task", scope=f"history:{task_id}")
        if not task or task["member_id"] != member:
            raise ValueError("同步任务不存在")
        return task

    async def listing(self, member, offset=0, limit=20):
        tasks = sorted(
            (
                t
                for t in (await self.store.scopes("task", entity="history")).values()
                if t["member_id"] == member
            ),
            key=lambda t: t["created_at"],
            reverse=True,
        )
        return {"tasks": [summary(t) for t in tasks[offset : offset + limit]], "total": len(tasks)}

    async def create(self, member, direction):
        if direction not in {"export", "import"}:
            raise ValueError("同步方向无效")
        async with self.e.locks[member]:
            account = await self.e.get(member, "account", {})
            if not (await self.e.status(member))["connected"]:
                raise ValueError("请先为此成员连接 Trakt 账号")
            if any(
                t["state"] in ACTIVE | {"ready", "paused"}
                for t in (await self.listing(member, limit=100000))["tasks"]
            ):
                raise ValueError("请先继续或取消当前任务，再重新扫描")
            task = {
                "id": uuid.uuid4().hex,
                "member_id": member,
                "direction": direction,
                "generation": account["generation"],
                "created_at": iso_time(self.e.clock()),
                "cutoff": iso_time(self.e.clock()),
                "state": "scanning",
                "phase": "local",
                "local_cursor": None,
                "remote_kind": "movies",
                "remote_page": 1,
                "plan_pages": 0,
                "cursor": 0,
                "total": 0,
                "scanned": 0,
                "unmatched": 0,
                "done": 0,
                "failed": 0,
                "skipped": 0,
                "attempt": 0,
                "next_at": 0,
            }
            await self.save(task)
            return summary(task)

    async def request(self, task, method, path, body=None, params=None):
        account = await self.e.get(task["member_id"], "account", {})
        if account.get("generation") != task["generation"]:
            raise ValueError("账号连接已变化，请取消后重新扫描")
        if account.get("blocked") or account.get("client_id") != self.e.client.client_id:
            raise ValueError("Trakt 授权失效，请重新连接")
        return await self.e.authorized(task["member_id"], account, method, path, body, params)

    async def remember_export_watch(self, task, row, watched_at):
        await self.e.remember_watch(
            task["member_id"], task["generation"], item(row), row["media"]["id"], watched_at
        )

    async def receipt(self, task, row, write=False, merged=False):
        scope = f"synced:{task['member_id']}-{task['generation']}"
        key = fingerprint(task["direction"], row)
        if write:
            if task["direction"] == "export":
                if not merged:
                    await self.remember_export_watch(task, row, row["watched_at"])
                await self.e.remember_source_delivery(
                    task["member_id"],
                    task["generation"],
                    item(row),
                    source_ids(row),
                    "minute_merged" if merged else "sent",
                    export={"watched_at": row["watched_at"], "owner": key},
                )
            await self.store.set(key, "minute_merged" if merged else True, scope=scope)
        else:
            saved = await self.store.get(key, scope=scope, default=False)
            if saved:
                if task["direction"] == "export":
                    if saved is True:
                        await self.remember_export_watch(task, row, row["watched_at"])
                    await self.e.remember_source_delivery(
                        task["member_id"],
                        task["generation"],
                        item(row),
                        source_ids(row),
                        "minute_merged" if saved == "minute_merged" else "sent",
                        export={"watched_at": row["watched_at"], "owner": key},
                    )
                return saved
            delivered = (
                await self.e.source_delivery(
                    task["member_id"],
                    task["generation"],
                    item(row),
                    source_ids(row),
                    details=True,
                )
                if task["direction"] == "export"
                else False
            )
            if delivered and delivered["state"] == "minute_merged":
                return "minute_merged"
            if delivered and delivered["state"] == "sent":
                if delivered.get("export"):
                    export = delivered["export"]
                    await self.remember_export_watch(task, row, export["watched_at"])
                    await self.store.set(
                        minute_key({**row, "watched_at": export["watched_at"]}),
                        export["owner"],
                        scope=self.scope(task),
                    )
                return "source"
            return False

    async def add_plans(self, task, rows):
        pending = []
        for row in rows:
            if item(row) and not await self.receipt(task, row):
                pending.append({**row, "state": "pending"})
        for at in range(0, len(pending), CHUNK):
            await self.store.set(
                f"plan_{task['plan_pages']}", pending[at : at + CHUNK], scope=self.scope(task)
            )
            task["plan_pages"] += 1
        task["total"] += len(pending)

    async def local_page(self, task):
        data = await self.ops.call(
            "playback.sync.catalog",
            {
                "member_id": task["member_id"],
                "cursor": task["local_cursor"],
                "limit": 50,
            },
        )
        plans = []
        for unit in data["items"]:
            media = unit["media"]
            media["title"] = str(media.get("title") or "未命名影片")[:120]
            if not item(unit):
                task["unmatched"] += 1
                continue
            # Index only local holdings: matching 30k remote records never needs 30k host reads.
            for kind in ("tmdb", "imdb"):
                if media.get(kind + "_id"):
                    await self.store.set(
                        f"match_{media['kind']}_{kind}_{media[kind + '_id']}_{unit['season']}_{unit['episode']}",
                        unit,
                        scope=self.scope(task),
                    )
            await self.store.set(unit_key(unit), unit, scope=self.scope(task))
            if task["direction"] == "export" and unit["played"]:
                for watch in unit["watches"]:
                    if timestamp(watch["watched_at"]) <= timestamp(task["cutoff"]):
                        row = {
                            "media": media,
                            "season": unit["season"],
                            "episode": unit["episode"],
                            "watched_at": watch["watched_at"],
                            "source_id": watch["id"],
                            "source_ids": list(watch.get("source_ids", [])),
                            "basis": watch["basis"],
                            "expected_updated_at": unit["updated_at"],
                        }
                        # Include already-receipted source facts when disclosing collisions
                        # in an incremental preview. They will not be queried or pushed again.
                        if await self.receipt(task, row) is True:
                            await self.store.set(
                                minute_key(row), fingerprint("export", row), scope=self.scope(task)
                            )
                        plans.append(row)
            task["local_stamp"] = digest(
                [task.get("local_stamp"), media, unit["season"], unit["episode"]]
            )
            task["scanned"] += 1
        await self.add_plans(task, plans)
        cursor = data["next_cursor"]
        if (
            cursor
            and task["local_cursor"]
            and tuple(map(int, cursor.split(":")))
            <= tuple(map(int, task["local_cursor"].split(":")))
        ):
            raise ValueError("本地分页没有前进，请检查主程序版本")
        task["local_cursor"] = cursor
        if cursor is None:
            if task["direction"] == "export":
                await self.ready(task)
            else:
                task["remote_stamp"] = self.watched_stamp(
                    await self.request(task, "GET", "/sync/last_activities")
                )
                task["local_unmatched"] = task["unmatched"]
                previous = await self.e.get(task["member_id"], "import_stamp")
                if any(task["remote_stamp"]) and previous == self.import_stamp(task):
                    await self.ready(task)
                else:
                    task["phase"] = "remote"

    @staticmethod
    def watched_stamp(stamp):
        return [
            stamp.get("movies", {}).get("watched_at"),
            stamp.get("episodes", {}).get("watched_at"),
        ]

    def import_stamp(self, task):
        return {
            "generation": task["generation"],
            "stamp": task["remote_stamp"],
            "local": task.get("local_stamp"),
        }

    async def remote_page(self, task):
        kind, page = task["remote_kind"], task["remote_page"]
        rows = await self.request(
            task, "GET", f"/sync/history/{kind}", params={"page": page, "limit": 100}
        )
        if not isinstance(rows, list):
            raise TraktError(502)
        signature = digest(rows)
        if rows and signature == task.get("signature"):
            raise ValueError("Trakt 返回了重复分页，请取消后重新扫描")
        task["signature"] = signature
        plans = []
        account = await self.e.get(task["member_id"], "account", {})
        for remote in rows:
            movie = kind == "movies"
            ids = remote.get("movie" if movie else "show", {}).get("ids", {})
            season, episode = (
                (0, 0)
                if movie
                else (
                    remote.get("episode", {}).get("season"),
                    remote.get("episode", {}).get("number"),
                )
            )
            when = remote.get("watched_at")
            if not when:
                continue
            if timestamp(when) > timestamp(task["cutoff"]):
                # The activity stamp already includes this record. Revisit it on a later scan
                # once eligible, even if Trakt's activity stamp and local inventory stay unchanged.
                task["post_cutoff_records"] = True
                continue
            unit = None
            for key in ("tmdb", "imdb"):
                if ids.get(key):
                    unit = await self.store.get(
                        f"match_{'movie' if movie else 'tv'}_{key}_{ids[key]}_{season}_{episode}",
                        scope=self.scope(task),
                    )
                    if unit:
                        break
            task["scanned"] += 1
            if unit is None:
                task["unmatched"] += 1
                continue
            if remote.get("id") is None:
                raise ValueError("Trakt 历史缺少记录 ID，无法安全去重")
            plans.append(
                {
                    "media": unit["media"],
                    "season": season,
                    "episode": episode,
                    "watched_at": when,
                    "source_id": digest(
                        [account.get("username") or task["generation"], remote["id"]]
                    ),
                    "expected_updated_at": unit["updated_at"],
                    "basis": "trakt",
                }
            )
        await self.add_plans(task, plans)
        count = getattr(rows, "page_count", 0)
        if (count and page >= count) or (not count and not rows):
            task.pop("signature", None)
            if kind == "movies":
                task.update(remote_kind="episodes", remote_page=1)
            else:
                stamp = self.watched_stamp(await self.request(task, "GET", "/sync/last_activities"))
                if stamp != task["remote_stamp"]:
                    # Never confirm a mixture of pages from a changing remote history.
                    raise ValueError("扫描期间 Trakt 历史发生变化，请取消后重新扫描")
                await self.ready(task)
        else:
            task["remote_page"] += 1

    async def ready(self, task):
        groups = {}
        minutes = set()
        task["minute_collisions"] = 0
        for page in range(task["plan_pages"]):
            rows = await self.store.get(f"plan_{page}", scope=self.scope(task), default=[])
            for row in rows:
                if task["direction"] == "export":
                    row["trakt_watched_at"] = iso_time(trakt_minute(row["watched_at"]))
                    key = digest([item(row), row["trakt_watched_at"]])
                    owner = await self.store.get(minute_key(row), scope=self.scope(task))
                    row["minute_collision"] = key in minutes or bool(
                        owner and owner != fingerprint("export", row)
                    )
                    task["minute_collisions"] += row["minute_collision"]
                    minutes.add(key)
                key = row["media"]["id"], row["season"]
                group = groups.setdefault(
                    key,
                    {
                        "title": row["media"]["title"],
                        "kind": row["media"]["kind"],
                        "season": row["season"],
                        "episodes": [],
                        "records": 0,
                        "state_time_records": 0,
                    },
                )
                group["records"] += 1
                group["state_time_records"] += row["basis"] == "state"
                if row["media"]["kind"] == "tv" and row["episode"] not in group["episodes"]:
                    group["episodes"].append(row["episode"])
            if task["direction"] == "export":
                await self.store.set(f"plan_{page}", rows, scope=self.scope(task))
        values = list(groups.values())
        for at in range(0, len(values), CHUNK):
            await self.store.set(
                f"groups_{at // CHUNK}", values[at : at + CHUNK], scope=self.scope(task)
            )
        task.update(
            state="ready",
            phase="execute",
            movies=sum(g["kind"] == "movie" for g in values),
            shows=len({key[0] for key, g in groups.items() if g["kind"] == "tv"}),
            seasons=sum(g["kind"] == "tv" for g in values),
            episodes=sum(len(g["episodes"]) for g in values),
            group_pages=(len(values) + CHUNK - 1) // CHUNK,
            preview_token=uuid.uuid4().hex,
        )

    async def confirm(self, member, task_id, token):
        async with self.e.locks[member]:
            task = await self.get(member, task_id)
            account = await self.e.get(member, "account", {})
            if task["state"] != "ready" or token != task.get("preview_token"):
                raise ValueError("预览已失效，请重新核对后确认")
            if account.get("generation") != task["generation"]:
                raise ValueError("账号已变化，请取消后重新扫描")
            if not self.e.enabled or not (await self.e.status(member))["connected"]:
                raise ValueError("插件已停用或 Trakt 连接不可用，请检查账号后重新确认")
            if task["next_at"] > self.e.clock():
                raise ValueError("Trakt 尚在退避等待，请稍后重新确认")
            if task["direction"] == "import" and task["total"]:
                try:
                    stamp = self.watched_stamp(
                        await self.request(task, "GET", "/sync/last_activities")
                    )
                except TraktError as exc:
                    if exc.auth_error:
                        await self.e.block_auth(member, account, await self.e.ledger(member))
                        task.update(error="Trakt 授权失效，请重新连接", next_at=0)
                    else:
                        task.update(
                            error="Trakt 暂不可用，预览已保留，请稍后重新确认",
                            next_at=self.e.clock() + max(5, exc.retry_after),
                        )
                    await self.save(task)
                    raise ValueError(task["error"]) from None
                if stamp != task["remote_stamp"]:
                    raise ValueError("Trakt 历史已变化，请取消后重新扫描")
            task.update(
                state="running", confirmed_at=iso_time(self.e.clock()), next_at=0, error=None
            )
            task.pop("preview_token", None)
            await self.save(task)
            return summary(task)

    async def details(self, member, task_id, page=0, records=False):
        task = await self.get(member, task_id)
        key = "plan" if records else "groups"
        return {
            "task": summary(task),
            "items": await self.store.get(f"{key}_{page}", scope=self.scope(task), default=[]),
            "page": page,
            "pages": task["plan_pages"] if records else task.get("group_pages", 0),
        }

    async def remote_has(self, task, row):
        when, page, previous = trakt_minute(row["watched_at"]), 1, None
        while True:
            rows = await self.request(
                task,
                "GET",
                "/sync/history/" + ("movies" if row["media"]["kind"] == "movie" else "episodes"),
                params={
                    "start_at": iso_time(when),
                    "end_at": iso_time(when + 60),
                    "limit": 100,
                    "page": page,
                },
            )
            if any(matches(item(row), r) and trakt_minute(r["watched_at"]) == when for r in rows):
                return True
            if not rows or (getattr(rows, "page_count", 0) and page >= rows.page_count):
                return False
            signature = digest(rows)
            if signature == previous:
                raise TraktError(502)
            previous, page = signature, page + 1

    async def execute_row(self, task, row, key, rows):
        saved = await self.receipt(task, row)
        if saved:
            row["state"] = "skipped" if saved == "minute_merged" else "sent"
            if saved == "minute_merged":
                row["reason"] = "Trakt 按分钟合并，同一分钟的额外观看无法单独保存"
            elif (
                saved is True
                and task["direction"] == "export"
                and not await self.store.get(minute_key(row), scope=self.scope(task))
            ):
                await self.store.set(
                    minute_key(row), fingerprint("export", row), scope=self.scope(task)
                )
            return
        if task["direction"] == "import":
            version = await self.store.get(
                "version_" + unit_key(row),
                scope=self.scope(task),
                default={"value": row["expected_updated_at"]},
            )
            result = await self.ops.call(
                "playback.sync.import",
                {
                    "member_id": task["member_id"],
                    "records": [
                        {
                            "media_item_id": row["media"]["id"],
                            "kind": row["media"]["kind"],
                            "tmdb_id": row["media"].get("tmdb_id"),
                            "imdb_id": row["media"].get("imdb_id"),
                            "season": row["season"],
                            "episode": row["episode"],
                            "source_id": row["source_id"],
                            "watched_at": row["watched_at"],
                            "expected_updated_at": version["value"],
                        }
                    ],
                },
            )
            outcome = result["outcomes"][0]
            if outcome["status"] not in {"applied", "already"}:
                row.update(state="skipped", reason=outcome["status"])
                return
            # Store the original import receipt's version, never a fresh version that could overwrite a newer action.
            # An old receipt from a previous binding/task must not move a fresh preview backwards.
            prior = timestamp(version["value"]) if version["value"] else float("-inf")
            if outcome["status"] == "applied" or timestamp(outcome["state_version"]) > prior:
                await self.store.set(
                    "version_" + unit_key(row),
                    {"value": outcome["state_version"]},
                    scope=self.scope(task),
                )
        else:
            m, s, e = row["media"]["id"], row["season"], row["episode"]
            cursor = f"{m}:{s}:{e - 1}" if e else f"{m - 1}:2147483647:2147483647"
            data = await self.ops.call(
                "playback.sync.catalog",
                {"member_id": task["member_id"], "cursor": cursor, "limit": 1},
            )
            current = next((u for u in data["items"] if unit_key(u) == unit_key(row)), None)
            if current and item(current) != item(row):
                row.update(state="skipped", reason="扫描后影片或季集编号已变化，请重新扫描确认")
                return
            if (
                not current
                or not current["played"]
                or not any(
                    w["id"] == row["source_id"] and w["watched_at"] == row["watched_at"]
                    for w in current["watches"]
                )
            ):
                row.update(state="skipped", reason="扫描后本地观看状态已变化")
                return
            if await self.e.source_pending(task["member_id"], task["generation"], source_ids(row)):
                row.update(state="failed", reason="同次实时播放尚未投递完成，请稍后重试此任务")
                return
            owner = await self.store.get(minute_key(row), scope=self.scope(task))
            await self.e.remember_source_delivery(
                task["member_id"],
                task["generation"],
                item(row),
                source_ids(row),
                "export_pending",
                export={
                    "watched_at": row["watched_at"],
                    "owner": fingerprint("export", row),
                    "collision": bool(owner and owner != fingerprint("export", row)),
                },
            )
            exists = await self.remote_has(task, row)
            if exists and owner and owner != fingerprint("export", row):
                row.update(
                    state="skipped",
                    uncertain=False,
                    reason="Trakt 按分钟合并，同一分钟的额外观看无法单独保存",
                )
                await self.receipt(task, row, write=True, merged=True)
                return
            if not exists:
                # Persist intent before POST: an interrupted response is checked remotely on resume.
                row["uncertain"] = True
                await self.store.set(key, rows, scope=self.scope(task))
                result = await self.request(
                    task, "POST", "/sync/history", history_body(item(row), row["watched_at"])
                )
                if not isinstance(result, dict) or not isinstance(result.get("added"), dict):
                    raise TraktError(502, uncertain=True)
                if any(result.get("not_found", {}).values()):
                    raise TraktError(422)
            await self.store.set(
                minute_key(row), fingerprint("export", row), scope=self.scope(task)
            )
        await self.receipt(task, row, write=True)
        row.update(state="sent", uncertain=False)

    async def execute(self, task):
        if task["cursor"] >= task["plan_pages"]:
            task["state"] = (
                "complete_with_errors" if task["failed"] or task["skipped"] else "complete"
            )
            task["completed_at"] = iso_time(self.e.clock())
            if (
                task["state"] == "complete"
                and task["direction"] == "import"
                and any(task["remote_stamp"])
                and not task.get("post_cutoff_records")
            ):
                await self.e.set(task["member_id"], "import_stamp", self.import_stamp(task))
            return
        key = f"plan_{task['cursor']}"
        rows = await self.store.get(key, scope=self.scope(task))
        processed = 0
        for row in rows:
            if row["state"] != "pending":
                continue
            if processed >= 5:
                task["processed"] = (
                    task["done"]
                    + task["failed"]
                    + task["skipped"]
                    + sum(r["state"] != "pending" for r in rows)
                )
                return
            processed += 1
            try:
                await self.execute_row(task, row, key, rows)
            except TraktError as exc:
                if exc.auth_error or exc.status not in {400, 404, 422}:
                    raise
                row.update(state="failed", reason=str(exc))
            await self.store.set(key, rows, scope=self.scope(task))
        task["cursor"] += 1
        task["done"] += sum(r["state"] == "sent" for r in rows)
        task["failed"] += sum(r["state"] == "failed" for r in rows)
        task["skipped"] += sum(r["state"] == "skipped" for r in rows)
        task["processed"] = task["done"] + task["failed"] + task["skipped"]

    async def control(self, member, task_id, command):
        async with self.e.locks[member]:
            task = await self.get(member, task_id)
            if command == "cancel" and task["state"] not in {"complete", "cancelled"}:
                task["state"] = "cancelled"
            elif command == "resume" and task["state"] in {"paused", "complete_with_errors"}:
                task.update(
                    state="running" if task.get("confirmed_at") else "scanning",
                    next_at=0,
                    error=None,
                    cursor=0,
                    done=0,
                    skipped=0,
                    failed=0,
                    processed=0,
                )
                for p in range(task["plan_pages"]):
                    rows = await self.store.get(f"plan_{p}", scope=self.scope(task))
                    for r in rows:
                        if r["state"] == "failed":
                            r["state"] = "pending"
                    await self.store.set(f"plan_{p}", rows, scope=self.scope(task))
            elif command == "pause" and task["state"] in ACTIVE:
                task["state"] = "paused"
            else:
                raise ValueError("当前任务不支持这个操作")
            await self.save(task)
            return summary(task)

    async def step(self, task_id, member):
        async with self.e.locks[member]:
            task = await self.get(member, task_id)
            if task["state"] not in ACTIVE or task["next_at"] > self.e.clock():
                return summary(task)
            if not self.e.enabled:
                task.update(state="paused", error="插件已停用")
            elif (await self.e.get(member, "account", {})).get("generation") != task["generation"]:
                task.update(state="cancelled", error="账号已断开或更换")
            elif not (await self.e.status(member))["connected"]:
                task.update(state="paused", error="Trakt 授权失效，请重新连接")
            else:
                checkpoint = copy.deepcopy(task)
                try:
                    if task["state"] == "scanning":
                        if task["phase"] == "local":
                            await self.local_page(task)
                        else:
                            await self.remote_page(task)
                    else:
                        await self.execute(task)
                    task.update(error=None, attempt=0, next_at=0)
                except asyncio.CancelledError:
                    raise
                except (TraktError, OpsError, OSError) as exc:
                    task = checkpoint
                    task["attempt"] += 1
                    code = getattr(exc, "status", 0)
                    auth_error = isinstance(exc, TraktError) and exc.auth_error
                    task["error"] = (
                        "Trakt 授权失效，请重新连接"
                        if auth_error
                        else "同步接口不可用，请检查兼容版本和操作授权"
                        if code in {400, 401, 403, 404, 422}
                        else "连接暂未完成，保持网页打开后重试"
                    )
                    if code in {400, 401, 403, 404, 422}:
                        task["state"] = "paused"
                    if auth_error:
                        account = await self.e.get(member, "account", {})
                        await self.e.block_auth(member, account, await self.e.ledger(member))
                    task["next_at"] = self.e.clock() + max(
                        getattr(exc, "retry_after", 5), min(3600, 5 * 2 ** min(task["attempt"], 10))
                    )
                except ValueError as exc:
                    task = checkpoint
                    task.update(state="paused", error=str(exc))
            await self.save(task)
            return summary(task)
