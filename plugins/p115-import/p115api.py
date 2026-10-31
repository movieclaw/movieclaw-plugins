"""115 的两条通道：官方 OpenAPI（自己填 AppID）与扫码登录的网页接口。

为什么两条都要：
- OpenAPI 覆盖列目录、建目录、改名、移动、删除、用户信息，稳定、官方；
- **转存（分享 → 自己的网盘）115 开放平台没有**，只能走网页接口，而网页接口要扫码拿 Cookie。
所以：转存、分享快照与批量异名改名走网页接口；其它文件操作有 OpenAPI 授权时优先走 OpenAPI，没有则走网页接口。

所有请求走用户在「设置 → 网络」里配的代理（``movieclaw_sdk.net``）。
日志与报错里不出现 Cookie、access_token。
"""

import asyncio
import base64
import hashlib
import html
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qsl, unquote as urllib_parse_unquote, urlsplit

import httpx

from movieclaw_sdk import net

SERVICE = "p115-import"

WEB_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
COOKIE_KEYS = ("UID", "CID", "SEID", "KID")

#: 扫码用的登录端（115 同一端同一时间只能在线一个）
LOGIN_APPS = {
    "harmony": "鸿蒙端",
    "apple_tv": "Apple TV 端",
    "os_linux": "Linux 客户端",
    "os_mac": "Mac 客户端",
    "os_windows": "Windows 客户端",
    "tv": "电视端",
    "wechatmini": "微信小程序",
    "web": "网页端",
}

#: 确认登录失效的错误码（限流、风控、网络错误都不算）
AUTH_CODES = frozenset(
    {99, 911, 990001, 40101009, 40101010, 40101017, 40101032,
     40140116, 40140117, 40140119, 40140120, 40140125}
)
#: 分享已失效 / 不存在
SHARE_GONE_CODES = frozenset({4100026, 4100008})
#: 需要提取码（115 对这个场景没有稳定错误码，只在明确文案里认）
SHARE_LOCKED_CODES = frozenset({4100009})
#: 参数错误：分享码、提取码不对都属于这一类
BAD_PARAM_CODES = frozenset({990002, 990003, 4100002})
#: 重名
CONFLICT_CODES = frozenset({20004, 20009, 20013, 20018})

_ERRNO_KEYS = ("errno", "errNo", "errcode", "errCode", "code")
_MESSAGE_KEYS = ("error", "message", "errmsg", "errmsg", "msg")


class ApiError(Exception):
    """115 拒绝了这个操作（或者返回了看不懂的东西）。"""

    def __init__(self, message, *, code=None):
        super().__init__(message)
        self.message = message
        self.code = code


class AuthExpired(ApiError):
    """登录态失效，必须重新扫码。"""


class ShareGone(ApiError):
    """分享不存在、被取消或已过期。"""


class ShareLocked(ApiError):
    """分享需要提取码。"""


class Conflict(ApiError):
    """目标目录里已有同名项。"""


class RateLimited(ApiError):
    """明确限流，延后而非提示重新登录。"""
    def __init__(self, message="115 请求过于频繁，稍后自动重试", *, retry_after=60, code=None):
        super().__init__(message,code=code)
        self.retry_after=max(1,min(3600,int(retry_after)))


class RiskControl(ApiError):
    """明确要求验证时停下来，不持续重试触发进一步风控。"""


def check_http(response):
    if response.status_code in (405,429):
        value=getattr(response,"headers",{}).get("Retry-After","")
        try:
            delay=int(value)
        except (ValueError,TypeError):
            delay=60
        raise RateLimited(retry_after=delay)
    if response.status_code>=500:
        raise ApiError(f"115 暂时不可用（HTTP {response.status_code}）")


# ---------------------------------------------------------------------- 解析


def cookie_header(cookies):
    return "; ".join(f"{k}={cookies[k]}" for k in COOKIE_KEYS if cookies.get(k))


def pick_cookies(header):
    """从整页 Cookie 里挑出登录态的四项。"""
    found = {}
    for part in (header or "").split(";"):
        name, _, value = part.strip().partition("=")
        if name in COOKIE_KEYS and value:
            found[name] = value
    return found


