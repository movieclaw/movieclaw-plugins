"""原生观影源的正式操作契约、成员生命周期和逐人隔离回归。"""

import asyncio
import json
from copy import deepcopy
from datetime import datetime
from types import SimpleNamespace

import pytest
from movieclaw_friends.native_source import HOST_PERMISSIONS, NativeSource, account_key
from movieclaw_friends.upstream import UpstreamError

CREATED = datetime(2026, 1, 2, 3, 4, 5)
ACCOUNT = f"member:7:{CREATED.isoformat()}"


class FakeOps:
    """只接受真实存在的两个 GET 操作，错误的参数范围会直接使测试失败。"""

    def __init__(self):
        self.calls = []
        self.members = [
            {
                "id": 7,
                "username": "alice",
                "nickname": "可重名的昵称",
                "status": "active",
                "created_at": "2026-01-02T03:04:05Z",
                "library_ids": [99],
            }
        ]
        self.entries = [self.watch(1, ended=None), self.watch(2, ended="2026-10-01T00:00:00Z")]
        self.fail = None

    @staticmethod
    def watch(id, ended):
        return {
            "id": id,
            "member_name": "可重名的昵称",
            "media": {
                "media_item_id": 10,
                "title": "测试影片",
                "kind": "movie",
                "file_path": "/private/media/film.mkv",
                "stream_url": "stream?token=secret",
                "poster_url": "https://image.tmdb.org/t/p/w500/poster.jpg",
            },
            "started_at": "2026-01-01T00:00:00Z",
            "ended_at": ended,
            "device_name": "私有卧室设备",
            "token": "secret",
        }

    async def call(self, operation, arguments):
        self.calls.append((operation, deepcopy(arguments)))
        if self.fail:
            raise self.fail
        if operation == "members.list":
            assert arguments == {}
            return deepcopy(self.members)
        assert operation == "playback.history"
        assert arguments == {"member_id": 7, "scope": "all", "limit": 50}
        return {"entries": deepcopy(self.entries)}


def principal(kind="member", *, plugin=None, member_id=7, created=CREATED):
    return SimpleNamespace(
        kind=kind,
        plugin=plugin,
        member_id=member_id,
        member=SimpleNamespace(created_at=created),
        name="alice",
    )


def test_principal_keys_include_lifecycle_and_ignore_names():
    assert account_key(principal()) == ACCOUNT
    other = principal()
    other.name = "另一个可重名的昵称"
    assert account_key(other) == ACCOUNT
    assert account_key(principal("admin")) == "admin:0"
    assert account_key(principal(created=datetime(2026, 1, 3))) != ACCOUNT
    assert HOST_PERMISSIONS == ("members.list", "playback.history", "search.library")


@pytest.mark.parametrize(
    "untrusted",
    [
        principal("agent"),
        principal("mcp"),
        principal("share"),
        principal("admin", plugin=object()),
        principal(member_id=None),
        principal(created=None),
    ],
)
def test_programmatic_or_incomplete_principals_cannot_become_accounts(untrusted):
    with pytest.raises(UpstreamError) as exc:
        account_key(untrusted)
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_member_identity_rechecks_lifecycle_without_user_credentials():
    ops = FakeOps()
    source = NativeSource(ops)
    for _ in range(2):
        assert await source.identity(ACCOUNT) == {
            "username": ACCOUNT,
            "nickname": "可重名的昵称",
            "role": "member",
            "member_id": 7,
        }
    assert ops.calls == [("members.list", {}), ("members.list", {})]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["deleted", "disabled", "reused_id", "missing_created"])
async def test_deleted_disabled_or_reused_member_stops_data_before_history_read(change):
    ops = FakeOps()
    if change == "deleted":
        ops.members = []
    elif change == "disabled":
        ops.members[0]["status"] = "disabled"
    elif change == "reused_id":
        ops.members[0]["created_at"] = "2026-02-01T00:00:00Z"
    else:
        del ops.members[0]["created_at"]
    with pytest.raises(UpstreamError) as exc:
        await NativeSource(ops).snapshot(ACCOUNT, 7, True, True)
    assert exc.value.status_code in (401, 403)
    assert ops.calls == [("members.list", {})]


