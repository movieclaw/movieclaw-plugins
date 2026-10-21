"""私人喜欢列表与广场独立授权：真实路由、SQLite、宿主读请求和并发预览回归。"""

import asyncio
import json
import sqlite3

import pytest
from movieclaw_friends.store import Store
from test_app import befriend, profile
from test_app import harness as harness
from test_native import native as native


def film(number=550, title="喜欢的电影", **extra):
    return {"tmdb_id": number, "kind": "movie", "title": title, "year": 1999} | extra


async def save(client, items, **extra):
    response = await client.put("/api/favorites", json={"items": items, **extra})
    assert response.status_code == 200, response.text
    return response.json()


async def preview(client, **changes):
    proposed = profile(discoverable=True, public_recent=False, share_favorites=True, **changes)
    response = await client.post("/api/profile/preview", json=proposed)
    assert response.status_code == 200, response.text
    return proposed, response.json()


async def publish(client, proposed, data):
    response = await client.patch(
        "/api/profile",
        json=proposed | {"public_consent": True, "preview_token": data["preview_token"]},
    )
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.asyncio
async def test_private_favorites_are_replaceable_scoped_and_do_not_auto_publish(harness):
    alice, bob = harness.clients["alice"], harness.clients["bob"]
    assert (await alice.get("/api/favorites")).json() == {"items": [], "public_items": []}
    first = await save(
        alice,
        [film(token="secret", file_path="/private/secret", poster_path="https://evil.test/x.jpg")],
        user_id=harness.users["bob"]["id"],
        member_id=2,
        publish=True,
    )
    assert first["public_items"] == []
    assert first["items"][0]["poster_path"] is None
    assert "secret" not in json.dumps(first)
    assert (await bob.get("/api/favorites?user_id=" + harness.users["alice"]["id"])).json() == {
        "items": [],
        "public_items": [],
    }
    assert (await bob.get("/api/directory")).json()["users"] == []
    second = await save(alice, [film(27205, "新的喜欢")])
    assert len(second["items"]) == 1 and second["items"][0]["tmdb_id"] == 27205
    assert second["public_items"] == []
    assert await save(alice, []) == {"items": [], "public_items": []}
    assert harness.upstream.snapshot_calls == []


@pytest.mark.asyncio
async def test_favorites_only_publication_requires_matching_preview_and_consent_without_history(
    harness,
):
    alice, bob = harness.clients["alice"], harness.clients["bob"]
    saved = await save(alice, [film(poster_path="/safe_Poster.jpg")])
    proposed, data = await preview(alice)
    assert data["public_profile"]["history"] == []
    assert data["public_profile"]["favorites"] == saved["items"]
    assert data["friend_data"]["favorites"] == []
    assert harness.upstream.snapshot_calls == []
    assert (await bob.get("/api/directory")).json()["users"] == []
    for payload in (
        proposed,
        proposed | {"preview_token": data["preview_token"]},
        proposed | {"public_consent": True},
        proposed
        | {"public_consent": True, "preview_token": data["preview_token"], "public_recent": True},
    ):
        assert (await alice.patch("/api/profile", json=payload)).status_code == 400
    assert (
        await bob.patch(
            "/api/profile",
            json=proposed | {"public_consent": True, "preview_token": data["preview_token"]},
        )
    ).status_code == 400
    result = await publish(alice, proposed, data)
    assert result["user"]["public_recent"] is False and result["user"]["share_favorites"] is True
    public = (await bob.get("/api/directory")).json()["users"][0]
    assert public["nickname"] == proposed["nickname"]
    assert public["history"] == [] and public["favorites"] == saved["items"]
    assert (await alice.get("/api/favorites")).json()["public_items"] == saved["items"]
    assert harness.upstream.snapshot_calls == []


@pytest.mark.asyncio
async def test_edit_preserves_authorized_snapshot_until_new_consent_and_invalidates_old_preview(
    harness,
):
    alice, bob = harness.clients["alice"], harness.clients["bob"]
    first = await save(alice, [film()])
    proposed, data = await preview(alice)
    await publish(alice, proposed, data)
    _, stale = await preview(alice)
    second = await save(alice, [film(129, "第二次选择")])
    assert second["public_items"] == first["items"]
    assert (await bob.get("/api/directory")).json()["users"][0]["favorites"] == first["items"]
    response = await alice.patch(
        "/api/profile",
        json=proposed | {"public_consent": True, "preview_token": stale["preview_token"]},
    )
    assert response.status_code == 400
    assert (await alice.get("/api/favorites")).json() == second
    proposed, current = await preview(alice)
    assert current["public_profile"]["favorites"] == second["items"]
    await publish(alice, proposed, current)
    assert (await bob.get("/api/directory")).json()["users"][0]["favorites"] == second["items"]


@pytest.mark.asyncio
async def test_recent_and_favorites_can_be_authorized_and_revoked_independently(harness):
    alice, bob = harness.clients["alice"], harness.clients["bob"]
    private = await save(alice, [film()])
    proposed = profile(discoverable=True, public_recent=True, share_favorites=True)
    data = (await alice.post("/api/profile/preview", json=proposed)).json()
    assert len(data["public_profile"]["history"]) == 5
    await publish(alice, proposed, data)
    public = (await bob.get("/api/directory")).json()["users"][0]
    assert len(public["history"]) == 5 and public["favorites"] == private["items"]
    only_recent = proposed | {"share_favorites": False}
    data = (await alice.post("/api/profile/preview", json=only_recent)).json()
    await publish(alice, only_recent, data)
    public = (await bob.get("/api/directory")).json()["users"][0]
    assert len(public["history"]) == 5 and public["favorites"] == []
    stored = harness.app.state.store.one(
        "SELECT * FROM users WHERE id=?", (harness.users["alice"]["id"],)
    )
    assert json.loads(stored["public_favorites"]) == []
    assert (await alice.get("/api/favorites")).json() == {
        "items": private["items"],
        "public_items": [],
    }
    only_name = proposed | {"public_recent": False, "share_favorites": False}
    data = (await alice.post("/api/profile/preview", json=only_name)).json()
    await publish(alice, only_name, data)
    public = (await bob.get("/api/directory")).json()["users"][0]
    assert public["history"] == public["favorites"] == []
    assert public["nickname"] == proposed["nickname"]


