"""115 输入适配层：复用 MovieClaw 的名称抽取库和宿主识别接口，不自建匹配引擎。

这里仅整理分享目录/文件的证据、检测冲突；哪部作品和进哪个库一律由宿主确认。
"""

import asyncio
import os
import re
import tempfile
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import PurePosixPath

import p115naming as naming
from movieclaw_enrich import enrich
from movieclaw_sdk.ops import OpsError

MODEL_DIRECTORY = "/app/models/torrent-ner"
MODEL_FILES = ("model.int8.onnx", "tokenizer.json", "labels.json")
MAX_RESOLVE_CALLS = 18
_PIN = re.compile(r"(?i)(?<![a-z0-9])tmdb(?:id)?\s*[-=:：]\s*([1-9]\d{0,9})(?!\d)")
_PIN_GROUP = re.compile(r"(?i)[\[{(]\s*tmdb(?:id)?\s*[-=:：]\s*\d+\s*[\]})]")
_AD_GROUP = re.compile(r"[\[【](?:[^\]】]*(?:公众号|公眾號|资源分享|資源分享|www\.|https?://|关注|關注|转自|轉自|严禁|嚴禁)[^\]】]*)[\]】]", re.I)
_TECH_GROUP = re.compile(r"[\[【](?:\s*(?:2160p|1080p|720p|4K|HDR10?\+?|DV|WEB-DL|BluRay|REMUX|中字|简繁|簡繁|内封|外挂|国粤|双语|多音轨|[ ._+,-]))+[\]】]", re.I)
_INDEX = re.compile(r"^[A-Za-z]\s*[-_]\s*(?=[\u3400-\u9fff])")
_CONVENTIONAL = re.compile(r"^(.+?)\s*[（(]((?:19|20)\d{2})[)）](?:\s|$|[.\[【_-])")
_CUT = re.compile(r"(?i)(?<![a-z0-9])(?:S\d{1,3}(?:[ ._-]*E\d{1,4})?|E[Pp]?\d{1,4}|(?:2160|1080|720|4320)[pi]|WEB[ ._-]*DL|Blu[ ._-]*Ray|BDRip|REMUX|HDTV|[xh][ .]?26[45]|HEVC|AVC|HDR10?\+?|DDP?\d|AAC|DTS)(?![a-z])|第\s*[0-9一二三四五六七八九十]+\s*[季集话話]|全\s*\d+\s*集")
_GENERIC = re.compile(r"^(?:season[ ._-]*\d+|s\d+|第[0-9一二三四五六七八九十]+季|电影|電影|剧集|劇集|电视剧|電視劇|国产剧|日韩剧|欧美剧|大陆|欧美|日韩|韩国|日本|中国|资源|資源|影剧资源|影视资源|影视|分享|视频|影片|合集|待整理|movies?|tv|videos?|downloads?|extras?|bonus|4k|1080p|2160p|[0-9 ._-]+)$", re.I)
_HASH = re.compile(r"^[a-f0-9]{16,}$", re.I)


class CoreParser:
    """经授权文件接口读取本体模型，在插件私有临时空间中给本体抽取库使用。"""

    def __init__(self, files, logger):
        self.files = files
        self.logger = logger
        self.lock = asyncio.Lock()
        self.ready = False
        self.temp = None

    async def ensure(self):
        if self.ready:
            return
        async with self.lock:
            if self.ready:
                return
            temp = tempfile.TemporaryDirectory(prefix="p115-core-ner-")
            try:
                for name in MODEL_FILES:
                    # 宿主检查 read 授权后递交文件描述符；不直接绕过文件接口读服务器。
                    with await self.files.open(f"{MODEL_DIRECTORY}/{name}", "rb") as source:
                        data = await asyncio.to_thread(source.read, 32 * 1024 * 1024 + 1)
                    if not data or len(data) > 32 * 1024 * 1024:
                        raise ValueError("模型文件为空或过大")
                    # 仅写本插件的临时工作区，不写回模型或媒体目录。
                    await asyncio.to_thread(_write_temporary, temp.name, name, data)
                os.environ["MOVIECLAW_NER_DIR"] = temp.name
                probe = await asyncio.to_thread(parsed, "Mousetrap.2026.S01E01.2160p.mkv")
                if not probe.get("titles_en") or probe.get("year") != 2026:
                    raise RuntimeError("本体名称模型没有成功加载，已停止识别，未执行任何转存")
            except Exception:
                temp.cleanup()
                raise
            self.temp = temp
            self.ready = True
            self.logger.info("已接入 MovieClaw 本体的文件名抽取库与只读模型")

    def close(self):
        if self.temp:
            self.temp.cleanup()
            self.temp = None
        self.ready = False


