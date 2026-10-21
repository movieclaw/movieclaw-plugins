"""任务历史、真实批次进度、选择性重核及本人授权边界。"""

import asyncio
import json
from datetime import UTC, datetime, timedelta

from movieclaw_friends.task_center import execution_view
from test_app import harness as _harness
from test_watch_alignment import preview, setup, targets
from test_watch_archive import connect, fact, finish

harness = _harness


async def test_import_result_is_frozen_and_record_changes_searchable(harness):
    h = harness
    alice, bob = h.clients["alice"], h.clients["bob"]
    first = await connect(h, [fact(550), fact(None, external="unknown")])
    base = f"/api/watch/jobs/{first['id']}"
    assert (await alice.get(base)).json()["can_commit"] is True
    await alice.post(base + "/commit", json={"consent": True})
    result = (await alice.get(base)).json()
    assert result["result"]["new_records"] == 2
    assert result["new_unique"] == 1 and result["already_known"] == 0
    archive = h.app.state.watch_archive

    async def read(*args, **kwargs):
        return {
            "account_id": "personal-id",
            "account_name": "本人",
            "records": [fact(550), fact(1396), fact(None, external="unknown")],
        }

    archive.external.read = read
    second = (await alice.post(f"/api/watch/connections/{first['source_id']}/preview")).json()
    await finish(h, second["id"])
    second_base = f"/api/watch/jobs/{second['id']}"
    await alice.post(second_base + "/commit", json={"consent": True})
    saved = (await alice.get(second_base)).json()["result"]
    assert (
        saved["new_records"] == 1
        and saved["updated_records"] == 0
        and saved["unchanged_records"] == 2
    )
    assert saved["new_unique"] == 1 and saved["already_known"] == 1
    assert (await alice.get(base)).json()["result"] == result["result"]
    assert len((await alice.get(base + "/records")).json()["records"]) == 2
    rows = (await alice.get(second_base + "/records?change=new&query=1396")).json()
    assert len(rows["records"]) == 1 and rows["records"][0]["change_kind"] == "new"
    pending = (await alice.get(second_base + "/records?match_state=pending")).json()
    assert len(pending["records"]) == 1 and not pending["records"][0]["matched"]
    assert (await bob.get(base + "/records")).status_code == 404
    tasks = (await alice.get("/api/watch/tasks?kind=import&limit=1")).json()
    assert tasks["next_offset"] == 1 and tasks["tasks"][0]["id"] == second["id"]
    assert not (await bob.get("/api/watch/tasks")).json()["tasks"]
    text = json.dumps(tasks)
    assert "personal-secret" not in text and "personal.example" not in text


