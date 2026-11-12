"""飞牛影视（fnOS 影视 / Trim Media）：把飞牛当 MovieClaw 的播放器与库伙伴。

三个能力，互相独立，可单独关：

1. **库接入**：发现飞牛上的媒体库，把「本机看得到目录」的库建成本地媒体库
   （``library.create``）。库就是普通本地库——识别、刮削、海报墙、播放、
   Jellyfin 全是主线原生行为，**飞牛挂了照常播**。认领关系记在插件自己的数据里。
2. **进度双向同步**：MC → 飞牛走播放事件（近实时，停止/看完不节流），
   飞牛 → MC 走定时轮询「继续观看」（活动期加速）。两侧共同有的片才同步，
   靠 (TMDB, 季, 集) 收敛。
3. **片头片尾写回**：主线识别完一季，按季取中位数写进飞牛——飞牛按季存、
   整季生效，之后在飞牛里播也会自动跳过。

外加一页管理界面（``web/``）：库映射、覆盖检查、片头片尾对比与手工改、
飞牛账号与库权限、运行日志、差异检查。

边界（对照插件规范自查过）：

- 不写主线数据库台账；一切经宿主操作，写入统一走 ``playback.sync.state``
  （它带 compare-and-set 与事件抑制，插件不重复实现可见性与并发控制）。
- 事件订阅只拿 payload 快照（``media.tmdb_id`` 等），不回查宿主库。
- 只加不删：飞牛删了库，本地库与文件不动——兜底库的意义就在这里。
"""

import asyncio

from movieclaw_api.plugins.keys import PLUGIN_DATA, PLUGIN_FILES, PLUGIN_ROUTES
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
from movieclaw_sdk.ops import HOST_OPS

from . import adopt_store
from . import markers as mk
from .config import Config, client_of
from .routes import Deps, build_router
from .runlog import RunLog
from .sync import (
    EXPECTED_MEMBER,
    activity_recent,
    handle_playback_event,
    note_activity,
    pull_tick,
)
from .trim_api import TrimError

PLUGIN_ID = "trim-media"
SYNC_JOB_TYPE = f"{PLUGIN_ID}:sync"
_PULL_FAST_ROUNDS = 12
_PULL_FAST_INTERVAL = 5.0


