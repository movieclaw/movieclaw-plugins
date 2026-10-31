"""纯函数：中文判断、姓名归一、豆瓣演员匹配、演员表合并。

这里不碰网络、不碰文件、不碰宿主操作，全部可以在单测里直接跑。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field, replace

from .settings import AvatarPolicy, Condition

#: 中日韩统一表意文字 / 扩展 A / 兼容区；够判断“是否中文名/角色”
CJK = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]")
#: 中文角色名后面跟着的拉丁原文尾巴，如「詹姆斯·邦德 James Bond 007」
_LATIN_ROLE_TAIL = re.compile(r"\s+[A-Za-z][A-Za-z0-9 .'’\-]*$")
#: 姓名里的标点/空白（保留中日韩字符与字母数字）
_NAME_NOISE = re.compile(r"[^\w\u3400-\u9fff]+|_")
#: 从「丹尼尔·克雷格 Daniel Craig」里取中文部分
_LATIN_PART = re.compile(r"[A-Za-z][A-Za-z0-9 .'’\-]*")


@dataclass(frozen=True)
class DoubanActor:
    """豆瓣 celebrities 接口里的一位演员（只保留用得到的字段）。"""

    name: str
    latin_name: str | None = None
    character: str | None = None
    avatar_url: str | None = None
    douban_id: str | None = None


@dataclass(frozen=True)
class CastEntry:
    """条目现有演员表的一行（对应 media_metadata.cast JSON）。"""

    name: str
    role: str | None = None
    thumb_url: str | None = None
    tmdb_person_id: int | None = None
    order: int = 0


@dataclass(frozen=True)
class Change:
    """一位演员的前后差异，供 dry-run / 报告使用。"""

    actor: str
    name_before: str
    name_after: str
    role_before: str | None
    role_after: str | None
    thumb_before: str | None
    thumb_after: str | None
    matched: bool
    name_changed: bool
    role_changed: bool
    thumb_changed: bool


@dataclass
class MergeResult:
    actors: list[CastEntry]
    changes: list[Change] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    matched: int = 0
    unresolved: list[str] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.dropped) or any(
            c.name_changed or c.role_changed or c.thumb_changed for c in self.changes
        )


def has_chinese(text: str | None) -> bool:
    return bool(text and CJK.search(text))


def cjk_part(text: str | None) -> str:
    """取字符串里的中文部分；没有中文返回空串。"""
    if not text:
        return ""
    return "".join(CJK.findall(text))


#: 姓名归一：全半角/重音/大小写/标点/空白都不影响匹配。
#: 另折叠日文汉字与中文简体的常见字形差异（聡/聪、瀬/濑、戸/户……）：
#: TMDB 保留日文原字，豆瓣转简体，不折叠同名演员就配不上。
_NAME_VARIANTS = str.maketrans({
    "聡": "聪",
    "瀬": "濑",
    "戸": "户",
    "辺": "边",
    "澤": "泽",
    "舘": "馆",
    "廣": "广",
    "榮": "荣",
    "櫻": "樱",
    "龍": "龙",
    "實": "实",
    "実": "实",
    "圓": "圆",
    "潤": "润",
    "詠": "咏",
    "麗": "丽",
    "禮": "礼",
    "豐": "丰",
    "慶": "庆",
    "恵": "惠",
    "姫": "姬",
    "穂": "穗",
})


def normalize_name(text: str | None) -> str:
    """姓名归一：全半角/重音/大小写/标点/空白/日中字形差异都不影响匹配。"""
    if not text:
        return ""
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    folded = stripped.translate(_NAME_VARIANTS)
    return _NAME_NOISE.sub("", folded).casefold()


def clean_role(role: str | None) -> str | None:
    """清理豆瓣角色名：去「饰/饰演」前缀，中文角色去掉后面的拉丁原文。"""
    text = (role or "").strip()
    if not text:
        return None
    text = re.sub(r"^(?:饰演|饰)\s*", "", text)
    if CJK.search(text):
        text = _LATIN_ROLE_TAIL.sub("", text)
    text = text.strip()
    return text or None


def absolute_thumb(url: str | None, base_url: str | None) -> str | None:
    """NFO 的 <thumb> 必须是 http(s) 绝对地址；本地 /images/... 用外部地址补全。"""
    if not url:
        return None
    url = url.strip()
    if url.startswith(("http://", "https://")):
        return url
    if url.startswith("/") and base_url:
        return f"{base_url.rstrip('/')}{url}"
    return None


#: 主程序本机头像地址（/images/people/<档位>/<两位>/<TMDB 文件名>，可带主机）。
#: 头像文件名本身就是 TMDB 头像路径——把本机地址归一化成 TMDB original URL 写进
#: NFO，主程序吸收后经 profile_path_of 还原为路径，头像随档位/重建自愈；
#: 写本机绝对 URL 则一旦头像目录清理/换档就会断链（详情页 nfo_thumb 优先）。
_LOCAL_PEOPLE_URL = re.compile(
    r"^(?:https?://[^/]+)?/images/people/[a-z0-9]+/[A-Za-z0-9_-]{2}/"
    r"([A-Za-z0-9_-]+\.(?:jpg|jpeg|png|webp))$"
)


def local_avatar_to_tmdb(url: str | None) -> str | None:
    """本机演职员头像 URL → TMDB original 绝对 URL；不是本机头像地址返回 None。"""
    if not url:
        return None
    matched = _LOCAL_PEOPLE_URL.match(url.strip())
    if not matched:
        return None
    return f"https://image.tmdb.org/t/p/original/{matched.group(1)}"


def _candidates(actor: CastEntry, douban: list[DoubanActor]) -> list[DoubanActor]:
    """按姓名找候选；中文名优先中文匹配，外文名优先 latin_name 匹配。

    中文名匹配两级：先按归一化后完全相等；失败再做包含兜底——豆瓣常把假名
    名字转译成中文（上坂すみれ → 上坂堇，归一化后只剩“上坂”），TMDB 保留
    原文。包含匹配要求短的一侧至少 2 个字，且歧义时交给 match_actor 用
    角色收窄、仍不唯一就放弃。
    """
    if has_chinese(actor.name):
        target = normalize_name(cjk_part(actor.name) or actor.name)
        if not target:
            return []
        exact = [d for d in douban if normalize_name(cjk_part(d.name) or d.name) == target]
        if exact:
            return exact
        if len(target) < 2:
            return []
        partial = []
        for d in douban:
            other = normalize_name(cjk_part(d.name) or d.name)
            if not other or other == target:
                continue
            if (target in other or other in target) and min(len(target), len(other)) >= 2:
                partial.append(d)
        return partial

    target = normalize_name(actor.name)
    if not target:
        return []
    by_latin = [d for d in douban if d.latin_name and normalize_name(d.latin_name) == target]
    if by_latin:
        return by_latin
    return [d for d in douban if normalize_name(d.name) == target]


def match_actor(actor: CastEntry, douban: list[DoubanActor]) -> DoubanActor | None:
    """一位现有演员 → 豆瓣演员；匹配不到或候选有歧义时返回 None。"""
    candidates = _candidates(actor, douban)
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    # 同名多人：用现有角色再收窄一次；仍不唯一就放弃，绝不按顺序硬配
    role_key = normalize_name(clean_role(actor.role))
    if role_key:
        by_role = [d for d in candidates if normalize_name(clean_role(d.character)) == role_key]
        if len(by_role) == 1:
            return by_role[0]
    return None


def _needs_gate(actor: CastEntry, condition: Condition, force_name: bool, force_role: bool) -> bool:
    name_missing = force_name or not has_chinese(actor.name)
    role_missing = force_role or not has_chinese(actor.role)
    if condition is Condition.NAME:
        return name_missing
    if condition is Condition.ROLE:
        return role_missing
    return name_missing or role_missing


def _should_drop(actor: CastEntry, condition: Condition) -> bool:
    name_no_zh = not has_chinese(actor.name)
    role_no_zh = not has_chinese(actor.role)
    if condition is Condition.NAME:
        return name_no_zh
    if condition is Condition.ROLE:
        return role_no_zh
    return name_no_zh and role_no_zh


def merge_cast(
    actors: list[CastEntry],
    douban: list[DoubanActor],
    *,
    condition: Condition = Condition.ALL,
    force_name: bool = False,
    force_role: bool = False,
    remove_non_chinese: bool = False,
    remove_no_avatar: bool = False,
    avatar_policy: AvatarPolicy = AvatarPolicy.TMDB_FIRST,
    base_url: str | None = None,
    keep_person_links: bool = True,
) -> MergeResult:
    """把豆瓣演员表合并进现有演员表。

    - 只改「处理条件」命中的演员；没命中的原样保留。
    - 默认只补非中文/空值，``force_*`` 才覆盖中文。
    - 改名前若现有头像是相对路径且没有外部地址，跳过改名以保住头像（只改角色）。
    - 返回的 ``CastEntry.thumb_url`` 已经是可写进 NFO 的绝对 URL 或 None。
    """
    result = MergeResult(actors=[])
    seen: set[str] = set()

    for actor in actors:
        matched = match_actor(actor, douban)
        gate = _needs_gate(actor, condition, force_name, force_role)

        if matched is None:
            if gate:
                result.unresolved.append(actor.name)
                if remove_non_chinese and _should_drop(actor, condition):
                    result.dropped.append(actor.name)
                    continue
            # 未匹配的演员也把本机头像地址归一化成 TMDB URL，避免本机文件
            # 清理/换档后头像断链
            kept_thumb = local_avatar_to_tmdb(actor.thumb_url) or actor.thumb_url
            if remove_no_avatar and kept_thumb is None:
                result.dropped.append(actor.name)
                continue
            result.actors.append(
                replace(actor, thumb_url=kept_thumb, order=len(result.actors))
            )
            result.changes.append(
                Change(
                    actor=actor.name,
                    name_before=actor.name,
                    name_after=actor.name,
                    role_before=actor.role,
                    role_after=actor.role,
                    thumb_before=actor.thumb_url,
                    thumb_after=kept_thumb,
                    matched=False,
                    name_changed=False,
                    role_changed=False,
                    thumb_changed=kept_thumb != actor.thumb_url,
                )
            )
            continue

        result.matched += 1
        new_name = actor.name
        new_role = actor.role
        # 现有头像若指向本机头像目录，先归一化成 TMDB URL（详见 local_avatar_to_tmdb）
        actor_thumb = local_avatar_to_tmdb(actor.thumb_url) or actor.thumb_url
        new_thumb = actor_thumb

        # 姓名：只接受中文豆瓣名，除非用户强制
        if (force_name or not has_chinese(actor.name)) and matched.name and (
            force_name or has_chinese(matched.name)
        ):
            new_name = matched.name.strip()

        # 角色：只接受中文角色，除非用户强制
        douban_role = clean_role(matched.character)
        if (force_role or not has_chinese(actor.role)) and douban_role and (
            force_role or has_chinese(douban_role)
        ):
            new_role = douban_role

        # 头像：先定“来源”，再转成绝对地址
        source_thumb = actor_thumb
        if (
            avatar_policy is AvatarPolicy.DOUBAN_FIRST
            and matched.avatar_url
            or avatar_policy is not AvatarPolicy.NONE
            and not source_thumb
        ):
            source_thumb = matched.avatar_url
        if avatar_policy is AvatarPolicy.NONE:
            source_thumb = actor_thumb
        new_thumb = absolute_thumb(source_thumb, base_url)

        # 改名后主程序无法按姓名带回原 profile_path；如果现有头像是相对路径
        # 又没有外部地址，就不改名，只改角色，避免头像丢失。
        if new_name != actor.name and actor_thumb and new_thumb is None:
            result.warnings.append(
                f"{actor.name}：缺少外部访问地址，保留原名以保住头像，只更新角色"
            )
            new_name = actor.name
            new_thumb = actor_thumb
        elif new_thumb is None and source_thumb == actor_thumb:
            # 没有改动来源、又无法绝对化：保留原值，避免被当成“删头像”
            new_thumb = actor_thumb

        entry = CastEntry(
            name=new_name,
            role=new_role,
            thumb_url=new_thumb,
            # 改名后人物页仍是 TMDB 名；用户可以选择不保留这个“不一致的链接”
            tmdb_person_id=(
                actor.tmdb_person_id if keep_person_links or new_name == actor.name else None
            ),
            order=actor.order,
        )
        if new_name in seen:
            result.warnings.append(f"{new_name}：合并后姓名重复，保留第一项")
            continue
        seen.add(new_name)

        if remove_non_chinese and _should_drop(entry, condition):
            result.dropped.append(new_name)
            continue
        if remove_no_avatar and entry.thumb_url is None:
            result.dropped.append(new_name)
            continue

        result.actors.append(replace(entry, order=len(result.actors)))
        result.changes.append(
            Change(
                actor=actor.name,
                name_before=actor.name,
                name_after=new_name,
                role_before=actor.role,
                role_after=new_role,
                thumb_before=actor.thumb_url,
                thumb_after=new_thumb,
                matched=True,
                name_changed=new_name != actor.name,
                role_changed=new_role != actor.role,
                # 与原始输入比较：本机头像地址归一化成 TMDB URL 也算变化，
                # 这样 NFO 里的旧地址会被修正重写
                thumb_changed=new_thumb != actor.thumb_url,
            )
        )

    # 主程序演职员条最多展示 40 位
    if len(result.actors) > 40:
        result.actors = result.actors[:40]
        result.warnings.append("演员表超过 40 位，已按原顺序截断")

    return result
