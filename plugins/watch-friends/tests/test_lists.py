"""以真实 SQLite 和 HTTP 路由验证插件私有片单的归属、容量、快照与撤回。"""

import asyncio
import json
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI, HTTPException, Request
from movieclaw_friends.app import Collection, Media
from movieclaw_friends.lists import register_lists
from movieclaw_friends.native import build_routers
from movieclaw_friends.store import Store


@dataclass
class ListsHarness:
    store: Store
    clients: dict
    app: FastAPI
    stale_user: dict | None = None


@pytest_asyncio.fixture
async def lists(tmp_path):
    store = Store(tmp_path)
    for user_id in ("alice", "bob", "charlie"):
        store.execute(
            "INSERT INTO users(id,username,nickname,member_id,role,token,invite_token) "
            "VALUES(?,?,?,?,?,?,?)",
            (user_id, user_id, user_id, 1, "member", "unused", store.secret()),
        )
    app = FastAPI()
    app.state.store = store
    harness = ListsHarness(store, {}, app)

    @app.middleware("http")
    async def trusted_test_identity(request: Request, call_next):
        # 仅用于测试宿主身份注入；生产身份来自 require_login，不接受此测试请求头。
        request.state.friends_user = store.one(
            "SELECT * FROM users WHERE id=?", (request.headers.get("x-test-session", ""),)
        )
        return await call_next(request)

    def user_for(request: Request):
        user = harness.stale_user or request.state.friends_user
        if user is None:
            raise HTTPException(401, "请先登录 MovieClaw")
        return user

    register_lists(app, store, user_for, Media, Collection)
    async with AsyncExitStack() as stack:
        for username in ("alice", "bob", "charlie", "anonymous"):
            harness.clients[username] = await stack.enter_async_context(
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="http://friends.test",
                    headers={"x-test-session": username},
                )
            )
        yield harness
    store.close()


def media(**changes):
    return {"tmdb_id": 550, "kind": "movie", "title": "搏击俱乐部", "year": 1999} | changes


async def create(lists, user="alice", **changes):
    response = await lists.clients[user].post(
        "/api/lists",
        json={"name": "我的片单", "description": "周末再看", "items": [media()]} | changes,
    )
    assert response.status_code == 201, response.text
    return response.json()


@pytest.mark.asyncio
async def test_full_edit_reorders_removes_and_keeps_published_snapshot(lists):
    client = lists.clients["alice"]
    item = await create(lists, items=[media(), media(tmdb_id=129), media(tmdb_id=27205)])
    assert item["revision"] == 0
    await client.post(f"/api/lists/{item['id']}/share")
    before = saved(lists, "alice", -item["id"])
    response = await client.put(
        f"/api/lists/{item['id']}",
        json={
            "name": "  新名字  ",
            "description": "新的介绍",
            "revision": 0,
            "items": [media(tmdb_id=27205, file_path="/private/movie.mkv"), media()],
        },
    )
    assert response.status_code == 200
    edited = response.json()
    assert edited["name"] == "新名字" and edited["description"] == "新的介绍"
    assert edited["revision"] == 1 and edited["shared"] is True
    assert [film["tmdb_id"] for film in edited["items"]] == [27205, 550]
    assert "/private" not in response.text
    assert saved(lists, "alice", -item["id"]) == before
    # 更新分享是单独确认的动作，此后好友快照才包含新名称与选片。
    await client.post(f"/api/lists/{item['id']}/share")
    assert json.loads(saved(lists, "alice", -item["id"])["data"])["name"] == "新名字"


@pytest.mark.asyncio
async def test_edit_rejects_other_account_and_stale_version(lists):
    item = await create(lists)
    path = f"/api/lists/{item['id']}"
    payload = {"name": "新的片单", "description": "", "items": [], "revision": 0}
    assert (await lists.clients["bob"].put(path, json=payload)).status_code == 404
    assert (await lists.clients["alice"].put(path, json=payload)).status_code == 200
    assert (await lists.clients["alice"].put(path, json=payload)).status_code == 409
    assert (await lists.clients["alice"].put(path, json={"name": "遗漏版本"})).status_code == 422
    assert (await lists.clients["alice"].get("/api/lists")).json()["lists"][0]["revision"] == 1


