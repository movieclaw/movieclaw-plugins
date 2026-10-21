"""功能总检查发现的边界缺口：先复现，再核对私有数据与写入次数不变。"""

import asyncio
import json
from contextlib import ExitStack

import test_web_e2e as web
from movieclaw_friends.auto_sync import AutoSync, stamp
from test_app import harness as _harness
from test_auto_sync import save, setup

harness = _harness


async def test_feed_pages_skip_hidden_before_pagination(harness):
    h = harness
    store = h.app.state.store
    owner = h.users["alice"]["id"]
    ids = []
    for index in range(108):
        cursor = store.db.execute(
            "INSERT INTO social_posts(user_id,media,body,created_at) VALUES(?,?,?,?)",
            (
                owner,
                json.dumps({"kind": "movie", "tmdb_id": 550, "title": "电影"}),
                str(index),
                stamp(),
            ),
        )
        ids.append(cursor.lastrowid)
    for number in ids[4:]:
        store.execute(
            "INSERT INTO community_hidden VALUES('post',?,1,?,'隐藏',?)", (number, owner, stamp())
        )
    first = (await h.clients["bob"].get("/api/feed?limit=2")).json()
    assert [v["id"] for v in first["posts"]] == ids[2:4][::-1]
    second = (
        await h.clients["bob"].get("/api/feed", params={"limit": 2, "before": first["next_before"]})
    ).json()
    assert [v["id"] for v in second["posts"]] == ids[:2][::-1]
    assert second["next_before"] is None


async def test_auto_readback_fixes_interrupted_progress_without_write(harness, monkeypatch):
    h = harness
    _, payload, writes, _, states = await setup(h, monkeypatch)
    await save(h, payload)
    service = h.app.state.auto_sync
    entered, release = asyncio.Event(), asyncio.Event()
    original = h.app.state.watch_archive.external.mark_played

    async def lost(*args):
        await original(*args)
        entered.set()
        await release.wait()

    h.app.state.watch_archive.external.mark_played = lost
    await h.clients["alice"].post("/api/watch/auto-sync/check")
    await asyncio.wait_for(entered.wait(), 2)
    await service.close()
    old = service.view(h.users["alice"]["id"])["runs"][0]
    assert old["processed"] == 0 and any(v["state"] == "uncertain" for v in old["counts"])
    replacement = AutoSync(h.app.state.store, h.app.state.watch_archive)
    h.app.state.auto_sync = replacement
    h.app.state.watch_archive.external.mark_played = original
    setting = replacement.settings(h.users["alice"]["id"])
    await replacement.run(h.users["alice"]["id"], setting["revision"])
    older = next(
        v for v in replacement.view(h.users["alice"]["id"])["runs"] if v["id"] == old["id"]
    )
    done = sum(v["count"] for v in older["counts"] if v["state"] == "done")
    assert done == 1
    assert older["processed"] >= done
    # 回读补齐原结果，不重新提交已成功的那一项。
    assert writes.count(("emby", "10")) == 1


def test_settings_fragment_survives_refresh(tmp_path):
    with ExitStack() as stack:
        origin, _ = stack.enter_context(web.native_at_port(tmp_path / "local"))
        (page,) = web.browser_pages(stack, 1)
        web.enter(page, origin, "alice")
        page.goto(origin + web.PREFIX + "/web/#network-settings")
        web.expect(page.locator("#panel-privacy")).to_be_visible()
        web.expect(page.locator("#network-settings .network-settings")).to_be_visible()
        page.reload()
        web.expect(page.locator("#panel-privacy")).to_be_visible()
        web.expect(page.locator("#network-settings .network-settings")).to_be_visible()


async def test_comment_pages_skip_hidden_before_pagination(harness):
    h = harness
    store = h.app.state.store
    owner = h.users["alice"]["id"]
    cursor = store.db.execute(
        "INSERT INTO social_posts(user_id,media,body,created_at) VALUES(?,?,?,?)",
        (owner, json.dumps({"kind": "movie", "tmdb_id": 550, "title": "电影"}), "推荐", stamp()),
    )
    post_id = cursor.lastrowid
    ids = []
    for index in range(108):
        cursor = store.db.execute(
            "INSERT INTO social_comments(post_id,user_id,body,created_at) VALUES(?,?,?,?)",
            (post_id, owner, str(index), stamp()),
        )
        ids.append(cursor.lastrowid)
    for number in ids[4:]:
        store.execute(
            "INSERT INTO community_hidden VALUES('comment',?,1,?,'隐藏',?)",
            (number, owner, stamp()),
        )
    first = (await h.clients["bob"].get(f"/api/feed/{post_id}/comments?limit=2")).json()
    assert [v["id"] for v in first["comments"]] == ids[2:4][::-1]
    second = (
        await h.clients["bob"].get(
            f"/api/feed/{post_id}/comments", params={"limit": 2, "before": first["next_before"]}
        )
    ).json()
    assert [v["id"] for v in second["comments"]] == ids[:2][::-1]
    assert second["next_before"] is None


def test_embedded_settings_uses_parent_fragment(tmp_path):
    from fastapi.responses import HTMLResponse

    with ExitStack() as stack:
        origin, host = stack.enter_context(web.native_at_port(tmp_path / "local"))

        @host.get("/embedded")
        async def embedded():
            return HTMLResponse('<iframe src="' + web.PREFIX + '/web/"></iframe>')

        (page,) = web.browser_pages(stack, 1)
        web.enter(page, origin, "alice")
        page.goto(origin + "/embedded#network-settings")
        frame = page.frame_locator("iframe")
        web.expect(frame.locator("#panel-privacy")).to_be_visible()
        web.expect(frame.locator("#network-settings .network-settings")).to_be_visible()
