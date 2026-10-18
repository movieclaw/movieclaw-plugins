# SPDX-License-Identifier: GPL-3.0-or-later
"""115 网盘后端：把内核契约 ``DriveBackend`` 落到 115 的 Cookie 接口上。

设计见 MovieClaw 仓库 docs/design/cloud-storage-115.md。账号、凭据加密、限速、重试、任务都在宿主，
这里只做协议。凭据里存四个登录 Cookie 与登录端（``app``）。
"""

from __future__ import annotations

import contextlib
import hashlib
import html
import secrets
import time
from typing import Any
from urllib.parse import parse_qs, urlsplit

from movieclaw_sdk import drives as dv

from .api import APP_VERSION, COOKIE_KEYS, Api115, pick_cookies, user_id_of
from .oss import DEFAULT_BUCKET, Oss, UploadGone, part_size_for

#: 扫码可选的登录端（同一端同一时间只能在线一个，新登录会踢掉旧的）
APPS: tuple[dv.Choice, ...] = (
    dv.Choice("alipaymini", "支付宝小程序", "推荐：一般没人用它登录 115"),
    dv.Choice("wechatmini", "微信小程序"),
    dv.Choice("tv", "电视端"),
    dv.Choice("apple_tv", "Apple TV 端"),
    dv.Choice("harmony", "鸿蒙端"),
    dv.Choice("os_linux", "Linux 客户端"),
    dv.Choice("os_mac", "Mac 客户端"),
    dv.Choice("os_windows", "Windows 客户端"),
    dv.Choice("web", "网页端", "会和浏览器里登录的 115 网页版互相踢下线"),
)
DEFAULT_APP = "alipaymini"

#: 操作动态的事件类型 → 交给宿主的种类（浏览、星标、标签这类不改东西的丢掉）
BEHAVIOR_KINDS: dict[int, dv.ChangeKind] = {
    1: "added",  # 上传图片
    2: "added",  # 上传文件
    14: "added",  # 接收文件（转存）
    23: "added",  # 复制文件
    5: "moved",  # 移动图片
    6: "moved",  # 移动文件
    22: "removed",  # 删除
    17: "dir_created",  # 新建目录
    18: "dir_created",  # 复制目录
    20: "renamed",  # 目录改名
    24: "renamed",  # 文件改名
}
DIR_BEHAVIORS = frozenset({17, 18, 20})
#: 一次 ``changes`` 最多翻这么多页（每页 1000 条；网页接口最多能翻到第 1 万条）
BEHAVIOR_PAGE = 1000
BEHAVIOR_MAX_PAGES = 10
#: 内存里记住的文件 id → 提取码（取直链要提取码）
PICKCODE_CACHE = 20000


def pickcode_to_id(pickcode: str) -> int:
    """提取码换文件 id（p115pickcode，插件依赖，用到时才导入，见 ``api.cipher``）。"""
    from p115pickcode import pickcode_to_id as convert

    return convert(pickcode)


def _is_dir(item: dict[str, Any]) -> bool:
    if "fc" in item:
        return str(item["fc"]) == "0"
    return "fid" not in item


def _name(item: dict[str, Any]) -> str:
    return html.unescape(str(item.get("n") or ""))


