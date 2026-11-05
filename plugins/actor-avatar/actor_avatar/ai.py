"""用 MovieClaw 内置的 AI 助手给配不上的演员推荐别名。

走插件 SDK 的 ``AGENT`` 服务（权限 ``agent:none``，安装时要用户批准）：只发「演员名 + 几部片的
番号 / 片名」，要它回一串可能的别名。**它说的不算**：每个候选都要再走一遍普通匹配、在 gfriends 里
查得到才收下，而且只进「待确认」，由用户点确认才会写。

护栏：每天上限（设置里改，0 = 不用）、同一位演员 30 天内不重复问、串行 + 两次之间至少隔几秒、
单轮超时；每次的 token 用量记在插件数据里，页面上能看到。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from movieclaw_sdk.agent import AgentBusy, AgentNotGranted, AgentUnavailable

from .junk import junk_reason
from .matching import Avatar, AvatarIndex, fold

USAGE_KEY = "ai:usage"
TRIED_KEY = "ai:tried"
#: 每轮自动运行最多问几位
PER_RUN = 5
COOLDOWN_DAYS = 30
MIN_GAP_S = 3.0
TIMEOUT_S = 120.0
MAX_CANDIDATES = 8
_TRIED_LIMIT = 500
_TOKEN_KEYS = ("prompt_tokens", "completion_tokens", "total_tokens", "cache_read_tokens")

INSTRUCTIONS = (
    "你在帮一个媒体库插件找日本成人影片女演员的其他艺名。只凭你已有的知识回答，不要调用任何工具，"
    "不要解释。输出必须是一个 JSON 字符串数组，不确定就输出 []。"
)


class AiError(Exception):
    """AI 推荐用不了（没授权 / 没配置模型 / 正忙 / 到了上限）；消息给页面直接显示。"""


def build_prompt(actor: str, titles: list[str]) -> str:
    lines = [f"演员名（来自刮削结果）：{actor}"]
    shown = [t.strip()[:80] for t in titles if t and t.strip()][:3]
    if shown:
        lines.append("她出演的片子（番号 / 片名）：")
        lines += [f"- {t}" for t in shown]
    lines.append(
        "请列出这位演员可能的其他名字：改名前后的艺名、日文原名、常见的汉字 / 假名写法。"
        f"最多 {MAX_CANDIDATES - 2} 个，不要包含上面这个名字本身。"
        '只输出 JSON 字符串数组，例如 ["名字1", "名字2"]；不知道就输出 []。'
    )
    return "\n".join(lines)


def parse_candidates(text: str) -> list[str]:
    """从回复里抠出 JSON 数组；容忍代码块、前后废话。"""
    if not text:
        return []
    found: list[str] = []
    for block in re.findall(r"\[[^\[\]]*\]", text):
        try:
            value = json.loads(block)
        except ValueError:
            continue
        if isinstance(value, list):
            found += [str(v).strip() for v in value if isinstance(v, str | int)]
            break
    out: list[str] = []
    for name in found:
        if name and len(name) <= 40 and name not in out:
            out.append(name)
    return out[:MAX_CANDIDATES]


def _today() -> str:
    return datetime.now(UTC).astimezone().date().isoformat()


class AiAdvisor:
    def __init__(
        self,
        *,
        agent: Any,
        store: Any,
        daily_limit: int,
        sleep: Callable[[float], Any] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
        today: Callable[[], str] = _today,
        logger: Any = None,
    ) -> None:
        self.agent = agent
        self.store = store
        self.daily_limit = daily_limit
        self.sleep = sleep
        self.clock = clock
        self.today = today
        self.logger = logger
        self._lock = asyncio.Lock()
        self._last = -1e9

    @property
    def enabled(self) -> bool:
        return self.agent is not None and self.daily_limit > 0

    async def _usage(self) -> dict[str, Any]:
        usage = await self.store.get(USAGE_KEY) or {}
        if usage.get("day") != self.today():
            usage = {
                "day": self.today(),
                "calls": 0,
                "tokens": {},
                "total": usage.get("total") or {"calls": 0, "tokens": {}},
                "last_error": usage.get("last_error"),
            }
        return usage

    async def status(self) -> dict[str, Any]:
        usage = await self._usage()
        reason = None
        if self.agent is None:
            reason = "这个 MovieClaw 版本没有给插件开放 AI 助手"
        elif self.daily_limit <= 0:
            reason = "设置里把每日上限设成了 0"
        return {
            "available": self.enabled,
            "reason": reason,
            "daily_limit": self.daily_limit,
            "used_today": usage["calls"],
            "remaining_today": max(0, self.daily_limit - usage["calls"]),
            "tokens_today": usage["tokens"],
            "total": usage["total"],
            "last_error": usage.get("last_error"),
        }

    async def recently_tried(self, actor: str) -> bool:
        tried = await self.store.get(TRIED_KEY) or {}
        entry = tried.get(actor)
        if not entry:
            return False
        try:
            at = datetime.fromisoformat(entry["at"])
        except (KeyError, ValueError):
            return False
        return datetime.now(UTC) - at < timedelta(days=COOLDOWN_DAYS)

    async def suggest(
        self,
        actor: str,
        titles: list[str],
        index: AvatarIndex,
        table: Any = None,
        *,
        exclude: set[str] | None = None,
    ) -> dict[str, Any]:
        """问一次；返回 ``{asked, accepted: [Avatar…], usage}``。用不了抛 :class:`AiError`。"""
        if self.agent is None:
            raise AiError("这个 MovieClaw 版本没有给插件开放 AI 助手")
        if self.daily_limit <= 0:
            raise AiError("AI 推荐已关闭（每日上限为 0）")
        async with self._lock:
            usage = await self._usage()
            if usage["calls"] >= self.daily_limit:
                raise AiError(
                    f"今天的 AI 推荐已用满 {self.daily_limit} 次，明天再试或在设置里调高上限"
                )
            wait = self._last + MIN_GAP_S - self.clock()
            if wait > 0:
                await self.sleep(wait)
            self._last = self.clock()
            reply = None
            error: str | None = None
            try:
                reply = await self.agent.ask(
                    build_prompt(actor, titles),
                    timeout=TIMEOUT_S,
                    tools="none",
                    instructions=INSTRUCTIONS,
                )
            except AgentNotGranted as exc:
                error = f"没有调用 AI 助手的授权（安装时要批准 agent:none）：{exc}"
            except AgentUnavailable as exc:
                error = f"MovieClaw 还没配置可用的模型：{exc}"
            except AgentBusy:
                error = "AI 助手正忙（本插件同时最多 2 轮），稍后再试"
            if error is None and reply is not None and reply.status != "done":
                error = f"AI 助手没答完（{reply.status}）：{(reply.error or '')[:200]}"
            tokens = {
                k: int(v)
                for k, v in ((getattr(reply, "usage", None) or {}).items())
                if k in _TOKEN_KEYS and isinstance(v, int)
            }
            if reply is not None:
                # 用量只要发出去了就记（失败的也计费）
                usage["calls"] += 1
                usage["total"]["calls"] = int(usage["total"].get("calls") or 0) + 1
                for k, v in tokens.items():
                    usage["tokens"][k] = int(usage["tokens"].get(k) or 0) + v
                    usage["total"]["tokens"][k] = int(usage["total"]["tokens"].get(k) or 0) + v
            usage["last_error"] = (
                {"at": datetime.now(UTC).isoformat(timespec="seconds"), "message": error}
                if error
                else None
            )
            await self.store.set(USAGE_KEY, usage)
            if reply is not None and getattr(reply, "session_id", None):
                # 会话归属 plugin:actor-avatar，问完就删，免得把 AI 助手的会话列表塞满
                try:
                    await self.agent.delete(reply.session_id)
                except Exception as exc:  # noqa: BLE001 - 清理失败不影响结果
                    if self.logger is not None:
                        self.logger.debug("actor-avatar：删除 AI 会话失败：%s", exc)
            if error is not None:
                raise AiError(error)

        asked = parse_candidates(reply.text or "")
        skip = {fold(actor)} | {fold(x) for x in (exclude or ())}
        accepted: list[Avatar] = []
        checked: list[dict[str, Any]] = []
        for name in asked:
            if junk_reason(name):
                checked.append({"name": name, "hit": None, "why": "不像名字"})
                continue
            match = index.lookup(name, None, table)
            if match is None:
                checked.append({"name": name, "hit": None, "why": "gfriends 里没有"})
                continue
            if fold(match.avatar.name) in skip or match.avatar in accepted:
                checked.append({"name": name, "hit": match.avatar.name, "why": "重复"})
                continue
            accepted.append(match.avatar)
            checked.append({"name": name, "hit": match.avatar.name, "why": match.how})
        await self._remember(actor, asked, [a.name for a in accepted])
        return {"actor": actor, "asked": checked, "accepted": accepted, "usage": tokens}

    async def _remember(self, actor: str, asked: list[str], accepted: list[str]) -> None:
        tried = await self.store.get(TRIED_KEY) or {}
        tried[actor] = {
            "at": datetime.now(UTC).isoformat(timespec="seconds"),
            "asked": asked[:MAX_CANDIDATES],
            "accepted": accepted,
        }
        while len(tried) > _TRIED_LIMIT:
            tried.pop(next(iter(tried)))
        await self.store.set(TRIED_KEY, tried)