@pytest.mark.asyncio
async def test_different_bound_member_cannot_read_any_history():
    ops = FakeOps()
    with pytest.raises(UpstreamError, match="归属不一致") as exc:
        await NativeSource(ops).snapshot(ACCOUNT, 8, True, True)
    assert exc.value.status_code == 403
    assert ops.calls == [("members.list", {})]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "current,history", [(True, False), (False, True), (True, True), (False, False)]
)
async def test_snapshot_only_reads_fixed_member_and_respects_independent_consent(current, history):
    ops = FakeOps()
    result = await NativeSource(ops).snapshot(ACCOUNT, 7, current, history)
    assert (result["current"] is not None) is current
    assert [row["id"] for row in result["history"]] == ([2] if history else [])
    assert result["sharing"] == {"current": current, "history": history}
    assert ops.calls[0] == ("members.list", {})
    if current or history:
        assert ops.calls[1:] == [
            ("playback.history", {"member_id": 7, "scope": "all", "limit": 50}),
            ("members.list", {}),
        ]
    else:
        assert len(ops.calls) == 1
    serialized = json.dumps(result, ensure_ascii=False)
    for secret in ("/private/", "secret", "卧室设备", "stream_url", "device_name", "member_name"):
        assert secret not in serialized
    assert datetime.fromisoformat(result["observed_at"]).tzinfo is not None


@pytest.mark.asyncio
async def test_administrator_always_reads_only_owner_zero():
    class AdminOps:
        async def call(self, operation, arguments):
            assert operation == "playback.history"
            assert arguments == {"member_id": 0, "scope": "all", "limit": 50}
            return {"entries": []}

    source = NativeSource(AdminOps())
    assert (await source.identity("admin:0"))["member_id"] == 0
    assert (await source.snapshot("admin:0", 0, True, True))["history"] == []


@pytest.mark.asyncio
async def test_history_also_validates_account_lifecycle_and_sanitizes():
    ops = FakeOps()
    history = await NativeSource(ops).history(ACCOUNT)
    assert len(history) == 2 and "token" not in history[0]
    assert ops.calls[0] == ("members.list", {})
    assert ops.calls[-1] == ("members.list", {})


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["history", "snapshot"])
@pytest.mark.parametrize("change", ["deleted", "disabled", "reused_id"])
async def test_history_read_rechecks_lifecycle_before_returning_any_data(method, change):
    class ChangedDuringRead(FakeOps):
        async def call(self, operation, arguments):
            result = await super().call(operation, arguments)
            if operation == "playback.history":
                if change == "deleted":
                    self.members = []
                elif change == "disabled":
                    self.members[0]["status"] = "disabled"
                else:
                    self.members[0]["created_at"] = "2026-10-20T00:00:00Z"
            return result

    ops = ChangedDuringRead()
    source = NativeSource(ops)
    with pytest.raises(UpstreamError) as exc:
        if method == "history":
            await source.history(ACCOUNT)
        else:
            await source.snapshot(ACCOUNT, 7, True, True)
    assert exc.value.status_code == 401
    assert [op for op, _ in ops.calls] == ["members.list", "playback.history", "members.list"]


@pytest.mark.asyncio
async def test_native_collections_require_browser_and_never_use_admin_visibility():
    ops = FakeOps()
    source = NativeSource(ops)
    assert source.browser_required is True
    for awaitable in (source.collections(ACCOUNT), source.collection(ACCOUNT, 4)):
        with pytest.raises(UpstreamError, match="当前登录") as exc:
            await awaitable
        assert exc.value.status_code == 409
    assert ops.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "account", ["alice", "member:07:2026-01-02", "member:-1:2026-01-02", "admin:7"]
)
async def test_invalid_account_key_does_not_issue_host_operation(account):
    ops = FakeOps()
    with pytest.raises(UpstreamError, match="账号标识无效"):
        await NativeSource(ops).identity(account)
    assert ops.calls == []


@pytest.mark.asyncio
async def test_missing_host_grants_have_safe_error_without_host_response_details():
    class Denied(Exception):
        status = 403

    ops = FakeOps()
    ops.fail = Denied("sensitive-host-response")
    with pytest.raises(UpstreamError, match="插件授权") as exc:
        await NativeSource(ops).snapshot(ACCOUNT, 7, True, True)
    assert exc.value.status_code == 403 and "sensitive-host-response" not in str(exc.value)


