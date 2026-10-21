"""双原生实例与真实签名区验证跨服动态的独立授权、关系作者和撤回。"""

import asyncio
import base64
import json
import uuid

import httpx
import pytest
from movieclaw_friends.peers import PeerClient, PeerError
from movieclaw_friends.social_federation import SOCIAL_PATH
from movieclaw_friends.store import digest
from test_native_federation import PREFIX, accept_pair, request_pair
from test_native_federation import native_pair as native_pair


@pytest.fixture
async def social_pair(native_pair):
    alice, bob, hosts, wire = native_pair
    for host in hosts.values():
        host.endpoints[SOCIAL_PATH] = host.signed(SOCIAL_PATH)
    yield alice, bob, hosts, wire


async def post(client, title="好友的公开推荐", **changes):
    response = await client.post(
        PREFIX + "/api/feed",
        json={
            "media": {"tmdb_id": 550, "kind": "movie", "title": title},
            "body": "本人主动发表的推荐",
            "publish_consent": True,
        }
        | changes,
    )
    assert response.status_code == 201, response.text
    return response.json()


async def share(client, name, enabled=True):
    response = await client.patch(
        PREFIX + "/api/profile", json={"nickname": name, "share_posts": enabled}
    )
    assert response.status_code == 200, response.text
    assert response.json()["user"]["share_posts"] is enabled


def path(relation, suffix=""):
    return PREFIX + f"/api/friends/{relation}/feed" + suffix


async def ready(pair):
    alice, bob, _, _ = pair
    ids = await accept_pair(alice, bob)
    await share(bob, "bob")
    return ids


async def test_remote_rating_only_and_legacy_text_recommendations(social_pair):
    alice, bob, _, _ = social_pair
    a_id, _ = await ready(social_pair)
    rated = await post(bob, body="", rating=4)
    legacy = await post(bob)
    response = await alice.get(path(a_id))
    assert response.status_code == 200, response.text
    received = {row["id"]: row for row in response.json()["posts"]}
    assert received[rated["id"]]["rating"] == 4
    assert received[rated["id"]]["body"] == ""
    assert received[legacy["id"]]["rating"] is None


async def test_friendship_does_not_imply_social_sharing_or_read_other_authors(social_pair):
    alice, bob, hosts, wire = social_pair
    first = await post(bob)
    a_id, b_id = await request_pair(alice, bob)
    assert (await alice.get(path(a_id))).status_code == 403
    assert (
        await bob.post(PREFIX + f"/api/requests/{b_id}/accept", json={"address_consent": True})
    ).status_code == 200
    response = await alice.get(path(a_id))
    assert response.status_code == 200, response.text
    assert response.json()["posts"] == [] and response.json()["enabled"] is False
    await share(bob, "bob")
    response = await alice.get(path(a_id))
    assert response.status_code == 200, response.text
    posts = response.json()["posts"]
    assert [row["id"] for row in posts] == [first["id"]]
    assert posts[0]["relationship_id"] == a_id and posts[0]["is_remote"] is True
    assert posts[0]["author"]["id"] == hosts["bob"].user_id
    # 本服发表仍可见，不以该作者的跨服开关作为本服feed开关。
    assert len((await bob.get(PREFIX + "/api/feed")).json()["posts"]) == 1
    assert (await alice.get(path(a_id) + "?limit=21")).status_code == 422
    for forbidden in (
        "https://",
        "peer_origin",
        "peer_endpoints",
        "token",
        "file_path",
        "media_item_id",
    ):
        assert forbidden not in response.text
    assert wire and all(
        "authorization" not in row.headers and "cookie" not in row.headers for row in wire
    )


