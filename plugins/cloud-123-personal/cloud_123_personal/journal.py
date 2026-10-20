"""Per-upload write-ahead journal in the official scoped PLUGIN_DATA store.

No filesystem/database fallback. Authentication and signed URLs never enter records.
The host runs one active writer for this plugin; DataApi has no compare-and-swap.
"""
from contextlib import asynccontextmanager
from copy import deepcopy
import asyncio
import hashlib
import json
import time
import weakref

from movieclaw_sdk.drives import ApiChanged, Transient


class Journal:
    scope = "upload:v2"
    max_parts = 10000

    def __init__(self, store):
        self.store = store
        self.locks = weakref.WeakValueDictionary()

    @staticmethod
    def key(binding):
        return "upload-" + hashlib.sha256(json.dumps(binding,ensure_ascii=True,separators=(",", ":")).encode()).hexdigest()

    @asynccontextmanager
    async def locked(self, binding):
        if self.store is None:
            raise ApiChanged("上传需要宿主正式 PLUGIN_DATA 存储；当前未注入，未发送上传请求")
        key = self.key(binding)
        lock = self.locks.setdefault(key, asyncio.Lock())
        async with lock:
            yield key

    async def load(self, key, binding):
        try:
            value = await self.store.get(key, scope=self.scope)
        except Exception:
            raise Transient("无法读取上传日志；未继续网盘写入，请稍后重试") from None
        if value is None:
            return None
        try:
            value = deepcopy(value)
            if set(value) != {"version", "binding", "phase", "state", "next", "hashes", "file_id", "updated"}:
                raise ValueError
            if value["version"] != 2 or value["binding"] != list(binding):raise ValueError
            if value["phase"] not in {"initializing","parts","committing","committed","complete","reuse_ack"}:raise ValueError
            if type(value["next"]) is not int or not 0 <= value["next"] <= self.max_parts:raise ValueError
            if not isinstance(value["hashes"],dict) or len(value["hashes"]) > 2:raise ValueError
            for index,digest in value["hashes"].items():
                if not index.isdecimal() or not 0 <= int(index) < self.max_parts:raise ValueError
                if not isinstance(digest,str) or len(digest)!=64 or any(c not in '0123456789abcdef' for c in digest):raise ValueError
            if len(json.dumps(value,ensure_ascii=False).encode()) > 60*1024:raise ValueError
        except (TypeError,ValueError,KeyError,AttributeError):
            raise ApiChanged("上传日志格式损坏或版本不受支持；保留日志，未继续写入") from None
        return value

    async def save(self, key, record):
        record = {**record, "updated": int(time.time())}
        if len(json.dumps(record,ensure_ascii=False).encode()) > 60*1024:
            raise ApiChanged("上传日志超过60KiB安全上限")
        try:
            # Uses the host's existing encryption as additional protection for upload metadata.
            await self.store.set(key, deepcopy(record), scope=self.scope, secret=True)
        except Exception:
            raise Transient("上传日志保存失败；保留任务并重试，不能假定本次网盘操作已完成") from None

    @staticmethod
    def fresh(binding):
        return dict(version=2,binding=list(binding),phase="initializing",state=None,
                    next=0,hashes={},file_id=None,updated=int(time.time()))
