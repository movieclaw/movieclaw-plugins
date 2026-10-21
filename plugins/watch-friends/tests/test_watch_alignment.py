"""只读差异、本人确认、库范围、重试幂等和原始档案保留的真实 HTTP 验收。"""

import asyncio
from copy import deepcopy

import pytest
from movieclaw_friends.peers import PeerError
from test_app import harness as _harness
from test_watch_archive import connect, fact

harness = _harness


def item(number=550, *, played=False, library="1", **kwargs):
    return fact(number, **kwargs) | {"played": played, "library_ids": [library]}


async def setup(h, monkeypatch, inventories=None):
    inventories = inventories or {
        "emby": [item(550), item(1396, kind="tv", season=1, episode=1, played=True)],
        "plex": [item(550, played=True), item(1396, kind="tv", season=1, episode=1)],
    }
    sources = {}
    for platform in inventories:
        source = await connect(h, [], platform)
        sources[platform] = source["source_id"]

    async def read(platform, base, token, progress, **kwargs):
        assert kwargs["only_watched"] is False and kwargs["include_libraries"] is True
        assert token == "personal-secret"
        progress(len(inventories[platform]))
        return {"account_id": "personal-id", "records": deepcopy(inventories[platform])}

    writes = []
    states = {
        (p, row["external_id"]): row["played"] for p, rows in inventories.items() for row in rows
    }

    async def inspect(client, source, base, token, external_id, expected):
        return {"played": states[(source["platform"], external_id)], "library_ids": {"1"}}

    async def mark(base, token, platform, account, external_id):
        writes.append((platform, external_id))
        states[(platform, external_id)] = True

    h.app.state.watch_archive.external.read = read
    h.app.state.watch_archive.external.mark_played = mark
    monkeypatch.setattr("movieclaw_friends.watch_alignment.inspect_item", inspect)
    return sources, writes, states


async def preview(h, sources, *, native=False, native_records=None):
    response = await h.clients["alice"].post(
        "/api/watch/alignment/preview",
        json={
            "source_ids": list(sources.values()),
            "include_native": native,
            "native_records": native_records or [],
        },
    )
    assert response.status_code == 202, response.text
    number = response.json()["id"]
    await asyncio.gather(*list(h.app.state.watch_archive.tasks.values()))
    status = await h.clients["alice"].get(f"/api/watch/alignment/{number}")
    assert status.status_code == 200, status.text
    assert status.json()["status"] in {"ready", "native"}, status.text
    return number


async def targets(h, number):
    return (await h.clients["alice"].get(f"/api/watch/alignment/{number}/targets")).json()[
        "targets"
    ]


async def test_preview_is_read_only_exact_episode_union_and_confirmed_idempotent_write(
    harness, monkeypatch
):
    h = harness
    sources, writes, states = await setup(h, monkeypatch)
    before = h.app.state.store.all("SELECT * FROM watch_facts")
    number = await preview(h, sources)
    rows = await targets(h, number)
    assert len(rows) == 4 and len([r for r in rows if r["state"] == "pending"]) == 2
    assert writes == [] and h.app.state.store.all("SELECT * FROM watch_facts") == before
    pending = next(r for r in rows if r["state"] == "pending" and r["media"]["kind"] == "tv")
    url = f"/api/watch/alignment/{number}/apply/{pending['id']}"
    assert (await h.clients["bob"].post(url, json={"consent": True})).status_code == 404
    assert (await h.clients["alice"].post(url, json={"consent": False})).status_code == 422
    assert writes == []
    results = await asyncio.gather(
        *[h.clients["alice"].post(url, json={"consent": True}) for _ in range(2)]
    )
    assert all(r.json()["state"] == "done" for r in results)
    assert len(writes) == 1 and states[writes[0]] is True
    assert pending["media"]["season_number"] == pending["media"]["episode_number"] == 1
    assert "external_id" not in str(rows) and "personal-secret" not in str(rows)
    assert h.app.state.store.all("SELECT * FROM watch_facts") == before


async def test_uncertain_success_retry_reads_back_without_writing_twice(harness, monkeypatch):
    h = harness
    sources, writes, states = await setup(h, monkeypatch)
    number = await preview(h, sources)
    row = next(r for r in await targets(h, number) if r["state"] == "pending")

    async def lost_response(base, token, platform, account, external_id):
        writes.append((platform, external_id))
        states[(platform, external_id)] = True
        raise PeerError("结果未确认")

    h.app.state.watch_archive.external.mark_played = lost_response
    url = f"/api/watch/alignment/{number}/apply/{row['id']}"
    assert (await h.clients["alice"].post(url, json={"consent": True})).json()["state"] == "failed"
    assert (await h.clients["alice"].post(url, json={"consent": True})).json()["state"] == "skipped"
    assert len(writes) == 1


async def test_uncertain_unconfirmed_mark_is_not_blindly_replayed(harness, monkeypatch):
    h = harness
    sources, writes, _ = await setup(h, monkeypatch)
    number = await preview(h, sources)
    row = next(r for r in await targets(h, number) if r["state"] == "pending")

    async def unknown(base, token, platform, account, external_id):
        writes.append((platform, external_id))
        raise PeerError("无法确认提交结果")

    h.app.state.watch_archive.external.mark_played = unknown
    url = f"/api/watch/alignment/{number}/apply/{row['id']}"
    for _ in range(2):
        assert (await h.clients["alice"].post(url, json={"consent": True})).json()[
            "state"
        ] == "failed"
    assert len(writes) == 1


