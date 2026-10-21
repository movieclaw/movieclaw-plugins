"""社区边界使用真实 SQLite 和 HTTP；测试身份来自可信映射，不信任请求中的角色。"""

import json
from contextlib import AsyncExitStack
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI, HTTPException, Request
from movieclaw_friends.app import Media
from movieclaw_friends.community import Community, register_community
from movieclaw_friends.feed import register_feed
from movieclaw_friends.store import Store


@pytest_asyncio.fixture
async def community(tmp_path):
    store = Store(tmp_path)
    for name in ("alice", "bob", "carol", "admin", "demo"):
        store.execute(
            "INSERT INTO users(id,username,nickname,role,token,invite_token) VALUES(?,?,?,?,?,?)",
            (
                name,
                name,
                name,
                "admin" if name == "admin" else "demo" if name == "demo" else "member",
                store.encrypt("private-token"),
                store.secret(),
            ),
        )
    app = FastAPI()

    def identity(request: Request):
        user = store.one("SELECT * FROM users WHERE id=?", (request.headers.get("x-test-user"),))
        if user is None:
            raise HTTPException(401, "测试会话未登录")
        return user

    service = register_community(app, store, identity)
    register_feed(app, store, identity, Media, community=service)
    async with AsyncExitStack() as stack:
        clients = {}
        for name in ("alice", "bob", "carol", "admin", "demo", "anonymous"):
            clients[name] = await stack.enter_async_context(
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="http://community.test",
                    headers={"x-test-user": name},
                )
            )
        yield SimpleNamespace(store=store, service=service, clients=clients, app=app)
    store.close()


