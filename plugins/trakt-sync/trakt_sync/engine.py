"""播放事件持久化与 Trakt 投递；每个成员独立加密凭据、队列和互斥锁。"""

import asyncio
import json
import time
import uuid
from collections import defaultdict

from movieclaw_sdk.ops import OpsError

from .activity import ActivityLog, describe
from .client import TraktError
from .policy import (
    history_body,
    identity,
    iso_time,
    matches,
    progress,
    session_key,
    timestamp,
)
from .recovery import LocalStateError, Recovery


class Engine(ActivityLog, Recovery):
    def __init__(
        self, store, client, health, logger, *, enabled=True, clock=time.time, resolver=None
    ):
        self.store, self.client, self.health, self.log = store, client, health, logger
        self.enabled, self.clock = enabled, clock
        self.resolver = resolver
        self.locks = defaultdict(asyncio.Lock)
        self.rotated = {}
        self.wake = asyncio.Event()
        self.bootstrap = asyncio.Lock()
        self.delivery_failures = {}

    @staticmethod
    def scope(member):
        return f"member:{member}"

    async def get(self, member, key, default=None):
        return await self.store.get(key, scope=self.scope(member), default=default)

    async def set(self, member, key, value, secret=False):
        await self.store.set(key, value, scope=self.scope(member), secret=secret)
        if key == "account":
            await self.store.set("linked", True, scope=self.scope(member))

    async def ledger(self, member):
        return await self.get(member, "ledger", {"queue": [], "seen": [], "sessions": {}})

    @staticmethod
    def finished(job):
        return job["path"] == "/sync/history" or (
            job["path"] == "/scrobble/stop" and job["body"].get("progress", 0) > 80
        )

    @staticmethod
    def drop_warning(ledger):
        count = max(0, ledger.get("dropped", 0) - ledger.get("acknowledged_dropped", 0))
        return f"队列容量不足，已有 {count} 条记录未同步；请检查 Trakt 连接" if count else None

    async def save_ledger(self, member, ledger):
        # 宿主的可靠消费者跨成员共享。容量不足必须显式降级，不能靠抛错阻塞所有成员。
        queue = ledger["queue"]
        queue[:] = [
            j
            for j in queue
            if self.finished(j) or self.clock() - timestamp(j["occurred_at"]) <= 300
        ]
        ledger["seen"] = ledger["seen"][-256:]
        ledger["sessions"] = dict(list(ledger["sessions"].items())[-16:])
        if ledger.get("last_error"):
            ledger["last_error"] = ledger["last_error"][:512]

        def oversized():
            return len(json.dumps(ledger, ensure_ascii=False).encode()) > 60000

        while len(queue) > 64 or oversized():
            if queue:
                at = next(
                    (i for i, j in enumerate(queue) if j["path"] == "/scrobble/start"),
                    None,
                )
                if at is None:
                    at = next((i for i, j in enumerate(queue) if not self.finished(j)), 0)
                job = queue.pop(at)
                if job["path"] != "/scrobble/start":
                    ledger["dropped"] = ledger.get("dropped", 0) + 1
            elif ledger.get("failed"):
                ledger["failed"].pop(0)
                ledger["dropped"] = ledger.get("dropped", 0) + 1
            elif ledger["seen"]:
                ledger["seen"].pop(0)
            elif ledger["sessions"]:
                ledger["sessions"].pop(next(iter(ledger["sessions"])))
            else:
                # 兼容历史数据中的异常大字段；不保留任何请求内容。
                ledger.clear()
                ledger.update(queue=[], seen=[], sessions={}, dropped=1)
        # 排队任务先执行；不能让已到期的失败标记覆盖队首退避时间，造成忙循环。
        deadlines = (
            [queue[0]["next_at"]]
            if queue
            else [j["retry_at"] for j in ledger.get("failed", []) if "retry_at" in j]
        )
        if deadlines:
            await self.set(member, "delivery", {"next_at": min(deadlines)})
        await self.set(member, "ledger", ledger)
        if not deadlines:
            await self.store.delete("delivery", scope=self.scope(member))
        self.wake.set()

    async def block_auth(self, member, account, ledger):
        current = await self.get(member, "account", account)
        await self.set(member, "account", {**current, "blocked": True}, secret=True)
        ledger.update(
            queue=[],
            failed=[],
            sessions={},
            last_error="Trakt 授权或访问权限失效，请断开后重新连接；旧待发队列已清除，失效期间不记录新活动",
        )
        await self.save_ledger(member, ledger)
        await self.health.degraded(f"member-{member}", ledger["last_error"])

    @staticmethod
    def auth_failed(job):
        return job.get("auth_error", False) or job.get("error_status") in (401, 403)

    async def initialize(self):
        # scopes(account) 会一次解密全部账号。先从明文 ledger/linked 找出成员，再逐个读凭据。
        async with self.bootstrap:
            if await self.store.get("delivery-index-v3", default=False):
                return
            ledgers = await self.store.scopes("ledger", entity="member")
            linked = await self.store.scopes("linked", entity="member")
            scopes = set(ledgers) | set(linked) | {self.scope(0)}
            for scope in scopes:
                value = scope.split(":", 1)[-1]
                if not value.isdigit():
                    continue
                member = int(value)
                try:
                    async with self.locks[member]:
                        # 先建立任务标记，单个损坏凭据不能阻断其他账号的迁移和投递。
                        if ledgers.get(scope, {}).get("queue"):
                            await self.set(member, "delivery", {"next_at": self.clock()})
                        account = await self.get(member, "account", {})
                        if not account:
                            continue
                        await self.set(member, "linked", True)
                        ledger = await self.ledger(member)
                        await self.retire_failed(member, ledger)
                        for j in ledger.get("failed", []):
                            j.setdefault("retry_at", self.clock() + 600)
                            if not j.get("source"):
                                j.setdefault(
                                    "repair_reason",
                                    "旧记录缺少本地文件标识；若季集有误，请修正后核对，或忽略此记录",
                                )
                        if not ledger.get("failed"):
                            ledger["last_error"] = None
                        if account.get("blocked") or any(
                            self.auth_failed(j) for j in ledger["queue"]
                        ):
                            await self.block_auth(member, account, ledger)
                        else:
                            await self.save_ledger(member, ledger)
                        await self.store.delete("storage_error", scope=scope)
                except Exception as exc:
                    self.log.warning("Trakt 单个成员数据暂不可读（%s）", type(exc).__name__)
                    await self.set(member, "linked", True)
                    await self.set(member, "storage_error", True)
            await self.store.set("delivery-index-v3", True)

    async def status(self, member):
        try:
            account = await self.get(member, "account", {})
            ledger = await self.ledger(member)
        except Exception:
            return {
                "enabled": self.enabled,
                "configured": bool(self.client.client_id),
                "has_account": True,
                "connected": False,
                "needs_reconnect": True,
                "username": None,
                "pending": 0,
                "failed": 0,
                "last_success": None,
                "last_error": "该成员的本地连接数据无法读取，请断开本地连接后重新授权；远端授权需在 Trakt 确认",
                "warning": None,
                "dropped": 0,
                "unacknowledged_dropped": 0,
                "member_id": member,
            }
        if account.get("access_token") and not await self.get(member, "linked", False):
            await self.set(member, "linked", True)
        warning = self.drop_warning(ledger)
        error = ledger.get("last_error")
        # 旧版把容量提示写进 last_error；迁移时保留 warning，恢复真实的连接健康状态。
        if error and error.startswith("队列容量不足，已有 "):
            error = None
        return {
            "enabled": self.enabled,
            "configured": bool(self.client.client_id),
            "has_account": bool(account.get("access_token")),
            "connected": bool(account.get("access_token"))
            and account.get("client_id") == self.client.client_id
            and not account.get("blocked"),
            "needs_reconnect": bool(account.get("blocked"))
            or bool(
                account.get("access_token") and account.get("client_id") != self.client.client_id
            ),
            "username": account.get("username"),
            "pending": len(ledger["queue"]),
            "failed": len(ledger.get("failed", [])),
            "last_success": ledger.get("last_success"),
            "last_error": error,
            "warning": warning,
            "dropped": ledger.get("dropped", 0),
            "unacknowledged_dropped": max(
                0, ledger.get("dropped", 0) - ledger.get("acknowledged_dropped", 0)
            ),
            "member_id": member,
        }

    async def acknowledge_dropped(self, member):
        async with self.locks[member]:
            ledger = await self.ledger(member)
            ledger["acknowledged_dropped"] = ledger.get("dropped", 0)
            if (ledger.get("last_error") or "").startswith("队列容量不足，已有 "):
                ledger["last_error"] = None
            await self.save_ledger(member, ledger)
            if not ledger.get("last_error") and not ledger.get("failed"):
                await self.health.ok(f"member-{member}")
            return {"acknowledged": ledger["acknowledged_dropped"]}

    async def begin(self, member):
        if not self.client.client_id:
            raise ValueError("Trakt 应用配置不可用，请检查插件设置")
        async with self.locks[member]:
            if (await self.get(member, "account", {})).get("access_token"):
                raise ValueError("该成员已绑定，请先断开连接再更换账号")
            result = await self.client.begin()
            now = self.clock()
            pending = {
                **result,
                "expires_at": now + min(float(result["expires_in"]), 1800),
                "interval": max(5, float(result.get("interval", 5))),
                "next_poll": now,
            }
            await self.set(member, "pending", pending, secret=True)
            return {
                k: pending[k] for k in ("verification_url", "user_code", "expires_at", "interval")
            }

    async def poll(self, member):
        async with self.locks[member]:
            pending = await self.get(member, "pending")
            if not pending:
                return {
                    "state": "connected" if (await self.status(member))["connected"] else "idle"
                }
            now = self.clock()
            if now >= pending["expires_at"]:
                await self.store.delete("pending", scope=self.scope(member))
                return {"state": "expired"}
            if now < pending["next_poll"]:
                return {"state": "pending", "interval": pending["interval"]}
            pending["next_poll"] = now + pending["interval"]
            await self.set(member, "pending", pending, secret=True)
            try:
                result = await self.client.poll(pending["device_code"])
            except TraktError as exc:
                if exc.status in (400, 429):
                    if exc.status == 429:
                        pending["interval"] = min(120, pending["interval"] + 5)
                        pending["next_poll"] = now + max(pending["interval"], exc.retry_after)
                        await self.set(member, "pending", pending, secret=True)
                    return {"state": "pending", "interval": pending["interval"]}
                if exc.status in (404, 409, 410, 418):
                    await self.store.delete("pending", scope=self.scope(member))
                    return {"state": "denied" if exc.status == 418 else "expired"}
                raise
            tokens = self.client.credentials(result, now=now)
            account = {
                **tokens,
                "username": None,
                "client_id": self.client.client_id,
                "generation": uuid.uuid4().hex,
            }
            await self.set(member, "account", account, secret=True)
            await self.store.delete("pending", scope=self.scope(member))
            # 设备码只能兑换一次，先保存令牌；资料接口暂时失败不能丢失授权。
            try:
                profile = await self.client.request(
                    "GET", "/users/settings", token=tokens["access_token"]
                )
                account["username"] = profile.get("user", {}).get("username")
                await self.set(member, "account", account, secret=True)
            except TraktError:
                pass
            return {"state": "connected", "username": account["username"]}

    async def disconnect(self, member):
        async with self.locks[member]:
            readable = True
            try:
                account = await self.get(member, "account", {})
            except Exception:
                account, readable = {}, False
            # 刷新已成功但落盘失败时，撤销最近拿到的令牌，不能再用过期的旧令牌。
            account = self.rotated.get(member, account)
            # 先撤销本地状态，之后的事件和排队任务均不能再使用这个账号。
            for key in (
                "account",
                "pending",
                "ledger",
                "activity",
                "linked",
                "delivery",
                "storage_error",
            ):
                await self.store.delete(key, scope=self.scope(member))
            self.rotated.pop(member, None)
            self.delivery_failures.pop(member, None)
            self.wake.set()
            revoked = readable
            if account.get("access_token") and account.get("client_id") == self.client.client_id:
                try:
                    await self.client.request(
                        "POST",
                        "/oauth/revoke",
                        {
                            **self.client.application_fields(),
                            "token": account["access_token"],
                        },
                    )
                    revoked = True
                except TraktError:
                    revoked = False
            elif account.get("access_token"):
                revoked = False
            await self.health.ok(f"member-{member}")
            return {"disconnected": True, "remote_revocation_confirmed": revoked}

    async def accept(self, kind, event, event_id, occurred_at):
        member = event.get("member_id")
        if not self.enabled or not isinstance(member, int) or member < 0:
            return
        async with self.locks[member]:
            try:
                account = await self.get(member, "account", {})
            except ValueError:
                # 损坏凭据是单成员故障，不能让共享可靠消费者反复重试此成员的事件。
                await self.set(member, "linked", True)
                await self.set(member, "storage_error", True)
                return
            if (
                not account.get("access_token")
                or account.get("client_id") != self.client.client_id
                or account.get("blocked")
            ):
                return  # 从不借用管理员或其他成员的账号。
            if not await self.get(member, "linked", False):
                await self.set(member, "linked", True)
            ledger = await self.ledger(member)
            if any(self.auth_failed(j) for j in ledger["queue"]):
                await self.block_auth(member, account, ledger)
                return
            if event_id in ledger["seen"]:
                return
            item, rate = identity(event), progress(event)
            completed = kind == "completed" or (
                kind == "stopped" and event.get("completed") is True
            )
            if completed:
                # MovieClaw 看完后清零续播点；完成事件才是事实，不能把它算成 0%。
                rate = 100.0
            at = timestamp(occurred_at)
            if at > self.clock() + 60:
                return
            key = session_key(event)
            state = ledger["sessions"].get(key, {})
            stale = self.clock() - at > 300
            request = None
            local_completion = bool(event.get("viewing_id")) and completed
            if item and (stale or local_completion or (completed and at < state.get("at", 0))):
                # 离线回传不能伪装成“正在观看”，完成记录保留原观看时间。
                if kind in ("stopped", "completed") and rate is not None and rate > 80:
                    if abs(at - state.get("terminal_at", -1000)) > 10:
                        request = {
                            "path": "/sync/history",
                            "body": history_body(item, occurred_at),
                        }
                        if local_completion:
                            request["clear_watching"] = not stale and ledger.get("watching") == key
                            state["closed"] = True
                            state["at"] = at
                        state["terminal_at"] = at
            elif item and at >= state.get("at", 0):
                if kind == "started":
                    state = {"session": event_id, "at": at, "last_start": at}
                    request = {
                        "path": "/scrobble/start",
                        "body": {**item, "progress": rate or 0},
                    }
                elif not state.get("closed") or (completed and "terminal_at" not in state):
                    state["at"] = at
                    if kind in ("stopped", "completed") and rate is not None:
                        state["closed"] = True
                        if rate > 80:
                            state["terminal_at"] = at
                        request = {
                            "path": "/scrobble/stop",
                            "body": {**item, "progress": rate},
                        }
                    elif kind == "progress" and rate is not None:
                        if at - state.get("last_start", 0) >= 60:
                            state["last_start"] = at
                            request = {
                                "path": "/scrobble/start",
                                "body": {**item, "progress": rate},
                            }
            if request:
                # 同一播放尚未发出的心跳只保留最新一条；停止事件不会被它覆盖。
                if request["path"] == "/scrobble/start":
                    ledger["watching"] = key
                    ledger["queue"] = [
                        j
                        for j in ledger["queue"]
                        if not (j["key"] == key and j["path"] == "/scrobble/start")
                    ]
                ledger["queue"].append(
                    {
                        **request,
                        "id": event_id,
                        "key": key,
                        "item": item,
                        "occurred_at": occurred_at,
                        "generation": account["generation"],
                        "source": {
                            "media_id": event["media"]["id"],
                            "file_id": event.get("file_id"),
                        },
                        "attempt": 0,
                        "next_at": self.clock(),
                        "uncertain": False,
                    }
                )
            ledger["sessions"][key] = state
            ledger["sessions"] = dict(
                sorted(
                    ledger["sessions"].items(),
                    key=lambda pair: pair[1].get("at", pair[1].get("terminal_at", 0)),
                )[-16:]
            )
            ledger["seen"] = [*ledger["seen"], event_id][-256:]
            await self.save_ledger(member, ledger)

            if (request and request["path"] != "/scrobble/start") or (
                not item and kind not in ("started", "progress")
            ):
                retained = request and any(j["id"] == event_id for j in ledger["queue"])
                await self.record_activity(
                    member,
                    event_id,
                    **describe(event, kind),
                    occurred_at=occurred_at,
                    state="queued" if retained else "skipped",
                    reason=None
                    if retained
                    else self.drop_warning(ledger)
                    if request
                    else "缺少有效的外部编号，未发送到 Trakt",
                )

    async def authorized(self, member, account, method, path, body=None, params=None):
        # 一次投递可能先查历史再写入；前一步可能已经轮换了单次刷新令牌。
        account = await self.get(member, "account", account)
        if member in self.rotated:
            account = self.rotated[member]
            await self.set(member, "account", account, secret=True)
            self.rotated.pop(member, None)
        if account.get("expires_at", 0) <= self.clock() + 120:
            account = await self.refresh(member, account)
        try:
            return await self.client.request(method, path, body, account["access_token"], params)
        except TraktError as exc:
            if exc.status != 401:
                raise
            account = await self.refresh(member, account)
            return await self.client.request(method, path, body, account["access_token"], params)

    async def refresh(self, member, account):
        tokens = await self.client.refresh(account["refresh_token"])
        updated = {**account, **self.client.credentials(tokens, now=self.clock())}
        # 刷新令牌只能用一次；持久化失败时留住新令牌，下轮先补写，绝不重复兑换旧令牌。
        self.rotated[member] = updated
        await self.set(member, "account", updated, secret=True)
        self.rotated.pop(member, None)
        return updated

    async def history_exists(self, member, account, job):
        when = timestamp(job["occurred_at"])
        kind = "movies" if "movie" in job["item"] else "episodes"
        for page in range(1, 1001):
            rows = await self.authorized(
                member,
                account,
                "GET",
                f"/sync/history/{kind}",
                params={
                    "start_at": iso_time(when - 90),
                    "end_at": iso_time(when + 90),
                    "limit": 100,
                    "page": page,
                },
            )
            for row in rows:
                if matches(job["item"], row) and abs(timestamp(row["watched_at"]) - when) <= 60:
                    return True
            if len(rows) < 100:
                return False
        raise TraktError(502)

    async def drain(self, member):
        async with self.locks[member]:
            account = await self.get(member, "account", {})
            if (
                not self.enabled
                or not account.get("access_token")
                or account.get("blocked")
                or account.get("client_id") != self.client.client_id
            ):
                await self.store.delete("delivery", scope=self.scope(member))
                return
            await self.store.delete("storage_error", scope=self.scope(member))
            ledger = await self.ledger(member)
            if not ledger["queue"]:
                await self._retry_locked(member, automatic=True)
                ledger = await self.ledger(member)
                if not ledger["queue"]:
                    return
            if ledger["queue"][0]["next_at"] > self.clock():
                return
            job = ledger["queue"][0]
            if job["generation"] != account["generation"]:
                ledger["queue"].pop(0)
                await self.save_ledger(member, ledger)
                return
            try:
                if job.get("source") and (job.get("attempt") or job.get("repair_attempt")):
                    job["revalidate"] = True
                await self.refresh_job(job)
                late = self.clock() - timestamp(job["occurred_at"]) > 300
                finished = self.finished(job)
                uncertain = job["uncertain"]
                if late and not finished:
                    result = {"action": "expired"}
                else:
                    # 写前记录不确定状态，覆盖远端成功但本地落盘前进程退出的窗口。
                    if finished:
                        job["uncertain"] = True
                        await self.save_ledger(member, ledger)
                    if finished and (job["path"] == "/sync/history" or uncertain or late):
                        if await self.history_exists(member, account, job):
                            result = {"action": "already_synced"}
                        else:
                            if (
                                job.get("clear_watching")
                                and not late
                                and ledger.get("watching") == job["key"]
                            ):
                                watching = await self.authorized(
                                    member, account, "GET", "/users/me/watching"
                                )
                                if matches(job["item"], watching):
                                    # pause 不生成已看历史；再用原观看时间写唯一一条历史。
                                    await self.authorized(
                                        member,
                                        account,
                                        "POST",
                                        "/scrobble/pause",
                                        {**job["item"], "progress": 100},
                                    )
                            result = await self.authorized(
                                member,
                                account,
                                "POST",
                                "/sync/history",
                                history_body(job["item"], job["occurred_at"]),
                            )
                    else:
                        result = await self.authorized(
                            member, account, "POST", job["path"], job["body"]
                        )
                    if result.get("not_found"):
                        if any(result["not_found"].values()):
                            raise TraktError(404)
            except LocalStateError as exc:
                if exc.deleted:
                    result = {"action": "local_deleted"}
                else:
                    ledger["queue"].pop(0)
                    job.update(repair_reason=exc.reason, retry_at=self.clock() + 600)
                    ledger["failed"] = [*ledger.get("failed", []), job][-64:]
                    await self.save_ledger(member, ledger)
                    return
            except OpsError:
                job["revalidate"] = True
                job["next_at"] = self.clock() + 600
                ledger["last_error"] = "暂时无法读取媒体元数据，稍后自动核对"
                await self.save_ledger(member, ledger)
                return
            except TraktError as exc:
                job["error_status"] = exc.status
                job["auth_error"] = exc.auth_error
                if exc.auth_error:
                    await self.block_auth(member, account, ledger)
                    return
                if (
                    exc.status == 422
                    and job["path"] in ("/scrobble/stop", "/scrobble/pause")
                    and 0 <= job["body"].get("progress", -1) < 1
                ):
                    # Trakt 会清除 watching，但不足 1% 的停止不保存进度，按协议返回 422。
                    result = {"action": "ignored_short_play"}
                elif exc.status == 409 and job["path"] == "/scrobble/stop":
                    result = {"action": "already_synced"}
                elif exc.status in (400, 404, 422):
                    # 无法匹配的一部片不能阻塞后面所有播放，保留可手动重试的失败记录。
                    ledger["queue"].pop(0)
                    job["retry_at"] = self.clock() + self.recovery_delay(job)
                    job["repair_attempt"] = job.get("repair_attempt", 0) + 1
                    ledger["failed"] = [*ledger.get("failed", []), job][-64:]
                    ledger["last_error"] = (
                        "有记录等待季集/编号核对；将定时自动核对，也可修正后立即重试"
                    )
                    await self.save_ledger(member, ledger)
                    await self.health.degraded(f"member-{member}", ledger["last_error"])
                    return
                else:
                    job["attempt"] += 1
                    job["uncertain"] = job["uncertain"] or exc.uncertain
                    job["next_at"] = self.clock() + max(
                        exc.retry_after, min(3600, 5 * 2 ** min(job["attempt"], 10))
                    )
                    ledger["last_error"] = str(exc)
                    await self.save_ledger(member, ledger)
                    await self.health.degraded(f"member-{member}", str(exc))
                    return
            ledger["queue"].pop(0)
            if ledger["queue"]:
                ledger["queue"][0]["next_at"] = max(ledger["queue"][0]["next_at"], self.clock() + 1)
            if job["path"] != "/scrobble/start" and ledger.get("watching") == job["key"]:
                ledger.pop("watching", None)
            if result.get("action") != "expired":
                ledger["last_success"] = iso_time(self.clock())
            ledger["last_error"] = (
                "有记录等待季集/编号核对；将定时自动核对，也可修正后立即重试"
                if ledger.get("failed")
                else None
            )
            await self.save_ledger(member, ledger)
            await self.complete_activity(member, job, result.get("action"))
            if ledger["last_error"]:
                await self.health.degraded(f"member-{member}", ledger["last_error"])
            else:
                await self.health.ok(f"member-{member}")

    async def retry_failed(self, member, delivery_id=None, correction=None):
        async with self.locks[member]:
            if (await self.get(member, "account", {})).get("blocked"):
                raise ValueError("请先断开并重新连接 Trakt 账号")
            return await self._retry_locked(member, delivery_id, correction=correction)

    def due_at(self, member, marker):
        return max(marker["next_at"], self.delivery_failures.get(member, (0, 0))[1])

    async def deliver_ready(self, work):
        for scope, marker in work.items():
            member = int(scope.split(":", 1)[1])
            if self.due_at(member, marker) > self.clock():
                continue
            try:
                await self.drain(member)
                self.delivery_failures.pop(member, None)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                attempt = self.delivery_failures.get(member, (0, 0))[0] + 1
                delay = min(300, 5 * 2 ** min(attempt, 6))
                self.delivery_failures[member] = (attempt, self.clock() + delay)
                self.log.warning(
                    "Trakt 单个成员投递暂未完成（%s），%s 秒后重试",
                    type(exc).__name__,
                    delay,
                )

    async def run(self):
        if not self.enabled:
            await asyncio.Future()
        failures = 0
        while True:
            self.wake.clear()
            try:
                await self.initialize()
                await self.deliver_ready(await self.store.scopes("delivery", entity="member"))
                work = await self.store.scopes("delivery", entity="member")
                delay = (
                    max(
                        1,
                        min(self.due_at(int(s.split(":", 1)[1]), m) for s, m in work.items())
                        - self.clock(),
                    )
                    if work
                    else None
                )
                failures = 0
                try:
                    await asyncio.wait_for(self.wake.wait(), timeout=delay)
                except TimeoutError:
                    pass
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failures += 1
                delay = min(300, 5 * 2 ** min(failures, 6))
                self.log.warning(
                    "Trakt 后台同步暂未完成（%s），%s 秒后重试",
                    type(exc).__name__,
                    delay,
                )
                await asyncio.sleep(delay)
