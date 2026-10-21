"""原生双实例联邦：宿主负责登录与公开区验签，插件再校验关系授权。"""

import base64
import hashlib
import hmac
import json
import time
from datetime import UTC, datetime
from types import SimpleNamespace
from urllib.parse import parse_qsl, urlencode

import httpx
import pytest
from movieclaw_friends.app import create_app
from movieclaw_friends.config import Settings
from movieclaw_friends.native_source import NativeSource, account_key
from starlette.responses import JSONResponse

PREFIX = "/api/v1/plugins/watch-friends"
FEDERATION = ("/federation/requests", "/federation/state", "/federation/data")
CREATED = datetime(2026, 1, 1, tzinfo=UTC)


def invitation_payload(code):
    return json.loads(base64.urlsafe_b64decode(code[5:] + "=" * (-len(code[5:]) % 4)))


def invitation_code(payload):
    return "mcf1." + base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")


class ReadOnlyOps:
    """仅提供原生数据源获准的两个读取操作，所有非预期操作直接使测试失败。"""

    def __init__(self, name):
        self.name = name
        self.calls = []

    async def call(self, operation, arguments):
        self.calls.append((operation, arguments))
        if operation == "members.list":
            assert arguments == {}
            return [{
                "id": 1, "username": self.name, "nickname": self.name,
                "created_at": CREATED.isoformat(), "status": "active",
            }]
        assert operation == "playback.history"
        assert arguments == {"member_id": 1, "scope": "all", "limit": 50}
        record = {
            "id": 1, "media": {
                "title": f"{self.name}的电影", "tmdb_id": 550,
                "file_path": "/private/movie.mkv", "stream_url": "private-stream-token",
            },
            "started_at": datetime.now(UTC).isoformat(), "ended_at": None,
            "device_id": "private-device", "token": "private-host-token",
        }
        return {"entries": [record, dict(record, id=2, ended_at=datetime.now(UTC).isoformat())]}


class NativeHost:
    """只模拟正式挂载边界：HMAC绑定完整path/query，成员身份由宿主注入。

    这里故意不把朋友请求当作成员会话；公开区通过宿主签名后，仍须通过插件的
    每关系凭据与双向同意。重写到子应用的内部路径，等价于原生包装器分发请求。
    """

    def __init__(self, origin, name):
        self.origin = origin
        self.name = name
        self.key = f"test-host-signing-key-{name}".encode()
        self.user_id = f"native-{name}"
        self.app = None
        self.endpoints = {path: self.signed(path) for path in FEDERATION}

    def signature(self, path, params):
        query = "&".join(f"{key}={value}" for key, value in sorted(params) if key != "sig")
        digest = hmac.new(self.key, f"{path}?{query}".encode(), hashlib.sha256).digest()
        return base64.urlsafe_b64encode(digest).decode().rstrip("=")

    def signed(self, path, *, expires=None):
        full = PREFIX + path
        params = [] if expires is None else [("exp", str(expires))]
        params.append(("sig", self.signature(full, params)))
        return self.origin + full + "?" + urlencode(params)

    async def __call__(self, scope, receive, send):
        path = scope["path"]
        if not path.startswith(PREFIX + "/"):
            await JSONResponse({"detail": "没有这个地址"}, 404)(scope, receive, send)
            return
        relative = path[len(PREFIX):]
        state = dict(scope.get("state", {}))
        if relative.startswith("/federation/"):
            params = parse_qsl(scope.get("query_string", b"").decode(), keep_blank_values=True)
            values = dict(params)
            exp = values.get("exp")
            if not hmac.compare_digest(values.get("sig", ""), self.signature(path, params)) or (
                exp is not None and (not exp.isdigit() or int(exp) < time.time())
            ):
                await JSONResponse({"detail": "链接无效"}, 404)(scope, receive, send)
                return
        elif relative.startswith("/api/"):
            headers = dict(scope.get("headers", []))
            if headers.get(b"x-test-host-user") != self.name.encode():
                await JSONResponse({"detail": "请先登录 MovieClaw"}, 401)(scope, receive, send)
                return
            state["friends_user"] = self.app.state.store.one(
                "SELECT * FROM users WHERE id=?", (self.user_id,)
            )
        elif relative == "/web/":
            relative = "/"
        else:
            await JSONResponse({"detail": "没有这个地址"}, 404)(scope, receive, send)
            return
        await self.app(
            {**scope, "path": relative, "raw_path": relative.encode(), "state": state},
            receive, send,
        )


