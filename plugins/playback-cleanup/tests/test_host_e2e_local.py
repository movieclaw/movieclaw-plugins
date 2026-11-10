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


# ====================================================================== 0.1.3：接下来继续隐藏
# 真播放走宿主 /playback/progress（落 playback_state + playback_log），首页卡片读 /playback/up-next
# （含 this_device），插件经真 HostOps 写回。宿主没有 continue_dismissed 时（如 #1203 基线 / 0.49.1）
# 只跑「只清进度并提示」那一条。


def _host_has_dismiss() -> bool:
    from movieclaw_api.schemas.playback_sync import SyncChangeIn, SyncUnit

    return "dismiss_continue" in SyncChangeIn.model_fields and "continue_dismissed" in SyncUnit.model_fields


needs_dismiss = pytest.mark.skipif(not _host_has_dismiss(), reason="宿主没有 continue_dismissed")
needs_old_host = pytest.mark.skipif(_host_has_dismiss(), reason="只在没有隐藏能力的宿主上跑")


class Recorder:
    """包一层真 OpsClient，记下插件发出的每个请求（看请求形状）。"""

    def __init__(self, inner) -> None:
        self.inner, self.calls = inner, []

    async def call(self, name, params):
        import copy

        self.calls.append((name, copy.deepcopy(params)))
        return await self.inner.call(name, params)

    def changes(self):
        return [c for n, p in self.calls if n == "playback.sync.state" for c in p["changes"]]


async def _seed_player(tmp_path: Path) -> dict:
    from movieclaw_db.engine import get_database
    from movieclaw_db.models import (
        FileSource,
        FileState,
        LibraryFile,
        MediaItem,
        Member,
    )
    from movieclaw_db.repositories.library_repo import LibraryRepository

    async with get_database().session() as s:
        lib = await LibraryRepository(s).create(
            name="混合", kind="movie", root_paths=[str(tmp_path / "mixed")]
        )
        s.add(Member(id=3, username="three", password_hash="unused"))
        movie = MediaItem(kind="movie", title="电影 P", original_title="P", tmdb_id=5151)
        show = MediaItem(kind="tv", title="剧 P", original_title="S", tmdb_id=6161)
        video = MediaItem(source="local", kind="video", external_id="local-e2e-pv",
                          tmdb_id=None, imdb_id=None, title="视频 P", original_title="V")
        s.add_all([movie, show, video])
        await s.flush()
        units = [(movie.id, 0, 0), (video.id, 0, 0)] + [(show.id, 1, e) for e in (1, 2, 3)]
        for mid, season, ep in units:
            s.add(LibraryFile(library_id=lib.id, media_item_id=mid, season_number=season,
                              episode_number=ep, file_path=str(tmp_path / f"p{mid}-{season}-{ep}.mkv"),
                              size_bytes=1, source=FileSource.SCANNED, state=FileState.IN_PLACE,
                              duration_seconds=600))
        await s.commit()
        return {"lib": lib.id, "movie": movie.id, "tv": show.id, "video": video.id}


@pytest.fixture
def player(host, tmp_path):
    from movieclaw_api.api.deps import require_login
    from movieclaw_api.services.auth import DeviceRef, Principal
    from movieclaw_db.engine import get_database
    from movieclaw_db.models import Member

    ids = host.client.portal.call(_seed_player, tmp_path)

    async def member(mid):
        async with get_database().session() as s:
            return await s.get(Member, mid)

    members = {m: host.client.portal.call(member, m) for m in (2, 3)}

    def as_user(mid, device):
        ref = DeviceRef(id=device, kind="ios", scope="full", name=f"d{device}")
        return Principal(kind="member", name=members[mid].username, member_id=mid,
                         is_admin=False, member=members[mid], device=ref)

    def http(mid, device, method, path, **kw):
        # 只在这一次请求里冒充播放器用户；插件的 HostOps 调用走真授权，不受影响
        host.app.dependency_overrides[require_login] = lambda: as_user(mid, device)
        try:
            r = getattr(host.client, method)(path, **kw)
        finally:
            host.app.dependency_overrides.pop(require_login, None)
        assert r.status_code == 200, r.text
        return r.json()["data"]

    def play(mid, item, season, episode, position, device=5):
        body = {"media_item_id": item, "season_number": season, "episode_number": episode}
        http(mid, device, "post", "/api/v1/playback/progress", json={**body, "event": "start"})
        http(mid, device, "post", "/api/v1/playback/progress",
             json={**body, "event": "stop", "position_ms": position})

    def up_next(mid, device=None):
        params = {"this_device": "true"} if device is not None else {}
        data = http(mid, device or 1, "get", "/api/v1/playback/up-next", params=params)
        return {(i["media_item_id"], i["season_number"], i["episode_number"]) for i in data["items"]}

    def titles(mid, device=None):
        return {k[0] for k in up_next(mid, device)}

    def resume(mid, item, season=0, episode=0):
        return http(mid, 1, "get", "/api/v1/playback/resume",
                    params={"media_item_id": item, "season_number": season, "episode_number": episode})

    store = FakeStore()

    async def make():
        rec = Recorder(await _plugin_ops(host.app))
        await store.set(SETTINGS_KEY, {"library_id": ids["lib"], "member_ids": [2], "dry_run": False})
        return rec, Runtime(ops=rec, store=store)

    rec, runtime = host.client.portal.call(make)
    return SimpleNamespace(ids=ids, play=play, up_next=up_next, titles=titles, resume=resume,
                           runtime=runtime, rec=rec, store=store, portal=host.client.portal)


