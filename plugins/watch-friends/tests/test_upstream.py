"""只读宿主适配测试：真实信封/分页、按人隔离、授权开关和外发字段。"""

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from movieclaw_friends.media import safe_poster_path, sanitize_media
from movieclaw_friends.upstream import MovieClawClient, UpstreamError


def ok(data):
    return httpx.Response(200, json={"success": True, "code": "OK", "data": data})


def own_collection(**changes):
    return {
        "id": 3,
        "name": "我的片单",
        "item_count": 0,
        "manageable": True,
        "editable": True,
        "builtin": None,
        "kind": "user",
    } | changes


def record(id=1, *, ended_at=None, **extra):
    return {
        "id": id,
        "member_name": "会重名的昵称",
        "media": {
            "media_item_id": 101,
            "kind": "movie",
            "title": "测试影片",
            "tmdb_id": 202,
            "year": 2026,
            "season_number": 0,
            "episode_number": 0,
            "episode_title": None,
            "poster_url": "https://image.tmdb.org/t/p/w500/poster.jpg",
            "file_path": "/private/media/movie.mkv",
            "stream_url": "https://server/stream?token=private-token",
            "token": "private-token",
        },
        # 开始时间很早仍可能在播；当前版真实响应没有 last_seen_at。
        "started_at": "2026-01-01T00:00:00Z",
        "ended_at": ended_at,
        "watched_ms": 120000,
        "end_position_ms": 130000,
        "device_name": "卧室电视",
        "client": "播放器",
        "device_id": "private-device",
        "token": "private-token",
        **extra,
    }


def member_routes(request):
    assert request.method == "GET"
    if request.url.path == "/api/v1/auth/me":
        assert request.headers["Authorization"] == "Bearer member-token"
        return ok({"username": "alice", "nickname": "同名", "role": "member"})
    if request.url.path == "/api/v1/members":
        assert request.headers["Authorization"] == "Bearer admin-token"
        return ok(
            [
                {"id": 8, "username": "bob", "nickname": "同名"},
                {"id": 7, "username": "alice", "nickname": "同名"},
            ]
        )
    raise AssertionError(f"意外请求：{request.url.path}")


@pytest.mark.parametrize("address", ["", "file:///private/server", "http://[", "http://server:bad"])
def test_invalid_service_address_has_clear_error(address):
    with pytest.raises(UpstreamError, match="服务地址无效") as exc:
        MovieClawClient(address)
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_identity_revalidates_token_and_maps_unique_username():
    paths = []

    def respond(request):
        paths.append(request.url.path)
        return member_routes(request)

    client = MovieClawClient("https://movieclaw.test/", "admin-token", httpx.MockTransport(respond))
    for _ in range(2):
        assert await client.identity("member-token") == {
            "username": "alice",
            "nickname": "同名",
            "role": "member",
            "member_id": 7,
        }
    assert paths == ["/api/v1/auth/me", "/api/v1/members"] * 2


@pytest.mark.asyncio
async def test_member_without_admin_can_connect_but_cannot_publish_watching():
    client = MovieClawClient(
        "https://movieclaw.test/api/v1", transport=httpx.MockTransport(member_routes)
    )
    assert (await client.identity("member-token"))["member_id"] is None
    disabled = await client.snapshot("member-token", None, False, False)
    assert disabled["current"] is None and disabled["history"] == []
    with pytest.raises(UpstreamError, match="未配置本机管理员") as exc:
        await client.snapshot("member-token", None, True, False)
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_snapshot_rejects_different_member_before_reading_history():
    client = MovieClawClient(
        "https://movieclaw.test", "admin-token", httpx.MockTransport(member_routes)
    )
    with pytest.raises(UpstreamError, match="账号与已绑定成员不一致") as exc:
        await client.snapshot("member-token", 8, True, True)
    assert exc.value.status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "share_current,share_history", [(True, False), (False, True), (True, True)]
)
async def test_snapshot_keeps_current_and_history_consent_separate(share_current, share_history):
    def respond(request):
        if request.url.path != "/api/v1/playback/history":
            return member_routes(request)
        assert request.method == "GET"
        assert request.headers["Authorization"] == "Bearer admin-token"
        assert dict(request.url.params) == {"member_id": "7", "scope": "all", "limit": "50"}
        return ok(
            {"entries": [record(), record(2, ended_at="2026-01-01T00:02:00Z")], "has_more": False}
        )

    client = MovieClawClient("https://movieclaw.test", "admin-token", httpx.MockTransport(respond))
    result = await client.snapshot("member-token", 7, share_current, share_history)
    assert (result["current"] is not None) is share_current
    assert [row["id"] for row in result["history"]] == ([2] if share_history else [])
    assert result["sharing"] == {"current": share_current, "history": share_history}
    assert datetime.fromisoformat(result["observed_at"]).tzinfo is not None
    raw = json.dumps(result, ensure_ascii=False)
    for private in (
        "private-token",
        "private-device",
        "/private/media",
        "卧室电视",
        "播放器",
        "member_name",
    ):
        assert private not in raw


