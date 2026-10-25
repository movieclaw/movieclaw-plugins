"""MovieClaw 原生入口：宿主负责登录、路由、网页及生命周期，无额外监听端口。

使用 inline 是为了直接消费宿主已验证的 Principal；不读取登录 Cookie/令牌，
不导入宿主数据库。共享数据通过清单批准的只读 HOST_OPS 取得；本人后台写入另需正式设备配对。
"""

import json
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from movieclaw_api.plugins.keys import PLUGIN_FILES, PLUGIN_ROUTES
from movieclaw_sdk import plugin
from movieclaw_sdk.ops import HOST_OPS
from pydantic import BaseModel, Field, ValidationError, field_validator

from .app import create_app
from .config import Settings as AppSettings
from .config import origin_url
from .external_watch import ExternalWatchClient, timestamp
from .native_source import HOST_PERMISSIONS, NativeSource, account_key
from .peers import PeerError
from .upstream import UpstreamError
from .watch_archive import now


class Settings(BaseModel):
    """仅提供插件自身选项；公开地址由宿主签名服务读取宿主既有配置。"""

    model_config = {"title": "映迹设置"}
    demo_enabled: bool = Field(
        default=True,
        title="允许演示模式",
        description="允许各账号在映迹页面自行开关演示；关闭后所有账号均隐藏演示内容。",
    )
    allow_private_peers: bool = Field(
        default=False,
        title="允许连接局域网服务器",
        description=(
            "用于好友服务器及本人 Emby/Plex：默认仅连接公网 HTTPS；"
            "信任的私网部署可开启 HTTP 与局域网连接。"
        ),
    )

    native_api_url: str = Field(
        default="",
        title="MovieClaw 本机接口地址（可选）",
        description=(
            "仅供本人配对和后台同步访问当前 MovieClaw。留空使用主程序外部访问地址；"
            "公网回环不可用时，可填写服务器自身可达的内网 HTTP(S) 源地址，不含 /api/v1。"
            "不要填写其他服务器；修改后须重新配对本人账号。"
        ),
    )

    @field_validator("native_api_url")
    @classmethod
    def validate_native_api_url(cls, value):
        # 由安装管理员设置固定目标，不从浏览器 Host/Origin 接受凭据发送地址。
        return origin_url(value) if value.strip() else ""


class NativeRoute(APIRoute):
    """在正式插件路由内统一处理中文错误、请求大小与浏览器写操作边界。"""

    def get_route_handler(self):
        original = super().get_route_handler()

        async def handle(request: Request):
            try:
                if request.method in {"POST", "PATCH", "PUT", "DELETE"}:
                    if "/federation/" not in request.url.path:
                        base = urlsplit(str(request.base_url))
                        allowed = {origin_url(f"{base.scheme}://{base.netloc}")}
                        public = getattr(request.app.state, "friends_public_origin", "")
                        if public:
                            allowed.add(public)
                        origin = request.headers.get("origin")
                        if request.headers.get("x-friends-request") != "1" or (
                            origin and origin not in allowed
                        ):
                            raise HTTPException(403, "请从 MovieClaw 映迹页面提交操作")
                    body = bytearray()
                    async for chunk in request.stream():
                        body.extend(chunk)
                        if len(body) > 1_000_000:
                            raise HTTPException(413, "请求内容过大")
                    request._body = bytes(body)
                response = await original(request)
            except UpstreamError as exc:
                response = JSONResponse({"detail": exc.message}, status_code=exc.status_code)
            except PeerError as exc:
                response = JSONResponse({"detail": str(exc)}, status_code=502)
            except ValidationError:
                response = JSONResponse(
                    {"detail": "数据格式不兼容或超出插件容量限制"}, status_code=422
                )
            except (json.JSONDecodeError, UnicodeDecodeError):
                response = JSONResponse({"detail": "请求内容不是有效的 JSON"}, status_code=422)
            response.headers["Cache-Control"] = "no-store"
            response.headers["Vary"] = "Cookie, Authorization"
            response.headers["Referrer-Policy"] = "no-referrer"
            response.headers["X-Content-Type-Options"] = "nosniff"
            return response

        return handle


