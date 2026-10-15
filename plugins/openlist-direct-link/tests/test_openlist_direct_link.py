"""路径规则与 OpenList 查询（仿 OpenList：/api/fs/get 验令牌、给签名）。"""

from __future__ import annotations

import asyncio
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
from pydantic import SecretStr

from movieclaw_api.hooks import MountedFile
from openlist_direct_link import Config, direct_link, openlist_path, parse_rules

TOKEN = "ol-token"


class _OpenList(BaseHTTPRequestHandler):
    seen: list[tuple[str, str | None]] = []

    def log_message(self, *_args) -> None:
        pass

    def do_POST(self) -> None:  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.seen.append((body["path"], self.headers.get("Authorization")))
        if self.headers.get("Authorization") != TOKEN:
            reply = {"code": 401, "message": "token is invalidated"}
        elif body["path"].startswith("/115/"):
            reply = {"code": 200, "data": {"sign": "s1gn"}}
        else:
            reply = {"code": 500, "message": "object not found"}
        data = json.dumps(reply).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class RulesTest(unittest.TestCase):
    def test_parse_skips_malformed_lines(self) -> None:
        rules = parse_rules(
            ["/cd2/CloudDrive/115/ => /115/", "  /mnt/all => /  ", "没有箭头", "相对 => /x", "/a => b"]
        )
        self.assertEqual(rules, [("/cd2/CloudDrive/115", "/115"), ("/mnt/all", "")])

    def test_longest_prefix_and_whole_segments(self) -> None:
        rules = parse_rules(["/cd2/CloudDrive => /all", "/cd2/CloudDrive/115 => /115"])
        self.assertEqual(openlist_path("/cd2/CloudDrive/115/电影/a.mkv", rules), "/115/电影/a.mkv")
        self.assertEqual(openlist_path("/cd2/CloudDrive/阿里/a.mkv", rules), "/all/阿里/a.mkv")
        self.assertIsNone(openlist_path("/cd2/CloudDrive2/a.mkv", rules))
        self.assertIsNone(openlist_path("/mnt/a.mkv", rules))
        self.assertEqual(openlist_path("/mnt/all/x/a.mkv", parse_rules(["/mnt/all => /"])), "/x/a.mkv")


class DirectLinkTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _OpenList)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def ask(self, path: str, token: str = TOKEN):
        config = Config(base_url=self.base + "/", token=SecretStr(token), ttl_s=120)
        rules = parse_rules(["/cd2/115 => /115"])

        async def run():
            async with httpx.AsyncClient() as client:
                return await direct_link(
                    client, config, rules, MountedFile(file_id=1, path=path, user_agent="Infuse/8")
                )

        return asyncio.run(run())

    def test_claimed_file_gets_the_signed_download_url(self) -> None:
        link = self.ask("/cd2/115/电影/星际 穿越 (2014).mkv")
        self.assertEqual(
            link.url,
            f"{self.base}/d/115/%E7%94%B5%E5%BD%B1/%E6%98%9F%E9%99%85%20%E7%A9%BF%E8%B6%8A%20%282014%29.mkv?sign=s1gn",
        )
        self.assertEqual(link.ttl_s, 120)
        self.assertIn(("/115/电影/星际 穿越 (2014).mkv", TOKEN), _OpenList.seen)

    def test_paths_outside_the_rules_are_not_claimed(self) -> None:
        before = len(_OpenList.seen)
        self.assertIsNone(self.ask("/rclone/阿里/a.mkv"))
        self.assertEqual(len(_OpenList.seen), before)  # 不认领的不去问 OpenList

    def test_openlist_refusal_is_an_error_for_the_caller(self) -> None:
        with self.assertRaises(LookupError):
            self.ask("/cd2/115/a.mkv", token="wrong")


if __name__ == "__main__":
    unittest.main()
