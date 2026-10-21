"""真实路由驱动三实例：双向批准、独立授权、签名两跳、撤回/到期/屏蔽。"""

import copy
import json
import time

import httpx
import pytest
from movieclaw_friends.app import create_app
from movieclaw_friends.config import Settings
from movieclaw_friends.network import LEASE
from movieclaw_friends.peers import PeerError
from test_federation import HEADERS, Source


@pytest.fixture
async def mesh(tmp_path):
    apps, clients, wire = {}, {}, []

    async def route(request):
        wire.append(request)
        return await httpx.ASGITransport(app=apps[request.url.host]).handle_async_request(request)

    for name in ("alice", "bob", "carol", "dave"):
        app = create_app(
            Settings(
                "http://host",
                f"http://{name}.test",
                tmp_path / name,
                allow_private_peers=True,
                demo_enabled=False,
            ),
            upstream=Source(name),
            peer_transport=httpx.MockTransport(route),
        )
        apps[f"{name}.test"] = app
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=f"http://{name}.test", headers=HEADERS
        )
        clients[name] = client
        assert (
            await client.post("/api/login", json={"token": f"secret-{name}"})
        ).status_code == 200
        user = app.state.store.one("SELECT * FROM users")
        app.state.store.execute("UPDATE users SET role='admin' WHERE id=?", (user["id"],))
        original_identity = app.state.upstream.identity

        async def identity(token, original=original_identity, app=app):
            result = await original(token)
            user = app.state.store.one(
                "SELECT role FROM users WHERE username=?", (result["username"],)
            )
            return {**result, "role": user["role"]}

        app.state.upstream.identity = identity
    yield clients, apps, wire
    for client in clients.values():
        await client.aclose()
    for app in apps.values():
        await app.state.network.close()
        app.state.store.close()


async def enable(client):
    result = await client.put("/api/network/admin", json={"enabled": True, "consent": True})
    assert result.status_code == 200, result.text


async def link(a, b, apps, a_name="alice", b_name="bob"):
    await enable(a)
    await enable(b)
    code = (await a.post("/api/network/admin/invite")).json()["code"]
    result = await b.post(
        "/api/network/admin/connect", json={"code": code, "name": a_name, "consent": True}
    )
    assert result.status_code == 200, result.text
    incoming = next(
        row
        for row in (await a.get("/api/network/admin")).json()["peers"]
        if row["state"] == "pending"
    )
    result = await a.patch("/api/network/admin/peers/" + incoming["id"], json={"state": "accepted"})
    assert result.status_code == 200, result.text
    return incoming["id"]


async def publish(client, **options):
    options = {"publish": True, **options}
    preview = await client.post("/api/network/preview", json=options)
    assert preview.status_code == 200, preview.text
    result = await client.put(
        "/api/network/publication",
        json={**options, "consent": True, "preview_token": preview.json()["preview_token"]},
    )
    assert result.status_code == 200, result.text
    return preview.json()


async def browse(client):
    assert (await client.put("/api/network/browse", json={"enabled": True})).status_code == 200
    return (await client.get("/api/network/directory")).json()["users"]


async def test_default_closed_no_inherit_and_both_admin_consent(mesh):
    clients, apps, wire = mesh
    a, b = clients["alice"], clients["bob"]
    assert (await a.get("/api/network/settings")).json()["options"] == {
        "publish": False,
        "forward": False,
        "discovery": False,
        "favorites": False,
        "recommendations": False,
        "recent": False,
    }
    assert (await a.get("/api/network/directory")).json()["users"] == []
    await apps["alice.test"].state.network.refresh()
    assert wire == []
    assert (await a.put("/api/network/admin", json={"enabled": True})).status_code == 400
    await enable(a)
    await enable(b)
    code = (await a.post("/api/network/admin/invite")).json()["code"]
    assert (
        await b.post("/api/network/admin/connect", json={"code": code, "name": "A"})
    ).status_code == 400
    result = await b.post(
        "/api/network/admin/connect", json={"code": code, "name": "A", "consent": True}
    )
    assert result.status_code == 200
    await publish(a)
    await apps["bob.test"].state.network.refresh()
    assert await browse(b) == []
    peers = (await a.get("/api/network/admin")).json()["peers"]
    assert peers[0]["state"] == "pending"
    assert "origin" not in result.text and "token" not in result.text
    await a.patch("/api/network/admin/peers/" + peers[0]["id"], json={"state": "accepted"})
    await apps["bob.test"].state.network.refresh()
    assert (await browse(b))[0]["nickname"] == "alice"
    # 普通成员不能管理网络；旧同服广场和好友授权未因此改变。
    apps["alice.test"].state.store.execute("UPDATE users SET role='member'")
    assert (await a.get("/api/network/admin")).status_code == 403
    assert not (await a.get("/api/me")).json()["user"]["discoverable"]


