"""p123client 的正式 request 注入点；不启用其会话、重试或磁盘 token 功能。"""
from contextlib import asynccontextmanager
import json
import logging
from urllib.parse import urlsplit

import httpx
from movieclaw_sdk.drives import (
    ApiChanged, AuthExpired, Conflict, NotFound, QuotaExceeded,
    RateLimited, RiskControl, Transient,
)
from movieclaw_sdk.net import http_transport

# jdzjy/p123client@5170035 的服务地址，通过 upstream 的 base_url 参数使用。
BASE_URL = "https://yun.123pan.com/b"
LOGIN_URL = "https://login.123pan.com"


def checked(body, *, allow_empty=False):
    if not isinstance(body, dict) or type(body.get("code")) is not int:
        raise ApiChanged("123 响应缺少有效状态码")
    code = body["code"]
    if code in (0, 200):
        if allow_empty and body.get("data") is None:
            return {}
        if not isinstance(body.get("data"), dict):
            raise ApiChanged("123 响应数据格式变化")
        return body["data"]
    # 服务文本只用于分类，绝不将其包含在异常或日志中。
    message = str(body.get("message", "")).lower()
    if any(w in message for w in ("captcha", "验证码", "人机验证", "风控")):
        raise RiskControl("123 要求人工验证；请在官方客户端完成验证后重新提交登录")
    if code == 429:
        raise RateLimited("123 请求频率受限")
    if code == 401:
        raise AuthExpired("123 登录凭据失效，请重新登录")
    if code == 5066:
        raise NotFound("123 文件或目录不存在")
    if any(w in message for w in ("空间不足", "容量不足")):
        raise QuotaExceeded("123 云盘空间不足")
    if any(w in message for w in ("同名", "已存在")):
        raise Conflict("123 已有同名项目，请检查内容")
    if code == 1 or code >= 500 and code < 600:
        raise Transient("123 服务暂时不可用")
    raise ApiChanged("123 返回未识别的业务状态；需要核对接口")


def secure_url(value):
    if not isinstance(value, str):
        raise ApiChanged("123 未返回有效链接")
    try:
        parts = urlsplit(value)
        if parts.scheme != "https" or not parts.hostname or parts.username or parts.password or parts.fragment:
            raise ValueError
    except ValueError:
        raise ApiChanged("123 链接格式不受支持") from None
    return value


class Client:
    def __init__(self, transport=None):
        # Runner enables INFO. HTTPX logs complete signed URLs at INFO; its
        # private-process diagnostics must not expose credentials or signatures.
        self._log_levels = [(logging.getLogger(name), logging.getLogger(name).level)
                            for name in ("httpx", "httpcore")]
        for logger, _ in self._log_levels:
            logger.setLevel(logging.CRITICAL + 1)
        self.http = httpx.AsyncClient(
            transport=transport if transport is not None else http_transport("cloud-123-personal"),
            timeout=httpx.Timeout(60, connect=10), follow_redirects=False,
        )

    async def aclose(self):
        try:
            await self.http.aclose()
        finally:
            for logger, level in self._log_levels:
                logger.setLevel(level)
            self._log_levels = []

    async def request(self, url, method="GET", parse=None, **kwargs):
        kwargs.pop("async_", None)
        # All library requests use the same SDK-metered, TLS-verifying transport.
        try:
            request = self.http.build_request(method, url, **kwargs)
            request.headers.pop("cookie", None)
            response = await self.http.send(request)
            self.http.cookies.clear()
        except httpx.RequestError:
            raise Transient("123 网络请求失败，请稍后重试") from None
        try:
            status = response.status_code
            if status == 429:
                retry = response.headers.get("Retry-After", "")
                raise RateLimited("123 请求频率受限", float(retry) if retry.isdigit() else None)
            if status == 401:
                raise AuthExpired("123 登录凭据失效，请重新登录")
            if status == 403:
                raise RiskControl("123 拒绝请求，请在官方客户端检查访问限制")
            if status >= 500:
                raise Transient("123 服务暂时不可用")
            if status < 200 or status >= 300:
                raise ApiChanged("123 HTTP 响应不符合预期")
            if parse is ...:
                return bytes(response.content)
            try:
                return response.json()
            except (ValueError, json.JSONDecodeError):
                raise ApiChanged("123 返回了非 JSON 响应") from None
        finally:
            await response.aclose()

    @asynccontextmanager
    async def session(self, token):
        from p123client import P123Client
        client = P123Client(token=token, check_for_relogin=False)
        try:
            yield client
        finally:
            client.token = ""
            # Injected request means p123client never constructs a session.

    async def call(self, client, method, payload=None, **kwargs):
        fn = getattr(client, method)
        args = () if payload is None else (payload,)
        result = await fn(*args, async_=True, request=self.request, base_url=BASE_URL, **kwargs)
        return checked(result, allow_empty=method in ("fs_trash", "upload_complete"))

    async def sign_in(self, phone, password):
        from p123client import P123Client
        result = await P123Client.login_passport(
            {"passport": phone, "password": password, "remember": True},
            async_=True, request=self.request, base_url=LOGIN_URL,
        )
        return checked(result)

    async def put(self, url, data):
        # Separate request without account headers; never serialize a signed URL into state.
        try:
            return await self.request(secure_url(url), method="PUT", content=data, parse=...)
        except (AuthExpired, RiskControl):
            # Storage signatures expiring do not mean the personal account expired.
            raise Transient("上传签名被拒绝；重试时将重新申请该分片链接") from None
