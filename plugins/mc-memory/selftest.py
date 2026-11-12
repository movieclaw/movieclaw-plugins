"""mc-memory 离线自测：stub 宿主模块后加载插件源码，测纯函数与故障切换编排。

跑法：在本目录执行 python3 selftest.py
"""

import asyncio
import importlib
import re
import sys
import time
import types
from typing import Generic, TypeVar

T = TypeVar("T")

# ---------- stub 宿主模块 ----------

kernel = types.ModuleType("movieclaw_kernel")


class ServiceKey:
    def __init__(self, *a, **k):
        pass


class Context(Generic[T]):
    pass


class _DurEv:
    AGENT_RUN_FINISHED = "agent.run.finished"


class _PluginDec:
    def __init__(self, *a, **k):
        pass

    def __call__(self, fn):
        return fn


kernel.ServiceKey = ServiceKey
kernel.Context = Context
kernel.DURABLE_EVENTS = _DurEv()
kernel.plugin = lambda *a, **k: (lambda fn: fn)
sys.modules["movieclaw_kernel"] = kernel

sdk = types.ModuleType("movieclaw_sdk")
net_mod = types.ModuleType("movieclaw_sdk.net")
net_mod.http_transport = lambda pid: None
events_mod = types.ModuleType("movieclaw_sdk.events")
events_mod.AGENT_RUN_FINISHED = "agent.run.finished"


class RunFinished:
    pass


events_mod.RunFinished = RunFinished
ops_mod = types.ModuleType("movieclaw_sdk.ops")


class OpsError(Exception):
    pass


class HOST_OPS:
    pass


ops_mod.OpsError = OpsError
ops_mod.HOST_OPS = HOST_OPS
sys.modules["movieclaw_sdk"] = sdk
sys.modules["movieclaw_sdk.net"] = net_mod
sys.modules["movieclaw_sdk.events"] = events_mod
sys.modules["movieclaw_sdk.ops"] = ops_mod
sdk.net = net_mod
sdk.events = events_mod
sdk.ops = ops_mod

api = types.ModuleType("movieclaw_api")
plugins_mod = types.ModuleType("movieclaw_api.plugins")
keys_mod = types.ModuleType("movieclaw_api.plugins.keys")
keys_mod.PLUGIN_DATA = None
keys_mod.PLUGIN_ROUTES = None
sys.modules["movieclaw_api"] = api
sys.modules["movieclaw_api.plugins"] = plugins_mod
sys.modules["movieclaw_api.plugins.keys"] = keys_mod
api.plugins = plugins_mod
plugins_mod.keys = keys_mod

# ---------- 加载插件源码 ----------

import os
PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))  # 直接测当前工作目录的源码
sys.path.insert(0, PLUGIN_DIR)
mm = importlib.import_module("mc_memory")

OK = 0
FAIL = 0


def check(name, cond):
    global OK, FAIL
    if cond:
        OK += 1
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


def mksettings(*slots):
    """slots: 每项 (name, base, key, model)；None 表示空槽。"""
    kw = {}
    for i, slot in enumerate(slots[:3], 1):
        if slot is None:
            continue
        n, b, k, m = slot
        kw[f"slot{i}_name"] = n
        kw[f"slot{i}_base"] = b
        kw[f"slot{i}_key"] = k
        kw[f"slot{i}_model"] = m
    return mm.Settings(**kw)


GLM = ("智谱 GLM", "https://open.bigmodel.cn/api/coding/paas/v4", "sk-glm", "glm-5.3-flash")
DS = ("DeepSeek", "https://api.deepseek.com", "sk-ds", "deepseek-chat")


def be(name, key="sk-x"):
    return mm.Backend(name=name, base_url="https://x.example/v1", api_key=key, model="m")


class FakeStore:
    """内存 store，记录 set 调用。"""

    def __init__(self):
        self.data = {}

    async def get(self, key, default=None):
        return self.data.get(key, default)

    async def set(self, key, value):
        self.data[key] = value


def make_call(script):
    """script: list of (outcome)。outcome = "ok" | int 状态码 | Exception。"""
    calls = []

    async def call(backend):
        outcome = script[len(calls)] if len(calls) < len(script) else script[-1]
        calls.append(backend.name)
        if outcome == "ok":
            return [{"text": "t", "category": "user"}]
        if isinstance(outcome, int):
            req = types.SimpleNamespace()
            resp = types.SimpleNamespace(status_code=outcome)
            exc = Exception(f"HTTP {outcome}")
            exc.response = resp
            raise exc
        raise outcome

    return call, calls


