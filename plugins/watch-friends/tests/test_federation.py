"""两个真实插件应用互相连接，验证双向同意、字段隔离与撤销后的读取边界。"""

import copy
from datetime import UTC, datetime

import httpx
import pytest
from movieclaw_friends.app import COOKIE, create_app
from movieclaw_friends.config import Settings
from movieclaw_friends.upstream import UpstreamError

HEADERS = {"X-Friends-Request": "1"}


class Source:
    def __init__(self, name):
        self.name = name
        self.valid = True

    async def identity(self, token):
        if token != f"secret-{self.name}" or not self.valid:
            raise UpstreamError("登录令牌已失效", 401)
        return {"username": self.name, "nickname": self.name, "role": "member", "member_id": 1}

    async def snapshot(self, token, member_id, share_current, share_history):
        await self.identity(token)
        record = {
            "id": 1,
            "media": {
                "tmdb_id": 550,
                "title": f"{self.name}的电影",
                "path": "/private/movie.mkv",
                "stream_url": "secret-url",
            },
            "started_at": datetime.now(UTC).isoformat(),
            "ended_at": None,
            "device_id": "secret-device",
            "token": "secret-token",
        }
        past = copy.deepcopy(record)
        past["ended_at"] = datetime.now(UTC).isoformat()
        return {
            "current": record if share_current else None,
            "history": [past] if share_history else [],
            "sharing": {"current": share_current, "history": share_history},
            "observed_at": datetime.now(UTC).isoformat(),
        }

    async def collections(self, token):
        return []

    async def collection(self, token, id):
        raise UpstreamError("片单不存在", 404)


@pytest.fixture
async def pair(tmp_path):
    apps = {}

    async def route(request):
        app = apps.get(request.url.host)
        if app is None:
            return httpx.Response(404, json={"detail": "unknown instance"})
        return await httpx.ASGITransport(app=app).handle_async_request(request)

    transport = httpx.MockTransport(route)
    sources = {}
    for host, name in (("a.test", "alice"), ("b.test", "bob")):
        sources[name] = Source(name)
        apps[host] = create_app(
            Settings(
                "http://movieclaw",
                f"http://{host}",
                tmp_path / name,
                allow_private_peers=True,
                demo_enabled=False,
            ),
            upstream=sources[name],
            peer_transport=transport,
        )
    async with (
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=apps["a.test"]),
            base_url="http://a.test",
            headers=HEADERS,
        ) as alice,
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=apps["b.test"]),
            base_url="http://b.test",
            headers=HEADERS,
        ) as bob,
    ):
        for client, name in ((alice, "alice"), (bob, "bob")):
            response = await client.post("/api/login", json={"token": f"secret-{name}"})
            assert response.status_code == 200, response.text
            response = await client.patch(
                "/api/profile",
                json={
                    "nickname": name,
                    "share_current": True,
                    "share_history": True,
                },
            )
            assert response.status_code == 200
        yield alice, bob, apps, sources
    for app in apps.values():
        app.state.store.close()


async def request_pair(alice, bob):
    invitation = (await bob.get("/api/me")).json()["user"]["invite_code"]
    response = await alice.post(
        "/api/requests",
        json={
            "invite_code": invitation,
            "address_consent": True,
        },
    )
    assert response.status_code == 200, response.text
    local_id = response.json()["id"]
    incoming = (await bob.get("/api/requests")).json()["incoming"]
    assert len(incoming) == 1
    return local_id, incoming[0]["id"]


