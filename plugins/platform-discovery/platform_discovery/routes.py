"""入口、静态资源与只读数据路由；鉴权由宿主 PLUGIN_ROUTES 注入。"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Query
from fastapi.routing import APIRoute
from movieclaw_sdk.ops import OpsError
from starlette.responses import HTMLResponse, RedirectResponse, Response

ASSETS = Path(__file__).parent / "assets"
ASSET_TYPES = {"yingyu-icon.svg": "image/svg+xml", "ambient-settings.js": "application/javascript", "ambient.js": "application/javascript", "workspace.js": "application/javascript", "workspace.css": "text/css", "theater.js": "application/javascript", "wall-tools.js": "application/javascript", "page.css": "text/css", "page.js": "application/javascript", "personal.js": "application/javascript", "posters.js": "application/javascript", "subscription.js": "application/javascript", "transport.js": "application/javascript", "navigation.js": "application/javascript", "tmdb-logo.svg": "image/svg+xml"}
PROXY_LABELS = {"host": "跟随服务器代理", "direct": "直接连接", "custom": "独立代理"}
TITLE_REF = re.compile(r"^(?:tmdb:(?:movie|tv):[1-9]\d*|douban:[1-9]\d*)$")
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' https: data:; connect-src 'self'; font-src 'self'; "
        "object-src 'none'; base-uri 'self'; frame-ancestors 'self'"
    ),
    "Cache-Control": "no-store",
}


class LoginNavigationRoute(APIRoute):
    """Redirect only failed HTML page navigation; API authentication stays 401."""
    def get_route_handler(self):
        original = super().get_route_handler()
        async def handle(request):
            try:
                return await original(request)
            except Exception as exc:
                prefix = "/api/v1/plugins/platform-discovery"
                if (getattr(exc, "status_code", None) != 401 or request.method != "GET"
                        or request.url.path.rstrip("/") not in {prefix, prefix + "/panel"}
                        or "text/html" not in request.headers.get("accept", "")):
                    raise
                view = request.query_params.get("view")
                target = prefix + "/panel"
                if view in {"personal", "random", "platforms", "theater"}:
                    query = {"view": view}
                    if view == "personal" and request.query_params.get("screen") == "1":
                        query["screen"] = "1"
                    target += "?" + urlencode(query)
                return RedirectResponse("/login?" + urlencode({"next": target}), status_code=303,
                                        headers={"Cache-Control": "no-store"})
        return handle


def build_routers(entry_id, config, sources, get_ops, recommendations=None, updater=None):
    pages, details = APIRouter(route_class=LoginNavigationRoute), APIRouter()
    prefix = f"/api/v1/plugins/{entry_id}"
    operation = f"plugins.{entry_id}."
    hidden = {"x-cli-hidden": True}

    def read_asset(name):
        path = ASSETS / name
        if not path.is_file():
            raise HTTPException(503, "插件页面资源尚未准备好")
        return path.read_text(encoding="utf-8")

    def static_asset(name):
        if name not in ASSET_TYPES:
            raise HTTPException(404, "资源不存在")
        return Response(read_asset(name), media_type=ASSET_TYPES[name], headers=SECURITY_HEADERS)

    @pages.get("", operation_id=operation + "entry", summary="进入映屿", openapi_extra=hidden)
    async def entry():
        return RedirectResponse(prefix + "/panel", status_code=307, headers={"Cache-Control": "no-store"})

    @pages.get("/", operation_id=operation + "entry-slash", openapi_extra=hidden)
    async def entry_slash():
        return RedirectResponse(prefix + "/panel", status_code=307, headers={"Cache-Control": "no-store"})

    @pages.get("/panel", operation_id=operation + "panel", summary="打开映屿发现页", openapi_extra=hidden)
    async def panel():
        return HTMLResponse(read_asset("page.html"), headers=SECURITY_HEADERS)

    @pages.get("/page.css", operation_id=operation + "style", openapi_extra=hidden)
    async def style():
        return static_asset("page.css")

    @pages.get("/page.js", operation_id=operation + "script", openapi_extra=hidden)
    async def script():
        return static_asset("page.js")

    @pages.get("/{name}.js", operation_id=operation + "root-script", openapi_extra=hidden)
    async def root_script(name: str):
        return static_asset(name + ".js")

    @pages.get("/assets/{name}", operation_id=operation + "asset", openapi_extra=hidden)
    async def asset(name: str):
        # 不接受任意文件路径，插件包里的 Python 与配置文件不会作为静态资源泄露。
        return static_asset(name)

    @pages.get("/catalog", operation_id=operation + "catalog", summary="读取平台目录与插件入口")
    async def catalog():
        result = await sources.catalog()
        return {**result, "entry_url": prefix + "/panel"}

    @pages.get("/status", operation_id=operation + "status", summary="读取发现页入口与配置状态")
    async def status():
        result = await sources.catalog()
        return {
            "state": "ready", "title": "映屿", "entry_url": prefix + "/panel",
            "region": config.watch_region, "platform_count": len(result["platforms"]),
            "tmdb_configured": bool(config.tmdb_token and config.tmdb_token.get_secret_value().strip()),
            "catalog_url": prefix + "/catalog", "feed_url": prefix + "/feeds/{platform}",
            "theme_preferences_url": "/api/v1/ui/preferences", "image_proxy_url": "/api/v1/images/proxy",
            "host_detail_links": {"tmdb": "/media/{media_type}/{id}", "douban": "/media/douban/{id}"},
            "detail_proxy_requires_admin": True,
            "proxy": {"mode": config.proxy_mode, "label": PROXY_LABELS[config.proxy_mode],
                      "custom_configured": bool(config.proxy_address or config.proxy_url)},
            "connection_test_url": prefix + "/connection-test",
            "recommendation_url": prefix + "/panel?view=random",
            "recommendation_categories_url": prefix + "/recommendation-categories",
            "personal_wall_url": prefix + "/panel?view=personal",
            "automatic_update": updater.status() if updater is not None else {"enabled": False},
            "cache": sources.cache_status() if hasattr(sources, "cache_status") else {"persistent": False},
            "message": "开启后通过入口打开独立发现页；榜单来源、地区与更新时间在每个平台内展示。",
        }

    @pages.get("/connection-test", operation_id=operation + "connection-test", summary="测试当前代理模式的榜源连接")
    async def connection_test():
        result = await sources.connection_test()
        # 只交出连通结果；代理地址、认证信息和上游响应不进入成员页面。
        return {"ok": bool(result["ok"]), "mode": config.proxy_mode,
                "message": result["message"], "latency_ms": result.get("latency_ms")}

    @pages.get("/recommendation-categories", operation_id=operation + "recommendation-categories", summary="读取随心看剧的地区分类")
    async def recommendation_categories():
        if recommendations is None:
            raise HTTPException(503, "推荐片库尚未准备好")
        return await recommendations.categories()

    @pages.get("/recommendations/{category}", operation_id=operation + "recommendations", summary="随机推荐某个地区的新老剧集")
    async def recommend(
        category: str,
        nonce: str = Query("0", min_length=1, max_length=64, pattern="^[A-Za-z0-9_-]+$"),
        session_id: str = Query("default", min_length=1, max_length=64, pattern="^[A-Za-z0-9_-]+$"),
    ):
        if recommendations is None:
            raise HTTPException(503, "推荐片库尚未准备好")
        available = await recommendations.categories()
        if category not in {item["id"] for item in available["categories"]}:
            raise HTTPException(404, "推荐分类不存在")
        return await recommendations.recommend(category, nonce=nonce, limit=12, session_id=session_id)

    @pages.get("/feeds/{platform}", operation_id=operation + "feed", summary="浏览某个平台的影视榜单")
    async def feed(
        platform: str,
        kind: str = Query("all", pattern="^(all|tv|variety|movie)$"),
        page: int = Query(1, ge=1, le=100),
    ):
        available = await sources.catalog()
        if platform not in {item["id"] for item in available["platforms"]}:
            raise HTTPException(404, "平台不存在或已在插件设置中关闭")
        return await sources.feed(platform, kind=kind, page=page)

    @details.get("/titles/{title_ref}", operation_id=operation + "title-details", summary="读取已匹配影视的宿主资料")
    async def title_details(title_ref: str):
        if not TITLE_REF.fullmatch(title_ref):
            raise HTTPException(400, "影视引用格式不正确")
        try:
            ops = await get_ops()
            return await ops.call("discover.get-title-details", {"title_ref": title_ref})
        except OpsError as exc:
            # 不把宿主错误的上游响应、诊断明细或令牌带回页面。
            raise HTTPException(exc.status, "MovieClaw 暂时无法读取这部作品的详情") from None

    return pages, details