def _errno(data):
    for key in _ERRNO_KEYS:
        value = data.get(key)
        if isinstance(value, bool) or value in (None, ""):
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


def _message(data):
    for key in _MESSAGE_KEYS:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _check(data, *, action="操作"):
    if not isinstance(data, dict):
        raise ApiError(f"115 返回了认不出的内容（{action}）")
    if data.get("state") in (True, 1) or ("state" not in data and "data" in data):
        return data
    code = _errno(data)
    message = _message(data)
    # 以明确文案为准：风控/验证码、操作频繁不应误判为登录失效。
    if any(word in message for word in ("验证码","安全验证","风控","異常驗證","captcha")):
        raise RiskControl("115 要求安全验证，请在官方客户端完成验证后手动继续；文件与检查点保留",code=code)
    if any(word in message for word in ("太频繁","过于频繁","操作频繁","频率限制","请求限流")):
        raise RateLimited(code=code)
    if code in AUTH_CODES:
        raise AuthExpired(f"115 登录已失效（{message or code}），请重新扫码")
    if code in SHARE_GONE_CODES:
        raise ShareGone(f"分享不存在或已被取消（{message or code}）")
    if code in SHARE_LOCKED_CODES:
        raise ShareLocked("这个分享需要提取码，请把带密码的链接一起给我")
    if code in BAD_PARAM_CODES:
        raise ApiError(
            "115 说参数不对：分享码或提取码有误，或者这个分享已经不存在"
            "（有提取码的话请连密码一起发我，例如 …/s/abc?password=1234）",
            code=code,
        )
    if code in CONFLICT_CODES:
        raise Conflict(f"目标位置已有同名文件（{message or code}）")
    raise ApiError(f"115 {action}失败：{message or code or '未说明原因'}", code=code)


def parse_share_link(text):
    """分享链接 / 分享码 → (share_code, receive_code)。

    认这些写法：``https://115.com/s/abc?password=1234``、``115cdn.com``、``share.115.com``、
    ``abc-1234``、``/abc-1234/``，以及光秃秃的分享码。
    """
    raw = (text or "").strip().strip("/#")
    if not raw:
        raise ValueError("请给我 115 分享链接或「分享码-提取码」")
    if "://" in raw or raw.startswith("115."):
        url = raw if "://" in raw else "https://" + raw
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        if not any(host==domain or host.endswith("."+domain) for domain in ("115.com","115cdn.com")):
            raise ValueError("这看起来不是受支持的 115 分享链接")
        code = parts.path.rstrip("/").rpartition("/")[-1]
        query = dict(parse_qsl(parts.query))
        password = query.get("password") or query.get("pwd") or ""
        if not code:
            raise ValueError("链接里没有分享码")
        return code, password
    body, _, tail = raw.partition("#")
    if "-" in body:
        code, _, password = body.partition("-")
        return code.strip(), password.strip()
    return body.strip(), ""


def _decoded(value):
    return html.unescape(str(value or ""))



#: 电驴 / 磁力链接（115 云下载认这两种，也认 HTTP/FTP）
_ED2K = re.compile(r"ed2k://\|file\|([^|]+)\|(\d+)\|([0-9A-Fa-f]{32})\|", re.I)
_MAGNET = re.compile(r"magnet:\?[^\s]+", re.I)


@dataclass
class OfflineItem:
    """一条可以直接交给 115 云下载的链接（电驴 / 磁力 / 直链）。"""

    url: str
    name: str = ""
    size: int = 0
    info_hash: str = ""
    kind: str = "ed2k"      # ed2k / magnet / http