@pytest.mark.parametrize("change", ["scope", "disconnect", "credentials", "expiry", "identity"])
async def test_changed_scope_connection_expiry_or_identity_stops_writes(
    harness, monkeypatch, change
):
    h = harness
    sources, writes, _ = await setup(h, monkeypatch)
    number = await preview(h, sources)
    row = next(r for r in await targets(h, number) if r["state"] == "pending")
    store = h.app.state.store
    owner = h.users["alice"]["id"]
    if change == "scope":
        store.display_policy.catalog(owner, row["source_id"], [{"id": "1", "name": "新范围"}])
    elif change == "disconnect":
        store.execute("UPDATE watch_sources SET active=0 WHERE id=?", (row["source_id"],))
    elif change == "credentials":
        store.execute("UPDATE watch_sources SET secret='changed' WHERE id=?", (row["source_id"],))
    elif change == "expiry":
        store.execute("UPDATE alignment_jobs SET created_at='2000-01-01T00:00:00+00:00'")
    else:

        async def changed(*args):
            raise PeerError("作品身份变化")

        monkeypatch.setattr("movieclaw_friends.watch_alignment.inspect_item", changed)
    response = await h.clients["alice"].post(
        f"/api/watch/alignment/{number}/apply/{row['id']}", json={"consent": True}
    )
    assert response.status_code in {404, 409} or response.json()["state"] == "failed"
    assert writes == []


async def test_excluded_titles_unknown_libraries_and_ambiguous_targets_never_write(
    harness, monkeypatch
):
    h = harness
    inventories = {
        "plex": [
            item(550, played=True),
            item(550, external="duplicate"),
            item(999, played=True, library="AV"),
            item(88, played=True, library="unknown"),
        ]
    }
    sources, writes, _ = await setup(h, monkeypatch, inventories)
    owner = h.users["alice"]["id"]
    h.app.state.store.display_policy.catalog(
        owner, sources["plex"], [{"id": "1", "name": "电影"}, {"id": "AV", "name": "排除库"}]
    )
    h.app.state.store.execute("UPDATE display_libraries SET excluded=1 WHERE library_id='AV'")
    number = await preview(h, sources)
    rows = await targets(h, number)
    assert len(rows) == 1 and rows[0]["state"] == "ambiguous"
    assert rows[0]["media"]["tmdb_id"] == 550
    assert (
        await h.clients["alice"].post(
            f"/api/watch/alignment/{number}/apply/{rows[0]['id']}", json={"consent": True}
        )
    ).status_code == 409
    assert writes == []


@pytest.mark.parametrize("when", ["preview", "apply"])
async def test_known_excluded_copy_cannot_be_reintroduced_from_fresh_allowed_inventory(
    harness, monkeypatch, when
):
    h = harness
    sources, writes, _ = await setup(h, monkeypatch, {"plex": [item(550)]})
    policy = h.app.state.store.display_policy
    owner = h.users["alice"]["id"]
    policy.catalog(owner, sources["plex"], [{"id": "1", "name": "电影"}])
    policy.catalog(owner, "movieclaw", [{"id": "1", "name": "电影"}, {"id": "AV", "name": "排除"}])
    h.app.state.store.execute("UPDATE display_libraries SET excluded=1 WHERE library_id='AV'")

    def exclude_copy():
        policy.remember(owner, "movieclaw", [item(550, library="AV", external="excluded-copy")])

    if when == "preview":
        exclude_copy()
    number = await preview(h, sources, native_records=[item(550, played=True)])
    rows = await targets(h, number)
    if when == "preview":
        assert not rows
    else:
        assert len(rows) == 1 and rows[0]["state"] == "pending"
        exclude_copy()  # 库勾选范围没变，但来源核对新发现了排除库的同作品副本。
        response = await h.clients["alice"].post(
            f"/api/watch/alignment/{number}/apply/{rows[0]['id']}", json={"consent": True}
        )
        assert response.json()["state"] == "failed"
    assert writes == []


async def test_native_preview_requires_all_exact_units_before_ready_and_no_delegated_write(
    harness, monkeypatch
):
    h = harness
    sources, writes, _ = await setup(h, monkeypatch)
    number = await preview(h, sources, native=True)
    base = f"/api/watch/alignment/{number}"
    assert (await h.clients["alice"].post(base + "/ready")).status_code == 409
    records = (await h.clients["alice"].get(base + "/records")).json()["records"]
    batch = [
        {"canonical_key": r["canonical_key"], "state": "pending", "media_item_id": 42}
        for r in records
    ]
    assert (
        await h.clients["bob"].post(base + "/native-targets", json={"targets": batch})
    ).status_code == 404
    assert (
        await h.clients["alice"].post(base + "/native-targets", json={"targets": batch})
    ).status_code == 200
    assert (await h.clients["alice"].post(base + "/ready")).status_code == 200
    row = next(r for r in await targets(h, number) if r["source_id"] == "movieclaw")
    assert (
        await h.clients["alice"].post(base + f"/apply/{row['id']}", json={"consent": True})
    ).status_code == 404
    prepared = (
        await h.clients["alice"].post(base + f"/native-prepare/{row['id']}", json={"consent": True})
    ).json()
    assert prepared["native_target"]["media_item_id"] == 42 and writes == []
    assert (
        await h.clients["alice"].post(
            base + f"/native-result/{row['id']}", json={"consent": True, "success": True}
        )
    ).json()["state"] == "done"
    assert writes == []  # 宿主回写始终留在本人浏览器，服务端只记录结果。