@pytest.mark.asyncio
async def test_native_history_exports_safe_poster_path_without_extra_metadata_operations():
    ops = FakeOps()
    result = await NativeSource(ops).snapshot(ACCOUNT, 7, True, True)
    current, past = result["current"]["media"], result["history"][0]["media"]
    assert current["poster_path"] == past["poster_path"] == "/poster.jpg"
    assert "poster_url" not in current and "tmdb_id" not in current
    assert ops.calls == [
        ("members.list", {}),
        ("playback.history", {"member_id": 7, "scope": "all", "limit": 50}),
        ("members.list", {}),
    ]


@pytest.mark.asyncio
async def test_native_history_never_treats_host_asset_or_local_item_id_as_portable_metadata():
    ops = FakeOps()
    for row in ops.entries:
        row["media"]["poster_url"] = "/images/assets/private-file.jpg?v=123"
    result = await NativeSource(ops).snapshot(ACCOUNT, 7, True, True)
    for media in (result["current"]["media"], result["history"][0]["media"]):
        assert media["media_item_id"] == 10
        assert "tmdb_id" not in media and media["poster_path"] is None
        assert "poster_url" not in media
    assert set(op for op, _ in ops.calls) <= set(HOST_PERMISSIONS)


class MetadataOps(FakeOps):
    def __init__(self):
        super().__init__()
        for row in self.entries:
            row["media"].update(library_id=99, browsable=True)
        self.results = {"测试影片": {"items": [self.hit()]}}
        self.search_failure = None
        self.active = 0
        self.peak = 0

    @staticmethod
    def hit(*, item_id=10, library_ids=None):
        return {
            "item": {
                "media_item_id": item_id,
                "tmdb_id": 550,
                "kind": "movie",
                "poster_url": "https://image.tmdb.org/t/p/w500/metadataPoster.jpg",
                "entry_dirs": ["/private/film"],
                "files": [{"token": "secret"}],
                "library_status": "不能分享的本地库存",
                "links": ["https://foreign.test"],
            },
            "library_ids": [99] if library_ids is None else library_ids,
        }

    async def call(self, operation, arguments):
        if operation != "search.library":
            return await super().call(operation, arguments)
        self.calls.append((operation, deepcopy(arguments)))
        assert set(arguments) == {"q", "limit"}
        assert arguments["q"].strip() and len(arguments["q"]) <= 100
        assert arguments["limit"] == 100
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(0)
            if self.search_failure:
                raise self.search_failure
            return deepcopy(self.results.get(arguments["q"], {"items": []}))
        finally:
            self.active -= 1


