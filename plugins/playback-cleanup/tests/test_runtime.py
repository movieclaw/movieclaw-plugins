from __future__ import annotations

import pytest
from conftest import event

from playback_cleanup.runtime import (
    HISTORY_KEY,
    LAST_RUN_KEY,
    PENDING_KEY,
    RECENT_KEY,
    SETTINGS_KEY,
    SkipItem,
    aware_iso,
    build_change,
    seek_cursor,
)


async def configure(store, **values):
    base = {"library_id": 1, "member_ids": [0], "dry_run": False}
    base.update(values)
    await store.set(SETTINGS_KEY, base)


def seed(host):
    host.add_media(10, 1, title="在库电影")
    host.add_media(11, 1, title="在库电影二")
    host.add_media(20, 2, title="别的库的电影")
    host.add_media(30, 1, kind="tv", title="在库剧集")
    host.progress(0, 10, pos=600_000)
    host.set_state(0, 11, position_ms=0, played=True)
    host.progress(0, 20, pos=300_000)
    host.progress(0, 30, 1, 2, pos=120_000)


# ---------------------------------------------------------------- 没选择时什么都不做
async def test_no_library_is_noop(runtime, host, store):
    seed(host)
    await store.set(SETTINGS_KEY, {"member_ids": [0], "dry_run": False})
    for reason in ("schedule", "manual", "preview"):
        result = await runtime.sweep(reason=reason, dry_run=False)
        assert result["status"] == "no_selection"
    assert await runtime.on_playback(event(0, 10)) == "no_selection"
    await runtime.drain()
    assert host.calls == []
    assert LAST_RUN_KEY not in store.data
    assert (await runtime.report())["state"] == "no_selection"


async def test_fresh_install_defaults_to_nothing(runtime, host):
    seed(host)
    report = await runtime.report()
    assert report["state"] == "no_selection"
    assert report["settings"]["dry_run"] is True
    assert report["settings"]["delay_seconds"] == 45
    assert report["settings"]["sweep_enabled"] is True
    assert (await runtime.sweep(reason="schedule", dry_run=True))["status"] == "no_selection"
    assert host.calls == []


async def test_no_members_is_noop(runtime, host, store):
    seed(host)
    await configure(store, member_ids=[])
    assert (await runtime.sweep(reason="manual", dry_run=False))["status"] == "no_members"
    assert await runtime.on_playback(event(0, 10)) == "no_members"
    assert host.state_calls() == []


# ---------------------------------------------------------------- 扫描
async def test_sweep_filters_by_library_and_position(runtime, host, store):
    seed(host)
    await configure(store)
    result = await runtime.sweep(reason="preview", dry_run=True)
    titles = sorted(i["title"] for i in result["items"])
    assert titles == ["在库剧集 S01E02", "在库电影"]
    assert result["counts"]["previewed"] == 2
    assert host.state_calls() == []  # 只预览不写


async def test_dry_run_setting_never_writes_from_schedule(runtime, host, store):
    seed(host)
    await configure(store, dry_run=True)
    settings = await runtime.settings()
    result = await runtime.sweep(reason="schedule", dry_run=settings.dry_run)
    assert result["counts"]["previewed"] == 2
    assert host.state_calls() == []


async def test_real_sweep_sends_only_position(runtime, host, store):
    seed(host)
    await configure(store)
    result = await runtime.sweep(reason="manual", dry_run=False)
    assert result["counts"]["cleared"] == 2
    (call,) = host.state_calls()
    assert call["member_id"] == 0
    for change in call["changes"]:
        assert change["position_ms"] == 0
        for forbidden in ("played", "is_favorite", "last_played_at"):
            assert forbidden not in change
        assert change["expected_updated_at"].endswith("+00:00")
    tv = next(c for c in call["changes"] if c["kind"] == "tv")
    assert (tv["season"], tv["episode"]) == (1, 2)
    movie = next(c for c in call["changes"] if c["kind"] == "movie")
    assert "season" not in movie and "episode" not in movie
    # 只清了在库的；别的库、已看的都没动
    assert host.states[(0, 10, 0, 0)]["position_ms"] == 0
    assert host.states[(0, 30, 1, 2)]["position_ms"] == 0
    assert host.states[(0, 20, 0, 0)]["position_ms"] == 300_000
    assert host.states[(0, 11, 0, 0)]["played"] is True
    assert host.states[(0, 10, 0, 0)]["last_played_at"] == "2026-10-31T20:00:00"
    history = store.data[HISTORY_KEY]
    assert {h["prior_position_ms"] for h in history} == {600_000, 120_000}


