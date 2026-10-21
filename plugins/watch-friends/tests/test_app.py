"""通过真实 ASGI 应用与 SQLite 验证同服授权、公开确认与撤回竞态。"""

import asyncio
import json
from contextlib import AsyncExitStack
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx
import pytest
import pytest_asyncio
from movieclaw_friends.app import COOKIE, create_app
from movieclaw_friends.config import Settings
from movieclaw_friends.upstream import UpstreamError

HEADERS = {"x-friends-request": "1", "origin": "http://a.test"}


class FakeUpstream:
    """模拟只读宿主边界；暂停返回用于复现撤回时已有请求仍在途的真实场景。"""

    def __init__(self):
        self.tokens = {
            f"token-{username}": {
                "username": username,
                "nickname": "相同昵称",
                "role": "member",
                "member_id": member_id,
            }
            for member_id, username in enumerate(("alice", "bob", "charlie"), 1)
        }
        self.identity_calls = []
        self.snapshot_calls = []
        self.collection_calls = []
        self.pause_token = None
        self.pause_collection_id = None
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def identity(self, token):
        self.identity_calls.append(token)
        if token not in self.tokens:
            raise UpstreamError("MovieClaw 登录令牌无效或已过期", 401)
        return deepcopy(self.tokens[token])

    def watch(self, username, id, *, current=False):
        return {
            "id": id,
            "media": {
                "media_item_id": id,
                "tmdb_id": id + 1000,
                "kind": "movie",
                "title": f"{username} 的影片 {id}",
                "year": 2026,
                "poster_url": "https://images.test/poster.jpg",
                "season_number": 0,
                "episode_number": 0,
                "file_path": "/private/library/secret.mkv",
                "stream_url": "https://host/stream?token=secret-token",
                "token": "secret-token",
            },
            "started_at": "2026-10-20T00:00:00Z",
            "ended_at": None if current else "2026-10-20T02:00:00Z",
            "watched_ms": 120000,
            "end_position_ms": 125000,
            "device_name": "私有卧室电视",
            "device_id": "secret-device",
            "path": "/private/library/secret.mkv",
        }

    async def snapshot(self, token, member_id, share_current, share_history):
        identity = self.tokens[token]
        assert identity["member_id"] == member_id
        self.snapshot_calls.append((token, member_id, share_current, share_history))
        # 先捕获请求开始时的同意状态，再阻塞，以便确实返回旧的已授权数据。
        snapshot = {
            "current": self.watch(identity["username"], 90, current=True)
            if share_current
            else None,
            "history": [self.watch(identity["username"], id) for id in range(7, 0, -1)]
            if share_history
            else [],
            "sharing": {"current": share_current, "history": share_history},
            "observed_at": datetime.now(UTC).isoformat(),
        }
        if token == self.pause_token:
            self.started.set()
            await self.release.wait()
        return snapshot

    async def collections(self, token):
        assert token in self.tokens
        return [
            {"id": 11, "name": "主动分享的片单", "item_count": 1},
            {"id": 12, "name": "未选择的私有片单", "item_count": 1},
        ]

    async def collection(self, token, id):
        self.collection_calls.append((token, id))
        collection = {
            "id": id,
            "name": "主动分享的片单" if id == 11 else "未选择的私有片单",
            "items": [self.watch(self.tokens[token]["username"], id)["media"]],
            "owner_token": "secret-token",
            "entry_dirs": ["/private/library"],
        }
        if id == self.pause_collection_id:
            self.started.set()
            await self.release.wait()
        return collection


@dataclass
class Harness:
    app: object
    upstream: FakeUpstream
    clients: dict
    users: dict


@pytest_asyncio.fixture
async def harness(tmp_path):
    upstream = FakeUpstream()
    settings = Settings(
        "http://movieclaw",
        public_url="http://a.test",
        data_dir=tmp_path,
        allow_private_peers=True,
        demo_enabled=False,
    )
    app = create_app(settings, upstream=upstream)
    async with AsyncExitStack() as stack:
        await stack.enter_async_context(app.router.lifespan_context(app))
        clients, users = {}, {}
        for username in ("alice", "bob", "charlie"):
            client = await stack.enter_async_context(
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="http://a.test",
                    headers=HEADERS,
                )
            )
            response = await client.post("/api/login", json={"token": f"token-{username}"})
            assert response.status_code == 200
            clients[username], users[username] = client, response.json()["user"]
        yield Harness(app, upstream, clients, users)


