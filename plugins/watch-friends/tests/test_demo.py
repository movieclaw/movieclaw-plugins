"""虚拟好友的产品行为回归：明确标记、主动添加、关闭与重启均不触及宿主数据。"""

import base64
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import httpx
import pytest
from movieclaw_friends.app import COOKIE, create_app
from movieclaw_friends.config import Settings
from movieclaw_friends.demo import DEMO_ID
from movieclaw_friends.upstream import UpstreamError

HEADERS = {"x-friends-request": "1", "origin": "http://a.test"}
DEMO_USERNAME = "__movieclaw_friends_demo__"
DEMO_NICKNAME = "小映"


class FakeUpstream:
    """允许真实用户鉴权；任何读取宿主观影数据的调用都会使演示测试失败。"""

    def __init__(self):
        self.identity_tokens = []
        self.data_calls = []

    async def identity(self, token):
        self.identity_tokens.append(token)
        if token == "token-alice":
            return {
                "username": "alice",
                "nickname": "真实用户 Alice",
                "member_id": 1,
                "role": "member",
            }
        if token == "token-bob":
            return {"username": "bob", "nickname": "真实用户 Bob", "member_id": 2, "role": "member"}
        if token == "spoof-demo-token":
            return {
                "username": DEMO_USERNAME,
                "nickname": "冒充者",
                "member_id": 99,
                "role": "member",
            }
        raise UpstreamError("令牌无效", 401)

    async def snapshot(self, *args):
        self.data_calls.append(("snapshot", args))
        raise AssertionError("演示用户不能读取 MovieClaw 观影数据")

    async def collections(self, *args):
        self.data_calls.append(("collections", args))
        raise AssertionError("演示用户不能读取 MovieClaw 片单")

    async def collection(self, *args):
        self.data_calls.append(("collection", args))
        raise AssertionError("演示用户不能读取 MovieClaw 片单内容")


@asynccontextmanager
async def instance(data_dir, *, enabled=None, peer_transport=None):
    options = {} if enabled is None else {"demo_enabled": enabled}
    settings = Settings(
        "http://movieclaw",
        public_url="http://a.test",
        data_dir=data_dir,
        allow_private_peers=True,
        **options,
    )
    upstream = FakeUpstream()
    app = create_app(settings, upstream=upstream, peer_transport=peer_transport)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://a.test", headers=HEADERS
        ) as client,
    ):
        yield app, client, upstream


async def login(client, token="token-alice"):
    response = await client.post("/api/login", json={"token": token})
    assert response.status_code == 200
    return response.json()["user"]


async def add_demo(client):
    response = await client.post(
        "/api/requests", json={"user_id": DEMO_ID, "address_consent": True}
    )
    assert response.status_code == 200
    assert response.json()["status"] == "accepted"
    assert response.json()["is_demo"] is True
    return response.json()["id"]


def assert_default_private(user):
    assert user["discoverable"] is False
    assert user["share_current"] is False
    assert user["share_history"] is False


@pytest.mark.asyncio
async def test_default_demo_is_marked_in_directory_without_auto_adding_real_users(tmp_path):
    async with instance(tmp_path) as (app, client, upstream):
        assert upstream.identity_tokens == [] and upstream.data_calls == []
        health = (await client.get("/health")).json()
        assert health["version"] == "0.1.1" and health["demo_enabled"] is True
        assert (await client.get("/api/info")).json()["demo_enabled"] is True
        own = await login(client)
        assert_default_private(own)
        assert own["is_demo"] is False
        directory = (await client.get("/api/directory")).json()["users"]
        assert len(directory) == 4
        virtual = next(user for user in directory if user["id"] == DEMO_ID)
        assert virtual["id"] == DEMO_ID and virtual["nickname"] == DEMO_NICKNAME
        assert virtual["is_demo"] is True and len(virtual["history"]) == 5
        assert all(row["media"]["demo_id"] for row in virtual["history"])
        assert (await client.get("/api/friends")).json()["friends"] == []
        assert app.state.store.all("SELECT * FROM relationships") == []
        stored = app.state.store.one("SELECT * FROM users WHERE id=?", (DEMO_ID,))
        assert stored["role"] == "demo" and stored["member_id"] is None
        assert app.state.store.decrypt(stored["token"]) == ""
        assert upstream.data_calls == []


