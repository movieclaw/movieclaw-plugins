"""刮削器写进来的「不是艺名」的演员串。

素人片的刮削结果常把介绍文字当演员名，例如 ``京香 大学生モデル``、
``かすみ 28歳 食品メーカー勤務Webデザイナー``、``あおいさん``。这些串拿去匹配只会误配
（或者永远配不上还占着待确认 / AI 的额度），所以先挑出来单独列，用户可以手动处理。
"""

from __future__ import annotations

import re
import unicodedata

#: 年龄：``23歳`` ``23才`` ``(23)``
_AGE = re.compile(r"\d{1,2}\s*(?:歳|才|岁)|[(（]\d{2}[)）]")
#: 介绍文字里常见的职业 / 身份词（只在名字带空白、即「名字 + 介绍」时才看）
_DESCRIPTORS = (
    "大学生",
    "女子大生",
    "専門学生",
    "学生",
    "モデル",
    "勤務",
    "会社員",
    "社員",
    "店員",
    "受付",
    "看護",
    "保育",
    "美容",
    "主婦",
    "人妻",
    "新妻",
    "OL",
    "ホール",
    "居酒屋",
    "アルバイト",
    "バイト",
    "デザイナー",
    "スタッフ",
    "メーカー",
    "販売",
    "事務",
    "経営",
    "インストラクター",
    "エステ",
    "ナース",
    "家庭教師",
    "介護",
    "銀行",
    "秘書",
    "嬢",
)
#: 只用称呼当名字：``あおいさん``
_HONORIFIC = re.compile(r"^\S{1,8}(?:さん|様|さま)$")
#: 整串就是占位词
_PLACEHOLDERS = {
    "素人",
    "素人娘",
    "名無し",
    "不明",
    "未知",
    "佚名",
    "匿名",
    "其他",
    "unknown",
    "n/a",
    "none",
    "null",
    "-",
}
_MAX_LEN = 24


def junk_reason(name: str) -> str | None:
    """不是艺名就返回原因（给页面显示），像艺名返回 ``None``。"""
    raw = unicodedata.normalize("NFKC", name or "").strip()
    if not raw:
        return None
    if raw.lower() in _PLACEHOLDERS:
        return "占位词"
    if _AGE.search(raw):
        return "带年龄"
    words = [w for w in re.split(r"\s+", raw) if w]
    if len(words) > 1 and any(d in w for w in words[1:] for d in _DESCRIPTORS):
        return "名字后面跟着职业介绍"
    if len(words) > 2:
        return "像一句介绍"
    if _HONORIFIC.match(raw):
        return "只有称呼（…さん）"
    if len(raw) > _MAX_LEN:
        return "太长，不像名字"
    return None
