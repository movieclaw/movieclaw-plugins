"""完整季集集合、跨源去重、分页和隐私边界下的全集折叠。"""

import pytest
from test_app import harness as _harness
from test_watch_archive import connect, fact

harness = _harness


async def imported(h, rows, platform="plex"):
    result = await connect(h, rows, platform)
    response = await h.clients["alice"].post(
        f"/api/watch/jobs/{result['id']}/commit", json={"consent": True}
    )
    assert response.status_code == 200
    return result


async def catalog(h, *, status="ended", counts=((1, 2),), number=1396):
    result = await h.clients["alice"].put(
        "/api/watch/series-catalog",
        json={
            "tmdb_id": number,
            "status": status,
            "seasons": [{"number": s, "episode_count": n} for s, n in counts],
        },
    )
    assert result.status_code == 200, result.text


@pytest.mark.asyncio
async def test_full_series_folds_before_paging_cross_source_and_keeps_facts_stats(harness):
    h = harness
    one = fact(1396, kind="tv", season=1, episode=1, date="2026-01-01T00:00:00Z")
    two = fact(1396, kind="tv", season=1, episode=2, date="2026-03-01T00:00:00Z")
    await imported(h, [one, fact(550, date="2026-02-01T00:00:00Z")])
    await imported(h, [one, two], "emby")
    before = h.app.state.store.all("SELECT * FROM watch_facts")
    stats = (await h.clients["alice"].get("/api/watch/profile")).json()["stats"]
    await catalog(h)
    first = (await h.clients["alice"].get("/api/watch/footprints?limit=1")).json()
    complete = first["records"][0]
    assert complete["series_complete"] and complete["episode_count"] == 2
    assert complete["media"]["season_number"] is None
    assert set(complete["sources"]) == {"plex", "emby"}
    assert complete["watched_at"].startswith("2026-03")
    second = (
        await h.clients["alice"].get(f"/api/watch/footprints?limit=1&offset={first['next_offset']}")
    ).json()
    assert second["records"][0]["media"]["tmdb_id"] == 550 and second["next_offset"] is None
    assert len((await h.clients["alice"].get("/api/watch/records")).json()["records"]) == 3
    episodes = (await h.clients["alice"].get("/api/watch/series/1396/records")).json()["records"]
    assert [row["media"]["episode_number"] for row in episodes] == [1, 2]
    assert (await h.clients["bob"].get("/api/watch/series/1396/records")).status_code == 404
    assert h.app.state.store.all("SELECT * FROM watch_facts") == before
    assert (await h.clients["alice"].get("/api/watch/profile")).json()["stats"] == stats


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "episodes,status,counts",
    [
        ([1, 3], "ended", ((1, 3),)),
        ([1, 3], "ended", ((1, 2),)),
        ([1, 2], "unknown", ()),
        ([1, 2], "ended", ((1, 2), (2, 2))),
    ],
)
async def test_incomplete_or_unknown_catalog_groups_without_claiming_completion(
    harness, episodes, status, counts
):
    h = harness
    await imported(h, [fact(1396, kind="tv", season=1, episode=e) for e in episodes])
    await catalog(h, status=status, counts=counts)
    rows = (await h.clients["alice"].get("/api/watch/footprints")).json()["records"]
    assert len(rows) == 1 and rows[0]["series_grouped"] and not rows[0]["series_complete"]
    assert rows[0]["episode_count"] == len(episodes)
    assert (rows[0]["progress_season"], rows[0]["progress_episode"]) == (1, max(episodes))


@pytest.mark.asyncio
async def test_multi_season_specials_and_stale_metadata(harness):
    h = harness
    rows = [fact(1396, kind="tv", season=s, episode=e) for s, e in [(1, 1), (1, 2), (2, 1), (0, 1)]]
    await imported(h, rows)
    await catalog(h, counts=((1, 2), (2, 1)))
    result = (await h.clients["alice"].get("/api/watch/footprints")).json()["records"]
    assert len(result) == 2
    complete = next(row for row in result if row.get("series_complete"))
    assert complete["episode_count"] == 3 and complete["season_count"] == 2
    h.app.state.store.execute(
        "UPDATE series_footprint_catalog SET checked_at='2000-01-01T00:00:00+00:00'"
    )
    stale = (await h.clients["alice"].get("/api/watch/footprints")).json()["records"]
    assert len(stale) == 2 and not any(row.get("series_complete") for row in stale)
    assert next(row for row in stale if row.get("series_grouped"))["progress_season"] == 2
    assert (await h.clients["alice"].get("/api/watch/series-progress")).json()["series"][0][
        "tmdb_id"
    ] == 1396


@pytest.mark.asyncio
async def test_private_scope_hides_series_and_does_not_leak_catalog_to_other_users(harness):
    h = harness
    rows = [fact(1396, kind="tv", season=1, episode=e) for e in (1, 2)]
    source = await imported(h, rows)
    for row in rows:
        row["library_ids"] = ["2"]
    policy = h.app.state.store.display_policy
    owner = h.users["alice"]["id"]
    policy.remember(owner, source["source_id"], rows, [{"id": "2", "name": "私人库"}])
    await catalog(h)
    await h.clients["alice"].put(
        "/api/display-policy",
        json={
            "excluded": [{"source_id": source["source_id"], "library_id": "2"}],
            "hide_private": True,
        },
    )
    assert (await h.clients["alice"].get("/api/watch/footprints")).json()["records"] == []
    assert (await h.clients["bob"].get("/api/watch/series-progress")).json()["series"] == []
    assert (
        await h.clients["bob"].put(
            "/api/watch/series-catalog",
            json={
                "tmdb_id": 1396,
                "status": "ended",
                "seasons": [{"number": 1, "episode_count": 2}],
            },
        )
    ).status_code == 404


