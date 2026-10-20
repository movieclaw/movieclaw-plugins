# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 sexyfeifan
"""映屿：官方插件运行时中的影视发现与账号偏好页面。"""

from __future__ import annotations

import ipaddress
import re
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator

from movieclaw_sdk import plugin
from movieclaw_sdk.ops import HOST_OPS
from movieclaw_api.plugins.keys import PLUGIN_ROUTES, PLUGIN_DATA

from .routes import build_routers
from .sources import DiscoverySources
from .recommendations import RecommendationSources
from .preferences import PLATFORM_IDS, migrate_platform_settings
from .cache import PublicCacheStore
from .personal import PersonalRecommendations
from .personal_routes import build_personal_router
from .scheduler import FeedUpdater
from .account_auth import host_account_dependency
from .theater import TheaterService
from .artwork_cache import ArtworkCache
from .theater_routes import build_theater_router, build_collection_router


class Settings(BaseModel):
    model_config = {"title": "映屿设置"}

    proxy_mode: Literal["host", "direct", "custom"] = Field(
        "host", title="代理模式",
        description="host：跟随服务器已有代理；direct：直接连接；custom：使用下方独立代理地址，仅影响映屿。",
    )
    proxy_address: str = Field(
        "", title="代理地址",
        description="仅自定义代理时填写，例如 http://192.168.1.2:7890。地址明文显示，便于核对；请填写服务器可访问的地址，127.0.0.1 指服务器本机。",
    )
    proxy_url: SecretStr | None = Field(
        None, title="带认证的代理地址（可选）",
        description="代理需要账号密码时，用此安全字段填写完整链接，并将上方普通地址留空。支持 HTTP、HTTPS、SOCKS5、SOCKS5H；保存后不回显。跟随服务器代理或直连时无需填写。",
        json_schema_extra={"x-secret": True, "secret": True},
    )
    tmdb_token: SecretStr | None = Field(
        None,
        title="可选：TMDB API Read Access Token",
        description="填写 Bearer 读取令牌，不是 v3 API Key。用于海外片库与海报补全；留空仍可查看已接入国内榜单和 Netflix 官方周榜。",
        json_schema_extra={"x-secret": True, "secret": True},
    )
    fanart_api_key: SecretStr | None = Field(None, title="Fanart.tv API Key（可选）", description="用于影片海报选优。留空仍使用 TMDB；密钥只在服务器保存。", json_schema_extra={"x-secret": True, "secret": True})
    fanart_client_key: SecretStr | None = Field(None, title="Fanart.tv Personal Key（可选）", description="可选个人密钥，用于更及时的 Fanart 图片。", json_schema_extra={"x-secret": True, "secret": True})
    poster_cache_mb: int = Field(2048, ge=64, le=20480, title="录像厅海报缓存容量（MB）", description="仅管理插件自己的服务器缓存。容量不足优先清理未锁定旧图，锁定海报保留；不改变主程序图片缓存。")
    watch_region: str = Field(
        "US", min_length=2, max_length=2, pattern="^[A-Za-z]{2}$",
        title="海外可观看地区", description="两位地区代码，例如 US、GB、HK；平台片库随地区变化。",
    )
    enable_iqiyi: bool = Field(True, title="显示爱奇艺")
    enable_tencent: bool = Field(True, title="显示腾讯视频")
    enable_youku: bool = Field(True, title="显示优酷")
    enable_mango: bool = Field(True, title="显示芒果 TV", description="展示芒果官方频道推荐，保留推荐顺序，不作为播放量榜。")
    enable_bilibili: bool = Field(True, title="显示哔哩哔哩", description="展示官方近三日影视榜，检查统计日期，拒绝历史榜。")
    enable_netflix: bool = Field(True, title="显示 Netflix")
    enable_hbo: bool = Field(True, title="显示 HBO Max", description="地区片库需要 TMDB 读取令牌。")
    enable_disney: bool = Field(True, title="显示 Disney+", description="地区片库需要 TMDB 读取令牌。")
    cache_minutes: int = Field(60, ge=5, le=1440, title="内容缓存（分钟）", description="公开片单保存到插件私有缓存；过期先展示已有内容并后台刷新，上游异常时标记缓存。")
    auto_refresh: bool = Field(True, title="自动更新榜单", description="插件启用期间在后台检查各平台，Netflix 从官网最新一周读取。关闭后仍可在打开页面时刷新。")
    auto_refresh_minutes: int = Field(60, ge=15, le=1440, title="自动检查间隔（分钟）", description="默认每小时检查；Netflix 官网发布新榜后自动获取。源站未发布新周时保留当前官方统计周。")
    request_timeout_seconds: int = Field(12, ge=3, le=30, title="上游请求超时（秒）")

    @model_validator(mode="before")
    @classmethod
    def migrate_platforms(cls, value):
        return migrate_platform_settings(value)

    @field_validator("proxy_address")
    @classmethod
    def valid_plain_proxy(cls, value: str) -> str:
        value = value.strip()
        if not value:
            return ""
        try:
            parts = urlsplit(value)
            if parts.username is not None or parts.password is not None:
                raise ValueError("带账号密码的链接请填写‘带认证的代理地址’，不要放在明文字段")
            return cls.valid_proxy_url(SecretStr(value)).get_secret_value()
        except ValueError:
            raise ValueError("普通代理地址格式不正确；带账号密码的链接请使用安全字段") from None

    @field_validator("proxy_url")
    @classmethod
    def valid_proxy_url(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None or not value.get_secret_value().strip():
            return None
        url = value.get_secret_value().strip()
        try:
            parts = urlsplit(url)
            hostname, port = parts.hostname, parts.port
            if (parts.scheme not in {"http", "https", "socks5", "socks5h"}
                    or not hostname or parts.query or parts.fragment or parts.path not in {"", "/"}
                    or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in url)
                    or (port is not None and not 1 <= port <= 65535)):
                raise ValueError
            try:
                ipaddress.ip_address(hostname)
            except ValueError:
                domain = hostname.encode("idna").decode("ascii").rstrip(".")
                labels = domain.split(".")
                if len(domain) > 253 or any(not re.fullmatch(r"(?!-)[A-Za-z0-9-]{1,63}(?<!-)", label) for label in labels):
                    raise ValueError
        except (ValueError, UnicodeError):
            raise ValueError("代理地址格式不正确：请使用支持的协议、有效主机与端口，且不含路径、查询参数或锚点") from None
        return SecretStr(url)

    @model_validator(mode="after")
    def custom_proxy_required(self):
        if self.proxy_mode == "custom" and not self.proxy_address and self.proxy_url is None:
            raise ValueError("自定义代理模式需要填写代理地址")
        return self

    @field_validator("watch_region")
    @classmethod
    def uppercase_region(cls, value: str) -> str:
        return value.upper()

