"""文件详情检查与 D 路径回读；不信任声明 Etag，不持久化下载地址。"""
from __future__ import annotations

import hashlib
import sys
from urllib.parse import urljoin, urlsplit

import httpx
from movieclaw_sdk import drives as dv, net
from . import client

READ_SIZE = 16 * 1024 * 1024


def integer(value, *, minimum=0):
    if isinstance(value, str) and value.isascii() and value.isdecimal() and len(value) <= 20:
        value = int(value)
    if type(value) is not int or value < minimum:
        raise dv.ApiChanged("123 返回了无效整数或文件 ID")
    return value


def https_url(value):
    try:
        parsed = urlsplit(value) if isinstance(value, str) and value.isprintable() else None
        if (parsed is None or parsed.scheme != "https" or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.fragment or any(c.isspace() for c in value)):
            raise ValueError
        parsed.port
    except ValueError:
        raise dv.ApiChanged("123 文件地址格式无效") from None
    return value


async def close(resource, primary):
    try:
        await resource.aclose()
    except Exception:
        if primary is None:
            raise dv.Transient("123 回读连接关闭失败，请稍后重试") from None


async def existing_file(token, remote_id, *, uploading=False):
    # 上传后暂不可见不能当作成品不存在；stat 则使用 SDK 的缺失语义。
    data = await client.file_info(token, remote_id)
    rows = data.get("infoList")
    if not isinstance(rows, list):
        raise dv.ApiChanged("123 文件详情列表无效")
    if not rows:
        if uploading:
            raise dv.Transient("123 成品暂不可见，请稍后重新核验")
        raise dv.NotFound("123 文件不存在")
    if len(rows) != 1 or not isinstance(rows[0], dict):
        raise dv.ApiChanged("123 文件详情不唯一")
    info = rows[0]
    if str(integer(info.get("FileId"), minimum=1)) != remote_id:
        raise dv.Conflict("123 文件详情 ID 与请求不一致")
    if type(info.get("Type")) is not int or info["Type"] not in (0, 1):
        raise dv.ApiChanged("123 文件详情类型无效")
    if info["Type"] != 0:
        raise dv.Conflict("123 目标是目录，不是文件")
    trashed = info.get("Trashed")
    if type(trashed) not in (int, bool) or trashed not in (0, 1):
        raise dv.ApiChanged("123 成品回收站状态无效")
    if trashed:
        if uploading:
            raise dv.Conflict("123 成品已在回收站")
        raise dv.NotFound("123 文件已在回收站")
    return info


async def checked_info(token, remote_id, *, uploading=False):
    info = await existing_file(token, remote_id, uploading=uploading)
    for key in ("Status", "PunishFlag", "AbnormalAlert"):
        if type(info.get(key)) is not int:
            raise dv.ApiChanged("123 成品可用状态无法识别")
        if info[key] != 0:
            raise dv.Transient("123 成品尚不可用或已受限，请稍后核验")
    name = info.get("FileName")
    if (not isinstance(name, str) or not name or name in (".", "..")
            or any(c in name for c in ("/", "\\", "\x00"))):
        raise dv.ApiChanged("123 文件详情名称无效")
    integer(info.get("Size"))
    integer(info.get("ParentFileId"))
    return info


async def bound_info(token, remote_id, binding):
    info = await checked_info(token, remote_id, uploading=True)
    if (str(integer(info.get("ParentFileId"))) != binding["parent_id"]
            or info.get("FileName") != binding["name"]):
        raise dv.Conflict("123 成品目录或名称与目标不一致")
    if integer(info.get("Size")) != binding["expected_size"]:
        raise dv.Conflict("123 成品大小与本地文件不一致")
    return info


async def download_url(token, info, *, user_agent=None):
    # 这两个字段是取原文件链接的协议参数，不把 Etag 当可信 MD5。
    if (not isinstance(info.get("Etag"), str) or not info["Etag"]
            or not isinstance(info.get("S3KeyFlag"), str)):
        raise dv.ApiChanged("123 原文件取链缺少必要字段")
    return https_url((await client.download_info(token, info, user_agent=user_agent)).get("DownloadUrl"))


async def verify(token, remote_id, binding):
    info = await bound_info(token, remote_id, binding)
    url = await download_url(token, info)
    size = binding["expected_size"]
    digest = hashlib.md5()
    http = httpx.AsyncClient(transport=net.http_transport("cloud-123"), trust_env=False,
                             follow_redirects=False, timeout=httpx.Timeout(20, connect=5))
    try:
        for start in range(0, max(size, 1), READ_SIZE):
            end = min(start + READ_SIZE, size) - 1
            headers = {"Accept-Encoding": "identity"}
            if size:
                headers["Range"] = f"bytes={start}-{end}"
            # 逐请求计量；只在本次调用内复用最后实际响应地址。
            for hop in range(5):
                request = http.build_request("GET", url, headers=headers)
                # 内容端 Cookie 不得带入下一跳或下一段。
                request.headers.pop("cookie", None)
                response = await http.send(request, stream=True)
                try:
                    if response.is_redirect:
                        url = https_url(urljoin(url, response.headers.get("location", "")))
                        if not response.headers.get("location"):
                            raise dv.ApiChanged("123 原文件重定向缺少地址")
                        continue
                    if response.status_code == 429:
                        raise dv.RateLimited("123 原文件回读请求过于频繁，请稍后重试")
                    if response.status_code in (401, 403, 404, 410) or response.status_code >= 500:
                        raise dv.Transient("123 原文件回读受限或链接失效，请稍后重新核验")
                    if (response.status_code != (206 if size else 200)
                            or response.headers.get("content-encoding", "identity") != "identity"
                            or size and response.headers.get("content-range") != f"bytes {start}-{end}/{size}"):
                        raise dv.ApiChanged("123 原文件范围或编码与预期不符")
                    received = 0
                    async for chunk in response.aiter_raw():
                        received += len(chunk)
                        if received > end - start + 1:
                            raise dv.ApiChanged("123 原文件回读返回了额外字节")
                        digest.update(chunk)
                    if received != end - start + 1:
                        raise dv.ApiChanged("123 原文件回读被截断")
                    break
                finally:
                    await close(response, sys.exception())
            else:
                raise dv.ApiChanged("123 原文件重定向过多")
    except dv.DriveError:
        raise
    except Exception:
        # aiter_raw 在 EOF 也会自动关闭；该关闭异常同样不能泄露底层连接信息。
        raise dv.Transient("123 原文件回读连接失败，请稍后从头核验") from None
    finally:
        await close(http, sys.exception())
    if digest.hexdigest() != binding["expected_md5"]:
        raise dv.Conflict("123 成品实际字节 MD5 与本地文件不一致")
    after = await bound_info(token, remote_id, binding)
    if after.get("S3KeyFlag") != info["S3KeyFlag"]:
        raise dv.Conflict("123 回读期间成品对象发生变化")
    return dv.RemoteFile(remote_id, binding["name"], size, parent_id=binding["parent_id"], sha1=None)
