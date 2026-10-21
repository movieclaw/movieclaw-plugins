"""续播基线、方向、冲突、活动保护、剪辑版与丢失响应不重放。"""

import asyncio
from copy import deepcopy

import pytest
from movieclaw_friends.peers import PeerError
from movieclaw_friends.resume_sync import ResumeSync
from test_app import harness as _harness
from test_auto_sync import save as save_watched
from test_auto_sync import setup as setup_watched

harness = _harness


async def setup(h, monkeypatch):
    sources, payload, _, _, _ = await setup_watched(h, monkeypatch)
    await save_watched(h, payload | {"enabled": False})
    positions = {"emby": 100000, "plex": 200000}
    durations = {"emby": 7200000, "plex": 7200000}
    editions = {"emby": "", "plex": ""}
    played = {"emby": False, "plex": False}
    active = {"emby": set(), "plex": set()}
    writes = []
    rows = {
        "emby": {
            "external_id": "10",
            "media": {"title": "电影", "kind": "movie", "tmdb_id": 550},
            "library_ids": ["1"],
        },
        "plex": {
            "external_id": "20",
            "media": {"title": "电影", "kind": "movie", "tmdb_id": 550},
            "library_ids": ["1"],
        },
    }

    def item(platform):
        return {
            **deepcopy(rows[platform]),
            "played": played[platform],
            "position_ms": positions[platform],
            "duration_ms": durations[platform],
            "edition": editions[platform],
        }

    async def read(platform, base, token, progress, **kw):
        progress(1)
        return {"account_id": "personal-id", "records": [item(platform)]}

    async def inspect(client, source, base, token, external_id, expected, **kwargs):
        return item(source["platform"]) | {
            "library_ids": {"1"},
            "userdata": {"Played": False, "PlayCount": 2},
        }

    async def sessions(source, *args):
        return active[source["platform"]]

    async def write(source, base, token, external_id, position, duration, current):
        writes.append((source["platform"], position))
        positions[source["platform"]] = position

    h.app.state.watch_archive.external.read = read
    h.app.state.watch_archive.external.active_items = sessions
    h.app.state.watch_archive.external.set_position = write
    monkeypatch.setattr("movieclaw_friends.resume_sync.inspect_item", inspect)
    response = await h.clients["alice"].put(
        "/api/watch/resume-sync",
        json={
            "enabled": True,
            "consent": True,
            "interval_minutes": 1,
            "scope_token": (await h.clients["alice"].get("/api/watch/resume-sync")).json()[
                "scope_token"
            ],
        },
    )
    assert response.status_code == 200, response.text
    return positions, durations, editions, played, active, writes


async def run(h):
    response = await h.clients["alice"].post("/api/watch/resume-sync/check")
    assert response.status_code == 202, response.text
    await asyncio.gather(*h.app.state.resume_sync.running.values())
    return (await h.clients["alice"].get("/api/watch/resume-sync")).json()


async def test_default_closed_and_requires_separate_consent(harness, monkeypatch):
    h = harness
    assert not (await h.clients["alice"].get("/api/watch/resume-sync")).json()["enabled"]
    assert (await h.clients["alice"].post("/api/watch/resume-sync/check")).status_code == 409
    await setup_watched(h, monkeypatch)
    assert (
        await h.clients["alice"].put("/api/watch/resume-sync", json={"enabled": True})
    ).status_code in {409, 422}


async def test_first_baseline_newest_change_not_max_rewind_and_no_echo(harness, monkeypatch):
    h = harness
    positions, _, _, _, _, writes = await setup(h, monkeypatch)
    await run(h)
    assert writes == []
    positions["emby"] = 300000
    result = await run(h)
    assert writes == [("plex", 300000)]
    assert result["runs"][0]["processed"] == result["runs"][0]["total"] == 1
    await run(h)
    assert len(writes) == 1
    positions["plex"] = 50000
    await run(h)
    assert writes[-1] == ("emby", 50000)
    await run(h)
    assert len(writes) == 2
    tasks = (await h.clients["alice"].get("/api/watch/tasks?kind=resume")).json()
    assert tasks["tasks"] and all(r["kind"] == "resume" for r in tasks["tasks"])
    number = tasks["tasks"][0]["id"]
    assert (await h.clients["bob"].get(f"/api/watch/resume-sync/runs/{number}")).status_code == 404


@pytest.mark.parametrize(
    "reason", ["active", "duration", "edition", "played", "near_end", "conflict"]
)
async def test_never_overwrites_uncertain_or_active_target(harness, monkeypatch, reason):
    h = harness
    positions, durations, editions, played, active, writes = await setup(h, monkeypatch)
    await run(h)
    positions["emby"] = 400000
    if reason == "active":
        active["plex"] = {"20"}
    if reason == "duration":
        durations["plex"] = 7400000
    if reason == "edition":
        editions["plex"] = "导演剪辑版"
    if reason == "played":
        played["plex"] = True
    if reason == "near_end":
        positions["emby"] = 7000000
    if reason == "conflict":
        positions["plex"] = 600000
    # 目标只改版本/状态时也算本轮变化；两端同时变更应保持不动。
    await run(h)
    assert writes == []