async def test_remote_like_and_comment_are_bound_idempotent_and_private_to_participants(
    social_pair,
):
    alice, bob, hosts, _ = social_pair
    a_id, _ = await ready(social_pair)
    item = await post(bob)
    base = path(a_id, f"/{item['id']}")
    for _ in range(2):
        response = await alice.put(
            base + "/like", json={"liked": True, "author_id": "somebody-else"}
        )
        assert response.status_code == 200, response.text
        assert response.json() == {"liked": True, "like_count": 1}
    request_id = str(uuid.uuid4())
    payload = {"body": "双方可见的跨服评论", "request_id": request_id, "author_id": "forged"}
    first = await alice.post(base + "/comments", json=payload)
    assert first.status_code == 200, first.text
    comment = first.json()
    assert comment["id"].startswith("r") and comment["author"]["is_self"] is True
    assert (await alice.post(base + "/comments", json=payload)).json() == comment
    assert (
        await alice.post(base + "/comments", json=payload | {"body": "更换内容"})
    ).status_code == 502
    store = hosts["bob"].app.state.store
    assert len(store.all("SELECT * FROM social_remote_comments")) == 1
    assert len(store.all("SELECT * FROM social_remote_likes")) == 1
    assert store.all("SELECT * FROM social_comments") == []
    owner_post = (await bob.get(PREFIX + f"/api/feed/{item['id']}")).json()
    assert owner_post["comments"][0]["body"] == payload["body"]
    # 其他本服成员的评论不能随帖导出；当前远端只读作者自己的回复及自己的评论。
    store.execute(
        "INSERT INTO users(id,username,nickname,role,token,invite_token) VALUES(?,?,?,?,?,?)",
        ("charlie", "charlie", "其他本服成员", "member", "unused", store.secret()),
    )
    store.execute(
        "INSERT INTO social_comments(post_id,user_id,body,created_at) VALUES(?,?,?,?)",
        (item["id"], "charlie", "没有同意跨服传播的评论", "2026-10-20T00:00:00+00:00"),
    )
    result = await alice.get(base + "/comments")
    assert result.status_code == 200 and len(result.json()["comments"]) == 1
    assert "没有同意跨服传播" not in result.text
    assert (await alice.delete(base + "/comments/1")).status_code == 502
    assert (await alice.delete(base + f"/comments/{comment['id']}")).status_code == 200
    assert store.all("SELECT * FROM social_remote_comments") == []


async def test_signed_route_token_and_target_author_checks_prevent_forgery(social_pair):
    alice, bob, hosts, _ = social_pair
    a_id, b_id = await ready(social_pair)
    item = await post(bob)
    unsigned = hosts["bob"].origin + PREFIX + SOCIAL_PATH
    assert (await bob.post(unsigned, json={"action": "feed"})).status_code == 404
    signed = hosts["bob"].endpoints[SOCIAL_PATH]
    assert (await bob.post(signed, json={"action": "feed"})).status_code == 401
    row = hosts["alice"].app.state.store.one("SELECT * FROM relationships WHERE id=?", (a_id,))
    token = hosts["alice"].app.state.store.decrypt(row["token"])
    foreign = hosts["bob"].app.state.store
    foreign.execute(
        "INSERT INTO users(id,username,nickname,role,token,invite_token) VALUES(?,?,?,?,?,?)",
        ("other", "other", "其他作者", "member", "unused", foreign.secret()),
    )
    result = foreign.db.execute(
        "INSERT INTO social_posts(user_id,media,body,created_at) VALUES(?,?,?,?)",
        ("other", json.dumps(item["media"]), "其他作者的帖子", item["created_at"]),
    )
    response = await bob.post(
        signed,
        headers={"x-watch-friends-token": token},
        json={"action": "like", "post_id": result.lastrowid, "liked": True, "user_id": "other"},
    )
    assert response.status_code == 404
    assert foreign.all("SELECT * FROM social_remote_likes") == []
    # 另一个有效关系令牌不能删除前一关系的评论，即使知道comment id。
    comment = (
        await alice.post(
            path(a_id, f"/{item['id']}/comments"),
            json={"body": "原作者评论", "request_id": str(uuid.uuid4())},
        )
    ).json()
    for name, relation_id, peer_name, remote_id in (
        ("alice", "other-a", "bob", None),
        ("bob", "other-b", "alice", "other-a"),
    ):
        store = hosts[name].app.state.store
        store.execute(
            "INSERT INTO relationships(id,user_id,peer_origin,peer_user_id,peer_name,direction,"
            "status,token,token_hash,remote_request_id,created_at) "
            "VALUES(?,?,?,?,?,?,'accepted',?,?,?,0)",
            (
                relation_id,
                hosts[name].user_id,
                hosts[peer_name].origin,
                hosts[peer_name].user_id,
                peer_name,
                "outgoing" if name == "alice" else "incoming",
                store.encrypt("other-relationship-token"),
                digest("other-relationship-token"),
                remote_id,
            ),
        )
        store.execute(
            "UPDATE relationships SET peer_endpoints=? WHERE id=?",
            (store.encrypt(json.dumps(hosts[peer_name].endpoints)), relation_id),
        )
    response = await bob.post(
        signed,
        headers={"x-watch-friends-token": "other-relationship-token"},
        json={"action": "delete_comment", "post_id": item["id"], "comment_id": comment["id"]},
    )
    assert response.status_code == 404
    assert len(foreign.all("SELECT * FROM social_remote_comments")) == 1


