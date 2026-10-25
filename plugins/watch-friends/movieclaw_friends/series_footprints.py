"""只聚合本人足迹的展示；不改原始单集、统计、已看标记或共享数据。"""

import asyncio
import json
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException, Query, Request
from pydantic import BaseModel, Field

from .peers import PeerError
from .series_platforms import read_catalog


def regular_episode(record):
    media = record["media"]
    season, episode = media.get("season_number"), media.get("episode_number")
    if (
        media.get("kind") == "tv"
        and type(season) is int
        and season > 0
        and type(episode) is int
        and episode > 0
    ):
        return season, episode
    return None


class SeriesFootprints:
    def __init__(self, store, archive):
        self.store, self.archive = store, archive
        store.execute(
            "CREATE TABLE IF NOT EXISTS series_footprint_catalog ("
            "user_id TEXT NOT NULL REFERENCES users(id),tmdb_id INTEGER NOT NULL,"
            "status TEXT NOT NULL,seasons TEXT NOT NULL,checked_at TEXT NOT NULL,"
            "PRIMARY KEY(user_id,tmdb_id))"
        )
        columns = {row["name"] for row in store.all("PRAGMA table_info(series_footprint_catalog)")}
        if "catalog_version" not in columns:
            store.execute(
                "ALTER TABLE series_footprint_catalog ADD COLUMN catalog_version INTEGER DEFAULT 0"
            )
        store.execute(
            "CREATE TABLE IF NOT EXISTS series_platform_catalog ("
            "user_id TEXT NOT NULL,tmdb_id INTEGER NOT NULL,source_id TEXT NOT NULL,"
            "episodes TEXT NOT NULL,checked_at TEXT NOT NULL,"
            "PRIMARY KEY(user_id,tmdb_id,source_id))"
        )

    def platform_catalogs(self, owner):
        """只使用仍然有效的本人连接；断开、重新绑定或过期后不沿用旧平台目录。"""
        result = {}
        for row in self.store.all(
            "SELECT c.* FROM series_platform_catalog c LEFT JOIN watch_sources s "
            "ON s.id=c.source_id WHERE c.user_id=? AND "
            "(c.source_id='movieclaw' OR (s.user_id=? AND s.active=1))",
            (owner, owner),
        ):
            if datetime.fromisoformat(row["checked_at"]) >= datetime.now(UTC) - timedelta(days=1):
                units = {tuple(unit) for unit in json.loads(row["episodes"])}
                if units:
                    result.setdefault(row["tmdb_id"], []).append(units)
        return result

    def grouped(self, owner):
        records = self.archive.records(owner)
        catalogs = {
            row["tmdb_id"]: row
            for row in self.store.all(
                "SELECT * FROM series_footprint_catalog WHERE user_id=?", (owner,)
            )
        }
        shows, pending = {}, {}
        for row in records:
            number = row["media"].get("tmdb_id")
            if type(number) is int and number > 0 and regular_episode(row):
                shows.setdefault(number, []).append(row)
            elif not row["matched"] and row.get("pending_series_key") and regular_episode(row):
                # 未匹配只按同一连接、原片名与年份折叠，不能跨来源认定作品或推测完结。
                pending.setdefault(row["pending_series_key"], []).append(row)
        complete, platform_catalogs = {}, self.platform_catalogs(owner)
        for number, rows in shows.items():
            watched = {regular_episode(row) for row in rows}
            local = platform_catalogs.get(number, [])
            # 某个平台的完整片库目录，或多个平台目录的并集，都可用跨平台已看事实核对。
            # 不能用「已看最大集号」推测总集数，也不将仅看完片库内容标成全剧完结。
            if local and any(units == watched for units in [*local, set().union(*local)]):
                complete[number] = (
                    rows,
                    len({s for s, _ in watched}),
                    len(watched),
                    "platform",
                )
            catalog = catalogs.get(number)
            if not catalog:
                continue
            # 目录有时效；刷新失败时保留进度合并，不以旧目录作完结结论。
            if datetime.fromisoformat(catalog["checked_at"]) < datetime.now(UTC) - timedelta(
                days=1
            ):
                continue
            seasons = json.loads(catalog["seasons"])
            if len(rows) != sum(season["episode_count"] for season in seasons):
                continue
            expected = {
                (season["number"], episode)
                for season in seasons
                for episode in range(1, season["episode_count"] + 1)
            }
            if expected and watched == expected:
                complete[number] = (
                    rows,
                    len(seasons),
                    len(expected),
                    catalog["status"],
                )
        result, emitted = [], set()
        for row in records:
            number = row["media"].get("tmdb_id")
            key = number if row["matched"] else row.get("pending_series_key")
            rows = shows.get(number) if row["matched"] else pending.get(key)
            if not rows or not regular_episode(row):
                result.append(row)
                continue
            if key in emitted:
                continue
            emitted.add(key)
            watched = {regular_episode(item) for item in rows}
            # 重看早期单集不使进度倒退；最高季集号仅作展示，不推测中间每集都看过。
            progress_season, progress_episode = max(watched)
            season_count, episode_count = len({s for s, _ in watched}), len(watched)
            basis = None
            if number in complete:
                _, season_count, episode_count, basis = complete[number]
            media = {
                **row["media"],
                "season_number": None,
                "episode_number": None,
                "episode_title": None,
                "poster_path": next(
                    (
                        item["media"]["poster_path"]
                        for item in rows
                        if item["media"].get("poster_path")
                    ),
                    None,
                ),
            }
            result.append(
                {
                    **row,
                    "media": media,
                    "series_grouped": True,
                    "series_complete": number in complete,
                    "progress_season": progress_season,
                    "progress_episode": progress_episode,
                    "season_count": season_count,
                    "episode_count": episode_count,
                    "completion_basis": basis,
                    "sources": sorted({source for record in rows for source in record["sources"]}),
                }
            )
        return result

    def pending(self, owner, force=False):
        catalogs = {
            row["tmdb_id"]: row
            for row in self.store.all(
                "SELECT tmdb_id,checked_at,catalog_version FROM series_footprint_catalog "
                "WHERE user_id=?",
                (owner,),
            )
        }
        shows = {}
        for record in self.archive.records(owner):
            number = record["media"].get("tmdb_id")
            if type(number) is int and number > 0 and regular_episode(record):
                show = shows.setdefault(
                    number, {"tmdb_id": number, "title": record["media"]["title"], "episodes": []}
                )
                show["episodes"].append(regular_episode(record))
        cutoff = datetime.now(UTC) - timedelta(days=1)
        return [
            row
            for number, row in shows.items()
            if force
            or number not in catalogs
            or not catalogs[number]["catalog_version"]
            or datetime.fromisoformat(catalogs[number]["checked_at"]) < cutoff
        ]


