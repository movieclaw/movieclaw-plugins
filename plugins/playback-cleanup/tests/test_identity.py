"""0.1.2：本地条目（无 TMDB / IMDb）写回时的 identity 令牌（MovieClaw v0.49.0 / #1203）。

覆盖：清理 / 历史 / 撤销的令牌传递；缺失 / 错配 / 过期令牌；数字 id 复用；旧历史记录；
有外部编号的电影 / 剧集；旧宿主（无 identity、extra=forbid）回退；dry-run 与 CAS 重试。
"""

from __future__ import annotations

import pytest
from conftest import event

from playback_cleanup.runtime import (
    HISTORY_KEY,
    RECENT_KEY,
    SETTINGS_KEY,
    OpsError,
    SkipItem,
    build_change,
)


async def configure(store, **values):
    base = {"library_id": 1, "member_ids": [0], "dry_run": False}
    base.update(values)
    await store.set(SETTINGS_KEY, base)


def catalog_calls(host):
    return [p for n, p in host.calls if n == "playback.sync.catalog"]


def changes_by_id(call):
    return {c["media_item_id"]: c for c in call["changes"]}


# ---------------------------------------------------------------- FakeHost 按最终契约校验
def _change(mid, kind, expected, **fields):
    return {"media_item_id": mid, "kind": kind, "expected_updated_at": expected, **fields}


def test_fake_host_enforces_local_identity_contract(host):
    """与 host-ref/test_playback_sync.py::test_local_item_identity_still_rejects_forged_ids_and_wrong_kind 同构。"""
    host.add_media(10, 1, tmdb_id=None, external_id="local-test-1")
    host.add_media(11, 1)  # 有 TMDB
    host.progress(0, 10, pos=90_000)
    v = host.states[(0, 10, 0, 0)]["updated_at"] + "+00:00"
    token = "local-test-1"
    outs = host.op_playback_sync_state(
        {
            "member_id": 0,
            "changes": [
                _change(10, "movie", v, identity=token, position_ms=0, tmdb_id=1),  # 伪造编号
                _change(10, "movie", v, identity=token, position_ms=0, imdb_id="tt1"),
                _change(10, "video", v, identity=token, position_ms=0),  # kind 不符
                _change(10, "tv", v, identity=token, position_ms=0, season=1, episode=1),
                _change(11, "movie", None, identity="ignored", played=True),  # 有编号却只给令牌
                _change(10, "movie", v, position_ms=0),  # 缺令牌
                _change(10, "movie", v, identity="not-the-token", position_ms=0),  # 错令牌
            ],
        }
    )["outcomes"]
    assert [o["status"] for o in outs] == ["mismatch"] * 7
    assert host.states[(0, 10, 0, 0)]["position_ms"] == 90_000
    ok = host.op_playback_sync_state(
        {"member_id": 0, "changes": [_change(10, "movie", v, identity=token, position_ms=0)]}
    )["outcomes"][0]
    assert ok["status"] == "applied"


def test_fake_legacy_host_rejects_identity_field_for_whole_batch(host):
    host.legacy = True
    host.add_media(10, 1)
    host.progress(0, 10, pos=5000)
    v = host.states[(0, 10, 0, 0)]["updated_at"] + "+00:00"
    with pytest.raises(OpsError) as exc:
        host.op_playback_sync_state(
            {"member_id": 0, "changes": [
                _change(10, "movie", v, tmdb_id=1010, position_ms=0),
                _change(10, "movie", v, tmdb_id=1010, identity="x", position_ms=0),
            ]}
        )
    assert exc.value.status == 422
    assert host.states[(0, 10, 0, 0)]["position_ms"] == 5000
    assert "identity" not in host.op_playback_sync_catalog({"member_id": 0})["items"][0]["media"]


# ---------------------------------------------------------------- 请求形状
def test_build_change_identity_rules():
    local = {"media": {"id": 1, "kind": "movie", "tmdb_id": None, "imdb_id": None, "identity": "local-a"}}
    assert build_change(local, 0, "v")["identity"] == "local-a"
    # 有外部编号：只带编号，不带令牌
    tmdb = {"media": {"id": 2, "kind": "movie", "tmdb_id": 5, "imdb_id": None, "identity": "tmdb:5"}}
    assert "identity" not in build_change(tmdb, 0, "v") and build_change(tmdb, 0, "v")["tmdb_id"] == 5
    imdb = {"media": {"id": 3, "kind": "movie", "tmdb_id": None, "imdb_id": "tt9", "identity": "imdb:tt9"}}
    change = build_change(imdb, 0, "v")
    assert change["imdb_id"] == "tt9" and "identity" not in change and "tmdb_id" not in change
    # 旧宿主没有令牌 / 空令牌：形状与 0.1.1 完全一致
    for media in ({"id": 4, "kind": "video", "tmdb_id": None, "imdb_id": None},
                  {"id": 4, "kind": "video", "tmdb_id": None, "imdb_id": None, "identity": ""},
                  {"id": 4, "kind": "video", "tmdb_id": None, "imdb_id": None, "identity": None}):
        assert build_change({"media": media}, 0, "v") == {
            "media_item_id": 4, "kind": "video", "expected_updated_at": "v", "position_ms": 0,
        }


