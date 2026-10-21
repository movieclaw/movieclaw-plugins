"""真实只读适配器核对包含未看单集的完整目录，防止把已看列表当成全集。"""

import httpx
import pytest
from movieclaw_friends.external_watch import ExternalWatchClient
from movieclaw_friends.peers import PeerError
from movieclaw_friends.series_platforms import read_catalog


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", ["emby", "plex"])
@pytest.mark.parametrize("bad_identity", [False, True])
async def test_full_platform_directory_includes_unwatched_and_ignores_specials(
    platform, bad_identity
):
    account = "a" * 32
    calls = []

    def handler(request):
        calls.append(request)
        assert request.method == "GET"
        assert request.headers["X-Emby-Token" if platform == "emby" else "X-Plex-Token"] == "secret"
        assert "secret" not in str(request.url)
        path = request.url.path
        if platform == "emby":
            if path == f"/Users/{account}":
                result = {"Id": account, "Name": "本人"}
            elif path.endswith("/Items/22"):
                result = {"SeriesId": "show"}
            elif path.endswith("/Items/show"):
                result = {"ProviderIds": {"Tmdb": "999" if bad_identity else "1396"}}
            else:
                assert "IsPlayed" not in request.url.params
                result = {
                    "TotalRecordCount": 3,
                    "Items": [
                        {
                            "Id": str(e),
                            "ParentIndexNumber": s,
                            "IndexNumber": e,
                            "UserData": {"Played": e == 1},
                        }
                        for s, e in ((1, 1), (1, 2), (0, 1))
                    ],
                }
        else:
            if path.endswith("/22"):
                result = {"MediaContainer": {"Metadata": [{"grandparentRatingKey": "44"}]}}
            elif path.endswith("/44"):
                result = {
                    "MediaContainer": {
                        "Metadata": [
                            {"Guid": [{"id": "tmdb://999" if bad_identity else "tmdb://1396"}]}
                        ]
                    }
                }
            else:
                assert path.endswith("/44/allLeaves") and "unwatched" not in request.url.params
                result = {
                    "MediaContainer": {
                        "totalSize": 3,
                        "Metadata": [
                            {"ratingKey": str(e), "parentIndex": s, "index": e, "viewCount": e == 1}
                            for s, e in ((1, 1), (1, 2), (0, 1))
                        ],
                    }
                }
        return httpx.Response(200, json=result)

    client = ExternalWatchClient(transport=httpx.MockTransport(handler))
    arguments = (
        client,
        {"platform": platform, "account_id": account},
        "https://platform.test",
        "secret",
        "22",
        1396,
    )
    if bad_identity:
        with pytest.raises(PeerError, match="身份不一致"):
            await read_catalog(*arguments)
    else:
        assert await read_catalog(*arguments) == [(1, 1), (1, 2)]
    assert calls
