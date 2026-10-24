"""PLUGIN_DATA 状态机：每站最近一轮结果、连续失败计数、失效告警去重位。

单键 ``keepalive`` 存全部站点的汇总 JSON（远小于 64KB 上限）：
``{site_id: {outcome, detail, site_name, consecutive_failures, last_ok_at, alerted_expired}}``。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

#: 连续失败多少轮才升级进日报失败明细（避免单次抖动刷屏）
FAILURE_THRESHOLD = 3

#: 状态机单键（全部站点汇总 JSON，存插件数据 global scope）
STATE_KEY = "keepalive"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def now_iso() -> str:
    """当前 UTC 时间的 ISO 串（runner 汇总用）。"""
    return _now_iso()


def normalize(raw: Any) -> dict[str, dict[str, Any]]:
    """插件数据里读出的旧值 → 规范状态表（坏数据当空表）。"""
    if not isinstance(raw, dict):
        return {}
    return {sid: dict(v) for sid, v in raw.items() if isinstance(v, dict)}


def record(
    states: dict[str, dict[str, Any]],
    site_id: str,
    outcome: str,
    detail: str,
    *,
    auth_expired: bool,
    site_name: str | None = None,
    alerted: bool | None = None,
) -> dict[str, dict[str, Any]]:
    """按结果更新单站状态（纯函数：返回新表，不改原表）。

    - 成功 / 仅在线：清失败计数与告警位，记 last_ok_at；
    - 失败：计数 +1；凭据失效直接置阈值——失效必须让用户看见；
    - 需人工 / 跳过：只更新展示字段，不计数。

    ``alerted`` 是「这一轮失效告警是否确实发出去了」。失效时传 ``False`` 会清掉
    告警位，下一轮重试；传 ``None``（本轮与失效告警无关）保持原位。
    """
    entry = dict(states.get(site_id, {}))
    entry["outcome"] = outcome
    entry["detail"] = detail
    entry["updated_at"] = _now_iso()
    if site_name:
        entry["site_name"] = site_name
    if outcome == "signin_ok" or outcome == "login_only":
        entry["consecutive_failures"] = 0
        entry["last_ok_at"] = _now_iso()
        entry.pop("alerted_expired", None)
        entry.pop("failure_alerted", None)
    elif outcome == "failed":
        if auth_expired:
            entry["consecutive_failures"] = FAILURE_THRESHOLD
            if alerted is True:
                entry["alerted_expired"] = True
            elif alerted is False:
                entry.pop("alerted_expired", None)
        else:
            entry["consecutive_failures"] = int(entry.get("consecutive_failures") or 0) + 1
    states_out = dict(states)
    states_out[site_id] = entry
    return states_out


def should_alert_expired(
    states: dict[str, dict[str, Any]], site_id: str, auth_expired: bool
) -> bool:
    """凭据失效是否要发告警事件：失效且尚未告警过（同一失效期只发一次）。"""
    if not auth_expired:
        return False
    return not states.get(site_id, {}).get("alerted_expired")


def failure_summary(
    states: dict[str, dict[str, Any]], *, pending_only: bool = False
) -> list[str]:
    """达到升级阈值的失败站点明细（进日报 / 失败告警）。

    凭据失效的站点不进这里：它有自己的「登录已失效」告警，重复一条没有信息量。
    ``pending_only=True`` 只返回还没发过失败告警的站点（避免每轮重复通知）。
    """
    lines = []
    for site_id, entry in sorted(states.items()):
        if int(entry.get("consecutive_failures") or 0) < FAILURE_THRESHOLD:
            continue
        if entry.get("alerted_expired"):
            continue
        if pending_only and entry.get("failure_alerted"):
            continue
        name = entry.get("site_name") or site_id
        lines.append(f"{name}：{entry.get('detail') or '连续失败'}")
    return lines


def mark_failure_alerted(states: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """把当前达到阈值、还没发过告警的站点标成已告警（本轮汇总发出去之后调）。"""
    out = dict(states)
    for site_id, entry in states.items():
        if int(entry.get("consecutive_failures") or 0) < FAILURE_THRESHOLD:
            continue
        if entry.get("alerted_expired") or entry.get("failure_alerted"):
            continue
        updated = dict(entry)
        updated["failure_alerted"] = True
        out[site_id] = updated
    return out