@pytest.mark.asyncio
async def test_demo_request_requires_consent_then_shares_virtual_activity_for_real_films(tmp_path):
    async with instance(tmp_path) as (app, client, upstream):
        own = await login(client)
        denied = await client.post("/api/requests", json={"user_id": DEMO_ID})
        assert denied.status_code == 400
        assert app.state.store.all("SELECT * FROM relationships") == []
        relation_id = await add_demo(client)
        friends = (await client.get("/api/friends")).json()["friends"]
        assert len(friends) == 1 and friends[0]["is_demo"] is True
        assert friends[0]["user_id"] == DEMO_ID
        response = await client.get(f"/api/friends/{relation_id}")
        assert response.status_code == 200
        shared = response.json()
        assert shared["friend"]["is_demo"] is True
        data = shared["data"]
        assert data["is_demo"] is True
        assert data["sharing"] == {"current": True, "history": True}
        assert data["current"]["ended_at"] is None
        expiry = datetime.fromisoformat(data["current"]["expires_at"])
        assert 0 < (expiry - datetime.now(UTC)).total_seconds() <= 95
        assert len(data["history"]) == 5 and all(row["ended_at"] for row in data["history"])
        assert data["presence"]["online"] is True
        assert data["avatar_id"] == "xiaoying"
        assert len(data["favorites"]) == 4
        assert len(data["collections"]) == 2
        assert all(len(collection["items"]) == 5 for collection in data["collections"])
        assert len({collection["name"] for collection in data["collections"]}) == 2
        media = [data["current"]["media"]] + [row["media"] for row in data["history"]]
        media += data["favorites"]
        media += [item for collection in data["collections"] for item in collection["items"]]
        assert len(media) == len({item["tmdb_id"] for item in media}) == 20
        assert all(item["demo_id"] and item["tmdb_id"] > 0 for item in media)
        assert all(item["poster_path"] and item["media_item_id"] is None for item in media)
        assert data["collections"][0]["name"] == "向时间与星空出发"
        assert "token-alice" not in json.dumps(data) and "http://movieclaw" not in json.dumps(data)
        assert_default_private((await client.get("/api/me")).json()["user"])
        assert upstream.data_calls == []
        assert set(upstream.identity_tokens) == {"token-alice"}
        # 演示好友也只属于主动申请的真实用户，其他用户不能复用关系编号。
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://a.test", headers=HEADERS
        ) as other:
            await login(other, "token-bob")
            assert (await other.get("/api/friends")).json()["friends"] == []
            assert (await other.get(f"/api/friends/{relation_id}")).status_code == 404
        assert (await client.delete(f"/api/friends/{relation_id}")).status_code == 200
        assert (await client.get(f"/api/friends/{relation_id}")).status_code == 403
        assert (
            app.state.store.one("SELECT cache FROM relationships WHERE id=?", (relation_id,))[
                "cache"
            ]
            is None
        )
        assert app.state.store.one("SELECT id FROM users WHERE username='alice'")["id"] == own["id"]


@pytest.mark.asyncio
async def test_real_account_cannot_impersonate_or_update_demo_profile(tmp_path):
    async with instance(tmp_path) as (app, client, upstream):
        await login(client)
        response = await client.patch(
            "/api/profile",
            json={
                "id": DEMO_ID,
                "user_id": DEMO_ID,
                "username": DEMO_USERNAME,
                "nickname": "真实用户的新昵称",
                "discoverable": False,
                "share_current": False,
                "share_history": False,
            },
        )
        assert response.status_code == 200
        assert response.json()["user"]["username"] == "alice"
        original = app.state.store.one("SELECT * FROM users WHERE id=?", (DEMO_ID,))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://a.test", headers=HEADERS
        ) as impostor:
            failed = await impostor.post("/api/login", json={"token": "spoof-demo-token"})
            assert failed.status_code == 409
            assert COOKIE not in impostor.cookies
            assert (
                await impostor.patch("/api/profile", json={"nickname": "篡改演示"})
            ).status_code == 401
        assert app.state.store.one("SELECT * FROM users WHERE id=?", (DEMO_ID,)) == original
        assert original["nickname"] == DEMO_NICKNAME and original["role"] == "demo"
        assert upstream.data_calls == []


