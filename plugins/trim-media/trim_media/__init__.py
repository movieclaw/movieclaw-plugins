"""飞牛影视（fnOS 影视 / Trim Media）：把飞牛当 MovieClaw 的播放器与库伙伴。

三个能力，互相独立，可单独关：

1. **库接入（adopt）**：发现飞牛上的媒体库，把「本机看得到目录」的库建成本地
   媒体库（``library.create``）。库就是普通本地库——识别、刮削、海报墙、播放、
   Jellyfin 全是主线原生行为，**飞牛挂了照常播**。认领关系记在插件自己的数据里
   （宿主的 ``LibraryView`` 不回 ``source_config``，进程外插件也无法登记外部库源）。
2. **进度双向同步（sync）**：MC → 飞牛走播放事件（近实时，停止/看完不节流），
   飞牛 → MC 走定时轮询「继续观看」（活动期加速）。两侧共同有的片才同步，
   靠 (TMDB, 季, 集) 收敛。
3. **片头片尾写回（markers）**：主线识别完一季，按季取中位数写进飞牛——
   飞牛按季存、整季生效，之后在飞牛里播也会自动跳过。

边界（对照插件规范自查过）：

- 不写主线数据库台账；一切经宿主操作，写入统一走 ``playback.sync.state``
  （它带 compare-and-set 与事件抑制，插件不重复实现可见性与并发控制）。
- 事件订阅只拿 payload 快照（``media.tmdb_id`` 等），不回查宿主库。
- 只加不删：飞牛删了库，本地库与文件不动——兜底库的意义就在这里。
"""

import asyncio
import os
import time
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field, SecretStr
from movieclaw_api.plugins.keys import PLUGIN_DATA, PLUGIN_ROUTES
from movieclaw_kernel import Context, plugin
from movieclaw_scheduler import SCHEDULED_TASKS, TaskDefinition, TriggerType
from movieclaw_sdk.events import (
    LIBRARY_SEGMENTS_UPDATED,
    PLAYBACK_COMPLETED,
    PLAYBACK_PROGRESS,
    PLAYBACK_STOPPED,
)
from movieclaw_sdk.events.library import SegmentsUpdated
from movieclaw_sdk.jobs import (
    JOB_HANDLERS,
    PLUGIN_JOBS,
    JobContext,
    JobRetry,
    RegisteredJobHandler,
)
from movieclaw_sdk.ops import HOST_OPS, OpsError

from .adopt import adopt_source_config, explain_create_error, plan_adopt
from .sync import (
    EXPECTED_MEMBER,
    activity_recent,
    handle_playback_event,
    note_activity,
    pull_tick,
    resolve_guid,
    skip_values_from_segments,
    median,
)
from .trim_api import TrimClient, TrimError

PLUGIN_ID = "trim-media"
SYNC_JOB_TYPE = f"{PLUGIN_ID}:sync"
_PULL_FAST_ROUNDS = 12
_PULL_FAST_INTERVAL = 5.0


class Config(BaseModel):
    """设置页表单：连接、路径映射、各能力的开关。"""

    base_url: str = Field(
        "", title="飞牛地址", description="影视服务地址，如 http://nas:5666（带不带 /v 都行）"
    )
    username: str = Field("", title="用户名")
    password: SecretStr | None = Field(None, title="密码")
    access_code: SecretStr | None = Field(
        None, title="访问码（可选）", description="开了设备访问码才填"
    )
    member_username: str = Field(
        "",
        title="这个飞牛账号对应哪个成员",
        description=(
            "MovieClaw 成员用户名；留空表示超管。只有这个成员的进度会写进飞牛，"
            "从飞牛拉回的进度也只记在名下"
        ),
    )
    path_map_from: str = Field(
        "",
        title="飞牛路径前缀",
        description="飞牛库目录的路径开头（如 /mnt/media/Movies）；配合下一项把库接成本地库",
    )
    path_map_to: str = Field(
        "",
        title="本机路径前缀",
        description="上面那个前缀在本机对应的路径；两个都填才接入，只填一个等于没配",
    )
    auto_adopt: bool = Field(
        True, title="自动接入新库", description="飞牛上新建的库在同步时自动建成本地媒体库"
    )
    sync_progress: bool = Field(
        True, title="同步观看进度", description="双向：MC ↔ 飞牛，两边都有的片才同步"
    )
    sync_markers: bool = Field(
        True, title="同步片头片尾到飞牛", description="MC 识别出的结论写回飞牛，按季生效"
    )


