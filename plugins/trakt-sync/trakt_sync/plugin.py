"""仅使用 MovieClaw 开放插件服务；管理接口沿用宿主管理员认证。"""

import uuid

import httpx
from fastapi import APIRouter, HTTPException, Query
from movieclaw_api.plugins.keys import PLUGIN_DATA, PLUGIN_HEALTH, PLUGIN_ROUTES
from movieclaw_kernel import DURABLE_EVENTS
from movieclaw_sdk import Context, net, plugin
from movieclaw_sdk.events import (
    LIBRARY_ITEM_FAVORITED,
    PLAYBACK_COMPLETED,
    PLAYBACK_MARKED_PLAYED,
    PLAYBACK_MARKED_UNPLAYED,
    PLAYBACK_PROGRESS,
    PLAYBACK_STARTED,
    PLAYBACK_STOPPED,
)
from movieclaw_sdk.ops import HOST_OPS
from pydantic import BaseModel, Field, SecretStr

from .actions import Actions
from .application import application_credentials
from .client import TraktClient, TraktError
from .engine import Engine
from .policy import iso_time
from .preferences import Preferences, preferences
from .recovery import MetadataResolver
from .transfers import Transfers

PLUGIN_ID = "trakt-sync"


class RetryOptions(BaseModel):
    season: int = Field(ge=0)
    episode: int = Field(gt=0)


class Settings(BaseModel):
    enabled: bool = Field(True, title="启用自动追踪", description="只同步已明确绑定的成员")
    client_id: str = Field(
        "",
        title="自定义 Trakt Client ID（高级，可留空）",
        max_length=256,
        description="默认使用 MovieClaw 维护的公开应用，无需填写；自行部署者可用自己的应用覆盖",
    )
    client_secret: SecretStr = Field(
        SecretStr(""),
        title="自定义 Trakt Client Secret（高级，可留空）",
        description="仅配合自定义应用使用，由宿主加密保存；不得填入他人的 Secret",
    )