async def test_sweep_mixed_batch_shapes(runtime, host, store):
    host.add_media(10, 1, title="TMDB 电影")
    host.add_media(11, 1, tmdb_id=None, imdb_id="tt0011", title="只有 IMDb")
    host.add_media(12, 1, tmdb_id=None, external_id="local-movie-12", title="本地电影")
    host.add_media(13, 1, kind="video", external_id="local-video-13", title="家庭录像")
    host.add_media(30, 1, kind="tv", title="剧集")
    for mid in (10, 11, 12, 13):
        host.progress(0, mid, pos=5000)
    host.progress(0, 30, 2, 3, pos=7000)
    await configure(store)
    result = await runtime.sweep(reason="manual", dry_run=False)
    assert result["counts"]["cleared"] == 5, result
    (call,) = host.state_calls()
    by_id = changes_by_id(call)
    assert by_id[10] == {"media_item_id": 10, "kind": "movie", "tmdb_id": 1010,
                         "expected_updated_at": by_id[10]["expected_updated_at"], "position_ms": 0}
    assert "identity" not in by_id[11] and by_id[11]["imdb_id"] == "tt0011" and "tmdb_id" not in by_id[11]
    assert by_id[12]["identity"] == "local-movie-12" and "tmdb_id" not in by_id[12] and "imdb_id" not in by_id[12]
    assert by_id[13]["identity"] == "local-video-13" and by_id[13]["kind"] == "video"
    assert "season" not in by_id[13]
    assert "identity" not in by_id[30] and (by_id[30]["season"], by_id[30]["episode"]) == (2, 3)
    assert by_id[30]["tmdb_id"] == 1030
    # 历史：本地条目记下当时用的令牌，有编号的记 None
    hist = {h["media_item_id"]: h for h in store.data[HISTORY_KEY]}
    assert hist[12]["identity"] == "local-movie-12"
    assert hist[13]["identity"] == "local-video-13"
    assert hist[10]["identity"] is None and hist[11]["identity"] is None and hist[30]["identity"] is None
    assert all(h["undoable"] for h in await runtime.history_rows())


# ---------------------------------------------------------------- 撤销只回放历史里的令牌
async def test_undo_replays_stored_token_without_catalog(runtime, host, store):
    host.add_media(12, 1, tmdb_id=None, external_id="local-a")
    host.progress(0, 12, pos=5000)
    await configure(store)
    await runtime.sweep(reason="manual", dry_run=False)
    entry = store.data[HISTORY_KEY][0]
    assert entry["identity"] == "local-a"
    n_catalog = len(catalog_calls(host))
    done = await runtime.undo(entry["id"])
    assert done["undo_status"] == "applied"
    assert len(catalog_calls(host)) == n_catalog  # 撤销不读 catalog
    (change,) = host.state_calls()[-1]["changes"]
    assert change == {"media_item_id": 12, "kind": "movie", "identity": "local-a",
                      "expected_updated_at": entry["state_version"], "position_ms": 5000}
    assert host.states[(0, 12, 0, 0)]["position_ms"] == 5000


async def test_undo_tv_episode_with_tmdb_has_no_identity(runtime, host, store):
    host.add_media(30, 1, kind="tv")
    host.progress(0, 30, 1, 5, pos=8000)
    await configure(store)
    await runtime.sweep(reason="manual", dry_run=False)
    entry = store.data[HISTORY_KEY][0]
    done = await runtime.undo(entry["id"])
    assert done["undo_status"] == "applied"
    (change,) = host.state_calls()[-1]["changes"]
    assert "identity" not in change and change["tmdb_id"] == 1030
    assert (change["season"], change["episode"]) == (1, 5)
    assert host.states[(0, 30, 1, 5)]["position_ms"] == 8000