def profile(**changes):
    return {
        "nickname": "显示姓名",
        "discoverable": False,
        "share_current": False,
        "share_history": False,
    } | changes


async def befriend(harness):
    alice, bob = harness.clients["alice"], harness.clients["bob"]
    response = await alice.post(
        "/api/requests",
        json={"invite_code": harness.users["bob"]["invite_code"], "address_consent": True},
    )
    assert response.status_code == 200
    relation_id = response.json()["id"]
    response = await bob.post(f"/api/requests/{relation_id}/accept", json={"address_consent": True})
    assert response.status_code == 200
    return relation_id


@pytest.mark.asyncio
async def test_real_friend_request_notifies_only_recipient_and_profiles_keep_privacy(harness):
    alice, bob, charlie = (harness.clients[name] for name in ("alice", "bob", "charlie"))
    assert (await alice.patch("/api/community/profile", json={
        "avatar_id": "xiaolu", "bio": "喜欢科幻电影",
    })).status_code == 200
    relation_id = await befriend(harness)
    notification = (await bob.get("/api/community/notifications")).json()
    assert notification["unread_count"] == 1
    row = notification["notifications"][0]
    assert row["kind"] == "friend_request" and row["target_id"] == relation_id
    assert row["actor"]["id"] == harness.users["alice"]["id"]
    assert row["actor"]["avatar_id"] == "xiaolu"
    assert (await alice.get("/api/community/notifications")).json()["notifications"] == []
    assert (await charlie.get("/api/community/notifications")).json()["notifications"] == []
    friend = (await bob.get(f"/api/friends/{relation_id}")).json()["friend"]
    assert friend["avatar_id"] == "xiaolu" and friend["bio"] == "喜欢科幻电影"
    public = (await charlie.get(f"/api/community/profiles/{harness.users['alice']['id']}")).json()
    assert public["profile"]["bio"] == "喜欢科幻电影" and public["public_profile"] is None
    assert harness.upstream.snapshot_calls == [("token-alice", 1, False, False)]


@pytest.mark.asyncio
@pytest.mark.parametrize("blocker", ["alice", "bob"])
async def test_block_during_local_snapshot_stops_delivery_and_unblock_restores_friend(
    harness, blocker,
):
    alice, bob = harness.clients["alice"], harness.clients["bob"]
    relation_id = await befriend(harness)
    await alice.patch("/api/profile", json=profile(share_history=True))
    harness.upstream.pause_token = "token-alice"
    inflight = asyncio.create_task(bob.get(f"/api/friends/{relation_id}"))
    await asyncio.wait_for(harness.upstream.started.wait(), timeout=2)
    other = "bob" if blocker == "alice" else "alice"
    target_id = harness.users[other]["id"]
    try:
        blocked = await harness.clients[blocker].post(
            "/api/community/blocks", json={"user_id": target_id}
        )
        assert blocked.status_code == 200
    finally:
        harness.upstream.release.set()
    response = await asyncio.wait_for(inflight, timeout=2)
    assert response.status_code == 404 and "alice 的影片" not in response.text
    stored = harness.app.state.store.one("SELECT * FROM relationships WHERE id=?", (relation_id,))
    assert stored["status"] == "accepted" and stored["cache"] is None
    assert (await bob.get("/api/friends")).json()["friends"] == []
    assert (await alice.get("/api/friends")).json()["friends"] == []
    await harness.clients[blocker].delete(f"/api/community/blocks/{target_id}")
    restored = await bob.get(f"/api/friends/{relation_id}")
    assert restored.status_code == 200 and restored.json()["data"]["history"]


def assert_no_secrets(data):
    serialized = json.dumps(data, ensure_ascii=False)
    for private in (
        "/private/",
        "secret-token",
        "secret-device",
        "私有卧室电视",
        "stream_url",
        "file_path",
        "owner_token",
        "entry_dirs",
    ):
        assert private not in serialized