@pytest.mark.asyncio
async def test_no_sharing_does_not_read_admin_history():
    client = MovieClawClient(
        "https://movieclaw.test", "admin-token", httpx.MockTransport(member_routes)
    )
    result = await client.snapshot("member-token", 7, False, False)
    assert result["current"] is None
    assert result["history"] == []


@pytest.mark.asyncio
async def test_admin_can_read_own_history_using_own_existing_token():
    paths = []

    def respond(request):
        assert request.headers["Authorization"] == "Bearer personal-admin"
        paths.append(request.url.path)
        if request.url.path.endswith("auth/me"):
            return ok({"username": "root", "nickname": "管理员", "role": "admin"})
        assert request.url.params["member_id"] == "0"
        return ok({"entries": [record()]})

    client = MovieClawClient("https://movieclaw.test", transport=httpx.MockTransport(respond))
    assert (await client.snapshot("personal-admin", 0, True, False))["current"]["id"] == 1
    assert paths == ["/api/v1/auth/me", "/api/v1/playback/history"]


@pytest.mark.asyncio
async def test_explicit_stale_heartbeat_is_not_current():
    old = (datetime.now(UTC) - timedelta(seconds=600)).isoformat()

    def respond(request):
        if request.url.path.endswith("auth/me"):
            return ok({"username": "root", "role": "admin"})
        return ok({"entries": [record(last_seen_at=old)]})

    client = MovieClawClient("https://movieclaw.test", transport=httpx.MockTransport(respond))
    assert (await client.snapshot("admin", 0, True, False))["current"] is None


@pytest.mark.asyncio
async def test_public_history_requires_admin_and_strips_private_fields():
    with pytest.raises(UpstreamError, match="未配置本机管理员"):
        await MovieClawClient("https://movieclaw.test").history(7)

    def respond(request):
        assert request.method == "GET"
        return ok({"entries": [record()]})

    client = MovieClawClient("https://movieclaw.test", "admin", httpx.MockTransport(respond))
    history = await client.history(7)
    assert history[0]["media"]["title"] == "测试影片"
    assert "device_name" not in history[0]
    assert "stream_url" not in history[0]["media"]


