"""同源相同内容折叠、实际更新计数、自动读取冷却及保留原始明细。"""

import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta

from test_app import harness as _harness
from test_watch_archive import connect, fact, finish

harness = _harness


async def native_read(h, *, automatic=False):
    response = await h.clients["alice"].post(
        "/api/watch/refresh" + ("?automatic=true" if automatic else "")
    )
    assert response.status_code == 202, response.text
    number = response.json()["id"]
    return number, await finish(h, number)


async def test_actual_updates_unchanged_rows_and_automatic_cooldown(harness):
    h = harness
    records, reads = [fact(550, date="2026-10-01T00:00:00Z")], []

    async def archive(token):
        reads.append(token)
        return deepcopy(records)

    h.upstream.archive = archive
    first, result = await native_read(h, automatic=True)
    assert result["result"]["new_records"] == 1
    assert result["result"]["updated_records"] == result["result"]["unchanged_records"] == 0
    facts = h.app.state.store.all("SELECT * FROM watch_facts")
    second, _ = await native_read(h, automatic=True)
    assert second == first and len(reads) == 1
    third, result = await native_read(h)
    assert third != first and len(reads) == 2
    assert result["result"]["unchanged_records"] == 1
    assert result["result"]["new_records"] == result["result"]["updated_records"] == 0
    assert h.app.state.store.all("SELECT * FROM watch_facts") == facts
    unchanged = (
        await h.clients["alice"].get(f"/api/watch/jobs/{third}/records?change=unchanged")
    ).json()
    assert len(unchanged["records"]) == 1
    # 数量、作品编号不变，但观看日期变化必须保留为真实更新任务。
    records[0]["watched_at"] = "2026-10-02T00:00:00Z"
    changed, result = await native_read(h)
    assert result["result"]["updated_records"] == 1 and result["result"]["unchanged_records"] == 0
    h.app.state.store.execute(
        "UPDATE watch_jobs SET finished_at=? WHERE user_id=?",
        ((datetime.now(UTC) - timedelta(minutes=6)).isoformat(), h.users["alice"]["id"]),
    )
    latest, _ = await native_read(h, automatic=True)
    assert latest != changed and len(reads) == 4
    # 其他用户不能复用本人的近期任务。
    other = await h.clients["bob"].post("/api/watch/refresh?automatic=true")
    assert other.json()["id"] not in {first, third, changed, latest}


async def test_legacy_duplicates_fold_before_pagination_without_deleting_details(harness):
    h = harness
    records = [fact(550)]

    async def archive(_):
        return deepcopy(records)

    h.upstream.archive = archive
    jobs = []
    for _ in range(6):
        number, _ = await native_read(h)
        jobs.append(number)
        saved = h.app.state.store.one("SELECT result FROM watch_jobs WHERE id=?", (number,))
        result = json.loads(saved["result"])
        result.pop("unchanged_records")
        result["updated_records"] = 0 if number == jobs[0] else 1
        h.app.state.store.execute(
            "UPDATE watch_jobs SET result=?,content_hash=NULL WHERE id=?",
            (json.dumps(result), number),
        )
    client = h.clients["alice"]
    grouped = (await client.get("/api/watch/tasks?kind=import&limit=1")).json()
    assert grouped["total"] == 2 and grouped["hidden_count"] == 4 and grouped["next_offset"] == 1
    assert grouped["tasks"][0]["id"] == jobs[-1] and grouped["tasks"][0]["repeat_count"] == 5
    assert grouped["tasks"][0]["same_as_previous"] is True
    page = (await client.get("/api/watch/tasks?kind=import&offset=1&limit=1")).json()
    assert page["tasks"][0]["id"] == jobs[0] and page["next_offset"] is None
    expanded = (await client.get("/api/watch/tasks?kind=import&collapse=false")).json()
    assert expanded["total"] == 6 and expanded["hidden_count"] == 0
    for number in jobs:
        assert len((await client.get(f"/api/watch/jobs/{number}/records")).json()["records"]) == 1
    assert (await h.clients["bob"].get("/api/watch/tasks")).json()["total"] == 0
    # 同样一条记录，内容变化后不允许按数量合并。
    records[0]["media"]["title"] = "修正后的片名"
    number, result = await native_read(h)
    assert result["result"]["updated_records"] == 1
    changed = (await client.get("/api/watch/tasks?kind=import")).json()
    assert changed["total"] == 3 and changed["tasks"][0]["id"] == number


async def test_different_connections_and_failures_are_not_merged(harness):
    h = harness
    for platform in ("emby", "plex"):
        connected = await connect(h, [fact(550)], platform)
        await h.clients["alice"].post(
            f"/api/watch/jobs/{connected['id']}/commit", json={"consent": True}
        )
        for _ in range(2):
            number = (
                await h.clients["alice"].post(
                    f"/api/watch/connections/{connected['source_id']}/preview"
                )
            ).json()["id"]
            await finish(h, number)
            await h.clients["alice"].post(
                f"/api/watch/jobs/{number}/commit", json={"consent": True}
            )
    tasks = (await h.clients["alice"].get("/api/watch/tasks?kind=import")).json()
    assert tasks["total"] == 4 and tasks["hidden_count"] == 2
    assert len({r["platform"] for r in tasks["tasks"]}) == 2
    h.app.state.store.execute(
        "UPDATE watch_jobs SET status='failed',error='读取失败' WHERE id=?",
        (tasks["tasks"][0]["id"],),
    )
    changed = (await h.clients["alice"].get("/api/watch/tasks?kind=import")).json()
    assert any(r["status"] == "failed" and r["error"] == "读取失败" for r in changed["tasks"])