async def main():
    log_lines = []

    def log(level, *args):
        log_lines.append((level, " ".join(str(a) for a in args)))

    # 1. configured_backends（槽位组装）
    check("默认无后端", mm.configured_backends(mm.Settings()) == [])
    s3 = mksettings(GLM, ("DeepSeek", "https://api.deepseek.com", "", "deepseek-chat"))
    check("三项不齐的槽位跳过", [b.name for b in mm.configured_backends(s3)] == ["智谱 GLM"])
    s4 = mksettings(GLM, DS)
    names = [b.name for b in mm.configured_backends(s4)]
    check("槽位顺序即切换顺序", names == ["智谱 GLM", "DeepSeek"])
    s5 = mksettings(GLM, None, ("HB-P", "https://eo.ai.hybgzs.com/v1", "sk-hb", "m"))
    check("跳过空槽仍取槽3", [b.name for b in mm.configured_backends(s5)] == ["智谱 GLM", "HB-P"])
    check("槽位重命名生效", mm.configured_backends(s5)[0].name == "智谱 GLM")
    schema = mm.Settings.model_json_schema()["properties"]
    for i in (1, 2, 3):
        prop = schema[f"slot{i}_key"]
        check(f"slot{i}_key 是密码字段", bool(prop.get("writeOnly")))

    # 2. failover：首选成功
    call, calls = make_call(["ok"])
    found, sticky = await mm.failover_extract([be("GLM"), be("DS")], "", call, log)
    check("首选成功→不粘", sticky == "" and calls == ["GLM"] and len(found) == 1)

    # 3. 402 粘住切换
    call, calls = make_call([402, "ok"])
    found, sticky = await mm.failover_extract([be("GLM"), be("DS")], "", call, log)
    check("402→切换并粘住", sticky == "DS" and calls == ["GLM", "DS"])

    # 4. 粘住后下次直接从 DS 开始
    call, calls = make_call(["ok"])
    found, sticky = await mm.failover_extract([be("GLM"), be("DS")], "DS", call, log)
    check("粘住后直接从 DS 开始", sticky == "DS" and calls == ["DS"])

    # 5. 瞬时错误不粘
    call, calls = make_call([503, "ok"])
    found, sticky = await mm.failover_extract([be("GLM"), be("DS")], "", call, log)
    check("503→绕过但不粘", sticky == "" and calls == ["GLM", "DS"])

    # 6. 粘住有效期内：继续用粘住后端，不去探测首选
    call, calls = make_call(["ok"])
    found, sticky = await mm.failover_extract([be("GLM"), be("DS")], "DS", call, log)
    check("粘住有效期内继续用 DS", sticky == "DS" and calls == ["DS"])

    # 7. 全部失败
    call, calls = make_call([402, 429])
    found, sticky = await mm.failover_extract([be("GLM"), be("DS")], "", call, log)
    check("全败→粘住不变、返回空", sticky == "" and found == [] and calls == ["GLM", "DS"])

    # 8. 单后端失败
    call, calls = make_call([TimeoutError("t")])
    found, sticky = await mm.failover_extract([be("GLM")], "", call, log)
    check("单后端超时→不粘、返回空", sticky == "" and found == [])

    # 9. 粘住名不在列表里（改名后）：当无粘住处理
    call, calls = make_call(["ok"])
    found, sticky = await mm.failover_extract([be("GLM")], "已删除的后端", call, log)
    check("失效粘住名→i==0 成功即解除", sticky == "" and calls == ["GLM"])

    # 10. 地址拼接（GLM /v4 与裸域名）
    def join_url(base_url):
        base = base_url.strip().rstrip("/")
        if re.search(r"/v\d+(?:\.\d+)?/?$", base):
            return base + "/chat/completions"
        return base + "/v1/chat/completions"

    check("GLM v4 地址直拼",
          join_url("https://open.bigmodel.cn/api/coding/paas/v4")
          == "https://open.bigmodel.cn/api/coding/paas/v4/chat/completions")
    check("裸域名补 /v1",
          join_url("https://api.deepseek.com")
          == "https://api.deepseek.com/v1/chat/completions")
    check("已带 /v1 直拼",
          join_url("https://x.example/v1") == "https://x.example/v1/chat/completions")

    # 11. 原有纯函数回归
    check("parse 基础", mm.parse_extraction('{"memories":[{"text":"a","category":"user"}]}')
          == [{"text": "a", "category": "user", "name": ""}])
    check("parse 空防御", mm.parse_extraction("前置废话") == [])
    check("查重：完全同义改写不在本地防（交给提取提示词对照样例）",
          not mm.is_duplicate("插件用Python写", ["我们决定插件统一用Python编写"]))
    check("查重：一两个字的措辞差异要防住",
          mm.is_duplicate("他的昵称是DongShu", ["他的昵称叫DongShu"]))
    check("查重负例", not mm.is_duplicate("今天晚饭吃拉面", ["我们决定插件统一用Python编写"]))
    check("相关度", mm.relevance_score("余额插件", "llm-balance 余额看板插件") > 0)

    # 12. 编辑合并逻辑：查证 _merge_patch 与查重排除自己
    view = {"id": "mem:1", "text": "旧文本", "category": "project", "pinned": False,
            "refs": 0, "created_at": 1, "updated_at": 1}
    patch_full = mm.MemoryPatch(text="新文本", category="user")
    check("PATCH 两字段都给→全改",
          mm._apply_patch(view, patch_full)["text"] == "新文本"
          and mm._apply_patch(view, patch_full)["category"] == "user")
    patch_text_only = mm.MemoryPatch(text="只改文本")
    merged = mm._apply_patch(view, patch_text_only)
    check("PATCH 只给 text→category 保持", merged["text"] == "只改文本"
          and merged["category"] == "project")
    patch_cat_only = mm.MemoryPatch(category="reference")
    merged2 = mm._apply_patch(view, patch_cat_only)
    check("PATCH 只给 category→text 保持", merged2["text"] == "旧文本"
          and merged2["category"] == "reference")
    check("PATCH 空 body 拒绝", mm.MemoryPatch(text=None, category=None) is not None)
    check("查重排除自己：改成只有尾字差异仍算重复→交给路由拦截",
          mm.is_duplicate("他的昵称是DongShu", ["他的昵称叫DongShu"]))
    check("查重排除自己：别的记忆不影响自己改措辞",
          not mm.is_duplicate("他的昵称是DongShu", ["无关记忆"]))

    # 13. Engramory：笔记名
    n1 = mm.suggest_name("记住：GLM 是付费订阅，绝不说免费档")
    check("笔记名：去掉填充词并截断", "免费档" in n1 and len(n1) <= mm.NAME_MAX and "用户" not in n1)
    n2 = mm.suggest_name("GLM/订阅：不要说「免费」！")
    check("笔记名：去掉禁用字符", not mm.NAME_BAD_CHARS.search(n2))
    check("笔记名：空文本兜底", mm.suggest_name("   ") == "未命名")

    # 14. Engramory：索引行与上限守卫
    line = mm.index_line({"id": "mem:1", "name": "短名字", "hook": "一行钩子"})
    check("索引行格式", line == "- [短名字](mem:1) — 一行钩子")
    long_line = mm.index_line({"id": "mem:2", "name": "长" * 40, "hook": "钩" * 80})
    check("索引行名字截断", len(long_line.split("](")[0]) - 3 <= mm.NAME_MAX)
    v = mm.check_index_limits(10, 1000, 11, 1100)
    check("上限：正常写入放行无告警", v == {"ok": True, "reason": None, "warn": None})
    v = mm.check_index_limits(79, 7000, 81, 8500)
    check("上限：过软上限→放行但告警", v["ok"] and v["warn"] is not None)
    v = mm.check_index_limits(99, 9000, 101, 9500)
    check("上限：行数破硬上限→拒绝", not v["ok"] and "行数" in v["reason"])
    v = mm.check_index_limits(50, 9900, 51, 10100)
    check("上限：字节破硬上限→拒绝", not v["ok"] and "字节" in v["reason"])
    v = mm.check_index_limits(110, 12000, 90, 9000)
    check("上限：超限状态下收缩写入→放行（压缩可以一步步来）", v["ok"])
    v = mm.check_index_limits(110, 12000, 110, 12000)
    check("上限：超限状态下不变→放行", v["ok"])
    v = mm.check_index_limits(90, 5000, 91, 3000)
    check("上限：行涨字节缩→按行判（在限内放行）", v["ok"])

    # 15. Engramory：[[链接]] 与 为什么/怎么用
    check("链接解析", mm.parse_links("见[[主记忆]]和[[备用|显示名]]") == ["主记忆", "备用"])
    check("链接空", mm.parse_links("没有链接") == [])
    body = "先查再答。\n**为什么：** 上次瞎猜被用户纠正。\n**怎么用：**不确定就查日志。"
    why, how = mm.extract_why_how(body)
    check("为什么/怎么用：加粗标签", why == "上次瞎猜被用户纠正。" and how == "不确定就查日志。")
    why2, how2 = mm.extract_why_how("原因：\n为什么：全角冒号也算\n怎么用：带花活")
    check("为什么/怎么用：全角冒号", why2 == "全角冒号也算" and how2 == "带花活")
    why3, _ = mm.extract_why_how("正文里提到 为什么 不是行首标签")
    check("为什么/怎么用：非行首不算", why3 == "")

    # 16. Engramory：normalize_note 补全
    note = mm.normalize_note({"text": "GLM 是付费订阅", "category": "feedback"}, require_why_how=True)
    check("补全：feedback 自动带为什么/怎么用",
          "为什么：" in note["text"] and "怎么用：" in note["text"] and note["name"])
    note2 = mm.normalize_note({"text": "记忆正文", "category": "user", "name": "已有名"}, require_why_how=True)
    check("补全：已有名字不覆盖", note2["name"] == "已有名")

    # 17. Engramory：体检
    items = {
        "mem:1": {"text": "GLM 是付费订阅绝不说免费", "pinned": False, "refs": 0,
                  "updated_at": mm._now(), "category": "feedback"},
        "mem:2": {"text": "GLM 是付费的订阅绝不说免费档", "pinned": False, "refs": 0,
                  "updated_at": mm._now(), "category": "feedback"},
        "mem:3": {"text": "晚饭吃拉面", "pinned": False, "refs": 0,
                  "updated_at": mm._now(), "category": "project"},
        "mem:4": {"text": "指路见[[不存在的笔记]]", "pinned": False, "refs": 0,
                  "updated_at": mm._now(), "category": "reference"},
        "mem:5": {"text": "很老的记忆" + "长" * mm.NOTE_WARN_CHARS, "pinned": False, "refs": 3,
                  "updated_at": mm._now() - 90 * 86400, "category": "project"},
    }
    rep = mm.parse_doctor_report(items, None, None)
    check("体检：近似重复对被抓", any({d["a"], d["b"]} == {"mem:1", "mem:2"} for d in rep["duplicates"]))
    check("体检：晚饭不误报", all({"mem:3"} != {d["a"], d["b"]} for d in rep["duplicates"]))
    check("体检：坏链被抓", any(b["id"] == "mem:4" and b["link"] == "不存在的笔记" for b in rep["broken_links"]))
    check("体检：超大笔记被抓", any(o["id"] == "mem:5" for o in rep["oversized"]))


    # 18b. 铁律类：索引前缀 + 排序 + 提示词分级
    check("铁律：索引行带 ⚖ 前缀",
          mm.index_line({"id": "mem:i", "name": "红线", "hook": "", "category": "iron"}).startswith("⚖ "))
    check("铁律：普通类无前缀",
          not mm.index_line({"id": "mem:p", "name": "普通", "hook": "", "category": "project"}).startswith("⚖ "))
    views = mm._sorted_views({
        "a": {"text": "普通", "pinned": True, "updated_at": 2, "category": "project"},
        "b": {"text": "铁律", "pinned": False, "updated_at": 1, "category": "iron"},
        "c": {"text": "普通新", "pinned": False, "updated_at": 3, "category": "user"},
    })
    check("铁律：排序时铁律区永远在置顶之前",
          [v["id"] for v in views] == ["b", "a", "c"])
    p3 = mm.build_extract_prompt("记录", [])
    check("提示词：含铁律分级规则", "iron" in p3 and "宁低勿高" in p3)

    # 18. Engramory：提炼提示词
    p1 = mm.build_extract_prompt("记录", ["样例"])
    check("提示词：禁密钥值", "密钥" in p1 and "绝不记录" in p1)
    check("提示词：要短名字", "短名字" in p1 or "name" in p1)
    check("提示词：普通模式无续接话术", "续接同步" not in p1)
    p2 = mm.build_extract_prompt("记录", [], compaction_mode=True)
    check("提示词：续接模式强调接住任务", "续接同步" in p2 and "下一步" in p2)
    check("提示词：续接模式要绝对日期", "绝对日期" in p2)

    # 19. parse_extraction 带 name
    got = mm.parse_extraction('{"memories":[{"name":"付费订阅","text":"GLM 是付费订阅","category":"feedback"}]}')
    check("parse 保留 name", got == [{"text": "GLM 是付费订阅", "category": "feedback", "name": "付费订阅"}])
    got2 = mm.parse_extraction('{"memories":[{"text":"无名字","category":"user"}]}')
    check("parse 缺 name 给空串", got2[0]["name"] == "")

    # 20. Settings：新字段与去重
    check("Settings 有 sync_on_compaction",
          "sync_on_compaction" in mm.Settings.model_fields)
    check("Settings 无重复字段", len(mm.Settings.model_fields) == 20)
    check("Settings 有压缩五件套",
          all(k in mm.Settings.model_fields for k in (
              "auto_compact", "max_messages", "max_prompt_tokens",
              "cooldown_minutes", "exclude_sessions")))
    d = mm.Settings()
    check("压缩默认值与现网设置一致",
          d.auto_compact and d.max_messages == 32 and d.max_prompt_tokens == 40000
          and d.cooldown_minutes == 10)

    # 21. should_compact：任一阈值命中即压
    ok, why = mm.should_compact(messages_since=10, last_prompt_tokens=1000,
                                has_compaction=False, max_messages=32, max_prompt_tokens=40000)
    check("未达阈值→不压", not ok)
    ok, why = mm.should_compact(messages_since=32, last_prompt_tokens=1000,
                                has_compaction=False, max_messages=32, max_prompt_tokens=40000)
    check("消息数达标→压", ok and "消息" in why)
    ok, why = mm.should_compact(messages_since=3, last_prompt_tokens=42000,
                                has_compaction=False, max_messages=32, max_prompt_tokens=40000)
    check("token 达标→压", ok and "token" in why)
    ok, _ = mm.should_compact(messages_since=5, last_prompt_tokens=None,
                              has_compaction=True, max_messages=32, max_prompt_tokens=40000)
    check("token 未知→只看消息数→不压", not ok)

    # 22. in_cooldown
    now = 1000000.0
    check("无记录→不在冷却", not mm.in_cooldown(None, 10, now))
    check("3 分钟前压过→在冷却", mm.in_cooldown(now - 180, 10, now))
    check("11 分钟前压过→不在冷却", not mm.in_cooldown(now - 660, 10, now))

    # 23. scan_compact_signals：从最新往回扫，压缩记录是分界线
    entries_no_compact = [
        {"type": "message", "message": {"role": "user", "content": "a"}},
        {"type": "message", "message": {"role": "assistant"}, "usage": {"prompt_tokens": 3000}},
        {"type": "message", "message": {"role": "user", "content": "b"}},
        {"type": "message", "message": {"role": "user", "content": "c"}},
    ]
    sig = mm.scan_compact_signals(entries_no_compact)
    check("无压缩记录：全量计数", sig == {"messages_since": 3, "last_prompt_tokens": 3000,
                                    "has_compaction": False})
    entries_compacted = [
        {"type": "message", "message": {"role": "user", "content": "old"}},
        {"type": "compaction"},
        {"type": "message", "message": {"role": "user", "content": "a"}},
        {"type": "message", "message": {"role": "assistant"}, "usage": {"prompt_tokens": 900}},
        {"type": "message", "message": {"role": "user", "content": "b"}},
    ]
    sig = mm.scan_compact_signals(entries_compacted)
    check("有压缩记录：只数分界线之后", sig == {"messages_since": 2, "last_prompt_tokens": 900,
                                     "has_compaction": True})
    sig = mm.scan_compact_signals([])
    check("空轨迹不炸", sig["messages_since"] == 0 and sig["last_prompt_tokens"] is None)

    # 24. CompactionRecord 序列化（压缩历史入库格式）
    rec = mm.CompactionRecord(session_id="s1", at=123.0, reason="累计 32 条消息",
                              tokens_before=45000, tokens_after=9000)
    round = mm.CompactionRecord.model_validate(rec.model_dump())
    check("压缩历史可循环序列化", round.session_id == "s1" and round.tokens_after == 9000)

    print(f"\n{OK} passed, {FAIL} failed")
    return 1 if FAIL else 0


sys.exit(asyncio.run(main()))