def parse_offline(text):
    """把输入拆成 115 云下载能吃的链接列表；不是这类链接返回空列表。

    ``ed2k://|file|名字|字节数|哈希|/`` 自带名字与体积，能先比体积再决定要不要下。
    磁力链只有 dn（名字），体积要等 115 解析出来才知道。
    """
    raw = (text or "").strip()
    if not raw:
        return []
    out = []
    for match in _ED2K.finditer(raw):
        name, size, digest = match.groups()
        out.append(OfflineItem(
            url=match.group(0) + "/" if not match.group(0).endswith("/") else match.group(0),
            name=html.unescape(name), size=int(size), info_hash=digest.lower(), kind="ed2k",
        ))
    for match in _MAGNET.finditer(raw):
        url = match.group(0)
        query = dict(parse_qsl(urlsplit(url).query))
        name = html.unescape(query.get("dn") or "")
        digest = ""
        xt = query.get("xt") or ""
        if xt.lower().startswith("urn:btih:"):
            digest = xt.split(":", 2)[-1]
        out.append(OfflineItem(url=url, name=name, size=0, info_hash=digest, kind="magnet"))
    if out:
        return out
    lower = raw.lower()
    if lower.startswith(("http://", "https://", "ftp://")):
        host = (urlsplit(raw).hostname or "").lower()
        if any(host==domain or host.endswith("."+domain) for domain in ("115.com","115cdn.com")):
            return []          # 115 自己的分享链接，走分享那条路
        name = urllib_parse_unquote(urlsplit(raw).path.rstrip("/").rpartition("/")[-1])
        out.append(OfflineItem(url=raw, name=name, kind="http"))
    return out


def is_offline_link(text):
    return bool(parse_offline(text))

# ---------------------------------------------------------------------- 数据


@dataclass
class Entry:
    """网盘里的一个文件或目录。"""

    remote_id: str
    name: str
    is_dir: bool
    size: int = 0
    sha1: str = ""
    pickcode: str = ""
    parent_id: str = ""


@dataclass
class ShareFile:
    """分享里的一个文件（可能带目录层级）。"""

    share_id: str          # 分享内的 file_id，转存时用它
    name: str
    is_dir: bool
    size: int = 0
    sha1: str = ""
    rel_path: str = ""     # 分享内的相对路径（目录分享时有层级）
    season: int | None = None
    episode: int | None = None


# ---------------------------------------------------------------------- 客户端


