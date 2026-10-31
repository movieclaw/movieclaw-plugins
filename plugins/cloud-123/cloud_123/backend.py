"""123 DriveBackend：逐契约实现账号、登录与目录。"""

from __future__ import annotations

import secrets
import time
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode, urlsplit

from pydantic import BaseModel, Field, SecretStr, ValidationError

from movieclaw_sdk import drives as dv

from .client import list_page, mkdir, qr_generate, qr_result, trash, user_info
from . import readback
from .upload import MAX_PART_BYTES, Uploads, binding_for
from .upload_store import valid_ref


# 与宿主登录窗口一致的本地保留期，不代表上游二维码保证有效十分钟。
LOGIN_TTL_S = 10 * 60


@dataclass
class _LoginSession:
    uni_id: str
    expires_at: float
    flow: dv.LoginFlow


class TokenCredentials(BaseModel):
    token: SecretStr = Field(title="Token", description="123 云盘 Token，可带 Bearer 前缀")


def _identity(data: dict[str, Any]) -> tuple[str, str]:
    uid = data.get("UID")
    if not (
        type(uid) is int and uid > 0
        or isinstance(uid, str) and uid.isascii() and uid.isdecimal() and int(uid) > 0
    ):
        raise dv.ApiChanged("123 账号查询缺少有效 UID")
    name = data.get("Nickname")
    return str(uid), name.strip() if isinstance(name, str) and name.strip() else f"123 用户 {uid}"


def _account_token(account: dv.DriveAccount) -> str:
    token = account.credentials.get("token")
    if not isinstance(token, str) or not token.strip():
        raise dv.AuthExpired("123 缺少 Token，请重新登录")
    # 导入时的规范化留给 login_with_form；存储中的非法头值不能进入 HTTP 请求。
    if not token.isascii() or any(character.isspace() for character in token):
        raise dv.Conflict("123 Token 格式不正确，请重新登录")
    return token


def _list_integer(value: Any) -> int:
    if type(value) is int:
        return value
    if isinstance(value, str) and value.isascii() and (value.isdecimal() or value == "-1"):
        try:
            return int(value)
        except ValueError:
            pass
    raise dv.ApiChanged("123 目录查询缺少有效 id 或游标")


def _dir_id(dir_id: str) -> str:
    if not isinstance(dir_id, str) or (dir_id and not (dir_id.isascii() and dir_id.isdecimal())):
        raise dv.NotFound("123 目录 id 格式不正确")
    return dir_id.lstrip("0") or "0"


def _file_id(remote_id: str) -> str:
    if (not isinstance(remote_id, str) or not remote_id.isascii() or not remote_id.isdecimal()
            or len(remote_id) > 20 or int(remote_id) <= 0):
        raise dv.NotFound("123 文件 id 格式不正确")
    return str(int(remote_id))


async def _dir_entries(token: str, parent_id: str) -> AsyncIterator[tuple[str, int, str, dict[str, Any]]]:
    cursor = 0
    while True:
        data = await list_page(token, parent_id, cursor)
        items = data.get("InfoList")
        next_cursor = _list_integer(data.get("Next"))
        if not isinstance(items, list) or next_cursor < -1:
            raise dv.ApiChanged("123 目录查询返回了无效列表或游标")
        if next_cursor != -1 and (not items or next_cursor <= cursor):
            raise dv.ApiChanged("123 目录分页未前进，无法确认列表完整性")
        for item in items:
            if not isinstance(item, dict):
                raise dv.ApiChanged("123 目录查询返回了无效条目")
            fid = _list_integer(item.get("FileId"))
            kind, name = item.get("Type"), item.get("FileName")
            if (fid <= 0 or type(kind) is not int or kind not in (0, 1)
                    or not isinstance(name, str) or not name or name in (".", "..")
                    or any(c in name for c in ("/", "\\", "\x00"))):
                raise dv.ApiChanged("123 目录查询返回了无效 id、类型或名称")
            remote_id = str(fid)
            if remote_id == parent_id or (
                "ParentFileId" in item and str(_list_integer(item["ParentFileId"])) != parent_id
            ):
                raise dv.Transient("123 目录关系发生变化，请重新查询")
            trashed = item.get("Trashed", False)
            if type(trashed) not in (bool, int) or trashed not in (0, 1):
                raise dv.ApiChanged("123 目录查询返回了无效回收站状态")
            if trashed:
                raise dv.Transient("123 普通目录返回了回收站条目，请重新查询")
            yield remote_id, kind, name, item
        if next_cursor == -1:
            return
        cursor = next_cursor


