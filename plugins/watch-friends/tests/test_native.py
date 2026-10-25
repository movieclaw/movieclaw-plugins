"""原生路由集成：模拟宿主登录与网页托管，使用真实业务、存储和 HOST_OPS 适配。"""

import tomllib
from contextlib import AsyncExitStack
from copy import deepcopy
from datetime import datetime
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from movieclaw_friends.app import COOKIE, create_app
from movieclaw_friends.config import Settings
from movieclaw_friends.native import apply, build_routers
from movieclaw_friends.native_source import NativeSource

PREFIX = "/api/v1/plugins/watch-friends"
ORIGIN = "http://host.test"
PLUGIN_ROOT = Path(__file__).resolve().parents[1]


class HostOps:
    """模拟正式只读操作的数据响应；用户身份由另一个宿主登录依赖给出。"""

    def __init__(self):
        self.calls = []
        self.members = [
            {
                "id": id,
                "username": name,
                "nickname": "相同昵称",
                "created_at": "2026-01-01T00:00:00Z",
                "status": "active",
            }
            for id, name in enumerate(("alice", "bob", "charlie"), 1)
        ]

    async def call(self, operation, arguments):
        self.calls.append((operation, deepcopy(arguments)))
        if operation == "members.list":
            assert arguments == {}
            return deepcopy(self.members)
        assert operation == "playback.history"
        assert set(arguments) == {"member_id", "scope", "limit"}
        member_id = arguments["member_id"]
        assert member_id in (1, 2, 3) and arguments["scope"] == "all" and arguments["limit"] == 50
        return {
            "entries": [
                {
                    "id": id,
                    "media": {
                        "media_item_id": id,
                        "title": f"成员 {member_id} 的影片 {id}",
                        "kind": "movie",
                        "file_path": "/private/library/film.mkv",
                        "stream_url": "https://host/stream?token=secret-host-token",
                    },
                    "started_at": "2026-10-01T00:00:00Z",
                    "ended_at": "2026-10-01T02:00:00Z",
                    "device_name": "卧室设备",
                }
                for id in range(7, 0, -1)
            ]
        }


@pytest_asyncio.fixture
async def native(tmp_path):
    ops = HostOps()
    business = create_app(
        Settings("", ORIGIN, tmp_path, demo_enabled=False, native=True), upstream=NativeSource(ops)
    )
    principals = {
        name: SimpleNamespace(
            kind="member",
            name=name,
            plugin=None,
            member_id=id,
            member=SimpleNamespace(nickname="相同昵称", created_at=datetime(2026, 1, 1)),
        )
        for id, name in enumerate(("alice", "bob", "charlie"), 1)
    }
    principals["program"] = SimpleNamespace(kind="admin", name="plugin", plugin=object())

    async def require_login(request: Request):
        principal = principals.get(request.headers.get("x-host-account"))
        if principal is None:
            raise HTTPException(401, "宿主尚未登录")
        return principal

    host = FastAPI()

    @host.middleware("http")
    async def host_authentication(request, call_next):
        # 生产由宿主 member 区鉴权；此处认证头只在测试宿主中代表已验证的会话。
        if request.url.path.startswith(PREFIX) and not request.headers.get("x-host-account"):
            return JSONResponse({"detail": "宿主尚未登录"}, status_code=401)
        return await call_next(request)

    member, public = build_routers(business, require_login)
    # 宿主按 web/index.html 发现并托管静态资源，业务 router 不重复提供静态服务器。
    host.mount(f"{PREFIX}/web", StaticFiles(directory=PLUGIN_ROOT / "web", html=True))
    host.include_router(member, prefix=PREFIX)
    host.include_router(public, prefix="/api/v1/public/plugins/watch-friends")
    async with AsyncExitStack() as stack:
        await stack.enter_async_context(business.router.lifespan_context(business))
        clients = {}
        for name in ("alice", "bob", "charlie"):
            clients[name] = await stack.enter_async_context(
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=host, raise_app_exceptions=False),
                    base_url=ORIGIN,
                    headers={"x-host-account": name, "x-friends-request": "1", "origin": ORIGIN},
                )
            )
        yield SimpleNamespace(
            app=business,
            host=host,
            ops=ops,
            clients=clients,
            principals=principals,
            member=member,
            public=public,
        )


async def me(client):
    response = await client.get(f"{PREFIX}/me")
    assert response.status_code == 200
    return response.json()["user"]


def profile(**changes):
    return {
        "nickname": "相同昵称",
        "discoverable": False,
        "share_current": False,
        "share_history": False,
    } | changes


async def friendship(native):
    alice, bob = native.clients["alice"], native.clients["bob"]
    await me(alice)
    target = await me(bob)
    response = await alice.post(
        f"{PREFIX}/requests",
        json={
            "invite_code": target["invite_code"],
            "address_consent": True,
        },
    )
    assert response.status_code == 200
    relation_id = response.json()["id"]
    assert (
        await bob.post(f"{PREFIX}/requests/{relation_id}/accept", json={"address_consent": True})
    ).status_code == 200
    return relation_id