def _write_temporary(directory, name, data):
    with open(os.path.join(directory, name), "wb") as target:
        target.write(data)


def normalize(text):
    return re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", text or "")).casefold()


def clean_source(text):
    value = unicodedata.normalize("NFKC", text or "").strip()
    ext = naming.video_ext(value)
    if ext:
        value = value[:-len(ext)]
    value = _PIN_GROUP.sub(" ", value)
    value = _AD_GROUP.sub(" ", value)
    value = _TECH_GROUP.sub(" ", value)
    value = re.sub(r"[\[【](?:韩剧|韓劇|日剧|日劇|美剧|美劇|国产剧|國產劇|电影|電影|剧集|劇集)[\]】]", " ", value)
    value = _INDEX.sub("", value)
    return re.sub(r"\s+", " ", value).strip(" ._-[]【】")


def usable_title(title):
    title = (title or "").strip(" ._-()[]{}【】")
    return bool(title and not _GENERIC.fullmatch(title) and not _HASH.fullmatch(title)
                and re.search(r"[A-Za-z\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]", title))


@lru_cache(maxsize=2048)
def parsed(text):
    # 与本体搜索、扫描使用同一个解析库；绝不改写其结果缓存。
    return enrich(text).model_dump()


@dataclass(frozen=True)
class Clue:
    title: str
    year: int | None
    kind: str
    source: str
    origin: str

    @property
    def key(self):
        return self.kind, normalize(self.title), self.year


def source_clues(text, kind, origin, *, year=None, explicit=False):
    clean = clean_source(text)
    if not clean or _HASH.fullmatch(clean) or _GENERIC.fullmatch(clean):
        return []
    attrs = parsed(clean)
    observed_year = year or attrs.get("year")
    titles = []
    convention = _CONVENTIONAL.match(clean)
    if explicit:
        titles.append(clean)
    elif convention:
        # 规范目录名整名优先，避免模型截短中文或吞掉片名自带的数字。
        titles.append(convention.group(1))
        observed_year = year or int(convention.group(2))
    else:
        # 只适配确定性发布结构，不按第一个年份盲切（Blade Runner 2049）。
        stem = _CUT.split(clean, maxsplit=1)[0].strip(" ._-()[]{}【】")
        years = list(re.finditer(r"(?<!\d)(?:19|20)\d{2}(?!\d)", stem))
        if years:
            chosen = years[-1]
            # 只有尾部年份/紧邻发布字段的年份才作为结构年份剥离。
            if not stem[chosen.end():].strip(" ._-()[]{}"):
                prefix = stem[:chosen.start()].strip(" ._-()[]{}")
                if prefix:
                    stem = prefix
                    observed_year = year or int(chosen.group())
        if usable_title(stem) or (observed_year and re.fullmatch(r"\d{3,4}", stem)):
            titles.append(stem)
    titles.extend(attrs.get("titles_zh") or [])
    titles.extend(attrs.get("titles_en") or [])
    titles.extend(attrs.get("title_candidates") or [])
    clues = []
    seen = set()
    for title in titles:
        title = re.sub(r"\s+", " ", title.replace(".", " ").replace("_", " ")).strip(" ._-()[]{}【】")
        key = normalize(title)
        numeric_movie = kind == "movie" and observed_year and re.fullmatch(r"\d{3,4}", title)
        if (usable_title(title) or numeric_movie) and key not in seen:
            seen.add(key)
            clues.append(Clue(title[:100], observed_year, kind, text[:400], origin))
    return clues[:4]


