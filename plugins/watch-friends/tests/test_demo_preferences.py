"""每账号演示偏好的真实 HTTP/SQLite 回归，不修改宿主设置、业务数据或真实好友授权。"""

import asyncio
import base64
import json
import sqlite3
from contextlib import AsyncExitStack, asynccontextmanager

import httpx
import pytest
from movieclaw_friends.app import create_app
from movieclaw_friends.config import Settings
from movieclaw_friends.demo import DEMO_ID
from movieclaw_friends.store import Store

HEADERS = {"x-friends-request": "1", "origin": "http://a.test"}


class ReadOnlySource:
    """只接受两位真实成员，记录读请求；无任何宿主写接口可供偏好操作调用。"""

    def __init__(self):
        self.calls = []

    async def identity(self, token):
        name = token.removeprefix("token-")
        assert name in {"alice", "bob"}
        return {
            "username": name,
            "nickname": name,
            "role": "member",
            "member_id": {"alice": 1, "bob": 2}[name],
        }

    async def snapshot(self, token, member_id, share_current, share_history):
        self.calls.append((token, member_id, share_current, share_history))
        return {
            "current": None,
            "history": [{"media": {"title": "真实作品", "tmdb_id": 550, "kind": "movie"}}]
            if share_history
            else [],
            "sharing": {"current": share_current, "history": share_history},
            "observed_at": "2026-10-20T00:00:00Z",
        }


@asynccontextmanager
async def instance(data_dir, *, global_enabled=True, peer_transport=None):
    source = ReadOnlySource()
    app = create_app(
        Settings(
            "http://movieclaw",
            public_url="http://a.test",
            data_dir=data_dir,
            allow_private_peers=True,
            demo_enabled=global_enabled,
        ),
        upstream=source,
        peer_transport=peer_transport,
    )
    async with AsyncExitStack() as stack:
        await stack.enter_async_context(app.router.lifespan_context(app))
        clients, users = {}, {}
        for name in ("alice", "bob"):
            client = await stack.enter_async_context(
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="http://a.test",
                    headers=HEADERS,
                )
            )
            response = await client.post("/api/login", json={"token": f"token-{name}"})
            assert response.status_code == 200
            clients[name], users[name] = client, response.json()["user"]
        yield app, clients, users, source


def disabled_response(*, available=True, preference=False):
    return {
        "enabled": False,
        "available": available,
        "preference": preference,
        "user": None,
        "data": None,
        "users": [],
        "details": {},
    }


async def toggle(client, enabled, **extra):
    response = await client.patch("/api/demo/preferences", json={"enabled": enabled, **extra})
    assert response.status_code == 200, response.text
    return response.json()


async def add_demo(client):
    response = await client.post(
        "/api/requests", json={"user_id": DEMO_ID, "address_consent": True}
    )
    assert response.status_code == 200
    return response.json()["id"]


@pytest.mark.asyncio
async def test_default_preference_and_toggling_are_private_to_current_account(tmp_path):
    async with instance(tmp_path) as (app, clients, users, source):
        alice, bob = clients["alice"], clients["bob"]
        before = (await alice.get("/api/me")).json()
        demo = (await alice.get("/api/demo")).json()
        assert demo["enabled"] is demo["available"] is demo["preference"] is True
        assert demo["user"]["id"] == DEMO_ID and demo["users"] and demo["details"]
        assert (
            await toggle(alice, False, user_id=users["bob"]["id"], owner="bob")
            == disabled_response()
        )
        assert (await alice.get("/api/demo")).json() == disabled_response()
        assert (await alice.get("/api/directory")).json() == {"users": []}
        other = (await bob.get("/api/demo")).json()
        assert other["enabled"] is other["preference"] is True
        assert len((await bob.get("/api/directory")).json()["users"]) > 0
        assert (await alice.get("/api/me")).json() == before
        assert source.calls == []
        assert app.state.store.all("SELECT * FROM relationships") == []
        restored = await toggle(alice, True)
        assert restored["enabled"] is True and restored["users"]


