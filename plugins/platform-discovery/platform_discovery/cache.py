"""Public discovery cache in the plugin's scoped SDK store; no filesystem access."""
from __future__ import annotations

import asyncio
import base64
import copy
from contextlib import suppress
import hashlib
import json
import math
import time
import zlib

MAX_BYTES = 4 * 1024 * 1024
CHUNK_SIZE = 46000  # Every SDK value must stay below 64 KiB, including JSON.
MAX_ENTRIES = 192
MAX_AGE = 7 * 86400


class PublicCacheStore:
    """Compressed, chunked JSON with the manifest committed last.

    The scoped store belongs only to this plugin. Values are public catalog
    fields, never configuration, credentials, or member library projections.
    Expired values remain readable for stale-on-error/background refresh.
    """

    def __init__(self, store, namespace="public-v2"):
        self.store = store
        self.scope = "cache:" + hashlib.sha256(namespace.encode()).hexdigest()[:24]
        self._lock = asyncio.Lock()
        self._index = None
        self.errors = 0
        self.last_saved_at = None

    async def _load_index(self):
        if self._index is None:
            value = await self.store.get("index-v2", scope=self.scope, default=[])
            self._index = [v for v in value if isinstance(v, str) and len(v) < 300][-MAX_ENTRIES:] if isinstance(value, list) else []
        return self._index

    def _id(self, key):
        return hashlib.sha256(key.encode()).hexdigest()[:32]

    async def get(self, key):
        meta = await self.store.get("m-" + self._id(key), scope=self.scope)
        if not isinstance(meta, dict) or meta.get("key") != key or meta.get("version") != 2:
            return None
        try:
            saved_at = float(meta.get("saved_at", 0))
            if not math.isfinite(saved_at) or saved_at <= 0 or saved_at > time.time() + 60:
                return None
        except (TypeError, ValueError):
            return None
        if time.time() - saved_at > MAX_AGE:
            return None
        count, generation = meta.get("chunks"), meta.get("generation", "")
        if not isinstance(count, int) or not 1 <= count <= 128 or not isinstance(generation, str) or len(generation) != 24:
            return None
        parts = []
        for i in range(count):
            value = await self.store.get(f"b-{self._id(key)}-{generation}-{i}", scope=self.scope)
            if not isinstance(value, str) or len(value) > CHUNK_SIZE:
                return None
            parts.append(value)
        try:
            compressed = base64.b64decode("".join(parts), validate=True)
            decoder = zlib.decompressobj()
            raw = decoder.decompress(compressed, MAX_BYTES + 1)
            if len(raw) > MAX_BYTES or not decoder.eof or decoder.unconsumed_tail:
                return None
            if hashlib.sha256(raw).hexdigest() != meta.get("sha256"):
                return None
            return json.loads(raw)
        except (ValueError, TypeError, zlib.error):
            return None

    async def _delete(self, key):
        identity = self._id(key)
        meta = await self.store.get("m-" + identity, scope=self.scope)
        await self.store.delete("m-" + identity, scope=self.scope)
        if isinstance(meta, dict) and isinstance(meta.get("chunks"), int) and 0 < meta["chunks"] <= 128:
            for i in range(meta["chunks"]):
                await self.store.delete(f"b-{identity}-{meta.get('generation')}-{i}", scope=self.scope)

    async def set(self, key, value, ttl_seconds):
        raw = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
        if len(raw) > MAX_BYTES:
            raise ValueError("公开缓存超过单项容量")
        encoded = base64.b64encode(zlib.compress(raw, 6)).decode("ascii")
        chunks = [encoded[i:i + CHUNK_SIZE] for i in range(0, len(encoded), CHUNK_SIZE)]
        identity, digest = self._id(key), hashlib.sha256(raw).hexdigest()
        generation = digest[:24]
        async with self._lock:
            index = await self._load_index()
            old = await self.store.get("m-" + identity, scope=self.scope)
            stamp = time.time()
            try:
                for i, chunk in enumerate(chunks):
                    await self.store.set(f"b-{identity}-{generation}-{i}", chunk, scope=self.scope)
                await self.store.set("m-" + identity, {"version": 2, "key": key, "generation": generation,
                    "sha256": digest, "chunks": len(chunks), "saved_at": stamp,
                    "expires_at": stamp + max(0, float(ttl_seconds))}, scope=self.scope)
            except Exception:
                # Only clean an uncommitted generation after confirming it is
                # not live. An RPC timeout can have an unknown write outcome.
                observed, confirmed = None, False
                try:
                    observed = await self.store.get("m-" + identity, scope=self.scope)
                    confirmed = True
                except Exception:
                    pass
                if confirmed and (not isinstance(observed, dict) or observed.get("generation") != generation) and (not isinstance(old, dict) or old.get("generation") != generation):
                    for i in range(len(chunks)):
                        with suppress(Exception):
                            await self.store.delete(f"b-{identity}-{generation}-{i}", scope=self.scope)
                raise
            updated = [k for k in index if k != key] + [key]
            evicted, self._index = updated[:-MAX_ENTRIES], updated[-MAX_ENTRIES:]
            await self.store.set("index-v2", self._index, scope=self.scope)
            self.last_saved_at = stamp
            if isinstance(old, dict) and old.get("generation") != generation and isinstance(old.get("chunks"), int) and 0 < old["chunks"] <= 128:
                for i in range(old["chunks"]):
                    await self.store.delete(f"b-{identity}-{old.get('generation')}-{i}", scope=self.scope)
            for previous in evicted:
                await self._delete(previous)

    async def items(self, prefix=""):
        result = {}
        for key in list(await self._load_index()):
            if key.startswith(prefix):
                value = await self.get(key)
                if value is not None:
                    result[key] = value
        return result

    def status(self):
        return {"persistent": True, "backend": "plugin_data", "write_errors": self.errors,
                "stored_entries": len(self._index or []), "last_saved_at": self.last_saved_at}


