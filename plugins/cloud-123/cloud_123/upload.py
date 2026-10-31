"""上传初始化、分片校验、合并及最小会话恢复。"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import secrets
import time

from movieclaw_sdk import drives as dv
from . import client, readback
from .upload_store import SCHEMA, HEX32, UploadStore, valid_ref

MD5 = re.compile(r"[0-9a-fA-F]{32}\Z")
# 与现有宿主 _send_parts 的单次字节上限一致，不限制文件总大小。
MAX_PART_BYTES = 512 * 1024 * 1024


def md5(value):
    if not isinstance(value, str) or not MD5.fullmatch(value):
        raise dv.ApiChanged("123 分片摘要格式无效")
    return value.lower()


def layout(size, part_size):
    if type(part_size) is not int or part_size <= 0 or min(part_size, max(size, 1)) > MAX_PART_BYTES:
        raise dv.Conflict("123 返回的分片大小超出宿主单片能力")
    return max(1, (size + part_size - 1) // part_size)


def binding_for(user_id, parent_id, name, fingerprint, range_data):
    if (not isinstance(user_id, str) or not user_id.isascii() or not user_id.isdecimal()
            or len(user_id) > 20 or int(user_id) <= 0):
        raise dv.Conflict("123 当前账号 UID 无效")
    if (not isinstance(name, str) or not name or not name.isprintable() or name in (".", "..")
            or any(c in name for c in ("/", "\\", "\x00"))
            or any(0xD800 <= ord(c) <= 0xDFFF for c in name) or len(name.encode("utf-8")) > 1024):
        raise dv.Conflict("123 上传文件名无效或过长")
    if (type(fingerprint.size) is not int or fingerprint.size < 0
            or not isinstance(fingerprint.md5, str) or not MD5.fullmatch(fingerprint.md5)):
        raise dv.Conflict("123 需要非负文件大小和有效 MD5")
    if range_data is not None:
        raise dv.Conflict("123 上传不使用区间挑战数据")
    return {"user_id": str(int(user_id)), "parent_id": parent_id, "name": name,
            "expected_size": fingerprint.size, "expected_md5": fingerprint.md5.lower()}


def session_key_for(binding):
    return hashlib.sha256(json.dumps(binding, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":")).encode()).hexdigest()


def check_put_intent(prior, requested):
    if prior is not None and (not isinstance(prior, dict) or prior != requested
                              or type(prior.get("index")) is not int or type(prior.get("size")) is not int):
        raise dv.Conflict("123 PUT 意图损坏或本次分片字节已变化")


class Uploads:
    def __init__(self, store, entries):
        self.data = UploadStore(store)
        self.entries = entries
        # 仅加速最近一次连续上传；丢失、重试或换代次都重新对账，不作完成证据。
        self.next_part = None

    async def find(self, token, binding):
        found = None
        seen = {}
        async for remote_id, kind, name, raw in self.entries(token, binding["parent_id"]):
            signature = (kind, name, raw.get("Size"), raw.get("ParentFileId"))
            if remote_id in seen and seen[remote_id] != signature:
                raise dv.Transient("123 目录条目发生变化，请重新核验")
            seen[remote_id] = signature
            if name != binding["name"]:
                continue
            if kind != 0 or found is not None and found != remote_id:
                raise dv.Conflict("123 上传目标被目录或多个同名文件占用")
            found = remote_id
        return found

    async def begin(self, token, binding, state):
        session_key = session_key_for(binding)
        # 成品优先，历史 state 损坏或存储不可用不妨碍当前指纹的独立回读。
        async with self.data.lock(session_key):
            self.next_part = None
            found = await self.find(token, binding)
            if found is not None:
                return dv.UploadStep(file=await readback.verify(token, found, binding))
            if state is not None and (not valid_ref(state) or state["session_key"] != session_key):
                raise dv.Conflict("123 上传恢复引用与账号、目标或指纹不一致")
            await self.data.ready()
            await self.data.resolve(session_key)
            # 顺带推进清理；维护会跳过当前持锁会话。
            await self.data.collect()
            return await self._prepare(token, binding, session_key, state)

    async def _prepare(self, token, binding, session_key, state):
        scope = "upload:" + session_key
        directory = await self.data.items(scope)
        index_rows = await self.data.items("upload_gc:" + session_key[:2])
        indexes = {}
        for key, item in index_rows.items():
            if key.startswith(session_key + ":"):
                generation = key[len(session_key) + 1:]
                if not HEX32.fullmatch(generation):
                    raise dv.Conflict("123 插件会话索引键损坏")
                if "retired:" + generation in directory:
                    retired = directory["retired:" + generation]
                    if (not valid_ref(retired) or retired["session_key"] != session_key
                            or retired["generation"] != generation):
                        raise dv.Conflict("123 插件退役标记归属无效")
                    continue
                if (not valid_ref(item) or item["session_key"] != session_key
                        or key != session_key + ":" + item["generation"]):
                    raise dv.Conflict("123 插件会话索引损坏")
                indexes[item["generation"]] = item
        for key, item in directory.items():
            if not key.startswith(("intent:", "retired:")):
                raise dv.Conflict("123 插件会话目录结构无效")
            if key.startswith("intent:") and "retired:" + key[7:] not in directory and key[7:] not in indexes:
                # 缺索引仍不能将已有意图当成没有初始化过。
                raise dv.Conflict("123 初始化意图缺少清理索引，请检查插件数据")
        candidates = []
        for generation, index in indexes.items():
            intent = directory.get("intent:" + generation)
            if intent is None:
                if await self.data.items("upload_data:" + generation):
                    raise dv.Conflict("123 上传数据缺少初始化意图，请检查插件数据")
                # 可能是登记中断或清理尾部；永不复用旧代次的键。
                continue
            if (not valid_ref(intent) or any(intent.get(k) != v for k, v in binding.items())
                    or intent["session_key"] != session_key or intent["generation"] != generation):
                raise dv.Conflict("123 上传初始化意图与当前文件不一致")
            candidates.append((index, intent))
        if len(candidates) > 1:
            raise dv.Conflict("123 存在多个活动上传会话，不能自动选择")
        if candidates:
            index, intent = candidates[0]
            generation = index["generation"]
            data_scope = "upload_data:" + generation
            info = await self.data.get(data_scope, "info")
            if info is None:
                raise dv.Conflict("123 初始化结果未定且会话信息缺失，请核对远端成品；不会重复初始化")
            self.validate_info(info, binding, index)
            if await self.data.has(data_scope, "merge_requested"):
                raise dv.Conflict("123 合并结果未知且未找到成品，请核对远端；不会重复合并或初始化")
            next_part = await self.reconcile(token, info)
            await self.touch(info)
            return dv.UploadStep(part_size=info["part_size"], next_part=next_part, state=self.ref(info))
        if state is not None and "retired:" + state["generation"] not in directory:
            raise dv.Conflict("123 上传引用已丢失，不能证明旧初始化结果")
        index = {"schema": SCHEMA, "session_key": session_key,
                 "generation": secrets.token_hex(16), "created_at": time.time()}
        intent = {**index, **binding}

        async def before_send():
            await self.data.register(index)
            await self.data.save(scope, "intent:" + index["generation"], intent)

        response = await client.upload_request(token, {"fileName": binding["name"],
            "parentFileId": binding["parent_id"], "size": binding["expected_size"],
            "etag": binding["expected_md5"], "type": 0, "duplicate": 0}, before_send=before_send)
        if response["code"] == 5060:
            found = await self.find(token, binding)
            if found is None:
                raise dv.Conflict("123 初始化发生同名冲突，但未找到可核验成品")
            return dv.UploadStep(file=await readback.verify(token, found, binding))
        data = response["data"]
        if type(data.get("Reuse")) is not bool:
            raise dv.ApiChanged("123 上传初始化缺少有效 Reuse")
        if "FileId" in data:
            readback.integer(data["FileId"], minimum=1)
        if data["Reuse"]:
            info = data.get("Info")
            if not isinstance(info, dict):
                raise dv.ApiChanged("123 秒传响应缺少最终文件信息")
            final_id = str(readback.integer(info.get("FileId"), minimum=1))
            return dv.UploadStep(file=await readback.verify(token, final_id, binding))
        if binding["expected_size"] == 0:
            raise dv.ApiChanged("123 零字节初始化未返回可核验成品")
        if data.get("Info") is not None:
            if not isinstance(data["Info"], dict):
                raise dv.ApiChanged("123 上传初始化 Info 格式无效")
            if "FileId" in data["Info"]:
                readback.integer(data["Info"]["FileId"], minimum=1)
        upload = {}
        for source, target in (("Bucket", "bucket"), ("Key", "key"),
                               ("StorageNode", "storageNode"), ("UploadId", "uploadId")):
            value = data.get(source)
            if (not isinstance(value, str) or not value or len(value) > 2048
                    or "://" in value or any(c.isspace() for c in value)):
                raise dv.ApiChanged("123 上传初始化缺少有效会话字段")
            upload[target] = value
        part_size = readback.integer(data.get("SliceSize"), minimum=1)
        count = layout(binding["expected_size"], part_size)
        info = {**intent, "init_file_id": str(readback.integer(data.get("FileId"), minimum=1)),
                "upload": upload, "part_size": part_size, "part_count": count, "multipart": count > 1}
        await self.data.save("upload_data:" + index["generation"], "info", info)
        return dv.UploadStep(part_size=part_size, next_part=0, state=self.ref(info))

    async def load_info(self, user_id, state):
        session_key, generation = state["session_key"], state["generation"]
        scope, data_scope = "upload:" + session_key, "upload_data:" + generation
        if ((scope, "retired:" + generation) in self.data.pending
                or await self.data.get(scope, "retired:" + generation) is not None):
            raise dv.Conflict("123 上传代次已退役，请重新 begin_upload")
        intent = await self.data.get(scope, "intent:" + generation)
        info = await self.data.get(data_scope, "info")
        locator = await self.data.get("upload_gc:" + session_key[:2], session_key + ":" + generation)
        try:
            if (not valid_ref(intent) or not valid_ref(locator)
                    or any(intent.get(k) != v or locator.get(k) != v for k, v in self.ref(state).items())
                    or intent.get("user_id") != user_id):
                raise ValueError
            parent = str(readback.integer(intent.get("parent_id")))
            binding = binding_for(user_id, parent, intent.get("name"), dv.Fingerprint(
                intent.get("expected_size"), "", intent.get("expected_md5"), ""), None)
            if (session_key_for(binding) != session_key
                    or any(intent.get(k) != v for k, v in {**locator, **binding}.items())):
                raise ValueError
        except (ValueError, dv.DriveError):
            raise dv.Conflict("123 上传会话缺失、损坏或账号绑定不一致") from None
        self.validate_info(info, binding, locator)
        return info, binding

    async def part(self, token, user_id, state, index, data):
        session_key, generation = state["session_key"], state["generation"]
        data_scope = "upload_data:" + generation
        async with self.data.lock(session_key):
            sequential = self.next_part == (session_key, generation, index)
            self.next_part = None
            await self.data.ready()
            await self.data.resolve(session_key)
            info, binding = await self.load_info(user_id, state)
            if (binding["expected_size"] == 0 or index >= info["part_count"]
                    or len(data) != min(info["part_size"], binding["expected_size"] - index * info["part_size"])):
                raise dv.Conflict("123 分片序号或长度与会话布局不一致")
            if await self.data.has(data_scope, "merge_requested"):
                raise dv.Conflict("123 合并结果未定，不能继续 PUT；请重新 begin_upload 核验成品")
            digest = hashlib.md5(data).hexdigest()
            if not info["multipart"] and digest != binding["expected_md5"]:
                raise dv.Conflict("123 单片源字节 MD5 与初始化指纹不一致")
            requested = {**self.ref(info), "index": index, "size": len(data), "md5": digest}
            prior = await self.data.get(data_scope, f"put_requested:{index}")
            check_put_intent(prior, requested)
            record = await self.data.get(data_scope, f"part:{index}")
            if record is not None:
                self.check_part(info, index, record)
            if record is not None and record["md5"].lower() != digest:
                raise dv.Conflict("123 本次源字节与已有分片确认不一致")
            if not info["multipart"] and prior is not None and record is None:
                found = await self.find(token, binding)
                if found is None:
                    raise dv.Conflict("123 单片 PUT 结果未知，不能凭空列表重传")
                await readback.verify(token, found, binding)
                # SDK 此处只接收 state；不伪造片确认，finish 时仍须独立核验成品。
                return self.ref(info)
            # 未完成的 PUT/确认或非连续调用不能使用顺序提示。
            fast = sequential and prior is None and record is None
            if not fast:
                local = await self.local_parts(info)
                remote = await self.remote_parts(token, info, local)
            if info["multipart"]:
                if not fast and any(n not in local or n + 1 not in remote for n in range(index)):
                    raise dv.Conflict("123 前序分片尚未确认，请从 begin_upload 返回的起点继续")
                present = None if fast else remote.get(index + 1)
                if present is not None and present != (len(data), digest):
                    raise dv.Conflict("123 远端分片与本次源字节 MD5 或大小不一致")
                confirmed = present is not None
            else:
                confirmed = record is not None
            if not confirmed:
                urls = (await client.upload_url(token, info["upload"], index, info["multipart"])).get("presignedUrls")
                url = readback.https_url(urls.get(str(index + 1)) if isinstance(urls, dict) else None)

                async def before_send():
                    await self.data.save(data_scope, f"put_requested:{index}", requested)

                await client.put_part(url, data, digest, before_send=before_send)
            if record is None:
                await self.data.save(data_scope, f"part:{index}", {**requested, "etag": digest})
            await self.touch(info)
            if fast or not any(number > index + 1 for number in remote):
                self.next_part = (session_key, generation, index + 1)
            return self.ref(info)

    async def finish(self, token, user_id, state):
        async with self.data.lock(state["session_key"]):
            self.next_part = None
            await self.data.ready()
            # 此前的片确认写入即使未定，也不能抢在成品核验前 resolve。
            info, binding = await self.load_info(user_id, state)
            found = await self.find(token, binding)
            if found is not None:
                result = await readback.verify(token, found, binding)
                await self.retire_verified(info)
                return result
            data_scope = "upload_data:" + info["generation"]
            if ((data_scope, "merge_requested") in self.data.pending
                    or await self.data.has(data_scope, "merge_requested")):
                raise dv.Conflict("123 合并结果未知且未找到成品；不会重复合并，请稍后重新核验")
            if info["expected_size"] == 0:
                raise dv.Conflict("123 零字节会话没有可核验成品，不伪造分片或合并")
            # 只恢复当前代次的必要片确认，不补写旧代次或可选维护记录。
            for (scope, key), pending in list(self.data.pending.items()):
                if scope == data_scope and key.startswith("part:"):
                    await self.data.save(data_scope, key, pending)
            local = await self.local_parts(info)
            if len(local) != info["part_count"]:
                raise dv.Conflict("123 分片确认不齐全，不能合并")
            if info["multipart"]:
                remote = await self.remote_parts(token, info, local)
                if len(remote) != info["part_count"]:
                    raise dv.Conflict("123 远端分片不齐全，请重新 begin_upload 对账续传")

            async def before_send():
                await self.data.save(data_scope, "merge_requested", self.ref(info))

            result = await client.upload_complete(token, {**info["upload"], "FileId": info["init_file_id"]},
                                                   info["multipart"], before_send=before_send)
            final = result.get("file_info")
            try:
                final_id = str(readback.integer(final.get("FileId") if isinstance(final, dict) else None,
                                               minimum=1))
            except dv.ApiChanged:
                raise dv.Transient("123 合并结果未确认，将回查成品；不会重复合并") from None
            await readback.bound_info(token, final_id, binding)
            # 本次明确合并成功 + 新详情绑定构成 P 路径；不保存完成快照。
            await self.retire_verified(info)
            return dv.RemoteFile(final_id, binding["name"], binding["expected_size"],
                                 parent_id=binding["parent_id"], sha1=None)

    async def retire_verified(self, info):
        # 仅核验成功且已持锁读到可信 info 的出口调用；不枚举、清数据或补写分片。
        try:
            async with asyncio.timeout(1):
                await self.data.save("upload:" + info["session_key"], "retired:" + info["generation"], {
                    **self.ref(info), "retired_at": time.time()})
        except (dv.DriveError, TimeoutError):
            pass  # 可选维护不能撤销核验成功；外部取消仍传播。

    async def touch(self, info):
        # 独立提示只供清理，写入失败不撤销可恢复状态；取消仍传播。
        try:
            await self.data.call("set", scope="upload_data:" + info["generation"], key="activity", value={
                **self.ref(info), "at": time.time()})
        except dv.DriveError:
            pass

    @staticmethod
    def ref(info):
        return {k: info[k] for k in ("schema", "session_key", "generation")}

    def validate_info(self, info, binding, index):
        try:
            if (not valid_ref(info) or any(info.get(k) != v for k, v in {**binding, **index}.items())
                    or type(info.get("part_size")) is not int
                    or type(info.get("part_count")) is not int
                    or info["part_count"] != layout(binding["expected_size"], info["part_size"])
                    or type(info.get("multipart")) is not bool or info["multipart"] != (info["part_count"] > 1)
                    or str(readback.integer(info.get("init_file_id"), minimum=1)) != info["init_file_id"]
                    or not isinstance(info.get("upload"), dict)
                    or set(info["upload"]) != {"bucket", "key", "storageNode", "uploadId"}
                    or any(not isinstance(v, str) or not v or len(v) > 2048 or "://" in v
                           or any(c.isspace() for c in v) for v in info["upload"].values())):
                raise ValueError
        except (ValueError, dv.DriveError):
            raise dv.Conflict("123 上传恢复信息损坏、绑定不一致或布局不受支持") from None

    def check_part(self, info, number, record):
        length = min(info["part_size"], info["expected_size"] - number * info["part_size"])
        try:
            if (number >= info["part_count"] or not valid_ref(record)
                    or any(record.get(k) != v for k, v in self.ref(info).items())
                    or type(record.get("index")) is not int or record["index"] != number
                    or type(record.get("size")) is not int or record["size"] != length
                    or md5(record.get("md5")) != md5(record.get("etag"))):
                raise ValueError
        except (ValueError, dv.DriveError):
            raise dv.Conflict("123 上传分片确认损坏或冲突") from None

    async def local_parts(self, info):
        rows = await self.data.items("upload_data:" + info["generation"])
        local = {}
        for key, record in rows.items():
            if not key.startswith("part:"):
                continue
            suffix = key[5:]
            if not re.fullmatch(r"0|[1-9][0-9]*", suffix) or len(key) > 200:
                raise dv.Conflict("123 分片确认键无效")
            number = int(suffix)
            self.check_part(info, number, record)
            check_put_intent(rows.get(f"put_requested:{number}"), {
                **self.ref(info), "index": number, "size": record["size"], "md5": record["md5"].lower()})
            local[number] = record
        return local

    async def remote_parts(self, token, info, local):
        count = info["part_count"]
        remote = {}
        marker = 0
        # 片号严格推进，空页终止；不依赖 maxParts 是否被服务端遵守。
        for _ in range(count + 1):
            data = await client.upload_list(token, {**info["upload"], "partNumberMarker": marker, "maxParts": 100})
            parts = data.get("Parts")
            if not isinstance(parts, list):
                raise dv.ApiChanged("123 分片列表格式无法识别")
            if "IsTruncated" in data and type(data["IsTruncated"]) is not bool:
                raise dv.ApiChanged("123 分片分页标记无效")
            if len(parts) > count:
                raise dv.ApiChanged("123 分片列表超过文件布局片数")
            if not parts:
                if data.get("IsTruncated"):
                    raise dv.ApiChanged("123 分片空页仍声明未结束")
                break
            last = marker
            for part in parts:
                if not isinstance(part, dict):
                    raise dv.ApiChanged("123 分片条目格式无效")
                number = readback.integer(part.get("PartNumber"), minimum=1)
                length = readback.integer(part.get("Size"))
                etag = client.part_etag(part.get("ETag"))
                if number > count or length != min(info["part_size"], info["expected_size"] - (number-1)*info["part_size"]):
                    raise dv.Conflict("123 远端分片布局与当前文件不一致")
                pair = (length, etag)
                if number <= marker and number not in remote:
                    raise dv.ApiChanged("123 分片列表出现游标之前的未知片")
                if number in remote and remote[number] != pair:
                    raise dv.Conflict("123 远端重复分片信息冲突")
                remote[number] = pair
                last = max(last, number)
            if last <= marker:
                raise dv.ApiChanged("123 分片分页未前进")
            marker = last
        else:
            raise dv.ApiChanged("123 无法确认分片列表完整性")
        if not info["multipart"]:
            if remote:
                raise dv.ApiChanged("123 单片上传出现未经验证的分片列表")
        for number, record in local.items():
            if number + 1 in remote and remote[number + 1] != (record["size"], record["md5"].lower()):
                raise dv.Conflict("123 本地确认与远端分片 MD5 或大小冲突")
        return remote

    async def reconcile(self, token, info):
        local = await self.local_parts(info)
        remote = await self.remote_parts(token, info, local)
        if not info["multipart"]:
            if 0 not in local and await self.data.get("upload_data:" + info["generation"], "put_requested:0") is not None:
                raise dv.Conflict("123 单片 PUT 结果未知，不能凭空列表重传")
            return int(0 in local)
        next_part = 0
        while next_part in local and next_part + 1 in remote:
            next_part += 1
        return next_part
