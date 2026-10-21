"""跨插件只交换经授权的投影；不转发任何 MovieClaw 凭据。"""

import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit

import httpx

from .config import origin_url


class PeerError(Exception):
    """对端不可达或协议不兼容，向页面提供不含地址、令牌的中文诊断。"""


class PeerClient:
    """连接邀请指定的对端，禁止重定向，并把 DNS 结果固定到本次连接。

    默认仅允许公网 HTTPS。管理员可显式开放私网以部署局域网实例。
    DNS 解析与实际连接使用同一个 IP，防止检查后重新解析到内网的地址重绑定。
    测试注入 transport 时不进行真实 DNS 或网络连接。
    """

    def __init__(self, allow_private: bool = False, transport=None, transport_factory=None):
        self.allow_private = allow_private
        self.transport = transport
        self.transport_factory = transport_factory

    def validate_endpoint(self, origin: str, path: str, endpoint: str) -> str:
        """签名能力只能指向对方同源、固定插件路径，不能借邀请探测其他接口。"""
        if not isinstance(endpoint, str) or len(endpoint) > 2048:
            raise PeerError("原生插件邀请缺少有效的签名接口")
        try:
            parsed = urlsplit(endpoint)
            endpoint_origin = origin_url(f"{parsed.scheme}://{parsed.netloc}")
        except ValueError as exc:
            raise PeerError("原生插件邀请的签名接口无效") from exc
        if (
            endpoint_origin != self.validate(origin)
            or parsed.fragment
            or parsed.path != "/api/v1/plugins/watch-friends" + path
            or not parsed.query
            or len(parsed.query) > 256
        ):
            raise PeerError("原生插件邀请的签名接口不匹配")
        from urllib.parse import parse_qs

        query = parse_qs(parsed.query, keep_blank_values=True)
        if set(query) - {"sig", "exp"} or len(query.get("sig", [])) != 1 or not query["sig"][0]:
            raise PeerError("原生插件邀请缺少宿主签名")
        return parsed.path + "?" + parsed.query

    def validate_endpoints(self, origin: str, endpoints: dict) -> None:
        paths = {"/federation/requests", "/federation/state", "/federation/data"}
        if (
            not isinstance(endpoints, dict)
            or not paths.issubset(endpoints)
            or set(endpoints) - paths - {"/federation/social"}
        ):
            raise PeerError("邀请码不属于兼容的原生映迹插件，请重新生成")
        for path in endpoints:
            self.validate_endpoint(origin, path, endpoints[path])

    def validate(self, origin: str) -> str:
        try:
            normalized = origin_url(origin)
        except ValueError as exc:
            raise PeerError("邀请码中的插件地址无效") from exc
        if not self.allow_private and urlsplit(normalized).scheme != "https":
            raise PeerError("跨服务器邀请需要 HTTPS；局域网部署请由管理员显式启用私网连接")
        return normalized

    async def request(
        self,
        origin: str,
        path: str,
        *,
        token: str = "",
        data=None,
        endpoint: str | None = None,
        require_signed: bool = False,
    ) -> dict:
        origin = self.validate(origin)
        if endpoint or require_signed:
            path = self.validate_endpoint(origin, path, endpoint)
        parsed = urlsplit(origin)
        headers = {"X-Watch-Friends-Token": token} if token else {}
        extensions = {}
        target = origin + path
        if self.transport is None:
            try:
                addresses = await asyncio.wait_for(
                    asyncio.to_thread(
                        socket.getaddrinfo,
                        parsed.hostname,
                        parsed.port or (443 if parsed.scheme == "https" else 80),
                        type=socket.SOCK_STREAM,
                    ),
                    timeout=5,
                )
                ips = [ipaddress.ip_address(item[4][0]) for item in addresses]
            except (OSError, ValueError, TimeoutError) as exc:
                raise PeerError("无法解析好友插件，请检查网络连接") from exc
            if not ips or (not self.allow_private and any(not ip.is_global for ip in ips)):
                raise PeerError("对端地址指向私有或保留网络，当前插件未允许连接")
            ip = ips[0]
            address = f"[{ip}]" if ip.version == 6 else str(ip)
            port = f":{parsed.port}" if parsed.port else ""
            target = f"{parsed.scheme}://{address}{port}{path}"
            headers["Host"] = parsed.netloc
            extensions["sni_hostname"] = parsed.hostname
        try:
            async with (
                httpx.AsyncClient(
                    transport=self.transport
                    or (self.transport_factory() if self.transport_factory else None),
                    timeout=8,
                    follow_redirects=False,
                    trust_env=False,
                ) as client,
                client.stream(
                    "POST" if data is not None else "GET",
                    target,
                    headers=headers,
                    json=data,
                    extensions=extensions,
                ) as response,
            ):
                if response.status_code != 200:
                    raise PeerError("好友插件拒绝了请求，关系可能已撤销或版本不兼容")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > 1_000_000:
                        raise PeerError("好友插件返回的数据过大")
                import json

                result = json.loads(body)
                if not isinstance(result, dict):
                    raise ValueError
                return result
        except (httpx.HTTPError, ValueError) as exc:
            raise PeerError("暂时无法连接好友插件，请稍后重试") from exc