@pytest.mark.asyncio
async def test_closing_directory_clears_public_snapshots_caches_but_preserves_private_favorites(
    harness,
):
    relation = await befriend(harness)
    alice, bob = harness.clients["alice"], harness.clients["bob"]
    saved = await save(alice, [film()])
    proposed, data = await preview(alice)
    await publish(alice, proposed, data)
    harness.app.state.store.execute(
        "UPDATE relationships SET cache='old-public-data' WHERE id=?", (relation,)
    )
    response = await alice.patch("/api/profile", json=profile(share_history=True))
    assert response.status_code == 200
    user = harness.app.state.store.one(
        "SELECT * FROM users WHERE id=?", (harness.users["alice"]["id"],)
    )
    assert json.loads(user["public_favorites"]) == json.loads(user["public_history"]) == []
    assert user["share_history"] == 1
    assert (
        harness.app.state.store.one("SELECT cache FROM relationships WHERE id=?", (relation,))[
            "cache"
        ]
        is None
    )
    assert (await alice.get("/api/favorites")).json() == saved
    assert (await bob.get("/api/directory")).json()["users"] == []
    friend = (await bob.get(f"/api/friends/{relation}")).json()["data"]
    assert friend["favorites"] == []
    assert len(friend["history"]) == 7
    await alice.patch("/api/demo/preferences", json={"enabled": False})
    assert (await alice.get("/api/favorites")).json() == saved


@pytest.mark.asyncio
async def test_favorite_edit_during_history_preview_cannot_republish_old_selection(harness):
    alice, bob = harness.clients["alice"], harness.clients["bob"]
    await save(alice, [film()])
    harness.upstream.pause_token = "token-alice"
    proposed = profile(discoverable=True, public_recent=True, share_favorites=True)
    task = asyncio.create_task(alice.post("/api/profile/preview", json=proposed))
    await asyncio.wait_for(harness.upstream.started.wait(), timeout=2)
    latest = await save(alice, [film(129, "并发保存的新电影")])
    harness.upstream.release.set()
    response = await task
    assert response.status_code == 409
    assert (await alice.get("/api/favorites")).json() == latest
    assert (await bob.get("/api/directory")).json()["users"] == []


@pytest.mark.asyncio
async def test_capacity_preview_token_and_login_body_validation(harness):
    alice = harness.clients["alice"]
    items = [{"title": "电影"}] * 500
    saved = await save(alice, items)
    proposed, data = await preview(alice)
    assert len(data["preview_token"]) <= 14000
    assert len(data["public_profile"]["favorites"]) == 500
    await publish(alice, proposed, data)
    assert len((await alice.get("/api/favorites")).json()["public_items"]) == 500
    for payload in ({}, {"items": [film()] * 501}, {"items": [{"title": "字" * 301}]}):
        response = await alice.put("/api/favorites", json=payload)
        assert response.status_code in {413, 422}
    response = await alice.put(
        "/api/favorites", json={"items": []}, headers={"origin": "http://evil.test"}
    )
    assert response.status_code == 403
    assert (await alice.get("/api/favorites")).json()["items"] == saved["items"]
    alice.cookies.clear()
    assert (await alice.get("/api/favorites")).status_code == 401
    assert (await alice.put("/api/favorites", json={"items": []})).status_code == 401


@pytest.mark.asyncio
async def test_native_favorites_uses_real_account_without_any_host_operation(native):
    base = "/api/v1/plugins/watch-friends"
    alice, bob = native.clients["alice"], native.clients["bob"]
    assert (
        await alice.put(f"{base}/favorites", json={"items": [film()], "user_id": "bob"})
    ).status_code == 200
    assert (await bob.get(f"{base}/favorites")).json() == {"items": [], "public_items": []}
    response = await alice.post(
        f"{base}/profile/preview",
        json=profile(discoverable=True, public_recent=False, share_favorites=True),
    )
    assert response.status_code == 200
    assert response.json()["public_profile"]["favorites"][0]["tmdb_id"] == 550
    assert native.ops.calls == []


def test_legacy_authorization_and_private_favorites_survive_schema_migration(tmp_path):
    store = Store(tmp_path)
    store.execute(
        "INSERT INTO users(id,username,nickname,role,token,invite_token,discoverable,"
        "public_history) VALUES(?,?,?,?,?,?,?,?)",
        ("alice", "alice", "原姓名", "member", store.encrypt("old"), store.secret(), 1, "[]"),
    )
    store.execute("INSERT INTO user_favorites VALUES(?,?)", ("alice", json.dumps([film()])))
    store.close()
    with sqlite3.connect(tmp_path / "friends.sqlite3") as db:
        for name in ("public_recent", "share_favorites", "public_favorites"):
            db.execute(f"ALTER TABLE users DROP COLUMN {name}")
    reopened = Store(tmp_path)
    try:
        user = reopened.one("SELECT * FROM users WHERE id='alice'")
        assert user["discoverable"] == user["public_recent"] == 1
        assert user["share_favorites"] == 0 and user["public_favorites"] == "[]"
        assert json.loads(
            reopened.one("SELECT data FROM user_favorites WHERE user_id='alice'")["data"]
        ) == [film()]
    finally:
        reopened.close()
