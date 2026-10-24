"""Explicit, one-shot adapter checks. Never import or call a downloader.

The public search operation supplies the authenticated site instance. An
unpredictable, expiring ticket authorizes just that instance to also read one
detail page and one small torrent file. Ordinary searches are unchanged.
"""
import asyncio
import secrets
import time
from datetime import datetime, timezone

from movieclaw_tracker.exceptions import TrackerParseError
from .management import redacted_error

PREFIX = "__pt_pack_probe__:"
TICKETS = {}
REPORTS = {}


def issue_ticket(site_id, keyword, include_torrent=True):
    now = time.monotonic()
    for key, ticket in list(TICKETS.items()):
        if ticket["expires"] < now:
            TICKETS.pop(key, None)
    token = secrets.token_hex(24)
    TICKETS[token] = {"site_id": site_id, "keyword": keyword, "expires": now + 120, "include_torrent": include_torrent}
    return token, PREFIX + token


def torrent_metadata(data):
    """Bounded bencode parsing; only non-secret aggregate metadata is returned."""
    if not isinstance(data, bytes) or not data or len(data) > 8 * 1024 * 1024:
        raise TrackerParseError("种子文件为空或超过 8 MiB 校验上限")
    pos, nodes = 0, 0

    def fail():
        raise TrackerParseError("响应不是有效的 bencode torrent 文件")

    def parse(depth=0):
        nonlocal pos, nodes
        nodes += 1
        if depth > 64 or nodes > 200000 or pos >= len(data):
            fail()
        marker = data[pos:pos+1]
        if marker == b"i":
            end = data.find(b"e", pos+1)
            raw = data[pos+1:end] if end >= 0 else b""
            import re
            if not re.fullmatch(rb"0|-?[1-9][0-9]{0,19}", raw):
                fail()
            pos = end+1
            return int(raw)
        if marker in (b"l", b"d"):
            pos += 1
            result = [] if marker == b"l" else {}
            while pos < len(data) and data[pos:pos+1] != b"e":
                key = parse(depth+1)
                if marker == b"d":
                    if not isinstance(key, bytes) or key in result:
                        fail()
                    result[key] = parse(depth+1)
                else:
                    result.append(key)
            if pos >= len(data):
                fail()
            pos += 1
            return result
        end = data.find(b":", pos)
        raw = data[pos:end] if end >= 0 else b""
        if not raw or not raw.isdigit() or len(raw) > 8 or (len(raw) > 1 and raw.startswith(b"0")):
            fail()
        length = int(raw)
        pos = end+1
        if pos+length > len(data):
            fail()
        value = data[pos:pos+length]
        pos += length
        return value

    root = parse()
    if pos != len(data) or not isinstance(root, dict) or not isinstance(root.get(b"info"), dict):
        fail()
    info = root[b"info"]
    if not isinstance(info.get(b"name"), bytes) or not info[b"name"]:
        fail()
    piece_length = info.get(b"piece length")
    if type(piece_length) is not int or piece_length <= 0:
        fail()
    if isinstance(info.get(b"files"), list) and info[b"files"]:
        lengths = []
        for entry in info[b"files"]:
            if not isinstance(entry, dict) or not isinstance(entry.get(b"path"), list) or not entry[b"path"]:
                fail()
            if any(not isinstance(p, bytes) or not p or p in (b".", b"..") or b"/" in p or b"\\" in p for p in entry[b"path"]):
                fail()
            lengths.append(entry.get(b"length"))
    elif type(info.get(b"length")) is int:
        lengths = [info[b"length"]]
    else:
        # v2-only trees require a different validator, not a false success.
        raise TrackerParseError("种子未提供 v1 文件长度，暂不能完整校验 v2-only 种子")
    if any(type(n) is not int or n < 0 for n in lengths):
        fail()
    total = sum(lengths)
    pieces = info.get(b"pieces")
    if not isinstance(pieces, bytes) or len(pieces) != 20 * ((total + piece_length - 1) // piece_length):
        fail()
    return {"bytes": len(data), "file_count": len(lengths), "content_bytes": total,
            "piece_count": len(pieces)//20, "bencode_valid": True}


class ProbeMixin:
    async def search(self, query):
        keyword = query.keyword
        if not keyword.startswith(PREFIX):
            return await self._search_plain(query)
        token = keyword[len(PREFIX):]
        ticket = TICKETS.get(token)
        if not ticket or ticket["site_id"] != self.site_id or ticket["expires"] < time.monotonic():
            raise TrackerParseError("诊断票据无效或已过期；请从管理员诊断入口发起")
        TICKETS.pop(token, None)
        report = {"site_id": self.site_id, "keyword": ticket["keyword"],
                  "checked_at": datetime.now(timezone.utc).isoformat(), "download_tasks_created": 0,
                  "search": {"ok": False}, "detail": {"ok": False}, "torrent": {"ok": False}}
        REPORTS[self.site_id] = report
        stage = "search"
        try:
            result = await self._search_plain(query.model_copy(update={"keyword": ticket["keyword"]}))
            report["search"] = {"ok": True, "count": len(result.items),
                                "sample_titles": [" ".join(i.title.split()) for i in result.items[:3]]}
            if not result.items:
                report["detail"] = report["torrent"] = {"ok": False, "error": "搜索没有结果，无法选择测试种子"}
                return result
            item = next((i for i in result.items if i.detail_url and i.download_url), result.items[0])
            report["sample"] = {"torrent_id": item.torrent_id, "title": " ".join(item.title.split()),
                                "size_bytes": item.size_bytes, "free": item.free}
            stage = "detail"
            detail = None
            try:
                if not item.detail_url:
                    raise TrackerParseError("搜索结果缺少详情链接")
                detail = await self.get_torrent_detail(item.detail_url)
                if detail.torrent_id != item.torrent_id or not detail.title.strip():
                    raise TrackerParseError("详情种子 ID 或标题不正确")
                report["detail"] = {"ok": True, "torrent_id": detail.torrent_id,
                                    "title": " ".join(detail.title.split()),
                                    "description_chars": len(detail.description),
                                    "size_bytes": detail.size_bytes, "file_count": len(detail.file_list),
                                    "has_download_url": bool(detail.download_url)}
            except Exception as exc:
                report["detail"] = {"ok": False, "error": redacted_error(str(exc))}
            if not ticket.get("include_torrent", True):
                report["torrent"] = {"ok": False, "skipped": True, "reason": "本次仅检查搜索和详情"}
                return result
            stage = "torrent"
            try:
                target = (detail.download_url if detail else None) or item.download_url
                if not target:
                    raise TrackerParseError("列表和详情均缺少普通种子下载链接")
                raw = await self.download_torrent(target)
                metadata = torrent_metadata(raw)
                del raw  # No file is stored, returned, or submitted to a downloader.
                consistent = not item.size_bytes or abs(metadata["content_bytes"] - item.size_bytes) <= max(1048576, item.size_bytes * .02)
                report["torrent"] = {"ok": consistent, **metadata, "size_consistent": consistent}
                if not consistent:
                    report["torrent"]["error"] = "种子内容大小与搜索结果不一致"
            except Exception as exc:
                report["torrent"] = {"ok": False, "error": redacted_error(str(exc))}
            return result
        except asyncio.CancelledError:
            report[stage] = {"ok": False, "error": "诊断请求中断或超时"}
            raise
        except Exception as exc:
            report[stage] = {"ok": False, "error": redacted_error(str(exc))}
            raise
        finally:
            report["complete"] = True