async def test_execution_progress_owner_stop_and_fresh_selected_recheck(harness, monkeypatch):
    h = harness
    alice, bob = h.clients["alice"], h.clients["bob"]
    sources, writes, states = await setup(h, monkeypatch)
    number = await preview(h, sources)
    base = f"/api/watch/alignment/{number}"
    pending = [r for r in await targets(h, number) if r["state"] == "pending"]
    ids = [r["id"] for r in pending]
    assert (await bob.get(base + "/report")).status_code == 404
    assert (
        await alice.post(base + "/execution", json={"target_ids": ids, "consent": False})
    ).status_code == 422
    response = await alice.post(base + "/execution", json={"target_ids": ids, "consent": True})
    assert response.status_code == 200, response.text
    execution = response.json()
    assert execution["total"] == execution["remaining"] == 2
    key = execution["id"]
    assert (
        await alice.post(base + "/execution", json={"target_ids": ids, "consent": True})
    ).status_code == 409
    assert (await alice.post(base + f"/apply/{ids[0]}", json={"consent": True})).status_code == 409
    assert (
        await bob.post(base + f"/apply/{ids[0]}", json={"consent": True, "execution_id": key})
    ).status_code == 404
    result = await alice.post(
        base + f"/apply/{ids[0]}", json={"consent": True, "execution_id": key}
    )
    assert result.json()["state"] == "done", result.text
    report = (await alice.get(base + "/report")).json()
    assert report["execution"]["processed"] == report["execution"]["remaining"] == 1
    assert report["execution"]["counts"][0]["source_id"] in sources.values()
    assert (
        await alice.post(base + f"/execution/{key}", json={"action": "finish"})
    ).status_code == 409
    await alice.post(base + f"/execution/{key}", json={"action": "stop"})
    assert (
        await alice.post(base + f"/apply/{ids[1]}", json={"consent": True, "execution_id": key})
    ).status_code == 409
    assert len(writes) == 1
    original_read = h.app.state.watch_archive.external.read

    async def updated_inventory(platform, *args, **kwargs):
        result = await original_read(platform, *args, **kwargs)
        for record in result["records"]:
            record["played"] = states[(platform, record["external_id"])]
        return result

    h.app.state.watch_archive.external.read = updated_inventory
    before = h.app.state.store.all("SELECT * FROM alignment_targets WHERE job_id=?", (number,))
    fresh = await alice.post(base + "/recheck", json={"target_ids": [ids[0]], "consent": True})
    assert fresh.status_code == 202, fresh.text
    await asyncio.gather(*list(h.app.state.watch_archive.tasks.values()))
    new_rows = await targets(h, fresh.json()["id"])
    assert len(new_rows) == 1 and new_rows[0]["state"] == "skipped"
    assert new_rows[0]["source_id"] == pending[0]["source_id"]
    assert (
        h.app.state.store.all("SELECT * FROM alignment_targets WHERE job_id=?", (number,)) == before
    )
    assert len(writes) == 1
    old_rows = await targets(h, number)
    assert next(r for r in old_rows if r["id"] == ids[0])["attempts"] == 1
    assert (
        await bob.post(base + "/recheck", json={"target_ids": ids, "consent": True})
    ).status_code == 404
    assert (
        await alice.post(base + "/recheck", json={"target_ids": [999999], "consent": True})
    ).status_code == 422


async def test_expired_reports_read_only_and_stale_execution_is_interrupted(harness, monkeypatch):
    h = harness
    sources, writes, _ = await setup(h, monkeypatch)
    number = await preview(h, sources)
    base = f"/api/watch/alignment/{number}"
    row = next(r for r in await targets(h, number) if r["state"] == "pending")
    started = (
        await h.clients["alice"].post(
            base + "/execution", json={"target_ids": [row["id"]], "consent": True}
        )
    ).json()
    old = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    started["updated_at"] = old
    h.app.state.store.execute(
        "UPDATE alignment_jobs SET created_at=?,execution=? WHERE id=?",
        (old, json.dumps(started), number),
    )
    report = (await h.clients["alice"].get(base + "/report")).json()
    assert report["writable"] is False and "一小时" in report["write_blocked_reason"]
    assert report["execution"]["status"] == "interrupted"
    assert report["execution"]["remaining"] == 1
    assert len((await h.clients["alice"].get(base + "/targets")).json()["targets"]) == 4
    assert (
        await h.clients["alice"].post(
            base + "/execution", json={"target_ids": [row["id"]], "consent": True}
        )
    ).status_code == 409
    assert (
        await h.clients["alice"].post(base + "/apply/" + str(row["id"]), json={"consent": True})
    ).status_code == 409
    tasks = (await h.clients["alice"].get("/api/watch/tasks?kind=alignment")).json()
    assert tasks["tasks"][0]["execution"]["status"] == "interrupted"
    assert writes == []
    assert (
        execution_view(
            h.app.state.store,
            h.app.state.alignment.job(
                number,
                h.app.state.store.one("SELECT id FROM users WHERE username=?", ("alice",))["id"],
                historical=True,
            ),
        )
        is not None
    )