class Pan:
    """115 的登录态与文件操作。登录态加密存在插件数据里。"""

    def __init__(self, store, app_id_provider=None):
        self._store = store
        self._app_id = app_id_provider or (lambda: "")
        self._web = None
        self._open = None
        self._lock = asyncio.Lock()
        self._dir_lock = asyncio.Lock()
        self._dirs = {}
        self._client = None
        self.request_count = 0
        self._rate_lock=asyncio.Lock()
        self._next_request=0.0

    async def close(self):
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------------------ 登录态

    async def web_login(self):
        if self._web is None:
            self._web = await self._store.get("web_login", default=None)
        return self._web

    async def open_login(self):
        if self._open is None:
            self._open = await self._store.get("open_login", default=None)
        return self._open

    async def save_web_login(self, cookies, app):
        value = {"cookies": {k: cookies[k] for k in COOKIE_KEYS if cookies.get(k)},
                 "app": app, "at": int(time.time())}
        await self._store.set("web_login", value, secret=True)
        self._web = value
        self._dirs.clear()
        return value

    async def save_open_login(self, data, app_id):
        value = {
            "access_token": data.get("access_token") or "",
            "refresh_token": data.get("refresh_token") or "",
            "expires_at": int(time.time()) + int(data.get("expires_in") or 7200) - 120,
            "app_id": str(app_id),
            "at": int(time.time()),
        }
        await self._store.set("open_login", value, secret=True)
        self._open = value
        return value

    async def forget(self, what):
        self._dirs.clear()
        await self._store.delete(f"{what}_login")
        if what == "web":
            self._web = None
        else:
            self._open = None

    async def require_web(self):
        login = await self.web_login()
        if not login or not all((login.get("cookies") or {}).get(k) for k in COOKIE_KEYS):
            raise AuthExpired("还没有扫码登录 115，请先扫码（mclaw plugins p115-import auth web start）")
        return login["cookies"]

    async def open_token(self, *, refresh=False):
        """OpenAPI 的 access_token；没配 AppID / 没授权时返回 None（调用方退回网页接口）。"""
        login = await self.open_login()
        if not login or not login.get("access_token"):
            return None
        if not refresh and int(login.get("expires_at") or 0) > time.time():
            return login["access_token"]
        async with self._lock:
            login = await self.open_login()
            if not refresh and int((login or {}).get("expires_at") or 0) > time.time():
                return login["access_token"]
            data = await self._post_json(
                "https://qrcodeapi.115.com/open/refreshToken",
                {"refresh_token": (login or {}).get("refresh_token") or ""},
                action="刷新 OpenAPI 授权",
            )
            body = data.get("data") or {}
            if not body.get("access_token"):
                raise AuthExpired("OpenAPI 授权已失效，请重新授权（mclaw plugins p115-import auth open start）")
            saved = await self.save_open_login(body, login.get("app_id") or self._app_id())
            return saved["access_token"]

    # ------------------------------------------------------------------ 底层请求

    async def _request(self, method, url, *, params=None, data=None, headers=None,
                       cookies=None, timeout=30.0):
        final = {"user-agent": WEB_UA, "accept": "*/*"}
        if cookies:
            final["cookie"] = cookie_header(cookies)
        if headers:
            final.update(headers)
        try:
            if self._client is None:
                self._client = httpx.AsyncClient(
                    transport=net.http_transport(SERVICE),
                    timeout=httpx.Timeout(30, connect=10.0),
                    limits=httpx.Limits(max_connections=4, max_keepalive_connections=4),
                    follow_redirects=False,
                )
            # 独立的转存接口没有本体网盘网关；按同账号共享1次/秒限速，
            # 不用无上限并发“优化”去触发115风控。连接池仍复用TLS。
            async with self._rate_lock:
                delay=self._next_request-time.monotonic()
                if delay>0:
                    await asyncio.sleep(delay)
                self._next_request=time.monotonic()+1.0
            self.request_count += 1
            # 连接池按业务操作复用，不在每次列目录/改名单独建立 TLS。
            return await self._client.request(method, url, params=params, data=data, headers=final,
                                               timeout=httpx.Timeout(timeout, connect=10.0))
        except httpx.TimeoutException as exc:
            raise ApiError("连接 115 超时，请稍后重试") from exc
        except httpx.HTTPError as exc:
            raise ApiError(f"连不上 115：{exc.__class__.__name__}") from exc

    async def _post_json(self, url, form, *, action, long_poll=False):
        response = await self._request(
            "POST", url, data=form, timeout=35 if not long_poll else 12
        )
        check_http(response)
        try:
            data = response.json()
        except ValueError as exc:
            raise ApiError(f"115 返回了认不出的内容（{action}）") from exc
        return _check(data, action=action)

    async def open_json(self, method, path, *, params=None, form=None, action="OpenAPI 操作",
                        retry=True):
        token = await self.open_token()
        if not token:
            raise ApiError("还没有完成 OpenAPI 授权")
        headers = {"authorization": "Bearer " + token}
        url = "https://proapi.115.com" + path
        response = await self._request(method, url, params=params, data=form, headers=headers)
        if response.status_code == 401:
            if retry:
                await self.open_token(refresh=True)
                return await self.open_json(method, path, params=params, form=form,
                                            action=action, retry=False)
            raise AuthExpired("OpenAPI 授权已失效，请重新授权")
        check_http(response)
        try:
            data = response.json()
        except ValueError as exc:
            raise ApiError(f"115 返回了认不出的内容（{action}）") from exc
        if isinstance(data, dict):
            code = _errno(data)
            if code in AUTH_CODES and retry and not any(word in _message(data) for word in ("验证码","安全验证","风控","太频繁","过于频繁","操作频繁","captcha")):
                await self.open_token(refresh=True)
                return await self.open_json(method, path, params=params, form=form,
                                            action=action, retry=False)
        return _check(data, action=action)

    async def web_json(self, method, url, *, params=None, form=None, action="115 操作",
                       extra_headers=None):
        cookies = await self.require_web()
        response = await self._request(method, url, params=params, data=form, cookies=cookies,
                                       headers=extra_headers)
        check_http(response)
        try:
            data = response.json()
        except ValueError as exc:
            raise ApiError(f"115 返回了认不出的内容（{action}）") from exc
        return _check(data, action=action)

    # ------------------------------------------------------------------ 扫码

    async def qr_png(self, url):
        """把二维码图片取回成 PNG 字节（页面直接内嵌，省得浏览器去连 115）。"""
        response = await self._request("GET", url, timeout=20.0)
        if response.status_code != 200 or not response.content.startswith(b"\x89PNG"):
            raise ApiError("取二维码图片失败，请稍后重试")
        return response.content

    async def qr_token(self, app):
        data = await self._get_json(
            f"https://qrcodeapi.115.com/api/1.0/{app}/1.0/token/", action="获取登录二维码"
        )
        token = data.get("data") or {}
        if not token.get("uid"):
            raise ApiError("115 没有给出二维码，请稍后重试")
        qr = token.get("qrcode") or f"https://115.com/scan/dg-{token['uid']}"
        png = f"https://qrcodeapi.115.com/api/1.0/{app}/1.0/qrcode?uid={token['uid']}"
        return {"uid": token["uid"], "time": token.get("time"), "sign": token.get("sign"),
                "qrcode": qr, "png": png, "app": app}

    async def qr_status(self, token):
        """0 等待 / 1 已扫 / 2 已确认 / -1 过期 / -2 取消；长轮询超时返回 None。"""
        params = {k: token[k] for k in ("uid", "time", "sign") if token.get(k)}
        try:
            data = await self._get_json(
                "https://qrcodeapi.115.com/get/status/", params=params,
                action="查询扫码状态", timeout=10.0, tolerant_timeout=True,
            )
        except ApiError:
            return None
        try:
            return int((data.get("data") or {}).get("status"))
        except (TypeError, ValueError):
            return None

    async def qr_result(self, app, uid):
        data = await self._post_json(
            f"https://qrcodeapi.115.com/app/1.0/{app}/1.0/login/qrcode/",
            {"account": uid}, action="确认扫码登录",
        )
        raw = ((data.get("data") or {}).get("cookie") or {})
        joined = "; ".join(f"{k}={v}" for k, v in raw.items())
        cookies = pick_cookies(joined)
        if len(cookies) < len(COOKIE_KEYS):
            raise ApiError("115 扫码确认后没有给出完整的登录状态")
        return cookies

    async def open_device_code(self, app_id):
        challenge = base64.b64encode(hashlib.md5(b"0" * 64).digest()).decode()
        data = await self._post_json(
            "https://qrcodeapi.115.com/open/authDeviceCode",
            {"client_id": str(app_id), "code_challenge": challenge,
             "code_challenge_method": "md5"},
            action="申请 OpenAPI 授权二维码",
        )
        token = data.get("data") or {}
        if not token.get("uid"):
            raise ApiError("115 没有给出授权二维码，请检查 AppID 是否正确")
        return {"uid": token["uid"], "time": token.get("time"), "sign": token.get("sign"),
                "qrcode": token.get("qrcode") or f"https://115.com/scan/dg-{token['uid']}",
                "png": f"https://qrcodeapi.115.com/api/1.0/web/1.0/qrcode?uid={token['uid']}"}

    async def open_device_token(self, uid):
        data = await self._post_json(
            "https://qrcodeapi.115.com/open/deviceCodeToToken",
            {"uid": uid, "code_verifier": "0" * 64}, action="换取 OpenAPI 令牌",
        )
        body = data.get("data") or {}
        if not body.get("access_token"):
            raise ApiError("115 没有给出 access_token，请重新授权")
        return body

    async def _get_json(self, url, *, params=None, action, timeout=30.0,
                        tolerant_timeout=False):
        response = await self._request("GET", url, params=params, timeout=timeout)
        check_http(response)
        try:
            data = response.json()
        except ValueError as exc:
            if tolerant_timeout:
                raise ApiError("115 没有响应（可能还在等待）") from exc
            raise ApiError(f"115 返回了认不出的内容（{action}）") from exc
        return _check(data, action=action)

    # ------------------------------------------------------------------ 文件操作

    async def _use_open(self):
        return bool(await self.open_token())

    async def list_dir(self, cid):
        """列一个目录（只返回直接子项，包含子目录）。"""
        if await self._use_open():
            return await self._open_list(cid)
        return await self._web_list(cid)

    async def _open_list(self, cid):
        out = []
        offset = 0
        while True:
            data = await self.open_json(
                "GET", "/open/ufile/files",
                params={"cid": str(cid or "0"), "limit": 1000, "offset": offset,
                        "aid": 1, "show_dir": 1, "cur": 1, "o": "file_name", "asc": 1},
                action="列目录",
            )
            items = data.get("data") or []
            if not isinstance(items, list):
                items = []
            for item in items:
                is_dir = str(item.get("fc", "")) == "0"
                out.append(Entry(
                    remote_id=str(item.get("fid") or item.get("cid") or ""),
                    name=_decoded(item.get("fn") or item.get("n")),
                    is_dir=is_dir,
                    size=0 if is_dir else int(item.get("fs") or item.get("s") or 0),
                    sha1=str(item.get("sha1") or item.get("sha") or "").upper(),
                    pickcode=str(item.get("pc") or ""),
                    parent_id=str(item.get("pid") or ""),
                ))
            offset += len(items)
            if len(items) < 1000:
                break
        return out

    async def _web_list(self, cid):
        out = []
        offset = 0
        while True:
            data = await self.web_json(
                "GET", "https://webapi.115.com/files",
                params={"cid": str(cid or "0"), "limit": 1000, "offset": offset,
                        "show_dir": 1, "aid": 1, "cur": 1, "nf": 1,
                        "o": "file_name", "asc": 1},
                action="列目录",
            )
            items = data.get("data") or []
            if not isinstance(items, list):
                items = []
            for item in items:
                is_dir = "fid" not in item or str(item.get("fc", "")) == "0"
                if is_dir and "cid" in item and "fid" not in item:
                    remote_id = str(item.get("cid"))
                else:
                    remote_id = str(item.get("fid") or item.get("cid") or "")
                if not remote_id or remote_id == "0":
                    continue
                out.append(Entry(
                    remote_id=remote_id,
                    name=_decoded(item.get("n") or item.get("file_name")),
                    is_dir=is_dir,
                    size=0 if is_dir else int(item.get("s") or item.get("size") or 0),
                    sha1=str(item.get("sha") or item.get("sha1") or "").upper(),
                    pickcode=str(item.get("pc") or ""),
                    parent_id=str(item.get("pid") or ""),
                ))
            offset += len(items)
            if len(items) < 1000:
                break
        return out

    async def mkdir(self, pid, name):
        if await self._use_open():
            data = await self.open_json(
                "POST", "/open/folder/add",
                form={"pid": str(pid or "0"), "file_name": name}, action="新建目录",
            )
            body = data.get("data") or {}
            new_id = body.get("file_id") or body.get("cid")
            if not new_id:
                raise ApiError("115 新建目录后没有给出目录 id")
            return str(new_id)
        data = await self.web_json(
            "POST", "https://webapi.115.com/files/add",
            form={"pid": str(pid or "0"), "cname": name}, action="新建目录",
        )
        new_id = data.get("cid") or data.get("file_id") or (data.get("data") or {}).get("cid")
        if not new_id:
            raise ApiError("115 新建目录后没有给出目录 id")
        return str(new_id)

    async def rename(self, fid, name):
        if await self._use_open():
            await self.open_json(
                "POST", "/open/ufile/update",
                form={"file_id": str(fid), "file_name": name}, action="改名",
            )
            return
        await self.web_json(
            "POST", "https://webapi.115.com/files/batch_rename",
            form={f"files_new_name[{fid}]": name}, action="改名",
        )

    async def rename_many(self, pairs):
        """已确认的网页异名批量接口。每次最多50项是插件保守分块，不是服务端上限。

        不猜OpenAPI数组参数；不在结果不明时改走另一接口盲重发。
        调用方负责分块、预检与按编号回读真实名字。
        """
        pairs=list(pairs)
        if not pairs:
            return
        if len(pairs)>50:
            raise ValueError("批量改名需先按50项分块")
        form={}
        for fid,name in pairs:
            fid=str(fid)
            if not fid or not str(name).strip() or name in (".","..") or "/" in name or "\\" in name:
                raise ValueError("批量改名的编号或单段文件名不合法")
            key=f"files_new_name[{fid}]"
            if key in form:
                raise ValueError("批量改名含重复文件编号")
            form[key]=name
        await self.web_json("POST","https://webapi.115.com/files/batch_rename",form=form,action="批量改名")

    async def move(self, fids, to_cid):
        fids = [str(f) for f in fids if str(f)]
        if not fids:
            return
        if await self._use_open():
            await self.open_json(
                "POST", "/open/ufile/move",
                form={"file_ids": ",".join(fids), "to_cid": str(to_cid)}, action="移动",
            )
            return
        form = {"pid": str(to_cid)}
        for index, fid in enumerate(fids):
            form[f"fid[{index}]"] = fid
        await self.web_json(
            "POST", "https://webapi.115.com/files/move", form=form, action="移动"
        )

    async def delete(self, fids):
        fids = [str(f) for f in fids if str(f)]
        if not fids:
            return
        removed=set(fids)
        self._dirs={key:value for key,value in self._dirs.items()
                    if key[0] not in removed and value[0] not in removed}
        if await self._use_open():
            await self.open_json(
                "POST", "/open/ufile/delete",
                form={"file_ids": ",".join(fids)}, action="删除",
            )
            return
        form = {}
        for index, fid in enumerate(fids):
            form[f"fid[{index}]"] = fid
        await self.web_json(
            "POST", "https://webapi.115.com/rb/delete", form=form, action="删除"
        )

    async def find_child(self, pid, name):
        for entry in await self.list_dir(pid):
            if entry.name == name:
                return entry
        return None

    async def ensure_dir(self, base, parts):
        """操作范围内缓存目录 ID；并发建同一目录时串行核对，不每集重列根目录。"""
        async with self._dir_lock:
            current = str(base)
            for part in [p for p in parts if p]:
                key = (current, part)
                cached=self._dirs.get(key)
                if cached and time.monotonic()-cached[1]<30:
                    current = cached[0]
                    continue
                found = await self.find_child(current, part)
                if found is not None and not found.is_dir:
                    raise Conflict(f"「{part}」这个名字已经被一个文件占了")
                if found is not None:
                    child_id = found.remote_id
                else:
                    try:
                        child_id = await self.mkdir(current, part)
                    except Conflict:
                        found = await self.find_child(current, part)
                        if found is None or not found.is_dir:
                            raise
                        child_id = found.remote_id
                self._dirs[key] = (child_id,time.monotonic())
                current = child_id
            return current

    async def account_name(self):
        login = await self.open_login()
        if login:
            try:
                data = await self.open_json("GET", "/open/user/info", action="查询账号")
                body = data.get("data") or {}
                return str(body.get("user_name") or body.get("nickname") or "")
            except ApiError:
                pass
        cookies = await self.require_web()
        data = await self.web_json(
            "GET", "https://my.115.com/?ct=ajax&ac=nav", action="查询账号"
        )
        return str((data.get("data") or {}).get("user_name") or "")


    # ------------------------------------------------------------------ 云下载（电驴 / 磁力）

    async def offline_add(self, urls, wp_path_id):
        """提交云下载任务（115 自己下到指定目录）。"""
        listing = [u for u in urls if u]
        if not listing:
            raise ValueError("没有要提交的链接")
        data = await self.open_json(
            "POST", "/open/offline/add_task_urls",
            form={"urls": "\n".join(listing), "wp_path_id": str(wp_path_id or "0")},
            action="提交云下载",
        )
        return data.get("data") or []

    async def offline_tasks(self, page=1):
        data = await self.open_json(
            "GET", "/open/offline/get_task_list", params={"page": page}, action="查询云下载任务"
        )
        return (data.get("data") or {})

    async def offline_quota(self):
        data = await self.open_json(
            "GET", "/open/offline/get_quota_info", action="查询云下载配额"
        )
        return data.get("data") or {}

    async def offline_delete(self, info_hash, *, delete_files=False):
        await self.open_json(
            "POST", "/open/offline/del_task",
            form={"info_hash": info_hash, "del_source_file": 1 if delete_files else 0},
            action="删除云下载任务",
        )

    # ------------------------------------------------------------------ 分享

    async def share_snap(self, share_code, receive_code, cid="0", offset=0, limit=1000):
        params = {"share_code": share_code, "receive_code": receive_code or "",
                  "cid": str(cid or "0"), "limit": limit, "offset": offset,
                  "asc": 0, "o": "file_name", "format": "json"}
        return await self.web_json(
            "GET", "https://webapi.115.com/share/snap", params=params,
            action="读取分享",
            extra_headers={"referer": f"https://115cdn.com/s/{share_code}?password={receive_code or ''}&"},
        )

    async def share_tree(self, share_code, receive_code, *, max_files=5000):
        """把分享里的所有文件摊平（递归子目录），目录本身也带出来。"""
        out = []
        seen_dir = set()
        visited_ids = set()

        async def walk(cid, prefix):
            if cid in visited_ids:
                return
            visited_ids.add(cid)
            if len(visited_ids)>max_files:
                raise ApiError("分享目录过多，无法完成完整盘点，请按作品拆分链接")
            offset = 0
            while True:
                data = await self.share_snap(share_code, receive_code, cid, offset, 1000)
                body = data.get("data") or {}
                items = body.get("list") or []
                total = int(body.get("count") or 0)
                for item in items:
                    name = _decoded(item.get("n") or item.get("fn"))
                    is_dir = str(item.get("fc", "1")) == "0" or "fid" not in item
                    rel = f"{prefix}{name}"
                    if is_dir:
                        if rel not in seen_dir:
                            seen_dir.add(rel)
                            out.append(ShareFile(share_id=str(item.get("cid") or item.get("fid") or ""),
                                                 name=name, is_dir=True, rel_path=rel))
                            child = item.get("cid") or item.get("fid")
                            if child:
                                await walk(str(child), rel + "/")
                        continue
                    out.append(ShareFile(
                        share_id=str(item.get("fid") or ""),
                        name=name, is_dir=False,
                        size=int(item.get("s") or item.get("size") or 0),
                        sha1=str(item.get("sha") or item.get("sha1") or "").upper(),
                        rel_path=rel,
                    ))
                    if len(out) >= max_files:
                        raise ApiError("分享文件清单达到安全上限，未使用截断清单执行，请按作品拆分链接")
                offset += len(items)
                if not items or offset >= total:
                    return
                if len(out) >= max_files:
                    raise ApiError("分享文件清单达到安全上限，未使用截断清单执行，请按作品拆分链接")

        await walk("0", "")
        return out

    async def share_meta(self, share_code, receive_code):
        data = await self.share_snap(share_code, receive_code, "0", 0, 1)
        body = data.get("data") or {}
        info = body.get("shareinfo") or {}
        return {
            "title": _decoded(info.get("share_title")),
            "user": _decoded((body.get("userinfo") or {}).get("user_name")),
            "count": int(body.get("count") or 0),
            "total_size": int(info.get("file_size") or 0),
        }

    async def share_receive(self, share_code, receive_code, file_ids, cid):
        """转存；返回 115 的原始响应（里面有 recv_file_count / receive_size，可用来核对）。

        **不能带 is_check=1**：带上它 115 只做校验、不真转存，回执照样是
        ``state: true, data: 0``——看起来成功、实际什么都没进来。这个坑踩过一次了。
        """
        ids = [str(f) for f in file_ids if str(f)]
        if not ids:
            raise ValueError("没有要转存的文件")
        return await self.web_json(
            "POST", "https://webapi.115.com/share/receive",
            form={"share_code": share_code, "receive_code": receive_code or "",
                  "file_id": ",".join(ids), "cid": str(cid)},
            action="转存",
            extra_headers={"referer": f"https://115cdn.com/s/{share_code}?password={receive_code or ''}&"},
        )