@plugin(
    PLUGIN_ID,
    title="飞牛影视",
    inject=(HOST_OPS, PLUGIN_DATA, PLUGIN_FILES, PLUGIN_JOBS, PLUGIN_ROUTES),
    permissions=(
        "library.list",
        "library.create",
        "library.scan.start",
        "library.items.list",
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
    files = ctx.use(PLUGIN_FILES).scoped(ctx)
    routes = ctx.use(PLUGIN_ROUTES)
    ops = await ctx.use(HOST_OPS).client(ctx)
    jobs = ctx.use(PLUGIN_JOBS).scoped(ctx)
    log = ctx.logger
    #: 运行日志：管理页的「运行日志」一栏读它，每 3 秒增量拉取
    runlog = RunLog()
    sink = runlog.handler()
    log.addHandler(sink)
    ctx.effect(lambda: log.removeHandler(sink))

    if not config.configured:
        log.info("飞牛影视：连接信息没填完整，到「设置 → 插件 → 飞牛影视」里填")

    # ---- 成员映射：配置的成员名 → id（事件侧按 id 过滤，比名字稳） --------
    member_state: dict[str, int | None] = {"id": None}

    async def refresh_member() -> int:
        name = config.member_username.strip().lower()
        EXPECTED_MEMBER.clear()
        if not name:
            EXPECTED_MEMBER["id"] = None
            member_state["id"] = None
            return 0  # 超管
        try:
            rows = await ops.call("members.list", {})
        except Exception as exc:  # noqa: BLE001 -- 成员查不到不该拖垮插件
            log.warning("读成员列表失败：%s", exc)
            EXPECTED_MEMBER["id"] = None
            member_state["id"] = None
            return -1
        for row in rows or []:
            if str(row.get("username") or "").strip().lower() == name:
                EXPECTED_MEMBER["id"] = int(row["id"])
                member_state["id"] = int(row["id"])
                return int(row["id"])
        log.warning("成员「%s」不存在，进度同步暂停；检查插件设置里的成员名", name)
        EXPECTED_MEMBER["id"] = None
        member_state["id"] = None
        return -1

    await refresh_member()

    # ---- 库接入 -------------------------------------------------------------
    async def run_sync(progress: JobContext | None = None) -> dict:
        """一轮：连飞牛 → 自动接入新库（只加不删）→ 触发扫描 → 记录结果。"""
        result: dict = {"adopted": [], "scanned": 0, "errors": []}
        trim = client_of(config)
        try:
            await trim.connect()
            result = await adopt_store.run_sync(ops, store, config, trim=trim, progress=progress)
            result["at"] = _stamp()
            await store.set("last_sync_result", result)
            for row in result.get("adopted") or []:
                log.info("接入飞牛库「%s」→ 本地库 #%s", row["name"], row["id"])
            return result
        except TrimError as exc:
            result["errors"].append(f"连不上飞牛：{exc}")
            result["at"] = _stamp()
            await store.set("last_sync_result", result)
            return result
        finally:
            await trim.close()

    async def sync_job(context: JobContext, data: dict) -> dict:
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
    def _want_member(payload) -> bool:
        """只处理映射成员（或超管）的事件：飞牛进度按账号存，别把全家人的写进一个账号。"""
        who = int(getattr(payload, "member_id", 0) or 0)
        expect = EXPECTED_MEMBER.get("id")
        if expect is None:
            return who == 0
        return who == int(expect)

    async def _push(payload, *, force: bool) -> None:
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

    async def on_progress(payload) -> None:
        await _push(payload, force=False)

    async def on_finalize(payload) -> None:
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
    async def write_marker(
        *, library_id: int, season_guid: str, opening: int, ending: int, label: str
    ) -> bool:
        """写一季的片头片尾；值没变就跳过（事件可能重复投递）。"""
        state = await store.get("markers_pushed_state") or {}
        if not isinstance(state, dict):
            state = {}
        if state.get(season_guid) == [int(opening), int(ending)]:
            return False
        trim = client_of(config)
        try:
            await trim.connect()
            await trim.set_play_config(season_guid, int(opening), int(ending))
        except TrimError as exc:
            log.warning("写飞牛片头片尾失败（%s）：%s", label, exc)
            return False
        finally:
            await trim.close()
        state[season_guid] = [int(opening), int(ending)]
        await store.set("markers_pushed_state", state)
        # 页面上的「已写回 N 季」：按季 guid 去重累计
        stats = await store.get("markers_stats") or {}
        row = stats.get(str(int(library_id)))
        row = row if isinstance(row, dict) else {}
        seasons = set(row.get("seasons") or [])
        seasons.add(season_guid)
        stats[str(int(library_id))] = {"seasons": sorted(seasons), "at": _stamp()}
        await store.set("markers_stats", stats)
        log.info("飞牛片头片尾已写：%s（片头 %ds / 片尾 %ds）", label, opening, ending)
        return True

    async def on_segments_updated(payload: SegmentsUpdated) -> None:
        if not config.sync_markers:
            return
        media = payload.media
        season = int(payload.season)
        tmdb_id = getattr(media, "tmdb_id", None)
        title = str(getattr(media, "title", "") or "")
        if not tmdb_id:
            return
        try:
            libraries = await adopt_store.claimed_libraries(ops, store)
        except Exception as exc:  # noqa: BLE001 -- 读库失败就跳过这一轮
            log.debug("读库清单失败，片头片尾跳过：%s", exc)
            return
        trim = client_of(config)
        try:
            await trim.connect()
            for lib in libraries:
                if str(lib.get("kind") or "") != "tv" or not lib.get("fn_guid"):
                    continue
                index = await mk.season_index(trim, str(lib["fn_guid"]), log=log)
                season_guid = mk.find_season_guid(index, int(tmdb_id), season)
                if season_guid is None:
                    continue
                values = await mk.season_values(
                    ops,
                    library_id=int(lib["id"]),
                    tmdb_id=int(tmdb_id),
                    season=season,
                    log=log,
                )
                if values is None:
                    continue
                opening, ending = values
                await write_marker(
                    library_id=int(lib["id"]),
                    season_guid=season_guid,
                    opening=opening,
                    ending=ending,
                    label=f"《{title}》S{season:02d}",
                )
                break
        except TrimError as exc:
            log.debug("飞牛不可用，片头片尾写回跳过：%s", exc)
        finally:
            await trim.close()

    ctx.on(LIBRARY_SEGMENTS_UPDATED, on_segments_updated)

    # ---- 管理页接口 --------------------------------------------------------
    deps = Deps(
        config=config,
        store=store,
        ops=ops,
        jobs=jobs,
        files=files,
        log=log,
        runlog=runlog,
        job_type=SYNC_JOB_TYPE,
        member_state=member_state,
        member_refresh=refresh_member,
    )
    routes.mount(ctx, build_router(deps))

    log.info(
        "飞牛影视插件已加载（地址 %s，库 %s）",
        config.base_url or "未配置",
        "自动接入开" if config.auto_adopt else "自动接入关",
    )


def _stamp() -> str:
    import time

    return time.strftime("%Y-%m-%dT%H:%M:%S")