def build_routers(app, require_login):
    """复用经过隐私测试的业务端点，移除独立登录与静态服务，挂入宿主正式三区。"""
    store = app.state.store

    async def account(request: Request, principal=Depends(require_login)):
        identity = account_key(principal)
        user = store.one("SELECT * FROM users WHERE username=?", (identity,))
        if user is None:
            nickname = (
                getattr(principal.member, "nickname", None)
                if getattr(principal, "member", None)
                else None
            ) or principal.name
            store.execute(
                "INSERT INTO users(id,username,nickname,member_id,role,token,invite_token) "
                "VALUES(?,?,?,?,?,?,?)",
                (
                    str(uuid.uuid4()),
                    identity,
                    nickname[:60],
                    principal.member_id or 0,
                    principal.kind,
                    store.encrypt(identity),
                    store.secret(),
                ),
            )
            user = store.one("SELECT * FROM users WHERE username=?", (identity,))
        store.plugin_joined_at(user["id"])
        # 此状态只由本依赖设置；页面不能通过JSON、header或成员ID指定另一账号。
        request.state.friends_user = user
        request.state.friends_login_name = principal.name
        member = getattr(principal, "member", None)
        name = (getattr(member, "nickname", None) or principal.name or user["nickname"])[:60]
        joined = getattr(member, "created_at", None) if principal.kind == "member" else None
        joined = timestamp(joined.isoformat() if hasattr(joined, "isoformat") else joined)
        store.execute(
            "INSERT INTO account_profiles(user_id,server_name,joined_at,updated_at)"
            " VALUES(?,?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET server_name=excluded.server_name,jo"
            "ined_at=excluded.joined_at,updated_at=excluded.updated_at",
            (user["id"], name, joined, now()),
        )

    member = APIRouter(route_class=NativeRoute)
    public = APIRouter(route_class=NativeRoute)
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        if route.path in {"/api/login", "/api/logout", "/"}:
            continue
        federation = route.path.startswith("/federation/")
        path = route.path.removeprefix("/api") if not federation else route.path
        name = route.name.replace("_", "-")
        (public if federation else member).add_api_route(
            path,
            route.endpoint,
            methods=route.methods,
            operation_id=f"plugins.watch-friends.{name}",
            dependencies=[] if federation else [Depends(account)],
            response_model=None,
        )
    return member, public


@plugin(
    "watch-friends",
    title="映迹",
    config=Settings,
    inject=(HOST_OPS, PLUGIN_ROUTES, PLUGIN_FILES),
    permissions=HOST_PERMISSIONS,
    reloadable=True,
    disableable=True,
)
async def apply(ctx):
    """登记一次原生接口；卸载自动摘除路由并关闭插件私有 SQLite 连接。"""
    from movieclaw_api.api.deps import require_login
    from movieclaw_sdk.net import http_transport

    files = ctx.use(PLUGIN_FILES).scoped(ctx)
    directory = await files.check(files.path("plugin"), write=True)
    routes = ctx.use(PLUGIN_ROUTES)
    endpoints = {}
    public_origin = "http://movieclaw.invalid"
    try:
        for path in (
            "/federation/requests",
            "/federation/state",
            "/federation/data",
            "/federation/social",
            "/federation/directory",
        ):
            endpoints[path] = await routes.sign(ctx, path, absolute=True)
        parsed = urlsplit(endpoints["/federation/requests"])
        public_origin = origin_url(f"{parsed.scheme}://{parsed.netloc}")
    except ValueError:
        endpoints.clear()
        ctx.logger.info("未配置宿主外部访问地址：同服好友可用，跨服连接请先在应用设置中配置。")
    settings = AppSettings(
        ctx.config.native_api_url,
        public_origin,
        Path(directory),
        allow_private_peers=ctx.config.allow_private_peers,
        demo_enabled=ctx.config.demo_enabled,
        native=True,
        network_endpoint=endpoints.pop("/federation/directory", ""),
        federation_endpoints=endpoints,
    )
    source = NativeSource(await ctx.use(HOST_OPS).client(ctx))
    app = create_app(
        settings,
        upstream=source,
        peer_transport_factory=lambda: http_transport("watch-friends"),
        task_factory=lambda coro: ctx.task(coro, name="观影档案读取"),
        external_watch=ExternalWatchClient(
            allow_private=settings.allow_private_peers,
            transport_factory=lambda: http_transport("watch-friends"),
        ),
    )
    ctx.effect(app.state.store.close, label="关闭映迹私有存储")
    ctx.effect(app.state.watch_archive.close, label="停止观影档案读取")
    ctx.effect(app.state.auto_sync.close, label="停止后台已看同步")
    app.state.auto_sync.logger = ctx.logger
    app.state.auto_sync.start()
    ctx.effect(app.state.resume_sync.close, label="停止本人续播同步")
    app.state.resume_sync.logger = ctx.logger
    app.state.resume_sync.start()
    ctx.effect(app.state.network.close, label="停止联网广场刷新")
    app.state.network.logger = ctx.logger
    app.state.network.start()
    member, public = build_routers(app, require_login)
    routes.mount(ctx, member, zone="member")
    routes.mount(ctx, public, zone="public")
    ctx.logger.info("映迹 v0.1.1 已加载：原生 Web、同服好友与签名跨服接口。")
