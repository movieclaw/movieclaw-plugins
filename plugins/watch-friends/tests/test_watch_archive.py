"""私人导入的完整分页、跨来源去重、凭据隔离、撤回与主程序身份边界。"""

import asyncio
import base64
from copy import deepcopy

import httpx
import pytest
from movieclaw_friends.external_watch import ExternalWatchClient, canonical, timestamp
from movieclaw_friends.native_source import NativeSource
from movieclaw_friends.peers import PeerError
from movieclaw_friends.watch_archive import safe_avatar
from test_app import harness as _harness
from test_native import native as _native
from test_native_source import ACCOUNT, FakeOps

harness = _harness
native = _native


def fact(number, *, kind="movie", season=None, episode=None, external=None, date=None):
    media = {
        "title": f"作品 {number}",
        "kind": kind,
        "tmdb_id": number,
        "media_item_id": 987,
        "file_path": "/private.mkv",
        "token": "not-public",
    }
    if kind == "tv":
        media.update(season_number=season, episode_number=episode)
    return {
        "external_id": str(external or f"{number}-{season}-{episode}"),
        "media": media,
        "watched_at": date,
    }


async def finish(harness, job):
    await asyncio.gather(*list(harness.app.state.watch_archive.tasks.values()))
    return (await harness.clients["alice"].get(f"/api/watch/jobs/{job}")).json()


async def connect(harness, records, platform="emby"):
    async def read(*args, **kwargs):
        return {"account_id": "personal-id", "account_name": "本人", "records": deepcopy(records)}

    harness.app.state.watch_archive.external.read = read
    response = await harness.clients["alice"].post(
        "/api/watch/connections",
        json={
            "platform": platform,
            "emby_user": "my-account" if platform == "emby" else "",
            "url": "https://personal.example",
            "token": "personal-secret",
        },
    )
    assert response.status_code == 202, response.text
    await finish(harness, response.json()["id"])
    return response.json()


@pytest.mark.asyncio
async def test_preview_is_private_and_confirmation_deduplicates_across_sources(harness):
    records = [
        fact(550),
        fact(550, external="repeat"),
        fact(1396, kind="tv", season=1, episode=1),
        fact(1396, kind="tv", season=1, episode=2),
        fact(None, external="missing-tmdb"),
    ]
    alice, bob = harness.clients["alice"], harness.clients["bob"]
    result = await connect(harness, records)
    job = result["id"]
    assert (await alice.get("/api/watch/profile")).json()["stats"]["movies"] == 0
    assert (await bob.get(f"/api/watch/jobs/{job}")).status_code == 404
    assert (
        await bob.post(f"/api/watch/jobs/{job}/commit", json={"consent": True})
    ).status_code == 404
    summary = (await alice.get(f"/api/watch/jobs/{job}")).json()
    assert (summary["status"], summary["matched"], summary["pending"], summary["new_unique"]) == (
        "preview",
        3,
        1,
        3,
    )
    assert (
        await alice.post(f"/api/watch/jobs/{job}/commit", json={"consent": False})
    ).status_code == 422
    for _ in range(2):
        response = await alice.post(f"/api/watch/jobs/{job}/commit", json={"consent": True})
        assert response.json()["stats"] == {"movies": 1, "shows": 1, "episodes": 2, "pending": 1}
    second = await connect(harness, records[:4], "plex")
    await alice.post(f"/api/watch/jobs/{second['id']}/commit", json={"consent": True})
    profile = (await alice.get("/api/watch/profile")).json()
    assert profile["stats"] == {"movies": 1, "shows": 1, "episodes": 2, "pending": 1}
    rows = (await alice.get("/api/watch/records")).json()["records"]
    assert len(rows) == 4
    assert any(set(row["sources"]) == {"emby", "plex"} for row in rows)
    assert all(row["watched_at"] is None for row in rows)
    assert all("media_item_id" not in row["media"] for row in rows)
    assert "personal-secret" not in str(rows) and "private.mkv" not in str(rows)
    assert (await bob.get("/api/watch/records")).json()["records"] == []
    assert (await alice.get("/api/me")).json()["user"]["discoverable"] is False
    assert (await alice.get("/api/feed")).json()["posts"] == []