def register_series_footprints(app, store, user_for, archive):
    service = SeriesFootprints(store, archive)
    app.state.series_footprints = service

    class Season(BaseModel):
        number: int = Field(ge=1, le=1000)
        episode_count: int = Field(ge=1, le=10000)

    class Unit(BaseModel):
        season: int = Field(ge=1, le=1000, strict=True)
        episode: int = Field(ge=1, le=100000, strict=True)

    class Catalog(BaseModel):
        tmdb_id: int = Field(ge=1, lt=2**53)
        status: str = Field(pattern="^(ended|airing|unknown)$")
        seasons: list[Season] = Field(default_factory=list, max_length=200)
        native_episodes: list[Unit] = Field(default_factory=list, max_length=30000)

    @app.get("/api/watch/footprints")
    async def viewing_footprints(
        request: Request, offset: int = Query(0, ge=0), limit: int = Query(18, ge=1, le=200)
    ):
        rows = service.grouped(user_for(request)["id"])
        return {
            "records": rows[offset : offset + limit],
            "next_offset": offset + limit if len(rows) > offset + limit else None,
        }

    @app.get("/api/watch/series-progress")
    async def series_to_verify(request: Request, force: bool = False):
        return {"series": service.pending(user_for(request)["id"], force)}

    @app.put("/api/watch/series-catalog")
    async def remember_series_catalog(payload: Catalog, request: Request):
        owner = user_for(request)["id"]
        policy = getattr(store, "display_policy", None)
        allowed = policy.guard(owner) if policy and policy.private_hidden(owner) else lambda _: True
        candidates = store.all(
            "SELECT media FROM watch_facts WHERE user_id=? AND canonical_key LIKE ?",
            (owner, f"tmdb:tv:{payload.tmdb_id}:s%:e%"),
        )
        if not any(
            regular_episode({"media": media}) and allowed(media)
            for media in (json.loads(row["media"]) for row in candidates)
        ):
            raise HTTPException(404, "本人可见足迹中没有这部剧集")
        seasons = [season.model_dump() for season in payload.seasons]
        if (
            len({season["number"] for season in seasons}) != len(seasons)
            or sum(season["episode_count"] for season in seasons) > 30000
        ):
            raise HTTPException(422, "剧集目录无效或超过核对上限")
        if payload.status == "ended" and not seasons:
            raise HTTPException(422, "缺少正片季集目录，不能确认全剧完成")
        # 目录由当前账号的正式作品预检读取，只影响该账号的私人展示，不作为分享授权。
        store.execute(
            "INSERT INTO series_footprint_catalog(user_id,tmdb_id,status,seasons,checked_at,"
            "catalog_version) VALUES(?,?,?,?,?,1) ON CONFLICT(user_id,tmdb_id) DO UPDATE SET "
            "status=excluded.status,seasons=excluded.seasons,checked_at=excluded.checked_at,"
            "catalog_version=1",
            (
                owner,
                payload.tmdb_id,
                payload.status,
                json.dumps(seasons),
                datetime.now(UTC).isoformat(),
            ),
        )
        store.execute(
            "INSERT OR REPLACE INTO series_platform_catalog VALUES(?,?,?,?,?)",
            (
                owner,
                payload.tmdb_id,
                "movieclaw",
                json.dumps(
                    sorted({(unit.season, unit.episode) for unit in payload.native_episodes})
                ),
                datetime.now(UTC).isoformat(),
            ),
        )
        return {"ok": True}

    @app.post("/api/watch/series/{tmdb_id}/platform-catalogs")
    async def refresh_platform_series(request: Request, tmdb_id: int, force: bool = False):
        owner = user_for(request)["id"]
        if not any(
            regular_episode(row) and row["media"].get("tmdb_id") == tmdb_id
            for row in archive.records(owner)
        ):
            raise HTTPException(404, "本人可见足迹中没有这部剧集")
        sources = store.all(
            "SELECT DISTINCT s.* FROM watch_sources s JOIN watch_facts f ON f.source_id=s.id "
            "WHERE s.user_id=? AND f.user_id=? AND s.active=1 AND f.canonical_key LIKE ?",
            (owner, owner, f"tmdb:tv:{tmdb_id}:s%:e%"),
        )
        failed = 0
        for source in sources:
            cached = store.one(
                "SELECT checked_at FROM series_platform_catalog "
                "WHERE user_id=? AND tmdb_id=? AND source_id=?",
                (owner, tmdb_id, source["id"]),
            )
            if (
                not force
                and cached
                and datetime.fromisoformat(cached["checked_at"])
                >= (datetime.now(UTC) - timedelta(days=1))
            ):
                continue
            fact = store.one(
                "SELECT external_id FROM watch_facts WHERE user_id=? AND source_id=? "
                "AND canonical_key LIKE ? ORDER BY watched_at DESC LIMIT 1",
                (owner, source["id"], f"tmdb:tv:{tmdb_id}:s%:e%"),
            )
            try:
                units = await asyncio.wait_for(
                    read_catalog(
                        archive.external,
                        source,
                        store.decrypt(source["origin"]),
                        store.decrypt(source["secret"]),
                        fact["external_id"],
                        tmdb_id,
                    ),
                    timeout=15,
                )
                archive.source_for(source["id"], owner)
            except (PeerError, HTTPException, TimeoutError):
                failed += 1
                units = []
            store.execute(
                "INSERT OR REPLACE INTO series_platform_catalog VALUES(?,?,?,?,?)",
                (owner, tmdb_id, source["id"], json.dumps(units), datetime.now(UTC).isoformat()),
            )
        return {"ok": True, "failed_sources": failed}

    @app.get("/api/watch/series/{tmdb_id}/records")
    async def series_episode_records(request: Request, tmdb_id: int):
        rows = [
            row
            for row in archive.records(user_for(request)["id"])
            if row["media"].get("kind") == "tv" and row["media"].get("tmdb_id") == tmdb_id
        ]
        if not rows:
            raise HTTPException(404, "本人可见足迹中没有这部剧集")
        return {
            "records": sorted(
                rows,
                key=lambda row: (
                    row["media"].get("season_number") or 0,
                    row["media"].get("episode_number") or 0,
                ),
            )
        }

    @app.get("/api/watch/pending-series/{record_id}/records")
    async def pending_series_records(request: Request, record_id: int):
        """分组锚点必须属于本人当前可见足迹；不可用其他账号的记录枚举私人档案。"""
        records = archive.records(user_for(request)["id"])
        anchor = next((row for row in records if row["id"] == record_id), None)
        if not anchor or anchor["matched"] or not regular_episode(anchor):
            raise HTTPException(404, "本人可见足迹中没有这部待匹配剧集")
        return {
            "records": sorted(
                (
                    row
                    for row in records
                    if not row["matched"]
                    and regular_episode(row)
                    and row.get("pending_series_key") == anchor.get("pending_series_key")
                ),
                key=lambda row: regular_episode(row),
            )
        }
