"""真实适配器协议：保留 Emby 收藏/次数、Plex 时间线单位及活动查询。"""

import json

import httpx
import pytest
from movieclaw_friends.external_watch import ExternalWatchClient
from movieclaw_friends.peers import PeerError


@pytest.mark.parametrize("platform", ["emby", "plex"])
async def test_real_position_write_protocol_and_active_sessions(platform):
    calls = []
    account = "a" * 32

    async def handler(request):
        calls.append(request)
        if request.url.path == "/Sessions":
            return httpx.Response(
                200,
                json=[
                    {"UserId": account, "NowPlayingItem": {"Id": "22"}},
                    {"UserId": "other", "NowPlayingItem": {"Id": "44"}},
                ],
            )
        if request.url.path == "/status/sessions":
            return httpx.Response(200, json={"MediaContainer": {"Metadata": [{"ratingKey": "22"}]}})
        assert "private-token" not in str(request.url)
        if platform == "emby":
            assert (
                request.method == "POST"
                and request.url.path == f"/Users/{account}/Items/22/UserData"
            )
            body = json.loads(request.content)
            assert body == {
                "PlaybackPositionTicks": 4500000000,
                "PlayCount": 4,
                "IsFavorite": True,
                "Played": False,
                "LastPlayedDate": "2026-01-01T00:00:00Z",
            }
        else:
            assert request.method == "GET" and request.url.path == "/:/timeline"
            assert request.url.params["state"] == "stopped"
            assert request.url.params["time"] == "450000"
            assert request.url.params["duration"] == "7200000"
            assert request.url.params["ratingKey"] == "22"
        return httpx.Response(200, text="<Response/>")

    client = ExternalWatchClient(transport=httpx.MockTransport(handler))
    source = {"platform": platform, "account_id": account}
    active = await client.active_items(source, "https://server.test", "private-token")
    assert active == {"22"}
    await client.set_position(
        source,
        "https://server.test",
        "private-token",
        "22",
        450000,
        7200000,
        {
            "userdata": {
                "PlaybackPositionTicks": 900,
                "PlayCount": 4,
                "IsFavorite": True,
                "Played": False,
                "LastPlayedDate": "2026-01-01T00:00:00Z",
            }
        },
    )
    assert len(calls) == 2


async def test_emby_incomplete_userdata_does_not_reset_counts_or_favorites():
    async def handler(request):
        raise AssertionError("不应发送写请求")

    client = ExternalWatchClient(transport=httpx.MockTransport(handler))
    with pytest.raises(PeerError, match="次数和收藏"):
        await client.set_position(
            {"platform": "emby", "account_id": "a" * 32},
            "https://server.test",
            "private-token",
            "22",
            450000,
            7200000,
            {"userdata": {"Played": False}},
        )