async def test_legacy_capability_is_explicitly_unsupported_without_guessing_social_url(social_pair):
    alice, bob, hosts, wire = social_pair
    a_id, _ = await ready(social_pair)
    store = hosts["alice"].app.state.store
    row = store.one("SELECT peer_endpoints FROM relationships WHERE id=?", (a_id,))
    endpoints = json.loads(store.decrypt(row["peer_endpoints"]))
    endpoints.pop(SOCIAL_PATH)
    hosts["bob"].endpoints.pop(SOCIAL_PATH)
    store.execute(
        "UPDATE relationships SET peer_endpoints=? WHERE id=?",
        (store.encrypt(json.dumps(endpoints)), a_id),
    )
    wire.clear()
    response = await alice.get(path(a_id))
    assert response.status_code == 409 and "尚不支持跨服动态" in response.text
    assert all(not str(request.url).split("?")[0].endswith(SOCIAL_PATH) for request in wire)


async def test_existing_three_capability_friendship_upgrades_without_new_request(native_pair):
    alice, bob, hosts, wire = native_pair
    a_id, b_id = await accept_pair(alice, bob)
    await share(bob, "bob")
    item = await post(bob)
    for name, relation in (("alice", a_id), ("bob", b_id)):
        store = hosts[name].app.state.store
        row = store.one("SELECT peer_endpoints FROM relationships WHERE id=?", (relation,))
        assert SOCIAL_PATH not in json.loads(store.decrypt(row["peer_endpoints"]))
    # 模拟两端在保留已有关系与凭据的情况下安装新版；不重新发送邀请。
    for host in hosts.values():
        host.endpoints[SOCIAL_PATH] = host.signed(SOCIAL_PATH)
    wire.clear()
    response = await alice.get(path(a_id))
    assert response.status_code == 200, response.text
    assert [row["id"] for row in response.json()["posts"]] == [item["id"]]
    for name, relation in (("alice", a_id), ("bob", b_id)):
        store = hosts[name].app.state.store
        rows = store.all("SELECT * FROM relationships")
        assert len(rows) == 1 and rows[0]["id"] == relation
        assert rows[0]["status"] == "accepted"
        assert SOCIAL_PATH in json.loads(store.decrypt(rows[0]["peer_endpoints"]))
    assert all(not request.url.path.endswith("/federation/requests") for request in wire)


async def test_block_then_revoke_cannot_be_bypassed_by_a_new_relationship(social_pair):
    alice, bob, hosts, _ = social_pair
    a_id, b_id = await ready(social_pair)
    assert (
        await bob.post(PREFIX + "/api/community/blocks", json={"relationship_id": b_id})
    ).status_code == 200
    assert (await bob.delete(PREFIX + f"/api/friends/{b_id}")).status_code == 200
    assert (await alice.delete(PREFIX + f"/api/friends/{a_id}")).status_code == 200
    invitation = (await bob.get(PREFIX + "/api/me")).json()["user"]["invite_code"]
    response = await alice.post(
        PREFIX + "/api/requests", json={"invite_code": invitation, "address_consent": True}
    )
    assert response.status_code == 502, response.text
    owner = hosts["bob"].app.state.store
    assert len(owner.all("SELECT * FROM relationships")) == 1
    assert owner.one("SELECT status FROM relationships WHERE id=?", (b_id,))["status"] == "revoked"
    assert len(owner.all("SELECT * FROM community_blocks")) == 1
    assert (await bob.get(PREFIX + "/api/requests")).json()["incoming"] == []
    assert (await alice.get(PREFIX + "/api/friends")).json()["friends"] == []