@pytest.mark.asyncio
async def test_full_catalog_folds_without_requiring_series_ended(harness):
    h = harness
    await imported(h, [fact(1396, kind="tv", season=1, episode=e) for e in (1, 2)])
    await catalog(h, status="airing")
    rows = (await h.clients["alice"].get("/api/watch/footprints")).json()["records"]
    assert len(rows) == 1 and rows[0]["completion_basis"] == "airing"


@pytest.mark.asyncio
@pytest.mark.parametrize("split", [False, True])
async def test_platform_catalog_and_combined_platform_catalog_fold_union_facts(
    harness, monkeypatch, split
):
    h = harness
    await imported(h, [fact(1396, kind="tv", season=1, episode=1)], "plex")
    await imported(h, [fact(1396, kind="tv", season=1, episode=2)], "emby")
    before = h.app.state.store.all("SELECT * FROM watch_facts")
    calls = []

    async def read(client, source, base, token, episode_id, number):
        calls.append(source["platform"])
        assert number == 1396
        return [(1, 1 if source["platform"] == "plex" else 2)] if split else [(1, 1), (1, 2)]

    monkeypatch.setattr("movieclaw_friends.series_footprints.read_catalog", read)
    # 元数据预告有第三集，但平台实际目录只包含已看完的两集；不谎称整剧完结。
    await catalog(h, status="airing", counts=((1, 3),))
    url = "/api/watch/series/1396/platform-catalogs"
    assert (await h.clients["bob"].post(url)).status_code == 404
    assert (await h.clients["alice"].post(url)).json()["failed_sources"] == 0
    rows = (await h.clients["alice"].get("/api/watch/footprints")).json()["records"]
    assert len(rows) == 1 and rows[0]["completion_basis"] == "platform"
    assert rows[0]["episode_count"] == 2
    assert set(rows[0]["sources"]) == {"plex", "emby"}
    assert h.app.state.store.all("SELECT * FROM watch_facts") == before
    await h.clients["alice"].post(url)
    assert len(calls) == 2  # 一天内重复核对不反复访问外部服务器。
    h.app.state.store.execute("UPDATE watch_sources SET active=0")
    incomplete = (await h.clients["alice"].get("/api/watch/footprints")).json()["records"]
    assert len(incomplete) == 1 and incomplete[0]["series_grouped"]
    assert not incomplete[0]["series_complete"]


@pytest.mark.asyncio
async def test_native_inventory_and_missing_episode_keep_correct_private_display(harness):
    h = harness
    await imported(h, [fact(1396, kind="tv", season=1, episode=e) for e in (1, 2)])
    payload = {
        "tmdb_id": 1396,
        "status": "unknown",
        "native_episodes": [{"season": 1, "episode": 1}, {"season": 1, "episode": 2}],
    }
    await h.clients["alice"].put("/api/watch/series-catalog", json=payload)
    rows = (await h.clients["alice"].get("/api/watch/footprints")).json()["records"]
    assert len(rows) == 1 and rows[0]["completion_basis"] == "platform"
    payload["native_episodes"].append({"season": 1, "episode": 3})
    await h.clients["alice"].put("/api/watch/series-catalog", json=payload)
    incomplete = (await h.clients["alice"].get("/api/watch/footprints")).json()["records"]
    assert len(incomplete) == 1 and incomplete[0]["series_grouped"]
    assert not incomplete[0]["series_complete"]


@pytest.mark.asyncio
async def test_unfinished_series_groups_across_sources_without_rewatch_regression(harness):
    h = harness
    latest = fact(1396, kind="tv", season=1, episode=1, date="2026-10-20T12:00:00Z")
    await imported(h, [latest, fact(550, date="2026-10-19T12:00:00Z")])
    await imported(
        h, [fact(1396, kind="tv", season=2, episode=3, date="2026-10-18T12:00:00Z")], "emby"
    )
    before = h.app.state.store.all("SELECT * FROM watch_facts")
    stats = (await h.clients["alice"].get("/api/watch/profile")).json()["stats"]
    result = (await h.clients["alice"].get("/api/watch/footprints?limit=1")).json()
    row = result["records"][0]
    assert row["series_grouped"] and not row["series_complete"]
    assert (row["progress_season"], row["progress_episode"]) == (2, 3)
    assert row["episode_count"] == 2  # 跳看不把中间未看的集数计入。
    assert row["watched_at"].startswith("2026-10-20")
    assert row["media"]["season_number"] is None
    assert set(row["sources"]) == {"plex", "emby"}
    next_page = (await h.clients["alice"].get("/api/watch/footprints?limit=1&offset=1")).json()
    assert next_page["records"][0]["media"]["tmdb_id"] == 550
    assert next_page["next_offset"] is None
    assert (
        len((await h.clients["alice"].get("/api/watch/series/1396/records")).json()["records"]) == 2
    )
    assert (await h.clients["alice"].get("/api/watch/profile")).json()["stats"] == stats
    assert h.app.state.store.all("SELECT * FROM watch_facts") == before
    assert (await h.clients["bob"].get("/api/watch/footprints")).json()["records"] == []