@pytest.mark.asyncio
async def test_credentials_encrypted_disconnect_revokes_preview_and_retains_other_source(harness):
    alice, bob = harness.clients["alice"], harness.clients["bob"]
    first = await connect(harness, [fact(550)])
    second = await connect(harness, [fact(550), fact(157336)], "plex")
    for job in (first, second):
        await alice.post(f"/api/watch/jobs/{job['id']}/commit", json={"consent": True})
    source = harness.app.state.store.one(
        "SELECT * FROM watch_sources WHERE id=?", (first["source_id"],)
    )
    assert source["secret"] != "personal-secret" and source["origin"] != "https://personal.example"
    listed = (await alice.get("/api/watch/connections")).json()
    assert "secret" not in str(listed) and "personal.example" not in str(listed)
    assert (await bob.delete(f"/api/watch/connections/{first['source_id']}")).status_code == 404
    assert (
        await alice.delete(f"/api/watch/connections/{first['source_id']}?erase=true")
    ).status_code == 200
    assert (await alice.get("/api/watch/profile")).json()["stats"]["movies"] == 2
    assert (
        await alice.post(f"/api/watch/jobs/{first['id']}/commit", json={"consent": True})
    ).status_code == 404
    assert (await alice.get(f"/api/watch/jobs/{first['id']}/records")).status_code == 404
    source = harness.app.state.store.one(
        "SELECT * FROM watch_sources WHERE id=?", (first["source_id"],)
    )
    assert source["secret"] == source["origin"] == ""


@pytest.mark.asyncio
async def test_stale_preview_and_inflight_revocation_cannot_publish_import(harness):
    alice = harness.clients["alice"]
    first = await connect(harness, [fact(550)])
    second = (await alice.post(f"/api/watch/connections/{first['source_id']}/preview")).json()
    await finish(harness, second["id"])
    assert (
        await alice.post(f"/api/watch/jobs/{first['id']}/commit", json={"consent": True})
    ).status_code == 409
    entered, release = asyncio.Event(), asyncio.Event()

    async def paused(*args, **kwargs):
        entered.set()
        await release.wait()
        return {"account_id": "personal-id", "account_name": "本人", "records": [fact(550)]}

    harness.app.state.watch_archive.external.read = paused
    third = (await alice.post(f"/api/watch/connections/{first['source_id']}/preview")).json()
    await entered.wait()
    await alice.delete(f"/api/watch/connections/{first['source_id']}")
    release.set()
    await asyncio.gather(
        *list(harness.app.state.watch_archive.tasks.values()), return_exceptions=True
    )
    assert (await alice.get("/api/watch/records")).json()["records"] == []
    assert (
        await alice.post(f"/api/watch/jobs/{third['id']}/commit", json={"consent": True})
    ).status_code == 404


@pytest.mark.asyncio
async def test_unmatched_episode_requires_explicit_valid_episode_mapping(harness):
    alice, bob = harness.clients["alice"], harness.clients["bob"]
    result = await connect(harness, [fact(None, kind="tv", external="episode")])
    path = f"/api/watch/jobs/{result['id']}"
    row = (await alice.get(path + "/records")).json()["records"][0]
    match_path = f"{path}/records/{row['id']}/match"
    assert (await alice.put(match_path, json={"tmdb_id": 1396, "kind": "tv"})).status_code == 422
    assert (
        await bob.put(
            match_path,
            json={"tmdb_id": 1396, "kind": "tv", "season_number": 1, "episode_number": 1},
        )
    ).status_code == 404
    assert (
        await alice.put(
            match_path,
            json={"tmdb_id": 1396, "kind": "tv", "season_number": 1, "episode_number": 1},
        )
    ).status_code == 200
    stats = (await alice.post(path + "/commit", json={"consent": True})).json()["stats"]
    assert stats == {"movies": 0, "shows": 1, "episodes": 1, "pending": 0}
    assert (
        await alice.put(
            match_path,
            json={"tmdb_id": 1396, "kind": "tv", "season_number": 1, "episode_number": 2},
        )
    ).status_code == 409


