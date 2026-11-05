"""测试替身：内存存储、真实临时目录文件、假主程序操作、假任务队列。全程不联网。"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from movieclaw_sdk.ops import OpsError  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"
STORE_VALUE_LIMIT = 64 * 1024


def load_filetree() -> dict:
    return json.loads((FIXTURES / "filetree.json").read_text("utf-8"))


class FakeStore:
    def __init__(self) -> None:
        self.data: dict[str, Any] = {}

    async def get(self, key: str, *, scope: str = "global", default=None):
        value = self.data.get(key, default)
        return json.loads(json.dumps(value)) if value is not None else value

    async def set(self, key: str, value, *, scope: str = "global", secret: bool = False) -> None:
        encoded = json.dumps(value, ensure_ascii=False)
        assert len(encoded.encode("utf-8")) < STORE_VALUE_LIMIT, f"{key} 超过 64KB"
        self.data[key] = json.loads(encoded)

    async def delete(self, key: str, *, scope: str = "global") -> bool:
        return self.data.pop(key, None) is not None


class FakeFiles:
    """宿主文件代理的替身：直接落在临时目录里（路径都是绝对路径）。"""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.private = root / "plugin-data"
        self.private.mkdir(parents=True, exist_ok=True)
        self.writes: list[str] = []

    def path(self, alias: str, *parts: str) -> str:
        assert alias == "plugin"
        return str(self.private.joinpath(*parts))

    async def exists(self, path) -> bool:
        return os.path.exists(path)

    async def open(self, path, mode: str = "rb"):
        if "w" in mode:
            self.writes.append(str(path))
        return open(path, mode)  # noqa: SIM115

    async def rename(self, src, dst) -> None:
        os.replace(src, dst)

    async def remove(self, path) -> None:
        os.remove(path)

    async def makedirs(self, path) -> None:
        os.makedirs(path, exist_ok=True)

    async def listdir(self, path) -> list[str]:
        return os.listdir(path)

    def response(self, path):
        return {"sendfile": str(path)}


def _read_nfo_actors(path: Path) -> list[dict] | None:
    """模拟主程序 read_local_sidecar：只认 http(s) thumb。"""
    import xml.etree.ElementTree as ET

    if not path.exists():
        return None
    root = ET.fromstring(path.read_bytes())
    actors = []
    for actor in root.findall("actor"):
        name = (actor.findtext("name") or "").strip()
        if not name:
            continue
        thumb = (actor.findtext("thumb") or "").strip()
        actors.append(
            {
                "name": name,
                "role": (actor.findtext("role") or "").strip() or None,
                "thumb": thumb if re.match(r"^https?://", thumb) else None,
            }
        )
    return actors


class FakeOps:
    """假主程序。``host_fixed=False`` 复现 0.46/0.47 的缺口：NFO thumb 读进来但详情不显示。"""

    def __init__(self, media_root: Path, *, host_fixed: bool = False) -> None:
        self.media_root = media_root
        self.host_fixed = host_fixed
        self.libraries = [
            {"id": 1, "name": "电影", "kind": "movie", "source": "tmdb"},
            {"id": 9, "name": "小姐姐", "kind": "video", "source": "local"},
            {"id": 12, "name": "家庭录像", "kind": "video", "source": "local"},
        ]
        self.items: dict[int, dict] = {}
        self.calls: list[tuple[str, dict]] = []
        self.refreshed: list[int] = []
        self._job = 0

    def add_item(
        self,
        item_id: int,
        code: str,
        actors: list[dict],
        *,
        title: str | None = None,
        library_id: int = 9,
        nfo: str | None = None,
    ) -> Path:
        directory = self.media_root / code
        directory.mkdir(parents=True, exist_ok=True)
        strm = directory / f"{code}.strm"
        strm.write_text("http://example.invalid/x")
        if nfo is not None:
            (directory / f"{code}.nfo").write_text(nfo, "utf-8")
        self.items[item_id] = {
            "media_item_id": item_id,
            "library_id": library_id,
            "kind": "video",
            "source": "local",
            "title": title or code,
            "local_meta": {
                "actors": [
                    {"name": a["name"], "role": a.get("role"), "thumb_url": a.get("thumb_url")}
                    for a in actors
                ]
            },
            "entry_dirs": [str(directory)],
            "files": [
                {"file_path": str(strm), "container": "strm", "state": "in_place", "missing": False}
            ],
        }
        return directory / f"{code}.nfo"

    async def call(self, op: str, args: dict) -> Any:
        self.calls.append((op, dict(args)))
        if op == "library.list":
            return [dict(lib) for lib in self.libraries]
        if op == "library.items.list":
            rows = [i for i in self.items.values() if i["library_id"] == args["library_id"]]
            rows.sort(key=lambda r: r["media_item_id"])
            page = rows[args["offset"] : args["offset"] + args["limit"]]
            return [
                {
                    "media_item_id": r["media_item_id"],
                    "kind": r["kind"],
                    "source": r["source"],
                    "title": r["title"],
                }
                for r in page
            ]
        if op == "library.items.get":
            item = self.items.get(args["media_item_id"])
            if item is None or item["library_id"] != args["library_id"]:
                raise OpsError(404, "NOT_FOUND", "not found")
            return json.loads(json.dumps(item))
        if op == "library.items.refresh-metadata":
            item = self.items[args["media_item_id"]]
            self.refreshed.append(args["media_item_id"])
            nfo = Path(item["files"][0]["file_path"]).with_suffix(".nfo")
            actors = _read_nfo_actors(nfo)
            if actors is not None:
                item["local_meta"]["actors"] = [
                    {
                        "name": a["name"],
                        "role": a["role"],
                        "thumb_url": a["thumb"] if self.host_fixed else None,
                    }
                    for a in actors
                ]
            self._job += 1
            return {"job_id": f"job-{self._job}"}
        if op == "jobs.show":
            return {"job": {"id": args["job_id"], "status": "succeeded"}}
        if op == "app.show":
            return {"external_url": ""}
        raise AssertionError(f"unexpected op {op}")


class FakeJobs:
    def __init__(self) -> None:
        self.created: list[dict] = []

    async def create(self, handler: str, **kwargs):
        self.created.append({"handler": handler, **kwargs})
        return SimpleNamespace(id=f"pj-{len(self.created)}", created=True)


class FakeContext:
    def __init__(self) -> None:
        self.progress: list[dict] = []

    async def raise_if_cancelled(self) -> None:
        return None

    async def update_progress(self, **kwargs) -> None:
        self.progress.append(kwargs)


@pytest.fixture
def logger():
    return logging.getLogger("actor-avatar-test")


@pytest.fixture
def store():
    return FakeStore()


@pytest.fixture
def files(tmp_path):
    return FakeFiles(tmp_path)


@pytest.fixture
def media_root(tmp_path):
    root = tmp_path / "media"
    root.mkdir()
    return root


@pytest.fixture
def filetree_data():
    return load_filetree()