async def test_two_hops_signature_forward_off_and_no_third_hop(mesh):
    clients, apps, wire = mesh
    a, b, c, d = [clients[n] for n in ("alice", "bob", "carol", "dave")]
    await link(a, b, apps)
    await link(b, c, apps, "bob", "carol")
    await link(c, d, apps, "carol", "dave")
    await publish(a)
    await apps["bob.test"].state.network.refresh()
    await apps["carol.test"].state.network.refresh()
    assert len(await browse(b)) == 1
    assert await browse(c) == []
    await publish(a, forward=True)
    await apps["bob.test"].state.network.refresh()
    await apps["carol.test"].state.network.refresh()
    result = await browse(c)
    assert result[0]["hops"] == 2
    assert result[0]["history"] == []
    assert result[0]["favorites"] == []
    for forbidden in ("http://", "origin", "endpoint", "private", "token", "public_key"):
        assert forbidden not in json.dumps(result)
    await apps["dave.test"].state.network.refresh()
    assert await browse(d) == []
    cached = apps["carol.test"].state.store.one("SELECT envelope FROM network_cache")
    envelope = json.loads(cached["envelope"])
    network = apps["dave.test"].state.network
    with pytest.raises(PeerError):
        network.validate_envelope(envelope, apps["alice.test"].state.network.node)
    tampered = copy.deepcopy(envelope)
    tampered["payload"]["expires"] = time.time() + 9999
    with pytest.raises(PeerError):
        network.validate_envelope(tampered, apps["bob.test"].state.network.node)
    tampered = copy.deepcopy(envelope)
    tampered["proofs"] = []
    with pytest.raises(PeerError):
        network.validate_envelope(tampered, apps["bob.test"].state.network.node)


async def test_withdraw_propagates_and_replay_cannot_restore(mesh):
    clients, apps, _ = mesh
    a, b, c = [clients[n] for n in ("alice", "bob", "carol")]
    await link(a, b, apps)
    await link(b, c, apps, "bob", "carol")
    await publish(a, forward=True)
    await apps["bob.test"].state.network.refresh()
    await apps["carol.test"].state.network.refresh()
    assert len(await browse(c)) == 1
    old = apps["carol.test"].state.store.one("SELECT * FROM network_cache")
    assert (await a.put("/api/network/publication", json={"publish": False})).status_code == 200
    assert await browse(b) == []
    assert await browse(c) == []
    envelope = json.loads(old["envelope"])
    assert not apps["carol.test"].state.network.receive(
        envelope, apps["bob.test"].state.network.node, old["via"]
    )
    await publish(a, forward=True)
    await apps["bob.test"].state.network.refresh()
    await apps["carol.test"].state.network.refresh()
    assert len(await browse(c)) == 1
    # 离线且通知未达时，租期仍由来源签名决定，过期即使直接查看也不能展示。
    apps["carol.test"].state.store.execute("UPDATE network_cache SET expires=?", (time.time() - 1,))
    assert await browse(c) == []


async def test_user_block_admin_pause_and_source_disable(mesh):
    clients, apps, _ = mesh
    a, b = clients["alice"], clients["bob"]
    peer_id = await link(a, b, apps)
    await publish(a)
    await apps["bob.test"].state.network.refresh()
    user = (await browse(b))[0]
    assert (await b.post("/api/network/blocks", json={"actor": user["id"]})).status_code == 200
    assert await browse(b) == []
    await b.delete("/api/network/blocks/" + user["id"])
    assert len(await browse(b)) == 1
    await a.put("/api/network/admin", json={"enabled": False})
    assert await browse(b) == []
    await enable(a)
    await apps["bob.test"].state.network.refresh()
    assert len(await browse(b)) == 1
    await a.patch("/api/network/admin/peers/" + peer_id, json={"state": "blocked"})
    await apps["bob.test"].state.network.refresh()
    assert await browse(b) == []