@pytest.mark.asyncio
async def test_disabled_known_demo_entries_are_blocked_without_deleting_existing_relation(tmp_path):
    async with instance(tmp_path) as (app, clients, _, source):
        alice = clients["alice"]
        relation_id = await add_demo(alice)
        assert (await alice.get(f"/api/friends/{relation_id}")).status_code == 200
        old_relation = app.state.store.one("SELECT * FROM relationships WHERE id=?", (relation_id,))
        demo = app.state.store.one("SELECT * FROM users WHERE id=?", (DEMO_ID,))
        invite = "mcf1." + base64.urlsafe_b64encode(
            json.dumps({"origin": "http://a.test", "invite": demo["invite_token"]}).encode()
        ).decode().rstrip("=")
        await toggle(alice, False)
        assert (await alice.get("/api/friends")).json() == {"friends": []}
        for payload in ({"user_id": DEMO_ID}, {"invite_code": invite}):
            response = await alice.post("/api/requests", json=payload | {"address_consent": True})
            assert response.status_code == 404
        for method, path, payload in (
            ("GET", f"/api/friends/{relation_id}", None),
            ("DELETE", f"/api/friends/{relation_id}", None),
            ("POST", f"/api/requests/{relation_id}/accept", {"address_consent": True}),
            ("POST", f"/api/requests/{relation_id}/reject", None),
        ):
            response = await alice.request(method, path, json=payload)
            assert response.status_code == 404
            assert response.json() == {"detail": "好友关系不存在"}
        assert (
            app.state.store.one("SELECT * FROM relationships WHERE id=?", (relation_id,))
            == old_relation
        )
        await toggle(alice, True)
        assert (await alice.get("/api/friends")).json()["friends"][0]["id"] == relation_id
        assert (await alice.get(f"/api/friends/{relation_id}")).status_code == 200
        assert source.calls == []


@pytest.mark.asyncio
async def test_preference_survives_restart_and_global_gate_cannot_be_overridden(tmp_path):
    async with instance(tmp_path) as (_, clients, _, _):
        relation_id = await add_demo(clients["alice"])
        await toggle(clients["alice"], False)
    async with instance(tmp_path, global_enabled=False) as (app, clients, _, source):
        alice, bob = clients["alice"], clients["bob"]
        assert (await alice.get("/api/demo")).json() == disabled_response(available=False)
        assert (await bob.get("/api/demo")).json() == disabled_response(
            available=False, preference=True
        )
        assert await toggle(alice, True) == disabled_response(available=False, preference=True)
        assert (await alice.get("/api/friends")).json() == {"friends": []}
        assert (await alice.get(f"/api/friends/{relation_id}")).status_code == 404
        assert (await alice.get("/api/directory")).json() == {"users": []}
        assert (
            app.state.store.one("SELECT status FROM relationships WHERE id=?", (relation_id,))[
                "status"
            ]
            == "accepted"
        )
        assert source.calls == []
    async with instance(tmp_path) as (_, clients, _, _):
        assert (await clients["alice"].get("/api/demo")).json()["enabled"] is True
        assert (await clients["alice"].get("/api/friends")).json()["friends"][0][
            "id"
        ] == relation_id
        await toggle(clients["alice"], False)
    async with instance(tmp_path) as (_, clients, _, _):
        assert (await clients["alice"].get("/api/demo")).json() == disabled_response()
        assert (await clients["bob"].get("/api/demo")).json()["enabled"] is True


@pytest.mark.asyncio
async def test_real_friend_data_private_lists_and_sharing_are_unchanged(tmp_path):
    async with instance(tmp_path) as (app, clients, users, _):
        alice, bob = clients["alice"], clients["bob"]
        response = await alice.post(
            "/api/requests",
            json={"invite_code": users["bob"]["invite_code"], "address_consent": True},
        )
        real_id = response.json()["id"]
        assert (
            await bob.post(f"/api/requests/{real_id}/accept", json={"address_consent": True})
        ).status_code == 200
        assert (
            await bob.patch("/api/profile", json={"nickname": "Bob", "share_history": True})
        ).status_code == 200
        own_list = await alice.post(
            "/api/lists",
            json={
                "name": "真实片单",
                "items": [{"tmdb_id": 550, "title": "真实作品", "kind": "movie"}],
            },
        )
        assert own_list.status_code == 201
        await add_demo(alice)
        real_before = (await alice.get(f"/api/friends/{real_id}")).json()
        settings_before = app.state.store.all(
            "SELECT id,discoverable,share_current,share_history,privacy_revision "
            "FROM users WHERE role<>'demo' ORDER BY id"
        )
        assert real_before["data"]["history"][0]["media"]["title"] == "真实作品"
        await toggle(alice, False)
        assert (await alice.get(f"/api/friends/{real_id}")).json() == real_before
        friends = (await alice.get("/api/friends")).json()["friends"]
        assert [friend["id"] for friend in friends] == [real_id]
        assert (await alice.get("/api/lists")).json() == {"lists": [own_list.json()]}
        assert (
            app.state.store.all(
                "SELECT id,discoverable,share_current,share_history,privacy_revision "
                "FROM users WHERE role<>'demo' ORDER BY id"
            )
            == settings_before
        )


