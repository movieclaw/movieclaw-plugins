"""仅处理已选择开启的实时操作，不调用主程序操作或自动扫描历史。"""

import asyncio
import copy
import hashlib

from .client import TraktError
from .policy import history_body, identity, iso_time
from .preferences import preferences

CHUNK = 30
ACTIVE = {"running"}


def item(row):
    return identity(
        {
            "media": row["media"],
            "unit": {"season": row.get("season", 0), "episode": row.get("episode", 0)},
        }
    )


class Actions:
    def __init__(self, engine):
        self.e, self.store = engine, engine.store

    def scope(self, task):
        return f"transfer:{task['id']}"

    async def save(self, task):
        await self.store.set("task", task, scope=self.scope(task))

    async def get(self, member, task_id):
        task = await self.store.get("task", scope=f"transfer:{task_id}")
        if not task or task["member_id"] != member or task["direction"] != "auto":
            raise ValueError("同步任务不存在")
        return task

    def new_task(self, member, direction, account, task_id):
        return {
            "id": task_id,
            "member_id": member,
            "direction": direction,
            "generation": account["generation"],
            "created_at": iso_time(self.e.clock()),
            "state": "running",
            "plan_pages": 0,
            "cursor": 0,
            "total": 0,
            "movies": 0,
            "episodes": 0,
            "done": 0,
            "failed": 0,
            "skipped": 0,
            "attempt": 0,
            "next_at": 0,
        }

    async def request(self, task, method, path, body=None, params=None):
        account = await self.e.get(task["member_id"], "account", {})
        if account.get("generation") != task["generation"]:
            raise ValueError("账号连接已变化，请核对后重试")
        if account.get("blocked") or account.get("client_id") != self.e.client.client_id:
            raise ValueError("Trakt 授权失效，请重新连接")
        return await self.e.authorized(task["member_id"], account, method, path, body, params)

    async def control(self, member, task_id, command):
        async with self.e.locks[member]:
            task = await self.get(member, task_id)
            if command == "cancel" and task["state"] not in {"complete", "cancelled"}:
                task["state"] = "cancelled"
            elif command == "resume" and task["state"] in {"paused", "complete_with_errors"}:
                task.update(
                    state="running",
                    next_at=0,
                    cursor=0,
                    done=0,
                    skipped=0,
                    failed=0,
                    error=None,
                )
                for page in range(task["plan_pages"]):
                    rows = await self.store.get(f"plan_{page}", scope=self.scope(task))
                    for row in rows:
                        if row["state"] == "failed":
                            row["state"] = "pending"
                    await self.store.set(f"plan_{page}", rows, scope=self.scope(task))
            else:
                raise ValueError("当前任务不支持这个操作")
            await self.save(task)
            return {"id": task["id"], "state": task["state"]}

    async def add_plans(self, task, rows):
        pending = [{**row, "state": "pending"} for row in rows if item(row) or row.get("body")]
        for at in range(0, len(pending), CHUNK):
            await self.store.set(
                f"plan_{task['plan_pages']}", pending[at : at + CHUNK], scope=self.scope(task)
            )
            task["plan_pages"] += 1
        task["total"] += len(pending)
        task["movies"] += sum(r["media"]["kind"] == "movie" for r in pending)
        task["episodes"] += sum(r["media"]["kind"] == "tv" for r in pending)

    async def execute(self, task):
        opts = await preferences(self.e, task["member_id"])
        if not getattr(opts, task["option"]):
            task.update(state="cancelled", error="同步开关已关闭")
            await self.e.record_activity(
                task["member_id"],
                task["id"],
                event=task["event"],
                title=task["title"],
                occurred_at=task["created_at"],
                state="skipped",
                reason="同步开关已关闭",
            )
            return
        if task["cursor"] >= task["plan_pages"]:
            task["state"] = "complete_with_errors" if task["failed"] else "complete"
            task["completed_at"] = iso_time(self.e.clock())
            await self.e.record_activity(
                task["member_id"],
                task["id"],
                state="failed"
                if task["failed"]
                else "skipped"
                if task["skipped"] and not task["done"]
                else "sent",
                completed_at=task["completed_at"],
                title=task["title"],
                event=task["event"],
                occurred_at=task["created_at"],
                kind="tv" if task["episodes"] else "movie",
                reason="有条目未匹配，请在 Trakt 核对"
                if task["failed"]
                else "较新的观看操作已覆盖旧任务，未重复发送"
                if task["skipped"]
                else None,
            )
            return
        key = f"plan_{task['cursor']}"
        rows = await self.store.get(key, scope=self.scope(task))
        processed = 0
        for row in rows:
            if row["state"] in {"sent", "failed", "skipped"}:
                continue
            if processed >= 5:
                return
            processed += 1
            path = task["path"]
            superseded = (
                path == "/sync/history"
                and await self.e.superseded_by_unwatch(
                    task["member_id"],
                    task["generation"],
                    item(row),
                    row["watched_at"],
                    row["media"]["id"],
                )
            ) or (
                path == "/sync/history/remove"
                and await self.e.has_newer_watch(
                    task["member_id"],
                    task["generation"],
                    item(row),
                    row["media"]["id"],
                    row["watched_at"],
                )
            )
            if superseded:
                row.update(state="skipped", reason="较新的观看操作已覆盖旧任务")
                await self.store.set(key, rows, scope=self.scope(task))
                continue
            try:
                body = row.get("body") or history_body(item(row), row["watched_at"])
                if path == "/sync/history/remove":
                    await self.e.remember_unwatch(
                        task["member_id"],
                        task["generation"],
                        item(row),
                        row["media"]["id"],
                        row["watched_at"],
                    )
                    for movie in body.get("movies", []):
                        movie.pop("watched_at", None)
                    for show in body.get("shows", []):
                        for season in show.get("seasons", []):
                            for episode in season.get("episodes", []):
                                episode.pop("watched_at", None)
                exists = False
                if path == "/sync/history":
                    account = await self.e.get(task["member_id"], "account", {})
                    exists = await self.e.history_exists(
                        task["member_id"],
                        account,
                        {"item": item(row), "occurred_at": row["watched_at"]},
                    )
                if not exists:
                    result = await self.request(task, "POST", path, body)
                    if not isinstance(result, dict):
                        raise TraktError(502)
                    if any(result.get("not_found", {}).values()):
                        raise TraktError(404)
                if path == "/sync/history":
                    await self.e.remember_watch(
                        task["member_id"],
                        task["generation"],
                        item(row),
                        row["media"]["id"],
                        row["watched_at"],
                    )
                row["state"] = "sent"
            except TraktError as exc:
                if exc.auth_error or exc.status not in {400, 404, 422}:
                    raise
                row.update(state="failed", reason=str(exc))
            await self.store.set(key, rows, scope=self.scope(task))
        task["cursor"] += 1
        task["done"] += sum(r["state"] == "sent" for r in rows)
        task["failed"] += sum(r["state"] == "failed" for r in rows)
        task["skipped"] += sum(r["state"] == "skipped" for r in rows)

    async def step(self, task_id, member):
        async with self.e.locks[member]:
            task = await self.get(member, task_id)
            if (
                not self.e.enabled
                or task["state"] not in ACTIVE
                or task["next_at"] > self.e.clock()
            ):
                return
            account = await self.e.get(member, "account", {})
            if account.get("generation") != task["generation"]:
                task.update(state="cancelled", error="账号已断开或更换")
            elif account.get("blocked") or account.get("client_id") != self.e.client.client_id:
                task.update(state="paused", error="Trakt 授权失效，请重新连接")
            else:
                checkpoint = copy.deepcopy(task)
                try:
                    task["next_at"] = 0
                    await self.execute(task)
                    task.update(error=None, attempt=0)
                except asyncio.CancelledError:
                    raise
                except (TraktError, OSError) as exc:
                    task = checkpoint
                    task["attempt"] += 1
                    task["error"] = (
                        "授权失效，请重新连接"
                        if isinstance(exc, TraktError) and exc.auth_error
                        else "连接暂未完成，稍后自动重试"
                    )
                    if isinstance(exc, TraktError) and exc.auth_error:
                        task["state"] = "paused"
                        await self.e.block_auth(member, account, await self.e.ledger(member))
                    task["next_at"] = self.e.clock() + max(
                        getattr(exc, "retry_after", 5), min(3600, 5 * 2 ** min(task["attempt"], 10))
                    )
                except ValueError as exc:
                    task = checkpoint
                    task.update(state="paused", error=str(exc))
                except Exception:
                    task = checkpoint
                    # 宿主暂不可达或进程内异常都保留游标，不能谎报整批完成。
                    task.update(error="后台暂未完成，稍后自动重试", next_at=self.e.clock() + 30)
            await self.save(task)

    async def automatic(self, kind, event, event_id, occurred_at):
        option = {
            "marked_played": "manual_watched",
            "marked_unplayed": "manual_unwatched",
            "favorited": "favorites",
        }[kind]
        member = event["member_id"]
        async with self.e.locks[member]:
            if not self.e.enabled or not getattr(await preferences(self.e, member), option):
                return
            account = await self.e.get(member, "account", {})
            if not (await self.e.status(member))["connected"]:
                return
            task_id = hashlib.sha256(
                f"{member}:{account['generation']}:{event_id}".encode()
            ).hexdigest()[:32]
            if await self.store.get("task", scope=f"transfer:{task_id}"):
                return
            task = self.new_task(member, "auto", account, task_id)
            units = event.get("units") or [None]
            rows = []
            for unit in units:
                payload = identity({**event, "unit": unit})
                if kind == "favorited":
                    media = event["media"]
                    ids = {
                        k: v
                        for k, v in {
                            "tmdb": media.get("tmdb_id"),
                            "imdb": media.get("imdb_id"),
                        }.items()
                        if v
                    }
                    if not ids or media["kind"] not in {"movie", "tv"}:
                        continue
                    body = {"movies" if media["kind"] == "movie" else "shows": [{"ids": ids}]}
                    if media["kind"] == "tv" and event.get("level") in {"season", "episode"}:
                        season = {"number": event["season"]}
                        if event["level"] == "episode":
                            season["episodes"] = [{"number": event["episode"]}]
                        body["shows"][0]["seasons"] = [season]
                    rows.append(
                        {
                            "media": media,
                            "season": event.get("season") or 0,
                            "episode": event.get("episode") or 0,
                            "watched_at": occurred_at,
                            "body": body,
                        }
                    )
                    break
                if not payload:
                    continue
                rows.append(
                    {
                        "media": event["media"],
                        "season": (unit or {}).get("season", 0),
                        "episode": (unit or {}).get("episode", 0),
                        "watched_at": occurred_at,
                    }
                )
            await self.add_plans(task, rows)
            if not rows:
                await self.e.record_activity(
                    member,
                    task_id,
                    event=kind,
                    title=event["media"].get("title", "未命名影片"),
                    kind=event["media"]["kind"],
                    occurred_at=occurred_at,
                    state="skipped",
                    reason="缺少可匹配的影片或季集编号",
                )
                return
            else:
                task["title"] = str(event["media"].get("title", "未命名影片"))[:120]
                task["event"] = kind
            task.update(
                state="running",
                phase="execute",
                option=option,
                confirmed_at=occurred_at,
                path={
                    "marked_played": "/sync/history",
                    "marked_unplayed": "/sync/history/remove",
                    "favorited": "/sync/watchlist",
                }[kind],
            )
            task["sequence"] = await self.e.get(member, "auto_sequence", 0) + 1
            await self.e.set(member, "auto_sequence", task["sequence"])
            await self.save(task)

    async def drain_once(self):
        tasks = sorted(
            (await self.store.scopes("task", entity="transfer")).values(),
            key=lambda t: (t["created_at"], t.get("sequence", 0)),
        )
        members = set()
        for task in tasks:
            if task["direction"] != "auto" or task["state"] not in {
                "running",
                "paused",
            }:
                continue
            if task["member_id"] in members:
                continue
            members.add(task["member_id"])
            await self.step(task["id"], task["member_id"])

    async def run(self):
        while True:
            try:
                await self.drain_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                self.e.log.warning("Trakt 操作同步暂未完成，保留检查点后重试")
            await asyncio.sleep(2)