@plugin(
    PLUGIN_ID,
    title="Trakt 自动追踪",
    config=Settings,
    inject=(PLUGIN_DATA, PLUGIN_HEALTH, PLUGIN_ROUTES, DURABLE_EVENTS, HOST_OPS),
    permissions=(
        "library.list",
        "library.items.get",
        "playback.sync.catalog",
        "playback.sync.import",
    ),
)
async def apply(ctx: Context):
    settings = ctx.config
    store = ctx.use(PLUGIN_DATA).scoped(ctx)
    health = ctx.use(PLUGIN_HEALTH).reporter(ctx)
    http = httpx.AsyncClient(
        transport=net.http_transport(PLUGIN_ID), timeout=10, follow_redirects=False
    )
    ctx.effect(http.aclose)
    client_id, client_secret = application_credentials(
        settings.client_id, settings.client_secret.get_secret_value()
    )
    client = TraktClient(client_id, client_secret, http)
    ops = await ctx.use(HOST_OPS).client(ctx)
    engine = Engine(
        store, client, health, ctx.logger, enabled=settings.enabled, resolver=MetadataResolver(ops)
    )

    routes = ctx.use(PLUGIN_ROUTES)
    transfers = Transfers(engine, ops)
    actions = Actions(engine)
    ctx.task(actions.run(), name="trakt-actions")

    def manual_listener(kind):
        async def receive(event):
            delivery = ctx.delivery
            if delivery and delivery.origin.is_plugin(PLUGIN_ID):
                return
            await actions.automatic(
                kind,
                event.model_dump(mode="json"),
                delivery.event_id if delivery else uuid.uuid4().hex,
                delivery.occurred_at if delivery else iso_time(engine.clock()),
            )

        return receive

    for event, kind in (
        (PLAYBACK_MARKED_PLAYED, "marked_played"),
        (PLAYBACK_MARKED_UNPLAYED, "marked_unplayed"),
        (LIBRARY_ITEM_FAVORITED, "favorited"),
    ):
        ctx.on(event, manual_listener(kind), id=f"sync-{kind}", durable=True)

    def listener(kind):
        async def receive(event):
            delivery = ctx.delivery
            await engine.accept(
                kind,
                event.model_dump(mode="json"),
                delivery.event_id if delivery else uuid.uuid4().hex,
                delivery.occurred_at if delivery else iso_time(engine.clock()),
            )

        return receive

    for event, kind in (
        (PLAYBACK_STARTED, "started"),
        (PLAYBACK_STOPPED, "stopped"),
        (PLAYBACK_COMPLETED, "completed"),
    ):
        ctx.on(event, listener(kind), id=f"track-{kind}", durable=True)
    ctx.on(PLAYBACK_PROGRESS, listener("progress"))
    ctx.task(engine.run(), name="trakt-delivery")
    router = APIRouter()

    @router.get("/status", operation_id="plugins.trakt-sync.status", summary="Trakt 同步状态")
    async def status(member_id: int = Query(0, ge=0)) -> dict:
        return await engine.status(member_id)

    @router.get(
        "/accounts",
        operation_id="plugins.trakt-sync.accounts",
        summary="查看 Trakt 账号总览",
    )
    async def accounts() -> dict:
        return await engine.accounts()

    @router.get(
        "/activity",
        operation_id="plugins.trakt-sync.activity",
        summary="查看成员发送记录",
    )
    async def activity(member_id: int = Query(0, ge=0)) -> dict:
        return await engine.activity(member_id)

    @router.post(
        "/acknowledge-dropped",
        operation_id="plugins.trakt-sync.acknowledge-dropped",
        summary="确认已知的未同步记录提示（不会补发记录）",
    )
    async def acknowledge_dropped(member_id: int = Query(0, ge=0)) -> dict:
        return await engine.acknowledge_dropped(member_id)

    @router.post(
        "/auth/begin",
        operation_id="plugins.trakt-sync.auth.begin",
        summary="为指定成员开始 Trakt 设备授权",
    )
    async def begin(member_id: int = Query(0, ge=0)) -> dict:
        try:
            return await engine.begin(member_id)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        except TraktError as exc:
            raise HTTPException(502, str(exc)) from None

    @router.post(
        "/auth/poll",
        operation_id="plugins.trakt-sync.auth.poll",
        summary="查询 Trakt 设备授权结果",
    )
    async def poll(member_id: int = Query(0, ge=0)) -> dict:
        try:
            return await engine.poll(member_id)
        except TraktError as exc:
            raise HTTPException(502, str(exc)) from None

    @router.post(
        "/disconnect",
        operation_id="plugins.trakt-sync.disconnect",
        summary="断开 Trakt（只清除此成员的连接和待同步队列）",
        openapi_extra={"x-cli-dangerous": "confirm"},
    )
    async def disconnect(member_id: int = Query(0, ge=0)) -> dict:
        return await engine.disconnect(member_id)

    @router.post("/retry", operation_id="plugins.trakt-sync.retry", summary="重试匹配失败的记录")
    async def retry(member_id: int = Query(0, ge=0)) -> dict:
        try:
            result = await engine.retry_failed(member_id)
            for task in (await store.scopes("task", entity="transfer")).values():
                if (
                    task["member_id"] == member_id
                    and task["direction"] == "auto"
                    and task["state"] in {"complete_with_errors", "paused"}
                ):
                    result["retried"] += max(1, task["failed"])
                    await actions.control(member_id, task["id"], "resume")
            return result
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    @router.post(
        "/retry/{delivery_id}",
        operation_id="plugins.trakt-sync.retry-one",
        summary="重试指定的失败记录",
    )
    async def retry_one(
        delivery_id: str, member_id: int = Query(0, ge=0), correction: RetryOptions | None = None
    ) -> dict:
        try:
            return await engine.retry_failed(
                member_id, delivery_id, correction.model_dump() if correction else None
            )
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    @router.post(
        "/dismiss/{delivery_id}",
        operation_id="plugins.trakt-sync.dismiss",
        summary="忽略此条失败记录，不删除 Trakt 历史",
    )
    async def dismiss(delivery_id: str, member_id: int = Query(0, ge=0)) -> dict:
        try:
            return await engine.dismiss_failed(member_id, delivery_id)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    @router.get("/viewer", operation_id="plugins.trakt-sync.viewer", summary="当前成员身份")
    async def viewer():
        return {"member_id": 0, "is_admin": True, "management": "admin_only"}

    @router.get("/profile", operation_id="plugins.trakt-sync.profile", summary="成员同步偏好")
    async def profile(member_id: int = Query(0, ge=0)):
        member = member_id
        return {
            "member_id": member,
            "preferences": (await preferences(engine, member)).model_dump(),
        }

    @router.put(
        "/profile", operation_id="plugins.trakt-sync.profile.set", summary="保存成员同步偏好"
    )
    async def save_profile(payload: Preferences, member_id: int = Query(0, ge=0)):
        member = member_id
        async with engine.locks[member]:
            await engine.set(member, "profile", payload.model_dump())
        return {"member_id": member, "preferences": payload.model_dump()}

    @router.post(
        "/actions/{task_id}/{command}",
        operation_id="plugins.trakt-sync.actions.control",
        summary="取消或重试手动操作同步",
    )
    async def control(task_id: str, command: str, member_id: int = Query(0, ge=0)):
        try:
            return await actions.control(member_id, task_id, command)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    class Scan(BaseModel):
        direction: str

    class Confirmation(BaseModel):
        token: str

    @router.get("/history", operation_id="plugins.trakt-sync.history", summary="历史同步批次")
    async def history(member_id: int = Query(0, ge=0), offset: int = Query(0, ge=0)):
        return await transfers.listing(member_id, offset)

    @router.post(
        "/history",
        operation_id="plugins.trakt-sync.history.scan",
        summary="创建扫描，不写入观看历史",
    )
    async def scan(payload: Scan, member_id: int = Query(0, ge=0)):
        try:
            return await transfers.create(member_id, payload.direction)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    @router.get(
        "/history/{task_id}",
        operation_id="plugins.trakt-sync.history.details",
        summary="查看预览或同步明细",
    )
    async def history_detail(
        task_id: str,
        member_id: int = Query(0, ge=0),
        page: int = Query(0, ge=0),
        records: bool = False,
    ):
        try:
            return await transfers.details(member_id, task_id, page, records)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from None

    @router.post(
        "/history/{task_id}/confirm",
        operation_id="plugins.trakt-sync.history.confirm",
        summary="确认当前全量预览",
        openapi_extra={"x-cli-dangerous": "confirm"},
    )
    async def confirm_history(task_id: str, payload: Confirmation, member_id: int = Query(0, ge=0)):
        try:
            return await transfers.confirm(member_id, task_id, payload.token)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    @router.post(
        "/history/{task_id}/{command}",
        operation_id="plugins.trakt-sync.history.step",
        summary="网页推进一批、暂停、继续或取消",
    )
    async def history_step(task_id: str, command: str, member_id: int = Query(0, ge=0)):
        try:
            if command == "tick":
                return await transfers.step(task_id, member_id)
            return await transfers.control(member_id, task_id, command)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    routes.mount(ctx, router, zone="admin")
    ctx.logger.info(
        "Trakt 插件已加载；使用现有事件，管理员按成员管理独立同步偏好；历史同步由网页扫描、确认后逐批执行"
    )
