"""MovieClaw#1246：「接下来继续」隐藏是作品级意图，只能落在作品当前锚点行上。

FakeHost（``dismiss=True``）按宿主规则：``dismiss_continue=true`` 写到非锚点行 → 整条 conflict 回滚
（``position_ms`` 也不写），记在 ``host.anchor_conflicts``；比隐藏新的播放事实按作品恢复。
"""

from __future__ import annotations

from conftest import event

from playback_cleanup.runtime import (
    DISMISS_CONFLICT_NOTE,
    HISTORY_KEY,
    NO_DISMISS_NOTE,
    NOT_ANCHOR_NOTE,
    SETTINGS_KEY,
    anchor_of,
    aware_iso,
    build_change,
    decide,
)

TV = 30


async def configure(store, **values):
    base = {"library_id": 1, "member_ids": [0], "dry_run": False}
    base.update(values)
    await store.set(SETTINGS_KEY, base)


def all_changes(host):
    return [c for call in host.state_calls() for c in call["changes"]]


def by_ep(changes):
    return {(c["season"], c["episode"]): c for c in changes if c["media_item_id"] == TV}


def show(host, *, e1=200_000, e2=0, e3=300_000):
    """E1、E2、E3 都播过，E3 最近（当前集 = 锚点）。"""
    host.dismiss = True
    host.add_media(TV, 1, kind="tv", title="剧")
    host.progress(0, TV, 1, 1, pos=e1, at="2026-11-01T01:00:00")
    host.progress(0, TV, 1, 2, pos=e2, at="2026-11-01T02:00:00")
    host.progress(0, TV, 1, 3, pos=e3, at="2026-11-01T03:00:00")


# ---------------------------------------------------------------- 纯函数
def test_anchor_of_matches_host_order():
    def r(s, e, at):
        return {"season": s, "episode": e, "last_played_at": at}

    rows = [r(1, 1, "2026-11-01T03:00:00"), r(1, 2, "2026-11-01T02:00:00.5"), r(-1, -1, "2027-01-01")]
    assert anchor_of(rows) is rows[0]  # 哨兵行排除；带不带微秒按时间比，不按字符串
    tie = [r(1, 1, "2026-11-01T03:00:00"), r(1, 2, "2026-11-01T03:00:00+00:00")]
    assert anchor_of(tie) is tie[1]  # 平局：(季, 集) 大者
    never = [r(1, 1, None), r(1, 2, None)]
    assert anchor_of(never) is never[1]
    assert anchor_of([r(1, 1, None), r(1, 2, "2026-11-01")])["episode"] == 2


def test_decide_per_row():
    def r(e, pos=0, at="2026-11-01T0%d:00:00", played=False, hidden=False):
        return {"season": 1, "episode": e, "position_ms": pos, "played": played,
                "last_played_at": at % e if at else None, "continue_dismissed": hidden}

    title = [r(1, 5), r(2), r(3, 7)]
    assert decide(title[0], title) == (True, None)  # 旧集有进度：只清进度
    assert decide(title[1], title) == (False, None)  # 旧集进度 0：不动
    assert decide(title[2], title) == (True, True)  # 锚点：清进度 + 隐藏
    zero = [r(1, 5), r(2)]
    assert decide(zero[1], zero) == (True, True)  # 锚点进度 0、没看完、未隐藏：补隐藏
    hidden = [r(1, 5), r(2, hidden=True)]
    assert decide(hidden[1], hidden) == (False, None)
    assert decide(hidden[0], hidden) == (True, None)
    watched = [r(1), r(2, played=True)]
    assert decide(watched[0], watched) == (False, None) and decide(watched[1], watched) == (False, None)
    legacy = [{k: v for k, v in x.items() if k != "continue_dismissed"} for x in title]
    assert [decide(x, legacy) for x in legacy] == [(True, None), (False, None), (True, None)]


# ---------------------------------------------------------------- 替身本身：旧写法确实会冲突
async def test_fake_host_rejects_dismiss_on_non_anchor_and_rolls_back(runtime, host):
    show(host)
    row = await runtime.lookup((0, TV, 1, 1))
    change = build_change(row, 0, aware_iso(row["updated_at"]), dismiss=True)  # 0.1.3 旧写法
    out = await host.call("playback.sync.state", {"member_id": 0, "changes": [change]})
    assert out["outcomes"] == [{"status": "conflict"}]
    assert host.states[(0, TV, 1, 1)]["position_ms"] == 200_000  # 连进度也没写
    assert host.states[(0, TV, 1, 1)]["continue_dismissed"] is False
    assert len(host.anchor_conflicts) == 1


