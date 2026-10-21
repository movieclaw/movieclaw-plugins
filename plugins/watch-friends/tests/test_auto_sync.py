"""后台同步真实隔离与故障场景：用本机假平台检验副作用，绝不写生产账号。"""

import asyncio
from copy import deepcopy

import pytest
from movieclaw_friends.auto_sync import AutoSync
from movieclaw_friends.peers import PeerError
from test_app import harness as _harness
from test_watch_alignment import item
from test_watch_archive import connect

harness = _harness


async def setup(h, monkeypatch, inventories=None):
    inventories = inventories or {
        "emby": [
            item(550, external="10"),
            item(1396, kind="tv", season=1, episode=1, played=True, external="11"),
        ],
        "plex": [
            item(550, played=True, external="20"),
            item(1396, kind="tv", season=1, episode=1, external="21"),
        ],
    }
    sources = {}
    for platform in inventories:
        source = await connect(h, [], platform)
        sources[platform] = source["source_id"]
        h.app.state.store.display_policy.catalog(
            h.users["alice"]["id"],
            source["source_id"],
            [{"id": "1", "name": "普通库"}, {"id": "adult", "name": "私人库"}],
        )
    writes, reads = [], []
    states = {(p, r["external_id"]): r["played"] for p, rows in inventories.items() for r in rows}

    async def read(platform, base, token, progress, **kwargs):
        assert kwargs["only_watched"] is False
        assert kwargs["included_libraries"] == ["1"]
        reads.append(platform)
        records = deepcopy(inventories[platform])
        for row in records:
            row["played"] = states[platform, row["external_id"]]
        progress(len(records))
        return {"account_id": "personal-id", "records": records}

    async def inspect(client, source, base, token, external_id, expected):
        row = next(r for r in inventories[source["platform"]] if r["external_id"] == external_id)
        return {
            "played": states[source["platform"], external_id],
            "library_ids": set(row["library_ids"]),
        }

    async def mark(base, token, platform, account, external_id):
        writes.append((platform, external_id))
        states[platform, external_id] = True

    h.app.state.watch_archive.external.read = read
    h.app.state.watch_archive.external.mark_played = mark
    monkeypatch.setattr("movieclaw_friends.auto_sync.inspect_item", inspect)
    payload = {
        "enabled": True,
        "consent": True,
        "sources": [
            {"source_id": sid, "direction": "both", "library_ids": ["1"]}
            for sid in sources.values()
        ],
    }
    return sources, payload, writes, reads, states


async def save(h, payload):
    response = await h.clients["alice"].put("/api/watch/auto-sync", json=payload)
    assert response.status_code == 200, response.text
    return response.json()


async def run(h):
    response = await h.clients["alice"].post("/api/watch/auto-sync/check")
    assert response.status_code == 202, response.text
    await asyncio.gather(*list(h.app.state.auto_sync.running.values()))
    return (await h.clients["alice"].get("/api/watch/auto-sync")).json()


async def test_default_off_and_explicit_consent_scope_validation(harness, monkeypatch):
    h = harness
    sources, payload, writes, reads, _ = await setup(h, monkeypatch)
    status = (await h.clients["alice"].get("/api/watch/auto-sync")).json()
    assert status["enabled"] is False and status["runs"] == []
    assert (await h.clients["alice"].post("/api/watch/auto-sync/check")).status_code == 409
    assert reads == writes == []
    for invalid in [
        payload | {"consent": False},
        payload | {"sources": payload["sources"][:1]},
        payload | {"sources": [*payload["sources"], payload["sources"][0]]},
        payload | {"sources": [{**s, "library_ids": ["unknown"]} for s in payload["sources"]]},
    ]:
        assert (
            await h.clients["alice"].put("/api/watch/auto-sync", json=invalid)
        ).status_code == 422
    assert (await h.clients["bob"].put("/api/watch/auto-sync", json=payload)).status_code == 404
    assert (
        await h.clients["alice"].put(
            "/api/watch/auto-sync",
            json=payload | {"sources": [{**payload["sources"][0], "source_id": "movieclaw"}]},
        )
    ).status_code == 404
    assert reads == writes == []


