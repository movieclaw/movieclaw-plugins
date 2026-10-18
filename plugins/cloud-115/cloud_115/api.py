# SPDX-License-Identifier: GPL-3.0-or-later
"""115 的 Cookie 接口。

接口路径、参数与加密方式参考 p115client（MIT），按 115 的改版跟进。这里只管「发请求、认响应、把错误归类」，
不管账号、限速和重试：限速与重试在宿主，插件抛 ``movieclaw_sdk.drives`` 的异常即可。
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from movieclaw_sdk import drives as dv

WEB_UA = "Mozilla/5.0"
APP_VERSION = "36.2.28"
UPLOAD_UA = f"Mozilla/5.0 115disk/{APP_VERSION} 115Browser/{APP_VERSION} 115wangpan_android/{APP_VERSION}"
#: 登录态只存这四个 Cookie
COOKIE_KEYS = ("UID", "CID", "SEID", "KID")
TIMEOUT = httpx.Timeout(30.0, connect=10.0)
#: 扫码状态是长轮询；宿主给 ``login_state`` 15 秒，这里 8 秒没变化就当「还在等」
QR_POLL_TIMEOUT = httpx.Timeout(8.0, connect=5.0)
#: 目录列举一页最多这么多条（115 的上限）
PAGE = 1150

#: 确认登录已失效的错误码（限流、风控、网络错误都不在这里）
AUTH_ERRNOS = frozenset({99, 911, 990001, 40101009, 40101010, 40101017, 40101032})
RATE_ERRNOS = frozenset({590075, 990005, 990009, 990019, 40110000})
NOT_FOUND_ERRNOS = frozenset({20009, 20013, 20018, 31003, 50003, 50015, 70005, 70008, 90008, 231011, 430004, 800001})
QUOTA_ERRNOS = frozenset({91005})
CONFLICT_ERRNOS = frozenset({20004})

_ERRNO_KEYS = ("errno", "errNo", "errcode", "errCode", "code", "msg_code")
_MESSAGE_KEYS = ("error", "msg", "error_msg", "message")


def cookie_header(cookies: dict[str, str]) -> str:
    return "; ".join(f"{k}={cookies[k]}" for k in COOKIE_KEYS if cookies.get(k))


def pick_cookies(header: str) -> dict[str, str]:
    """从整页 Cookie 里挑出登录态的四项。"""
    found: dict[str, str] = {}
    for part in header.split(";"):
        name, _, value = part.strip().partition("=")
        if name in COOKIE_KEYS and value:
            found[name] = value
    return found


def user_id_of(cookies: dict[str, str]) -> str:
    """UID 形如 ``{user_id}_{登录端}_{时间}``。"""
    return cookies.get("UID", "").split("_", 1)[0]


def _errno(data: dict[str, Any]) -> int | None:
    for key in _ERRNO_KEYS:
        value = data.get(key)
        if value not in (None, "", 0, "0"):
            try:
                return int(value)
            except (TypeError, ValueError):
                return None
    return None


def _message(data: dict[str, Any]) -> str:
    for key in _MESSAGE_KEYS:
        value = data.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def check(data: Any) -> dict[str, Any]:
    """按 115 的约定判断成功与否，失败时抛对应的 ``DriveError``。"""
    if not isinstance(data, dict):
        raise dv.ApiChanged("115 返回了认不出的内容，可能需要更新插件")
    state = data.get("state", True)
    if state and state not in ("0", "false"):
        return data
    errno = _errno(data)
    message = _message(data) or (f"115 返回错误 {errno}" if errno else "115 返回失败")
    raise error_for(errno, message)


def error_for(errno: int | None, message: str) -> dv.DriveError:
    if errno in AUTH_ERRNOS:
        return dv.AuthExpired(f"115 登录已失效：{message}")
    if errno in RATE_ERRNOS:
        return dv.RateLimited(f"115 提示操作太频繁：{message}")
    if errno in NOT_FOUND_ERRNOS or (errno is None and ("不存在" in message or "已删除" in message)):
        return dv.NotFound(message)
    if errno in QUOTA_ERRNOS:
        return dv.QuotaExceeded(f"115 空间不足：{message}")
    if errno in CONFLICT_ERRNOS or (errno is None and "已存在" in message):
        return dv.Conflict(message)
    return dv.DriveError(message)


def cipher():
    """115 的请求加密（p115cipher，插件依赖）。用到时才导入：依赖在管理员批准安装后才有，
    入口模块在那之前也要能导入（安装前检查、插件发现）。"""
    import p115cipher

    return p115cipher


def _maybe_decrypt(content: bytes) -> Any:
    """有的接口回明文 JSON，有的回 ECDH 加密体。"""
    body = content.strip()
    if body[:1] + body[-1:] in (b"{}", b"[]", b'""'):
        return json.loads(body)
    return json.loads(cipher().ecdh_aes_decrypt(body))


class Api115:
    """115 的接口集合。``transport`` 给测试注入仿 115 服务。"""

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._transport = transport

    async def _send(
        self,
        method: str,
        url: str,
        *,
        cookies: dict[str, str] | None = None,
        ua: str = WEB_UA,
        timeout: httpx.Timeout = TIMEOUT,
        **kwargs: Any,
    ) -> httpx.Response:
        headers = {"user-agent": ua, "accept": "*/*", **kwargs.pop("headers", {})}
        if cookies:
            headers["cookie"] = cookie_header(cookies)
        try:
            async with httpx.AsyncClient(transport=self._transport, timeout=timeout, follow_redirects=False) as client:
                response = await client.request(method, url, headers=headers, **kwargs)
        except httpx.TimeoutException as exc:
            raise dv.Transient("连接 115 超时") from exc
        except httpx.TransportError as exc:
            raise dv.Transient(f"连不上 115：{exc}") from exc
        if response.status_code in (405, 429):
            raise dv.RateLimited(f"115 拒绝了请求（HTTP {response.status_code}），可能是访问太频繁")
        if response.status_code >= 500 or response.status_code == 401:
            raise dv.Transient(f"115 暂时不可用（HTTP {response.status_code}）")
        return response

    async def _json(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        response = await self._send(method, url, **kwargs)
        try:
            data = response.json()
        except ValueError as exc:
            raise dv.ApiChanged("115 返回了认不出的内容，可能需要更新插件") from exc
        return check(data)

    # ------------------------------------------------------------------ 扫码登录

    async def qr_token(self, app: str) -> dict[str, Any]:
        data = await self._json("GET", f"https://qrcodeapi.115.com/api/1.0/{app}/1.0/token/")
        token = data.get("data") or {}
        if not token.get("uid"):
            raise dv.ApiChanged("115 没有给出二维码，可能需要更新插件")
        return token

    async def qr_status(self, token: dict[str, Any]) -> int | None:
        """扫码状态：0 等待、1 已扫、2 已确认、-1 过期、-2 取消；长轮询超时返回 None。"""
        params = {k: token[k] for k in ("uid", "time", "sign") if k in token}
        try:
            response = await self._send(
                "GET", "https://qrcodeapi.115.com/get/status/", params=params, timeout=QR_POLL_TIMEOUT
            )
        except dv.Transient:
            return None
        try:
            status = (response.json().get("data") or {}).get("status")
            return int(status)
        except (ValueError, TypeError, AttributeError) as exc:
            raise dv.ApiChanged("认不出 115 的扫码状态，可能需要更新插件") from exc

    async def qr_login(self, app: str, uid: str) -> dict[str, str]:
        data = await self._json(
            "POST",
            f"https://qrcodeapi.115.com/app/1.0/{app}/1.0/login/qrcode/",
            data={"account": uid},
        )
        cookies = pick_cookies("; ".join(f"{k}={v}" for k, v in ((data.get("data") or {}).get("cookie") or {}).items()))
        if len(cookies) < len(COOKIE_KEYS):
            raise dv.ApiChanged("115 扫码确认后没有给出完整的登录 Cookie")
        return cookies

    # ------------------------------------------------------------------ 账号

    async def logged_in(self, cookies: dict[str, str]) -> bool:
        response = await self._send("GET", "https://my.115.com/?ct=guide&ac=status", cookies=cookies)
        try:
            data = response.json()
        except ValueError:
            return False
        state = data.get("state") if isinstance(data, dict) else None
        return bool(state)

    async def user_nav(self, cookies: dict[str, str]) -> dict[str, Any]:
        data = await self._json("GET", "https://my.115.com/?ct=ajax&ac=nav", cookies=cookies)
        return data.get("data") or {}

    async def space(self, cookies: dict[str, str]) -> tuple[int | None, int | None]:
        data = await self._json(
            "GET",
            "https://proapi.115.com/android/user/space_info",
            cookies=cookies,
            params={"app_ver": APP_VERSION},
        )
        info = data.get("data") or {}

        def size(key: str) -> int | None:
            value = (info.get(key) or {}).get("size")
            return int(value) if value is not None else None

        return size("all_total"), size("all_use")

    # ------------------------------------------------------------------ 文件与目录

    async def files(self, cookies: dict[str, str], **params: Any) -> dict[str, Any]:
        query = {"aid": 1, "count_folders": 1, "record_open_time": 1, "show_dir": 1, "limit": PAGE}
        query.update(params)
        return await self._json("GET", "https://webapi.115.com/files", cookies=cookies, params=query)

    async def file_info(self, cookies: dict[str, str], file_id: str) -> dict[str, Any]:
        data = await self._json(
            "GET", "https://webapi.115.com/files/get_info", cookies=cookies, params={"file_id": file_id}
        )
        items = data.get("data") or []
        if not items:
            raise dv.NotFound("115 上没有这个文件")
        return items[0]

    async def mkdir(self, cookies: dict[str, str], parent_id: str, name: str) -> str:
        data = await self._json(
            "POST", "https://webapi.115.com/files/add", cookies=cookies, data={"cname": name, "pid": parent_id}
        )
        new_id = data.get("cid") or data.get("file_id")
        if not new_id:
            raise dv.ApiChanged("115 新建目录后没有给出目录 id")
        return str(new_id)

    async def delete(self, cookies: dict[str, str], file_ids: list[str]) -> None:
        form = {f"fid[{i}]": fid for i, fid in enumerate(file_ids)}
        await self._json("POST", "https://webapi.115.com/rb/delete", cookies=cookies, data=form)

    async def behavior(self, cookies: dict[str, str], offset: int, limit: int) -> dict[str, Any]:
        data = await self._json(
            "GET",
            "https://webapi.115.com/behavior/detail",
            cookies=cookies,
            params={"type": "", "limit": limit, "offset": offset},
        )
        return data.get("data") or {}

    async def download_url(self, cookies: dict[str, str], user_id: str, pickcode: str, ua: str) -> dict[str, Any]:
        payload = json.dumps({"pickcode": pickcode, "user_id": int(user_id)}).encode()
        response = await self._send(
            "POST",
            "https://proapi.115.com/app/chrome/downurl",
            cookies=cookies,
            ua=ua,
            data={"data": cipher().rsa_encrypt(payload).decode("ascii")},
        )
        try:
            data = check(_maybe_decrypt(response.content))
            items = json.loads(cipher().rsa_decrypt(data["data"])) if data.get("data") else {}
        except (ValueError, KeyError) as exc:
            raise dv.ApiChanged("认不出 115 的下载地址，可能需要更新插件") from exc
        if not items:
            raise dv.NotFound("115 上没有这个文件，或文件已进回收站")
        return items

    # ------------------------------------------------------------------ 上传

    async def upload_key(self, cookies: dict[str, str]) -> str:
        data = await self._json("GET", "https://webapi.115.com/files/uploadinfo", cookies=cookies)
        key = data.get("userkey")
        if not key:
            raise dv.ApiChanged("115 没有给出上传密钥")
        return str(key)

    async def upload_init(self, cookies: dict[str, str], payload: dict[str, Any]) -> dict[str, Any]:
        request = cipher().make_upload_payload(dict(payload))
        response = await self._send(
            "POST",
            "https://uplb.115.com/4.0/initupload.php",
            cookies=cookies,
            ua=UPLOAD_UA,
            params={**request["params"], "app_ver": APP_VERSION},
            content=request["data"],
            headers={"content-type": "application/x-www-form-urlencoded"},
        )
        try:
            data = _maybe_decrypt(response.content)
        except ValueError as exc:
            raise dv.ApiChanged("认不出 115 的上传初始化响应，可能需要更新插件") from exc
        if not isinstance(data, dict) or "status" not in data:
            check(data)
            raise dv.ApiChanged("认不出 115 的上传初始化响应，可能需要更新插件")
        return data

    async def oss_token(self) -> dict[str, Any]:
        response = await self._send("GET", "https://uplb.115.com/3.0/gettoken.php")
        try:
            data = response.json()
        except ValueError as exc:
            raise dv.ApiChanged("认不出 115 的上传凭证，可能需要更新插件") from exc
        if str(data.get("StatusCode")) != "200" or not data.get("AccessKeyId"):
            raise dv.Transient("115 暂时没给上传凭证")
        return data

    async def raw(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        """直接发请求（OSS 用）：只归类网络错误与 5xx，其余交给调用方。"""
        return await self._send(method, url, **kwargs)