@pytest.fixture
async def native_pair(tmp_path):
    hosts = {name: NativeHost(f"https://{name}.test", name) for name in ("alice", "bob")}
    wire = []

    async def connect(request):
        wire.append(request)
        name = request.url.host.removesuffix(".test")
        assert name in hosts, "不应连接邀请端点以外的服务器"
        return await httpx.ASGITransport(app=hosts[name]).handle_async_request(request)

    for name, host in hosts.items():
        host.ops = ReadOnlyOps(name)
        host.app = create_app(
            Settings(
                "http://host-not-contacted", host.origin, tmp_path / name,
                native=True, demo_enabled=False, federation_endpoints=host.endpoints,
                network_endpoint=host.signed("/federation/directory"),
            ),
            upstream=NativeSource(host.ops), peer_transport=httpx.MockTransport(connect),
        )
        principal = SimpleNamespace(
            kind="member", member_id=1, member=SimpleNamespace(created_at=CREATED), plugin=None,
        )
        account = account_key(principal)
        store = host.app.state.store
        store.execute(
            "INSERT INTO users(id,username,nickname,member_id,role,token,invite_token) "
            "VALUES(?,?,?,?,?,?,?)",
            (host.user_id, account, name, 1, "member", store.encrypt(account), store.secret()),
        )
    async with (
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=hosts["alice"]), base_url=hosts["alice"].origin,
            headers={"X-Test-Host-User": "alice", "X-Friends-Request": "1"},
        ) as alice,
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=hosts["bob"]), base_url=hosts["bob"].origin,
            headers={"X-Test-Host-User": "bob", "X-Friends-Request": "1"},
        ) as bob,
    ):
        for client, name in ((alice, "alice"), (bob, "bob")):
            response = await client.patch(PREFIX + "/api/profile", json={
                "nickname": name, "share_current": True, "share_history": True,
            })
            assert response.status_code == 200, response.text
        yield alice, bob, hosts, wire
    for host in hosts.values():
        host.app.state.store.close()


async def request_pair(alice, bob):
    invitation = (await bob.get(PREFIX + "/api/me")).json()["user"]["invite_code"]
    response = await alice.post(PREFIX + "/api/requests", json={
        "invite_code": invitation, "address_consent": True,
    })
    assert response.status_code == 200, response.text
    incoming = (await bob.get(PREFIX + "/api/requests")).json()["incoming"]
    assert len(incoming) == 1
    return response.json()["id"], incoming[0]["id"]


async def accept_pair(alice, bob):
    a_id, b_id = await request_pair(alice, bob)
    response = await bob.post(PREFIX + f"/api/requests/{b_id}/accept", json={
        "address_consent": True,
    })
    assert response.status_code == 200, response.text
    return a_id, b_id