@pytest.mark.asyncio
async def test_login_identity_default_privacy_and_encrypted_credentials(harness):
    alice, bob = harness.clients["alice"], harness.clients["bob"]
    user = (await alice.get("/api/me")).json()["user"]
    assert user["username"] == "alice"
    assert user["nickname"] == harness.users["bob"]["nickname"]
    assert user["id"] != harness.users["bob"]["id"]
    assert {key: user[key] for key in ("discoverable", "share_current", "share_history")} == {
        "discoverable": False,
        "share_current": False,
        "share_history": False,
    }
    assert (await bob.get("/api/directory")).json() == {"users": []}
    assert (await alice.get("/api/preview")).json()["history"] == []
    assert "token-alice" not in json.dumps((await alice.get("/api/me")).json())
    stored = harness.app.state.store.one("SELECT * FROM users WHERE username='alice'")
    assert stored["token"] != "token-alice"
    assert harness.app.state.store.decrypt(stored["token"]) == "token-alice"
    response = await alice.post("/api/login", json={"token": "invalid"})
    assert response.status_code == 401
    assert harness.upstream.identity_calls[-1] == "invalid"
    assert COOKIE in alice.cookies


@pytest.mark.asyncio
async def test_csrf_headers_origin_and_unauthenticated_boundary(harness):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=harness.app), base_url="http://a.test"
    ) as anonymous:
        assert (await anonymous.get("/api/directory")).status_code == 401
        missing = await anonymous.post("/api/login", json={"token": "token-alice"})
        assert missing.status_code == 403
        forged = await anonymous.post(
            "/api/login",
            json={"token": "token-alice"},
            headers={"x-friends-request": "1", "origin": "https://unrelated.test"},
        )
        assert forged.status_code == 403
        allowed = await anonymous.post("/api/login", json={"token": "token-alice"}, headers=HEADERS)
        assert allowed.status_code == 200
        cookie = allowed.headers["set-cookie"].lower()
        assert "httponly" in cookie and "samesite=strict" in cookie
        assert allowed.headers["cache-control"] == "no-store"
        assert (await anonymous.post("/api/logout", headers=HEADERS)).status_code == 200
        assert (await anonymous.get("/api/me")).status_code == 401


@pytest.mark.asyncio
async def test_public_preview_is_not_published_and_requires_matching_user_settings_consent(harness):
    alice, bob = harness.clients["alice"], harness.clients["bob"]
    proposed = profile(discoverable=True)
    preview = await alice.post("/api/profile/preview", json=proposed)
    assert preview.status_code == 200
    result = preview.json()
    assert len(result["public_profile"]["history"]) == 5
    assert_no_secrets(result["public_profile"])
    assert (await bob.get("/api/directory")).json()["users"] == []
    assert not (await alice.get("/api/me")).json()["user"]["discoverable"]
    token = result["preview_token"]
    assert (
        await alice.patch("/api/profile", json=proposed | {"preview_token": token})
    ).status_code == 400
    assert (
        await alice.patch("/api/profile", json=proposed | {"public_consent": True})
    ).status_code == 400
    # 同名、同设定也不能使用另一个用户的公开确认令牌。
    assert (
        await bob.patch(
            "/api/profile", json=proposed | {"preview_token": token, "public_consent": True}
        )
    ).status_code == 400
    assert (
        await alice.patch(
            "/api/profile",
            json=proposed
            | {
                "share_history": True,
                "preview_token": token,
                "public_consent": True,
            },
        )
    ).status_code == 400
    published = await alice.patch(
        "/api/profile", json=proposed | {"preview_token": token, "public_consent": True}
    )
    assert published.status_code == 200
    public = (await bob.get("/api/directory")).json()["users"]
    assert len(public) == 1 and public[0]["id"] == harness.users["alice"]["id"]
    assert public[0]["nickname"] == proposed["nickname"]
    assert [entry["id"] for entry in public[0]["history"]] == [7, 6, 5, 4, 3]
    assert set(public[0]) == {
        "id", "nickname", "history", "favorites", "updated_at", "is_demo", "avatar_id", "bio"
    }
    assert public[0]["favorites"] == []
    assert public[0]["is_demo"] is False
    assert_no_secrets(public)
    # 公开前五条的单独同意不打开好友历史共享。
    own_data = (await alice.get("/api/preview")).json()
    assert own_data["history"] == [] and own_data["sharing"]["history"] is False
    hidden = await alice.patch("/api/profile", json=profile())
    assert hidden.status_code == 200
    assert (await bob.get("/api/directory")).json()["users"] == []
    stored = harness.app.state.store.one(
        "SELECT public_history,public_updated_at FROM users WHERE id=?",
        (harness.users["alice"]["id"],),
    )
    assert json.loads(stored["public_history"]) == [] and stored["public_updated_at"] is None