@pytest.mark.asyncio
async def test_native_archive_reads_complete_pages_not_recent_five_and_guards_cursor():
    class Pages(FakeOps):
        async def call(self, operation, arguments):
            if operation == "members.list":
                return deepcopy(self.members)
            assert operation == "playback.history"
            assert arguments["member_id"] == 7 and arguments["limit"] == 200
            before = arguments.get("before")
            entries = []
            for number in range(201, 1, -1) if before is None else [1]:
                entries.append(
                    {
                        "id": number,
                        "completed": number != 100,
                        "media": {"tmdb_id": number, "kind": "movie", "title": f"film {number}"},
                        "ended_at": "2026-10-20T00:00:00Z",
                    }
                )
            return {"entries": entries, "next_cursor": 2 if before is None else None}

    result = await NativeSource(Pages()).archive(ACCOUNT)
    assert len(result) == 200 and result[-1]["external_id"] == "1"

    class BadPages(Pages):
        async def call(self, operation, arguments):
            result = await super().call(operation, arguments)
            if operation == "playback.history":
                result["next_cursor"] = 2
            return result

    with pytest.raises(Exception, match="分页重复"):
        await NativeSource(BadPages()).archive(ACCOUNT)


@pytest.mark.asyncio
async def test_external_emby_uses_current_user_and_resolves_series_not_episode_id():
    calls = []

    def respond(request):
        calls.append(request)
        assert request.headers["X-Emby-Token"] == "mine"
        assert "mine" not in str(request.url)
        if request.url.path == "/emby/Users/" + "a" * 32:
            return httpx.Response(200, json={"Id": "a" * 32, "Name": "本人"})
        if request.url.path == "/emby/Users/" + "a" * 32 + "/Items/series":
            return httpx.Response(200, json={"ProviderIds": {"Tmdb": "1396"}})
        assert request.url.path == "/emby/Users/" + "a" * 32 + "/Items"
        assert request.url.params["IsPlayed"] == "true"
        return httpx.Response(
            200,
            json={
                "Items": [
                    {
                        "Id": "movie",
                        "Type": "Movie",
                        "Name": "搏击俱乐部",
                        "ProviderIds": {"Tmdb": "550"},
                        "UserData": {"Played": True},
                    },
                    {
                        "Id": "episode",
                        "Type": "Episode",
                        "Name": "第一集",
                        "SeriesName": "绝命毒师",
                        "SeriesId": "series",
                        "SeriesProviderIds": {},
                        "ProviderIds": {"Tmdb": "episode-should-not-use"},
                        "ParentIndexNumber": 1,
                        "IndexNumber": 1,
                        "UserData": {"Played": True},
                    },
                    {"Id": "not-watched", "Type": "Movie", "UserData": {"Played": False}},
                ],
                "TotalRecordCount": 3,
            },
        )

    client = ExternalWatchClient(transport=httpx.MockTransport(respond))
    result = await client.read(
        "emby", "https://example.test/emby", "mine", lambda _: None, account="a" * 32
    )
    assert len(result["records"]) == 2
    assert canonical(result["records"][1]["media"]) == "tmdb:tv:1396:s1:e1"
    assert all(row["watched_at"] is None for row in result["records"])


