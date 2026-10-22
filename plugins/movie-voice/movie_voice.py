"""通知由宿主投递与加密；只核实库存，不接触密码或推送设备密钥。"""
import asyncio
import time

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from movieclaw_sdk import plugin
from movieclaw_sdk.ops import HOST_OPS
from movieclaw_sdk.agent import AGENT
from movieclaw_api.plugins.keys import PLUGIN_ROUTES, PLUGIN_DATA
from movieclaw_sdk.events import PLUGIN_EVENTS, MediaRef, plugin_event


class FoundMovie(BaseModel):
    """使用真实库存的 MediaRef，让宿主生成账号有权访问的影片打开路径。"""
    model_config = ConfigDict(frozen=True)
    media: MediaRef


class NotifyMovie(BaseModel):
    model_config = ConfigDict(extra="forbid")
    library_id: int = Field(gt=0)
    media_item_id: int = Field(gt=0)


FOUND = plugin_event("movie-voice.movie.found", FoundMovie, title="找到库内影片")


def movie_snapshot(result, requested_id):
    """只相信宿主返回的影片身份和在位库存，不接受前端自报片名。"""
    if isinstance(result.get("data"), dict):
        result = result["data"]
    item = result.get("item", result)
    if (item.get("media_item_id") != requested_id or item.get("kind") not in ("movie", "tv")
            or not item.get("title") or result.get("file_count", 0) < 1):
        raise ValueError("影片未入库或已不可用，请刷新搜索结果")
    return MediaRef(id=requested_id, kind=item["kind"], title=item["title"],
                    year=item.get("year"), tmdb_id=item.get("tmdb_id"))


OPERATIONS = ("library.items.get", "search.library", "search.titles", "discover.get-title-details", "subscriptions.create")


@plugin("movie-voice", title="寻映", inject=(AGENT, HOST_OPS, PLUGIN_ROUTES, PLUGIN_EVENTS, PLUGIN_DATA),
        permissions=("agent:none", *OPERATIONS), reloadable=True, disableable=True)
async def apply(ctx):
    publisher = await ctx.use(PLUGIN_EVENTS).register(ctx, FOUND)
    router = APIRouter()
    gate = asyncio.Lock()
    recent = {}

    @router.post("/notify", operation_id="plugins.movie-voice.notify",
                 summary="将已在库影片的观看入口发送给管理员自己的推送设备")
    async def notify(payload: NotifyMovie, request: Request):
        # JSON 写请求与浏览器来源检查防止第三方网页通过表单触发通知。
        if (request.headers.get("sec-fetch-site") == "cross-site"
                or not request.headers.get("content-type", "").startswith("application/json")):
            raise HTTPException(403, "请从寻映页面发送通知")
        async with gate:
            now = time.monotonic()
            key = (payload.library_id, payload.media_item_id)
            if recent.get(key, 0) > now - 30:
                return {"queued": False, "message": "刚刚已发送，请查看手机通知"}
            ops = await ctx.use(HOST_OPS).client(ctx)
            try:
                result = await ops.call("library.items.get", payload.model_dump())
                media = movie_snapshot(result, payload.media_item_id)
            except Exception:
                raise HTTPException(503, "无法确认这部影片仍在库中，请刷新后重试") from None
            await publisher.publish(FOUND, FoundMovie(media=media))
            if len(recent) >= 128:
                recent.clear()
            recent[key] = now
        return {"queued": True, "message": "已交给 MovieClaw 通知系统；请点击手机通知进入影片。实际送达受设备权限与通知偏好影响。"}

    # 本版本仅服务管理员本人；不允许普通成员借用插件身份发通知给管理员。
    ctx.use(PLUGIN_ROUTES).mount(ctx, router, zone="admin")
    from shortcut_backend import mount_shortcut
    await mount_shortcut(ctx, publisher, FOUND, FoundMovie, movie_snapshot)