@pytest.mark.asyncio
async def test_request_needs_consent_only_recipient_accepts_and_pending_is_private(harness):
    alice, bob, charlie = (harness.clients[name] for name in ("alice", "bob", "charlie"))
    invite = harness.users["bob"]["invite_code"]
    assert (await alice.post("/api/requests", json={"invite_code": invite})).status_code == 400
    # 未开放广场的账号不能仅靠可枚举 user_id 收申请。
    assert (
        await alice.post(
            "/api/requests", json={"user_id": harness.users["bob"]["id"], "address_consent": True}
        )
    ).status_code == 404
    sent = await alice.post("/api/requests", json={"invite_code": invite, "address_consent": True})
    assert sent.status_code == 200
    relation_id = sent.json()["id"]
    assert (await alice.get(f"/api/friends/{relation_id}")).status_code == 403
    assert (await charlie.get(f"/api/friends/{relation_id}")).status_code == 404
    assert len((await bob.get("/api/requests")).json()["incoming"]) == 1
    assert len((await alice.get("/api/requests")).json()["outgoing"]) == 1
    assert (
        await alice.post(f"/api/requests/{relation_id}/accept", json={"address_consent": True})
    ).status_code == 409
    assert (
        await charlie.post(f"/api/requests/{relation_id}/accept", json={"address_consent": True})
    ).status_code == 404
    assert (await bob.post(f"/api/requests/{relation_id}/accept", json={})).status_code == 400
    assert (
        await bob.post(f"/api/requests/{relation_id}/accept", json={"address_consent": True})
    ).status_code == 200
    assert len((await alice.get("/api/friends")).json()["friends"]) == 1
    assert len((await bob.get("/api/friends")).json()["friends"]) == 1
    assert (await charlie.get("/api/friends")).json()["friends"] == []


@pytest.mark.asyncio
async def test_current_history_permissions_and_selected_collection_snapshots_are_independent(
    harness,
):
    alice, bob = harness.clients["alice"], harness.clients["bob"]
    relation_id = await befriend(harness)
    assert (await alice.patch("/api/profile", json=profile(share_current=True))).status_code == 200
    only_current = (await bob.get(f"/api/friends/{relation_id}")).json()["data"]
    assert only_current["current"]["media"]["title"] == "alice 的影片 90"
    assert only_current["history"] == []
    assert only_current["current"]["expires_at"] is not None
    assert_no_secrets(only_current)
    assert (await alice.patch("/api/profile", json=profile(share_history=True))).status_code == 200
    only_history = (await bob.get(f"/api/friends/{relation_id}")).json()["data"]
    assert only_history["current"] is None and len(only_history["history"]) == 7
    rows = (await alice.get("/api/collections")).json()["collections"]
    assert not any(row["shared"] for row in rows)
    assert (await alice.post("/api/collections/11/share")).status_code == 200
    with_collection = (await bob.get(f"/api/friends/{relation_id}")).json()["data"]
    assert [row["id"] for row in with_collection["collections"]] == [11]
    assert harness.upstream.collection_calls == [("token-alice", 11)]
    assert with_collection["collections"][0]["items"][0]["title"] == "alice 的影片 11"
    assert_no_secrets(with_collection)
    # 片单保存的是主动选择时的副本，后续好友读取不会重新访问未选片单。
    await bob.get(f"/api/friends/{relation_id}")
    assert harness.upstream.collection_calls == [("token-alice", 11)]
    assert (await alice.delete("/api/collections/11/share")).status_code == 200
    assert (
        harness.app.state.store.one("SELECT cache FROM relationships WHERE id=?", (relation_id,))[
            "cache"
        ]
        is None
    ), "取消片单共享后不能在插件好友缓存中继续保留该片单副本"
    assert (await bob.get(f"/api/friends/{relation_id}")).json()["data"]["collections"] == []
    assert harness.app.state.store.one("SELECT * FROM collections") is None


@pytest.mark.asyncio
async def test_revoking_relationship_removes_cache_and_stops_both_sides_access(harness):
    alice, bob, charlie = (harness.clients[name] for name in ("alice", "bob", "charlie"))
    relation_id = await befriend(harness)
    await alice.patch("/api/profile", json=profile(share_history=True))
    assert (await bob.get(f"/api/friends/{relation_id}")).status_code == 200
    assert (
        harness.app.state.store.one("SELECT cache FROM relationships WHERE id=?", (relation_id,))[
            "cache"
        ]
        is not None
    )
    assert (await charlie.delete(f"/api/friends/{relation_id}")).status_code == 404
    assert (await bob.delete(f"/api/friends/{relation_id}")).status_code == 200
    stored = harness.app.state.store.one("SELECT * FROM relationships WHERE id=?", (relation_id,))
    assert stored["status"] == "revoked" and stored["cache"] is None
    calls = len(harness.upstream.snapshot_calls)
    assert (await bob.get(f"/api/friends/{relation_id}")).status_code == 403
    assert (await alice.get(f"/api/friends/{relation_id}")).status_code == 403
    assert len(harness.upstream.snapshot_calls) == calls
    assert (await bob.get("/api/friends")).json()["friends"] == []