@pytest.mark.asyncio
async def test_append_invalidates_open_editor_but_duplicate_does_not(lists):
    item = await create(lists)
    path = f"/api/lists/{item['id']}"
    first = (
        await lists.clients["alice"].post(path + "/items", json={"media": media(tmdb_id=129)})
    ).json()
    repeated = (
        await lists.clients["alice"].post(path + "/items", json={"media": media(tmdb_id=129)})
    ).json()
    assert first["revision"] == repeated["revision"] == 1
    assert (
        await lists.clients["alice"].put(
            path,
            json={
                "name": item["name"],
                "description": "",
                "items": item["items"],
                "revision": 0,
            },
        )
    ).status_code == 409
    assert len(repeated["items"]) == 2


def saved(lists, user, source_id):
    return lists.store.one(
        "SELECT * FROM collections WHERE user_id=? AND source_id=?", (user, source_id)
    )


def seed_shared(lists, count, user="alice"):
    for number in range(1, count + 1):
        snapshot = {"id": number, "name": "宿主片单", "items": []}
        lists.store.execute(
            "INSERT INTO collections VALUES(?,?,?,?)",
            (user, number, "宿主片单", json.dumps(snapshot)),
        )


def seed_relationships(lists):
    for relation_id, owner, target in (
        ("outgoing", "alice", "bob"),
        ("incoming", "bob", "alice"),
        ("unrelated", "bob", "charlie"),
    ):
        lists.store.execute(
            "INSERT INTO relationships(id,user_id,target_id,direction,status,cache,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (relation_id, owner, target, "outgoing", "accepted", '{"old":"snapshot"}', 1),
        )


@pytest.mark.asyncio
async def test_create_is_private_and_strips_unapproved_fields(lists):
    item = await create(
        lists,
        name="  周末精选  ",
        owner="bob",
        member_id=999,
        shared=True,
        items=[media(file_path="/secret/file.mkv", token="private-token", library_id=10)],
    )
    assert item["name"] == "周末精选"
    assert item["description"] == "周末再看"
    assert item["shared"] is False
    assert item["items"][0]["tmdb_id"] == 550
    assert set(item) == {"id", "name", "description", "items", "shared", "revision"}
    stored = lists.store.one("SELECT * FROM plugin_lists WHERE id=?", (item["id"],))
    assert stored["user_id"] == "alice"
    assert datetime.fromisoformat(stored["created_at"]).astimezone(UTC)
    for secret in ("file_path", "/secret", "private-token", "library_id"):
        assert secret not in stored["data"]
        assert secret not in json.dumps(item)
    assert not lists.store.all("SELECT * FROM collections")


@pytest.mark.asyncio
async def test_list_scopes_to_current_identity_and_ignores_owner_queries(lists):
    alice = await create(lists)
    bob = await create(lists, "bob", name="其他成员的片单")
    response = await lists.clients["alice"].get("/api/lists?owner=bob&member_id=999&user_id=bob")
    assert response.status_code == 200
    assert response.json() == {"lists": [alice]}
    assert (await lists.clients["bob"].get("/api/lists")).json() == {"lists": [bob]}


