from __future__ import annotations

import io
import logging
import os
import unittest
from pathlib import Path
from types import SimpleNamespace

from cast_cn.douban import DoubanActor
from cast_cn.runtime import Runtime
from cast_cn.settings import AvatarPolicy, Settings

NFO_PATH = os.path.normpath("library/Matrix/movie.nfo")


class FakeStore:
    def __init__(self) -> None:
        self.data: dict[str, object] = {}

    async def get(self, key: str, *, scope: str = "global", default=None):
        return self.data.get(key, default)

    async def set(self, key: str, value, *, scope: str = "global", secret: bool = False) -> None:
        self.data[key] = value

    async def delete(self, key: str, *, scope: str = "global") -> bool:
        return self.data.pop(key, None) is not None


class _Handle:
    def __init__(self, files: FakeFiles, path: str, mode: str) -> None:
        self.files = files
        self.path = path
        self.mode = mode
        self.buffer = io.BytesIO(files.data.get(path, b"") if "r" in mode else b"")

    def read(self, *args):
        return self.buffer.read(*args)

    def write(self, data: bytes) -> int:
        return self.buffer.write(data)

    def close(self) -> None:
        if "w" in self.mode or "a" in self.mode:
            self.files.data[self.path] = self.buffer.getvalue()


class FakeFiles:
    def __init__(self, data: dict[str, bytes] | None = None) -> None:
        self.data = dict(data or {})

    async def exists(self, path) -> bool:
        return str(path) in self.data

    async def open(self, path, mode: str = "rb"):
        return _Handle(self, str(path), mode)

    async def rename(self, src, dst) -> None:
        self.data[str(dst)] = self.data.pop(str(src))

    async def remove(self, path) -> None:
        self.data.pop(str(path), None)

    async def listdir(self, path) -> list[str]:
        prefix = str(path).rstrip("/") + "/"
        return [Path(name).name for name in self.data if name.startswith(prefix)]


class FakeOps:
    def __init__(self, before: dict, after: dict) -> None:
        self.before = before
        self.after = after
        self.refreshed = False
        self.refresh_calls = 0
        self.search_calls = 0

    async def call(self, operation_id: str, arguments: dict):
        if operation_id == "library.list":
            return [
                {
                    "id": 1,
                    "source": "tmdb",
                    "capabilities": {"write_nfo": True},
                }
            ]
        if operation_id == "library.items.get":
            return self.after if self.refreshed else self.before
        if operation_id == "library.items.refresh-metadata":
            self.refresh_calls += 1
            self.refreshed = True
            return {"job_id": "job-1", "created": True}
        if operation_id == "jobs.wait":
            return {
                "changed": True,
                "job": {"id": "job-1", "status": "succeeded", "revision": 1},
            }
        if operation_id == "search.titles":
            self.search_calls += 1
            raise AssertionError("有 douban_id 时不应搜索")
        raise AssertionError(f"unexpected op: {operation_id}")


class FakeJobs:
    def __init__(self) -> None:
        self.created: list[dict] = []

    async def create(self, job_type: str, **kwargs):
        self.created.append({"job_type": job_type, **kwargs})
        return SimpleNamespace(created=True, id="job-1", job_type=job_type)


class FakeHealth:
    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []

    async def degraded(self, key: str, message: str, **kwargs) -> None:
        self.events.append(("degraded", key))

    async def ok(self, key: str) -> None:
        self.events.append(("ok", key))


class FakeContext:
    def __init__(self) -> None:
        self.logger = logging.getLogger("cast-cn-test")
        self.progress: list[str] = []

    async def update_progress(self, **kwargs) -> None:
        self.progress.append(str(kwargs.get("phase")))

    async def raise_if_cancelled(self) -> None:
        return None

    async def current_progress(self) -> dict:
        return {}

    async def cancel_requested(self) -> bool:
        return False


class FakeDouban:
    async def celebrities(self, douban_id: str) -> list[DoubanActor]:
        assert douban_id == "123"
        return [
            DoubanActor(
                name="基努·里维斯",
                latin_name="Keanu Reeves",
                character="饰 尼奥 Neo",
                avatar_url="https://img.douban.com/keanu.jpg",
            )
        ]


def _item(actors: list[dict]) -> dict:
    return {
        "media_item_id": 42,
        "kind": "movie",
        "source": "tmdb",
        "tmdb_id": 603,
        "douban_id": "123",
        "title": "黑客帝国",
        "original_title": "The Matrix",
        "year": 1999,
        "entry_dirs": ["library/Matrix"],
        "local_meta": {"actors": actors},
    }


def _runtime(
    files: FakeFiles, ops: FakeOps, settings: Settings
) -> tuple[Runtime, FakeStore, FakeContext]:
    store = FakeStore()
    context = FakeContext()
    runtime = Runtime(
        ctx=context,
        settings=settings,
        ops=ops,
        store=store,
        files=files,
        health=FakeHealth(),
        jobs=FakeJobs(),
        douban=FakeDouban(),
        base_url="",
    )
    return runtime, store, context


def _before() -> dict:
    return _item(
        [
            {
                "name": "Keanu Reeves",
                "role": "Neo",
                "thumb_url": None,
                "tmdb_person_id": 6384,
            }
        ]
    )