async def test_conflict_retries_once_with_fresh_version(runtime, host, store):
    seed(host)
    await configure(store)
    # 写入前客户端又报了一次进度：第一次 CAS 冲突，重读后用新版本重试
    host.before_state.append(lambda: host.progress(0, 10, pos=650_000))
    result = await runtime.sweep(reason="manual", dry_run=False)
    assert result["counts"]["cleared"] == 2
    calls = host.state_calls()
    assert len(calls) == 2
    first = next(c for c in calls[0]["changes"] if c["media_item_id"] == 10)
    (retry,) = calls[1]["changes"]
    assert retry["media_item_id"] == 10
    assert retry["expected_updated_at"] != first["expected_updated_at"]
    assert host.states[(0, 10, 0, 0)]["position_ms"] == 0
    entry = next(h for h in store.data[HISTORY_KEY] if h["media_item_id"] == 10)
    assert entry["prior_position_ms"] == 650_000


async def test_conflict_twice_gives_up(runtime, host, store):
    host.add_media(10, 1)
    host.progress(0, 10, pos=5000)
    await configure(store)
    host.before_state.append(lambda: host.progress(0, 10, pos=6000))
    real = host.op_playback_sync_state

    def flaky(p):
        host.progress(0, 10, pos=7000)  # 每次都抢先改：重试也冲突
        return real(p)

    host.op_playback_sync_state = flaky
    result = await runtime.sweep(reason="manual", dry_run=False)
    assert result["counts"]["conflicts"] == 1
    assert len(host.state_calls()) == 2  # 只重试一次
    assert host.states[(0, 10, 0, 0)]["position_ms"] == 7000


async def test_schedule_noop_when_sweep_off_or_disabled(runtime, host, store):
    seed(host)
    await configure(store, sweep_enabled=False)
    assert (await runtime.sweep(reason="schedule", dry_run=False))["status"] == "sweep_off"
    await configure(store, enabled=False)
    assert (await runtime.sweep(reason="schedule", dry_run=False))["status"] == "disabled"
    assert host.calls == []
    # 手动「立即清理一次」不看启用开关
    assert (await runtime.sweep(reason="manual", dry_run=False))["counts"]["cleared"] == 2


async def test_local_movie_without_external_ids_is_cleared(runtime, host, store):
    host.add_media(10, 1, tmdb_id=None, title="本地电影")
    host.progress(0, 10, pos=5000)
    await configure(store)
    result = await runtime.sweep(reason="manual", dry_run=False)
    assert result["items"][0]["status"] == "cleared"
    assert host.states[(0, 10, 0, 0)]["position_ms"] == 0
    (call,) = host.state_calls()
    change = call["changes"][0]
    assert change["kind"] == "movie" and "tmdb_id" not in change and "imdb_id" not in change
    assert change["identity"] == "local-movie-10"  # #1203：回传 catalog 下发的令牌
    # 撤销同样不带编号，回放记录里的令牌
    entry = store.data[HISTORY_KEY][0]
    assert entry["tmdb_id"] is None and entry["identity"] == "local-movie-10"
    done = await runtime.undo(entry["id"])
    assert done["undo_status"] == "applied"
    assert host.states[(0, 10, 0, 0)]["position_ms"] == 5000


async def test_local_video_without_ids_is_cleared(runtime, host, store):
    host.add_media(40, 3, kind="video", title="家庭录像")
    host.progress(0, 40, pos=90_000)
    await configure(store, library_id=3)
    # 不带 kinds 的 catalog（旧宿主 / 默认）不含 video；插件总是带上
    assert host.op_playback_sync_catalog({"member_id": 0})["items"] == []
    result = await runtime.sweep(reason="manual", dry_run=False)
    assert result["counts"]["cleared"] == 1
    assert result["items"][0]["title"] == "家庭录像"
    assert host.states[(0, 40, 0, 0)]["position_ms"] == 0
    catalogs = [p for n, p in host.calls if n == "playback.sync.catalog"]
    assert catalogs and all(p["kinds"] == ["movie", "tv", "video"] for p in catalogs)
    change = host.state_calls()[0]["changes"][0]
    assert change["kind"] == "video" and "season" not in change and "tmdb_id" not in change