async def test_native_signed_federation_requires_both_users_consent(native_pair):
    alice, bob, hosts, _ = native_pair
    invite = (await bob.get(PREFIX + "/api/me")).json()["user"]["invite_code"]
    payload = invitation_payload(invite)
    assert payload["endpoints"] == hosts["bob"].endpoints
    response = await alice.post(PREFIX + "/api/requests", json={"invite_code": invite})
    assert response.status_code == 400
    a_id, b_id = await request_pair(alice, bob)
    for client, relation_id in ((alice, a_id), (bob, b_id)):
        assert (await client.get(PREFIX + f"/api/friends/{relation_id}")).status_code == 403
    assert (await bob.post(PREFIX + f"/api/requests/{b_id}/accept", json={})).status_code == 400
    assert (await alice.post(
        PREFIX + f"/api/requests/{a_id}/accept", json={"address_consent": True}
    )).status_code == 409
    assert (await bob.post(
        PREFIX + f"/api/requests/{b_id}/accept", json={"address_consent": True}
    )).status_code == 200
    # 接收方先读取，让发起方通过正式签名state链接完成pending -> accepted。
    response = await bob.get(PREFIX + f"/api/friends/{b_id}")
    assert response.status_code == 200, response.text
    assert response.json()["data"]["current"]["media"]["title"] == "alice的电影"
    response = await alice.get(PREFIX + f"/api/friends/{a_id}")
    assert response.status_code == 200, response.text
    assert len(response.json()["data"]["history"]) == 1
    for private in ("file_path", "stream_url", "private-host-token", "private-device"):
        assert private not in response.text


@pytest.mark.parametrize("path", FEDERATION)
async def test_native_unsigned_or_tampered_public_route_is_404(native_pair, path):
    alice, _, hosts, _ = native_pair
    url = hosts["alice"].origin + PREFIX + path
    if path.endswith("requests"):
        response = await alice.post(url, json={})
    else:
        response = await alice.get(url)
    assert response.status_code == 404
    wrong = hosts["alice"].endpoints[path] + "&exp=9999999999"
    assert (await alice.get(wrong)).status_code == 404
    expired = hosts["alice"].signed(path, expires=int(time.time()) - 1)
    assert (await alice.get(expired)).status_code == 404


async def test_native_relationship_secret_uses_custom_header_without_host_credentials(native_pair):
    alice, bob, hosts, wire = native_pair
    a_id, _ = await accept_pair(alice, bob)
    assert (await alice.get(PREFIX + f"/api/friends/{a_id}")).status_code == 200
    assert wire
    for request in wire:
        assert "authorization" not in request.headers
        assert "cookie" not in request.headers
        assert "sig=" in str(request.url)
        if request.url.path.endswith(("/state", "/data")):
            assert len(request.headers["X-Watch-Friends-Token"]) >= 32
        assert "member:1:" not in request.content.decode()
    row = hosts["alice"].app.state.store.one(
        "SELECT * FROM relationships WHERE id=?", (a_id,)
    )
    token = hosts["alice"].app.state.store.decrypt(row["token"])
    url = hosts["bob"].endpoints["/federation/data"]
    assert (await bob.get(url)).status_code == 401
    assert (await bob.get(url, headers={"X-Watch-Friends-Token": "bogus"})).status_code == 401
    assert (await bob.get(url, headers={"X-Watch-Friends-Token": token})).status_code == 200
    # 签名capability只存在后台关系记录与不透明邀请码中，不进好友展示结果。
    stored = row["peer_endpoints"]
    assert "https://" not in stored
    assert json.loads(hosts["alice"].app.state.store.decrypt(stored)) == hosts["bob"].endpoints


@pytest.mark.parametrize("mutation", ["other_origin", "other_path", "no_signature", "missing"])
async def test_native_invitation_rejects_unrelated_or_unsigned_endpoints(native_pair, mutation):
    alice, bob, hosts, wire = native_pair
    invite = (await bob.get(PREFIX + "/api/me")).json()["user"]["invite_code"]
    payload = invitation_payload(invite)
    endpoints = payload["endpoints"]
    if mutation == "other_origin":
        endpoints["/federation/data"] = endpoints["/federation/data"].replace(
            "https://bob.test", "https://unrelated.test"
        )
    elif mutation == "other_path":
        endpoints["/federation/data"] = "https://bob.test/api/v1/auth/me?sig=borrowed"
    elif mutation == "no_signature":
        endpoints["/federation/data"] = endpoints["/federation/data"].split("?", 1)[0]
    else:
        endpoints.pop("/federation/data")
    response = await alice.post(PREFIX + "/api/requests", json={
        "invite_code": invitation_code(payload), "address_consent": True,
    })
    assert response.status_code in {400, 502}, response.text
    assert wire == []
    for host in hosts.values():
        assert host.app.state.store.all("SELECT id FROM relationships") == []