@pytest.mark.asyncio
async def test_shared_snapshot_uses_negative_id_without_overwriting_host_list(lists):
    item = await create(lists)
    seed_shared(lists, item["id"])
    response = await lists.clients["alice"].post(f"/api/lists/{item['id']}/share")
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    snapshot = json.loads(saved(lists, "alice", -item["id"])["data"])
    assert snapshot["id"] == -item["id"]
    assert snapshot["name"] == item["name"]
    assert snapshot["items"] == item["items"]
    assert saved(lists, "alice", item["id"])["name"] == "宿主片单"
    assert (await lists.clients["alice"].get("/api/lists")).json()["lists"][0]["shared"] is True
    assert not saved(lists, "bob", -item["id"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method,suffix", [("POST", "/share"), ("DELETE", "/share"), ("DELETE", "")]
)
async def test_other_members_cannot_share_revoke_or_delete_a_list(lists, method, suffix):
    item = await create(lists)
    await lists.clients["alice"].post(f"/api/lists/{item['id']}/share")
    before = lists.store.one("SELECT privacy_revision FROM users WHERE id='alice'")
    response = await lists.clients["bob"].request(
        method, f"/api/lists/{item['id']}{suffix}?owner=alice&member_id=1"
    )
    missing = await lists.clients["bob"].request(method, f"/api/lists/999999{suffix}")
    assert response.status_code == missing.status_code == 404
    assert response.json() == missing.json()
    assert saved(lists, "alice", -item["id"])
    assert lists.store.one("SELECT * FROM plugin_lists WHERE id=?", (item["id"],))
    assert lists.store.one("SELECT privacy_revision FROM users WHERE id='alice'") == before


@pytest.mark.asyncio
@pytest.mark.parametrize("delete_list", [False, True])
async def test_revocation_clears_both_relationship_caches_and_bumps_revision(lists, delete_list):
    item = await create(lists)
    client = lists.clients["alice"]
    await client.post(f"/api/lists/{item['id']}/share")
    seed_relationships(lists)
    before = lists.store.one("SELECT privacy_revision FROM users WHERE id='alice'")[
        "privacy_revision"
    ]
    response = await client.delete(f"/api/lists/{item['id']}" + ("" if delete_list else "/share"))
    assert response.status_code == 200
    assert not saved(lists, "alice", -item["id"])
    assert (
        lists.store.one("SELECT privacy_revision FROM users WHERE id='alice'")["privacy_revision"]
        == before + 1
    )
    relations = {
        row["id"]: row["cache"] for row in lists.store.all("SELECT id,cache FROM relationships")
    }
    assert relations["outgoing"] is relations["incoming"] is None
    assert relations["unrelated"]
    rows = (await client.get("/api/lists")).json()["lists"]
    if delete_list:
        assert rows == []
    else:
        assert rows[0]["shared"] is False
        # 重复撤回也应安全，且保留个人私有片单内容。
        assert (await client.delete(f"/api/lists/{item['id']}/share")).status_code == 200
        assert rows[0]["items"] == item["items"]


@pytest.mark.asyncio
async def test_deleted_id_is_not_reused(lists):
    first = await create(lists)
    assert (await lists.clients["alice"].delete(f"/api/lists/{first['id']}")).status_code == 200
    second = await create(lists)
    assert second["id"] > first["id"]


@pytest.mark.asyncio
async def test_fifty_private_lists_limit_is_per_user_and_concurrent(lists):
    for _ in range(49):
        await create(lists)
    responses = await asyncio.gather(
        *(lists.clients["alice"].post("/api/lists", json={"name": "最后一席"}) for _ in range(2))
    )
    assert sorted(response.status_code for response in responses) == [201, 400]
    assert len((await lists.clients["alice"].get("/api/lists")).json()["lists"]) == 50
    assert (await create(lists, "bob"))["shared"] is False


@pytest.mark.asyncio
async def test_share_limit_counts_host_snapshots_but_allows_existing_share(lists):
    first = await create(lists)
    second = await create(lists)
    seed_shared(lists, 49)
    seed_shared(lists, 50, "bob")
    client = lists.clients["alice"]
    assert (await client.post(f"/api/lists/{first['id']}/share")).status_code == 200
    assert (await client.post(f"/api/lists/{first['id']}/share")).status_code == 200
    assert (await client.post(f"/api/lists/{second['id']}/share")).status_code == 400
    assert not saved(lists, "alice", -second["id"])
    assert (await client.delete(f"/api/lists/{first['id']}/share")).status_code == 200
    assert (await client.post(f"/api/lists/{second['id']}/share")).status_code == 200


@pytest.mark.asyncio
async def test_stale_privacy_revision_cannot_publish(lists):
    item = await create(lists)
    lists.stale_user = lists.store.one("SELECT * FROM users WHERE id='alice'")
    lists.store.execute("UPDATE users SET privacy_revision=privacy_revision+1 WHERE id='alice'")
    response = await lists.clients["alice"].post(f"/api/lists/{item['id']}/share")
    assert response.status_code == 409
    assert not saved(lists, "alice", -item["id"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"name": ""},
        {"name": " \n "},
        {"name": "字" * 121},
        {"name": "名称", "description": "字" * 501},
        {"name": "名称", "items": [media()] * 501},
        {"name": "名称", "items": [{"title": "字" * 301}]},
    ],
)
async def test_invalid_create_cannot_write_a_list(lists, payload):
    response = await lists.clients["alice"].post("/api/lists", json=payload)
    assert response.status_code == 422
    assert not lists.store.all("SELECT * FROM plugin_lists")


