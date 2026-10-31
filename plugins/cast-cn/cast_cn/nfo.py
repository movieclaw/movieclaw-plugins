"""条目 NFO 的解析、演员合并与序列化。

只做 XML 层；文件读写、备份、原子替换和授权检查在 runtime 里。
"""

from __future__ import annotations

import contextlib
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from dataclasses import dataclass

from .matching import CastEntry

MOVIE = "movie"
TVSHOW = "tvshow"
_ROOTS = {MOVIE, TVSHOW}


class NfoError(Exception):
    """NFO 不可安全修改：解析失败、根元素不对、身份不一致等。"""


@dataclass(frozen=True)
class NfoIdentity:
    kind: str
    tmdb_id: int
    title: str
    original_title: str | None
    year: int | None


def nfo_filename(kind: str) -> str:
    if kind == "movie":
        return "movie.nfo"
    if kind == "tv":
        return "tvshow.nfo"
    raise NfoError(f"不支持的内容类型：{kind}")


def parse_nfo(text: str) -> ET.Element:
    """解析条目 NFO；保留注释与处理指令。"""
    parser = ET.XMLParser(target=ET.TreeBuilder(insert_comments=True, insert_pis=True))
    try:
        root = ET.fromstring(text, parser=parser)
    except ET.ParseError as exc:
        raise NfoError(f"NFO 解析失败：{exc}") from exc
    if root.tag not in _ROOTS:
        raise NfoError(f"NFO 根元素不是 movie/tvshow：{root.tag!r}")
    return root


def _positive_int(value: str | None) -> int | None:
    text = (value or "").strip()
    if not text.isdigit():
        return None
    number = int(text)
    return number if number > 0 else None


def read_tmdb_id(root: ET.Element) -> int | None:
    """读条目 NFO 的 TMDB 条目 id：<tmdbid> 优先，其次 <uniqueid type="tmdb">。"""
    direct = _positive_int(root.findtext("tmdbid"))
    if direct is not None:
        return direct
    for node in root.findall("uniqueid"):
        if (node.get("type") or "").strip().lower() in {"tmdb", "tmdbid"}:
            value = _positive_int(node.text)
            if value is not None:
                return value
    return None


def build_identity_root(identity: NfoIdentity) -> ET.Element:
    """没有 NFO 时创建最小身份档（主程序写出的也是这一套字段）。"""
    root = ET.Element(MOVIE if identity.kind == "movie" else TVSHOW)
    ET.SubElement(root, "title").text = identity.title
    if identity.original_title:
        ET.SubElement(root, "originaltitle").text = identity.original_title
    if identity.year is not None:
        ET.SubElement(root, "year").text = str(identity.year)
    ET.SubElement(root, "tmdbid").text = str(identity.tmdb_id)
    unique = ET.SubElement(root, "uniqueid")
    unique.set("type", "tmdb")
    unique.set("default", "true")
    unique.text = str(identity.tmdb_id)
    return root


def ensure_identity(root: ET.Element, identity: NfoIdentity) -> bool:
    """确保 NFO 声明了当前条目的 tmdb id；身份不一致直接拒绝。"""
    declared = read_tmdb_id(root)
    if declared is not None and declared != identity.tmdb_id:
        raise NfoError(
            f"NFO 声明的 tmdbid={declared} 与条目 tmdb_id={identity.tmdb_id} 不一致，拒绝写入"
        )
    if declared is not None:
        return False

    # 没有身份声明：补一份；不改 title/year 等已有内容
    ET.SubElement(root, "tmdbid").text = str(identity.tmdb_id)
    unique = ET.SubElement(root, "uniqueid")
    unique.set("type", "tmdb")
    unique.set("default", "true")
    unique.text = str(identity.tmdb_id)
    return True


def _actor_element(actor: CastEntry, order: int) -> ET.Element:
    node = ET.Element("actor")
    ET.SubElement(node, "name").text = actor.name
    if actor.role:
        ET.SubElement(node, "role").text = actor.role
    thumb = (actor.thumb_url or "").strip()
    if thumb.startswith(("http://", "https://")):
        ET.SubElement(node, "thumb").text = thumb
    if actor.tmdb_person_id is not None:
        ET.SubElement(node, "tmdbid").text = str(actor.tmdb_person_id)
    ET.SubElement(node, "order").text = str(actor.order if actor.order else order)
    return node


def _actor_signature(node: ET.Element) -> tuple[str, str, str, str, str]:
    return (
        (node.findtext("name") or "").strip(),
        (node.findtext("role") or "").strip(),
        (node.findtext("thumb") or "").strip(),
        (node.findtext("tmdbid") or "").strip(),
        (node.findtext("order") or "").strip(),
    )


def merge_actors(root: ET.Element, actors: Iterable[CastEntry]) -> bool:
    """替换根元素下全部 <actor>；其他节点原样保留，返回是否真的变化。"""
    actors = list(actors)
    existing = root.findall("actor")
    children = list(root)
    insert_at = min(
        (children.index(node) for node in existing if node in children),
        default=len(children),
    )
    before = [_actor_signature(node) for node in existing]

    for node in existing:
        root.remove(node)

    new_nodes = [_actor_element(actor, index) for index, actor in enumerate(actors)]
    after = [_actor_signature(node) for node in new_nodes]
    for offset, node in enumerate(new_nodes):
        root.insert(insert_at + offset, node)
    return before != after


def serialize_nfo(root: ET.Element) -> str:
    """序列化为 UTF-8 NFO 文本；带 MovieClaw 同款 XML 声明。"""
    with contextlib.suppress(Exception):
        # 注释节点等极端情况下退化为紧凑输出
        ET.indent(root, space="  ")
    body = ET.tostring(root, encoding="unicode", short_empty_elements=True)
    return '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n' + body + "\n"
