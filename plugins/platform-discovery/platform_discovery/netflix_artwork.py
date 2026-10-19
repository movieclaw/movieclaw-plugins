"""Exact Netflix artwork and conservative TMDB identity matching."""
from __future__ import annotations

import asyncio
import copy
import re
import time
import unicodedata
from datetime import date, datetime, timedelta
from urllib.parse import urlsplit

import httpx


OFFICIAL_PAGES = {
    "Films (English)": "https://www.netflix.com/tudum/top10/films",
    "Films (Non-English)": "https://www.netflix.com/tudum/top10/films-non-english",
    "TV (English)": "https://www.netflix.com/tudum/top10/tv",
    "TV (Non-English)": "https://www.netflix.com/tudum/top10/tv-non-english",
}
OFFICIAL_LABELS = {
    "Films (English)": "Movies | English", "Films (Non-English)": "Movies | Non-English",
    "TV (English)": "Shows | English", "TV (Non-English)": "Shows | Non-English",
}
ARTWORK_MESSAGES = {
    "ready": "官方作品图已与榜单统计周对齐。",
    "cache_hit": "使用已核对统计周的官方作品图缓存。",
    "stale_cache": "官方图页暂不可用，保留已核对统计周的作品图。",
    "week_mismatch": "官方图页的统计周与榜单不同，未套用其他周的图片。",
    "parse_error": "官方图页格式暂未识别，未套用未经核对的图片。",
    "request_error": "官方图页暂时无法读取，请稍后重试。",
    "timeout": "官方图页读取超时，请稍后重试。",
    "upstream_denied": "官方图页拒绝了本次请求，未取得可核对的图片。",
    "upstream_limited": "官方图页暂时限制请求频率，请稍后重试。",
    "upstream_error": "官方图页服务暂时异常，请稍后重试。",
    "invalid_week": "榜单统计周无效，未发起图片请求。",
    "unsupported_category": "该榜单分类尚无已验证的官方图片来源。",
    "missing_title": "同周官方图页未找到此作品及季度，保留其他可靠图片。",
}


