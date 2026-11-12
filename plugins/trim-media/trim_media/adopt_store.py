"""库接入的落地部分：飞牛库清单 → 接入计划 → 建本地库 → 记账。

和 ``adopt.py`` 的分工：那个是纯函数（映射、去冗余、冲突判断，可单测）；
这里碰宿主操作与插件数据，供自动同步任务与管理页共用**同一套**判定，
避免「自动接入」和「手动接入」走出两种结果。

认领关系记在插件自己的数据里（``ledger``）：宿主的库行不回 ``source_config``，
进程外插件也无法登记外部库源。记录形状：

    {"<飞牛库 guid>": {"id": 12, "name": "电影", "kind": "movie", "at": "..."}}

兼容早期只存 id 的写法（``{"<guid>": 12}``）。
"""

from __future__ import annotations

import os
import time
from typing import Any

from .adopt import adopt_source_config, explain_create_error, plan_adopt


#: 建出来的库名统一加这个前缀，和 MC 自己的库区分开
NAME_PREFIX = "飞牛"


def _stamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


async def load_ledger(store: Any) -> dict[str, dict[str, Any]]:
    """读认领台账，把早期「guid → id」的形状升级成完整记录。"""
    raw = await store.get("ledger") or {}
    out: dict[str, dict[str, Any]] = {}
    if not isinstance(raw, dict):
        return out
    for guid, value in raw.items():
        if isinstance(value, dict) and value.get("id"):
            out[str(guid)] = dict(value)
        elif isinstance(value, int):
            out[str(guid)] = {"id": int(value), "name": "", "kind": "", "at": ""}
    return out


async def save_ledger(store: Any, ledger: dict[str, dict[str, Any]]) -> None:
    await store.set("ledger", ledger)


def guid_by_library_id(ledger: dict[str, dict[str, Any]]) -> dict[int, str]:
    return {int(row["id"]): guid for guid, row in ledger.items() if row.get("id")}


async def existing_with_ledger(ops: Any, store: Any) -> list[dict[str, Any]]:
    """宿主库行 + 插件认领台账：已接入的行补上 ``source_config.library_guid``，
    让 ``plan_adopt`` 按同一口径把认过领的库标成 mapped。"""
    rows = await ops.call("library.list", {"scope": "all"})
    ledger = await load_ledger(store)
    by_id = guid_by_library_id(ledger)
    existing: list[dict[str, Any]] = []
    for row in rows or []:
        item = {k: row[k] for k in ("name", "root_paths", "id", "kind", "stats") if k in row}
        guid = by_id.get(int(row["id"])) if isinstance(row.get("id"), int) else None
        if guid is not None:
            item["source_config"] = {"library_guid": guid}
        existing.append(item)
    return existing


async def claimed_libraries(ops: Any, store: Any) -> list[dict[str, Any]]:
    """插件认领的本地库：宿主库行 + 飞牛库 guid / 飞牛库名 / 库存统计。

    宿主库行被删（用户在网页里“移除库”）时台账里的那条自动失效，不报错。
    """
    rows = await ops.call("library.list", {"scope": "all"}) or []
    ledger = await load_ledger(store)
    by_id = guid_by_library_id(ledger)
    out: list[dict[str, Any]] = []
    for row in rows:
        library_id = row.get("id")
        if not isinstance(library_id, int):
            continue
        guid = by_id.get(int(library_id))
        if guid is None:
            continue
        meta = ledger.get(guid) or {}
        stats = row.get("stats") or {}
        out.append(
            {
                "id": int(library_id),
                "name": str(row.get("name") or ""),
                "kind": str(row.get("kind") or ""),
                "fn_guid": guid,
                "fn_name": str(meta.get("name") or ""),
                "root_paths": list(row.get("root_paths") or []),
                "stats": {
                    "item_count": int(stats.get("item_count") or 0),
                    "file_count": int(stats.get("file_count") or 0),
                    "missing_count": int(stats.get("missing_count") or 0),
                    "unidentified_count": int(stats.get("unidentified_count") or 0),
                },
                "last_scan_at": ((row.get("last_scan") or {}).get("finished_at")),
            }
        )
    return out


async def build_plans(
    ops: Any, store: Any, config: Any, *, dir_exists: Any = os.path.isdir
) -> list[dict[str, Any]]:
    """飞牛库清单 + 接入判定（自动同步与「接入预览」共用）。"""
    from .config import client_of

    trim = client_of(config)
    try:
        await trim.connect()
        remote = await trim.libraries()
    finally:
        await trim.close()
    return plan_adopt(
        remote,
        existing=await existing_with_ledger(ops, store),
        path_map_from=config.path_map_from,
        path_map_to=config.path_map_to,
        name_prefix=NAME_PREFIX,
        dir_exists=dir_exists,
    )


async def adopt_one(
    ops: Any,
    store: Any,
    plan: dict[str, Any],
    *,
    server_url: str,
    server_guid: str,
    name: str | None = None,
    scan: bool = True,
) -> dict[str, Any]:
    """按一条计划建库并记账；返回 ``{id, name}``。失败抛 ``OpsError``。"""
    body = adopt_source_config(plan, server_url=server_url, server_guid=server_guid)
    # 库就是普通本地库：source_config 主线不收（来源未登记会 400），认领关系记插件台账
    body.pop("source_config", None)
    if name:
        body["name"] = name
    created = await ops.call("library.create", {"body": body})
    library_id = int(created["id"])
    ledger = await load_ledger(store)
    ledger[str(plan["guid"])] = {
        "id": library_id,
        "name": str(plan.get("name") or ""),
        "kind": str(plan.get("kind") or ""),
        "at": _stamp(),
    }
    await save_ledger(store, ledger)
    if scan:
        try:
            await ops.call("library.scan.start", {"library_id": library_id})
        except Exception as exc:  # noqa: BLE001 -- 409 = 建库即扫描，不用再排
            if getattr(exc, "status", None) != 409:
                raise
    return {"id": library_id, "name": str(body["name"])}


async def run_sync(
    ops: Any, store: Any, config: Any, *, trim: Any, progress: Any = None
) -> dict[str, Any]:
    """一轮自动接入：发现飞牛上的新库，接成本地库并扫描（**只加不删**）。

    飞牛上删了库，本地库与文件不动——兜底库的意义就在这里。
    """
    result: dict[str, Any] = {"adopted": [], "scanned": 0, "errors": []}
    if not config.auto_adopt:
        result["skipped"] = "自动接入已关闭"
        return result
    remote = await trim.libraries()
    plans = plan_adopt(
        remote,
        existing=await existing_with_ledger(ops, store),
        path_map_from=config.path_map_from,
        path_map_to=config.path_map_to,
        name_prefix=NAME_PREFIX,
        dir_exists=os.path.isdir,
    )
    versions = dict(getattr(trim, "versions", None) or {})
    if versions:
        await store.set("trim_versions", versions)
    for plan in plans:
        if plan["action"] != "adopt":
            continue
        try:
            created = await adopt_one(
                ops,
                store,
                plan,
                server_url=config.base_url.rstrip("/"),
                server_guid=str(await store.get("server_guid") or "trim"),
            )
        except Exception as exc:  # noqa: BLE001 -- 单个库失败不影响其他
            result["errors"].append(f"建库「{plan['name']}」失败：{explain_create_error(exc)}")
            continue
        result["adopted"].append(
            {"guid": plan["guid"], "name": created["name"], "id": created["id"]}
        )
        result["scanned"] += 1
    return result