def client_of(config: Config) -> TrimClient:
    return TrimClient(
        config.base_url,
        config.username,
        config.password.get_secret_value() if config.password else "",
        config.access_code.get_secret_value() if config.access_code else "",
    )


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


@plugin(
    PLUGIN_ID,
    title="飞牛影视",
    inject=(HOST_OPS, PLUGIN_DATA, PLUGIN_JOBS, PLUGIN_ROUTES),
    permissions=(
        "library.list",
        "library.create",
        "library.scan.start",
        "library.items.segments",
        "playback.sync.catalog",
        "playback.sync.state",
        "members.list",
    ),
    config=Config,
)
async def trim_media(ctx: Context[Config]) -> None:
    config = (
        ctx.config if isinstance(ctx.config, Config) else Config.model_validate(ctx.config or {})
    )
    store = ctx.use(PLUGIN_DATA).scoped(ctx)
    routes = ctx.use(PLUGIN_ROUTES)
    ops = await ctx.use(HOST_OPS).client(ctx)
    jobs = ctx.use(PLUGIN_JOBS).scoped(ctx)
    log = ctx.logger

    if not config.base_url or not config.username or config.password is None:
        log.info("飞牛影视：连接信息没填完整，到「设置 → 插件 → 飞牛影视」里填")

    # ---- 成员映射：配置的成员名 → id（events 侧按 id 过滤，比名字稳） --------
    async def refresh_member() -> int:
        name = config.member_username.strip().lower()
        EXPECTED_MEMBER.clear()
        if not name:
            EXPECTED_MEMBER["id"] = None
            return 0  # 超管
        try:
            rows = await ops.call("members.list", {})
        except OpsError as exc:
            log.warning("读成员列表失败：%s", exc.message)
            EXPECTED_MEMBER["id"] = None
            return -1
        for row in rows or []:
            if str(row.get("username") or "").strip().lower() == name:
                EXPECTED_MEMBER["id"] = int(row["id"])
                return int(row["id"])
        log.warning("成员「%s」不存在，进度同步暂停；检查插件设置里的成员名", name)
        EXPECTED_MEMBER["id"] = None
        return -1

    await refresh_member()

    # ---- 库接入 -------------------------------------------------------------
    async def existing_with_ledger() -> list[dict[str, Any]]:
        """宿主库行 + 插件认领台账：已接入的行补上 ``source_config.library_guid``，
        让 ``plan_adopt`` 按同一口径把认过领的库标成 mapped。"""
        rows = await ops.call("library.list", {"scope": "all"})
        existing = [
            {k: row[k] for k in ("name", "root_paths", "id") if k in row} for row in rows or []
        ]
        ledger: dict[str, int] = await store.get("ledger") or {}
        by_id = {int(v): k for k, v in ledger.items()}
        for row in existing:
            guid = by_id.get(int(row["id"])) if isinstance(row.get("id"), int) else None
            if guid is not None:
                row["source_config"] = {"library_guid": guid}
        return existing

    async def run_sync(progress: JobContext | None = None) -> dict[str, Any]:
        """一轮：连飞牛 → 自动接入新库（只加不删）→ 触发扫描 → 记录结果。"""
        trim = client_of(config)
        result: dict[str, Any] = {"adopted": [], "scanned": 0, "errors": []}
        try:
            await trim.connect()
            if config.auto_adopt:
                ledger: dict[str, int] = await store.get("ledger") or {}
                plans = plan_adopt(
                    await trim.libraries(),
                    existing=await existing_with_ledger(),
                    path_map_from=config.path_map_from,
                    path_map_to=config.path_map_to,
                    name_prefix="飞牛",
                    dir_exists=os.path.isdir,
                )
                occupied: list[tuple[str, str]] = [
                    (str(row.get("name")), str(root))
                    for row in await ops.call("library.list", {"scope": "all"}) or []
                    for root in row.get("root_paths") or []
                ]
                for plan in plans:
                    if plan["action"] != "adopt":
                        continue
                    body = adopt_source_config(
                        plan,
                        server_url=trim.base_url.rstrip("/"),
                        server_guid=str(await store.get("server_guid") or "trim"),
                    )
                    # 库就是普通本地库：source_config 主线不收（来源未登记会 400），
                    # 认领关系记在插件自己的 ledger
                    body.pop("source_config", None)
                    try:
                        created = await ops.call("library.create", {"body": body})
                    except OpsError as exc:
                        result["errors"].append(
                            f"建库「{plan['name']}」失败：{explain_create_error(exc)}"
                        )
                        continue
                    ledger[plan["guid"]] = int(created["id"])
                    await store.set("ledger", ledger)
                    result["adopted"].append(
                        {"guid": plan["guid"], "name": body["name"], "id": created["id"]}
                    )
                    occupied.extend((body["name"], root) for root in body["root_paths"])
                    try:
                        await ops.call("library.scan.start", {"library_id": int(created["id"])})
                        result["scanned"] += 1
                    except OpsError as exc:
                        if exc.status != 409:  # 409 = 库正忙（建库即扫描），不用再排
                            result["errors"].append(
                                f"「{body['name']}」扫描触发失败：{exc.message}"
                            )
            versions = dict(trim.versions or {})
            if versions:
                await store.set("trim_versions", versions)
            await store.set("last_sync_result", {**result, "at": _now_iso()})
            return result
        except TrimError as exc:
            result["errors"].append(f"连不上飞牛：{exc}")
            await store.set("last_sync_result", {**result, "at": _now_iso()})
            return result
        finally:
            await trim.close()

    async def sync_job(context: JobContext, data: dict[str, Any]) -> dict[str, Any]:
        result = await run_sync(progress=context)
        errors = result.get("errors") or []
        if not result["adopted"] and len(errors) == 1 and errors[0].startswith("连不上"):
            raise JobRetry(errors[0], delay_seconds=300)
        return result

    ctx.contribute(
        JOB_HANDLERS,
        SYNC_JOB_TYPE,
        RegisteredJobHandler(sync_job, frozenset({1}), title="同步飞牛影视库"),
    )

    async def scheduled_sync() -> None:
        ref = await jobs.create(SYNC_JOB_TYPE, input={}, dedupe_key="all")
        if not ref.created:
            log.info("上一轮飞牛同步还在进行，本轮跳过")

    ctx.contribute(
        SCHEDULED_TASKS,
        "sync",
        TaskDefinition(
            key=f"{PLUGIN_ID}.sync",
            title="同步飞牛影视库",
            description="发现飞牛上的新库并接成本地媒体库（只加不删）",
            handler=scheduled_sync,
            default_trigger_type=TriggerType.INTERVAL,
            default_interval_seconds=6 * 3600,
            default_enabled=True,
        ),
    )

    # ---- 进度：MC → 飞牛（事件驱动） ----------------------------------------
    def _want_member(payload: Any) -> bool:
        """只处理映射成员（或超管）的事件：飞牛进度按账号存，别把全家人的写进一个账号。"""
        who = int(getattr(payload, "member_id", 0) or 0)
        expect = EXPECTED_MEMBER.get("id")
        if expect is None:
            return who == 0
        return who == int(expect)

    async def _push(payload: Any, *, force: bool) -> None:
        if not config.sync_progress or not _want_member(payload):
            return
        note_activity()
        trim = client_of(config)
        try:
            await trim.connect()
            await handle_playback_event(
                payload=payload,
                trim=trim,
                log=log,
                force=force,
                member_username=config.member_username,
            )
        except TrimError as exc:
            log.debug("飞牛不可用，进度跳过：%s", exc)
        finally:
            await trim.close()

    async def on_progress(payload: Any) -> None:
        await _push(payload, force=False)

    async def on_finalize(payload: Any) -> None:
        await _push(payload, force=True)  # 停止 / 看完不节流

    ctx.on(PLAYBACK_PROGRESS, on_progress)
    ctx.on(PLAYBACK_STOPPED, on_finalize)
    ctx.on(PLAYBACK_COMPLETED, on_finalize)

    # ---- 进度：飞牛 → MC（定时轮询，活动期加速） ----------------------------
    async def pull_progress_task() -> None:
        if not config.sync_progress:
            return
        if config.member_username.strip() and EXPECTED_MEMBER.get("id") is None:
            return  # 成员映射没解析出来：宁缺毋滥，别把进度记错人
        trim = client_of(config)
        try:
            await trim.connect()
            target = EXPECTED_MEMBER.get("id")
            member = 0 if target is None else int(target)
            await pull_tick(trim=trim, ops=ops, log=log, member_id=member)
            rounds = 0
            while rounds < _PULL_FAST_ROUNDS and activity_recent():
                await asyncio.sleep(_PULL_FAST_INTERVAL)
                await pull_tick(trim=trim, ops=ops, log=log, member_id=member)
                rounds += 1
        except TrimError as exc:
            log.debug("飞牛不可用，拉回进度跳过：%s", exc)
        finally:
            await trim.close()

    ctx.contribute(
        SCHEDULED_TASKS,
        "pull-progress",
        TaskDefinition(
            key=f"{PLUGIN_ID}.pull-progress",
            title="同步飞牛观看进度",
            description="把飞牛「继续观看」里的真实进度写回 MovieClaw（两边都有的片才同步）",
            handler=pull_progress_task,
            default_trigger_type=TriggerType.INTERVAL,
            default_interval_seconds=60,
            default_enabled=True,
        ),
    )

    # ---- 片头片尾：MC → 飞牛（识别完成事件驱动） ----------------------------
    async def on_segments_updated(payload: SegmentsUpdated) -> None:
        if not config.sync_markers:
            return
        media = payload.media
        season = int(payload.season)
        tmdb_id = getattr(media, "tmdb_id", None)
        title = str(getattr(media, "title", "") or "")
        if not tmdb_id:
            return
        trim = client_of(config)
        try:
            await trim.connect()
            guid = await resolve_guid(
                trim, tmdb_id=int(tmdb_id), title=title, season=season, episode=0, log=log
            )
            if guid is None:
                log.info("飞牛里找不到《%s》第 %d 季，片头片尾跳过", title, season)
                return
            # 主线结论：先经 catalog 拿 media_item_id（事件只有 TMDB 锚点，没有库行）
            catalogs = await ops.call(
                "playback.sync.catalog", {"member_id": 0, "tmdb_id": int(tmdb_id), "limit": 50}
            )
            media_item_id = None
            for unit in (catalogs or {}).get("items") or []:
                m = unit.get("media") or {}
                if int(m.get("tmdb_id") or 0) == int(tmdb_id):
                    media_item_id = int(m.get("id") or 0)
                    break
            if not media_item_id:
                return
            for lib in await ops.call("library.list", {"scope": "all"}) or []:
                segs = await ops.call(
                    "library.items.segments",
                    {
                        "library_id": int(lib["id"]),
                        "media_item_id": media_item_id,
                        "season": season,
                    },
                )
                if not segs:
                    continue
                # 季内各集取中位数 → 一套 (opening, ending) 写给整季（飞牛按季存）
                openings: list[int] = []
                endings: list[int] = []
                for row in segs:
                    values = skip_values_from_segments(
                        row.get("segments"), row.get("duration_seconds")
                    )
                    if values is None:
                        continue
                    opening, ending = values
                    if opening:
                        openings.append(opening)
                    if ending:
                        endings.append(ending)
                if not openings and not endings:
                    break
                skip_opening = median(openings) if openings else 0
                skip_ending = median(endings) if endings else 0
                key = f"marker:{guid}"
                if await store.get(key) == [skip_opening, skip_ending]:
                    break  # 值没变就不调接口（事件可能重复投递）
                try:
                    await trim.set_play_config(guid, skip_opening, skip_ending)
                    await store.set(key, [skip_opening, skip_ending])
                    log.info(
                        "飞牛片头片尾已写：《%s》S%02d（片头 %ds / 片尾 %ds）",
                        title, season, skip_opening, skip_ending,
                    )
                except TrimError as exc:
                    log.warning("写飞牛片头片尾失败（%s S%02d）：%s", title, season, exc)
                break
        except TrimError as exc:
            log.debug("飞牛不可用，片头片尾写回跳过：%s", exc)
        finally:
            await trim.close()

    ctx.on(LIBRARY_SEGMENTS_UPDATED, on_segments_updated)

    # ---- 管理路由（admin 区） ----------------------------------------------
    router = APIRouter()

    @router.get(
        "/status",
        operation_id=f"plugins.{PLUGIN_ID}.status",
        summary="运行状态：连接、版本、已接入的库与最近同步",
    )
    async def status() -> dict:
        ledger: dict[str, int] = await store.get("ledger") or {}
        claimed = set(ledger.values())
        libraries = [
            {
                "id": row.get("id"),
                "name": row.get("name"),
                "kind": row.get("kind"),
                "root_paths": row.get("root_paths"),
                "primary_root": row.get("primary_root"),
            }
            for row in await ops.call("library.list", {"scope": "all"}) or []
            if row.get("id") in claimed
        ]
        return {
            "configured": bool(config.base_url and config.username and config.password),
            "base_url": config.base_url,
            "versions": await store.get("trim_versions") or {},
            "member_username": config.member_username.strip(),
            "member_resolved": EXPECTED_MEMBER.get("id"),
            "auto_adopt": config.auto_adopt,
            "sync_progress": config.sync_progress,
            "sync_markers": config.sync_markers,
            "last_sync_result": await store.get("last_sync_result"),
            "libraries": libraries,
        }

    @router.post(
        "/test",
        operation_id=f"plugins.{PLUGIN_ID}.test",
        summary="测试连接：登录飞牛并返回服务版本",
    )
    async def test() -> dict:
        trim = client_of(config)
        try:
            await trim.connect()
            return {"ok": True, "versions": dict(trim.versions or {})}
        except TrimError as exc:
            return {"ok": False, "error": str(exc)}
        finally:
            await trim.close()

    @router.get(
        "/plan",
        operation_id=f"plugins.{PLUGIN_ID}.plan",
        summary="接入预览：飞牛上有哪些库、各自能接还是跳过（含原因）",
    )
    async def plan() -> dict:
        trim = client_of(config)
        try:
            await trim.connect()
            plans = plan_adopt(
                await trim.libraries(),
                existing=await existing_with_ledger(),
                path_map_from=config.path_map_from,
                path_map_to=config.path_map_to,
                name_prefix="飞牛",
                dir_exists=os.path.isdir,
            )
            return {"plans": plans}
        except TrimError as exc:
            return {"plans": [], "error": str(exc)}
        finally:
            await trim.close()

    @router.post(
        "/sync-now",
        operation_id=f"plugins.{PLUGIN_ID}.sync-now",
        summary="立即同步一轮（接入新库并触发扫描）",
    )
    async def sync_now() -> dict:
        ref = await jobs.create(SYNC_JOB_TYPE, input={}, dedupe_key="manual")
        return {"created": ref.created, "job_id": ref.id}

    routes.mount(ctx, router)

    log.info("飞牛影视插件已加载（base_url=%s）", config.base_url or "未配置")
