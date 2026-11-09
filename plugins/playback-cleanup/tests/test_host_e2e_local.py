"""端到端：真 MovieClaw（含 #1203：catalog kinds= 与本地条目 identity 令牌）+ 真 HostOps 授权 + 插件 Runtime。

没有宿主源码时跳过。运行方式::

    PYTHONPATH=/workspace/movieclaw/src:plugins/playback-cleanup \\
      /workspace/movieclaw/.venv/bin/python -m pytest -q plugins/playback-cleanup/tests/test_host_e2e_local.py

宿主源码目录取 ``MOVIECLAW_SRC``，默认 ``/workspace/movieclaw/src``。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_SRC = os.environ.get("MOVIECLAW_SRC", "/workspace/movieclaw/src")
if Path(_SRC).is_dir() and _SRC not in sys.path:
    sys.path.insert(0, _SRC)

pytest.importorskip("movieclaw_api", reason="没有 MovieClaw 宿主源码（设 MOVIECLAW_SRC）")
pytest.importorskip("fastapi.testclient")

# CI runs validate against the published host tag, which may not yet include
# MovieClaw#1203（catalog kinds= + 本地条目 identity 令牌）. Skip then; FakeHost unit tests cover the plugin.
def _host_has_local_null_sync() -> bool:
    try:
        import inspect
        from movieclaw_api.services import playback_sync as ps
        from movieclaw_api.api.routes import playback_sync as route
        from movieclaw_api.schemas.playback_sync import SyncChangeIn, SyncMedia
    except Exception:
        return False
    if "kinds" not in inspect.signature(ps.catalog).parameters:
        return False
    if "kinds" not in inspect.signature(route.catalog).parameters:
        return False
    if "identity" not in SyncChangeIn.model_fields or "identity" not in SyncMedia.model_fields:
        return False
    # #1203 local-null 规则：无外部编号的条目须带 identity == external_id，缺令牌即拒
    media = type("M", (), {"kind": "movie", "tmdb_id": None, "imdb_id": None, "external_id": "x"})()
    good = type("R", (), {"kind": "movie", "tmdb_id": None, "imdb_id": None, "identity": "x"})()
    bare = type("R", (), {"kind": "movie", "tmdb_id": None, "imdb_id": None, "identity": None})()
    try:
        return bool(ps.same_identity(media, good)) and not ps.same_identity(media, bare)
    except Exception:
        return False


if not _host_has_local_null_sync():
    pytest.skip(
        "host lacks catalog kinds= / local identity token (needs MovieClaw#1203); unit tests still run",
        allow_module_level=True,
    )


from conftest import FakeStore  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from playback_cleanup.runtime import SETTINGS_KEY, Runtime  # noqa: E402

PLUGIN_ID = "playback-cleanup"
OPERATIONS = (
    "members.list",
    "library.list",
    "library.items.list",
    "playback.sync.catalog",
    "playback.sync.state",
)


@pytest.fixture
def host(tmp_path, monkeypatch):
    for key, value in {
        "DATABASE_URL": f"sqlite+aiosqlite:///{tmp_path / 'sync.db'}",
        "SECRET_KEY_FILE": str(tmp_path / ".secret_key"),
        "MOVIECLAW_DATA_DIR": str(tmp_path),
        "SITE_CONFIGS_DIR": str(tmp_path / "sites"),
        "SCHEDULER_ENABLED": "false",
        "LOG_DIR": str(tmp_path / "logs"),
    }.items():
        monkeypatch.setenv(key, value)

    from movieclaw_api.core.config import get_settings
    from movieclaw_api.services.auth import reset_auth_state
    from movieclaw_api.settings.store import reset_setting_store
    from movieclaw_db.crypto import reset_secret_box

    def reset():
        reset_setting_store()
        reset_secret_box()
        reset_auth_state()
        get_settings.cache_clear()

    reset()
    from movieclaw_api.app import create_app

    app = create_app()
    with TestClient(app) as client:
        seeded = client.portal.call(_seed, tmp_path)
        yield SimpleNamespace(app=app, client=client, **seeded)
    reset()


async def _seed(tmp_path: Path) -> dict:
    from movieclaw_db.engine import get_database
    from movieclaw_db.models import FileSource, FileState, LibraryFile, MediaItem, Member, PlaybackState
    from movieclaw_db.repositories.library_repo import LibraryRepository

    async with get_database().session() as s:
        repo = LibraryRepository(s)
        other = await repo.create(name="其他", kind="video", root_paths=[str(tmp_path / "other")])
        movies = await repo.create(name="电影", kind="movie", root_paths=[str(tmp_path / "movies")])
        s.add(Member(id=2, username="two", password_hash="unused"))
        video = MediaItem(source="local", kind="video", external_id="local-e2e-video",
                          tmdb_id=None, imdb_id=None, title="家庭录像", original_title="L")
        local_movie = MediaItem(source="local", kind="movie", external_id="local-e2e-movie",
                                tmdb_id=None, imdb_id=None, title="本地电影", original_title="L")
        tmdb_movie = MediaItem(kind="movie", title="有编号的电影", original_title="M", tmdb_id=4242)
        s.add_all([video, local_movie, tmdb_movie])
        await s.flush()
        for lib, media in ((other, video), (other, local_movie), (movies, tmdb_movie)):
            s.add(
                LibraryFile(
                    library_id=lib.id,
                    media_item_id=media.id,
                    file_path=str(tmp_path / f"{media.id}.mkv"),
                    size_bytes=1,
                    source=FileSource.SCANNED,
                    state=FileState.IN_PLACE,
                    duration_seconds=600,
                )
            )
            s.add(PlaybackState(member_id=2, media_item_id=media.id, position_ms=90_000))
        await s.commit()
        return {
            "other_id": other.id,
            "movies_id": movies.id,
            "video_id": video.id,
            "local_movie_id": local_movie.id,
            "tmdb_movie_id": tmdb_movie.id,
        }


async def _positions(member_id: int) -> dict[int, int]:
    from movieclaw_db.engine import get_database
    from movieclaw_db.models import PlaybackState

    async with get_database().session() as s:
        rows = (await s.execute(select(PlaybackState).where(PlaybackState.member_id == member_id))).scalars()
        return {r.media_item_id: r.position_ms for r in rows}


async def _plugin_ops(app):
    """真 HostOps：第三方插件、用户批准了清单里的全部操作；返回的 OpsClient 直接当 ``Runtime.ops``。"""
    from movieclaw_api.services.host_ops import HostOps

    ops = HostOps(app)
    ops.configure(PLUGIN_ID, grants=OPERATIONS)
    ctx = SimpleNamespace(
        entry_id=PLUGIN_ID, third_party=True, permissions=OPERATIONS, effect=lambda *a, **kw: None
    )
    return await ops.client(ctx)


def test_catalog_kinds_through_host_ops(host):
    async def run():
        client = await _plugin_ops(host.app)
        default = await client.call("playback.sync.catalog", {"member_id": 2})
        wide = await client.call(
            "playback.sync.catalog", {"member_id": 2, "kinds": ["movie", "tv", "video"]}
        )
        return (
            {u["media"]["id"]: u["media"]["kind"] for u in default["items"]},
            {u["media"]["id"]: u["media"]["kind"] for u in wide["items"]},
        )

    default, wide = host.client.portal.call(run)
    assert host.video_id not in default
    assert {host.local_movie_id, host.tmdb_movie_id} <= set(default)
    assert wide[host.video_id] == "video"


def test_runtime_sweep_clears_local_items_without_external_ids(host):
    store = FakeStore()

    async def run():
        runtime = Runtime(ops=await _plugin_ops(host.app), store=store)
        await store.set(
            SETTINGS_KEY,
            {"library_id": host.other_id, "member_ids": [2], "dry_run": False, "delay_seconds": 5},
        )
        result = await runtime.sweep(reason="manual", dry_run=False)
        return runtime, result

    runtime, result = host.client.portal.call(run)
    assert result["status"] == "ok", result
    by_id = {i["media_item_id"]: i for i in result["items"]}
    assert by_id[host.video_id]["status"] == "cleared"
    assert by_id[host.local_movie_id]["status"] == "cleared"
    assert host.tmdb_movie_id not in by_id  # 别的库：不碰
    assert runtime._kinds_ok is True

    positions = host.client.portal.call(_positions, 2)
    assert positions[host.video_id] == 0
    assert positions[host.local_movie_id] == 0
    assert positions[host.tmdb_movie_id] == 90_000

    # 历史记下当时的令牌；撤销只回放它
    entry = next(r for r in store.data["history"] if r["media_item_id"] == host.video_id)
    assert entry["identity"] == "local-e2e-video"
    tmdb_entry = [r for r in store.data["history"] if r["media_item_id"] == host.tmdb_movie_id]
    assert tmdb_entry == []

    async def undo():
        return await runtime.undo(entry["id"])

    assert host.client.portal.call(undo)["undo_status"] == "applied"
    assert host.client.portal.call(_positions, 2)[host.video_id] == 90_000


def test_runtime_clear_single_unit_and_tmdb_item(host):
    store = FakeStore()

    async def run():
        runtime = Runtime(ops=await _plugin_ops(host.app), store=store)
        out = []
        for mid in (host.video_id, host.tmdb_movie_id):
            row = await runtime.lookup((2, mid, 0, 0))
            assert row is not None and row["position_ms"] == 90_000
            out += await runtime.clear(2, [row], source="manual")
        # 伪造编号的本地条目：宿主拒绝
        row = await runtime.lookup((2, host.local_movie_id, 0, 0))
        row["media"]["tmdb_id"] = 999
        out += await runtime.clear(2, [row], source="manual")
        return out

    results = host.client.portal.call(run)
    assert [r["status"] for r in results] == ["cleared", "cleared", "mismatch"]
    positions = host.client.portal.call(_positions, 2)
    assert positions[host.video_id] == 0
    assert positions[host.tmdb_movie_id] == 0
    assert positions[host.local_movie_id] == 90_000


async def _replace_with_b(media_id: int, external_id: str, position_ms: int | None) -> None:
    """删除条目 A（连同状态与文件），以同一数字主键建条目 B（模拟 SQLite 复用最大已删 id）。"""
    from movieclaw_db.engine import get_database
    from movieclaw_db.models import FileSource, FileState, LibraryFile, MediaItem, PlaybackState

    async with get_database().session() as s:
        old = await s.get(MediaItem, media_id)
        files = (
            await s.execute(select(LibraryFile).where(LibraryFile.media_item_id == media_id))
        ).scalars().all()
        library_id, path = files[0].library_id, files[0].file_path
        states = (
            await s.execute(select(PlaybackState).where(PlaybackState.media_item_id == media_id))
        ).scalars().all()
        for row in [*files, *states]:
            await s.delete(row)
        await s.delete(old)
        await s.flush()
        media = MediaItem(id=media_id, source="local", kind="movie", external_id=external_id,
                          tmdb_id=None, imdb_id=None, title="本地 B", original_title="B")
        s.add(media)
        await s.flush()
        s.add(LibraryFile(library_id=library_id, media_item_id=media_id, file_path=path + ".b.mkv",
                          size_bytes=1, source=FileSource.SCANNED, state=FileState.IN_PLACE,
                          duration_seconds=600))
        if position_ms is not None:
            s.add(PlaybackState(member_id=2, media_item_id=media_id, position_ms=position_ms))
        await s.commit()


def test_runtime_sends_catalog_identity_and_stale_token_after_id_reuse_is_rejected(host):
    store = FakeStore()
    mid = host.local_movie_id

    async def preview():
        runtime = Runtime(ops=await _plugin_ops(host.app), store=store)
        return runtime, await runtime.lookup((2, mid, 0, 0))

    runtime, stale = host.client.portal.call(preview)
    assert stale["media"]["identity"] == "local-e2e-movie"
    host.client.portal.call(_replace_with_b, mid, "local-e2e-b", 70_000)

    async def clear(row):
        return await runtime.clear(2, [row], source="manual")

    # 带 A 的令牌：mismatch，B 不动
    assert [r["status"] for r in host.client.portal.call(clear, stale)] == ["mismatch"]
    assert host.client.portal.call(_positions, 2)[mid] == 70_000
    assert "history" not in store.data

    # 重新读到 B 的令牌：applied
    async def fresh():
        return await runtime.lookup((2, mid, 0, 0))

    row = host.client.portal.call(fresh)
    assert row["media"]["identity"] == "local-e2e-b"
    assert [r["status"] for r in host.client.portal.call(clear, row)] == ["cleared"]
    assert host.client.portal.call(_positions, 2)[mid] == 0
    assert store.data["history"][0]["identity"] == "local-e2e-b"


def test_undo_after_id_reuse_replays_old_token_and_is_mismatch(host):
    store = FakeStore()
    mid = host.local_movie_id

    async def clear_a():
        runtime = Runtime(ops=await _plugin_ops(host.app), store=store)
        row = await runtime.lookup((2, mid, 0, 0))
        return runtime, await runtime.clear(2, [row], source="manual")

    runtime, results = host.client.portal.call(clear_a)
    assert [r["status"] for r in results] == ["cleared"]
    entry = store.data["history"][0]
    assert entry["identity"] == "local-e2e-movie"
    host.client.portal.call(_replace_with_b, mid, "local-e2e-b2", 0)

    async def undo():
        return await runtime.undo(entry["id"])

    done = host.client.portal.call(undo)
    assert done["undo_status"] == "mismatch"
    assert host.client.portal.call(_positions, 2)[mid] == 0  # B 没被写成 A 的 90_000


def test_legacy_local_history_without_token_is_blocked_on_real_host(host):
    store = FakeStore()
    mid = host.video_id

    async def run():
        runtime = Runtime(ops=await _plugin_ops(host.app), store=store)
        row = await runtime.lookup((2, mid, 0, 0))
        (res,) = await runtime.clear(2, [row], source="manual")
        assert res["status"] == "cleared"
        # 模拟 0.1.1 写的记录：没有 identity
        rows = store.data["history"]
        rows[0].pop("identity")
        await store.set("history", rows)
        try:
            await runtime.undo(rows[0]["id"])
        except Exception as exc:  # noqa: BLE001
            return str(exc), await runtime.history_rows()
        return None, await runtime.history_rows()

    error, rows = host.client.portal.call(run)
    assert error and "令牌" in error
    assert rows[0]["undoable"] is False
    assert host.client.portal.call(_positions, 2)[mid] == 0