async def test_undo_after_id_reuse_is_mismatch(runtime, host, store):
    """清了 A；A 被删、B 复用数字 id 并有了自己的进度：撤销带 A 的令牌 → mismatch，B 不受影响。"""
    host.add_media(12, 1, tmdb_id=None, external_id="local-item-a")
    host.progress(0, 12, pos=5000)
    await configure(store)
    await runtime.sweep(reason="manual", dry_run=False)
    entry = store.data[HISTORY_KEY][0]
    host.replace_media(12, "local-item-b")
    host.progress(0, 12, pos=0)
    # 让 B 的 updated_at 恰好等于 A 的清理版本，只靠令牌拦住
    host.states[(0, 12, 0, 0)]["updated_at"] = entry["state_version"].replace("+00:00", "")
    n_catalog = len(catalog_calls(host))
    done = await runtime.undo(entry["id"])
    assert done["undo_status"] == "mismatch" and not done["undoable"]
    assert len(catalog_calls(host)) == n_catalog
    assert host.state_calls()[-1]["changes"][0]["identity"] == "local-item-a"
    assert host.states[(0, 12, 0, 0)]["position_ms"] == 0
    with pytest.raises(SkipItem):
        await runtime.undo(entry["id"])


async def test_legacy_local_history_without_token_is_not_undone(runtime, host, store):
    """0.1.1 写的本地条目记录没有令牌：不能撤销，也绝不去 catalog 取 B 的新令牌。"""
    host.add_media(12, 1, tmdb_id=None, external_id="local-item-b")  # 现在 id 12 是 B
    host.progress(0, 12, pos=0)
    legacy = {
        "id": "old000000001", "at": "2026-11-05T08:00:00+00:00", "source": "event",
        "member_id": 0, "media_item_id": 12, "kind": "movie", "tmdb_id": None, "imdb_id": None,
        "season": 0, "episode": 0, "title": "本地 A", "prior_position_ms": 5000,
        "expected_updated_at": "2026-11-05T07:59:00+00:00",
        "state_version": "2026-11-05T08:00:00+00:00",
        "undone_at": None, "undo_status": None, "undo_version": None,
    }
    await store.set(HISTORY_KEY, [legacy])
    (row,) = await runtime.history_rows()
    assert row["undoable"] is False and "令牌" in row["undo_blocked"]
    assert (await runtime.report())["undoable"] == 0
    with pytest.raises(SkipItem, match="令牌"):
        await runtime.undo(legacy["id"])
    assert host.calls == []  # 没读 catalog，也没写
    assert store.data[HISTORY_KEY][0]["undo_status"] is None  # 记录原样保留


async def test_legacy_tmdb_history_without_identity_key_still_undoes(runtime, host, store):
    host.add_media(10, 1)
    host.progress(0, 10, pos=0)
    version = host.states[(0, 10, 0, 0)]["updated_at"] + "+00:00"
    legacy = {
        "id": "old000000002", "at": "2026-11-05T08:00:00+00:00", "source": "manual",
        "member_id": 0, "media_item_id": 10, "kind": "movie", "tmdb_id": 1010, "imdb_id": None,
        "season": 0, "episode": 0, "title": "片10", "prior_position_ms": 4000,
        "expected_updated_at": None, "state_version": version,
        "undone_at": None, "undo_status": None, "undo_version": None,
    }
    await store.set(HISTORY_KEY, [legacy])
    assert (await runtime.history_rows())[0]["undoable"] is True
    done = await runtime.undo(legacy["id"])
    assert done["undo_status"] == "applied"
    assert catalog_calls(host) == []
    assert "identity" not in host.state_calls()[0]["changes"][0]
    assert host.states[(0, 10, 0, 0)]["position_ms"] == 4000


# ---------------------------------------------------------------- 过期 / 错配令牌、id 复用
async def test_stale_token_after_id_reuse_rejected_on_clear(runtime, host, store):
    host.add_media(12, 1, tmdb_id=None, external_id="local-item-a")
    host.progress(0, 12, pos=5000)
    await configure(store)
    stale = await runtime.lookup((0, 12, 0, 0))
    assert stale["media"]["identity"] == "local-item-a"
    host.replace_media(12, "local-item-b")
    host.progress(0, 12, pos=7000)
    (result,) = await runtime.clear(0, [stale], source="manual")
    assert result["status"] == "mismatch"
    assert host.states[(0, 12, 0, 0)]["position_ms"] == 7000
    assert HISTORY_KEY not in store.data
    # B 的令牌才行
    fresh = await runtime.lookup((0, 12, 0, 0))
    assert fresh["media"]["identity"] == "local-item-b"
    (result,) = await runtime.clear(0, [fresh], source="manual")
    assert result["status"] == "cleared"
    assert store.data[HISTORY_KEY][0]["identity"] == "local-item-b"