def collect_evidence(share_title, paths, body):
    paths = [str(p).replace("\\", "/") for p in paths]
    pinned = []
    for text in [share_title, *paths]:
        for match in _PIN.finditer(text or ""):
            value = int(match.group(1))
            if value not in pinned:
                pinned.append(value)
    # 显式季集/季目录比模型对单独中文片名的 movie 猜测可靠。
    has_episodes = any(naming.episode_from_path(p)[1] is not None for p in paths)
    requested_kind = getattr(body, "kind", None)
    if requested_kind not in (None, "", "tv", "movie"):
        raise ValueError("类型只能是 movie 或 tv")
    kind = requested_kind or ("tv" if has_episodes else None)
    if not kind:
        texts = [share_title, *paths]
        declared_tv = any(re.search(r"全\s*\d+\s*集|第\s*[0-9一二三四五六七八九十]+\s*季|[\[【](?:韩剧|韓劇|日剧|日劇|美剧|美劇|剧集|劇集)[\]】]", text) for text in texts)
        votes = [parsed(clean_source(PurePosixPath(p).name)).get("media_type") for p in paths]
        kind = "tv" if declared_tv or votes.count("tv") > votes.count("movie") else "movie"
    year = getattr(body, "year", None)
    explicit_title = (getattr(body, "title", None) or "").strip()
    share = source_clues(explicit_title or share_title, kind, "指定片名" if explicit_title else "分享标题", year=year, explicit=bool(explicit_title))
    groups = []
    for path in paths:
        parts = PurePosixPath(path).parts
        sources = []
        # 相近作品目录与视频文件均保留，独立匹配后交叉核对，不能只读分享标题。
        for directory in reversed(parts[:-1]):
            if naming.season_from_dir(directory) is None and usable_title(clean_source(directory)):
                sources.append(source_clues(directory, kind, "作品目录", year=year))
        sources.append(source_clues(parts[-1] if parts else path, kind, "视频文件", year=year))
        sources = [s for s in sources if s]
        groups.append((path, sources))
    return {"kind": kind, "pins": pinned, "share": share, "groups": groups,
            "explicit_title": bool(explicit_title), "has_episodes": has_episodes}


def merge_candidates(rows):
    unique = {}
    for row in rows:
        tmdb_id = row.get("tmdb_id")
        kind = row.get("kind")
        if tmdb_id and kind in ("tv", "movie"):
            unique.setdefault((kind, int(tmdb_id)), dict(row))
    return list(unique.values())


def candidate_of(result):
    return {k: result.get(k) for k in ("tmdb_id", "kind", "title", "year")}


