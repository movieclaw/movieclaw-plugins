"""本站独立的 PT 目录与自助适配；不修改主程序或保存原生账号凭据。"""
from pathlib import Path
import movieclaw_tracker
import yaml
from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict
from movieclaw_sdk import plugin
from movieclaw_sdk.agent import AGENT
from movieclaw_sdk.ops import HOST_OPS
from movieclaw_api.plugins.keys import PLUGIN_DATA, PLUGIN_FILES, PLUGIN_ROUTES, SITE_CLASSES
from .adapters import PackNexusSite, GreatPosterWallSite, ZhuqueSite
from .identity import with_identity
from .workspace import Workspace, workspace_router, legacy_value
from .management import management_router
from .assistant import assistant_router


class Settings(BaseModel):
    # 旧值仍保留用于迁移和回退；主页面提供所有实际设置，不再展示固定站点菜单。
    model_config = ConfigDict(extra="allow")


def hostname(url):
    from urllib.parse import urlparse
    return (urlparse(url).hostname or "").lower().removeprefix("www.")


def official_conflict(candidate, officials):
    hosts = {hostname(candidate.get(k, "")) for k in ("base_url", "web_base_url")} - {""}
    return any(candidate["site_id"] == o.get("site_id") or hosts.intersection(
        {hostname(o.get(k, "")) for k in ("base_url", "web_base_url")} - {""}
    ) for o in officials)


@plugin("pt-site-pack", title="PT岛", inject=(PLUGIN_FILES, PLUGIN_ROUTES, HOST_OPS, AGENT, PLUGIN_DATA),
        requires={"site-classes": "^1.0", "site-data-packs": "^1.0"},
        config=None, reloadable=True, disableable=True,
        permissions=("site.catalog", "site.list", "site.stats", "site.verify", "search.torrents", "dl.tasks", "agent:none"))
async def apply(ctx):
    official_dir = Path(movieclaw_tracker.__file__).parent / "sites" / "configs"
    officials = [yaml.safe_load(p.read_text()) for p in official_dir.glob("*.yaml") if not p.name.startswith("_")]
    officials = [o for o in officials if isinstance(o, dict) and o.get("site_id")]
    pack_sites = [yaml.safe_load(p.read_text()) for p in sorted((Path(__file__).parent / "sites").glob("*.yaml"))]
    registrations = [{"site_id": raw["site_id"], "name": raw["display_name"],
        "state": "disabled" if not legacy_value(ctx, raw["site_id"], True) else "official-skipped" if official_conflict(raw, officials) else "registered"} for raw in pack_sites]
    workspace = Workspace(ctx, officials, pack_sites, registrations)
    ctx.contribute(SITE_CLASSES, "nexus", with_identity(PackNexusSite, workspace.user_agent, workspace.evidence.observe, workspace.profiles.observe, workspace.statistics_selectors))
    ctx.contribute(SITE_CLASSES, "greatposterwall", with_identity(GreatPosterWallSite, workspace.user_agent, workspace.evidence.observe, workspace.profiles.observe, workspace.statistics_selectors))
    ctx.contribute(SITE_CLASSES, "zhuque", with_identity(ZhuqueSite, workspace.user_agent, workspace.evidence.observe, workspace.profiles.observe, workspace.statistics_selectors))
    await workspace.start()
    try:
        from movieclaw_api.services.site_access import get_site_access
        access = get_site_access()
        for site in movieclaw_tracker.list_sites():
            await access.invalidate(site.site_id)
    except RuntimeError:
        pass
    router = APIRouter()

    @router.get("/status", operation_id="plugins.pt-site-pack.status")
    async def status():
        return {"sites": registrations, "version": "0.1.0", "management_page": "/apps/pt-site-pack",
                "authentication": "账号和 Cookie 由本服务器原生站点管理保存", "custom_count": len(workspace.state["custom"])}

    router.routes.extend(management_router(ctx, officials, pack_sites, registrations, workspace).routes)
    router.routes.extend(workspace_router(workspace).routes)
    router.routes.extend((await assistant_router(ctx, workspace)).routes)
    ctx.use(PLUGIN_ROUTES).mount(ctx, router)
