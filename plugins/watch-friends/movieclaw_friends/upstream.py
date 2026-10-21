"""MovieClaw 只读适配器：只调用现有 GET 接口，不依赖宿主 Python 包。"""

from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import urlsplit

import httpx

from .media import sanitize_media as _media


class UpstreamError(Exception):
    """可直接映射为插件 API 错误的中文消息；不包含上游正文或凭据。"""

    def __init__(self, message: str, status_code: int = 502) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def _record(value: dict) -> dict:
    fields = (
        "id",
        "started_at",
        "ended_at",
        "watched_ms",
        "start_position_ms",
        "end_position_ms",
        "duration_ms",
        "progress_percent",
        "completed",
    )
    result = {key: value[key] for key in fields if key in value}
    result["media"] = _media(value["media"])
    return result


def _shareable_collection(value: dict) -> bool:
    # 可见不等于有权分享：成员可见的全家片单可能由别人创建。宿主不返回创建者
    # ID，普通成员以 manageable/editable 证明归属；管理员只能确认可管理范围。
    # 老版本缺任一权限字段时不作推断，防止默认值把系统或他人的片单放进来。
    return (
        value.get("manageable") is True
        and value.get("editable") is True
        and "builtin" in value
        and value["builtin"] is None
        and value.get("kind") == "user"
    )


def _ongoing(value: dict, now: datetime) -> bool:
    if "ended_at" not in value or value["ended_at"] is not None:
        return False
    # 当前宿主不返回 last_seen_at，而是在 GET /history 内按 300 秒心跳保鲜期
    # 计算 ended_at。不能拿 started_at 当心跳：看两小时的电影也可能仍在播放。
    # 若后续宿主补充心跳字段，再额外检查；这里只推断在播，不声称知道暂停状态。
    heartbeat = value.get("last_seen_at")
    if heartbeat is None:
        return True
    try:
        seen = datetime.fromisoformat(heartbeat.replace("Z", "+00:00"))
        if seen.tzinfo is None:
            seen = seen.replace(tzinfo=UTC)
        return 0 <= (now - seen).total_seconds() <= 300
    except (AttributeError, TypeError, ValueError):
        return False


