"""原生本人配对：拒绝别人的批准、密文存储、撤销与跨账号隔离。"""

import httpx
import pytest
from test_native import PREFIX, me
from test_native import native as _native

native = _native


async def api(native, approved="alice"):
    requests = []

    async def route(request):
        requests.append(request)
        path = request.url.path
        if path.endswith("/auth/device/authorize"):
            data = {
                "device_code": "private-code",
                "user_code": "ABCD",
                "interval": 5,
                "expires_in": 300,
            }
        elif path.endswith("/auth/device/token"):
            data = {"token": "secret-device-token"}
        elif path.endswith("/auth/me"):
            data = {"username": approved, "role": "member"}
        elif path.endswith("/auth/devices/current"):
            assert request.method == "DELETE"
            data = None
        else:
            raise AssertionError(path)
        return httpx.Response(200, json={"success": True, "data": data})

    native.app.state.native_sync.transport = httpx.MockTransport(route)
    return requests


@pytest.mark.parametrize("approved", ["alice", "bob"])
async def test_device_pair_binds_only_current_user_and_never_exposes_token(native, approved):
    await me(native.clients["alice"])
    wire = await api(native, approved)
    response = await native.clients["alice"].post(PREFIX + "/watch/native-sync/pair")
    assert response.status_code == 200, response.text
    assert "private-code" not in response.text and "secret-device-token" not in response.text
    assert (
        await native.clients["bob"].post(PREFIX + "/watch/native-sync/pair/check")
    ).status_code == 409
    response = await native.clients["alice"].post(PREFIX + "/watch/native-sync/pair/check")
    if approved == "bob":
        assert response.status_code == 403
        assert not native.app.state.store.one("SELECT * FROM native_sync_grants")
        assert wire[-1].method == "DELETE"
        return
    assert response.status_code == 200, response.text
    owner = (await me(native.clients["alice"]))["id"]
    grant = native.app.state.store.one("SELECT * FROM native_sync_grants WHERE user_id=?", (owner,))
    assert "secret-device-token" not in str(grant)
    sync = native.app.state.native_sync
    assert (await sync.verify(owner))["account_id"] == "alice"
    assert (await native.clients["bob"].get(PREFIX + "/watch/native-sync")).json()["bound"] is False
    assert (await native.clients["alice"].delete(PREFIX + "/watch/native-sync")).status_code == 200
    assert not native.app.state.store.one("SELECT * FROM native_sync_grants")
    assert wire[-1].method == "DELETE"


async def test_native_grant_scope_pin_stable_and_invalid_account_stops(native):
    await me(native.clients["alice"])
    await api(native)
    await native.clients["alice"].post(PREFIX + "/watch/native-sync/pair")
    await native.clients["alice"].post(PREFIX + "/watch/native-sync/pair/check")
    owner = (await me(native.clients["alice"]))["id"]
    sync = native.app.state.native_sync
    from movieclaw_friends.watch_alignment import source_pin

    assert source_pin(sync.source_for(owner)) == source_pin(sync.source_for(owner))
    native.ops.members[0]["status"] = "disabled"
    with pytest.raises(Exception, match="删除|停用"):
        await sync.verify(owner)