def theater_cache_root():
    from pathlib import Path
    from movieclaw_api.core.config import get_settings
    return Path(get_settings().image_cache_dir).parent / "platform-discovery-posters"


@plugin(
    "platform-discovery", title="映屿", config=Settings,
    inject=(HOST_OPS, PLUGIN_ROUTES, PLUGIN_DATA), permissions=("discover.get-title-details", "discover.filter-titles"),
    reloadable=True, disableable=True,
)
async def apply(ctx):
    namespace = "public-v2:" + ctx.config.watch_region + (":tmdb" if ctx.config.tmdb_token else ":public")
    private_store = ctx.use(PLUGIN_DATA).scoped(ctx)
    storage = PublicCacheStore(private_store, namespace)
    sources = DiscoverySources(ctx.config, entry_id=ctx.entry_id, storage=storage)
    await sources.initialize()

    async def get_ops():
        return await ctx.use(HOST_OPS).client(ctx)

    recommendations = RecommendationSources(sources, get_ops, storage=storage)
    personal = PersonalRecommendations(sources, get_ops, private_store=private_store, storage=storage)
    updater = FeedUpdater(sources, enabled=ctx.config.auto_refresh, interval_minutes=ctx.config.auto_refresh_minutes)
    pages, details = build_routers(ctx.entry_id, ctx.config, sources, get_ops, recommendations, updater)
    # Keep a flat list for the process SDK's route/schema serializer as well as
    # older FastAPI versions shipped by the host.
    # 进程内官方登录依赖在挂载时注入，不借用插件默认管理员身份。
    pages.routes.extend(build_personal_router(ctx.entry_id, personal, get_ops, account_dependency=host_account_dependency()).routes)
    artwork_cache = ArtworkCache(theater_cache_root(), max_bytes=getattr(ctx.config, "poster_cache_mb", 2048) * 1024**2)
    theater = TheaterService(private_store, sources, get_ops, artwork_cache, ctx.config)
    pages.routes.extend(build_theater_router(ctx.entry_id, theater, host_account_dependency()).routes)
    pages.routes.extend(build_collection_router(ctx.entry_id, host_account_dependency()).routes)
    ctx.effect(theater.close, label="close-theater-artwork")
    routes = ctx.use(PLUGIN_ROUTES)
    # 公开影视数据供登录成员浏览；宿主详情代理限管理员，避免跨成员的库存投影。
    routes.mount(ctx, pages, zone="member")
    routes.mount(ctx, details, zone="admin")
    ctx.effect(sources.close, label="close-discovery-sources")
    ctx.effect(recommendations.close, label="close-recommendation-sources")
    ctx.effect(personal.close, label="close-personal-recommendations")
    ctx.effect(updater.close, label="close-automatic-feed-updater")
    updater.start(ctx)
