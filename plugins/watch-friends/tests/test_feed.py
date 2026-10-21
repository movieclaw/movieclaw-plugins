"""真实 SQLite/HTTP 验证推荐公开同意、互动归属、分页与删除。"""

import asyncio
import json
from contextlib import AsyncExitStack
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI, HTTPException, Request
from movieclaw_friends.app import Collection, Media, create_app
from movieclaw_friends.config import Settings
from movieclaw_friends.feed import register_feed
from movieclaw_friends.lists import register_lists
from movieclaw_friends.store import Store


@pytest_asyncio.fixture
async def social(tmp_path):
    store = Store(tmp_path)
    for name in ("alice", "bob", "demo"):
        store.execute(
            "INSERT INTO users(id,username,nickname,role,token,invite_token) VALUES(?,?,?,?,?,?)",
            (name, name, name, "demo" if name == "demo" else "member", "unused", store.secret()),
        )
    app = FastAPI()

    def identity(request: Request):
        user = store.one("SELECT * FROM users WHERE id=?", (request.headers.get("x-test-user"),))
        if user is None:
            raise HTTPException(401, "测试会话未登录")
        return user

    register_feed(app, store, identity, Media)
    register_lists(app, store, identity, Media, Collection)
    async with AsyncExitStack() as stack:
        clients = {}
        for name in ("alice", "bob", "demo", "anonymous"):
            clients[name] = await stack.enter_async_context(
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="http://social.test",
                    headers={"x-test-user": name},
                )
            )
        yield SimpleNamespace(store=store, clients=clients)
    store.close()


def film(**changes):
    return {"tmdb_id": 157336, "kind": "movie", "title": "星际穿越", "year": 2014} | changes


async def publish(social, username="alice", **changes):
    response = await social.clients[username].post(
        "/api/feed",
        json={
            "media": film(),
            "body": "想和朋友一起重看的电影。",
            "publish_consent": True,
        }
        | changes,
    )
    assert response.status_code == 201, response.text
    return response.json()


@pytest.mark.asyncio
async def test_publication_requires_consent_and_only_exposes_selected_metadata(social):
    alice = social.clients["alice"]
    denied = await alice.post("/api/feed", json={"media": film(), "body": "推荐"})
    assert denied.status_code == 400
    before = social.store.one("SELECT privacy_revision,discoverable FROM users WHERE id='alice'")
    post = await publish(
        social,
        author_id="bob",
        media=film(
            media_item_id=42,
            demo_id="interstellar",
            token="secret",
            library_id=2,
            poster_path="https://private.test/image.jpg",
            file_path="/private/movie.mkv",
        ),
    )
    assert post["author"] == {"id": "alice", "nickname": "alice", "is_self": True}
    assert "media_item_id" not in post["media"] and "demo_id" not in post["media"]
    assert post["media"]["poster_path"] is None
    visible = (await social.clients["bob"].get("/api/feed")).json()["posts"][0]
    assert visible["author"]["is_self"] is False
    assert all(secret not in str(visible) for secret in ("secret", "library_id", "/private"))
    assert (
        social.store.one("SELECT privacy_revision,discoverable FROM users WHERE id='alice'")
        == before
    )
    for username in ("anonymous", "demo"):
        assert (await social.clients[username].get("/api/feed")).status_code in (401, 403)


@pytest.mark.asyncio
async def test_likes_are_idempotent_comments_owned_and_post_delete_cascades(social):
    post = await publish(social)
    alice, bob = social.clients["alice"], social.clients["bob"]
    path = f"/api/feed/{post['id']}"
    for _ in range(2):
        assert (await bob.put(path + "/like", json={"liked": True})).json() == {
            "liked": True,
            "like_count": 1,
        }
    assert (await alice.put(path + "/like", json={"liked": True})).json()["like_count"] == 2
    comment = (
        await bob.post(
            path + "/comments",
            json={
                "body": "<script>这段文字只作为内容展示</script>",
                "user_id": "alice",
            },
        )
    ).json()
    assert comment["author"]["id"] == "bob"
    assert (await alice.delete(f"/api/feed/comments/{comment['id']}")).status_code == 404
    assert (await bob.delete(path)).status_code == 404
    assert (await bob.put(path + "/like", json={"liked": False})).json()["like_count"] == 1
    assert (await alice.delete(path)).status_code == 200
    assert social.store.all("SELECT * FROM social_likes") == []
    assert social.store.all("SELECT * FROM social_comments") == []
    assert (await bob.post(path + "/comments", json={"body": "已删除"})).status_code == 404


