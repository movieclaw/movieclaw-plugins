"""0.1.3：清理时一并从「接下来继续」隐藏（``dismiss_continue`` / ``continue_dismissed``）。

FakeHost 的 ``dismiss=True`` 模拟配套新宿主；默认（False）= 现有 0.49.1：catalog 没有
``continue_dismissed``，sync.state 多送 ``dismiss_continue`` 整批 422。真宿主行为见
``test_host_e2e_local.py``。
"""

from __future__ import annotations

import pytest
from conftest import event

from playback_cleanup.runtime import (
    HISTORY_KEY,
    NO_DISMISS_NOTE,
    SETTINGS_KEY,
    build_change,
    needs_work,
)


async def configure(store, **values):
    base = {"library_id": 1, "member_ids": [0], "dry_run": False}
    base.update(values)
    await store.set(SETTINGS_KEY, base)


def all_changes(host):
    return [c for call in host.state_calls() for c in call["changes"]]


def seed(host):
    host.add_media(10, 1, title="电影")
    host.add_media(11, 1, kind="tv", title="剧")
    host.add_media(12, 1, kind="video", title="本地视频")
    host.progress(0, 10, pos=600_000)
    host.progress(0, 11, 1, 2, pos=300_000)
    host.progress(0, 12, pos=90_000)


# ---------------------------------------------------------------- 形状
def test_build_change_only_adds_dismiss_when_asked():
    row = {"media": {"id": 1, "kind": "movie", "tmdb_id": 5}, "season": 0, "episode": 0}
    assert "dismiss_continue" not in build_change(row, 0, None)
    assert build_change(row, 0, None, dismiss=True)["dismiss_continue"] is True
    assert build_change(row, 0, None, dismiss=False)["dismiss_continue"] is False


def test_needs_work_rules():
    base = {"position_ms": 0, "played": False, "last_played_at": "2026-11-01T00:00:00"}
    assert needs_work({**base, "position_ms": 5})
    assert not needs_work(base)  # 旧宿主：进度 0 没有能写的
    assert needs_work({**base, "continue_dismissed": False})
    assert not needs_work({**base, "continue_dismissed": True})
    assert not needs_work({**base, "continue_dismissed": False, "played": True})
    assert not needs_work({**base, "continue_dismissed": False, "last_played_at": None})


# ---------------------------------------------------------------- 新宿主
async def test_sweep_clears_and_dismisses_movie_tv_video(runtime, host, store):
    host.dismiss = True
    seed(host)
    await configure(store)
    run = await runtime.sweep(reason="manual", dry_run=False)
    assert run["counts"]["cleared"] == 3
    assert NO_DISMISS_NOTE not in run["notes"]
    changes = all_changes(host)
    assert {(c["media_item_id"], c["position_ms"], c["dismiss_continue"]) for c in changes} == {
        (10, 0, True),
        (11, 0, True),
        (12, 0, True),
    }
    # video 本地条目仍带令牌、不带编号
    (video,) = [c for c in changes if c["media_item_id"] == 12]
    assert video["identity"] == "local-video-12" and "tmdb_id" not in video
    for key in [(0, 10, 0, 0), (0, 11, 1, 2), (0, 12, 0, 0)]:
        assert host.states[key]["position_ms"] == 0
        assert host.states[key]["continue_dismissed"] is True
        assert host.states[key]["played"] is False  # 不伪装已看
    history = store.data[HISTORY_KEY]
    assert all(h["dismissed"] is True and h["prior_dismissed"] is False for h in history)
    assert all("warning" not in i for i in run["items"])


async def test_zero_position_not_dismissed_is_still_handled(runtime, host, store):
    """0.1.2 清过（进度 0、没看完、播过、未隐藏）：补上隐藏；看完 / 没播过 / 已隐藏的不动。"""
    host.dismiss = True
    host.add_media(10, 1, title="0.1.2 清过的")
    host.add_media(11, 1, title="看完了")
    host.add_media(12, 1, title="已隐藏")
    host.add_media(13, 1, title="没播过")
    host.set_state(0, 10, position_ms=0)
    host.set_state(0, 11, position_ms=0, played=True)
    host.set_state(0, 12, position_ms=0, continue_dismissed=True)
    await configure(store)
    run = await runtime.sweep(reason="manual", dry_run=False)
    assert run["counts"]["cleared"] == 1
    (change,) = all_changes(host)
    assert (change["media_item_id"], change["position_ms"], change["dismiss_continue"]) == (
        10,
        0,
        True,
    )
    assert host.states[(0, 10, 0, 0)]["continue_dismissed"] is True
    (entry,) = store.data[HISTORY_KEY]
    assert entry["prior_position_ms"] == 0 and entry["prior_dismissed"] is False
    assert runtime.undoable(entry)  # 只隐藏的记录也能撤销


