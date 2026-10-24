"""本站站点目录：持久配置、乐观版本保护与可恢复的适配历史。"""
import asyncio
import copy
import json
import re
from dataclasses import fields
from pathlib import Path
from uuid import uuid4

import yaml
from fastapi import APIRouter, HTTPException, Request
from starlette.responses import Response
from parsel import Selector
from movieclaw_api.plugins.keys import PLUGIN_DATA, PLUGIN_FILES, SITE_CLASSES, SITE_DATA_PACKS
from movieclaw_tracker import list_sites
from movieclaw_tracker.selectors import NexusPHPSelectors
from .assistant import public_home, host, SECRET
from .identity import validate_user_agent, with_identity
from .management import check_write_origin
from .adapters import PackNexusSite
from .overview import HREvidence
from .profiles import ProfileEvidence

MAX_SITES = 100
ROOT_FIELDS = {"site_id", "display_name", "base_url", "framework", "auth", "selectors", "categories", "min_request_interval", "timeout", "max_retries", "http2", "timezone", "hr_seed_hours"}


def legacy_value(ctx, name, default=None):
    config = ctx.config
    return config.get(name, default) if isinstance(config, dict) else getattr(config, name, default)


def validate_config(value):
    """导入只接受声明式 NexusPHP 配置；不允许代码、凭据或任意认证扩展。"""
    if isinstance(value, str):
        if len(value) > 100000:
            raise ValueError("适配文件不能超过 100 KB")
        try:
            value = yaml.safe_load(value)
        except yaml.YAMLError:
            raise ValueError("YAML 格式不正确") from None
    if not isinstance(value, dict) or set(value) - ROOT_FIELDS:
        raise ValueError("只接受站点配置字段，不接受 custom_class、代码或凭据")
    raw = copy.deepcopy(value)
    if not re.fullmatch(r"[a-z][a-z0-9_-]{2,39}", str(raw.get("site_id", ""))):
        raise ValueError("站点 ID 需为 3–40 位小写字母、数字、连字符或下划线")
    if not isinstance(raw.get("display_name"), str) or not 1 <= len(raw["display_name"]) <= 80:
        raise ValueError("请填写不超过 80 字的站点名称")
    base = public_home(str(raw.get("base_url", "")))
    from urllib.parse import urlparse
    if urlparse(base).path != "/":
        raise ValueError("站点地址只填写 HTTPS 域名，不含路径")
    raw["base_url"] = base.rstrip("/")
    if raw.get("framework") != "nexusphp":
        raise ValueError("自助添加目前支持 NexusPHP；其他框架需要专用适配器")
    if raw.get("auth", {"supported": ["cookie"]}) != {"supported": ["cookie"]}:
        raise ValueError("自定义站点由原生设置保存 Cookie，文件不能包含认证信息")
    raw["auth"] = {"supported": ["cookie"]}
    selectors = raw.setdefault("selectors", {})
    allowed = {f.name for f in fields(NexusPHPSelectors) if not f.name.startswith("login_")}
    if not isinstance(selectors, dict) or set(selectors) - allowed:
        raise ValueError("包含不支持的选择器或登录字段")
    for key, item in selectors.items():
        if key == "page_offset":
            if type(item) is not int or item not in (-1, 0): raise ValueError("分页偏移只能为 -1 或 0")
        elif key.startswith("promo_") and key.endswith("_rules"):
            if not isinstance(item, dict) or len(item) > 20: raise ValueError("促销规则格式不正确")
            for css, rate in item.items():
                if not isinstance(css, str) or type(rate) not in (int, float) or not 0 <= rate <= 10: raise ValueError("促销规则格式不正确")
                try: Selector(text="<html/>").css(css)
                except Exception: raise ValueError("促销选择器格式不正确") from None
        else:
            if not isinstance(item, str) or len(item) > 500 or any(c in item for c in ("\r", "\n")) or SECRET.search(item):
                raise ValueError("选择器格式不正确或疑似包含凭据")
            if key.endswith("_css") and item:
                try: Selector(text="<html/>").css(item.removesuffix("::next_sibling_text"))
                except Exception: raise ValueError("CSS 选择器不能解析：" + key) from None
            if key.endswith("_path") and (not re.fullmatch(r"[a-zA-Z0-9_./-]{1,100}", item) or ".." in item or item.startswith("/")):
                raise ValueError("请求路径必须为本站相对路径")
    selectors.setdefault("profile_uid_css", "a[href*='userdetails.php?id=']::attr(href)")
    categories = raw.setdefault("categories", {})
    if not isinstance(categories, dict) or set(categories) - {"movie", "tv", "documentary", "anime", "music", "game", "av", "other"}:
        raise ValueError("分类名称不正确")
    if any(not isinstance(ids, list) or len(ids) > 100 or any(not re.fullmatch(r"\d{1,6}", str(i)) for i in ids) for ids in categories.values()):
        raise ValueError("分类 ID 必须为数字数组")
    for key, default, low, high in [("min_request_interval", 3, 3, 120), ("timeout", 30, 5, 60), ("max_retries", 2, 1, 3)]:
        n = raw.setdefault(key, default)
        if type(n) not in (int, float) or not low <= n <= high: raise ValueError(key + " 超出支持范围")
    if "http2" in raw and type(raw["http2"]) is not bool: raise ValueError("http2 必须为布尔值")
    if "timezone" in raw:
        from zoneinfo import ZoneInfo
        try: ZoneInfo(raw["timezone"])
        except Exception: raise ValueError("时区不正确") from None
    if "hr_seed_hours" in raw and (type(raw["hr_seed_hours"]) is not int or not 0 <= raw["hr_seed_hours"] <= 8760): raise ValueError("H&R 时长不正确")
    return raw