async def test_old_host_without_kinds_falls_back(runtime, host, store):
    host.old_host = True
    host.add_media(10, 1)
    host.add_media(40, 1, kind="video")
    host.progress(0, 10, pos=5000)
    host.progress(0, 40, pos=5000)
    await configure(store)
    result = await runtime.sweep(reason="manual", dry_run=False)
    assert [i["media_item_id"] for i in result["items"]] == [10]
    assert result["counts"]["cleared"] == 1 and result["status"] == "ok"
    catalogs = [p for n, p in host.calls if n == "playback.sync.catalog"]
    assert "kinds" in catalogs[0] and all("kinds" not in p for p in catalogs[1:])
    # 记住了：之后的 lookup 不再先试 kinds
    n = len(host.calls)
    assert (await runtime.lookup((0, 10, 0, 0)))["position_ms"] == 0
    assert len(host.calls) == n + 1 and "kinds" not in host.calls[-1][1]


async def test_old_host_local_null_write_is_mismatch(runtime, host, store):
    """旧宿主对无编号条目的写回回 mismatch：照实显示，不当成清掉了。"""
    host.add_media(10, 1, tmdb_id=None)
    host.progress(0, 10, pos=5000)
    # v0.46–0.48：catalog 不给 identity、写回只认吻合的 TMDB / IMDb 编号、多送 identity 整批 422
    host.legacy = True
    await configure(store)
    result = await runtime.sweep(reason="manual", dry_run=False)
    assert result["items"][0]["status"] == "mismatch"
    assert result["counts"]["skipped"] == 1 and HISTORY_KEY not in store.data
    assert "identity" not in host.state_calls()[0]["changes"][0]


async def test_item_with_tmdb_still_sends_it(runtime, host, store):
    host.add_media(10, 1)
    host.progress(0, 10, pos=5000)
    await configure(store)
    result = await runtime.sweep(reason="manual", dry_run=False)
    assert result["items"][0]["status"] == "cleared"
    assert host.state_calls()[0]["changes"][0]["tmdb_id"] == 1010


async def test_forged_tmdb_on_local_item_is_mismatch(runtime, host, store):
    host.add_media(10, 1, tmdb_id=None)
    host.progress(0, 10, pos=5000)
    await configure(store)
    row = await runtime.lookup((0, 10, 0, 0))
    row["media"]["tmdb_id"] = 999  # 过期映射 / 伪造的编号
    (result,) = await runtime.clear(0, [row], source="manual")
    assert result["status"] == "mismatch"
    assert host.states[(0, 10, 0, 0)]["position_ms"] == 5000


async def test_item_with_tmdb_requires_it(host):
    host.add_media(10, 1)
    host.progress(0, 10, pos=5000)
    st = host.states[(0, 10, 0, 0)]
    expected = st["updated_at"] + "+00:00"
    out = host.op_playback_sync_state(
        {"member_id": 0, "changes": [{"media_item_id": 10, "kind": "movie",
                                      "expected_updated_at": expected, "position_ms": 0}]}
    )
    assert out["outcomes"][0]["status"] == "mismatch"


async def test_multiple_members(runtime, host, store):
    host.add_media(10, 1)
    host.progress(0, 10, pos=5000)
    host.progress(7, 10, pos=8000)
    host.progress(8, 10, pos=9000)  # 没选
    await configure(store, member_ids=[0, 7])
    result = await runtime.sweep(reason="manual", dry_run=False)
    assert result["counts"]["cleared"] == 2
    assert host.states[(8, 10, 0, 0)]["position_ms"] == 9000


async def test_catalog_paginates(runtime, host, store):
    for mid in range(1, 251):
        host.add_media(mid, 1)
    host.progress(0, 249, pos=1000)
    await configure(store)
    result = await runtime.sweep(reason="preview", dry_run=True)
    assert [i["media_item_id"] for i in result["items"]] == [249]
    pages = [p for n, p in host.calls if n == "playback.sync.catalog"]
    assert len(pages) == 3


# ---------------------------------------------------------------- 事件路径
async def test_event_ignores_other_library_and_member(runtime, host, store):
    seed(host)
    await configure(store)
    assert await runtime.on_playback(event(0, 20)) == "other_library"
    assert await runtime.on_playback(event(7, 10)) == "other_member"
    await runtime.drain()
    assert host.state_calls() == []


async def test_event_schedules_for_video(runtime, host, store):
    host.add_media(40, 3, kind="video")
    host.progress(0, 40, pos=90_000)
    await configure(store, library_id=3)
    assert await runtime.on_playback(event(0, 40, "video")) == "scheduled"
    await runtime.drain()
    assert host.states[(0, 40, 0, 0)]["position_ms"] == 0
    assert store.data[RECENT_KEY][0]["status"] == "cleared"
    assert await runtime.on_playback(event(0, 40, "music")) == "other_kind"


