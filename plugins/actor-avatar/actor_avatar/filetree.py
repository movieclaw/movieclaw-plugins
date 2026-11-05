"""gfriends 头像索引（Filetree.json）的下载与缓存。

- 条件请求：带上次的 ``ETag``（``If-None-Match``），没变化时源站回 304，一个字节正文都不传；
- 传输压缩：请求带 ``Accept-Encoding: gzip``（原始 ~6.5 MB，压缩后 ~1 MB），httpx 自动解压；
- 本地缓存：解析通过的 JSON 以 gzip 存进插件私有目录，断网/源站故障时继续用旧索引；
- 内存里按 ETag 缓存解析好的 :class:`AvatarIndex`，同一进程不重复解析。
"""

from __future__ import annotations

import asyncio
import contextlib
import gzip
import json
import time
from typing import Any
from urllib.parse import quote

import httpx

from .matching import AvatarIndex

FILETREE = "Filetree.json"
CACHE_FILE = "filetree.json.gz"
META_KEY = "filetree:meta"
#: 索引的体积上限：防止镜像返回异常的大文件把内存吃满
MAX_BYTES = 64 * 1024 * 1024


class FiletreeError(Exception):
    """拿不到可用的索引（源站失败且没有本地缓存）。"""


def image_url(base: str, company: str, file: str) -> str:
    base = base if base.endswith("/") else base + "/"
    return f"{base}Content/{quote(company)}/{quote(file)}"


def _validate(data: Any) -> dict:
    if not isinstance(data, dict) or not isinstance(data.get("Content"), dict):
        raise FiletreeError("Filetree.json 格式不对（缺少 Content）")
    if not data["Content"]:
        raise FiletreeError("Filetree.json 是空的")
    return data


class FiletreeCache:
    def __init__(self, *, files: Any, store: Any, http: httpx.AsyncClient, logger: Any) -> None:
        self.files = files
        self.store = store
        self.http = http
        self.logger = logger
        self._index: AvatarIndex | None = None
        self._index_tag: str | None = None
        self._lock = asyncio.Lock()

    def _cache_path(self) -> str:
        return self.files.path("plugin", CACHE_FILE)

    async def meta(self) -> dict:
        value = await self.store.get(META_KEY, default={})
        return value if isinstance(value, dict) else {}

    async def index(self, base: str, ttl_hours: int, *, force: bool = False) -> AvatarIndex:
        async with self._lock:
            meta = await self.meta()
            fresh = (
                not force
                and meta.get("url") == base + FILETREE
                and time.time() - float(meta.get("checked_at") or 0) < ttl_hours * 3600
            )
            if fresh and self._index is not None and self._index_tag == meta.get("tag"):
                return self._index
            if not fresh:
                try:
                    meta = await self._refresh(base, meta)
                except (httpx.HTTPError, FiletreeError, ValueError) as exc:
                    if not await self.files.exists(self._cache_path()):
                        raise FiletreeError(f"下载头像索引失败：{exc}") from exc
                    self.logger.warning("actor-avatar：头像索引更新失败，继续用本地缓存：%s", exc)
                    meta = {**meta, "last_error": str(exc)[:300], "error_at": time.time()}
                    await self.store.set(META_KEY, meta)
            if self._index is not None and self._index_tag == meta.get("tag"):
                return self._index
            data = await self._read_cache()
            self._index = AvatarIndex(data)
            self._index_tag = meta.get("tag")
            return self._index

    async def _refresh(self, base: str, meta: dict) -> dict:
        url = base + FILETREE
        headers = {"Accept-Encoding": "gzip"}
        have_cache = await self.files.exists(self._cache_path())
        if meta.get("url") == url and meta.get("etag") and have_cache:
            headers["If-None-Match"] = str(meta["etag"])
        resp = await self.http.get(url, headers=headers)
        now = time.time()
        if resp.status_code == 304:
            meta = {**meta, "checked_at": now, "last_status": 304, "last_error": None}
            await self.store.set(META_KEY, meta)
            return meta
        resp.raise_for_status()
        raw = resp.content
        if len(raw) > MAX_BYTES:
            raise FiletreeError("Filetree.json 体积异常，已拒绝")
        data = _validate(json.loads(raw.decode("utf-8-sig")))
        await self._write_cache(raw)
        info = data.get("Information") or {}
        etag = resp.headers.get("etag")
        meta = {
            "url": url,
            "etag": etag,
            # tag 标识「本地缓存是哪一版」：没有 ETag 时用抓取时间
            "tag": etag or f"t{int(now)}",
            "fetched_at": now,
            "checked_at": now,
            "last_status": resp.status_code,
            "last_error": None,
            "bytes": len(raw),
            "wire_bytes": int(resp.headers.get("content-length") or 0) or None,
            "encoding": resp.headers.get("content-encoding"),
            "total": info.get("TotalNum"),
            "timestamp": info.get("Timestamp"),
            "companies": len(data["Content"]),
        }
        await self.store.set(META_KEY, meta)
        self._index = AvatarIndex(data)
        self._index_tag = meta["tag"]
        return meta

    async def _write_cache(self, raw: bytes) -> None:
        packed = await asyncio.to_thread(gzip.compress, raw, 6)
        path = self._cache_path()
        temp = f"{path}.tmp"
        handle = await self.files.open(temp, "wb")
        try:
            await asyncio.to_thread(handle.write, packed)
        finally:
            await asyncio.to_thread(handle.close)
        try:
            await self.files.rename(temp, path)
        except Exception:
            with contextlib.suppress(Exception):
                await self.files.remove(temp)
            raise

    async def _read_cache(self) -> dict:
        handle = await self.files.open(self._cache_path(), "rb")
        try:
            packed = await asyncio.to_thread(handle.read)
        finally:
            await asyncio.to_thread(handle.close)
        try:
            raw = await asyncio.to_thread(gzip.decompress, packed)
            return _validate(json.loads(raw.decode("utf-8-sig")))
        except (OSError, ValueError) as exc:
            raise FiletreeError(f"本地索引缓存损坏：{exc}") from exc