async def _facts(member_id: int, items: list[int]):
    """除了 position / updated_at / continue_dismissed 以外的全部观看事实，加播放日志。"""
    from movieclaw_db.engine import get_database
    from movieclaw_db.models import PlaybackLog, PlaybackState

    async with get_database().session() as s:
        states = (await s.execute(select(PlaybackState).where(
            PlaybackState.member_id == member_id, PlaybackState.media_item_id.in_(items)
        ).order_by(PlaybackState.id))).scalars().all()
        logs = (await s.execute(select(PlaybackLog).where(
            PlaybackLog.member_id == member_id, PlaybackLog.media_item_id.in_(items)
        ).order_by(PlaybackLog.id))).scalars().all()
        return (
            [(r.media_item_id, r.season_number, r.episode_number, r.played, r.play_count,
              r.is_favorite, r.favorited_at, r.last_played_at, r.audio_track) for r in states],
            [(r.id, r.device_id, r.last_seen_at, r.end_position_ms, r.watched_ms, r.completed)
             for r in logs],
        )


async def _versions(member_id: int):
    from movieclaw_db.engine import get_database
    from movieclaw_db.models import PlaybackState

    async with get_database().session() as s:
        rows = (await s.execute(select(PlaybackState).where(PlaybackState.member_id == member_id))).scalars()
        return {(r.media_item_id, r.season_number, r.episode_number): r.updated_at for r in rows}


def _sweep(p, dry_run=False):
    return p.portal.call(lambda: p.runtime.sweep(reason="manual", dry_run=dry_run))


@needs_dismiss
def test_e2e_cleanup_hides_up_next_for_movie_tv_video_and_keeps_facts(player):
    p, ids = player, player.ids
    p.play(2, ids["tv"], 1, 1, None)  # E1 看完
    p.play(2, ids["tv"], 1, 2, 120_000)
    p.play(2, ids["movie"], 0, 0, 120_000)
    p.play(2, ids["video"], 0, 0, 90_000)
    p.play(3, ids["movie"], 0, 0, 60_000, device=9)  # 另一成员
    mine = {ids["movie"], ids["tv"], ids["video"]}
    assert mine <= p.titles(2) and mine <= p.titles(2, device=5)
    assert (ids["tv"], 1, 2) in p.up_next(2)
    items = sorted(mine)
    before = p.portal.call(_facts, 2, items)
    other_before = p.portal.call(_facts, 3, items)
    counts = {k: p.resume(2, *k)["play_count"] for k in [(ids["movie"],), (ids["video"],)]}

    # 只预览：一个都不写
    versions = p.portal.call(_versions, 2)
    preview = _sweep(p, dry_run=True)
    assert preview["counts"]["previewed"] == 3 and not p.rec.changes()
    assert p.portal.call(_versions, 2) == versions

    run = _sweep(p)
    assert run["counts"]["cleared"] == 3, run
    assert all(c["dismiss_continue"] is True and c["position_ms"] == 0 for c in p.rec.changes())
    # resume 位置 0；首页与本设备条都没有这三部（剧集不跳到 E3）
    assert p.resume(2, ids["movie"])["position_ms"] == 0
    assert p.resume(2, ids["video"])["position_ms"] == 0
    assert p.resume(2, ids["tv"], 1, 2)["position_ms"] == 0
    assert not (mine & p.titles(2)) and not (mine & p.titles(2, device=5))
    # played / play_count / 收藏 / last_played_at / 播放日志都不变
    assert p.portal.call(_facts, 2, items) == before
    assert {k: p.resume(2, *k)["play_count"] for k in counts} == counts
    # 成员隔离
    assert ids["movie"] in p.titles(3) and p.portal.call(_facts, 3, items) == other_before

    # 再播放：卡片回来，之后还能再清
    p.play(2, ids["movie"], 0, 0, 200_000)
    assert ids["movie"] in p.titles(2) and ids["movie"] in p.titles(2, device=5)
    p.rec.calls.clear()
    again = _sweep(p)
    assert again["counts"]["cleared"] == 1
    assert ids["movie"] not in p.titles(2)


