"""三态结果模型与证据判定（纯函数，单测重点）。

「保活」语境下要区分四件事（社区共识，见 MoviePilot autosignin / fqm）：
签到成功（拿到每日奖励）、登录在线（会话有效，签到未确认或未做）、失败、
跳过（站点没启用 / 类型未适配，本轮没有访问）。

**登录在线不许冒充签到成功**；未知响应一律保守判「仅在线」。
**没访问就不许冒充失败**——未适配的站点记跳过，不进失败统计。

判定纪律（全部纯函数可测）：
1. 先剥 ``<script>/<style>``、隐藏元素再匹配文本——防模板字符串冒充页面提示；
2. 登录失效证据优先于一切成功词：出现密码输入框即判失效；
3. Cloudflare 拦截特征只看标题与可见文本（不看脚本），判「需人工」；
4. 明确的否定提示（如「回答错误」）优先于成功关键词；
5. 「今天已签到」等幂等提示视为签到成功。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from parsel import Selector


class Outcome(str, Enum):
    """单站点一轮保活的结果（三态 + 需人工 + 跳过）。"""

    SIGNIN_OK = "signin_ok"  # 签到成功（含「今天已签」幂等）
    LOGIN_ONLY = "login_only"  # 登录有效，签到未确认 / 未做
    FAILED = "failed"  # 请求失败或证据不明
    NEED_HUMAN = "need_human"  # 需要人工处理（CF 拦截 / 验证码 / 答题），不算失败
    SKIPPED = "skipped"  # 本轮没访问（站点未启用 / 类型未适配），不算失败


@dataclass
class SiteResult:
    """一轮对单个站点的判定结果。"""

    outcome: Outcome
    detail: str = ""  # 人话，进日志 / 日报 / 通知
    auth_expired: bool = False  # True → 触发凭据失效告警
    need_human: bool = field(default=False)  # 冗余位，方便调用方过滤


# ---------------------------------------------------------------------------
# 证据关键词（NexusPHP 系；来自 MoviePilot autosignin / fqm 的判据收敛）
# ---------------------------------------------------------------------------

#: 签到成功：获得奖励的提示
SIGNIN_SUCCESS_WORDS = (
    "签到成功",
    "獲得.*魔力",
    "获得.*魔力",
    "签到已得",
    "本次签到获得",
    "已连续签到",
    "这是您的第.*次签到",
)

#: 签到幂等：今天已经签过
SIGNIN_ALREADY_WORDS = (
    "今天已经签到",
    "已签到过",
    "请勿重复刷新",
    "今日已签",
)

#: 仍然可以签到（页面还有签到按钮 = 没签上）
SIGNIN_BUTTON_WORDS = (
    "立即签到",
    "点击签到",
    "开始签到",
)

#: 签到失败 / 否定提示（优先于成功词）
SIGNIN_NEGATIVE_WORDS = (
    "回答错误",
    "签到失败",
    "無法簽到",
    "无法签到",
)

#: 登录失效：页面出现登录表单 / 密码框
LOGIN_FORM_CSS = (
    "input[type='password']",
    "form input[name='password']",
)

#: 登录在线：页面出现登出链接（NexusPHP 标准）
LOGOUT_LINK_CSS = (
    "a[href*='logout']",
    "a[href*='logout.php']",
)

#: Cloudflare / WAF 拦截特征（不区分大小写）
CHALLENGE_PATTERNS = (
    re.compile(r"just a moment", re.I),
    re.compile(r"请稍候.{0,20}(再试|访问)", re.I),
    re.compile(r"ddos[- ]guard", re.I),
    re.compile(r"cf[- ]challenge", re.I),
    re.compile(r"checking your browser", re.I),
    re.compile(r"enable javascript and cookies", re.I),
)


def visible_text(html: str) -> str:
    """剥掉 script/style/noscript 与隐藏元素后的可见文本（判定的统一输入）。"""
    doc = Selector(text=html)
    for node in doc.css("script, style, noscript"):
        node.drop()
    hidden = [
        node
        for node in doc.css(
            "[hidden], [aria-hidden='true'], [style*='display:none'], [style*='display: none']"
        )
        if node.root is not None
    ]
    for node in hidden:
        node.drop()
    return " ".join(doc.xpath("//text()").getall())


def _matches(text: str, patterns: tuple) -> str | None:
    """返回第一处命中的片段；用于 detail 里给出证据。元素可传字符串或已编译正则。"""
    for pat in patterns:
        if isinstance(pat, re.Pattern):
            m = pat.search(text)
        else:
            m = re.search(pat, text, re.I)
        if m:
            return m.group(0)
    return None


def has_login_form(html: str) -> bool:
    """页面是否出现密码输入框（= 会话已失效，被重定向到登录页）。"""
    doc = Selector(text=html)
    return any(doc.css(css).get() for css in LOGIN_FORM_CSS)


def has_logout_link(html: str) -> bool:
    """页面是否出现登出链接（= 登录在线）。"""
    doc = Selector(text=html)
    return any(doc.css(css).get() for css in LOGOUT_LINK_CSS)


def is_challenge_page(html: str) -> bool:
    """是否 Cloudflare / WAF 拦截页。

    只看标题与**可见**文本：站点自己的 JS 里出现 "just a moment" 之类的字符串
    不能当成拦截证据（与关键词判定同一条纪律）。
    """
    title_m = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    if title_m and _matches(title_m.group(1), CHALLENGE_PATTERNS):
        return True
    return _matches(visible_text(html), CHALLENGE_PATTERNS) is not None


def _status_code(response: dict[str, Any]) -> int:
    """宿主回传的 HTTP 状态码；假响应 / 缺字段按 200。"""
    try:
        return int(response.get("status_code") or 200)
    except (TypeError, ValueError):
        return 200


# ---------------------------------------------------------------------------
# 判定入口
# ---------------------------------------------------------------------------


def judge_nexusphp_page(
    html: str, *, want_signin: bool, status_code: int = 200, login_confirm: bool = True
) -> SiteResult:
    """NexusPHP 页面响应 → 结果。

    ``want_signin=False``（用户关了签到）时只判登录态：有登出链接即在线。
    ``login_confirm=False`` 用于签到页这类没有导航栏的页面：不要求出现登出链接，
    只有密码框才判失效（签到页本身不提供「已登录」证据）。
    """
    if status_code >= 500:
        return SiteResult(Outcome.FAILED, f"站点返回 HTTP {status_code}，服务端故障")
    if is_challenge_page(html):
        return SiteResult(
            Outcome.NEED_HUMAN,
            "站点开启了人机验证（Cloudflare 等），需要人工访问",
            need_human=True,
        )
    if status_code in (401, 403):
        # 没有 challenge 特征也被拒：多为 WAF / IP 限制。判「需人工」不给用户
        # 发失效告警——一条 403 不足以断定凭据失效。
        return SiteResult(
            Outcome.NEED_HUMAN,
            f"站点拒绝访问（HTTP {status_code}），可能被 WAF 拦截，需要人工访问",
            need_human=True,
        )
    if has_login_form(html):
        return SiteResult(Outcome.FAILED, "页面出现登录表单，凭据已失效", auth_expired=True)
    text = visible_text(html)
    if login_confirm and not has_logout_link(html) and not _matches(text, ("退出", "登出", "注销")):
        return SiteResult(Outcome.FAILED, "页面未见登录证据，无法确认会话有效")
    if not want_signin:
        return SiteResult(Outcome.LOGIN_ONLY, "登录在线")
    if (neg := _matches(text, SIGNIN_NEGATIVE_WORDS)) is not None:
        return SiteResult(Outcome.FAILED, f"签到被站点拒绝：{neg}")
    if _matches(text, SIGNIN_SUCCESS_WORDS):
        return SiteResult(Outcome.SIGNIN_OK, "签到成功")
    if _matches(text, SIGNIN_ALREADY_WORDS):
        return SiteResult(Outcome.SIGNIN_OK, "今天已签到（幂等成功）")
    if _matches(text, SIGNIN_BUTTON_WORDS):
        # 还挂着签到按钮且没有成功提示 = 未确认；登录是有效的
        return SiteResult(Outcome.LOGIN_ONLY, "登录在线，签到未确认（页面仍显示签到按钮）")
    return SiteResult(Outcome.LOGIN_ONLY, "登录在线，签到未确认")
