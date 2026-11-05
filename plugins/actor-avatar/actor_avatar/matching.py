"""演员名 → gfriends 头像索引的匹配（纯函数，无网络、无宿主依赖）。

匹配分两档：

- **确认**：原名、NFKC/空白归一（含重复字「々」展开）、简/繁/日新字体折叠、括号别名、
  用户在面板里确认过的别名、MDCx 中文译名表（见 ``aliases.py``）。这些命中直接写入。
- **待确认**：从条目标题里「猜」出来的候选（标题末尾常带演员日文名）。猜测会误命中
  （「武藤彩香」里藏着「藤彩香」），所以只进待确认列表，绝不自动写入。

字形折叠表 ``fold_table.txt`` 是离线用 OpenCC（jp2t → t2s，再补 s2t → t2jp 的反向）
按单字生成的「异体 → 简体」映射，运行时不依赖 OpenCC。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

#: 文件名里表示「AI 修复版」的前缀（gfriends 约定：去掉前缀就是原图）
AI_FIX_PREFIX = "AI-Fix-"
#: 推断候选的最短长度（字符数）：太短的名字在标题里到处都能「找到」
MIN_INFER_LEN = 3

_BRACKETS = re.compile(r"[（(【\[]([^）)】\]]*)[）)】\]]")
_SPACES = re.compile(r"[\s\u3000·・･.]+")
_IMAGE_EXT = re.compile(r"\.(jpe?g|png|webp)$", re.IGNORECASE)
#: 重复符号：「野々浦」=「野野浦」，「いすゞ」=「いすず」
_REPEAT = {"々": "", "〻": "", "ゝ": "", "ヽ": "", "ゞ": "\u3099", "ヾ": "\u3099"}


@lru_cache(maxsize=1)
def _fold_map() -> dict[int, str]:
    text = (Path(__file__).with_name("fold_table.txt")).read_text(encoding="utf-8")
    return {ord(text[i]): text[i + 1] for i in range(0, len(text) - 1, 2)}


def expand_repeats(text: str) -> str:
    """把重复符号展开成前一个字：``野々浦暖`` → ``野野浦暖``，``いすゞ`` → ``いすず``。"""
    if not any(ch in _REPEAT for ch in text):
        return text
    out: list[str] = []
    for ch in text:
        if ch in _REPEAT and out:
            prev = out[-1]
            out.append(unicodedata.normalize("NFC", prev + _REPEAT[ch]) if _REPEAT[ch] else prev)
        else:
            out.append(ch)
    return "".join(out)


def basic(name: str) -> str:
    """NFKC + 去空白/间隔号 + 重复字展开 + 小写：全角半角、空格、「々」写法不同都归成一样。"""
    text = unicodedata.normalize("NFKC", name or "")
    return expand_repeats(_SPACES.sub("", text).strip()).lower()


def fold(name: str) -> str:
    """在 :func:`basic` 之上把简/繁/日新字体折叠成同一个字（黒→黑、涼→凉、澤/沢→泽）。"""
    return basic(name).translate(_fold_map())


def strip_brackets(name: str) -> str:
    return _BRACKETS.sub("", name or "").strip()


def bracket_aliases(name: str) -> list[str]:
    """括号里的别名：``三上悠亜（鬼頭桃菜）`` → ``["鬼頭桃菜"]``。"""
    return [m.strip() for m in _BRACKETS.findall(name or "") if m.strip()]


def display_name(filename: str) -> str:
    """Filetree 键（``三上悠亜.jpg``）→ 名字。"""
    return _IMAGE_EXT.sub("", filename or "").strip()


@dataclass(frozen=True)
class Avatar:
    """索引里的一张头像。``file`` 是仓库里的真实文件名（可能带 ``AI-Fix-`` 前缀）。"""

    name: str
    company: str
    file: str

    def pick(self, ai_fix: bool) -> str:
        """要下载的文件名：关掉 AI 修复时去掉前缀，取原图。"""
        return self.file if ai_fix else self.file.replace(AI_FIX_PREFIX, "", 1)


@dataclass(frozen=True)
class Match:
    avatar: Avatar
    how: str  # exact / basic / fold / bracket / alias / table / infer
    confirmed: bool
    query: str


class AvatarIndex:
    """gfriends Filetree 的查找索引。

    Filetree 的公司按质量**升序**排列，后面的覆盖前面的（与 gfriends-inputer 一致），
    所以同名时保留最后出现的那张。每个名字建三层键：原名、basic、fold；
    再把键里括号去掉/括号内别名作为较弱的键（不覆盖已有的强键）。
    """

    def __init__(self, filetree: dict) -> None:
        content = (filetree or {}).get("Content") or {}
        self.exact: dict[str, Avatar] = {}
        self.basic: dict[str, Avatar] = {}
        self.folded: dict[str, Avatar] = {}
        self.weak: dict[str, Avatar] = {}
        for company, files in content.items():
            if not isinstance(files, dict):
                continue
            for key, value in files.items():
                name = display_name(key)
                if not name:
                    continue
                real = str(value or key).split("?", 1)[0]
                avatar = Avatar(name=name, company=str(company), file=real)
                self.exact[name] = avatar
                self.basic[basic(name)] = avatar
                self.folded[fold(name)] = avatar
                for alias in [strip_brackets(name), *bracket_aliases(name)]:
                    if alias and alias != name:
                        self.weak[fold(alias)] = avatar

    def __len__(self) -> int:
        return len(self.exact)

    def by_name(self, name: str) -> Avatar | None:
        """按 gfriends 里的名字取（手动别名用）：原名或折叠后相等。"""
        raw = (name or "").strip()
        if not raw:
            return None
        return self.exact.get(raw) or self.folded.get(fold(raw))

    def search(self, query: str, limit: int = 20) -> list[Avatar]:
        """给页面的自动补全：前缀命中排前面，其次子串。"""
        key = fold(query or "")
        if not key:
            return []
        head: list[Avatar] = []
        rest: list[Avatar] = []
        for folded, avatar in self.folded.items():
            if folded.startswith(key):
                head.append(avatar)
            elif key in folded:
                rest.append(avatar)
            if len(head) >= limit:
                break
        head.sort(key=lambda a: len(a.name))
        rest.sort(key=lambda a: len(a.name))
        return (head + rest)[:limit]

    def _find(self, name: str) -> Avatar | None:
        return self.exact.get(name) or self.folded.get(fold(name)) or self.weak.get(fold(name))

    def lookup(
        self,
        name: str,
        aliases: dict[str, str] | None = None,
        table: Any = None,
    ) -> Match | None:
        """确认档的查找：命中就可以直接写。

        ``table`` 是 :class:`aliases.NameTable`（中文译名 → 日文原名），最后才查。
        """
        raw = (name or "").strip()
        if not raw:
            return None
        if aliases:
            target = aliases.get(raw) or aliases.get(fold(raw))
            if target:
                hit = self.exact.get(target) or self.folded.get(fold(target))
                if hit:
                    return Match(hit, "alias", True, raw)
        if raw in self.exact:
            return Match(self.exact[raw], "exact", True, raw)
        if basic(raw) in self.basic:
            return Match(self.basic[basic(raw)], "basic", True, raw)
        if fold(raw) in self.folded:
            return Match(self.folded[fold(raw)], "fold", True, raw)
        for alias in [strip_brackets(raw), *bracket_aliases(raw)]:
            if not alias or alias == raw:
                continue
            key = fold(alias)
            hit = self.folded.get(key) or self.weak.get(key)
            if hit:
                return Match(hit, "bracket", True, raw)
        if fold(raw) in self.weak:
            return Match(self.weak[fold(raw)], "bracket", True, raw)
        if table is not None:
            for alias in dict.fromkeys([raw, strip_brackets(raw), *bracket_aliases(raw)]):
                target = table.get(alias) if alias else None
                hit = self._find(target) if target else None
                if hit:
                    return Match(hit, "table", True, raw)
        return None

    def infer_from_title(self, title: str, exclude: set[str] | None = None) -> list[Avatar]:
        """从标题末尾的词里猜候选（只给待确认列表用）。

        只看标题按空白切开后的**最后两个词**里与索引名完全相等的词——番号片名的
        演员名基本都在末尾；整串子串搜索误命中太多，不做。
        """
        words = [w for w in re.split(r"[\s\u3000]+", title or "") if w]
        found: list[Avatar] = []
        seen = set(exclude or ())
        for word in words[-2:]:
            for part in [word, strip_brackets(word), *bracket_aliases(word)]:
                key = fold(part)
                if len(key) < MIN_INFER_LEN or key in seen:
                    continue
                hit = self.folded.get(key)
                if hit:
                    seen.add(key)
                    found.append(hit)
        return found
