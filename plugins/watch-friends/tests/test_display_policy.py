"""媒体库排除在真实 HTTP/SQLite 边界的隐私回归。"""

import asyncio
import json
from copy import deepcopy

import httpx
import pytest
from movieclaw_friends.external_watch import ExternalWatchClient
from movieclaw_friends.native_source import NativeSource
from test_app import befriend, profile
from test_app import harness as _harness
from test_watch_archive import connect, fact

harness = _harness
LIBRARIES = [{"id": "1", "name": "电影"}, {"id": "2", "name": "私人库"}]


def index(h, source="movieclaw", numbers=(1, 2, 90), owner="alice"):
    policy = h.app.state.store.display_policy
    records = []
    for number in numbers:
        media = h.upstream.watch(owner, number)["media"]
        records.append(
            {
                "external_id": str(number),
                "media": media,
                "library_ids": ["1" if number == 1 else "2"],
            }
        )
    policy.remember(h.users[owner]["id"], source, records, LIBRARIES)
    return records


async def exclude(h, source="movieclaw", *, hide_private=True):
    response = await h.clients["alice"].put(
        "/api/display-policy",
        json={
            "excluded": [{"source_id": source, "library_id": "2"}],
            "hide_private": hide_private,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.asyncio
async def test_owner_scope_unknown_sources_cross_source_deny_and_reindex(harness):
    h = harness
    policy = h.app.state.store.display_policy
    owner = h.users["alice"]["id"]
    records = index(h)
    assert policy.guard(owner)({"title": "未知", "kind": "movie"})
    assert (
        await h.clients["bob"].put(
            "/api/display-policy",
            json={
                "excluded": [{"source_id": "movieclaw", "library_id": "2"}],
            },
        )
    ).status_code == 422
    data = await exclude(h)
    assert data["restricted"] and data["hide_private"]
    assert policy.filter(owner, records) == records[:1]
    assert not policy.guard(owner)({"kind": "movie", "title": "未知"})
    duplicate = deepcopy(records[1])
    duplicate["library_ids"] = ["1"]
    policy.remember(owner, "other", [duplicate], LIBRARIES)
    assert not policy.guard(owner)(duplicate)
    # 同名、跨来源和更改本机编号不能绕过；外部编号不会成为宿主编号。
    duplicate["media"]["tmdb_id"] = None
    duplicate["media"]["media_item_id"] = 10000
    assert not policy.guard(owner)(duplicate)
    policy.remember(owner, "other", [], LIBRARIES)
    assert not policy.guard(owner)(records[1])
    # 部分更新丢失来源后必须清除旧的允许映射。
    policy.remember(owner, "movieclaw", [{**records[0], "library_ids": []}])
    assert not policy.guard(owner)(records[0])
    assert (await h.clients["bob"].get("/api/display-policy")).json()["groups"] == []
    assert not any(word in str(data) for word in ("token-", "origin", "secret"))


@pytest.mark.asyncio
async def test_saved_public_snapshots_friend_current_history_lists_and_stale_preview(harness):
    h = harness
    alice, bob = h.clients["alice"], h.clients["bob"]
    owner = h.users["alice"]["id"]
    store = h.app.state.store
    records = index(h)
    relation = await befriend(h)
    settings = profile(
        discoverable=True, public_recent=True, share_current=True, share_history=True
    )
    preview = (await alice.post("/api/profile/preview", json=settings)).json()
    assert (
        await alice.patch(
            "/api/profile",
            json={**settings, "public_consent": True, "preview_token": preview["preview_token"]},
        )
    ).status_code == 200
    stale = (await alice.post("/api/profile/preview", json=settings)).json()
    # 模拟旧的、已经获准公开的快照；变更规则不修改原始存储。
    saved = [h.upstream.watch("alice", n) for n in (1, 2, 3)]
    store.execute(
        "UPDATE users SET public_history=?,public_favorites=?,share_favorites=1 WHERE id=?",
        (json.dumps(saved), json.dumps([r["media"] for r in records[:2]]), owner),
    )
    original = store.one("SELECT public_history FROM users WHERE id=?", (owner,))
    await exclude(h)
    assert (
        await alice.patch(
            "/api/profile",
            json={**settings, "public_consent": True, "preview_token": stale["preview_token"]},
        )
    ).status_code == 400
    directory = (await bob.get("/api/directory")).json()["users"][0]
    assert [row["media"]["tmdb_id"] for row in directory["history"]] == [1001]
    assert [row["tmdb_id"] for row in directory["favorites"]] == [1001]
    public = (await bob.get(f"/api/community/profiles/{owner}")).json()["public_profile"]
    assert len(public["history"]) == len(public["favorites"]) == 1
    friend = (await bob.get(f"/api/friends/{relation}")).json()["data"]
    assert friend["current"] is None
    assert [row["media"]["tmdb_id"] for row in friend["history"]] == [1001]
    snapshot = store.display_policy.snapshot(
        owner,
        {
            "collections": [
                {"name": "混合", "items": [r["media"] for r in records]},
                {"name": "仅私人", "items": [records[1]["media"]]},
            ]
        },
    )
    assert len(snapshot["collections"]) == 1
    assert snapshot["collections"][0]["items"] == [records[0]["media"]]
    assert store.one("SELECT public_history FROM users WHERE id=?", (owner,)) == original
    assert "library" not in json.dumps(friend)


@pytest.mark.asyncio
async def test_old_feed_and_comments_hidden_new_publication_rejected(harness):
    h = harness
    alice, bob = h.clients["alice"], h.clients["bob"]
    records = index(h)
    created = await alice.post(
        "/api/feed", json={"media": records[1]["media"], "body": "旧推荐", "publish_consent": True}
    )
    assert created.status_code == 201, created.text
    path = f"/api/feed/{created.json()['id']}"
    assert (await bob.post(path + "/comments", json={"body": "旧评论"})).status_code == 201
    await exclude(h)
    assert (await bob.get("/api/feed")).json()["posts"] == []
    assert (await bob.get(path + "/comments")).status_code == 404
    assert (await bob.put(path + "/like", json={"liked": True})).status_code == 404
    assert (
        await alice.post(
            "/api/feed",
            json={"media": records[1]["media"], "body": "新推荐", "publish_consent": True},
        )
    ).status_code == 403
    assert (
        await alice.post(
            "/api/feed",
            json={"media": records[0]["media"], "body": "允许的推荐", "publish_consent": True},
        )
    ).status_code == 201


@pytest.mark.asyncio
async def test_private_archive_pagination_statistics_and_optional_self_display(harness):
    h = harness
    alice = h.clients["alice"]
    rows = [fact(550), fact(551), fact(552)]
    connected = await connect(h, rows, "plex")
    assert (
        await alice.post(f"/api/watch/jobs/{connected['id']}/commit", json={"consent": True})
    ).status_code == 200
    source = connected["source_id"]
    for pos, row in enumerate(rows):
        row["library_ids"] = ["1" if pos == 2 else "2"]
    policy = h.app.state.store.display_policy
    policy.remember(h.users["alice"]["id"], source, rows, LIBRARIES)
    count = h.app.state.store.one("SELECT COUNT(*) AS n FROM watch_facts")["n"]
    await exclude(h, source)
    result = (await alice.get("/api/watch/records?limit=1")).json()
    assert len(result["records"]) == 1 and result["next_offset"] is None
    assert result["records"][0]["media"]["tmdb_id"] == 552
    assert (await alice.get("/api/watch/profile")).json()["stats"]["movies"] == 1
    await exclude(h, source, hide_private=False)
    assert len((await alice.get("/api/watch/records")).json()["records"]) == 3
    assert (await alice.get("/api/watch/profile")).json()["stats"]["movies"] == 3
    assert not policy.guard(h.users["alice"]["id"])(rows[0])
    assert h.app.state.store.one("SELECT COUNT(*) AS n FROM watch_facts")["n"] == count


@pytest.mark.asyncio
async def test_provenance_scan_does_not_import_or_invalidate_preview_and_disconnect_race(harness):
    h = harness
    row = fact(550)
    connected = await connect(h, [row], "plex")
    store = h.app.state.store
    original = store.one("SELECT * FROM watch_jobs WHERE id=?", (connected["id"],))

    async def read(*args, **kwargs):
        assert kwargs == {"include_libraries": True}
        return {
            "account_id": "personal-id",
            "records": [{**row, "library_ids": ["2"]}],
            "libraries": LIBRARIES,
        }

    archive = h.app.state.watch_archive
    archive.external.read = read
    response = await h.clients["alice"].post("/api/display-policy/refresh")
    assert response.status_code == 202
    await asyncio.gather(*list(archive.tasks.values()))
    assert store.one("SELECT * FROM watch_jobs WHERE id=?", (connected["id"],)) == original
    assert store.one("SELECT COUNT(*) AS n FROM watch_facts")["n"] == 0
    assert store.display_policy.settings(h.users["alice"]["id"])["scan"]["processed"] == 1
    started, released = asyncio.Event(), asyncio.Event()

    async def paused(*args, **kwargs):
        started.set()
        await released.wait()
        return await read(*args, **kwargs)

    archive.external.read = paused
    await h.clients["alice"].post("/api/display-policy/refresh")
    await started.wait()
    store.execute("UPDATE watch_sources SET active=0 WHERE id=?", (connected["source_id"],))
    store.execute("DELETE FROM display_memberships")
    released.set()
    await asyncio.gather(*list(archive.tasks.values()))
    assert store.one("SELECT COUNT(*) AS n FROM display_memberships")["n"] == 0


@pytest.mark.asyncio
async def test_emby_library_membership_is_user_bound_and_not_guessed():
    result = {"account_id": "bound", "records": [fact(550, external="a"), fact(551, external="b")]}

    def respond(request):
        assert request.headers["X-Emby-Token"] == "private-key"
        assert request.url.path.startswith("/Users/bound/")
        if request.url.path.endswith("/Views"):
            return httpx.Response(
                200, json={"Items": [{"Id": "1", "Name": "电影"}, {"Id": "2", "Name": "私人库"}]}
            )
        assert request.url.params["IsPlayed"] == "true"
        keys = ["a", "b"] if request.url.params["ParentId"] == "1" else ["b", "not-watched"]
        return httpx.Response(
            200, json={"Items": [{"Id": key} for key in keys], "TotalRecordCount": len(keys)}
        )

    await ExternalWatchClient(transport=httpx.MockTransport(respond)).emby_library_provenance(
        "https://emby.test", "private-key", result
    )
    assert [row["library_ids"] for row in result["records"]] == [["1"], ["1", "2"]]
    assert result["libraries"] == LIBRARIES


@pytest.mark.asyncio
async def test_native_full_library_membership_even_when_public_id_already_known(harness):
    class Ops:
        async def call(self, operation, arguments):
            assert operation == "search.library"
            return {
                "items": [
                    {
                        "item": {
                            "media_item_id": 10,
                            "tmdb_id": 550,
                            "kind": "movie",
                            "title": "作品",
                        },
                        "library_ids": [1, 2],
                    }
                ]
            }

    h = harness
    policy = h.app.state.store.display_policy
    source = NativeSource(Ops())
    source.display_policy = policy
    rows = [
        {
            "id": 1,
            "media": {
                "media_item_id": 10,
                "library_id": 1,
                "kind": "movie",
                "tmdb_id": 550,
                "title": "作品",
            },
        }
    ]
    assert await source._enrich_media(rows)
    assert rows[0]["media"]["_display_library_ids"] == [1, 2]
    policy.catalog(h.users["alice"]["id"], "movieclaw", LIBRARIES)
    policy.remember_native("alice", rows)
    await exclude(h)
    assert not policy.guard(h.users["alice"]["id"])(rows[0])


@pytest.mark.asyncio
async def test_bad_full_catalog_rolls_back_and_keeps_exclusion(harness):
    from fastapi import HTTPException

    h = harness
    records = index(h)
    await exclude(h)
    policy = h.app.state.store.display_policy
    owner = h.users["alice"]["id"]
    before = h.app.state.store.all("SELECT * FROM display_memberships")
    with pytest.raises(HTTPException):
        policy.remember(owner, "movieclaw", [], [{"id": "bad/path", "name": "无效"}])
    assert h.app.state.store.all("SELECT * FROM display_memberships") == before
    assert not policy.guard(owner)(records[1])
    assert policy.guard(owner)(records[0])


@pytest.mark.asyncio
async def test_source_scan_rejects_changed_external_account_without_touching_index(harness):
    h = harness
    connected = await connect(h, [fact(550)], "plex")
    archive = h.app.state.watch_archive

    async def changed(*args, **kwargs):
        return {
            "account_id": "different-person",
            "records": [{**fact(551), "library_ids": ["1"]}],
            "libraries": LIBRARIES,
        }

    archive.external.read = changed
    await h.clients["alice"].post("/api/display-policy/refresh")
    await asyncio.gather(*list(archive.tasks.values()))
    data = (await h.clients["alice"].get("/api/display-policy")).json()
    assert data["scan"]["errors"] and data["groups"] == []
    assert (
        h.app.state.store.one(
            "SELECT latest_job FROM watch_sources WHERE id=?", (connected["source_id"],)
        )["latest_job"]
        == connected["id"]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("hide_history", [False, True])
@pytest.mark.parametrize("exclude_stats", [False, True])
async def test_footprints_and_statistics_independent_with_public_exclusion_always_on(
    harness, hide_history, exclude_stats
):
    h = harness
    alice = h.clients["alice"]
    rows = [fact(550), fact(551), fact(552)]
    connected = await connect(h, rows, "plex")
    await alice.post(f"/api/watch/jobs/{connected['id']}/commit", json={"consent": True})
    for index, row in enumerate(rows):
        row["library_ids"] = ["1" if index == 2 else "2"]
    store = h.app.state.store
    owner = h.users["alice"]["id"]
    store.display_policy.remember(owner, connected["source_id"], rows, LIBRARIES)
    before = store.all("SELECT * FROM watch_facts")
    response = await alice.put(
        "/api/display-policy",
        json={
            "excluded": [{"source_id": connected["source_id"], "library_id": "2"}],
            "hide_private": hide_history,
            "exclude_statistics": exclude_stats,
        },
    )
    assert response.status_code == 200
    saved = response.json()
    assert saved["hide_private"] is hide_history
    assert saved["exclude_statistics"] is exclude_stats
    profile = (await alice.get("/api/watch/profile")).json()
    assert profile["stats"]["movies"] == (1 if exclude_stats else 3)
    assert profile["display_scope"] == {
        "history_hidden": hide_history,
        "statistics_excluded": exclude_stats,
    }
    records = (await alice.get("/api/watch/records")).json()["records"]
    assert len(records) == (1 if hide_history else 3)
    assert store.all("SELECT * FROM watch_facts") == before
    assert not store.display_policy.guard(owner)(rows[0])
    bob = (await h.clients["bob"].get("/api/display-policy")).json()
    assert bob["hide_private"] is True and bob["exclude_statistics"] is True


@pytest.mark.asyncio
async def test_split_setting_migration_keeps_each_legacy_choice(harness):
    from movieclaw_friends.display_policy import DisplayPolicy

    h = harness
    store = h.app.state.store
    for user, choice in (("alice", False), ("bob", True)):
        store.execute(
            "INSERT INTO display_settings(user_id,hide_private) VALUES(?,?)",
            (h.users[user]["id"], choice),
        )
    store.execute("ALTER TABLE display_settings DROP COLUMN exclude_statistics")
    policy = DisplayPolicy(store)
    for user, expected in (("alice", False), ("bob", True), ("charlie", True)):
        settings = policy.settings(h.users[user]["id"])
        assert settings["hide_private"] is expected
        assert settings["exclude_statistics"] is expected
    # 再次加载不能重新覆盖已经独立保存的选择。
    store.execute(
        "UPDATE display_settings SET exclude_statistics=1 WHERE user_id=?",
        (h.users["alice"]["id"],),
    )
    assert DisplayPolicy(store).settings(h.users["alice"]["id"])["exclude_statistics"] is True
    assert not policy.private_hidden(h.users["alice"]["id"])


async def test_shared_list_cover_follows_its_movie_privacy_scope(harness):
    h = harness
    owner = h.users["alice"]["id"]
    records = index(h)
    await exclude(h)
    policy = h.app.state.store.display_policy
    public, private = (row["media"] for row in records[:2])
    snapshot = {
        "collections": [
            {
                "id": -1,
                "name": "混合片单",
                "items": [public, private],
                "cover": {**private, "cover_path": "/private-poster.jpg", "cover_type": "poster"},
            }
        ]
    }
    result = policy.snapshot(owner, snapshot)
    assert result["collections"][0]["items"] == [public]
    assert "cover" not in result["collections"][0]
    assert snapshot["collections"][0]["cover"]["cover_path"] == "/private-poster.jpg"
