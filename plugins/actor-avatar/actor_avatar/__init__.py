"""演员头像补全插件入口。

用 gfriends 头像库给本地视频库（默认「小姐姐」）的演员补头像：
只往条目旁的 NFO 写 ``<actor><thumb>``，再触发一次「重新读取 NFO」，不改主程序数据库。
"""

import base64
import binascii
import re
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException
from movieclaw_api.plugins.keys import (
    PLUGIN_DATA,
    PLUGIN_FILES,
    PLUGIN_HEALTH,
    PLUGIN_ROUTES,
)
from movieclaw_kernel import DURABLE_EVENTS
from movieclaw_scheduler import SCHEDULED_TASKS, TaskDefinition, TriggerType
from movieclaw_sdk import Context, net, plugin
from movieclaw_sdk.agent import AGENT
from movieclaw_sdk.events import LIBRARY_ITEMS_ARRIVED
from movieclaw_sdk.jobs import (
    JOB_HANDLERS,
    PLUGIN_JOBS,
    JobContext,
    RegisteredJobHandler,
)
from movieclaw_sdk.ops import HOST_OPS, OpsError
from pydantic import BaseModel, Field

from .ai import AiAdvisor, AiError
from .aliases import NameTableCache, NameTableError
from .filetree import FiletreeCache, FiletreeError
from .runtime import RUN_JOB, SEARCH_JOB, SWEEP_TASK, UNDO_JOB, Runtime, SkipItem
from .search import BROWSER_HEADERS, Searcher
from .settings import Settings

PLUGIN_ID = "actor-avatar"
OPERATIONS = (
    "app.show",
    "library.list",
    "library.items.list",
    "library.items.get",
    "library.items.refresh-metadata",
    "jobs.show",
    # MovieClaw 内置 AI 助手，无工具档：只用来给配不上的演员推荐别名（安装时要批准，按模型用量计费）
    "agent:none",
)
_AVATAR_NAME = re.compile(r"^[0-9a-f]{40}\.(jpg|png|webp)$")

_RUNTIME: Runtime | None = None


def _agent_client(ctx: Context) -> Any:
    """拿 AI 助手；宿主不提供（或旧版）时返回 None，插件其余功能照常。"""
    try:
        return ctx.use(AGENT).scoped(ctx)
    except Exception as exc:  # noqa: BLE001 - 宿主没有这个服务就当没有
        ctx.logger.info("actor-avatar：AI 助手不可用：%s", exc)
        return None


class RunRequest(BaseModel):
    library_id: int | None = None
    media_item_ids: list[int] = Field(default_factory=list, max_length=500)
    dry_run: bool | None = None


class UndoRequest(BaseModel):
    media_item_ids: list[int] = Field(default_factory=list, max_length=5000)
    all: bool = False


class ConfirmRequest(BaseModel):
    actor: str = Field(min_length=1, max_length=200)
    candidate: str = Field(min_length=1, max_length=200)
    rerun: bool = True


class LibrariesRequest(BaseModel):
    library_ids: list[int] = Field(default_factory=list, max_length=50)


class ActorRequest(BaseModel):
    actor: str = Field(min_length=1, max_length=200)


class ManualAliasRequest(BaseModel):
    actor: str = Field(min_length=1, max_length=200)
    target: str = Field(min_length=1, max_length=200)
    rerun: bool = True


class ManualUploadRequest(BaseModel):
    actor: str = Field(min_length=1, max_length=200)
    #: 图片内容的 base64（页面先缩到 600px 以内再传）；原图上限 5 MB
    data: str = Field(min_length=8, max_length=7_200_000)
    rerun: bool = True