@needs_dismiss
def test_e2e_undo_restores_position_and_dismiss_and_stale_undo_is_rejected(player):
    p, ids = player, player.ids
    p.play(2, ids["tv"], 1, 1, None)
    p.play(2, ids["tv"], 1, 2, 120_000)
    p.play(2, ids["movie"], 0, 0, 120_000)
    _sweep(p)
    history = {h["media_item_id"]: h for h in p.store.data["history"]}
    assert history[ids["tv"]]["prior_dismissed"] is False

    tv = p.portal.call(lambda: p.runtime.undo(history[ids["tv"]]["id"]))
    assert tv["undo_status"] == "applied"
    assert p.resume(2, ids["tv"], 1, 2)["position_ms"] == 120_000
    assert (ids["tv"], 1, 2) in p.up_next(2)  # 回到 E2，不是 E3
    # 撤销过、之后没再动：兜底扫描不再清
    p.rec.calls.clear()
    _sweep(p)
    assert ids["tv"] not in {c["media_item_id"] for c in p.rec.changes()}

    # 电影之后又播过：撤销冲突，什么都不写
    p.play(2, ids["movie"], 0, 0, 300_000)
    versions = p.portal.call(_versions, 2)
    movie = p.portal.call(lambda: p.runtime.undo(history[ids["movie"]]["id"]))
    assert movie["undo_status"] == "conflict"
    assert p.portal.call(_versions, 2) == versions
    assert p.resume(2, ids["movie"])["position_ms"] == 300_000


@needs_dismiss
def test_e2e_position_already_zero_is_still_dismissed(player):
    """现场问题的复现：只清进度（0.1.2 的写法）后卡片还在；0.1.3 兜底补上隐藏。"""
    p, ids = player, player.ids
    p.play(2, ids["video"], 0, 0, 90_000)

    async def old_style_clear():
        row = await p.runtime.lookup((2, ids["video"], 0, 0))
        from playback_cleanup.runtime import aware_iso, build_change

        change = build_change(row, 0, aware_iso(row["updated_at"]))  # 0.1.2：不带 dismiss_continue
        return await p.rec.inner.call("playback.sync.state", {"member_id": 2, "changes": [change]})

    assert p.portal.call(old_style_clear)["outcomes"][0]["status"] == "applied"
    assert p.resume(2, ids["video"])["position_ms"] == 0
    assert ids["video"] in p.titles(2)  # 只清进度不够

    run = _sweep(p)
    assert run["counts"]["cleared"] == 1
    (change,) = p.rec.changes()
    assert change["dismiss_continue"] is True and change["identity"] == "local-e2e-pv"
    assert ids["video"] not in p.titles(2) and ids["video"] not in p.titles(2, device=5)
    # 撤销恢复未隐藏
    entry = p.store.data["history"][0]
    assert entry["prior_position_ms"] == 0
    assert p.portal.call(lambda: p.runtime.undo(entry["id"]))["undo_status"] == "applied"
    assert ids["video"] in p.titles(2)


@needs_dismiss
def test_e2e_event_path_and_request_shapes(player):
    p, ids = player, player.ids
    p.play(2, ids["movie"], 0, 0, 100_000)

    async def fire():
        return await p.runtime.fire((2, ids["movie"], 0, 0))

    assert p.portal.call(fire)["status"] == "cleared"
    assert ids["movie"] not in p.titles(2)
    for name, params in p.rec.calls:
        if name == "playback.sync.catalog":
            assert set(params) <= {"member_id", "limit", "cursor", "kinds"}
    (change,) = p.rec.changes()
    assert set(change) == {"media_item_id", "kind", "expected_updated_at", "position_ms",
                           "tmdb_id", "dismiss_continue"}


@needs_old_host
def test_e2e_host_without_dismiss_only_clears_position_and_warns(player):
    """#1203 基线（= 0.49.1 的契约）：不发 dismiss_continue、不 422，照实提示卡片可能还在。"""
    from playback_cleanup.runtime import NO_DISMISS_NOTE

    p, ids = player, player.ids
    p.play(2, ids["movie"], 0, 0, 120_000)
    p.play(2, ids["video"], 0, 0, 90_000)
    run = _sweep(p)
    assert run["counts"]["cleared"] == 2 and NO_DISMISS_NOTE in run["notes"]
    assert all(i["warning"] == NO_DISMISS_NOTE for i in run["items"])
    assert all("dismiss_continue" not in c for c in p.rec.changes())
    assert p.resume(2, ids["movie"])["position_ms"] == 0
    # 旧宿主上卡片仍在——这正是现场问题，插件单装解决不了
    assert {ids["movie"], ids["video"]} <= p.titles(2)
    assert all("dismissed" not in h for h in p.store.data["history"])


