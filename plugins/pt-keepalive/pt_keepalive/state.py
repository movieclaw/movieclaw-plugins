"""PLUGIN_DATA 状态机：每站最近一轮结果、连续失败计数、失效告警去重位。

单键 ``keepalive`` 存全部站点的汇总 JSON（远小于 64KB 上限）：
``{site_id: {outcome, detail, site_name, consecutive_failures, last_ok_at, alerted_expired,
auth_expired, retries}}``。
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
    retries: int = 0,
) -> dict[str, dict[str, Any]]:
    """按结果更新单站状态（纯函数：返回新表，不改原表）。

    - 成功 / 仅在线：清失败计数与告警位，记 last_ok_at；
    - 失败：计数 +1；凭据失效直接置阈值——失效必须让用户看见；
    - 需人工 / 跳过：只更新展示字段，不计数。

    ``alerted`` 是「这一轮失效告警是否确实发出去了」。失效时传 ``False`` 会清掉
    告警位，下一轮重试；传 ``None``（本轮与失效告警无关）保持原位。

    ``retries`` 是本轮临时错误重试轮数用尽仍未通过的次数（0 = 本轮没重试）：
    失败行要写「已自动重试 N 次」，跨轮的连续失败告警从状态里读，所以存下来；
    凭据失效位（``auth_expired``）同理——连续失败告警要靠它给「需要重新登录」的提示。
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
        entry.pop("failures_before_expired", None)
        entry.pop("auth_expired", None)
        entry.pop("retries", None)
    elif outcome == "failed":
        entry["auth_expired"] = auth_expired
        if retries:
            entry["retries"] = retries
        else:
            entry.pop("retries", None)
        if auth_expired:
            # 真实的连续轮数另存一份：置成阈值是为了让它立刻进告警，
            # 那个数不是真的（见 _failure_lines 为什么不能拿它写「连续 N 轮」）
            previous = int(entry.get("consecutive_failures") or 0)
            entry["failures_before_expired"] = previous + 1
            entry["consecutive_failures"] = FAILURE_THRESHOLD
            if alerted is True:
                entry["alerted_expired"] = True
            elif alerted is False:
                entry.pop("alerted_expired", None)
        else:
            # 失效那一轮把计数置成了阈值；从这里接着数真实轮数，别把它当成真的
            base = entry.pop("failures_before_expired", None)
            if base is None:
                base = int(entry.get("consecutive_failures") or 0)
            entry["consecutive_failures"] = base + 1
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
        lines.append(
            failure_line(
                name,
                entry.get("detail") or "",
                auth_expired=bool(entry.get("auth_expired")),
                retries=int(entry.get("retries") or 0),
                streak=int(entry.get("consecutive_failures") or 0),
            )
        )
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


# ---------------------------------------------------------------------------
# 失败原因的归类与报告措辞（issue #58：失败通知要短，并按错误类型说明可能原因）
# ---------------------------------------------------------------------------

#: 网络类错误在 detail 里的特征词 → 报告里的一句话短原因（按顺序取第一条命中）。
#: detail 由插件自己产出（判定层、宿主 409/503 的翻译都在本包内），措辞稳定，按词归类可靠；
#: 长异常原文（证书 / DNS / 代理细节）只留给 status 路由与日志。
_TRANSIENT_SHORT: tuple[tuple[str, str], ...] = (
    ("站点响应超时", "站点响应超时"),  # 宿主代请求超时（宿主统一回 503）
    ("响应超时", "响应超时"),  # 直连的读取 / 发送超时（M-Team）
    ("连接超时", "连接超时"),
    ("请求超时", "请求超时"),
    ("超时", "连接超时"),
    ("无法连接", "无法连接站点"),
    ("连接被站点重置", "连接被重置"),
    ("被中断", "连接被中断"),
    ("代理连接失败", "代理连接失败"),
    ("网络错误", "网络错误"),
)

#: detail 已把原因讲清、再补笼统提示反而画蛇添足的错误：签名轮换、限流、插件自身异常
_SELF_EXPLANATORY_MARKS = ("签名错误", "限流", "执行异常")

_HINT_TRANSIENT = "可能是临时网络波动，并非账号失效"
_HINT_AUTH = "需要重新登录"
_HINT_SITE = "站点暂时异常"

_UNEXPLAINED = "原因未说明"


def failure_cause(detail: str, *, auth_expired: bool = False) -> tuple[str, str]:
    """失败明细 → （短原因，可能原因提示）。

    提示按错误类型三选一：连接超时 / 无法连接 →「可能是临时网络波动，并非账号失效」；
    登录或 Cookie 失效 →「需要重新登录」；站点返回错误 →「站点暂时异常」。
    网络类只给短原因（截掉异常原文），其余原样；detail 已说明白的不再追加提示。
    """
    detail = (detail or "").strip()
    if auth_expired:
        return detail or _UNEXPLAINED, _HINT_AUTH
    if any(mark in detail for mark in _SELF_EXPLANATORY_MARKS):
        return detail or _UNEXPLAINED, ""
    for mark, short in _TRANSIENT_SHORT:
        if mark in detail:
            return short, _HINT_TRANSIENT
    return detail or _UNEXPLAINED, _HINT_SITE


def failure_line(
    site_name: str,
    detail: str,
    *,
    auth_expired: bool = False,
    retries: int = 0,
    streak: int = 0,
) -> str:
    """一条失败记录 → 报告里的一行（日报与连续失败告警共用，``streak`` 是连续失败轮数）。

    形如「M-Team 保活失败：连接超时，已自动重试 3 次，已连续 2 轮。可能是临时网络波动，
    并非账号失效」：先讲事实（哪个站、什么错、重试与连续轮数），再用一句话说可能原因，
    让用户一眼判断要不要重新登录（issue #58）。
    """
    reason, hint = failure_cause(detail, auth_expired=auth_expired)
    head = f"{site_name} 保活失败：{reason}"
    if retries:
        head += f"，已自动重试 {retries} 次"
    if streak > 1:
        head += f"，已连续 {streak} 轮"
    return f"{head}。{hint}" if hint else f"{head}。"
