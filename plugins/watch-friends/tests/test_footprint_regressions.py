"""临时资料缺失不应拆散已确认剧集；未匹配分组只影响本人的展示。"""

import json
from copy import deepcopy

import pytest
from test_app import harness as _harness
from test_watch_archive import connect, fact, finish

harness = _harness


@pytest.mark.asyncio
async def test_reimport_missing_identity_and_poster_keeps_verified_episodes(harness):
    h = harness
    client = h.clients["alice"]
    rows = [fact(1396, kind="tv", season=1, episode=e, external=str(e)) for e in (1, 2)]
    for row in rows:
        row["media"].update(title="同一部剧", year=2026, poster_path="/verified.jpg")
    first = await connect(h, rows)
    await client.post(f"/api/watch/jobs/{first['id']}/commit", json={"consent": True})
    before = (await client.get("/api/watch/profile")).json()["stats"]
    weakened = deepcopy(rows)
    for row in weakened:
        row["media"].update(tmdb_id=None, poster_path=None)

    async def read(*args, **kwargs):
        return {
            "account_id": "personal-id",
            "account_name": "本人",
            "records": weakened,
        }

    h.app.state.watch_archive.external.read = read
    job = (await client.post(f"/api/watch/connections/{first['source_id']}/preview")).json()["id"]
    preview = await finish(h, job)
    assert preview["matched"] == 2 and preview["pending"] == 0
    await client.post(f"/api/watch/jobs/{job}/commit", json={"consent": True})
    assert (await client.get("/api/watch/profile")).json()["stats"] == before
    records = (await client.get("/api/watch/records")).json()["records"]
    assert all(r["matched"] and r["media"]["poster_path"] == "/verified.jpg" for r in records)
    grouped = (await client.get("/api/watch/footprints")).json()["records"]
    assert len(grouped) == 1 and grouped[0]["series_grouped"] and grouped[0]["episode_count"] == 2


@pytest.mark.asyncio
async def test_pending_series_fold_privately_without_matching_or_cross_source_merge(
    harness,
):
    h = harness
    client = h.clients["alice"]
    rows = [fact(None, kind="tv", season=1, episode=e, external=str(e)) for e in (1, 3)]
    for row in rows:
        row["media"].update(title="待核对的剧", year=2025)
    for platform in ("emby", "plex"):
        job = await connect(h, rows, platform)
        await client.post(f"/api/watch/jobs/{job['id']}/commit", json={"consent": True})
    before = h.app.state.store.all("SELECT * FROM watch_facts")
    records = (await client.get("/api/watch/footprints")).json()["records"]
    assert len(records) == 2
    for r in records:
        assert r["series_grouped"] and not r["series_complete"] and not r["matched"]
        assert r["progress_episode"] == 3 and r["episode_count"] == 2
        url = f"/api/watch/pending-series/{r['id']}/records"
        assert len((await client.get(url)).json()["records"]) == 2
        assert (await h.clients["bob"].get(url)).status_code == 404
    assert (await client.get("/api/watch/profile")).json()["stats"] == {
        "movies": 0,
        "shows": 0,
        "episodes": 0,
        "pending": 4,
    }
    assert h.app.state.store.all("SELECT * FROM watch_facts") == before
    owner = h.users["alice"]["id"]
    for row in rows:
        row["library_ids"] = ["private"]
    h.app.state.store.display_policy.remember(
        owner, job["source_id"], rows, [{"id": "private", "name": "私人库"}]
    )
    await client.put("/api/display-policy", json={
        "excluded": [{"source_id": job["source_id"], "library_id": "private"}],
        "hide_private": True,
    })
    assert (await client.get("/api/watch/footprints")).json()["records"] == []
    assert (await client.get(f"/api/watch/pending-series/{records[0]['id']}/records")).status_code == 404


@pytest.mark.parametrize(
    "changed",
    [
        {"title": "另一部剧"},
        {"year": 2024},
        {"kind": "movie"},
        {"season_number": 2},
        {"episode_number": 2},
        {"episode_number": None},
        {"tmdb_id": 999},
    ],
)
def test_missing_metadata_never_borrows_identity_of_different_work(changed):
    from movieclaw_friends.watch_archive import preserve_verified_media

    old = {
        "tmdb_id": 1396,
        "kind": "tv",
        "title": "同一部剧",
        "year": 2025,
        "season_number": 1,
        "episode_number": 1,
        "poster_path": "/old.jpg",
    }
    incoming = {**old, "tmdb_id": None, "poster_path": None, **changed}
    assert preserve_verified_media(incoming, old) == incoming