async def test_bidirectional_exact_single_episode_dedup_and_private_scope(harness, monkeypatch):
    h = harness
    sources, payload, writes, _, states = await setup(h, monkeypatch)
    owner = h.users["alice"]["id"]
    # 对外排除与本人同步白名单分开，后台不变更展示、观看足迹或主程序。
    h.app.state.store.execute("UPDATE display_libraries SET excluded=1 WHERE user_id=?", (owner,))
    before = h.app.state.store.all("SELECT * FROM watch_facts")
    await save(h, payload)
    status = await run(h)
    assert status["runs"][0]["status"] == "done"
    assert sorted(writes) == [("emby", "10"), ("plex", "21")]
    assert status["runs"][0]["processed"] == status["runs"][0]["total"] == 2
    assert all(states.values())
    assert h.app.state.store.all("SELECT * FROM watch_facts") == before
    await run(h)
    await run(h)
    assert len(writes) == 2
    # 连续无变更检查不刷屏，保存上一轮真正写入的明细。
    status = (await h.clients["alice"].get("/api/watch/auto-sync")).json()
    assert len(status["runs"]) == 2
    detail = await h.clients["alice"].get(
        f"/api/watch/auto-sync/runs/{status['runs'][1]['id']}/targets"
    )
    assert len(detail.json()["targets"]) == 2
    assert (
        "secret" not in str(status)
        and "https://" not in str(status)
        and "pin" not in status["sources"][0]
    )
    assert (
        await h.clients["bob"].get(f"/api/watch/auto-sync/runs/{status['runs'][0]['id']}/targets")
    ).status_code == 404
    assert (await h.clients["bob"].get("/api/watch/auto-sync")).json()["runs"] == []
    # 后台成功后由用户手动取消的已看不反复覆盖。
    states["emby", "10"] = False
    status = await run(h)
    assert len(writes) == 2
    assert status["runs"][0]["status"] == "attention"


async def test_unselected_unknown_libraries_ambiguous_identity_and_episode_zero_skipped(
    harness, monkeypatch
):
    h = harness
    inventories = {
        "emby": [
            item(550, external="10"),
            item(1, library="adult", external="11"),
            item(2, library="future", external="12"),
            item(3, external="13"),
            item(3, external="14"),
            item(4, kind="tv", season=1, episode=0, external="15"),
        ],
        "plex": [
            item(550, played=True, external="20"),
            item(1, played=True, external="21"),
            item(2, played=True, external="22"),
            item(3, played=True, external="23"),
            item(4, kind="tv", season=1, episode=0, played=True, external="24"),
        ],
    }
    _, payload, writes, _, _ = await setup(h, monkeypatch, inventories)
    await save(h, payload)
    result = await run(h)
    assert writes == [("emby", "10")]
    assert sum(s["ambiguous"] for s in result["runs"][0]["summary"].values()) == 1


async def test_direction_only_reads_source_only_writes_destination(harness, monkeypatch):
    h = harness
    _, payload, writes, _, _ = await setup(h, monkeypatch)
    payload["sources"][0]["direction"] = "read"
    payload["sources"][1]["direction"] = "write"
    await save(h, payload)
    await run(h)
    assert writes == [("plex", "21")]


@pytest.mark.parametrize("accepted", [False, True])
async def test_lost_response_restart_never_blindly_replays(accepted, harness, monkeypatch):
    h = harness
    _, payload, writes, _, states = await setup(h, monkeypatch)
    original = h.app.state.watch_archive.external.mark_played

    async def lost(*args):
        if accepted:
            await original(*args)
        else:
            writes.append((args[2], args[4]))
        raise PeerError("平台未确认结果")

    h.app.state.watch_archive.external.mark_played = lost
    await save(h, payload)
    result = await run(h)
    assert result["runs"][0]["status"] == "attention" and len(writes) == 2
    await h.app.state.auto_sync.close()
    h.app.state.auto_sync = AutoSync(h.app.state.store, h.app.state.watch_archive)
    # 路由闭包保持旧对象；直接用重建服务验证同一份持久意图。
    setting = h.app.state.auto_sync.settings(h.users["alice"]["id"])
    await h.app.state.auto_sync.run(h.users["alice"]["id"], setting["revision"])
    assert len(writes) == 2
    assert all(states.values()) if accepted else not all(states.values())


async def test_pause_during_read_no_write_and_does_not_resume(harness, monkeypatch):
    h = harness
    _, payload, writes, _, _ = await setup(h, monkeypatch)
    started, release = asyncio.Event(), asyncio.Event()
    original = h.app.state.watch_archive.external.read

    async def paused(*args, **kwargs):
        started.set()
        await release.wait()
        return await original(*args, **kwargs)

    h.app.state.watch_archive.external.read = paused
    await save(h, payload)
    await h.clients["alice"].post("/api/watch/auto-sync/check")
    await asyncio.wait_for(started.wait(), 2)
    response = await h.clients["alice"].post("/api/watch/auto-sync/pause")
    assert response.json()["enabled"] is False
    assert response.json()["runs"][0]["status"] == "paused" and writes == []
    assert (await h.clients["alice"].post("/api/watch/auto-sync/check")).status_code == 409


async def test_disconnected_evidence_and_stale_account_stop_before_write(harness, monkeypatch):
    h = harness
    sources, payload, writes, _, _ = await setup(h, monkeypatch)
    original = h.app.state.watch_archive.external.read

    async def disconnect(*args, **kwargs):
        result = await original(*args, **kwargs)
        h.app.state.store.execute(
            "UPDATE watch_sources SET active=0 WHERE id=?", (sources["emby"],)
        )
        return result

    h.app.state.watch_archive.external.read = disconnect
    await save(h, payload)
    assert (await run(h))["runs"][0]["status"] == "paused"
    assert writes == []


