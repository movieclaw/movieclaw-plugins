"""续播进度清理插件入口。

在插件页面选一个媒体库和若干成员：这些成员在这个库里播放结束后，延迟几十秒把那一集 /
那部片的续播进度（``position_ms``）清零；可选每小时兜底扫描一次残留的续播进度。
只经 ``playback.sync.state`` 写续播位置，不动已看、收藏、播放次数、音轨字幕与播放流水。
没选库或没选成员时什么都不做。
"""

from typing import Any

from fastapi import APIRouter, HTTPException
from movieclaw_api.plugins.keys import PLUGIN_DATA, PLUGIN_ROUTES
from movieclaw_kernel import DURABLE_EVENTS
from movieclaw_scheduler import SCHEDULED_TASKS, TaskDefinition, TriggerType
from movieclaw_sdk import Context, plugin
from movieclaw_sdk.events import PLAYBACK_COMPLETED, PLAYBACK_STOPPED
from movieclaw_sdk.ops import HOST_OPS
from pydantic import BaseModel, Field

from .runtime import SWEEP_INTERVAL, SWEEP_TASK, Runtime, SkipItem

PLUGIN_ID = "playback-cleanup"
OPERATIONS = (
    "members.list",
    "library.list",
    "library.items.list",
    "playback.sync.catalog",
    # 危险操作：须逐条列出（playback.* 之类的展开不含它）；只用来写 position_ms
    "playback.sync.state",
)
REQUIRES = {
    "scheduled-tasks": "^1.0",
    "plugin-data": "^1.0",
    "plugin-routes": "^1.0",
    "kernel/durable-events": "^1.0",
    "playback.stopped": "^1.1",
    "playback.completed": "^1.1",
}


class SettingsIn(BaseModel):
    library_id: int | None = Field(default=None, ge=1)
    member_ids: list[int] = Field(default_factory=list, max_length=200)
    delay_seconds: int = Field(default=45)
    sweep_enabled: bool = True
    enabled: bool = True
    dry_run: bool = True


class UndoIn(BaseModel):
    id: str = Field(min_length=1, max_length=40)


@plugin(
    PLUGIN_ID,
    title="续播进度清理",
    inject=(DURABLE_EVENTS, HOST_OPS, PLUGIN_DATA, PLUGIN_ROUTES),
    requires=REQUIRES,
    permissions=OPERATIONS,
)
async def apply(ctx: Context) -> None:
    ops = await ctx.use(HOST_OPS).client(ctx)
    store = ctx.use(PLUGIN_DATA).scoped(ctx)
    routes = ctx.use(PLUGIN_ROUTES)

    runtime = Runtime(ops=ops, store=store, logger=ctx.logger)
    ctx.effect(runtime.close, label="cancel-timers")
    restored = await runtime.restore_pending()
    if restored:
        ctx.logger.info("playback-cleanup：接着排上 %d 个延迟清理", restored)

    async def on_playback(event: Any) -> None:
        await runtime.on_playback(event)

    # 可靠订阅：插件重启 / 暂时不可用时宿主补投；处理里只排一个延迟任务，很快返回
    ctx.on(PLAYBACK_STOPPED, on_playback, id="cleanup-stopped", durable=True)
    ctx.on(PLAYBACK_COMPLETED, on_playback, id="cleanup-completed", durable=True)

    async def sweep_tick() -> None:
        settings = await runtime.settings()
        await runtime.sweep(reason="schedule", dry_run=settings.dry_run)

    ctx.contribute(
        SCHEDULED_TASKS,
        "sweep",
        TaskDefinition(
            key=SWEEP_TASK,
            title="续播进度兜底清理",
            handler=sweep_tick,
            default_trigger_type=TriggerType.INTERVAL,
            default_interval_seconds=SWEEP_INTERVAL,
            description=(
                "每小时扫一遍插件页面选中的媒体库和成员，清掉残留的续播进度（默认只预览）。"
                "没选库、没选成员、插件停用或关掉「兜底扫描」时什么都不做。"
            ),
        ),
    )

    router = APIRouter()

    @router.get("/report", operation_id="plugins.playback-cleanup.report", summary="运行概况")
    async def report() -> dict[str, Any]:
        return await runtime.report()

    @router.get(
        "/settings",
        operation_id="plugins.playback-cleanup.settings",
        summary="设置与可选的媒体库、成员",
    )
    async def settings_get() -> dict[str, Any]:
        try:
            return await runtime.choices()
        except SkipItem as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @router.put(
        "/settings",
        operation_id="plugins.playback-cleanup.settings-save",
        summary="保存设置",
    )
    async def settings_save(payload: SettingsIn) -> dict[str, Any]:
        try:
            return await runtime.save_settings(payload.model_dump())
        except SkipItem as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    def _start(dry_run: bool, reason: str) -> dict[str, Any]:
        started = runtime.start_sweep(reason=reason, dry_run=dry_run)
        return {"started": started, "running": runtime.running()}

    @router.post(
        "/preview",
        operation_id="plugins.playback-cleanup.preview",
        summary="立即预览（只列出会清哪些，不写入）",
    )
    async def preview() -> dict[str, Any]:
        return _start(True, "preview")

    @router.post(
        "/run",
        operation_id="plugins.playback-cleanup.run",
        summary="立即清理一次（真的写入，不看只预览开关）",
    )
    async def run() -> dict[str, Any]:
        return _start(False, "manual")

    @router.get("/history", operation_id="plugins.playback-cleanup.history", summary="清理记录")
    async def history() -> dict[str, Any]:
        return {"items": await runtime.history_rows()}

    @router.post(
        "/undo",
        operation_id="plugins.playback-cleanup.undo",
        summary="撤销一条清理（把续播进度写回去）",
    )
    async def undo(payload: UndoIn) -> dict[str, Any]:
        try:
            return await runtime.undo(payload.id)
        except SkipItem as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    # 默认管理员区：这些接口会改别的成员的播放状态
    routes.mount(ctx, router)

    settings = await runtime.settings()
    ctx.logger.info(
        "playback-cleanup：已加载（state=%s，dry_run=%s，delay=%ss，sweep=%s）",
        settings.selection_state(),
        settings.dry_run,
        settings.delay_seconds,
        settings.sweep_enabled,
    )