@pytest.mark.asyncio
async def test_plex_per_token_item_state_and_series_guid_not_admin_history():
    paths = []

    def respond(request):
        paths.append(request.url.path)
        assert request.headers["X-Plex-Token"] == "mine" and "mine" not in str(request.url)
        if request.url.path == "/library/sections":
            data = {"Directory": [{"key": "1", "type": "movie"}, {"key": "2", "type": "show"}]}
        elif request.url.path == "/library/metadata/series":
            raise AssertionError("rating keys must be numeric")
        elif request.url.path == "/library/metadata/88":
            data = {"Metadata": [{"Guid": [{"id": "tmdb://1396"}]}]}
        elif request.url.path == "/library/sections/1/all":
            data = {
                "Metadata": [
                    {
                        "ratingKey": "5",
                        "type": "movie",
                        "title": "搏击俱乐部",
                        "viewCount": 2,
                        "Guid": [{"id": "tmdb://550"}],
                        "lastViewedAt": 1700000000,
                    }
                ],
                "totalSize": 1,
            }
        else:
            assert request.url.path == "/library/sections/2/all"
            assert request.url.params["type"] == "4"
            data = {
                "Metadata": [
                    {
                        "ratingKey": "8",
                        "type": "episode",
                        "grandparentTitle": "绝命毒师",
                        "grandparentRatingKey": "88",
                        "viewCount": 1,
                        "parentIndex": 1,
                        "index": 2,
                        "Guid": [{"id": "tmdb://999"}],
                    }
                ],
                "totalSize": 1,
            }
        return httpx.Response(200, json={"MediaContainer": data})

    result = await ExternalWatchClient(transport=httpx.MockTransport(respond)).read(
        "plex", "https://plex.test", "mine", lambda _: None
    )
    assert canonical(result["records"][1]["media"]) == "tmdb:tv:1396:s1:e2"
    assert result["records"][0]["watched_at"].startswith("2023-")
    assert not any("history" in path or "accounts" in path for path in paths)


@pytest.mark.parametrize(
    "url",
    [
        "http://public.test",
        "https://u:p@server.test",
        "https://server.test/?token=secret",
        "https://server.test/../admin",
        "file:///tmp/private",
        "https://server.test/#token",
    ],
)
def test_connections_reject_unsafe_addresses(url):
    with pytest.raises(PeerError):
        ExternalWatchClient().validate(url)


@pytest.mark.asyncio
async def test_external_redirect_and_upstream_error_body_never_leak_token():
    for status in (302, 401, 500):
        client = ExternalWatchClient(
            transport=httpx.MockTransport(
                lambda _, code=status: httpx.Response(
                    code, text="personal-secret", headers={"Location": "https://evil.test"}
                )
            )
        )
        with pytest.raises(PeerError) as error:
            await client.get("https://personal.test", "/Users/Me", "personal-secret", "emby")
        assert "personal-secret" not in str(error.value)


@pytest.mark.asyncio
async def test_avatar_thumbnail_is_user_bound_and_untrusted_url_rejected(harness):
    alice, bob = harness.clients["alice"], harness.clients["bob"]
    thumb = "data:image/jpeg;base64," + base64.b64encode(b"\xff\xd8\xffthumbnail\xff\xd9").decode()
    assert safe_avatar(thumb) == thumb
    for value in [
        "https://private.test/?token=secret",
        "data:image/svg+xml;base64,PHN2Zz4=",
        "data:image/jpeg;base64,aGVsbG8=",
    ]:
        assert safe_avatar(value) is None
        assert (await alice.put("/api/watch/avatar", json={"data": value})).status_code == 422
    result = await alice.put("/api/watch/avatar", json={"data": thumb})
    assert result.status_code == 200
    user = harness.users["alice"]["id"]
    response = await bob.get(f"/api/watch/avatar/{user}")
    assert response.content.startswith(b"\xff\xd8\xff")
    assert (await alice.get(f"/api/watch/avatar/{harness.users['bob']['id']}")).status_code == 404
    await alice.put("/api/watch/avatar", json={"data": None})
    assert (await bob.get(f"/api/watch/avatar/{user}")).status_code == 404


def test_canonical_unknown_dates_and_no_whole_series_marking():
    assert timestamp("invalid-date") is None and timestamp(None) is None
    assert canonical({"kind": "movie", "tmdb_id": True}) is None
    assert canonical({"kind": "tv", "tmdb_id": 1396}) is None
    assert (
        canonical({"kind": "tv", "tmdb_id": 1396, "season_number": "1", "episode_number": 1})
        is None
    )


