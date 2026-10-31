"""手动分阶段采样；不判断成品完成，也不推导安全续传规则。"""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import getpass
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import warnings
from datetime import datetime, timezone
from urllib.parse import urlsplit

import httpx
from p123client import P123Client


MAX_BYTES = 32 * 1024 * 1024
NUMERIC_FIELDS = {"code", "status", "type", "size", "slicesize", "partnumber", "partsize",
                  "next", "total", "maxparts", "partnumbermarker", "nextpartnumbermarker",
                  "punishflag", "trashed"}
SECRET_FIELDS = {"authorization", "cookie", "setcookie", "token", "accesstoken",
                 "refreshtoken", "password", "secret", "signature", "presignedurls",
                 "accesskey", "secretkey", "securitytoken"}


class ProbeError(Exception):
    pass


def private_evidence(value, token: str, field: str = ""):
    """私有证据保留协议原值；清除已知鉴权字段、当前 Token、Bearer 和 URL。"""
    if field.lower().replace("_", "").replace("-", "") in SECRET_FIELDS:
        return "<removed>"
    if isinstance(value, dict):
        return {private_evidence(k, token): private_evidence(v, token, k) for k, v in value.items()}
    if isinstance(value, list):
        return [private_evidence(v, token, field) for v in value]
    if isinstance(value, str):
        value = value.replace(token, "<removed-token>")
        value = re.sub(r"(?i)\bBearer\s+[^\s\"<>]+", "<removed-bearer>", value)
        return re.sub(r"(?i)https?://[^\s\"<>]+", "<removed-url>", value)
    return value


def redact(value, salt: str, field: str = ""):
    """保留协议结构、状态及哈希；其余标量用本次采样内稳定的代号替代。"""
    field = field.lower()
    if isinstance(value, dict):
        return {k: redact(v, salt, k) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v, salt, field) for v in value]
    if value is None or isinstance(value, bool):
        return value
    if type(value) is int and not field.endswith("id"):
        return value
    if field in NUMERIC_FIELDS and (
        type(value) is int or isinstance(value, str) and value.isascii() and value.isdecimal()
    ):
        return value
    if field in ("etag", "md5") and isinstance(value, str):
        digest = value.strip('"')
        if len(digest) == 32 and all(c in "0123456789abcdefABCDEF" for c in digest):
            return value
    digest = hashlib.sha256((salt + json.dumps(value, sort_keys=True)).encode()).hexdigest()[:16]
    return f"<redacted:{type(value).__name__}:{digest}>"


def save(path: Path, value) -> None:
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        os.chmod(temporary, 0o600)
        json.dump(value, stream, ensure_ascii=False, indent=2)
    temporary.replace(path)