class MovieClawClient:
    """仅通过现有鉴权 GET 读取数据，好友关系与授权全部由插件自己维护。

    管理员凭据只用于本机读取成员映射及逐成员历史。私有片单始终使用所有者
    的凭据读取，不能用管理员身份冒充用户。所有外发数据均经过字段白名单。
    "只读"指不调用业务写接口；宿主鉴权仍可能自行记录设备最近访问时间。
    """

    def __init__(self, base_url: str, admin_token: str = "", transport=None) -> None:
        try:
            parsed = urlsplit(base_url.strip())
            _ = parsed.port
        except ValueError:
            raise UpstreamError("MovieClaw 服务地址无效，请填写 HTTP 或 HTTPS 地址", 400) from None
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise UpstreamError("MovieClaw 服务地址无效，请填写 HTTP 或 HTTPS 地址", 400)
        base = base_url.strip().rstrip("/")
        self.base_url = base if base.endswith("/api/v1") else f"{base}/api/v1"
        self.admin_token = admin_token
        self.transport = transport

    async def _get(self, path: str, token: str, params: dict | None = None):
        if not token:
            raise UpstreamError("缺少 MovieClaw 登录令牌，请先连接账号", 401)
        try:
            async with httpx.AsyncClient(
                base_url=f"{self.base_url}/",
                timeout=8,
                follow_redirects=False,
                transport=self.transport,
            ) as client:
                response = await client.get(
                    path.lstrip("/"),
                    params=params,
                    headers={"Authorization": f"Bearer {token}"},
                )
        except httpx.RequestError:
            raise UpstreamError("无法连接 MovieClaw，请检查服务地址和网络") from None
        if response.status_code == 401:
            raise UpstreamError("MovieClaw 登录令牌无效或已过期，请重新连接账号", 401)
        if response.status_code == 403:
            raise UpstreamError("当前 MovieClaw 凭据没有读取这些数据的权限", 403)
        if response.status_code == 404:
            raise UpstreamError("MovieClaw 接口或片单不存在，或当前账号不可见", 404)
        if not response.is_success:
            raise UpstreamError("MovieClaw 读取失败，请检查服务状态与地址")
        try:
            body = response.json()
        except ValueError:
            raise UpstreamError("MovieClaw 返回的数据不是有效 JSON") from None
        if not isinstance(body, dict) or body.get("success") is not True or "data" not in body:
            raise UpstreamError("MovieClaw 返回的数据格式不受支持")
        return body["data"]

    async def identity(self, token: str) -> dict:
        """每次向宿主验证令牌，按唯一用户名映射成员，绝不使用可重复的昵称。"""
        data = await self._get("auth/me", token)
        if not isinstance(data, dict) or not isinstance(data.get("username"), str):
            raise UpstreamError("MovieClaw 未返回有效的账号身份")
        role = data.get("role")
        if role not in ("admin", "member"):
            raise UpstreamError("当前 MovieClaw 版本未提供可识别的成员身份")
        member_id = 0 if role == "admin" else None
        if role == "member" and self.admin_token:
            members = await self._get("members", self.admin_token)
            if not isinstance(members, list):
                raise UpstreamError("MovieClaw 返回的成员列表格式不受支持")
            matches = [
                row
                for row in members
                if isinstance(row, dict) and row.get("username") == data["username"]
            ]
            if len(matches) != 1 or type(matches[0].get("id")) is not int or matches[0]["id"] <= 0:
                raise UpstreamError("无法按唯一用户名确认 MovieClaw 成员，请联系服务器管理员", 403)
            member_id = matches[0]["id"]
        # 未配置管理员读取凭据时仍可登录及分享本人片单，但无法确认历史所属成员。
        return {
            "username": data["username"],
            "nickname": data.get("nickname") or data["username"],
            "role": role,
            "member_id": member_id,
        }

    async def _history(self, member_id: int, token: str) -> list:
        if type(member_id) is not int or member_id < 0:
            raise UpstreamError("尚未确认 MovieClaw 成员身份，无法同步观看记录", 403)
        data = await self._get(
            "playback/history",
            token,
            {"member_id": member_id, "scope": "all", "limit": 50},
        )
        if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
            raise UpstreamError("MovieClaw 返回的观看记录格式不受支持")
        entries = data["entries"]
        if any(
            not isinstance(row, dict) or not isinstance(row.get("media"), dict) for row in entries
        ):
            raise UpstreamError("MovieClaw 返回的观看记录格式不受支持")
        return entries

    async def history(self, member_id: int) -> list:
        if not self.admin_token:
            raise UpstreamError("未配置本机管理员读取凭据，无法读取成员观看记录", 403)
        return [_record(row) for row in await self._history(member_id, self.admin_token)]

    async def collections(self, token: str) -> list:
        data = await self._get("collections", token)
        if not isinstance(data, list) or any(not isinstance(row, dict) for row in data):
            raise UpstreamError("MovieClaw 返回的片单列表格式不受支持")
        return [
            {key: row.get(key) for key in ("id", "name", "item_count")}
            for row in data
            if _shareable_collection(row)
        ]

    async def collection(self, token: str, id: int) -> dict:
        if type(id) is not int or id < 1:
            raise UpstreamError("片单编号必须是正整数", 400)
        data = await self._get(f"collections/{id}", token)
        if not isinstance(data, dict) or data.get("id") != id:
            raise UpstreamError("MovieClaw 返回的片单格式不受支持")
        if not _shareable_collection(data):
            raise UpstreamError(
                "无法确认片单归属：仅可共享当前账号可管理的自建片单，请检查权限或升级宿主版本",
                403,
            )
        if isinstance(data.get("item_count"), int) and data["item_count"] > 500:
            raise UpstreamError("片单超过 500 部，当前版本暂不支持共享，请先选择较小的片单", 400)
        items = []
        offset = 0
        while True:
            page = await self._get(
                f"collections/{id}/items", token, {"limit": 200, "offset": offset}
            )
            if not isinstance(page, list) or any(not isinstance(row, dict) for row in page):
                raise UpstreamError("MovieClaw 返回的片单内容格式不受支持")
            # 同时限制实际分页累计，避免计数缓存过旧或宿主忽略 offset 时无限读取。
            if len(items) + len(page) > 500:
                raise UpstreamError(
                    "片单超过 500 部，当前版本暂不支持共享，请先选择较小的片单", 400
                )
            items.extend(_media(row) for row in page)
            if len(page) < 200:
                break
            offset += len(page)
        return {"id": id, "name": data.get("name", ""), "items": items}

    async def snapshot(
        self,
        token: str,
        member_id: int | None,
        share_current: bool,
        share_history: bool,
    ) -> dict:
        identity = await self.identity(token)
        if identity["member_id"] != member_id:
            raise UpstreamError("MovieClaw 账号与已绑定成员不一致，请重新连接账号", 403)
        now = datetime.now(UTC)
        entries = []
        if share_current or share_history:
            if member_id is None:
                raise UpstreamError("未配置本机管理员读取凭据，无法确认成员及同步观看记录", 403)
            reader = token if identity["role"] == "admin" else self.admin_token
            entries = await self._history(member_id, reader)
        current = next((row for row in entries if _ongoing(row, now)), None)
        return {
            "current": _record(current) if share_current and current is not None else None,
            # 历史授权不能旁路当前授权：尚未结束的场次只放在 current 里。
            "history": [_record(row) for row in entries if row.get("ended_at") is not None]
            if share_history
            else [],
            "sharing": {"current": bool(share_current), "history": bool(share_history)},
            "observed_at": now.isoformat(),
        }