@pytest.mark.asyncio
async def test_federation_cannot_add_demo_and_does_not_contact_remote(tmp_path):
    remote_calls = []

    def remote(request):
        remote_calls.append(request.url)
        raise AssertionError("虚拟演示用户不能触发联邦网络请求")

    async with instance(tmp_path, peer_transport=httpx.MockTransport(remote)) as (
        app,
        client,
        upstream,
    ):
        invite = app.state.store.one("SELECT invite_token FROM users WHERE id=?", (DEMO_ID,))
        response = await client.post(
            "/federation/requests",
            json={
                "invite_token": invite["invite_token"],
                "request_id": "remote-request",
                "origin": "http://remote.test",
                "user_id": "remote-user",
                "token": "p" * 40,
            },
        )
        assert response.status_code == 403
        assert remote_calls == [] and upstream.identity_tokens == [] and upstream.data_calls == []
        assert app.state.store.all("SELECT * FROM relationships") == []


@pytest.mark.asyncio
async def test_disabling_demo_hides_directory_blocks_new_requests_and_old_friend_data(tmp_path):
    async with instance(tmp_path) as (app, client, upstream):
        await login(client)
        relation_id = await add_demo(client)
        assert (await client.get(f"/api/friends/{relation_id}")).status_code == 200
        stored_demo = app.state.store.one("SELECT invite_token FROM users WHERE id=?", (DEMO_ID,))
        payload = {"origin": "http://a.test", "invite": stored_demo["invite_token"]}
        code = "mcf1." + base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
        assert upstream.data_calls == []
    # 复用同一独立数据库，确保禁用后旧关系与已有缓存也不会继续对外提供数据。
    async with instance(tmp_path, enabled=False) as (app, client, upstream):
        assert (await client.get("/health")).json()["demo_enabled"] is False
        await login(client)
        assert (await client.get("/api/directory")).json()["users"] == []
        assert (await client.get("/api/friends")).json()["friends"] == []
        assert (await client.get(f"/api/friends/{relation_id}")).status_code == 404
        for target in ({"user_id": DEMO_ID}, {"invite_code": code}):
            assert (
                await client.post("/api/requests", json=target | {"address_consent": True})
            ).status_code == 404
        assert_default_private((await client.get("/api/me")).json()["user"])
        assert upstream.data_calls == []


@pytest.mark.asyncio
async def test_restarting_seed_is_idempotent_and_preserves_real_settings_and_relationships(
    tmp_path,
):
    async with instance(tmp_path) as (app, client, upstream):
        own = await login(client)
        relation_id = await add_demo(client)
        assert (
            await client.patch(
                "/api/profile",
                json={
                    "nickname": "Alice 保存的昵称",
                    "discoverable": False,
                    "share_current": False,
                    "share_history": True,
                },
            )
        ).status_code == 200
        before = app.state.store.one("SELECT * FROM users WHERE id=?", (own["id"],))
        demo_before = app.state.store.one("SELECT * FROM users WHERE id=?", (DEMO_ID,))
        relation_before = app.state.store.one(
            "SELECT * FROM relationships WHERE id=?", (relation_id,)
        )
        assert upstream.data_calls == []
    async with instance(tmp_path) as (app, client, upstream):
        assert app.state.store.one("SELECT * FROM users WHERE id=?", (own["id"],)) == before
        assert app.state.store.one("SELECT * FROM users WHERE id=?", (DEMO_ID,)) == demo_before
        assert (
            app.state.store.one("SELECT * FROM relationships WHERE id=?", (relation_id,))
            == relation_before
        )
        assert len(app.state.store.all("SELECT * FROM users WHERE role='demo'")) == 4
        assert upstream.identity_tokens == [] and upstream.data_calls == []
        await login(client)
        assert len((await client.get("/api/friends")).json()["friends"]) == 1
        saved = (await client.get("/api/me")).json()["user"]
        assert saved["nickname"] == "Alice 保存的昵称" and saved["share_history"] is True


