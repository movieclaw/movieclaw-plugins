"""丢包、进程重建、轮换和解绑通过真实多实例路由验证。"""

import pytest
from movieclaw_friends.network_management import NetworkManagement
from movieclaw_friends.peers import PeerError
from test_network import enable, link
from test_network import mesh as _mesh

mesh = _mesh


async def test_lost_join_response_retries_same_invite_after_restart(mesh):
    clients, apps, _ = mesh
    a, b = clients["alice"], clients["bob"]
    await enable(a)
    await enable(b)
    code = (await a.post("/api/network/admin/invite")).json()["code"]
    network = apps["bob.test"].state.network
    original = network.request

    async def lost(row, data):
        await original(row, data)
        raise PeerError("模拟响应丢失")

    network.request = lost
    result = await b.post(
        "/api/network/admin/connect", json={"code": code, "name": "A", "consent": True}
    )
    assert result.status_code == 200 and result.json()["peers"][0]["error"]
    assert len((await a.get("/api/network/admin")).json()["peers"]) == 1
    peer = result.json()["peers"][0]["id"]
    network.request = original
    network.management = NetworkManagement(network)
    assert (await b.post(f"/api/network/admin/peers/{peer}/retry")).status_code == 200
    assert len((await a.get("/api/network/admin")).json()["peers"]) == 1
    assert not (await b.get("/api/network/admin")).json()["peers"][0]["error"]


@pytest.mark.parametrize("phase", ["rotate_prepare", "rotate_commit"])
async def test_rotation_lost_reply_old_token_rejected_and_data_kept(mesh, phase):
    clients, apps, _ = mesh
    a, b = clients["alice"], clients["bob"]
    await link(a, b, apps)
    network = apps["bob.test"].state.network
    peer = network.store.one("SELECT * FROM network_peers")
    token = network.store.decrypt(peer["token"])
    original, lost_once = network.request, False

    async def lost(row, data):
        nonlocal lost_once
        result = await original(row, data)
        if data["action"] == phase and not lost_once:
            lost_once = True
            raise PeerError("模拟丢包")
        return result

    network.request = lost
    assert (await b.post(f"/api/network/admin/peers/{peer['id']}/rotate")).status_code == 200
    result = await b.post(f"/api/network/admin/peers/{peer['id']}/retry")
    assert result.status_code == 200 and not result.json()["peers"][0]["error"]
    response = await a.post(
        "/federation/directory", json={"action": "status"}, headers={"X-Watch-Friends-Token": token}
    )
    assert response.status_code == 401
    row = network.store.one("SELECT * FROM network_peers")
    assert row["node"] == peer["node"] and row["state"] == "accepted"
    assert (await network.request(row, {"action": "status"}))["state"] == "accepted"
    assert (
        await clients["carol"].post(f"/api/network/admin/peers/{peer['id']}/retry")
    ).status_code == 404


async def test_remove_pair_and_reconnect_requires_new_approval(mesh):
    clients, apps, _ = mesh
    a, b = clients["alice"], clients["bob"]
    await link(a, b, apps)
    peer = (await b.get("/api/network/admin")).json()["peers"][0]
    result = await b.delete("/api/network/admin/peers/" + peer["id"])
    assert result.status_code == 200 and result.json()["remote_notified"]
    assert (await a.get("/api/network/admin")).json()["peers"] == []
    await link(a, b, apps)
    assert (await b.get("/api/network/admin")).json()["peers"][0]["state"] == "accepted"


async def test_repeated_rotation_and_old_credential_cannot_commit(mesh):
    clients, apps, _ = mesh
    await link(clients["alice"], clients["bob"], apps)
    network = apps["bob.test"].state.network
    peer = network.store.one("SELECT * FROM network_peers")
    path = f"/api/network/admin/peers/{peer['id']}/rotate"
    for _ in range(3):
        result = await clients["bob"].post(path)
        assert result.status_code == 200, result.text
        assert not result.json()["peers"][0]["error"]
    assert len(network.store.all("SELECT * FROM network_peers")) == 1
