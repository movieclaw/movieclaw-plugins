"""记忆与上下文管家：AI 助手的跨会话记忆（Engramory 协议）+ 会话自动压缩，二合一。

原理（全部开放契约，不碰主程序内部）：
- 可靠订阅 agent.run.finished：每轮对话结束广播一次，只读一次会话轨迹，
  同时驱动两件事——
  ① 自动压缩（原 session-keeper）：自上次压缩起累计的用户消息数或最近一次
  请求的输入 token 达到阈值就调 session.compact-context 压缩上下文（只压不删，
  完整轨迹保留），后续请求从压缩历史继续——这是「动态省 token」的主开关；
  ② 记忆提炼：把新增对话交给提取模型沉淀长期记忆；压缩发生的轮次强制提炼
  一次（无视最少新消息数），赶在上下文被压缩前把值得长期记住的事实存进
  记忆库，交接摘要留给下一轮的续接同步兜底（Engramory §5）；
- 按会话水位线（上次提炼的时间点）找出新增对话，交给提取模型提炼 0～8 条
  值得长期记住的事实；检测到会话里新出现压缩摘要时额外触发「续接同步」提炼，
  接住交接摘要里的进行中任务（Engramory §5：压缩前同步）；
- 提取结果先在本地查重（字符 bigram 相似度，无模型调用），再按类写入
  插件数据（加密存储），每条是一篇「笔记」：名字 + 正文（feedback/project
  带为什么/怎么用）+ 类型，对应 Engramory 的一文件一事实；
- 记忆分两层：活跃笔记进「有界索引」（行数/字节双上限，写前拦截，超限先
  归档或合并，Engramory §6 anti-bloat），冷笔记归档出索引、留库可恢复；
- AI 助手开局查索引（只拿指针），要细节按 id 取，关键词检索直接命中全文
  （Engramory §4 回忆协议：索引每读、细节按需）；
- 左侧栏网页提供浏览 / 新增 / 编辑 / 置顶 / 归档 / 删除 + 体检看板 + 压缩管家
  （开关与阈值、压缩历史、手动压缩）。

为什么省 token：对话历史每轮全文重读，成本随轮数线性膨胀；记忆库把「必须
长期记住的东西」搬出对话流，开局只带一份有界索引（常数级、几百字节），
细节按需单取，把 O(n) 历史重读变成常数级的按需查询；自动压缩再把压不掉的
近期上下文定期折成交接摘要，让每次请求的输入 token 回落到低位。
"""

import json
import re
import time
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException
from movieclaw_sdk import net
from pydantic import BaseModel, Field, SecretStr

from movieclaw_api.plugins.keys import PLUGIN_DATA, PLUGIN_ROUTES
from movieclaw_kernel import DURABLE_EVENTS, Context, plugin
from movieclaw_sdk.events import AGENT_RUN_FINISHED, RunFinished
from movieclaw_sdk.ops import HOST_OPS, OpsError

PLUGIN_ID = "mc-memory"
STICKY_TTL = 24 * 3600        # 粘住切换的有效期：过了就重新按配置顺序探测，首选恢复即切回
COMPACT_LOG_MAX = 50          # 压缩历史最多保留条数
COOLDOWN_KEY = "last_compact_at"   # 按会话记上次压缩时间（冷却用）

CATEGORIES = ("iron", "user", "feedback", "project", "reference")
IRON_LINE_PREFIX = "⚖ "      # 铁律在索引行前的标记：让 AI 一眼看出这是凌驾一切的硬边界
MAX_ACTIVE = 100            # 活跃记忆上限（= 索引硬上限行数：活跃记忆必须装得进有界索引）
INDEX_SOFT_LINES = 80       # 索引软上限（行）：超过提醒归档/合并
INDEX_SOFT_BYTES = 8000     # 索引软上限（字节）
INDEX_HARD_LINES = 100      # 索引硬上限（行）：让索引变大并超限的写入被拒
INDEX_HARD_BYTES = 10000
# 单条字数：人工写入不设硬限（接口只留 HARD_CHARS 兼底，防单条撑爆存储）；
# AI 自动提炼引导写短（EXTRACT_TEXT_MAX，防止模型沉淀时写失控）；
# 超过 NOTE_WARN_CHARS 的笔记体检只提醒拆分，不拦截
HARD_CHARS = 20000         # 接口兼底：超过拒收（这么长请拆多条）
EXTRACT_TEXT_MAX = 600     # AI 自动提炼单条软上限
NOTE_WARN_CHARS = 480      # 超过则体检提醒拆分（只提醒不拦截）
NAME_MAX = 24               # 笔记名长度上限（索引指针里的 handle）
HOOK_MAX = 48               # 索引行钩子长度上限
EXTRACT_MAX = 8             # 单次最多提取条数
HISTORY_MAX_CHARS = 24000   # 提炼输入的字符上限（约 6k token）
ASSISTANT_SNIPPET = 400     # 提炼输入里 assistant 回答的截取长度
COMPACT_SNIPPET = 1200      # 续接同步时压缩摘要的截取长度
# 与主线的压缩摘要前缀同源（movieclaw_agent.prompts.SUMMARY_PREFIX）：插件依赖边界
# 够不到主线内部，只能自带前缀做 startswith 匹配，主线改文案时需同步这里
COMPACTION_PREFIX = "【上下文压缩摘要】"
INDEX_HEADER = "# 记忆索引\n> 只列指针；细节按 id 取，别在这里堆内容。\n"
NAME_BAD_CHARS = re.compile(r'[\s#|()?\[\]/\\:*"<>`*_]+')
WHY_HOW_RE = re.compile(r"^\*{0,2}(为什么|怎么用|Why|How to apply)\s*[：:]?\*{0,2}", re.I)


class Backend(BaseModel):
    """一个 OpenAI 兼容的提取后端（内部表示，从槽位设置里组装）。"""

    name: str
    base_url: str
    api_key: SecretStr
    model: str


