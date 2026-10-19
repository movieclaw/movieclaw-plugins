"""Strict parsers for public first-party Mango TV and Bilibili discovery data.

Verified 2026-10-18 against the scripts loaded by the respective official sites:
https://static.hitv.com/pc2/pcuicore.ae42b29.js (channel/index and vrsList)
https://s1.hdslb.com/bfs/static/2233-monorepo/popular/static/js/index.55b42adc.js
Mango channel modules are editorial selections, never numeric popularity ranks.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlsplit


BILIBILI_RANK_API = "https://api.bilibili.com/pgc/season/rank/web/list"
BILIBILI_TYPES = {"movie": 2, "tv": 5, "variety": 7}
KIND_NAMES = {"movie": "电影", "tv": "电视剧", "variety": "综艺"}
MANGO_CHANNELS = {"tv": "4", "variety": "6", "movie": "9"}
MANGO_MODULES = {"tv": "热播剧集", "variety": "王牌节目", "movie": "全网热播 · 新片速递"}
# Anonymous public PC-web path: no user identifier, ticket or login cookie.
MANGO_CHANNEL_BASE = "https://dc.bz.mgtv.com/dynamic/v1/channel/index/0/9.0.9/10/10000000/space/0/4/"
MANGO_PAGES = {"tv": "https://www.mgtv.com/tv/", "variety": "https://www.mgtv.com/show/", "movie": "https://www.mgtv.com/movie/"}


class DomesticSourceError(ValueError):
    """Safe, deliberately authored messages suitable for the plugin page."""


def _today():
    return datetime.now(timezone(timedelta(hours=8))).date()


def _positive_int(value):
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return number if number > 0 and str(value).strip() == str(number) else None


def _rating(value):
    if isinstance(value, dict):
        value = value.get("score")
    match = re.fullmatch(r"\s*(\d{1,2}(?:\.\d+)?)\s*分?\s*", str(value or ""))
    if match and 0 <= float(match.group(1)) <= 10:
        return float(match.group(1))
    return None


def parse_bilibili_rank(payload, kind, *, today=None):
    """Require the exact licensed-film/TV/variety category and a current period."""
    if kind not in BILIBILI_TYPES or not isinstance(payload, dict) or payload.get("code") != 0:
        raise DomesticSourceError("哔哩哔哩公开影视榜暂时不可用")
    data = payload.get("data") or payload.get("result") or {}
    if not isinstance(data, dict):
        raise DomesticSourceError("哔哩哔哩影视榜数据格式已变化")
    note = str(data.get("note") or "")
    if KIND_NAMES[kind] not in note or (data.get("season_type") is not None and str(data["season_type"]) != str(BILIBILI_TYPES[kind])):
        raise DomesticSourceError("哔哩哔哩榜源分类不符，暂不展示为影视榜")
    matches = re.findall(r"(\d{4})年\s*(\d{1,2})月\s*(\d{1,2})日", note)
    try:
        dates = [date(*map(int, parts)) for parts in matches]
    except ValueError:
        raise DomesticSourceError("哔哩哔哩榜源日期异常，暂不展示为当前热播。") from None
    if len(dates) != 2:
        raise DomesticSourceError("哔哩哔哩榜源未提供可验证日期，暂不标为热播榜")
    start, latest = dates
    today = today or _today()
    if start > latest or (latest - start).days > 14 or latest > today + timedelta(days=1):
        raise DomesticSourceError("哔哩哔哩榜源日期异常，暂不展示为当前热播。")
    if latest < today - timedelta(days=14):
        raise DomesticSourceError("哔哩哔哩榜源返回历史数据（" + latest.isoformat() + "），暂不展示为当前热播。")
    items, seen = [], set()
    rows = data.get("list")
    if not isinstance(rows, list):
        rows = []
    for row in rows[:200]:
        if not isinstance(row, dict):
            continue
        season_id, rank = _positive_int(row.get("season_id")), _positive_int(row.get("rank"))
        title = str(row.get("title") or "").strip()
        if not title or not season_id or not rank or season_id in seen:
            continue
        seen.add(season_id)
        episode = row.get("new_ep") if isinstance(row.get("new_ep"), dict) else {}
        items.append({"title": title, "native_id": str(season_id), "rank": rank,
                      "poster_url": row.get("cover"), "backdrop_url": row.get("ss_horizontal_cover"),
                      "source_url": f"https://www.bilibili.com/bangumi/play/ss{season_id}",
                      "rating": _rating(row.get("rating")), "rating_source": "Bilibili",
                      "subtitle": episode.get("index_show") or row.get("desc") or "",
                      "metadata": {"rank_scope": KIND_NAMES[kind] + "榜", "period": note}})
    if not items:
        raise DomesticSourceError("哔哩哔哩公开影视榜没有可验证的影视条目")
    return items, note, latest.isoformat()


def parse_mango_channel(payload, kind, *, today=None):
    """Read only the verified editorial module; reject ads, playlists and extras."""
    if kind not in MANGO_CHANNELS or not isinstance(payload, dict) or payload.get("code") != 200:
        raise DomesticSourceError("芒果 TV 官方频道暂时不可用")
    channel = payload.get("channel") or {}
    if not isinstance(channel, dict) or str(channel.get("vclassId")) != MANGO_CHANNELS[kind] or channel.get("title") != KIND_NAMES[kind]:
        raise DomesticSourceError("芒果 TV 频道分类不符，暂不展示推荐")
    # A response timestamp, when actually supplied, is distinct from each card's
    # content-update timestamp. Never pretend an old film was recently released.
    updated = None
    stamp = payload.get("serverTime") or payload.get("timestamp")
    if stamp:
        try:
            timestamp = float(stamp)
            if timestamp > 10_000_000_000:
                timestamp /= 1000
            moment = datetime.fromtimestamp(timestamp, timezone.utc)
        except (ValueError, TypeError, OverflowError, OSError):
            raise DomesticSourceError("芒果 TV 源响应日期异常，暂不展示推荐") from None
        current_day = today or _today()
        if not current_day - timedelta(days=14) <= moment.date() <= current_day + timedelta(days=1):
            raise DomesticSourceError("芒果 TV 源响应已过期或日期异常，暂不展示推荐")
        # serverTime dates the response, not the editorial list's generation.
        # Keep updated_at absent rather than relabeling it as a list update.
    modules = payload.get("data") if isinstance(payload.get("data"), list) else []
    module = next((m for m in modules if isinstance(m, dict) and m.get("moduleTitle") == MANGO_MODULES[kind]), None)
    if module is None:
        raise DomesticSourceError("芒果 TV 官方频道未返回已验证的推荐栏目")
    items, seen = [], set()
    components = module.get("DSLList") if isinstance(module.get("DSLList"), list) else []
    for component in components:
        if not isinstance(component, dict) or not isinstance(component.get("data"), dict):
            continue
        rows = component["data"].get("items")
        for row in rows[:100] if isinstance(rows, list) else []:
            if not isinstance(row, dict) or str(row.get("jumpKind")) not in {"1", "12"}:
                continue
            title, native_id = str(row.get("name") or "").strip(), _positive_int(row.get("jumpId"))
            try:
                direct = urlsplit(str(row.get("videoUrl") or ""))
                link_ok = direct.scheme in {"https", "http"} and direct.hostname == "www.mgtv.com" and not direct.username and not direct.password and bool(re.match(r"^/b/" + str(native_id) + r"(?:/|\.html$)", direct.path))
            except ValueError:
                link_ok = False
            if not title or not native_id or native_id in seen or not link_ok:
                continue
            # Movie promotional snippets share this module with full films.
            try:
                duration = float(row.get("durationTopic") or 0)
            except (ValueError, TypeError):
                duration = 0
            if kind == "movie" and (0 < duration < 600 or any(s in title for s in ("番外", "预告", "花絮", "特辑"))):
                continue
            seen.add(native_id)
            portrait, landscape = row.get("imageV"), row.get("imageH")
            items.append({"title": title, "native_id": str(native_id), "poster_url": portrait or landscape,
                          "backdrop_url": landscape, "source_url": "https://www.mgtv.com" + direct.path,
                          "subtitle": str(row.get("updateInfo") or "").strip(), "overview": str(row.get("subName") or "").strip(),
                          "metadata": {"selection_scope": MANGO_MODULES[kind], "selection_order": len(items) + 1,
                                       "poster_aspect": "portrait" if portrait else "landscape"}})
    if not items:
        raise DomesticSourceError("芒果 TV 官方推荐栏目没有可用的正片条目")
    return items, MANGO_MODULES[kind], updated