@pytest.mark.asyncio
async def test_native_join_time_name_and_avatar_cannot_be_taken_from_request(native):
    from test_native import PREFIX

    alice, bob = native.clients["alice"], native.clients["bob"]
    response = await alice.get(PREFIX + "/watch/profile?joined_at=2000-01-01&nickname=bob")
    profile = response.json()["profile"]
    assert profile["nickname"] == "相同昵称" and profile["joined_at"].startswith("2026-01-01")
    thumb = "data:image/jpeg;base64," + base64.b64encode(b"\xff\xd8\xffalice\xff\xd9").decode()
    assert (
        await alice.put(PREFIX + "/watch/avatar", json={"data": thumb, "user_id": "bob"})
    ).status_code == 200
    own = (await alice.get(PREFIX + "/me")).json()["user"]
    other = (await bob.get(PREFIX + "/me")).json()["user"]
    assert "/watch/avatar/" + own["id"] in own["avatar_url"]
    assert "avatar_url" not in other
    assert (await bob.get(PREFIX + "/watch/profile")).json()["stats"]["movies"] == 0
    assert (await alice.patch(PREFIX + "/community/profile", json={"avatar_id": "xiaolu"})).json()[
        "profile"
    ]["avatar_id"] == "initials"
    assert (await bob.get(PREFIX + "/watch/avatar/" + own["id"])).content.endswith(b"alice\xff\xd9")
    assert (await alice.get(PREFIX + "/watch/avatar/" + other["id"])).status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "selection,expected_error",
    [("", "请先指定"), ("missing", "唯一匹配"), ("duplicate", "唯一匹配")],
)
async def test_emby_never_guesses_first_user_or_uses_users_me(selection, expected_error):
    paths = []

    def respond(request):
        paths.append(request.url.path)
        assert request.url.path == "/Users/Query"
        rows = [{"Id": "a" * 32, "Name": "someone-else"}]
        if selection == "duplicate":
            rows = [{"Id": "a" * 32, "Name": "duplicate"}, {"Id": "b" * 32, "Name": "DUPLICATE"}]
        return httpx.Response(200, json={"Items": rows})

    with pytest.raises(PeerError, match=expected_error):
        await ExternalWatchClient(transport=httpx.MockTransport(respond)).read(
            "emby", "https://emby.test", "private-token", lambda _: None, account=selection
        )
    assert not paths if not selection else paths == ["/Users/Query"]


@pytest.mark.asyncio
async def test_emby_server_key_uses_exact_selected_user_and_never_reads_other_history():
    paths = []

    def respond(request):
        paths.append(request.url.path)
        assert request.headers["X-Emby-Token"] == "server-key"
        if request.url.path == "/Users/Query":
            assert request.url.params["NameStartsWithOrGreater"] == "my-account"
            return httpx.Response(
                200,
                json={
                    "Items": [
                        {"Id": "b" * 32, "Name": "someone-else"},
                        {"Id": "a" * 32, "Name": "my-account"},
                    ]
                },
            )
        assert request.url.path == "/Users/" + "a" * 32 + "/Items"
        return httpx.Response(200, json={"Items": [], "TotalRecordCount": 0})

    client = ExternalWatchClient(transport=httpx.MockTransport(respond))
    result = await client.read(
        "emby", "https://emby.test", "server-key", lambda _: None, account="my-account"
    )
    assert result["account_id"] == "a" * 32 and result["records"] == []
    assert "/Users/Me" not in paths


@pytest.mark.asyncio
async def test_emby_personal_token_can_use_id_without_admin_user_query():
    def respond(request):
        if request.url.path == "/Users/Query":
            return httpx.Response(403, text="private-token must never leak")
        if request.url.path == "/Users/" + "a" * 32:
            return httpx.Response(200, json={"Id": "a" * 32, "Name": "my-account"})
        assert request.url.path == "/Users/" + "a" * 32 + "/Items"
        return httpx.Response(200, json={"Items": [], "TotalRecordCount": 0})

    client = ExternalWatchClient(transport=httpx.MockTransport(respond))
    with pytest.raises(PeerError, match="用户 ID") as error:
        await client.read(
            "emby", "https://emby.test", "private-token", lambda _: None, account="my-account"
        )
    assert "private-token" not in str(error.value)
    assert (
        await client.read(
            "emby", "https://emby.test", "private-token", lambda _: None, account="a" * 32
        )
    )["account_id"] == "a" * 32


