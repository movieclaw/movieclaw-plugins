"""M-Team（馒头）保活：账号密码 + TOTP 登录网页会话，再刷新「最后浏览时间」。

**为什么不能借宿主的 API Key**：M-Team 的 ``/api/member/updateLastBrowse`` 只认网页
会话 JWT。同一个 API Key 调 ``/api/member/profile`` 正常返回，调这个接口一律回
``{"code":401,"message":"Full authentication is required..."}``（2026-10-20 实测），
所以本站点不走 ``site.proxy.request`` 代请求，而是在插件里自己登录——与社区方案
`scjtqs2/mtlogin <https://github.com/scjtqs2/mtlogin>`_ 的路线一致：

1. ``POST /api/login``（用户名 + 密码 + 空 turnstile）→ ``code 1001`` 表示账号开了二次验证；
2. 同一请求再带 ``otpCode``（本地算的 TOTP 动态码）→ ``message=SUCCESS``，
   响应头 ``Authorization`` 是 JWT、``Did`` 是服务端下发的设备号；
3. ``POST /api/member/profile`` 确认会话有效（顺带读 lastBrowse / lastLogin）；
4. ``POST /api/member/updateLastBrowse`` 刷新最后浏览时间。

每个请求都要带 ``_timestamp``（13 位毫秒）与 ``_sgin`` 签名，外加一套伪装成官方前端的
请求头（``version`` / ``webversion`` / ``Did`` / ``visitorid`` / ``Ts`` / ``referer``）。
签名密钥 ``SIGN_SECRET`` 是站点前端 JS 里硬编码的，站点改版轮换时只改这一处常量。

**两个容易判错的地方**（都有单测）：

- ``updateLastBrowse`` 在站点认为「无需更新」（间隔短于约 60 秒、lastBrowse 已经新鲜）
  时回 ``{"code":"1","message":"FAIL"}``，此时**确实没有刷新**——它既不是成功，
  也不是账号失效。记「登录在线」，不进失败统计（本机还跑着 mtlogin 时，每轮都会
  撞上这个）。
- 登录失败会消耗账号的尝试次数（``您還有 N 次機會``），所以鉴权类错误**不重试**。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from pt_keepalive.result import Outcome, SiteResult

PLUGIN_ID = "pt-keepalive"

#: 站点前端 JS 里硬编码的签名密钥（改版轮换时只改这里）
SIGN_SECRET = b"HLkPcWmycL57mfJt"

#: API 域名。站点网页域名（referer/origin）与之不同，必须一起伪装
API_BASE = "https://api.m-team.io"
REFERER = "https://kp.m-team.cc/"

#: 伪装成官方前端：客户端版本与 API 版本（站点前端发版时一起改）
CLIENT_VERSION = "1.1.4"
WEB_VERSION = "1140"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0"
)

TOTP_STEP = 30
TOTP_DIGITS = 6

#: 会话（JWT + 设备指纹）在插件数据里的键；写入时 ``secret=True`` 由宿主加密
SESSION_KEY = "mteam_session"

DEFAULT_TIMEOUT = 30.0

#: 签名错误：前端签名密钥被轮换，插件需要跟着改
_SIGNATURE_MARKS = ("簽名錯誤", "签名错误")
#: 站点限流：既不是凭据问题也不是站点改版
_RATE_MARKS = ("請求過於頻繁", "请求过于频繁")
#: 凭据类：账号密码错、账号不存在、令牌失效、请求无效（JWT 不对）
_AUTH_MARKS = (
    "full authentication",
    "無效的請求",
    "无效的请求",
    "用戶名或密碼錯誤",
    "用户名或密码错误",
    "此帳號不存在",
    "令牌",
    "請先登入",
    "请先登录",
)


# ---------------------------------------------------------------------------
# 纯函数：签名、TOTP、响应判定（单测重点）
# ---------------------------------------------------------------------------


def sign(method: str, path: str, timestamp_ms: int) -> str:
    """``base64(HMAC-SHA1("{METHOD}&{path}&{ts_ms}"))``——站点前端算法的复刻。"""
    message = f"{method}&{path}&{timestamp_ms}".encode()
    return base64.b64encode(hmac.new(SIGN_SECRET, message, hashlib.sha1).digest()).decode()


def normalize_totp_secret(raw: str) -> str:
    """TOTP 密钥 → 纯 Base32。

    用户常整条粘贴 ``otpauth://totp/...?secret=XXXX``，也常用小写、带空格或短横线：
    这些都先规整掉，免得「密钥明明对，却算出 6 位垃圾」。
    """
    text = (raw or "").strip()
    if "secret=" in text.lower():
        text = re.split(r"[?&]secret=", text, flags=re.I, maxsplit=1)[1].split("&")[0]
    return re.sub(r"[\s-]+", "", text).upper().rstrip("=")


def totp_code(secret: str, *, at: float | None = None) -> str:
    """按 RFC 6238 算 6 位动态码（不引入 pyotp：宿主只保证标准库 + 自带那几个包）。

    密钥不是合法 Base32 时抛 ``ValueError``——这是用户在设置里填错了，重试没有意义。
    """
    key_text = normalize_totp_secret(secret)
    padded = key_text + "=" * ((-len(key_text)) % 8)
    try:
        key = base64.b32decode(padded)
    except (ValueError, TypeError) as exc:  # binascii.Error 是 ValueError 的子类
        raise ValueError("两步验证密钥不是合法的 Base32（应是字母和数字，可含 =）") from exc
    counter = int(time.time() if at is None else at) // TOTP_STEP
    digest = hmac.new(key, counter.to_bytes(8, "big"), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = int.from_bytes(digest[offset : offset + 4], "big") & 0x7FFFFFFF
    return str(value % (10**TOTP_DIGITS)).zfill(TOTP_DIGITS)


def device_id() -> str:
    """设备指纹（32 位十六进制）。``visitorid`` 生成后永久复用，``Did`` 由站点下发。"""
    return secrets.token_hex(16)


class Reply(str, Enum):
    """站点响应的语义分类。"""

    OK = "ok"
    NEED_TOTP = "need_totp"
    NO_CHANGE = "no_change"  # 站点认为无需刷新（不是失败）
    AUTH = "auth"  # 凭据类：账号密码错 / 令牌失效
    SIGNATURE = "signature"  # 签名错误：前端密钥被轮换
    RATE = "rate"  # 请求过于频繁
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Verdict:
    reply: Reply
    message: str = ""


def classify(payload: Any, *, status_code: int = 200) -> Verdict:
    """站点 JSON 响应 → 语义分类。

    ``code`` 在 M-Team 各接口里有数字也有字符串（``0`` / ``"0"`` / ``401``），
    一律按字符串比。HTTP 401/403 先于「成功」判据：被拒的响应即便正文像成功也不采信。
    """
    if not isinstance(payload, dict):
        return Verdict(Reply.UNKNOWN, "响应不是 JSON 对象")
    message = str(payload.get("message") or payload.get("msg") or "")
    code = str(payload.get("code"))
    if status_code in (401, 403) or code in ("401", "403"):
        return Verdict(Reply.AUTH, message or f"HTTP {status_code}")
    if code == "1001":
        return Verdict(Reply.NEED_TOTP, message)
    if code == "0" or message.upper() in ("SUCCESS", "OK"):
        return Verdict(Reply.OK, message)
    lowered = message.lower()
    if any(mark in lowered for mark in _SIGNATURE_MARKS):
        return Verdict(Reply.SIGNATURE, message)
    if any(mark in lowered for mark in _RATE_MARKS):
        return Verdict(Reply.RATE, message)
    if any(mark in lowered for mark in _AUTH_MARKS):
        return Verdict(Reply.AUTH, message)
    return Verdict(Reply.UNKNOWN, message)


def classify_update(payload: Any, *, status_code: int = 200) -> Verdict:
    """``updateLastBrowse`` 的响应判定：``{code:1, message:"FAIL"}`` = 站点判定无需刷新。"""
    verdict = classify(payload, status_code=status_code)
    no_change = (
        isinstance(payload, dict)
        and str(payload.get("code")) == "1"
        and str(payload.get("message") or "").upper() == "FAIL"
    )
    if verdict.reply is Reply.UNKNOWN and no_change:
        return Verdict(Reply.NO_CHANGE, verdict.message)
    return verdict


# ---------------------------------------------------------------------------
# 凭据与会话
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Credentials:
    """插件设置里的 M-Team 登录信息（明文只活在本进程里）。"""

    username: str
    password: str
    #: 可空：账号没开二次验证时不需要
    totp_secret: str = ""


def credentials(username: str, password: str, totp_secret: str = "") -> Credentials | None:
    """设置值 → 凭据；账号或密码为空表示没配置（返回 ``None``，不拿它去试登录）。"""
    name = (username or "").strip()
    secret_value = password or ""
    if not name or not secret_value:
        return None
    return Credentials(username=name, password=secret_value, totp_secret=(totp_secret or "").strip())


@dataclass
class Session:
    """一次登录的产物，跨轮复用（避免每轮都走登录，也保住服务端认可的 Did）。"""

    token: str = ""
    did: str = ""
    visitorid: str = ""

    @classmethod
    def from_dict(cls, raw: Any) -> Session:
        if not isinstance(raw, dict):
            return cls()
        return cls(
            token=str(raw.get("token") or ""),
            did=str(raw.get("did") or ""),
            visitorid=str(raw.get("visitorid") or ""),
        )

    def to_dict(self) -> dict[str, str]:
        return {"token": self.token, "did": self.did, "visitorid": self.visitorid}

    def prepare(self) -> None:
        """补齐设备指纹：``visitorid`` 一旦生成就永久复用（每次变会被风控认作新设备）。"""
        if not self.visitorid:
            self.visitorid = device_id()
        if not self.did:
            self.did = device_id()


class TransportError(Exception):
    """连不上站点（网络、超时、代理）。"""


# ---------------------------------------------------------------------------
# 客户端
# ---------------------------------------------------------------------------


def _default_transport() -> Any:
    """插件出口：用户在「设置 → 网络」给 pt-keepalive 配的代理自动生效。"""
    from movieclaw_sdk import net

    return net.http_transport(PLUGIN_ID)


@dataclass
class MTeamClient:
    """一轮 M-Team 保活：确保有可用会话 → 确认 profile → 刷新最后浏览时间。"""

    credentials: Credentials
    session: Session = field(default_factory=Session)
    transport: Any = None
    timeout: float = DEFAULT_TIMEOUT
    _http: Any = None

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    # -- 传输 ---------------------------------------------------------------

    async def _client(self) -> Any:
        if self._http is None:
            import httpx

            transport = self.transport if self.transport is not None else _default_transport()
            self._http = httpx.AsyncClient(
                transport=transport,
                timeout=httpx.Timeout(self.timeout, connect=10.0),
                follow_redirects=False,
                trust_env=False,
            )
        return self._http

    def _headers(self, *, auth: bool) -> dict[str, str]:
        headers = {
            "User-Agent": USER_AGENT,
            "referer": REFERER,
            "origin": REFERER,
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Accept": "application/json;charset=UTF-8",
            "Ts": str(int(time.time())),
            "version": CLIENT_VERSION,
            "webversion": WEB_VERSION,
            "Did": self.session.did,
            "visitorid": self.session.visitorid,
        }
        if auth and self.session.token:
            headers["Authorization"] = self.session.token
        return headers

    async def _post(
        self, path: str, *, data: dict[str, str] | None = None, auth: bool = False
    ) -> tuple[Any, str]:
        """发一个签名请求；返回（解析后的 JSON，响应头里的 Authorization）。

        网络错误抛 :class:`TransportError`。``Did`` 在这里顺手更新：服务端每次响应都可能
        滚动下发新的设备号，不跟着更新，下一轮就成了「新设备」。
        """
        import httpx

        self.session.prepare()
        timestamp = int(time.time() * 1000)
        form = {"_timestamp": str(timestamp), "_sgin": sign("POST", path, timestamp), **(data or {})}
        client = await self._client()
        try:
            response = await client.post(
                API_BASE + path, headers=self._headers(auth=auth), data=form
            )
        except httpx.HTTPError as exc:
            raise TransportError(f"{type(exc).__name__}: {exc}") from exc
        did = response.headers.get("Did") or response.headers.get("did") or ""
        if did:
            self.session.did = did
        token = response.headers.get("Authorization") or response.headers.get("authorization") or ""
        try:
            payload = json.loads(response.text) if response.text else None
        except ValueError:
            payload = None
        if payload is None:
            # 非 JSON（网关错误页 / Cloudflare 拦截页）：交给判定层按「未预期」处理
            return {"_text": response.text[:200]}, token
        return payload, token

    # -- 三个动作 -----------------------------------------------------------

    async def _login(self) -> SiteResult | None:
        """登录；成功返回 ``None``，失败直接给本轮结果。"""
        form = {
            "username": self.credentials.username,
            "password": self.credentials.password,
            "turnstile": "",
        }
        payload, token = await self._post("/api/login", data=form)
        verdict = classify(payload)
        if verdict.reply is Reply.NEED_TOTP:
            if not self.credentials.totp_secret:
                return SiteResult(
                    Outcome.FAILED,
                    "该账号开了两步验证，但插件设置里没填 TOTP 密钥（请在设置里补上）",
                    auth_expired=True,
                )
            try:
                code = totp_code(self.credentials.totp_secret)
            except ValueError as exc:
                # 密钥格式错是配置问题：重试无用，也让用户知道该改哪儿
                return SiteResult(Outcome.FAILED, f"两步验证密钥填错了：{exc}", auth_expired=True)
            payload, token = await self._post("/api/login", data={**form, "otpCode": code})
            verdict = classify(payload)
        if verdict.reply is not Reply.OK:
            return self._result_for(verdict)
        if not token:
            return SiteResult(
                Outcome.FAILED, "登录返回成功，但响应头里没有 Authorization 令牌（站点可能改版）"
            )
        self.session.token = token
        return None

    async def _profile(self) -> tuple[Verdict, dict[str, Any]]:
        payload, _token = await self._post("/api/member/profile", auth=True)
        verdict = classify(payload)
        data = payload.get("data") if isinstance(payload, dict) else None
        return verdict, data if isinstance(data, dict) else {}

    async def _update_last_browse(self) -> Verdict:
        payload, _token = await self._post("/api/member/updateLastBrowse", auth=True)
        return classify_update(payload)

    # -- 一轮 ---------------------------------------------------------------

    async def run(self) -> SiteResult:
        """跑一轮：会话若无有效则登录（令牌失效时重登一次），随后刷新最后浏览时间。"""
        try:
            failure = await self._ensure_session()
            if failure is not None:
                return failure
            verdict = await self._update_last_browse()
        except TransportError as exc:
            return SiteResult(Outcome.FAILED, f"连接 M-Team 失败：{exc}")
        if verdict.reply is Reply.OK:
            return SiteResult(Outcome.SIGNIN_OK, "最后浏览时间已刷新")
        if verdict.reply is Reply.NO_CHANGE:
            return SiteResult(
                Outcome.LOGIN_ONLY,
                "登录在线；站点判定无需刷新最后浏览时间（间隔过短或刚被其他工具刷新）",
            )
        return self._result_for(verdict)

    async def _ensure_session(self) -> SiteResult | None:
        """保证手里有可用会话：缓存令牌有效就直接用，失效则重登一次。成功返回 ``None``。

        没有缓存令牌时直接登录，不拿空令牌先去撞 profile（那只会白挨一个 401）。
        """
        if self.session.token:
            verdict, _data = await self._profile()
            if verdict.reply is Reply.OK:
                return None
            if verdict.reply is not Reply.AUTH:
                return self._result_for(verdict)
            # 令牌过期：清掉重登，本轮再试一次
            self.session.token = ""
        return await self._login()

    def _result_for(self, verdict: Verdict) -> SiteResult:
        """站点语义 → 本轮结果（凭据失效才置 ``auth_expired``）。"""
        message = (verdict.message or "").strip()
        if verdict.reply is Reply.AUTH:
            return SiteResult(
                Outcome.FAILED, f"登录被拒：{message or '站点要求重新登录'}", auth_expired=True
            )
        if verdict.reply is Reply.SIGNATURE:
            return SiteResult(
                Outcome.FAILED,
                f"站点判定签名错误（{message}）：前端签名密钥可能已轮换，插件需要更新",
            )
        if verdict.reply is Reply.RATE:
            return SiteResult(Outcome.FAILED, "站点限流（請求過於頻繁），本轮跳过、下一轮再试")
        return SiteResult(Outcome.FAILED, f"站点返回未预期的响应：{message or '（空）'}")