class PersistentCache(dict):
    """Dictionary API used by source resolvers, asynchronously persisted."""

    def __init__(self, storage):
        super().__init__()
        self.storage = storage
        self._pending = {}
        self._worker = None
        self._closed = False
        self.hydrated = 0

    async def hydrate(self):
        try:
            for key, value in (await self.storage.items("dict:")).items():
                try:
                    parts = json.loads(key[5:])
                    cache_key = tuple(parts) if isinstance(parts, list) else parts
                    if not isinstance(value, dict) or "payload" not in value:
                        continue
                    wall_expiry = float(value["expires_at"])
                    if not math.isfinite(wall_expiry):
                        continue
                    expires = time.monotonic() + wall_expiry - time.time()
                    dict.__setitem__(self, cache_key, (expires, value["payload"]))
                    self.hydrated += 1
                except (ValueError, TypeError, KeyError):
                    continue
        except Exception:
            self.storage.errors += 1

    def __setitem__(self, key, value):
        dict.__setitem__(self, key, value)
        if self._closed or not isinstance(value, tuple) or len(value) != 2:
            return
        # An empty failed response must never replace persisted usable content.
        payload = value[1]
        if isinstance(payload, dict) and payload.get("status") in {"unavailable", "needs_config", "unsupported"}:
            return
        expires = time.time() + float(value[0]) - time.monotonic()
        record = {"expires_at": expires, "payload": copy.deepcopy(payload)}
        name = "dict:" + json.dumps(key, ensure_ascii=False, separators=(",", ":"))
        self._pending[name] = record
        if len(self) > MAX_ENTRIES:
            dict.pop(self, next(iter(self)))
        try:
            if self._worker is None or self._worker.done():
                self._worker = asyncio.get_running_loop().create_task(self._drain())
        except RuntimeError:
            pass

    async def _drain(self):
        while self._pending:
            name = next(iter(self._pending))
            record = self._pending.pop(name)
            try:
                await self.storage.set(name, record, max(0, record["expires_at"] - time.time()))
            except Exception:
                self.storage.errors += 1
                self._pending.setdefault(name, record)
                break

    async def flush(self):
        if self._worker is not None:
            await self._worker
        if self._pending:
            await self._drain()

    async def close(self):
        self._closed = True
        await self.flush()
