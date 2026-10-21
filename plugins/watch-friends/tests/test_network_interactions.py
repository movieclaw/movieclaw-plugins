"""两跳密封消息、陌生人互动、好友入口与撤回边界。"""

from test_network import browse, link, publish
from test_network import mesh as _mesh

mesh = _mesh


async def prepare(mesh):
    clients, apps, wire = mesh
    a, b, c = clients["alice"], clients["bob"], clients["carol"]
    await link(a, b, apps)
    await link(b, c, apps, "bob", "carol")
    post = await a.post(
        "/api/feed",
        json={
            "media": {"title": "搏击俱乐部", "kind": "movie", "tmdb_id": 550},
            "rating": 5,
            "publish_consent": True,
        },
    )
    assert post.status_code == 201, post.text
    await publish(a, forward=True, recommendations=True)
    await apps["bob.test"].state.network.refresh()
    await apps["carol.test"].state.network.refresh()
    card = (await browse(c))[0]
    from movieclaw_friends.network_interactions import post_key

    key = post_key(card["recommendations"][0])
    return clients, apps, wire, card, key, post.json()


async def test_two_hop_like_comment_dedup_delete_report_and_withdraw(mesh):
    clients, apps, wire, card, key, post = await prepare(mesh)
    c = clients["carol"]
    path = f"/api/network/users/{card['id']}/interact"
    response = await c.post(
        path, json={"action": "like", "post_key": key, "liked": True, "consent": True}
    )
    assert response.status_code == 200, response.text
    assert response.json()["like_count"] == 1
    response = await c.post(
        path,
        json={
            "action": "comment",
            "post_key": key,
            "body": "这部值得看",
            "consent": True,
            "request_id": "same-comment-request",
        },
    )
    assert response.status_code == 200, response.text
    assert len(response.json()["comments"]) == 1
    again = await c.post(
        path,
        json={
            "action": "comment",
            "post_key": key,
            "body": "这部值得看",
            "consent": True,
            "request_id": "same-comment-request",
        },
    )
    assert len(again.json()["comments"]) == 1
    assert all(
        "这部值得看" not in request.content.decode()
        for request in wire
        if "/federation/directory" in request.url.path
    )
    page = await clients["alice"].get("/api/feed")
    assert page.json()["posts"][0]["like_count"] == 1
    assert page.json()["posts"][0]["comments"][0]["body"] == "这部值得看"
    comment = response.json()["comments"][0]["id"]
    response = await c.post(
        path,
        json={"action": "delete_comment", "post_key": key, "comment_id": comment, "consent": True},
    )
    assert response.status_code == 200 and response.json()["comments"] == []
    response = await c.post(
        path, json={"action": "report", "post_key": key, "body": "测试审核", "consent": True}
    )
    assert response.status_code == 200
    report = (await clients["alice"].get("/api/network/admin/reports")).json()["reports"][0]
    assert (
        await clients["alice"].post(
            f"/api/network/admin/reports/{report['id']}/resolve", json={"action": "hide"}
        )
    ).status_code == 200
    response = await c.post(path, json={"action": "view", "post_key": key})
    assert response.status_code == 404
    await clients["alice"].put("/api/network/publication", json={"publish": False})
    response = await c.post(path, json={"action": "invite", "consent": True})
    assert response.status_code == 404


async def test_friend_request_from_two_hop_card_no_private_access_before_accept(mesh):
    clients, apps, wire, card, key, post = await prepare(mesh)
    path = f"/api/network/users/{card['id']}/interact"
    response = await clients["carol"].post(path, json={"action": "invite", "consent": True})
    assert response.status_code == 200, response.text
    number = response.json()["id"]
    assert response.json()["status"] == "pending"
    assert (await clients["carol"].get(f"/api/friends/{number}")).status_code == 403
    requests = (await clients["alice"].get("/api/requests")).json()
    assert len(requests["incoming"]) == 1
    # 中转不能读取好友邀请、网络端点或任何用户的旧好友凭据。
    for request in wire:
        if request.url.host == "bob.test" and "/federation/directory" in request.url.path:
            assert "mcf1." not in request.content.decode()
    assert len(apps["bob.test"].state.store.all("SELECT * FROM relationships")) == 0


async def test_source_reply_explicitly_public_and_network_comment_moderation(mesh):
    clients, apps, wire, card, key, post = await prepare(mesh)
    a, c = clients["alice"], clients["carol"]
    assert (
        await a.post(f"/api/feed/{post['id']}/network-comments", json={"body": "给所有影友的回复"})
    ).status_code == 422
    response = await a.post(
        f"/api/feed/{post['id']}/network-comments",
        json={"body": "给所有影友的回复", "consent": True},
    )
    assert response.status_code == 201, response.text
    row = response.json()
    assert row["author"]["is_self"]
    result = await c.post(
        f"/api/network/users/{card['id']}/interact", json={"action": "view", "post_key": key}
    )
    assert result.status_code == 200, result.text
    assert result.json()["comments"][0]["body"] == "给所有影友的回复"
    assert result.json()["comments"][0]["author"]["nickname"] == "alice"
    assert (
        await clients["bob"].delete("/api/feed/network-comments/" + row["id"][1:])
    ).status_code == 404
    assert (await a.delete("/api/feed/network-comments/" + row["id"][1:])).status_code == 200


async def test_large_profile_avatar_does_not_bloat_comment_page_and_block_stops_writes(mesh):
    import base64

    clients, apps, wire, card, key, post = await prepare(mesh)
    store = apps["carol.test"].state.store
    avatar = (
        "data:image/jpeg;base64,"
        + base64.b64encode(b"\xff\xd8\xff" + b"x" * 62000 + b"\xff\xd9").decode()
    )
    user = store.one("SELECT id FROM users")
    store.execute(
        "INSERT INTO account_profiles(user_id,avatar_data,updated_at) VALUES(?,?,?) "
        "ON CONFLICT(user_id) DO UPDATE SET avatar_data=excluded.avatar_data",
        (user["id"], avatar, "2026-10-21T00:00:00Z"),
    )
    path = f"/api/network/users/{card['id']}/interact"
    for i in range(12):
        response = await clients["carol"].post(
            path,
            json={"action": "comment", "post_key": key, "body": f"公开评论 {i}", "consent": True},
        )
        assert response.status_code == 200, response.text
    assert len(response.content) < 20000
    author = apps["alice.test"].state.store.one("SELECT id FROM network_public_actors")["id"]
    assert (
        await clients["alice"].post("/api/network/blocks", json={"actor": author})
    ).status_code == 200
    response = await clients["carol"].post(
        path, json={"action": "comment", "post_key": key, "body": "应被拒绝", "consent": True}
    )
    assert response.status_code == 404
    assert not apps["alice.test"].state.store.one(
        "SELECT 1 FROM network_comments WHERE body='应被拒绝'"
    )