@plugin(
    PLUGIN_ID,
    title="演员头像补全",
    inject=(
        DURABLE_EVENTS,
        AGENT,
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
    jobs = ctx.use(PLUGIN_JOBS).scoped(ctx)
    routes = ctx.use(PLUGIN_ROUTES)
    health = ctx.use(PLUGIN_HEALTH).reporter(ctx)

    await files.makedirs(files.path("plugin"))

    base_url = (settings.public_base_url or "").strip()
    # 托管模式和手动上传的头像都要外部地址
    if not base_url:
        try:
            app_config = await ops.call("app.show", {})
            if isinstance(app_config, dict):
                base_url = str(app_config.get("external_url") or "").strip()
        except OpsError as exc:
            ctx.logger.warning("actor-avatar：读取外部访问地址失败：%s", exc.message)

    http = httpx.AsyncClient(
        transport=net.http_transport(PLUGIN_ID),
        timeout=httpx.Timeout(60.0, connect=15.0),
        follow_redirects=True,
        headers={"User-Agent": "MovieClaw-actor-avatar/0.3"},
    )
    ctx.effect(http.aclose, label="close-http")
    # 搜索补头像用的客户端：浏览器 UA、不跟随跳转（3xx 一律当没图 / 被拦），超时短一些
    search_http = httpx.AsyncClient(
        transport=net.http_transport(PLUGIN_ID),
        timeout=httpx.Timeout(20.0, connect=10.0),
        follow_redirects=False,
        headers=BROWSER_HEADERS,
    )
    ctx.effect(search_http.aclose, label="close-search-http")

    filetree = FiletreeCache(files=files, store=store, http=http, logger=ctx.logger)
    names = NameTableCache(files=files, store=store, http=http, logger=ctx.logger)

    async def sign(path: str) -> str:
        # 不带过期时间：NFO 里的地址要长期有效；外部地址由运行时拼（可在插件设置里覆盖）
        return await routes.sign(ctx, path)

    runtime = Runtime(
        settings=settings,
        ops=ops,
        store=store,
        files=files,
        jobs=jobs,
        filetree=filetree,
        http=http,
        names=names,
        logger=ctx.logger,
        sign=sign,
        base_url=base_url,
        ai=AiAdvisor(
            agent=_agent_client(ctx),
            store=store,
            daily_limit=settings.ai_daily_limit,
            logger=ctx.logger,
        ),
        searcher=Searcher(http=search_http, settings=settings, logger=ctx.logger),
    )
    _RUNTIME = runtime

    async def on_arrived(payload: Any) -> None:
        if not (runtime.settings.enabled and runtime.settings.process_arrivals):
            return
        library_id = getattr(payload, "library_id", None)
        ids = [
            int(media.id)
            for item in (getattr(payload, "items", ()) or ())
            if (media := getattr(item, "media", None)) is not None
            and getattr(media, "id", None) is not None
        ]
        if library_id is None or not ids:
            return
        libraries, _notes = await runtime.target_libraries()
        if int(library_id) not in {int(lib["id"]) for lib in libraries}:
            return
        await runtime.enqueue_run(reason="arrived", library_id=int(library_id), media_item_ids=ids)

    ctx.on(LIBRARY_ITEMS_ARRIVED, on_arrived, id="actor-avatar:arrived", durable=True)

    async def sweep_tick() -> None:
        if runtime.settings.enabled:
            await runtime.enqueue_run(reason="schedule")

    ctx.contribute(
        SCHEDULED_TASKS,
        "sweep",
        TaskDefinition(
            key=SWEEP_TASK,
            title="演员头像补全",
            handler=sweep_tick,
            default_trigger_type=TriggerType.CRON,
            default_cron="30 4 * * *",
            description=(
                "按设置的本地视频库分批扫描，给没有头像的演员从 gfriends 补头像（默认只预览）。"
            ),
        ),
    )

    async def run_job(context: JobContext, data: dict[str, Any]) -> dict[str, Any]:
        result = await runtime.run(context, data)
        if result.get("status") == "failed":
            await health.degraded("run", "；".join(result.get("notes") or [])[:200] or "运行失败")
        else:
            await health.ok("run")
        return result

    async def undo_job(context: JobContext, data: dict[str, Any]) -> dict[str, Any]:
        return await runtime.undo(context, data)

    ctx.contribute(
        JOB_HANDLERS,
        RUN_JOB,
        RegisteredJobHandler(run_job, frozenset({1}), title="演员头像补全"),
    )

    async def search_job(context: JobContext, data: dict[str, Any]) -> dict[str, Any]:
        return await runtime.search_job(context, data)

    ctx.contribute(
        JOB_HANDLERS,
        SEARCH_JOB,
        RegisteredJobHandler(search_job, frozenset({1}), title="按番号搜索演员头像"),
    )
    ctx.contribute(
        JOB_HANDLERS,
        UNDO_JOB,
        RegisteredJobHandler(undo_job, frozenset({1}), title="撤销演员头像"),
    )

    router = APIRouter()

    @router.post("/run", operation_id="plugins.actor-avatar.run", summary="立即运行演员头像补全")
    async def run(payload: RunRequest) -> dict[str, Any]:
        ref = await runtime.enqueue_run(
            reason="manual",
            library_id=payload.library_id,
            media_item_ids=payload.media_item_ids,
            dry_run=payload.dry_run,
        )
        return {"created": ref.created, "job_id": ref.id}

    @router.get(
        "/report", operation_id="plugins.actor-avatar.report", summary="演员头像补全运行报告"
    )
    async def report() -> dict[str, Any]:
        return await runtime.report()

    @router.get(
        "/libraries",
        operation_id="plugins.actor-avatar.libraries",
        summary="可选的媒体库与当前勾选",
    )
    async def libraries() -> dict[str, Any]:
        try:
            return await runtime.library_choices()
        except SkipItem as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @router.put(
        "/libraries",
        operation_id="plugins.actor-avatar.libraries-save",
        summary="保存目标媒体库",
    )
    async def libraries_save(payload: LibrariesRequest) -> dict[str, Any]:
        try:
            return await runtime.save_selection(payload.library_ids)
        except SkipItem as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post(
        "/write/disarm",
        operation_id="plugins.actor-avatar.write-disarm",
        summary="停止自动写入（回到只在手动点写入时才写）",
    )
    async def write_disarm() -> dict[str, Any]:
        await runtime.disarm()
        return {"write_armed": False}

    @router.post(
        "/names/refresh",
        operation_id="plugins.actor-avatar.names-refresh",
        summary="立即检查中文译名表更新",
    )
    async def names_refresh() -> dict[str, Any]:
        try:
            table = await names.table(
                runtime.settings.name_table_url(), runtime.settings.filetree_ttl_hours, force=True
            )
        except NameTableError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return {"keys": len(table), "meta": await names.meta()}

    @router.get(
        "/ledger", operation_id="plugins.actor-avatar.ledger", summary="已写入头像的条目台账"
    )
    async def ledger() -> dict[str, Any]:
        return {"items": await runtime.ledger_rows()}

    @router.post("/undo", operation_id="plugins.actor-avatar.undo", summary="撤销本插件写入的头像")
    async def undo(payload: UndoRequest) -> dict[str, Any]:
        if not payload.media_item_ids and not payload.all:
            raise HTTPException(status_code=400, detail="要么给 media_item_ids，要么 all=true")
        ref = await runtime.enqueue_undo(None if payload.all else payload.media_item_ids)
        return {"created": ref.created, "job_id": ref.id}

    @router.get("/pending", operation_id="plugins.actor-avatar.pending", summary="待确认的推断匹配")
    async def pending() -> dict[str, Any]:
        return {"items": await runtime.pending()}

    @router.post(
        "/pending/confirm",
        operation_id="plugins.actor-avatar.pending-confirm",
        summary="确认推断匹配",
    )
    async def confirm(payload: ConfirmRequest) -> dict[str, Any]:
        try:
            result = await runtime.confirm(payload.actor, payload.candidate)
        except SkipItem as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if payload.rerun and result["items"]:
            ref = await runtime.enqueue_run(reason="confirm", media_item_ids=result["items"])
            result["job_id"] = ref.id
        return result

    @router.post(
        "/pending/reject",
        operation_id="plugins.actor-avatar.pending-reject",
        summary="拒绝推断匹配",
    )
    async def reject(payload: ActorRequest) -> dict[str, Any]:
        return await runtime.reject(payload.actor)

    @router.post(
        "/aliases/remove",
        operation_id="plugins.actor-avatar.alias-remove",
        summary="删除已确认的别名",
    )
    async def alias_remove(payload: ActorRequest) -> dict[str, Any]:
        return await runtime.forget_alias(payload.actor)

    @router.get(
        "/unmatched",
        operation_id="plugins.actor-avatar.unmatched",
        summary="最近没配上的演员（含单独列出的刮削垃圾名）",
    )
    async def unmatched() -> dict[str, Any]:
        return await runtime.unmatched()

    @router.get(
        "/search", operation_id="plugins.actor-avatar.search", summary="搜 gfriends 名字与中文译名"
    )
    async def search(q: str = "", limit: int = 20) -> dict[str, Any]:
        try:
            return {"items": await runtime.search(q[:100], max(1, min(limit, 50)))}
        except SkipItem as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @router.get(
        "/manual", operation_id="plugins.actor-avatar.manual", summary="手动设置的别名与头像"
    )
    async def manual() -> dict[str, Any]:
        return {"items": await runtime.manual_rows()}

    async def _rerun(result: dict[str, Any], rerun: bool) -> dict[str, Any]:
        if rerun and result.get("items"):
            ref = await runtime.enqueue_run(reason="manual-entry", media_item_ids=result["items"])
            result["job_id"] = ref.id
        return result

    @router.post(
        "/manual/alias",
        operation_id="plugins.actor-avatar.manual-alias",
        summary="手动把演员名对到 gfriends 里的名字",
    )
    async def manual_alias(payload: ManualAliasRequest) -> dict[str, Any]:
        try:
            result = await runtime.set_manual_alias(payload.actor, payload.target)
        except SkipItem as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return await _rerun(result, payload.rerun)

    @router.post(
        "/manual/upload",
        operation_id="plugins.actor-avatar.manual-upload",
        summary="手动上传演员头像",
    )
    async def manual_upload(payload: ManualUploadRequest) -> dict[str, Any]:
        raw = payload.data.split(",", 1)[1] if payload.data.startswith("data:") else payload.data
        try:
            body = base64.b64decode(raw, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise HTTPException(status_code=400, detail="图片编码不对") from exc
        try:
            result = await runtime.set_manual_upload(payload.actor, body)
        except SkipItem as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return await _rerun(result, payload.rerun)

    @router.post(
        "/manual/remove",
        operation_id="plugins.actor-avatar.manual-remove",
        summary="删除手动设置（已写入的头像用撤销）",
    )
    async def manual_remove(payload: ActorRequest) -> dict[str, Any]:
        return await runtime.remove_manual(payload.actor)

    @router.get(
        "/uploads/{name}",
        operation_id="plugins.actor-avatar.upload-preview",
        summary="预览上传的头像（管理页面用）",
    )
    async def upload_preview(name: str) -> Any:
        path = await runtime.upload_path(name) if _AVATAR_NAME.match(name) else None
        if path is None:
            raise HTTPException(status_code=404)
        return files.response(path)

    @router.get("/ai", operation_id="plugins.actor-avatar.ai-status", summary="AI 推荐的额度与用量")
    async def ai_status() -> dict[str, Any]:
        return await runtime.ai_status()

    @router.post(
        "/ai/suggest",
        operation_id="plugins.actor-avatar.ai-suggest",
        summary="让 AI 助手给一位演员推荐别名（结果只进待确认）",
    )
    async def ai_suggest(payload: ActorRequest) -> dict[str, Any]:
        try:
            result = await runtime.ai_suggest(payload.actor)
        except AiError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except SkipItem as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return {**result, "status": await runtime.ai_status()}

    @router.post(
        "/avatar-search",
        operation_id="plugins.actor-avatar.avatar-search",
        summary="按番号给一位演员搜索头像（后台任务，结果只进待确认）",
    )
    async def avatar_search(payload: ActorRequest) -> dict[str, Any]:
        try:
            ref = await runtime.enqueue_search(payload.actor)
        except SkipItem as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"created": ref.created, "job_id": ref.id, "state": "queued"}

    @router.get(
        "/avatar-search/status",
        operation_id="plugins.actor-avatar.avatar-search-status",
        summary="搜索头像的进度与结果",
    )
    async def avatar_search_status(actor: str = "") -> dict[str, Any]:
        return await runtime.search_status(actor[:200] or None)

    @router.post(
        "/filetree/refresh",
        operation_id="plugins.actor-avatar.filetree-refresh",
        summary="立即检查头像索引更新",
    )
    async def filetree_refresh() -> dict[str, Any]:
        try:
            index = await filetree.index(
                runtime.settings.source_base(), runtime.settings.filetree_ttl_hours, force=True
            )
        except FiletreeError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return {"names": len(index), "meta": await filetree.meta()}

    routes.mount(ctx, router)

    # 托管模式下载的头像、手动上传的头像都从这里给出（签名链接，只认 40 位哈希文件名）
    public = APIRouter()

    @public.get(
        "/avatar/{name}", operation_id="plugins.actor-avatar.avatar", summary="托管的演员头像"
    )
    async def avatar(name: str) -> Any:
        if not _AVATAR_NAME.match(name):
            raise HTTPException(status_code=404)
        path = files.path("plugin", "avatars", name)
        if not await files.exists(path):
            raise HTTPException(status_code=404)
        return files.response(path)

    routes.mount(ctx, public, zone="public")

    ctx.logger.info(
        "actor-avatar：已加载（旧版库设置=%s，dry_run=%s，mirror=%s，thumb=%s）",
        settings.legacy_library_refs(),
        settings.dry_run,
        settings.mirror.value,
        settings.thumb_mode.value,
    )