async def test_two_instances_require_both_consents_and_sanitize_data(pair):
    alice, bob, apps, _ = pair
    invitation = (await bob.get("/api/me")).json()["user"]["invite_code"]
    assert (await alice.post("/api/requests", json={"invite_code": invitation})).status_code == 400
    a_id, b_id = await request_pair(alice, bob)
    assert (await alice.get(f"/api/friends/{a_id}")).status_code == 403
    assert (await bob.get(f"/api/friends/{b_id}")).status_code == 403
    assert (await bob.post(f"/api/requests/{b_id}/accept", json={})).status_code == 400
    response = await bob.post(f"/api/requests/{b_id}/accept", json={"address_consent": True})
    assert response.status_code == 200

    # 接收方先访问：即使发起方还没轮询过，双方也应完成相互确认。
    response = await bob.get(f"/api/friends/{b_id}")
    assert response.status_code == 200, response.text
    assert response.json()["data"]["current"]["media"]["title"] == "alice的电影"
    response = await alice.get(f"/api/friends/{a_id}")
    assert response.status_code == 200, response.text
    assert len(response.json()["data"]["history"]) == 1
    for forbidden in ("/private/", "stream_url", "secret-token", "secret-device", "peer_origin"):
        assert forbidden not in response.text
    assert "origin" not in response.json()["friend"]
    assert apps["a.test"].state.store.one("SELECT cache FROM relationships WHERE id=?", (a_id,))[
        "cache"
    ]


async def test_privacy_change_and_either_side_revocation_stop_remote_access(pair):
    alice, bob, apps, _ = pair
    a_id, b_id = await request_pair(alice, bob)
    await bob.post(f"/api/requests/{b_id}/accept", json={"address_consent": True})
    assert (await alice.get(f"/api/friends/{a_id}")).status_code == 200
    await bob.patch(
        "/api/profile",
        json={
            "nickname": "bob",
            "share_current": True,
            "share_history": False,
        },
    )
    data = (await alice.get(f"/api/friends/{a_id}")).json()["data"]
    assert data["current"]
    assert data["history"] == []
    row = apps["a.test"].state.store.one("SELECT * FROM relationships WHERE id=?", (a_id,))
    peer_token = apps["a.test"].state.store.decrypt(row["token"])
    await alice.delete(f"/api/friends/{a_id}")
    response = await bob.get("/federation/data", headers={"Authorization": f"Bearer {peer_token}"})
    assert response.status_code == 403
    assert (await bob.get(f"/api/friends/{b_id}")).status_code == 403
    assert (
        apps["b.test"].state.store.one("SELECT cache FROM relationships WHERE id=?", (b_id,))[
            "cache"
        ]
        is None
    )


async def test_invalid_peer_credentials_and_spoofed_instance(pair):
    alice, bob, _, _ = pair
    assert (await alice.get("/federation/data")).status_code == 401
    assert (
        await alice.get("/federation/state", headers={"Authorization": "Bearer bogus"})
    ).status_code == 401
    invitation = (await bob.get("/api/me")).json()["user"]["invite_code"]
    import base64
    import json

    payload = json.loads(
        base64.urlsafe_b64decode(invitation[5:] + "=" * (-len(invitation[5:]) % 4))
    )
    response = await bob.post(
        "/federation/requests",
        json={
            "invite_token": payload["invite"],
            "origin": "http://a.test",
            "user_id": "fake",
            "request_id": "fake",
            "token": "x" * 43,
        },
    )
    assert response.status_code == 502
    assert (await bob.get("/api/requests")).json()["incoming"] == []


async def test_revoked_upstream_identity_cannot_use_stale_plugin_cookie(pair):
    alice, bob, _, sources = pair
    a_id, b_id = await request_pair(alice, bob)
    await bob.post(f"/api/requests/{b_id}/accept", json={"address_consent": True})
    assert COOKIE in alice.cookies
    sources["alice"].valid = False
    assert (await alice.get(f"/api/friends/{a_id}")).status_code == 401
    assert (await alice.get("/api/directory")).status_code == 401
    # 隐私撤回不依赖宿主在线，原插件会话仍可停止本地授权。
    assert (await alice.patch("/api/profile", json={"nickname": "alice"})).status_code == 200


async def test_browser_api_does_not_disclose_server_addresses(pair):
    alice, _, _, _ = pair
    for path in ("/api/info", "/api/me"):
        result = (await alice.get(path)).json()
        assert "upstream_url" not in result
        assert "public_url" not in result


async def test_invalid_invite_is_rejected_without_outbound_connection(pair):
    alice, _, _, _ = pair
    for code in ("wrong", "mcf1.{}", "mcf1.bm90LWpzb24"):
        response = await alice.post(
            "/api/requests",
            json={
                "invite_code": code,
                "address_consent": True,
            },
        )
        assert response.status_code == 400, response.text