@pytest.mark.asyncio
async def test_native_mount_uses_host_login_and_has_no_separate_auth_endpoints(native):
    manifest = tomllib.loads((PLUGIN_ROOT / "movieclaw-plugin.toml").read_text())
    assert manifest["plugin"]["runtime"] == "inline"
    assert import_module(manifest["plugin"]["entry"]).apply is apply
    assert manifest["plugin"]["version"] == "0.1.2"
    assert manifest["permissions"]["operations"] == [
        "members.list",
        "playback.history",
        "search.library",
    ]
    assert not {"/login", "/logout", "/"}.intersection(route.path for route in native.member.routes)
    assert all(route.path.startswith("/federation/") for route in native.public.routes)
    alice = native.clients["alice"]
    assert (await alice.post(f"{PREFIX}/login", json={"token": "not-used"})).status_code == 404
    assert (await alice.post(f"{PREFIX}/logout")).status_code == 404
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=native.host), base_url=ORIGIN
    ) as anonymous:
        assert (await anonymous.get(f"{PREFIX}/me")).status_code == 401
        assert (await anonymous.get(f"{PREFIX}/web/")).status_code == 401
    page = await alice.get(f"{PREFIX}/web/")
    assert page.status_code == 200 and "无需单独登录" in page.text
    assert 'type="password"' not in page.text
    assert (await alice.get(f"{PREFIX}/web/app.js")).status_code == 200


@pytest.mark.asyncio
async def test_native_accounts_are_separate_without_host_tokens_or_plugin_cookies(native):
    alice, bob = native.clients["alice"], native.clients["bob"]
    first, second = await me(alice), await me(bob)
    assert first["id"] != second["id"] and first["nickname"] == second["nickname"]
    assert first["username"] == "member:1:2026-01-01T00:00:00"
    assert second["username"] == "member:2:2026-01-01T00:00:00"
    assert COOKIE not in alice.cookies and COOKIE not in bob.cookies
    assert native.app.state.store.all("SELECT * FROM sessions") == []
    injected_cookie = await alice.get(
        f"{PREFIX}/me",
        headers={
            "cookie": f"{COOKIE}=pretend-bob; movieclaw_session=not-a-real-host-token",
            "x-friends-user-id": second["id"],
        },
    )
    assert injected_cookie.status_code == 200
    assert injected_cookie.json()["user"]["id"] == first["id"]
    for row in native.app.state.store.all("SELECT * FROM users"):
        assert native.app.state.store.decrypt(row["token"]) == row["username"]
        assert not row["discoverable"] and not row["share_current"] and not row["share_history"]
    forged = await alice.patch(
        f"{PREFIX}/profile?user_id={second['id']}",
        json=profile(
            nickname="Alice 修改昵称",
            id=second["id"],
            member_id=2,
            username=second["username"],
        ),
    )
    assert forged.status_code == 200 and forged.json()["user"]["id"] == first["id"]
    assert (await me(bob))["nickname"] == "相同昵称"
    denied = await alice.get(f"{PREFIX}/me", headers={"x-host-account": "program"})
    assert denied.status_code == 403


@pytest.mark.asyncio
async def test_native_friendship_still_requires_both_sides_and_filters_history(native):
    alice, bob, third = (native.clients[name] for name in ("alice", "bob", "charlie"))
    target = await me(bob)
    assert (
        await alice.post(f"{PREFIX}/requests", json={"invite_code": target["invite_code"]})
    ).status_code == 400
    sent = await alice.post(
        f"{PREFIX}/requests",
        json={
            "invite_code": target["invite_code"],
            "address_consent": True,
        },
    )
    relation_id = sent.json()["id"]
    assert (await alice.get(f"{PREFIX}/friends/{relation_id}")).status_code == 403
    assert (
        await alice.post(f"{PREFIX}/requests/{relation_id}/accept", json={"address_consent": True})
    ).status_code == 409
    assert (
        await bob.post(f"{PREFIX}/requests/{relation_id}/accept", json={"address_consent": True})
    ).status_code == 200
    assert (await third.get(f"{PREFIX}/friends/{relation_id}")).status_code == 404
    assert (
        await alice.patch(f"{PREFIX}/profile", json=profile(share_history=True))
    ).status_code == 200
    result = await bob.get(f"{PREFIX}/friends/{relation_id}")
    assert result.status_code == 200
    data = result.json()["data"]
    assert len(data["history"]) == 7 and data["current"] is None
    assert all("成员 1" in item["media"]["title"] for item in data["history"])
    for private in ("/private/", "secret-host-token", "stream_url", "卧室设备"):
        assert private not in result.text
    assert ("playback.history", {"member_id": 1, "scope": "all", "limit": 50}) in native.ops.calls