def test_verified_metadata_only_fills_missing_values_and_valid_poster():
    from movieclaw_friends.watch_archive import preserve_verified_media

    old = {
        "tmdb_id": 1396,
        "kind": "tv",
        "title": "同一部剧",
        "year": 2025,
        "season_number": 1,
        "episode_number": 1,
        "poster_path": "/old.jpg",
    }
    incoming = {**old, "poster_path": "/new.jpg"}
    assert preserve_verified_media(incoming, old) == incoming
    assert preserve_verified_media({**old, "tmdb_id": None}, None)["tmdb_id"] is None
    assert (
        preserve_verified_media(
            {**old, "poster_path": None},
            {**old, "poster_path": "https://private.test/secret"},
        )["poster_path"]
        is None
    )


@pytest.mark.asyncio
async def test_late_preview_preserves_newly_verified_fact(harness):
    h = harness
    client = h.clients["alice"]
    row = fact(None, kind="tv", season=1, episode=1)
    job = await connect(h, [row])
    await client.post(f"/api/watch/jobs/{job['id']}/commit", json={"consent": True})
    late = (await client.post(f"/api/watch/connections/{job['source_id']}/preview")).json()["id"]
    await finish(h, late)
    # 模拟同一来源事实在预览读取完成后获得正式身份，而非用户手工更改预览。
    store = h.app.state.store
    media = {**row["media"], "tmdb_id": 1396, "poster_path": "/verified.jpg"}
    store.execute(
        "UPDATE watch_facts SET canonical_key=?,media=?",
        ("tmdb:tv:1396:s1:e1", json.dumps(media)),
    )
    await client.post(f"/api/watch/jobs/{late}/commit", json={"consent": True})
    record = (await client.get("/api/watch/records")).json()["records"][0]
    assert record["matched"] and record["media"]["poster_path"] == "/verified.jpg"
    assert (await finish(h, late))["matched"] == 1


@pytest.mark.asyncio
async def test_group_uses_existing_verified_poster_but_does_not_merge_specials_or_years(
    harness,
):
    h = harness
    client = h.clients["alice"]
    rows = [fact(1396, kind="tv", season=1, episode=e, external=str(e)) for e in (1, 3)]
    rows[0]["media"]["poster_path"] = "/old.jpg"
    rows[1]["media"]["poster_path"] = None
    job = await connect(h, rows)
    await client.post(f"/api/watch/jobs/{job['id']}/commit", json={"consent": True})
    record = (await client.get("/api/watch/footprints")).json()["records"][0]
    assert record["episode_count"] == 2 and record["media"]["poster_path"] == "/old.jpg"
    pending = [
        fact(None, kind="tv", season=s, episode=e, external=str(i))
        for i, (s, e) in enumerate([(1, 1), (1, 2), (0, 1), (1, None)])
    ]
    for row in pending:
        row["media"].update(title="待确认", year=2025)
    pending[1]["media"]["year"] = 2024
    job = await connect(h, pending, "plex")
    await client.post(f"/api/watch/jobs/{job['id']}/commit", json={"consent": True})
    result = (await client.get("/api/watch/footprints")).json()["records"]
    assert len(result) == 5
    assert all(row.get("episode_count", 1) == 1 for row in result if not row["matched"])
    shared = h.app.state.watch_archive.shareable_history(h.users["alice"]["id"])
    assert all(
        "pending_series_key" not in row and "pending_series_key" not in row["media"]
        for row in shared
    )


@pytest.mark.asyncio
async def test_native_automatic_import_keeps_matching_when_enrichment_temporarily_fails(harness):
    h = harness
    archive = h.app.state.watch_archive
    rows = [fact(1396, kind="tv", season=1, episode=e, external=str(e)) for e in (1, 2)]
    for row in rows:
        row["media"].update(title="原有剧集", poster_path="/verified.jpg")

    async def read(*args):
        return deepcopy(rows)

    archive.source.archive = read
    first = (await h.clients["alice"].post("/api/watch/refresh")).json()["id"]
    await finish(h, first)
    original = archive.stats(h.users["alice"]["id"])
    for row in rows:
        row["media"].update(tmdb_id=None, poster_path=None)
    job = (await h.clients["alice"].post("/api/watch/refresh")).json()["id"]
    result = await finish(h, job)
    assert result["status"] == "complete" and result["result"]["unchanged_records"] == 2
    assert archive.stats(h.users["alice"]["id"]) == original
    records = h.app.state.series_footprints.grouped(h.users["alice"]["id"])
    assert len(records) == 1 and records[0]["episode_count"] == 2
    assert records[0]["media"]["poster_path"] == "/verified.jpg"