class Probe:
    def __init__(self, directory: Path, state: dict, token: str, http: httpx.AsyncClient):
        self.directory, self.state, self.http = directory, state, http
        self.token = token
        self.client = P123Client(token=token, check_for_relogin=False)

    def persist(self):
        save(self.directory / "private.json", self.state)

    def record(self, operation: str, response: httpx.Response):
        try:
            body = response.json()
        except ValueError:
            body = {"non_json_bytes": len(response.content)}
        event = {
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "operation": operation, "http_status": response.status_code,
            "expected_size": self.state["size"], "expected_md5": self.state["md5"],
            "response": private_evidence(body, self.token),
            "etag": private_evidence(response.headers.get("etag"), self.token),
        }
        for filename in ("evidence.private.jsonl", "samples.jsonl"):
            if filename == "samples.jsonl":
                event = {**event, "response": redact(event["response"], self.state["salt"]),
                         "etag": redact(event["etag"], self.state["salt"], "etag")}
            with (self.directory / filename).open("a", encoding="utf-8") as stream:
                os.chmod(stream.name, 0o600)
                stream.write(json.dumps(event, ensure_ascii=False) + "\n")

    async def api(self, operation: str, method, *args, **options):
        async def request(url, method="GET", *, parse, **kwargs):
            parsed = urlsplit(url)
            if parsed.scheme != "https" or parsed.netloc != "123pan.com" or not parsed.path.startswith("/b/api/"):
                raise ProbeError("拒绝非预期 Web API 地址")
            response = await self.http.request(method, url, **kwargs)
            self.record(operation, response)
            if not response.is_success:
                raise ProbeError(f"{operation}: HTTP {response.status_code}，查看脱敏样本")
            try:
                return parse(response, response.content)
            except (ValueError, TypeError):
                raise ProbeError(f"{operation}: 非 JSON 响应") from None

        result = await method(*args, async_=True, request=request, **options)
        if not isinstance(result, dict) or type(result.get("code")) is not int or result["code"] not in (0, 200):
            raise ProbeError(f"{operation}: 业务响应不成功，停止操作")
        if not isinstance(result.get("data"), dict):
            raise ProbeError(f"{operation}: 响应结构待核实")
        return result["data"]

    async def identity(self):
        data = await self.api("identity", self.client.user_info)
        uid = data.get("UID")
        if not (type(uid) is int and uid > 0 or isinstance(uid, str) and uid.isascii() and uid.isdecimal()):
            raise ProbeError("未取得有效账号 UID")
        uid = str(uid)
        if "uid" in self.state and self.state["uid"] != uid:
            raise ProbeError("本次账号与采样会话不一致")
        self.state["uid"] = uid

    def payload(self):
        data = self.state["upload"]
        # 字段依据固定版本 upload_auth / upload_list 的请求契约；未知形状不猜测。
        if not all(isinstance(data.get(k), str) and data[k] for k in ("Bucket", "Key", "StorageNode", "UploadId")):
            raise ProbeError("初始化会话字段与预期不符，先分析样本")
        return {"bucket": data["Bucket"], "key": data["Key"],
                "storageNode": data["StorageNode"], "uploadId": data["UploadId"]}

    def file_id(self):
        data = self.state["upload"]
        info = data.get("Info")
        values = [source["FileId"] for source in (data, info)
                  if isinstance(source, dict) and "FileId" in source]
        ids = set()
        for value in values:
            if not (type(value) is int or isinstance(value, str) and value.isascii() and value.isdecimal()):
                raise ProbeError("初始化 FileId 无效，先分析样本")
            try:
                number = int(value)
            except ValueError:
                raise ProbeError("初始化 FileId 无效，先分析样本") from None
            if number <= 0:
                raise ProbeError("初始化 FileId 无效，先分析样本")
            ids.add(str(number))
        if len(ids) != 1:
            raise ProbeError("初始化 FileId 缺失或两处不一致，先分析样本，不猜测文件 id")
        return ids.pop()

    def layout(self):
        size = self.state["upload"].get("SliceSize")
        if isinstance(size, str) and size.isascii() and size.isdecimal():
            size = int(size)
        if type(size) is not int or size <= 0 or size > 512 * 1024 * 1024:
            raise ProbeError("SliceSize 无效，先分析样本")
        return size, max(1, -(-self.state["size"] // size))

    def before_write(self, operation: str):
        if self.state.get("pending") or self.state.get("complete_sent"):
            raise ProbeError("已有结果未知的写请求或已提交合并；只能 inspect，不盲目重发")
        self.state["pending"] = operation
        self.persist()

    async def start(self):
        listing = await self.api("empty_test_directory", self.client.fs_list, {
            "parentFileId": self.state["parent"], "limit": 100, "next": 0,
            "trashed": False, "driveId": 0, "inDirectSpace": False,
        })
        if listing.get("InfoList") != [] or listing.get("Next") not in (-1, "-1"):
            raise ProbeError("仅允许在确认完全为空的独立测试目录初始化")
        self.before_write("initialize")
        data = await self.api("initialize", self.client.upload_request, {
            "fileName": self.state["name"], "parentFileId": self.state["parent"],
            "size": self.state["size"], "etag": self.state["md5"], "type": 0, "duplicate": 0,
        })
        # 仅保存继续采样需要的会话字段；响应证据另存，不保存 Token 或预签名 URL。
        self.state["upload"] = {k: data[k] for k in (
            "FileId", "Reuse", "SliceSize", "Bucket", "Key", "StorageNode", "UploadId"
        ) if k in data}
        if isinstance(data.get("Info"), dict) and "FileId" in data["Info"]:
            self.state["upload"]["Info"] = {"FileId": data["Info"]["FileId"]}
        self.state["pending"] = None
        self.persist()

    async def inspect(self):
        if "upload" not in self.state:
            raise ProbeError("初始化结果未知，没有可信文件 id；保留样本，手工核对测试目录")
        # 每个查询各发一次；其中一个失败也不把它解释成文件不存在或无分片。
        errors = []
        for operation, method, payload in (
            ("file_info", self.client.fs_info, self.file_id),
            ("upload_parts", self.client.upload_list, self.payload),
        ):
            try:
                await self.api(operation, method, payload())
            except ProbeError as exc:
                errors.append(str(exc))
        if errors:
            raise ProbeError("；".join(errors))

    async def put(self):
        self.file_id()
        if self.state["upload"].get("Reuse") is not False:
            raise ProbeError("非明确 Reuse=False，不发送文件字节")
        size, count = self.layout()
        index = self.state["next_part"]
        if index >= count:
            raise ProbeError("所有片已发送，不重复 PUT；下一步手动 complete")
        content = (self.directory / "sample.bin").read_bytes()
        if len(content) != self.state["size"] or hashlib.md5(content).hexdigest() != self.state["md5"]:
            raise ProbeError("本地样本已变化，停止上传")
        self.before_write(f"put:{index}")
        payload = self.payload()
        if count > 1:
            payload.update(partNumberStart=index + 1, partNumberEnd=index + 2)
            data = await self.api("prepare", self.client.upload_prepare, payload)
        else:
            data = await self.api("auth", self.client.upload_auth, payload)
        urls = data.get("presignedUrls")
        url = urls.get(str(index + 1)) if isinstance(urls, dict) else None
        parsed = urlsplit(url) if isinstance(url, str) else None
        if parsed is None or parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ProbeError("预签名地址无效，停止上传")
        # 独立 HTTP 请求不使用 P123Client 的默认 Bearer；禁止重定向。
        response = await self.http.put(url, content=content[index * size:(index + 1) * size],
                                       headers={"Cookie": ""})
        self.record(f"put:{index}", response)
        if not response.is_success:
            raise ProbeError(f"PUT: HTTP {response.status_code}，不自动重发")
        self.state["next_part"] += 1
        self.state["pending"] = None
        self.persist()

    async def complete(self):
        file_id = self.file_id()
        _, count = self.layout()
        if self.state["next_part"] != count:
            raise ProbeError("本工具尚未发送全部分片，拒绝合并")
        self.before_write("complete")
        await self.api("complete", self.client.upload_complete,
                       {**self.payload(), "FileId": file_id}, is_multipart=count > 1)
        self.state["complete_sent"] = True
        self.state["pending"] = None
        self.persist()


def normalize_token(token):
    token = token.strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    if not token or not token.isascii() or any(c.isspace() or not c.isprintable() for c in token):
        raise ProbeError("Token 格式无效")
    return token


async def run_locked(args, directory, token=None):
    if args.command == "start":
        if not args.parent or not args.parent.isascii() or not args.parent.isdecimal() or int(args.parent) == 0:
            raise ProbeError("start 必须指定非根目录的正整数 --parent")
        if args.bytes < 0 or args.bytes > MAX_BYTES:
            raise ProbeError("样本大小必须在 0 至 32 MiB 之间")
        if args.source:
            with args.source.open("rb") as source:
                content = source.read(MAX_BYTES + 1)
        else:
            content = secrets.token_bytes(args.bytes)
        if len(content) > MAX_BYTES:
            raise ProbeError("样本超过 32 MiB")
        sample = directory / "sample.bin"
        sample.write_bytes(content)
        os.chmod(sample, 0o600)
        state = {"salt": secrets.token_hex(16), "parent": str(int(args.parent)),
                 "name": f"movieclaw-probe-{secrets.token_hex(12)}.bin", "size": len(content),
                 "md5": hashlib.md5(content).hexdigest(), "next_part": 0}
        save(directory / "private.json", state)
    else:
        state = json.loads((directory / "private.json").read_text())
    if token is None:
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            try:
                token = getpass.getpass("123 Token（仅用于本次进程，不写文件）: ")
            except getpass.GetPassWarning:
                raise ProbeError("无法关闭终端输入回显；请在支持隐藏输入的终端运行，未读取 Token") from None
    token = normalize_token(token)
    # 独立诊断脚本：不运行在宿主中，不冒充宿主预算/RPC 验证。
    http = httpx.AsyncClient(timeout=20, follow_redirects=False, trust_env=False)
    completed = False
    try:
        probe = Probe(directory, state, token, http)
        await probe.identity()
        await getattr(probe, args.command)()
        completed = True
    finally:
        try:
            await http.aclose()
        except Exception:
            # 关闭失败不覆盖业务异常和取消；关闭期间的新取消仍传播。
            if completed:
                raise ProbeError("HTTP 连接关闭失败；阶段状态已保存，先检查记录，不盲目重发") from None
    print("本阶段完成。samples.jsonl 是脱敏候选；共享前仍须人工检查。未判定成品完成或续传安全。")


async def run(args, token=None):
    directory = args.workdir.resolve()
    if args.command == "start":
        directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    # 本地诊断状态不能由两个命令交错推进；锁文件永不删除，避免锁住不同 inode。
    with (directory / ".lock").open("a") as lock:
        os.chmod(lock.name, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ProbeError("该采样目录已有命令运行") from None
        await run_locked(args, directory, token)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("start", "inspect", "put", "complete"))
    parser.add_argument("--workdir", type=Path, required=True)
    parser.add_argument("--parent", help="仅 start 使用：独立空测试目录 id，禁止根目录")
    parser.add_argument("--bytes", type=int, default=12 * 1024 * 1024)
    parser.add_argument("--source", type=Path, help="仅 start 使用：复用既有采样文件，验证秒传；最多 32 MiB")
    args = parser.parse_args()
    try:
        asyncio.run(run(args))
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("已取消；已发出的写请求可能提交。保留采样目录，只读核对后再处理。")
        raise SystemExit(130)
    except ProbeError as exc:
        print(str(exc))
        raise SystemExit(1)
    except Exception as exc:
        # 不打印可能带 Token / 预签名 URL 的第三方异常消息或 traceback。
        print(f"采样中断（{type(exc).__name__}），未自动重试。保留本地私有状态并检查脱敏样本。")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
