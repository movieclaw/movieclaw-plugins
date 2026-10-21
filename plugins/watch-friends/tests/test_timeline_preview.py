"""真实导入的混合时间线、公开预览和重新确认分享边界。"""

from datetime import datetime

import pytest
from movieclaw_friends.timeline import merge_history
from test_app import befriend, profile
from test_app import harness as _harness
from test_watch_archive import connect, fact, finish

harness = _harness


@pytest.mark.asyncio
async def test_timeline_merges_platforms_uses_latest_actual_date_and_pages(harness):
    h = harness
    alice = h.clients["alice"]
    plex = await connect(h, [fact(550, date="2026-03-01T10:00:00Z"), fact(900)], "plex")
    emby = await connect(h, [fact(551, date="2026-05-01T10:00:00Z")])
    for job in (plex, emby):
        await alice.post(f"/api/watch/jobs/{job['id']}/commit", json={"consent": True})

    async def native(_):
        return [fact(552, date="2026-04-01T10:00:00Z"), fact(550, date="2026-06-01T10:00:00Z")]

    h.upstream.archive = native
    job = (await alice.post("/api/watch/refresh")).json()["id"]
    await finish(h, job)
    await alice.post(f"/api/watch/jobs/{job}/commit", json={"consent": True})
    first = (await alice.get("/api/watch/records?limit=2")).json()
    second = (await alice.get(f"/api/watch/records?limit=2&offset={first['next_offset']}")).json()
    rows = first["records"] + second["records"]
    assert [row["media"]["tmdb_id"] for row in rows] == [550, 551, 552, 900]
    assert rows[0]["watched_at"] == "2026-06-01T10:00:00+00:00"
    assert set(rows[0]["sources"]) == {"plex", "movieclaw"}
    assert rows[-1]["watched_at"] is None and second["next_offset"] is None


def test_merge_history_compares_instants_not_local_strings_and_keeps_unknown_last():
    rows = [
        {"id": n, "media": fact(500 + n)["media"], "ended_at": date}
        for n, date in [(1, "2026-05-01T15:00:00+08:00"), (2, "2026-05-01T09:00:00Z"), (3, None)]
    ]
    assert [row["id"] for row in merge_history(rows)] == [2, 1, 3]


@pytest.mark.asyncio
@pytest.mark.parametrize("public", [False, True])
async def test_preview_includes_imports_without_sharing_until_confirmed(harness, public):
    h = harness
    alice, bob = h.clients["alice"], h.clients["bob"]
    relation = await befriend(h)
    rows = [
        fact(550, date="2026-04-01T10:00:00Z"),
        fact(551),
        fact(552, date="2026-05-01T10:00:00Z"),
    ]
    job = await connect(h, rows, "plex")
    await alice.post(f"/api/watch/jobs/{job['id']}/commit", json={"consent": True})

    async def empty(*args):
        return {
            "current": None,
            "history": [],
            "sharing": {"current": False, "history": True},
            "observed_at": "2026-10-20T00:00:00Z",
        }

    h.upstream.snapshot = empty
    for index, row in enumerate(rows):
        row["library_ids"] = ["2" if index == 2 else "1"]
    store = h.app.state.store
    owner = h.users["alice"]["id"]
    store.display_policy.remember(
        owner,
        job["source_id"],
        rows,
        [{"id": "1", "name": "普通电影"}, {"id": "2", "name": "私人库"}],
    )
    await alice.put(
        "/api/display-policy",
        json={
            "excluded": [{"source_id": job["source_id"], "library_id": "2"}],
            "hide_private": False,
            "exclude_statistics": False,
        },
    )
    await alice.patch("/api/profile", json=profile(share_history=True))
    assert (await bob.get(f"/api/friends/{relation}")).json()["data"]["history"] == []
    settings = profile(discoverable=public, public_recent=True, share_history=True)
    result = (await alice.post("/api/profile/preview", json=settings)).json()
    assert result["archive_included"]
    assert [row["media"]["tmdb_id"] for row in result["friend_data"]["history"]] == [550, 551]
    assert result["friend_data"]["history"][-1]["ended_at"] is None
    assert "library_ids" not in str(result) and "personal.example" not in str(result)
    assert not store.one("SELECT share_imported_history FROM users WHERE id=?", (owner,))[
        "share_imported_history"
    ]
    assert (await bob.get(f"/api/friends/{relation}")).json()["data"]["history"] == []
    saved = await alice.patch(
        "/api/profile",
        json={**settings, "public_consent": True, "preview_token": result["preview_token"]},
    )
    assert saved.status_code == 200, saved.text
    shared = (await bob.get(f"/api/friends/{relation}")).json()["data"]["history"]
    assert [row["media"]["tmdb_id"] for row in shared] == [550, 551]
    if public:
        directory = (await bob.get("/api/directory")).json()["users"][0]
        assert [row["media"]["tmdb_id"] for row in directory["history"]] == [550, 551]
    else:
        assert (await bob.get("/api/directory")).json()["users"] == []


@pytest.mark.asyncio
async def test_plugin_entry_date_is_stable_not_external_watch_date(harness):
    h = harness
    alice = h.clients["alice"]
    first = (await alice.get("/api/watch/profile")).json()["profile"]["plugin_joined_at"]
    datetime.fromisoformat(first)
    await connect(h, [fact(550, date="1980-01-01T00:00:00Z")], "plex")
    second = (await alice.get("/api/watch/profile?plugin_joined_at=1980-01-01")).json()["profile"][
        "plugin_joined_at"
    ]
    assert first == second and not first.startswith("1980")


@pytest.mark.asyncio
async def test_legacy_plugin_date_uses_first_plugin_action_and_survives_reload(harness):
    h = harness
    connected = await connect(h, [fact(550, date="1980-01-01T00:00:00Z")], "plex")
    store = h.app.state.store
    owner = h.users["alice"]["id"]
    store.execute("DELETE FROM plugin_memberships WHERE user_id=?", (owner,))
    store.execute(
        "UPDATE watch_sources SET created_at=? WHERE id=?",
        ("2026-09-01T12:00:00+00:00", connected["source_id"]),
    )
    assert store.plugin_joined_at(owner) == "2026-09-01T12:00:00+00:00"
    store.execute(
        "UPDATE watch_sources SET created_at=? WHERE id=?",
        ("2026-10-01T12:00:00+00:00", connected["source_id"]),
    )
    assert store.plugin_joined_at(owner) == "2026-09-01T12:00:00+00:00"