async def test_preview_exact_consent_revision_and_no_cross_account(mesh):
    clients, apps, _ = mesh
    a = clients["alice"]
    await enable(a)
    preview = (await a.post("/api/network/preview", json={"publish": True})).json()
    assert (
        await a.put(
            "/api/network/publication",
            json={"publish": True, "preview_token": preview["preview_token"]},
        )
    ).status_code == 409
    assert (
        await a.put(
            "/api/network/publication",
            json={
                "publish": True,
                "forward": True,
                "consent": True,
                "preview_token": preview["preview_token"],
            },
        )
    ).status_code == 409
    assert (
        await a.put(
            "/api/network/publication",
            json={"publish": True, "consent": True, "preview_token": preview["preview_token"]},
        )
    ).status_code == 200
    assert (
        await a.put(
            "/api/network/publication",
            json={"publish": True, "consent": True, "preview_token": preview["preview_token"]},
        )
    ).status_code == 409
    await enable(clients["bob"])
    assert (
        await clients["bob"].put(
            "/api/network/publication",
            json={"publish": True, "consent": True, "preview_token": preview["preview_token"]},
        )
    ).status_code == 409


async def test_movie_projection_exclusion_future_content_and_deleted_recommendations(mesh):
    clients, apps, _ = mesh
    a, b = clients["alice"], clients["bob"]
    await link(a, b, apps)
    app = apps["alice.test"]
    store = app.state.store
    owner = store.one("SELECT id FROM users")["id"]
    ordinary = {
        "kind": "movie",
        "tmdb_id": 550,
        "title": "搏击俱乐部",
        "year": 1999,
        "poster_path": "/poster.jpg",
    }
    private = {"kind": "movie", "tmdb_id": 551, "title": "私人影片", "year": 2000}
    await a.put("/api/favorites", json={"items": [ordinary, private]})
    post = await a.post(
        "/api/feed",
        json={
            "media": {**ordinary, "cover_path": "/chosen-cover.jpg", "cover_type": "backdrop"},
            "body": "a" * 2000,
            "rating": 5,
            "publish_consent": True,
        },
    )
    assert post.status_code == 201, post.text
    projected = await publish(a, favorites=True, recommendations=True, forward=True)
    assert len(projected["card"]["favorites"]) == 2
    assert projected["card"]["history"] == []
    await apps["bob.test"].state.network.refresh()
    result = await browse(b)
    assert result[0]["recommendations"][0]["rating"] == 5
    assert result[0]["recommendations"][0]["media"]["cover_path"] == "/chosen-cover.jpg"
    assert result[0]["recommendations"][0]["media"]["cover_type"] == "backdrop"
    assert "media_item_id" not in json.dumps(result)
    # 新增喜欢不会扩大发布过的具体快照。
    future = {**ordinary, "tmdb_id": 552, "title": "后来加入"}
    await a.put("/api/favorites", json={"items": [ordinary, private, future]})
    await apps["bob.test"].state.network.refresh()
    assert len((await browse(b))[0]["favorites"]) == 2
    policy = store.display_policy
    libraries = [{"id": "1", "name": "电影"}, {"id": "2", "name": "AV"}]
    policy.remember(
        owner,
        "movieclaw",
        [
            {"external_id": "1", "media": ordinary, "library_ids": ["1"]},
            {"external_id": "2", "media": private, "library_ids": ["2"]},
        ],
        libraries,
    )
    response = await a.put(
        "/api/display-policy", json={"excluded": [{"source_id": "movieclaw", "library_id": "2"}]}
    )
    assert response.status_code == 200, response.text
    await apps["bob.test"].state.network.refresh()
    assert [v["tmdb_id"] for v in (await browse(b))[0]["favorites"]] == [550]
    await a.delete("/api/feed/" + str(post.json()["id"]))
    await apps["bob.test"].state.network.refresh()
    assert (await browse(b))[0]["recommendations"] == []
    # 原始喜欢与本服发帖/隐私授权不受互联撤回影响。
    assert len((await a.get("/api/favorites")).json()["items"]) == 3
    assert not store.one("SELECT share_history FROM users")["share_history"]


