"""按制片地区抽样真实剧库；不读取个人记录，不限制首播年份。"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import inspect
import math
import random
import re
import time
from collections import OrderedDict
from datetime import datetime, timezone

TMDB_DISCOVER = "https://api.themoviedb.org/3/discover/tv"
MIN_RATING = 6.0
MIN_VOTES = 20
SCRIPTED_GENRES = (18, 35, 80, 9648, 10759, 10765, 10768, 10751)
EXCLUDED_GENRES = {16, 99, 10763, 10764, 10767}
EXCLUDED_NAMES = ("animation", "documentary", "reality", "talk", "news", "动画", "纪录", "真人秀", "脱口秀", "新闻")
CATEGORIES = (
    {"id": "korean", "name": "韩剧", "wordmark": "K-DRAMA", "color": "#ff91a6", "countries": ["KR"], "languages": ["ko"]},
    {"id": "chinese", "name": "华语剧", "wordmark": "华语剧", "color": "#ffc486", "countries": ["CN", "TW", "HK"], "languages": ["zh", "cn"]},
    {"id": "japanese", "name": "日剧", "wordmark": "J-DRAMA", "color": "#9db9ff", "countries": ["JP"], "languages": ["ja"]},
    {"id": "american", "name": "美剧", "wordmark": "US TV", "color": "#c7a4ff", "countries": ["US"], "languages": ["en"]},
    {"id": "british", "name": "英剧", "wordmark": "BRITISH TV", "color": "#a6d9bc", "countries": ["GB"], "languages": ["en"]},
    {"id": "thai", "name": "泰剧", "wordmark": "THAI DRAMA", "color": "#f0d984", "countries": ["TH"], "languages": ["th"]},
)
CATEGORY_BY_ID = {category["id"]: category for category in CATEGORIES}
HISTORY_BATCHES = 5
MAX_SESSIONS = 128
SESSION_TTL = 86400
MAX_POOL_ITEMS = 600
MAX_BATCHES = 64


def _number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def _identity(value):
    text = str(value or "")
    return int(text) if re.fullmatch(r"[1-9]\d*", text) else None


def _item(identity, title, *, original_title="", rating=None, year=None, poster=None, backdrop=None, overview="", metadata=None):
    return {"id": f"recommendation:tmdb:tv:{identity}", "title": str(title).strip()[:300],
            "original_title": str(original_title or "")[:300], "media_type": "tv", "year": year,
            "rating": rating, "rating_source": "TMDB", "poster_url": poster,
            "backdrop_url": backdrop, "overview": str(overview or "")[:6000],
            "detail_ref": f"tmdb:tv:{identity}", "source_url": f"https://www.themoviedb.org/tv/{identity}",
            "metadata": metadata or {}}


def _direct_item(row, country, languages):
    if not isinstance(row, dict):
        return None
    identity = _identity(row.get("id"))
    rating, votes = _number(row.get("vote_average")), _number(row.get("vote_count"))
    origins, raw_genres = row.get("origin_country"), row.get("genre_ids")
    if (not isinstance(origins, list) or not isinstance(raw_genres, list)
            or any(type(genre) is not int for genre in raw_genres)):
        return None
    genres = set(raw_genres)
    if (not identity or not isinstance(row.get("name"), str) or not row["name"].strip() or row.get("adult") is True
            or country not in origins
            or row.get("original_language") not in languages
            or genres.intersection(EXCLUDED_GENRES) or not genres.intersection(SCRIPTED_GENRES)
            or rating is None or rating < MIN_RATING or votes is None or votes < MIN_VOTES):
        return None
    release = str(row.get("first_air_date") or "")
    image = lambda path, size: "https://image.tmdb.org/t/p/" + size + path if isinstance(path, str) and re.fullmatch(r"/[A-Za-z0-9._-]+", path) else None
    return _item(identity, row["name"], original_title=row.get("original_name"), rating=rating,
                 year=int(release[:4]) if re.match(r"\d{4}", release) else None,
                 poster=image(row.get("poster_path"), "w500"), backdrop=image(row.get("backdrop_path"), "w1280"),
                 overview=row.get("overview"), metadata={"origin_country": list(origins), "selection_country": country,
                     "original_language": row["original_language"], "vote_count": int(votes)})


def _host_item(row, country):
    if not isinstance(row, dict) or row.get("media_type") != "tv" or row.get("provider") != "tmdb":
        return None
    reference = str(row.get("title_ref") or "")
    match = re.fullmatch(r"tmdb:tv:([1-9]\d*)", reference)
    rating = _number(row.get("provider_rating"))
    genres = [str(genre).casefold() for genre in row.get("genres") or []]
    if (not match or not row.get("title") or rating is None or rating < MIN_RATING
            or any(excluded in genre for genre in genres for excluded in EXCLUDED_NAMES)):
        return None
    # 使用字段白名单：宿主返回的 library_status 等管理员库存信息不能进入成员页面。
    return _item(int(match[1]), row["title"], original_title=row.get("original_title"), rating=rating,
                 year=row.get("release_year"), poster=row.get("poster_url"), backdrop=row.get("backdrop_url"),
                 overview=row.get("overview"), metadata={"selection_country": country, "original_language_verified": False})


class RecommendationSources:
    def __init__(self, discovery, ops_getter=None, storage=None):
        self.discovery, self.ops_getter, self.storage = discovery, ops_getter, storage
        self._cache, self._locks, self._closed = {}, {}, False
        self._sessions, self._refreshes = OrderedDict(), {}
        self.ttl = max(300, min(86400, int(getattr(discovery, "ttl", 3600))))

    async def categories(self):
        return {"categories": [{**copy.deepcopy(category), "description": "经典与新作 · 口碑剧库随机抽样"} for category in CATEGORIES],
                "default_category": "korean", "method": "地区剧库随机抽样"}

    async def close(self):
        self._closed = True
        tasks = list(self._refreshes.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._refreshes.clear()
        self._cache.clear()
        self._sessions.clear()
        self._locks.clear()

    async def _query(self, country, languages, backend, page, language=None, ops=None):
        if backend == "tmdb":
            params = {"language": "zh-CN", "include_adult": "false", "with_origin_country": country,
                      "with_original_language": language, "with_genres": "|".join(map(str, SCRIPTED_GENRES)),
                      "without_genres": ",".join(map(str, sorted(EXCLUDED_GENRES))),
                      "vote_average.gte": MIN_RATING, "vote_count.gte": MIN_VOTES, "sort_by": "vote_average.desc", "page": page}
            data = await self.discovery._json(TMDB_DISCOVER, params, tmdb=True)
            items = [_direct_item(row, country, languages) for row in data.get("results", [])]
        else:
            # 宿主的评分排序包含最低票数；只读取公开剧库操作并投影公开字段。
            data = await ops.call("discover.filter-titles", {"media_type": "tv", "genre_ids": [18],
                "origin_country": country, "rating_gte": MIN_RATING, "sort": "rating", "page": page})
            items = [_host_item(row, country) for row in data.get("titles", [])]
        total = _number(data.get("total_pages")) or 1
        return [item for item in items if item], max(1, min(500, int(total)))

    async def _initial_stream(self, country, category, backend, language, ops):
        first, total = await self._query(country, category["languages"], backend, 1, language, ops)
        stream = {"country": country, "language": language, "total": total, "pages": [1]}
        failed = False
        # 首屏仅追加一页；后续换批接近耗尽时再逐步扩充，而非一次遍历剧库。
        if total > 1:
            page = random.SystemRandom().randint(2, total)
            try:
                extra, total = await self._query(country, category["languages"], backend, page, language, ops)
                first.extend(extra)
                stream.update(total=total, pages=[1, page])
            except Exception:
                failed = True
        return first, stream, failed

    async def _ops(self, backend):
        ops = await self.ops_getter() if backend == "host" and self.ops_getter else None
        if backend == "host" and ops is None:
            raise ValueError("没有可用的宿主剧库")
        return ops

    async def _build(self, category, backend):
        ops = await self._ops(backend)
        variants = category["languages"] if backend == "tmdb" else [None]
        keys = [(country, language) for country in category["countries"] for language in variants]
        results = await asyncio.gather(*(self._initial_stream(country, category, backend, language, ops)
            for country, language in keys), return_exceptions=True)
        buckets = {country: {} for country in category["countries"]}
        streams, partial, successes = [], False, 0
        for (country, language), result in zip(keys, results):
            if isinstance(result, Exception):
                partial = True
                streams.append({"country": country, "language": language, "total": 1, "pages": []})
                continue
            items, stream, failed = result
            successes += 1
            streams.append(stream)
            partial = partial or failed
            for item in items:
                buckets[country].setdefault(item["id"], item)
        if not successes:
            raise ValueError("剧库暂时不可用")
        return {"buckets": {country: list(items.values()) for country, items in buckets.items()},
                "streams": streams, "backend": backend, "partial": partial, "status": "ready",
                "expires": time.monotonic() + self.ttl, "expires_wall": time.time() + self.ttl,
                "fetched_at": datetime.now(timezone.utc).isoformat()}

    async def _storage_call(self, method, *args):
        if self.storage is None:
            return None
        result = getattr(self.storage, method)(*args)
        return await result if inspect.isawaitable(result) else result

    async def _save_pool(self, category, pool):
        try:
            value = {key: copy.deepcopy(pool[key]) for key in
                     ("buckets", "streams", "backend", "partial", "expires_wall", "fetched_at")}
            for items in value["buckets"].values():
                for item in items:
                    item["overview"] = str(item.get("overview") or "")[:1000]
            await self._storage_call("set", "recommendations:v2:" + category["id"] + ":" + pool["backend"], value, self.ttl)
        except Exception:
            # 磁盘或缓存故障不妨碍展示已经验证的公开内容。
            pass

    async def _load_pool(self, category, backend):
        try:
            value = await self._storage_call("get", "recommendations:v2:" + category["id"] + ":" + backend)
            if not isinstance(value, dict) or value.get("backend") != backend:
                return None
            if not isinstance(value.get("buckets"), dict) or not isinstance(value.get("streams"), list):
                return None
            if set(value["buckets"]) != set(category["countries"]):
                return None
            remaining = float(value["expires_wall"]) - time.time()
            pool = copy.deepcopy(value)
            pool.update(status="ready" if remaining > 0 else "stale", expires=time.monotonic() + remaining)
            return pool
        except Exception:
            return None

    async def _new_pool(self, category, backend):
        try:
            pool = await self._build(category, backend)
        except Exception:
            if backend != "tmdb" or self.ops_getter is None:
                raise
            pool = await self._build(category, "host")
        self._cache[category["id"]] = pool
        await self._save_pool(category, pool)
        return pool

    def _schedule_refresh(self, category, backend):
        identity = category["id"]
        if self._closed or identity in self._refreshes:
            return
        async def refresh():
            try:
                async with self._locks.setdefault(identity, asyncio.Lock()):
                    await self._new_pool(category, backend)
            except Exception:
                cached = self._cache.get(identity)
                if cached:
                    cached.update(status="stale", expires=time.monotonic() + 120)
            finally:
                self._refreshes.pop(identity, None)
        self._refreshes[identity] = asyncio.create_task(refresh())

    async def _pool(self, category):
        backend = "tmdb" if getattr(self.discovery, "_token", "") else "host"
        cached = self._cache.get(category["id"])
        if cached is None:
            cached = await self._load_pool(category, backend)
            if cached is None and backend == "tmdb" and self.ops_getter is not None:
                cached = await self._load_pool(category, "host")
            if cached is not None:
                self._cache[category["id"]] = cached
        if cached and cached["expires"] > time.monotonic():
            return cached
        if cached and any(cached["buckets"].values()):
            cached["status"] = "stale"
            self._schedule_refresh(category, backend)
            return cached
        try:
            return await self._new_pool(category, backend)
        except Exception:
            return None

    def _session(self, session_id, category_id):
        now = time.monotonic()
        for key in list(self._sessions):
            if self._sessions[key]["last_seen"] + SESSION_TTL < now:
                del self._sessions[key]
        session = self._sessions.pop(session_id, {"last_seen": now, "categories": {}})
        session["last_seen"] = now
        self._sessions[session_id] = session
        while len(self._sessions) > MAX_SESSIONS:
            self._sessions.popitem(last=False)
        return session["categories"].setdefault(category_id, {"history": [], "batches": OrderedDict()})

    @staticmethod
    def _history_ids(session):
        return {identity for batch in session["history"] for identity in batch}

    async def _refill(self, pool, category, session, limit):
        if pool["status"] == "stale":
            return
        history = self._history_ids(session)
        fresh_counts = {country: len({item["id"] for item in items if item["id"] not in history})
                        for country, items in pool["buckets"].items()}
        target = math.ceil(limit / len(category["countries"]))
        depleted = {country for country, count in fresh_counts.items() if count < target}
        if not depleted:
            return
        try:
            ops = await self._ops(pool["backend"])
        except Exception:
            pool["partial"] = True
            return
        requests = []
        for country in depleted:
            # 每个缺额地区最多追加两页，避免缺少语言流导致一次发出过多请求。
            options = [(stream, page) for stream in pool["streams"] if stream["country"] == country
                       for page in range(1, stream["total"] + 1) if page not in stream["pages"]]
            requests.extend(random.SystemRandom().sample(options, min(2, len(options))))
        if not requests:
            return
        async def fetch(stream, page):
            items, total = await self._query(stream["country"], category["languages"], pool["backend"],
                                              page, stream["language"], ops)
            return stream, page, items, total
        results = await asyncio.gather(*(fetch(stream, page) for stream, page in requests), return_exceptions=True)
        for result in results:
            if isinstance(result, Exception):
                pool["partial"] = True
                continue
            stream, page, items, total = result
            stream["pages"].append(page)
            stream["total"] = total
            bucket = pool["buckets"][stream["country"]]
            known = {item["id"] for item in bucket}
            bucket.extend(item for item in items if item["id"] not in known)
        # 共享候选池和会话记录均有容量上限，不把连续浏览变成无限内存增长。
        per_country = max(1, MAX_POOL_ITEMS // len(category["countries"]))
        for country, items in pool["buckets"].items():
            pool["buckets"][country] = items[-per_country:]
        await self._save_pool(category, pool)

    def _select(self, pool, category, nonce, limit, session):
        key = (nonce, limit)
        if key in session["batches"]:
            return copy.deepcopy(session["batches"][key])
        seed = int.from_bytes(hashlib.sha256((category["id"] + ":" + nonce).encode()).digest(), "big")
        rng = random.Random(seed)
        countries = list(pool["buckets"])
        rng.shuffle(countries)
        # 所有地区的未看候选优先；不足时先释放最早一批，最后才释放上一批。
        ages = {identity: index + 1 for index, batch in enumerate(session["history"]) for identity in batch}
        queues = {country: {} for country in countries}
        for country in countries:
            for item in pool["buckets"][country]:
                queues[country].setdefault(ages.get(item["id"], 0), []).append(item)
            for group in queues[country].values():
                rng.shuffle(group)
        selected, seen = [], set()
        for age in range(len(session["history"]) + 1):
            while len(selected) < limit:
                progress = False
                for country in countries:
                    queue = queues[country].get(age, [])
                    while queue and queue[0]["id"] in seen:
                        queue.pop(0)
                    if queue and len(selected) < limit:
                        item = queue.pop(0)
                        selected.append(item)
                        seen.add(item["id"])
                        progress = True
                if not progress:
                    break
        repeated = bool(seen.intersection(ages))
        session["history"].append([item["id"] for item in selected])
        session["history"] = session["history"][-HISTORY_BATCHES:]
        session["batches"][key] = (copy.deepcopy(selected), repeated)
        if len(session["batches"]) > MAX_BATCHES:
            session["batches"].popitem(last=False)
        return copy.deepcopy(selected), repeated

    async def recommend(self, category_id, nonce="0", limit=12, session_id="default"):
        if self._closed:
            raise ValueError("推荐剧库已停止")
        if category_id not in CATEGORY_BY_ID:
            raise ValueError("推荐馆不存在")
        if type(limit) is not int or not 1 <= limit <= 24:
            raise ValueError("每批剧集数量需在 1 到 24 之间")
        nonce = str(nonce)
        if len(nonce) > 128:
            raise ValueError("换一批标识过长")
        if not isinstance(session_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", session_id):
            raise ValueError("浏览会话标识不合规")
        category = CATEGORY_BY_ID[category_id]
        async with self._locks.setdefault(category_id, asyncio.Lock()):
            pool = await self._pool(category)
            backend = pool["backend"] if pool else ("tmdb" if getattr(self.discovery, "_token", "") else "host")
            source = {"kind": "random_library", "label": "TMDB 口碑剧库 · 随机抽样" if backend == "tmdb" else "MovieClaw 剧库 · 按制片地区抽样",
                      "url": "https://www.themoviedb.org/tv", "attribution": "TMDB", "countries": list(category["countries"]),
                      "language_filtered": backend == "tmdb", "rating_gte": MIN_RATING,
                      "minimum_votes": MIN_VOTES if backend == "tmdb" else 50}
            if pool is None:
                return {"category_id": category_id, "nonce": nonce, "items": [], "source": source,
                        "status": "unavailable", "message": "暂时无法读取真实剧库，请检查 MovieClaw 的 TMDB 连接或插件网络设置后重试。"}
            session = self._session(session_id, category_id)
            if (nonce, limit) not in session["batches"]:
                await self._refill(pool, category, session, limit)
            items, repeated = self._select(pool, category, nonce, limit, session)
            message = "不限制首播年份，按地区与口碑条件随机抽样；本次浏览会话尽量避开最近五批。"
            if backend == "host":
                message += " 宿主只提供制片地区筛选，没有原始语言字段；华语馆合并中国大陆、台湾和香港的剧库。" if category_id == "chinese" else " 宿主按制片地区筛选，未提供原始语言字段。"
            if pool["partial"]:
                message += " 部分地区或分页暂不可用，当前展示已成功获取的候选。"
            if pool["status"] == "stale":
                message += " 当前先展示上次成功的候选缓存；后续会重试刷新。"
            if repeated:
                message += " 当前可用候选不足，已优先选择未看剧集，其余从较早批次循环补足。"
            if not items:
                message += " 当前筛选暂无符合条件的剧集。"
            return {"category_id": category_id, "nonce": nonce, "items": items, "source": source,
                    "status": pool["status"], "message": message, "fetched_at": pool["fetched_at"],
                    "selection": {"history_batches": HISTORY_BATCHES, "repeated": repeated}}