async def test_scope_changes_during_live_verification_stop_write(harness, monkeypatch):
    h = harness
    _, payload, writes, _, _ = await setup(h, monkeypatch)
    import movieclaw_friends.auto_sync as module

    original = module.inspect_item

    async def changed(*args):
        result = await original(*args)
        h.app.state.store.execute("UPDATE auto_sync_settings SET revision=revision+1")
        return result

    monkeypatch.setattr(module, "inspect_item", changed)
    await save(h, payload)
    assert (await run(h))["runs"][0]["status"] == "paused" and writes == []


async def test_disabled_identity_and_read_failure_do_not_sync_partial_inventory(
    harness, monkeypatch
):
    h = harness
    _, payload, writes, reads, _ = await setup(h, monkeypatch)
    await save(h, payload)
    del h.upstream.tokens["token-alice"]
    assert (await run(h))["runs"][0]["status"] == "failed"
    assert reads == writes == []


async def test_scheduler_resumes_persisted_opt_in_and_obeys_next_check(harness, monkeypatch):
    h = harness
    _, payload, writes, reads, _ = await setup(h, monkeypatch)
    service = h.app.state.auto_sync
    await service.close()
    await save(h, payload)
    service.start()

    async def completed():
        while len(writes) < 2 or service.running:
            await asyncio.sleep(0.01)

    await asyncio.wait_for(completed(), 3)
    assert len(reads) == 2
    await service.close()
    reloaded = AutoSync(h.app.state.store, h.app.state.watch_archive)
    reloaded.start()
    await asyncio.sleep(0.03)
    assert len(reads) == 2  # 未到下次检查，不因插件重载提前重跑。
    assert reloaded.view(h.users["alice"]["id"])["enabled"] is True
    await reloaded.close()


@pytest.mark.parametrize("change", ["unwatched", "library", "offline"])
async def test_live_source_evidence_reverified_before_any_submission(change, harness, monkeypatch):
    h = harness
    _, payload, writes, _, states = await setup(h, monkeypatch)
    import movieclaw_friends.auto_sync as module

    original = module.inspect_item

    async def changed(client, source, base, token, external_id, expected):
        if (source["platform"], external_id) in [("plex", "20"), ("emby", "11")]:
            if change == "offline":
                raise PeerError("来源暂时离线")
            if change == "library":
                return {"played": True, "library_ids": {"adult"}}
            return {"played": False, "library_ids": {"1"}}
        return await original(client, source, base, token, external_id, expected)

    monkeypatch.setattr(module, "inspect_item", changed)
    await save(h, payload)
    await run(h)
    assert writes == []


async def test_pause_during_submitted_request_persists_intent_and_never_repeats(
    harness, monkeypatch
):
    h = harness
    _, payload, writes, _, _ = await setup(h, monkeypatch)
    started = asyncio.Event()

    async def slow(*args):
        writes.append((args[2], args[4]))
        started.set()
        await asyncio.Event().wait()

    h.app.state.watch_archive.external.mark_played = slow
    await save(h, payload)
    await h.clients["alice"].post("/api/watch/auto-sync/check")
    await asyncio.wait_for(started.wait(), 2)
    paused = await h.clients["alice"].post("/api/watch/auto-sync/pause")
    assert paused.json()["enabled"] is False
    assert paused.json()["runs"][0]["counts"][0]["state"] in {"uncertain", "cancelled"}
    assert len(writes) == 1

    # 第一条提交不明时只读核验；其余未提交的条目仍可正常处理。
    async def normal(*args):
        writes.append((args[2], args[4]))

    h.app.state.watch_archive.external.mark_played = normal
    await save(h, payload)
    await run(h)
    assert len(writes) == 2 and len(set(writes)) == 2


async def test_one_offline_connection_does_not_block_other_online_servers(harness, monkeypatch):
    h = harness
    sources, payload, writes, reads, _ = await setup(h, monkeypatch)
    store = h.app.state.store
    first = store.one("SELECT * FROM watch_sources WHERE id=?", (sources["plex"],))
    store.execute(
        "INSERT INTO watch_sources(id,user_id,platform,origin,secret,account_id,account_name,"
        "created_at) VALUES(?,?,?,?,?,?,?,?)",
        (
            "offline",
            first["user_id"],
            "plex",
            store.encrypt("https://offline.test"),
            first["secret"],
            first["account_id"],
            "离线本人",
            first["created_at"],
        ),
    )
    store.display_policy.catalog(first["user_id"], "offline", [{"id": "1", "name": "电影库"}])
    payload["sources"].append({"source_id": "offline", "direction": "both", "library_ids": ["1"]})
    original = h.app.state.watch_archive.external.read

    async def offline(platform, base, *args, **kwargs):
        if base == "https://offline.test":
            raise PeerError("服务器暂时离线")
        return await original(platform, base, *args, **kwargs)

    h.app.state.watch_archive.external.read = offline
    await save(h, payload)
    result = await run(h)
    assert len(writes) == 2 and result["runs"][0]["status"] == "attention"
    assert result["runs"][0]["summary"]["offline"]["error"] == "服务器暂时离线"
    await run(h)
    result = await run(h)
    assert len(writes) == 2 and result["runs"][1]["status"] == "attention"