@pytest.mark.parametrize("token", [None, "", "wrong-token"])
async def test_missing_or_wrong_token_is_mismatch(runtime, host, store, token):
    host.add_media(12, 1, tmdb_id=None, external_id="local-a")
    host.progress(0, 12, pos=5000)
    await configure(store)
    row = await runtime.lookup((0, 12, 0, 0))
    if token is None:
        row["media"].pop("identity")
    else:
        row["media"]["identity"] = token
    (result,) = await runtime.clear(0, [row], source="manual")
    assert result["status"] == "mismatch"
    change = host.state_calls()[-1]["changes"][0]
    if token == "wrong-token":
        assert change["identity"] == "wrong-token"
    else:
        assert "identity" not in change
    assert host.states[(0, 12, 0, 0)]["position_ms"] == 5000
    assert HISTORY_KEY not in store.data


async def test_forged_external_id_with_token_is_mismatch(runtime, host, store):
    host.add_media(12, 1, tmdb_id=None, external_id="local-a")
    host.progress(0, 12, pos=5000)
    await configure(store)
    row = await runtime.lookup((0, 12, 0, 0))
    row["media"]["tmdb_id"] = 999
    (result,) = await runtime.clear(0, [row], source="manual")
    assert result["status"] == "mismatch"
    change = host.state_calls()[-1]["changes"][0]
    assert change["tmdb_id"] == 999 and "identity" not in change
    assert host.states[(0, 12, 0, 0)]["position_ms"] == 5000


# ---------------------------------------------------------------- CAS：只重试一次，令牌变了不改写
async def test_conflict_retry_keeps_same_token(runtime, host, store):
    host.add_media(12, 1, tmdb_id=None, external_id="local-a")
    host.progress(0, 12, pos=5000)
    await configure(store)
    host.before_state.append(lambda: host.progress(0, 12, pos=6000))
    result = await runtime.sweep(reason="manual", dry_run=False)
    assert result["counts"]["cleared"] == 1
    first, retry = host.state_calls()
    assert first["changes"][0]["identity"] == retry["changes"][0]["identity"] == "local-a"
    assert retry["changes"][0]["expected_updated_at"] != first["changes"][0]["expected_updated_at"]
    assert store.data[HISTORY_KEY][0]["prior_position_ms"] == 6000


async def test_conflict_then_id_reuse_does_not_write_with_new_token(runtime, host, store):
    host.add_media(12, 1, tmdb_id=None, external_id="local-item-a")
    host.progress(0, 12, pos=5000)
    await configure(store)
    real = host.op_playback_sync_state

    def conflict_then_replace(p):
        host.progress(0, 12, pos=5500)  # 抢先改：这次 CAS 冲突
        out = real(p)
        host.replace_media(12, "local-item-b")  # 重读之前 A 被删、B 复用了 id
        host.progress(0, 12, pos=9000)
        host.op_playback_sync_state = real
        return out

    host.op_playback_sync_state = conflict_then_replace
    result = await runtime.sweep(reason="manual", dry_run=False)
    assert len(host.state_calls()) == 1  # 没有拿 B 的令牌重试
    (item,) = result["items"]
    assert item["status"] == "mismatch" and "身份" in item["error"]
    assert result["counts"]["skipped"] == 1 and result["counts"]["cleared"] == 0
    assert host.states[(0, 12, 0, 0)]["position_ms"] == 9000
    assert HISTORY_KEY not in store.data


async def test_conflict_then_reidentified_tmdb_does_not_write(runtime, host, store):
    host.add_media(10, 1)
    host.progress(0, 10, pos=5000)
    await configure(store)
    real = host.op_playback_sync_state

    def conflict_then_rematch(p):
        host.progress(0, 10, pos=5500)
        out = real(p)
        host.replace_media(10, "tmdb:2222", tmdb_id=2222)
        host.progress(0, 10, pos=9000)
        host.op_playback_sync_state = real
        return out

    host.op_playback_sync_state = conflict_then_rematch
    result = await runtime.sweep(reason="manual", dry_run=False)
    assert len(host.state_calls()) == 1
    assert result["items"][0]["status"] == "mismatch"
    assert host.states[(0, 10, 0, 0)]["position_ms"] == 9000