async def test_preview_recent_only_when_explicit_and_no_private_fields(mesh):
    clients, apps, _ = mesh
    app = apps["alice.test"]
    # 测试来源添加媒体类型；生产 NativeSource 本来就规范化类型。
    original = app.state.upstream.snapshot
    calls = []

    async def snapshot(*args):
        calls.append(args)
        data = await original(*args)
        for row in data["history"]:
            row["media"]["kind"] = "movie"
        return data

    app.state.network.source.snapshot = snapshot
    await enable(clients["alice"])
    await publish(clients["alice"])
    assert not calls
    data = await publish(clients["alice"], recent=True)
    assert len(calls) == 1
    assert len(data["card"]["history"]) == 1
    for forbidden in ("private-device", "secret-token", "stream_url", "/private/"):
        assert forbidden not in json.dumps(data)


async def test_snapshot_changed_during_preview_requires_new_confirmation(mesh):
    clients, _, _ = mesh
    a = clients["alice"]
    await enable(a)
    preview = (
        await a.post("/api/network/preview", json={"publish": True, "favorites": True})
    ).json()
    await a.put(
        "/api/favorites", json={"items": [{"kind": "movie", "tmdb_id": 550, "title": "新喜欢"}]}
    )
    response = await a.put(
        "/api/network/publication",
        json={
            "publish": True,
            "favorites": True,
            "consent": True,
            "preview_token": preview["preview_token"],
        },
    )
    assert response.status_code == 409


async def test_valid_signature_cannot_smuggle_urls_or_extend_source_lease(mesh):
    _, apps, _ = mesh
    a, b = apps["alice.test"].state.network, apps["bob.test"].state.network
    card = {
        "nickname": "Alice",
        "bio": "",
        "avatar_data": None,
        "favorites": [],
        "recommendations": [],
        "history": [],
    }
    issued = time.time()
    payload = {
        "actor": "a",
        "revision": 1,
        "issued": issued,
        "expires": issued + LEASE,
        "max_hops": 2,
        "card": card,
    }
    envelope = a.signed(payload)
    assert b.validate_envelope(envelope, a.node)[3] == card
    extra = copy.deepcopy(payload)
    extra["card"]["favorites"] = [
        {
            "kind": "movie",
            "tmdb_id": 550,
            "title": "电影",
            "poster_path": None,
            "stream_url": "https://private.test/token",
        }
    ]
    with pytest.raises(PeerError):
        b.validate_envelope(a.signed(extra), a.node)
    expired = copy.deepcopy(payload)
    expired["issued"] = time.time() - LEASE - 1
    expired["expires"] = time.time() - 1
    with pytest.raises(PeerError):
        b.validate_envelope(a.signed(expired), a.node)
    extended = copy.deepcopy(payload)
    extended["expires"] = time.time() + LEASE + 1000
    with pytest.raises(PeerError):
        b.validate_envelope(a.signed(extended), a.node)


async def test_revoked_publisher_no_longer_renews_signed_cards(mesh):
    clients, apps, _ = mesh
    a, b = clients["alice"], clients["bob"]
    await link(a, b, apps)
    await publish(a, forward=True)
    await apps["bob.test"].state.network.refresh()
    assert len(await browse(b)) == 1
    source = apps["alice.test"]
    source.state.upstream.valid = False
    getattr(source.state.network, "publisher_checks", {}).clear()
    await apps["bob.test"].state.network.refresh()
    assert await browse(b) == []
    # 只停止输出，既有私有档案和名片授权不被迁移/删除。
    assert source.state.store.one("SELECT snapshot FROM network_preferences")["snapshot"]


