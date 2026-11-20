"""库接入：发现飞牛上的媒体库并接成本地库（宿主入口，插件不写台账）。

按定稿设计（media-server-plugins.md §2.1）：

- **本机可见**（用户配了路径映射、目录在容器里真实存在）：``library.create`` 建普通
  本地库，根目录写挂载路径，``scrape_overrides`` 关掉镜像（不往飞牛的目录写 NFO/海报）。
  入账交给主线扫描——识别、刮削、探规格、缺失对账全部原生，飞牛挂了照常播；
- **没配映射 / 换算不出**的库不接，在清单里说明原因（一期只做本机可见；
  远程模式需要内核的只读 DriveBackend，内核还没有）。

建库走宿主操作而不是插件自己写库：名称/根路径校验、与「自动入库」目录的冲突
检查、内置合集初始化都由宿主统一做；失败按 ``OpsError.message_key`` 分支
（撞名、根重叠各有各的说法），不比较会随语言变的文案。
"""

from __future__ import annotations

import os
from typing import Any

from .trim_api import CATEGORY_MOVIE, CATEGORY_TV, TrimLibrary

#: 蓝光原盘的内部结构目录：里面的 .m2ts 是同一部片的分片，不是独立影片。
#: 飞牛常把整张原盘拷进来，数文件时会虚高；主线扫描自己也跳过这些。
SKIP_DIR_NAMES = frozenset({"@eaDir", "BDMV", "CERTIFICATE", "VIDEO_TS", "AUDIO_TS"})


def map_remote_to_local(path: str, frm: str, to: str) -> str | None:
    """飞牛侧目录 → 本机容器内路径；不在映射前缀下返回 None。

    两个前缀都得由用户自己配：只配一半等于没配，宁可返回 None 也不生成
    以根目录开头的半截路径。
    """
    raw = (path or "").strip()
    prefix = (frm or "").strip()
    base = (to or "").strip().rstrip("/")
    if not raw or not prefix or not base or not raw.startswith(prefix):
        return None
    # 前缀必须落在目录边界上：/mnt/media 不能吃掉 /mnt/media2（否则整库路径都错位）
    if len(raw) > len(prefix) and raw[len(prefix)] != "/" and not prefix.endswith("/"):
        return None
    tail = raw[len(prefix):].strip("/")
    return f"{base}/{tail}" if tail else base


def pick_roots(local_dirs: list[str]) -> list[str]:
    """同一个飞牛库的多条目录 → 去冗余后的根列表。

    父目录也在列表里时丢掉子目录（父已经覆盖它）：飞牛自身常把「分类目录」
    与「全部分类目录的父目录」一起挂在一个库里，照抄会被宿主的
    「库根不能嵌套」约束拒绝。
    """
    uniq = sorted({d.strip().rstrip("/") for d in local_dirs if d and d.strip()})
    keep: list[str] = []
    for d in uniq:
        if any(d != other and d.startswith(other + "/") for other in uniq):
            continue  # 被同库的父目录覆盖
        keep.append(d)
    return keep


def roots_cover(have: list[str], want: list[str]) -> bool:
    """``have`` 这些根是否把 ``want`` 全部收进来了（相等，或 ``want`` 在某个根之下）。"""
    roots = [str(h).rstrip("/") for h in have if h]
    return bool(roots) and all(
        any(w == r or w.startswith(r + "/") for r in roots) for w in want
    )


def roots_conflict(roots: list[str], occupied: list[tuple[str, str]]) -> tuple[str, str] | None:
    """库根与已占用根是否相同或嵌套（平台的硬约束）；冲突时返回（库名, 根）。"""
    for root in roots:
        r = root.rstrip("/")
        for name, other in occupied:
            s = other.rstrip("/")
            if r == s or r.startswith(s + "/") or s.startswith(r + "/"):
                return name, other
    return None


def walk_videos(root: str, exts: set[str]) -> list[str]:
    """列出一个根下的视频文件（跳过隐藏目录、系统目录与蓝光结构目录）。

    与主线扫描同一套扩展名口径；只读、不改动任何东西。
    """
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and d not in SKIP_DIR_NAMES]
        for name in filenames:
            if os.path.splitext(name)[1].lower() in exts:
                found.append(os.path.join(dirpath, name))
    return found


