"""下载器直连 WebUI 底座：qBittorrent WebUI v2 与 Transmission RPC 的 httpx 客户端。

只用「内核宿主操作没有的粒度」：
- 逐种导出 .torrent（qB ≥ 4.5 时）——不是主通道，主通道读磁盘种子文件；
- 指向已有数据加种（savepath + 暂停 / 跳过校验 / 标签）；
- 触发校验、恢复做种（校验完成自动做种的 watcher 用）。

qB ≥ 4.5 的源优先走「磁盘种子文件」（BT_backup），WebUI export 仅当磁盘
文件缺失时兜底；TR 做源只走磁盘（RPC 无导出）。凭据在插件设置里重填
（保险库凭据对插件不可读，这是设计内的），网络走 movieclaw_sdk.net.http_transport。
"""

# 子模块允许 postponed annotations（入口模块不行：配置模型要在导入期解析类型）；
# login() -> QbittorrentWebUI 的类内前向引用依赖它。
from __future__ import annotations

import base64
from typing import Any
from urllib.parse import urlsplit

import httpx

#: qB torrents/export 的最低版本（qBittorrent 4.5.0 / Web API 2.8.14）
QB_EXPORT_MIN_VERSION = (4, 5, 0)


class TransferError(Exception):
    """带中文说明的直连失败；消息直接展示给用户。"""


# ---------------------------------------------------------------------------
# qBittorrent WebUI v2
# ---------------------------------------------------------------------------