async def test_local_conflict_twice_gives_up(runtime, host, store):
    host.add_media(12, 1, tmdb_id=None, external_id="local-a")
    host.progress(0, 12, pos=5000)
    await configure(store)
    real = host.op_playback_sync_state

    def always_conflict(p):
        host.progress(0, 12, pos=host.states[(0, 12, 0, 0)]["position_ms"] + 1)
        return real(p)

    host.op_playback_sync_state = always_conflict
    result = await runtime.sweep(reason="manual", dry_run=False)
    assert result["counts"]["conflicts"] == 1
    assert len(host.state_calls()) == 2
    assert all(c["changes"][0]["identity"] == "local-a" for c in host.state_calls())


# ---------------------------------------------------------------- 事件路径 / dry-run
async def test_event_path_sends_token_for_local_video(runtime, host, store):
    host.add_media(40, 3, kind="video", external_id="local-video-40")
    host.progress(0, 40, pos=90_000)
    await configure(store, library_id=3)
    assert await runtime.on_playback(event(0, 40, "video")) == "scheduled"
    await runtime.drain()
    (call,) = host.state_calls()
    assert call["changes"][0]["identity"] == "local-video-40"
    assert host.states[(0, 40, 0, 0)]["position_ms"] == 0
    assert store.data[RECENT_KEY][0]["status"] == "cleared"
    assert store.data[HISTORY_KEY][0]["identity"] == "local-video-40"


async def test_dry_run_never_writes_local_items(runtime, host, store):
    host.add_media(12, 1, tmdb_id=None, external_id="local-a")
    host.progress(0, 12, pos=5000)
    await configure(store, dry_run=True)
    settings = await runtime.settings()
    result = await runtime.sweep(reason="schedule", dry_run=settings.dry_run)
    assert result["counts"]["previewed"] == 1
    await runtime.on_playback(event(0, 12))
    await runtime.drain()
    assert host.state_calls() == []
    assert store.data[RECENT_KEY][0]["status"] == "preview"
    assert host.states[(0, 12, 0, 0)]["position_ms"] == 5000


# ---------------------------------------------------------------- 旧宿主（v0.46–0.48）
async def test_legacy_host_shape_unchanged_and_local_mismatch(runtime, host, store):
    host.legacy = True
    host.old_host = True  # catalog 也不认 kinds
    host.add_media(10, 1)
    host.add_media(12, 1, tmdb_id=None)
    host.add_media(30, 1, kind="tv")
    host.progress(0, 10, pos=5000)
    host.progress(0, 12, pos=5000)
    host.progress(0, 30, 1, 1, pos=5000)
    await configure(store)
    result = await runtime.sweep(reason="manual", dry_run=False)
    # 没有整批 422：有编号的照常清掉，无编号的照实 mismatch、不记为已清
    assert result["status"] == "ok"
    status = {i["media_item_id"]: i["status"] for i in result["items"]}
    assert status == {10: "cleared", 12: "mismatch", 30: "cleared"}
    assert result["counts"]["cleared"] == 2 and result["counts"]["skipped"] == 1
    (call,) = host.state_calls()
    assert all("identity" not in c for c in call["changes"])
    assert {h["media_item_id"] for h in store.data[HISTORY_KEY]} == {10, 30}
    assert all(h["identity"] is None for h in store.data[HISTORY_KEY])
    assert runtime._kinds_ok is False
    # 撤销也不带 identity（不会 422）
    entry = next(h for h in store.data[HISTORY_KEY] if h["media_item_id"] == 10)
    assert (await runtime.undo(entry["id"]))["undo_status"] == "applied"
    assert "identity" not in host.state_calls()[-1]["changes"][0]


async def test_legacy_host_event_local_item_is_mismatch(runtime, host, store):
    host.legacy = True
    host.add_media(40, 3, kind="video")
    host.progress(0, 40, pos=90_000)
    await configure(store, library_id=3)
    await runtime.on_playback(event(0, 40, "video"))
    await runtime.drain()
    assert "identity" not in host.state_calls()[0]["changes"][0]
    assert store.data[RECENT_KEY][0]["status"] == "mismatch"
    assert HISTORY_KEY not in store.data
    assert host.states[(0, 40, 0, 0)]["position_ms"] == 90_000


async def test_history_with_tokens_fits_store_limit(runtime, host, store):
    """令牌进了历史：120 条长令牌 + 长片名仍在插件数据单值 64KB 以内（FakeStore 会断言）。"""
    for mid in range(1, 201):
        host.add_media(mid, 1, kind="video", title="很长的片名" * 4,
                       external_id=f"{mid}:path:" + "f" * 64)
        host.progress(0, mid, pos=1000)
    await configure(store)
    await runtime.sweep(reason="manual", dry_run=False)
    assert len(store.data[HISTORY_KEY]) == 120
    assert all(h["identity"].endswith("f" * 64) for h in store.data[HISTORY_KEY])