async def test_event_path_dismisses_zero_position(runtime, host, store):
    host.dismiss = True
    host.add_media(10, 1)
    host.set_state(0, 10, position_ms=0)  # 停在开头
    await configure(store)
    assert await runtime.on_playback(event(0, 10)) == "scheduled"
    await runtime.drain()
    (change,) = all_changes(host)
    assert change["dismiss_continue"] is True
    assert host.states[(0, 10, 0, 0)]["continue_dismissed"] is True


async def test_undo_restores_real_prior_dismiss_value(runtime, host, store):
    host.dismiss = True
    host.add_media(10, 1)
    host.add_media(11, 1)
    host.progress(0, 10, pos=600_000)
    host.progress(0, 11, pos=300_000)
    host.states[(0, 11, 0, 0)]["continue_dismissed"] = True  # 之前就隐藏着
    await configure(store)
    await runtime.sweep(reason="manual", dry_run=False)
    by_mid = {h["media_item_id"]: h for h in store.data[HISTORY_KEY]}
    assert by_mid[10]["prior_dismissed"] is False and by_mid[11]["prior_dismissed"] is True
    host.calls.clear()
    assert (await runtime.undo(by_mid[10]["id"]))["undo_status"] == "applied"
    assert (await runtime.undo(by_mid[11]["id"]))["undo_status"] == "applied"
    sent = {c["media_item_id"]: c for c in all_changes(host)}
    assert (sent[10]["position_ms"], sent[10]["dismiss_continue"]) == (600_000, False)
    assert (sent[11]["position_ms"], sent[11]["dismiss_continue"]) == (300_000, True)
    assert host.states[(0, 10, 0, 0)]["continue_dismissed"] is False
    assert host.states[(0, 11, 0, 0)]["continue_dismissed"] is True


async def test_undo_after_replay_is_rejected_and_guard_stops_redismiss(runtime, host, store):
    host.dismiss = True
    host.add_media(10, 1)
    host.add_media(11, 1)
    host.set_state(0, 10, position_ms=0)
    host.progress(0, 11, pos=300_000)
    await configure(store)
    await runtime.sweep(reason="manual", dry_run=False)
    by_mid = {h["media_item_id"]: h for h in store.data[HISTORY_KEY]}
    # 11 之后又播过：撤销冲突，不写
    host.progress(0, 11, pos=400_000)
    before = dict(host.states[(0, 11, 0, 0)])
    assert (await runtime.undo(by_mid[11]["id"]))["undo_status"] == "conflict"
    assert host.states[(0, 11, 0, 0)] == before
    # 10 撤销（只隐藏的记录）：恢复未隐藏；下一次扫描不再把它隐藏回去
    assert (await runtime.undo(by_mid[10]["id"]))["undo_status"] == "applied"
    assert host.states[(0, 10, 0, 0)]["continue_dismissed"] is False
    host.calls.clear()
    run = await runtime.sweep(reason="manual", dry_run=False)
    assert 10 not in {c["media_item_id"] for c in all_changes(host)}
    assert run["counts"]["skipped"] >= 1


async def test_old_history_entries_never_send_dismiss(runtime, host, store):
    """0.1.2 写的记录（没有 dismissed 键）在新宿主上撤销，也不带 dismiss_continue。"""
    host.dismiss = True
    host.add_media(10, 1)
    host.progress(0, 10, pos=0)
    version = host.states[(0, 10, 0, 0)]["updated_at"] + "+00:00"
    await store.set(
        HISTORY_KEY,
        [
            {"id": "old1", "member_id": 0, "media_item_id": 10, "kind": "movie",
             "tmdb_id": 1010, "imdb_id": None, "identity": None, "season": 0, "episode": 0,
             "title": "片10", "prior_position_ms": 500_000, "state_version": version,
             "undo_status": None}
        ],
    )
    assert (await runtime.undo("old1"))["undo_status"] == "applied"
    (change,) = all_changes(host)
    assert "dismiss_continue" not in change and change["position_ms"] == 500_000


