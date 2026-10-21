"""在正式原生挂载前置宿主签名校验，独立互联凭据不能借用好友关系。"""

import json

from test_native_federation import PREFIX
from test_native_federation import native_pair as _native_pair

native_pair = _native_pair


async def test_native_network_separate_signed_path_and_server_approval(native_pair):
    alice, bob, hosts, wire = native_pair
    path = PREFIX + "/api/network"
    for host in hosts.values():
        host.app.state.store.execute(
            "UPDATE users SET role='admin',username='admin:0',member_id=0,"
            "token=? WHERE role!='demo'",
            (host.app.state.store.encrypt("admin:0"),),
        )
    for client in (alice, bob):
        result = await client.put(path + "/admin", json={"enabled": True, "consent": True})
        assert result.status_code == 200, result.text
    code = (await alice.post(path + "/admin/invite")).json()["code"]
    response = await bob.post(
        path + "/admin/connect", json={"code": code, "name": "A", "consent": True}
    )
    assert response.status_code == 200, response.text
    peers = (await alice.get(path + "/admin")).json()["peers"]
    assert len(peers) == 1 and peers[0]["state"] == "pending"
    assert (
        await alice.post(PREFIX + "/federation/directory", json={"action": "directory"})
    ).status_code == 404
    await alice.patch(path + "/admin/peers/" + peers[0]["id"], json={"state": "accepted"})
    options = {"publish": True, "forward": True}
    preview = (await alice.post(path + "/preview", json=options)).json()
    response = await alice.put(
        path + "/publication",
        json={**options, "consent": True, "preview_token": preview["preview_token"]},
    )
    assert response.status_code == 200, response.text
    await hosts["bob"].app.state.network.refresh()
    await bob.put(path + "/browse", json={"enabled": True})
    result = await bob.get(path + "/directory")
    assert len(result.json()["users"]) == 1
    assert all("sig=" in str(r.url) for r in wire if r.url.path.endswith("/federation/directory"))
    assert "endpoints" not in result.text
    # 双方旧好友签名和邀请码没有新增联网能力；两套令牌完全独立。
    invitation = (await alice.get(PREFIX + "/api/me")).json()["user"]["invite_code"]
    assert "/federation/directory" not in json.dumps(invitation)
    friend_token = hosts["alice"].app.state.store.secret()
    signed = hosts["alice"].signed("/federation/directory")
    response = await alice.post(
        signed, json={"action": "directory"}, headers={"X-Watch-Friends-Token": friend_token}
    )
    assert response.status_code == 401
