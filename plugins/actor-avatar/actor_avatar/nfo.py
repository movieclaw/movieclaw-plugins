"""sidecar NFO 的最小改写：只动 ``<actor><thumb>``，别的一个字节都不该变。

规则（M0 在本地 MovieClaw 0.46.0 上验证过，见 README）：

- 本地内容库刷新元数据时会把同名 sidecar NFO **全字段重读**：NFO 里有的字段覆盖档案，
  没有的保留；但 ``<actor>`` 是**整表替换**——NFO 里有演员，档案演员表就以它为准。
  所以新建 NFO 时必须把档案里现有的全部演员（含角色）一起写进去，否则会丢人。
- 新建 NFO 时同时写当前 ``<title>``：以后条目被重新识别（删库重扫）时片名不会退回文件名。
- 头像地址必须是 ``http(s)://`` 绝对地址：宿主读 sidecar 时会丢掉相对地址。
- 已有 NFO：只给「没有头像」或「头像是本插件写的」演员设置 ``<thumb>``；
  NFO 里一个演员都没有而档案里有时，按档案补上全部演员（否则刷新后只剩空表）。
- 写之前做不变式检查：去掉本插件写的 thumb 后，新树必须与原树逐节点相同。
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

DECLARATION = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
_DECL_RE = re.compile(r"^\s*<\?xml[^>]*\?>\s*", re.IGNORECASE)


class NfoError(Exception):
    """NFO 无法安全改写（解析失败、不变式不成立等）：本条跳过，不写盘。"""


def decode(data: bytes) -> str:
    """只接受 UTF-8：主程序按 UTF-8 读 NFO；别的编码改写成 UTF-8 会让主程序「突然读懂」
    原来读不出的字段，等于改了档案，所以直接跳过。"""
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise NfoError("NFO 不是 UTF-8 编码，为避免改动其他字段已跳过") from exc


def parse(text: str) -> ET.Element:
    body = _DECL_RE.sub("", text.lstrip("\ufeff"), count=1)
    parser = ET.XMLParser(target=ET.TreeBuilder(insert_comments=True, insert_pis=True))
    try:
        parser.feed(body)
        return parser.close()
    except ET.ParseError as exc:
        raise NfoError(f"NFO 不是合法 XML：{exc}") from exc


def serialize(root: ET.Element, original: str | None = None) -> str:
    """序列化。给了原文时，根元素前后的内容（XML 声明、前置注释等）按原样保留。"""
    body = ET.tostring(root, encoding="unicode")
    if original:
        prefix, suffix = _envelope(original, str(root.tag))
        if prefix is not None:
            return prefix + body + suffix
    return DECLARATION + body + "\n"


def _envelope(text: str, tag: str) -> tuple[str | None, str]:
    text = text.lstrip("\ufeff")
    start = re.search(r"<" + re.escape(tag) + r"(?=[\s>/])", text)
    end = text.rfind("</" + tag)
    if start is None or end < 0:
        return None, ""
    close = text.find(">", end)
    if close < 0:
        return None, ""
    return text[: start.start()], text[close + 1 :]


def actor_names(text: str | None) -> dict[str, str | None]:
    """NFO 里的演员 → 现有 thumb（没有为 None）。"""
    if not text:
        return {}
    root = parse(text)
    result: dict[str, str | None] = {}
    for actor in root.findall("actor"):
        name = (actor.findtext("name") or "").strip()
        if name and name not in result:
            result[name] = (actor.findtext("thumb") or "").strip() or None
    return result


@dataclass
class Edit:
    text: str
    created: bool
    #: 本次真正写下的 演员名 → thumb
    written: dict[str, str] = field(default_factory=dict)
    #: 因已有别人的头像而跳过的演员
    kept: list[str] = field(default_factory=list)
    #: 按档案补进 NFO 的演员数（NFO 原本没有演员表时）
    added_actors: int = 0

    @property
    def changed(self) -> bool:
        return self.created or bool(self.written) or self.added_actors > 0


def _new_actor(name: str, role: str | None, indent: str) -> ET.Element:
    actor = ET.Element("actor")
    inner = indent + "  "
    actor.text = inner
    name_el = ET.SubElement(actor, "name")
    name_el.text = name
    name_el.tail = inner
    if role:
        role_el = ET.SubElement(actor, "role")
        role_el.text = role
        role_el.tail = inner
    actor[-1].tail = indent
    return actor


def _set_thumb(actor: ET.Element, url: str, tracker: _Tracker) -> None:
    thumb = actor.find("thumb")
    if thumb is not None:
        tracker.replaced.setdefault(id(thumb), thumb.text)
        thumb.text = url
        return
    children = list(actor)
    thumb = ET.Element("thumb")
    thumb.text = url
    if children:
        last = children[-1]
        thumb.tail = last.tail
        last.tail = actor.text if actor.text and actor.text.strip() == "" else last.tail
    else:
        actor.text = "\n      "
        thumb.tail = "\n    "
    actor.append(thumb)
    tracker.skip.add(id(thumb))


@dataclass
class _Tracker:
    skip: set[int] = field(default_factory=set)
    replaced: dict[int, str | None] = field(default_factory=dict)


def apply_thumbs(
    text: str | None,
    wanted: dict[str, str],
    *,
    owned: dict[str, str] | None = None,
    db_actors: list[dict] | None = None,
    title: str | None = None,
) -> Edit:
    """把 ``wanted``（演员名 → 头像地址）写进 NFO。

    ``owned``：本插件以前写过的 演员名 → thumb；只有这些 thumb 允许被替换。
    ``db_actors``：档案里的演员（``{"name", "role"}``），新建/空演员表时用来补全。
    ``text`` 为 None 表示 NFO 不存在，新建一份（含 ``title`` 与全部演员）。
    """
    owned = owned or {}
    db_actors = [a for a in (db_actors or []) if str(a.get("name") or "").strip()]
    created = text is None
    tracker = _Tracker()
    if created:
        root = ET.Element("movie")
        root.text = "\n  "
        if title:
            title_el = ET.SubElement(root, "title")
            title_el.text = title
            title_el.tail = "\n"
        original = None
    else:
        root = parse(text)  # type: ignore[arg-type]
        original = _signature(root, _Tracker())

    edit = Edit(text="", created=created)
    if not root.findall("actor") and db_actors:
        children = list(root)
        if children:
            indent = root.text if (root.text or "").strip() == "" and root.text else "\n  "
            children[-1].tail = indent
        else:
            indent = "\n  "
            root.text = indent
        for index, info in enumerate(db_actors):
            actor = _new_actor(str(info["name"]).strip(), info.get("role") or None, indent)
            actor.tail = indent if index < len(db_actors) - 1 else "\n"
            root.append(actor)
            tracker.skip.add(id(actor))
        edit.added_actors = len(db_actors)

    seen: set[str] = set()
    for actor in root.findall("actor"):
        name = (actor.findtext("name") or "").strip()
        if not name or name in seen or name not in wanted:
            continue
        seen.add(name)
        current = (actor.findtext("thumb") or "").strip()
        if current == wanted[name]:
            continue
        if current and current != owned.get(name):
            edit.kept.append(name)
            continue
        _set_thumb(actor, wanted[name], tracker)
        edit.written[name] = wanted[name]

    if not edit.changed:
        edit.text = text or ""
        return edit
    if original is not None and _signature(root, tracker) != original:
        raise NfoError("改写后的 NFO 与原文不一致（不变式检查失败），已放弃写入")
    edit.text = serialize(root, text)
    return edit


def strip_thumbs(text: str, owned: dict[str, str]) -> tuple[str, list[str]]:
    """撤销：删掉 thumb 值与 ``owned`` 完全相同的那些 ``<thumb>``。返回 (新文本, 撤掉的演员)。"""
    root = parse(text)
    removed: list[str] = []
    for actor in root.findall("actor"):
        name = (actor.findtext("name") or "").strip()
        thumb = actor.find("thumb")
        if thumb is None or not name or name not in owned:
            continue
        if (thumb.text or "").strip() != owned[name]:
            continue  # 被别人改过：不碰
        children = list(actor)
        idx = children.index(thumb)
        if idx > 0:
            children[idx - 1].tail = thumb.tail
        else:
            actor.text = thumb.tail
        actor.remove(thumb)
        removed.append(name)
    if not removed:
        return text, []
    return serialize(root, text), removed


def _signature(root: ET.Element, tracker: _Tracker) -> tuple:
    """节点树的结构签名（忽略空白）；跳过 tracker 标记的新节点，被替换的 thumb 取原值。"""

    def node(el: ET.Element) -> tuple:
        text = tracker.replaced.get(id(el), el.text) if id(el) in tracker.replaced else el.text
        kids = tuple(node(child) for child in el if id(child) not in tracker.skip)
        return (
            str(el.tag),
            tuple(sorted(el.attrib.items())),
            (text or "").strip(),
            (el.tail or "").strip(),
            kids,
        )

    return node(root)
