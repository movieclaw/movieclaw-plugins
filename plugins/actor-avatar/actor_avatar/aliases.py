"""中文译名 → 日文原名的对照表（来自 MDCx 的 ``mapping_actor.xml``）。

NFO 里的演员名常是刮削器翻成的中文（「凉森玲梦」），gfriends 头像库只按日文原名存
（「涼森れむ」）。MDCx 维护了一份演员名对照表：每条有简中 / 繁中 / 日文名与一串别名。

- **不随插件打包**：MDCx 以 GPL-3.0（另附非商用条款）发布，表由插件运行时从镜像下载
  （约 0.9 MB，gzip 传输约 0.3 MB），带 ETag 条件请求，解析后只把精简的
  「折叠键 → 日文名」存进插件私有目录；
- **只取无歧义的键**：一个键对应多个不同日文名的丢掉；带【誤認注意】的条目整条不用；
  带【同名異人】的条目只用它自己的简/繁/日文名，不用别名串；
- 键用 :func:`matching.fold` 归一，与头像索引同一套折叠规则。
"""

from __future__ import annotations

import asyncio
import contextlib
import gzip
import time
import xml.etree.ElementTree as ET
from typing import Any

import httpx

from .matching import fold

MDCX_PATH = "resources/mapping_table/mapping_actor.xml"
JSDELIVR_URL = "https://cdn.jsdelivr.net/gh/sqzw-x/mdcx@master/" + MDCX_PATH
GITHUB_URL = "https://raw.githubusercontent.com/sqzw-x/mdcx/master/" + MDCX_PATH
CACHE_FILE = "name_table.tsv.gz"
META_KEY = "name_table:meta"
#: 原表 ~0.9 MB；上限防镜像返回异常大文件
MAX_BYTES = 16 * 1024 * 1024
#: 折叠后短于这个长度的键不用：单字、两字的别名撞名太多
MIN_KEY_LEN = 2
# 解析规则的版本：规则变了，旧缓存（解析后的表）作废，下次不带 If-None-Match 整份重下重解析
PARSER_VERSION = 2
_MISREAD = "【誤認注意】"
_HOMONYM = "【同名異人】"


class NameTableError(Exception):
    """拿不到可用的译名表（源站失败且没有本地缓存）。"""


def parse_mapping(raw: bytes) -> dict[str, str]:
    """``mapping_actor.xml`` → ``{折叠键: 日文名}``，有歧义的键已剔除。"""
    if len(raw) > MAX_BYTES:
        raise NameTableError("译名表体积异常，已拒绝")
    if b"<!ENTITY" in raw or b"<!DOCTYPE" in raw:
        raise NameTableError("译名表含 DTD/实体声明，已拒绝")
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise NameTableError(f"译名表格式不对：{exc}") from exc
    seen: dict[str, set[str]] = {}
    for node in root.iter("a"):
        jp = (node.get("jp") or "").strip()
        keyword = node.get("keyword") or ""
        parts = [k.strip() for k in keyword.split(",") if k.strip()]
        # 整条标「误认注意」才整条不用；更常见的是某个别名后缀「…【誤認注意】」，只丢那个别名
        if not jp or _MISREAD in parts:
            continue
        names = [node.get("zh_cn") or "", node.get("zh_tw") or "", jp]
        if _HOMONYM not in parts:
            names += [k for k in parts if "【" not in k]
        for name in names:
            key = fold(name)
            if len(key) < MIN_KEY_LEN:
                continue
            seen.setdefault(key, set()).add(jp)
    table = {key: next(iter(jps)) for key, jps in seen.items() if len(jps) == 1}
    if not table:
        raise NameTableError("译名表是空的")
    return table


def dump_table(table: dict[str, str]) -> bytes:
    lines = (f"{k}\t{v}" for k, v in sorted(table.items()))
    return gzip.compress("\n".join(lines).encode("utf-8"), 6)


def load_table(packed: bytes) -> dict[str, str]:
    text = gzip.decompress(packed).decode("utf-8")
    table: dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.partition("\t")
        if sep and key and value:
            table[key] = value
    return table


