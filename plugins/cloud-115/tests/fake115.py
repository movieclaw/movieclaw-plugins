# SPDX-License-Identifier: GPL-3.0-or-later
"""仿 115（含阿里云 OSS）的 ``httpx.MockTransport``：只实现插件用到的接口，状态全在内存。

上传初始化的请求体按真实方式 AES 解开后再校验，OSS 请求按 V1 签名规则独立重算核对；
只有取直链的响应（115 用私钥 RSA 加密）仿不了，测试里替换掉 ``rsa_decrypt``。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qsl, unquote

import httpx
from p115cipher import AES_IV, AES_KEY
from p115cipher.util import aes_cbc_decrypt
from p115pickcode import id_to_pickcode

COOKIES = {"UID": "10001_R2_1700000000", "CID": "c1", "SEID": "s1", "KID": "k1"}
USER_KEY = "UK123"
STS = {"AccessKeyId": "AK", "AccessKeySecret": "SK", "SecurityToken": "ST", "Expiration": "2099-01-01T00:00:00Z"}


@dataclass
class Node:
    id: int
    parent: int
    name: str
    data: bytes | None = None  # None 表示目录

    @property
    def is_dir(self) -> bool:
        return self.data is None

    @property
    def sha1(self) -> str:
        return hashlib.sha1(self.data or b"").hexdigest().upper()

    @property
    def pickcode(self) -> str:
        return id_to_pickcode(self.id)


@dataclass
class Fake115:
    nodes: dict[int, Node] = field(default_factory=dict)
    seq: int = 1000
    #: 扫码状态按顺序吐出
    qr_statuses: list[int] = field(default_factory=lambda: [0, 1, 2])
    logged_in: bool = True
    events: list[dict[str, Any]] = field(default_factory=list)
    #: 秒传库：内容在这里的文件会秒传命中
    rapid: dict[str, bytes] = field(default_factory=dict)
    #: 下一次 files 列举返回这个错误码（限流、登录失效）
    fail_errno: int | None = None
    oss_uploads: dict[str, dict[int, bytes]] = field(default_factory=dict)
    oss_objects: dict[str, dict[str, Any]] = field(default_factory=dict)
    requests: list[str] = field(default_factory=list)
    deleted: list[int] = field(default_factory=list)

    # ------------------------------------------------------------------ 数据

    def mkdir(self, parent: int, name: str) -> int:
        self.seq += 1
        self.nodes[self.seq] = Node(self.seq, parent, name)
        return self.seq

    def put(self, parent: int, name: str, data: bytes) -> int:
        self.seq += 1
        self.nodes[self.seq] = Node(self.seq, parent, name, data)
        return self.seq

    def children(self, cid: int) -> list[Node]:
        return [n for n in self.nodes.values() if n.parent == cid]

    def descendants(self, cid: int) -> list[Node]:
        out: list[Node] = []
        for node in self.children(cid):
            out.append(node)
            if node.is_dir:
                out.extend(self.descendants(node.id))
        return out

    def chain(self, cid: int) -> list[dict[str, Any]]:
        path = []
        while cid:
            node = self.nodes[cid]
            path.append({"cid": node.id, "name": node.name, "pid": node.parent})
            cid = node.parent
        return [{"cid": 0, "name": "根目录", "pid": 0}, *reversed(path)]

    @staticmethod
    def item(node: Node) -> dict[str, Any]:
        if node.is_dir:
            return {"cid": node.id, "pid": node.parent, "n": node.name.replace("&", "&amp;"), "pc": node.pickcode}
        return {
            "fid": node.id,
            "cid": node.parent,
            "n": node.name.replace("&", "&amp;"),
            "s": len(node.data or b""),
            "sha": node.sha1,
            "pc": node.pickcode,
            "fc": 1,
        }

    # ------------------------------------------------------------------ 路由

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        self.requests.append(f"{request.method} {url.host}{url.path}")
        if url.host.endswith("aliyuncs.com"):
            return self.oss(request)
        if url.host == "qrcodeapi.115.com":
            return self.qrcode(request)
        if url.host == "uplb.115.com":
            return self.uplb(request)
        if not self._cookies_ok(request):
            if url.host == "my.115.com":
                return httpx.Response(200, text="<html>请登录</html>")
            return _json({"state": False, "errno": 99, "error": "请重新登录"})
        if url.host == "my.115.com":
            if url.params.get("ac") == "status":
                return _json({"state": True})
            return _json({"state": True, "data": {"user_name": "影视仓库", "vip": 1}})
        if url.host == "proapi.115.com" and url.path.endswith("space_info"):
            return _json({"state": True, "data": {"all_total": {"size": 60 * 2**40}, "all_use": {"size": 41 * 2**40}}})
        if url.host == "proapi.115.com" and url.path == "/app/chrome/downurl":
            return self.downurl(request)
        if url.host == "webapi.115.com":
            return self.webapi(request)
        return httpx.Response(404)

    def _cookies_ok(self, request: httpx.Request) -> bool:
        header = request.headers.get("cookie", "")
        return self.logged_in and all(f"{k}={v}" in header for k, v in COOKIES.items())

    def qrcode(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/token/"):
            return _json(
                {
                    "state": 1,
                    "data": {"uid": "QRUID", "time": 1, "sign": "SIG", "qrcode": "https://115.com/scan/dg-QRUID"},
                }
            )
        if path == "/get/status/":
            assert request.url.params.get("uid") == "QRUID"
            status = self.qr_statuses.pop(0) if self.qr_statuses else 2
            return _json({"state": 1, "data": {"status": status}})
        if path.endswith("/login/qrcode/"):
            assert dict(parse_qsl(request.content.decode()))["account"] == "QRUID"
            return _json({"state": 1, "data": {"cookie": dict(COOKIES)}})
        return httpx.Response(404)

    def webapi(self, request: httpx.Request) -> httpx.Response:
        path, params = request.url.path, request.url.params
        if path == "/files":
            if self.fail_errno is not None:
                errno, self.fail_errno = self.fail_errno, None
                return _json({"state": False, "errno": errno, "error": "出错了"})
            cid = int(params["cid"])
            if cid and cid not in self.nodes:
                cid = 0  # 115 对不存在的目录悄悄列根目录
            nodes = self.descendants(cid) if params.get("cur") == "0" else self.children(cid)
            if params.get("nf") == "1":
                nodes = [n for n in nodes if n.is_dir]
            elif params.get("show_dir") == "0":
                nodes = [n for n in nodes if not n.is_dir]
            offset, limit = int(params.get("offset", 0)), int(params.get("limit", 32))
            page = nodes[offset : offset + min(limit, 3)]  # 一页只给 3 条，逼插件翻页
            return _json(
                {
                    "state": True,
                    "cid": cid,
                    "count": len(nodes),
                    "path": self.chain(cid),
                    "data": [self.item(n) for n in page],
                }
            )
        if path == "/files/get_info":
            node = self.nodes.get(int(params["file_id"]))
            if node is None:
                return _json({"state": False, "errno": 430004, "error": "文件不存在或已删除"})
            return _json({"state": True, "data": [self.item(node)]})
        if path == "/files/add":
            form = dict(parse_qsl(request.content.decode()))
            pid = int(form["pid"])
            if any(n.is_dir and n.name == form["cname"] for n in self.children(pid)):
                return _json({"state": False, "errno": 20004, "error": "该目录名称已存在"})
            return _json({"state": True, "cid": self.mkdir(pid, form["cname"])})
        if path == "/rb/delete":
            ids = [int(v) for k, v in parse_qsl(request.content.decode()) if k.startswith("fid")]
            if any(i not in self.nodes for i in ids):
                return _json({"state": False, "errno": 430004, "error": "文件不存在或已删除"})
            for i in ids:
                self.nodes.pop(i)
                self.deleted.append(i)
            return _json({"state": True})
        if path == "/behavior/detail":
            offset, limit = int(params["offset"]), int(params["limit"])
            ordered = sorted(self.events, key=lambda e: e["id"], reverse=True)
            return _json({"state": True, "data": {"count": len(ordered), "list": ordered[offset : offset + limit]}})
        if path == "/files/uploadinfo":
            return _json({"state": True, "userkey": USER_KEY, "user_id": 10001})
        return httpx.Response(404)

    def downurl(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["user-agent"] == "TestPlayer/1.0"
        assert dict(parse_qsl(request.content.decode())).get("data")
        # 真 115 的 data 是私钥 RSA 加密的；测试把 rsa_decrypt 换成 base64 解码
        files = {
            str(n.id): {
                "file_name": n.name,
                "file_size": len(n.data or b""),
                "pick_code": n.pickcode,
                "sha1": n.sha1,
                "url": {"url": f"https://cdnfhnfile.115cdn.net/{n.id}?t=4102444800&f=1"},
            }
            for n in self.nodes.values()
            if not n.is_dir
        }
        return _json({"state": True, "data": base64.b64encode(json.dumps(files).encode()).decode()})

    # ------------------------------------------------------------------ 上传

    def uplb(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/3.0/gettoken.php":
            return _json({"StatusCode": "200", **STS})
        if path != "/4.0/initupload.php":
            return httpx.Response(404)
        assert request.url.params.get("k_ec")
        form = dict(parse_qsl(_unpad(aes_cbc_decrypt(request.content, AES_KEY, AES_IV)).decode("latin-1")))
        assert form["userkey"] == USER_KEY and form["userid"] == "10001"
        assert form["sig"] and form["token"]
        pid = int(form["target"].removeprefix("U_1_"))
        sha1, size = form["fileid"], int(form["filesize"])
        if sha1 in self.rapid:
            data = self.rapid[sha1]
            if size >= 1024 and not form.get("sign_val"):
                return _json({"status": 7, "statuscode": 701, "sign_key": "SK1", "sign_check": "100-199"})
            if form.get("sign_val"):
                assert form["sign_key"] == "SK1"
                if form["sign_val"] != hashlib.sha1(data[100:200]).hexdigest().upper():
                    return _json({"status": 0, "statusmsg": "校验失败"})
            node = self.put(pid, form["filename"], data)
            return _json({"status": 2, "statuscode": 0, "pickcode": id_to_pickcode(node)})
        self.seq += 1
        pickcode = id_to_pickcode(self.seq)
        reserved = self.seq
        callback_var = json.dumps({"x:pick_code": pickcode, "x:target": form["target"], "x:user_id": 10001})
        self.oss_objects[f"obj/{reserved}"] = {"id": reserved, "pid": pid, "name": form["filename"], "sha1": sha1}
        return _json(
            {
                "status": 1,
                "statuscode": 0,
                "pickcode": pickcode,
                "bucket": "fhnfile",
                "object": f"obj/{reserved}",
                "callback": {
                    "callback": json.dumps({"callbackUrl": "https://uplb.115.com/3.0/callback.php"}),
                    "callback_var": callback_var,
                },
            }
        )

    def oss(self, request: httpx.Request) -> httpx.Response:
        assert request.url.host == "fhnfile.oss-cn-shenzhen.aliyuncs.com"
        _check_signature(request)
        key = unquote(request.url.path.lstrip("/"))
        params = request.url.params
        if request.method == "POST" and "uploads" in params:
            self.seq += 1
            upload_id = f"UP{self.seq}"
            self.oss_uploads[upload_id] = {}
            return httpx.Response(
                200,
                content=f"<InitiateMultipartUploadResult><UploadId>{upload_id}</UploadId></InitiateMultipartUploadResult>".encode(),
            )
        upload_id = params.get("uploadId", "")
        if upload_id not in self.oss_uploads:
            return httpx.Response(404, content=b"<Error><Code>NoSuchUpload</Code></Error>")
        parts = self.oss_uploads[upload_id]
        if request.method == "PUT":
            parts[int(params["partNumber"])] = request.content
            return httpx.Response(200, headers={"ETag": f'"{hashlib.md5(request.content).hexdigest().upper()}"'})
        if request.method == "GET":
            body = "".join(
                f'<Part><PartNumber>{n}</PartNumber><ETag>"{hashlib.md5(d).hexdigest().upper()}"</ETag></Part>'
                for n, d in sorted(parts.items())
            )
            return httpx.Response(200, content=f"<ListPartsResult>{body}</ListPartsResult>".encode())
        # 合并：校验回调参数，拼出文件
        assert json.loads(base64.b64decode(request.headers["x-oss-callback"]))["callbackUrl"]
        assert json.loads(base64.b64decode(request.headers["x-oss-callback-var"]))["x:pick_code"]
        numbers = [int(n) for n in re.findall(r"<PartNumber>(\d+)</PartNumber>", request.content.decode())]
        data = b"".join(parts[n] for n in numbers)
        meta = self.oss_objects[key]
        assert hashlib.sha1(data).hexdigest().upper() == meta["sha1"]
        self.nodes[meta["id"]] = Node(meta["id"], meta["pid"], meta["name"], data)
        del self.oss_uploads[upload_id]
        return _json({"state": True, "data": {"file_id": str(meta["id"]), "pick_code": id_to_pickcode(meta["id"])}})


def _json(data: Any) -> httpx.Response:
    return httpx.Response(200, json=data)


def _unpad(data: bytes) -> bytes:
    return data[: -data[-1]] if data and data[-1] <= 16 else data


def _check_signature(request: httpx.Request) -> None:
    """按 OSS V1 规则独立重算签名。"""
    h = request.headers
    sub = sorted(
        (k, v) for k, v in request.url.params.multi_items() if k in {"uploads", "uploadId", "partNumber", "sequential"}
    )
    resource = f"/fhnfile{request.url.raw_path.decode().split('?')[0]}"
    if sub:
        resource += "?" + "&".join(f"{k}={v}" for k, v in sub)
    oss = "\n".join(
        f"{k}:{v}" for k, v in sorted((k.lower(), v) for k, v in h.items() if k.lower().startswith("x-oss-"))
    )
    text = "\n".join([request.method, h.get("content-md5", ""), h.get("content-type", ""), h["date"], oss, resource])
    want = base64.b64encode(hmac.new(STS["AccessKeySecret"].encode(), text.encode(), hashlib.sha1).digest()).decode()
    assert h["authorization"] == f"OSS {STS['AccessKeyId']}:{want}", text
    assert h["x-oss-security-token"] == STS["SecurityToken"]
