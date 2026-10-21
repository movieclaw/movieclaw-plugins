"""普通未观看作品可按需核对并推荐，排除库、跨来源副本及不完整搜索仍受保护。"""

from copy import deepcopy

import pytest
from movieclaw_friends.native_source import NativeSource
from movieclaw_friends.upstream import UpstreamError
from test_app import harness as _harness
from test_display_policy import exclude, index
from test_native_source import ACCOUNT, FakeOps

harness = _harness
MEDIA = {"kind": "tv", "tmdb_id": 95717, "title": "想见你", "year": 2019}


def hit(media=MEDIA, *, number=7257, libraries=None):
    return {
        "item": {**media, "media_item_id": number, "file_path": "/private", "token": "secret"},
        "library_ids": libraries if libraries is not None else [1],
    }


class Ops(FakeOps):
    """全部只读搜索在测试进程内响应；支持分页与成员生命周期复核。"""

    def __init__(self, pages=None):
        super().__init__()
        self.pages = pages or {None: {"items": [hit()]}}

    async def call(self, operation, arguments):
        if operation != "search.library":
            return await super().call(operation, arguments)
        self.calls.append((operation, deepcopy(arguments)))
        assert arguments["q"] == "想见你" and arguments["limit"] == 100
        return deepcopy(self.pages[arguments.get("cursor")])


async def test_native_exact_identity_complete_pages_and_no_watched_data_reads():
    ops = Ops(
        {
            None: {"items": [hit({**MEDIA, "tmdb_id": 1})], "next_cursor": "next"},
            "next": {"items": [hit()]},
        }
    )
    records = await NativeSource(ops).recommendation_provenance(ACCOUNT, MEDIA)
    assert len(records) == 1 and records[0]["external_id"] == "item:7257"
    assert records[0]["library_ids"] == [1]
    assert "private" not in str(records) and "secret" not in str(records)
    assert [op for op, _ in ops.calls] == [
        "members.list",
        "search.library",
        "search.library",
        "members.list",
    ]
    assert ops.calls[2][1]["cursor"] == "next"


@pytest.mark.parametrize("problem", ["repeat", "missing-library", "wrong-kind", "bad-id"])
async def test_incomplete_or_wrong_identity_is_never_accepted(problem):
    row = hit()
    if problem == "repeat":
        ops = Ops(
            {
                None: {"items": [row], "next_cursor": "same"},
                "same": {"items": [], "next_cursor": "same"},
            }
        )
    else:
        if problem == "missing-library":
            row["library_ids"] = []
        elif problem == "wrong-kind":
            row["item"]["kind"] = "movie"
        else:
            row["item"]["tmdb_id"] = "95717"
        ops = Ops({None: {"items": [row]}})
    if problem in {"repeat", "missing-library"}:
        with pytest.raises(UpstreamError):
            await NativeSource(ops).recommendation_provenance("admin:0", MEDIA)
    else:
        assert await NativeSource(ops).recommendation_provenance("admin:0", MEDIA) == []


async def test_unwatched_publication_resolves_without_import_and_excluded_cannot_bypass(harness):
    h = harness
    policy = h.app.state.store.display_policy
    owner = h.users["alice"]["id"]
    index(h)
    await exclude(h)
    native, calls = NativeSource(Ops()), []

    async def provenance(account, media):
        assert account == "token-alice"
        calls.append(media)
        return await native.recommendation_provenance("admin:0", media)

    h.upstream.recommendation_provenance = provenance
    alice = h.clients["alice"]
    facts = h.app.state.store.all("SELECT * FROM watch_facts")
    assert not policy.guard(owner)(MEDIA)
    result = await alice.post("/api/feed/eligibility", json={"media": MEDIA})
    assert result.json() == {"allowed": True, "status": "allowed", "message": "可公开推荐"}
    assert len(calls) == 1 and (await alice.get("/api/feed")).json()["posts"] == []
    assert h.app.state.store.all("SELECT * FROM watch_facts") == facts
    assert policy.guard(owner)(MEDIA)
    assert (
        h.app.state.store.one(
            "SELECT 1 FROM display_memberships WHERE user_id=?", (h.users["bob"]["id"],)
        )
        is None
    )
    post = await alice.post(
        "/api/feed", json={"media": MEDIA, "rating": 5, "publish_consent": True}
    )
    assert post.status_code == 201 and post.json()["rating"] == 5
    assert len(calls) == 1
    # 同一 TMDB 作品在任一排除库出现，已核实的普通库副本也不能绕过。
    policy.remember(
        owner, "movieclaw", [{"external_id": "adult-copy", "media": MEDIA, "library_ids": ["2"]}]
    )
    blocked = await alice.post("/api/feed/eligibility", json={"media": MEDIA})
    assert blocked.json()["status"] == "excluded" and len(calls) == 1
    assert (
        await alice.post("/api/feed", json={"media": MEDIA, "rating": 5, "publish_consent": True})
    ).status_code == 403


async def test_failed_lookup_can_retry_and_publish_also_checks_without_preflight(harness):
    h = harness
    index(h)
    await exclude(h)
    attempts = []

    async def provenance(account, media):
        attempts.append(media)
        if len(attempts) == 1:
            raise UpstreamError("secret upstream address")
        return [{"external_id": "item:7257", "media": MEDIA, "library_ids": ["1"]}]

    h.upstream.recommendation_provenance = provenance
    client = h.clients["alice"]
    first = await client.post("/api/feed/eligibility", json={"media": MEDIA})
    assert first.json()["status"] == "unverified" and "secret" not in first.text
    response = await client.post(
        "/api/feed", json={"media": MEDIA, "rating": 5, "publish_consent": True}
    )
    assert response.status_code == 201 and len(attempts) == 2
