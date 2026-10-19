import asyncio
import unittest

from platform_discovery.scheduler import FeedUpdater


class Sources:
    def __init__(self):
        self.calls = []

    async def catalog(self):
        return {"platforms": [{"id": "iqiyi"}, {"id": "netflix"}, {"id": "tencent"}]}

    async def refresh(self, platform, kind="all", page=1):
        self.calls.append((platform, kind, page))
        if platform == "tencent":
            raise RuntimeError("private-authenticated-proxy-url")
        return {"status": "ready", "items": [1], "has_more": platform == "netflix" and page == 1,
                "source": {"updated_at": "2026-10-11"}}


class UpdaterTests(unittest.IsolatedAsyncioTestCase):
    async def test_prioritizes_netflix_checks_all_enabled_and_preserves_safe_status(self):
        sources = Sources()
        updater = FeedUpdater(sources)
        await updater.refresh_once()
        self.assertEqual(sources.calls[:2], [("netflix", "all", 1), ("netflix", "all", 2)])
        self.assertEqual(len(updater.last_results), 3)
        self.assertEqual(updater.last_results[-1]["status"], "unavailable")
        self.assertIsNotNone(updater.status()["next_check"])
        self.assertIsNotNone(updater.status()["last_check"])
        self.assertNotIn("private-authenticated", str(updater.status()))

    async def test_enabled_loop_owned_by_context_and_cancels(self):
        class Context:
            def task(self, coroutine, *, name):
                self.name = name
                return asyncio.create_task(coroutine)
        sources, ctx = Sources(), Context()
        updater = FeedUpdater(sources, startup_delay=0)
        updater.start(ctx)
        await asyncio.sleep(.01)
        self.assertTrue(sources.calls)
        self.assertEqual(ctx.name, "automatic-feed-refresh")
        await updater.close()
        self.assertIsNone(updater.status()["next_check"])
        self.assertFalse(updater.running)

    async def test_disabled_does_not_launch_or_make_requests(self):
        sources = Sources()
        updater = FeedUpdater(sources, enabled=False)
        updater.start(None)
        await asyncio.sleep(0)
        self.assertEqual(sources.calls, [])
        self.assertFalse(updater.status()["enabled"])


if __name__ == "__main__":
    unittest.main()
