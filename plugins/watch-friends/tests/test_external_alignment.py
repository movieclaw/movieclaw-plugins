"""正式平台 HTTP 契约：完整已看/未看库存、单项身份复核与固定回写端点。"""

import httpx
import pytest
from movieclaw_friends.external_alignment import inspect_item
from movieclaw_friends.external_watch import ExternalWatchClient
from movieclaw_friends.peers import PeerError


@pytest.mark.parametrize("platform", ["emby", "plex"])
@pytest.mark.parametrize("kind", ["movie", "tv"])
async def test_real_adapter_rechecks_identity_and_only_marks_single_item(platform, kind):
    account = "a" * 32
    calls, state = [], {"played": False}
    expected = {
        "kind": kind,
        "tmdb_id": 550 if kind == "movie" else 1396,
        "season_number": 1,
        "episode_number": 2,
    }

    def handler(request):
        calls.append(request)
        assert "secret" not in str(request.url)
        assert "cookie" not in request.headers and "authorization" not in request.headers
        path = request.url.path
        if request.method != "GET":
            if platform == "emby":
                assert request.method == "POST" and path == f"/Users/{account}/PlayedItems/22"
                assert "DatePlayed" not in request.url.params
            else:
                assert request.method == "PUT" and path == "/:/scrobble"
                assert request.url.params["key"] == "22"
            assert not request.content
            state["played"] = True
            return httpx.Response(200, text="<html>OK</html>")
        if platform == "emby":
            if path == f"/Users/{account}":
                result = {"Id": account, "Name": "本人"}
            elif path.endswith("/Ancestors"):
                assert request.url.params["UserId"] == account
                result = [{"Id": "1"}]
            else:
                result = {
                    "Id": "22",
                    "Type": "Movie" if kind == "movie" else "Episode",
                    "ProviderIds": {"Tmdb": "550"},
                    "SeriesProviderIds": {"Tmdb": "1396"},
                    "ParentIndexNumber": 1,
                    "IndexNumber": 2,
                    "UserData": {"Played": state["played"], "PlaybackPositionTicks": 30000},
                }
        elif path.endswith("/44"):
            result = {"MediaContainer": {"Metadata": [{"Guid": [{"id": "tmdb://1396"}]}]}}
        else:
            result = {
                "MediaContainer": {
                    "Metadata": [
                        {
                            "ratingKey": "22",
                            "librarySectionID": 1,
                            "type": "movie" if kind == "movie" else "episode",
                            "Guid": [{"id": "tmdb://550"}],
                            "grandparentRatingKey": "44",
                            "parentIndex": 1,
                            "index": 2,
                            "viewCount": int(state["played"]),
                            "viewOffset": 30000,
                        }
                    ]
                }
            }
        return httpx.Response(200, json=result)

    client = ExternalWatchClient(transport=httpx.MockTransport(handler))
    source = {"platform": platform, "account_id": account}
    before = await inspect_item(client, source, "https://server.test", "secret", "22", expected)
    assert before == {"played": False, "library_ids": {"1"}}
    assert all(r.method == "GET" for r in calls)
    await client.mark_played("https://server.test", "secret", platform, account, "22")
    after = await inspect_item(client, source, "https://server.test", "secret", "22", expected)
    assert after["played"] is True
    assert len([r for r in calls if r.method != "GET"]) == 1
    for mismatch in (expected | {"tmdb_id": 999}, expected | {"kind": "tv", "episode_number": 3}):
        with pytest.raises(PeerError, match="身份"):
            await inspect_item(client, source, "https://server.test", "secret", "22", mismatch)
    with pytest.raises(PeerError):
        await client.mark_played("https://server.test", "secret", platform, account, "../22")


@pytest.mark.parametrize("platform", ["emby", "plex"])
@pytest.mark.parametrize("whitelist", [False, True])
async def test_inventory_reads_both_played_and_unplayed_with_library_provenance(
    platform, whitelist
):
    account = "a" * 32

    def handler(request):
        assert request.method == "GET"
        path = request.url.path
        if platform == "emby":
            if path == f"/Users/{account}":
                data = {"Id": account, "Name": "本人"}
            elif path.endswith("/Views"):
                data = {"Items": [{"Id": "1", "Name": "电影"}, {"Id": "2", "Name": "排除"}]}
            else:
                assert request.url.params.get("ParentId") == "1"
                assert "IsPlayed" not in request.url.params
                data = {
                    "TotalRecordCount": 2,
                    "Items": [
                        {
                            "Id": str(n),
                            "Name": f"电影{n}",
                            "Type": "Movie",
                            "ProviderIds": {"Tmdb": str(n)},
                            "UserData": {"Played": n == 550},
                        }
                        for n in (550, 551)
                    ],
                }
        elif path == "/library/sections":
            data = {
                "MediaContainer": {
                    "Directory": [
                        {"key": "1", "type": "movie", "title": "电影"},
                        {"key": "2", "type": "movie", "title": "排除"},
                    ]
                }
            }
        else:
            assert path == "/library/sections/1/all"
            assert "unwatched" not in request.url.params
            data = {
                "MediaContainer": {
                    "totalSize": 2,
                    "Metadata": [
                        {
                            "ratingKey": str(n),
                            "title": f"电影{n}",
                            "type": "movie",
                            "Guid": [{"id": f"tmdb://{n}"}],
                            "viewCount": int(n == 550),
                        }
                        for n in (550, 551)
                    ],
                }
            }
        return httpx.Response(200, json=data)

    client = ExternalWatchClient(transport=httpx.MockTransport(handler))
    result = await client.read(
        platform,
        "https://server.test",
        "secret",
        lambda _: None,
        account=account,
        include_libraries=True,
        only_watched=False,
        **({"included_libraries": ["1"]} if whitelist else {"excluded_libraries": ["2"]}),
    )
    assert [row["played"] for row in result["records"]] == [True, False]
    assert all(row["library_ids"] == ["1"] for row in result["records"])