def _after() -> dict:
    return _item(
        [
            {
                "name": "基努·里维斯",
                "role": "尼奥",
                "thumb_url": "https://img.douban.com/keanu.jpg",
                "tmdb_person_id": 6384,
            }
        ]
    )


def _initial_nfo() -> dict[str, bytes]:
    return {
        NFO_PATH: (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            "<movie><title>黑客帝国</title><tmdbid>603</tmdbid></movie>\n"
        ).encode()
    }


class RuntimeTest(unittest.IsolatedAsyncioTestCase):
    async def test_process_item_writes_nfo_and_refreshes(self) -> None:
        files = FakeFiles(_initial_nfo())
        ops = FakeOps(_before(), _after())
        settings = Settings(avatar_policy=AvatarPolicy.TMDB_FIRST, keep_person_links=True)
        runtime, store, context = _runtime(files, ops, settings)

        result = await runtime.process_item(
            context, {"media_item_id": 42, "library_id": 1, "manual": True}
        )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(ops.refresh_calls, 1)
        text = files.data[NFO_PATH].decode()
        self.assertIn("<name>基努·里维斯</name>", text)
        self.assertIn("<role>尼奥</role>", text)
        self.assertIn("<thumb>https://img.douban.com/keanu.jpg</thumb>", text)
        state = await store.get("item:42")
        self.assertEqual(state["status"], "ok")
        self.assertFalse(state["pending_refresh"])

    async def test_dry_run_does_not_write_or_refresh(self) -> None:
        files = FakeFiles(_initial_nfo())
        ops = FakeOps(_before(), _item([]))
        settings = Settings(dry_run=True)
        runtime, store, context = _runtime(files, ops, settings)

        result = await runtime.process_item(
            context, {"media_item_id": 42, "library_id": 1, "manual": True}
        )

        self.assertEqual(result["status"], "dry_run")
        self.assertEqual(ops.refresh_calls, 0)
        self.assertNotIn("<name>基努·里维斯</name>", files.data[NFO_PATH].decode())
        state = await store.get("item:42")
        self.assertEqual(state["status"], "dry_run")

    async def test_second_run_is_noop(self) -> None:
        files = FakeFiles(_initial_nfo())
        ops = FakeOps(_before(), _after())
        runtime, store, context = _runtime(files, ops, Settings())

        first = await runtime.process_item(
            context, {"media_item_id": 42, "library_id": 1, "manual": True}
        )
        self.assertEqual(first["status"], "ok")
        ops.refreshed = False
        ops.before = _after()
        second = await runtime.process_item(
            context, {"media_item_id": 42, "library_id": 1, "manual": True}
        )
        self.assertEqual(second["status"], "noop")
        self.assertEqual(ops.refresh_calls, 1)

    async def test_restore_writes_backup_and_starts_refresh(self) -> None:
        original = _initial_nfo()[NFO_PATH]
        files = FakeFiles(_initial_nfo())
        ops = FakeOps(_before(), _after())
        runtime, store, context = _runtime(files, ops, Settings())

        await runtime.process_item(
            context, {"media_item_id": 42, "library_id": 1, "manual": True}
        )
        self.assertIn("<name>基努·里维斯</name>", files.data[NFO_PATH].decode())

        restored = await runtime.restore(42)
        self.assertEqual(restored["status"], "restored")
        self.assertTrue(restored["refresh_started"])
        self.assertEqual(restored["job_id"], "job-1")
        self.assertEqual(files.data[NFO_PATH], original)


class DoubanTitleTest(unittest.TestCase):
    def test_pick_douban_titles_prefers_seasons_for_tv(self) -> None:
        titles = [
            {
                "provider": "douban",
                "external_id": "26359270",
                "title": "怪奇物语 第一季",
                "media_type": None,
                "release_year": None,
            },
            {
                "provider": "douban",
                "external_id": "3871917",
                "title": "Stranger Things",
                "media_type": None,
                "release_year": None,
            },
            {
                "provider": "douban",
                "external_id": "26844438",
                "title": "怪奇物语 第二季",
                "media_type": None,
                "release_year": None,
            },
        ]
        # 剧集：季条目优先，且按季号升序
        self.assertEqual(
            Runtime._pick_douban_titles(
                titles, title="怪奇物语", query="怪奇物语", year=2016, kind="tv"
            ),
            ["26359270", "26844438"],
        )
        # 电影：精确总条目优先
        self.assertEqual(
            Runtime._pick_douban_titles(
                titles, title="怪奇物语", query="Stranger Things", year=2016, kind="movie"
            ),
            ["3871917"],
        )
        # 只有一个季条目时，用它是合理回落
        self.assertEqual(
            Runtime._pick_douban_titles(
                titles[:1], title="怪奇物语", query="怪奇物语", year=2016, kind="tv"
            ),
            ["26359270"],
        )

    def test_season_number_parses_chinese_and_arabic(self) -> None:
        self.assertEqual(Runtime._season_number("怪奇物语 第一季"), 1)
        self.assertEqual(Runtime._season_number("怪奇物语 第十二季"), 12)
        self.assertEqual(Runtime._season_number("Show Season 3"), 3)
        self.assertEqual(Runtime._season_number("Show 第3季"), 3)
        self.assertIsNone(Runtime._season_number("Stranger Things"))


if __name__ == "__main__":
    unittest.main()