async def test_event_clears_after_delay(runtime, host, store):
    seed(host)
    await configure(store, delay_seconds=30)
    slept = []

    async def sleep(seconds):
        slept.append(seconds)

    runtime.sleep = sleep
    assert await runtime.on_playback(event(0, 30, "tv", 1, 2)) == "scheduled"
    assert store.data[PENDING_KEY]
    await runtime.drain()
    assert slept == [30]
    assert host.states[(0, 30, 1, 2)]["position_ms"] == 0
    (call,) = host.state_calls()
    assert call["changes"][0]["position_ms"] == 0 and "played" not in call["changes"][0]
    assert store.data[PENDING_KEY] == {}
    assert store.data[RECENT_KEY][0]["status"] == "cleared"
    assert store.data[RECENT_KEY][0]["source"] == "event"


async def test_event_late_progress_conflict_then_zero(runtime, host, store):
    seed(host)
    await configure(store)
    # 延迟期间客户端最后一次上报落地；写入瞬间又来一次（冲突）→ 重读重试 → 最终为 0
    await runtime.on_playback(event(0, 10))
    host.progress(0, 10, pos=610_000)
    host.before_state.append(lambda: host.progress(0, 10, pos=620_000))
    await runtime.drain()
    assert host.states[(0, 10, 0, 0)]["position_ms"] == 0
    assert len(host.state_calls()) == 2
    assert store.data[HISTORY_KEY][0]["prior_position_ms"] == 620_000


async def test_event_noop_when_already_zero(runtime, host, store):
    seed(host)
    await configure(store)
    await runtime.on_playback(event(0, 11))  # 已看，position 0
    await runtime.drain()
    assert host.state_calls() == []


async def test_event_dry_run_only_records(runtime, host, store):
    seed(host)
    await configure(store, dry_run=True)
    await runtime.on_playback(event(0, 10))
    await runtime.drain()
    assert host.state_calls() == []
    assert store.data[RECENT_KEY][0]["status"] == "preview"


async def test_event_disabled_is_noop(runtime, host, store):
    seed(host)
    await configure(store, enabled=False)
    assert await runtime.on_playback(event(0, 10)) == "disabled"
    assert host.calls == []


async def test_events_coalesce(runtime, host, store):
    seed(host)
    await configure(store)
    import asyncio

    gate = asyncio.Event()

    async def sleep(_s):
        await gate.wait()

    runtime.sleep = sleep
    await runtime.on_playback(event(0, 10))  # stopped
    await runtime.on_playback(event(0, 10))  # completed：重新计时，只留一个
    assert len(runtime._timers) == 1
    gate.set()
    await runtime.drain()
    assert len(host.state_calls()) == 1


async def test_event_rechecks_settings_when_timer_fires(runtime, host, store):
    seed(host)
    await configure(store)
    import asyncio

    gate = asyncio.Event()

    async def sleep(_s):
        await gate.wait()

    runtime.sleep = sleep
    await runtime.on_playback(event(0, 10))
    await configure(store, member_ids=[7])  # 延迟期间取消了这个成员
    gate.set()
    await runtime.drain()
    assert host.state_calls() == []


async def test_new_item_refreshes_library_cache(runtime, host, store):
    seed(host)
    await configure(store)
    await runtime.library_titles(1)
    host.add_media(12, 1, title="刚入库")
    host.progress(0, 12, pos=1000)
    runtime.clock.now += 60
    assert await runtime.on_playback(event(0, 12)) == "scheduled"
    await runtime.drain()
    assert host.states[(0, 12, 0, 0)]["position_ms"] == 0


async def test_restore_pending_after_restart(host, store):
    from conftest import Clock, instant

    from playback_cleanup.runtime import Runtime

    seed(host)
    await configure(store)
    await store.set(PENDING_KEY, {"0:10:0:0": 0})
    rt = Runtime(ops=host, store=store, clock=Clock(), sleep=instant)
    assert await rt.restore_pending() == 1
    await rt.drain()
    assert host.states[(0, 10, 0, 0)]["position_ms"] == 0


