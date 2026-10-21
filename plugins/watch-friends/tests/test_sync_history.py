"""后台历史分页及选择性重核：不限制十条，也不在打开历史时写平台。"""

import asyncio
import json
from datetime import UTC, datetime, timedelta

from test_app import harness as _harness
from test_auto_sync import save as save_watched
from test_auto_sync import setup as setup_watched
from test_watch_alignment import item

harness = _harness


async def test_all_runs_paginated_unified_owner_and_selected_recheck(harness, monkeypatch):
    h = harness
    sources, payload, writes, reads, _ = await setup_watched(h, monkeypatch)
    await save_watched(h, payload | {"enabled": False})
    previous = h.app.state.watch_archive.external.read

    async def read(*args, **kwargs):
        kwargs.setdefault("included_libraries", ["1"])
        return await previous(*args, **kwargs)

    h.app.state.watch_archive.external.read = read
    store = h.app.state.store
    owner = h.users["alice"]["id"]
    for i in range(27):
        number = f"history-{i:03}"
        created = (datetime.now(UTC) - timedelta(minutes=i)).isoformat()
        store.execute(
            "INSERT INTO "
            "auto_sync_runs(id,user_id,status,stage,total,processed,created_at)"
            " VALUES(?,?,'done','finished',1,1,?)",
            (number, owner, created),
        )
        store.execute(
            "INSERT INTO "
            "auto_sync_targets(run_id,source_id,canonical_key,media,state)"
            " VALUES(?,?,?,?,'failed')",
            (number, sources["emby"], "tmdb:movie:550", json.dumps(item(550)["media"])),
        )
    rows = []
    offset = 0
    while offset is not None:
        page = (
            await h.clients["alice"].get(f"/api/watch/auto-sync/runs?offset={offset}&limit=7")
        ).json()
        rows += page["runs"]
        offset = page["next_offset"]
    assert len(rows) == 27 and len({r["id"] for r in rows}) == 27
    page = (await h.clients["alice"].get("/api/watch/tasks?kind=auto_sync&offset=20")).json()
    assert len(page["tasks"]) == 7 and page["total"] == 27
    assert (await h.clients["bob"].get("/api/watch/auto-sync/runs")).json()["runs"] == []
    assert (await h.clients["bob"].get("/api/watch/auto-sync/runs/history-026")).status_code == 404
    target = store.one("SELECT id FROM auto_sync_targets WHERE run_id='history-026'")["id"]
    response = await h.clients["alice"].post(
        "/api/watch/auto-sync/runs/history-026/recheck",
        json={"target_ids": [target], "consent": True},
    )
    assert response.status_code == 202, response.text
    await asyncio.gather(*h.app.state.watch_archive.tasks.values())
    assert writes == []
    targets = (
        await h.clients["alice"].get("/api/watch/alignment/" + response.json()["id"] + "/targets")
    ).json()["targets"]
    assert len(targets) == 1 and targets[0]["source_id"] == sources["emby"]