async def test_social_proxy_cannot_be_redirected_to_an_arbitrary_origin(social_pair):
    alice, _, hosts, wire = social_pair
    a_id, _ = await ready(social_pair)
    store = hosts["alice"].app.state.store
    row = store.one("SELECT peer_endpoints FROM relationships WHERE id=?", (a_id,))
    endpoints = json.loads(store.decrypt(row["peer_endpoints"]))
    endpoints[SOCIAL_PATH] = (
        "https://evil.example/api/v1/plugins/watch-friends/federation/social?sig=malicious"
    )
    hosts["bob"].endpoints[SOCIAL_PATH] = endpoints[SOCIAL_PATH]
    store.execute(
        "UPDATE relationships SET peer_endpoints=? WHERE id=?",
        (store.encrypt(json.dumps(endpoints)), a_id),
    )
    wire.clear()
    response = await alice.get(path(a_id))
    assert response.status_code == 502
    assert all(request.url.host != "evil.example" for request in wire)
    assert "evil.example" not in response.text and "malicious" not in response.text


def test_optional_social_capability_keeps_three_legacy_paths_and_fixed_target():
    peers = PeerClient()
    paths = {"/federation/requests", "/federation/state", "/federation/data"}
    endpoints = {route: "https://peer.test" + PREFIX + route + "?sig=valid" for route in paths}
    peers.validate_endpoints("https://peer.test", endpoints)
    endpoints[SOCIAL_PATH] = "https://peer.test" + PREFIX + SOCIAL_PATH + "?sig=valid"
    peers.validate_endpoints("https://peer.test", endpoints)
    for invalid in (
        "https://evil.example" + PREFIX + SOCIAL_PATH + "?sig=valid",
        "https://peer.test/private?sig=valid",
        "https://peer.test" + PREFIX + SOCIAL_PATH,
    ):
        with pytest.raises(PeerError):
            peers.validate_endpoints("https://peer.test", endpoints | {SOCIAL_PATH: invalid})


async def test_remote_post_and_comment_pagination_are_bounded_without_duplicates(social_pair):
    alice, bob, _, _ = social_pair
    a_id, _ = await ready(social_pair)
    created = [await post(bob, f"公开推荐{number}") for number in range(22)]
    first = (await alice.get(path(a_id))).json()
    assert len(first["posts"]) == 20
    second = (await alice.get(path(a_id) + f"?before={first['next_before']}")).json()
    assert [row["id"] for row in first["posts"] + second["posts"]] == [
        row["id"] for row in reversed(created)
    ]
    base = path(a_id, f"/{created[0]['id']}/comments")
    ids = []
    for number in range(32):
        response = await alice.post(
            base, json={"body": str(number), "request_id": str(uuid.uuid4())}
        )
        assert response.status_code == 200, response.text
        ids.append(response.json()["id"])
    a = (await alice.get(base + "?limit=30")).json()
    assert len(a["comments"]) == 30
    b = (await alice.get(base + "?limit=30&before=" + a["next_before"])).json()
    assert [row["id"] for row in a["comments"] + b["comments"]] == list(reversed(ids))
    assert b["next_before"] is None


@pytest.mark.parametrize("number", [0, -1, 2**64])
async def test_remote_comment_cursor_rejects_out_of_range_sqlite_ids(social_pair, number):
    _, bob, hosts, _ = social_pair
    a_id, _ = await ready(social_pair)
    item = await post(bob)
    store = hosts["alice"].app.state.store
    row = store.one("SELECT token FROM relationships WHERE id=?", (a_id,))
    cursor = (
        base64.urlsafe_b64encode(json.dumps(["2026-10-20T00:00:00+00:00", 1, number]).encode())
        .decode()
        .rstrip("=")
    )
    response = await bob.post(
        hosts["bob"].endpoints[SOCIAL_PATH],
        headers={"X-Watch-Friends-Token": store.decrypt(row["token"])},
        json={"action": "comments", "post_id": item["id"], "before": cursor},
    )
    assert response.status_code == 422, response.text


async def test_blocking_bound_peer_stops_social_access_without_destroying_relationship(social_pair):
    alice, bob, hosts, _ = social_pair
    a_id, b_id = await ready(social_pair)
    await post(bob)
    assert (await alice.get(path(a_id))).status_code == 200
    store = hosts["bob"].app.state.store
    # 社区本服主人以已绑定关系屏蔽该远端，不接受客户端任意构造远端作者。
    response = await bob.post(PREFIX + "/api/community/blocks", json={"relationship_id": b_id})
    assert response.status_code == 200, response.text
    response = await alice.get(path(a_id))
    assert response.status_code in {403, 502}
    assert response.json().get("posts") is None
    assert store.one("SELECT status FROM relationships WHERE id=?", (b_id,))["status"] == "accepted"


