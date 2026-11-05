from __future__ import annotations

import gzip

import httpx
import pytest
from actor_avatar.aliases import (
    CACHE_FILE,
    NameTable,
    NameTableCache,
    NameTableError,
    dump_table,
    load_table,
    parse_mapping,
)
from actor_avatar.matching import fold

from .conftest import FIXTURES

URL = "https://cdn.example.com/mapping_actor.xml"


@pytest.fixture
def raw():
    return (FIXTURES / "mapping_actor.xml").read_bytes()


def test_parse_mapping(raw):
    table = NameTable(parse_mapping(raw))
    assert table.get("凉森玲梦") == "涼森れむ"
    assert table.get("涼森玲夢") == "涼森れむ"
    assert table.get("傻梦") == "涼森れむ"  # keyword 别名
    assert table.get("八挂 海") == "八掛うみ"  # 空白归一
    assert table.get("野野浦暖") == "野々浦暖"
    # 整条标【誤認注意】：整条不用
    assert table.get("酸素") is None and table.get("错误") is None
    # 只有某个别名带【誤認注意】后缀：只丢那个别名，其余照用
    assert table.get("大槻响") == "大槻ひびき" and table.get("安藤絵里") == "大槻ひびき"
    assert table.get("まひる") is None
    # 一个键对应多个日文名：丢掉
    assert table.get("美月") is None and table.get("共用名") is None
    assert table.get("美月レイア") == "美月レイア"
    # 【同名異人】只用自己的简/繁/日文名，不用别名串
    assert table.get("水卜樱") == "水卜さくら"
    assert table.get("同名别名") is None


def test_rejects_dtd_and_garbage():
    with pytest.raises(NameTableError):
        parse_mapping(b'<?xml version="1.0"?><!DOCTYPE a [<!ENTITY x "y">]><actor/>')
    with pytest.raises(NameTableError):
        parse_mapping(b"<actor><a")
    with pytest.raises(NameTableError):
        parse_mapping(b"<actor></actor>")


def test_dump_roundtrip(raw):
    table = parse_mapping(raw)
    assert load_table(dump_table(table)) == table
    assert all(k == fold(k) for k in table)


class Source:
    def __init__(self, body: bytes) -> None:
        self.body = body
        self.fail = False
        self.seen: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(dict(request.headers))
        if self.fail:
            return httpx.Response(503)
        if request.headers.get("if-none-match") == '"v1"':
            return httpx.Response(304)
        return httpx.Response(200, content=self.body, headers={"etag": '"v1"'})


async def test_cache_etag_and_fallback(files, store, logger, raw):
    src = Source(raw)
    http = httpx.AsyncClient(transport=httpx.MockTransport(src.handler))
    cache = NameTableCache(files=files, store=store, http=http, logger=logger)
    table = await cache.table(URL, 24)
    assert table.get("凉森玲梦") == "涼森れむ"
    path = files.path("plugin", CACHE_FILE)
    with open(path, "rb") as fh:
        assert gzip.decompress(fh.read()).decode().count("\t") == len(table)
    meta = await cache.meta()
    assert meta["etag"] == '"v1"' and meta["keys"] == len(table)

    # 新鲜期内不请求
    await cache.table(URL, 24)
    assert len(src.seen) == 1
    # 过期：条件请求 304
    await cache.table(URL, 24, force=True)
    assert src.seen[-1].get("if-none-match") == '"v1"'

    # 源站挂了：新进程（内存里没有表）读本地缓存
    src.fail = True
    fresh = NameTableCache(files=files, store=store, http=http, logger=logger)
    assert (await fresh.table(URL, 24, force=True)).get("八挂海") == "八掛うみ"
    assert (await fresh.meta())["last_error"]


async def test_cache_no_source_no_cache(files, store, logger):
    src = Source(b"")
    src.fail = True
    http = httpx.AsyncClient(transport=httpx.MockTransport(src.handler))
    cache = NameTableCache(files=files, store=store, http=http, logger=logger)
    with pytest.raises(NameTableError):
        await cache.table(URL, 24)


async def test_parser_upgrade_discards_old_cache(files, store, logger, raw):
    """旧版本解析出的缓存：即使 ETag 没变也要整份重下（不带 If-None-Match）重新解析。"""
    src = Source(raw)
    http = httpx.AsyncClient(transport=httpx.MockTransport(src.handler))
    cache = NameTableCache(files=files, store=store, http=http, logger=logger)
    await cache.table(URL, 24)
    meta = await cache.meta()
    meta.pop("parser")
    await store.set("name_table:meta", meta)
    fresh = NameTableCache(files=files, store=store, http=http, logger=logger)
    await fresh.table(URL, 24)
    assert len(src.seen) == 2 and "if-none-match" not in src.seen[-1]
    assert (await fresh.meta())["parser"] >= 2