def plan_adopt(
    remote_libs: list[TrimLibrary],
    *,
    existing: list[dict[str, Any]],
    path_map_from: str,
    path_map_to: str,
    name_prefix: str,
    dir_exists: Any,
) -> list[dict[str, Any]]:
    """把飞牛库清单 → 接入计划（纯函数，不打接口不落库，单测直接覆盖）。

    ``existing`` 是宿主库行的精简视图：``[{name, source_config, root_paths}]``。
    返回每库一行：``{guid, name, kind, action, roots, local_dirs, detail}``：

    - ``action="adopt"``：可接入（roots 非空、不与已有库重叠）；
    - ``action="mapped"``：已经接过（按飞牛 library_guid 认）；
    - ``action="claim"``：本机已有库的根与它**完全相同**（比如从上一版插件沿用下来的库）——
      直接把那个库认领过来，不重复建、不重新扫描，条目与观看记录原样保留；
    - ``action="skip"``：不支持 / 换算不出本机路径 / 根重叠，``detail`` 说明原因。

    顺序按根路径长度升序（覆盖大的父目录先占位），后面的被完全覆盖时点名说明。
    """
    mapped_guids = {
        str((row.get("source_config") or {}).get("library_guid"))
        for row in existing
        if (row.get("source_config") or {}).get("library_guid")
    }
    occupied: list[tuple[str, str]] = [
        (str(row.get("name")), str(root))
        for row in existing
        for root in (row.get("root_paths") or [])
    ]
    names = {str(row.get("name")) for row in existing}

    pending: list[dict[str, Any]] = []
    for lib in remote_libs:
        if lib.guid in mapped_guids:
            row_existing = next(
                (r for r in existing
                 if str((r.get("source_config") or {}).get("library_guid")) == lib.guid),
                None,
            )
            pending.append({
                "guid": lib.guid, "name": lib.name,
                "kind": "movie" if lib.category == CATEGORY_MOVIE else "tv",
                "action": "mapped", "roots": [], "local_dirs": [],
                "detail": f"已接入为「{row_existing.get('name')}」" if row_existing else "已接入",
            })
            continue
        if lib.category not in (CATEGORY_MOVIE, CATEGORY_TV):
            pending.append({
                "guid": lib.guid, "name": lib.name, "kind": "", "action": "skip",
                "roots": [], "local_dirs": [],
                "detail": f"分类 {lib.category} 暂不支持（只支持电影 / 剧集库）",
            })
            continue
        local_dirs: list[str] = []
        for remote_dir in lib.dir_list:
            local = map_remote_to_local(remote_dir, path_map_from, path_map_to)
            if local and dir_exists(local):
                local_dirs.append(local)
        roots = pick_roots(local_dirs)
        if roots:
            # 本机已经有一个库把这些目录都收进来了（根相同，或它的根更大）：认领它，
            # 别重建——重建等于丢掉条目与观看记录，还要重新识别几千个文件。
            want = [str(r).rstrip("/") for r in roots]
            same = next(
                (
                    row
                    for row in existing
                    if not (row.get("source_config") or {}).get("library_guid")
                    and roots_cover(row.get("root_paths") or [], want)
                ),
                None,
            )
            if same is not None:
                have = {str(h).rstrip("/") for h in (same.get("root_paths") or []) if h}
                extra = "（它的目录更大，直接沿用）" if have - set(want) else ""
                pending.append({
                    "guid": lib.guid, "name": lib.name,
                    "kind": "movie" if lib.category == CATEGORY_MOVIE else "tv",
                    "action": "claim", "roots": roots, "local_dirs": local_dirs,
                    "library_id": int(same["id"]),
                    "library_name": str(same.get("name") or ""),
                    "detail": f"认领已有的库「{same.get('name')}」{extra}"
                              "（条目与观看记录保持不变）",
                })
                continue
        if not roots:
            if lib.dir_list:
                pending.append({
                    "guid": lib.guid, "name": lib.name,
                    "kind": "movie" if lib.category == CATEGORY_MOVIE else "tv",
                    "action": "skip", "roots": [], "local_dirs": local_dirs,
                    "detail": "目录换算不出本机路径（没配路径映射或容器里看不到），"
                              "一期只接本机可见的库",
                })
            else:
                pending.append({
                    "guid": lib.guid, "name": lib.name,
                    "kind": "movie" if lib.category == CATEGORY_MOVIE else "tv",
                    "action": "skip", "roots": [], "local_dirs": [],
                    "detail": "这个账号看不到库的实际目录（需要管理员账号）",
                })
            continue
        pending.append({
            "guid": lib.guid, "name": lib.name,
            "kind": "movie" if lib.category == CATEGORY_MOVIE else "tv",
            "action": "adopt", "roots": roots, "local_dirs": local_dirs, "detail": "",
        })

    # 父目录先建：根路径短的（覆盖大的）优先占位
    pending.sort(key=lambda p: min((len(r) for r in p["roots"]), default=0))
    for plan in pending:
        if plan["action"] != "adopt":
            continue
        conflict = roots_conflict(plan["roots"], occupied)
        if conflict is not None:
            plan["action"] = "skip"
            plan["detail"] = f"根 {plan['roots'][0]} 与「{conflict[0]}」的 {conflict[1]} 重叠：" \
                              "内容已在那个库里，不重复建"
            continue
        name = f"{name_prefix} · {plan['name']}" if name_prefix else plan["name"]
        candidate = name
        suffix = 2
        while candidate in names:
            candidate = f"{name} ({suffix})"
            suffix += 1
        plan["library_name"] = candidate
        names.add(candidate)
        occupied.extend((candidate, root) for root in plan["roots"])
    return pending


def adopt_source_config(plan: dict[str, Any], *, server_url: str, server_guid: str) -> dict[str, Any]:
    """接入计划 → 建库请求体（library.create 的 body）。"""
    return {
        "name": plan.get("library_name") or plan["name"],
        "kind": plan["kind"],
        "root_paths": list(plan["roots"]),
        "source_config": {
            "library_guid": plan["guid"],
            "library_name": plan["name"],
            "server_url": server_url,
            "server_guid": server_guid,
        },
        "realtime_watch": bool(plan["roots"]),
        "generate_thumbnails": bool(plan["roots"]),
        "scrape_overrides": {
            # 飞牛的目录是它自己的库目录：不往里面镜像 NFO / 海报 / 剧照，
            # 元数据由主线自己的刮削资产目录管（library.create 的表单同款字段）
            "mirror_images": False,
            "mirror_nfo": False,
            "mirror_episode_thumbs": False,
        },
    }


def explain_create_error(exc: Any) -> str:
    """建库失败 → 给人看的一句话（按 message_key 分支，不比较文案）。"""
    key = getattr(exc, "message_key", None) or ""
    params = getattr(exc, "params", None) or {}
    if key == "errors.library.nameInUse":
        return f"库名「{params.get('name')}」已被占用"
    if key in (
        "errors.library.rootOverlapsLibrary",
        "errors.library.rootOverlapsWatch",
        "errors.library.rootOverlapsTarget",
    ):
        return f"根目录与「{params.get('name')}」的重叠（平台不允许库根嵌套）：{params}"
    return str(exc)