async def test_cas_retry_once_keeps_dismiss_and_identity_change_stops(runtime, host, store):
    host.dismiss = True
    host.add_media(10, 1)
    host.add_media(11, 1, kind="video", external_id="A")
    host.progress(0, 10, pos=600_000)
    host.progress(0, 11, pos=90_000)
    await configure(store)
    host.before_state.append(lambda: host.progress(0, 10, pos=650_000))
    host.before_state.append(lambda: host.replace_media(11, "B", kind="video") or host.progress(0, 11, pos=5))
    run = await runtime.sweep(reason="manual", dry_run=False)
    statuses = {i["media_item_id"]: i["status"] for i in run["items"]}
    assert statuses == {10: "cleared", 11: "mismatch"}
    calls = host.state_calls()
    assert len(calls) == 2  # 第一次 + 一次重试
    (retry,) = calls[1]["changes"]
    assert (retry["media_item_id"], retry["dismiss_continue"]) == (10, True)
    assert host.states[(0, 11, 0, 0)]["position_ms"] == 5  # B 没被动


async def test_cas_second_conflict_gives_up(runtime, host, store):
    host.dismiss = True
    host.add_media(10, 1)
    host.progress(0, 10, pos=600_000)
    await configure(store)
    orig = host.op_playback_sync_state

    def always_bump(p):
        host.progress(0, 10, pos=700_000)
        return orig(p)

    host.op_playback_sync_state = always_bump
    run = await runtime.sweep(reason="manual", dry_run=False)
    assert run["counts"]["conflicts"] == 1
    assert len(host.state_calls()) == 2
    assert host.states[(0, 10, 0, 0)]["continue_dismissed"] is False


@pytest.mark.parametrize("dismiss", [True, False])
async def test_dry_run_and_no_selection_never_write(runtime, host, store, dismiss):
    host.dismiss = dismiss
    seed(host)
    host.add_media(13, 1)
    host.set_state(0, 13, position_ms=0)
    await configure(store, dry_run=True)
    run = await runtime.sweep(reason="schedule", dry_run=True)
    assert run["counts"]["previewed"] == (4 if dismiss else 3)
    assert all(i["dismiss_supported"] is dismiss for i in run["items"])
    assert not host.state_calls()
    await runtime.on_playback(event(0, 13))
    await runtime.drain()
    assert not host.state_calls()
    await store.set(SETTINGS_KEY, {"library_id": None, "member_ids": [0], "dry_run": False})
    assert (await runtime.sweep(reason="manual", dry_run=False))["status"] == "no_selection"
    await store.set(SETTINGS_KEY, {"library_id": 1, "member_ids": [], "dry_run": False})
    assert (await runtime.sweep(reason="manual", dry_run=False))["status"] == "no_members"
    assert not host.state_calls()


# ---------------------------------------------------------------- 现有宿主（0.49.1，没有隐藏能力）
async def test_current_host_only_clears_position_and_warns(runtime, host, store):
    seed(host)
    host.add_media(13, 1)
    host.set_state(0, 13, position_ms=0)  # 0.1.2 清过的：旧宿主上没有能写的
    await configure(store)
    run = await runtime.sweep(reason="manual", dry_run=False)
    assert run["counts"]["cleared"] == 3
    assert NO_DISMISS_NOTE in run["notes"]
    assert all(i.get("warning") == NO_DISMISS_NOTE for i in run["items"] if i["status"] == "cleared")
    changes = all_changes(host)
    assert all("dismiss_continue" not in c for c in changes)
    assert 13 not in {c["media_item_id"] for c in changes}
    # 请求形状与 0.1.2 一致
    assert {tuple(sorted(c)) for c in changes} == {
        ("expected_updated_at", "kind", "media_item_id", "position_ms", "tmdb_id"),
        ("episode", "expected_updated_at", "kind", "media_item_id", "position_ms", "season", "tmdb_id"),
        ("expected_updated_at", "identity", "kind", "media_item_id", "position_ms"),
    }
    history = store.data[HISTORY_KEY]
    assert all("dismissed" not in h and "prior_dismissed" not in h for h in history)
    host.calls.clear()
    await runtime.undo(history[0]["id"])
    assert all("dismiss_continue" not in c for c in all_changes(host))


async def test_current_host_event_path_warns(runtime, host, store):
    host.add_media(10, 1)
    host.progress(0, 10, pos=600_000)
    await configure(store)
    await runtime.on_playback(event(0, 10))
    await runtime.drain()
    recent = store.data["recent"][0]
    assert recent["status"] == "cleared" and recent["warning"] == NO_DISMISS_NOTE
    assert "dismiss_continue" not in all_changes(host)[0]


async def test_catalog_request_shape_unchanged(runtime, host, store):
    for dismiss in (False, True):
        host.dismiss = dismiss
        seed(host)
        await configure(store)
        await runtime.sweep(reason="manual", dry_run=True)
        await runtime.lookup((0, 10, 0, 0))
        for name, params in host.calls:
            if name == "playback.sync.catalog":
                assert set(params) <= {"member_id", "limit", "cursor", "kinds"}
        host.calls.clear()