@pytest.mark.parametrize(
    "change", ["unshare", "revoke_owner", "revoke_viewer", "block_owner", "block_viewer"]
)
async def test_inflight_unshare_or_either_revocation_stops_data_without_cache(
    social_pair, monkeypatch, change
):
    alice, bob, hosts, _ = social_pair
    a_id, b_id = await ready(social_pair)
    await post(bob, "在途不能继续展示的推荐")
    started, release = asyncio.Event(), asyncio.Event()
    original = httpx.MockTransport.handle_async_request

    async def delayed(transport, request):
        response = await original(transport, request)
        if request.url.path.endswith(SOCIAL_PATH):
            started.set()
            await release.wait()
        return response

    monkeypatch.setattr(httpx.MockTransport, "handle_async_request", delayed)
    task = asyncio.create_task(alice.get(path(a_id)))
    await asyncio.wait_for(started.wait(), timeout=2)
    if change == "unshare":
        await share(bob, "bob", False)
    elif change == "revoke_owner":
        await bob.delete(PREFIX + f"/api/friends/{b_id}")
    elif change == "revoke_viewer":
        await alice.delete(PREFIX + f"/api/friends/{a_id}")
    else:
        client, relation = (bob, b_id) if change == "block_owner" else (alice, a_id)
        assert (
            await client.post(PREFIX + "/api/community/blocks", json={"relationship_id": relation})
        ).status_code == 200
    release.set()
    response = await task
    assert response.status_code in ({403, 404, 502} if change.startswith("block") else {403}), (
        response.text
    )
    assert "在途不能继续展示" not in response.text
    assert (
        hosts["alice"].app.state.store.one("SELECT cache FROM relationships WHERE id=?", (a_id,))[
            "cache"
        ]
        is None
    )


@pytest.mark.parametrize("change", ["revoke", "block"])
async def test_inflight_friend_snapshot_checks_remote_revocation_before_browser_output(
    social_pair, monkeypatch, change
):
    alice, bob, hosts, _ = social_pair
    a_id, b_id = await ready(social_pair)
    assert (
        await bob.patch(
            PREFIX + "/api/profile",
            json={"nickname": "bob", "share_current": True, "share_history": True},
        )
    ).status_code == 200
    started, release = asyncio.Event(), asyncio.Event()
    original = httpx.MockTransport.handle_async_request

    async def delayed(transport, request):
        response = await original(transport, request)
        if request.url.path.endswith("/federation/data"):
            started.set()
            await release.wait()
        return response

    monkeypatch.setattr(httpx.MockTransport, "handle_async_request", delayed)
    task = asyncio.create_task(alice.get(PREFIX + f"/api/friends/{a_id}"))
    await asyncio.wait_for(started.wait(), timeout=2)
    if change == "revoke":
        assert (await bob.delete(PREFIX + f"/api/friends/{b_id}")).status_code == 200
    else:
        assert (
            await bob.post(PREFIX + "/api/community/blocks", json={"relationship_id": b_id})
        ).status_code == 200
    release.set()
    response = await task
    assert response.status_code in {403, 502}, response.text
    assert "bob的电影" not in response.text
    assert (
        hosts["alice"].app.state.store.one("SELECT cache FROM relationships WHERE id=?", (a_id,))[
            "cache"
        ]
        is None
    )


async def test_multi_image_recommendation_survives_native_signed_friend_feed(social_pair):
    alice, bob, _, _ = social_pair
    a_id, _ = await ready(social_pair)
    images = [
        {"path": f"/selected-{i}.jpg", "type": "poster" if i % 2 else "backdrop"} for i in range(9)
    ]
    published = await post(
        bob, media={"tmdb_id": 550, "kind": "movie", "title": "多图推荐", "cover_images": images}
    )
    result = await alice.get(path(a_id))
    assert result.status_code == 200
    received = next(row for row in result.json()["posts"] if row["id"] == published["id"])
    assert received["media"]["cover_images"] == images
    assert received["media"]["cover_path"] == images[0]["path"]