# ---------------------------------------------------------------- 扫描
async def test_sweep_dismisses_only_anchor_and_old_episodes_only_clear(runtime, host, store):
    show(host)
    await configure(store)
    run = await runtime.sweep(reason="manual", dry_run=False)
    assert host.anchor_conflicts == []
    assert run["counts"] == {"candidates": 2, "cleared": 2, "previewed": 0, "conflicts": 0,
                             "skipped": 0, "failed": 0}
    sent = by_ep(all_changes(host))
    assert set(sent) == {(1, 1), (1, 3)}  # E2 进度已 0、不是锚点：不写
    assert "dismiss_continue" not in sent[(1, 1)] and sent[(1, 1)]["position_ms"] == 0
    assert sent[(1, 3)]["dismiss_continue"] is True
    # 旧集的请求形状与旧宿主一致
    assert set(sent[(1, 1)]) == {"media_item_id", "kind", "expected_updated_at", "position_ms",
                                 "tmdb_id", "season", "episode"}
    assert TV not in host.up_next(0)
    items = {(i["season"], i["episode"]): i for i in run["items"]}
    assert items[(1, 1)]["dismiss"] is False and items[(1, 1)]["note"] == NOT_ANCHOR_NOTE
    assert items[(1, 3)]["dismiss"] is True and "note" not in items[(1, 3)]
    assert all("warning" not in i for i in run["items"])
    hist = {h["episode"]: h for h in store.data[HISTORY_KEY]}
    assert "dismissed" not in hist[1] and "prior_dismissed" not in hist[1]
    assert hist[3]["dismissed"] is True and hist[3]["prior_dismissed"] is False
    # 再扫一次：作品已隐藏、旧集进度 0，什么都不写
    host.calls.clear()
    again = await runtime.sweep(reason="manual", dry_run=False)
    assert again["counts"]["candidates"] == 0 and not host.state_calls()
    # 真实重新播放：卡片回来；再清又能划走
    host.progress(0, TV, 1, 3, pos=400_000)
    assert TV in host.up_next(0)
    await runtime.sweep(reason="manual", dry_run=False)
    assert TV not in host.up_next(0) and host.anchor_conflicts == []


async def test_already_hidden_title_old_episode_only_clears_once(runtime, host, store):
    show(host, e1=200_000, e3=0)
    host.states[(0, TV, 1, 3)]["continue_dismissed"] = True  # 锚点已隐藏
    await configure(store)
    run = await runtime.sweep(reason="manual", dry_run=False)
    assert run["counts"]["cleared"] == 1
    (change,) = all_changes(host)
    assert (change["episode"], change["position_ms"]) == (1, 0) and "dismiss_continue" not in change
    host.calls.clear()
    assert (await runtime.sweep(reason="manual", dry_run=False))["counts"]["candidates"] == 0
    assert not host.state_calls() and host.anchor_conflicts == []


async def test_old_episode_zero_and_anchor_watched_is_left_alone(runtime, host, store):
    """锚点看完了（首页出下一集，是正常的「接下来」）：旧集进度 0 不动，旧集有进度只清进度。"""
    show(host, e1=0, e2=50_000, e3=0)
    host.states[(0, TV, 1, 3)]["played"] = True
    await configure(store)
    await runtime.sweep(reason="manual", dry_run=False)
    (change,) = all_changes(host)
    assert change["episode"] == 2 and "dismiss_continue" not in change


async def test_tie_uses_same_anchor_as_host(runtime, host, store):
    host.dismiss = True
    host.add_media(TV, 1, kind="tv")
    same = "2026-11-01T05:00:00"
    host.progress(0, TV, 1, 1, pos=100_000, at=same)  # 先建：id 小
    host.progress(0, TV, 1, 2, pos=200_000, at=same)
    assert host.anchor(0, TV) == (1, 2)
    await configure(store)
    run = await runtime.sweep(reason="manual", dry_run=False)
    assert run["counts"]["cleared"] == 2 and host.anchor_conflicts == []
    sent = by_ep(all_changes(host))
    assert sent[(1, 2)]["dismiss_continue"] is True and "dismiss_continue" not in sent[(1, 1)]
    assert TV not in host.up_next(0)


async def test_movies_unaffected_by_grouping(runtime, host, store):
    host.dismiss = True
    host.add_media(10, 1)
    host.add_media(11, 1)
    host.progress(0, 10, pos=1000)
    host.progress(0, 11, pos=2000)
    await configure(store)
    await runtime.sweep(reason="manual", dry_run=False)
    assert all(c["dismiss_continue"] is True for c in all_changes(host))
    assert host.up_next(0) == set()


async def test_dry_run_preview_marks_which_row_hides(runtime, host, store):
    show(host)
    await configure(store, dry_run=True)
    run = await runtime.sweep(reason="schedule", dry_run=True)
    assert {i["episode"]: i["dismiss"] for i in run["items"]} == {1: False, 3: True}
    assert not host.state_calls()


# ---------------------------------------------------------------- 锚点中途被顶替
async def test_anchor_moved_by_other_device_reread_and_dismiss_new_anchor(runtime, host, store):
    show(host, e1=0, e2=0, e3=300_000)
    await configure(store)
    # 写回前一刻，别的设备播了 E1（进度 50s）：E1 成了锚点
    host.before_state.append(lambda: host.progress(0, TV, 1, 1, pos=50_000))
    run = await runtime.sweep(reason="manual", dry_run=False)
    calls = host.state_calls()
    assert len(calls) == 2  # 第一次 + 重读后一次
    assert len(host.anchor_conflicts) == 1  # 只有竞态那一次
    retry = by_ep(calls[1]["changes"])
    assert retry[(1, 1)]["dismiss_continue"] is True  # 新锚点
    assert "dismiss_continue" not in retry[(1, 3)] and retry[(1, 3)]["position_ms"] == 0
    assert run["counts"]["conflicts"] == 0 and run["counts"]["cleared"] == 2
    assert host.states[(0, TV, 1, 1)]["continue_dismissed"] is True
    assert host.states[(0, TV, 1, 3)]["position_ms"] == 0
    assert TV not in host.up_next(0)