async def test_member_background_marks_only_own_exact_units(native, monkeypatch):
    import asyncio

    from movieclaw_friends.watch_archive import now

    user = await me(native.clients["alice"])
    await api(native)
    await native.clients["alice"].post(PREFIX + "/watch/native-sync/pair")
    await native.clients["alice"].post(PREFIX + "/watch/native-sync/pair/check")
    sync = native.app.state.native_sync
    store = native.app.state.store
    owner = user["id"]
    played = {10: False, 20: False}
    writes = []

    async def host(request):
        assert request.headers["authorization"] == "Bearer secret-device-token"
        path = request.url.path
        if path.endswith("/auth/me"):
            data = {"username": "alice", "role": "member"}
        elif path.endswith("/libraries"):
            data = [{"id": 1, "kind": "movie"}, {"id": 2, "kind": "tv"}]
        elif path.endswith("/libraries/1/items"):
            data = [{"media_item_id": 10, "title": "电影", "kind": "movie", "tmdb_id": 550}]
        elif path.endswith("/libraries/2/items"):
            data = [
                {
                    "media_item_id": 20,
                    "title": "剧集",
                    "kind": "tv",
                    "tmdb_id": 1396,
                    "seasons": [1],
                }
            ]
        elif path.endswith("/episodes"):
            data = {"season_number": 1, "episodes": [{"episode_number": 1, "owned": True}]}
        elif "/discover/titles/" in path:
            kind = "tv" if "tv" in path else "movie"
            number = 20 if kind == "tv" else 10
            data = {
                "title": {
                    "title_ref": f"tmdb:{kind}:{1396 if kind == 'tv' else 550}",
                    "media_type": kind,
                },
                "library_links": [
                    {"media_item_id": number, "library_id": 2 if kind == "tv" else 1}
                ],
            }
        elif path.endswith("/playback/resume"):
            number = int(request.url.params["media_item_id"])
            data = {"played": played[number], "position_ms": 0, "duration_ms": 7200000}
        elif path.endswith("/playback/marks"):
            import json

            body = json.loads(request.content)
            writes.append(body)
            assert body["played"] is True
            if body["media_item_id"] == 20:
                assert body["season_number"] == 1 and body["episode_number"] == 1
            else:
                assert "episode_number" not in body
            played[body["media_item_id"]] = True
            data = {"played": True}
        else:
            raise AssertionError(path)
        return httpx.Response(200, json={"success": True, "data": data})

    sync.transport = httpx.MockTransport(host)
    store.execute(
        "INSERT INTO watch_sources(id,user_id,platform,origin,secret,account_id,"
        "account_name,created_at) VALUES(?,?,?,?,?,?,?,?)",
        (
            "external",
            owner,
            "emby",
            store.encrypt("https://emby.test"),
            store.encrypt("emby-token"),
            "account",
            "本人",
            now(),
        ),
    )
    store.display_policy.catalog(owner, "external", [{"id": "1", "name": "电影"}])
    store.display_policy.catalog(
        owner, "movieclaw", [{"id": "1", "name": "电影"}, {"id": "2", "name": "剧集"}]
    )

    async def read(*args, **kwargs):
        return {
            "account_id": "account",
            "records": [
                {
                    "external_id": "a",
                    "media": {"kind": "movie", "tmdb_id": 550, "title": "电影"},
                    "played": True,
                    "library_ids": ["1"],
                },
                {
                    "external_id": "b",
                    "media": {
                        "kind": "tv",
                        "tmdb_id": 1396,
                        "title": "剧集",
                        "season_number": 1,
                        "episode_number": 1,
                    },
                    "played": True,
                    "library_ids": ["1"],
                },
            ],
        }

    async def inspect(*args, **kwargs):
        return {"played": True, "library_ids": {"1"}}

    native.app.state.watch_archive.external.read = read
    monkeypatch.setattr("movieclaw_friends.auto_sync.inspect_item", inspect)
    payload = {
        "enabled": True,
        "consent": True,
        "sources": [
            {"source_id": "external", "direction": "read", "library_ids": ["1"]},
            {"source_id": "movieclaw", "direction": "write", "library_ids": ["1", "2"]},
        ],
    }
    response = await native.clients["alice"].put(PREFIX + "/watch/auto-sync", json=payload)
    assert response.status_code == 200, response.text
    await native.clients["alice"].post(PREFIX + "/watch/auto-sync/check")
    await asyncio.gather(*native.app.state.auto_sync.running.values())
    result = (await native.clients["alice"].get(PREFIX + "/watch/auto-sync")).json()
    assert result["runs"][0]["status"] == "done", result["runs"][0]
    assert len(writes) == 2 and all(played.values())
    await native.clients["alice"].post(PREFIX + "/watch/auto-sync/check")
    await asyncio.gather(*native.app.state.auto_sync.running.values())
    assert len(writes) == 2


