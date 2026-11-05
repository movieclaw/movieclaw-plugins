from __future__ import annotations

import gzip
import json
import os
import time

import httpx
import pytest
from actor_avatar.filetree import CACHE_FILE, META_KEY, FiletreeCache, FiletreeError, image_url
from actor_avatar.settings import JSDELIVR_BASE


class Source:
    """假镜像：记录请求，支持 ETag / 304 / 故障。"""

    def __init__(self, data: dict) -> None:
        self.body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.etag = 'W/"v1"'
        self.requests: list[httpx.Request] = []
        self.fail = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.fail:
            raise httpx.ConnectError("boom", request=request)
        if request.headers.get("if-none-match") == self.etag:
            return httpx.Response(304, headers={"etag": self.etag})
        return httpx.Response(200, content=self.body, headers={"etag": self.etag})


@pytest.fixture
def source(filetree_data):
    return Source(filetree_data)


@pytest.fixture
def cache(source, files, store, logger):
    http = httpx.AsyncClient(transport=httpx.MockTransport(source.handler))
    return FiletreeCache(files=files, store=store, http=http, logger=logger)


async def test_first_fetch_writes_gzip_cache(cache, source, files, store):
    index = await cache.index(JSDELIVR_BASE, 24)
    assert len(index) > 10
    assert str(source.requests[0].url) == JSDELIVR_BASE + "Filetree.json"
    assert "gzip" in source.requests[0].headers["accept-encoding"]
    assert "if-none-match" not in source.requests[0].headers
    path = files.path("plugin", CACHE_FILE)
    with open(path, "rb") as fh:
        assert json.loads(gzip.decompress(fh.read()))["Content"]
    meta = store.data[META_KEY]
    assert meta["etag"] == 'W/"v1"' and meta["total"] == 16 and meta["companies"] == 5
    assert not os.path.exists(path + ".tmp")


async def test_within_ttl_no_request(cache, source):
    first = await cache.index(JSDELIVR_BASE, 24)
    second = await cache.index(JSDELIVR_BASE, 24)
    assert first is second and len(source.requests) == 1


async def test_after_ttl_conditional_304(cache, source, store, files, logger):
    await cache.index(JSDELIVR_BASE, 24)
    store.data[META_KEY]["checked_at"] = time.time() - 25 * 3600
    # 新进程：内存索引为空，必须从磁盘缓存读
    fresh = FiletreeCache(files=files, store=store, http=cache.http, logger=logger)
    index = await fresh.index(JSDELIVR_BASE, 24)
    assert source.requests[-1].headers["if-none-match"] == 'W/"v1"'
    assert store.data[META_KEY]["last_status"] == 304
    assert index.lookup("涼森れむ")


async def test_changed_upstream_replaces_cache(cache, source, store, filetree_data):
    await cache.index(JSDELIVR_BASE, 24)
    filetree_data["Content"]["7-Moodyz"]["新人.jpg"] = "新人.jpg?t=1"
    source.body = json.dumps(filetree_data, ensure_ascii=False).encode()
    source.etag = 'W/"v2"'
    index = await cache.index(JSDELIVR_BASE, 24, force=True)
    assert index.lookup("新人") and store.data[META_KEY]["etag"] == 'W/"v2"'


async def test_network_error_falls_back_to_cache(cache, source, store):
    await cache.index(JSDELIVR_BASE, 24)
    source.fail = True
    index = await cache.index(JSDELIVR_BASE, 24, force=True)
    assert index.lookup("涼森れむ")
    assert "boom" in store.data[META_KEY]["last_error"]


async def test_network_error_without_cache_raises(cache, source):
    source.fail = True
    with pytest.raises(FiletreeError):
        await cache.index(JSDELIVR_BASE, 24)


async def test_bad_payload_rejected(cache, source):
    source.body = b'{"Information": {}}'
    with pytest.raises(FiletreeError):
        await cache.index(JSDELIVR_BASE, 24)


async def test_mirror_change_refetches_without_etag(cache, source):
    await cache.index(JSDELIVR_BASE, 24)
    await cache.index("https://raw.githubusercontent.com/gfriends/gfriends/master/", 24)
    assert len(source.requests) == 2
    assert "if-none-match" not in source.requests[1].headers


def test_image_url_quotes_company_and_file():
    url = image_url(JSDELIVR_BASE, "z-DMM(骑)", "AI-Fix-涼森れむ.jpg")
    assert url == (
        "https://cdn.jsdelivr.net/gh/gfriends/gfriends@master/Content/"
        "z-DMM%28%E9%AA%91%29/AI-Fix-%E6%B6%BC%E6%A3%AE%E3%82%8C%E3%82%80.jpg"
    )
    assert (
        image_url("https://m.example/x", "a b", "c.jpg")
        == "https://m.example/x/Content/a%20b/c.jpg"
    )