async def _find_dir(token: str, parent_id: str, name: str) -> str | None:
    found = None
    seen: dict[str, tuple[int, str]] = {}
    async for remote_id, kind, child_name, _ in _dir_entries(token, parent_id):
        signature = (kind, child_name)
        if remote_id in seen and seen[remote_id] != signature:
            raise dv.Transient("123 同一条目的目录信息发生变化，请重新查询")
        seen[remote_id] = signature
        if child_name != name:
            continue
        if kind == 0:
            raise dv.Conflict("123 目标目录名已被文件占用")
        if found is not None and found != remote_id:
            raise dv.Conflict("123 存在多个同名目录，无法确定目标")
        found = remote_id
    return found


class Pan123(dv.DriveBackend):
    type = "123"
    title = "123 云盘"
    capabilities = dv.Capabilities(
        writable=True, rapid_upload_hashes=("md5",), recycle_bin=True,
    )
    login = dv.Login(scan=True, form=TokenCredentials)

    def __init__(self, store=None) -> None:
        self._flows: dict[str, _LoginSession] = {}
        self._uploads = Uploads(store, _dir_entries)

    async def begin_upload(self, account, dir_id, name, fingerprint, state, range_data) -> dv.UploadStep:
        parent_id = _dir_id(dir_id)
        if len(parent_id) > 20:
            raise dv.NotFound("123 目录 id 格式不正确")
        binding = binding_for(account.user_id, parent_id, name, fingerprint, range_data)
        token = _account_token(account)
        uid, _ = _identity(await user_info(token))
        if uid != binding["user_id"]:
            raise dv.Conflict("123 凭据对应的用户与当前账号不一致，请重新登录")
        return await self._uploads.begin(token, binding, state)

    async def upload_part(self, account, state, index, data):
        if (not valid_ref(state) or type(index) is not int or index < 0
                or not isinstance(data, bytes) or not 0 < len(data) <= MAX_PART_BYTES):
            raise dv.Conflict("123 分片引用、序号或字节长度无效")
        token = _account_token(account)
        uid, _ = _identity(await user_info(token))
        if uid != account.user_id:
            raise dv.Conflict("123 凭据对应的用户与当前账号不一致，请重新登录")
        return await self._uploads.part(token, uid, state, index, data)

    async def finish_upload(self, account, state) -> dv.RemoteFile:
        if not valid_ref(state):
            raise dv.Conflict("123 上传恢复引用无效")
        token = _account_token(account)
        uid, _ = _identity(await user_info(token))
        if uid != account.user_id:
            raise dv.Conflict("123 凭据对应的用户与当前账号不一致，请重新登录")
        return await self._uploads.finish(token, uid, state)

    async def stat(self, account: dv.DriveAccount, remote_id: str) -> dv.RemoteFile:
        remote_id = _file_id(remote_id)
        info = await readback.checked_info(_account_token(account), remote_id)
        return dv.RemoteFile(remote_id, info["FileName"], readback.integer(info["Size"]),
                             parent_id=str(readback.integer(info.get("ParentFileId"))), sha1=None)

    async def direct_link(self, account: dv.DriveAccount, remote_id: str, user_agent: str) -> dv.DirectLink:
        remote_id = _file_id(remote_id)
        if (not isinstance(user_agent, str) or not user_agent.isascii()
                or any(ord(c) < 32 or ord(c) == 127 for c in user_agent)):
            raise dv.Conflict("123 取链 User-Agent 格式不正确")
        token = _account_token(account)
        info = await readback.checked_info(token, remote_id)
        url = await readback.download_url(token, info, user_agent=user_agent)
        return dv.DirectLink(url, expires_in=60)

    async def delete(self, account: dv.DriveAccount, remote_ids: list[str]) -> None:
        if not isinstance(remote_ids, list) or len(remote_ids) > 100:
            raise dv.Conflict("123 回收文件必须是最多 100 个 ID 的列表")
        try:
            targets = list(dict.fromkeys(_file_id(remote_id) for remote_id in remote_ids))
        except dv.NotFound:
            raise dv.Conflict("123 回收文件 ID 格式不正确") from None
        if not targets:
            return
        token = _account_token(account)
        uid, _ = _identity(await user_info(token))
        if uid != account.user_id:
            raise dv.Conflict("123 凭据对应的用户与当前账号不一致，请重新登录")
        pending = []
        for remote_id in targets:
            try:
                await readback.existing_file(token, remote_id)
            except dv.NotFound:
                continue
            pending.append(remote_id)
        if not pending:
            return
        try:
            await trash(token, pending)
        except dv.NotFound:
            # 核验后文件可能消失；写接口的缺失响应不能证明整批已回收。
            pass
        remaining = False
        for remote_id in pending:
            try:
                await readback.existing_file(token, remote_id)
            except dv.NotFound:
                continue
            remaining = True
        if remaining:
            raise dv.Transient("123 部分文件尚未确认回收，请重试核对剩余文件")

    async def begin_login(self, options: dict[str, str]) -> dv.LoginFlow:
        if options:
            raise ValueError("123 扫码登录不支持附加选项")
        now = time.monotonic()
        for flow_id, session in list(self._flows.items()):
            if session.expires_at <= now:
                del self._flows[flow_id]

        data = await qr_generate()
        uni_id, url = data.get("uniID"), data.get("url")
        if not isinstance(uni_id, str) or not uni_id.strip():
            raise dv.ApiChanged("123 二维码生成缺少有效 uniID")
        if not isinstance(url, str) or not url.isprintable() or any(c.isspace() for c in url):
            raise dv.ApiChanged("123 二维码生成缺少有效 URL")
        try:
            parsed = urlsplit(url)
            valid_url = (
                parsed.scheme == "https" and parsed.hostname
                and parsed.username is None and parsed.password is None
                and not (parsed.query or parsed.fragment)
            )
        except ValueError:
            valid_url = False
        if not valid_url:
            raise dv.ApiChanged("123 二维码生成缺少有效 URL")
        query = urlencode({"env": "production", "uniID": uni_id, "source": "123pan", "type": "login"})
        flow = dv.LoginFlow(
            flow_id=secrets.token_urlsafe(16), status="pending",
            message="用 123 云盘 App 扫码", qr=parsed._replace(query=query).geturl(),
        )
        self._flows[flow.flow_id] = _LoginSession(uni_id, time.monotonic() + LOGIN_TTL_S, flow)
        return flow

    def _login_end(self, flow_id: str, session: _LoginSession) -> dv.LoginFlow | None:
        if self._flows.get(flow_id) is not session:
            return dv.LoginFlow(flow_id, "expired", "这次扫码已经结束，请重新扫码")
        if session.flow.terminal:
            return session.flow
        if time.monotonic() >= session.expires_at:
            session.flow = dv.LoginFlow(flow_id, "expired", "二维码已过期，请重新扫码")
            return session.flow
        return None

    async def login_state(self, flow_id: str) -> dv.LoginFlow:
        session = self._flows.get(flow_id)
        if session is None:
            return dv.LoginFlow(flow_id, "expired", "这次扫码已经结束，请重新扫码")
        if ended := self._login_end(flow_id, session):
            return ended
        try:
            response = await qr_result(session.uni_id)
            if ended := self._login_end(flow_id, session):
                return ended
            data = response["data"]
            if response["code"] == 200:
                try:
                    result = await self.login_with_form({"token": data.get("token")})
                except ValueError:
                    raise dv.ApiChanged("123 扫码成功响应缺少有效 Token") from None
                if ended := self._login_end(flow_id, session):
                    return ended
                flow = dv.LoginFlow(flow_id, "confirmed", "已登录", result=dv.LoginResult(
                    result.user_id, result.display_name, {**result.credentials, "login_method": "scan"},
                ))
            else:
                status = data.get("loginStatus")
                if type(status) is not int or status not in (0, 1, 2, 3, 4):
                    raise dv.ApiChanged("123 扫码状态响应缺少有效 loginStatus")
                if status == 2:
                    flow = dv.LoginFlow(flow_id, "failed", "已在手机上取消登录")
                elif status == 4:
                    flow = dv.LoginFlow(flow_id, "expired", "二维码已过期，请重新扫码")
                elif status in (1, 3):
                    flow = dv.LoginFlow(flow_id, "scanned", "已扫码，等待登录确认", qr=session.flow.qr)
                else:
                    # 并发查询的旧 pending 响应不能把 scanned 降回未扫码。
                    flow = session.flow
        except dv.DriveError:
            if ended := self._login_end(flow_id, session):
                return ended
            raise
        session.flow = flow
        return flow

    async def cancel_login(self, flow_id: str) -> None:
        session = self._flows.get(flow_id)
        if session is None or self._login_end(flow_id, session):
            return
        session.flow = dv.LoginFlow(flow_id, "failed", "已取消登录")

    async def login_with_form(self, values: dict[str, Any]) -> dv.LoginResult:
        try:
            token = TokenCredentials.model_validate(values).token.get_secret_value()
        except ValidationError:
            # pydantic 的默认错误文本可能包含原始输入。
            raise ValueError("请填写有效的 123 Token") from None
        token = token.strip().removeprefix("Bearer ").strip()
        if not token or token == "Bearer" or any(not "!" <= character <= "~" for character in token):
            raise ValueError("123 Token 格式不正确")
        try:
            data = await user_info(token)
        except dv.Conflict:
            # 查询适配层用 Conflict 表示构造器无法解析凭据；表单错误遵循 SDK 的 ValueError。
            raise ValueError("123 Token 格式无法解析") from None
        uid, name = _identity(data)
        return dv.LoginResult(uid, name, {"token": token, "login_method": "token"})

    async def account_info(self, account: dv.DriveAccount) -> dv.AccountInfo:
        data = await user_info(_account_token(account))
        uid, name = _identity(data)
        if uid != account.user_id:
            raise dv.Conflict("123 凭据对应的用户与当前账号不一致，请重新登录")

        used = data.get("SpaceUsed")
        permanent, temporary = data.get("SpacePermanent"), data.get("SpaceTemp")
        total = (permanent + temporary
                 if all(type(value) is int and value >= 0 for value in (permanent, temporary)) else None)
        return dv.AccountInfo(
            display_name=name,
            vip="会员" if data.get("Vip") is True else None,
            quota_used=used if type(used) is int and used >= 0 else None,
            quota_total=total,
        )

    async def list_dir(self, account: dv.DriveAccount, dir_id: str) -> list[dv.RemoteDir]:
        parent_id = _dir_id(dir_id)
        token = _account_token(account)
        seen: dict[str, tuple[int, str]] = {}
        dirs: list[dv.RemoteDir] = []
        async for remote_id, kind, name, _ in _dir_entries(token, parent_id):
            signature = (kind, name)
            if remote_id in seen:
                if seen[remote_id] != signature:
                    raise dv.Transient("123 同一条目的目录信息发生变化，请重新查询")
                continue
            seen[remote_id] = signature
            if kind == 1:
                dirs.append(dv.RemoteDir(remote_id=remote_id, name=name))
        return dirs

    async def list_tree(self, account: dv.DriveAccount, dir_id: str) -> dv.RemoteTree:
        root_id = _dir_id(dir_id)
        token = _account_token(account)
        pending = deque([(root_id, "")])
        seen: dict[str, tuple[str, int, str, int | None]] = {}
        paths: set[tuple[str, str]] = set()
        files: list[dv.RemoteFile] = []
        dir_ids: list[str] = []
        while pending:
            parent_id, base = pending.popleft()
            async for remote_id, kind, name, raw in _dir_entries(token, parent_id):
                if remote_id == root_id:
                    raise dv.Transient("123 目录出现循环，请重新查询")
                size = None
                if kind == 0:
                    size = _list_integer(raw.get("Size"))
                    if size < 0:
                        raise dv.ApiChanged("123 文件大小无效")
                signature = (parent_id, kind, name, size)
                if remote_id in seen:
                    if seen[remote_id] != signature:
                        raise dv.Transient("123 同一条目的树信息发生变化，请重新扫描")
                    continue
                if (parent_id, name) in paths:
                    raise dv.Transient("123 多个条目占用同一路径，请重新扫描")
                rel_path = base + name
                seen[remote_id] = signature
                paths.add((parent_id, name))
                if kind == 1:
                    dir_ids.append(remote_id)
                    pending.append((remote_id, rel_path + "/"))
                else:
                    files.append(dv.RemoteFile(remote_id, name, size, parent_id=parent_id, rel_path=rel_path))
        return dv.RemoteTree(files=tuple(files), dir_ids=tuple(dir_ids))

    async def ensure_dir(self, account: dv.DriveAccount, parent_id: str, path: str) -> str:
        current = _dir_id(parent_id)
        if not isinstance(path, str):
            raise dv.Conflict("123 目录路径格式不正确")
        if not path:
            return parent_id
        parts = path.split("/")
        if any(not part or part in (".", "..") or "\\" in part or "\x00" in part for part in parts):
            raise dv.Conflict("123 目录路径必须由有效的相对目录名组成")
        token = _account_token(account)
        visited = {current}
        for name in parts:
            child = await _find_dir(token, current, name)
            if child is None:
                # 创建结果未知时直接退出；后续调用仍先查目录，不能盲目补发写请求。
                await mkdir(token, current, name)
                child = await _find_dir(token, current, name)
                if child is None:
                    raise dv.Transient("123 目录创建后尚未查到精确名称，请稍后重试")
            if child in visited:
                raise dv.Transient("123 目录出现循环，请重新查询")
            visited.add(child)
            current = child
        return current
