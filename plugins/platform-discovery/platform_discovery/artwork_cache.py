"""Plugin-only persistent poster copies. Atomic files, bounded LRU and per-account pins."""
from __future__ import annotations
import asyncio
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import threading
import time
import uuid
from PIL import Image, ImageOps

MAX_DOWNLOAD = 12 * 1024 * 1024
MAX_PIXELS = 24_000_000


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


class ArtworkCache:
    def __init__(self, root, max_bytes=2 * 1024**3):
        self.root = Path(root)
        self.max_bytes = max_bytes
        self._lock = threading.RLock()
        self._gate = asyncio.Semaphore(3)
        self._tasks = {}
        self.downloads = self.hits = self.errors = 0
        self.ready = False

    def _db(self):
        self.root.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.root / 'index.sqlite3', timeout=15)
        db.execute('CREATE TABLE IF NOT EXISTS images (key TEXT PRIMARY KEY, bytes INTEGER, accessed REAL, meta TEXT)')
        db.execute('CREATE TABLE IF NOT EXISTS pins (owner TEXT, item TEXT, key TEXT, PRIMARY KEY(owner,item))')
        self.ready = True
        return db

    def _get(self, key):
        if len(key) != 64 or any(c not in '0123456789abcdef' for c in key):
            return None
        with self._lock, self._db() as db:
            row = db.execute('SELECT meta FROM images WHERE key=?', (key,)).fetchone()
            if row is None or not all((self.root / (key + suffix)).is_file() for suffix in ('.jpg', '-thumb.jpg')):
                return None
            db.execute('UPDATE images SET accessed=? WHERE key=?', (time.time(), key))
            return json.loads(row[0])

    async def get(self, key):
        return await asyncio.to_thread(self._get, key)

    def _store(self, key, data, meta):
        with Image.open(io.BytesIO(data)) as image:
            if image.width * image.height > MAX_PIXELS or image.width < 80 or image.height < 100:
                raise ValueError('图片尺寸不适合海报')
            image.load()
            image = ImageOps.exif_transpose(image).convert('RGB')
            width, height = image.size
            image.thumbnail((1600, 2400))
            full = io.BytesIO(); image.save(full, 'JPEG', quality=89, optimize=True)
            image.thumbnail((400, 600))
            small = io.BytesIO(); image.save(small, 'JPEG', quality=84, optimize=True)
        blobs = [full.getvalue(), small.getvalue()]
        needed = sum(map(len, blobs))
        result = {**meta, 'key': key, 'width': width, 'height': height, 'bytes': needed}
        with self._lock, self._db() as db:
            size = db.execute('SELECT COALESCE(SUM(bytes),0) FROM images WHERE key!=?', (key,)).fetchone()[0]
            if size + needed > self.max_bytes:
                for old, count in db.execute('SELECT key,bytes FROM images WHERE key!=? AND key NOT IN (SELECT key FROM pins) ORDER BY accessed', (key,)).fetchall():
                    for suffix in ('.jpg', '-thumb.jpg'):
                        (self.root / (old + suffix)).unlink(missing_ok=True)
                    db.execute('DELETE FROM images WHERE key=?', (old,)); size -= count
                    if size + needed <= self.max_bytes:
                        break
            if size + needed > self.max_bytes:
                raise ValueError('海报缓存已满；锁定海报会保留，请提高容量或解锁后重试')
            for suffix, blob in zip(('.jpg', '-thumb.jpg'), blobs):
                target = self.root / (key + suffix)
                temp = self.root / (key + '.' + uuid.uuid4().hex + '.tmp')
                try:
                    with temp.open('wb') as out:
                        out.write(blob); out.flush(); os.fsync(out.fileno())
                    os.replace(temp, target)
                finally:
                    temp.unlink(missing_ok=True)
            db.execute('INSERT OR REPLACE INTO images VALUES (?,?,?,?)', (key, needed, time.time(), json.dumps(result)))
        return result

    async def ensure(self, candidate, client):
        key = digest(candidate['url'])
        cached = await self.get(key)
        if cached:
            self.hits += 1
            return cached
        if key not in self._tasks:
            self._tasks[key] = asyncio.create_task(self._download(key, candidate, client))
        task = self._tasks[key]
        try:
            return await asyncio.shield(task)
        finally:
            if task.done() and self._tasks.get(key) is task:
                self._tasks.pop(key, None)

    async def _download(self, key, candidate, client):
        try:
            async with self._gate:
                cached = await self.get(key)
                if cached:
                    return cached
                data = bytearray()
                async with client.stream('GET', candidate['url'], headers={'Accept': 'image/*'}) as response:
                    response.raise_for_status()
                    if response.headers.get('content-type', '').split(';')[0] not in {'image/jpeg','image/png','image/webp'}:
                        raise ValueError('上游未返回图片')
                    async for part in response.aiter_bytes():
                        data.extend(part)
                        if len(data) > MAX_DOWNLOAD:
                            raise ValueError('海报超过下载大小限制')
                result = await asyncio.to_thread(self._store, key, bytes(data), candidate)
                self.downloads += 1
                return result
        except Exception:
            self.errors += 1
            raise

    async def pin(self, owner, item, key):
        def write():
            with self._lock, self._db() as db:
                if key is None:
                    db.execute('DELETE FROM pins WHERE owner=? AND item=?', (owner, item))
                elif db.execute('SELECT 1 FROM images WHERE key=?', (key,)).fetchone():
                    db.execute('INSERT OR REPLACE INTO pins VALUES (?,?,?)', (owner, item, key))
                else:
                    raise ValueError('请先缓存海报再锁定')
        await asyncio.to_thread(write)

    async def stats(self):
        def read():
            with self._lock, self._db() as db:
                count, size = db.execute('SELECT COUNT(*),COALESCE(SUM(bytes),0) FROM images').fetchone()
                pins = db.execute('SELECT COUNT(DISTINCT key) FROM pins').fetchone()[0]
                return {'images': count, 'bytes': size, 'capacity_bytes': self.max_bytes, 'pinned': pins,
                        'downloads_this_run': self.downloads, 'hits_this_run': self.hits, 'errors_this_run': self.errors,
                        'persistent': True}
        return await asyncio.to_thread(read)

    async def prune(self):
        def clean():
            with self._lock, self._db() as db:
                keys = [r[0] for r in db.execute('SELECT key FROM images WHERE key NOT IN (SELECT key FROM pins)').fetchall()]
                for key in keys:
                    for suffix in ('.jpg','-thumb.jpg'):
                        (self.root / (key + suffix)).unlink(missing_ok=True)
                    db.execute('DELETE FROM images WHERE key=?', (key,))
                return len(keys)
        return await asyncio.to_thread(clean)

    async def close(self):
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