async def test_native_incoming_rejects_cross_origin_sender_capability(native_pair):
    _, bob, hosts, wire = native_pair
    payload = invitation_payload((await bob.get(PREFIX + "/api/me")).json()["user"]["invite_code"])
    endpoints = dict(hosts["alice"].endpoints)
    endpoints["/federation/state"] = endpoints["/federation/state"].replace(
        "https://alice.test", "https://unrelated.test"
    )
    response = await bob.post(hosts["bob"].endpoints["/federation/requests"], json={
        "invite_token": payload["invite"], "request_id": "untrusted-request",
        "origin": hosts["alice"].origin, "user_id": hosts["alice"].user_id,
        "token": "untrusted-relation-secret" * 2, "endpoints": endpoints,
    })
    assert response.status_code in {400, 502}, response.text
    assert wire == []
    assert hosts["bob"].app.state.store.all("SELECT id FROM relationships") == []


@pytest.mark.parametrize("revoker", ["alice", "bob"])
async def test_native_either_party_can_revoke_even_with_signed_urls(native_pair, revoker):
    alice, bob, hosts, _ = native_pair
    a_id, b_id = await accept_pair(alice, bob)
    for client, relation_id in ((alice, a_id), (bob, b_id)):
        assert (await client.get(PREFIX + f"/api/friends/{relation_id}")).status_code == 200
    selected, relation_id = (alice, a_id) if revoker == "alice" else (bob, b_id)
    assert (await selected.delete(PREFIX + f"/api/friends/{relation_id}")).status_code == 200
    for client, relation_id in ((alice, a_id), (bob, b_id)):
        assert (await client.get(PREFIX + f"/api/friends/{relation_id}")).status_code == 403
    row = hosts["alice"].app.state.store.one("SELECT * FROM relationships WHERE id=?", (a_id,))
    token = hosts["alice"].app.state.store.decrypt(row["token"])
    response = await bob.get(hosts["bob"].endpoints["/federation/state"], headers={
        "X-Watch-Friends-Token": token,
    })
    assert response.status_code == 200
    assert set(response.json()) == {"status", "request_id"}
    assert (await bob.get(hosts["bob"].endpoints["/federation/data"], headers={
        "X-Watch-Friends-Token": token,
    })).status_code == 403
    for name, relation_id in (("alice", a_id), ("bob", b_id)):
        assert hosts[name].app.state.store.one(
            "SELECT cache FROM relationships WHERE id=?", (relation_id,)
        )["cache"] is None


async def test_native_browser_display_omits_addresses_and_signed_capabilities(native_pair):
    alice, bob, hosts, _ = native_pair
    a_id, _ = await accept_pair(alice, bob)
    paths = ("/api/info", "/api/me", "/api/requests", "/api/friends", f"/api/friends/{a_id}")
    for path in paths:
        response = await alice.get(PREFIX + path)
        assert response.status_code == 200, response.text
        for hidden in ("https://alice.test", "https://bob.test", "peer_endpoints", "peer_origin"):
            assert hidden not in response.text
        assert "sig=" not in response.text
    page = await alice.get(PREFIX + "/web/")
    assert page.status_code == 200
    assert "alice.test" not in page.text and "bob.test" not in page.text
    assert "host-not-contacted" not in page.text


async def test_native_missing_host_identity_cannot_reuse_browser_supplied_user_id(native_pair):
    alice, _, _, _ = native_pair
    response = await alice.get(PREFIX + "/api/me", headers={"X-Test-Host-User": "forged"})
    assert response.status_code == 401
