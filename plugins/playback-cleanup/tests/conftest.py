"""测试替身：内存插件数据、假宿主操作（按 MovieClaw v0.49.0 / #1203 的 catalog / sync.state 语义：
游标翻页、``kinds`` 过滤（默认 movie + tv）、compare-and-set、``media.identity``（= external_id）、
身份校验（无外部编号的本地条目必须回传当时的 identity 令牌、不得声明编号）、省略字段不改）。

``legacy = True`` 模拟 v0.46–0.48：catalog 不下发 ``identity``，``SyncChangeIn`` 是
``extra=forbid``——请求里多一个 ``identity`` 整批 422；同一性只认吻合的外部编号。
全程不联网、不需要真宿主。"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from playback_cleanup.runtime import OpsError, Runtime  # noqa: E402

STORE_VALUE_LIMIT = 64 * 1024
BASE = datetime(2026, 11, 1, 8, 0, tzinfo=UTC)


class FakeStore:
    def __init__(self) -> None:
        self.data: dict[str, Any] = {}

    async def get(self, key: str, *, scope: str = "global", default=None):
        value = self.data.get(key, default)
        return json.loads(json.dumps(value)) if value is not None else value

    async def set(self, key: str, value, *, scope: str = "global", secret: bool = False) -> None:
        encoded = json.dumps(value, ensure_ascii=False)
        assert len(encoded.encode("utf-8")) < STORE_VALUE_LIMIT, f"{key} 超过 64KB"
        self.data[key] = json.loads(encoded)

    async def delete(self, key: str, *, scope: str = "global") -> bool:
        return self.data.pop(key, None) is not None


class FakeHost:
    """媒体库、成员、按成员的播放状态。时间按宿主习惯给不带时区的 UTC 字符串。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.libraries = [
            {"id": 1, "name": "电影", "kind": "movie", "source": "tmdb"},
            {"id": 2, "name": "剧集", "kind": "tv", "source": "tmdb"},
            {"id": 3, "name": "其他视频", "kind": "video", "source": "local"},
        ]
        self.members = [{"id": 7, "username": "kid", "nickname": "小朋友", "status": "active"}]
        # media_item_id -> (library_id, media dict)
        self.media: dict[int, tuple[int, dict]] = {}
        # (member, media, season, episode) -> state dict
        self.states: dict[tuple[int, int, int, int], dict] = {}
        self.tick = 0
        #: sync.state 写入前的钩子：模拟客户端在此刻又上报了一次进度
        self.before_state: list = []
        #: 模拟 0.46.0：catalog 的操作目录里没有 kinds，带上就 INVALID_ARGUMENTS
        self.old_host = False
        #: 模拟 v0.46–0.48 的 sync 契约：没有 identity（下发 / 接收都没有）
        self.legacy = False
        #: 模拟配套新宿主：catalog 带 continue_dismissed，sync.state 认 dismiss_continue。
        #: 默认 False = 现有 0.49.1（#1203）：没有这一列，多送 dismiss_continue 整批 422
        self.dismiss = False
        #: media_item_id -> external_id（宿主 catalog 以 media.identity 下发）
        self.external: dict[int, str] = {}
        #: 状态行主键（宿主锚点平局按 id 倒序）：按创建先后递增
        self.row_ids: dict[tuple[int, int, int, int], int] = {}
        #: 被宿主锚点规则（MovieClaw#1246）拒掉的 dismiss_continue=true 写入
        self.anchor_conflicts: list[dict] = []

    # ---- 造数据
    def add_media(self, mid: int, library_id: int, kind: str = "movie", title: str = "", **ids):
        """默认带 TMDB 编号；``kind="video"`` 默认没有编号（「其他」库的本地条目）。"""
        external_id = ids.pop("external_id", None)
        ids.setdefault("tmdb_id", None if kind == "video" else 1000 + mid)
        self.media[mid] = (
            library_id,
            {"id": mid, "kind": kind, "title": title or f"片{mid}", "imdb_id": None, **ids},
        )
        self.external[mid] = external_id or (
            f"tmdb:{ids['tmdb_id']}" if ids.get("tmdb_id") else f"local-{kind}-{mid}"
        )

    def replace_media(self, mid: int, external_id: str, **kw) -> None:
        """删除条目 A（连同其播放状态），再以同一数字 id 建条目 B（SQLite 复用主键）。"""
        library_id = self.media[mid][0]
        self.states = {k: v for k, v in self.states.items() if k[1] != mid}
        kw.setdefault("tmdb_id", None)
        self.add_media(mid, library_id, external_id=external_id, **kw)

    def same_identity(self, media: dict | None, c: dict) -> bool:
        """与宿主 ``movieclaw_api.services.playback_sync.same_identity``（#1203）同构。"""
        if not media or media["kind"] != c["kind"]:
            return False
        if self.legacy:  # 0.46.0：只认吻合的外部编号，无编号条目一律 mismatch
            return bool(
                (c.get("tmdb_id") and c["tmdb_id"] == media.get("tmdb_id"))
                or (c.get("imdb_id") and c["imdb_id"] == media.get("imdb_id"))
            )
        if media.get("tmdb_id") is None and media.get("imdb_id") is None:
            return (
                not c.get("tmdb_id")
                and not c.get("imdb_id")
                and bool(c.get("identity"))
                and c["identity"] == self.external.get(media["id"])
            )
        return bool(
            (c.get("tmdb_id") and c["tmdb_id"] == media.get("tmdb_id"))
            or (c.get("imdb_id") and c["imdb_id"] == media.get("imdb_id"))
        )

    def stamp(self) -> str:
        self.tick += 1
        return (BASE + timedelta(seconds=self.tick)).replace(tzinfo=None).isoformat()

    def set_state(self, member: int, mid: int, season: int = 0, episode: int = 0, **values):
        key = (member, mid, season, episode)
        self.row_ids.setdefault(key, len(self.row_ids) + 1)
        state = self.states.setdefault(
            key,
            {"played": False, "position_ms": 0, "is_favorite": False, "play_count": 0,
             "last_played_at": "2026-10-31T20:00:00"},
        )
        state.update(values)
        state["updated_at"] = self.stamp()
        return state

    def progress(self, member: int, mid: int, season: int = 0, episode: int = 0, pos: int = 1,
                 at: str | None = None):
        """一次真实播放上报：（新宿主替身）刷新 last_played_at（默认现在），宿主清掉整部作品的 continue_dismissed
        （#1246：比隐藏新的播放事实按作品恢复，再播放卡片回来）。"""
        self.restore_title(member, mid)
        state = self.set_state(member, mid, season, episode, position_ms=pos, continue_dismissed=False)
        if at or self.dismiss:  # 旧宿主替身保持原来的固定时间（既有断言依赖它）
            state["last_played_at"] = at or state["updated_at"]
        return state

    def restore_title(self, member: int, mid: int) -> None:
        for (m, i, _s, _e), st in self.states.items():
            if m == member and i == mid and st.get("continue_dismissed"):
                st["continue_dismissed"] = False
                st["updated_at"] = self.stamp()

    def anchor(self, member: int, mid: int) -> tuple[int, int] | None:
        """与宿主 ``playback_up_next._ranked_states`` 同一口径：last_played_at 倒序、id 倒序。"""
        rows = [
            (st["last_played_at"], self.row_ids.get(k, 0), (k[2], k[3]))
            for k, st in self.states.items()
            if k[0] == member and k[1] == mid and st.get("last_played_at") and k[2] >= 0 and k[3] >= 0
        ]
        return max(rows)[2] if rows else None

    def hidden(self, member: int, mid: int) -> bool:
        """首页按作品判定：任一行带标记即隐藏（#1246）。"""
        return any(
            st.get("continue_dismissed")
            for k, st in self.states.items()
            if k[0] == member and k[1] == mid
        )

    def up_next(self, member: int) -> set[int]:
        """简化的「接下来继续」：有锚点、锚点没看完、作品没隐藏的作品。"""
        out = set()
        for (m, mid, _s, _e) in self.states:
            if m != member or mid in out:
                continue
            a = self.anchor(member, mid)
            if a and not self.states[(member, mid, *a)]["played"] and not self.hidden(member, mid):
                out.add(mid)
        return out

    def state_calls(self) -> list[dict]:
        return [p for name, p in self.calls if name == "playback.sync.state"]

    # ---- 宿主操作
    async def call(self, name: str, params: dict) -> Any:
        self.calls.append((name, json.loads(json.dumps(params))))
        handler = getattr(self, "op_" + name.replace(".", "_").replace("-", "_"), None)
        if handler is None:
            raise OpsError(404, "not_found", f"no op {name}")
        return handler(params)

    def op_library_list(self, _p):
        return list(self.libraries)

    def op_members_list(self, _p):
        return list(self.members)

    def op_library_items_list(self, p):
        lib = int(p["library_id"])
        rows = [
            {"media_item_id": mid, "kind": media["kind"], "title": media["title"], "library_id": lib}
            for mid, (lid, media) in sorted(self.media.items())
            if lid == lib
        ]
        offset, limit = int(p.get("offset") or 0), p.get("limit")
        return rows[offset : offset + limit] if limit else rows[offset:]

    def _units(self, member: int, kinds: list[str]):
        out = []
        for mid, (_lib, media) in sorted(self.media.items()):
            if media["kind"] not in kinds:
                continue
            if media["kind"] != "tv":  # 电影 / 视频：单一 0/0 单元
                out.append((mid, 0, 0))
            for (m, i, s, e) in self.states:
                if m == member and i == mid and media["kind"] == "tv":
                    out.append((mid, s, e))
        return sorted(set(out))

    def op_playback_sync_catalog(self, p):
        if self.old_host and "kinds" in p:
            raise OpsError(400, "INVALID_ARGUMENTS", "这些参数不属于 playback_sync_catalog：kinds")
        member = int(p.get("member_id") or 0)
        limit = int(p.get("limit") or 50)
        kinds = p.get("kinds") or ["movie", "tv"]
        if isinstance(kinds, str):
            kinds = [kinds]
        assert set(kinds) <= {"movie", "tv", "video"}, kinds
        units = self._units(member, list(kinds))
        if p.get("cursor"):
            cur = tuple(map(int, p["cursor"].split(":")))
            units = [u for u in units if u > cur]
        page, more = units[:limit], len(units) > limit
        items = []
        for mid, s, e in page:
            st = self.states.get((member, mid, s, e))
            media = dict(self.media[mid][1])
            if not self.legacy:
                media["identity"] = self.external[mid]  # #1203：必填
            items.append(
                {
                    "media": media,
                    "season": s,
                    "episode": e,
                    "played": st["played"] if st else False,
                    "position_ms": st["position_ms"] if st else 0,
                    "is_favorite": st["is_favorite"] if st else False,
                    "updated_at": st["updated_at"] if st else None,
                    "last_played_at": st["last_played_at"] if st else None,
                    "watches": [],
                }
            )
            if self.dismiss:
                items[-1]["continue_dismissed"] = bool(st and st.get("continue_dismissed"))
        return {"items": items, "next_cursor": ":".join(map(str, page[-1])) if more and page else None}

    def op_playback_sync_state(self, p):
        member = int(p.get("member_id") or 0)
        allowed = {"media_item_id", "kind", "tmdb_id", "imdb_id", "season", "episode",
                   "expected_updated_at", "played", "position_ms", "is_favorite", "last_played_at"}
        if not self.legacy:
            allowed.add("identity")
        if self.dismiss:
            allowed.add("dismiss_continue")
        for c in p["changes"]:
            extra = set(c) - allowed
            if extra:  # pydantic extra=forbid：整批 422，一条也不写
                raise OpsError(422, "INVALID_ARGUMENTS", f"Extra inputs are not permitted: {sorted(extra)}")
            if "identity" in c:
                assert isinstance(c["identity"], str) and c["identity"], "identity 须为非空字符串"
        for hook in list(self.before_state):
            hook()
        self.before_state.clear()
        dismissing = {c["media_item_id"] for c in p["changes"] if c.get("dismiss_continue") is True}
        outcomes = []
        for c in p["changes"]:
            expected = c["expected_updated_at"]
            assert expected is None or "+" in expected or expected.endswith("Z"), "须带时区"
            entry = self.media.get(c["media_item_id"])
            media = entry[1] if entry else None
            if not self.same_identity(media, c):
                outcomes.append({"status": "mismatch"})
                continue
            assert c["kind"] == "tv" or not (c.get("season") or c.get("episode")), "非剧集不带季集"
            tv = c["kind"] == "tv"
            key = (
                member,
                c["media_item_id"],
                (c.get("season") or 0) if tv else 0,
                (c.get("episode") or 0) if tv else 0,
            )
            st = self.states.get(key) or {}
            current = (
                datetime.fromisoformat(st["updated_at"]).replace(tzinfo=UTC) if st else None
            )
            old = {"continue_dismissed": False, **st} if self.dismiss else st
            want = datetime.fromisoformat(expected) if expected else None
            if current != want:
                outcomes.append({"status": "conflict"})
                continue
            new = dict(old)
            if c.get("played") is not None:
                new["played"] = c["played"]
            if c.get("position_ms") is not None:
                new["position_ms"] = c["position_ms"]
            if c.get("dismiss_continue") is not None:
                assert isinstance(c["dismiss_continue"], bool)
                new["continue_dismissed"] = c["dismiss_continue"]
            if new == old:
                outcomes.append({"status": "unchanged", "state_version": st["updated_at"]})
                continue
            if c.get("dismiss_continue") is True:
                # #1246：作品级意图，写完仍须是作品当前锚点，否则整条回滚（position_ms 也不写）
                saved = self.states.get(key)
                self.states[key] = new
                self.row_ids.setdefault(key, len(self.row_ids) + 1)
                anchor = self.anchor(member, key[1])
                if anchor is not None and anchor != key[2:]:
                    if saved is None:
                        self.states.pop(key)
                    else:
                        self.states[key] = saved
                    self.anchor_conflicts.append(dict(c))
                    outcomes.append({"status": "conflict"})
                    continue
            elif (
                self.dismiss
                and (new.get("position_ms") or 0) > 0
                and new.get("position_ms") != old.get("position_ms")
                and key[1] not in dismissing
            ):
                self.restore_title(member, key[1])  # 进度往前走 = 播放事实：按作品恢复
                new["continue_dismissed"] = False
            new["updated_at"] = self.stamp()
            self.states[key] = new
            self.row_ids.setdefault(key, len(self.row_ids) + 1)
            outcomes.append({"status": "applied", "state_version": new["updated_at"]})
        return {"outcomes": outcomes}


class Clock:
    def __init__(self) -> None:
        self.now = BASE.timestamp() + 3600

    def __call__(self) -> float:
        return self.now


async def instant(_seconds: float) -> None:
    return None


@pytest.fixture
def host() -> FakeHost:
    return FakeHost()


@pytest.fixture
def store() -> FakeStore:
    return FakeStore()


@pytest.fixture
def runtime(host, store) -> Runtime:
    return Runtime(ops=host, store=store, clock=Clock(), sleep=instant)


def event(member_id: int, mid: int, kind: str = "movie", season: int | None = None,
          episode: int | None = None, position_ms: int = 0):
    unit = SimpleNamespace(season=season, episode=episode) if season is not None else None
    return SimpleNamespace(
        member_id=member_id,
        media=SimpleNamespace(id=mid, kind=kind, title=f"片{mid}", tmdb_id=1000 + mid, imdb_id=None),
        unit=unit,
        position_ms=position_ms,
    )