class QbittorrentWebUI:
    """登录一次、全程复用 SID cookie 的 qB WebUI 客户端（只用转种需要的端点）。"""

    def __init__(self, client: httpx.AsyncClient):
        self._client = client

    @classmethod
    async def login(
        cls,
        transport: httpx.AsyncBaseTransport,
        base_url: str,
        username: str,
        password: str,
    ) -> QbittorrentWebUI:
        client = httpx.AsyncClient(transport=transport, base_url=base_url.rstrip("/"), timeout=30.0)
        resp = await client.post("/api/v2/auth/login", data={"username": username, "password": password})
        if resp.status_code != 200 or resp.text.strip() != "Ok.":
            raise TransferError(
                f"qBittorrent 登录失败（{base_url}）：HTTP {resp.status_code} {resp.text[:120]}"
                "——请核对插件设置里的 WebUI 地址、用户名与密码"
            )
        return cls(client)

    async def close(self) -> None:
        await self._client.aclose()

    def _check(self, resp: httpx.Response, path: str) -> None:
        if resp.status_code == 403:
            raise TransferError(
                "qBittorrent 返回 403：登录已失效或 WebUI 开启了 Host 校验/白名单，"
                "请在 qB 设置里放行 MovieClaw 所在地址"
            )
        if resp.status_code != 200:
            raise TransferError(f"qBittorrent 接口 {path} 返回 HTTP {resp.status_code}")

    async def version(self) -> str:
        resp = await self._client.get("/api/v2/app/version")
        self._check(resp, "app/version")
        return resp.text.strip()

    async def completed(self) -> list[dict[str, Any]]:
        """filter=completed：下载完成（含做种中 / 已暂停做种）的全部任务。"""
        resp = await self._client.get("/api/v2/torrents/info", params={"filter": "completed"})
        self._check(resp, "torrents/info")
        rows = resp.json()
        if not isinstance(rows, list):
            raise TransferError("qBittorrent torrents/info 返回结构异常")
        return rows

    async def all_hashes(self) -> set[str]:
        """全部任务的 info_hash（判重用）。"""
        resp = await self._client.get("/api/v2/torrents/info")
        self._check(resp, "torrents/info")
        rows = resp.json()
        if not isinstance(rows, list):
            raise TransferError("qBittorrent torrents/info 返回结构异常")
        return {str(r.get("hash") or "").lower() for r in rows if r.get("hash")}

    async def existing_hashes(self, hashes: list[str]) -> set[str]:
        """按 hash 批量探测目标下载器里已有的任务（分批防 URL 超长）。"""
        found: set[str] = set()
        for start in range(0, len(hashes), 200):
            chunk = hashes[start : start + 200]
            resp = await self._client.get("/api/v2/torrents/info", params={"hashes": "|".join(chunk)})
            self._check(resp, "torrents/info")
            found.update(str(row.get("hash") or "").lower() for row in resp.json() or [])
        return found

    async def states(self, hashes: list[str]) -> dict[str, str]:
        """hash → 当前 state 词（watcher 判断是否校验完用）。"""
        rows: list[dict[str, Any]] = []
        for start in range(0, len(hashes), 200):
            chunk = hashes[start : start + 200]
            resp = await self._client.get("/api/v2/torrents/info", params={"hashes": "|".join(chunk)})
            self._check(resp, "torrents/info")
            rows.extend(resp.json() or [])
        return {str(row.get("hash") or "").lower(): str(row.get("state") or "") for row in rows}

    async def trackers(self, info_hash: str) -> list[dict[str, Any]]:
        """该任务的 tracker 行（补 announce 用；伪条目由 qb_tracker_groups 滤掉）。"""
        resp = await self._client.get("/api/v2/torrents/trackers", params={"hash": info_hash})
        self._check(resp, "torrents/trackers")
        rows = resp.json()
        return rows if isinstance(rows, list) else []

    async def export(self, info_hash: str) -> bytes:
        """导出 .torrent 原始字节；404/409 统一提示版本要求。"""
        resp = await self._client.get("/api/v2/torrents/export", params={"hash": info_hash})
        if resp.status_code in (404, 409):
            raise TransferError(
                f"qBittorrent 无法导出 {info_hash} 的种子文件（HTTP {resp.status_code}）："
                "WebUI 导出需要 qBittorrent ≥ 4.5，老版本请配置「源种子文件目录」读磁盘"
            )
        self._check(resp, "torrents/export")
        return resp.content

    async def add(
        self,
        metainfo: bytes,
        save_path: str,
        *,
        paused: bool,
        skip_checking: bool,
        tags: list[str],
    ) -> None:
        """多部分表单加种：指向已有数据（savepath）、默认暂停、可选跳过校验。

        autoTMM 显式关掉，防止目标 qB 的全局「自动管理模式」无视 savepath。
        """
        data: dict[str, str] = {
            "savepath": save_path,
            "autoTMM": "false",
            "paused": "true" if paused else "false",
        }
        if skip_checking:
            data["skip_checking"] = "true"
        if tags:
            data["tags"] = ",".join(tags)
        resp = await self._client.post(
            "/api/v2/torrents/add",
            data=data,
            files={"torrents": ("torrent.bencode", metainfo, "application/x-bittorrent")},
        )
        self._check(resp, "torrents/add")
        if resp.text.strip() != "Ok.":
            raise TransferError(f"qBittorrent 加种被拒：{resp.text[:200]}")

    async def recheck(self, hashes: list[str]) -> None:
        resp = await self._client.post("/api/v2/torrents/recheck", data={"hashes": "|".join(hashes)})
        self._check(resp, "torrents/recheck")

    async def resume(self, hashes: list[str]) -> None:
        resp = await self._client.post("/api/v2/torrents/resume", data={"hashes": "|".join(hashes)})
        self._check(resp, "torrents/resume")

    async def delete(self, hashes: list[str]) -> None:
        """删任务；deleteFiles=false 永不碰数据文件。"""
        resp = await self._client.post(
            "/api/v2/torrents/delete", data={"hashes": "|".join(hashes), "deleteFiles": "false"}
        )
        self._check(resp, "torrents/delete")


def qb_tracker_groups(rows: list[dict[str, Any]]) -> list[list[str]]:
    """qB torrents/trackers 的行 → 按 tier 分组的真实 tracker URL。

    qB 会往列表里塞伪条目（"** [DHT] …"、"*** [PeX] …"、"*** [LSD] …"），
    url 以 * 开头的一律滤掉；tier 缺失按 0 处理，组间按 tier 升序、组内保持原序。
    """
    grouped: dict[int, list[str]] = {}
    for row in rows:
        url = str(row.get("url") or "").strip()
        if not url or url.startswith("*"):
            continue
        try:
            tier = int(row.get("tier") or 0)
        except (TypeError, ValueError):
            tier = 0
        grouped.setdefault(tier, []).append(url)
    return [grouped[tier] for tier in sorted(grouped) if grouped[tier]]


def version_tuple(version: str) -> tuple[int, ...]:
    """'4.6.7' / 'v4.6.7' / '4.6.7beta' → 可比较的数字元组。"""
    digits: list[int] = []
    for part in version.strip().lstrip("vV").split("."):
        head = "".join(ch for ch in part if ch.isdigit())
        if not head:
            break
        digits.append(int(head))
    return tuple(digits)


# ---------------------------------------------------------------------------
# Transmission RPC
# ---------------------------------------------------------------------------