async def recognize(ops, evidence, body):
    """宿主确认全部有效证据，冲突/空候选绝不自动执行；无写媒体操作。"""
    trace = []
    candidates = []
    cache = {}
    ready = {}
    selected = getattr(body, "tmdb_id", None)
    kind = evidence["kind"]
    budget_exceeded = False
    authoritative = None

    async def query(clue, select=None):
        nonlocal budget_exceeded
        key = (clue.key, select)
        if key in cache:
            return cache[key]
        if len(cache) >= MAX_RESOLVE_CALLS:
            budget_exceeded = True
            return None
        payload = {"kind": clue.kind, "title": clue.title, "hint": clue.title}
        if clue.year:
            payload["year"] = clue.year
        if select:
            payload.update(selected_tmdb_id=int(select), selected_kind=clue.kind)
        # 原始目录/发布名作副标题，留给本体抽取中英文备选词，不先截坏。
        payload["subtitle"] = clue.source
        try:
            result = await ops.call("dl.resolve-target", payload)
        except OpsError as exc:
            if select and exc.status == 400:
                result = {"status": "not_found", "candidates": []}
            else:
                raise
        result = result or {"status": "not_found"}
        cache[key] = result
        trace.append({"origin": clue.origin, "title": clue.title, "year": clue.year,
                      "kind": clue.kind, "status": result.get("status"),
                      "tmdb_id": result.get("tmdb_id")})
        candidates.extend(result.get("candidates") or [])
        if result.get("status") == "ready":
            if result.get("ok") is False or not result.get("library_id"):
                raise RuntimeError("MovieClaw 已识别作品，但未通过目标媒体库检查，未执行转存")
            ready[(result["kind"], int(result["tmdb_id"]))] = result
        return result

    async def source(variants):
        for clue in variants:
            result = await query(clue, selected if selected else None)
            if result and result.get("status") == "ready":
                return result
            if result and not clue.year and not selected:
                # 无年份时宿主先返回候选；各年份仅作检验假设，不把首个候选当答案。
                # 所有同类型年份分别回交宿主验证；出现多个身份就保留歧义。
                years = list(dict.fromkeys(c.get("year") for c in result.get("candidates", [])
                                           if c.get("kind") == clue.kind and c.get("year")))
                hits = {}
                unresolved_year = False
                for hypothesis in years:
                    checked = await query(Clue(clue.title, int(hypothesis), clue.kind,
                                              clue.source, clue.origin + "（无年份候选复核）"))
                    if checked and checked.get("status") == "ready":
                        hits[(checked["kind"], int(checked["tmdb_id"]))] = checked
                    elif checked and checked.get("status") == "ambiguous":
                        unresolved_year = True
                if len(hits) == 1 and not unresolved_year:
                    return next(iter(hits.values()))
                if len(hits) > 1 or unresolved_year:
                    # 真正的同名歧义不能再改用截短的模型别名来规避。
                    return None
        return None

    if len(evidence["pins"]) > 1 and not selected:
        return {"status": "ambiguous", "kind": kind, "candidates": [], "recognition": trace,
                "message": "分享里的 TMDB 标记互相冲突，已停止转存；请确认作品或分别提供作品目录链接"}

    pin = selected or (evidence["pins"][0] if evidence["pins"] else None)
    if pin:
        # 编号是原始证据/用户确认；先向宿主取正式资料再进入同一条路由预检。
        details = await ops.call("discover.get-title-details", {"title_ref": f"tmdb:{kind}:{int(pin)}"})
        official = (details or {}).get("title") or {}
        if not official.get("title"):
            return {"status": "not_found", "kind": kind, "candidates": [], "recognition": trace,
                    "message": "指定编号没有查到作品资料；请确认片名和电影/剧集类型"}
        clue = Clue(official["title"], official.get("release_year"), kind, official["title"], "编号资料核验")
        authoritative = await query(clue, int(pin))
        if not authoritative or authoritative.get("status") != "ready":
            return {"status": "ambiguous", "kind": kind, "candidates": merge_candidates(candidates),
                    "recognition": trace, "message": "编号未通过 MovieClaw 的作品与分库校验，未执行转存"}
        # 之后每个文件仍独立检查，不把一个编号强套给整个合集。
        selected = None

    share_result = await source(evidence["share"])
    effective_share = authoritative or share_result
    failures = []
    group_cache = {}
    for path, sources in evidence["groups"]:
        if budget_exceeded:
            break
        signature = tuple(tuple(c.key for c in variants) for variants in sources)
        if signature in group_cache:
            matched = group_cache[signature]
        else:
            matched = []
            for variants in sources:
                hit = await source(variants)
                if hit:
                    matched.append(hit)
            group_cache[signature] = matched
        if matched:
            # 目录和文件任一能够证实身份即可，二者都命中时须完全一致。
            for hit in matched:
                ready[(hit["kind"], int(hit["tmdb_id"]))] = hit
        elif effective_share and not sources:
            pass
        elif effective_share and authoritative and not candidates:
            # 明确编号已通过本体核验，文件没有冲突候选时可作为身份锚。
            pass
        elif effective_share and evidence["explicit_title"] and not candidates:
            # 用户明确片名 + 无其他可证实身份：允许明确输入兜底，不适用于多部片冲突。
            pass
        else:
            failures.append(path)

    if len(ready) > 1:
        candidates.extend(candidate_of(hit) for hit in ready.values())
        return {"status": "ambiguous", "kind": kind, "candidates": merge_candidates(candidates),
                "recognition": trace, "message": "目录、分享标题或视频文件识别出了不同作品，已停止混合转存；请分别提供作品目录链接"}
    if budget_exceeded:
        return {"status": "ambiguous", "kind": kind, "candidates": merge_candidates(candidates),
                "recognition": trace, "message": "独立识别线索过多，未完成全部文件的身份核对；请按作品拆分链接"}
    if failures:
        candidates.extend(candidate_of(hit) for hit in ready.values())
        return {"status": "ambiguous" if candidates else "not_found", "kind": kind,
                "candidates": merge_candidates(candidates), "recognition": trace,
                "message": f"还有 {len(failures)} 个文件不能确定作品，未转存；请补充片名和年份"
                           + ("，或从候选确认" if candidates else "") + "，不需要 TMDB 编号"}
    if ready:
        result = dict(next(iter(ready.values())))
        result["recognition"] = trace
        return result
    return {"status": "ambiguous" if candidates else "not_found", "kind": kind,
            "candidates": merge_candidates(candidates), "recognition": trace,
            "message": "找到候选但不能可靠确定作品，请确认片名和年份后重试" if candidates else
                       "已检查分享标题、作品目录和视频文件，仍未找到可确认的作品；请补充片名（有年份请一起发），不需要 TMDB 编号"}
