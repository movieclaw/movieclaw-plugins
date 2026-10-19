"""Account-private taste, feedback, discovery and synchronized poster-wall tools.

Production inline routes derive identity from MovieClaw's verified Principal.
Encrypted plugin-data scopes isolate accounts; legacy capabilities remain only
for migration and compatibility tests. Public source pools never store marks.
"""
from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import inspect
import json
import math
import random
import re
import secrets
import time
import weakref
import zlib
from datetime import datetime, timezone
from urllib.parse import urlsplit

WALL_COUNTRIES = {"all": "全部国家／地区", "CN": "中国大陆", "HK": "中国香港", "TW": "中国台湾", "KR": "韩国", "JP": "日本",
                  "US": "美国", "GB": "英国", "FR": "法国", "DE": "德国", "ES": "西班牙", "IT": "意大利", "SE": "瑞典", "DK": "丹麦",
                  "NO": "挪威", "TH": "泰国", "IN": "印度", "SG": "新加坡", "MY": "马来西亚", "ID": "印度尼西亚", "PH": "菲律宾",
                  "CA": "加拿大", "AU": "澳大利亚", "NZ": "新西兰", "BR": "巴西", "MX": "墨西哥", "TR": "土耳其", "RU": "俄罗斯"}
WALL_ERAS = {"all": (None, None), "classic": (None, 1999), "recent": (2010, None), "before1930": (None, 1929),
             **{str(year) + "s": (year, year + 9) for year in range(1930, 2030, 10)}}


def wall_values(value, allowed):
    if isinstance(value, str):
        values = value.split(",")
    elif isinstance(value, (list, tuple)):
        values = list(value)
    else:
        raise ValueError("无效筛选")
    if len(values) > len(allowed) or any(not isinstance(v, str) or v not in allowed for v in values):
        raise ValueError("无效筛选")
    if "all" in values:
        if len(values) != 1: raise ValueError("不限不能和具体选项混用")
        return ()
    return tuple(sorted(set(values)))


def valid_wall_filters(media_type, era, country="all", genres="all", exclude_genres="all", exploration="balanced"):
    if not isinstance(media_type, str) or media_type not in {"all", "movie", "tv"}: return False
    try:
        wall_values(era, WALL_ERAS);wall_values(country, WALL_COUNTRIES)
        included = wall_values(genres, {"all": "不限", **{str(k): v for k, v in GENRES.items()}})
        excluded = wall_values(exclude_genres, {"all": "不限", **{str(k): v for k, v in GENRES.items()}})
        if set(included).intersection(excluded) or not isinstance(exploration, str) or exploration not in {"familiar", "balanced", "discovery"}: return False
    except ValueError:
        return False
    return True


def wall_canonical(value, allowed):
    return ",".join(wall_values(value, allowed)) or "all"


def wall_windows(era):
    values = wall_values(era, WALL_ERAS)
    if not values: return [(None, None)]
    windows = sorted((WALL_ERAS[v] for v in values), key=lambda pair: pair[0] or 0)
    merged = []
    for low, high in windows:
        if merged and (merged[-1][1] is None or low is None or low <= merged[-1][1] + 1):
            old_low, old_high = merged[-1]
            merged[-1] = (old_low, None if old_high is None or high is None else max(old_high, high))
        else:
            merged.append((low, high))
    return merged


def wall_filters(media_type, era, country="all", genres="all", exclude_genres="all", exploration="balanced"):
    era, country = wall_canonical(era, WALL_ERAS), wall_canonical(country, WALL_COUNTRIES)
    return {"media_type": media_type, "era": era, **({"country": country} if country != "all" else {}), **wall_extra(genres, exclude_genres, exploration)}


def wall_filter_key(media_type, era, country="all", genres="all", exclude_genres="all", exploration="balanced"):
    era, country = wall_canonical(era, WALL_ERAS), wall_canonical(country, WALL_COUNTRIES)
    key = "" if media_type == era == "all" else ":" + media_type + ":" + era
    return key + (":country=" + country if country != "all" else "") + (":options=" + json.dumps(wall_extra(genres, exclude_genres, exploration), sort_keys=True) if wall_extra(genres, exclude_genres, exploration) else "")


def wall_extra(genres="all", exclude_genres="all", exploration="balanced"):
    allowed = {"all": "不限", **{str(k): v for k, v in GENRES.items()}}
    included, excluded = wall_canonical(genres, allowed), wall_canonical(exclude_genres, allowed)
    return {**({"genres": included} if included != "all" else {}),
            **({"exclude_genres": excluded} if excluded != "all" else {}),
            **({"exploration": exploration} if exploration != "balanced" else {})}


def wall_selection(value, previous=None):
    """Old open pages retain newly added fields when saving legacy selectors."""
    if not isinstance(value, dict) or not {"media_type", "countries", "eras"} <= value.keys() or value.keys() - {"media_type", "countries", "eras", "genres", "exclude_genres", "exploration"}:
        raise PersonalProfileError("筛选条件无效。")
    previous = previous or {}
    for key in ("countries", "eras", "genres", "exclude_genres"):
        if key in value and not isinstance(value[key], list): raise PersonalProfileError("筛选条件无效。")
    genres, excluded = value.get("genres", previous.get("genres", [])), value.get("exclude_genres", previous.get("exclude_genres", []))
    exploration = value.get("exploration", previous.get("exploration", "balanced"))
    if not valid_wall_filters(value["media_type"], value["eras"], value["countries"], genres, excluded, exploration):
        raise PersonalProfileError("题材包含与排除不能重叠，或筛选条件无效。")
    result = {"media_type": value["media_type"], "countries": list(wall_values(value["countries"], WALL_COUNTRIES)), "eras": list(wall_values(value["eras"], WALL_ERAS))}
    allowed = {"all": "不限", **{str(k): v for k, v in GENRES.items()}}
    for key, values in (("genres", genres), ("exclude_genres", excluded)):
        if values: result[key] = list(wall_values(values, allowed))
    if exploration != "balanced": result["exploration"] = exploration
    return result


MIN_RATING = 6.5
MAX_HISTORY = 8
MAX_BATCHES = 3
RECENT_BATCHES = 40
CHUNK_SIZE = 44000
MAX_PRIVATE_BYTES = 1024 * 1024
MAX_POOL_ITEMS = 1800
EXPANSION_ROUNDS = 4
EXPANSION_QUERIES = 24
EXPANSION_SECONDS = 12
PUBLIC_CATALOG_KEY = "personal-catalog:v1"
PUBLIC_CATALOG_TTL = 3600
GENRES = {18: "剧情", 35: "喜剧", 80: "犯罪", 9648: "悬疑", 10749: "爱情", 878: "科幻", 14: "奇幻",
          12: "冒险", 28: "动作", 16: "动画", 53: "惊悚", 27: "恐怖", 10752: "战争", 36: "历史",
          10751: "家庭", 10759: "动作冒险", 10765: "科幻奇幻", 10768: "战争政治"}
HOST_GENRES = {label: identity for identity, label in GENRES.items()}
HOST_GENRES.update({"drama": 18, "comedy": 35, "crime": 80, "mystery": 9648, "romance": 10749,
                   "science fiction": 878, "fantasy": 14, "adventure": 12, "action": 28, "animation": 16,
                   "thriller": 53, "horror": 27, "war": 10752, "history": 36, "family": 10751,
                   "action & adventure": 10759, "sci-fi & fantasy": 10765, "war & politics": 10768})
THEMES = {"human": {18: 1, 10751: .5, 36: .4}, "mystery": {9648: 1, 80: .8, 53: .6},
          "romance": {10749: 1, 18: .4}, "fantasy": {878: 1, 14: .9, 10765: 1},
          "adventure": {12: 1, 28: .8, 10759: 1}, "fun": {35: 1, 10751: .4}, "animation": {16: 1}}
REGIONS = {"chinese": (["CN", "TW", "HK"], ["zh", "cn"]), "korean": (["KR"], ["ko"]),
           "japanese": (["JP"], ["ja"]), "western": (["US", "GB", "CA", "AU", "FR", "DE", "IT"], ["en", "fr", "de", "it"]),
           "asian": (["TH", "IN", "ID", "SG"], ["th", "hi", "ta", "id"]), "global": ([], [])}
REGION_NAMES = {"CN": "中国大陆", "TW": "中国台湾", "HK": "中国香港", "KR": "韩国", "JP": "日本", "US": "美国",
                "GB": "英国", "TH": "泰国", "FR": "法国", "DE": "德国", "IT": "意大利", "IN": "印度", "CA": "加拿大", "AU": "澳大利亚"}
AVOID = {"horror": {27}, "war": {10752, 10768}, "animation": {16}, "none": set()}


def _choice(identity, label, description=""):
    return {"id": identity, "label": label, "description": description}


QUESTIONS = [
    {"id": "themes", "title": "什么故事最容易让你投入？", "description": "选择最多三个，让主题比热度更懂你。", "multiple": True, "max_choices": 3,
     "choices": [_choice("human", "人物与人生"), _choice("mystery", "谜案与博弈"), _choice("romance", "爱情与关系"),
                 _choice("fantasy", "科幻与奇幻"), _choice("adventure", "动作与冒险"), _choice("fun", "喜剧与日常"), _choice("animation", "动画世界")]},
    {"id": "pace", "title": "你喜欢故事怎样展开？", "description": "节奏是软偏好，会结合题材与简介线索参考。", "multiple": False, "max_choices": 1,
     "choices": [_choice("quiet", "慢慢沉进去", "愿意陪人物相处"), _choice("balanced", "张弛有度", "故事与氛围都重要"), _choice("fast", "尽快抓住我", "偏爱悬念、行动和推进")]},
    {"id": "mood", "title": "今晚最想带走哪一种感受？", "description": "它会影响同样题材里的取舍。", "multiple": False, "max_choices": 1,
     "choices": [_choice("warm", "温暖与共鸣"), _choice("light", "轻松与快乐"), _choice("dark", "紧张与冲击"), _choice("epic", "辽阔与震撼"), _choice("open", "保持开放")]},
    {"id": "regions", "title": "想从哪些地方的故事开始？", "description": "最多选两项；优先照顾偏好，也保留少量不同文化的发现。", "multiple": True, "max_choices": 2,
     "choices": [_choice("global", "世界各地"), _choice("chinese", "华语"), _choice("korean", "韩国"), _choice("japanese", "日本"), _choice("western", "欧美"), _choice("asian", "亚洲其他地区")]},
    {"id": "era", "title": "你和老电影、老剧的距离是？", "description": "三种选择都会保留跨年代作品。", "multiple": False, "max_choices": 1,
     "choices": [_choice("classic", "想多补经典", "更愿意遇见2000年以前的作品"), _choice("mixed", "好故事不看年份", "新旧都欢迎"), _choice("modern", "更贴近当代", "多一些近年的表达")]},
    {"id": "format", "title": "最近更想用哪种方式看故事？", "description": "决定电影与剧集的大致比例。", "multiple": False, "max_choices": 1,
     "choices": [_choice("movie", "偏电影", "一段完整的相遇"), _choice("mixed", "电影剧集都好"), _choice("tv", "偏剧集", "和人物多待一阵")]},
    {"id": "exploration", "title": "这次想走多远？", "description": "影响大众经典与较少人提起的佳作之间的比例。", "multiple": False, "max_choices": 1,
     "choices": [_choice("familiar", "先选公认好看的"), _choice("balanced", "熟悉与惊喜各一半"), _choice("discovery", "给我一点意外")]},
    {"id": "avoid", "title": "哪些题材这次先跳过？", "description": "按来源的类型标签排除，不能替代完整的内容分级。", "multiple": True, "max_choices": 2,
     "choices": [_choice("none", "没有特别避开的"), _choice("horror", "恐怖题材"), _choice("war", "战争题材"), _choice("animation", "动画题材")]},
]
QUESTION_BY_ID = {question["id"]: question for question in QUESTIONS}
DEFAULT_ANSWERS = {"themes": ["human", "mystery", "fun"], "pace": "balanced", "mood": "open", "regions": ["global"],
                   "era": "mixed", "format": "mixed", "exploration": "balanced", "avoid": ["none"]}


class PersonalProfileError(ValueError):
    def __init__(self, message, *, status=400):
        super().__init__(message)
        self.status = status


def _now():
    return datetime.now(timezone.utc).isoformat()


def _safe_url(value):
    if not isinstance(value, str) or len(value) > 1500:
        return None
    try:
        parsed = urlsplit(value)
        return value if parsed.scheme == "https" and parsed.hostname and not parsed.username and not parsed.password else None
    except ValueError:
        return None