async def post(community, author="alice", body="推荐一部值得聊聊的电影。"):
    response = await community.clients[author].post(
        "/api/feed",
        json={
            "media": {
                "tmdb_id": 157336,
                "kind": "movie",
                "title": "星际穿越",
                "poster_path": "/spQm5r317XPNHK1941ChWmqzkZs.jpg",
            },
            "body": body,
            "publish_consent": True,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


async def report(community, target_id, *, kind="post", user="bob"):
    response = await community.clients[user].post(
        "/api/community/reports",
        json={"target_type": kind, "target_id": target_id, "reason": "请管理员核对这条内容"},
    )
    assert response.status_code == 200, response.text
    return response.json()["id"]


@pytest.mark.asyncio
async def test_profile_presets_bio_are_public_but_never_change_identity_or_privacy(community):
    alice, bob = community.clients["alice"], community.clients["bob"]
    before = community.store.one("SELECT * FROM users WHERE id='alice'")
    response = await alice.patch(
        "/api/community/profile",
        json={
            "avatar_id": "xiaolu",
            "bio": "<script>这只是我的文字简介</script>",
            "user_id": "bob",
            "role": "admin",
            "discoverable": True,
        },
    )
    assert response.status_code == 200
    own = response.json()
    assert own["profile"]["id"] == "alice" and own["profile"]["role"] == "member"
    assert own["profile"]["avatar_id"] == "xiaolu" and own["is_admin"] is False
    assert own["public_profile"] is None
    public = (await bob.get("/api/community/profiles/alice")).json()
    assert public["profile"]["bio"] == "<script>这只是我的文字简介</script>"
    assert public["profile"]["avatar_id"] == "xiaolu"
    assert "role" not in public["profile"] and public["public_profile"] is None
    assert all(value not in json.dumps(public) for value in ("private-token", "invite_token"))
    assert community.store.one("SELECT * FROM users WHERE id='alice'") == before
    assert (await bob.get("/api/community/profile")).json()["profile"]["bio"] == ""
    assert (await alice.patch("/api/community/profile", json={"bio": ""})).json()["profile"][
        "avatar_id"
    ] == "xiaolu"
    assert (await community.clients["admin"].get("/api/community/profile")).json()[
        "is_admin"
    ] is True
    for payload in ({"avatar_id": "https://private.test/avatar.jpg"}, {"bio": "字" * 301}):
        assert (await alice.patch("/api/community/profile", json=payload)).status_code == 422


@pytest.mark.asyncio
async def test_profile_page_only_returns_already_confirmed_public_snapshots(community):
    bob = community.clients["bob"]
    media = {
        "tmdb_id": 157336,
        "title": "已公开作品",
        "kind": "movie",
        "path": "/private/never.mkv",
        "poster_url": "https://private.test/image?token=secret",
    }
    history = [{"media": media, "ended_at": f"2026-10-{index + 1:02d}"} for index in range(8)]
    community.store.execute(
        "UPDATE users SET public_history=?,public_favorites=? WHERE id='alice'",
        (json.dumps(history), json.dumps([media])),
    )
    assert (await bob.get("/api/community/profiles/alice")).json()["public_profile"] is None
    # 即使发过公开推荐，个人页也不能借此读出尚未确认的观看数据。
    await post(community)
    assert (await bob.get("/api/community/profiles/alice")).json()["public_profile"] is None
    community.store.execute(
        "UPDATE users SET discoverable=1,public_recent=1,share_favorites=1 WHERE id='alice'"
    )
    visible = (await bob.get("/api/community/profiles/alice")).json()["public_profile"]
    assert len(visible["history"]) == 5 and len(visible["favorites"]) == 1
    assert "private.test" not in json.dumps(visible) and "/private/" not in json.dumps(visible)
    own = (await community.clients["alice"].get("/api/community/profile")).json()
    assert own["public_profile"] == visible
    community.store.execute("UPDATE users SET public_recent=0,share_favorites=0 WHERE id='alice'")
    card = (await bob.get("/api/community/profiles/alice")).json()["public_profile"]
    assert card["history"] == card["favorites"] == []
    community.store.execute("UPDATE users SET discoverable=0 WHERE id='alice'")
    assert (await bob.get("/api/community/profiles/alice")).json()["public_profile"] is None


@pytest.mark.asyncio
async def test_like_notifications_are_idempotent_comments_distinct_and_reads_owned(community):
    created = await post(community)
    alice, bob, carol = (community.clients[name] for name in ("alice", "bob", "carol"))
    path = f"/api/feed/{created['id']}"
    for liked in (True, True, False, True):
        assert (await bob.put(path + "/like", json={"liked": liked})).status_code == 200
    for index in range(2):
        assert (
            await bob.post(path + "/comments", json={"body": f"私人地址不进通知 secret {index}"})
        ).status_code == 201
    assert (await alice.put(path + "/like", json={"liked": True})).status_code == 200
    assert (await alice.post(path + "/comments", json={"body": "本人评论"})).status_code == 201
    data = (await alice.get("/api/community/notifications")).json()
    assert data["unread_count"] == len(data["notifications"]) == 3
    assert {item["kind"] for item in data["notifications"]} == {"like", "comment"}
    assert all(item["actor"]["id"] == "bob" for item in data["notifications"])
    assert "secret" not in json.dumps(data) and "body" not in json.dumps(data)
    assert (await carol.get("/api/community/notifications")).json()["notifications"] == []
    notification_id = data["notifications"][0]["id"]
    assert (
        await bob.post(f"/api/community/notifications/{notification_id}/read")
    ).status_code == 404
    for _ in range(2):
        marked = await alice.post(f"/api/community/notifications/{notification_id}/read")
        assert marked.json() == {"ok": True, "unread_count": 2}
    assert (await alice.get("/api/community/notifications/unread")).json()["unread_count"] == 2


@pytest.mark.asyncio
async def test_notification_pagination_and_blocked_rows_do_not_hide_later_visible_rows(community):
    service, alice = community.service, community.clients["alice"]
    ids = []
    for index in range(5):
        ids.append(service.notify("alice", "carol", "comment", 1, key=f"carol-{index}"))
    for index in range(4):
        service.notify("alice", "bob", "comment", 1, key=f"bob-{index}")
    assert service.notify("alice", "alice", "like", 1) is None
    assert service.notify("demo", "alice", "like", 1) is None
    assert (await alice.post("/api/community/blocks", json={"user_id": "bob"})).status_code == 200
    first = (await alice.get("/api/community/notifications?limit=2")).json()
    second = (
        await alice.get(f"/api/community/notifications?limit=2&before={first['next_before']}")
    ).json()
    third = (
        await alice.get(f"/api/community/notifications?limit=2&before={second['next_before']}")
    ).json()
    assert first["unread_count"] == 5
    assert [row["id"] for page in (first, second, third) for row in page["notifications"]] == list(
        reversed(ids)
    )
    assert third["next_before"] is None
    assert service.notify("alice", "bob", "like", 2) is None


@pytest.mark.asyncio
async def test_blocks_are_two_way_but_do_not_delete_posts_or_affect_third_people(community):
    alice_post = await post(community, "alice")
    bob_post = await post(community, "bob")
    carol_post = await post(community, "carol")
    alice, bob, carol = (community.clients[name] for name in ("alice", "bob", "carol"))
    comment = (
        await alice.post(f"/api/feed/{carol_post['id']}/comments", json={"body": "Alice评论"})
    ).json()
    assert (await bob.post("/api/community/blocks", json={"user_id": "alice"})).status_code == 200
    assert community.service.blocked("alice", "bob") is True
    assert community.service.blocked("bob", "alice") is True
    assert len((await bob.get("/api/feed")).json()["posts"]) == 2
    assert len((await alice.get("/api/feed")).json()["posts"]) == 2
    assert len((await carol.get("/api/feed")).json()["posts"]) == 3
    assert (await bob.get("/api/community/profiles/alice")).status_code == 404
    assert (await alice.get("/api/community/profiles/bob")).status_code == 404
    for client, target in ((alice, bob_post), (bob, alice_post)):
        assert (
            await client.put(f"/api/feed/{target['id']}/like", json={"liked": True})
        ).status_code == 404
        assert (
            await client.post(f"/api/feed/{target['id']}/comments", json={"body": "不能评论"})
        ).status_code == 404
    assert (await bob.get(f"/api/feed/{carol_post['id']}/comments")).json()["comments"] == []
    assert (await carol.get(f"/api/feed/{carol_post['id']}/comments")).json()["comments"][0][
        "id"
    ] == comment["id"]
    # Alice 无权移除 Bob 的屏蔽；解除后原有内容仍在，未被偷偷删除。
    await alice.delete("/api/community/blocks/bob")
    assert community.service.blocked("alice", "bob") is True
    await bob.delete("/api/community/blocks/alice")
    assert community.service.blocked("alice", "bob") is False
    assert len((await bob.get("/api/feed")).json()["posts"]) == 3
    assert len(community.store.all("SELECT * FROM social_posts")) == 3
    assert (await bob.post("/api/community/blocks", json={"user_id": "missing"})).status_code == 404
    assert (await bob.post("/api/community/blocks", json={"user_id": "bob"})).status_code == 400


def remote_relation(community, *, relation_id="bound-remote", owner="alice", peer_id="bob"):
    community.store.execute(
        "INSERT INTO relationships(id,user_id,peer_origin,peer_user_id,peer_name,direction,"
        "status,created_at,cache) VALUES(?,?,?,?,?,'outgoing','accepted',0,?)",
        (relation_id, owner, "https://private-peer.test", peer_id, "已绑定远端朋友", "old-cache"),
    )
    return relation_id


@pytest.mark.asyncio
async def test_cross_server_blocks_use_bound_relationship_not_colliding_remote_user_id(community):
    relation_id = remote_relation(community)
    actor_id = community.service.remote_actor(relation_id)
    alice, bob = community.clients["alice"], community.clients["bob"]
    before = len(community.store.all("SELECT * FROM users"))
    remote = (await alice.get("/api/community/profiles/bob?relationship_id=" + relation_id)).json()
    assert remote["profile"]["id"] == actor_id
    assert remote["profile"]["nickname"] == "已绑定远端朋友"
    assert remote["profile"]["is_remote"] is True and remote["public_profile"] is None
    blocked = await alice.post("/api/community/blocks", json={"relationship_id": relation_id})
    assert blocked.json() == {"ok": True, "user_id": actor_id}
    assert community.service.can_interact("alice", actor_id) is False
    assert community.service.can_interact("alice", "bob") is True
    assert (
        await bob.post("/api/community/blocks", json={"relationship_id": relation_id})
    ).status_code == 404
    assert (
        await alice.get("/api/community/profiles/carol?relationship_id=" + relation_id)
    ).status_code == 404
    blocklist = (await alice.get("/api/community/blocks")).json()
    assert blocklist["blocks"][0]["user"]["id"] == actor_id
    assert "private-peer.test" not in json.dumps(blocklist)
    assert (
        community.store.one("SELECT cache FROM relationships WHERE id=?", (relation_id,))["cache"]
        is None
    )
    community.store.execute("UPDATE relationships SET status='revoked' WHERE id=?", (relation_id,))
    assert (
        await alice.get("/api/community/profiles/bob?relationship_id=" + relation_id)
    ).status_code == 404
    assert (await alice.delete("/api/community/blocks/" + actor_id)).status_code == 200
    assert len(community.store.all("SELECT * FROM users")) == before


@pytest.mark.asyncio
async def test_reports_verify_real_targets_and_administrator_role_cannot_be_forged(community):
    created = await post(community)
    alice, bob = community.clients["alice"], community.clients["bob"]
    report_id = await report(community, created["id"])
    assert await report(community, created["id"]) == report_id
    for target_id, expected in ((created["id"], 400), (99999, 404)):
        assert (
            await alice.post(
                "/api/community/reports",
                json={"target_type": "post", "target_id": target_id, "reason": "理由"},
            )
        ).status_code == expected
    assert (
        await bob.post(
            "/api/community/reports",
            json={
                "target_type": "post",
                "target_id": created["id"],
                "reason": "远端同号不能举报本服内容",
                "relationship_id": "fake-remote",
            },
        )
    ).status_code == 422
    assert (
        await bob.get("/api/community/admin/reports", headers={"x-role": "admin"})
    ).status_code == 403
    assert (
        await bob.post(
            f"/api/community/admin/reports/{report_id}/resolve",
            json={"action": "hide", "role": "admin", "moderator_id": "admin"},
        )
    ).status_code == 403
    assert community.service.visible("post", created["id"], "bob") is True


@pytest.mark.asyncio
async def test_moderation_hides_and_restores_without_deleting_evidence_or_audit(community):
    created = await post(community, body="需要管理员查看的原始推荐")
    report_id = await report(community, created["id"])
    admin, bob = community.clients["admin"], community.clients["bob"]
    pending = (await admin.get("/api/community/admin/reports")).json()["reports"][0]
    assert pending["target"]["body"] == created["body"]
    assert pending["target"]["media"]["tmdb_id"] == 157336
    assert pending["target"]["author"]["id"] == "alice"
    assert pending["reporter"]["id"] == "bob" and pending["audit"] == []
    path = f"/api/community/admin/reports/{report_id}/resolve"
    hidden = (await admin.post(path, json={"action": "hide", "note": "已核对上下文"})).json()[
        "report"
    ]
    assert hidden["hidden"] is True and hidden["status"] == "resolved"
    assert hidden["audit"][0]["moderator_id"] == "admin"
    assert (await bob.get("/api/feed")).json()["posts"] == []
    assert (
        await bob.put(f"/api/feed/{created['id']}/like", json={"liked": True})
    ).status_code == 404
    assert (
        community.store.one("SELECT body FROM social_posts WHERE id=?", (created["id"],))["body"]
        == created["body"]
    )
    restored = (await admin.post(path, json={"action": "unhide"})).json()["report"]
    assert restored["hidden"] is False and len(restored["audit"]) == 2
    assert (await bob.get("/api/feed")).json()["posts"][0]["id"] == created["id"]
    ignored = (await admin.post(path, json={"action": "dismiss", "note": "复核完成"})).json()[
        "report"
    ]
    assert [row["action"] for row in ignored["audit"]] == ["hide", "unhide", "dismiss"]
    assert (await admin.get("/api/community/admin/reports")).json()["reports"] == []
    assert (
        len((await admin.get("/api/community/admin/reports?status=resolved")).json()["reports"])
        == 1
    )


@pytest.mark.asyncio
async def test_comment_moderation_does_not_hide_its_parent_post(community):
    created = await post(community)
    comment = (
        await community.clients["bob"].post(
            f"/api/feed/{created['id']}/comments", json={"body": "待审核评论"}
        )
    ).json()
    report_id = await report(community, comment["id"], kind="comment", user="carol")
    path = f"/api/community/admin/reports/{report_id}/resolve"
    assert (await community.clients["admin"].post(path, json={"action": "hide"})).status_code == 200
    visible = (await community.clients["alice"].get("/api/feed")).json()["posts"]
    assert len(visible) == 1 and visible[0]["id"] == created["id"]
    assert visible[0]["comments"] == []
    assert (
        await community.clients["admin"].post(path, json={"action": "unhide"})
    ).status_code == 200
    assert community.service.visible("comment", comment["id"], "alice") is True


@pytest.mark.asyncio
async def test_deleted_report_target_still_has_evidence_and_can_be_dismissed(community):
    created = await post(community, body="被作者删除前的原始内容")
    report_id = await report(community, created["id"])
    assert (
        await community.clients["alice"].delete(f"/api/feed/{created['id']}")
    ).status_code == 200
    admin = community.clients["admin"]
    preserved = (await admin.get("/api/community/admin/reports")).json()["reports"][0]
    assert preserved["target_exists"] is False
    assert preserved["target"]["body"] == "被作者删除前的原始内容"
    path = f"/api/community/admin/reports/{report_id}/resolve"
    assert (await admin.post(path, json={"action": "hide"})).status_code == 404
    dismissed = (await admin.post(path, json={"action": "dismiss"})).json()["report"]
    assert dismissed["status"] == "resolved" and len(dismissed["audit"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("user,status", [("anonymous", 401), ("demo", 403)])
async def test_community_routes_reject_missing_or_demo_identity(community, user, status):
    client = community.clients[user]
    for method, path, body in (
        ("GET", "/profile", None),
        ("PATCH", "/profile", {"bio": "不应保存"}),
        ("GET", "/notifications", None),
        ("POST", "/notifications/1/read", None),
        ("POST", "/blocks", {"user_id": "alice"}),
        ("POST", "/reports", {"target_type": "post", "target_id": 1, "reason": "举报"}),
        ("GET", "/admin/reports", None),
    ):
        assert (
            await client.request(method, "/api/community" + path, json=body)
        ).status_code == status


@pytest.mark.asyncio
async def test_schema_reinitialization_preserves_profiles_notifications_and_real_accounts(
    community,
):
    await community.clients["alice"].patch("/api/community/profile", json={"bio": "保留的简介"})
    notification_id = community.service.notify("alice", "bob", "friend_request", "relation-1")
    before = community.store.all("SELECT * FROM users ORDER BY id")
    restarted = Community(community.store)
    assert restarted.public_profile("alice", "alice")["bio"] == "保留的简介"
    assert restarted.notify("alice", "bob", "friend_request", "relation-1") == notification_id
    assert restarted.unread_count("alice") == 1
    assert community.store.all("SELECT * FROM users ORDER BY id") == before


@pytest.mark.asyncio
async def test_author_recommendations_visible_without_publishing_private_card(community):
    alice = await post(community, body="独立发布的公开推荐")
    await post(community, author="bob", body="另一个作者的内容")
    bob = community.clients["bob"]
    result = (await bob.get("/api/feed?author_id=alice")).json()
    assert [row["id"] for row in result["posts"]] == [alice["id"]]
    assert result["next_before"] is None
    assert (await bob.get("/api/community/profiles/alice")).json()["public_profile"] is None
    assert (
        await community.clients["anonymous"].get("/api/feed?author_id=alice")
    ).status_code == 401
    assert (await bob.get("/api/feed?author_id=missing")).json()["posts"] == []
    await community.clients["alice"].delete(f"/api/feed/{alice['id']}")
    assert (await bob.get("/api/feed?author_id=alice")).json()["posts"] == []


@pytest.mark.asyncio
async def test_author_recommendations_paginate_after_visibility_and_never_bypass_blocks(community):
    first = await post(community)
    second = await post(community)
    # 超过一页的审核隐藏内容，不得使个人主页显示假空页，也不泄露隐藏计数。
    for _ in range(102):
        community.store.execute(
            "INSERT INTO social_posts(user_id,media,body,created_at) "
            "VALUES('alice',?,'隐藏','2026-10-21')",
            (json.dumps(first["media"]),),
        )
        number = community.store.one("SELECT last_insert_rowid() AS id")["id"]
        community.store.execute(
            "INSERT INTO community_hidden VALUES('post',?,1,'admin','','2026-10-21')", (number,)
        )
    bob = community.clients["bob"]
    first_page = (await bob.get("/api/feed?author_id=alice&limit=1")).json()
    assert [r["id"] for r in first_page["posts"]] == [second["id"]]
    next_page = (
        await bob.get(f"/api/feed?author_id=alice&limit=1&before={first_page['next_before']}")
    ).json()
    assert [r["id"] for r in next_page["posts"]] == [first["id"]]
    assert next_page["next_before"] is None
    # 媒体库排除仍由同一可见性守卫执行。
    community.store.display_policy = SimpleNamespace(guard=lambda _: lambda media: False)
    assert (await bob.get("/api/feed?author_id=alice")).json()["posts"] == []
    del community.store.display_policy
    await bob.post("/api/community/blocks", json={"user_id": "alice"})
    assert (await bob.get("/api/feed?author_id=alice")).json()["posts"] == []
    assert (await community.clients["alice"].get("/api/feed?author_id=bob")).json()["posts"] == []
