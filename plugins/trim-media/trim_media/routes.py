"""管理页后端：状态、库接入、覆盖检查、片头片尾、飞牛账号、运行日志。

页面（``web/``）是同源的一页，所有数据都从这里的路由拿；插件自己的能力只有这些，
所以前端要的字段在这里一次成型，页面不做二次拼装。

边界：这里不写 MovieClaw 的数据库——库经 ``library.create`` 由宿主建，
片头片尾经 ``library.items.segments`` 读，账号与片头片尾的写入只打飞牛接口。
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any

from fastapi import APIRouter, HTTPException

from . import adopt_store
from . import markers as mk
from .accounts import set_library_access
from .accounts import snapshot as account_snapshot
from .adopt import SKIP_DIR_NAMES, explain_create_error, plan_adopt, roots_conflict
from .config import Config, client_of
from .runlog import RunLog
from .sync import push_markers
from .trim_api import TrimAuthError, TrimError

PLUGIN_ID = "trim-media"

#: 数文件覆盖时认的视频扩展名（与主线扫描同一套口径）
VIDEO_EXTS = frozenset({".mkv", ".mp4", ".avi", ".ts", ".m2ts", ".wmv", ".mov", ".iso"})
#: 单库最多数这么多文件就停（再大的库不该靠页面点一下去全量遍历）
COVERAGE_MAX_FILES = 50000
#: 总览缓存时长（秒）
OVERVIEW_TTL = 300.0
#: 账号快照缓存时长（秒）
ACCOUNT_TTL = 300.0


class Deps:
    """路由依赖：入口模块把当前生效的配置、服务和状态容器交进来。"""

    def __init__(
        self,
        *,
        config: Config,
        store: Any,
        ops: Any,
        jobs: Any,
        files: Any,
        log: Any,
        runlog: RunLog,
        job_type: str,
        member_state: dict[str, Any],
        member_refresh: Any,
    ) -> None:
        self.config = config
        self.store = store
        self.ops = ops
        self.jobs = jobs
        self.files = files
        self.log = log
        self.runlog = runlog
        self.job_type = job_type
        self.member_state = member_state
        self.member_refresh = member_refresh


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _age(stamp: str | None) -> float:
    if not stamp:
        return 1e9
    try:
        parsed = time.mktime(time.strptime(stamp, "%Y-%m-%dT%H:%M:%S"))
    except (TypeError, ValueError):
        return 1e9
    return time.time() - parsed


async def _claimed_libraries(deps: Deps) -> list[dict[str, Any]]:
    """插件认领的本地库（实现见 adopt_store，自动同步与页面共用）。"""
    return await adopt_store.claimed_libraries(deps.ops, deps.store)


async def _marker_stats(deps: Deps) -> dict[str, dict[str, Any]]:
    return await deps.store.get("markers_stats") or {}


# ---------------------------------------------------------------- 总览数据
async def _marker_overview(deps: Deps, *, refresh: bool) -> dict[str, Any]:
    cached = await deps.store.get("markers_overview")
    if (
        not refresh
        and isinstance(cached, dict)
        and cached.get("seasons") is not None
        and _age(cached.get("checked_at")) < OVERVIEW_TTL
    ):
        return {**cached, "from_cache": True}

    config = deps.config
    libraries = await _claimed_libraries(deps)
    trim = client_of(config)
    try:
        await trim.ensure_login()
        payload = await mk.overview(
            trim,
            deps.ops,
            libraries=libraries,
            library_id=None,
            log=deps.log,
        )
    except TrimAuthError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"飞牛登录失败（{exc}）。偶发时点「刷新对比」重试；"
            "持续出现请到设置里重新保存账号密码",
        ) from exc
    except TrimError as exc:
        raise HTTPException(status_code=502, detail=f"连不上飞牛：{exc}") from exc
    finally:
        await trim.close()
    await deps.store.set("markers_overview", payload)
    return {**payload, "from_cache": False}


async def _season_guid_of(deps: Deps, library_id: int, tmdb_id: int, season: int) -> str:
    """(库, TMDB, 季) → 飞牛季 guid；找不到就是飞牛里没这一季。"""
    libraries = {int(lib["id"]): lib for lib in await _claimed_libraries(deps)}
    lib = libraries.get(int(library_id))
    if lib is None:
        raise HTTPException(status_code=404, detail="这个库不是插件接入的")
    guid = str(lib.get("fn_guid") or "")
    if not guid:
        raise HTTPException(status_code=409, detail="这个库没有对应的飞牛库")
    trim = client_of(deps.config)
    try:
        await trim.ensure_login()
        index = await mk.season_index(trim, guid, log=deps.log)
    except TrimError as exc:
        raise HTTPException(status_code=502, detail=f"连不上飞牛：{exc}") from exc
    finally:
        await trim.close()
    season_guid = mk.find_season_guid(index, tmdb_id, season)
    if season_guid is None:
        raise HTTPException(status_code=404, detail="飞牛里没有这一季（可能没接入这个库）")
    return season_guid


async def _record_marker_write(deps: Deps, *, library_id: int, season_guid: str) -> None:
    stats = await _marker_stats(deps)
    key = str(int(library_id))
    row = stats.get(key) if isinstance(stats.get(key), dict) else {}
    seasons = set(row.get("seasons") or [])
    seasons.add(season_guid)
    stats[key] = {"seasons": sorted(seasons), "at": _now()}
    await deps.store.set("markers_stats", stats)


# ---------------------------------------------------------------- 数文件
async def _walk_videos(files: Any, root: str, out: list[str], budget: int) -> None:
    """用插件的文件接口递归数视频（受批准路径约束，不能越权读别的目录）。"""
    try:
        names = await files.listdir(root)
    except (PermissionError, FileNotFoundError, NotADirectoryError):
        return
    for name in names:
        if len(out) >= budget:
            return
        if name.startswith(".") or name in SKIP_DIR_NAMES:
            continue
        path = f"{root.rstrip('/')}/{name}"
        try:
            stat = await files.stat(path)
        except PermissionError:
            continue
        if stat is None:
            continue
        if stat.get("is_dir"):
            await _walk_videos(files, path, out, budget)
        elif os.path.splitext(name)[1].lower() in VIDEO_EXTS:
            out.append(path)


async def _coverage(deps: Deps, lib: dict[str, Any]) -> dict[str, Any] | None:
    roots = [r for r in (lib.get("root_paths") or []) if r]
    if not roots:
        return None
    found: list[str] = []
    for root in roots:
        await _walk_videos(deps.files, root, found, COVERAGE_MAX_FILES)
    total = len(found)
    covered = min(total, int((lib.get("stats") or {}).get("file_count") or 0))
    uncovered = max(0, total - covered)
    # 未覆盖文件按目录归并，页面点开看是哪几个子目录
    buckets: dict[str, int] = {}
    for path in found:
        parent = os.path.dirname(path)
        buckets[parent] = buckets.get(parent, 0) + 1
    top = sorted(buckets.items(), key=lambda kv: kv[1], reverse=True)[:12]
    return {
        "library": lib.get("name") or "",
        "library_id": lib.get("id"),
        "files": total,
        "covered": covered,
        "uncovered": uncovered,
        "truncated": total >= COVERAGE_MAX_FILES,
        "top_dirs": [{"dir": d, "files": n} for d, n in top],
        "local_roots": roots,
        "at": _now(),
    }


# ---------------------------------------------------------------- 路由
def build_router(deps: Deps) -> APIRouter:
    router = APIRouter()
    config = deps.config
    ops = deps.ops
    store = deps.store
    log = deps.log

    # ---- 状态 ------------------------------------------------------------
    @router.get(
        "/status",
        operation_id=f"plugins.{PLUGIN_ID}.status",
        summary="运行状态：连接、版本、已接入的库、片头片尾写回数与最近同步",
    )
    async def status() -> dict:
        libraries = await _claimed_libraries(deps)
        stats = await _marker_stats(deps)
        coverage_cache: dict[int, Any] = {}
        for lib in libraries:
            coverage_cache[int(lib["id"])] = await store.get(f"coverage:{lib['id']}")
        out_libs = []
        for lib in libraries:
            row = stats.get(str(lib["id"])) or {}
            out_libs.append(
                {
                    **lib,
                    "source_configured": True,
                    "last_sync_at": lib.get("last_scan_at"),
                    "markers_pushed": len(row.get("seasons") or []),
                    "markers_at": row.get("at"),
                    "coverage": coverage_cache.get(int(lib["id"])),
                }
            )
        last_sync = await store.get("last_sync_result") or {}
        return {
            "configured": config.configured,
            "base_url": config.base_url,
            "versions": await store.get("trim_versions") or {},
            "member_username": config.member_username.strip(),
            "member_resolved": deps.member_state.get("id"),
            "verify_tls": config.verify_tls,
            "auto_adopt": config.auto_adopt,
            "sync_progress": config.sync_progress,
            "sync_markers": config.sync_markers,
            "last_sync_at": last_sync.get("at"),
            "last_sync_result": last_sync,
            "libraries": out_libs,
        }

    # ---- 连接与同步 -------------------------------------------------------
    @router.post(
        "/test",
        operation_id=f"plugins.{PLUGIN_ID}.test",
        summary="测试连接：登录飞牛并返回服务版本",
    )
    async def test() -> dict:
        trim = client_of(config)
        try:
            info = await trim.test()
            versions = dict(trim.versions or {})
            if versions:
                await store.set("trim_versions", versions)
            if isinstance(info, dict) and info.get("server_guid"):
                await store.set("server_guid", str(info["server_guid"]))
            return {
                "ok": True,
                "versions": versions,
                "server_name": (info or {}).get("server_name"),
                "libraries": (info or {}).get("libraries") or [],
            }
        except TrimError as exc:
            return {"ok": False, "error": str(exc)}
        finally:
            await trim.close()

    @router.post(
        "/sync-now",
        operation_id=f"plugins.{PLUGIN_ID}.sync-now",
        summary="立即同步一轮（接入新库并触发扫描）",
    )
    async def sync_now() -> dict:
        ref = await deps.jobs.create(deps.job_type, input={}, dedupe_key="manual")
        return {"created": ref.created, "job_id": ref.id}

    # ---- 库接入 ----------------------------------------------------------
    @router.get(
        "/libraries",
        operation_id=f"plugins.{PLUGIN_ID}.libraries",
        summary="飞牛侧媒体库清单：目录、能否接入、是否已接入",
    )
    async def libraries() -> list[dict]:
        trim = client_of(config)
        try:
            await trim.ensure_login()
            remote = await trim.libraries()
        except TrimError as exc:
            raise HTTPException(status_code=502, detail=f"连不上飞牛：{exc}") from exc
        finally:
            await trim.close()
        plans = plan_adopt(
            remote,
            existing=await adopt_store.existing_with_ledger(ops, store),
            path_map_from=config.path_map_from,
            path_map_to=config.path_map_to,
            name_prefix=adopt_store.NAME_PREFIX,
            dir_exists=os.path.isdir,
        )
        by_guid = {lib.guid: lib for lib in remote}
        out: list[dict] = []
        for plan in plans:
            lib = by_guid.get(plan["guid"])
            overlap = "重叠" in str(plan.get("detail") or "")
            out.append(
                {
                    "guid": plan["guid"],
                    "name": plan.get("name") or "",
                    "category": lib.category if lib else "",
                    "dirs": list(lib.dir_list) if lib else [],
                    "kind": plan.get("kind") or "",
                    "supported": plan["action"] != "skip",
                    "overlap": overlap,
                    "claimable": plan["action"] == "claim",
                    "imported": plan["action"] in ("mapped", "claim"),
                    "local_roots": plan.get("roots") or [],
                    "detail": plan.get("detail") or "",
                }
            )
        return out

    @router.get(
        "/plan",
        operation_id=f"plugins.{PLUGIN_ID}.plan",
        summary="接入预览（自动接入同一套判定）",
    )
    async def plan() -> dict:
        try:
            plans = await adopt_store.build_plans(ops, store, config)
            return {"plans": plans}
        except TrimError as exc:
            return {"plans": [], "error": str(exc)}

    @router.post(
        "/create-library",
        operation_id=f"plugins.{PLUGIN_ID}.create-library",
        summary="把指定的飞牛库接成本地媒体库",
    )
    async def create_library(body: dict) -> dict:
        guid = str(body.get("library_guid") or "").strip()
        if not guid:
            raise HTTPException(status_code=400, detail="需要 library_guid")
        trim = client_of(config)
        try:
            await trim.ensure_login()
            remote = await trim.libraries()
        except TrimError as exc:
            raise HTTPException(status_code=502, detail=f"连不上飞牛：{exc}") from exc
        finally:
            await trim.close()
        plans = plan_adopt(
            remote,
            existing=await adopt_store.existing_with_ledger(ops, store),
            path_map_from=config.path_map_from,
            path_map_to=config.path_map_to,
            name_prefix=adopt_store.NAME_PREFIX,
            dir_exists=os.path.isdir,
        )
        plan = next((p for p in plans if p["guid"] == guid), None)
        if plan is None:
            raise HTTPException(status_code=404, detail="飞牛上没有这个库")
        if plan["action"] == "mapped":
            raise HTTPException(status_code=409, detail=plan.get("detail") or "已经接入过")
        if plan["action"] not in ("adopt", "claim"):
            raise HTTPException(status_code=400, detail=plan.get("detail") or "这个库不能接入")

        # 手动接入可以指定显示名与本机路径（本机路径必须真实存在）
        name = str(body.get("name") or "").strip() or plan.get("library_name") or plan["name"]
        local_root = str(body.get("local_root") or "").strip().rstrip("/")
        if local_root:
            if not await asyncio.to_thread(os.path.isdir, local_root):
                raise HTTPException(status_code=400, detail=f"本机路径不存在：{local_root}")
            plan = {**plan, "roots": [local_root]}
        existing = await adopt_store.existing_with_ledger(ops, store)
        if plan["action"] == "claim":
            # 认领已有库：不改名、不改根，直接记进台账
            try:
                claimed = await adopt_store.adopt_one(
                    ops,
                    store,
                    plan,
                    server_url=config.base_url.rstrip("/"),
                    server_guid=str(await store.get("server_guid") or "trim"),
                    scan=False,
                )
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            log.info("认领已有库「%s」为飞牛库 %s", claimed["name"], plan["name"])
            return {
                "library_id": claimed["id"],
                "name": claimed["name"],
                "claimed": True,
                "created": False,
            }
        if any(str(row.get("name")) == name for row in existing):
            raise HTTPException(status_code=409, detail=f"库名「{name}」已被占用")
        conflict = roots_conflict(
            plan["roots"],
            [(str(row.get("name")), str(root)) for row in existing for root in (row.get("root_paths") or [])],
        )
        if conflict is not None:
            raise HTTPException(
                status_code=409,
                detail=f"目录与「{conflict[0]}」的 {conflict[1]} 重叠，平台不允许库根嵌套",
            )
        try:
            created = await adopt_store.adopt_one(
                ops,
                store,
                plan,
                server_url=config.base_url.rstrip("/"),
                server_guid=str(await store.get("server_guid") or "trim"),
                name=name,
            )
        except TrimError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 -- 宿主操作用户可见的失败原因
            raise HTTPException(status_code=400, detail=explain_create_error(exc)) from exc
        ref = await deps.jobs.create(deps.job_type, input={}, dedupe_key="manual")
        log.info("飞牛库「%s」已接入为本地库「%s」（#%s）", plan["name"], name, created["id"])
        return {"library_id": created["id"], "name": name, "sync_job": ref.id, "created": ref.created}

    # ---- 覆盖检查 --------------------------------------------------------
    @router.get(
        "/check-coverage",
        operation_id=f"plugins.{PLUGIN_ID}.check-coverage",
        summary="检查库范围覆盖：本机目录下有多少视频文件，MC 台账收了哪些",
    )
    async def check_coverage(library_id: int | None = None) -> dict:
        libs = await _claimed_libraries(deps)
        out: list[dict] = []
        for lib in libs:
            if library_id is not None and int(lib["id"]) != int(library_id):
                continue
            data = await _coverage(deps, lib)
            if data is None:
                continue
            await store.set(f"coverage:{lib['id']}", data)
            out.append(data)
        return {"libraries": out}

    # ---- 片头片尾 --------------------------------------------------------
    @router.get(
        "/markers",
        operation_id=f"plugins.{PLUGIN_ID}.markers-overview",
        summary="片头片尾总览：MC 识别值 × 飞牛当前值（按季）",
    )
    async def markers_overview(refresh: bool = False) -> dict:
        return await _marker_overview(deps, refresh=refresh)

    @router.post(
        "/markers/push",
        operation_id=f"plugins.{PLUGIN_ID}.markers-push",
        summary="把 MC 识别的片头片尾写进飞牛（可全量、可指定某季）",
    )
    async def markers_push(body: dict) -> dict:
        payload = await _marker_overview(deps, refresh=False)
        rows = payload.get("seasons") or []
        library_id = body.get("library_id")
        tmdb_id = body.get("tmdb_id")
        season = body.get("season")
        targets = []
        labels: dict[str, tuple[int, str]] = {}
        for row in rows:
            if library_id is not None and int(row["library_id"]) != int(library_id):
                continue
            if tmdb_id is not None and int(row["tmdb_id"]) != int(tmdb_id):
                continue
            if season is not None and int(row["season"]) != int(season):
                continue
            if row.get("mc_opening") is None or row.get("state") == "same":
                continue
            guid = str(row.get("season_guid") or "")
            if not guid:
                continue
            targets.append(
                (guid, int(row["mc_opening"]), int(row.get("mc_ending") or 0),
                 f"{row.get('title')} S{int(row['season']):02d}")
            )
            labels[guid] = (int(row["library_id"]), f"{row.get('title')} S{int(row['season']):02d}")
        if not targets:
            return {"written": 0, "skipped": 0, "failed": 0}
        state = await store.get("markers_pushed_state") or {}
        if not isinstance(state, dict):
            state = {}
        trim = client_of(config)
        try:
            await trim.ensure_login()
            result = await push_markers(trim, rows=targets, state=state, log=log)
        except TrimError as exc:
            raise HTTPException(status_code=502, detail=f"连不上飞牛：{exc}") from exc
        finally:
            await trim.close()
        await store.set("markers_pushed_state", state)
        for guid, (lib_id, _label) in labels.items():
            if guid in state:
                await _record_marker_write(deps, library_id=lib_id, season_guid=guid)
        await store.delete("markers_overview")
        return result

    @router.get(
        "/markers/season",
        operation_id=f"plugins.{PLUGIN_ID}.markers-season-get",
        summary="读飞牛上某一季当前的片头片尾秒数",
    )
    async def markers_season_get(library_id: int, tmdb_id: int, season: int) -> dict:
        season_guid = await _season_guid_of(deps, library_id, tmdb_id, season)
        trim = client_of(config)
        try:
            await trim.ensure_login()
            return await mk.read_season(trim, season_guid)
        except TrimError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        finally:
            await trim.close()

    @router.post(
        "/markers/season",
        operation_id=f"plugins.{PLUGIN_ID}.markers-season-set",
        summary="直接改飞牛上某一季的片头片尾秒数（reset=清除）",
    )
    async def markers_season_set(body: dict) -> dict:
        try:
            library_id = int(body["library_id"])
            tmdb_id = int(body["tmdb_id"])
            season = int(body["season"])
        except (KeyError, TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="需要 library_id、tmdb_id、season") from exc
        season_guid = await _season_guid_of(deps, library_id, tmdb_id, season)
        trim = client_of(config)
        try:
            await trim.ensure_login()
            current = await mk.read_season(trim, season_guid)
            if body.get("reset"):
                # 飞牛把 0 当「未设置」保留旧值，写 1 秒顶掉旧配置（实际等于不跳）
                opening = ending = 1
            else:
                opening = body.get("opening")
                ending = body.get("ending")
                opening = int(current.get("skip_opening") or 0) if opening is None else int(opening)
                ending = int(current.get("skip_ending") or 0) if ending is None else int(ending)
            result = await mk.write_season(
                trim, season_guid, opening=opening, ending=ending
            )
        except TrimError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        finally:
            await trim.close()
        await _record_marker_write(deps, library_id=library_id, season_guid=season_guid)
        await store.delete("markers_overview")
        return result

    # ---- 飞牛账号 --------------------------------------------------------
    @router.get(
        "/account",
        operation_id=f"plugins.{PLUGIN_ID}.account",
        summary="飞牛账号：当前登录账号、账号列表与可见库",
    )
    async def account(refresh: bool = False) -> dict:
        cached = await store.get("account:snapshot")
        if not refresh and isinstance(cached, dict) and cached.get("current"):
            if _age(cached.get("at")) < ACCOUNT_TTL:
                return cached
        trim = client_of(config)
        try:
            await trim.ensure_login()
            data = await account_snapshot(trim)
        except TrimError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        finally:
            await trim.close()
        payload = {**data, "at": _now()}
        await store.set("account:snapshot", payload)
        return payload

    @router.post(
        "/account/library-access",
        operation_id=f"plugins.{PLUGIN_ID}.account-library-access",
        summary="设置某个飞牛账号可见的媒体库（需要插件账号是飞牛管理员）",
    )
    async def account_library_access(body: dict) -> dict:
        trim = client_of(config)
        try:
            await trim.ensure_login()
            result = await set_library_access(trim, body)
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (ValueError, RuntimeError) as exc:
            status = 502 if isinstance(exc, RuntimeError) else 400
            raise HTTPException(status_code=status, detail=str(exc)) from exc
        except TrimError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        finally:
            await trim.close()
        await store.delete("account:snapshot")
        log.info("飞牛账号库权限已更新：%s（%d 个库）", result["guid"], len(result["library_guids"]))
        return result

    # ---- 运行日志 --------------------------------------------------------
    @router.get(
        "/runtime-logs",
        operation_id=f"plugins.{PLUGIN_ID}.runtime-logs",
        summary="插件运行日志（页面每 3 秒增量拉取）",
    )
    async def runtime_logs(limit: int = 200, since: int = 0) -> dict:
        items = deps.runlog.tail(limit, since=since)
        return {"items": items, "count": len(items)}

    # ---- 差异检查 --------------------------------------------------------
    @router.post(
        "/align",
        operation_id=f"plugins.{PLUGIN_ID}.align",
        summary="差异检查：飞牛里有哪些条目、MC 库里有没有（只读，不改数据）",
    )
    async def align() -> dict:
        """两侧按 TMDB 编号对账，列出「飞牛有、MC 没有」和反过来的条目。

        飞牛那边可能放着还没接入的库、或库里新增但 MC 还没扫到的文件，
        跑一遍就知道缺什么；不改任何数据。
        """
        libs = await _claimed_libraries(deps)
        trim = client_of(config)
        report: list[dict[str, Any]] = []
        try:
            await trim.ensure_login()
            for lib in libs:
                guid = str(lib.get("fn_guid") or "")
                if not guid:
                    continue
                try:
                    remote = await trim.all_items(guid)
                except TrimError as exc:
                    report.append({"library": lib["name"], "error": str(exc)})
                    continue
                remote_tmdb = {
                    int(item.tmdb_id): (item.title or "")
                    for item in remote
                    if item.tmdb_id
                }
                local = await mk._all_items(ops, int(lib["id"]))
                local_tmdb = {
                    int(item["tmdb_id"]): str(item.get("title") or "")
                    for item in local
                    if item.get("tmdb_id")
                }
                missing_in_mc = [
                    {"tmdb_id": k, "title": v}
                    for k, v in sorted(remote_tmdb.items())
                    if k not in local_tmdb
                ][:100]
                missing_in_trim = [
                    {"tmdb_id": k, "title": v}
                    for k, v in sorted(local_tmdb.items())
                    if k not in remote_tmdb
                ][:100]
                report.append(
                    {
                        "library": lib["name"],
                        "library_id": lib["id"],
                        "trim_entries": len(remote_tmdb),
                        "mc_entries": len(local_tmdb),
                        "missing_in_mc": missing_in_mc,
                        "missing_in_trim": missing_in_trim,
                        "at": _now(),
                    }
                )
        except TrimError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        finally:
            await trim.close()
        return {"libraries": report}

    return router


__all__ = ["Deps", "build_router"]