@pytest.mark.asyncio
async def test_collection_uses_owner_token_paginates_and_sanitizes():
    offsets = []

    def respond(request):
        assert request.method == "GET"
        assert request.headers["Authorization"] == "Bearer owner-token"
        if request.url.path == "/api/v1/collections":
            return ok([own_collection(item_count=201, rules=["私有规则"])])
        if request.url.path == "/api/v1/collections/3":
            return ok(own_collection(member_id=7))
        assert request.url.path == "/api/v1/collections/3/items"
        offset = int(request.url.params["offset"])
        offsets.append(offset)
        media = record()["media"]
        return ok([media] * (200 if offset == 0 else 1))

    client = MovieClawClient("https://movieclaw.test", "admin-token", httpx.MockTransport(respond))
    assert await client.collections("owner-token") == [
        {"id": 3, "name": "我的片单", "item_count": 201}
    ]
    result = await client.collection("owner-token", 3)
    assert offsets == [0, 200]
    assert len(result["items"]) == 201
    assert set(result) == {"id", "name", "items"}
    assert "private-token" not in json.dumps(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("item_count", [500, 501])
async def test_collection_limit_rejects_oversized_pages_without_truncating(item_count):
    offsets = []

    def respond(request):
        if request.url.path == "/api/v1/collections/3":
            # 没有可靠计数时仍必须根据分页累计拒绝过大的片单。
            return ok(own_collection(name="大合集"))
        offset = int(request.url.params["offset"])
        offsets.append(offset)
        return ok([record()["media"]] * min(200, item_count - offset))

    client = MovieClawClient("https://movieclaw.test", transport=httpx.MockTransport(respond))
    if item_count > 500:
        with pytest.raises(UpstreamError, match="超过 500 部") as exc:
            await client.collection("owner", 3)
        assert exc.value.status_code == 400
    else:
        assert len((await client.collection("owner", 3))["items"]) == 500
    assert offsets == [0, 200, 400]


@pytest.mark.asyncio
async def test_collection_known_oversized_count_does_not_fetch_pages():
    def respond(request):
        assert request.url.path == "/api/v1/collections/3"
        return ok(own_collection(name="大合集", item_count=501))

    client = MovieClawClient("https://movieclaw.test", transport=httpx.MockTransport(respond))
    with pytest.raises(UpstreamError, match="超过 500 部") as exc:
        await client.collection("owner", 3)
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_collection_list_only_exposes_confirmed_manageable_user_collections():
    def respond(request):
        assert request.url.path == "/api/v1/collections"
        return ok(
            [
                own_collection(),
                own_collection(id=4, manageable=False),
                own_collection(id=5, editable=False),
                own_collection(id=6, builtin="favorites", kind="builtin"),
                {"id": 7, "name": "旧版本未知归属片单", "item_count": 1},
            ]
        )

    client = MovieClawClient("https://movieclaw.test", transport=httpx.MockTransport(respond))
    assert await client.collections("owner") == [{"id": 3, "name": "我的片单", "item_count": 0}]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        {"manageable": False},
        {"editable": False},
        {"builtin": "favorites"},
        {"kind": "series"},
        {"manageable": 1},
    ],
)
async def test_direct_collection_share_cannot_bypass_ownership_or_builtin_restrictions(change):
    def respond(request):
        # 拒绝归属后不得继续读取片单内容。
        assert request.url.path == "/api/v1/collections/3"
        return ok(own_collection(**change))

    client = MovieClawClient("https://movieclaw.test", transport=httpx.MockTransport(respond))
    with pytest.raises(UpstreamError, match="片单归属") as exc:
        await client.collection("owner", 3)
    assert exc.value.status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["manageable", "editable", "builtin", "kind"])
async def test_missing_ownership_fields_hide_list_entry_and_reject_direct_share(missing):
    metadata = own_collection()
    del metadata[missing]

    def respond(request):
        if request.url.path == "/api/v1/collections":
            return ok([metadata])
        assert request.url.path == "/api/v1/collections/3"
        return ok(metadata)

    client = MovieClawClient("https://movieclaw.test", transport=httpx.MockTransport(respond))
    assert await client.collections("owner") == []
    with pytest.raises(UpstreamError, match="升级宿主版本") as exc:
        await client.collection("owner", 3)
    assert exc.value.status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "poster",
    [
        "/api/v1/libraries/1/artwork/poster",
        "/private/movie/poster.jpg",
        "https://image.test/p.jpg?token=private-token",
        "https://username:private-token@image.test/p.jpg",
        "https://server.test/playback/stream",
    ],
)
async def test_private_or_credentialed_posters_are_not_shared(poster):
    def respond(request):
        row = record()
        row["media"]["poster_url"] = poster
        return ok({"entries": [row]})

    client = MovieClawClient("https://movieclaw.test", "admin", httpx.MockTransport(respond))
    media = (await client.history(7))[0]["media"]
    assert media["poster_path"] is None and "poster_url" not in media


@pytest.mark.parametrize("path", ["/abc-DEF_123.jpg", "/a.png", "/p.webp", "/poster.jpeg"])
def test_media_accepts_only_tmdb_single_file_relative_poster_paths(path):
    assert safe_poster_path(path) == path
    assert sanitize_media({"title": "测试", "poster_path": path})["poster_path"] == path


