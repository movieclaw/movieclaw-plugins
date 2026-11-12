"""会话管家：动态自动压缩 AI 会话上下文，持续节省 token。

原理（全部用开放契约，不碰主程序内部）：
- 可靠订阅 `agent.run.finished`——每轮对话结束（回答完或出错）都会广播；
- 调宿主操作 `session.get-transcript`（view=chat，轻量）统计自上次压缩以来的
  用户消息数与最近一次 assistant 回复记录的 prompt_tokens；
- 超过阈值就调 `session.compact-context`：保留预算内的用户原话 + 生成交接摘要，
  完整轨迹不删，后续请求从压缩历史继续——这就是「动态省钱」。

触发规则（满足其一即压，且都要过冷却）：
- 消息数：自上次压缩起累计的用户消息 ≥ max_messages（默认 16）；
- token：最近一次回复记录的 prompt_tokens ≥ max_prompt_tokens（默认 60000）；
  prompt_tokens 是每次请求的全量输入（历史 + 本轮），它降不下来上下文就一直贵，
  所以它是「该压了」的最直接信号。

保护机制：
- 冷却：同会话两次压缩至少间隔 cooldown_minutes（默认 10 分钟）；
- 正在运行的会话不压（compact-context 会拒绝，跳过等下一轮）；
- 压缩本身是一次模型调用（约等于一次普通请求的成本），只在「继续聊会贵得多」时触发；
- 幂等：按事件 id 去重，重复投递不会压两次。
"""

import time

from pydantic import BaseModel, Field, SecretStr  # noqa: F401  (SecretStr 占位说明可用)

from movieclaw_api.plugins.keys import PLUGIN_DATA
from movieclaw_kernel import DURABLE_EVENTS, Context, plugin
from movieclaw_sdk.events import AGENT_RUN_FINISHED, RunFinished
from movieclaw_sdk.ops import HOST_OPS, OpsError


class Settings(BaseModel):
    """全部阈值可调；留空用默认值。"""

    max_messages: int = Field(
        16, ge=4, le=200,
        title="消息数阈值",
        description="自上次压缩起累计的用户消息达到这个数就自动压缩。默认 16。",
    )
    max_prompt_tokens: int = Field(
        60000, ge=8000, le=1000000,
        title="输入 token 阈值",
        description="最近一次请求的输入 token（全量上下文）达到这个数就自动压缩。默认 60000。",
    )
    cooldown_minutes: int = Field(
        10, ge=1, le=1440,
        title="冷却分钟",
        description="同一会话两次压缩的最小间隔。默认 10 分钟。",
    )
    exclude_sessions: list[str] = Field(
        default_factory=list,
        title="排除的会话 ID",
        description="这些会话不自动压缩（一行一个会话 ID）。",
    )


PLUGIN_ID = "session-keeper"
COOLDOWN_KEY = "last_compact_at"


# ---------- 纯函数：判断该不该压（独立自测用） ----------

def should_compact(
    *, messages_since: int, last_prompt_tokens: int | None,
    has_compaction: bool, max_messages: int, max_prompt_tokens: int,
) -> tuple[bool, str]:
    """返回 (是否压缩, 原因)。

    - messages_since：自上次压缩（或会话开头）以来的用户消息数；
    - last_prompt_tokens：最近一次 assistant 记录的 prompt_tokens（可能为 None，
      比如还没跑完一轮、或记录缺失）；
    - has_compaction：轨迹里是否已有压缩记录（决定消息数的起点，不影响判断本身）。
    """
    if messages_since >= max_messages:
        return True, f"累计 {messages_since} 条消息 ≥ 阈值 {max_messages}"
    if last_prompt_tokens is not None and last_prompt_tokens >= max_prompt_tokens:
        return True, f"输入 {last_prompt_tokens} token ≥ 阈值 {max_prompt_tokens}"
    return False, (
        f"未达阈值（消息 {messages_since}/{max_messages}，"
        f"输入 {last_prompt_tokens if last_prompt_tokens is not None else '未知'} token）"
    )