@pytest.mark.asyncio
async def test_metadata_search_is_deduplicated_and_only_public_identity_leaves_source():
    ops = MetadataOps()
    source = NativeSource(ops)
    result = await source.snapshot(ACCOUNT, 7, True, True)
    for media in (result["current"]["media"], result["history"][0]["media"]):
        assert media["tmdb_id"] == 550 and media["poster_path"] == "/metadataPoster.jpg"
        assert media["title"] == "测试影片"
        assert "library_id" not in media and "browsable" not in media
    assert [op for op, _ in ops.calls] == [
        "members.list",
        "playback.history",
        "members.list",
        "search.library",
        "members.list",
    ]
    for private in (
        "/private/",
        "secret",
        "library_status",
        "links",
        "entry_dirs",
        "files",
        "foreign.test",
    ):
        assert private not in json.dumps(result, ensure_ascii=False)
    # 去重只在一次读取内有效，不把其他成员的查询结果长期缓存。
    await source.snapshot(ACCOUNT, 7, True, True)
    assert len([op for op, _ in ops.calls if op == "search.library"]) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "current,history,queries",
    [
        (True, False, ["当前影片"]),
        (False, True, ["历史影片"]),
        (False, False, []),
    ],
)
async def test_metadata_is_only_requested_for_records_actually_shared(current, history, queries):
    ops = MetadataOps()
    ops.entries[0]["media"]["title"] = "当前影片"
    ops.entries[1]["media"]["title"] = "历史影片"
    await NativeSource(ops).snapshot(ACCOUNT, 7, current, history)
    assert [args["q"] for op, args in ops.calls if op == "search.library"] == queries


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", ["no_library", "no_item", "not_browsable", "empty_title", "known_tmdb"]
)
async def test_missing_scope_or_existing_public_anchor_never_causes_broad_metadata_search(change):
    ops = MetadataOps()
    for row in ops.entries:
        if change == "no_library":
            del row["media"]["library_id"]
        elif change == "no_item":
            row["media"]["media_item_id"] = None
        elif change == "not_browsable":
            row["media"]["browsable"] = False
        elif change == "empty_title":
            row["media"]["title"] = "  "
        else:
            row["media"].update(tmdb_id=1234, kind="movie")
    result = await NativeSource(ops).snapshot(ACCOUNT, 7, True, True)
    assert not any(op == "search.library" for op, _ in ops.calls)
    if change == "known_tmdb":
        assert result["current"]["media"]["tmdb_id"] == 1234


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", ["wrong_item", "wrong_library", "duplicate", "denied", "not_found"]
)
async def test_metadata_failure_or_nonmatching_identity_keeps_basic_record_without_guessing(
    failure,
):
    ops = MetadataOps()
    if failure == "wrong_item":
        ops.results["测试影片"] = {"items": [ops.hit(item_id=11)]}
    elif failure == "wrong_library":
        ops.results["测试影片"] = {"items": [ops.hit(library_ids=[98])]}
    elif failure == "duplicate":
        ops.results["测试影片"] = {"items": [ops.hit(), ops.hit()]}
    elif failure == "denied":

        class Denied(Exception):
            status = 403

        ops.search_failure = Denied("secret-host-error")
    else:
        ops.results["测试影片"] = {"items": [], "next_cursor": "never-follow-this"}
    result = await NativeSource(ops).snapshot(ACCOUNT, 7, True, True)
    assert result["current"]["media"]["title"] == "测试影片"
    assert "tmdb_id" not in result["current"]["media"]
    assert len(result["history"]) == 1
    assert [op for op, _ in ops.calls].count("search.library") == 1


@pytest.mark.asyncio
async def test_metadata_checks_only_first_hundred_candidates_and_never_follows_cursor():
    ops = MetadataOps()
    ops.results["测试影片"] = {
        "items": [ops.hit(item_id=11)] * 100 + [ops.hit()],
        "next_cursor": "ignored",
    }
    result = await NativeSource(ops).snapshot(ACCOUNT, 7, True, False)
    assert "tmdb_id" not in result["current"]["media"]
    assert [args for op, args in ops.calls if op == "search.library"] == [
        {"q": "测试影片", "limit": 100}
    ]


@pytest.mark.asyncio
async def test_metadata_fanout_is_at_most_four_and_unique_queries_at_most_fifty():
    ops = MetadataOps()
    ops.entries = []
    for id in range(60):
        row = ops.watch(id, ended="2026-10-01T00:00:00Z")
        row["media"].update(media_item_id=id + 1, library_id=99, title=f"影片 {id}")
        ops.entries.append(row)
    result = await NativeSource(ops).snapshot(ACCOUNT, 7, False, True)
    assert len(result["history"]) == 50
    assert len([op for op, _ in ops.calls if op == "search.library"]) == 50
    assert ops.peak == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["deleted", "disabled", "reused_id"])
async def test_member_lifecycle_is_rechecked_after_metadata_requests(change):
    class ChangedDuringMetadata(MetadataOps):
        async def call(self, operation, arguments):
            result = await super().call(operation, arguments)
            if operation == "search.library":
                if change == "deleted":
                    self.members = []
                elif change == "disabled":
                    self.members[0]["status"] = "disabled"
                else:
                    self.members[0]["created_at"] = "2026-10-20T00:00:00Z"
            return result

    ops = ChangedDuringMetadata()
    with pytest.raises(UpstreamError) as exc:
        await NativeSource(ops).snapshot(ACCOUNT, 7, True, True)
    assert exc.value.status_code == 401
    assert ops.calls[-1] == ("members.list", {})