@pytest.mark.asyncio
async def test_disabling_history_clears_cache_and_drops_inflight_old_snapshot(harness):
    alice, bob = harness.clients["alice"], harness.clients["bob"]
    relation_id = await befriend(harness)
    await alice.patch("/api/profile", json=profile(share_current=True, share_history=True))
    await bob.get(f"/api/friends/{relation_id}")
    harness.upstream.pause_token = "token-alice"
    inflight = asyncio.create_task(bob.get(f"/api/friends/{relation_id}"))
    await asyncio.wait_for(harness.upstream.started.wait(), timeout=2)
    try:
        assert (
            await alice.patch("/api/profile", json=profile(share_current=True, share_history=False))
        ).status_code == 200
        assert (
            harness.app.state.store.one(
                "SELECT cache FROM relationships WHERE id=?", (relation_id,)
            )["cache"]
            is None
        )
    finally:
        harness.upstream.release.set()
    result = await asyncio.wait_for(inflight, timeout=2)
    assert result.status_code == 200
    data = result.json()["data"]
    assert data["current"] is not None and data["history"] == []
    assert data["sharing"] == {"current": True, "history": False}
    cached = json.loads(
        harness.app.state.store.one("SELECT cache FROM relationships WHERE id=?", (relation_id,))[
            "cache"
        ]
    )
    assert cached["history"] == [] and not cached["sharing"]["history"]


@pytest.mark.asyncio
async def test_revoking_during_snapshot_does_not_return_or_recache_old_data(harness):
    alice, bob = harness.clients["alice"], harness.clients["bob"]
    relation_id = await befriend(harness)
    await alice.patch("/api/profile", json=profile(share_history=True))
    harness.upstream.pause_token = "token-alice"
    inflight = asyncio.create_task(bob.get(f"/api/friends/{relation_id}"))
    await asyncio.wait_for(harness.upstream.started.wait(), timeout=2)
    try:
        assert (await alice.delete(f"/api/friends/{relation_id}")).status_code == 200
    finally:
        harness.upstream.release.set()
    result = await asyncio.wait_for(inflight, timeout=2)
    assert result.status_code == 403
    assert "alice 的影片" not in result.text
    stored = harness.app.state.store.one("SELECT * FROM relationships WHERE id=?", (relation_id,))
    assert stored["cache"] is None and stored["status"] == "revoked"


@pytest.mark.asyncio
async def test_unsharing_collection_prevents_older_inflight_share_from_restoring_it(harness):
    alice, bob = harness.clients["alice"], harness.clients["bob"]
    relation_id = await befriend(harness)
    assert (await alice.post("/api/collections/11/share")).status_code == 200
    assert (await bob.get(f"/api/friends/{relation_id}")).json()["data"]["collections"]
    harness.upstream.pause_collection_id = 11
    inflight = asyncio.create_task(alice.post("/api/collections/11/share"))
    await asyncio.wait_for(harness.upstream.started.wait(), timeout=2)
    try:
        assert (await alice.delete("/api/collections/11/share")).status_code == 200
    finally:
        harness.upstream.release.set()
    result = await asyncio.wait_for(inflight, timeout=2)
    assert result.status_code == 409
    assert harness.app.state.store.one("SELECT * FROM collections") is None
    assert (await bob.get(f"/api/friends/{relation_id}")).json()["data"]["collections"] == []
    listed = (await alice.get("/api/collections")).json()["collections"]
    assert next(row for row in listed if row["id"] == 11)["shared"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["profile", "unshare_collection"])
