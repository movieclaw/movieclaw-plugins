"""演职人员中文刮削插件入口。

只做一件事：用豆瓣补齐条目详情页演职员条的中文名/角色/头像。
实现方式是写条目 NFO，再让主程序重新吸收；不改主程序、不改人物表。
"""

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from movieclaw_api.plugins.keys import (
    PLUGIN_DATA,
    PLUGIN_FILES,
    PLUGIN_HEALTH,
    PLUGIN_ROUTES,
)
from movieclaw_kernel import DURABLE_EVENTS
from movieclaw_scheduler import SCHEDULED_TASKS, TaskDefinition, TriggerType
from movieclaw_sdk import Context, plugin
from movieclaw_sdk.events import LIBRARY_ITEMS_ARRIVED
from movieclaw_sdk.jobs import (
    JOB_HANDLERS,
    PLUGIN_JOBS,
    JobContext,
    RegisteredJobHandler,
)
from movieclaw_sdk.ops import HOST_OPS, OpsError

from .douban import DoubanClient
from .runtime import ITEM_JOB, SWEEP_JOB, SWEEP_TASK, Runtime, SkipItem
from .settings import Settings

PLUGIN_ID = "cast-cn"
OPERATIONS = (
    "app.show",
    "library.list",
    "library.items.list",
    "library.items.get",
    "library.items.refresh-metadata",
    "jobs.wait",
    "people.show",
    "search.titles",
)

_RUNTIME: Runtime | None = None


class RunRequest(BaseModel):
    library_id: int | None = None
    media_item_id: int | None = None
    dry_run: bool | None = None


class RestoreRequest(BaseModel):
    media_item_id: int


@plugin(
    PLUGIN_ID,
    title="演职人员中文刮削",
    inject=(
        DURABLE_EVENTS,
        HOST_OPS,
        PLUGIN_DATA,
        PLUGIN_FILES,
        PLUGIN_ROUTES,
        PLUGIN_JOBS,
        PLUGIN_HEALTH,
    ),
    requires={
        "library.items.arrived": "^1.0",
        "scheduled-tasks": "^1.0",
        "job-handlers": "^1.1",
    },
    permissions=OPERATIONS,
    config=Settings,
)
async def apply(ctx: Context) -> None:
    global _RUNTIME

    settings: Settings = ctx.config or Settings()

    ops = await ctx.use(HOST_OPS).client(ctx)
    store = ctx.use(PLUGIN_DATA).scoped(ctx)
    files = ctx.use(PLUGIN_FILES).scoped(ctx)
    health = ctx.use(PLUGIN_HEALTH).reporter(ctx)
    jobs = ctx.use(PLUGIN_JOBS).scoped(ctx)
    routes = ctx.use(PLUGIN_ROUTES)

    base_url = (settings.public_base_url or "").strip()
    if not base_url:
        try:
            app_config = await ops.call("app.show", {})
            if isinstance(app_config, dict):
                base_url = str(app_config.get("external_url") or "").strip()
        except OpsError as exc:
            ctx.logger.warning("cast-cn：读取外部访问地址失败：%s", exc.message)
        except Exception as exc:  # noqa: BLE001 - 地址拿不到不影响主流程
            ctx.logger.warning("cast-cn：读取外部访问地址失败：%s", exc)

    douban = DoubanClient(settings.douban_api_base_url, logger=ctx.logger)
    ctx.effect(douban.aclose, label="close-douban")

    runtime = Runtime(
        ctx=ctx,
        settings=settings,
        ops=ops,
        store=store,
        files=files,
        health=health,
        jobs=jobs,
        douban=douban,
        base_url=base_url,
    )
    _RUNTIME = runtime

    async def on_arrived(payload: Any) -> None:
        if not runtime.settings.enabled:
            return
        library_id = getattr(payload, "library_id", None)
        for item in getattr(payload, "items", ()) or ():
            media = getattr(item, "media", None)
            media_item_id = getattr(media, "id", None)
            if library_id is None or media_item_id is None:
                continue
            await runtime.enqueue_item(int(media_item_id), int(library_id), reason="arrived")

    ctx.on(
        LIBRARY_ITEMS_ARRIVED,
        on_arrived,
        id="cast-cn:arrived",
        durable=True,
    )

    async def sweep_tick() -> None:
        if not runtime.settings.enabled:
            return
        await runtime.enqueue_sweep(reason="schedule")

    ctx.contribute(
        SCHEDULED_TASKS,
        "sweep",
        TaskDefinition(
            key=SWEEP_TASK,
            title="演职人员中文刮削补跑",
            handler=sweep_tick,
            default_trigger_type=TriggerType.CRON,
            default_cron="0 4 * * *",
            description="按配置的媒体库分页扫描，给缺中文名/角色的条目补一次 NFO。",
        ),
    )

    async def run_item_job(context: JobContext, data: dict[str, Any]) -> dict[str, Any]:
        return await runtime.process_item(context, data)

    async def run_sweep_job(context: JobContext, data: dict[str, Any]) -> dict[str, Any]:
        return await runtime.sweep(context, data)

    ctx.contribute(
        JOB_HANDLERS,
        ITEM_JOB,
        RegisteredJobHandler(run_item_job, frozenset({1}), title="演职人员中文刮削"),
    )
    ctx.contribute(
        JOB_HANDLERS,
        SWEEP_JOB,
        RegisteredJobHandler(run_sweep_job, frozenset({1}), title="演职人员中文刮削补跑"),
    )

    router = APIRouter()

    @router.post(
        "/run",
        operation_id="plugins.cast-cn.run",
        summary="立即运行演职人员中文刮削",
    )
    async def run(payload: RunRequest) -> dict[str, Any]:
        if payload.media_item_id is not None:
            if payload.library_id is None:
                raise HTTPException(
                    status_code=400, detail="指定 media_item_id 时必须同时给 library_id"
                )
            ref = await runtime.enqueue_item(
                int(payload.media_item_id),
                int(payload.library_id),
                reason="manual",
                manual=True,
                dry_run=payload.dry_run,
            )
        else:
            ref = await runtime.enqueue_sweep(
                reason="manual",
                library_id=(int(payload.library_id) if payload.library_id is not None else None),
                manual=True,
                dry_run=payload.dry_run,
            )
        return {"created": ref.created, "job_id": ref.id}

    @router.get(
        "/report",
        operation_id="plugins.cast-cn.report",
        summary="演职人员中文刮削运行报告",
    )
    async def report() -> dict[str, Any]:
        return await runtime.report()

    @router.post(
        "/restore",
        operation_id="plugins.cast-cn.restore",
        summary="恢复某条目最近一次 NFO 备份",
    )
    async def restore(payload: RestoreRequest) -> dict[str, Any]:
        try:
            return await runtime.restore(int(payload.media_item_id))
        except SkipItem as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    routes.mount(ctx, router)

    ctx.logger.info(
        "cast-cn：已加载（enabled=%s，external_url=%s，libraries=%s）",
        settings.enabled,
        "已配置" if base_url else "空",
        settings.libraries or "全部",
    )