def _finite(value):
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _validate_answers(answers):
    if not isinstance(answers, dict) or set(answers) != set(QUESTION_BY_ID):
        raise PersonalProfileError("请完成全部八道口味问题。")
    result = {}
    for identity, question in QUESTION_BY_ID.items():
        value = answers[identity]
        allowed = {choice["id"] for choice in question["choices"]}
        if question["multiple"]:
            if (not isinstance(value, list) or not 1 <= len(value) <= question["max_choices"]
                    or any(not isinstance(item, str) or item not in allowed for item in value) or len(set(value)) != len(value)):
                raise PersonalProfileError("多选题的选项数量或内容无效。")
            if (identity == "regions" and "global" in value or identity == "avoid" and "none" in value) and len(value) > 1:
                raise PersonalProfileError("不限选项不能与具体偏好同时选择。")
            result[identity] = list(value)
        elif not isinstance(value, str) or value not in allowed:
            raise PersonalProfileError("口味选项无效。")
        else:
            result[identity] = value
    if "animation" in result["themes"] and "animation" in result["avoid"]:
        raise PersonalProfileError("动画偏好与跳过动画不能同时选择。")
    return result


def _vector(answers):
    result = {}
    for theme in answers["themes"]:
        for genre, weight in THEMES[theme].items():
            result["genre:" + str(genre)] = max(weight, result.get("genre:" + str(genre), 0))
    for region in answers["regions"]:
        countries, languages = REGIONS[region]
        result.update({"country:" + country: 1 for country in countries})
        result.update({"language:" + language: 1 for language in languages})
    for name in ("pace", "mood", "era", "format", "exploration"):
        result[name + ":" + answers[name]] = 1
    return result


def _combined(current, history):
    previous = [row["vector"] for row in history[-MAX_HISTORY:] if isinstance(row.get("vector"), dict)]
    if not previous:
        return copy.deepcopy(current)
    weights = [.65 ** (len(previous) - index - 1) for index in range(len(previous))]
    total = sum(weights)
    result = {key: .72 * value for key, value in current.items()}
    for vector, weight in zip(previous, weights):
        for key, value in vector.items():
            result[key] = result.get(key, 0) + .28 * weight / total * value
    return result


def _summary(answers, vector, tested=True):
    if not tested:
        return {"title": "先遇见好故事", "description": "还没有口味测试，先从跨年代的优质作品探索。", "tags": ["跨年代", "电影与剧集", "口碑基础"], "axes": []}
    labels = {question["id"]: {choice["id"]: choice["label"] for choice in question["choices"]} for question in QUESTIONS}
    tags = [labels["themes"][theme] for theme in answers["themes"]]
    tags.extend([labels["pace"][answers["pace"]], labels["era"][answers["era"]]])
    axes = [{"id": "story", "label": "人物共鸣", "value": round(100 * min(1, vector.get("genre:18", 0)))},
            {"id": "mystery", "label": "悬念博弈", "value": round(100 * min(1, max(vector.get("genre:9648", 0), vector.get("genre:80", 0))))},
            {"id": "imagination", "label": "想象世界", "value": round(100 * min(1, max(vector.get("genre:878", 0), vector.get("genre:14", 0), vector.get("genre:10765", 0))))},
            {"id": "light", "label": "轻松治愈", "value": round(100 * min(1, max(vector.get("genre:35", 0), vector.get("mood:warm", 0))))},
            {"id": "exploration", "label": "发现意愿", "value": round(100 * (vector.get("exploration:discovery", 0) + .5 * vector.get("exploration:balanced", 0)))}]
    return {"title": " · ".join(tags[:2]), "description": "以本次选择为主，结合此前测试的稳定偏好。", "tags": tags, "axes": axes}


def _era(year):
    return "classic" if year and year < 1990 else "middle" if year and year < 2010 else "modern"


def _public_item(row, media_type, country=None, host=False):
    if not isinstance(row, dict):
        return None
    if host:
        match = re.fullmatch(r"tmdb:(movie|tv):([1-9]\d*)", str(row.get("title_ref") or ""))
        if not match or match[1] != media_type or row.get("provider") != "tmdb" or row.get("media_type") != media_type:
            return None
        identity, title = int(match[2]), row.get("title")
        rating, votes, year = _finite(row.get("provider_rating")), 50, row.get("release_year")
        raw_genres = row.get("genres") or []
        if not isinstance(raw_genres, list) or any(not isinstance(label, str) for label in raw_genres):
            return None
        names = [label.strip().casefold() for label in raw_genres]
        if any(word in name for name in names for word in ("documentary", "reality", "talk", "news", "纪录", "真人秀", "脱口秀", "新闻")):
            return None
        genres = list(dict.fromkeys(HOST_GENRES[name] for name in names if name in HOST_GENRES))
        countries, language = [country] if country else [], ""
        poster, backdrop = _safe_url(row.get("poster_url")), _safe_url(row.get("backdrop_url"))
        original = row.get("original_title") or ""
    else:
        raw_id = str(row.get("id") or "")
        if not re.fullmatch(r"[1-9]\d*", raw_id) or row.get("adult") is True:
            return None
        identity, title = int(raw_id), row.get("title" if media_type == "movie" else "name")
        rating, votes = _finite(row.get("vote_average")), _finite(row.get("vote_count"))
        date = str(row.get("release_date" if media_type == "movie" else "first_air_date") or "")
        year = int(date[:4]) if re.match(r"\d{4}", date) else None
        raw_genres = row.get("genre_ids")
        if not isinstance(raw_genres, list) or any(type(genre) is not int for genre in raw_genres):
            return None
        genres = raw_genres
        countries = row.get("origin_country") if isinstance(row.get("origin_country"), list) else []
        if country and countries and country not in countries:
            return None
        if country and not countries:
            countries = [country]  # The discover request itself applied this country filter.
        language = str(row.get("original_language") or "")[:12]
        image = lambda value, size: "https://image.tmdb.org/t/p/" + size + value if isinstance(value, str) and re.fullmatch(r"/[A-Za-z0-9._-]+", value) else None
        poster, backdrop = image(row.get("poster_path"), "w500"), image(row.get("backdrop_path"), "w1280")
        original = row.get("original_title" if media_type == "movie" else "original_name") or ""
    if (not isinstance(title, str) or not title.strip() or rating is None or rating < MIN_RATING or votes is None
            or votes < (50 if host or media_type == "tv" else 100) or not poster):
        return None
    if any(genre in {99, 10763, 10764, 10767} for genre in genres):
        return None
    if type(year) is not int or not 1880 <= year <= datetime.now(timezone.utc).year + 1:
        year = None
    return {"id": f"personal:tmdb:{media_type}:{identity}", "title": title.strip()[:180], "original_title": str(original)[:180],
            "media_type": media_type, "year": year, "rating": rating, "rating_source": "TMDB", "poster_url": poster,
            "backdrop_url": backdrop, "overview": str(row.get("overview") or "")[:1200],
            "detail_ref": f"tmdb:{media_type}:{identity}", "source_url": f"https://www.themoviedb.org/{media_type}/{identity}",
            "metadata": {"genre_ids": genres, "genres": [GENRES[genre] for genre in genres if genre in GENRES],
                         "origin_country": [value for value in countries if isinstance(value, str) and re.fullmatch(r"[A-Z]{2}", value)],
                         "original_language": language, "vote_count": int(votes) if not host else None,
                         "minimum_votes": 50 if host or media_type == "tv" else 100, "language_verified": not host}}


def _signal(item):
    """只保留题材、地区、语言、年代和格式；不留库存、路径、季集或下载信息。"""
    if not isinstance(item, dict):
        return {}
    metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    result = {}
    for genre in metadata.get("genre_ids", [])[:20]:
        if type(genre) is int and genre in GENRES:
            result["genre:" + str(genre)] = 1.
    for country in metadata.get("origin_country", [])[:10]:
        if isinstance(country, str) and re.fullmatch(r"[A-Z]{2}", country):
            result["country:" + country] = 1.
    language = metadata.get("original_language")
    if isinstance(language, str) and re.fullmatch(r"[a-z]{2,3}", language):
        result["language:" + language] = 1.
    if item.get("media_type") in {"movie", "tv"}:
        result["format:" + item["media_type"]] = 1.
    year = item.get("year")
    if type(year) is int:
        result["era:" + ("classic" if year < 2000 else "modern" if year >= 2010 else "mixed")] = 1.
    return result


def _behavior(profile, pool):
    if profile.get("learning_enabled", True) is False:
        return {}, {}, {"saved": 0, "dismissed": 0, "subscriptions": 0, "liked": 0, "watched": 0, "enabled": False}
    lookup = {item["detail_ref"]: item for item in pool.get("items", [])}
    positive, negative = {}, {}
    subscription = profile.get("subscriptions") or {}
    dismissed = {row.get("detail_ref") for row in profile.get("dismissed", [])}
    # One positive observation per title: explicit likes outweigh saves and
    # subscriptions; watching alone is only a weak familiarity signal.
    evidence = {}
    for name, weight in (("watched", .2), ("saved", 1.), ("liked", 2.5)):
        for row in profile.get(name, []):
            ref = row.get("item", {}).get("detail_ref")
            if ref and ref not in dismissed:
                evidence[ref] = (row, weight)
    for ref, row in subscription.get("features", {}).items():
        if ref not in dismissed and (ref not in evidence or evidence[ref][1] < 1.4):
            evidence[ref] = (row, 1.4)
    rows = [(row, weight, positive) for row, weight in evidence.values()]
    rows += [(row, 1., negative) for row in profile.get("dismissed", [])]
    totals = {id(positive): {}, id(negative): {}}
    for row, weight, target in rows:
        features = row.get("features") or _signal(row.get("item") or lookup.get(row.get("detail_ref")))
        stamp = row.get("liked_at") or row.get("watched_at") or row.get("saved_at") or row.get("hidden_at") or row.get("observed_at")
        try:
            age = max(0, (datetime.now(timezone.utc) - datetime.fromisoformat(stamp)).total_seconds() / 86400)
            weight *= max(.25, .5 ** (age / 180))
        except (ValueError, TypeError):
            pass
        groups = {key.split(":", 1)[0] for key in features}
        for group in groups:
            totals[id(target)][group] = totals[id(target)].get(group, 0) + weight
        for key in features:
            target[key] = target.get(key, 0) + weight
    for target in (positive, negative):
        for key in target:
            target[key] /= max(1., totals[id(target)].get(key.split(":", 1)[0], 1.))
    return positive, negative, {"saved": len(profile.get("saved", [])), "dismissed": len(profile.get("dismissed", [])),
        "liked": len(profile.get("liked", [])), "watched": len(profile.get("watched", [])),
        "subscriptions": len(subscription.get("refs", [])), "subscription_features": len(subscription.get("features", {})), "enabled": True}