async def test_old_public_preview_cannot_override_a_later_privacy_change(harness, change):
    alice, bob = harness.clients["alice"], harness.clients["bob"]
    assert (await alice.post("/api/collections/11/share")).status_code == 200
    proposed = profile(discoverable=True)
    preview = (await alice.post("/api/profile/preview", json=proposed)).json()
    if change == "profile":
        assert (
            await alice.patch("/api/profile", json=profile(share_history=True))
        ).status_code == 200
    else:
        assert (await alice.delete("/api/collections/11/share")).status_code == 200
    stale = await alice.patch(
        "/api/profile",
        json=proposed | {"public_consent": True, "preview_token": preview["preview_token"]},
    )
    assert stale.status_code == 400
    assert (await bob.get("/api/directory")).json()["users"] == []
    actual = (await alice.get("/api/me")).json()["user"]
    assert actual["discoverable"] is False
    if change == "profile":
        assert actual["share_history"] is True
    else:
        assert harness.app.state.store.one("SELECT * FROM collections") is None
    fresh = (await alice.post("/api/profile/preview", json=proposed)).json()
    assert (
        await alice.patch(
            "/api/profile",
            json=proposed | {"public_consent": True, "preview_token": fresh["preview_token"]},
        )
    ).status_code == 200


@pytest.mark.asyncio
async def test_same_username_with_new_member_id_cannot_inherit_existing_friends(harness):
    alice = harness.clients["alice"]
    relation_id = await befriend(harness)
    await alice.patch("/api/profile", json=profile(share_history=True))
    harness.upstream.tokens["new-alice-token"] = {
        **harness.upstream.tokens["token-alice"],
        "member_id": 999,
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=harness.app), base_url="http://a.test", headers=HEADERS
    ) as newcomer:
        response = await newcomer.post("/api/login", json={"token": "new-alice-token"})
        assert response.status_code == 409
        assert COOKIE not in newcomer.cookies
        assert (await newcomer.get(f"/api/friends/{relation_id}")).status_code == 401
    original = harness.app.state.store.one("SELECT * FROM users WHERE username='alice'")
    assert original["id"] == harness.users["alice"]["id"]
    assert original["member_id"] == 1
    assert harness.app.state.store.decrypt(original["token"]) == "token-alice"
    assert len(harness.app.state.store.all("SELECT * FROM users WHERE username='alice'")) == 1
    assert len((await alice.get("/api/friends")).json()["friends"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "share_current,share_history", [(False, False), (True, False), (True, True)]
)
async def test_settings_preview_reads_once_and_separates_public_from_friend_permissions(
    harness,
    share_current,
    share_history,
):
    alice = harness.clients["alice"]
    before = len(harness.upstream.snapshot_calls)
    response = await alice.post(
        "/api/profile/preview",
        json=profile(
            discoverable=True,
            share_current=share_current,
            share_history=share_history,
        ),
    )
    assert response.status_code == 200
    result = response.json()
    assert len(harness.upstream.snapshot_calls) == before + 1
    assert harness.upstream.snapshot_calls[-1] == ("token-alice", 1, share_current, True)
    assert len(result["public_profile"]["history"]) == 5
    friend = result["friend_data"]
    assert friend["sharing"] == {"current": share_current, "history": share_history}
    assert (friend["current"] is not None) is share_current
    assert len(friend["history"]) == (7 if share_history else 0)
    assert_no_secrets(result["public_profile"])
    assert_no_secrets(friend)
    unchanged = (await alice.get("/api/me")).json()["user"]
    assert unchanged["discoverable"] is False and unchanged["share_current"] is False
    assert unchanged["share_history"] is False


async def test_new_upstream_credential_invalidates_older_plugin_sessions(harness):
    alice = harness.clients["alice"]
    harness.upstream.tokens["replacement-alice"] = deepcopy(harness.upstream.tokens["token-alice"])
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=harness.app), base_url="http://a.test", headers=HEADERS
    ) as newer_device:
        response = await newer_device.post("/api/login", json={"token": "replacement-alice"})
        assert response.status_code == 200
        assert (await newer_device.get("/api/me")).status_code == 200
        assert (await alice.get("/api/me")).status_code == 401


@pytest.mark.parametrize("offline", [False, True])
async def test_source_collection_disappears_but_shared_snapshot_can_be_revoked(harness, offline):
    alice = harness.clients["alice"]
    assert (await alice.post("/api/collections/11/share")).status_code == 200

    async def unavailable(token):
        if offline:
            raise UpstreamError("宿主暂时不可用")
        return []

    harness.upstream.collections = unavailable
    result = (await alice.get("/api/collections")).json()
    assert result["collections"] == [
        {
            "id": 11,
            "name": "主动分享的片单",
            "item_count": 1,
            "shared": True,
            "source_available": False,
        }
    ]
    assert (await alice.delete("/api/collections/11/share")).status_code == 200
    assert harness.app.state.store.all("SELECT * FROM collections") == []
