"""在干净进程走真实宿主隔离加载，再验证原生apply登记和私有资源释放。"""

import json
import os
import subprocess
import sys
from importlib import import_module
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]

# 子进程不继承pytest的插件pythonpath，不创建宿主隔离名的父namespace。
# 这样才能重现直接import movieclaw_friends检测不到的真实安装加载问题。
PROGRAM = r"""
import asyncio
import inspect
import json
import logging
import sqlite3
import sys
import tomllib
from pathlib import Path

from movieclaw_api.plugins.keys import PLUGIN_FILES, PLUGIN_ROUTES
from movieclaw_api.plugins.packages import Installed, _inline
from movieclaw_kernel import Plugin
from movieclaw_sdk.ops import HOST_OPS

plugin_root = Path(sys.argv[1]).resolve()
work = Path(sys.argv[2]).resolve()
private = work / "data" / "plugins" / "data" / "watch-friends"
namespace = "movieclaw_packages.watch_friends.movieclaw_friends"
assert plugin_root not in {Path(item).resolve() for item in sys.path if item}
assert "movieclaw_packages" not in sys.modules
assert "movieclaw_packages.watch_friends" not in sys.modules
assert "movieclaw_friends" not in sys.modules
manifest = tomllib.loads((plugin_root / "movieclaw-plugin.toml").read_text())
assert manifest["plugin"]["version"] == "0.1.2"
assert manifest["plugin"]["runtime"] == "inline"
permissions = ("members.list", "playback.history", "search.library")
assert set(manifest["permissions"]["operations"]) == set(permissions)
package = Installed(
    id="watch-friends", version="0.1.2", title="映迹",
    entry="movieclaw_friends", runtime="inline", operations=list(permissions),
)
connections = []


def audit(event, args):
    if event == "socket.bind":
        raise AssertionError("原生插件不得另行绑定端口或套接字")
    if event == "sqlite3.connect":
        database = Path(args[0]).resolve()
        assert database.is_relative_to(private), "不得打开宿主业务数据库"
        connections.append(database)


sys.addaudithook(audit)


class Files:
    def scoped(self, ctx):
        return self

    def path(self, alias):
        assert alias == "plugin"
        return str(private)

    async def check(self, path, *, write):
        assert Path(path).resolve() == private
        assert write is True
        private.mkdir(parents=True, exist_ok=True)
        return private


class Ops:
    async def client(self, ctx):
        return self

    async def call(self, operation, arguments):
        raise AssertionError("apply只能登记，不能在安装时读取真实用户观影数据")


class Routes:
    def __init__(self):
        self.mounts = []
        self.signed = []

    async def sign(self, ctx, path, *, absolute):
        assert absolute is True
        self.signed.append(path)
        return "https://friends.example/api/v1/plugins/watch-friends" + path + "?sig=test"

    def mount(self, ctx, router, *, zone):
        assert zone in {"member", "public"}
        self.mounts.append((zone, router))
        assert all(
            route.operation_id.startswith("plugins.watch-friends.")
            for route in router.routes
        )
        return "/api/v1/plugins/watch-friends"


class Context:
    entry_id = "watch-friends"
    logger = logging.getLogger("watch-friends-package-test")

    def __init__(self, loaded):
        self.config = loaded.config(demo_enabled=True)
        self.permissions = loaded.permissions
        self.routes = Routes()
        self.services = {HOST_OPS: Ops(), PLUGIN_FILES: Files(), PLUGIN_ROUTES: self.routes}
        self.used = []
        self.effects = []

    def use(self, key):
        self.used.append(key)
        assert key in self.services, "只能使用正式批准的三个插件服务"
        return self.services[key]

    def task(self, coro, *, name):
        return asyncio.create_task(coro, name=name)

    def effect(self, disposer, *, label):
        self.effects.append(disposer)


async def startup_and_release(loaded):
    ctx = Context(loaded)
    await loaded.apply(ctx)
    assert set(ctx.used) == {HOST_OPS, PLUGIN_FILES, PLUGIN_ROUTES}
    assert {zone for zone, router in ctx.routes.mounts} == {"member", "public"}
    mounted = {zone: router for zone, router in ctx.routes.mounts}
    # CLI 目录按 operation_id 登记；不同网页路径也不能共用同一个操作编号。
    operation_ids = [
        route.operation_id for router in mounted.values() for route in router.routes
    ]
    duplicates = {name for name in operation_ids if operation_ids.count(name) > 1}
    assert not duplicates, f"原生路由操作编号重复：{sorted(duplicates)}"
    member_paths = {route.path for route in mounted["member"].routes}
    assert "/me" in member_paths and "/profile" in member_paths
    assert not {"/login", "/logout", "/"}.intersection(member_paths)
    federation = {
        "/federation/requests", "/federation/state", "/federation/data", "/federation/social",
        "/federation/directory"
    }
    assert {route.path for route in mounted["public"].routes} == federation
    assert set(ctx.routes.signed) == federation
    assert len(ctx.effects) == 5
    store = ctx.effects[0].__self__
    virtual = store.all("SELECT id,role FROM users WHERE role='demo'")
    assert len(virtual) == 4
    assert store.all("SELECT id FROM users WHERE role!='demo'") == []
    assert store.all("SELECT * FROM sessions") == []
    assert (private / "friends.sqlite3").is_file()
    assert (private / "credentials.key").is_file()
    for dispose in reversed(ctx.effects):
        result = dispose()
        if inspect.isawaitable(result):
            await result
    try:
        store.db.execute("SELECT 1")
    except sqlite3.ProgrammingError:
        pass
    else:
        raise AssertionError("释放插件后私有SQLite连接仍然打开")


first = _inline(plugin_root, package)
assert isinstance(first, Plugin)
assert first.name == "watch-friends"
assert first.permissions == permissions
assert first.apply.__module__ == namespace + ".native"
assert sys.modules[namespace].__version__ == "0.1.2"
assert "movieclaw_packages" not in sys.modules
assert "movieclaw_packages.watch_friends" not in sys.modules
assert not any(name == "movieclaw_friends" or name.startswith("movieclaw_friends.")
               for name in sys.modules)
first_module = sys.modules[namespace]
first_app = sys.modules[namespace + ".app"]
asyncio.run(startup_and_release(first))
second = _inline(plugin_root, package)
assert second is not first
assert second.apply is not first.apply
assert sys.modules[namespace] is not first_module
assert sys.modules[namespace + ".app"] is not first_app
assert second.permissions == permissions
assert sys.modules[namespace].__version__ == "0.1.2"
asyncio.run(startup_and_release(second))
assert connections == [private / "friends.sqlite3", private / "friends.sqlite3"]
assert "movieclaw_packages" not in sys.modules
assert "movieclaw_packages.watch_friends" not in sys.modules
assert not any(name == "movieclaw_friends" or name.startswith("movieclaw_friends.")
               for name in sys.modules)
print(json.dumps({"loaded": "watch-friends", "version": "0.1.2", "loads": 2,
                  "zones": ["member", "public"], "private_connections": len(connections)}))
"""


def test_actual_host_inline_loading_startup_and_reload_in_clean_process(tmp_path):
    host = import_module("movieclaw_api.plugins.packages")
    host_src = Path(host.__file__).resolve().parents[2]
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(host_src)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    result = subprocess.run(
        [sys.executable, "-c", PROGRAM, str(PLUGIN_ROOT), str(tmp_path)],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout) == {
        "loaded": "watch-friends",
        "version": "0.1.2",
        "loads": 2,
        "zones": ["member", "public"],
        "private_connections": 2,
    }
