"""Marketplace CI uses unittest without pytest; keep recovery coverage there too."""

import copy
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
from movieclaw_sdk.ops import OpsError

from trakt_sync.policy import iso_time
from trakt_sync.recovery import MetadataResolver

from . import test_trakt_sync as support


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = support.TrackingTests.asyncSetUp

    async def full_completion_queue(self, count):
        for number in range(count):
            event = {
                **self.event,
                "media": {"id": number + 1, "kind": "movie", "tmdb_id": number + 1},
            }
            await self.engine.accept("completed", event, f"watch-{number}", iso_time(self.now))
        ledger = await self.engine.ledger(0)
        ledger["queue"][0]["revalidate"] = True
        ledger["sessions"]["fixture"] = {"at": self.now, "note": ""}
        size = len(json.dumps(ledger, ensure_ascii=False).encode())
        ledger["sessions"]["fixture"]["note"] = "x" * (59999 - size)
        await self.engine.save_ledger(0, ledger)
        self.engine.resolver = AsyncMock(
            return_value={"movie": {"ids": {"tmdb": 1, "imdb": "tt123456789"}}}
        )

    async def test_capacity_eviction_before_send_never_removes_the_next_completion(self):
        await self.full_completion_queue(2)
        await self.engine.drain(0)
        self.assertEqual(self.requests, [])
        ledger = await self.engine.ledger(0)
        self.assertEqual([job["id"] for job in ledger["queue"]], ["watch-1"])
        self.assertEqual(ledger["dropped"], 1)
        await self.engine.drain(0)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(json.loads(self.requests[0].content)["movie"]["ids"], {"tmdb": 2})
        self.assertEqual((await self.engine.status(0))["pending"], 0)
        self.assertEqual((await self.engine.status(0))["dropped"], 1)

    async def test_single_oversized_revalidated_completion_is_not_sent_untracked(self):
        await self.full_completion_queue(1)
        await self.engine.drain(0)
        self.assertEqual(self.requests, [])
        status = await self.engine.status(0)
        self.assertEqual(status["pending"], 0)
        self.assertEqual(status["dropped"], 1)
        self.assertTrue(status["warning"])

    async def test_capacity_revalidation_keeps_uncertain_write_durable_across_restart(self):
        await self.full_completion_queue(2)
        # A heartbeat can be evicted instead, leaving the completion durably queued.
        ledger = await self.engine.ledger(0)
        ledger["queue"][1]["path"] = "/scrobble/start"
        await self.engine.save_ledger(0, ledger)

        def committed(request):
            if request.method == "POST":
                persisted = self.engine.store.data["member:0", "ledger"]["queue"]
                self.assertEqual([job["id"] for job in persisted], ["watch-0"])
                self.assertTrue(persisted[0]["uncertain"])
                raise RuntimeError("fixture exit after remote commit")

        self.handler = committed
        with self.assertRaises(RuntimeError):
            await self.engine.drain(0)
        self.handler = lambda r: httpx.Response(
            200, json=[{"movie": {"ids": {"tmdb": 1}}, "watched_at": iso_time(self.now)}]
        )
        restarted = type(self.engine)(
            self.engine.store,
            self.engine.client,
            self.engine.health,
            self.engine.log,
            clock=lambda: self.now,
        )
        await restarted.drain(0)
        self.assertEqual(sum(r.method == "POST" for r in self.requests), 1)
        self.assertEqual((await restarted.status(0))["pending"], 0)

    async def failed_episode(self):
        self.event.update(
            file_id=3019,
            media={"id": 267, "kind": "tv", "tmdb_id": 9},
            unit={"season": 0, "episode": 1},
        )
        self.handler = lambda _: httpx.Response(404)
        await self.engine.accept("completed", self.event, "failed", iso_time(self.now))
        await self.engine.drain(0)
        self.handler = None

    def resolver(self, deleted=False):
        async def call(operation, args):
            if operation == "library.list":
                return [{"id": 2}]
            if deleted:
                raise OpsError(404, "NOT_FOUND", "fixture")
            return {
                "kind": "tv",
                "tmdb_id": 99,
                "files": [
                    {
                        "id": 3019,
                        "season_number": 1,
                        "episode_number": 44,
                        "file_path": "/private/not-retained.mkv",
                    }
                ],
            }

        return MetadataResolver(SimpleNamespace(call=AsyncMock(side_effect=call)))

    async def test_auto_repair_by_file_id_preserves_timestamp(self):
        await self.failed_episode()
        self.engine.resolver = self.resolver()
        when = iso_time(self.now)
        self.now += 600
        await self.engine.drain(0)
        self.assertEqual(
            [r.url.path for r in self.requests],
            ["/scrobble/stop", "/sync/history/episodes", "/sync/history"],
        )
        body = json.loads(self.requests[-1].content)["shows"][0]
        self.assertEqual(body["ids"], {"tmdb": 99})
        self.assertEqual(
            body["seasons"], [{"number": 1, "episodes": [{"number": 44, "watched_at": when}]}]
        )
        self.assertNotIn("not-retained", json.dumps(list(self.engine.store.data.values())))

    async def test_deleted_entry_retires_without_trakt_write(self):
        await self.failed_episode()
        self.engine.resolver = self.resolver(deleted=True)
        self.now += 600
        await self.engine.drain(0)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual((await self.engine.status(0))["failed"], 0)

    async def test_legacy_manual_correction_skips_existing_history(self):
        await self.failed_episode()
        ledger = await self.engine.ledger(0)
        ledger["failed"][0].pop("source")
        await self.engine.save_ledger(0, ledger)
        self.handler = lambda r: httpx.Response(
            200,
            json=[
                {
                    "show": {"ids": {"tmdb": 9}},
                    "episode": {"season": 1, "number": 1},
                    "watched_at": iso_time(self.now),
                }
            ],
        )
        await self.engine.retry_failed(0, "failed", {"season": 1, "episode": 1})
        await self.engine.drain(0)
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(self.requests[-1].method, "GET")

    async def test_legacy_migration_keeps_one_watch_retires_fifteen_live_events(self):
        await self.failed_episode()
        ledger = await self.engine.ledger(0)
        for n in range(15):
            job = copy.deepcopy(ledger["failed"][0])
            job.update(id=f"live-{n}", path="/scrobble/start", body={"progress": 30})
            ledger["failed"].append(job)
        await self.engine.save_ledger(0, ledger)
        self.now += 3600
        await self.engine.initialize()
        self.assertEqual((await self.engine.status(0))["failed"], 1)

    async def test_metadata_error_keeps_task_and_dismiss_does_not_delete_history(self):
        await self.failed_episode()
        self.engine.resolver = AsyncMock(side_effect=OpsError(503, "UNAVAILABLE", "fixture"))
        self.now += 600
        await self.engine.drain(0)
        self.assertEqual((await self.engine.status(0))["failed"], 1)
        self.assertEqual(len(self.requests), 1)
        with self.assertRaises(ValueError):
            await self.engine.dismiss_failed(1, "failed")
        await self.engine.dismiss_failed(0, "failed")
        self.assertEqual(len(self.requests), 1)
        self.assertEqual((await self.engine.status(0))["failed"], 0)