@pytest.mark.parametrize("accepted", [False, True])
async def test_lost_response_and_service_rebuild_no_duplicate_write(harness, monkeypatch, accepted):
    h = harness
    positions, _, _, _, _, writes = await setup(h, monkeypatch)
    await run(h)
    positions["emby"] = 400000
    original = h.app.state.watch_archive.external.set_position

    async def lost(*args):
        if accepted:
            await original(*args)
        else:
            writes.append((args[0]["platform"], args[4]))
        raise PeerError("模拟丢失响应")

    h.app.state.watch_archive.external.set_position = lost
    await run(h)
    assert len(writes) == 1
    await h.app.state.resume_sync.close()
    service = ResumeSync(h.app.state.auto_sync, lambda r: None)
    await service.run(
        h.users["alice"]["id"],
        service.store.one("SELECT revision FROM resume_settings")["revision"],
    )
    assert len(writes) == 1
    if accepted:
        assert service.store.one("SELECT state FROM resume_ledger")["state"] == "done"
        assert (
            service.store.one("SELECT state FROM resume_targets ORDER BY id LIMIT 1")["state"]
            == "done"
        )


async def test_read_only_target_direction_and_token_change_stop(harness, monkeypatch):
    h = harness
    positions, _, _, _, _, writes = await setup(h, monkeypatch)
    await run(h)
    source = h.app.state.store.one("SELECT * FROM watch_sources WHERE platform='plex'")
    h.app.state.store.execute(
        "UPDATE watch_sources SET secret=? WHERE id=?",
        (h.app.state.store.encrypt("different-secret"), source["id"]),
    )
    positions["emby"] = 400000
    result = await run(h)
    assert writes == [] and result["runs"][0]["status"] == "paused"


async def test_batch_target_unknown_duration_is_checked_live_before_transfer(harness, monkeypatch):
    h = harness
    positions, _, _, _, _, writes = await setup(h, monkeypatch)
    positions["plex"] = 0
    original = h.app.state.watch_archive.external.read

    async def batch_read(platform, *args, **kwargs):
        result = await original(platform, *args, **kwargs)
        if platform == "plex":
            result["records"][0]["duration_ms"] = None
        return result

    h.app.state.watch_archive.external.read = batch_read
    await run(h)
    positions["emby"] = 300000
    result = await run(h)
    assert result["runs"][0]["status"] == "done"
    assert writes == [("plex", 300000)]


async def test_watched_and_resume_share_same_inflight_external_inventory(harness, monkeypatch):
    h = harness
    await setup(h, monkeypatch)
    view = (await h.clients["alice"].get("/api/watch/auto-sync")).json()
    saved = await h.clients["alice"].put(
        "/api/watch/auto-sync",
        json={
            "enabled": True,
            "consent": True,
            "sources": view["sources"],
        },
    )
    assert saved.status_code == 200
    first, release = asyncio.Event(), asyncio.Event()
    original = h.app.state.watch_archive.external.read
    calls = []

    async def read(platform, *args, **kwargs):
        calls.append(platform)
        first.set()
        await release.wait()
        return await original(platform, *args, **kwargs)

    h.app.state.watch_archive.external.read = read
    await h.clients["alice"].post("/api/watch/auto-sync/check")
    await first.wait()
    await h.clients["alice"].post("/api/watch/resume-sync/check")
    for _ in range(5):
        await asyncio.sleep(0)
    release.set()
    await asyncio.gather(
        *h.app.state.auto_sync.running.values(), *h.app.state.resume_sync.running.values()
    )
    assert sorted(calls) == ["emby", "plex"]
    assert not h.app.state.auto_sync.inventory_reads.entries


async def test_large_baseline_uses_bounded_transactions_and_remains_cancellable(
    harness, monkeypatch
):
    h = harness
    _, _, _, _, _, writes = await setup(h, monkeypatch)
    original = h.app.state.watch_archive.external.read

    async def many(platform, *args, **kwargs):
        result = await original(platform, *args, **kwargs)
        first = result["records"][0]
        result["records"] = [
            {**first, "external_id": str(index), "media": {**first["media"], "tmdb_id": index + 1}}
            for index in range(1000)
        ]
        return result

    h.app.state.watch_archive.external.read = many
    operations = []
    store = h.app.state.store
    store.db.set_trace_callback(lambda sql: operations.append(sql.split(" ", 1)[0]))
    try:
        result = await run(h)
    finally:
        store.db.set_trace_callback(None)
    assert result["runs"][0]["status"] == "done"
    assert store.one("SELECT COUNT(*) AS n FROM resume_observations")["n"] == 2000
    assert operations.count("BEGIN") == operations.count("COMMIT") == 20
    assert not writes