class Pan115(dv.DriveBackend):
    type = "115"
    title = "115 网盘"
    description = "扫码或浏览器 Cookie 登录；秒传按 SHA1，直链是原画"
    accent_color = "#2777f8"
    capabilities = dv.Capabilities(
        writable=True,
        rapid_upload_hashes=("sha1",),
        range_challenge=True,
        list_hashes=True,
        change_feed=True,
        original_link=True,
        link_needs_ua=True,
        recycle_bin=True,
        default_qps=1.2,
        default_daily_budget=8000,
        default_read_slots=2,
    )
    login = dv.Login(
        scan=True,
        cookie_domains=("115.com",),
        options=(
            dv.LoginOption(
                key="app",
                label="登录端",
                choices=APPS,
                default=DEFAULT_APP,
                help=(
                    "115 每种登录端同一时间只能在线一个。"
                    "请选一个你自己和别的工具（CloudDrive2、OpenList 等）都没在用的端。"
                ),
            ),
        ),
        hint="用 115 App 扫码。扫码登录比浏览器 Cookie 稳定：网页端的登录态会和浏览器互相踢下线。",
    )

    def __init__(self, api: Api115 | None = None) -> None:
        self._api = api or Api115()
        self._oss = Oss(self._api)
        self._flows: dict[str, dict[str, Any]] = {}
        self._pickcodes: dict[str, str] = {}
        self._userkeys: dict[str, str] = {}
        self._dirs: dict[tuple[str, str, str], str] = {}

    # ------------------------------------------------------------------ 登录

    async def begin_login(self, options: dict[str, str]) -> dv.LoginFlow:
        app = options.get("app") or DEFAULT_APP
        if app not in {c.value for c in APPS}:
            raise ValueError(f"不认识的登录端：{app}")
        token = await self._api.qr_token(app)
        flow_id = secrets.token_urlsafe(12)
        self._flows[flow_id] = {"app": app, "token": token, "status": "pending"}
        qr = token.get("qrcode") or f"https://115.com/scan/dg-{token['uid']}"
        return dv.LoginFlow(flow_id=flow_id, status="pending", message="用 115 App 扫码", qr=qr)

    async def login_state(self, flow_id: str) -> dv.LoginFlow:
        flow = self._flows.get(flow_id)
        if flow is None:
            return dv.LoginFlow(flow_id=flow_id, status="failed", message="这次扫码已经结束，请重新开始")
        status = await self._api.qr_status(flow["token"])
        if status is None or status == 0:
            return dv.LoginFlow(flow_id=flow_id, status=flow["status"], message=_waiting(flow["status"]))
        if status == 1:
            flow["status"] = "scanned"
            return dv.LoginFlow(flow_id=flow_id, status="scanned", message=_waiting("scanned"))
        self._flows.pop(flow_id, None)
        if status == 2:
            cookies = await self._api.qr_login(flow["app"], flow["token"]["uid"])
            result = await self._result(cookies, flow["app"])
            return dv.LoginFlow(flow_id=flow_id, status="confirmed", message="已登录", result=result)
        if status == -1:
            return dv.LoginFlow(flow_id=flow_id, status="expired", message="二维码已过期，请重新扫码")
        if status == -2:
            return dv.LoginFlow(flow_id=flow_id, status="failed", message="已在手机上取消登录")
        return dv.LoginFlow(flow_id=flow_id, status="failed", message=f"扫码登录没成功（状态 {status}）")

    async def cancel_login(self, flow_id: str) -> None:
        self._flows.pop(flow_id, None)

    async def login_with_cookie(self, cookie_header: str) -> dv.LoginResult:
        cookies = pick_cookies(cookie_header)
        missing = [k for k in COOKIE_KEYS if k not in cookies]
        if missing:
            raise ValueError(f"Cookie 里缺少 115 的登录项：{'、'.join(missing)}")
        if not await self._api.logged_in(cookies):
            raise dv.AuthExpired("这份 Cookie 已经登录失效，请在浏览器里重新登录 115 后再导入")
        return await self._result(cookies, "web")

    async def _result(self, cookies: dict[str, str], app: str) -> dv.LoginResult:
        user_id = user_id_of(cookies)
        try:
            nav = await self._api.user_nav(cookies)
        except dv.DriveError:
            nav = {}
        name = str(nav.get("user_name") or f"115 用户 {user_id}")
        return dv.LoginResult(user_id=user_id, display_name=name, credentials={**cookies, "app": app})

    # ------------------------------------------------------------------ 账号

    def _cookies(self, account: dv.DriveAccount) -> dict[str, str]:
        cookies = {k: account.credentials.get(k, "") for k in COOKIE_KEYS}
        if not all(cookies.values()):
            raise dv.AuthExpired("115 登录信息不全，请重新登录")
        return cookies

    async def account_info(self, account: dv.DriveAccount) -> dv.AccountInfo:
        cookies = self._cookies(account)
        if not await self._api.logged_in(cookies):
            raise dv.AuthExpired("115 登录已失效（可能在别处用同一登录端登录了），请重新扫码")
        try:
            nav = await self._api.user_nav(cookies)
        except dv.DriveError:
            nav = {}
        total, used = await self._api.space(cookies)
        vip = "会员" if nav.get("vip") else None
        name = str(nav.get("user_name") or f"115 用户 {account.user_id}")
        return dv.AccountInfo(display_name=name, vip=vip, quota_total=total, quota_used=used)

    # ------------------------------------------------------------------ 目录

    async def _pages(self, cookies: dict[str, str], cid: str, **params: Any):
        """翻完一次列举的所有页；``cid`` 不存在时 115 会悄悄列根目录，这里改成 ``NotFound``。"""
        offset = 0
        while True:
            data = await self._api.files(cookies, cid=cid, offset=offset, **params)
            if str(data.get("cid")) != str(cid):
                raise dv.NotFound("115 上没有这个目录")
            items = data.get("data") or []
            yield data, items
            offset += len(items)
            if not items or offset >= int(data.get("count") or 0):
                return

    async def list_dir(self, account: dv.DriveAccount, dir_id: str) -> list[dv.RemoteDir]:
        cookies = self._cookies(account)
        dirs: list[dv.RemoteDir] = []
        async for _, items in self._pages(cookies, dir_id or "0", cur=1, nf=1):
            for item in items:
                if _is_dir(item):
                    dirs.append(dv.RemoteDir(remote_id=str(item["cid"]), name=_name(item)))
        return dirs

    async def list_tree(self, account: dv.DriveAccount, dir_id: str) -> dv.RemoteTree:
        cookies = self._cookies(account)
        parents: dict[str, tuple[str, str]] = {}  # 目录 id → (父目录 id, 名称)
        async for _, items in self._pages(cookies, dir_id, cur=0, nf=1):
            for item in items:
                if _is_dir(item):
                    parents[str(item["cid"])] = (str(item.get("pid") or ""), _name(item))
        files: list[tuple[dict[str, Any], str]] = []
        async for _, items in self._pages(cookies, dir_id, cur=0, show_dir=0):
            files.extend((item, str(item.get("cid"))) for item in items if not _is_dir(item))
        # 整树列目录漏掉的（以防万一）：按祖先链补齐
        for _, parent in files:
            if parent != dir_id and parent not in parents:
                await self._fill_ancestors(cookies, parent, parents)
        paths: dict[str, str | None] = {dir_id: ""}

        def path_of(cid: str, depth: int = 0) -> str | None:
            if cid in paths:
                return paths[cid]
            if cid not in parents or depth > 64:
                paths[cid] = None  # 不在被列目录底下
                return None
            pid, name = parents[cid]
            base = path_of(pid, depth + 1)
            paths[cid] = None if base is None else f"{base}{name}/"
            return paths[cid]

        out: list[dv.RemoteFile] = []
        for item, parent in files:
            base = path_of(parent)
            if base is None:
                continue
            remote_id = str(item["fid"])
            if item.get("pc"):
                self._remember(remote_id, str(item["pc"]))
            out.append(
                dv.RemoteFile(
                    remote_id=remote_id,
                    name=_name(item),
                    size=int(item.get("s") or 0),
                    sha1=str(item.get("sha") or "").upper() or None,
                    parent_id=parent,
                    rel_path=base + _name(item),
                )
            )
        dir_ids = tuple(cid for cid in parents if path_of(cid) is not None)
        return dv.RemoteTree(files=tuple(out), dir_ids=dir_ids)

    async def _fill_ancestors(self, cookies: dict[str, str], cid: str, parents: dict[str, tuple[str, str]]) -> None:
        data = await self._api.files(cookies, cid=cid, offset=0, limit=1, nf=1, cur=1)
        chain = data.get("path") or []
        for node in chain:
            node_id = str(node.get("cid"))
            if node_id != "0" and node_id not in parents:
                parents[node_id] = (str(node.get("pid") or "0"), html.unescape(str(node.get("name") or "")))

    async def changes(self, account: dv.DriveAccount, cursor: str | None) -> dv.ChangePage:
        cookies = self._cookies(account)
        if cursor is None:
            data = await self._api.behavior(cookies, 0, 1)
            events = data.get("list") or []
            return dv.ChangePage(cursor=str(events[0]["id"]) if events else "0")
        last = int(cursor or 0)
        newest = last
        found: list[dict[str, Any]] = []
        for page in range(BEHAVIOR_MAX_PAGES):
            data = await self._api.behavior(cookies, page * BEHAVIOR_PAGE, BEHAVIOR_PAGE)
            events = data.get("list") or []
            reached = False
            for event in events:  # 新的在前
                event_id = int(event.get("id") or 0)
                if event_id <= last:
                    reached = True
                    break
                newest = max(newest, event_id)
                found.append(event)
            if reached or len(events) < BEHAVIOR_PAGE:
                break
        # 翻到头还没接上游标：中间漏掉的交给宿主的每周复查
        changes: list[dv.Change] = []
        for event in reversed(found):
            kind = BEHAVIOR_KINDS.get(int(event.get("type") or 0))
            if kind is None or not event.get("file_id"):
                continue
            is_dir = int(event.get("type") or 0) in DIR_BEHAVIORS or not event.get("sha1")
            parent = event.get("parent_id")
            changes.append(
                dv.Change(
                    kind=kind,
                    remote_id=str(event["file_id"]),
                    is_dir=is_dir,
                    name=html.unescape(str(event.get("file_name") or "")),
                    parent_id=str(parent) if parent not in (None, "") else None,
                )
            )
            if not is_dir and event.get("pick_code"):
                self._remember(str(event["file_id"]), str(event["pick_code"]))
        return dv.ChangePage(cursor=str(newest), changes=tuple(changes))

    async def ensure_dir(self, account: dv.DriveAccount, parent_id: str, path: str) -> str:
        cookies = self._cookies(account)
        current = parent_id
        for name in (part for part in path.split("/") if part):
            key = (account.user_id, current, name)
            cached = self._dirs.get(key)
            if cached is None:
                cached = await self._find_child(cookies, current, name)
                if cached is None:
                    try:
                        cached = await self._api.mkdir(cookies, current, name)
                    except dv.Conflict:
                        cached = await self._find_child(cookies, current, name)
                        if cached is None:
                            raise
                self._dirs[key] = cached
            current = cached
        return current

    async def _find_child(self, cookies: dict[str, str], parent_id: str, name: str) -> str | None:
        async for _, items in self._pages(cookies, parent_id or "0", cur=1, nf=1):
            for item in items:
                if _is_dir(item) and _name(item) == name:
                    return str(item["cid"])
        return None

    # ------------------------------------------------------------------ 上传

    async def begin_upload(
        self,
        account: dv.DriveAccount,
        dir_id: str,
        name: str,
        fingerprint: dv.Fingerprint,
        state: dict[str, Any] | None,
        range_data: bytes | None,
    ) -> dv.UploadStep:
        cookies = self._cookies(account)
        if state and state.get("upload_id"):
            try:
                parts = await self._oss.uploaded_parts(state["bucket"], state["object"], state["upload_id"])
            except UploadGone:
                state = None  # OSS 上的分片上传没了：从头来
            else:
                etags: list[str] = []
                while len(etags) + 1 in parts:
                    etags.append(parts[len(etags) + 1])
                state = {**state, "etags": etags}
                return dv.UploadStep(part_size=state["part_size"], next_part=len(etags), state=state)
        sign_key = sign_val = ""
        if state and state.get("sign_key") and range_data is not None:
            sign_key = str(state["sign_key"])
            sign_val = hashlib.sha1(range_data).hexdigest().upper()
        payload = {
            "appid": 0,
            "appversion": APP_VERSION,
            "topupload": "true",
            "target": f"U_1_{dir_id}",
            "sign_key": sign_key,
            "sign_val": sign_val,
            "fileid": fingerprint.sha1.upper(),
            "filename": name,
            "filesize": fingerprint.size,
            "path": "",
            "userid": account.user_id,
            "userkey": await self._userkey(account, cookies),
        }
        data = await self._api.upload_init(cookies, payload)
        status = int(data.get("status") or 0)
        if status == 2:
            pickcode = str(data["pickcode"])
            remote_id = str(pickcode_to_id(pickcode))
            self._remember(remote_id, pickcode)
            return dv.UploadStep(
                file=dv.RemoteFile(
                    remote_id=remote_id,
                    name=name,
                    size=fingerprint.size,
                    sha1=fingerprint.sha1.upper(),
                    parent_id=dir_id,
                )
            )
        if status in (6, 7, 8) and data.get("sign_check"):
            start, _, end = str(data["sign_check"]).partition("-")
            return dv.UploadStep(
                need_range=(int(start), int(end) - int(start) + 1), state={"sign_key": data["sign_key"]}
            )
        if status == 1 and data.get("object"):
            bucket = str(data.get("bucket") or DEFAULT_BUCKET)
            key = str(data["object"])
            callback = data.get("callback") or {}
            if not callback.get("callback") or not callback.get("callback_var"):
                raise dv.ApiChanged("115 的上传初始化没有给出回调参数，可能需要更新插件")
            upload_id = await self._oss.start(bucket, key)
            new_state = {
                "pickcode": str(data.get("pickcode") or ""),
                "bucket": bucket,
                "object": key,
                "callback": {"callback": callback["callback"], "callback_var": callback["callback_var"]},
                "upload_id": upload_id,
                "part_size": part_size_for(fingerprint.size),
                "etags": [],
                "name": name,
                "size": fingerprint.size,
                "sha1": fingerprint.sha1.upper(),
                "dir_id": dir_id,
            }
            return dv.UploadStep(part_size=new_state["part_size"], next_part=0, state=new_state)
        message = data.get("statusmsg") or data.get("message") or data.get("error") or f"状态 {status}"
        if status == 0 and data.get("statuscode") in (1001, "1001"):
            self._userkeys.pop(account.user_id, None)
        raise dv.DriveError(f"115 上传初始化失败：{message}")

    async def _userkey(self, account: dv.DriveAccount, cookies: dict[str, str]) -> str:
        key = self._userkeys.get(account.user_id)
        if key is None:
            key = self._userkeys[account.user_id] = await self._api.upload_key(cookies)
        return key

    async def upload_part(
        self, account: dv.DriveAccount, state: dict[str, Any], index: int, data: bytes
    ) -> dict[str, Any]:
        try:
            etag = await self._oss.put_part(state["bucket"], state["object"], state["upload_id"], index + 1, data)
        except UploadGone as exc:
            raise dv.Transient("OSS 上的分片上传已过期，稍后从头传") from exc
        etags = list(state.get("etags") or [])[:index]
        etags.append(etag)
        return {**state, "etags": etags}

    async def finish_upload(self, account: dv.DriveAccount, state: dict[str, Any]) -> dv.RemoteFile:
        try:
            result = await self._oss.complete(
                state["bucket"], state["object"], state["upload_id"], list(state["etags"]), state["callback"]
            )
        except UploadGone as exc:
            raise dv.Transient("OSS 上的分片上传已过期，稍后从头传") from exc
        info = result.get("data") if isinstance(result.get("data"), dict) else {}
        pickcode = str(info.get("pick_code") or state.get("pickcode") or "")
        remote_id = str(info.get("file_id") or (pickcode_to_id(pickcode) if pickcode else ""))
        if not remote_id:
            raise dv.ApiChanged("115 接收了上传，但没有给出文件 id")
        if pickcode:
            self._remember(remote_id, pickcode)
        return dv.RemoteFile(
            remote_id=remote_id,
            name=state["name"],
            size=int(state["size"]),
            sha1=state["sha1"],
            parent_id=state.get("dir_id"),
        )

    # ------------------------------------------------------------------ 删除与文件

    async def delete(self, account: dv.DriveAccount, remote_ids: list[str]) -> None:
        cookies = self._cookies(account)
        if not remote_ids:
            return
        try:
            await self._api.delete(cookies, remote_ids)
        except dv.NotFound:
            # 有的已经不在了：逐个删，不在的当删成功
            for remote_id in remote_ids:
                with contextlib.suppress(dv.NotFound):
                    await self._api.delete(cookies, [remote_id])
        for remote_id in remote_ids:
            self._pickcodes.pop(remote_id, None)

    async def stat(self, account: dv.DriveAccount, remote_id: str) -> dv.RemoteFile:
        cookies = self._cookies(account)
        item = await self._api.file_info(cookies, remote_id)
        if _is_dir(item):
            raise dv.NotFound("这是一个目录，不是文件")
        if item.get("pc"):
            self._remember(remote_id, str(item["pc"]))
        return dv.RemoteFile(
            remote_id=remote_id,
            name=_name(item),
            size=int(item.get("s") or 0),
            sha1=str(item.get("sha") or "").upper() or None,
            parent_id=str(item.get("cid")) if item.get("cid") is not None else None,
        )

    async def direct_link(self, account: dv.DriveAccount, remote_id: str, user_agent: str) -> dv.DirectLink:
        cookies = self._cookies(account)
        pickcode = self._pickcodes.get(remote_id)
        if pickcode is None:
            await self.stat(account, remote_id)
            pickcode = self._pickcodes.get(remote_id)
            if pickcode is None:
                raise dv.ApiChanged("115 没有给出这个文件的提取码")
        items = await self._api.download_url(cookies, account.user_id, pickcode, user_agent)
        entry = items.get(remote_id) or next(iter(items.values()))
        link = entry.get("url") if isinstance(entry, dict) else None
        url = link.get("url") if isinstance(link, dict) else link
        if not url:
            raise dv.NotFound("115 没有给出这个文件的下载地址")
        return dv.DirectLink(url=str(url), expires_in=_expires_in(str(url)))

    def _remember(self, remote_id: str, pickcode: str) -> None:
        if len(self._pickcodes) >= PICKCODE_CACHE:
            self._pickcodes.pop(next(iter(self._pickcodes)))
        self._pickcodes[remote_id] = pickcode


def _waiting(status: str) -> str:
    return "已扫码，请在手机上确认登录" if status == "scanned" else "用 115 App 扫码"


def _expires_in(url: str) -> float:
    """直链里的 ``t`` 是过期时间戳；留一分钟余量，拿不到就按 5 分钟。"""
    try:
        expires = int(parse_qs(urlsplit(url).query)["t"][0])
    except (KeyError, ValueError, IndexError):
        return 300.0
    return float(max(60, min(3600, expires - time.time() - 60)))
