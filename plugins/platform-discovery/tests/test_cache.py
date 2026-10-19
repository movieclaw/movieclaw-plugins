"""Public SDK cache persistence, corruption boundaries and source SWR behavior."""

import asyncio
import base64
import copy
import hashlib
import importlib.util
import json
import random
import sys
import time
import types
import unittest
import zlib
from pathlib import Path
from unittest.mock import patch

import httpx
from pydantic import SecretStr


package_dir = Path(__file__).parents[1] / "platform_discovery"
package = types.ModuleType("platform_discovery_cache_tests")
package.__path__ = [str(package_dir)]
sys.modules[package.__name__] = package


def load_module(name):
    spec = importlib.util.spec_from_file_location(package.__name__ + "." + name, package_dir / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


cache = load_module("cache")
sources = load_module("sources")


class FakeSDKStore:
    """The host enforces 64 KiB for each serialized plugin-data value."""

    def __init__(self):
        self.values, self.writes, self.deletes = {}, [], []
        self.fail_set = None

    async def get(self, key, *, scope, default=None):
        return copy.deepcopy(self.values.get((scope, key), default))

    async def set(self, key, value, *, scope):
        raw = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
        if len(raw) > 64 * 1024:
            raise ValueError("SDK value exceeds 64 KiB")
        if self.fail_set is not None and self.fail_set(key, scope, value):
            raise OSError("PRIVATE storage diagnostic")
        self.writes.append((scope, key, len(raw)))
        self.values[scope, key] = copy.deepcopy(value)

    async def delete(self, key, *, scope):
        self.deletes.append((scope, key))
        self.values.pop((scope, key), None)


class PublicCacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_large_public_json_round_trips_below_sdk_value_limit(self):
        sdk = FakeSDKStore()
        storage = cache.PublicCacheStore(sdk)
        value = {"titles": [{"id": 1, "description": base64.b64encode(random.Random(8).randbytes(180000)).decode()}]}
        self.assertGreater(len(json.dumps(value).encode()), 64 * 1024)
        await storage.set("large-feed", value, 3600)
        self.assertEqual(await cache.PublicCacheStore(sdk).get("large-feed"), value)
        chunks = [key for _, key, _ in sdk.writes if key.startswith("b-")]
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(length < 64 * 1024 for _, _, length in sdk.writes))

    async def test_manifest_failure_preserves_previous_committed_payload(self):
        sdk = FakeSDKStore()
        storage = cache.PublicCacheStore(sdk)
        before = {"items": [{"title": "原有真实片单"}]}
        await storage.set("feed", before, 60)
        old_manifest = copy.deepcopy(sdk.values[storage.scope, "m-" + storage._id("feed")])
        sdk.fail_set = lambda key, scope, value: key.startswith("m-")
        with self.assertRaises(OSError):
            await storage.set("feed", {"items": [{"title": "新片单尚未提交"}]}, 60)
        self.assertEqual(sdk.values[storage.scope, "m-" + storage._id("feed")], old_manifest)
        self.assertEqual(await cache.PublicCacheStore(sdk).get("feed"), before)

    async def test_chunk_failure_preserves_previous_committed_payload(self):
        sdk = FakeSDKStore()
        storage = cache.PublicCacheStore(sdk)
        await storage.set("feed", {"items": ["原片单"]}, 60)
        sdk.fail_set = lambda key, scope, value: key.startswith("b-")
        with self.assertRaises(OSError):
            await storage.set("feed", {"items": ["新片单"]}, 60)
        self.assertEqual(await storage.get("feed"), {"items": ["原片单"]})

    async def test_corrupt_metadata_and_chunks_are_rejected(self):
        sdk = FakeSDKStore()
        storage = cache.PublicCacheStore(sdk)
        await storage.set("feed", {"items": ["真实公开片单"]}, 60)
        original = copy.deepcopy(sdk.values)
        meta_key = (storage.scope, "m-" + storage._id("feed"))
        chunk_key = next(key for key in sdk.values if key[1].startswith("b-"))
        changes = (
            ("wrong-version", lambda: sdk.values[meta_key].update(version=1)),
            ("wrong-key", lambda: sdk.values[meta_key].update(key="another-feed")),
            ("bad-time", lambda: sdk.values[meta_key].update(saved_at="broken")),
            ("nonfinite-time", lambda: sdk.values[meta_key].update(saved_at=float("nan"))),
            ("too-old", lambda: sdk.values[meta_key].update(saved_at=time.time() - cache.MAX_AGE - 1)),
            ("zero-chunks", lambda: sdk.values[meta_key].update(chunks=0)),
            ("too-many-chunks", lambda: sdk.values[meta_key].update(chunks=129)),
            ("bad-generation", lambda: sdk.values[meta_key].update(generation="bad")),
            ("wrong-hash", lambda: sdk.values[meta_key].update(sha256="0" * 64)),
            ("missing-chunk", lambda: sdk.values.pop(chunk_key)),
            ("oversized-chunk", lambda: sdk.values.__setitem__(chunk_key, "x" * (cache.CHUNK_SIZE + 1))),
            ("bad-base64", lambda: sdk.values.__setitem__(chunk_key, "not!base64!")),
        )
        for name, corrupt in changes:
            with self.subTest(name=name):
                sdk.values = copy.deepcopy(original)
                corrupt()
                self.assertIsNone(await storage.get("feed"))

    async def test_oversized_values_and_compression_bombs_are_rejected(self):
        sdk = FakeSDKStore()
        storage = cache.PublicCacheStore(sdk)
        with self.assertRaises(ValueError):
            await storage.set("oversized", {"overview": "x" * cache.MAX_BYTES}, 60)
        self.assertFalse(sdk.writes)
        raw = json.dumps({"overview": "x" * cache.MAX_BYTES}).encode()
        encoded = base64.b64encode(zlib.compress(raw)).decode()
        generation = "a" * 24
        identity = storage._id("bomb")
        sdk.values[storage.scope, "m-" + identity] = {"key": "bomb", "version": 2, "saved_at": time.time(),
            "generation": generation, "chunks": 1, "sha256": hashlib.sha256(raw).hexdigest()}
        sdk.values[storage.scope, "b-" + identity + "-" + generation + "-0"] = encoded
        self.assertIsNone(await storage.get("bomb"))

    async def test_scopes_and_logical_keys_do_not_mix(self):
        sdk = FakeSDKStore()
        us = cache.PublicCacheStore(sdk, "public-v2:US:tmdb")
        cn = cache.PublicCacheStore(sdk, "public-v2:CN:tmdb")
        await us.set("feed", {"title": "美国地区"}, 60)
        await cn.set("feed", {"title": "中国地区"}, 60)
        await us.set("other-feed", {"title": "另一个榜单"}, 60)
        self.assertNotEqual(us.scope, cn.scope)
        self.assertEqual(await us.get("feed"), {"title": "美国地区"})
        self.assertEqual(await cn.get("feed"), {"title": "中国地区"})
        self.assertEqual(await us.get("other-feed"), {"title": "另一个榜单"})
        self.assertIsNone(await cn.get("other-feed"))
        self.assertEqual(set(await cn.items()), {"feed"})

    async def test_tuple_expiry_is_restored_without_extending_ttl(self):
        sdk = FakeSDKStore()
        storage = cache.PublicCacheStore(sdk)
        with patch.object(cache, "time", types.SimpleNamespace(time=lambda: 1000, monotonic=lambda: 100)):
            live = cache.PersistentCache(storage)
            live[("netflix-match", "Squid Game", "tv")] = (160, {"poster_url": "https://image.tmdb.org/poster.jpg"})
            await live.close()
        with patch.object(cache, "time", types.SimpleNamespace(time=lambda: 1040, monotonic=lambda: 900)):
            restarted = cache.PersistentCache(cache.PublicCacheStore(sdk))
            await restarted.hydrate()
            expiry, value = restarted[("netflix-match", "Squid Game", "tv")]
            self.assertEqual(expiry, 920)
            self.assertEqual(value["poster_url"], "https://image.tmdb.org/poster.jpg")
            await restarted.close()
        with patch.object(cache, "time", types.SimpleNamespace(time=lambda: 1100, monotonic=lambda: 2000)):
            expired = cache.PersistentCache(cache.PublicCacheStore(sdk))
            await expired.hydrate()
            self.assertEqual(expired[("netflix-match", "Squid Game", "tv")][0], 1960)
            await expired.close()

    async def test_close_flushes_without_deleting_saved_data_or_overwriting_with_failure(self):
        sdk = FakeSDKStore()
        storage = cache.PublicCacheStore(sdk)
        live = cache.PersistentCache(storage)
        key = ("netflix", "tv", 1)
        live[key] = (time.monotonic() + 100, {"status": "ready", "items": [{"title": "真实剧集"}]})
        await live.flush()
        live[key] = (time.monotonic() + 120, {"status": "unavailable", "items": []})
        await live.close()
        self.assertFalse(sdk.deletes)
        self.assertTrue(sdk.values)
        restarted = cache.PersistentCache(cache.PublicCacheStore(sdk))
        await restarted.hydrate()
        self.assertEqual(restarted[key][1]["items"], [{"title": "真实剧集"}])
        await restarted.close()

    async def test_persistence_failure_can_retry_pending_write(self):
        sdk = FakeSDKStore()
        storage = cache.PublicCacheStore(sdk)
        live = cache.PersistentCache(storage)
        sdk.fail_set = lambda key, scope, value: True
        live[("tencent", "tv", 1)] = (time.monotonic() + 60, {"items": [{"title": "真实片单"}]})
        await live.flush()
        self.assertTrue(live._pending)
        self.assertGreater(storage.errors, 0)
        sdk.fail_set = None
        await live.close()
        self.assertFalse(live._pending)
        restarted = cache.PersistentCache(cache.PublicCacheStore(sdk))
        await restarted.hydrate()
        self.assertIn(("tencent", "tv", 1), restarted)
        await restarted.close()


class SourcePersistentSWRTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.instances = []

    async def asyncTearDown(self):
        for instance in self.instances:
            await instance.close()

    def make(self, handler, storage):
        config = {"watch_region": "US", "tmdb_token": SecretStr("PRIVATE-TMDB-TOKEN"), "proxy_mode": "custom",
                  "proxy_url": SecretStr("http://PRIVATE-USER:PRIVATE-PASSWORD@proxy.invalid:7890")}
        source = sources.DiscoverySources(config, storage=storage, transport=httpx.MockTransport(handler))
        self.instances.append(source)
        return source

    @staticmethod
    def response(title):
        return httpx.Response(200, json={"data": {"ErrCode": 0, "Results": [{"ChannelID": "2", "Result": {
            "TotalNum": 1, "ListInfo": [{"ID": "mzc00200sample", "Query": title, "VideoType": "2",
                "HzImgURL": "https://puui.qpic.cn/sample"}]}}]}})

    async def test_restart_returns_stale_immediately_and_singleflights_background_refresh(self):
        sdk = FakeSDKStore()
        first = self.make(lambda request: self.response("旧片单"), cache.PublicCacheStore(sdk))
        await first.initialize()
        await first.feed("tencent", "tv")
        key = ("tencent", "tv", 1)
        first._cache[key] = (time.monotonic() - 1, first._cache[key][1])
        await first.close()
        self.instances.remove(first)
        entered, release = asyncio.Event(), asyncio.Event()
        calls = 0
        async def handler(request):
            nonlocal calls
            calls += 1
            entered.set()
            await release.wait()
            return self.response("刷新后的片单")
        restarted = self.make(handler, cache.PublicCacheStore(sdk))
        await restarted.initialize()
        responses = await asyncio.wait_for(asyncio.gather(*(restarted.feed("tencent", "tv") for _ in range(20))), 0.5)
        self.assertTrue(all(result["status"] == "stale" for result in responses))
        self.assertTrue(all(result["items"][0]["title"] == "旧片单" for result in responses))
        self.assertTrue(all(result["cache"]["refreshing"] for result in responses))
        self.assertEqual(restarted.cache_status()["restored_entries"], 1)
        await asyncio.wait_for(entered.wait(), 0.5)
        self.assertEqual(calls, 1)
        release.set()
        await asyncio.gather(*list(restarted._refresh_tasks.values()))
        self.assertEqual((await restarted.feed("tencent", "tv"))["items"][0]["title"], "刷新后的片单")
        payload = json.dumps({str(key): value for key, value in sdk.values.items()}, ensure_ascii=False)
        for forbidden in ("PRIVATE-", "proxy.invalid", "proxy_url", "tmdb_token"):
            self.assertNotIn(forbidden, payload)

    async def test_failed_background_refresh_preserves_and_repersists_last_real_items(self):
        sdk = FakeSDKStore()
        fail = False
        async def handler(request):
            if fail:
                raise httpx.ConnectError("PRIVATE upstream network diagnostic", request=request)
            return self.response("上次成功片单")
        source = self.make(handler, cache.PublicCacheStore(sdk))
        await source.initialize()
        await source.feed("tencent", "tv")
        key = ("tencent", "tv", 1)
        source._cache[key] = (time.monotonic() - 1, source._cache[key][1])
        fail = True
        immediate = await asyncio.wait_for(source.feed("tencent", "tv"), 0.5)
        self.assertEqual(immediate["status"], "stale")
        await asyncio.gather(*list(source._refresh_tasks.values()))
        after = await source.feed("tencent", "tv")
        self.assertEqual(after["items"][0]["title"], "上次成功片单")
        self.assertEqual(after["status"], "stale")
        self.assertNotIn("PRIVATE", json.dumps(after))
        await source.close()
        self.instances.remove(source)
        def no_network(request):
            self.fail("The persisted stale response has a short retry backoff")
        restarted = self.make(no_network, cache.PublicCacheStore(sdk))
        await restarted.initialize()
        restored = await restarted.feed("tencent", "tv")
        self.assertEqual(restored["items"][0]["title"], "上次成功片单")
        self.assertEqual(restored["status"], "stale")


if __name__ == "__main__":
    unittest.main()