class Settings(BaseModel):
    """提取后端按槽位顺序组成「主力→兜底」链：额度用尽/鉴权失效自动切下一个；
    瞬时抖动只绕过不切换；只填一个槽位就是单后端。空槽位不启用。"""

    auto_compact: bool = Field(
        True, title="自动压缩",
        description="每轮对话结束后检查会话长度，达到阈值就自动压缩上下文，持续节省 token。",
    )
    max_messages: int = Field(
        32, ge=4, le=200,
        title="消息数阈值",
        description="自上次压缩起累计的用户消息达到这个数就自动压缩。默认 32。",
    )
    max_prompt_tokens: int = Field(
        40000, ge=8000, le=1000000,
        title="输入 token 阈值",
        description="最近一次请求的输入 token（全量上下文）达到这个数就自动压缩。默认 40000。",
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
    auto_extract: bool = Field(
        True, title="自动提炼",
        description="会话结束后自动提炼记忆；关闭后只保留手动增删。",
    )
    sync_on_compaction: bool = Field(
        True, title="压缩前同步",
        description="检测到会话新出现压缩摘要时，先按续接同步的规则提炼一次，"
                    "接住交接摘要里的进行中任务（Engramory §5）。",
    )
    min_new_messages: int = Field(
        2, ge=1, le=50,
        title="最少新消息数",
        description="会话本轮新增的用户消息达到这个数才触发提炼，避免每轮都调模型。",
    )
    slot1_name: str = Field("", title="后端 1 名称", description="如「智谱 GLM」；留空则叫「后端 1」。")
    slot1_base: str = Field("", title="后端 1 接口地址", description="OpenAI 兼容地址，如 https://open.bigmodel.cn/api/coding/paas/v4 或 https://api.deepseek.com")
    slot1_key: SecretStr = Field(default=SecretStr(""), title="后端 1 API Key")
    slot1_model: str = Field("", title="后端 1 模型名", description="如 glm-5.3-flash（智谱 GLM Coding Lite 订阅）")
    slot2_name: str = Field("", title="后端 2 名称", description="如「DeepSeek」；不用兜底就留空。")
    slot2_base: str = Field("", title="后端 2 接口地址", description="如 https://api.deepseek.com")
    slot2_key: SecretStr = Field(default=SecretStr(""), title="后端 2 API Key")
    slot2_model: str = Field("", title="后端 2 模型名", description="如 deepseek-chat")
    slot3_name: str = Field("", title="后端 3 名称", description="预留第三家（如中转站）；不用留空。")
    slot3_base: str = Field("", title="后端 3 接口地址", description="OpenAI 兼容地址")
    slot3_key: SecretStr = Field(default=SecretStr(""), title="后端 3 API Key")
    slot3_model: str = Field("", title="后端 3 模型名", description="模型名")


# ---------- 纯函数：解析 / 查重 / 打分（独立自测用） ----------

def parse_extraction(raw: str) -> list[dict[str, str]]:
    """模型返回的 JSON → 记忆条目列表。任何不合规的条目直接丢弃，绝不抛异常。"""
    if not raw or not raw.strip():
        return []
    text = raw.strip()
    if text.startswith("```"):                      # 剥掉可能的 markdown 围栏
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        data = json.loads(text)
    except Exception:  # noqa: BLE001 —— 容忍模型在 JSON 外多说了话
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            return []
        try:
            data = json.loads(text[start:end + 1])
        except Exception:  # noqa: BLE001
            return []
    if not isinstance(data, dict):
        return []
    items = data.get("memories")
    if not isinstance(items, list):
        return []
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in items[:EXTRACT_MAX]:
        if not isinstance(item, dict):
            continue
        text_v = str(item.get("text") or "").strip()
        cat = str(item.get("category") or "").strip()
        if not text_v or len(text_v) > EXTRACT_TEXT_MAX or cat not in CATEGORIES:
            continue
        key = text_v[:80]
        if key in seen:                             # 模型一次输出里自己重复
            continue
        seen.add(key)
        out.append({"text": text_v, "category": cat,
                    "name": str(item.get("name") or "").strip()[:NAME_MAX]})
    return out


def norm_text(text: str) -> str:
    """查重 / 检索用的归一化：去空白与标点、转小写。"""
    return re.sub(r"[\s，。、；：！？,.:;!?()（）\[\]【】\"'“”‘’·—…]+", "", text or "").lower()


def _grams(text: str) -> set[str]:
    if not text:
        return set()
    return {text[i:i + 2] for i in range(len(text) - 1)} or {text}


def similarity(a_norm: str, b_norm: str) -> float:
    """字符 bigram 的 Dice 系数（0～1）。包含关系直接判 1。"""
    if not a_norm or not b_norm:
        return 0.0
    if a_norm == b_norm or a_norm in b_norm or b_norm in a_norm:
        return 1.0
    ga, gb = _grams(a_norm), _grams(b_norm)
    return 2 * len(ga & gb) / (len(ga) + len(gb))


def _edit_distance(a: str, b: str, cap: int = 80) -> int:
    """编辑距离（Levenshtein）；超长文本不参与（返回 cap），避免 O(n·m) 拖慢检索。"""
    if not a:
        return len(b)
    if not b:
        return len(a)
    if len(a) > cap or len(b) > cap:
        return cap + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[-1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def is_duplicate(new_text: str, existing_texts: list[str], threshold: float = 0.82) -> bool:
    """与既有记忆近似判为重复。纯本地计算，无模型调用。两级判断：
    - 字符 bigram Dice ≥ 阈值（长文本的乱序 / 插删容忍）；
    - 编辑距离 ≤ max(2, 长度 // 15)（短文本一两个字的措辞差异，如「是→叫」）。
    语义级的改写重复（同义不同词）靠提取提示词对照既有记忆样例来防，这里不追。"""
    a = norm_text(new_text)
    if not a:
        return True
    # 短文本只容忍 1 个字的措辞差异（如「是→叫」）；再大就把不同事实误伤成重复了
    dist_cap = max(1, len(a) // 20)
    for old in existing_texts:
        b = norm_text(old)
        if similarity(a, b) >= threshold:
            return True
        if _edit_distance(a, b) <= dist_cap:
            return True
    return False


def relevance_score(query_norm: str, text_norm: str) -> int:
    """检索打分：query 的 bigram 在文本里命中多少个（0 = 不相关）。"""
    if not query_norm:
        return 0
    if query_norm in text_norm:
        return 999                                  # 整串命中，直接顶满
    grams_q = _grams(query_norm)
    return sum(1 for g in grams_q if g in text_norm)


# ---------- Engramory 纪律：笔记名 / 索引行 / 上限守卫 / 链接 / 协议校验 ----------

def suggest_name(text: str) -> str:
    """从记忆正文取一个短笔记名：去禁用字符与修饰词，取前段，{NAME_MAX} 字内。
    中文子串匹配名字，只需避开索引指针带不了的字符。"""
    t = NAME_BAD_CHARS.sub("", (text or "").strip())
    for w in ("用户", "记住", "以后", "我们", "他们", "这个", "那个", "已经", "可以", "需要",
              "一定", "必须", "总是", "永远", "所有", "任何", "不要"):
        t = t.replace(w, "")
    t = t.strip("，。、；：！？,.:;!?…—")
    return t[:NAME_MAX] or "未命名"


def index_line(m: dict) -> str:
    """索引指针行：`⚖ - [名字](id) — 钩子`（铁律类带 ⚖ 前缀）。名字与钩子都截断，保证行有界。"""
    name = str(m.get("name") or "").strip()[:NAME_MAX] or suggest_name(str(m.get("text", "")))
    hook = str(m.get("hook") or "").strip()[:HOOK_MAX]
    line = f"- [{name}]({m.get('id')})" + (f" — {hook}" if hook else "")
    return (IRON_LINE_PREFIX + line) if m.get("category") == "iron" else line


def index_usage(lines: int, size: int) -> dict:
    """索引用量与超限判定（行数、字节分开判，谁先超算谁）。"""
    return {
        "lines": lines, "bytes": size,
        "soft_lines": INDEX_SOFT_LINES, "soft_bytes": INDEX_SOFT_BYTES,
        "hard_lines": INDEX_HARD_LINES, "hard_bytes": INDEX_HARD_BYTES,
        "over_soft": lines > INDEX_SOFT_LINES or size > INDEX_SOFT_BYTES,
        "over_hard": lines > INDEX_HARD_LINES or size > INDEX_HARD_BYTES,
    }


def check_index_limits(current_count: int, current_bytes: int, new_count: int,
                       new_bytes: int) -> dict:
    """写前上限拦截（Engramory §6：只拒绝既超上限、又比原来变大的那一项）。
    返回 {"ok": bool, "reason": str|None, "warn": str|None}：
    - ok=False：写入被拒（让索引变大且超硬上限）；
    - warn：软上限告警，放行但提醒整理；
    - 压缩方向的写入（变小或不变）永远放行。"""
    if new_count <= current_count and new_bytes <= current_bytes:
        return {"ok": True, "reason": None, "warn": None}
    line_ok = not (new_count > INDEX_HARD_LINES and new_count > current_count)
    byte_ok = not (new_bytes > INDEX_HARD_BYTES and new_bytes > current_bytes)
    if not line_ok or not byte_ok:
        bad = "行数" if not line_ok else "字节"
        return {"ok": False, "warn": None,
                "reason": (f"索引{bad}超硬上限（{new_count} 行/{new_bytes} 字节，上限 "
                           f"{INDEX_HARD_LINES} 行/{INDEX_HARD_BYTES} 字节）：先归档或合并旧记忆再写入。"
                           "归档出索引不删内容。")}
    warn = None
    if new_count > INDEX_SOFT_LINES or new_bytes > INDEX_SOFT_BYTES:
        warn = (f"索引已过软上限（{new_count} 行/{new_bytes} 字节，软上限 "
                f"{INDEX_SOFT_LINES} 行/{INDEX_SOFT_BYTES} 字节），建议归档或合并冷记忆")
    return {"ok": True, "reason": None, "warn": warn}


_LINK_RE = re.compile(r"\[\[([^\]\|]+)(?:\|([^\]]*))?\]\]")

def parse_links(text: str) -> list[str]:
    """提取笔记正文里的 [[链接]] 目标名列表（[[名字|显示]] 取名字）。"""
    return [m.group(1).strip() for m in _LINK_RE.finditer(text or "") if m.group(1).strip()]


def extract_why_how(text: str) -> tuple[str, str]:
    """从 feedback / project 笔记正文提取「为什么」「怎么用」两行（Engramory §2：
    行首标签才算；兼容全角冒号与加粗标签）。返回 (为什么, 怎么用)，缺则为空串。"""
    why = how = ""
    for raw in (text or "").splitlines():
        line = raw.strip()
        m = WHY_HOW_RE.match(line)
        if not m:
            continue
        label = m.group(1)
        rest = line[m.end():].strip()
        if label.lower().startswith(("为什么", "why")) and not why:
            why = rest
        elif label.lower().startswith(("怎么用", "how")) and not how:
            how = rest
    return why, how


def normalize_note(item: dict, *, require_why_how: bool) -> dict:
    """模型产出的候选条目 → 合规笔记（Engramory 纪律：名字、为什么/怎么用、绝对日期）。
    不合规的键值原样保留交给查重路由；缺名字补名字，缺为什么/怎么用按需补。"""
    out = dict(item)
    text = str(out.get("text") or "")
    if not out.get("name"):
        out["name"] = suggest_name(text)
    if require_why_how:
        why, how = extract_why_how(text)
        if not why:
            out["text"] = text + "\n**为什么：** 跨会话仍会用到（提炼时模型未给出原因）。"
        if not how:
            out["text"] = str(out["text"]) + "\n**怎么用：** 相关任务开始时先核对这条再动手。"
    return out


def parse_doctor_report(items: dict, active: list, archived: list) -> dict:
    """纯函数体检（engramory_doctor 对应物）：
    - 近似重复对：活跃库两两查重（阈值略降到 0.75，宁可多报让模型定夺）；
    - 超大笔记：超过提醒阈值的（时间线堆积的前兆，Engramory §6 建议拆结论与历史；只提醒不拦截）；
    - [[链接]]：指向不存在笔记名的链接（engramory 的坏链，我们没有文件名，按笔记名查）；
    - 建议归档：很老、无引用、未置顶的冷记忆。
    只报告、不动数据。"""
    texts = [(k, str(m.get("text", "")), str(m.get("name") or suggest_name(str(m.get("text", "")))))
             for k, m in sorted(items.items())]
    dup_pairs = []
    names = {t[2] for t in texts}
    for i in range(len(texts)):
        ki, ti, ni = texts[i]
        for j in range(i + 1, len(texts)):
            kj, tj, nj = texts[j]
            if is_duplicate(ti, [tj], threshold=0.75):
                dup_pairs.append({"a": ki, "b": kj})
    big = [{"id": k, "chars": len(t)} for k, t, _ in texts if len(t) >= NOTE_WARN_CHARS]
    broken = []
    for k, t, n in texts:
        for target in parse_links(t):
            if target not in names:
                broken.append({"id": k, "link": target})
                break
    now = _now()
    stale = []
    for k, t, n in texts:
        m = items[k]
        if (not m.get("pinned") and int(m.get("refs") or 0) == 0
                and now - float(m.get("updated_at") or 0) > 60 * 86400
                and m.get("category") != "user"):
            stale.append(k)
    return {"duplicates": dup_pairs, "oversized": big, "broken_links": broken,
            "stale_candidates": stale}


def build_extract_prompt(history_text: str, existing_sample: list[str],
                         *, compaction_mode: bool = False) -> str:
    """提炼提示词。compaction_mode=True 是「续接同步」模式（压缩前同步，Engramory §5）：
    重点接住交接摘要里的目标、状态、决定、障碍、下一步。"""
    sample = "\n".join(f"- {s}" for s in existing_sample) or "（暂无）"
    tail = (
        "这是会话刚被压缩后的「续接同步」：对话里出现了交接摘要。请优先接住摘要里的"
        "\n进行中任务——目标、当前状态、做过的决定、约束、卡点、下一步——"
        "\n用绝对日期（写「2026-11-11」不写「昨天」）。"
        if compaction_mode else
        "\n相对时间必须换算成绝对日期。")
    return f"""你是记忆管理员，按 Engramory 协议提炼「值得跨会话长期记住的事实」，输出 JSON。

只提炼这几类：
- iron：工作铁律——凌驾于其他一切记忆之上的硬边界（安全红线、不可逾越的流程、绝对禁区）。
  只有「违反会造成实际损失/风险、且没有例外余地」的才归这类；拿不准就不用 iron，宁低勿高；也必须带「为什么」和「怎么用」
- user：用户的身份、称呼、长期偏好
- feedback：对 AI 工作方式的纠正与要求（如「GLM 是付费订阅不说免费」）——必须带「为什么」和「怎么用」
- project：跨会话仍有效的项目决策、约束、进行中事项——必须带「为什么」和「怎么用」，写当前结论不写流水账
- reference：重要的外部资源指针（地址、位置、平台）

绝不记录：密钥、密码、token、Cookie 的值（只记「放在哪里」）；一次性任务细节；闲聊；
代码片段本身；人人都知道的常识；与「既有记忆样例」重复的内容。
每条独立成立（不依赖上下文就能看懂）、尽量不超过 {EXTRACT_TEXT_MAX} 字（流程/约束类可写全不截断）；给一条 4～12 字的短名字（name）。
重要性分级只有两档：普通（feedback/project/user/reference）与铁律（iron）；铁律宁缺勿滥。
feedback / project / iron 的 text 里用「**为什么：**」「**怎么用：**」两行写原因和用法。{tail}
没有值得记的就输出空列表。

既有记忆样例（避免语义重复）：
{sample}

对话记录：
<transcript>
{history_text}
</transcript>

只输出 JSON：{{"memories": [{{"name": "…", "text": "…", "category": "user|feedback|project|reference"}}]}}"""


def _now() -> float:
    return time.time()


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())


def _message_text(content: Any) -> str:
    """消息 content（字符串或内容块列表）→ 纯文本。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            str(p.get("text", "")) for p in content if isinstance(p, dict) and p.get("text"))
    return ""


def configured_backends(settings: Settings) -> list[Backend]:
    """把填好的槽位组装成后端列表（按槽位顺序，即故障切换顺序）；三项不齐的槽位跳过。"""
    out: list[Backend] = []
    for n in (1, 2, 3):
        key = getattr(settings, f"slot{n}_key").get_secret_value().strip()
        base = getattr(settings, f"slot{n}_base").strip()
        model = getattr(settings, f"slot{n}_model").strip()
        if not (key and base and model):
            continue
        name = getattr(settings, f"slot{n}_name").strip() or f"后端{n}"
        out.append(Backend(name=name, base_url=base, api_key=SecretStr(key), model=model))
    return out


async def failover_extract(
    backends: list[Backend],
    active: str,
    call,
    log,
) -> tuple[list[dict[str, str]], str]:
    """按顺序尝试后端，第一个成功的生效；返回 (记忆条目, 新的粘住后端名)。

    粘住策略：额度/鉴权类错误（401/402/403/429）切换后记住；瞬时错误（超时/5xx）
    只本次绕过、不粘。粘住不是永久的：由调用方传入过期的粘住名（空串），
    首选后端恢复后自动回到配置顺序。全失败时粘住名不变，返回空列表。"""
    ordered = list(backends)
    if active:
        ordered.sort(key=lambda b: b.name != active)      # 稳定排序：粘住者提前，其余保持配置顺序
    errors: list[str] = []
    sticky_fail = False
    for i, backend in enumerate(ordered):
        try:
            found = await call(backend)
        except Exception as exc:  # noqa: BLE001 —— 换下一个后端
            status = getattr(getattr(exc, "response", None), "status_code", None)
            errors.append(f"{backend.name}：{exc}")
            if status in (401, 402, 403, 429):
                sticky_fail = True
                log("warning", "%s 后端「%s」额度或鉴权不可用（HTTP %s），尝试下一个",
                    PLUGIN_ID, backend.name, status)
            else:
                log("warning", "%s 后端「%s」本次调用失败（不粘住），尝试下一个：%s",
                    PLUGIN_ID, backend.name, exc)
            continue
        if i == 0:
            if backend.name != active:
                return found, ""                          # 无粘住或已过期：首选生效，粘住清空
            return found, active
        if sticky_fail and backend.name != active:
            log("info", "%s 提取后端切换到「%s」（额度用尽/鉴权失效，已粘住）",
                PLUGIN_ID, backend.name)
            return found, backend.name
        return found, active
    log("warning", "%s 提炼失败（全部 %d 个后端）：%s",
        PLUGIN_ID, len(ordered), "；".join(errors))
    return [], active


# ---------- 自动压缩：纯函数判断（原 session-keeper，并入后共用一次轨迹读取） ----------

class CompactionRecord(BaseModel):
    """一次压缩的历史记录（管理页「压缩管家」看板展示用）。"""

    session_id: str
    at: float
    reason: str
    tokens_before: int | None = None
    tokens_after: int | None = None


def should_compact(*, messages_since: int, last_prompt_tokens: int | None,
                   has_compaction: bool, max_messages: int, max_prompt_tokens: int) -> tuple[bool, str]:
    """返回 (是否压缩, 原因)。满足任一阈值即压：
    - messages_since：自上次压缩（或会话开头）以来的用户消息数；
    - last_prompt_tokens：最近一次 assistant 记录的 prompt_tokens（可能为 None）；
    - has_compaction：轨迹里是否已有压缩记录（决定消息数起点，不影响判断本身）。"""
    if messages_since >= max_messages:
        return True, f"累计 {messages_since} 条消息 ≥ 阈值 {max_messages}"
    if last_prompt_tokens is not None and last_prompt_tokens >= max_prompt_tokens:
        return True, f"输入 {last_prompt_tokens} token ≥ 阈值 {max_prompt_tokens}"
    return False, (f"未达阈值（消息 {messages_since}/{max_messages}，"
                   f"输入 {last_prompt_tokens if last_prompt_tokens is not None else '未知'} token）")


def in_cooldown(last_ts: float | None, cooldown_minutes: int, now: float) -> bool:
    """同一会话两次压缩的最小间隔。"""
    if last_ts is None:
        return False
    return (now - last_ts) < cooldown_minutes * 60


def scan_compact_signals(entries: list) -> dict:
    """扫一遍轨迹条目（从最新往回扫），得到压缩判断所需信号：
    - messages_since：自上次压缩（或会话开头）以来的用户消息数；
    - last_prompt_tokens：最近一次 assistant 记录的输入 token；
    - has_compaction：轨迹里是否已有压缩记录。
    注意：压缩记录（type=compaction）是分界线，遇到就停。"""
    messages_since = 0
    last_prompt_tokens = None
    has_compaction = False
    for entry in reversed(entries or []):
        etype = entry.get("type")
        if etype == "compaction":
            has_compaction = True
            break                             # 压缩记录是分界线：之前的都是上个窗口，不数
        if etype != "message":
            continue
        msg = entry.get("message") or {}
        if msg.get("role") == "user":
            messages_since += 1
        elif msg.get("role") == "assistant":
            usage = entry.get("usage") or {}
            if last_prompt_tokens is None and usage.get("prompt_tokens"):
                last_prompt_tokens = usage["prompt_tokens"]
    return {"messages_since": messages_since, "last_prompt_tokens": last_prompt_tokens,
            "has_compaction": has_compaction}


# ---------- 路由（自动成为 mclaw 命令；store / all_items 由 apply 初始化） ----------

router = APIRouter()


def _memory_view(key: str, mem: dict, *, pointer: bool = False) -> dict:
    """pointer=True 返回索引指针视图（名字 + 钩子，不带正文，Engramory §3/§4）。"""
    if pointer:
        name = str(mem.get("name") or "").strip() or suggest_name(str(mem.get("text", "")))
        hook = str(mem.get("hook") or "").strip()
        if not hook:
            head = re.split(r"\n", str(mem.get("text", "")), maxsplit=1)[0].strip()
            hook = head[:HOOK_MAX] + ("…" if len(head) > HOOK_MAX else "")
        return {"id": key, "name": name[:NAME_MAX], "hook": hook,
                "category": mem.get("category", "project"),
                "pinned": bool(mem.get("pinned")), "archived": bool(mem.get("archived")),
                "updated_at": mem.get("updated_at")}
    return {
        "id": key,
        "name": str(mem.get("name") or "") or suggest_name(str(mem.get("text", ""))),
        "hook": str(mem.get("hook") or ""),
        "text": str(mem.get("text", "")),
        "category": mem.get("category", "project"),
        "pinned": bool(mem.get("pinned")),
        "archived": bool(mem.get("archived")),
        "refs": int(mem.get("refs", 0)),
        "created_at": mem.get("created_at"),
        "updated_at": mem.get("updated_at"),
    }


def _sorted_views(items: dict[str, dict], *, pointers: bool = False) -> list[dict]:
    """铁律（iron）永远最前 → 置顶 → 其余按更新时间倒序；归档永远沉底。
    （铁律压过置顶：硬边界的优先级高于任何手动置顶）"""
    ordered = sorted(items.items(),
                     key=lambda p: (p[1].get("category") != "iron",
                                    not bool(p[1].get("pinned")),
                                    bool(p[1].get("archived")),
                                    -float(p[1].get("updated_at") or 0)))
    return [_memory_view(k, m, pointer=pointers) for k, m in ordered]


async def _load_all(store) -> dict[str, dict]:
    return {k: v for k, v in (await store.items()).items()
            if k.startswith("mem:") and isinstance(v, dict)}


async def _load_active(store) -> dict[str, dict]:
    return {k: v for k, v in (await _load_all(store)).items() if not v.get("archived")}


def _index_usage(items: dict[str, dict]) -> dict:
    """当前有界索引的用量：只数活跃（未归档）记忆的指针行（Engramory §6）。"""
    lines = [index_line(m) for m in items.values() if not m.get("archived")]
    return index_usage(len(lines), len(INDEX_HEADER) + sum(len(l) + 1 for l in lines))


@router.get("/index", operation_id=f"plugins.{PLUGIN_ID}.index",
            summary="记忆索引：默认返回有界索引（一行一条指针，不带正文，开局查这个）；"
                    "带 query 按相关度全文检索；detail=true 返回全文")
async def memory_index(query: str | None = None, category: str | None = None,
                       limit: int = 30, detail: bool = False,
                       include_archived: bool = False) -> dict:
    """Engramory §4：索引每读、细节按需。无 query = 只发索引指针；关键词检索
    才返回全文（检索本身就是「打开了相关笔记」）。
    include_archived=true 时归档记忆也参与（默认每轮上下文只用活跃索引，
    管理页想翻归档旧账才开）。"""
    store = _STORE
    all_items = await _load_all(store)
    archived_count = sum(1 for m in all_items.values() if m.get("archived"))
    if not include_archived:
        items = {k: m for k, m in all_items.items() if not m.get("archived")}
    else:
        items = all_items
    if category in CATEGORIES:
        items = {k: m for k, m in items.items() if m.get("category") == category}
    if query:
        q = norm_text(query)
        scored = []
        for key, mem in items.items():
            hay = norm_text(str(mem.get("text", "")) + " " + str(mem.get("name") or ""))
            hit = relevance_score(q, hay)
            if hit <= 0:
                continue
            scored.append((hit + (500 if mem.get("pinned") else 0), key, mem))
        scored.sort(key=lambda p: -p[0])
        return {"count": len(scored[:limit]),
                "memories": [_memory_view(k, m) for _, k, m in scored[:limit]]}
    # include_archived=true 时归档条目也参与列表（_sorted_views 排序保证归档条目沉底）
    ranked = _sorted_views(items, pointers=not detail)
    usage = _index_usage(items)
    return {"count": len(ranked[:limit]), "memories": ranked[:limit],
            "archived_count": archived_count,
            "index": usage,
            "warning": "索引超软上限：建议 plugins mc-memory archive 归档冷记忆或合并近似重复"
                       if usage["over_soft"] else None}


@router.get("/get", operation_id=f"plugins.{PLUGIN_ID}.get",
            summary="按 id 取一条记忆全文（索引里看着相关再取这个）")
async def memory_get(id: str) -> dict:
    mem = (await _load_all(_STORE)).get(id)
    if not isinstance(mem, dict):
        raise HTTPException(status_code=404, detail=f"记忆不存在：{id}")
    view = _memory_view(id, mem)
    view["refs"] = int(mem.get("refs") or 0) + 1
    mem = dict(mem)
    mem["refs"] = view["refs"]
    mem["last_ref_at"] = _now()
    await _STORE.set(id, mem)                       # 引用计数：体检「建议归档」要用的活跃信号
    return view


@router.get("/doctor", operation_id=f"plugins.{PLUGIN_ID}.doctor",
            summary="记忆库体检：近似重复对、超大笔记、失效 [[链接]]、可归档的冷记忆、索引用量")
async def memory_doctor() -> dict:
    items = await _load_active(_STORE)   # 只体检活跃笔记：已归档的已移出索引，再报就是误导
    report = parse_doctor_report(items, None, None)
    report["index"] = _index_usage(items)
    report["total"] = len(items)
    return report


@router.get("/list", operation_id=f"plugins.{PLUGIN_ID}.list",
            summary="列出全部记忆（管理页用，置顶优先、按更新时间倒序）")
async def memory_list() -> dict:
    items = await _load_all(_STORE)
    views = _sorted_views(items)
    return {"count": len(views), "memories": views}


async def _guard_index(store, extra: int = 1) -> str | None:
    """写前上限守卫：按「活跃数 + extra」预测写入后的索引用量并拦截。
    返回软上限告警（None = 无告警）；超硬上限直接 409。"""
    items = await _load_all(store)
    active = [m for m in items.values() if not m.get("archived")]
    usage = _index_usage({str(i): m for i, m in enumerate(active)})
    new_active = len(active) + extra
    est_lines = new_active + 2                       # 标题 + 提示行
    est_bytes = usage["bytes"] + extra * (NAME_MAX + HOOK_MAX + 16)
    verdict = check_index_limits(usage["lines"] + 2, usage["bytes"],
                                 est_lines, est_bytes)
    if not verdict["ok"]:
        raise HTTPException(status_code=409, detail=verdict["reason"])
    return verdict["warn"]


@router.post("/add", operation_id=f"plugins.{PLUGIN_ID}.add",
             summary="写入一条记忆（笔记）：text 必填；category 是 user/feedback/project/reference；"
                     "可选 name 笔记名、hook 索引钩子。会写前查重并卡索引上限")
async def memory_add(text: str, category: str = "project", pinned: bool = False,
                     name: str = "", hook: str = "") -> dict:
    store = _STORE
    text = (text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="记忆内容不能为空")
    if category not in CATEGORIES:
        raise HTTPException(
            status_code=400,
            detail=f"category 只能是 {'/'.join(CATEGORIES)} 之一，收到的是「{category}」")
    items = await _load_all(store)
    active_n = sum(1 for m in items.values() if not m.get("archived"))
    if active_n >= MAX_ACTIVE:
        raise HTTPException(
            status_code=409,
            detail=f"活跃记忆已满（{MAX_ACTIVE} 条）：先归档冷记忆（plugins mc-memory archive）再写入")
    if len(text) > HARD_CHARS:
        raise HTTPException(
            status_code=413,
            detail=f"单条超过 {HARD_CHARS} 字（兼底上限）：这么长请拆成多条笔记再用 [[链接]] 互链")
    if is_duplicate(text, [str(m.get("text", "")) for m in items.values()
                           if not m.get("archived")]):
        raise HTTPException(status_code=409,
                            detail="活跃记忆里已有一条内容几乎相同的，不重复记录；"
                                   "要改旧的用 plugins mc-memory update")
    warn = await _guard_index(store, extra=1)
    note_name = (name or "").strip()[:NAME_MAX] or suggest_name(text)
    mem = {"text": text, "category": category, "pinned": pinned,
           "name": note_name, "hook": (hook or "").strip()[:HOOK_MAX],
           "refs": 0, "created_at": _now(), "updated_at": _now()}
    key = f"mem:{int(time.time() * 1000)}"
    await store.set(key, mem)
    return {"ok": True, "id": key, "memory": _memory_view(key, mem), "warning": warn}


@router.post("/archive", operation_id=f"plugins.{PLUGIN_ID}.archive",
             summary="归档一条记忆：移出有界索引（不再每轮占用上下文），内容保留、可随时恢复。"
                     "这是索引超限时的首选整理手段")
async def memory_archive(id: str, archived: bool = True) -> dict:
    store = _STORE
    mem = (await _load_all(store)).get(id)
    if not isinstance(mem, dict):
        raise HTTPException(status_code=404, detail=f"记忆不存在：{id}")
    mem = dict(mem)
    mem["archived"] = bool(archived)
    mem["archived_at"] = _now() if archived else None
    mem["updated_at"] = _now()
    await store.set(id, mem)
    usage = _index_usage(await _load_all(store))
    return {"ok": True, "memory": _memory_view(id, mem), "index": usage}


@router.post("/restore", operation_id=f"plugins.{PLUGIN_ID}.restore",
             summary="把归档的记忆恢复回索引（写前卡上限：满了会被拒绝）")
async def memory_restore(id: str) -> dict:
    return await memory_archive(id=id, archived=False)


@router.post("/delete", operation_id=f"plugins.{PLUGIN_ID}.delete",
             summary="彻底删除一条记忆（按 id，不进归档；归档用 plugins mc-memory archive）")
async def memory_delete(id: str) -> dict:
    ok = await _STORE.delete(id)
    if not ok:
        raise HTTPException(status_code=404, detail=f"记忆不存在：{id}")
    return {"ok": True}


class MemoryPatch(BaseModel):
    """编辑记忆的请求体；PATCH 语义：没给的字段保持原值。"""

    text: str | None = Field(None, min_length=1, max_length=HARD_CHARS,
                             title="新的记忆内容（不限字数，超长体检会提醒拆分）")
    category: str | None = Field(None, title="新的分类")
    name: str | None = Field(None, title="笔记名（索引指针里显示的短名字）")
    hook: str | None = Field(None, title="索引钩子（一行短提示）")


def _apply_patch(mem: dict, patch: MemoryPatch) -> dict:
    """把 PATCH 合并进一条记忆，返回新字典（不动原对象）。"""
    out = dict(mem)
    if patch.text is not None:
        text = patch.text.strip()
        if not text:
            raise HTTPException(status_code=400, detail="记忆内容不能为空")
        out["text"] = text
    if patch.category is not None:
        if patch.category not in CATEGORIES:
            raise HTTPException(
                status_code=400,
                detail=f"category 只能是 {'/'.join(CATEGORIES)} 之一，收到的是「{patch.category}」")
        out["category"] = patch.category
    if patch.name is not None:
        out["name"] = patch.name.strip()[:NAME_MAX]
    if patch.hook is not None:
        out["hook"] = patch.hook.strip()[:HOOK_MAX]
    return out


@router.patch("/update", operation_id=f"plugins.{PLUGIN_ID}.update",
              summary="编辑一条记忆：只传要改的字段（text / category），没传的保持原值")
async def memory_update(id: str, patch: MemoryPatch) -> dict:
    store = _STORE
    items = await _load_all(store)
    mem = items.get(id)
    if not isinstance(mem, dict):
        raise HTTPException(status_code=404, detail=f"记忆不存在：{id}")
    if all(v is None for v in (patch.text, patch.category, patch.name, patch.hook)):
        raise HTTPException(status_code=400, detail="至少给出 text / category / name / hook 之一")
    merged = _apply_patch(mem, patch)
    if patch.text is not None and merged["text"] != str(mem.get("text", "")):
        others = [str(m.get("text", "")) for k, m in items.items() if k != id]
        if is_duplicate(merged["text"], others):
            raise HTTPException(status_code=409,
                                detail="已有一条内容几乎相同的记忆，不重复记录")
    warn = await _guard_index(store, extra=0) if not mem.get("archived") else None
    merged["updated_at"] = _now()
    await store.set(id, merged)
    return {"ok": True, "memory": _memory_view(id, merged), "warning": warn}


@router.post("/pin", operation_id=f"plugins.{PLUGIN_ID}.pin",
             summary="置顶 / 取消置顶一条记忆（置顶的召回时永远排最前）")
async def memory_pin(id: str, pinned: bool = True) -> dict:
    store = _STORE
    items = await _load_all(store)
    mem = items.get(id)
    if not isinstance(mem, dict):
        raise HTTPException(status_code=404, detail=f"记忆不存在：{id}")
    mem["pinned"] = bool(pinned)
    mem["updated_at"] = _now()
    await store.set(id, mem)
    return {"ok": True, "memory": _memory_view(id, mem)}


@router.get("/stats", operation_id=f"plugins.{PLUGIN_ID}.stats",
            summary="记忆库统计：活跃/归档总数、分类计数、索引用量")
async def memory_stats() -> dict:
    items = await _load_all(_STORE)
    active = {k: m for k, m in items.items() if not m.get("archived")}
    by_cat: dict[str, int] = {}
    for mem in active.values():
        c = str(mem.get("category", "project"))
        by_cat[c] = by_cat.get(c, 0) + 1
    return {"total": len(active), "archived": len(items) - len(active),
            "by_category": by_cat, "max": MAX_ACTIVE,
            "index": _index_usage(items),
            "extraction": {
                "auto": bool(_CONFIG.auto_extract),
                "sync_on_compaction": bool(_CONFIG.sync_on_compaction),
                "backends": [b.name for b in configured_backends(_CONFIG)],
            },
            "compaction": {
                "auto": bool(_CONFIG.auto_compact),
                "max_messages": _CONFIG.max_messages,
                "max_prompt_tokens": _CONFIG.max_prompt_tokens,
                "cooldown_minutes": _CONFIG.cooldown_minutes,
            }}


@router.get("/compact-status", operation_id=f"plugins.{PLUGIN_ID}.compact-status",
            summary="压缩管家状态：最近若干次压缩历史与累计效果（含未启用自动压缩的会话）")
async def compact_status(limit: int = 20) -> dict:
    records = [r for r in (await _STORE.get("compact_history", default=[]))
               if isinstance(r, dict)]
    records.sort(key=lambda r: -float(r.get("at") or 0))
    shown = records[:max(1, limit)]
    saved = sum((r.get("tokens_before") or 0) - (r.get("tokens_after") or 0)
                for r in records
                if isinstance(r.get("tokens_before"), int) and isinstance(r.get("tokens_after"), int)
                and r["tokens_before"] > r["tokens_after"])
    return {"total": len(records), "tokens_saved": saved, "recent": shown,
            "auto": bool(_CONFIG.auto_compact),
            "max_messages": _CONFIG.max_messages,
            "max_prompt_tokens": _CONFIG.max_prompt_tokens,
            "cooldown_minutes": _CONFIG.cooldown_minutes}


@router.post("/compact-now", operation_id=f"plugins.{PLUGIN_ID}.compact-now",
             summary="手动压缩指定会话（不走阈值判断与冷却；刷新会话列表取 session_id）")
async def compact_now(session_id: str) -> dict:
    if not session_id.strip():
        raise HTTPException(status_code=400, detail="session_id 不能为空")
    try:
        result = await _OPS.call("session.compact-context", {"session_id": session_id.strip()})
    except OpsError as exc:
        detail = exc.message if getattr(exc, "message", None) else str(exc)
        if "不存在" in detail or "not found" in detail.lower():
            raise HTTPException(status_code=404, detail=f"会话不存在：{session_id}")
        raise HTTPException(status_code=409, detail=f"压缩未执行：{detail}")
    await _record_compaction(session_id.strip(), "手动压缩",
                             result.get("tokens_before"), result.get("tokens_after"))
    _LOGGER.info("%s 手动压缩会话 %s：%s → %s token", PLUGIN_ID, session_id,
                 result.get("tokens_before"), result.get("tokens_after"))
    return {"ok": True, "session_id": session_id.strip(),
            "tokens_before": result.get("tokens_before"),
            "tokens_after": result.get("tokens_after")}


# ---------- 插件入口 ----------

_STORE = None
_CONFIG = None
_LOGGER = None
_OPS = None


@plugin(
    PLUGIN_ID,
    title="记忆与上下文管家",
    inject=(DURABLE_EVENTS, PLUGIN_DATA, HOST_OPS, PLUGIN_ROUTES),
    permissions=("session.get-transcript", "session.compact-context"),
    config=Settings,
)
async def apply(ctx: Context[Settings]) -> None:
    global _STORE, _CONFIG, _LOGGER, _OPS
    _STORE = ctx.use(PLUGIN_DATA).scoped(ctx)
    _CONFIG = ctx.config
    _LOGGER = ctx.logger
    _OPS = ops = await ctx.use(HOST_OPS).client(ctx)
    ctx.use(PLUGIN_ROUTES).mount(ctx, router)

    existing = await _load_all(_STORE)
    _LOGGER.info(
        "%s 已加载：%d 条记忆；自动提炼=%s；后端顺序=%s；自动压缩=%s"
        "（消息阈值 %d，token 阈值 %d，冷却 %d 分钟）",
        PLUGIN_ID, len(existing),
        "开" if _CONFIG.auto_extract else "关",
        " → ".join(b.name for b in configured_backends(_CONFIG)) or "未配置",
        "开" if _CONFIG.auto_compact else "关",
        _CONFIG.max_messages, _CONFIG.max_prompt_tokens, _CONFIG.cooldown_minutes)

    async def _call_backend(client: httpx.AsyncClient, backend: Backend,
                            prompt: str) -> list[dict[str, str]]:
        """调单个后端；地址已带版本段（/v1、/v4 等）就直接拼 chat/completions。"""
        base = backend.base_url.strip().rstrip("/")
        if re.search(r"/v\d+(?:\.\d+)?/?$", base):
            base += "/chat/completions"
        else:
            base += "/v1/chat/completions"
        resp = await client.post(
            base,
            headers={"Authorization": f"Bearer {backend.api_key.get_secret_value()}"},
            json={"model": backend.model, "temperature": 0.2,
                  "messages": [{"role": "user", "content": prompt}]})
        resp.raise_for_status()
        return parse_extraction(resp.json()["choices"][0]["message"]["content"])

    async def _extract_via_model(history_text: str, *,
                                 compaction_mode: bool = False) -> list[dict[str, str]]:
        """顺序容错调提取模型；失败返回空列表，绝不影响会话。"""
        backends = configured_backends(_CONFIG)
        if not backends:
            return []
        current = await _load_all(_STORE)
        sample = [str(m.get("text", "")) for m in
                  sorted(current.values(),
                         key=lambda m: -float(m.get("updated_at") or 0))[:40]]
        prompt = build_extract_prompt(history_text, sample, compaction_mode=compaction_mode)
        transport = net.http_transport(PLUGIN_ID)
        async with httpx.AsyncClient(transport=transport, timeout=60,
                                     follow_redirects=True) as client:
            sticky = await _STORE.get("sticky_backend", default={})
            active = ""
            if isinstance(sticky, dict):
                name, since = str(sticky.get("name") or ""), float(sticky.get("since") or 0)
                if name and _now() - since < STICKY_TTL and name in {
                        b.name for b in backends}:
                    active = name                          # 粘住未过期：直接从它开始
                elif name:
                    _LOGGER.info("%s 粘住后端「%s」已过有效期，重新按配置顺序探测",
                                 PLUGIN_ID, name)
            found, new_active = await failover_extract(
                backends, active,
                call=lambda b: _call_backend(client, b, prompt),
                log=lambda level, *args: getattr(_LOGGER, level)(*args))
        if new_active != active:
            if new_active:
                await _STORE.set("sticky_backend",
                                 {"name": new_active, "since": _now()})
            else:
                await _STORE.delete("sticky_backend")
        return found

    async def _persist(found: list[dict[str, str]]) -> tuple[int, list[str]]:
        """候选 → 合规笔记入库。活跃库将满时先把最冷的记忆「试归档」出索引
        （Engramory §6：先压缩索引、绝不因为索引满就丢掉刚学到的），仍装不下才丢弃。
        返回 (新存条数, 被试归档的 id 列表)。"""
        current = await _load_all(_STORE)
        active_items = {k: m for k, m in current.items() if not m.get("archived")}
        # 试归档：最冷 = 未置顶、引用最少、最久没更新的排前面
        overflow = len(active_items) + len(found) - MAX_ACTIVE
        to_archive: list[str] = []
        if overflow > 0:
            cold = sorted(((k, m) for k, m in active_items.items() if not m.get("pinned")),
                          key=lambda p: (int(p[1].get("refs") or 0),
                                         float(p[1].get("updated_at") or 0)))
            to_archive = [k for k, _ in cold[:overflow]]
        existing_texts = [str(m.get("text", "")) for m in active_items.values()]
        saved = 0
        for item in found:
            if len(active_items) + saved >= MAX_ACTIVE:
                break
            note = normalize_note(item, require_why_how=item.get("category") in ("feedback", "project"))
            text = str(note.get("text", ""))
            if is_duplicate(text, existing_texts):
                continue
            mem = {"text": text[:EXTRACT_TEXT_MAX], "category": item["category"],
                   "pinned": False, "refs": 0,
                   "name": str(note.get("name") or suggest_name(text))[:NAME_MAX],
                   "hook": "",
                   "created_at": _now(), "updated_at": _now()}
            key = f"mem:{int(time.time() * 1000)}-{saved}"
            await _STORE.set(key, mem)
            current[key] = mem
            active_items[key] = mem
            existing_texts.append(text)
            saved += 1
        for k in to_archive:
            m = dict(current[k])
            m["archived"] = True
            m["archived_at"] = _now()
            m["updated_at"] = _now()
            await _STORE.set(k, m)
            current[k] = m
        return saved, to_archive

    async def _record_compaction(session_id: str, reason: str,
                                 tokens_before, tokens_after) -> None:
        """记一次压缩到历史（给管理页看板）。失败不影响主流程。"""
        try:
            history = [r for r in (await _STORE.get("compact_history", default=[]))
                       if isinstance(r, dict)]
            history.append(CompactionRecord(
                session_id=session_id, at=_now(), reason=reason,
                tokens_before=tokens_before, tokens_after=tokens_after).model_dump())
            await _STORE.set("compact_history", history[-COMPACT_LOG_MAX:])
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("%s 记录压缩历史失败：%s", PLUGIN_ID, exc)

    async def _maybe_compact(session_id: str, entries: list) -> dict | None:
        """压缩判断 + 执行 + 记录。返回结果 dict（含是否压缩），判断不通过/失败返回 None。"""
        if not _CONFIG.auto_compact or session_id in _CONFIG.exclude_sessions:
            return None
        if in_cooldown(await _STORE.get(f"{COOLDOWN_KEY}:{session_id}"),
                        _CONFIG.cooldown_minutes, _now()):
            return None
        signals = scan_compact_signals(entries)
        do, reason = should_compact(
            messages_since=signals["messages_since"],
            last_prompt_tokens=signals["last_prompt_tokens"],
            has_compaction=signals["has_compaction"],
            max_messages=_CONFIG.max_messages,
            max_prompt_tokens=_CONFIG.max_prompt_tokens)
        if not do:
            return None
        try:
            result = await ops.call("session.compact-context", {"session_id": session_id})
        except OpsError as exc:
            # 正在运行的会话会拒绝；跳过，下一轮结束再试
            _LOGGER.info("%s 会话 %s 压缩未执行：%s", PLUGIN_ID, session_id, exc.message)
            return None
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("%s 会话 %s 压缩异常：%s", PLUGIN_ID, session_id, exc)
            return None
        await _STORE.set(f"{COOLDOWN_KEY}:{session_id}", _now())
        await _record_compaction(session_id, reason,
                                 result.get("tokens_before"), result.get("tokens_after"))
        _LOGGER.info("%s 会话 %s 已自动压缩（%s）：%s → %s token",
                     PLUGIN_ID, session_id, reason,
                     result.get("tokens_before"), result.get("tokens_after"))
        return {"reason": reason, "tokens_before": result.get("tokens_before"),
                "tokens_after": result.get("tokens_after")}

    async def on_run_finished(event: RunFinished) -> None:
        # 幂等：同一事件可能投递两次
        event_id = ctx.delivery.event_id if ctx.delivery else None
        seen = await _STORE.get("seen_events", default=[])
        if event_id and event_id in seen:
            return
        if event_id:
            await _STORE.set("seen_events", [event_id, *seen][:300])

        if not _CONFIG.auto_extract and not _CONFIG.auto_compact:
            return
        need_extract = _CONFIG.auto_extract and bool(configured_backends(_CONFIG))
        if not need_extract and not _CONFIG.auto_compact:
            return
        session_id = event.session_id

        # 两个功能共用一次轨迹读取：压缩扫全量条目，提炼扫水位线之后的消息
        try:
            data = await ops.call("session.get-transcript",
                                  {"session_id": session_id, "view": "chat"})
        except OpsError as exc:
            _LOGGER.warning("%s 读会话 %s 失败：%s", PLUGIN_ID, session_id, exc.message)
            return
        entries = data.get("entries") or []

        # ① 自动压缩（先压）：达标就把上下文折成交接摘要，轻装上阵
        compacted = await _maybe_compact(session_id, entries)

        if not need_extract:
            return

        # ② 记忆提炼（后提炼）：压缩发生的轮次强制提炼一次——赶在上下文被
        # 折叠之前把值得长期记住的事实存进记忆库；交接摘要出现时下一轮
        # 的续接同步会接住（Engramory §5）。
        watermark = await _STORE.get(f"watermark:{session_id}", default=None)
        new_user_msgs: list[str] = []
        history_parts: list[str] = []
        compaction_digest = ""                       # 本轮新出现的压缩摘要（续接同步用）
        total = 0
        for entry in entries:
            if entry.get("type") != "message" or entry.get("generated_skill_instruction"):
                continue
            ts = str(entry.get("timestamp") or "")
            if watermark and ts <= watermark:
                continue
            msg = entry.get("message") or {}
            role = msg.get("role")
            text = _message_text(msg.get("content")).strip()
            if not text:
                continue
            if text.startswith(COMPACTION_PREFIX):
                if len(text) > len(compaction_digest):
                    compaction_digest = text          # 留最长的一份给续接同步
                continue
            if role == "user":
                new_user_msgs.append(text)
                history_parts.append(f"用户：{text[:1200]}")
                total += len(text)
            elif role == "assistant":
                snippet = text[:ASSISTANT_SNIPPET]
                history_parts.append(f"AI：{snippet}")
                total += len(snippet)
            if total >= HISTORY_MAX_CHARS:
                break

        # 压缩摘要续接同步（Engramory §5）：出现了新的交接摘要（且开关开着）就先接住
        # 进行中任务，不受「最少新消息数」限制——摘要里的未完成任务比消息数更要紧。
        # 压缩刚发生：压缩前的新对话还没提炼过，也强制提炼一次（同样无视最少新消息数）。
        if ((compaction_digest and _CONFIG.sync_on_compaction) or compacted) and history_parts:
            if compaction_digest:
                history_parts = history_parts[:8] + [
                    "\n".join(compaction_digest.split("\n")[:COMPACT_SNIPPET // 30])]
            found = await _extract_via_model("\n".join(history_parts),
                                             compaction_mode=bool(compaction_digest))
            saved, archived_ids = await _persist(found)
            await _STORE.set(f"watermark:{session_id}", _now_iso())
            _LOGGER.info("%s 会话 %s %s：候选 %d 条，新存 %d 条%s",
                         PLUGIN_ID, session_id,
                         "续接同步完成" if compaction_digest else "压缩前强制提炼完成",
                         len(found), saved,
                         f"，试归档 {len(archived_ids)} 条" if archived_ids else "")
            return

        if len(new_user_msgs) < _CONFIG.min_new_messages or not history_parts:
            return

        found = await _extract_via_model("\n".join(history_parts))
        saved, archived_ids = await _persist(found)
        await _STORE.set(f"watermark:{session_id}", _now_iso())
        _LOGGER.info("%s 会话 %s 提炼完成：候选 %d 条，新存 %d 条（新增用户消息 %d 条）%s",
                     PLUGIN_ID, session_id, len(found), saved, len(new_user_msgs),
                     f"，试归档 {len(archived_ids)} 条" if archived_ids else "")

    ctx.on(AGENT_RUN_FINISHED, on_run_finished, id="auto-extract", durable=True)