class TransmissionRPC:
    """Transmission RPC 客户端：409 换 X-Transmission-Session-Id 重试一次。"""

    def __init__(self, client: httpx.AsyncClient, path: str, session_id: str = ""):
        self._client = client
        self._path = path
        self._session_id = session_id

    @classmethod
    async def connect(
        cls,
        transport: httpx.AsyncBaseTransport,
        base_url: str,
        username: str,
        password: str,
    ) -> TransmissionRPC:
        parts = urlsplit(base_url)
        path = parts.path if parts.path not in ("", "/") else "/transmission/rpc"
        if not path.endswith("/rpc"):
            path = path.rstrip("/") + "/transmission/rpc"
        client = httpx.AsyncClient(
            transport=transport,
            base_url=f"{parts.scheme}://{parts.netloc}",
            auth=(username, password) if username or password else None,
            timeout=30.0,
        )
        rpc = cls(client, path)
        await rpc.session_get()
        return rpc

    async def close(self) -> None:
        await self._client.aclose()

    async def _rpc(self, method: str, arguments: dict[str, Any]) -> dict[str, Any]:
        payload = {"method": method, "arguments": arguments}
        for attempt in (1, 2):
            headers = {"X-Transmission-Session-Id": self._session_id} if self._session_id else {}
            resp = await self._client.post(self._path, json=payload, headers=headers)
            if resp.status_code == 409 and attempt == 1:
                # TR 约定：409 时响应头带新的会话 id，带上重发即可
                self._session_id = resp.headers.get("x-transmission-session-id", "")
                continue
            if resp.status_code == 401:
                raise TransferError("Transmission 认证失败（401）：请核对插件设置里的用户名与密码")
            if resp.status_code != 200:
                raise TransferError(f"Transmission RPC {method} 返回 HTTP {resp.status_code}")
            body = resp.json()
            if not isinstance(body, dict) or body.get("result") != "success":
                result = body.get("result") if isinstance(body, dict) else resp.text[:120]
                raise TransferError(f"Transmission RPC {method} 报错：{result}")
            return body.get("arguments") or {}
        raise TransferError("Transmission RPC 会话握手失败")

    async def session_get(self) -> dict[str, Any]:
        return await self._rpc("session-get", {})

    async def completed(self) -> list[dict[str, Any]]:
        """percentDone >= 1 的任务（已完成 / 做种中 / 暂停做种）。"""
        args = await self._rpc(
            "torrent-get",
            {"fields": ["hashString", "name", "downloadDir", "percentDone", "labels", "sizeWhenDone"]},
        )
        return [
            t
            for t in args.get("torrents") or []
            if t.get("hashString") and float(t.get("percentDone") or 0) >= 1.0
        ]

    async def all_hashes(self) -> set[str]:
        args = await self._rpc("torrent-get", {"fields": ["hashString"]})
        return {
            str(t.get("hashString") or "").lower()
            for t in args.get("torrents") or []
            if t.get("hashString")
        }

    async def status_of(self, hashes: list[str]) -> dict[str, dict[str, Any]]:
        """hash → {status, percentDone}（watcher 判断是否校验完用）。"""
        args = await self._rpc(
            "torrent-get",
            {"fields": ["hashString", "status", "percentDone"], "ids": hashes},
        )
        return {
            str(t.get("hashString") or "").lower(): t
            for t in args.get("torrents") or []
            if t.get("hashString")
        }

    async def add(
        self,
        metainfo: bytes,
        download_dir: str,
        *,
        paused: bool,
        labels: list[str],
    ) -> str:
        """metainfo（base64）加种，返回 "added" 或 "duplicate"。labels 需 TR ≥ 3.0。"""
        arguments: dict[str, Any] = {
            "metainfo": base64.b64encode(metainfo).decode("ascii"),
            "download-dir": download_dir,
            "paused": paused,
        }
        if labels:
            arguments["labels"] = labels
        args = await self._rpc("torrent-add", arguments)
        if "torrent-duplicate" in args:
            return "duplicate"
        if "torrent-added" in args:
            return "added"
        raise TransferError(f"Transmission 加种响应异常：{list(args)}")

    async def start(self, hashes: list[str]) -> None:
        await self._rpc("torrent-start", {"ids": hashes})

    async def remove(self, hashes: list[str]) -> None:
        """删任务；delete-local-data 恒为 false，永不碰数据文件。"""
        await self._rpc("torrent-remove", {"ids": hashes, "delete-local-data": False})