class PersonalRecommendations:
    valid_wall_filters = staticmethod(valid_wall_filters)
    def __init__(self, discovery, ops_getter=None, private_store=None, storage=None):
        self.discovery, self.ops_getter, self.store = discovery, ops_getter, private_store
        self.storage = storage if storage is not None else getattr(discovery, "storage", None)
        self._locks, self._pools, self._pool_locks = weakref.WeakValueDictionary(), {}, weakref.WeakValueDictionary()
        self._public_snapshot, self._public_loaded, self._public_live_pool = None, False, None
        self._public_task, self._public_retry_at = None, 0
        self._public_lock = asyncio.Lock()
        self._closed = False
        self._account_tokens = {}

    async def initialize(self):
        await self._load_public_catalog()

    async def close(self):
        self._closed = True
        if self._public_task is not None:
            self._public_task.cancel()
            await asyncio.gather(self._public_task, return_exceptions=True)
        self._locks.clear()
        self._pools.clear()
        self._pool_locks.clear()

    @staticmethod
    def _catalog_item(item):
        """Project only public source fields; never persist private annotations."""
        if not isinstance(item, dict):
            return None
        match = re.fullmatch(r"tmdb:(movie|tv):([1-9]\d*)", str(item.get("detail_ref") or ""))
        rating, metadata = _finite(item.get("rating")), item.get("metadata")
        if (not match or not isinstance(item.get("title"), str) or not item["title"].strip() or rating is None
                or not MIN_RATING <= rating <= 10 or not _safe_url(item.get("poster_url")) or not isinstance(metadata, dict)):
            return None
        genres = metadata.get("genre_ids")
        if not isinstance(genres, list) or any(type(genre) is not int for genre in genres) or set(genres).intersection({99, 10763, 10764, 10767}):
            return None
        verified = metadata.get("language_verified") is True
        minimum = 100 if match[1] == "movie" and verified else 50
        votes = _finite(metadata.get("vote_count"))
        asserted_minimum = _finite(metadata.get("minimum_votes")) or 0
        if (verified and (votes is None or votes < minimum)) or (not verified and asserted_minimum < minimum):
            return None
        year = item.get("year")
        if type(year) is not int or not 1880 <= year <= datetime.now(timezone.utc).year + 1:
            year = None
        countries = metadata.get("origin_country")
        countries = [country for country in countries if isinstance(country, str) and re.fullmatch(r"[A-Z]{2}", country)] if isinstance(countries, list) else []
        language = metadata.get("original_language")
        language = language if isinstance(language, str) and re.fullmatch(r"[a-z]{2,3}", language) else ""
        return {"id": f"personal:tmdb:{match[1]}:{match[2]}", "title": item["title"].strip()[:180],
                "original_title": str(item.get("original_title") or "")[:180], "media_type": match[1], "year": year,
                "rating": rating, "rating_source": "TMDB", "poster_url": _safe_url(item["poster_url"]),
                "backdrop_url": _safe_url(item.get("backdrop_url")), "overview": str(item.get("overview") or "")[:500],
                "detail_ref": f"tmdb:{match[1]}:{match[2]}", "source_url": f"https://www.themoviedb.org/{match[1]}/{match[2]}",
                "metadata": {"genre_ids": genres, "genres": [GENRES[genre] for genre in genres if genre in GENRES],
                             "origin_country": countries, "original_language": language,
                             "vote_count": int(votes) if votes is not None else None, "minimum_votes": minimum,
                             "language_verified": verified}}

    async def _public_call(self, method, *args):
        if self.storage is None:
            return None
        result = getattr(self.storage, method)(*args)
        return await result if inspect.isawaitable(result) else result

    async def _load_public_catalog(self):
        if self._public_loaded:
            return
        async with self._public_lock:
            if self._public_loaded:
                return
            self._public_loaded = True
            try:
                value = await self._public_call("get", PUBLIC_CATALOG_KEY)
                if not isinstance(value, dict) or value.get("version") != 1 or not isinstance(value.get("items"), list):
                    return
                items = {item["id"]: item for raw in value["items"][-MAX_POOL_ITEMS:]
                         if (item := self._catalog_item(raw)) is not None}
                if items:
                    self._public_snapshot = {"version": 1, "items": list(items.values()),
                        "backend": "host" if value.get("backend") == "host" else "tmdb",
                        "fetched_at": str(value.get("fetched_at") or "")[:50]}
            except Exception:
                pass  # Public cache availability cannot invalidate a private profile.

    async def _save_public_catalog(self, rows, backend):
        projected = [item for row in rows if (item := self._catalog_item(row)) is not None]
        if not projected:
            return
        await self._load_public_catalog()
        async with self._public_lock:
            items = {item["id"]: item for item in (self._public_snapshot or {}).get("items", [])}
            items.update({item["id"]: item for item in projected})
            self._public_snapshot = {"version": 1, "items": list(items.values())[-MAX_POOL_ITEMS:],
                                     "backend": backend, "fetched_at": _now()}
            try:
                await self._public_call("set", PUBLIC_CATALOG_KEY, copy.deepcopy(self._public_snapshot), PUBLIC_CATALOG_TTL)
            except Exception:
                pass

    def _restored_pool(self, snapshot, *, private=False):
        return {"items": copy.deepcopy(snapshot["items"]), "expires": time.monotonic() + 120,
                "next_queries": [], "fetched_queries": set(), "attempted_queries": set(), "stream_totals": {},
                "status": "stale", "partial": False, "backend": snapshot.get("backend", "tmdb"),
                "restored": True, "private_fallback": private, "fetched_at": snapshot.get("fetched_at")}

    def _schedule_public_refresh(self):
        if self._closed or self._public_task is not None or time.monotonic() < self._public_retry_at:
            return
        async def refresh():
            try:
                pool = await self._pool(DEFAULT_ANSWERS, "public-refresh", _restore=False,
                                        deadline=time.monotonic() + EXPANSION_SECONDS)
                if pool and pool["status"] == "ready":
                    self._public_live_pool = copy.deepcopy(pool)
                    for signature, cached in list(self._pools.items()):
                        if cached.get("restored"):
                            self._pools[signature] = copy.deepcopy(pool)
                    self._public_retry_at = time.monotonic() + 300
                else:
                    self._public_retry_at = time.monotonic() + 120
            except Exception:
                self._public_retry_at = time.monotonic() + 120
            finally:
                self._public_task = None
        self._public_task = asyncio.create_task(refresh())

    async def _latest_private_batch(self, scope, profile):
        names = ([profile["last_success_batch"]] if isinstance(profile.get("last_success_batch"), str) else [])
        names += list(reversed(profile.get("batches", [])))
        stale = None
        for name in dict.fromkeys(names):
            if not isinstance(name, str) or not re.fullmatch(r"batch:[0-9a-f]{32}", name):
                continue
            value = await self._read(scope, name)
            if not isinstance(value, dict) or not isinstance(value.get("items"), list):
                continue
            rows = [item for raw in value["items"][:96] if (item := self._catalog_item(raw)) is not None]
            if not rows:
                continue
            backend = "host" if all(not item["metadata"]["language_verified"] for item in rows) else "tmdb"
            candidate = {"items": rows, "backend": backend, "fetched_at": value.get("fetched_at")}
            if value.get("status") == "ready":
                profile["last_success_batch"] = name  # Preserve successful pre-upgrade batches too.
                return self._restored_pool(candidate, private=True)
            stale = stale or candidate
        return self._restored_pool(stale, private=True) if stale else None

    @staticmethod
    def _account_scope(identity):
        if not isinstance(identity, str) or not re.fullmatch(r"(?:admin:0|member:[1-9]\d*:[A-Za-z0-9:.+\-]+)", identity):
            raise PersonalProfileError("无法确认账号身份。", status=403)
        return "account:" + hashlib.sha256(identity.encode()).hexdigest()[:32]

    async def account_token(self, identity):
        scope = self._account_scope(identity)
        if scope in self._account_tokens:
            return self._account_tokens[scope]
        async with self._locks.setdefault(scope, asyncio.Lock()):
            binding = await self._read(scope, "binding")
            if not binding:
                created = await self.create_profile()
                binding = {"version": 1, "profile_token": created["profile_token"], "imports": [], "created_at": _now()}
                await self._write(scope, "binding", binding)
            token = binding["profile_token"]
            self._scope(token)
            if len(self._account_tokens) >= 1024:
                self._account_tokens.pop(next(iter(self._account_tokens)))
            self._account_tokens[scope] = token
            return token

    @staticmethod
    def _merge_legacy_profile(target, source):
        """合并旧浏览器档案，不覆盖更新的测试，不复活已跨设备移除的片单。"""
        target = copy.deepcopy(target)
        original_ids = {row["id"] for row in target.get("history", [])}
        incoming = [row for row in source.get("history", []) if row["id"] not in original_ids]
        history = {row["id"]: row for row in target.get("history", []) + incoming}
        rows = sorted(history.values(), key=lambda row: (row["created_at"], row["id"]))[-MAX_HISTORY:]
        target["history"] = rows
        if rows:
            newest = rows[-1]
            target.update(answers=copy.deepcopy(newest["answers"]), vector=_combined(newest["vector"], rows[:-1]))
        old_count = target.get("test_count", 0)
        target["test_count"] = max(old_count + len(incoming), source.get("test_count", 0) if not original_ids else 0)
        tombstones = target.get("feedback_tombstones", {})
        for name, stamp in (("saved", "saved_at"), ("dismissed", "hidden_at")):
            ref_of = (lambda row: row.get("item", {}).get("detail_ref")) if name == "saved" else (lambda row: row.get("detail_ref"))
            merged = {ref_of(row): row for row in target.get(name, [])}
            for row in source.get(name, []):
                ref = ref_of(row)
                if not ref or tombstones.get(name + ":" + ref, "") >= row.get(stamp, ""):
                    continue
                current = merged.get(ref)
                if current is None or current.get(stamp, "") < row.get(stamp, ""):
                    merged[ref] = copy.deepcopy(row)
            target[name] = sorted(merged.values(), key=lambda row: row.get(stamp, ""), reverse=True)[:100 if name == "saved" else 300]
        # 旧订阅只是可重读的派生资料，由当前账号下一次真实同步重新建立。
        if not target.get("recent"):
            target["recent"] = copy.deepcopy(source.get("recent", [])[-RECENT_BATCHES:])
        if not target.get("behavior_setting_changed_at") and "learning_enabled" in source:
            target["learning_enabled"] = source["learning_enabled"]
            target["behavior_setting_changed_at"] = source.get("updated_at") or _now()
        target.update(revision=secrets.token_hex(12), updated_at=_now())
        return target

    async def open_account(self, identity, legacy_token=None):
        scope = self._account_scope(identity)
        token = await self.account_token(identity)
        imported, reason = False, ""
        if legacy_token is not None:
            if not isinstance(legacy_token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", legacy_token):
                raise PersonalProfileError("旧档案凭证无效。")
            legacy_hash = hashlib.sha256(legacy_token.encode()).hexdigest()
            claim_scope = "migration:" + legacy_hash[:32]
            async with self._locks.setdefault(scope, asyncio.Lock()):
                binding = await self._read(scope, "binding")
                if legacy_hash not in binding.get("imports", []) and len(binding.get("imports", [])) < 256:
                    async with self._locks.setdefault(claim_scope, asyncio.Lock()):
                        claim = await self._read(claim_scope, "owner")
                        if claim and claim.get("account") != scope:
                            reason = "旧档案已导入另一账号，当前账号保持独立。"
                        else:
                            try:
                                legacy = await self._profile(self._scope(legacy_token))
                            except PersonalProfileError as exc:
                                if exc.status != 404:
                                    raise
                                legacy = None
                            if legacy:
                                # 先认领，保存失败也只允许同一账号重试，避免跨账号重复导入。
                                await self._write(claim_scope, "owner", {"account": scope})
                                async with self._locks.setdefault(self._scope(token), asyncio.Lock()):
                                    target = await self._profile(self._scope(token))
                                    target = self._merge_legacy_profile(target, legacy)
                                    await self._write(self._scope(token), "profile", target)
                                binding["imports"] = binding.get("imports", []) + [legacy_hash]
                                await self._write(scope, "binding", binding)
                                imported = True
        view = await self.get_profile(token)
        return {"profile": view, "sync": {"mode": "account", "imported": imported, "migration_conflict": bool(reason), "message": reason or "已按 MovieClaw 登录账号同步。"}}

    @staticmethod
    def _wall_settings(record):
        if not record:
            return {"filters": {"media_type": "all", "countries": [], "eras": []}, "revision": "", "updated_at": None}
        if not isinstance(record, dict) or not re.fullmatch(r"[0-9a-f]{24}", str(record.get("revision", ""))):
            raise PersonalProfileError("筛选设置暂不可用。", status=503)
        return copy.deepcopy(record)

    async def get_wall_filters(self, profile_token):
        scope = self._scope(profile_token)
        async with self._locks.setdefault(scope, asyncio.Lock()):
            return self._wall_settings(await self._read(scope, "wall-filters"))

    async def set_wall_filters(self, profile_token, selection):
        if not isinstance(selection, dict) or not isinstance(selection.get("revision"), str) or not re.fullmatch(r"[0-9a-f]{24}|", selection["revision"]):
            raise PersonalProfileError("筛选条件无效。")
        scope = self._scope(profile_token)
        async with self._locks.setdefault(scope, asyncio.Lock()):
            current = self._wall_settings(await self._read(scope, "wall-filters"))
            value = wall_selection({k: v for k, v in selection.items() if k != "revision"}, current["filters"])
            if current["filters"] == value: return current
            if current["revision"] != selection["revision"]:
                raise PersonalProfileError("另一设备已更新筛选。", status=409)
            record = {"filters": value, "revision": secrets.token_hex(12), "updated_at": _now()}
            await self._write(scope, "wall-filters", record)
            return copy.deepcopy(record)

    async def _filter_revision(self, scope, expected=None):
        current = self._wall_settings(await self._read(scope, "wall-filters"))["revision"]
        if expected is not None and expected != current:
            raise PersonalProfileError("另一设备已更新筛选。", status=409)
        return current

    async def questionnaire(self):
        return {"version": 1, "title": "找到你的故事坐标", "description": "八个选择，可以随时重测；本次偏好占主要权重，往期测试帮助保留稳定口味。", "questions": copy.deepcopy(QUESTIONS)}

    @staticmethod
    def _shelf_view(profile):
        return {name: copy.deepcopy(profile.get(name, [])) for name in ("saved", "dismissed", "watched", "liked")}

    async def shelf(self, profile_token):
        scope = self._scope(profile_token)
        async with self._locks.setdefault(scope, asyncio.Lock()):
            return self._shelf_view(await self._profile(scope))

    async def feedback(self, profile_token, action, item, *, undoable=False):
        """Account-private, reversible feedback; never written to public pools."""
        scope = self._scope(profile_token)
        operations = {"save": ("saved", "saved_at", 100), "hide": ("dismissed", "hidden_at", 300),
                      "watch": ("watched", "watched_at", 1000), "like": ("liked", "liked_at", 300)}
        removals = {"remove": "saved", "unhide": "dismissed", "unwatch": "watched", "unlike": "liked"}
        if action not in operations.keys() | removals.keys() or not isinstance(item, dict):
            raise PersonalProfileError("片单操作无效。")
        ref = item.get("detail_ref")
        if not isinstance(ref, str) or not re.fullmatch(r"tmdb:(movie|tv):[1-9]\d{0,10}", ref):
            raise PersonalProfileError("作品身份无效。")
        cleaned = self._catalog_item(item) if action in operations else None
        if action in operations and (cleaned is None or len(json.dumps(cleaned, ensure_ascii=False).encode()) > 4096):
            raise PersonalProfileError("这部作品暂时无法加入私人片单。")
        async with self._locks.setdefault(scope, asyncio.Lock()):
            profile = await self._profile(scope)
            stamp = _now()
            before_feedback = copy.deepcopy(self._shelf_view(profile))
            name = operations[action][0] if action in operations else removals[action]
            def ref_of(row):
                return row.get("detail_ref") if name == "dismissed" else row.get("item", {}).get("detail_ref")
            rows = [row for row in profile.get(name, []) if ref_of(row) != ref]
            tombstones = profile.setdefault("feedback_tombstones", {})
            if action in operations:
                _, timestamp, limit = operations[action]
                if len(rows) >= limit:
                    label = {"saved": "稍后看", "watched": "已看过", "liked": "喜欢", "dismissed": "不感兴趣"}[name]
                    raise PersonalProfileError(f"{label}已满 {limit} 部，请先移除一些作品。")
                if name == "dismissed":
                    row = {"detail_ref": ref, "title": cleaned["title"], "features": _signal(cleaned), timestamp: stamp}
                else:
                    if name in {"watched", "liked"}:
                        # Keep public display/features, avoid retaining large synopses.
                        cleaned.update(overview="", backdrop_url=None, original_title="")
                    row = {"item": cleaned, timestamp: stamp}
                rows.insert(0, row)
                profile[name] = rows[:limit]
                tombstones.pop(name + ":" + ref, None)
                if action == "save":
                    profile["dismissed"] = [row for row in profile.get("dismissed", []) if row.get("detail_ref") != ref]
                    tombstones["dismissed:" + ref] = stamp
            else:
                profile[name] = rows
                tombstones[name + ":" + ref] = stamp
            profile["feedback_tombstones"] = dict(sorted(tombstones.items(), key=lambda row: row[1])[-2000:])
            affected = [name] + (["dismissed"] if action == "save" else [])
            versions = profile.setdefault("feedback_versions", {})
            for field in affected: versions[field + ":" + ref] = secrets.token_hex(12)
            # Keep version guards only for existing marks, tombstones and live undo entries.
            undo = None
            if undoable:
                labels = {"save": "加入稍后看", "remove": "移除稍后看", "hide": "不感兴趣", "unhide": "恢复推荐", "watch": "已看过", "unwatch": "取消已看过", "like": "喜欢", "unlike": "取消喜欢"}
                old_rows = {field: next((row for row in before_feedback[field] if (row.get("detail_ref") or row.get("item", {}).get("detail_ref")) == ref), None) for field in affected}
                undo = {"id": secrets.token_hex(16), "title": (item.get("title") or next((row.get("title") or row.get("item", {}).get("title") for row in old_rows.values() if row), "这部影片"))[:180],
                        "label": labels[action], "detail_ref": ref, "created_at": stamp, "expires_at": time.time() + 900,
                        "before": old_rows, "versions": {field: versions[field + ":" + ref] for field in affected}}
                profile["undo_actions"] = ([row for row in profile.get("undo_actions", []) if row.get("expires_at", 0) > time.time()] + [undo])[-20:]
            protected = set(profile.get("feedback_tombstones", {}))
            protected.update(field + ":" + (row.get("detail_ref") or row.get("item", {}).get("detail_ref", "")) for field, rows in self._shelf_view(profile).items() for row in rows)
            protected.update(field + ":" + row["detail_ref"] for row in profile.get("undo_actions", []) for field in row.get("versions", {}))
            profile["feedback_versions"] = {key: value for key, value in versions.items() if key in protected}
            profile.update(revision=secrets.token_hex(12), updated_at=stamp)
            await self._write(scope, "profile", profile)
            return {**self._shelf_view(profile), **({"undo_action": self._undo_view(undo)} if undo else {})}

    @staticmethod
    def _undo_view(row):
        return {key: row[key] for key in ("id", "title", "label", "created_at", "expires_at")}

    async def recent_actions(self, profile_token):
        scope = self._scope(profile_token)
        async with self._locks.setdefault(scope, asyncio.Lock()):
            profile = await self._profile(scope)
            return {"items": [self._undo_view(row) for row in reversed(profile.get("undo_actions", [])) if row.get("expires_at", 0) > time.time() and not row.get("undone") and all(profile.get("feedback_versions", {}).get(name + ":" + row["detail_ref"]) == version for name, version in row.get("versions", {}).items())]}

    async def undo_feedback(self, profile_token, identity):
        if not isinstance(identity, str) or not re.fullmatch(r"[0-9a-f]{32}", identity):
            raise PersonalProfileError("撤销操作无效。")
        scope = self._scope(profile_token)
        async with self._locks.setdefault(scope, asyncio.Lock()):
            profile = await self._profile(scope)
            row = next((row for row in profile.get("undo_actions", []) if row.get("id") == identity), None)
            if not row or row.get("expires_at", 0) <= time.time(): raise PersonalProfileError("撤销已过期，请在我的片单中修改标记。", status=409)
            if row.get("undone"): return self._shelf_view(profile)
            ref, versions = row["detail_ref"], profile.setdefault("feedback_versions", {})
            if any(versions.get(name + ":" + ref) != version for name, version in row["versions"].items()):
                raise PersonalProfileError("这部影片的标记后来已被修改，无法撤销旧操作。", status=409)
            stamp = _now()
            for name, previous in row["before"].items():
                rows = [entry for entry in profile.get(name, []) if (entry.get("detail_ref") or entry.get("item", {}).get("detail_ref")) != ref]
                limit = {"saved": 100, "liked": 300, "watched": 1000, "dismissed": 300}[name]
                if previous is not None:
                    if len(rows) >= limit: raise PersonalProfileError("当前片单已满，请先整理后再撤销。", status=409)
                    rows.append(copy.deepcopy(previous))
                    date = {"saved": "saved_at", "liked": "liked_at", "watched": "watched_at", "dismissed": "hidden_at"}[name]
                    rows.sort(key=lambda entry: entry.get(date, ""), reverse=True)
                    profile.setdefault("feedback_tombstones", {}).pop(name + ":" + ref, None)
                else:
                    profile.setdefault("feedback_tombstones", {})[name + ":" + ref] = stamp
                profile[name] = rows
                versions[name + ":" + ref] = secrets.token_hex(12)
            row["undone"] = True
            profile.update(revision=secrets.token_hex(12), updated_at=stamp)
            await self._write(scope, "profile", profile)
            return self._shelf_view(profile)

    @staticmethod
    def _preset_record(record):
        if not record: return {"revision": "", "items": [], "archived": []}
        if not isinstance(record, dict) or not isinstance(record.get("items"), list) or not re.fullmatch(r"[0-9a-f]{24}", str(record.get("revision", ""))):
            raise PersonalProfileError("筛选方案暂不可用。", status=503)
        return copy.deepcopy(record)

    async def get_presets(self, profile_token):
        scope = self._scope(profile_token)
        async with self._locks.setdefault(scope, asyncio.Lock()):
            return self._preset_record(await self._read(scope, "filter-presets"))

    async def change_preset(self, profile_token, change):
        if (not isinstance(change, dict) or not {"operation", "revision"} <= change.keys() or change.keys() - {"operation", "revision", "id", "name", "filters"}
                or change.get("operation") not in {"save", "remove", "restore"} or not isinstance(change.get("revision"), str) or not re.fullmatch(r"[0-9a-f]{24}|", change["revision"])):
            raise PersonalProfileError("筛选方案操作无效。")
        operation = change["operation"]
        if operation == "save":
            name = re.sub(r"\s+", " ", change.get("name", "")).strip() if isinstance(change.get("name"), str) else ""
            if not 1 <= len(name) <= 32 or any(ord(c) < 32 for c in name): raise PersonalProfileError("方案名称请填写 1–32 个字。")
            selection = wall_selection(change.get("filters"))
        elif not isinstance(change.get("id"), str) or not re.fullmatch(r"[0-9a-f]{16}", change["id"]):
            raise PersonalProfileError("筛选方案身份无效。")
        scope = self._scope(profile_token)
        async with self._locks.setdefault(scope, asyncio.Lock()):
            record = self._preset_record(await self._read(scope, "filter-presets"))
            if record["revision"] != change["revision"]: raise PersonalProfileError("另一设备已修改方案，请重新载入。", status=409)
            archived = record.setdefault("archived", [])
            if operation == "save":
                if any(row["name"].casefold() == name.casefold() for row in record["items"]): raise PersonalProfileError("已有同名方案，请使用其他名称。", status=409)
                if len(record["items"]) >= 20: raise PersonalProfileError("最多收藏 20 个方案，请先移除不用的方案。")
                if len(record["items"]) + len(archived) >= 100: raise PersonalProfileError("方案存档已满，请恢复已有方案使用。")
                record["items"].append({"id": secrets.token_hex(8), "name": name, "filters": selection, "created_at": _now()})
            else:
                source, target = (record["items"], archived) if operation == "remove" else (archived, record["items"])
                entry = next((row for row in source if row["id"] == change["id"]), None)
                if entry is None: raise PersonalProfileError("此方案已变化，请重新载入。", status=409)
                if operation == "restore" and (len(target) >= 20 or any(row["name"].casefold() == entry["name"].casefold() for row in target)):
                    raise PersonalProfileError("方案已满或有同名方案，暂时无法恢复。", status=409)
                source.remove(entry);target.append(entry)
            record["revision"] = secrets.token_hex(12)
            await self._write(scope, "filter-presets", record)
            return copy.deepcopy(record)

    async def apply_preset(self, profile_token, choice):
        if (not isinstance(choice, dict) or set(choice) != {"id", "presets_revision", "filters_revision"}
                or not isinstance(choice["id"], str) or not re.fullmatch(r"[0-9a-f]{16}", choice["id"])
                or any(not isinstance(choice[key], str) or not re.fullmatch(r"[0-9a-f]{24}|", choice[key]) for key in ("presets_revision", "filters_revision"))):
            raise PersonalProfileError("快捷方案参数无效。")
        scope = self._scope(profile_token)
        async with self._locks.setdefault(scope, asyncio.Lock()):
            presets = self._preset_record(await self._read(scope, "filter-presets"))
            if presets["revision"] != choice["presets_revision"]:
                raise PersonalProfileError("收藏方案已变化，请刷新后重新选择。", status=409)
            entry = next((row for row in presets["items"] if row["id"] == choice["id"]), None)
            if entry is None: raise PersonalProfileError("此方案已被移除，请选择其他方案。", status=409)
            current = self._wall_settings(await self._read(scope, "wall-filters"))
            if current["revision"] != choice["filters_revision"]:
                raise PersonalProfileError("另一设备已修改筛选，请刷新后重新选择。", status=409)
            value = wall_selection({"genres": [], "exclude_genres": [], "exploration": "balanced", **entry["filters"]})
            if current["filters"] == value: return current
            result = {"filters": value, "revision": secrets.token_hex(12), "updated_at": _now()}
            await self._write(scope, "wall-filters", result)
            return copy.deepcopy(result)

    @staticmethod
    def _history_item(item):
        if not isinstance(item, dict) or not re.fullmatch(r"tmdb:(movie|tv):[1-9]\d{0,10}", str(item.get("detail_ref", ""))) or not isinstance(item.get("title"), str) or not item["title"].strip():
            raise PersonalProfileError("浏览影片资料无效。")
        ref = item["detail_ref"];meta = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        poster = _safe_url(item.get("poster_url"))
        return {"id": "personal:" + ref, "detail_ref": ref, "title": item["title"].strip()[:180], "media_type": ref.split(":")[1],
                "year": item.get("year") if type(item.get("year")) is int and 1880 <= item["year"] <= datetime.now(timezone.utc).year + 1 else None,
                "rating": _finite(item.get("rating")), "rating_source": "TMDB", "poster_url": poster if poster and len(poster) <= 600 else None,
                "source_url": "https://www.themoviedb.org/" + "/".join(ref.split(":")[1:]),
                "metadata": {"genre_ids": [v for v in meta.get("genre_ids", [])[:30] if type(v) is int and 0 < v < 1000000] if isinstance(meta.get("genre_ids"), list) else [],
                             "origin_country": [v for v in meta.get("origin_country", [])[:10] if isinstance(v, str) and re.fullmatch(r"[A-Z]{2}", v)] if isinstance(meta.get("origin_country"), list) else [],
                             "original_language": str(meta.get("original_language", ""))[:3], "minimum_votes": _finite(meta.get("minimum_votes")) or 0,
                             "language_verified": meta.get("language_verified") is True, "vote_count": _finite(meta.get("vote_count"))}}

    async def _history_record(self, scope):
        record = await self._read(scope, "browse-history")
        if not record: return {"views": [], "walls": []}
        if not isinstance(record, dict) or not isinstance(record.get("views"), list) or not isinstance(record.get("walls"), list):
            raise PersonalProfileError("浏览历史暂不可用，请稍后重试。", status=503)
        return record

    async def get_history(self, profile_token):
        scope = self._scope(profile_token)
        async with self._locks.setdefault(scope, asyncio.Lock()):
            return copy.deepcopy(await self._history_record(scope))

    async def record_view(self, profile_token, item):
        item = self._history_item(item)
        scope = self._scope(profile_token)
        async with self._locks.setdefault(scope, asyncio.Lock()):
            record = await self._history_record(scope)
            record["views"] = [{"item": item, "viewed_at": _now()}] + [row for row in record["views"] if row.get("item", {}).get("detail_ref") != item["detail_ref"]][:119]
            await self._write(scope, "browse-history", record)
            return {"saved": True}

    async def _history_wall(self, scope, identity, result):
        record = await self._history_record(scope)
        items = [self._history_item(item) for item in result["items"][:96]]
        entry = {"id": identity.removeprefix("batch:"), "items": items, "displayed_at": _now(), "filters": result["filters"]}
        record["walls"] = [entry] + [row for row in record["walls"] if row.get("id") != entry["id"]][:5]
        await self._write(scope, "browse-history", record)

    async def learning(self, profile_token, enabled):
        if type(enabled) is not bool:
            raise PersonalProfileError("学习开关无效。")
        scope = self._scope(profile_token)
        async with self._locks.setdefault(scope, asyncio.Lock()):
            profile = await self._profile(scope)
            profile.update(learning_enabled=enabled, behavior_setting_changed_at=_now(), revision=secrets.token_hex(12))
            await self._write(scope, "profile", profile)
            return self._profile_view(profile)

    async def _subscription_feature(self, ref):
        kind, identity = ref.split(":")[1:]
        try:
            if getattr(self.discovery, "_token", ""):
                value = await self.discovery._json("https://api.themoviedb.org/3/" + kind + "/" + identity, {"language": "zh-CN"}, tmdb=True)
                if value.get("id") != int(identity):
                    return None
                countries = value.get("origin_country") or [row.get("iso_3166_1") for row in value.get("production_countries", []) if isinstance(row, dict)]
                date = str(value.get("release_date" if kind == "movie" else "first_air_date") or "")
                item = {"media_type": kind, "year": int(date[:4]) if re.match(r"\d{4}", date) else None,
                        "metadata": {"genre_ids": [row.get("id") for row in value.get("genres", []) if isinstance(row, dict)],
                            "origin_country": countries, "original_language": value.get("original_language")}}
            else:
                ops = await self.ops_getter() if self.ops_getter else None
                if ops is None:
                    return None
                value = await ops.call("discover.get-title-details", {"title_ref": ref})
                value = value.get("data", value)
                title, metadata = value.get("title", {}), value.get("metadata", {})
                if title.get("title_ref") != ref:
                    return None
                item = {"media_type": kind, "year": title.get("release_year"), "metadata": {
                    "genre_ids": [HOST_GENRES[name.casefold()] for name in title.get("genres", []) if isinstance(name, str) and name.casefold() in HOST_GENRES],
                    "origin_country": [], "original_language": metadata.get("language")}}
            return {"features": _signal(item), "observed_at": _now()}
        except Exception:
            return None

    async def sync_subscriptions(self, profile_token, refs):
        if not isinstance(refs, list) or len(refs) > 1000 or any(not isinstance(ref, str) or not re.fullmatch(r"tmdb:(movie|tv):[1-9]\d{0,10}", ref) for ref in refs):
            raise PersonalProfileError("订阅记录格式无效。")
        refs = list(dict.fromkeys(refs))
        scope = self._scope(profile_token)
        async with self._locks.setdefault(scope, asyncio.Lock()):
            profile = await self._profile(scope)
            old = profile.get("subscriptions") or {}
            features = {ref: row for ref, row in old.get("features", {}).items() if ref in refs[:120]}
            lookup = {item["detail_ref"]: item for pool in self._pools.values() for item in pool.get("items", [])}
            lookup.update({item["detail_ref"]: item for item in (self._public_snapshot or {}).get("items", [])})
            for ref in refs[:120]:
                if ref not in features and ref in lookup:
                    features[ref] = {"features": _signal(lookup[ref]), "observed_at": _now()}
            pending = [ref for ref in refs[:120] if ref not in features][:12] if profile.get("learning_enabled", True) else []
            semaphore = asyncio.Semaphore(4)
            async def fetch(ref):
                async with semaphore:
                    try:
                        async with asyncio.timeout(1.5):
                            return ref, await self._subscription_feature(ref)
                    except TimeoutError:
                        return ref, None
            for ref, value in await asyncio.gather(*(fetch(ref) for ref in pending)):
                if value:
                    features[ref] = value
            value = {"refs": refs, "features": features, "scope": "visible"}
            if value != old:
                profile.update(subscriptions=value, revision=secrets.token_hex(12))
                await self._write(scope, "profile", profile)
            return self._profile_view(profile)

    def _scope(self, token):
        if self._closed:
            raise PersonalProfileError("私人推荐服务已停止。", status=503)
        if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
            raise PersonalProfileError("私人口味档案不可用，请在当前浏览器重新建立。", status=404)
        return "taste:" + hashlib.sha256(token.encode()).hexdigest()[:32]

    async def _call(self, method, *args, **kwargs):
        if self.store is None:
            raise PersonalProfileError("服务器暂未开放私人档案存储。", status=503)
        try:
            result = getattr(self.store, method)(*args, **kwargs)
            return await result if inspect.isawaitable(result) else result
        except Exception:
            raise PersonalProfileError("暂时无法读取或保存私人档案，请稍后重试。", status=503) from None

    async def _read(self, scope, key, _retry=True):
        meta = await self._call("get", "m:" + key, scope=scope, default=None)
        if not isinstance(meta, dict) or meta.get("version") != 1:
            return None
        if type(meta.get("chunks")) is not int or not 1 <= meta["chunks"] <= 48:
            return None
        generation = meta.get("generation")
        if not isinstance(generation, str) or not re.fullmatch(r"[0-9a-f]{24}", generation):
            return None
        try:
            parts = []
            for index in range(meta["chunks"]):
                value = await self._call("get", f"b:{key}:{generation}:{index}", scope=scope, default=None)
                if not isinstance(value, str) or len(value) > CHUNK_SIZE:
                    if _retry:
                        current = await self._call("get", "m:" + key, scope=scope, default=None)
                        if isinstance(current, dict) and current.get("generation") != generation:
                            return await self._read(scope, key, _retry=False)
                    return None
                parts.append(value)
            decoder = zlib.decompressobj()
            raw = decoder.decompress(base64.b64decode("".join(parts), validate=True), MAX_PRIVATE_BYTES + 1)
            if len(raw) > MAX_PRIVATE_BYTES or not decoder.eof or decoder.unconsumed_tail or hashlib.sha256(raw).hexdigest() != meta.get("digest"):
                return None
            return json.loads(raw)
        except (ValueError, TypeError, zlib.error):
            return None

    async def _write(self, scope, key, value):
        raw = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
        if len(raw) > MAX_PRIVATE_BYTES:
            raise PersonalProfileError("私人档案已超过存储容量。")
        digest = hashlib.sha256(raw).hexdigest()
        generation = digest[:24]
        encoded = base64.b64encode(zlib.compress(raw, 6)).decode()
        chunks = [encoded[index:index + CHUNK_SIZE] for index in range(0, len(encoded), CHUNK_SIZE)]
        old = await self._call("get", "m:" + key, scope=scope, default=None)
        written = []
        try:
            for index, chunk in enumerate(chunks):
                name = f"b:{key}:{generation}:{index}"
                await self._call("set", name, chunk, scope=scope, secret=True)
                written.append(name)
            await self._call("set", "m:" + key, {"version": 1, "chunks": len(chunks), "generation": generation, "digest": digest}, scope=scope, secret=True)
        except Exception:
            # A failed RPC can have committed remotely. Never delete a live or
            # uncertain generation; confirm the manifest before cleaning up.
            known, current = False, None
            try:
                current = await self._call("get", "m:" + key, scope=scope, default=None)
                known = True
            except Exception:
                pass
            if known and isinstance(current, dict) and current.get("generation") == generation:
                pass
            else:
                safe_to_clean = known and (not isinstance(old, dict) or old.get("generation") != generation)
                if safe_to_clean:
                    for name in written:
                        try:
                            await self._call("delete", name, scope=scope)
                        except Exception:
                            pass
                raise PersonalProfileError("暂时无法保存私人档案，请稍后重试。", status=503) from None
        if isinstance(old, dict) and old.get("generation") != generation and type(old.get("chunks")) is int and 0 < old["chunks"] <= 48:
            for index in range(old["chunks"]):
                try:
                    await self._call("delete", f"b:{key}:{old.get('generation')}:{index}", scope=scope)
                except Exception:
                    pass

    async def _profile(self, scope):
        value = await self._read(scope, "profile")
        if not isinstance(value, dict) or value.get("version") != 1:
            raise PersonalProfileError("私人口味档案不可用，请在当前浏览器重新建立。", status=404)
        return value

    def _profile_view(self, profile):
        tested = bool(profile.get("history"))
        answers = profile.get("answers") or DEFAULT_ANSWERS
        return {"test_revision": profile["history"][-1]["id"] if profile["history"] else "", "has_test": tested, "revision": profile["revision"], "updated_at": profile["updated_at"],
                "history_count": profile.get("test_count", len(profile["history"])), "answers": copy.deepcopy(profile.get("answers") or {}),
                "summary": _summary(answers, profile.get("vector") or _vector(answers), tested),
                "learning": {"enabled": profile.get("learning_enabled", True), "saved": len(profile.get("saved", [])),
                    "dismissed": len(profile.get("dismissed", [])), "watched": len(profile.get("watched", [])), "liked": len(profile.get("liked", [])), "subscriptions": len((profile.get("subscriptions") or {}).get("refs", [])),
                    "subscription_features": len((profile.get("subscriptions") or {}).get("features", {}))},
                "history": [{"id": row["id"], "created_at": row["created_at"], "answers": copy.deepcopy(row["answers"]),
                             "summary": _summary(row["answers"], row["vector"])} for row in reversed(profile["history"])]}

    async def create_profile(self):
        token = secrets.token_urlsafe(32)
        scope = self._scope(token)
        profile = {"version": 1, "revision": secrets.token_hex(12), "updated_at": _now(), "answers": {}, "vector": {},
                   "history": [], "test_count": 0, "recent": [], "batches": []}
        await self._write(scope, "profile", profile)
        return {"profile_token": token, "profile": self._profile_view(profile)}

    async def get_profile(self, profile_token):
        scope = self._scope(profile_token)
        async with self._locks.setdefault(scope, asyncio.Lock()):
            return self._profile_view(await self._profile(scope))

    async def submit(self, profile_token, answers, expected_test=None):
        scope, answers = self._scope(profile_token), _validate_answers(answers)
        async with self._locks.setdefault(scope, asyncio.Lock()):
            profile = await self._profile(scope)
            current_test = profile["history"][-1]["id"] if profile["history"] else ""
            if expected_test is not None and expected_test != current_test:
                raise PersonalProfileError("另一设备已更新口味，请重新载入测试。", status=409)
            vector = _vector(answers)
            combined = _combined(vector, profile["history"])
            stamp, revision = _now(), secrets.token_hex(12)
            profile["history"].append({"id": revision, "created_at": stamp, "answers": answers, "vector": vector})
            profile["history"] = profile["history"][-MAX_HISTORY:]
            profile.update(answers=answers, vector=combined, updated_at=stamp, revision=revision, test_count=profile.get("test_count", 0) + 1)
            await self._write(scope, "profile", profile)
            return self._profile_view(profile)

    async def _query(self, media_type, era, page=1, genre=None, country=None, backend="tmdb", ops=None):
        if backend == "tmdb":
            params = {"language": "zh-CN", "include_adult": "false", "sort_by": "vote_average.desc", "page": page,
                      "vote_average.gte": MIN_RATING, "vote_count.gte": 100 if media_type == "movie" else 50,
                      "without_genres": "99" if media_type == "movie" else "99,10763,10764,10767"}
            prefix = "primary_release_date" if media_type == "movie" else "first_air_date"
            if isinstance(era, tuple) and len(era) == 2:
                low, high = era
                if low is not None: params[prefix + ".gte"] = f"{low}-01-01"
                if high is not None: params[prefix + ".lte"] = f"{high}-12-31"
            elif isinstance(era, str) and era.startswith("wall:") and era[5:] in WALL_ERAS:
                era = era[5:]
                low, high = WALL_ERAS[era]
                if low is not None: params[prefix + ".gte"] = f"{low}-01-01"
                if high is not None: params[prefix + ".lte"] = f"{high}-12-31"
            elif era == "classic":
                params[prefix + ".lte"] = "1989-12-31"
            elif era == "middle":
                params.update({prefix + ".gte": "1990-01-01", prefix + ".lte": "2009-12-31"})
            elif era == "modern":
                params[prefix + ".gte"] = "2010-01-01"
            if genre:
                params["with_genres"] = genre
            if country:
                params["with_origin_country"] = country
            data = await self.discovery._json("https://api.themoviedb.org/3/discover/" + media_type, params, tmdb=True)
            rows = data.get("results", [])
        else:
            params = {"media_type": media_type, "rating_gte": MIN_RATING, "sort": "rating", "page": page}
            if genre:
                params["genre_ids"] = [int(genre)]
            if country:
                params["origin_country"] = country
            if type(era) is int:
                params["year"] = era
            data = await ops.call("discover.filter-titles", params)
            rows = data.get("titles", [])
        items = [_public_item(row, media_type, country, host=backend == "host") for row in rows]
        total = _finite(data.get("total_pages")) or 1
        return [item for item in items if item], max(1, min(500, int(total)))

    async def _pool(self, answers, nonce, expand=False, _backend=None, query_budget=6, deadline=None, _restore=True):
        backend = _backend or ("tmdb" if getattr(self.discovery, "_token", "") else "host")
        signature = (backend, tuple(sorted(answers["themes"])), tuple(sorted(answers["regions"])), tuple(answers.get("_learned_genres", [])))
        chosen_country, chosen_era, chosen_kind = answers.get("_wall_country", "all"), answers.get("_wall_era", "all"), answers.get("_wall_kind", "all")
        chosen_genres = answers.get("_wall_genres", "all")
        targeted = chosen_country != "all" or chosen_era != "all" or chosen_kind != "all" or chosen_genres != "all"
        if chosen_genres != "all": signature += (chosen_genres,)
        if targeted: signature += (chosen_country, chosen_era, chosen_kind)
        async with self._pool_locks.setdefault(signature, asyncio.Lock()):
            cached = self._pools.get(signature)
            if cached is None and _restore and not targeted:
                await self._load_public_catalog()
                if self._public_live_pool is not None:
                    cached = copy.deepcopy(self._public_live_pool)
                elif self._public_snapshot is not None:
                    cached = self._restored_pool(self._public_snapshot)
                if cached is not None:
                    self._pools[signature] = cached
            if cached and cached.get("restored") and _restore and not expand:
                self._schedule_public_refresh()
                return cached
            if cached and cached["expires"] > time.monotonic() and not expand and (_restore or not cached.get("restored")):
                if cached.get("restored"):
                    self._schedule_public_refresh()
                return cached
            if cached and expand:
                backend = cached["backend"]
            try:
                ops = await self.ops_getter() if backend == "host" and self.ops_getter else None
            except Exception:
                if cached and cached["items"]:
                    cached.update(status="stale", expires=time.monotonic() + 120)
                    return cached
                return None
            if backend == "host" and ops is None:
                return None
            genres = list(dict.fromkeys([str(genre) for genre in answers.get("_learned_genres", [])] + [str(genre) for theme in answers["themes"] for genre in THEMES[theme]][:1]))[:3]
            countries = list(dict.fromkeys(country for region in answers["regions"] for country in REGIONS[region][0]))
            requests = [(kind, era, 1, None, None) for kind in ("movie", "tv") for era in
                        (("classic", "middle", "modern") if backend == "tmdb" else (None,))]
            requests += [(kind, None, 1, genre, None) for genre in genres for kind in ("movie", "tv")] if genres else []
            requests += [(kind, None, 1, None, country) for country in countries[:3] for kind in ("movie", "tv")]
            if targeted:
                kinds = ("movie", "tv") if chosen_kind == "all" else (chosen_kind,)
                countries = wall_values(chosen_country, WALL_COUNTRIES) or (None,)
                windows = wall_windows(chosen_era)
                if backend == "host" and chosen_era != "all":
                    lanes = []
                    for low, high in windows:
                        years = list(range(max(1880, low or 1900), min(high or datetime.now(timezone.utc).year, datetime.now(timezone.utc).year) + 1))
                        random.Random(hashlib.sha256((nonce + repr((low, high))).encode()).digest()).shuffle(years)
                        lanes.extend(years[:4])
                else:
                    lanes = [window if window != (None, None) else None for window in windows]
                requests = [(kind, lane, 1, genre, country) for lane in lanes for genre in (wall_values(chosen_genres, {"all": "不限", **{str(k): v for k, v in GENRES.items()}}) or (None,)) for country in countries for kind in kinds]
            deferred = requests[8:] if targeted else []
            if targeted: requests = requests[:8]
            if expand and cached:
                queued = list(cached["next_queries"])
                requests, deferred = queued[:query_budget], queued[query_budget:]
                if not requests:
                    return cached
            semaphore = asyncio.Semaphore(4)
            async def fetch(query):
                async with semaphore:
                    kind, era, page, genre, country = query
                    if deadline is None:
                        value = await self._query(kind, era, page, genre, country, backend, ops)
                    else:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise TimeoutError("公开候选补页已达到本次时间预算")
                        value = await asyncio.wait_for(self._query(kind, era, page, genre, country, backend, ops), remaining)
                    return query, value
            results = await asyncio.gather(*(fetch(query) for query in requests), return_exceptions=True)
            items = {item["id"]: item for item in cached["items"]} if cached and expand else {}
            next_queries, partial, successful, neutral_rows = deferred, False, 0, []
            fetched_queries = set(cached.get("fetched_queries", ())) if cached and expand else set()
            attempted_queries = set(cached.get("attempted_queries", fetched_queries)) if cached and expand else set()
            stream_totals = dict(cached.get("stream_totals", {})) if cached and expand else {}
            for query, result in zip(requests, results):
                attempted_queries.add(query)
                kind, era, page, genre, country = query
                stream = (kind, era, genre, country)
                if isinstance(result, Exception):
                    partial = True
                    total = stream_totals.get(stream, 1)
                else:
                    _, (rows, total) = result
                    successful += 1
                    fetched_queries.add(query)
                    stream_totals[stream] = total
                    items.update({item["id"]: item for item in rows})
                    if genre is None and country is None:
                        neutral_rows.extend(rows)
                remaining = [candidate for candidate in range(1, total + 1) if (kind, era, candidate, genre, country) not in attempted_queries]
                if remaining:
                    rng = random.Random(hashlib.sha256((nonce + repr((kind, era, genre, country))).encode()).digest())
                    next_page = rng.choice(remaining)
                    next_queries.append((kind, era, next_page, genre, country))
            if not successful:
                if cached and cached["items"]:
                    cached.update(status="stale", expires=time.monotonic() + 120, next_queries=next_queries,
                                  attempted_queries=attempted_queries, stream_totals=stream_totals)
                    return cached
                if backend == "tmdb" and self.ops_getter is not None:
                    fallback = await self._pool(answers, nonce, _backend="host", _restore=_restore, deadline=deadline)
                    if fallback:
                        self._pools[signature] = fallback
                    return fallback
                return None
            pool = {"items": list(items.values())[-MAX_POOL_ITEMS:], "expires": time.monotonic() + 3600,
                    "next_queries": next_queries, "fetched_queries": fetched_queries, "attempted_queries": attempted_queries,
                    "stream_totals": stream_totals, "last_query_count": len(requests), "status": "ready", "partial": partial, "backend": backend}
            if len(self._pools) >= 24 and signature not in self._pools:
                self._pools.pop(next(iter(self._pools)))
            self._pools[signature] = pool
            if not targeted:
                await self._save_public_catalog(neutral_rows, backend)
            return pool

    def _rank(self, item, vector, answers, rng, recent, behavior=None):
        metadata, year = item["metadata"], item["year"]
        genres = metadata["genre_ids"]
        avoided = set().union(*(AVOID[choice] for choice in answers["avoid"]))
        if (avoided and not genres) or set(genres).intersection(avoided):
            return None
        score, reasons = 35 + (item["rating"] - MIN_RATING) * 8, []
        genre_score = max((vector.get("genre:" + str(genre), 0) for genre in genres), default=0)
        exploration = answers.get("_wall_exploration", "balanced")
        score += {"familiar": 34, "balanced": 24, "discovery": 12}[exploration] * genre_score
        matching = [GENRES[genre] for genre in genres if genre in GENRES and vector.get("genre:" + str(genre), 0) >= .5]
        if matching:
            reasons.append("与你偏好的" + "、".join(matching[:2]) + "题材相近")
        location = max([vector.get("country:" + country, 0) for country in metadata["origin_country"]]
                       + [vector.get("language:" + metadata["original_language"], 0), 0])
        score += {"familiar": 20, "balanced": 15, "discovery": 6}[exploration] * location
        if location >= .5:
            country_name = next((REGION_NAMES[country] for country in metadata["origin_country"] if country in REGION_NAMES), None)
            reasons.append((country_name + "的故事，贴近你的地区偏好") if country_name else "原始语言贴近你的偏好")
        if year:
            score += 8 * (vector.get("era:classic", 0) if year < 2000 else vector.get("era:modern", 0) if year >= 2010 else vector.get("era:mixed", 0))
        if vector.get("pace:fast", 0) and set(genres).intersection({28, 12, 53, 9648, 10759}):
            score += 4 * vector["pace:fast"]
        if vector.get("pace:quiet", 0) and set(genres).intersection({18, 36, 10749}):
            score += 4 * vector["pace:quiet"]
        mood_genres = {"warm": {18, 10751}, "light": {35, 10751}, "dark": {80, 53, 27}, "epic": {12, 10752, 10768, 10765}}
        score += max((5 * vector.get("mood:" + mood, 0) for mood, values in mood_genres.items() if set(genres).intersection(values)), default=0)
        familiarity = min(1, math.log10(max(1, metadata["vote_count"] or 50)) / 5)
        score += 5 * familiarity * vector.get("exploration:familiar", 0) + 5 * (1 - familiarity) * vector.get("exploration:discovery", 0)
        if behavior and rng.random() >= {"familiar": .05, "balanced": .2, "discovery": .5}[exploration]:
            positive, negative, _ = behavior
            features = _signal(item)
            def similarity(values):
                genre = max((values.get(key, 0) for key in features if key.startswith("genre:")), default=0)
                country = max((values.get(key, 0) for key in features if key.startswith(("country:", "language:"))), default=0)
                era = max((values.get(key, 0) for key in features if key.startswith("era:")), default=0)
                return .65 * genre + .2 * country + .1 * era + .05 * values.get("format:" + item["media_type"], 0)
            boost, penalty = 12 * similarity(positive), 8 * similarity(negative)
            score += boost - penalty
            if boost - penalty >= 2:
                reasons.insert(0, "参考你的喜欢、片单与观看偏好")
        score += rng.uniform(0, {"familiar": 3, "balanced": 8, "discovery": 20}[exploration])
        reasons.append((str(year) + "年的" + ("经典选择" if year < 2000 else "口碑故事")) if year else "有评分基础的真实剧库作品")
        reasons.append(f"TMDB {item['rating']:.1f} 分 · 有评分基础")
        return score, reasons[:3]

    def _select(self, pool, profile, nonce, limit):
        answers = profile.get("answers") or DEFAULT_ANSWERS
        vector = (profile.get("vector") or _vector(answers)) if profile["history"] else {}
        rng = random.Random(hashlib.sha256((profile["revision"] + ":" + nonce).encode()).digest())
        recent = {identity for batch in profile.get("recent", []) for identity in batch}
        ranked = []
        behavior = _behavior(profile, pool)
        for item in pool["items"]:
            result = self._rank(item, vector, answers, rng, recent, behavior)
            if result:
                ranked.append((result[0], item, result[1]))
        ranked.sort(key=lambda row: row[0], reverse=True)
        selected, seen, counts = [], set(), {"movie": 0, "tv": 0, "classic": 0, "middle": 0, "modern": 0}
        movie_ratio = .75 if answers["format"] == "movie" else .25 if answers["format"] == "tv" else .5
        targets = {"movie": round(limit * movie_ratio), "tv": limit - round(limit * movie_ratio)}
        # Hard novelty comes before era, format or taste. A soft score penalty
        # allowed old era seeds to displace unseen works from another era.
        active = []
        def choose(predicate):
            if len(selected) >= limit:
                return False
            options = [row for row in active if row[1]["id"] not in seen and predicate(row[1])]
            if not options:
                return False
            def adjusted(row):
                score, item, _ = row
                era = _era(item["year"])
                country = next(iter(item["metadata"]["origin_country"]), item["metadata"]["original_language"])
                genre = next(iter(item["metadata"]["genre_ids"]), 0)
                return score - ({"familiar": .6, "balanced": 1, "discovery": 2.5}[answers.get("_wall_exploration", "balanced")]) * (counts.get("country:" + country, 0) * 2.2 + counts.get("genre:" + str(genre), 0) * 1.4 + counts[era] * .8)
            score, item, reasons = max(options, key=adjusted)
            value = copy.deepcopy(item)
            value["recommendation_reasons"] = reasons
            value["metadata"]["is_exploration"] = not any("偏好" in reason or "题材相近" in reason for reason in reasons)
            value["metadata"]["is_new_to_recent"] = item["id"] not in recent
            selected.append(value)
            seen.add(item["id"])
            counts[item["media_type"]] += 1
            counts[_era(item["year"])] += 1
            country = next(iter(item["metadata"]["origin_country"]), item["metadata"]["original_language"])
            genre = next(iter(item["metadata"]["genre_ids"]), 0)
            counts["country:" + country] = counts.get("country:" + country, 0) + 1
            counts["genre:" + str(genre)] = counts.get("genre:" + str(genre), 0) + 1
            return True
        ages = {identity: index for index, batch in enumerate(profile.get("recent", [])) for identity in batch}
        groups = [[row for row in ranked if row[1]["id"] not in recent]]
        groups.extend([row for row in ranked if ages.get(row[1]["id"]) == age] for age in range(max(0, len(profile.get("recent", [])) - 1)))
        # 只有恢复池里确实没有上面以外作品时才允许显示旧墙，不能让离线恢复变成空白。
        last_ids = set(profile.get("recent", [[]])[-1]) if profile.get("recent") else set()
        if ranked and all(row[1]["id"] in last_ids for row in ranked):
            groups.append(ranked)
        for active in groups:
            for era in ("classic", "middle", "modern"):
                for _ in range(max(1, min(4, limit // 8))):
                    choose(lambda item, era=era: _era(item["year"]) == era and item["year"] is not None)
            for kind in ("movie", "tv"):
                while len(selected) < limit and counts[kind] < targets[kind]:
                    if not choose(lambda item, kind=kind: item["media_type"] == kind):
                        break
            while len(selected) < limit and choose(lambda item: True):
                pass
            if len(selected) >= limit:
                break
        rng.shuffle(selected)
        return selected

    async def recommend(self, profile_token, nonce="0", limit=48, media_type="all", era="all", country="all", genres="all", exclude_genres="all", exploration="balanced", *, _prepare=False, _after=None, _queue_id="", expected_filters=None):
        scope = self._scope(profile_token)
        if not valid_wall_filters(media_type, era, country, genres, exclude_genres, exploration):
            raise PersonalProfileError("海报筛选条件无效。")
        era, country = wall_canonical(era, WALL_ERAS), wall_canonical(country, WALL_COUNTRIES)
        if not isinstance(nonce, str) or not 1 <= len(nonce) <= 96 or type(limit) is not int or not 3 <= limit <= 96:
            raise PersonalProfileError("海报墙参数无效。")
        async with self._locks.setdefault(scope, asyncio.Lock()):
            profile = await self._profile(scope)
            filter_revision = await self._filter_revision(scope, expected_filters)
            basis = self._wall_basis(profile)
            options = [limit, media_type, era] + ([country] if country != "all" else []) + ([wall_extra(genres, exclude_genres, exploration)] if wall_extra(genres, exclude_genres, exploration) else [])
            stage_key = await self._stage_key(scope, _queue_id, create=True) if _prepare else "next-wall"
            stage = await self._read(scope, stage_key) if _prepare else None
            if not isinstance(stage, dict) or stage.get("basis") != basis or stage.get("options") != options or stage.get("filters_revision", "") != filter_revision:
                stage = {"basis": basis, "options": options, "filters_revision": filter_revision, "queue": []}
            queue = stage.get("queue", [])
            # Expiring the head also discards descendants selected against it.
            if any(row.get("expires", 0) < time.time() for row in queue):
                queue = stage["queue"] = []
            if _prepare and _after is not None:
                if not isinstance(_after, str) or _after and not re.fullmatch(r"[0-9a-f]{48}", _after):
                    raise PersonalProfileError("预备队列无效。", status=409)
                positions = {row["ticket"]: index for index, row in enumerate(queue)}
                if not _after or _after == stage.get("receipt", {}).get("ticket"):
                    position = 0
                elif _after in positions:
                    position = positions[_after] + 1
                else:
                    raise PersonalProfileError("预备队列已更新。", status=409)
                if position < len(queue):
                    row = queue[position]
                    return {**row["result"], "prepared_ticket": row["ticket"]}
            if _prepare and len(queue) >= 3:
                row = queue[-1]
                return {**row["result"], "prepared_ticket": row["ticket"]}
            reserved = {item["detail_ref"] for row in queue for item in row["result"]["items"]} if _prepare else set()
            if reserved:
                profile = copy.deepcopy(profile)
                profile["recent"] = profile.get("recent", []) + [[item["id"] for item in row["result"]["items"]] for row in queue]
            filters = wall_filter_key(media_type, era, country, genres, exclude_genres, exploration)
            batch_key = "batch:" + hashlib.sha256(("learning-v2:" + profile["revision"] + ":" + nonce + ":" + str(limit) + filters).encode()).hexdigest()[:32]
            cached = await self._read(scope, batch_key)
            if not _prepare and isinstance(cached, dict) and isinstance(cached.get("items"), list) and isinstance(cached.get("freshness"), dict):
                if cached.get("status") == "stale":
                    self._schedule_public_refresh()
                    cached.setdefault("cache", {})["refreshing"] = self._public_task is not None
                return cached
            answers = copy.deepcopy(profile.get("answers") or DEFAULT_ANSWERS)
            answers.update(_wall_country=country, _wall_era=era, _wall_kind=media_type, _wall_genres=wall_canonical(genres, {"all": "不限", **{str(k): v for k, v in GENRES.items()}}))
            await self._load_public_catalog()
            positive, negative, _ = _behavior(profile, self._public_snapshot or {"items": []})
            excluded = set().union(*(AVOID[choice] for choice in answers["avoid"]))
            learned = [(int(key[6:]), value - negative.get(key, 0) * .6) for key, value in positive.items() if key.startswith("genre:")]
            answers["_learned_genres"] = [genre for genre, weight in sorted(learned, key=lambda row: row[1], reverse=True) if weight >= .25 and genre not in excluded][:3]
            if not self._pools and self._public_snapshot is None and media_type == era == country == "all":
                # This is still the same authenticated browser capability. Its
                # encrypted successful batch must not be borrowed by any other
                # profile or copied into the shared public catalog.
                pool = await self._latest_private_batch(scope, profile)
                if pool:
                    self._schedule_public_refresh()
                else:
                    pool = await self._pool(answers, nonce, deadline=time.monotonic() + EXPANSION_SECONDS)
            else:
                pool = await self._pool(answers, nonce, deadline=time.monotonic() + EXPANSION_SECONDS)
            if not pool or not pool.get("items"):
                pool = await self._latest_private_batch(scope, profile) or pool
            recent = {identity for batch in profile.get("recent", []) for identity in batch}
            avoided = set().union(*(AVOID[choice] for choice in answers["avoid"]))
            dismissed = {row.get("detail_ref") for row in profile.get("dismissed", [])}
            held = {row.get("item", {}).get("detail_ref") for row in profile.get("saved", [])}
            held.update((profile.get("subscriptions") or {}).get("refs", []))
            held.update(row.get("item", {}).get("detail_ref") for row in profile.get("watched", []))
            included_genres = {int(g) for g in wall_values(genres, {"all": "不限", **{str(k): v for k, v in GENRES.items()}})}
            excluded_genres = {int(g) for g in wall_values(exclude_genres, {"all": "不限", **{str(k): v for k, v in GENRES.items()}})}
            def eligible(candidate):
                genres = set(candidate["metadata"]["genre_ids"])
                year = candidate.get("year")
                return (candidate.get("detail_ref") not in dismissed | held | reserved and not ((avoided and not genres) or genres.intersection(avoided))
                        and (not included_genres or bool(genres.intersection(included_genres)))
                        and not (genres.intersection(excluded_genres) or (excluded_genres and not genres))
                        and (media_type == "all" or candidate["media_type"] == media_type)
                        and (country == "all" or bool(set(wall_values(country, WALL_COUNTRIES)).intersection(candidate["metadata"]["origin_country"])))
                        and (era == "all" or type(year) is int and any((low is None or year >= low) and (high is None or year <= high) for low, high in wall_windows(era))))
            expanded, remaining_queries = False, EXPANSION_QUERIES
            deadline = time.monotonic() + EXPANSION_SECONDS
            # Reserve enough shared public candidates for repeated walls, while
            # bounding source calls and waiting time for small/overlapping pages.
            target = min(MAX_POOL_ITEMS, max(limit * 3, limit * 8 - len(recent) // 4))
            for _ in range(EXPANSION_ROUNDS):
                if not pool or pool["status"] == "stale" or not pool["next_queries"]:
                    break
                unseen = [item for item in pool["items"] if item["id"] not in recent and eligible(item)]
                if len(unseen) >= target or remaining_queries <= 0 or time.monotonic() >= deadline:
                    break
                budget = min(6, remaining_queries, len(pool["next_queries"]))
                pool = await self._pool(answers, nonce, expand=True, query_budget=budget, deadline=deadline)
                expanded = True
                remaining_queries -= budget
            selected_pool = {**pool, "items": [item for item in pool["items"] if eligible(item)]} if pool else None
            ranking_profile = {**profile, "answers": {**(profile.get("answers") or DEFAULT_ANSWERS), "_wall_exploration": exploration}}
            items = self._select(selected_pool, ranking_profile, nonce, limit) if selected_pool else []
            tested = bool(profile["history"])
            message = "本次偏好权重更高，结合历次测试，兼顾题材、地区与跨年代多样性。" if tested else "还没有测试，先为你提供跨年代的优质探索。完成口味测试后会更贴近你。"
            if pool and pool["backend"] == "host":
                message += " 当前通过宿主公开剧库抽样，语言、节奏与内容分级字段有限。"
            if pool and pool["status"] == "stale":
                if pool.get("private_fallback"):
                    message += " 先展示此私人口味档案上次成功的片单，正在后台重试剧库连接；可检查代理设置后重试。"
                elif pool.get("restored"):
                    message += " 先展示已持久保存的公开候选，正在后台重试剧库连接；可检查代理设置后重试。"
                else:
                    message += " 剧库连接暂时失败，当前使用上次成功获取的公开候选；可检查代理设置后重试。"
            if pool and pool["partial"]:
                message += " 部分公开剧库请求暂不可用，先展示已成功读取的候选。"
            if len(items) < limit:
                message += " 当前已缓存且符合条件的真实候选不足，暂不凑满。" if pool else " 暂时无法连接真实剧库，且没有可恢复的片单；请检查代理或服务器网络后重试，这不表示作品已下线。"
            new_count = sum(item["id"] not in recent for item in items)
            repeated_count = len(items) - new_count
            repeats = repeated_count > 0
            if repeats:
                message += f" 当前可用新候选不足，已从较早推荐中循环补充 {repeated_count} 部。"
            pool_count = len([item for item in pool["items"] if eligible(item)]) if pool else 0
            unseen_count = len([item for item in pool["items"] if eligible(item) and item["id"] not in recent]) if pool else 0
            source = {"kind": "personal_library", "label": "TMDB 真实剧库 · 私人口味海报墙", "url": "https://www.themoviedb.org/", "attribution": "TMDB"}
            result = {"items": items, "source": source, "status": pool["status"] if pool else "unavailable", "message": message,
                      "nonce": nonce, "batch_id": batch_key[6:], "fetched_at": _now(), "profile_summary": self._profile_view(profile)["summary"],
                      "filters": wall_filters(media_type, era, country, genres, exclude_genres, exploration),
                      "freshness": {"new_count": new_count, "repeated_count": repeated_count, "pool_count": pool_count,
                                    "window_batches": RECENT_BATCHES, "immediate_repeat_count": sum(item["id"] in set(profile.get("recent", [[]])[-1]) for item in items) if profile.get("recent") else 0, "expanded": expanded, "exhausted": unseen_count < limit},
                      "personalization": {"behavior": _behavior(profile, selected_pool or {"items": []})[2], "tested": tested, "history_count": profile.get("test_count", 0),
                                          "current_weight": .72 if len(profile["history"]) > 1 else 1, "previous_weight": .28 if len(profile["history"]) > 1 else 0,
                                          "repeated": repeats}}
            result["cache"] = {"restored": bool(pool and pool.get("restored")),
                               "private_batch": bool(pool and pool.get("private_fallback")),
                               "refreshing": self._public_task is not None,
                               "last_success_at": pool.get("fetched_at") if pool else None}
            if _prepare:
                ticket = secrets.token_hex(24)
                stage["queue"] = queue + [{"ticket": ticket, "expires": time.time() + 300, "batch_key": batch_key, "result": result}]
                await self._write(scope, stage_key, stage)
                return {**result, "prepared_ticket": ticket}
            if items:
                await self._record_wall(scope, profile, batch_key, result)
            return result

    @staticmethod
    def _wall_basis(profile):
        # Recommendation history changes independently of preference revision.
        return hashlib.sha256(json.dumps([profile["revision"], profile.get("recent", [])], sort_keys=True).encode()).hexdigest()

    async def _stage_key(self, scope, queue_id, *, create=False):
        if not isinstance(queue_id, str) or queue_id and not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", queue_id):
            raise PersonalProfileError("预备队列标识无效。")
        if not queue_id:
            return "next-wall"  # Older open pages remain compatible.
        key = "next-wall:" + hashlib.sha256(queue_id.encode()).hexdigest()[:24]
        if create:
            index = await self._read(scope, "next-wall-index")
            index = index if isinstance(index, list) else []
            expired = [row for row in index if row.get("at", 0) < time.time() - 300]
            active = [row for row in index if row not in expired and row.get("key") != key]
            evicted = expired + active[:-3]
            for row in evicted:
                old = row.get("key", "")
                if old == key or not re.fullmatch(r"next-wall:[0-9a-f]{24}", old):
                    continue
                meta = await self._call("get", "m:" + old, scope=scope, default=None)
                if isinstance(meta, dict) and type(meta.get("chunks")) is int and 0 < meta["chunks"] <= 48:
                    for i in range(meta["chunks"]):
                        await self._call("delete", f"b:{old}:{meta.get('generation')}:{i}", scope=scope)
                await self._call("delete", "m:" + old, scope=scope)
            await self._write(scope, "next-wall-index", (active + [{"key": key, "at": time.time()}])[-4:])
        return key

    async def resume_wall(self, profile_token, nonce="0", limit=48, media_type="all", era="all", country="all", genres="all", exclude_genres="all", exploration="balanced", expected_filters=None):
        if not valid_wall_filters(media_type, era, country, genres, exclude_genres, exploration) or type(limit) is not int or not 3 <= limit <= 96:
            raise PersonalProfileError("海报筛选条件无效。")
        era, country = wall_canonical(era, WALL_ERAS), wall_canonical(country, WALL_COUNTRIES)
        scope = self._scope(profile_token)
        async with self._locks.setdefault(scope, asyncio.Lock()):
            await self._filter_revision(scope, expected_filters)
            profile = await self._profile(scope)
            names = profile.get("batches", [])
            cached = await self._read(scope, names[-1]) if names else None
            empty = {"items": [], "status": "empty"}
            if not isinstance(cached, dict) or not isinstance(cached.get("items"), list) or len(cached["items"]) < limit:
                return empty
            if cached.get("filters") != wall_filters(media_type, era, country, genres, exclude_genres, exploration):
                return empty
            filters = wall_filter_key(media_type, era, country, genres, exclude_genres, exploration)
            expected = "batch:" + hashlib.sha256(("learning-v2:" + profile["revision"] + ":" + str(cached.get("nonce", "")) + ":" + str(limit) + filters).encode()).hexdigest()[:32]
            if names[-1] != expected:
                return empty
            excluded = {row.get("detail_ref") for row in profile.get("dismissed", [])}
            excluded.update(row.get("item", {}).get("detail_ref") for name in ("watched", "saved") for row in profile.get(name, []))
            excluded.update((profile.get("subscriptions") or {}).get("refs", []))
            if any(item.get("detail_ref") in excluded for item in cached["items"]):
                return empty
            result = copy.deepcopy(cached)
            result["cache"] = {**result.get("cache", {}), "resume": True}
            result["message"] = "已恢复上次海报墙，换一面继续发现。"
            for item in result["items"]:
                item.setdefault("metadata", {})["is_new_to_recent"] = False
            result["freshness"] = {**result.get("freshness", {}), "new_count": 0, "repeated_count": len(result["items"])}
            return result

    async def prepare_wall(self, profile_token, *, after=None, queue_id="", **options):
        return await self.recommend(profile_token, **options, _prepare=True, _after=after, _queue_id=queue_id)

    @staticmethod
    def _prepared_queue(stage):
        return [{**row["result"], "prepared_ticket": row["ticket"]} for row in stage.get("queue", [])]

    async def rotate_wall(self, profile_token, rotation, *, preview=False, queue_id="", expected_filters=None):
        """Validate before fading; only the subsequent visible patch records exposure."""
        if not isinstance(rotation, dict) or set(rotation) != {"ticket", "operation", "current", "replace"}:
            raise PersonalProfileError("轮替参数无效。")
        ticket, operation, current, replacements = (rotation[k] for k in ("ticket", "operation", "current", "replace"))
        if (not isinstance(ticket, str) or not re.fullmatch(r"[0-9a-f]{48}", ticket)
                or not isinstance(operation, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,48}", operation)
                or not isinstance(current, list) or not 3 <= len(current) <= 96
                or any(not isinstance(ref, str) or not re.fullmatch(r"tmdb:(?:movie|tv):[1-9]\d{0,11}", ref) for ref in current)
                or len(set(current)) != len(current) or not isinstance(replacements, dict) or not 1 <= len(replacements) <= 3
                or any(old not in current or not isinstance(new, str) for old, new in replacements.items())
                or len(set(replacements.values())) != len(replacements) or set(replacements.values()) & set(current)):
            raise PersonalProfileError("轮替参数无效。")
        fingerprint = hashlib.sha256(json.dumps(rotation, sort_keys=True).encode()).hexdigest()
        scope = self._scope(profile_token)
        async with self._locks.setdefault(scope, asyncio.Lock()):
            revision = await self._filter_revision(scope, expected_filters)
            profile = await self._profile(scope)
            key = await self._stage_key(scope, queue_id)
            stage = await self._read(scope, key)
            if not isinstance(stage, dict) or stage.get("basis") != self._wall_basis(profile) or stage.get("filters_revision", "") != revision:
                raise PersonalProfileError("偏好或展示历史已变化，请重新选片。", status=409)
            receipt = stage.get("rotation_receipt", {})
            if receipt.get("operation") == operation:
                if receipt.get("fingerprint") != fingerprint or receipt.get("expires", 0) < time.time():
                    raise PersonalProfileError("轮替记录已失效。", status=409)
                return {**receipt["result"], "prepared_queue": self._prepared_queue(stage)}
            queue = stage.get("queue", [])
            if not queue or queue[0].get("ticket") != ticket or any(row.get("expires", 0) < time.time() for row in queue):
                raise PersonalProfileError("预备片单已失效。", status=409)
            names = profile.get("batches", [])
            previous = await self._read(scope, names[-1]) if names else None
            if not isinstance(previous, dict) or previous.get("filters") != queue[0]["result"].get("filters"):
                raise PersonalProfileError("当前海报墙已变化。", status=409)
            displayed = {item["detail_ref"]: item for item in previous.get("items", [])}
            candidates = {item["detail_ref"]: item for item in queue[0]["result"]["items"]}
            if any(ref not in displayed for ref in current) or any(ref not in candidates for ref in replacements.values()):
                raise PersonalProfileError("轮替影片已变化。", status=409)
            result = copy.deepcopy(previous)
            result["items"] = [copy.deepcopy(candidates[replacements[ref]] if ref in replacements else displayed[ref]) for ref in current]
            result["nonce"] = "ambient-" + operation
            filters = result.get("filters", {})
            filter_key = wall_filter_key(filters.get("media_type", "all"), filters.get("era", "all"), filters.get("country", "all"), filters.get("genres", "all"), filters.get("exclude_genres", "all"), filters.get("exploration", "balanced"))
            batch_key = "batch:" + hashlib.sha256(("learning-v2:" + profile["revision"] + ":" + result["nonce"] + ":" + str(len(current)) + filter_key).encode()).hexdigest()[:32]
            result["batch_id"] = batch_key[6:]
            result["freshness"] = {**result.get("freshness", {}), "new_count": len(replacements), "repeated_count": len(current) - len(replacements)}
            result["rotation"] = {"count": len(replacements), "operation": operation}
            if preview:
                return result
            used = set(replacements.values())
            queue[0]["result"]["items"] = [item for item in queue[0]["result"]["items"] if item["detail_ref"] not in used]
            queue[0]["partial"] = True
            if not queue[0]["result"]["items"]:
                queue.pop(0)
            await self._record_wall(scope, profile, batch_key, result)
            stage["basis"] = self._wall_basis(profile)
            stage["rotation_receipt"] = {"operation": operation, "fingerprint": fingerprint, "expires": time.time() + 300, "result": result}
            await self._write(scope, key, stage)
            return {**result, "prepared_queue": self._prepared_queue(stage)}

    async def commit_wall(self, profile_token, ticket, *, queue_id="", expected_filters=None):
        if not isinstance(ticket, str) or not re.fullmatch(r"[0-9a-f]{48}", ticket):
            raise PersonalProfileError("预备片单无效。", status=409)
        scope = self._scope(profile_token)
        async with self._locks.setdefault(scope, asyncio.Lock()):
            filter_revision = await self._filter_revision(scope, expected_filters)
            profile = await self._profile(scope)
            stage_key = await self._stage_key(scope, queue_id)
            stage = await self._read(scope, stage_key)
            if not isinstance(stage, dict) or stage.get("basis") != self._wall_basis(profile) or stage.get("filters_revision", "") != filter_revision:
                raise PersonalProfileError("偏好或展示历史已变化，请重新选片。", status=409)
            receipt = stage.get("receipt", {})
            if receipt.get("ticket") == ticket and receipt.get("expires", 0) >= time.time():
                return {**receipt["result"], "prepared_queue": self._prepared_queue(stage)}
            queue = stage.get("queue", [])
            if not queue or queue[0].get("ticket") != ticket or queue[0].get("expires", 0) < time.time():
                raise PersonalProfileError("预备片单已失效，请重新选片。", status=409)
            row = queue[0]
            result = copy.deepcopy(row["result"])
            target = stage["options"][0]
            total = sum(len(entry["result"]["items"]) for entry in queue)
            if total < target and any(entry.get("partial") for entry in queue):
                raise PersonalProfileError("候选不足，请重新选片。", status=409)
            target = min(target, total)
            selected = []
            while queue and len(selected) < target:
                head = queue[0]
                take = min(target - len(selected), len(head["result"]["items"]))
                selected.extend(head["result"]["items"][:take])
                head["result"]["items"] = head["result"]["items"][take:]
                if not head["result"]["items"]:
                    queue.pop(0)
                else:
                    head["partial"] = True
            result["items"] = selected
            if result.get("items"):
                await self._record_wall(scope, profile, row["batch_key"], result)
            stage["receipt"] = {**row, "result": result}
            stage["basis"] = self._wall_basis(profile)
            await self._write(scope, stage_key, stage)
            return {**result, "prepared_queue": self._prepared_queue(stage)}

    async def _record_wall(self, scope, profile, batch_key, result):
        items = result["items"]
        await self._write(scope, batch_key, result)
        previous_batches = profile.get("batches", [])
        previous_success = profile.get("last_success_batch")
        profile["batches"] = (previous_batches + [batch_key])[-MAX_BATCHES:]
        profile["recent"] = (profile.get("recent", []) + [[item["id"] for item in items]])[-RECENT_BATCHES:]
        if result["status"] == "ready":
            profile["last_success_batch"] = batch_key
        await self._write(scope, "profile", profile)
        try:
            await self._history_wall(scope, batch_key, result)
        except PersonalProfileError:
            result["history_notice"] = "本面已展示，但浏览历史暂未保存。"
        cleanup = previous_batches[:-MAX_BATCHES + 1]
        if previous_success and previous_success != profile.get("last_success_batch"):
            cleanup.append(previous_success)
        for old in dict.fromkeys(cleanup):
            if old in profile["batches"] or old == profile.get("last_success_batch"):
                continue
            try:
                meta = await self._call("get", "m:" + old, scope=scope, default=None)
                if isinstance(meta, dict) and type(meta.get("chunks")) is int and 0 < meta["chunks"] <= 48:
                    for index in range(meta["chunks"]):
                        await self._call("delete", f"b:{old}:{meta.get('generation')}:{index}", scope=scope)
                await self._call("delete", "m:" + old, scope=scope)
            except Exception:
                pass  # Expired-batch cleanup cannot invalidate a committed current result.