def in_cooldown(last_ts: float | None, cooldown_minutes: int, now: float) -> bool:
    if last_ts is None:
        return False
    return (now - last_ts) < cooldown_minutes * 60


@plugin(
    PLUGIN_ID,
    title="会话管家（自动压缩）",
    inject=(DURABLE_EVENTS, PLUGIN_DATA, HOST_OPS),
    permissions=("session.get-transcript", "session.compact-context"),
    config=Settings,
)
async def apply(ctx: Context[Settings]) -> None:
    store = ctx.use(PLUGIN_DATA).scoped(ctx)
    ops = await ctx.use(HOST_OPS).client(ctx)

    ctx.logger.info("%s 已加载：消息阈值 %d，token 阈值 %d，冷却 %d 分钟",
                    PLUGIN_ID, ctx.config.max_messages, ctx.config.max_prompt_tokens,
                    ctx.config.cooldown_minutes)

    async def on_run_finished(event: RunFinished) -> None:
        # 幂等：同一事件可能投递两次
        event_id = ctx.delivery.event_id if ctx.delivery else None
        seen = await store.get("seen_events", default=[])
        if event_id and event_id in seen:
            return
        if event_id:
            await store.set("seen_events", [event_id, *seen][:300])

        session_id = event.session_id
        if session_id in ctx.config.exclude_sessions:
            return

        # 冷却检查
        key = f"{COOLDOWN_KEY}:{session_id}"
        if in_cooldown(await store.get(key), ctx.config.cooldown_minutes, time.time()):
            ctx.logger.info("会话 %s 在冷却期内，跳过", session_id)
            return

        # 读轻量轨迹：统计自上次压缩以来的用户消息数 + 最近一次输入 token
        try:
            data = await ops.call("session.get-transcript",
                                  {"session_id": session_id, "view": "chat"})
        except OpsError as exc:
            ctx.logger.warning("读会话 %s 轨迹失败：%s", session_id, exc.message)
            return

        entries = data.get("entries") or []
        messages_since = 0
        last_prompt_tokens = None
        has_compaction = False
        for entry in reversed(entries):          # 从最新往回扫
            etype = entry.get("type")
            if etype == "compaction" and not has_compaction:
                has_compaction = True            # 遇到第一条压缩记录就停（它是分界线）
                continue
            if etype != "message":
                continue
            msg = entry.get("message") or {}
            if msg.get("role") == "user":
                messages_since += 1
            elif msg.get("role") == "assistant":
                usage = entry.get("usage") or {}
                if last_prompt_tokens is None and usage.get("prompt_tokens"):
                    last_prompt_tokens = usage["prompt_tokens"]
            if has_compaction and messages_since >= 1 and last_prompt_tokens is not None:
                break                            # 两个信号都拿到了

        do, reason = should_compact(
            messages_since=messages_since,
            last_prompt_tokens=last_prompt_tokens,
            has_compaction=has_compaction,
            max_messages=ctx.config.max_messages,
            max_prompt_tokens=ctx.config.max_prompt_tokens,
        )
        if not do:
            ctx.logger.info("会话 %s：%s，不压缩", session_id, reason)
            return

        # 执行压缩
        try:
            result = await ops.call("session.compact-context", {"session_id": session_id})
            before = result.get("tokens_before")
            after = result.get("tokens_after")
            ctx.logger.info("会话 %s 已自动压缩（%s）：%s → %s token",
                            session_id, reason, before, after)
            await store.set(key, time.time())
        except OpsError as exc:
            # 运行中的会话会拒绝；跳过，下一轮结束再试
            ctx.logger.info("会话 %s 压缩未执行：%s", session_id, exc.message)
        except Exception as exc:  # noqa: BLE001
            ctx.logger.warning("会话 %s 压缩异常：%s", session_id, exc)

    ctx.on(AGENT_RUN_FINISHED, on_run_finished, id="auto-compact", durable=True)