class NameTable:
    """查表：中文 / 繁体 / 别名 → 日文原名（查不到为 None）。"""

    def __init__(self, table: dict[str, str] | None = None) -> None:
        self.table = table or {}

    def __len__(self) -> int:
        return len(self.table)

    def search(self, query: str, limit: int = 10) -> list[tuple[str, str]]:
        """``[(译名键, 日文名)]``：键里含查询串的（给页面的自动补全）。"""
        key = fold(query or "")
        if not key:
            return []
        out: list[tuple[str, str]] = []
        for alias, target in self.table.items():
            if key in alias:
                out.append((alias, target))
                if len(out) >= limit:
                    break
        return out

    def get(self, name: str) -> str | None:
        return self.table.get(fold(name))


class NameTableCache:
    """译名表的下载与缓存：与头像索引同一套做法（ETag 条件请求、本地缓存兜底）。"""

    def __init__(self, *, files: Any, store: Any, http: httpx.AsyncClient, logger: Any) -> None:
        self.files = files
        self.store = store
        self.http = http
        self.logger = logger
        self._table: NameTable | None = None
        self._tag: str | None = None
        self._lock = asyncio.Lock()

    def _cache_path(self) -> str:
        return self.files.path("plugin", CACHE_FILE)

    async def meta(self) -> dict:
        value = await self.store.get(META_KEY, default={})
        return value if isinstance(value, dict) else {}

    async def table(self, url: str, ttl_hours: int, *, force: bool = False) -> NameTable:
        async with self._lock:
            meta = await self.meta()
            stale_parser = meta.get("parser") != PARSER_VERSION
            fresh = (
                not force
                and not stale_parser
                and meta.get("url") == url
                and time.time() - float(meta.get("checked_at") or 0) < ttl_hours * 3600
            )
            if fresh and self._table is not None and self._tag == meta.get("tag"):
                return self._table
            if not fresh:
                try:
                    meta = await self._refresh(url, meta)
                except (httpx.HTTPError, NameTableError, ValueError) as exc:
                    if not await self.files.exists(self._cache_path()):
                        raise NameTableError(f"下载译名表失败：{exc}") from exc
                    self.logger.warning("actor-avatar：译名表更新失败，继续用本地缓存：%s", exc)
                    meta = {**meta, "last_error": str(exc)[:300], "error_at": time.time()}
                    await self.store.set(META_KEY, meta)
            if self._table is not None and self._tag == meta.get("tag"):
                return self._table
            self._table = NameTable(await self._read_cache())
            self._tag = meta.get("tag")
            return self._table

    async def _refresh(self, url: str, meta: dict) -> dict:
        headers = {"Accept-Encoding": "gzip"}
        have_cache = await self.files.exists(self._cache_path())
        if (
            meta.get("url") == url
            and meta.get("etag")
            and have_cache
            and meta.get("parser") == PARSER_VERSION
        ):
            headers["If-None-Match"] = str(meta["etag"])
        resp = await self.http.get(url, headers=headers)
        now = time.time()
        if resp.status_code == 304:
            meta = {**meta, "checked_at": now, "last_status": 304, "last_error": None}
            await self.store.set(META_KEY, meta)
            return meta
        resp.raise_for_status()
        table = await asyncio.to_thread(parse_mapping, resp.content)
        packed = await asyncio.to_thread(dump_table, table)
        await self._write_cache(packed)
        etag = resp.headers.get("etag")
        meta = {
            "url": url,
            "etag": etag,
            "tag": etag or f"t{int(now)}",
            "fetched_at": now,
            "checked_at": now,
            "last_status": resp.status_code,
            "last_error": None,
            "bytes": len(resp.content),
            "keys": len(table),
            "parser": PARSER_VERSION,
        }
        await self.store.set(META_KEY, meta)
        self._table = NameTable(table)
        self._tag = meta["tag"]
        return meta

    async def _write_cache(self, packed: bytes) -> None:
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

    async def _read_cache(self) -> dict[str, str]:
        handle = await self.files.open(self._cache_path(), "rb")
        try:
            packed = await asyncio.to_thread(handle.read)
        finally:
            await asyncio.to_thread(handle.close)
        try:
            return await asyncio.to_thread(load_table, packed)
        except (OSError, ValueError, EOFError) as exc:
            raise NameTableError(f"本地译名表缓存损坏：{exc}") from exc
