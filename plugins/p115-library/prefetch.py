"""2.8.0: pre-sign the direct links of the NEXT episodes while one plays.

A /play request costs a 115 signing round-trip behind the API interval
(~1-3 s before the player gets its redirect). When an episode starts, the
following episodes' links are signed in ONE batched request -- with the same
player User-Agent (302 links are bound to it) -- and cached until the link's
own expiry, so pressing "next episode" is served from cache instantly.

"Next" comes from the plugin's own sync records: the STRM files in the same
folder sorted naturally (S01E09 < S01E10), continuing into the next sibling
folder (Season 01 -> Season 02) at the end of a season. Folders with a single
video (movies) have no "next", so nothing is fetched for them.
"""
import asyncio
import hashlib
import os
import re
import time

from sync_engine import SIDE, scope_key

_NUM = re.compile(r'(\d+)')


def natural_key(text):
    return [int(part) if part.isdigit() else part.lower() for part in _NUM.split(text)]


class EpisodeIndex:
    """fid -> position among the videos of its folder (from the manifests)."""
    TTL = 600.

    def __init__(self):
        self.where = {}      # fid -> (folder, index)
        self.folders = {}    # folder -> [(name, fid, pickcode)] sorted
        self.subdirs = {}    # parent -> [folder] sorted
        self.built_at = 0.
        self.lock = asyncio.Lock()

    def stale(self):
        return time.monotonic() - self.built_at > self.TTL

    async def build(self, engine):
        folders = {}
        try:
            mappings = list(engine.cfg.parsed_mappings)
        except Exception:
            mappings = []
        for mapping in mappings:
            sk = scope_key(mapping)
            nodes = await engine.manifest_nodes('manifest:' + sk)
            records = {}
            for data in nodes.values():
                records.update(data.get('records', {}))
            for rel, rec in records.items():
                if rel.startswith(SIDE) or not isinstance(rec, dict):
                    continue
                fp = rec.get('fingerprint'); rid = rec.get('id'); path = rec.get('path')
                if not rid or not path or not isinstance(fp, list) or len(fp) < 3 or not fp[2]:
                    continue
                fid = hashlib.sha256((sk + ':' + str(rid)).encode()).hexdigest()[:24]
                folders.setdefault(os.path.dirname(path), []).append((os.path.basename(path), fid, str(fp[2])))
        where = {}; subdirs = {}
        for folder, items in folders.items():
            items.sort(key=lambda x: natural_key(x[0]))
            for i, (_, fid, _) in enumerate(items):
                where[fid] = (folder, i)
            subdirs.setdefault(os.path.dirname(folder), []).append(folder)
        for parent in subdirs:
            subdirs[parent].sort(key=lambda f: natural_key(os.path.basename(f)))
        self.where, self.folders, self.subdirs = where, folders, subdirs
        self.built_at = time.monotonic()

    def following(self, fid, n):
        """Up to n (name, fid, pickcode) after fid; empty for single-video folders."""
        pos = self.where.get(fid)
        if not pos or n <= 0:
            return []
        folder, i = pos
        items = self.folders.get(folder, [])
        if len(items) < 2 and not self._next_folder(folder):
            return []
        out = list(items[i + 1:i + 1 + n])
        nxt = folder
        while len(out) < n:
            nxt = self._next_folder(nxt)
            if not nxt:
                break
            out.extend(self.folders.get(nxt, [])[:n - len(out)])
        return out

    def _next_folder(self, folder):
        """Next sibling folder that holds videos (Season 01 -> Season 02)."""
        siblings = self.subdirs.get(os.path.dirname(folder), [])
        if folder not in siblings:
            return None
        k = siblings.index(folder)
        # only continue into a sibling whose name looks like the same series
        # structure (a season folder), never into an unrelated movie folder
        if not re.search(r'(?i)(season|s\d|第.+季|specials)', os.path.basename(folder)):
            return None
        for cand in siblings[k + 1:]:
            if self.folders.get(cand) and re.search(r'(?i)(season|s\d|第.+季|specials)', os.path.basename(cand)):
                return cand
        return None


class Prefetcher:
    """Debounced, best-effort background prefetch triggered by /play."""
    DEBOUNCE = 600.       # per (episode, UA, mode)
    MIN_REMAINING = 1200. # refresh links that would expire within 20 min

    def __init__(self, cfg, drive, engine, log=None):
        self.cfg, self.drive, self.engine, self.log = cfg, drive, engine, log
        self.index = EpisodeIndex()
        self.recent = {}
        self.tasks = set()

    def schedule(self, fid, ua, allow_cookie_url):
        n = int(getattr(self.cfg, 'prefetch_episodes', 0) or 0)
        if n <= 0:
            return None
        key = (fid, ua or '', bool(allow_cookie_url))
        now = time.monotonic()
        if now - self.recent.get(key, -1e9) < self.DEBOUNCE:
            return None
        self.recent[key] = now
        if len(self.recent) > 2000:
            for k in sorted(self.recent, key=self.recent.get)[:1000]:
                self.recent.pop(k, None)
        task = asyncio.ensure_future(self.run(fid, ua or '', bool(allow_cookie_url), n))
        self.tasks.add(task); task.add_done_callback(self.tasks.discard)
        return task

    async def run(self, fid, ua, allow_cookie_url, n):
        try:
            async with self.index.lock:
                if self.index.stale() or fid not in self.index.where:
                    await self.index.build(self.engine)
            nxt = self.index.following(fid, n)
            if not nxt:
                return 0
            primed = await self.drive.prime_links([pc for _, _, pc in nxt], ua=ua, allow_cookie_url=allow_cookie_url,
                                                  min_remaining=self.MIN_REMAINING, use_expiry=True)
            if primed and self.log:
                names = '、'.join(os.path.splitext(name)[0] for name, _, _ in nxt)
                await self.log('预取后续剧集直链 %d 个：%s（下一集可秒开）' % (primed, names[:200]))
            return primed
        except asyncio.CancelledError:
            raise
        except Exception:
            return 0  # strictly best effort: never affects playback