@pytest.mark.parametrize(
    "path",
    [
        "https://evil.test/p.jpg",
        "//evil.test/p.jpg",
        "/images/assets/p.jpg",
        "/private/p.jpg",
        "C:\\films\\poster.jpg",
        "/../p.jpg",
        "/%2e%2e/p.jpg",
        "/p.jpg?token=secret",
        "/p.jpg#secret",
        "/p.svg",
        "/p.jpg/stream",
        "/p.jpg\n",
        "/" + "a" * 201 + ".jpg",
    ],
)
def test_media_rejects_hosts_credentials_local_paths_and_nonimage_poster_values(path):
    assert safe_poster_path(path) is None
    result = sanitize_media({"title": "测试", "poster_path": path})
    assert result["poster_path"] is None and path not in result.values()


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://image.tmdb.org/t/p/w500/hash123.jpg", "/hash123.jpg"),
        ("https://image.tmdb.org/t/p/original/hash123.png", "/hash123.png"),
        ("https://image.tmdb.org/t/p/w500/p.jpg?api_key=secret", None),
        ("https://image.tmdb.org/t/p/w500/p.jpg#secret", None),
        ("https://image.tmdb.org.evil.test/t/p/w500/p.jpg", None),
        ("https://secret@image.tmdb.org/t/p/w500/p.jpg", None),
        ("https://image.tmdb.org:444/t/p/w500/p.jpg", None),
        ("https://mirror.test/t/p/w500/p.jpg", None),
        ("https://assets.fanart.tv/p.jpg", None),
        ("/images/assets/local.jpg?v=1", None),
        ("https://image.tmdb.org/t/p/w500/private/p.jpg", None),
        ("https://image.tmdb.org/t/p/w500/%2e%2e.jpg", None),
    ],
)
def test_host_poster_url_is_reduced_to_known_tmdb_relative_path_or_removed(url, expected):
    result = sanitize_media({"media_item_id": 23, "title": "测试", "poster_url": url})
    assert result["poster_path"] == expected
    assert "poster_url" not in result
    assert "tmdb_id" not in result


def test_media_preserves_true_tmdb_anchor_without_expanding_detail_payload():
    result = sanitize_media(
        {
            "media_item_id": 23,
            "tmdb_id": 456,
            "kind": "movie",
            "title": "真实编号",
            "poster_path": "/public-poster.jpg",
            "overview": "不在共享快照中保存详情",
            "genres": [{"name": "动画"}],
            "url": "https://foreign.test",
            "token": "secret",
        }
    )
    assert result == {
        "media_item_id": 23,
        "tmdb_id": 456,
        "kind": "movie",
        "title": "真实编号",
        "poster_path": "/public-poster.jpg",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [301, 401, 403, 404, 500])
async def test_errors_are_chinese_do_not_follow_redirects_or_echo_tokens(status):
    calls = []

    def respond(request):
        calls.append(request.url)
        return httpx.Response(
            status, headers={"Location": "https://evil.test"}, text="private-token"
        )

    client = MovieClawClient("https://movieclaw.test", transport=httpx.MockTransport(respond))
    with pytest.raises(UpstreamError) as exc:
        await client.identity("private-token")
    assert len(calls) == 1
    assert "private-token" not in exc.value.message
    assert exc.value.status_code == (status if status in (401, 403, 404) else 502)


@pytest.mark.asyncio
async def test_network_and_malformed_responses_use_safe_errors():
    def disconnected(request):
        raise httpx.ConnectError("private-token", request=request)

    client = MovieClawClient("https://movieclaw.test", transport=httpx.MockTransport(disconnected))
    with pytest.raises(UpstreamError, match="无法连接 MovieClaw") as exc:
        await client.identity("private-token")
    assert "private-token" not in str(exc.value)
    for data in ([1, 2], {"success": True}, {"success": False, "data": {}}):
        client = MovieClawClient(
            "https://movieclaw.test",
            transport=httpx.MockTransport(
                lambda request, data=data: httpx.Response(200, json=data)
            ),
        )
        with pytest.raises(UpstreamError, match="格式不受支持"):
            await client.identity("private-token")
