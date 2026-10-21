"""待匹配足迹修正：本人确认、同来源批量、重导入记忆及成人库边界。"""

import json
from copy import deepcopy

import pytest
from test_watch_archive import connect, fact, finish
from test_watch_archive import harness as _harness

harness = _harness


@pytest.mark.asyncio
async def test_pending_series_correction_is_scoped_and_survives_reimport(harness):
    alice, bob = harness.clients["alice"], harness.clients["bob"]
    rows = [
        fact(
            None,
            kind="tv",
            season=1,
            episode=i,
            external=f"episode-{i}",
            date="2026-06-01T10:00:00Z",
        )
        for i in range(1, 4)
    ]
    for row in rows:
        row["media"].update(title="超能路人甲", year=2026)
    other_year = deepcopy(rows[0])
    other_year["external_id"] = "other-year"
    other_year["media"]["year"] = 2025
    rows.append(other_year)
    job = await connect(harness, rows, "plex")
    await alice.post(f"/api/watch/jobs/{job['id']}/commit", json={"consent": True})
    other = await connect(harness, rows[:1], "emby")
    await alice.post(f"/api/watch/jobs/{other['id']}/commit", json={"consent": True})
    all_rows = (await alice.get("/api/watch/records")).json()["records"]
    record = next(r for r in all_rows if r["media"]["year"] == 2026 and r["sources"] == ["plex"])
    route = f"/api/watch/records/{record['id']}/match"
    payload = {
        "tmdb_id": 259837,
        "kind": "tv",
        "all_episodes": True,
        "poster_path": "/valid.jpg",
        "consent": True,
    }
    assert (await bob.put(route, json=payload)).status_code == 404
    assert (await alice.put(route, json={**payload, "consent": False})).status_code == 422
    assert (await alice.put(route, json={**payload, "kind": "movie"})).status_code == 422
    assert (
        await alice.put(
            route, json={**payload, "poster_path": "https://secret.invalid/?token=private"}
        )
    ).status_code == 422

    async def read(*args, **kwargs):
        return {"account_id": "personal-id", "account_name": "本人", "records": deepcopy(rows)}

    harness.app.state.watch_archive.external.read = read
    # 保留一份先于校正生成的预览，确认旧预览也不能把已修正资料覆盖回空值。
    stale = (await alice.post(f"/api/watch/connections/{job['source_id']}/preview")).json()
    await finish(harness, stale["id"])
    result = await alice.put(route, json=payload)
    assert result.status_code == 200, result.text
    assert result.json()["updated"] == 3
    assert result.json()["stats"] == {"movies": 0, "shows": 1, "episodes": 3, "pending": 2}
    assert (await alice.put(route, json=payload)).status_code == 409
    await alice.post(f"/api/watch/jobs/{stale['id']}/commit", json={"consent": True})
    new = (await alice.post(f"/api/watch/connections/{job['source_id']}/preview")).json()
    await finish(harness, new["id"])
    preview = (await alice.get(f"/api/watch/jobs/{new['id']}/records")).json()["records"]
    assert sum(r["matched"] for r in preview) == 3
    await alice.post(f"/api/watch/jobs/{new['id']}/commit", json={"consent": True})
    result = (await alice.get("/api/watch/records")).json()["records"]
    matched = [r for r in result if r["matched"]]
    assert len(matched) == 3
    assert {r["media"]["episode_number"] for r in matched} == {1, 2, 3}
    assert all(
        r["media"]["poster_path"] == "/valid.jpg" and r["watched_at"] == "2026-06-01T10:00:00+00:00"
        for r in matched
    )
    assert (await alice.get("/api/feed")).json()["posts"] == []
    # 条目 ID 被源平台重用为其他作品时，不套用之前的匹配。
    changed = deepcopy(rows[0]["media"])
    changed["title"] = "另一部剧"
    assert (
        harness.app.state.watch_archive.corrected(
            harness.app.state.store.one("SELECT id FROM users WHERE username='alice'")["id"],
            job["source_id"],
            "episode-1",
            changed,
        )
        == changed
    )
    await alice.delete(f"/api/watch/connections/{job['source_id']}?erase=true")
    assert harness.app.state.store.all("SELECT * FROM watch_corrections") == []


@pytest.mark.asyncio
async def test_matching_cannot_bypass_excluded_library(harness):
    alice = harness.clients["alice"]
    store = harness.app.state.store
    rows = [fact(None, kind="tv", season=1, episode=1, external="private")]
    rows[0]["media"].update(title="隐私剧集", year=2026)
    rows[0]["library_ids"] = ["adult"]
    job = await connect(harness, rows, "plex")
    await alice.post(f"/api/watch/jobs/{job['id']}/commit", json={"consent": True})
    owner = store.one("SELECT id FROM users WHERE username='alice'")["id"]
    policy = store.display_policy
    policy.catalog(owner, job["source_id"], [{"id": "adult", "name": "排除库"}])
    store.execute("UPDATE display_libraries SET excluded=1 WHERE user_id=?", (owner,))
    row = store.one("SELECT id FROM watch_facts WHERE user_id=?", (owner,))
    response = await alice.put(
        f"/api/watch/records/{row['id']}/match",
        json={"tmdb_id": 259837, "kind": "tv", "consent": True},
    )
    assert response.status_code == 200, response.text
    assert response.json()["stats"]["episodes"] == 0
    assert (await alice.get("/api/watch/records")).json()["records"] == []
    membership = store.one("SELECT keys_json FROM display_memberships WHERE user_id=?", (owner,))
    assert "tmdb:tv:259837" in json.loads(membership["keys_json"])