@pytest.mark.parametrize("value,expected", [(None, True), ("false", False), ("true", True)])
def test_demo_environment_flag_defaults_enabled_and_can_be_disabled(
    monkeypatch, tmp_path, value, expected
):
    monkeypatch.setenv("MOVIECLAW_URL", "http://movieclaw")
    monkeypatch.setenv("FRIENDS_PUBLIC_URL", "http://a.test")
    monkeypatch.setenv("FRIENDS_DATA_DIR", str(tmp_path))
    if value is None:
        monkeypatch.delenv("FRIENDS_DEMO_ENABLED", raising=False)
    else:
        monkeypatch.setenv("FRIENDS_DEMO_ENABLED", value)
    assert Settings.from_env().demo_enabled is expected


@pytest.mark.asyncio
async def test_readonly_demo_preview_has_four_people_posters_and_details_without_sharing(tmp_path):
    async with instance(tmp_path) as (app, client, upstream):
        assert (await client.get("/api/demo")).status_code == 401
        own = await login(client)
        result = (await client.get("/api/demo")).json()
        assert result["enabled"] is True and len(result["users"]) == 4
        assert result["user"]["id"] == DEMO_ID
        assert len({item["user"]["id"] for item in result["users"]}) == 4
        assert len(result["details"]) == 80
        assert len(result["data"]["collections"]) == 2
        for index, item in enumerate(result["users"]):
            assert item["user"]["is_demo"] is True and item["data"]["is_demo"] is True
            assert item["user"]["presence"]["online"] is (index < 2)
            assert item["data"]["presence"]["online"] is (index < 2)
            assert item["user"]["avatar_id"] == item["data"]["avatar_id"]
            assert datetime.fromisoformat(item["data"]["presence"]["last_seen"])
            current = item["data"]["current"]
            if index < 2:
                assert current is not None and current["playback_rate"] == 1
            else:
                assert current is None
            assert len(item["data"]["history"]) == 5
            assert len(item["data"]["collections"]) == 2
            assert len(item["data"]["favorites"]) == 4
            media = [current["media"]] if current else []
            media.extend(row["media"] for row in item["data"]["history"])
            media.extend(item["data"]["favorites"])
            media.extend(
                film for collection in item["data"]["collections"] for film in collection["items"]
            )
            for film in media:
                assert film["demo_id"] in result["details"]
                assert film["tmdb_id"] > 0 and film["media_item_id"] is None
                assert film["poster_path"].endswith(".jpg")
                detail = result["details"][film["demo_id"]]
                assert detail["title"] == film["title"] and detail["overview"]
                assert detail["genres"] and detail["runtime_minutes"] > 0
                assert detail["tmdb_id"] == film["tmdb_id"] and detail["year"] == film["year"]
                assert detail["poster_path"] == film["poster_path"]
                assert detail["source_url"].startswith("https://www.themoviedb.org/movie/")
        assert (await client.get("/api/friends")).json()["friends"] == []
        assert (await client.get("/api/lists")).json()["lists"] == []
        assert app.state.store.all("SELECT * FROM relationships") == []
        assert upstream.data_calls == []
        assert (await client.get("/api/me")).json()["user"] == own


@pytest.mark.asyncio
async def test_disabled_demo_preview_returns_no_synthetic_person_or_media(tmp_path):
    async with instance(tmp_path, enabled=False) as (_, client, upstream):
        await login(client)
        assert (await client.get("/api/demo")).json() == {
            "enabled": False,
            "available": False,
            "preference": True,
            "user": None,
            "data": None,
            "users": [],
            "details": {},
        }
        assert upstream.data_calls == []


