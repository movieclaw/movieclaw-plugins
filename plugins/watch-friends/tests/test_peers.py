"""跨服网络边界测试：走生产 DNS 分支，仅替换实际 HTTP 传输以避免外网请求。"""

import ipaddress
import socket

import httpx
import pytest
from movieclaw_friends import peers
from movieclaw_friends.config import origin_url
from movieclaw_friends.peers import PeerClient, PeerError


@pytest.mark.parametrize(
    "raw,canonical",
    [
        ("https://FRIEND.example:443/", "https://friend.example"),
        ("http://localhost:80", "http://localhost"),
        ("https://[2606:4700:4700::1111]:9443", "https://[2606:4700:4700::1111]:9443"),
        ("https://电影.example", "https://xn--p3t879b.example"),
    ],
)
def test_origin_matches_browser_canonical_form(raw, canonical):
    assert origin_url(raw) == canonical


def network(monkeypatch, handler, *, addresses=("93.184.216.34",), allow_private=False):
    """保留 PeerClient.transport=None，验证解析结果确实用于连接而非只做检查。"""
    dns_calls, requests, client_options = [], [], []
    original_client = httpx.AsyncClient

    def resolve(host, port, *, type):
        dns_calls.append((host, port, type))
        result = []
        for address in addresses:
            family = (
                socket.AF_INET6 if ipaddress.ip_address(address).version == 6 else socket.AF_INET
            )
            endpoint = (address, port, 0, 0) if family == socket.AF_INET6 else (address, port)
            result.append((family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", endpoint))
        return result

    def respond(request):
        requests.append(request)
        return handler(request)

    def client(**kwargs):
        client_options.append(dict(kwargs))
        assert kwargs["transport"] is None
        kwargs["transport"] = httpx.MockTransport(respond)
        return original_client(**kwargs)

    monkeypatch.setattr(peers.socket, "getaddrinfo", resolve)
    monkeypatch.setattr(peers.httpx, "AsyncClient", client)
    return PeerClient(allow_private=allow_private), dns_calls, requests, client_options


@pytest.mark.asyncio
@pytest.mark.parametrize("address", ["93.184.216.34", "2606:4700:4700::1111"])
async def test_dns_checked_once_and_connection_pinned_with_original_tls_name(monkeypatch, address):
    client, dns, requests, options = network(
        monkeypatch,
        lambda request: httpx.Response(200, json={"status": "accepted"}),
        addresses=(address,),
    )

    assert await client.request(
        "https://friend.example:9443", "/federation/state", token="relationship-only-secret"
    ) == {"status": "accepted"}
    assert dns == [("friend.example", 9443, socket.SOCK_STREAM)]
    assert len(requests) == 1
    request = requests[0]
    assert request.url.host == address
    assert request.url.port == 9443
    assert request.url.path == "/federation/state"
    assert request.headers["Host"] == "friend.example:9443"
    assert request.extensions["sni_hostname"] == "friend.example"
    assert request.headers["X-Watch-Friends-Token"] == "relationship-only-secret"
    assert "Authorization" not in request.headers
    assert options[0]["follow_redirects"] is False
    assert options[0]["trust_env"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "addresses",
    [
        ("127.0.0.1",),
        ("10.1.2.3",),
        ("172.16.1.2",),
        ("192.168.1.2",),
        ("169.254.169.254",),
        ("100.64.0.1",),
        ("203.0.113.1",),
        ("::1",),
        ("fd00::1",),
        ("fe80::1",),
        ("::ffff:127.0.0.1",),
        ("93.184.216.34", "127.0.0.1"),
    ],
)
async def test_private_reserved_or_mixed_dns_answers_rejected_before_http(monkeypatch, addresses):
    client, dns, requests, options = network(
        monkeypatch,
        lambda request: pytest.fail("私网地址不应发出 HTTP 请求"),
        addresses=addresses,
    )
    with pytest.raises(PeerError, match="私有或保留网络"):
        await client.request("https://friend.example", "/federation/state", token="secret")
    assert len(dns) == 1
    assert requests == []
    assert options == []


@pytest.mark.asyncio
async def test_private_peer_requires_explicit_administrator_opt_in(monkeypatch):
    client, dns, requests, _ = network(
        monkeypatch,
        lambda request: httpx.Response(200, json={"ok": True}),
        addresses=("127.0.0.1",),
        allow_private=True,
    )
    assert await client.request("http://friend.local:8765", "/federation/state") == {"ok": True}
    assert dns == [("friend.local", 8765, socket.SOCK_STREAM)]
    assert requests[0].url.host == "127.0.0.1"
    assert requests[0].headers["Host"] == "friend.local:8765"


@pytest.mark.asyncio
async def test_plain_http_rejected_before_dns_when_private_peers_disabled(monkeypatch):
    client, dns, requests, _ = network(
        monkeypatch, lambda request: pytest.fail("不应连接未加密对端")
    )
    with pytest.raises(PeerError, match="需要 HTTPS"):
        await client.request("http://friend.example", "/federation/state")
    assert dns == requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
async def test_redirect_never_followed_and_location_not_reported(monkeypatch, status):
    client, dns, requests, _ = network(
        monkeypatch,
        lambda request: httpx.Response(
            status, headers={"Location": "http://127.0.0.1/private-token"}
        ),
    )
    with pytest.raises(PeerError, match="拒绝了请求") as exc:
        await client.request("https://friend.example", "/federation/state", token="secret")
    assert len(dns) == len(requests) == 1
    assert "127.0.0.1" not in str(exc.value)
    assert "private-token" not in str(exc.value)


class Chunks(httpx.AsyncByteStream):
    """使用分块响应检查上限在读取期间执行，超限后不继续消费后续数据。"""

    def __init__(self, values):
        self.values = values
        self.read_count = 0
        self.closed = False

    async def __aiter__(self):
        for value in self.values:
            self.read_count += 1
            yield value

    async def aclose(self):
        self.closed = True


@pytest.mark.asyncio
async def test_response_limit_stops_stream_before_remaining_chunks(monkeypatch):
    stream = Chunks([b"x" * 600_000, b"x" * 600_000, b"do-not-read"])
    client, _, _, _ = network(monkeypatch, lambda request: httpx.Response(200, stream=stream))
    with pytest.raises(PeerError, match="数据过大"):
        await client.request("https://friend.example", "/federation/data")
    assert stream.read_count == 2
    assert stream.closed is True


@pytest.mark.asyncio
async def test_response_exactly_at_limit_is_accepted(monkeypatch):
    prefix, suffix = b'{"value":"', b'"}'
    padding = 1_000_000 - len(prefix) - len(suffix)
    payload = prefix + b"x" * padding + suffix
    stream = Chunks([payload[:500_000], payload[500_000:]])
    client, _, _, _ = network(monkeypatch, lambda request: httpx.Response(200, stream=stream))
    result = await client.request("https://friend.example", "/federation/data")
    assert len(result["value"]) == padding
    assert stream.read_count == 2
    assert stream.closed is True


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [b"not-json-with-secret", b"[]", b"null"])
async def test_invalid_response_uses_safe_error_without_peer_body(monkeypatch, payload):
    client, _, _, _ = network(monkeypatch, lambda request: httpx.Response(200, content=payload))
    with pytest.raises(PeerError, match="暂时无法连接") as exc:
        await client.request("https://friend.example", "/federation/data", token="secret")
    assert "secret" not in str(exc.value)


@pytest.mark.asyncio
async def test_dns_failure_does_not_connect_or_echo_hostname(monkeypatch):
    client, _, requests, options = network(
        monkeypatch, lambda request: pytest.fail("DNS 失败不应继续请求")
    )

    def unavailable(*args, **kwargs):
        raise socket.gaierror("private-host-secret")

    monkeypatch.setattr(peers.socket, "getaddrinfo", unavailable)
    with pytest.raises(PeerError, match="无法解析好友插件") as exc:
        await client.request("https://friend.example", "/federation/state")
    assert "private-host-secret" not in str(exc.value)
    assert requests == options == []


@pytest.mark.asyncio
async def test_native_signed_endpoint_preserves_query_and_pins_connection(monkeypatch):
    client, dns, requests, _ = network(
        monkeypatch, lambda request: httpx.Response(200, json={"status": "accepted"})
    )
    path = "/federation/state"
    signed = (
        "https://friend.example:9443/api/v1/plugins/watch-friends/federation/state"
        "?exp=9999999999&sig=host-issued-signature"
    )
    assert await client.request(
        "https://friend.example:9443", path, endpoint=signed, require_signed=True,
        token="relationship-only-secret",
    ) == {"status": "accepted"}
    assert dns == [("friend.example", 9443, socket.SOCK_STREAM)]
    assert requests[0].url.host == "93.184.216.34"
    assert requests[0].url.path == "/api/v1/plugins/watch-friends/federation/state"
    assert requests[0].url.query == b"exp=9999999999&sig=host-issued-signature"
    assert requests[0].headers["X-Watch-Friends-Token"] == "relationship-only-secret"
    assert "authorization" not in requests[0].headers


@pytest.mark.asyncio
async def test_native_required_signature_cannot_fall_back_to_unsigned_path(monkeypatch):
    client, dns, requests, _ = network(
        monkeypatch, lambda request: pytest.fail("缺少签名能力时不能尝试未签名路由")
    )
    with pytest.raises(PeerError, match="签名接口"):
        await client.request(
            "https://friend.example", "/federation/data", require_signed=True,
        )
    assert dns == requests == []


@pytest.mark.parametrize("endpoint", [None, 1, "https://[invalid", "https://friend.example:bad/x"])
def test_native_malformed_endpoint_has_protocol_error(endpoint):
    with pytest.raises(PeerError, match="签名接口"):
        PeerClient().validate_endpoint("https://friend.example", "/federation/data", endpoint)
