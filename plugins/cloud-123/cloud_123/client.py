"""P123Client 请求适配；请求经过宿主出口，不自行重试。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from functools import partial
import re
from typing import Any

import httpx

from movieclaw_sdk import drives as dv
from movieclaw_sdk import net


def _client(token: str):
    # 安装前的模块发现不应要求第三方依赖已经就绪。
    from p123client import P123Client

    try:
        return P123Client(token=token, check_for_relogin=False)
    except (ValueError, TypeError):
        raise dv.Conflict("123 Token 格式无法解析，请重新登录") from None


async def user_info(token: str) -> dict[str, Any]:
    return (await _call(_client(token).user_info, "账号查询"))["data"]


async def list_page(token: str, parent_id: str, cursor: int) -> dict[str, Any]:
    method = partial(_client(token).fs_list, {
        "parentFileId": parent_id, "limit": 100, "next": cursor,
        "orderBy": "file_id", "orderDirection": "asc", "driveId": 0,
        "trashed": False, "inDirectSpace": False, "event": "homeListFile",
    })
    return (await _call(method, "目录查询"))["data"]


async def mkdir(token: str, parent_id: str, name: str) -> None:
    method = partial(_client(token).fs_mkdir, name, parent_id=parent_id, duplicate=0)
    await _call(method, "目录创建")


async def file_info(token: str, remote_id: str) -> dict[str, Any]:
    return (await _call(partial(_client(token).fs_info, remote_id), "文件详情"))["data"]


async def download_info(token: str, info: dict[str, Any], *, user_agent: str | None = None) -> dict[str, Any]:
    headers = {} if user_agent is None else {"user-agent": user_agent}
    return (await _call(partial(_client(token).download_info, info, headers=headers), "原文件取链"))["data"]


async def trash(token: str, remote_ids: list[str]) -> None:
    await _call(partial(_client(token).fs_trash, remote_ids, event="intoRecycle"), "移入回收站")


async def upload_request(token: str, payload: dict[str, Any], *, before_send: Callable[[], Awaitable[None]]) -> dict[str, Any]:
    return await _call(partial(_client(token).upload_request, payload), "上传初始化",
                       allowed_codes=(5060,), before_send=before_send)


async def upload_list(token: str, payload: dict[str, Any]) -> dict[str, Any]:
    return (await _call(partial(_client(token).upload_list, payload), "分片查询"))["data"]


async def upload_complete(token: str, payload: dict[str, Any], multipart: bool, *, before_send) -> dict[str, Any]:
    method = partial(_client(token).upload_complete, payload, is_multipart=multipart)
    return (await _call(method, "上传合并", before_send=before_send))["data"]


async def upload_url(token: str, payload: dict[str, Any], index: int, multipart: bool) -> dict[str, Any]:
    api = _client(token)
    if multipart:
        method = partial(api.upload_prepare, {**payload, "partNumberStart": index + 1, "partNumberEnd": index + 2})
    else:
        method = partial(api.upload_auth, dict(payload))
    return (await _call(method, "分片取链"))["data"]


def part_etag(value):
    if isinstance(value, str) and value.startswith('"') and value.endswith('"'):
        value = value[1:-1]
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{32}", value):
        raise dv.ApiChanged("123 分片 ETag 格式无效")
    return value.lower()


async def _send(http, request, *, before_send=None, stream=False):
    pace_token = None
    if before_send is not None:
        original = net.pacer.get()

        async def pace_then_prepare():
            if original is not None:
                await original()
            # SDK 出口放行后、真正发送前确认意图；预算拒绝不留下未知写入。
            await before_send()

        pace_token = net.pacer.set(pace_then_prepare)
    try:
        return await http.send(request, stream=stream)
    finally:
        if pace_token is not None:
            net.pacer.reset(pace_token)


async def put_part(url: str, data: bytes, digest: str, *, before_send) -> None:
    # 内容出口独立持有，不带 API Token/Cookie，不跟随预签名地址的重定向。
    http = httpx.AsyncClient(transport=net.http_transport("cloud-123"), trust_env=False,
                             follow_redirects=False, timeout=httpx.Timeout(20, connect=5))
    completed = False
    try:
        request = http.build_request("PUT", url, content=data)
        response = await _send(http, request, before_send=before_send, stream=True)
        verified = False
        try:
            if response.status_code == 429:
                raise dv.RateLimited("123 分片上传请求过于频繁，请稍后重试")
            if response.is_redirect:
                raise dv.ApiChanged("123 分片上传返回了未经验证的重定向")
            if not response.is_success:
                raise dv.Transient(f"123 分片上传暂时失败（HTTP {response.status_code}）")
            if part_etag(response.headers.get("etag")) != digest:
                raise dv.Conflict("123 PUT ETag 与本地分片 MD5 不一致")
            verified = True
        finally:
            try:
                await response.aclose()
            except Exception:
                if verified:
                    raise dv.Transient("123 分片响应关闭失败，请重试对账") from None
        completed = True
    except dv.DriveError:
        raise
    except Exception:
        raise dv.Transient("123 分片上传连接失败，请重试对账") from None
    finally:
        try:
            await http.aclose()
        except Exception:
            if completed:
                raise dv.Transient("123 分片连接关闭失败，请重试对账") from None


async def qr_generate() -> dict[str, Any]:
    from p123client import P123Client

    try:
        return (await _call(P123Client.login_qrcode_generate, "二维码生成"))["data"]
    except dv.AuthExpired:
        # 此请求不带凭据，401 不能证明用户的登录已失效。
        raise dv.Transient("123 二维码生成暂时不可用，请稍后重试") from None


async def qr_result(uni_id: str) -> dict[str, Any]:
    from p123client import P123Client

    try:
        return await _call(partial(P123Client.login_qrcode_result, uni_id), "扫码状态查询")
    except dv.AuthExpired:
        raise dv.Transient("123 扫码状态查询暂时不可用，请稍后重试") from None


async def _call(method: Callable[..., Awaitable[Any]], operation: str, *, allowed_codes: tuple[int, ...] = (),
                before_send: Callable[[], Awaitable[None]] | None = None) -> dict[str, Any]:
    # 每次调用独立持有客户端及出口，退出时关闭；不共享账号 headers 或 Cookie。
    http = httpx.AsyncClient(
        transport=net.http_transport("cloud-123"),
        timeout=httpx.Timeout(20.0, connect=5.0),
        follow_redirects=False,
        trust_env=False,
    )
    completed = False
    try:
        async def request(url: str, method: str = "GET", *, parse, **kwargs):
            request = http.build_request(method, url, **kwargs)
            response = await _send(http, request, before_send=before_send)
            if response.status_code == 401:
                raise dv.AuthExpired("123 登录已失效，请重新登录")
            if response.status_code == 429:
                raise dv.RateLimited("123 请求过于频繁，请稍后重试")
            if not response.is_success:
                raise dv.Transient(f"123 {operation}暂时失败（HTTP {response.status_code}）")
            try:
                return parse(response, response.content)
            except (ValueError, TypeError):
                raise dv.ApiChanged(f"123 {operation}返回了无法解析的响应") from None

        try:
            result = await method(async_=True, request=request)
        except (httpx.RequestError, OSError):
            # 不把带 Token、请求头或原始响应的第三方异常交给宿主展示。
            raise dv.Transient(f"123 {operation}连接失败，请稍后重试") from None

        if not isinstance(result, dict) or type(result.get("code")) is not int:
            raise dv.ApiChanged(f"123 {operation}缺少有效状态码")
        code = result["code"]
        if code == 401:
            raise dv.AuthExpired("123 登录已失效，请重新登录")
        if code == 429:
            raise dv.RateLimited("123 请求过于频繁，请稍后重试")
        if code == 5066:
            raise dv.NotFound("123 文件或目录不存在")
        if code not in (0, 200, *allowed_codes):
            # P123Client.check_response 会把 code=1 也归为认证错误，这里不沿用。
            raise dv.Transient(f"123 {operation}暂时失败（业务码 {code}）")
        if not isinstance(result.get("data"), dict):
            raise dv.ApiChanged(f"123 {operation}缺少有效数据")
        completed = True
        return result
    finally:
        try:
            await http.aclose()
        except Exception:
            # 仅隔离关闭异常，保留请求/响应异常和取消；关闭期间的新取消也继续传播。
            if completed:
                raise dv.Transient(f"123 {operation}连接关闭失败，请稍后重试") from None