def test_demo_progress_advances_at_one_times_speed_and_restarts_its_dedicated_movie():
    from datetime import timedelta

    from movieclaw_friends.demo import snapshot

    start = datetime(2026, 1, 1, 0, 10, tzinfo=UTC)
    before = snapshot(observed=start)
    after = snapshot(observed=start + timedelta(seconds=10))
    three_minutes = snapshot(observed=start + timedelta(seconds=180))
    remaining = (before["current"]["duration_ms"] - before["current"]["end_position_ms"]) / 1000
    next_film = snapshot(observed=start + timedelta(seconds=remaining + 1))
    assert before["current"]["media"] == after["current"]["media"]
    assert before["current"]["media"] == three_minutes["current"]["media"]
    assert after["current"]["end_position_ms"] - before["current"]["end_position_ms"] == 10_000
    assert after["current"]["progress_percent"] > before["current"]["progress_percent"]
    assert before["current"]["playback_rate"] == 1
    assert before["current"]["started_at"] == after["current"]["started_at"]
    assert next_film["current"]["media"] == before["current"]["media"]
    assert next_film["current"]["end_position_ms"] == 1000


def test_real_movie_catalog_has_verified_identity_safe_posters_and_copy_isolation():
    from movieclaw_friends.demo import details
    from movieclaw_friends.media import safe_poster_path

    catalog = details()
    assert len(catalog) == 80
    assert len({film["tmdb_id"] for film in catalog.values()}) == 80
    assert len({film["title"] for film in catalog.values()}) == 80
    assert {
        (film["tmdb_id"], film["title"], film["year"], film["runtime_minutes"])
        for film in catalog.values()
    } >= {
        (157336, "星际穿越", 2014, 169),
        (27205, "盗梦空间", 2010, 148),
        (37165, "楚门的世界", 1998, 103),
        (129, "千与千寻", 2001, 125),
        (508442, "心灵奇旅", 2020, 101),
        (354912, "寻梦环游记", 2017, 105),
        (10681, "机器人总动员", 2008, 98),
        (14160, "飞屋环游记", 2009, 96),
    }
    for key, film in catalog.items():
        assert film["demo_id"] == key and film["kind"] == "movie"
        assert safe_poster_path(film["poster_path"]) == film["poster_path"]
        assert f"/movie/{film['tmdb_id']}" in film["source_url"]
        assert 0 < len(film["overview"]) <= 100 and film["genres"]
        assert 1900 <= film["year"] <= 2026 and 0 < film["runtime_minutes"] < 400
        assert "media_item_id" not in film and "poster_url" not in film
    catalog["interstellar"]["genres"].append("错误类型")
    assert "错误类型" not in details()["interstellar"]["genres"]


def test_demo_movie_groups_and_sections_have_no_repeats_and_recommendations_use_own_favorites():
    from datetime import timedelta

    from movieclaw_friends.demo import DEMO_USERS, profile, snapshot
    from movieclaw_friends.demo_catalog import FILMS

    observed = datetime(2026, 10, 20, 12, tzinfo=UTC)
    avatars = ("xiaoying", "achuan", "xiaolu", "xingzi")
    shared_ids = set()
    collection_ids = set()
    collection_names = set()
    for index, user in enumerate(DEMO_USERS):
        data = snapshot(user["id"], observed=observed)
        expected = {film["tmdb_id"] for film in FILMS[index * 20 : (index + 1) * 20]}
        assert len(expected) == 20
        assert data["avatar_id"] == profile(user["id"])["avatar_id"] == avatars[index]
        assert len(data["history"]) == 5 and len(data["favorites"]) == 4
        assert len(data["collections"]) == 2
        media = [row["media"] for row in data["history"]] + data["favorites"]
        for collection in data["collections"]:
            assert len(collection["items"]) == 5
            media.extend(collection["items"])
            collection_ids.add(collection["id"])
            collection_names.add(collection["name"])
        if data["current"]:
            media.append(data["current"]["media"])
        ids = {film["tmdb_id"] for film in media}
        assert len(ids) == len(media) == (20 if index < 2 else 19)
        assert ids <= expected and not ids & shared_ids
        shared_ids.update(ids)
        assert len(data["recommendations"]) == 2
        favorite_ids = {film["tmdb_id"] for film in data["favorites"]}
        assert len({row["id"] for row in data["recommendations"]}) == 2
        for row in data["recommendations"]:
            assert row["media"]["tmdb_id"] in favorite_ids
            assert row["body"] and datetime.fromisoformat(row["created_at"]) < observed
        # 几天后的当前播放仍在专属槽位，不会轮换进历史、喜欢或他人的片单。
        later = snapshot(user["id"], observed=observed + timedelta(days=12))
        assert later["history"][0]["media"] == data["history"][0]["media"]
        assert later["favorites"] == data["favorites"]
        assert later["collections"] == data["collections"]
        if data["current"]:
            assert later["current"]["media"] == data["current"]["media"]
    assert len(shared_ids) == 78  # 两位离线人物的在播槽位不展示。
    assert len(collection_ids) == len(collection_names) == 8