# ====================================================================== #1246：只对作品锚点隐藏
# 宿主只接受落在作品当前锚点（last_played_at 最大）的 dismiss_continue=true，否则整条 conflict 回滚。
# 多集剧 E1、E2、E3 都播过、E3 最近：旧集只清进度，E3 一并隐藏，不再每次扫描都冲突。


def _host_has_anchor_rule() -> bool:
    try:
        from movieclaw_api.services import playback_up_next as up

        return hasattr(up, "continue_anchor")
    except ImportError:
        return False


needs_anchor = pytest.mark.skipif(
    not (_host_has_dismiss() and _host_has_anchor_rule()), reason="宿主没有 #1246 的锚点规则"
)


def _tv_units(p):
    return {k for k in p.up_next(2) if k[0] == p.ids["tv"]}


@needs_anchor
def test_e2e_multi_episode_only_anchor_is_dismissed_and_old_episodes_never_conflict(player):
    p, tv = player, player.ids["tv"]
    p.play(2, tv, 1, 1, 100_000)
    p.play(2, tv, 1, 2, 200_000)
    p.play(2, tv, 1, 3, 150_000)  # 当前集 = 最近播放
    assert _tv_units(p) == {(tv, 1, 3)}
    assert (tv, 1, 3) in p.up_next(2, device=5)
    before = p.portal.call(_facts, 2, [tv])

    run = _sweep(p)
    print("\nNEW first sweep counts:", run["counts"])
    print("NEW first sweep items:", [(i["episode"], i["status"], i.get("dismiss")) for i in run["items"]])
    assert run["counts"]["conflicts"] == 0, run
    assert run["counts"]["cleared"] == 3, run
    sent = {c["episode"]: c for c in p.rec.changes()}
    assert sent[3]["dismiss_continue"] is True
    assert "dismiss_continue" not in sent[1] and "dismiss_continue" not in sent[2]
    for ep in (1, 2, 3):
        assert p.resume(2, tv, 1, ep)["position_ms"] == 0
    assert tv not in p.titles(2) and tv not in p.titles(2, device=5)
    assert p.portal.call(_facts, 2, [tv]) == before  # played / play_count / last_played_at / 日志不变

    # 再扫：作品已隐藏、旧集进度 0，不再写、不再冲突
    p.rec.calls.clear()
    again = _sweep(p)
    print("NEW second sweep counts:", again["counts"])
    assert again["counts"]["candidates"] == 0 and again["counts"]["conflicts"] == 0
    assert not p.rec.changes()

    # 真实重新播放 E2（经宿主 /playback/progress）：卡片回来，锚点换成 E2
    p.play(2, tv, 1, 2, 250_000)
    assert _tv_units(p) == {(tv, 1, 2)} and tv in p.titles(2, device=5)
    replayed = p.portal.call(_facts, 2, [tv])
    p.rec.calls.clear()
    third = _sweep(p)
    print("NEW after replay counts:", third["counts"])
    assert third["counts"]["conflicts"] == 0 and third["counts"]["cleared"] == 1
    (change,) = p.rec.changes()
    assert (change["episode"], change["dismiss_continue"]) == (2, True)
    assert tv not in p.titles(2) and tv not in p.titles(2, device=5)
    assert p.portal.call(_facts, 2, [tv]) == replayed


@needs_anchor
def test_e2e_anchor_moved_between_read_and_write(player):
    """预览之后、写回之前，别的设备播了 E1：E3 的隐藏被宿主拒（锚点变了），插件重读重算，改为隐藏 E1。"""
    p, tv = player, player.ids["tv"]
    p.play(2, tv, 1, 2, 200_000)
    p.play(2, tv, 1, 3, 150_000)

    async def stale():
        return await p.runtime.title_rows(2, tv)

    rows = p.portal.call(stale)
    e3 = next(r for r in rows if r["episode"] == 3)
    p.play(2, tv, 1, 1, 60_000, device=9)  # 另一台设备

    async def clear():
        return await p.runtime.clear(2, [e3], source="manual", titles={tv: rows})

    results = p.portal.call(clear)
    print("\nNEW moved-anchor results:", [(r["episode"], r["status"], r.get("dismiss")) for r in results])
    by_ep = {r["episode"]: r for r in results}
    assert by_ep[1]["status"] == "cleared" and by_ep[1]["dismiss"] is True
    assert by_ep[3]["status"] == "cleared" and by_ep[3]["dismiss"] is False
    calls = [prm for n, prm in p.rec.calls if n == "playback.sync.state"]
    assert len(calls) == 2  # 第一次被拒 + 重算后一次
    assert p.resume(2, tv, 1, 3)["position_ms"] == 0
    assert p.resume(2, tv, 1, 1)["position_ms"] == 0
    assert tv not in p.titles(2) and tv not in p.titles(2, device=5) and tv not in p.titles(2, device=9)