@pytest.mark.asyncio
async def test_feed_and_comments_page_without_duplicates(social):
    posts = [await publish(social, body=f"推荐{index}") for index in range(5)]
    alice = social.clients["alice"]
    first = (await alice.get("/api/feed?limit=3")).json()
    second = (await alice.get(f"/api/feed?limit=3&before={first['next_before']}")).json()
    assert [row["id"] for row in first["posts"] + second["posts"]] == [
        row["id"] for row in reversed(posts)
    ]
    path = f"/api/feed/{posts[0]['id']}/comments"
    for index in range(6):
        assert (await alice.post(path, json={"body": f"评论{index}"})).status_code == 201
    a = (await alice.get(path + "?limit=4")).json()
    b = (await alice.get(path + f"?limit=4&before={a['next_before']}")).json()
    assert len({row["id"] for row in a["comments"] + b["comments"]}) == 6
    preview = (await alice.get("/api/feed")).json()["posts"][-1]
    assert preview["comment_count"] == 6 and len(preview["comments"]) == 3
    assert (await alice.get("/api/feed?limit=999")).status_code == 422


@pytest.mark.asyncio
async def test_rejects_missing_identity_blank_and_oversized_content(social):
    alice = social.clients["alice"]
    for media in (film(tmdb_id=-1), film(tmdb_id=None), film(kind="episode")):
        assert (
            await alice.post(
                "/api/feed",
                json={
                    "media": media,
                    "body": "推荐",
                    "publish_consent": True,
                },
            )
        ).status_code == 400
    for body in ("  ", "多" * 2001):
        assert (
            await alice.post(
                "/api/feed",
                json={
                    "media": film(),
                    "body": body,
                    "publish_consent": True,
                },
            )
        ).status_code == 422
    post = await publish(social)
    assert (
        await alice.post(f"/api/feed/{post['id']}/comments", json={"body": "空" * 501})
    ).status_code == 422
    assert (
        await alice.put(f"/api/feed/{post['id']}/like", json={"liked": "true"})
    ).status_code == 422


@pytest.mark.asyncio
async def test_rating_only_and_rating_with_reason_persist_for_other_members(social):
    for rating, reason in ((1, ""), (5, "  "), (4, "  很喜欢镜头与配乐  ")):
        created = await publish(social, rating=rating, body=reason)
        loaded = (
            await social.clients["bob"].get(f"/api/feed/{created['id']}")
        ).json()
        assert created["rating"] == loaded["rating"] == rating
        assert created["body"] == loaded["body"] == reason.strip()
    legacy = await publish(social)
    assert legacy["rating"] is None
    # 未填写正文也能评分，但公开同意与已有媒体库排除边界仍然生效。
    assert (
        await social.clients["alice"].post(
            "/api/feed", json={"media": film(), "rating": 5}
        )
    ).status_code == 400
    response = await social.clients["alice"].post(
        "/api/feed", json={"media": film(), "rating": 3, "publish_consent": True}
    )
    assert response.status_code == 201 and response.json()["body"] == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("rating", [0, 6, -1, 2.5, True, "4"])
async def test_rating_rejects_invalid_values_even_when_reason_is_present(social, rating):
    response = await social.clients["alice"].post(
        "/api/feed",
        json={"media": film(), "body": "推荐理由", "rating": rating, "publish_consent": True},
    )
    assert response.status_code == 422
    assert social.store.all("SELECT * FROM social_posts") == []