def test_offline_demo_presence_and_history_stay_stable_when_refreshed():
    from datetime import timedelta

    from movieclaw_friends.demo import DEMO_USERS, profile, snapshot

    observed = datetime(2026, 10, 20, 12, tzinfo=UTC)
    for index, user in enumerate(DEMO_USERS):
        before = snapshot(user["id"], observed=observed)
        assert profile(user["id"], observed=observed)["presence"] == before["presence"]
        assert before["presence"]["online"] is (index < 2)
        last_seen = datetime.fromisoformat(before["presence"]["last_seen"])
        assert last_seen <= observed
        if index >= 2:
            after = snapshot(user["id"], observed=observed + timedelta(hours=1))
            assert before["current"] is None and after["current"] is None
            assert before["presence"] == after["presence"]
            assert before["history"] == after["history"]
            assert all(
                datetime.fromisoformat(row["ended_at"]) < last_seen for row in before["history"]
            )


@pytest.mark.asyncio
async def test_seed_migrates_legacy_demo_films_without_resetting_real_accounts(tmp_path):
    from movieclaw_friends.demo import seed

    async with instance(tmp_path) as (app, client, upstream):
        own = await login(client)
        relation_id = await add_demo(client)
        store = app.state.store
        legacy = json.dumps([{"media": {"demo_id": "starlight", "title": "旧虚构电影"}}])
        store.execute("UPDATE users SET public_history=? WHERE id=?", (legacy, DEMO_ID))
        # 即使真实用户主动保存过同样内容，目录升级也没有权力修改它。
        store.execute(
            "UPDATE users SET public_history=?,demo_enabled=0 WHERE id=?", (legacy, own["id"])
        )
        real_before = store.one("SELECT * FROM users WHERE id=?", (own["id"],))
        demo_before = store.one("SELECT * FROM users WHERE id=?", (DEMO_ID,))
        relation_before = store.one("SELECT * FROM relationships WHERE id=?", (relation_id,))
        seed(store)
        virtual = store.one("SELECT * FROM users WHERE id=?", (DEMO_ID,))
        history = json.loads(virtual["public_history"])
        assert len(history) == 5 and all(row["media"]["tmdb_id"] > 0 for row in history)
        assert virtual["token"] == demo_before["token"]
        assert virtual["invite_token"] == demo_before["invite_token"]
        assert store.one("SELECT * FROM users WHERE id=?", (own["id"],)) == real_before
        assert (
            store.one("SELECT * FROM relationships WHERE id=?", (relation_id,)) == relation_before
        )
        seed(store)
        assert store.one("SELECT * FROM users WHERE id=?", (DEMO_ID,)) == virtual
        assert upstream.data_calls == []


@pytest.mark.asyncio
async def test_federated_poster_url_is_discarded_and_negative_host_share_id_is_rejected(tmp_path):
    from movieclaw_friends.app import Media

    for invalid in (
        "https://private.example/secret.jpg?token=hidden",
        "//evil/x.jpg",
        "/x/../a.jpg",
    ):
        assert Media(title="安全测试", poster_path=invalid).poster_path is None
    assert (
        Media(title="公开影片", poster_path="/safe_Poster-1.jpg").poster_path
        == "/safe_Poster-1.jpg"
    )
    async with instance(tmp_path) as (app, client, _):
        await login(client)
        response = await client.post(
            "/api/collections/-1/share",
            json={"collection": {"id": -1, "name": "冒用插件片单编号", "items": []}},
        )
        assert response.status_code == 400
        assert app.state.store.all("SELECT * FROM collections") == []
