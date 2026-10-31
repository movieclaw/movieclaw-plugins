"""已有 PLUGIN_DATA 门面的窄边界；不可变记录和有界、可重入的本地清理。"""
from __future__ import annotations

import asyncio
import json
import re
import time
from weakref import WeakValueDictionary

from movieclaw_sdk import drives as dv

SCHEMA = 5
MAX_VALUE = 64 * 1024  # 与 PLUGIN_DATA 的单值限制一致。
RETENTION = 7 * 86400
GRACE = 86400
HEX64 = re.compile(r"[0-9a-f]{64}\Z")
HEX32 = re.compile(r"[0-9a-f]{32}\Z")
DATA_KEY = re.compile(r"info|merge_requested|activity|(?:part|put_requested):(?:0|[1-9][0-9]*)\Z")


def valid_ref(value):
    return (isinstance(value, dict) and type(value.get("schema")) is int and value["schema"] == SCHEMA
            and isinstance(value.get("session_key"), str) and HEX64.fullmatch(value["session_key"])
            and isinstance(value.get("generation"), str) and HEX32.fullmatch(value["generation"]))


def encoded_size(value):
    try:
        return len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8"))
    except (ValueError, TypeError, OverflowError):
        raise dv.Conflict("123 插件存储数据无法序列化") from None