async def test_anchor_keeps_moving_is_reported_as_conflict(runtime, host, store):
    show(host, e1=0, e2=0, e3=300_000)
    await configure(store)
    orig = host.op_playback_sync_state
    episodes = iter([1, 2, 1, 2])

    def other_device_keeps_playing(p):
        host.progress(0, TV, 1, next(episodes), pos=10_000)
        return orig(p)

    host.op_playback_sync_state = other_device_keeps_playing
    run = await runtime.sweep(reason="manual", dry_run=False)
    assert len(host.state_calls()) == 2  # 只重试一次
    assert run["counts"]["conflicts"] >= 1
    conflicted = [i for i in run["items"] if i["status"] == "conflict" and i["dismiss"]]
    assert conflicted and conflicted[0]["error"] == DISMISS_CONFLICT_NOTE
    assert not host.hidden(0, TV) and TV in host.up_next(0)  # 没有假装已隐藏
    assert not [h for h in store.data.get(HISTORY_KEY, []) if h.get("dismissed")]


# ---------------------------------------------------------------- 事件路径
async def test_event_on_current_and_older_episode(runtime, host, store):
    show(host)
    await configure(store)
    assert await runtime.on_playback(event(0, TV, "tv", 1, 1)) == "scheduled"
    await runtime.drain()
    (old,) = all_changes(host)
    assert old["episode"] == 1 and "dismiss_continue" not in old
    assert TV in host.up_next(0)
    host.calls.clear()
    await runtime.on_playback(event(0, TV, "tv", 1, 3))
    await runtime.drain()
    (cur,) = all_changes(host)
    assert cur["episode"] == 3 and cur["dismiss_continue"] is True
    assert TV not in host.up_next(0) and host.anchor_conflicts == []
    host.calls.clear()
    await runtime.on_playback(event(0, TV, "tv", 1, 2))  # 旧集进度 0、作品已隐藏
    await runtime.drain()
    assert not host.state_calls()


# ---------------------------------------------------------------- 撤销
async def test_undo_anchor_and_old_episode(runtime, host, store):
    show(host)
    await configure(store)
    await runtime.sweep(reason="manual", dry_run=False)
    hist = {h["episode"]: h for h in store.data[HISTORY_KEY]}
    host.calls.clear()
    assert (await runtime.undo(hist[3]["id"]))["undo_status"] == "applied"
    assert (await runtime.undo(hist[1]["id"]))["undo_status"] == "applied"
    sent = by_ep(all_changes(host))
    assert (sent[(1, 3)]["position_ms"], sent[(1, 3)]["dismiss_continue"]) == (300_000, False)
    assert sent[(1, 1)]["position_ms"] == 200_000 and "dismiss_continue" not in sent[(1, 1)]
    assert TV in host.up_next(0) and host.anchor_conflicts == []


async def test_undo_rehide_when_no_longer_anchor_is_refused(runtime, host, store):
    show(host, e3=300_000)
    host.states[(0, TV, 1, 3)]["continue_dismissed"] = True  # 清理前就隐藏着
    await configure(store)
    await runtime.sweep(reason="manual", dry_run=False)
    entry = next(h for h in store.data[HISTORY_KEY] if h["episode"] == 3)
    assert entry["prior_dismissed"] is True
    # 之后导入了一条更新的 E2 观看（只改时间、不动 E3 的版本）：E2 成了锚点
    host.states[(0, TV, 1, 2)]["last_played_at"] = "2026-11-02T00:00:00"
    before = dict(host.states[(0, TV, 1, 3)])
    assert (await runtime.undo(entry["id"]))["undo_status"] == "conflict"
    assert host.states[(0, TV, 1, 3)] == before  # 不强行恢复


# ---------------------------------------------------------------- 旧宿主
async def test_old_host_multi_episode_shape(runtime, host, store):
    show(host)
    host.dismiss = False
    for st in host.states.values():
        st.pop("continue_dismissed", None)
    await configure(store)
    run = await runtime.sweep(reason="manual", dry_run=False)
    assert run["counts"]["cleared"] == 2 and NO_DISMISS_NOTE in run["notes"]
    sent = by_ep(all_changes(host))
    assert set(sent) == {(1, 1), (1, 3)}
    assert all("dismiss_continue" not in c for c in sent.values())
    assert all(i["warning"] == NO_DISMISS_NOTE and "dismiss" not in i for i in run["items"])
    # 旧宿主上只为整部作品的锚点多读 catalog？不读：剧集分组只用扫描时读到的行
    assert [n for n, _ in host.calls].count("playback.sync.catalog") == 1
