"""A plugin-owned refresh loop; it stops with the plugin process."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone


def _utc():
    return datetime.now(timezone.utc)


class FeedUpdater:
    def __init__(self, sources, *, enabled=True, interval_minutes=60, startup_delay=3):
        self.sources = sources
        self.enabled = bool(enabled)
        self.interval = max(15, min(1440, int(interval_minutes))) * 60
        self.startup_delay = max(0, startup_delay)
        self._task = None
        self._lock = asyncio.Lock()
        self.running = False
        self.last_check = None
        self.next_check = None
        self.last_results = []

    def start(self, ctx):
        if self.enabled and self._task is None:
            self.next_check = (_utc() + timedelta(seconds=self.startup_delay)).isoformat()
            self._task = ctx.task(self._run(), name="automatic-feed-refresh")

    def status(self):
        return {"enabled": self.enabled, "interval_minutes": self.interval // 60,
                "running": self.running, "last_check": self.last_check,
                "next_check": self.next_check, "results": self.last_results,
                "message": "插件启用期间自动检查榜源；源站发布新一周后更新，异常时保留上次片单。"}

    async def refresh_once(self):
        # A future manual trigger may share this method without parallel runs.
        async with self._lock:
            self.running = True
            self.next_check = None
            results = []
            try:
                catalog = await self.sources.catalog()
                platforms = [p["id"] for p in catalog.get("platforms", [])]
                if "netflix" in platforms:
                    platforms.remove("netflix")
                    platforms.insert(0, "netflix")
                # Public first pages only. Other types retain their normal on-read
                # refresh. Netflix's second page completes all four top-ten charts.
                for platform in platforms:
                    try:
                        feed = await self.sources.refresh(platform, kind="all", page=1)
                        if platform == "netflix" and feed.get("has_more"):
                            await self.sources.refresh(platform, kind="all", page=2)
                        results.append({"platform": platform, "status": feed.get("status", "unavailable"),
                                        "source_week": feed.get("source", {}).get("updated_at"),
                                        "count": len(feed.get("items", []))})
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        # Network diagnostics may contain proxy credentials. Only
                        # publish the safe state, never exception text.
                        results.append({"platform": platform, "status": "unavailable", "count": 0})
                self.last_results = results
                self.last_check = _utc().isoformat()
            finally:
                self.running = False
                self.next_check = (_utc() + timedelta(seconds=self.interval)).isoformat() if self.enabled else None
            return results

    async def _run(self):
        await asyncio.sleep(self.startup_delay)
        while True:
            try:
                await self.refresh_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                # A transient catalog/store failure must not stop future checks.
                self.next_check = (_utc() + timedelta(seconds=self.interval)).isoformat()
            await asyncio.sleep(self.interval)

    async def close(self):
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        self.running = False
        self.next_check = None