@pytest.mark.asyncio
async def test_existing_recommendations_and_interactions_survive_rating_migration(social):
    created = await publish(social)
    path = f"/api/feed/{created['id']}"
    await social.clients["bob"].put(path + "/like", json={"liked": True})
    await social.clients["bob"].post(path + "/comments", json={"body": "保留的评论"})
    # 还原没有评分列的旧版数据库，再以新版初始化；不重建推荐及其互动表。
    social.store.execute("ALTER TABLE social_posts DROP COLUMN rating")
    register_feed(FastAPI(), social.store, lambda _: None, Media)
    loaded = (await social.clients["bob"].get(path)).json()
    assert loaded["id"] == created["id"] and loaded["body"] == created["body"]
    assert loaded["rating"] is None and loaded["like_count"] == 1
    assert loaded["comments"][0]["body"] == "保留的评论"


@pytest.mark.asyncio
async def test_recommended_movie_appends_to_own_list_without_republishing(social):
    alice, bob = social.clients["alice"], social.clients["bob"]
    created = (await alice.post("/api/lists", json={"name": "想看的电影"})).json()
    path = f"/api/lists/{created['id']}"
    assert (await alice.post(path + "/share")).status_code == 200
    for _ in range(2):
        response = await alice.post(path + "/items", json={"media": film()})
        assert response.status_code == 200 and len(response.json()["items"]) == 1
    assert (await bob.post(path + "/items", json={"media": film()})).status_code == 404
    public = social.store.one("SELECT data FROM collections WHERE user_id='alice'")
    assert '"items": []' in public["data"]
    assert (await alice.get("/api/lists")).json()["lists"][0]["shared"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("username,status", [("anonymous", 401), ("demo", 403)])
async def test_every_social_write_rejects_missing_or_demo_identity(social, username, status):
    post = await publish(social)
    comment = (
        await social.clients["alice"].post(
            f"/api/feed/{post['id']}/comments", json={"body": "原评论"}
        )
    ).json()
    before = {
        table: social.store.all(f"SELECT * FROM {table}")
        for table in ("social_posts", "social_likes", "social_comments")
    }
    for method, path, body in (
        ("POST", "/api/feed", {"media": film(), "body": "不能发布", "publish_consent": True}),
        ("DELETE", f"/api/feed/{post['id']}", None),
        ("PUT", f"/api/feed/{post['id']}/like", {"liked": True}),
        ("POST", f"/api/feed/{post['id']}/comments", {"body": "不能评论"}),
        ("DELETE", f"/api/feed/comments/{comment['id']}", None),
    ):
        response = await social.clients[username].request(method, path, json=body)
        assert response.status_code == status
    for table, rows in before.items():
        assert social.store.all(f"SELECT * FROM {table}") == rows


@pytest.mark.asyncio
async def test_comment_author_can_delete_own_comment_and_likes_remain(social):
    post = await publish(social)
    bob = social.clients["bob"]
    path = f"/api/feed/{post['id']}"
    await bob.put(path + "/like", json={"liked": True})
    comment = (await bob.post(path + "/comments", json={"body": "后来想撤回"})).json()
    response = await bob.delete(f"/api/feed/comments/{comment['id']}")
    assert response.status_code == 200
    row = (await bob.get("/api/feed")).json()["posts"][0]
    assert row["comment_count"] == 0 and row["comments"] == []
    assert row["like_count"] == 1 and row["liked"] is True
    assert social.store.one("SELECT id FROM social_posts WHERE id=?", (post["id"],))
    assert (await bob.get(path + "/comments")).json() == {"comments": [], "next_before": None}


@pytest.mark.asyncio
async def test_cursor_pages_ignore_newer_insertions_and_enforce_bounds(social):
    posts = [await publish(social) for _ in range(4)]
    alice = social.clients["alice"]
    first = (await alice.get("/api/feed?limit=2")).json()
    await publish(social)
    second = (await alice.get(f"/api/feed?limit=2&before={first['next_before']}")).json()
    assert [row["id"] for row in first["posts"] + second["posts"]] == [
        row["id"] for row in reversed(posts)
    ]
    assert second["next_before"] is None
    path = f"/api/feed/{posts[0]['id']}/comments"
    comments = [(await alice.post(path, json={"body": str(i)})).json() for i in range(4)]
    a = (await alice.get(path + "?limit=2")).json()
    await alice.post(path, json={"body": "后来新增"})
    b = (await alice.get(path + f"?limit=2&before={a['next_before']}")).json()
    assert [row["id"] for row in a["comments"] + b["comments"]] == [
        row["id"] for row in reversed(comments)
    ]
    assert b["next_before"] is None
    for suffix in ("?limit=31", "?limit=0", "?before=0"):
        assert (await alice.get(path + suffix)).status_code == 422


@pytest.mark.asyncio
async def test_append_at_capacity_is_idempotent_and_parallel_changes_do_not_get_lost(social):
    alice = social.clients["alice"]
    initial = [film(tmdb_id=i) for i in range(1, 501)]
    created = (await alice.post("/api/lists", json={"name": "容量边界", "items": initial})).json()
    path = f"/api/lists/{created['id']}/items"
    same = await alice.post(path, json={"media": film(tmdb_id=1)})
    assert same.status_code == 200 and len(same.json()["items"]) == 500
    assert (await alice.post(path, json={"media": film(tmdb_id=501)})).status_code == 400
    empty = (await alice.post("/api/lists", json={"name": "同时添加"})).json()
    path = f"/api/lists/{empty['id']}/items"
    responses = await asyncio.gather(
        *(alice.post(path, json={"media": film(tmdb_id=identity)}) for identity in (1, 2, 1))
    )
    assert all(response.status_code == 200 for response in responses)
    stored = social.store.one("SELECT data FROM plugin_lists WHERE id=?", (empty["id"],))
    assert {row["tmdb_id"] for row in json.loads(stored["data"])} == {1, 2}
    assert len(json.loads(stored["data"])) == 2


@pytest.mark.asyncio
async def test_demo_recommendations_reach_api_without_writing_real_feed(tmp_path):
    # native 状态由测试宿主注入；任何演示读取都无需宿主数据源。
    business = create_app(
        Settings("", "http://social.test", tmp_path, native=True), upstream=object()
    )
    store = business.state.store
    store.execute(
        "INSERT INTO users(id,username,nickname,role,token,invite_token) VALUES(?,?,?,?,?,?)",
        ("alice", "alice", "Alice", "member", "unused", store.secret()),
    )

    @business.middleware("http")
    async def authenticated_test_host(request, call_next):
        request.state.friends_user = store.one("SELECT * FROM users WHERE id='alice'")
        return await call_next(request)

    async with (
        business.router.lifespan_context(business),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=business), base_url="http://social.test"
        ) as client,
    ):
        result = (await client.get("/api/demo")).json()
        assert result["enabled"] is True and len(result["users"]) == 4
        for entry in result["users"]:
            recommendations = entry["data"]["recommendations"]
            assert 1 <= len(recommendations) <= 8
            assert all(row["media"]["tmdb_id"] > 0 for row in recommendations)
            assert all(
                row["id"].startswith(entry["user"]["id"] + "-recommendation-")
                for row in recommendations
            )
        assert (await client.get("/api/feed")).json() == {"posts": [], "next_before": None}
        for table in ("social_posts", "social_likes", "social_comments"):
            assert store.all(f"SELECT * FROM {table}") == []


@pytest.mark.asyncio
async def test_selected_cover_roundtrip_and_safe_federated_projection(social):
    from movieclaw_friends.media import sanitize_media
    from movieclaw_friends.network import Network

    post = await publish(social, media=film(cover_path="/chosen-cover.jpg", cover_type="backdrop"))
    assert post["media"]["cover_path"] == "/chosen-cover.jpg"
    assert post["media"]["cover_type"] == "backdrop"
    viewed = (await social.clients["bob"].get(f"/api/feed/{post['id']}")).json()
    assert viewed["media"] == post["media"]
    assert Network.clean_media(post["media"])["cover_path"] == "/chosen-cover.jpg"
    for unsafe in ("https://private.test/a.jpg?token=secret", "/../private.jpg", "/x.svg"):
        post = await publish(social, media=film(cover_path=unsafe, cover_type="poster"))
        assert "cover_path" not in post["media"]
        assert "cover_path" not in sanitize_media(film(cover_path=unsafe, cover_type="poster"))
    denied = await social.clients["alice"].post("/api/feed", json={
        "media": film(cover_path="/cover.jpg", cover_type="script"),
        "body": "推荐", "publish_consent": True,
    })
    assert denied.status_code == 422
