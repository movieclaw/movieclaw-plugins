"""Bounded shards: every plugin-data value stays comfortably below 64 KiB."""
import hashlib
import json
import time

LIMIT = 32 * 1024

class Ledger:
    def __init__(self, store):
        self.store = store
        self.buckets = {}
        self.ready = False

    def bucket(self, key):
        return 'dedupe-v2-' + hashlib.sha256(key.encode()).hexdigest()[:2]

    async def load(self, name):
        if name not in self.buckets:
            raw = await self.store.get(name, default={})
            now = time.time()
            self.buckets[name] = {k: v for k, v in raw.items() if isinstance(v, (int, float)) and v > now}
        return self.buckets[name]

    async def write(self, name):
        now = time.time()
        data = {k: v for k, v in self.buckets[name].items() if v > now}
        while len(json.dumps(data, ensure_ascii=False).encode()) > LIMIT:
            # Each shard is bounded independently, avoiding a single oversized value.
            del data[next(iter(data))]
        await self.store.set(name, data)
        self.buckets[name] = data

    async def migrate(self):
        if self.ready:
            return
        if not await self.store.get('dedupe-v2-migrated', default=False):
            old = await self.store.get('dedupe-v1', default={})
            touched = set()
            for key, expiry in old.items():
                if isinstance(expiry, (int, float)) and expiry > time.time():
                    name = self.bucket(key)
                    (await self.load(name))[key] = expiry
                    touched.add(name)
            for name in sorted(touched):
                await self.write(name)
            await self.store.set('dedupe-v2-migrated', True)
            # Keep the old value untouched for a recoverable upgrade.
        self.ready = True

    async def contains(self, key):
        return (await self.load(self.bucket(key))).get(key, 0) > time.time()

    async def remember(self, entries):
        touched = set()
        for key, expiry in entries.items():
            name = self.bucket(key)
            data = await self.load(name)
            data.pop(key, None)
            data[key] = expiry
            touched.add(name)
        for name in sorted(touched):
            await self.write(name)
