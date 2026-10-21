"""媒体库展示边界：来源留在私有索引，公开出口每次重新判定，不信任前端隐藏。"""

import asyncio
import hashlib
import json
import re
import unicodedata
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime

from fastapi import HTTPException, Request
from pydantic import BaseModel, Field


def stamp():
    return datetime.now(UTC).isoformat()


def work_keys(value):
    media = value.get("media", value)
    keys = []
    if media.get("kind") in {"movie", "tv"}:
        number = media.get("tmdb_id")
        if type(number) is int and number > 0:
            keys.append(f"tmdb:{media['kind']}:{number}")
        title = media.get("title")
        if isinstance(title, str) and title.strip():
            normalized = " ".join(unicodedata.normalize("NFKC", title).casefold().split())
            keys.append(
                "title:" + media["kind"] + ":" + hashlib.sha256(normalized.encode()).hexdigest()
            )
        number = media.get("media_item_id")
        if type(number) is int and number > 0:
            keys.append(f"host:{number}")
    return keys


class DisplayPolicy:
    def __init__(self, store):
        self.store = store
        self._cache = {}
        with store.lock:
            store.db.executescript("""
                CREATE TABLE IF NOT EXISTS display_settings (
                    user_id TEXT PRIMARY KEY REFERENCES users(id),
                    hide_private INTEGER NOT NULL DEFAULT 1,
                    exclude_statistics INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE IF NOT EXISTS display_libraries (
                    user_id TEXT NOT NULL REFERENCES users(id),source_id TEXT NOT NULL,
                    library_id TEXT NOT NULL,name TEXT NOT NULL,excluded INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL,PRIMARY KEY(user_id,source_id,library_id)
                );
                CREATE TABLE IF NOT EXISTS display_memberships (
                    user_id TEXT NOT NULL REFERENCES users(id),source_id TEXT NOT NULL,
                    external_id TEXT NOT NULL,keys_json TEXT NOT NULL,libraries_json TEXT NOT NULL,
                    PRIMARY KEY(user_id,source_id,external_id)
                );
                CREATE TABLE IF NOT EXISTS display_scans (
                    id TEXT PRIMARY KEY,user_id TEXT NOT NULL REFERENCES users(id),
                    status TEXT NOT NULL,
                    processed INTEGER NOT NULL DEFAULT 0,errors TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL
                );
            """)
            columns = {row["name"] for row in store.all("PRAGMA table_info(display_settings)")}
            if "exclude_statistics" not in columns:
                store.execute(
                    "ALTER TABLE display_settings ADD COLUMN "
                    "exclude_statistics INTEGER NOT NULL DEFAULT 1"
                )
                # 拆分选项时延续每个账号原有的统计行为，不替用户重新选择。
                store.execute("UPDATE display_settings SET exclude_statistics=hide_private")
            store.execute("UPDATE display_scans SET status='interrupted' WHERE status='reading'")

    @contextmanager
    def atomic(self):
        with self.store.lock:
            nested = self.store.db.in_transaction
            if not nested:
                self.store.db.execute("BEGIN")
            try:
                yield
                if not nested:
                    self.store.db.commit()
            except BaseException:
                if not nested:
                    self.store.db.rollback()
                raise

    def configured(self, owner):
        return bool(
            self.store.one(
                "SELECT 1 FROM display_libraries WHERE user_id=? AND excluded=1", (owner,)
            )
        )

    def guard(self, owner):
        rows = self.store.all(
            "SELECT source_id,library_id,excluded FROM display_libraries WHERE user_id=?", (owner,)
        )
        excluded = {(row["source_id"], row["library_id"]) for row in rows if row["excluded"]}
        if not excluded:
            return lambda value: True
        cached = self._cache.get(owner)
        scope = tuple((row["source_id"], row["library_id"], row["excluded"]) for row in rows)
        if cached and cached[0] == scope:
            return cached[1]
        known = {(row["source_id"], row["library_id"]) for row in rows}
        allowed_keys, denied_keys = set(), set()
        for row in self.store.all(
            "SELECT source_id,keys_json,libraries_json FROM display_memberships WHERE user_id=?",
            (owner,),
        ):
            libs = {(row["source_id"], str(number)) for number in json.loads(row["libraries_json"])}
            keys = json.loads(row["keys_json"])
            if libs & excluded:
                denied_keys.update(keys)
            elif libs and libs <= known:
                allowed_keys.update(keys)

        def allowed(value):
            keys = set(work_keys(value))
            # 任一来源命中排除库则隐藏；不能用同一作品在其他来源的副本绕过。
            return bool(keys & allowed_keys) and not bool(keys & denied_keys)

        self._cache[owner] = (scope, allowed, frozenset(denied_keys))
        return allowed

    def verdict(self, owner, media):
        """区分已命中排除规则与缺少来源，避免把普通未观看影片误称为排除内容。"""
        if self.guard(owner)(media):
            return "allowed"
        return "excluded" if set(work_keys(media)) & self.denied_keys(owner) else "unverified"

    def denied_keys(self, owner):
        """已知排除库的作品身份；对齐新库存时不能借允许库的副本重新放行。"""
        if not self.configured(owner):
            return frozenset()
        self.guard(owner)
        return self._cache[owner][2]

    def filter(self, owner, rows):
        allowed = self.guard(owner)
        return [row for row in rows if allowed(row)]

    def snapshot(self, owner, snapshot):
        allowed = self.guard(owner)
        result = dict(snapshot)
        result["current"] = (
            snapshot.get("current")
            if snapshot.get("current") and allowed(snapshot["current"])
            else None
        )
        result["history"] = [row for row in snapshot.get("history", []) if allowed(row)]
        result["favorites"] = [row for row in snapshot.get("favorites", []) if allowed(row)]
        result["collections"] = []
        for collection in snapshot.get("collections", []):
            items = [item for item in collection.get("items", []) if allowed(item)]
            if items or not collection.get("items"):
                filtered = {**collection, "items": items}
                if collection.get("cover") and not allowed(collection["cover"]):
                    filtered.pop("cover", None)
                result["collections"].append(filtered)
        return result

    def catalog(self, owner, source, libraries):
        if len(libraries) > 200:
            raise HTTPException(422, "媒体库数量超过单次限制")
        parameters = []
        for library in libraries:
            identifier = str(library.get("id", ""))
            name = str(library.get("name", "")).strip()[:120]
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", identifier) or not name:
                raise HTTPException(422, "媒体库名称或编号无效")
            parameters.append((owner, source, identifier, name, stamp()))
        with self.atomic():
            self.store.db.executemany(
                "INSERT INTO display_libraries(user_id,source_id,library_id,name,updated_at) "
                "VALUES(?,?,?,?,?) ON CONFLICT(user_id,source_id,library_id) "
                "DO UPDATE SET name=excluded.name,updated_at=excluded.updated_at",
                parameters,
            )
        self._cache.pop(owner, None)

    def remember(self, owner, source, records, libraries=None):
        with self.atomic():
            if libraries is not None:
                self.store.execute(
                    "DELETE FROM display_memberships WHERE user_id=? AND source_id=?",
                    (owner, source),
                )
                self.catalog(owner, source, libraries)
            for row in records:
                # 本地条目编号只对 MovieClaw 来源有效，不能混入 Plex/Emby 的编号。
                media = dict(row["media"])
                if source != "movieclaw":
                    media.pop("media_item_id", None)
                refs = [
                    str(value)
                    for value in row.get("library_ids", [])
                    if re.fullmatch(r"[A-Za-z0-9_-]{1,80}", str(value))
                ]
                if not refs:
                    self.store.execute(
                        "DELETE FROM display_memberships WHERE user_id=? AND source_id=? "
                        "AND external_id=?",
                        (owner, source, str(row["external_id"])),
                    )
                    continue
                self.store.execute(
                    "INSERT INTO "
                    "display_memberships(user_id,source_id,external_id,keys_json,libraries_json)"
                    " VALUES(?,?,?,?,?) ON "
                    "CONFLICT(user_id,source_id,external_id) DO UPDATE SET "
                    "keys_json=excluded.keys_json,libraries_json=excluded.libraries_json",
                    (
                        owner,
                        source,
                        str(row["external_id"]),
                        json.dumps(work_keys(media)),
                        json.dumps(refs),
                    ),
                )
        self._cache.pop(owner, None)

    def remember_native(self, account, rows):
        user = self.store.one("SELECT id FROM users WHERE username=?", (account,))
        if not user:
            return
        records = []
        for row in rows:
            if row.get("id") is not None:
                records.append(
                    {
                        "external_id": str(row["id"]),
                        "media": row["media"],
                        "library_ids": row["media"].get("_display_library_ids", []),
                    }
                )
        self.remember(user["id"], "movieclaw", records)

    def private_hidden(self, owner):
        row = self.store.one("SELECT hide_private FROM display_settings WHERE user_id=?", (owner,))
        return row is None or bool(row["hide_private"])

    def statistics_excluded(self, owner):
        row = self.store.one(
            "SELECT exclude_statistics FROM display_settings WHERE user_id=?", (owner,)
        )
        return row is None or bool(row["exclude_statistics"])

    def settings(self, owner):
        sources = {
            row["id"]: row
            for row in self.store.all(
                "SELECT id,platform,account_name,active FROM watch_sources WHERE user_id=?",
                (owner,),
            )
        }
        groups = {}
        for row in self.store.all(
            "SELECT source_id,library_id,name,excluded,updated_at FROM "
            "display_libraries WHERE user_id=? ORDER BY source_id,name",
            (owner,),
        ):
            key = row["source_id"]
            groups.setdefault(
                key,
                {
                    "source_id": key,
                    "platform": "movieclaw"
                    if key == "movieclaw"
                    else sources.get(key, {}).get("platform", "unknown"),
                    "account_name": sources.get(key, {}).get("account_name"),
                    "active": key == "movieclaw" or bool(sources.get(key, {}).get("active")),
                    "libraries": [],
                },
            )["libraries"].append(
                {"id": row["library_id"], "name": row["name"], "excluded": bool(row["excluded"])}
            )
        scan = self.store.one(
            "SELECT id,status,processed,errors FROM display_scans WHERE "
            "user_id=? ORDER BY created_at DESC LIMIT 1",
            (owner,),
        )
        if scan:
            scan["errors"] = json.loads(scan["errors"])
        return {
            "groups": list(groups.values()),
            "hide_private": self.private_hidden(owner),
            "exclude_statistics": self.statistics_excluded(owner),
            "restricted": self.configured(owner),
            "scan": scan,
        }

    def start(self, user, archive):
        existing = self.store.one(
            "SELECT id FROM display_scans WHERE user_id=? AND status='reading'", (user["id"],)
        )
        if existing:
            return existing["id"]
        identifier = str(uuid.uuid4())
        self.store.execute(
            "INSERT INTO display_scans(id,user_id,status,created_at) VALUES(?,?,'reading',?)",
            (identifier, user["id"], stamp()),
        )
        task = archive.task_factory(self.scan(identifier, user, archive))
        archive.tasks["display:" + identifier] = task
        task.add_done_callback(lambda _: archive.tasks.pop("display:" + identifier, None))
        return identifier

    async def scan(self, identifier, user, archive):
        errors, processed = [], 0
        try:
            if getattr(archive.source, "browser_required", False):
                try:
                    records = await archive.source.archive(self.store.decrypt(user["token"]))
                    with self.atomic():
                        self.store.execute(
                            "DELETE FROM display_memberships WHERE user_id=? "
                            "AND source_id='movieclaw'",
                            (user["id"],),
                        )
                        self.remember(user["id"], "movieclaw", records)
                    processed += len(records)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    errors.append("MovieClaw 媒体库核对未完成；来源未确认的内容将隐藏")
            sources = self.store.all(
                "SELECT * FROM watch_sources WHERE user_id=? AND active=1", (user["id"],)
            )
            for source in sources:
                try:
                    kwargs = {"include_libraries": True}
                    if source["platform"] == "emby":
                        kwargs["account"] = source["account_id"] or source["account_selector"]
                    result = await archive.external.read(
                        source["platform"],
                        self.store.decrypt(source["origin"]),
                        self.store.decrypt(source["secret"]),
                        lambda _: None,
                        **kwargs,
                    )
                    latest = archive.source_for(source["id"], user["id"])
                    if latest.get("account_id") and latest["account_id"] != result["account_id"]:
                        raise ValueError("account changed")
                    self.remember(
                        user["id"], source["id"], result["records"], result.get("libraries", [])
                    )
                    processed += len(result["records"])
                    self.store.execute(
                        "UPDATE display_scans SET processed=? WHERE id=?", (processed, identifier)
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    errors.append(
                        source["platform"]
                        + " 媒体库核对未完成；该来源未确认的内容不能在排除模式下分享"
                    )
            self.store.execute(
                "UPDATE display_scans SET status='complete',errors=? WHERE id=?",
                (json.dumps(errors, ensure_ascii=False), identifier),
            )
        except asyncio.CancelledError:
            self.store.execute(
                "UPDATE display_scans SET status='interrupted' WHERE id=?", (identifier,)
            )
            raise


def register_display_policy(app, store, user_for, archive, policy):
    class Library(BaseModel):
        id: str = Field(min_length=1, max_length=80)
        name: str = Field(min_length=1, max_length=120)

    class Catalog(BaseModel):
        libraries: list[Library] = Field(max_length=200)

    class Exclusion(BaseModel):
        source_id: str = Field(min_length=1, max_length=80)
        library_id: str = Field(min_length=1, max_length=80)

    class Preferences(BaseModel):
        excluded: list[Exclusion] = Field(max_length=1000)
        hide_private: bool = Field(default=True, strict=True)
        exclude_statistics: bool | None = Field(default=None, strict=True)

    @app.get("/api/display-policy")
    async def display_preferences(request: Request):
        return policy.settings(user_for(request)["id"])

    @app.put("/api/display-policy/movieclaw")
    async def sync_native_library_labels(payload: Catalog, request: Request):
        owner = user_for(request)["id"]
        # 当前浏览器仅同步本人可见库的显示名称；来源成员身份不由此接口决定。
        policy.catalog(owner, "movieclaw", [entry.model_dump() for entry in payload.libraries])
        return {"ok": True}

    @app.put("/api/display-policy")
    async def save_display_preferences(payload: Preferences, request: Request):
        owner = user_for(request)["id"]
        chosen = {(entry.source_id, entry.library_id) for entry in payload.excluded}
        known = {
            (row["source_id"], row["library_id"])
            for row in store.all(
                "SELECT source_id,library_id FROM display_libraries WHERE user_id=?", (owner,)
            )
        }
        if not chosen <= known:
            raise HTTPException(422, "所选媒体库不属于本人已读取的库，请刷新后重试")
        with policy.atomic():
            store.execute("UPDATE display_libraries SET excluded=0 WHERE user_id=?", (owner,))
            for source, library in chosen:
                store.execute(
                    "UPDATE display_libraries SET excluded=1 WHERE user_id=?"
                    " AND source_id=? AND library_id=?",
                    (owner, source, library),
                )
            store.execute(
                "INSERT INTO display_settings(user_id,hide_private,exclude_statistics) "
                "VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET "
                "hide_private=excluded.hide_private,exclude_statistics=excluded.exclude_statistics",
                (
                    owner,
                    payload.hide_private,
                    payload.hide_private
                    if payload.exclude_statistics is None
                    else payload.exclude_statistics,
                ),
            )
            store.execute(
                "UPDATE users SET privacy_revision=privacy_revision+1 WHERE id=?", (owner,)
            )
            store.execute(
                "UPDATE relationships SET cache=NULL WHERE user_id=? OR target_id=?", (owner, owner)
            )
        policy._cache.pop(owner, None)
        return policy.settings(owner)

    @app.post("/api/display-policy/refresh", status_code=202)
    async def refresh_display_libraries(request: Request):
        return {"id": policy.start(user_for(request), archive)}