async def test_inflight_result_survives_stop_without_submitting_remaining(harness, monkeypatch):
    h = harness
    sources, writes, _ = await setup(h, monkeypatch)
    number = await preview(h, sources)
    base = f"/api/watch/alignment/{number}"
    rows = [r for r in await targets(h, number) if r["state"] == "pending"]
    alice = h.clients["alice"]
    execution = (
        await alice.post(
            base + "/execution", json={"target_ids": [r["id"] for r in rows], "consent": True}
        )
    ).json()
    entered, release = asyncio.Event(), asyncio.Event()
    original = h.app.state.watch_archive.external.mark_played

    async def paused(*args):
        entered.set()
        await release.wait()
        await original(*args)

    h.app.state.watch_archive.external.mark_played = paused
    request = asyncio.create_task(
        alice.post(
            base + f"/apply/{rows[0]['id']}",
            json={"consent": True, "execution_id": execution["id"]},
        )
    )
    await asyncio.wait_for(entered.wait(), 2)
    report = (await alice.get(base + "/report")).json()
    assert report["execution"]["current"]["media"]["title"] == rows[0]["media"]["title"]
    assert report["execution"]["processed"] == 0 and report["execution"]["remaining"] == 2
    await alice.post(base + f"/execution/{execution['id']}", json={"action": "stop"})
    release.set()
    assert (await request).json()["state"] == "done"
    report = (await alice.get(base + "/report")).json()
    assert report["execution"]["status"] == "stopped"
    assert report["execution"]["processed"] == report["execution"]["remaining"] == 1
    assert len(writes) == 1


async def test_mixed_selected_recheck_restricts_native_units_and_tracks_native_result(
    harness, monkeypatch
):
    h = harness
    alice = h.clients["alice"]
    sources, writes, _ = await setup(h, monkeypatch)
    number = await preview(h, sources, native=True)
    base = f"/api/watch/alignment/{number}"
    records = (await alice.get(base + "/records")).json()["records"]
    await alice.post(
        base + "/native-targets",
        json={
            "targets": [
                {"canonical_key": r["canonical_key"], "state": "pending", "media_item_id": 42}
                for r in records
            ]
        },
    )
    await alice.post(base + "/ready")
    rows = await targets(h, number)
    native = next(
        r for r in rows if r["source_id"] == "movieclaw" and r["media"]["kind"] == "movie"
    )
    external = next(r for r in rows if r["source_id"] != "movieclaw" and r["media"]["kind"] == "tv")
    fresh = (
        await alice.post(
            base + "/recheck", json={"target_ids": [native["id"], external["id"]], "consent": True}
        )
    ).json()["id"]
    await asyncio.gather(*list(h.app.state.watch_archive.tasks.values()))
    new_base = f"/api/watch/alignment/{fresh}"
    native_records = (await alice.get(new_base + "/records")).json()["records"]
    assert len(native_records) == 1 and native_records[0]["media"]["kind"] == "movie"
    await alice.post(
        new_base + "/native-targets",
        json={
            "targets": [
                {
                    "canonical_key": native_records[0]["canonical_key"],
                    "state": "pending",
                    "media_item_id": 42,
                }
            ]
        },
    )
    assert (await alice.post(new_base + "/ready")).status_code == 200
    new_rows = await targets(h, fresh)
    assert len(new_rows) == 2
    target = next(r for r in new_rows if r["source_id"] == "movieclaw")
    execution = (
        await alice.post(
            new_base + "/execution", json={"target_ids": [target["id"]], "consent": True}
        )
    ).json()
    payload = {"consent": True, "execution_id": execution["id"], "begin_attempt": True}
    assert (
        await alice.post(new_base + f"/native-prepare/{target['id']}", json=payload)
    ).status_code == 200
    await alice.post(new_base + f"/execution/{execution['id']}", json={"action": "stop"})
    result = await alice.post(
        new_base + f"/native-result/{target['id']}",
        json={"consent": True, "success": True, "execution_id": execution["id"]},
    )
    assert result.json()["state"] == "done"
    report = (await alice.get(new_base + "/report")).json()
    assert report["execution"]["processed"] == 1 and report["execution"]["remaining"] == 0
    assert writes == []