@pytest.mark.asyncio
async def test_pending_demo_requests_and_inflight_friend_lists_follow_latest_preference(tmp_path):
    started, release = asyncio.Event(), asyncio.Event()

    async def remote(request):
        started.set()
        await release.wait()
        return httpx.Response(200, json={"status": "accepted"})

    async with instance(tmp_path, peer_transport=httpx.MockTransport(remote)) as (
        app,
        clients,
        users,
        _,
    ):
        alice = clients["alice"]
        relation_id = await add_demo(alice)
        store = app.state.store
        store.execute("UPDATE relationships SET created_at=100 WHERE id=?", (relation_id,))
        store.execute(
            "INSERT INTO relationships(id,user_id,peer_origin,peer_name,direction,status,"
            "token,created_at) "
            "VALUES(?,?,?,?,'outgoing','accepted',?,1)",
            (
                "remote",
                users["alice"]["id"],
                "http://peer.test",
                "真实远端好友",
                store.encrypt("relationship-secret"),
            ),
        )
        task = asyncio.create_task(alice.get("/api/friends"))
        await asyncio.wait_for(started.wait(), timeout=2)
        await toggle(alice, False)
        release.set()
        response = await task
        assert response.status_code == 200
        assert [friend["id"] for friend in response.json()["friends"]] == ["remote"]
        store.execute("UPDATE relationships SET status='pending' WHERE id=?", (relation_id,))
        assert (await alice.get("/api/requests")).json() == {"incoming": [], "outgoing": []}


@pytest.mark.asyncio
async def test_preference_requires_login_same_origin_and_a_boolean(tmp_path):
    async with instance(tmp_path) as (app, clients, _, _):
        alice = clients["alice"]
        for payload in ({}, {"enabled": "false"}, {"enabled": 0}, {"enabled": None}):
            assert (await alice.patch("/api/demo/preferences", json=payload)).status_code == 422
        assert (
            await alice.patch(
                "/api/demo/preferences",
                json={"enabled": False},
                headers={"origin": "http://evil.test"},
            )
        ).status_code == 403
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://a.test"
        ) as anonymous:
            assert (
                await anonymous.patch(
                    "/api/demo/preferences", json={"enabled": False}, headers=HEADERS
                )
            ).status_code == 401
            assert (await anonymous.get("/api/demo")).status_code == 401
        assert (await alice.get("/api/demo")).json()["preference"] is True


def test_legacy_private_database_migrates_without_resetting_existing_settings(tmp_path):
    store = Store(tmp_path)
    store.execute(
        "INSERT INTO users(id,username,nickname,member_id,role,token,invite_token,"
        "share_history,privacy_revision) VALUES(?,?,?,?,?,?,?,?,?)",
        (
            "alice",
            "alice",
            "保留的昵称",
            1,
            "member",
            store.encrypt("existing"),
            store.secret(),
            1,
            7,
        ),
    )
    old = store.one("SELECT * FROM users WHERE id='alice'")
    store.close()
    with sqlite3.connect(tmp_path / "friends.sqlite3") as db:
        db.execute("ALTER TABLE users DROP COLUMN demo_enabled")
    reopened = Store(tmp_path)
    try:
        assert reopened.one("SELECT * FROM users WHERE id='alice'") == old
        assert old["demo_enabled"] == 1 and old["privacy_revision"] == 7
    finally:
        reopened.close()