@pytest.mark.asyncio
async def test_emby_rejects_disabled_or_mismatched_user_id_before_reading(harness):
    for user in (
        {"Id": "b" * 32, "Name": "different"},
        {"Id": "a" * 32, "Name": "disabled", "Policy": {"IsDisabled": True}},
    ):

        def respond(request, row=user):
            assert request.url.path == "/Users/" + "a" * 32
            return httpx.Response(200, json=row)

        with pytest.raises(PeerError):
            await ExternalWatchClient(transport=httpx.MockTransport(respond)).read(
                "emby", "https://emby.test", "mine", lambda _: None, account="a" * 32
            )


@pytest.mark.asyncio
async def test_legacy_emby_connection_can_bind_once_without_new_token_or_touching_plex(harness):
    alice, bob = harness.clients["alice"], harness.clients["bob"]
    store = harness.app.state.store
    plex = await connect(harness, [fact(550)], "plex")
    await alice.post(f"/api/watch/jobs/{plex['id']}/commit", json={"consent": True})
    store.execute(
        "INSERT INTO watch_sources(id,user_id,platform,origin,secret,created_at) "
        "VALUES(?,?,?,?,?,?)",
        (
            "old-emby",
            harness.users["alice"]["id"],
            "emby",
            store.encrypt("https://emby.test"),
            store.encrypt("existing-secret"),
            "2026-10-20",
        ),
    )
    secret = store.one("SELECT secret FROM watch_sources WHERE id='old-emby'")["secret"]
    bound_calls = []

    async def read(platform, base, token, progress, **kwargs):
        bound_calls.append((platform, token, kwargs))
        return {"account_id": "a" * 32, "account_name": "my-account", "records": [fact(157336)]}

    harness.app.state.watch_archive.external.read = read
    path = "/api/watch/connections/old-emby/account"
    assert (
        await bob.put(path, json={"emby_user": "my-account", "consent": True})
    ).status_code == 404
    assert (
        await alice.put(path, json={"emby_user": "my-account", "consent": False})
    ).status_code == 422
    result = await alice.put(path, json={"emby_user": "my-account", "consent": True})
    assert result.status_code == 202
    assert (await finish(harness, result.json()["id"]))["status"] == "preview"
    assert bound_calls == [("emby", "existing-secret", {"account": "my-account"})]
    assert store.one("SELECT secret FROM watch_sources WHERE id='old-emby'")["secret"] == secret
    assert (
        await alice.put(path, json={"emby_user": "someone-else", "consent": True})
    ).status_code == 409
    assert (await alice.get("/api/watch/profile")).json()["stats"]["movies"] == 1
    assert (await alice.get("/api/watch/records")).json()["records"][0]["sources"] == ["plex"]
    # Re-read the same bound ID rather than resolving a possibly renamed username again.
    job = (await alice.post("/api/watch/connections/old-emby/preview")).json()
    await finish(harness, job["id"])
    assert bound_calls[-1][2] == {"account": "a" * 32}


@pytest.mark.asyncio
async def test_emby_requires_explicit_selection_at_connection_boundary(harness):
    response = await harness.clients["alice"].post(
        "/api/watch/connections",
        json={"platform": "emby", "url": "https://emby.test", "token": "mine"},
    )
    assert response.status_code == 422 and "用户名" in response.json()["detail"]
    assert harness.app.state.store.one("SELECT COUNT(*) AS n FROM watch_sources")["n"] == 0


@pytest.mark.asyncio
async def test_emby_schema_migration_preserves_plex_imported_facts_and_credentials(harness):
    from movieclaw_friends.watch_archive import WatchArchive

    alice = harness.clients["alice"]
    result = await connect(harness, [fact(550), fact(1396, kind="tv", season=1, episode=1)], "plex")
    await alice.post(f"/api/watch/jobs/{result['id']}/commit", json={"consent": True})
    store = harness.app.state.store
    before = store.all("SELECT * FROM watch_facts")
    sources = store.all("SELECT * FROM watch_sources")
    store.execute("ALTER TABLE watch_sources DROP COLUMN account_selector")
    migrated = WatchArchive(store, harness.upstream, harness.app.state.settings)
    assert store.all("SELECT * FROM watch_facts") == before
    after = store.all("SELECT * FROM watch_sources")
    assert after == sources and after[0]["account_selector"] == ""
    await migrated.close()
