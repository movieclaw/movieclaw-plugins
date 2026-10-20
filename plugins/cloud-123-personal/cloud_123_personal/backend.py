"""Personal API adapter; paginated traversal intentionally differs from native-tree contract."""
from contextlib import asynccontextmanager
from base64 import urlsafe_b64decode
import json
import time
import re
import hashlib
from collections import deque
from dataclasses import replace

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError
from movieclaw_sdk.drives import (
    AccountInfo, ApiChanged, AuthExpired, Capabilities, Conflict, DirectLink,
    DriveBackend, Login, LoginResult, NotFound, RemoteDir, RemoteFile, RemoteTree, Transient, UploadStep,
)
from .client import Client, secure_url
from .journal import Journal


class Credentials(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    phone: SecretStr = Field(title="手机号")
    password: SecretStr = Field(title="密码")


def integer(value):
    if isinstance(value, bool) or not re.fullmatch(r"[0-9]+", str(value)):
        raise ApiChanged("123 返回了无效数字字段")
    return int(value)


def ident(value):
    return str(integer(value))


def new_name_checked(name):
    if not isinstance(name, str) or not name or name in (".", "..") or any(c in name for c in '\\/:*?|><"\x00'):
        raise ValueError("文件或目录名称无效")
    return name


def remote_display_name(value):
    """Remote labels are not new Windows filenames; preserve spelling verbatim."""
    if not isinstance(value, str):
        raise ApiChanged("123 名称字段 FileName 缺失或不是字符串")
    if not value:
        raise ApiChanged("123 名称字段 FileName 为空")
    if "\x00" in value:
        raise ApiChanged("123 名称字段 FileName 含 NUL")
    return value


def relative_path_segment(value):
    """A literal remote name in the SDK's slash-delimited relative path."""
    name = remote_display_name(value)
    if name in (".", "..") or "/" in name or "\\" in name:
        raise ApiChanged("123 名称含路径分隔符或点路径段，无法无歧义表示相对路径")
    return name


def file_from(row):
    try:
        if integer(row["Type"]) != 0:
            raise Conflict("目标是目录，不是文件")
        name = remote_display_name(row.get("FileName"))
        return RemoteFile(
            remote_id=ident(row["FileId"]), name=name, size=integer(row["Size"]),
            sha1=None, parent_id=ident(row["ParentFileId"]) if "ParentFileId" in row else None,
        )
    except (KeyError, TypeError):
        raise ApiChanged("123 文件元数据缺少必需字段") from None


class Personal123(DriveBackend):
    type = "123-personal"
    title = "123 云盘个人账号（实验版）"
    description = "手机号登录；逐目录完整分页扫描，上传日志恢复；与原生整树契约有差异"
    accent_color = "#246BFD"
    capabilities = Capabilities(
        writable=True, rapid_upload_hashes=("md5",), list_hashes=False,
        recycle_bin=True, original_link=True, link_needs_ua=True,
    )
    login = Login(form=Credentials, hint="逐目录扫描实验版；遇验证码请到官方客户端处理后重试")

    MAX_SCAN_ENTRIES = 200000
    MAX_SCAN_PAGES = 20000
    MAX_DEPTH = 256
    MAX_PATH = 8192

    def __init__(self, transport=None, store=None):
        self.client = Client(transport)
        self.journal = Journal(store)

    async def aclose(self):
        await self.client.aclose()

    async def _login(self, phone, password):
        data = await self.client.sign_in(phone, password)
        token = data.get("token")
        if not isinstance(token, str) or not token:
            raise ApiChanged("123 登录响应缺少令牌")
        async with self.client.session(token) as session:
            me = await self.client.call(session, "user_info")
        if not me.get("UID"):
            raise ApiChanged("123 用户响应缺少真实 UID")
        # exp is scheduling information only; identity comes from authenticated user_info.
        expires = 0
        try:
            expires = int(json.loads(urlsafe_b64decode(token.split(".")[1] + "=="))["exp"])
        except (ValueError, KeyError, IndexError, TypeError):
            pass
        return LoginResult(
            user_id=ident(me["UID"]), display_name=str(me.get("Nickname") or "123 用户"),
            credentials={"phone": phone, "password": password, "token": token, "expires_at": str(expires)},
        )

    async def login_with_form(self, values):
        try:
            model = Credentials.model_validate(values)
            phone, password = model.phone.get_secret_value(), model.password.get_secret_value()
            if not re.fullmatch(r"\+?[0-9]{7,15}", phone) or not password:
                raise ValueError
        except (ValidationError, ValueError):
            raise ValueError("请填写有效手机号和密码") from None
        return await self._login(phone, password)

    @asynccontextmanager
    async def _session(self, account):
        credentials = account.credentials
        token = credentials.get("token")
        if not token:
            raise AuthExpired("请先通过表单登录 123 个人账号")
        try:
            expires = int(credentials.get("expires_at", "0"))
        except ValueError:
            raise AuthExpired("已保存的登录数据无效，请重新登录") from None
        if expires and expires <= time.time() + 120:
            if not credentials.get("phone") or not credentials.get("password"):
                raise AuthExpired("缺少续期所需凭据，请重新表单登录")
            result = await self._login(credentials.get("phone", ""), credentials.get("password", ""))
            if result.user_id != account.user_id:
                raise AuthExpired("刷新后的账号身份不符，请重新登录")
            account.credentials = dict(result.credentials)
            token = result.credentials["token"]
        async with self.client.session(token) as session:
            yield session

    async def account_info(self, account):
        async with self._session(account) as session:
            me = await self.client.call(session, "user_info")
            if ident(me.get("UID")) != account.user_id:
                raise AuthExpired("当前登录身份与保存的账号不符")
            return AccountInfo(str(me.get("Nickname") or "123 用户"),
                               quota_total=integer(me["SpacePermanent"]) if "SpacePermanent" in me else None,
                               quota_used=integer(me["SpaceUsed"]) if "SpaceUsed" in me else None)

    async def _info(self, session, remote_id):
        data = await self.client.call(session, "fs_info", ident(remote_id))
        rows = data.get("infoList")
        if not isinstance(rows, list):
            raise ApiChanged("123 文件详情列表格式变化")
        found = [r for r in rows if isinstance(r, dict) and ident(r.get("FileId")) == ident(remote_id)]
        if not found:
            raise NotFound("123 文件或目录不存在")
        if len(found) != 1:
            raise ApiChanged("123 文件详情含重复 ID")
        row = found[0]
        if row.get("Trashed") in (True, 1, "1"):
            raise NotFound("123 文件已在回收站")
        return row

    async def stat(self, account, remote_id):
        async with self._session(account) as session:
            return file_from(await self._info(session, remote_id))

    async def _children(self, session, parent_id, budget=None, check_parent=True, directories_only=False):
        parent = ident(parent_id or "0")
        if check_parent and parent != "0":
            row = await self._info(session, parent)
            if integer(row.get("Type")) != 1:
                raise Conflict("目标不是目录")
        if budget is None:
            budget = {"pages": 0, "entries": 0}
        rows, seen, names, cursors = [], set(), set(), {"0"}
        page, cursor = 1, "0"
        while True:
            if budget["pages"] >= self.MAX_SCAN_PAGES:
                raise ApiChanged("扫描超过分页安全上限；未返回部分结果，请缩小扫描目录")
            budget["pages"] += 1
            data = await self.client.call(session, "fs_list", {
                "parentFileId": int(parent), "Page": page, "next": int(cursor), "limit": 100,
                "trashed": False, "SearchData": "", "inDirectSpace": False,
                "orderBy": "file_id", "orderDirection": "asc",
            })
            batch = data.get("InfoList")
            if not isinstance(batch, list) or "Next" not in data or len(batch) > 100:
                raise ApiChanged("123 目录分页缺少必需字段或超过请求页大小")
            for row in batch:
                if not isinstance(row, dict):raise ApiChanged("123 目录项格式变化")
                kind = integer(row.get("Type"))
                if kind not in (0,1):raise ApiChanged("123 返回未知项目类型")
                fid = ident(row.get("FileId"))
                if fid == "0" or fid == parent or fid in seen or row.get("Trashed") in (True,1,"1"):
                    raise Transient("123 目录含循环、重复或已回收项目，请重新扫描")
                if ident(row.get("ParentFileId")) != parent:
                    raise Transient("123 项目在扫描期间移动，未返回部分结果")
                seen.add(fid)
                budget["entries"] += 1
                if budget["entries"] > self.MAX_SCAN_ENTRIES:
                    raise ApiChanged("扫描超过项目安全上限；未返回部分结果，请缩小扫描目录")
                if directories_only and kind == 0:
                    # File labels/sizes are irrelevant to a picker addressed by directory ID.
                    # Still validate Type, ID, parent and pagination for the whole response.
                    continue
                name = remote_display_name(row.get("FileName"))
                if not directories_only and name in names:
                    raise Conflict("123 同一目录有重名项目，无法生成唯一相对路径")
                if kind == 0:file_from(row)
                names.add(name);rows.append(row)
            nxt = data["Next"]
            if type(nxt) not in (str,int):raise ApiChanged("123 分页游标格式变化")
            if str(nxt) == "-1":return rows
            cursor = ident(nxt)
            if not batch or cursor in cursors:
                raise ApiChanged("123 分页游标重复或空页仍要求继续，未返回部分结果")
            cursors.add(cursor)
            page += 1

    async def list_dir(self, account, dir_id):
        async with self._session(account) as session:
            rows = await self._children(session, dir_id, directories_only=True)
            try:
                return [RemoteDir(ident(r["FileId"]), relative_path_segment(r["FileName"])) for r in rows if integer(r["Type"]) == 1]
            except (KeyError, TypeError):
                raise ApiChanged("123 目录元数据缺失") from None

    async def list_tree(self, account, dir_id):
        # User-authorized fallback: complete paginated traversal, NOT a native tree API.
        root = ident(dir_id or "0")
        files, directories, seen = [], [], {root}
        queue = deque([(root, "", 0, None)])
        budget = {"pages": 0, "entries": 0}
        async with self._session(account) as session:
            while queue:
                parent, prefix, depth, expected = queue.popleft()
                if expected is not None:
                    current = await self._info(session,parent)
                    if (current.get("FileName"),ident(current.get("ParentFileId")),integer(current.get("Type"))) != expected:
                        raise Transient("目录在扫描期间改名或移动，未返回部分结果")
                rows = await self._children(session,parent,budget,check_parent=expected is None)
                for row in rows:
                    fid = ident(row["FileId"])
                    if fid in seen:raise Transient("123 树中出现重复 ID 或循环，未返回部分结果")
                    seen.add(fid)
                    path = prefix + relative_path_segment(row["FileName"])
                    if len(path) > self.MAX_PATH:raise ApiChanged("扫描路径超过安全长度上限")
                    if integer(row["Type"]) == 1:
                        if depth + 1 > self.MAX_DEPTH:raise ApiChanged("扫描目录超过安全深度上限")
                        directories.append(fid)
                        queue.append((fid,path+"/",depth+1,(row["FileName"],parent,1)))
                    else:
                        files.append(replace(file_from(row),rel_path=path))
        return RemoteTree(tuple(files), tuple(directories))

    async def ensure_dir(self, account, parent_id, path):
        if path == "":
            return parent_id
        components = path.split("/")
        for part in components:
            new_name_checked(part)
        parent = ident(parent_id or "0")
        async with self._session(account) as session:
            for part in components:
                rows = await self._children(session, parent)
                hits = [r for r in rows if r.get("FileName") == part]
                if len(hits) > 1 or hits and integer(hits[0].get("Type")) != 1:
                    raise Conflict("同名项目不是唯一目录")
                if hits:
                    parent = ident(hits[0]["FileId"])
                    continue
                data = await self.client.call(session, "fs_mkdir", part, parent_id=int(parent), duplicate=0)
                created = data.get("Info") or data
                parent = ident(created.get("FileId"))
                check = await self._info(session, parent)
                if check.get("FileName") != part or integer(check.get("Type")) != 1:
                    raise Conflict("目录创建结果与请求不符")
        return parent

    async def direct_link(self, account, remote_id, user_agent):
        async with self._session(account) as session:
            row = await self._info(session, remote_id)
            file_from(row)
            if not row.get("Etag") or not row.get("S3KeyFlag"):
                raise ApiChanged("123 缺少取链所需的远端 MD5 或存储标记")
            data = await self.client.call(session, "download_info", row, headers={"user-agent": user_agent, "platform": "android"})
            return DirectLink(secure_url(data.get("DownloadUrl")), expires_in=300.0)

    async def delete(self, account, remote_ids):
        if len(remote_ids) > 100:
            raise ValueError("单次删除最多 100 个文件")
        if not remote_ids:
            return
        async with self._session(account) as session:
            # Per-id acknowledgement prevents one missing id from hiding a failed batch.
            for remote_id in dict.fromkeys(remote_ids):
                try:
                    row = await self._info(session, remote_id)
                    file_from(row)
                    await self.client.call(session, "fs_trash", ident(remote_id))
                except NotFound:
                    continue

    @staticmethod
    def _same(row, name, size, md5):
        if row.get("FileName") != name or integer(row.get("Type")) != 0:
            return False
        etag = row.get("Etag")
        return integer(row.get("Size")) == size and isinstance(etag, str) and etag.upper() == md5.upper()

    async def _existing(self, session, parent, name, size, md5, pending_id=None):
        matches = [r for r in await self._children(session, parent) if r.get("FileName") == name]
        # Pending metadata can already contain the requested MD5 and size.
        if len(matches) > 1:
            raise Conflict("123 存在多个同名项目")
        if matches:
            row = matches[0]
            if pending_id is not None and ident(row["FileId"]) == pending_id:
                # Initial metadata may already describe the final bytes. It is not
                # proof that this upload was committed; resume/complete it instead.
                if integer(row.get("Type")) != 0:
                    raise Conflict("上传 ID 已对应目录，不能继续")
                return None
            if self._same(row, name, size, md5):
                # Metadata can describe an uncommitted upload; require Reuse acknowledgement.
                return True
            raise Conflict("同名文件内容不同，不能覆盖")
        return None

    @staticmethod
    def _binding(account, parent, name, size, md5):
        return [account.user_id, parent, name, size, md5.upper()]

    def _state_binding(self, account, state):
        self._validate_state(account, state)
        return self._binding(account,state["parent"],state["name"],state["size"],state["md5"])

    async def _confirmed_file(self, session, record):
        _,parent,name,size,md5 = record["binding"]
        row = await self._info(session, record["file_id"])
        if not self._same(row,name,size,md5) or ident(row.get("ParentFileId")) != parent:
            raise Conflict("已确认文件的名称、目录、大小或 MD5 不符，不能复用")
        return file_from(row)

    def _check_record_state(self, account, record, state=None):
        saved = record["state"]
        if saved is None:raise ApiChanged("日志没有已确认的上传会话，不能猜测恢复")
        self._validate_state(account,saved)
        if record["binding"] != self._state_binding(account,saved):raise ApiChanged("上传日志绑定不一致")
        if state is not None and {**state,"next":0} != {**saved,"next":0}:
            raise Conflict("宿主状态与持久化上传会话不符")
        total = max(1,(saved["size"]+saved["part_size"]-1)//saved["part_size"])
        if record["next"] > total or (record["next"] and str(record["next"]-1) not in record["hashes"]):
            raise ApiChanged("上传日志确认进度不完整，不能跳过分片")
        return saved

    async def begin_upload(self, account, dir_id, name, fingerprint, state, range_data):
        new_name_checked(name)
        parent = ident(dir_id or "0")
        if not re.fullmatch(r"[a-fA-F0-9]{32}",fingerprint.md5) or type(fingerprint.size) is not int or fingerprint.size < 0:
            raise ValueError("上传指纹无效")
        if range_data is not None:raise ValueError("123 MD5 上传不使用区间挑战")
        binding = self._binding(account,parent,name,fingerprint.size,fingerprint.md5)
        if state and self._state_binding(account,state) != binding:raise Conflict("续传状态不属于当前文件")
        async with self.journal.locked(binding) as key:
            record = await self.journal.load(key,binding)
            async with self._session(account) as session:
                if record is not None:
                    if record["phase"] in ("initializing","committing"):
                        raise ApiChanged("上传初始化或合并结果不确定；已保留日志，不能重复写入或凭元数据判成功")
                    if record["phase"] in ("complete","reuse_ack","committed"):
                        if state and record["state"] is not None:self._check_record_state(account,record,state)
                        result = await self._confirmed_file(session,record)
                        record["phase"] = "complete"
                        await self.journal.save(key,record)
                        return UploadStep(file=result)
                    saved = self._check_record_state(account,record,state)
                    # Progress comes only from durable PUT acknowledgements, never guessed service fields.
                    return UploadStep(part_size=saved["part_size"],next_part=record["next"],
                                      state={**saved,"next":record["next"]})
                if state:
                    raise ApiChanged("宿主有上传状态但插件日志缺失；不能证明进度或合并结果，未新建会话")
                existing = await self._existing(session,parent,name,fingerprint.size,fingerprint.md5)
                record = Journal.fresh(binding)
                await self.journal.save(key,record)  # write intent BEFORE network mutation
                data = await self.client.call(session,"upload_request",{
                    "fileName":name,"parentFileId":int(parent),"type":0,
                    "size":fingerprint.size,"etag":fingerprint.md5.lower(),"duplicate":0,
                })
                if type(data.get("Reuse")) is not bool:raise ApiChanged("123 上传初始化缺少 Reuse 标志")
                fid = ident(data.get("FileId") or (data.get("Info") or {}).get("FileId"))
                if fid == "0":raise ApiChanged("123 返回零文件 ID；需要额外验收，未猜测秒传对象")
                record["file_id"] = fid
                if data["Reuse"]:
                    record["phase"] = "reuse_ack"
                    await self.journal.save(key,record)
                    result = await self._confirmed_file(session,record)
                    record["phase"] = "complete"
                    await self.journal.save(key,record)
                    return UploadStep(file=result)
                if existing:raise ApiChanged("同名元数据存在但服务未确认秒传完成，不能判断文件已完成")
                size = integer(data.get("SliceSize"))
                if size <= 0 or min(size,max(fingerprint.size,1)) > 512*1024*1024:
                    raise ApiChanged("123 上传分片大小无效")
                total = max(1,(fingerprint.size+size-1)//size)
                if total > self.journal.max_parts:raise ApiChanged("上传超过10000分片安全上限")
                upload = {"FileId":fid}
                for field in ("Bucket","Key","StorageNode","UploadId"):
                    if not isinstance(data.get(field),str) or not data[field]:raise ApiChanged("123 上传初始化缺少会话信息")
                    upload[field] = data[field]
                saved = dict(version=2,user=account.user_id,parent=parent,name=name,size=fingerprint.size,
                             md5=fingerprint.md5.upper(),part_size=size,next=0,upload=upload)
                record.update(phase="parts",state=saved)
                await self.journal.save(key,record)
                return UploadStep(part_size=size,state=saved)

    @staticmethod
    def _validate_state(account, state):
        expected = {"version", "user", "parent", "name", "size", "md5", "part_size", "next", "upload"}
        try:
            if set(state) != expected or state["version"] != 2 or state["user"] != account.user_id:
                raise ValueError
            new_name_checked(state["name"])
            ident(state["parent"])
            if integer(state["part_size"]) <= 0 or not re.fullmatch(r"[A-F0-9]{32}", state["md5"]):
                raise ValueError
            total = max(1, (integer(state["size"]) + state["part_size"] - 1) // state["part_size"])
            if integer(state["next"]) > total:
                raise ValueError
            upload = state["upload"]
            if set(upload) != {"FileId", "Bucket", "Key", "StorageNode", "UploadId"}:
                raise ValueError
            ident(upload["FileId"])
            if any(not isinstance(v, str) or not v for v in upload.values()):
                raise ValueError
        except (ValueError, TypeError, KeyError):
            raise Conflict("续传状态无效或属于其它账号") from None

    async def upload_part(self, account, state, index, data):
        binding = self._state_binding(account,state)
        if type(index) is not int or index != state["next"]:raise Conflict("上传分片顺序与状态不符")
        expected = min(state["part_size"],state["size"]-index*state["part_size"])
        if index < 0 or expected < 0 or len(data) != expected or expected == 0 and (state["size"] != 0 or index != 0):
            raise Conflict("上传分片长度不符")
        async with self.journal.locked(binding) as key:
            record = await self.journal.load(key,binding)
            if record is None:raise ApiChanged("上传日志缺失，不能继续旧会话")
            self._check_record_state(account,record,state)
            if record["phase"] != "parts":raise ApiChanged("上传已进入合并阶段，不能再次写分片")
            digest = hashlib.sha256(data).hexdigest()
            prior = record["hashes"].get(str(index))
            if prior is not None and prior != digest:raise Conflict("重复或重试分片字节与日志不一致")
            if index < record["next"]:
                if prior is None:raise Conflict("只能幂等重试最近确认的分片；请用 begin_upload 获取恢复进度")
                return {**state,"next":index+1}
            if index != record["next"]:raise Conflict("不能跳过未确认的分片")
            record["hashes"][str(index)] = digest
            await self.journal.save(key,record)  # bind retry bytes before PUT (including lost ACK)
            async with self._session(account) as session:
                payload = dict(state["upload"])
                multipart = state["size"] > state["part_size"]
                if multipart:payload.update(partNumberStart=index+1,partNumberEnd=index+2)
                signed = await self.client.call(session,"upload_prepare" if multipart else "upload_auth",payload)
                urls = signed.get("presignedUrls")
                if not isinstance(urls,dict):raise ApiChanged("123 上传链接响应格式变化")
                await self.client.put(urls.get(str(index+1)),data)
            record["next"] = index+1
            record["hashes"] = {str(index):digest}
            await self.journal.save(key,record)  # checkpoint before returning to host
            return {**state,"next":index+1}

    async def finish_upload(self, account, state):
        binding = self._state_binding(account,state)
        async with self.journal.locked(binding) as key:
            record = await self.journal.load(key,binding)
            if record is None:raise ApiChanged("上传日志缺失，不能合并旧会话")
            self._check_record_state(account,record,state)
            total = max(1,(state["size"]+state["part_size"]-1)//state["part_size"])
            if state["next"] != total or record["next"] != total:raise Conflict("上传分片未完成，不能合并")
            if record["phase"] in ("committing","initializing"):
                raise ApiChanged("合并结果不确定；保留日志，不能重复合并或凭元数据判定完成")
            async with self._session(account) as session:
                if record["phase"] == "parts":
                    existing = await self._existing(session,state["parent"],state["name"],state["size"],state["md5"],state["upload"]["FileId"])
                    if existing:raise Conflict("合并前发现其它同名文件，不能覆盖")
                    record["phase"] = "committing"
                    await self.journal.save(key,record)
                    await self.client.call(session,"upload_complete",{**state["upload"],"fileSize":state["size"]},
                                           is_multipart=state["size"]>state["part_size"])
                    record["phase"] = "committed"
                    await self.journal.save(key,record)
                result = await self._confirmed_file(session,record)
                record["phase"] = "complete"
                await self.journal.save(key,record)
                return result
