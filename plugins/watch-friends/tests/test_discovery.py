"""无中央依赖的可选公开发现：真实多实例路由、独立同意、本服投影和好友门槛。"""

import copy
import json
import time

import httpx
import pytest
from fastapi import HTTPException
from movieclaw_friends.network import encode
from movieclaw_friends.network_interactions import actor_id
from test_federation import HEADERS
from test_network import browse, enable, publish
from test_network import mesh as _mesh

mesh = _mesh


async def configure(client, *, serve=False, enabled=True):
    await enable(client)
    result = await client.put(
        "/api/discovery/admin",
        json={
            "enabled": enabled,
            "serve": serve,
            "consent": True,
        },
    )
    assert result.status_code == 200, result.text
    return result.json()


async def connect(client, server):
    code = (await server.post("/api/discovery/admin/code")).json()["code"]
    response = await client.post(
        "/api/discovery/admin/seeds",
        json={
            "name": "可选公共入口",
            "code": code,
            "consent": True,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["seeds"][0]


async def public_mesh(mesh):
    clients, apps, wire = mesh
    a, b, c = (clients[n] for n in ("alice", "bob", "carol"))
    await configure(b, serve=True)
    await configure(a)
    await configure(c)
    await connect(a, b)
    await connect(c, b)
    result = await a.post(
        "/api/feed",
        json={
            "media": {"title": "搏击俱乐部", "tmdb_id": 550, "kind": "movie"},
            "rating": 4,
            "body": "值得重看",
            "publish_consent": True,
        },
    )
    assert result.status_code == 201, result.text
    await publish(a, discovery=True, recommendations=True)
    await apps["alice.test"].state.network.refresh()
    await apps["carol.test"].state.network.refresh()
    cards = await browse(c)
    assert len(cards) == 1
    return clients, apps, wire, cards[0]


async def test_defaults_and_existing_publication_not_promoted(mesh):
    clients, apps, wire = mesh
    a = clients["alice"]
    view = (await a.get("/api/discovery/admin")).json()
    assert not view["enabled"] and not view["serve"] and not view["seeds"]
    await enable(a)
    await publish(a, recommendations=True)
    assert (await a.get("/api/network/settings")).json()["options"]["discovery"] is False
    await apps["alice.test"].state.network.refresh()
    assert wire == []
    assert (await a.post("/api/discovery/admin/code")).status_code == 409
    assert (
        await a.post("/api/network/preview", json={"publish": True, "discovery": True})
    ).status_code == 409
    await configure(a, serve=True)
    assert await apps["alice.test"].state.network.discovery.local_packets() == []
    assert apps["alice.test"].state.store.all("SELECT * FROM network_peers") == []


async def test_public_opt_in_directory_without_trusted_links_and_no_private_data(mesh):
    clients, apps, wire, card = await public_mesh(mesh)
    assert card["discovered"] and card["nickname"] == "alice"
    assert card["recommendations"][0]["rating"] == 4
    assert card["recommendations"][0]["body"] == "值得重看"
    assert card["history"] == [] and card["favorites"] == []
    text = json.dumps(card)
    assert all(
        value not in text for value in ("http", "endpoint", "secret", "invite", "descriptor")
    )
    assert (await clients["carol"].get("/api/network/settings")).json()["peer_count"] == 0
    for app in apps.values():
        assert not app.state.store.all("SELECT * FROM network_peers")
        assert not app.state.store.all("SELECT * FROM relationships")
    # 中间节点只获得明确公开名片，不能获知好友申请凭据。
    assert all("mcf1." not in req.content.decode() for req in wire if req.url.host == "bob.test")
    options = (await clients["carol"].get("/api/network/settings")).json()["options"]
    assert not options["publish"] and not options["discovery"]


async def test_discovered_friend_request_requires_consent_and_mutual_accept(mesh):
    clients, apps, wire, card = await public_mesh(mesh)
    c, a = clients["carol"], clients["alice"]
    path = f"/api/network/users/{card['id']}/interact"
    assert (await c.post(path, json={"action": "invite"})).status_code == 422
    assert (await c.post(path, json={"action": "like", "consent": True})).status_code == 403
    response = await c.post(path, json={"action": "invite", "consent": True})
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "pending"
    assert (await c.get("/api/friends/" + response.json()["id"])).status_code == 403
    assert len((await a.get("/api/requests")).json()["incoming"]) == 1
    assert not apps["bob.test"].state.store.all("SELECT * FROM relationships")
    assert all("mcf1." not in req.content.decode() for req in wire if req.url.host == "bob.test")


async def test_withdraw_expiry_replay_and_new_publication(mesh):
    clients, apps, wire, card = await public_mesh(mesh)
    a, c = clients["alice"], clients["carol"]
    origin = apps["alice.test"].state.network.discovery
    seed = apps["bob.test"].state.network.discovery
    consumer = apps["carol.test"].state.network.discovery
    old = (await origin.local_packets())[0]
    await publish(a, recommendations=True, discovery=False)
    seed.receive(old, "replay")
    assert seed.cached() == []
    # 离线接收端不能延长来源租期；缓存超过有效期后立即隐藏。
    row = consumer.store.one("SELECT * FROM discovery_cards")
    consumer.store.execute("UPDATE discovery_cards SET expires=?", (time.time() - 1,))
    assert await browse(c) == []
    consumer.store.execute("UPDATE discovery_cards SET expires=?", (row["expires"],))
    await publish(a, recommendations=True, discovery=True)
    await origin.refresh()
    await consumer.refresh()
    assert len(await browse(c)) == 1
    assert (await a.put("/api/network/publication", json={"publish": False})).status_code == 200
    assert seed.cached() == []
    assert (
        await c.post(
            f"/api/network/users/{card['id']}/interact", json={"action": "invite", "consent": True}
        )
    ).status_code == 502


@pytest.mark.parametrize(
    "mutation", ["signature", "expired", "purpose", "private", "actor", "oversize"]
)
async def test_reject_invalid_or_promoted_packets(mesh, mutation):
    clients, apps, _ = mesh
    await configure(clients["alice"], serve=True)
    await configure(clients["bob"], serve=True)
    await publish(clients["alice"], discovery=True)
    a, b = apps["alice.test"].state.network.discovery, apps["bob.test"].state.network.discovery
    packet = copy.deepcopy((await a.local_packets())[0])
    if mutation == "signature":
        packet["signature"] = encode(b"x" * 64)
    else:
        payload = packet["payload"]
        if mutation == "expired":
            payload["expires"] = time.time() - 1
        elif mutation == "purpose":
            payload["purpose"] = "trusted-network-without-public-grant"
        elif mutation == "private":
            payload["card"]["token"] = "secret"
        elif mutation == "actor":
            payload["actor"] = "x" * 100
        else:
            payload["card"]["bio"] = "x" * 240000
        packet = a.sign(payload)
    with pytest.raises(HTTPException):
        b.receive(packet, "public")
    assert not b.cached()


async def test_block_and_admin_disable_stop_discovery(mesh):
    clients, apps, wire, card = await public_mesh(mesh)
    c, a = clients["carol"], clients["alice"]
    assert (await c.post("/api/network/blocks", json={"actor": card["id"]})).status_code == 200
    assert await browse(c) == []
    # 来源也可以屏蔽指定公共身份，申请不会借目录绕过屏蔽。
    target = apps["alice.test"].state.store.one("SELECT id FROM users")["id"]
    sender = apps["carol.test"].state.store.one("SELECT id FROM users")["id"]
    actor = actor_id(apps["carol.test"].state.network.node, sender)
    apps["alice.test"].state.store.execute(
        "INSERT INTO network_blocks VALUES(?,?,?)", (target, actor, "carol")
    )
    await c.delete("/api/network/blocks/" + card["id"])
    result = await c.post(
        f"/api/network/users/{card['id']}/interact", json={"action": "invite", "consent": True}
    )
    assert result.status_code == 502, result.text
    assert not apps["alice.test"].state.store.all("SELECT * FROM relationships")
    await configure(a, enabled=False)
    assert await apps["alice.test"].state.network.discovery.local_packets() == []
    assert not apps["bob.test"].state.network.discovery.cached()


async def test_public_node_and_seed_require_separate_admin_consent(mesh):
    clients, apps, _ = mesh
    a, b = clients["alice"], clients["bob"]
    await enable(a)
    assert (await a.put("/api/discovery/admin", json={"enabled": True})).status_code == 422
    assert (
        await a.put("/api/discovery/admin", json={"serve": True, "consent": True})
    ).status_code == 422
    await configure(b, serve=True)
    await configure(a)
    code = (await b.post("/api/discovery/admin/code")).json()["code"]
    assert (
        await a.post("/api/discovery/admin/seeds", json={"name": "b", "code": code})
    ).status_code == 422
    seed = await connect(a, b)
    assert (
        await a.post(
            "/api/discovery/admin/seeds", json={"name": "b", "code": code, "consent": True}
        )
    ).status_code == 409
    assert (await a.delete("/api/discovery/admin/seeds/" + seed["id"])).status_code == 200
    assert (await a.get("/api/discovery/admin")).json()["seed_count"] == 0
    uid = apps["alice.test"].state.store.one("SELECT id FROM users")["id"]
    apps["alice.test"].state.store.execute("UPDATE users SET role='member' WHERE id=?", (uid,))
    assert (await a.get("/api/discovery/admin")).status_code == 403


async def test_network_publication_also_local_without_expanding_private_grants(mesh):
    clients, apps, _ = mesh
    a = clients["alice"]
    app = apps["alice.test"]
    await enable(a)
    post = await a.post(
        "/api/feed",
        json={
            "media": {"title": "搏击俱乐部", "kind": "movie", "tmdb_id": 550},
            "rating": 5,
            "publish_consent": True,
        },
    )
    assert post.status_code == 201
    await publish(a, recommendations=True)
    uid = app.state.store.one("SELECT id FROM users")["id"]
    assert not app.state.store.one("SELECT discoverable FROM users WHERE id=?", (uid,))[
        "discoverable"
    ]
    original = app.state.upstream.identity

    async def identity(token):
        if token == "secret-local":
            return {"username": "local", "nickname": "本服读者", "member_id": 2, "role": "member"}
        return await original(token)

    app.state.upstream.identity = identity
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://alice.test", headers=HEADERS
    ) as viewer:
        assert (await viewer.post("/api/login", json={"token": "secret-local"})).status_code == 200
        cards = (await viewer.get("/api/directory")).json()["users"]
        assert len(cards) == 1 and cards[0]["network_public"]
        assert cards[0]["recommendations"][0]["rating"] == 5
        assert cards[0]["history"] == []
        profile = (await viewer.get("/api/community/profiles/" + uid)).json()["public_profile"]
        assert profile["recommendations"] == cards[0]["recommendations"]
        result = await viewer.post("/api/requests", json={"user_id": uid, "address_consent": True})
        assert result.status_code == 200 and result.json()["status"] == "pending"
        assert (await viewer.get("/api/friends/" + result.json()["id"])).status_code == 403
        await a.put("/api/network/publication", json={"publish": False})
        assert (await viewer.get("/api/directory")).json()["users"] == []
        user = app.state.store.one("SELECT * FROM users WHERE id=?", (uid,))
        assert not user["discoverable"] and not user["share_history"] and not user["share_current"]


async def test_disable_reenable_and_remove_readd_do_not_replay_old_withdrawal(mesh):
    clients, apps, wire, card = await public_mesh(mesh)
    a, b, c = (clients[n] for n in ("alice", "bob", "carol"))
    origin, consumer = (apps[n + ".test"].state.network.discovery for n in ("alice", "carol"))
    await configure(a, enabled=False)
    await configure(a)
    await origin.refresh()
    await consumer.refresh()
    assert len(await browse(c)) == 1
    seed = origin.seeds()[0]
    assert (await a.delete("/api/discovery/admin/seeds/" + seed["id"])).status_code == 200
    assert not apps["bob.test"].state.network.discovery.cached()
    await connect(a, b)
    await origin.refresh()
    await consumer.refresh()
    assert len(await browse(c)) == 1


async def test_claimed_endpoint_must_prove_card_identity_before_contact(mesh):
    clients, apps, wire, card = await public_mesh(mesh)
    a = apps["alice.test"].state.network.discovery
    c = apps["carol.test"].state.network.discovery
    packet = (await a.local_packets())[0]
    packet["payload"]["descriptor"]["origin"] = "http://bob.test"
    packet = a.sign(packet["payload"])
    c.store.execute("DELETE FROM discovery_cards")
    c.receive(packet, "public")
    wire.clear()
    response = await clients["carol"].post(
        f"/api/network/users/{card['id']}/interact", json={"action": "invite", "consent": True}
    )
    assert response.status_code == 502
    messages = [json.loads(req.content).get("message", {}) for req in wire]
    assert messages and all(msg.get("kind") == "ping" for msg in messages)
    assert not apps["carol.test"].state.store.all("SELECT * FROM relationships")


async def test_disable_while_fetching_never_accepts_late_directory(mesh):
    clients, apps, wire, card = await public_mesh(mesh)
    c = apps["carol.test"].state.network.discovery
    original = c.request

    async def delayed(descriptor, message):
        result = await original(descriptor, message)
        if message["kind"] == "directory":
            c.store.execute("UPDATE discovery_config SET enabled=0,revision=revision+1")
            c.store.execute("DELETE FROM discovery_cards")
        return result

    c.request = delayed
    await c.refresh()
    assert not c.cached() and not c.directory(c.store.one("SELECT id FROM users")["id"])


async def test_public_peer_never_inherits_private_network_permission(mesh):
    clients, apps, wire = mesh
    from movieclaw_friends.discovery import Discovery

    network = apps["alice.test"].state.network
    network.peers.transport = None
    network.peers.allow_private = True
    public = Discovery(network)
    assert not public.peer.allow_private
    with pytest.raises(HTTPException):
        public.validate_descriptor(
            {"origin": "http://192.168.1.10", "endpoint": "", "key": network.public}
        )
