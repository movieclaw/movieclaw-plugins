"""仅监听回环地址的手动采样面板；无远端自动操作。"""

import argparse
import asyncio
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import tempfile
import threading
import webbrowser

try:
    from . import probe_upload as probe
except ImportError:
    import probe_upload as probe


REPORT_OPERATIONS = {"empty_test_directory", "initialize", "file_info", "upload_parts",
                     "auth", "prepare", "complete"}


class Panel:
    def __init__(self, directory=None):
        self.directory = Path(directory).resolve() if directory else None
        self.token = ""
        self.revision = 0
        self.lock = threading.Lock()

    def state(self):
        if self.directory is None:
            return {}
        return json.loads((self.directory / "private.json").read_text())

    def report(self):
        if self.directory is None:
            return ""
        path = self.directory / "samples.jsonl"
        if not path.exists():
            return ""
        with path.open("rb") as stream:
            raw = stream.read(4 * 1024 * 1024 + 1)
        if len(raw) > 4 * 1024 * 1024:
            raise probe.ProbeError("报告超过 4 MiB，请在本地人工检查，面板不截断证据")
        rows = []
        for line in raw.decode().splitlines():
            event = json.loads(line)
            operation = event.get("operation", "")
            # 不向浏览器或导出文件发送账号响应，兼容已有的脱敏报告。
            if operation in REPORT_OPERATIONS or (operation.startswith("put:") and operation[4:].isdecimal()):
                rows.append(json.dumps(event, ensure_ascii=False))
        return "\n".join(rows) + ("\n" if rows else "")

    def view(self):
        allowed = []
        count = None
        reason = "请选择已有采样，或创建新采样。"
        try:
            state = self.state()
            if not isinstance(state, dict):
                raise ValueError
        except (OSError, ValueError):
            state = {}
            reason = "本地状态缺失或损坏；保留目录，请打开有效采样。未访问网盘。"
        if state:
            allowed = ["inspect"]
            reason = "初始化记录不完整；只读查询，保留证据。"
            if state.get("pending"):
                reason = "存在结果未知的操作；仅可查询，不重发上传或合并。"
            elif state.get("complete_sent"):
                reason = "已提交合并；仅可查询，仍需核验成品。"
            elif "upload" in state:
                try:
                    instance = probe.Probe(self.directory, state, "", None)
                    instance.file_id()
                    instance.payload()
                    _, count = instance.layout()
                    next_part = state["next_part"]
                    if type(next_part) is not int or not 0 <= next_part <= count:
                        raise probe.ProbeError("本地分片进度无效")
                    if state["upload"].get("Reuse") is False:
                        allowed.append("put" if next_part < count else "complete")
                        reason = "每次只上传一片；查询后再决定下一步。" if next_part < count else "全部片已记录成功；可手动提交一次合并。"
                    else:
                        reason = "Reuse 不是明确的 false；只查询成品，不上传或合并。"
                except (probe.ProbeError, KeyError, TypeError, ValueError):
                    reason = "会话字段不满足上传条件；仅可查询，先检查报告。"
        if not self.token:
            allowed = []
        try:
            report = self.report()
        except (OSError, ValueError, TypeError, AttributeError, probe.ProbeError):
            report = ""
            allowed = [action for action in allowed if action == "inspect"]
            reason = "报告缺失权限、损坏或超过 4 MiB；不能展示或导出，请在本地检查。写操作已禁用。"
        return {"directory": str(self.directory) if self.directory else "", "token_ready": bool(self.token),
                "revision": self.revision, "allowed": allowed, "reason": reason,
                "size": state.get("size"), "md5": state.get("md5"),
                "next_part": state.get("next_part"), "part_count": count,
                "pending": state.get("pending"), "complete_sent": bool(state.get("complete_sent")),
                "report": report}

    def execute(self, data):
        # 单次票据防止双击、旧标签页或失去响应后重放下一片。
        if data.get("revision") != self.revision:
            raise probe.ProbeError("页面状态已变化，请刷新本地状态；未执行操作")
        self.revision += 1
        action = data.get("action")
        if action == "token":
            self.token = probe.normalize_token(data.get("token", ""))
        elif action == "forget":
            self.token = ""
        elif action == "open":
            directory = Path(data["directory"]).expanduser().resolve()
            json.loads((directory / "private.json").read_text())
            self.directory = directory
        elif action in ("start", "inspect", "put", "complete"):
            if not self.token:
                raise probe.ProbeError("请先输入 Token")
            args = argparse.Namespace(command=action, workdir=self.directory, parent=None, bytes=0, source=None)
            if action == "start":
                parent = data.get("parent", "")
                size = data.get("bytes")
                if not isinstance(parent, str) or not parent.isascii() or not parent.isdecimal() or int(parent) <= 0:
                    raise probe.ProbeError("测试目录 ID 必须为非零正整数")
                if type(size) is not int or size not in (0, 12 * 1024 * 1024, 32 * 1024 * 1024):
                    raise probe.ProbeError("请选择 0、12 或 32 MiB 样本")
                if data.get("reuse"):
                    if self.directory is None:
                        raise probe.ProbeError("请先打开包含原样本的采样目录")
                    args.source = self.directory / "sample.bin"
                args.parent, args.bytes = parent, size
                args.workdir = Path(tempfile.gettempdir()) / ("movieclaw-123-probe-" + secrets.token_hex(8))
            elif action not in self.view()["allowed"]:
                raise probe.ProbeError("当前状态不允许此操作；请查询或检查记录")
            try:
                asyncio.run(probe.run(args, token=self.token))
            finally:
                # 即使远端或本地写入失败，也保留新目录，禁止静默开启替代会话。
                if action == "start" and args.workdir.exists():
                    self.directory = args.workdir
        else:
            raise probe.ProbeError("未知操作")