# ---------------------------------------------------------------- 撤销
async def test_undo_restores_prior_position(runtime, host, store):
    seed(host)
    await configure(store)
    await runtime.sweep(reason="manual", dry_run=False)
    entry = next(h for h in await runtime.history_rows() if h["media_item_id"] == 10)
    assert entry["undoable"]
    done = await runtime.undo(entry["id"])
    assert done["undo_status"] == "applied" and not done["undoable"]
    undo_call = host.state_calls()[-1]
    (change,) = undo_call["changes"]
    assert change == {
        "media_item_id": 10,
        "kind": "movie",
        "tmdb_id": 1010,
        "expected_updated_at": entry["state_version"],
        "position_ms": 600_000,
    }
    assert host.states[(0, 10, 0, 0)]["position_ms"] == 600_000
    with pytest.raises(SkipItem):
        await runtime.undo(entry["id"])
    # 撤销后没再播：兜底扫描不会又把它清掉
    result = await runtime.sweep(reason="schedule", dry_run=False)
    assert result["counts"]["cleared"] == 0
    assert host.states[(0, 10, 0, 0)]["position_ms"] == 600_000
    # 用户又播了：恢复正常清理
    host.progress(0, 10, pos=700_000)
    result = await runtime.sweep(reason="schedule", dry_run=False)
    assert result["counts"]["cleared"] == 1


async def test_undo_conflict_when_played_again(runtime, host, store):
    seed(host)
    await configure(store)
    await runtime.sweep(reason="manual", dry_run=False)
    entry = next(h for h in await runtime.history_rows() if h["media_item_id"] == 10)
    host.progress(0, 10, pos=1234)
    done = await runtime.undo(entry["id"])
    assert done["undo_status"] == "conflict"
    assert host.states[(0, 10, 0, 0)]["position_ms"] == 1234


# ---------------------------------------------------------------- 设置与工具函数
async def test_save_settings_validates(runtime, host, store):
    seed(host)
    choices = await runtime.save_settings({"library_id": 2, "member_ids": [0, 7], "delay_seconds": 60})
    assert choices["settings"]["library_id"] == 2
    assert [m["id"] for m in choices["members"]] == [0, 7]
    assert choices["members"][0]["name"] == "超管"
    video = next(lib for lib in choices["libraries"] if lib["kind"] == "video")
    assert video["eligible"] and video["reason"] is None
    host.libraries.append({"id": 4, "name": "音乐", "kind": "music", "source": "local"})
    music = next(lib for lib in (await runtime.choices())["libraries"] if lib["kind"] == "music")
    assert not music["eligible"] and music["reason"]
    with pytest.raises(SkipItem):
        await runtime.save_settings({"library_id": 99})
    with pytest.raises(SkipItem):
        await runtime.save_settings({"library_id": 1, "member_ids": [42]})
    with pytest.raises(SkipItem):
        await runtime.save_settings({"library_id": 1, "delay_seconds": 2})
    with pytest.raises(SkipItem):
        await runtime.save_settings({"library_id": 1, "delay_seconds": 301})


async def test_unselecting_cancels_pending(runtime, host, store):
    seed(host)
    await configure(store)
    import asyncio

    runtime.sleep = lambda _s: asyncio.Event().wait()
    await runtime.on_playback(event(0, 10))
    await runtime.save_settings({"library_id": None, "member_ids": [0]})
    assert runtime._timers == {} and store.data[PENDING_KEY] == {}


async def test_manual_start_sweep_runs_in_background(runtime, host, store):
    seed(host)
    await configure(store)
    assert runtime.start_sweep(reason="manual", dry_run=False)
    assert not runtime.start_sweep(reason="manual", dry_run=False)  # 正在跑
    await runtime._sweep
    assert store.data[LAST_RUN_KEY]["counts"]["cleared"] == 2
    assert runtime.running() is None


def test_seek_cursor_lands_before_unit():
    assert seek_cursor(10, 1, 2) == "10:1:1"
    assert seek_cursor(10, 0, 0).startswith("9:")
    assert seek_cursor(10, 2, 0).startswith("10:1:")


def test_aware_iso():
    assert aware_iso("2026-11-01T08:00:00") == "2026-11-01T08:00:00+00:00"
    assert aware_iso("2026-11-01T16:00:00+08:00") == "2026-11-01T08:00:00+00:00"
    assert aware_iso("2026-11-01T08:00:00Z") == "2026-11-01T08:00:00+00:00"
    assert aware_iso(None) is None


def test_build_change_shape():
    row = {"media": {"id": 3, "kind": "tv", "tmdb_id": 9, "imdb_id": "tt1"}, "season": 1, "episode": 4}
    assert build_change(row, 0, "x") == {
        "media_item_id": 3, "kind": "tv", "tmdb_id": 9, "imdb_id": "tt1",
        "season": 1, "episode": 4, "expected_updated_at": "x", "position_ms": 0,
    }


async def test_history_is_capped(runtime, host, store):
    for mid in range(1, 201):
        host.add_media(mid, 1, title="很长的片名" * 4)
        host.progress(0, mid, pos=1000)
    await configure(store)
    await runtime.sweep(reason="manual", dry_run=False)
    assert len(store.data[HISTORY_KEY]) == 120