@pytest.mark.asyncio
async def test_native_public_preview_is_bound_to_exact_actor_and_settings(native):
    alice, bob = native.clients["alice"], native.clients["bob"]
    await me(alice)
    await me(bob)
    proposed = profile(discoverable=True)
    preview = await alice.post(f"{PREFIX}/profile/preview", json=proposed)
    assert preview.status_code == 200
    token = preview.json()["preview_token"]
    assert len(preview.json()["public_profile"]["history"]) == 5
    assert preview.json()["friend_data"]["history"] == []
    assert (await bob.get(f"{PREFIX}/directory")).json()["users"] == []
    publication = proposed | {"public_consent": True, "preview_token": token}
    assert (await bob.patch(f"{PREFIX}/profile", json=publication)).status_code == 400
    assert (
        await alice.patch(f"{PREFIX}/profile", json=publication | {"share_history": True})
    ).status_code == 400
    assert (await alice.patch(f"{PREFIX}/profile", json=publication)).status_code == 200
    public = (await bob.get(f"{PREFIX}/directory")).json()["users"]
    assert len(public) == 1 and len(public[0]["history"]) == 5
    assert public[0]["id"] == (await me(alice))["id"]


@pytest.mark.asyncio
async def test_native_browser_collection_snapshot_is_sanitized_and_bound_to_current_user(native):
    relation_id = await friendship(native)
    alice, bob = native.clients["alice"], native.clients["bob"]
    first, second = await me(alice), await me(bob)
    listing = (await alice.get(f"{PREFIX}/collections")).json()
    assert listing["browser_required"] is True and listing["collections"] == []
    posted = await alice.post(
        f"{PREFIX}/collections/11/share",
        json={
            "member_id": 2,
            "user_id": second["id"],
            "collection": {
                "id": 11,
                "name": "本人选择的片单",
                "member_id": 2,
                "owner_token": "secret-host-token",
                "items": [
                    {
                        "title": "手动发布的影片",
                        "kind": "movie",
                        "path": "/private/film.mkv",
                        "token": "secret-host-token",
                    }
                ],
            },
        },
    )
    assert posted.status_code == 200
    saved = native.app.state.store.one("SELECT * FROM collections WHERE source_id=11")
    assert saved["user_id"] == first["id"]
    assert "secret-host-token" not in saved["data"] and "/private/" not in saved["data"]
    assert (await bob.get(f"{PREFIX}/collections")).json()["collections"] == []
    shared = (await bob.get(f"{PREFIX}/friends/{relation_id}")).json()["data"]["collections"]
    assert len(shared) == 1 and shared[0]["items"][0]["title"] == "手动发布的影片"
    mismatch = await alice.post(
        f"{PREFIX}/collections/12/share",
        json={
            "collection": {"id": 11, "name": "编号不匹配", "items": []},
        },
    )
    assert mismatch.status_code == 422
    assert all(op in {"members.list", "playback.history"} for op, _ in native.ops.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [b"not-json", b"{", b"[]", b'"text"', b"null"])
async def test_native_collection_bad_json_is_rejected_without_server_error(native, body):
    response = await native.clients["alice"].post(
        f"{PREFIX}/collections/11/share", content=body, headers={"content-type": "application/json"}
    )
    assert response.status_code == 422
    assert native.app.state.store.all("SELECT * FROM collections") == []


@pytest.mark.asyncio
async def test_native_write_origin_and_body_size_boundaries(native):
    alice = native.clients["alice"]
    no_header = await alice.patch(
        f"{PREFIX}/profile", json=profile(), headers={"x-friends-request": ""}
    )
    assert no_header.status_code == 403
    foreign = await alice.patch(
        f"{PREFIX}/profile", json=profile(), headers={"origin": "https://untrusted.test"}
    )
    assert foreign.status_code == 403
    huge = await alice.post(f"{PREFIX}/collections/11/share", content=b"x" * 1_000_001)
    assert huge.status_code == 413
    assert native.app.state.store.all("SELECT * FROM collections") == []


@pytest.mark.asyncio
async def test_native_write_works_when_host_is_deployed_under_a_url_prefix(native):
    # FastAPI root_path 出现在 request.base_url 中，但浏览器 Origin 始终只有源地址。
    native.host.root_path = "/movieclaw"
    response = await native.clients["alice"].patch(f"{PREFIX}/profile", json=profile())
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_member_recreated_during_host_history_read_cannot_publish_new_person_data(native):
    relation_id = await friendship(native)
    alice, bob = native.clients["alice"], native.clients["bob"]
    assert (
        await alice.patch(f"{PREFIX}/profile", json=profile(share_history=True))
    ).status_code == 200
    original_call = native.ops.call

    async def replaced_during_read(operation, arguments):
        result = await original_call(operation, arguments)
        if operation == "playback.history":
            # 宿主读取期间旧成员被删除，同一数字 ID 已由新创建的账号使用。
            native.ops.members[0]["created_at"] = "2026-10-20T00:00:00Z"
            result["entries"][0]["media"]["title"] = "新成员不该外发的影片"
        return result

    native.ops.call = replaced_during_read
    response = await bob.get(f"{PREFIX}/friends/{relation_id}")
    assert response.status_code in (401, 403)
    assert "新成员不该外发的影片" not in response.text
    assert (
        native.app.state.store.one("SELECT cache FROM relationships WHERE id=?", (relation_id,))[
            "cache"
        ]
        is None
    )