@pytest.mark.asyncio
async def test_exact_capacity_and_empty_list_are_supported(lists):
    empty = await create(lists, items=[])
    assert empty["items"] == []
    full = await create(lists, name="字" * 120, description="字" * 500, items=[media()] * 500)
    assert len(full["items"]) == 500
    assert (await lists.clients["alice"].post(f"/api/lists/{full['id']}/share")).status_code == 200
    assert len(json.loads(saved(lists, "alice", -full["id"])["data"])["items"]) == 500


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method,path,payload",
    [
        ("GET", "/api/lists", None),
        ("POST", "/api/lists", {"name": "未登录"}),
        ("POST", "/api/lists/1/share", None),
        ("DELETE", "/api/lists/1/share", None),
        ("DELETE", "/api/lists/1", None),
    ],
)
async def test_all_list_operations_require_authenticated_identity(lists, method, path, payload):
    response = await lists.clients["anonymous"].request(method, path, json=payload)
    assert response.status_code == 401
    assert not lists.store.all("SELECT * FROM plugin_lists")


@pytest.mark.asyncio
async def test_native_route_registration_preserves_identity_creation_and_csrf(lists):
    async def require_login():
        return SimpleNamespace(
            kind="admin", name="管理员", member=None, member_id=None, plugin=None
        )

    member, public = build_routers(lists.app, require_login)
    assert not public.routes
    host = FastAPI()
    host.include_router(member, prefix="/api/v1/plugins/watch-friends")
    base = "/api/v1/plugins/watch-friends/lists"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=host), base_url="http://host.test"
    ) as client:
        assert (await client.post(base, json={"name": "未经页面提交"})).status_code == 403
        response = await client.post(
            base,
            json={"name": "原生片单", "user_id": "bob", "items": [media()]},
            headers={"x-friends-request": "1", "origin": "http://host.test"},
        )
        assert response.status_code == 201
        assert response.json()["name"] == "原生片单"
        assert response.json()["shared"] is False
        assert (await client.get(base)).json() == {"lists": [response.json()]}
    owner = lists.store.one(
        "SELECT users.username FROM plugin_lists JOIN users ON users.id=plugin_lists.user_id"
    )
    assert owner["username"] == "admin:0"


async def test_private_cover_saved_shared_snapshot_independent_and_bad_cover_rejected(lists):
    alice = lists.clients["alice"]
    movie = {"tmdb_id": 157336, "kind": "movie", "title": "星际穿越"}
    cover = {**movie, "cover_path": "/selected-cover.jpg", "cover_type": "backdrop"}
    data = {"name": "新的片单封面", "items": [movie], "cover": cover}
    result = await alice.post("/api/lists", json=data)
    assert result.status_code == 201, result.text
    row = result.json()
    assert row["cover"]["cover_path"] == cover["cover_path"]
    assert (await lists.clients["bob"].get("/api/lists")).json()["lists"] == []
    assert (await alice.post(f"/api/lists/{row['id']}/share")).status_code == 200
    snapshot = json.loads(lists.store.one("SELECT data FROM collections")["data"])
    assert snapshot["cover"]["cover_path"] == "/selected-cover.jpg"
    edited = await alice.put(
        f"/api/lists/{row['id']}",
        json={
            **data,
            "cover": {**cover, "cover_path": "/new-cover.jpg"},
            "revision": row["revision"],
        },
    )
    assert edited.status_code == 200, edited.text
    assert (
        json.loads(lists.store.one("SELECT data FROM collections")["data"])["cover"]
        == snapshot["cover"]
    )
    invalid = await alice.post("/api/lists", json={**data, "cover": {**cover, "tmdb_id": 550}})
    assert invalid.status_code == 422
    unsafe = await alice.post(
        "/api/lists", json={**data, "cover": {**cover, "cover_path": "https://private.test/token"}}
    )
    assert unsafe.status_code == 422
