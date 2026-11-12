"""飞牛账号面板与库权限：把飞牛的账号信息搬进 MovieClaw 的管理页。

飞牛只有一套账号信息（``user/info`` / ``manager/user/list``），没有会员等级、
下载额度这类字段——页面只展示接口真给的东西。

写权限（``POST /manager/user/{guid}``）没有公开文档，请求体按读接口返回行的形状
镜像；写完**立刻重新拉一遍校验**，没生效就报错，不静默假成功。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from .trim_api import TrimAuthError, TrimClient, TrimError

#: 每个账号在卡片上最多列出的库标签数（再多页面会挤）
_LIB_TAGS = 24


def _stamp(value: Any) -> str | None:
    """飞牛的 last_login_time 是 unix 秒；0 表示从没登录过。"""
    try:
        seconds = int(value or 0)
    except (TypeError, ValueError):
        return None
    if seconds <= 0:
        return None
    # 带 +00:00：前端按本地时区换算，别退化成「8 小时前」
    return datetime.fromtimestamp(seconds, UTC).isoformat()


def _compact(row: dict[str, Any]) -> dict[str, Any]:
    libraries = row.get("mediadb_list") or []
    libs = libraries if isinstance(libraries, list) else []
    return {
        "guid": row.get("guid"),
        "username": row.get("username") or "",
        "is_admin": bool(row.get("is_admin")),
        "media_permission": row.get("media_permission"),
        "library_count": len(libs),
        "libraries": [
            {"guid": str(lib.get("guid") or ""), "title": str(lib.get("title") or "")}
            for lib in libs
            if isinstance(lib, dict)
        ][:_LIB_TAGS],
        "last_login_at": _stamp(row.get("last_login_time")),
        "sources": [
            str(src.get("source_name") or src.get("source") or "")
            for src in (row.get("sources") or [])
            if isinstance(src, dict)
        ],
    }


async def snapshot(trim: TrimClient) -> dict[str, Any]:
    """当前登录账号 + 全部账号；``users`` 为空说明登录账号不是管理员。"""
    info = await trim.user_info()
    users = await trim.users()
    by_guid = {str(row.get("guid")): row for row in users if row.get("guid")}
    current_row: dict[str, Any] = dict(by_guid.get(str(info.get("guid"))) or {})
    for key in ("guid", "username", "is_admin", "lan", "audio_lan", "sources"):
        if info.get(key) is not None:
            current_row[key] = info[key]
    return {
        "configured": True,
        "current": _compact(current_row) if current_row else None,
        "users": [_compact(row) for row in users],
        "is_admin": bool(info.get("is_admin")) if info else False,
    }


async def set_library_access(trim: TrimClient, body: dict[str, Any]) -> dict[str, Any]:
    """改某个账号可见的媒体库；返回生效后的库清单。

    ``body``: ``{guid, library_guids: [guid, ...]}``
    """
    guid = str(body.get("guid") or "")
    wanted = [str(g) for g in (body.get("library_guids") or [])]
    if not guid:
        raise ValueError("需要账号 guid")
    if not wanted:
        raise ValueError("至少要勾一个库（全不勾等于这个账号看不到任何内容）")

    info = await trim.user_info()
    if not bool(info.get("is_admin")):
        raise PermissionError("插件连接的飞牛账号不是管理员，改不了别人的库权限")

    all_libs = await trim.libraries()
    by_guid = {lib.guid: lib for lib in all_libs}
    chosen: list[dict[str, str]] = []
    for g in wanted:
        lib = by_guid.get(g)
        if lib is None:
            raise ValueError(f"库 {g} 不在飞牛上")
        chosen.append({"guid": lib.guid, "title": lib.name})

    users = await trim.users()
    target = next((u for u in users if str(u.get("guid")) == guid), None)
    if target is None:
        raise LookupError("飞牛上没有这个账号")
    # 原行整份带上，只覆盖 mediadb_list：这个接口没有文档，行里可能还有它需要的
    # 其他字段（如 fnOS 绑定 id），只回传挑选过的字段可能把没见过的字段清掉
    payload = dict(target)
    payload["guid"] = guid
    payload["mediadb_list"] = chosen
    await trim._request("POST", f"/manager/user/{guid}", data=payload, error_cls=TrimError)

    after = await trim.users()
    now = next((u for u in after if str(u.get("guid")) == guid), None)
    now_guids = {
        str(lib.get("guid") or "")
        for lib in ((now or {}).get("mediadb_list") or [])
        if isinstance(lib, dict)
    }
    if not set(wanted).issubset(now_guids):
        raise RuntimeError(
            "写入了但飞牛没生效（这个服务版本的接口行为可能不同），"
            "请到飞牛影视的账号管理里手动改"
        )
    return {
        "guid": guid,
        "library_guids": sorted(now_guids),
        "libraries": sorted(lib.name for g, lib in by_guid.items() if g in now_guids),
    }


__all__ = ["snapshot", "set_library_access", "TrimAuthError", "TrimError"]
