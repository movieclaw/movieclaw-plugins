# SPDX-License-Identifier: GPL-3.0-or-later
"""115 的字节传到阿里云 OSS：分片上传（签名方式参考 p115oss，MIT）。

宿主按 ``part_size`` 逐片读本地文件交过来，所以不管文件多大都走分片上传——单次 PUT 要整个文件进内存。
OSS 的临时凭证向 115 要，过期前换新；完成分片时带 115 给的回调参数，OSS 回调 115 后文件才出现在网盘里。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
from datetime import datetime
from email.utils import formatdate
from typing import Any
from urllib.parse import quote, urlencode
from xml.etree import ElementTree

from movieclaw_sdk import drives as dv

from .api import Api115

ENDPOINT_HOST = "oss-cn-shenzhen.aliyuncs.com"
DEFAULT_BUCKET = "fhnfile"
#: 参与签名的查询参数
SUBRESOURCES = frozenset({"uploads", "uploadId", "partNumber", "sequential"})
#: 分片大小：20 MB 起，文件越大分片越大，保证不超过 OSS 的 10000 片
MIN_PART = 20 * 1024 * 1024
MAX_PARTS = 9000
#: 临时凭证还剩这么久就换新（秒）
TOKEN_MARGIN = 300


def part_size_for(size: int) -> int:
    step = 1024 * 1024
    need = -(-size // MAX_PARTS)
    return max(MIN_PART, -(-need // step) * step)


def sign(
    token: dict[str, Any], method: str, bucket: str, key: str, headers: dict[str, str], params: dict[str, Any]
) -> dict[str, str]:
    """OSS V1 签名，返回带认证的请求头。"""
    headers = {k.lower(): v for k, v in headers.items()}
    headers["x-oss-security-token"] = token["SecurityToken"]
    headers.setdefault("date", formatdate(usegmt=True))
    sub = sorted((k, str(v)) for k, v in params.items() if k in SUBRESOURCES)
    resource = f"/{bucket}/{quote(key)}" + (f"?{urlencode(sub)}" if sub else "")
    oss_headers = "\n".join(f"{k}:{v}" for k, v in sorted(headers.items()) if k.startswith("x-oss-"))
    text = "\n".join(
        [
            method.upper(),
            headers.get("content-md5", ""),
            headers.get("content-type", ""),
            headers["date"],
            oss_headers,
            resource,
        ]
    )
    digest = hmac.new(token["AccessKeySecret"].encode(), text.encode(), hashlib.sha1).digest()
    headers["authorization"] = f"OSS {token['AccessKeyId']}:{base64.b64encode(digest).decode()}"
    return headers


def _b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


def _xml_text(content: bytes, tag: str) -> str | None:
    try:
        node = ElementTree.fromstring(content).find(tag)
    except ElementTree.ParseError:
        return None
    return node.text if node is not None else None


class Oss:
    def __init__(self, api: Api115) -> None:
        self._api = api
        self._token: dict[str, Any] | None = None
        self._token_until = 0.0

    async def _credentials(self, refresh: bool = False) -> dict[str, Any]:
        if refresh or self._token is None or time.time() > self._token_until:
            token = await self._api.oss_token()
            self._token = token
            self._token_until = time.time() + 3600 - TOKEN_MARGIN
            try:
                until = datetime.fromisoformat(str(token.get("Expiration")).replace("Z", "+00:00"))
                self._token_until = until.timestamp() - TOKEN_MARGIN
            except ValueError:
                pass
        return self._token

    async def _request(
        self,
        method: str,
        bucket: str,
        key: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        content: bytes | None = None,
    ):
        params = params or {}
        url = f"https://{bucket}.{ENDPOINT_HOST}/{quote(key)}"
        for attempt in (0, 1):
            token = await self._credentials(refresh=attempt == 1)
            signed = sign(token, method, bucket, key, dict(headers or {}), params)
            response = await self._api.raw(method, url, params=params, headers=signed, content=content)
            code = _xml_text(response.content, "Code") if response.status_code >= 400 else None
            if code in ("InvalidAccessKeyId", "SecurityTokenExpired", "AccessDenied") and attempt == 0:
                continue  # 凭证过期：换新再试一次
            return response, code
        return response, code

    async def start(self, bucket: str, key: str) -> str:
        response, code = await self._request("POST", bucket, key, params={"sequential": 1, "uploads": 1})
        upload_id = _xml_text(response.content, "UploadId") if response.status_code < 400 else None
        if not upload_id:
            raise dv.Transient(f"OSS 分片上传开不了头（{code or response.status_code}）")
        return upload_id

    async def put_part(self, bucket: str, key: str, upload_id: str, number: int, data: bytes) -> str:
        response, code = await self._request(
            "PUT", bucket, key, params={"partNumber": number, "uploadId": upload_id}, content=data
        )
        if code == "NoSuchUpload":
            raise UploadGone()
        if response.status_code >= 400:
            raise dv.Transient(f"OSS 分片 {number} 没传上（{code or response.status_code}）")
        etag = response.headers.get("etag", "").strip('"')
        if etag.upper() != hashlib.md5(data).hexdigest().upper():
            raise dv.Transient(f"OSS 分片 {number} 校验不一致，重传")
        return etag

    async def uploaded_parts(self, bucket: str, key: str, upload_id: str) -> dict[int, str]:
        """已经传上去的分片（续传时核对）。分片上传已不在了抛 ``UploadGone``。"""
        response, code = await self._request("GET", bucket, key, params={"uploadId": upload_id})
        if code == "NoSuchUpload" or response.status_code == 404:
            raise UploadGone()
        if response.status_code >= 400:
            raise dv.Transient(f"OSS 分片列表取不到（{code or response.status_code}）")
        parts: dict[int, str] = {}
        root = ElementTree.fromstring(response.content)
        for node in root.iterfind("Part"):
            number, etag = node.findtext("PartNumber"), node.findtext("ETag")
            if number and etag:
                parts[int(number)] = etag.strip('"')
        return parts

    async def complete(
        self, bucket: str, key: str, upload_id: str, etags: list[str], callback: dict[str, str]
    ) -> dict[str, Any]:
        body = "".join(
            f'<Part><PartNumber>{i}</PartNumber><ETag>"{etag}"</ETag></Part>' for i, etag in enumerate(etags, start=1)
        )
        headers = {
            "content-type": "text/xml",
            "x-oss-callback": _b64(callback["callback"]),
            "x-oss-callback-var": _b64(callback["callback_var"]),
        }
        response, code = await self._request(
            "POST",
            bucket,
            key,
            params={"uploadId": upload_id},
            headers=headers,
            content=f"<CompleteMultipartUpload>{body}</CompleteMultipartUpload>".encode(),
        )
        if code == "NoSuchUpload":
            raise UploadGone()
        if response.status_code >= 400:
            raise dv.Transient(f"OSS 合并分片失败（{code or response.status_code}）")
        try:
            data = response.json()
        except ValueError:
            return {}
        if isinstance(data, dict) and data.get("state") is False:
            raise dv.DriveError(f"115 没有接收上传的文件：{data.get('message') or data.get('error') or data}")
        return data if isinstance(data, dict) else {}


class UploadGone(Exception):
    """OSS 上的分片上传已经不在了（过期或被清理），要从头传。"""