async def test_publisher_cached_check_cannot_extend_authority_forever(mesh, monkeypatch):
    clients, apps, _ = mesh
    a, b = clients["alice"], clients["bob"]
    await link(a, b, apps)
    await publish(a)
    service = apps["alice.test"].state.network
    first = (await service.catalog(apps["bob.test"].state.network.node))[0]
    real = time.time()
    monkeypatch.setattr(time, "time", lambda: real + 58)
    later = (await service.catalog(apps["bob.test"].state.network.node))[0]
    assert later["payload"]["expires"] == first["payload"]["expires"]
    apps["alice.test"].state.upstream.valid = False
    monkeypatch.setattr(time, "time", lambda: real + 61)
    assert await service.catalog(apps["bob.test"].state.network.node) == []


async def test_paused_peer_during_publisher_verification_gets_no_card(mesh):
    import asyncio

    clients, apps, _ = mesh
    a, b = clients["alice"], clients["bob"]
    peer = await link(a, b, apps)
    await publish(a)
    service = apps["alice.test"].state.network
    original = service.source.identity
    entered, release = asyncio.Event(), asyncio.Event()

    async def slow(token):
        entered.set()
        await release.wait()
        return await original(token)

    service.source.identity = slow
    task = asyncio.create_task(apps["bob.test"].state.network.refresh())
    await asyncio.wait_for(entered.wait(), 2)
    await a.patch("/api/network/admin/peers/" + peer, json={"state": "paused"})
    release.set()
    await asyncio.wait_for(task, 2)
    assert await browse(b) == []


async def test_malformed_withdraw_is_validation_error_not_server_failure(mesh):
    clients, apps, _ = mesh
    a, b = clients["alice"], clients["bob"]
    await link(a, b, apps)
    store = apps["alice.test"].state.store
    peer = store.one("SELECT token FROM network_peers")
    response = await b.post(
        "/federation/directory",
        headers={"X-Watch-Friends-Token": store.decrypt(peer["token"])},
        json={"action": "withdraw", "events": [{"payload": "invalid"}]},
    )
    assert response.status_code == 422


async def test_own_published_card_without_peers_and_withdrawal(mesh):
    clients, apps, wire = mesh
    a = clients["alice"]
    await enable(a)
    movie = {"kind": "movie", "tmdb_id": 550, "title": "搏击俱乐部", "year": 1999}
    assert (await a.put("/api/favorites", json={"items": [movie]})).status_code == 200
    projected = await publish(a, favorites=True)
    cards = await browse(a)
    assert len(cards) == 1 and cards[0]["is_self"] is True and cards[0]["hops"] == 0
    assert cards[0]["favorites"] == projected["card"]["favorites"]
    assert cards[0]["history"] == cards[0]["recommendations"] == []
    assert not wire
    assert (await a.post("/api/network/blocks", json={"actor": cards[0]["id"]})).status_code == 400
    # 本人视图也受当前库范围过滤，未重新确认的新作品不能自行加入。
    store = apps["alice.test"].state.store
    owner = store.one("SELECT id FROM users")["id"]
    store.display_policy.remember(
        owner,
        "movieclaw",
        [{"external_id": "1", "media": movie, "library_ids": ["2"]}],
        [{"id": "2", "name": "私人库"}],
    )
    assert (
        await a.put(
            "/api/display-policy",
            json={"excluded": [{"source_id": "movieclaw", "library_id": "2"}]},
        )
    ).status_code == 200
    assert (await browse(a))[0]["favorites"] == []
    assert (await a.put("/api/network/publication", json={"publish": False})).status_code == 200
    assert await browse(a) == []
    await publish(a)
    assert len(await browse(a)) == 1
    assert (await a.put("/api/network/admin", json={"enabled": False})).status_code == 200
    assert await browse(a) == []


async def test_admin_saved_state_requires_consent_only_when_enabling(mesh):
    a = mesh[0]["alice"]
    assert (await a.put("/api/network/admin", json={"enabled": True})).status_code == 400
    await enable(a)
    assert (await a.get("/api/network/admin")).json()["enabled"] is True
    assert (await a.put("/api/network/admin", json={"enabled": True})).status_code == 200
    assert (await a.put("/api/network/admin", json={"enabled": False})).status_code == 200
    assert (await a.get("/api/network/admin")).json()["enabled"] is False
    assert (await a.put("/api/network/admin", json={"enabled": True})).status_code == 400