class Server(ThreadingHTTPServer):
    daemon_threads = False

    def __init__(self, panel, port=0):
        self.panel = panel
        self.key = secrets.token_urlsafe(32)
        super().__init__(("127.0.0.1", port), Handler)
        self.origin = f"http://127.0.0.1:{self.server_port}"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, status, value, content_type="application/json; charset=utf-8"):
        body = json.dumps(value, ensure_ascii=False).encode() if content_type.startswith("application/json") else value.encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'nonce-" + self.server.key + "'; style-src 'nonce-" + self.server.key + "'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'")
        try:
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass  # 浏览器断开不重放、不回滚已发出的远端操作。

    def authorized(self):
        if self.headers.get("Host") != self.server.origin.removeprefix("http://"):
            return False
        origin = self.headers.get("Origin")
        if origin is not None and origin != self.server.origin:
            return False
        return True

    def do_GET(self):
        self.handle_request(False)

    def do_POST(self):
        self.handle_request(True)

    def handle_request(self, mutate):
        if not self.authorized():
            self.reply(403, {"error": "拒绝非本机页面请求"})
            return
        if not mutate and self.path == "/":
            html = Path(__file__).with_name("probe_panel.html").read_text()
            self.reply(200, html.replace("__PANEL_KEY__", self.server.key), "text/html; charset=utf-8")
            return
        if self.path != "/api" or not hmac.compare_digest(self.headers.get("X-Panel-Key", "").encode(), self.server.key.encode()):
            self.reply(403, {"error": "页面凭据无效，请重新打开面板"})
            return
        panel = self.server.panel
        if not panel.lock.acquire(blocking=False):
            self.reply(409, {"error": "已有操作进行中，请等待；不会自动重试"})
            return
        try:
            if mutate:
                if self.headers.get("Origin") != self.server.origin or self.headers.get("Content-Type") != "application/json":
                    self.reply(403, {"error": "拒绝跨来源或非 JSON 请求"})
                    return
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 16384:
                    raise probe.ProbeError("请求大小无效")
                self.connection.settimeout(5)
                data = json.loads(self.rfile.read(length))
                if not isinstance(data, dict):
                    raise probe.ProbeError("请求格式无效")
                panel.execute(data)
            self.reply(200, panel.view())
        except (Exception, asyncio.CancelledError) as exc:
            message = str(exc) if isinstance(exc, probe.ProbeError) else "操作中断（" + type(exc).__name__ + "）；保留状态，请刷新本地状态，不重发写操作"
            self.reply(400, {"error": message})
        finally:
            panel.lock.release()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workdir", type=Path, help="打开已有采样；只读取本地状态，不请求网盘")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--no-open", action="store_true")
    args = parser.parse_args()
    server = Server(Panel(args.workdir), args.port)
    print("本地采样面板：" + server.origin + "（关闭页面不取消进行中的操作；Ctrl+C 退出服务）", flush=True)
    if not args.no_open:
        webbrowser.open(server.origin)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        server.panel.token = ""


if __name__ == "__main__":
    main()