class UploadStore:
    def __init__(self, store):
        self.store = store
        self.locks = WeakValueDictionary()
        self.gc_lock = asyncio.Lock()
        self.pending = {}
        self.checked = False

    def lock(self, session_key):
        lock = self.locks.get(session_key)
        if lock is None:
            lock = self.locks[session_key] = asyncio.Lock()
        return lock

    async def call(self, method, *, scope="global", key=None, value=None, default=None):
        if (not re.fullmatch(r"global|[a-z_]+:[A-Za-z0-9_.\-]{1,100}", scope)
                or key is not None and (not isinstance(key, str) or not 1 <= len(key) <= 200)
                or method == "set" and encoded_size(value) > MAX_VALUE):
            raise dv.Conflict("123 插件存储参数或容量超限")
        if self.store is None:
            raise dv.Conflict("123 上传需要注入 PLUGIN_DATA 服务")
        kwargs = {"scope": scope}
        if key is not None:
            kwargs["key"] = key
        if method == "set":
            kwargs.update(value=value, secret=True)
        if method == "get":
            kwargs["default"] = default
        try:
            async with asyncio.timeout(5):
                return await getattr(self.store, method)(**kwargs)
        except (LookupError, PermissionError):
            raise dv.Conflict("123 插件数据服务不可用，请修复服务配置") from None
        except ValueError:
            if method in ("get", "items"):
                raise dv.Conflict("123 插件数据不可读取，请检查数据或密钥") from None
            raise dv.Transient("123 插件数据写入结果未知（ValueError），请重试核对") from None
        except Exception as error:
            raise dv.Transient(f"123 插件数据 {method} 失败（{type(error).__name__}），请重试核对") from None

    async def ready(self):
        if not self.checked:
            await self.call("get", key="upload_selfcheck")
            self.checked = True

    async def get(self, scope, key):
        return await self.call("get", scope=scope, key=key)

    async def has(self, scope, key):
        # 门面的 default 在本地处理；显式 null 也是已存在的坏标记，不能当成未发送。
        missing = object()
        return await self.call("get", scope=scope, key=key, default=missing) is not missing

    async def items(self, scope):
        result = await self.call("items", scope=scope)
        if not isinstance(result, dict):
            raise dv.Conflict("123 插件数据列表无效")
        return result

    async def save(self, scope, key, value):
        if encoded_size(value) > MAX_VALUE:
            raise dv.Conflict("123 插件单值容量超限")
        prior = await self.get(scope, key)
        if prior is not None:
            if prior != value:
                raise dv.Conflict("123 插件不可变记录冲突")
            self.pending.pop((scope, key), None)
            return
        # 发出前保留同键同内容，取消/超时也不意味着回滚；不在异常路径补写。
        self.pending[(scope, key)] = value
        await self.call("set", scope=scope, key=key, value=value)
        self.pending.pop((scope, key), None)

    async def resolve(self, session_key):
        for (scope, key), value in list(self.pending.items()):
            if value.get("session_key") != session_key:
                continue
            generation = value["generation"]
            if await self.get("upload:" + session_key, "retired:" + generation) is not None:
                if await self.get(scope, key) == value:
                    self.pending.pop((scope, key), None)
                continue
            await self.save(scope, key, value)

    async def register(self, index):
        bucket = "upload_gc:" + index["session_key"][:2]
        key = index["session_key"] + ":" + index["generation"]
        await self.save(bucket, key, index)

    async def collect(self):
        # 维护是可选操作；单轮一桶、四候选、最多十六次删除。
        if self.store is None or self.gc_lock.locked():
            return
        async with self.gc_lock:
            try:
                async with asyncio.timeout(1):
                    cursor = await self.get("global", "upload_gc_cursor")
                    if (not isinstance(cursor, dict) or type(cursor.get("bucket")) is not int
                            or not 0 <= cursor["bucket"] < 256 or not isinstance(cursor.get("after"), str)):
                        cursor = {"bucket": 0, "after": ""}
                    bucket = cursor["bucket"]
                    scope = f"upload_gc:{bucket:02x}"
                    # 先推进提示，慢/坏桶或候选中断也不能长期挡住后续条目。
                    await self.call("set", key="upload_gc_cursor", value={"bucket": (bucket + 1) % 256, "after": ""})
                    try:
                        rows = await self.items(scope)
                    except dv.DriveError:
                        rows = {}
                    keys = sorted(k for k in rows if k > cursor["after"])
                    started = time.monotonic()
                    budget = [16]
                    for key in keys[:4]:
                        cursor["after"] = key
                        await self.call("set", key="upload_gc_cursor", value=dict(cursor))
                        item = rows[key]
                        if (not valid_ref(item) or key != item["session_key"] + ":" + item["generation"]
                                or item["session_key"][:2] != f"{bucket:02x}"):
                            continue
                        lock = self.lock(item["session_key"])
                        if lock.locked():
                            continue
                        try:
                            async with lock:
                                await self._collect_one(item, budget)
                        except dv.DriveError:
                            pass
                        if not budget[0] or time.monotonic() - started >= 1:
                            break
                    if not keys or cursor["after"] == keys[-1]:
                        cursor = {"bucket": (bucket + 1) % 256, "after": ""}
                    await self.call("set", key="upload_gc_cursor", value=cursor)
            except (dv.DriveError, TimeoutError):
                # 不影响上传；取消不属于 DriveError，仍传播。
                pass

    async def _collect_one(self, item, budget):
        session_key, generation = item["session_key"], item["generation"]
        scope, data_scope = "upload:" + session_key, "upload_data:" + generation
        for (pending_scope, key), value in list(self.pending.items()):
            if value.get("generation") == generation:
                if await self.get(pending_scope, key) != value:
                    return
                self.pending.pop((pending_scope, key), None)
        retired = await self.get(scope, "retired:" + generation)
        now = time.time()
        if retired is None:
            if await self.has(data_scope, "merge_requested"):
                return
            intent = await self.get(scope, "intent:" + generation)
            info = await self.get(data_scope, "info")
            if info is not None and intent is None:
                return
            for record in (intent, info):
                if record is not None and (not valid_ref(record) or record["session_key"] != session_key
                                           or record["generation"] != generation):
                    return
            created = item.get("created_at")
            activity = await self.get(data_scope, "activity")
            if type(created) not in (int, float) or not 0 <= created <= now:
                return
            if activity is not None:
                if (not isinstance(activity, dict) or activity.get("session_key") != session_key
                        or activity.get("generation") != generation or type(activity.get("at")) not in (int, float)
                        or not 0 <= activity["at"] <= now):
                    return
                created = max(created, activity["at"])
            if now - created < RETENTION:
                return
            if intent is None:
                # 登记中断或清理尾部只剩索引；恢复不会复用它，无需重建退役标记。
                if not await self.items(data_scope) and budget[0]:
                    budget[0] -= 1
                    await self.call("delete", scope="upload_gc:" + session_key[:2],
                                    key=session_key + ":" + generation)
                return
            retired = {**item, "retired_at": now}
            await self.save(scope, "retired:" + generation, retired)
        if (not valid_ref(retired) or retired["session_key"] != session_key
                or retired["generation"] != generation or type(retired.get("retired_at")) not in (int, float)
                or not 0 <= retired["retired_at"] <= now):
            return
        rows = await self.items(data_scope)
        if any(not isinstance(key, str) or len(key) > 200 or not DATA_KEY.fullmatch(key) for key in rows):
            return
        # 逐批删除任意片数；重入不要求 info/片确认齐全，不新增清理记录。
        deleting = sorted(rows)[:budget[0]]
        for key in deleting:
            if not budget[0]:
                return
            budget[0] -= 1
            await self.call("delete", scope=data_scope, key=key)
        if len(rows) > len(deleting) or now - retired["retired_at"] < GRACE:
            return
        if await self.items(data_scope):
            return
        if budget[0] < 3:
            return
        budget[0] -= 3
        await self.call("delete", scope=scope, key="intent:" + generation)
        await self.call("delete", scope=scope, key="retired:" + generation)
        await self.call("delete", scope="upload_gc:" + session_key[:2], key=session_key + ":" + generation)