def valid_week(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return None
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        return None


def normalized(value):
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return "".join(c for c in text if c.isalnum())


def official_title_key(value):
    text = str(value or "").strip()
    # Tudum labels WWE episodes as "Raw: 2026 - September 28, 2026"
    # while its TSV says "Raw: September 28, 2026". Remove only this
    # duplicated year; keep the exact episode date in the identity.
    match = re.fullmatch(r"(.+?):\s*((?:19|20)\d{2})\s*[-–—]\s*([A-Za-z]+\s+\d{1,2},\s*((?:19|20)\d{2}))", text)
    if match and match.group(2) == match.group(4):
        text = match.group(1) + ": " + match.group(3)
    return normalized(text)


def base_title(value):
    text = str(value or "").strip()
    text = re.sub(r"\s*\((?:19|20)\d{2}\)\s*$", "", text)
    shortened = re.sub(r"(?:\s*[:\-–—]\s*|\s+)(?:season\s+\d+|limited\s+series|mini\s*series|第\s*\d+\s*季)\s*$", "", text, flags=re.I)
    return shortened.strip() or text


def image_url(value):
    value = str(value or "").strip().strip("\"'")
    try:
        parts = urlsplit(value)
        host = parts.hostname or ""
        if (parts.scheme == "https" and not parts.username and not parts.password
                and any(host == domain or host.endswith("." + domain) for domain in ("nflximg.net", "nflxso.net"))
                and not parts.path.lower().endswith(".svg")):
            return value
    except ValueError:
        pass
    return None


def parse_official_artwork(root):
    """Read rendered title cards, not logos or executable page scripts."""
    nodes = list(root.nodes())
    selected = next((n for n in nodes if n.attrs.get("data-uia") == "top10-filters-week-select-option"
                     and n.attrs.get("aria-selected") == "true"), None)
    if selected is None:
        raise ValueError("Official artwork has no verifiable week")
    end = selected.text().strip().split(" - ")[-1].strip()
    try:
        week = datetime.strptime(end, "%m/%d/%y").date().isoformat()
    except ValueError:
        week = datetime.strptime(end, "%m/%d/%Y").date().isoformat()
    records = {}
    for card in nodes:
        if card.attrs.get("data-uia") != "top10-card":
            continue
        children = list(card.nodes())
        logo = next((n for n in children if n.attrs.get("data-uia") == "top10-card-logo"), None)
        title_img = next((n for n in logo.nodes() if n.tag == "img" and n.attrs.get("alt")), None) if logo else None
        if title_img is None:
            continue
        title = title_img.attrs["alt"]
        background = re.search(r"background-image\s*:\s*url\(([^)]+)\)", card.attrs.get("style", ""), re.I)
        artwork = image_url(background.group(1)) if background else None
        watch = next((n for n in children if n.attrs.get("data-uia") == "play-on-netflix-link"), None)
        link = watch.attrs.get("href", "") if watch else ""
        parts = urlsplit(link)
        watch_id = re.fullmatch(r"/(?:watch|title)/(\d+)", parts.path)
        source_url = "https://www.netflix.com" + parts.path if parts.hostname == "www.netflix.com" and watch_id else None
        if artwork:
            records[official_title_key(title)] = {"title": title, "poster_url": artwork, "backdrop_url": artwork,
                                           "poster_fallback_urls": [], "poster_source": "Netflix Tudum", "poster_aspect": "landscape",
                                           "netflix_id": watch_id.group(1) if source_url else None, "source_url": source_url}
    for cell in nodes:
        if cell.attrs.get("data-uia") != "top10-table-row-title":
            continue
        children = list(cell.nodes())
        button = next((n for n in children if n.tag == "button"), None)
        image = next((n for n in children if n.tag == "img"), None)
        thumbnail = image_url(image.attrs.get("src")) if image else None
        if button is None or not thumbnail:
            continue
        title = button.text().strip()
        key = official_title_key(title)
        record = records.setdefault(key, {"title": title, "poster_url": thumbnail, "backdrop_url": thumbnail,
                                          "poster_fallback_urls": [], "poster_source": "Netflix Tudum", "poster_aspect": "landscape"})
        if thumbnail != record["poster_url"] and thumbnail not in record["poster_fallback_urls"]:
            record["poster_fallback_urls"].append(thumbnail)
    return week, records


def parse_official_board(root, category):
    """Validate the anonymous SSR global weekly table, including metric units.

    Read only rendered controls and table cells. Embedded application state and
    abbreviated card metrics are deliberately not ranking data sources.
    """
    nodes = list(root.nodes())
    def selected(uia):
        matches = [n.text().strip() for n in nodes if n.attrs.get("data-uia") == uia
                   and n.attrs.get("aria-selected") == "true"]
        if len(matches) != 1:
            raise ValueError("Unverified selected control")
        return matches[0]
    if category not in OFFICIAL_LABELS or selected("top10-country-select-option") != "Global":
        raise ValueError("Unverified global scope")
    if selected("top10-category-select-option") != OFFICIAL_LABELS[category]:
        raise ValueError("Unverified category")
    period = selected("top10-filters-week-select-option")
    match = re.fullmatch(r"(\d{1,2}/\d{1,2}/(?:\d{4}|\d{2}))\s+-\s+(\d{1,2}/\d{1,2}/(?:\d{4}|\d{2}))", period)
    if not match:
        raise ValueError("Unverified weekly range")
    def parse_date(value):
        return datetime.strptime(value, "%m/%d/%Y" if len(value.split("/")[-1]) == 4 else "%m/%d/%y").date()
    start, end = map(parse_date, match.groups())
    if start.weekday() != 0 or end.weekday() != 6 or end - start != timedelta(days=6):
        raise ValueError("Unverified weekly interval")
    tables = [n for n in nodes if n.attrs.get("data-uia") == "top10-table"]
    if len(tables) != 1:
        raise ValueError("Unverified ranking table")
    table_nodes = list(tables[0].nodes())
    headers = {n.attrs.get("data-uia"): n.text().strip() for n in table_nodes if n.tag == "th"}
    if any(headers.get("top10-table-header-" + key) != label for key, label in
           (("views", "Views"), ("runtime", "Runtime"), ("hours", "Hours Viewed"))):
        raise ValueError("Unverified metric units")
    def integer(value):
        if not re.fullmatch(r"(?:\d{1,3}(?:,\d{3})+|\d+)", value):
            raise ValueError("Unverified integer metric")
        return int(value.replace(",", ""))
    rows = []
    for node in table_nodes:
        if node.tag != "tr":
            continue
        cells = {n.attrs.get("data-uia"): n for n in node.nodes() if n.tag == "td"}
        title_cell = cells.get("top10-table-row-title")
        if title_cell is None:
            continue
        title_nodes = list(title_cell.nodes())
        title = next((n.text().strip() for n in title_nodes if n.tag == "button"), "")
        rank = next((n.text().strip() for n in title_nodes if "rank" in n.attrs.get("class", "").split()), "")
        if not title or len(title) > 300 or not re.fullmatch(r"0?[1-9]|10", rank):
            raise ValueError("Unverified title or rank")
        def cell(key):
            value = cells.get("top10-table-row-" + key)
            if value is None:
                raise ValueError("Missing metric")
            return value.text().strip()
        runtime = cell("runtime")
        if not re.fullmatch(r"\d{1,3}:[0-5]\d", runtime):
            raise ValueError("Unverified hours and minutes")
        hours, minutes = map(int, runtime.split(":"))
        runtime_minutes = hours * 60 + minutes
        views, viewed_hours, weeks = integer(cell("views")), integer(cell("hours")), integer(cell("weeks"))
        if not runtime_minutes or not views or not viewed_hours or not weeks:
            raise ValueError("Empty ranking metric")
        rows.append({"category": category, "weekly_rank": int(rank), "show_title": base_title(title) if category.startswith("TV") else title,
                     "season_title": title if category.startswith("TV") else "N/A", "weekly_views": views,
                     "weekly_hours_viewed": viewed_hours, "runtime": runtime_minutes / 60,
                     "runtime_display": runtime, "runtime_minutes": runtime_minutes, "weeks_in_top_10": weeks,
                     "week": end.isoformat()})
    if len(rows) != 10 or sorted(row["weekly_rank"] for row in rows) != list(range(1, 11)):
        raise ValueError("Incomplete ranking")
    if len({official_title_key(row["season_title"] if row["season_title"] != "N/A" else row["show_title"]) for row in rows}) != 10:
        raise ValueError("Duplicate ranking identity")
    artwork_week, records = parse_official_artwork(root)
    if artwork_week != end.isoformat():
        raise ValueError("Artwork week mismatch")
    return {"week": end.isoformat(), "week_start": start.isoformat(), "rows": rows, "artwork": records}


def _expected_year(item):
    # A season's release year is not the series' first-air year (notably WWE).
    hint = item.get("year") or item.get("metadata", {}).get("release_year")
    if not hint:
        match = re.search(r"\((19\d{2}|20\d{2})\)\s*$", str(item.get("title", "")))
        hint = match.group(1) if match else None
    try:
        year = int(hint)
        return year if 1850 <= year <= 2100 else None
    except (TypeError, ValueError):
        return None


class NetflixArtwork:
    def __init__(self, fetch, json_fetch, make_item, parse_html, cache, locks, ttl, timeout, tmdb_base):
        self.fetch, self.json_fetch, self.make_item, self.parse_html = fetch, json_fetch, make_item, parse_html
        self.cache, self.locks = cache, locks
        self.ttl, self.timeout, self.tmdb_base = ttl, timeout, tmdb_base
        self.last_diagnostics = {}

    def _state(self, category, week, state, official_week=None, cause=None):
        result = {"state": state, "category": category if category in OFFICIAL_PAGES else "unknown",
                  "expected_week": valid_week(week), "official_week": valid_week(official_week),
                  "message": ARTWORK_MESSAGES[state]}
        if cause in ARTWORK_MESSAGES:
            result["cause"] = cause
        return result

    def _cached_state(self, category, week):
        cached = self.cache.get(("netflix-artwork-state", category, week))
        if cached and isinstance(cached[1], dict):
            return copy.deepcopy(cached[1])
        return self._state(category, week, "cache_hit", official_week=week)

    def _save_state(self, category, week, state, expiry):
        # The shared dict may be a persistent cache. Store public, fixed-schema
        # diagnostics only; exception strings, URLs and headers never enter it.
        self.cache[("netflix-artwork-state", category, week)] = (expiry, copy.deepcopy(state))

    def prime(self, category, week, records):
        """Reuse artwork from a fully validated ranking download, without I/O."""
        if category not in OFFICIAL_PAGES or not valid_week(week) or not isinstance(records, dict):
            return
        expiry = time.monotonic() + self.ttl
        self.cache[("netflix-artwork", category, week)] = (expiry, copy.deepcopy(records))
        self._save_state(category, week, self._state(category, week, "ready", week), expiry)

    async def _official(self, category, week):
        if category not in OFFICIAL_PAGES:
            return {}
        if not valid_week(week):
            return {}
        key = ("netflix-artwork", category, week)
        cached = self.cache.get(key)
        if cached and cached[0] > time.monotonic():
            return copy.deepcopy(cached[1])
        async with self.locks.setdefault(key, asyncio.Lock()):
            cached = self.cache.get(key)
            if cached and cached[0] > time.monotonic():
                return copy.deepcopy(cached[1])
            page_week, cause = None, None
            try:
                # Official default pages advance before the downloadable TSV.
                # The public week parameter was verified on all four fixed
                # routes; no default/current chart is used to label old ranks.
                document = await self.fetch(OFFICIAL_PAGES[category], params={"week": week})
            except httpx.HTTPStatusError as exc:
                code = exc.response.status_code
                cause = "upstream_denied" if code in {401, 403} else "upstream_limited" if code == 429 else "upstream_error"
            except (httpx.TimeoutException, TimeoutError):
                cause = "timeout"
            except Exception:
                cause = "request_error"
            else:
                try:
                    page_week, records = parse_official_artwork(self.parse_html(document))
                except Exception:
                    cause = "parse_error"
                else:
                    if page_week != week:
                        cause = "week_mismatch"
                    elif not records:
                        cause = "parse_error"
                    else:
                        expiry = time.monotonic() + self.ttl
                        self.cache[key] = (expiry, copy.deepcopy(records))
                        self._save_state(category, week, self._state(category, week, "ready", page_week), expiry)
                        return records
            # Both success and negative results use the shared dict, allowing
            # safe restoration after restart and a short retry backoff.
            records = copy.deepcopy(cached[1]) if cached and cached[1] else {}
            expiry = time.monotonic() + 120
            state = self._state(category, week, "stale_cache" if records else cause, page_week, cause if records else None)
            self.cache[key] = (expiry, records)
            self._save_state(category, week, state, expiry)
            return records

    def _apply(self, item, artwork, tmdb=False):
        backup = list(item.get("poster_fallback_urls") or [])
        metadata = item.setdefault("metadata", {})
        preserve_tmdb = (not tmdb and metadata.get("poster_source") == "TMDB" and item.get("poster_url")
                         and re.fullmatch(r"tmdb:(?:tv|movie):[1-9]\d*", str(item.get("detail_ref") or "")))
        if preserve_tmdb:
            backup.insert(0, artwork.get("poster_url"))
        elif item.get("poster_url") and item["poster_url"] != artwork.get("poster_url"):
            backup.insert(0, item["poster_url"])
        backup.extend(artwork.get("poster_fallback_urls") or [])
        for key in ("poster_url", "backdrop_url", "detail_ref", "rating", "rating_source", "year", "overview"):
            if preserve_tmdb and (key == "poster_url" or key == "backdrop_url" and item.get("backdrop_url")):
                continue
            if artwork.get(key) is not None:
                item[key] = artwork[key]
        item["poster_fallback_urls"] = list(dict.fromkeys(v for v in backup if v and v != item.get("poster_url")))[:4]
        if not preserve_tmdb:
            metadata["poster_source"] = "TMDB" if tmdb else artwork.get("poster_source", "Netflix Tudum")
            metadata["poster_aspect"] = "portrait" if tmdb else artwork.get("poster_aspect", "landscape")
        if artwork.get("source_url") and not tmdb:
            item["source_url"] = artwork["source_url"]
        if artwork.get("netflix_id"):
            metadata["netflix_id"] = artwork["netflix_id"]

    async def official_items(self, items, week):
        categories = list(dict.fromkeys(i.get("metadata", {}).get("rank_scope") for i in items))
        records = dict(zip(categories, await asyncio.gather(*(self._official(c, week) for c in categories))))
        matched, states = 0, []
        for category in categories:
            state = (self._state(category, week, "unsupported_category") if category not in OFFICIAL_PAGES
                     else self._state(category, week, "invalid_week") if not valid_week(week)
                     else self._cached_state(category, week))
            states.append(state)
        for item in items:
            meta = item.get("metadata", {})
            full_title = meta.get("season_title")
            if not full_title or full_title == "N/A":
                full_title = item["title"]
            artwork = records.get(meta.get("rank_scope"), {}).get(official_title_key(full_title))
            status = next(s for s, category in zip(states, categories) if category == meta.get("rank_scope"))
            diagnostic = copy.deepcopy(status)
            if artwork:
                self._apply(item, artwork)
                matched += 1
                diagnostic["matched"] = True
                diagnostic["image_week"] = valid_week(week)
            else:
                diagnostic["matched"] = False
                if status["state"] in {"ready", "cache_hit"}:
                    diagnostic.update(state="missing_title", message=ARTWORK_MESSAGES["missing_title"])
            item.setdefault("metadata", {})["netflix_artwork"] = diagnostic
        missing = len(items) - matched
        known_weeks = {state["official_week"] for state in states if state.get("official_week")}
        state = "ready" if matched == len(items) and all(s["state"] in {"ready", "cache_hit"} for s in states) else "partial" if matched else "unavailable"
        summary = {"state": state, "expected_week": valid_week(week),
                   "official_week": next(iter(known_weeks)) if len(known_weeks) == 1 else None,
                   "image_week": valid_week(week) if matched else None,
                   "matched": matched, "missing": missing,
                   "message": "同周官方作品图已核对。" if state == "ready" else "部分官方作品图暂不可用，已保留其他可靠图片与可用缓存。" if matched else "官方作品图暂不可用；榜单数据保留原统计周，不使用其他周的图片。",
                   "sources": states}
        self.last_diagnostics = copy.deepcopy(summary)
        return summary

    async def _match(self, item):
        media_type = item["media_type"]
        query = base_title(item["title"])
        wanted = {normalized(query), normalized(item["title"])} - {""}
        expected_year = _expected_year(item)
        responses = await asyncio.gather(*(self.json_fetch(self.tmdb_base + "/search/" + media_type,
            {"query": query, "language": language, "include_adult": "false"}, tmdb=True) for language in ("zh-CN", "en-US")), return_exceptions=True)
        candidates = {}
        for response in responses:
            if not isinstance(response, dict):
                continue
            for row in response.get("results", []):
                try:
                    identity = int(row.get("id") or 0)
                except (ValueError, TypeError):
                    continue
                if identity <= 0:
                    continue
                release = row.get("release_date") or row.get("first_air_date") or ""
                if expected_year and (not str(release).startswith(str(expected_year) + "-")):
                    continue
                entry = candidates.setdefault(identity, {"row": copy.deepcopy(row), "names": set()})
                entry["names"].update(normalized(row.get(k)) for k in ("title", "name", "original_title", "original_name"))
                for k in ("poster_path", "backdrop_path", "overview", "release_date", "first_air_date"):
                    if not entry["row"].get(k) and row.get(k):
                        entry["row"][k] = row[k]
        exact = [entry for entry in candidates.values() if wanted.intersection(entry["names"])]
        if not exact and 0 < len(candidates) <= 5:
            async def aliases(identity, entry):
                try:
                    result = await self.json_fetch(f"{self.tmdb_base}/{media_type}/{identity}/alternative_titles", tmdb=True)
                    names = {normalized(r.get("title")) for r in result.get("titles", result.get("results", []))}
                    return entry if wanted.intersection(names) else None
                except Exception:
                    return None
            exact = [entry for entry in await asyncio.gather(*(aliases(identity, entry) for identity, entry in candidates.items())) if entry]
        # Keep all same-name IDs in the ambiguity check, including records with
        # no image. Missing artwork is not evidence that a remake is wrong.
        if len(exact) != 1 or not exact[0]["row"].get("poster_path"):
            return None
        return self.make_item("netflix", exact[0]["row"], media_type)

    async def enrich(self, item):
        metadata = item.get("metadata", {})
        identity = metadata.get("netflix_id") or normalized(item.get("title"))
        key = ("netflix-match", item["media_type"], f"{identity}:{_expected_year(item) or ''}")
        cached = self.cache.get(key)
        if cached and cached[0] > time.monotonic():
            self._apply(item, cached[1], tmdb=True)
            return
        async with self.locks.setdefault(key, asyncio.Lock()):
            cached = self.cache.get(key)
            if cached and cached[0] > time.monotonic():
                self._apply(item, cached[1], tmdb=True)
                return
            try:
                # Includes time spent waiting for the shared request semaphore.
                async with asyncio.timeout(self.timeout):
                    found = await self._match(item)
                if found:
                    self.cache[key] = (time.monotonic() + 86400, copy.deepcopy(found))
                    self._apply(item, found, tmdb=True)
                elif cached:
                    self._apply(item, cached[1], tmdb=True)
            except Exception:
                if cached:
                    self._apply(item, cached[1], tmdb=True)