@pytest.mark.parametrize("legacy", [False, True])
async def test_native_inventory_batches_episode_states_and_only_enriches_resume(native, legacy):
    user = await me(native.clients["alice"])
    await api(native)
    await native.clients["alice"].post(PREFIX + "/watch/native-sync/pair")
    await native.clients["alice"].post(PREFIX + "/watch/native-sync/pair/check")
    sync = native.app.state.native_sync
    calls, progress = [], []
    states = {
        i: {"played": i < 300, "position_ms": 25000 if i == 700 else 0} for i in range(1, 1001)
    }

    async def host(request):
        calls.append(request)
        path = request.url.path
        assert request.headers["authorization"] == "Bearer secret-device-token"
        if path.endswith("/auth/me"):
            data = {"username": "alice", "role": "member"}
        elif path.endswith("/libraries"):
            data = [{"id": 2, "kind": "tv"}, {"id": 9, "kind": "tv"}]
        elif path.endswith("/libraries/2/items"):
            data = [
                {
                    "media_item_id": 20,
                    "kind": "tv",
                    "title": "测试剧集",
                    "tmdb_id": 1396,
                    "seasons": [1],
                }
            ]
        elif path.endswith("/episodes"):
            data = {
                "season_number": 1,
                "episodes": [
                    {"episode_number": i, "owned": True, **({} if legacy else state)}
                    for i, state in states.items()
                ],
            }
        elif path.endswith("/playback/resume"):
            data = {**states[int(request.url.params["episode_number"])], "duration_ms": 3600000}
        else:
            raise AssertionError(path)
        return httpx.Response(200, json={"success": True, "data": data})

    sync.transport = httpx.MockTransport(host)
    result = await sync.read(user["id"], ["2"], progress.append)
    assert len(result["records"]) == 1000
    assert progress[-1] == 1000 and len(progress) <= 12
    assert result["records"][699]["duration_ms"] == 3600000
    assert result["records"][699]["position_ms"] == 25000
    reads = [request for request in calls if request.url.path.endswith("/playback/resume")]
    assert len(reads) == (1000 if legacy else 1)
    assert len(calls) == (1004 if legacy else 5)
    assert not sync._read_clients
    assert all(request.method == "GET" for request in calls)


@pytest.mark.parametrize("filter_mode", ["supported", "ignored", "unsupported"])
async def test_movie_inventory_uses_disjoint_batch_filters_or_safe_fallback(native, filter_mode):
    user = await me(native.clients["alice"])
    await api(native)
    await native.clients["alice"].post(PREFIX + "/watch/native-sync/pair")
    await native.clients["alice"].post(PREFIX + "/watch/native-sync/pair/check")
    sync = native.app.state.native_sync
    calls = []
    films = [
        {"media_item_id": i, "kind": "movie", "tmdb_id": i, "title": f"电影{i}"}
        for i in range(1, 101)
    ]
    states = {
        i: {"played": i <= 10, "position_ms": 25000 if i == 11 else 0, "duration_ms": 3600000}
        for i in range(1, 101)
    }

    async def host(request):
        calls.append(request)
        path = request.url.path
        if path.endswith("/auth/me"):
            data = {"username": "alice", "role": "member"}
        elif path.endswith("/libraries"):
            data = [{"id": 1, "kind": "movie"}]
        elif path.endswith("/libraries/1/items"):
            mode = request.url.params.get("w")
            if mode and filter_mode == "unsupported":
                return httpx.Response(400, json={"detail": "旧服务器不支持"})
            data = films
            if mode and filter_mode == "supported":
                data = [
                    film
                    for film in films
                    if (
                        (mode == "played" and states[film["media_item_id"]]["played"])
                        or (mode == "watching" and film["media_item_id"] == 11)
                        or (mode == "unwatched" and film["media_item_id"] > 11)
                    )
                ]
        elif path.endswith("/playback/resume"):
            data = states[int(request.url.params["media_item_id"])]
        else:
            raise AssertionError(path)
        return httpx.Response(200, json={"success": True, "data": data})

    sync.transport = httpx.MockTransport(host)
    result = await sync.read(user["id"], ["1"], lambda n: None)
    assert len(result["records"]) == 100
    assert sum(row["played"] for row in result["records"]) == 10
    assert result["records"][10]["position_ms"] == 25000
    queries = [r for r in calls if r.url.path.endswith("/playback/resume")]
    assert len(queries) == (1 if filter_mode == "supported" else 100)