class Workspace:
    def __init__(self, ctx, officials, bundled, registrations):
        self.ctx, self.officials, self.bundled, self.registrations = ctx, officials, bundled, registrations
        self.data = ctx.use(PLUGIN_DATA).scoped(ctx)
        self.files = ctx.use(PLUGIN_FILES).scoped(ctx)
        self.evidence = HREvidence(self.data)
        self.profiles = ProfileEvidence(self.data)
        self.lock = asyncio.Lock()
        self.state = None
        self.state_file = None
        self.directory = None
        self.remove_pack = None
        self.bases = {s.site_id: getattr(s.site_class, "_pt_identity_base", s.site_class) for s in list_sites()}

    def user_agent(self, sid):
        return self.state["user_agents"].get(sid) or self.state["default_user_agent"]

    def statistics_record(self, sid):
        return self.state.get("statistics_overrides", {}).get(sid, {})

    def statistics_selectors(self, sid):
        return self.statistics_record(sid).get("selectors", {})

    async def start(self):
        await self.evidence.start()
        await self.profiles.start()
        self.state = await self.data.get("workspace-v1")
        if self.state and "file" in self.state:
            self.state_file = self.state["file"]
            with await self.files.open(self.state["file"], "rb") as source:
                self.state = json.loads(await asyncio.to_thread(source.read))
        if self.state is None:
            legacy = legacy_value(self.ctx, "hitpt_user_agent", "")
            self.state = {"revision": 0, "default_user_agent": "", "user_agents": {"hitpt": validate_user_agent(legacy)} if legacy else {}, "selected": [], "custom": {}, "jobs": {}}
            await self.persist()
        self.ctx.contribute(SITE_CLASSES, "custom-nexus", with_identity(PackNexusSite, self.user_agent, self.evidence.observe, self.profiles.observe, self.statistics_selectors))
        for raw in self.officials:
            sid = raw["site_id"]
            if sid in self.bases:
                self.ctx.contribute(SITE_CLASSES, "identity-" + sid, with_identity(self.bases[sid], self.user_agent, self.evidence.observe, self.profiles.observe, self.statistics_selectors))
        await self.publish()
        if hasattr(self.ctx, "effect"):
            self.ctx.effect(self.close, label="site-workspace")

    async def persist(self):
        # 目录和历史写入插件自有文件，最后原子提交指针，避免数据单值 64 KB 上限。
        filename = self.files.path("plugin", "workspace-" + uuid4().hex + ".json")
        await self.files.makedirs(str(Path(filename).parent))
        content = json.dumps(self.state, ensure_ascii=False).encode()
        if len(content) > 8 * 1024 * 1024: raise ValueError("自定义配置与历史超过 8 MB，请精简配置")
        with await self.files.open(filename, "wb") as target:
            await asyncio.to_thread(target.write, content)
        await self.data.set("workspace-v1", {"file": filename}, secret=True)
        previous, self.state_file = self.state_file, filename
        if previous and hasattr(self.files, "remove"):
            try: await self.files.remove(previous)
            except OSError: pass

    def close(self):
        if self.remove_pack:
            self.remove_pack()
            self.remove_pack = None

    async def publish(self):
        directory = self.files.path("plugin", "catalog-" + uuid4().hex)
        await self.files.makedirs(directory)
        configs = {}
        for raw in self.officials:
            item = copy.deepcopy(raw)
            if item["site_id"] in self.bases:
                item["custom_class"] = "pt-site-pack:identity-" + item["site_id"]
            configs[item["site_id"]] = item
        official_hosts = {host(v["base_url"]) for v in self.officials}
        for raw in self.bundled:
            sid = raw["site_id"]
            if sid not in configs and host(raw["base_url"]) not in official_hosts and legacy_value(self.ctx, sid, True):
                configs[sid] = raw
        for sid, record in self.state["custom"].items():
            if record["enabled"] and not record.get("archived"):
                item = copy.deepcopy(record["config"])
                item["custom_class"] = "pt-site-pack:custom-nexus"
                configs[sid] = item
        for sid, item in configs.items():
            with await self.files.open(str(Path(directory) / (sid + ".yaml")), "wb") as out:
                await asyncio.to_thread(out.write, yaml.safe_dump(item, allow_unicode=True).encode())
        # 注册新的完整目录后释放旧贡献；不会累积覆盖栈，也无需重启主程序。
        if hasattr(self.ctx, "registry"):
            previous = self.remove_pack
            self.remove_pack = self.ctx.registry(SITE_DATA_PACKS).add("pt-site-pack:workspace", Path(directory), entry_id="pt-site-pack", tier=1, override=True)
            if previous: previous()
        else:
            self.ctx.contribute(SITE_DATA_PACKS, "workspace", Path(directory))
        old_directory, self.directory = self.directory, directory
        if old_directory and hasattr(self.files, "remove"):
            try: await self.files.remove(old_directory)
            except OSError: pass
        try:
            from movieclaw_api.services.site_access import get_site_access
            access = get_site_access()
            for sid in self.state["custom"]: await access.invalidate(sid)
        except RuntimeError:
            pass

    def check_revision(self, payload):
        if payload.get("revision") != self.state["revision"]:
            raise HTTPException(409, "内容已被另一页面更新，请刷新后再保存")

    async def mutate(self, payload, operation, publish=False):
        async with self.lock:
            self.check_revision(payload)
            previous = copy.deepcopy(self.state)
            try:
                result = operation()
                self.state["revision"] += 1
                if publish: await self.publish()
                await self.persist()
            except Exception:
                self.state = previous
                if publish: await self.publish()
                raise
            return {"revision": self.state["revision"], **(result or {})}

    def assert_new(self, config):
        sid = config["site_id"]
        current = self.state["custom"].get(sid)
        if current:
            if host(current["config"]["base_url"]) != host(config["base_url"]):
                raise ValueError("不能把现有站点 ID 改绑到其他域名，请另建站点")
            return
        known = self.officials + self.bundled + [r["config"] for r in self.state["custom"].values()]
        for raw in known:
            if raw["site_id"] == sid or host(raw["base_url"]) == host(config["base_url"]):
                raise ValueError("目录已支持此 ID 或域名，请直接添加已有站点")
        for row in list_sites():
            if row.site_id == sid or host(row.base_url) == host(config["base_url"]): raise ValueError("此站点已由其他适配提供，请直接配置账号")
        if len(self.state["custom"]) >= MAX_SITES: raise ValueError("本站自定义站点已达 100 个")

    def save_config(self, config):
        self.assert_new(config)
        sid = config["site_id"]
        old = self.state["custom"].get(sid)
        history = ([copy.deepcopy(old["config"])] + old["history"])[:10] if old else []
        self.state["custom"][sid] = {"config": config, "history": history, "enabled": True, "archived": False}
        if sid not in self.state["selected"]: self.state["selected"].append(sid)
        return {"site_id": sid, "message": "已加入本站目录，请配置账号并验证"}


def workspace_router(workspace):
    router = APIRouter()

    @router.get("/workspace", operation_id="plugins.pt-site-pack.workspace")
    async def read():
        state = workspace.state
        return {**state, "custom": {sid: {**record, "history": len(record["history"])} for sid, record in state["custom"].items()},
                "jobs": [{k: j.get(k) for k in ("id", "site_url", "site_id", "state", "message")} for j in reversed(list(state["jobs"].values()))]}

    @router.post("/workspace/validate", operation_id="plugins.pt-site-pack.validate")
    async def validate(request: Request, payload: dict):
        check_write_origin(request)
        try: return {"config": validate_config(payload.get("yaml"))}
        except ValueError as exc: raise HTTPException(400, str(exc)) from None

    @router.post("/workspace/identity", operation_id="plugins.pt-site-pack.identity")
    async def identity(request: Request, payload: dict):
        check_write_origin(request)
        try:
            ua = validate_user_agent(payload.get("user_agent", ""))
            sid = payload.get("site_id")
            if sid and sid not in {s.site_id for s in list_sites()} | set(workspace.state["custom"]): raise ValueError("目录中没有此站点")
            def change():
                if sid:
                    if ua: workspace.state["user_agents"][sid] = ua
                    else: workspace.state["user_agents"].pop(sid, None)
                else: workspace.state["default_user_agent"] = ua
                return {"message": "已保存，后续请求使用此设置"}
            return await workspace.mutate(payload, change)
        except ValueError as exc: raise HTTPException(400, str(exc)) from None

    @router.post("/workspace/select", operation_id="plugins.pt-site-pack.select")
    async def select(request: Request, payload: dict):
        check_write_origin(request)
        sid = payload.get("site_id")
        if sid not in {s.site_id for s in list_sites()}: raise HTTPException(404, "目录中没有此站点")
        def change():
            if sid not in workspace.state["selected"]: workspace.state["selected"].append(sid)
            return {"site_id": sid}
        return await workspace.mutate(payload, change)

    @router.post("/workspace/import", operation_id="plugins.pt-site-pack.import")
    async def import_config(request: Request, payload: dict):
        check_write_origin(request)
        try:
            config = validate_config(payload.get("yaml"))
            return await workspace.mutate(payload, lambda: workspace.save_config(config), publish=True)
        except ValueError as exc: raise HTTPException(400, str(exc)) from None

    @router.post("/workspace/template", operation_id="plugins.pt-site-pack.template")
    async def template(request: Request, payload: dict):
        check_write_origin(request)
        try:
            raw = validate_config({"site_id": payload.get("site_id"), "display_name": payload.get("display_name"), "base_url": payload.get("site_url"), "framework": "nexusphp"})
            return {"yaml": yaml.safe_dump(raw, allow_unicode=True)}
        except ValueError as exc: raise HTTPException(400, str(exc)) from None

    @router.post("/workspace/custom/{site_id}/{action}", operation_id="plugins.pt-site-pack.custom-action")
    async def custom_action(site_id: str, action: str, request: Request, payload: dict):
        check_write_origin(request)
        if site_id not in workspace.state["custom"]: raise HTTPException(404, "没有此自定义站点")
        if action not in {"enable", "disable", "rollback", "archive"}: raise HTTPException(400, "不支持的操作")
        def change():
            record = workspace.state["custom"][site_id]
            if action == "rollback":
                if not record["history"]: raise HTTPException(409, "没有可回退的上一版")
                old = record["config"]; record["config"] = record["history"][0];record["history"] = [old] + record["history"][1:]
            else:
                record["enabled"] = action == "enable"
                record["archived"] = action == "archive"
            return {"site_id": site_id, "message": "适配已更新，原生账号配置保留"}
        return await workspace.mutate(payload, change, publish=True)

    @router.get("/workspace/export/{site_id}", operation_id="plugins.pt-site-pack.export")
    async def export(site_id: str):
        record = workspace.state["custom"].get(site_id)
        if not record: raise HTTPException(404, "没有此自定义站点")
        return Response(yaml.safe_dump(record["config"], allow_unicode=True), media_type="application/yaml", headers={"Content-Disposition": f'attachment; filename="{site_id}.yaml"', "Cache-Control": "no-store"})

    return router
