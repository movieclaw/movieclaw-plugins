"""原生 Web 的真实浏览器验收：同源宿主身份、正式原生路由与插件私有存储。

测试专用 require_login 从受控 Cookie 映射可信 Principal，产品不提供该登录入口。
观影数据通过 NativeSource 的只读 HOST_OPS；私有片单由浏览器以当前宿主身份 GET。
所有服务只监听临时本机端口，使用临时数据库，不访问使用者的服务或凭据。
"""

import hashlib
import hmac
import json
import re
import secrets
import socket
import subprocess
import threading
import time
from contextlib import ExitStack, asynccontextmanager, contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from movieclaw_friends.app import create_app
from movieclaw_friends.config import Settings
from movieclaw_friends.native import build_routers
from movieclaw_friends.native_source import NativeSource

playwright_api = pytest.importorskip("playwright.sync_api")
expect = playwright_api.expect
ROOT = Path(__file__).resolve().parents[1]
PREFIX = "/api/v1/plugins/watch-friends"
HOST_COOKIE = "test_host_account"
USERS = {"alice": (1, "林栖"), "bob": (2, "周末电影人"), "carol": (3, "路过同好")}
CREATED = "2026-10-01T00:00:00+00:00"


class HostOps:
    """只模拟 SDK 已登记的两项读操作，任何其他宿主操作直接失败。"""

    def __init__(self):
        self.calls = []

    async def call(self, operation, arguments):
        self.calls.append((operation, arguments.copy()))
        if operation == "members.list":
            return [
                {
                    "id": id,
                    "username": name,
                    "nickname": nickname,
                    "status": "active",
                    "created_at": CREATED,
                }
                for name, (id, nickname) in USERS.items()
            ]
        assert operation == "playback.history", "插件只能调用获准的只读 HOST_OPS"
        member_id = arguments["member_id"]
        nickname = next(nickname for id, nickname in USERS.values() if id == member_id)
        records = []
        for index in range(8):
            started = datetime.now(UTC) - timedelta(hours=index + 1)
            records.append(
                {
                    "id": member_id * 100 + index,
                    "media": {
                        "media_item_id": index + 10,
                        "title": f"{nickname}的影片{index}",
                        "tmdb_id": 550 + index,
                        "kind": "movie",
                        "year": 2025,
                        "poster_path": "/test-poster.jpg",
                        "path": "/private/never-share.mkv",
                    },
                    "started_at": started.isoformat(),
                    "ended_at": None
                    if index == 0
                    else (started + timedelta(minutes=40)).isoformat(),
                    "duration_ms": 7200000,
                    "end_position_ms": 2400000,
                    "progress_percent": 33.3,
                    "watched_ms": 2400000,
                    "completed": False,
                    "device_id": "private-device",
                }
            )
        return {"entries": records, "next_cursor": None}


def native_host(origin, directory, *, demo_enabled=False, collection_count=1):
    ops = HostOps()
    signing_key = secrets.token_bytes(32)

    def signature(path):
        return hmac.new(signing_key, path.encode(), hashlib.sha256).hexdigest()

    business = create_app(
        Settings(
            "",
            origin,
            directory,
            native=True,
            allow_private_peers=True,
            demo_enabled=demo_enabled,
            network_endpoint=origin
            + PREFIX
            + "/federation/directory?sig="
            + signature(PREFIX + "/federation/directory"),
            federation_endpoints={
                path: origin + PREFIX + path + "?sig=" + signature(PREFIX + path)
                for path in ("/federation/requests", "/federation/state", "/federation/data")
            },
        ),
        upstream=NativeSource(ops),
    )

    @asynccontextmanager
    async def lifespan(_app):
        yield
        business.state.store.close()

    host = FastAPI(lifespan=lifespan)
    host.state.friends_public_origin = origin
    host.state.ops = ops
    host.state.business = business
    host.state.calls = []
    host.state.collection_count = collection_count
    host.state.collections_available = True
    host.state.search_available = True
    host.state.media_calls = []
    host.state.series_catalog = {}
    host.state.poster_images = {}
    host.state.backdrop_urls = {}

    def require_login(request: Request):
        name = request.cookies.get(HOST_COOKIE)
        if name not in USERS:
            raise HTTPException(401, "请先登录 MovieClaw")
        member_id, nickname = USERS[name]
        return SimpleNamespace(
            kind="member",
            member_id=member_id,
            name=name,
            plugin=None,
            member=SimpleNamespace(nickname=nickname, created_at=datetime.fromisoformat(CREATED)),
        )

    @host.post("/api/v1/subscriptions/title-preview")
    async def metadata_preview(request: Request):
        require_login(request)
        payload = await request.json()
        return {
            "success": True,
            "data": host.state.series_catalog.get(
                payload.get("title_ref"), {"status": "not_found", "media": None, "seasons": []}
            ),
        }

    @host.middleware("http")
    async def readonly_native_collections(request, call_next):
        # 正式宿主的公开插件网关校验签名；此处也先于业务路由核对受控 HMAC。
        if request.url.path.startswith(PREFIX + "/federation/") and not hmac.compare_digest(
            request.query_params.get("sig", ""), signature(request.url.path)
        ):
            return JSONResponse({"detail": "测试宿主签名无效"}, status_code=403)
        if request.url.path.startswith("/api/v1/collections"):
            assert request.method == "GET", "不得写入宿主片单"
            assert "authorization" not in request.headers, "不得透传访问令牌"
            assert "x-friends-request" not in request.headers, "宿主 GET 不携带插件写标记"
            host.state.calls.append(
                (request.url.path, request.url.query, request.cookies.get(HOST_COOKIE))
            )
        if request.url.path.startswith(("/api/v1/discover/", "/api/v1/images/", "/api/v1/search/")):
            assert "authorization" not in request.headers, "媒体读取不得透传访问令牌"
            assert "x-friends-request" not in request.headers, "宿主媒体接口不携带插件写标记"
        return await call_next(request)

    def collection(id):
        return {
            "id": id,
            "name": "超过容量的片单" if id == 43 else "值得重看的故事",
            "item_count": 501 if id == 43 else host.state.collection_count,
            "kind": "user",
            "builtin": None,
            "manageable": True,
            "editable": True,
            "visibility": "private",
        }

    @host.get("/api/v1/auth/me")
    async def own_account(request: Request):
        if (
            getattr(business.state, "testing_admin", False)
            and request.cookies.get(HOST_COOKIE) == "test-admin"
        ):
            principal = SimpleNamespace(name="管理员")
        else:
            principal = require_login(request)
        return {
            "success": True,
            "data": {
                "username": principal.name,
                "avatar_url": "/api/v1/auth/avatar?v=test"
                if getattr(host.state, "with_avatar", False)
                else None,
            },
        }

    @host.get("/api/v1/collections")
    async def collections(request: Request):
        require_login(request)
        if not host.state.collections_available:
            raise HTTPException(503, "模拟源片单暂不可用")
        return {
            "success": True,
            "data": [
                collection(42),
                collection(43),
                {
                    **collection(99),
                    "name": "他人的私有片单",
                    "manageable": False,
                    "editable": False,
                },
            ],
        }

    @host.get("/api/v1/collections/{id}")
    async def collection_metadata(id: int, request: Request):
        require_login(request)
        return {"success": True, "data": collection(id)}

    @host.get("/api/v1/collections/{id}/items")
    async def collection_items(id: int, request: Request, limit: int = 200, offset: int = 0):
        require_login(request)
        assert 1 <= limit <= 200
        count = collection(id)["item_count"]
        return {
            "success": True,
            "data": [
                {
                    "media_item_id": index + 10,
                    "title": "海上钢琴师" if index == 0 else f"快照影片{index}",
                    "kind": "movie",
                    "year": 1998,
                    "tmdb_id": index + 1900,
                    "poster_url": "https://image.tmdb.org/t/p/w500/native-poster.jpg",
                    "path": "/private/piano.mkv",
                    "stream_url": "https://private.test/stream?token=secret",
                    "device_id": "private-device",
                }
                for index in range(offset, min(offset + limit, count))
            ],
        }

    @host.get("/api/v1/discover/titles/{title_ref:path}")
    async def title_details(title_ref: str, request: Request):
        require_login(request)
        assert title_ref.startswith(("tmdb:movie:", "tmdb:tv:"))
        host.state.media_calls.append(("details", title_ref))
        return {
            "success": True,
            "data": {
                "title": {
                    "title_ref": title_ref,
                    "provider": "tmdb",
                    "external_id": title_ref.split(":")[-1],
                    "media_type": title_ref.split(":")[1],
                    "title": "海上钢琴师" if title_ref.endswith(":1900") else "电影详情示例",
                    "overview": "在大海与钢琴之间，寻找属于自己的故事。<script>不可执行</script>",
                    "release_year": 1998,
                    "genres": ["剧情", "音乐"],
                    "extent_label": "165分钟",
                    "poster_url": "https://image.tmdb.org/t/p/w500/detail-poster.jpg",
                    "backdrop_url": host.state.backdrop_urls.get(title_ref),
                    "library_status": {"private_field": "不得展示的库存数据"},
                },
                "metadata": {
                    "directors": ["朱塞佩·托纳多雷"],
                    "country": "意大利",
                    "language": "英语",
                    "released": "1998-10-28",
                    "source_url": "https://private.test/never-render",
                },
                "library_links": [{"path": "/private/never-render.mkv"}],
            },
        }

    @host.get("/api/v1/images/proxy")
    async def image_proxy(request: Request, url: str, w: int):
        require_login(request)
        assert re.fullmatch(
            r"https://image\.tmdb\.org/t/p/(?:w500|w780|w1280|original)/[A-Za-z0-9_-]+\.(?:jpg|jpeg|png|webp)",
            url,
        ) and w in (240, 500, 1280)
        assert "?" not in url and ".." not in url and "/private/" not in url
        host.state.media_calls.append(("backdrop" if w == 1280 else "poster", url))
        if url in host.state.poster_images:
            return Response(host.state.poster_images[url], media_type="image/jpeg")
        return Response(
            '<svg xmlns="http://www.w3.org/2000/svg" width="200" height="300">'
            '<rect width="200" height="300" fill="#334155"/></svg>',
            media_type="image/svg+xml",
        )

    @host.post("/api/v1/search/titles")
    async def search_titles(request: Request):
        require_login(request)
        payload = await request.json()
        assert payload["provider"] == "tmdb" and payload["save_history"] is False
        if not host.state.search_available:
            raise HTTPException(403, "当前账号没有搜索权限")
        host.state.media_calls.append(("search", payload["query"]))
        return {
            "success": True,
            "data": {
                "titles": [
                    {
                        "title_ref": "tmdb:movie:1900",
                        "provider": "tmdb",
                        "external_id": "1900",
                        "media_type": "movie",
                        "title": "海上钢琴师",
                        "release_year": 1998,
                        "poster_url": "https://image.tmdb.org/t/p/w500/search-poster.jpg",
                        "path": "/private/never-copy.mkv",
                    }
                ],
            },
        }

    member, public = build_routers(business, require_login)
    host.include_router(member, prefix=PREFIX)
    host.include_router(public, prefix=PREFIX)
    host.mount(PREFIX + "/web", StaticFiles(directory=ROOT / "web", html=True))

    @host.get("/apps/watch-friends")
    @host.get("/apps/watch-friends/")
    async def page_entry():
        return RedirectResponse(PREFIX + "/web/")

    return host


@contextmanager
def native_at_port(directory, **options):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
    app = native_host(origin, directory, **options)
    server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert server.started, "测试宿主未启动"
    try:
        yield origin, app
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()


def enter(page, origin, name):
    page.context.add_cookies([{"name": HOST_COOKIE, "value": name, "url": origin}])
    page.goto(origin + "/apps/watch-friends/")
    expect(page.locator("#workspace")).to_be_visible()
    expect(page.locator("#greeting")).to_contain_text(USERS[name][1])
    page.locator('[data-tab="friends"]').click()
    assert page.locator("#login-token").count() == 0
    expect(page.locator("a.brand")).to_have_attribute("href", "/apps/watch-friends")


def consent(page, *, demo=False):
    expect(page.locator("#address-dialog")).to_be_visible()
    expect(
        page.locator("#address-demo-notice" if demo else "#address-real-notice")
    ).to_contain_text("自动回应" if demo else "服务器地址会暴露")
    expect(page.locator("#confirm-address")).to_be_disabled()
    page.locator("#address-checkbox").check()
    page.locator("#confirm-address").click()
    expect(page.locator("#address-dialog")).not_to_be_visible()


def browser_pages(stack, count=1):
    playwright = stack.enter_context(playwright_api.sync_playwright())
    browser = playwright.chromium.launch()
    stack.callback(browser.close)
    pages = []
    for _ in range(count):
        context = browser.new_context(viewport={"width": 1440, "height": 1050})
        stack.callback(context.close)
        pages.append(context.new_page())
    return pages


def test_native_assets_and_manifest_are_generated():
    subprocess.run(["python3", str(ROOT / "tools/build_web.py"), "--check"], check=True)
    manifest = json.loads((ROOT / "web/manifest.webmanifest").read_text())
    assert manifest["short_name"] == "映迹" and manifest["start_url"] == "."
    assert manifest["icons"][0]["purpose"] == "monochrome"
    assert (ROOT / "web" / manifest["icons"][0]["src"]).is_file()


def test_web_native_plaza_consent_cross_instance_and_mobile(tmp_path):
    errors = []
    with ExitStack() as stack:
        a_url, a = stack.enter_context(native_at_port(tmp_path / "a"))
        b_url, b = stack.enter_context(native_at_port(tmp_path / "b"))
        alice, bob, carol = browser_pages(stack, 3)
        for page in (alice, bob, carol):
            page.on("pageerror", lambda exc: errors.append(str(exc)))
        enter(alice, a_url, "alice")
        enter(bob, b_url, "bob")
        enter(carol, a_url, "carol")
        carol.locator('[data-tab="privacy"]').click()
        carol.locator("#discoverable-input").check()
        carol.get_by_role("button", name="保存设置", exact=True).click()
        expect(carol.locator("#preview-dialog")).to_be_visible()
        assert alice.context.request.get(a_url + PREFIX + "/directory").json()["users"] == []
        carol.locator("#confirm-public").click()
        expect(carol.locator("#preview-dialog")).not_to_be_visible()
        alice.locator('[data-tab="directory"]').click()
        expect(alice.locator("#directory-list")).to_contain_text("路过同好的影片5")
        expect(alice.locator("#directory-list")).not_to_contain_text("路过同好的影片6")
        expect(alice.locator("#directory-list")).not_to_contain_text("路过同好的影片0")
        alice.get_by_role("button", name="申请添加好友", exact=True).click()
        consent(alice)
        carol.locator('[data-tab="friends"]').click()
        carol.locator("#refresh-button").click()
        carol.locator("#incoming-requests").get_by_role("button", name="接受", exact=True).click()
        consent(carol)
        alice.locator('[data-tab="friends"]').click()
        alice.locator("#refresh-button").click()
        alice.locator(".friend-button", has_text="路过同好").click()
        expect(alice.locator("#friend-detail")).to_contain_text("尚未授权分享观影历史")
        bob.locator('[data-tab="privacy"]').click()
        bob.locator("#share-current-input").check()
        bob.locator("#share-history-input").check()
        bob.get_by_role("button", name="保存设置", exact=True).click()
        expect(bob.locator("#preview-dialog")).to_be_visible()
        bob.locator("#confirm-public").click()
        expect(bob.locator("#toast")).to_contain_text("分享设置已保存")
        bob.on("dialog", lambda dialog: dialog.accept())
        bob.locator('[data-tab="collections"]').click()
        bob.locator(".collection-card", has_text="值得重看的故事").get_by_role(
            "button", name="分享给好友", exact=True
        ).click()
        expect(bob.locator("#collection-list")).to_contain_text("已分享快照")
        invite = bob.context.request.get(b_url + PREFIX + "/me").json()["user"]["invite_code"]
        alice.locator("#invite-input").fill(invite)
        alice.locator("#invite-form").get_by_role("button", name="添加好友").click()
        consent(alice)
        expect(alice.locator("#toast")).to_contain_text("好友申请已发送")
        bob.locator('[data-tab="friends"]').click()
        bob.locator("#refresh-button").click()
        bob.locator("#incoming-requests").get_by_role("button", name="接受", exact=True).click()
        consent(bob)
        alice.locator("#refresh-button").click()
        alice.locator(".friend-button", has_text="周末电影人").click()
        expect(alice.locator("#friend-detail")).to_contain_text("周末电影人的影片0")
        expect(alice.locator("#friend-detail")).to_contain_text("周末电影人的影片1")
        expect(alice.locator("#friend-detail")).to_contain_text("值得重看的故事")
        alice.locator("#friend-detail details").get_by_text("值得重看的故事").click()
        expect(alice.locator("#friend-detail")).to_contain_text("海上钢琴师")
        text = alice.locator("body").inner_text()
        assert "127.0.0.1" not in text and "/private/" not in text and "secret" not in text
        assert alice.evaluate("Object.keys(localStorage)") == []
        assert alice.evaluate("Object.keys(sessionStorage)") == []
        alice.screenshot(path=str(tmp_path / "friends-native-desktop.png"), full_page=True)
        alice.set_viewport_size({"width": 390, "height": 844})
        assert alice.evaluate("document.documentElement.scrollWidth <= innerWidth")
        alice.screenshot(path=str(tmp_path / "friends-native-mobile.png"), full_page=True)
        alice.on("dialog", lambda dialog: dialog.accept())
        alice.get_by_role("button", name="删除好友", exact=True).click()
        expect(alice.locator("#friend-list")).not_to_contain_text("周末电影人")
        assert not errors, errors
        assert all(
            operation in {"members.list", "playback.history"}
            for operation, _ in a.state.ops.calls + b.state.ops.calls
        )
        assert all(name == "bob" for _, _, name in b.state.calls)


def test_web_native_discards_prior_account_preview_and_saves_without_followup_me(tmp_path):
    with ExitStack() as stack:
        origin, _ = stack.enter_context(native_at_port(tmp_path / "plugin"))
        (page,) = browser_pages(stack)
        enter(page, origin, "alice")
        old_preview = page.context.request.post(
            origin + PREFIX + "/profile/preview",
            data={
                "nickname": "林栖",
                "discoverable": False,
                "share_current": False,
                "share_history": True,
            },
            headers={"X-Friends-Request": "1"},
        ).json()
        held = []
        page.route("**" + PREFIX + "/profile/preview", lambda route: held.append(route))
        page.locator('[data-tab="privacy"]').click()
        page.locator("#share-history-input").check()
        page.locator("#preview-button").click()
        page.wait_for_timeout(50)
        assert len(held) == 1
        page.context.clear_cookies()
        page.locator("#refresh-button").click()
        expect(page.locator("#auth-required")).to_be_visible()
        expect(page.locator("#host-login-link")).to_have_attribute("target", "_top")
        page.context.add_cookies([{"name": HOST_COOKIE, "value": "bob", "url": origin}])
        page.locator("#retry-session").click()
        expect(page.locator("#greeting")).to_contain_text("周末电影人")
        held[0].fulfill(json=old_preview)
        expect(page.locator("#preview-button")).to_be_enabled()
        expect(page.locator("#preview-dialog")).not_to_be_visible()
        assert "林栖的影片" not in page.locator("body").inner_text()
        unexpected_me = []

        def fail_me(route):
            unexpected_me.append(True)
            route.fulfill(status=502, json={"detail": "模拟宿主离线"})

        page.route("**" + PREFIX + "/me", fail_me)
        page.unroute("**" + PREFIX + "/profile/preview")
        page.locator('[data-tab="privacy"]').click()
        page.locator("#share-history-input").check()
        page.get_by_role("button", name="保存设置", exact=True).click()
        expect(page.locator("#preview-dialog")).to_be_visible()
        page.locator("#confirm-public").click()
        expect(page.locator("#toast")).to_contain_text("分享设置已保存")
        assert not unexpected_me


def test_web_native_private_collection_complete_snapshot_and_source_failure(tmp_path):
    with ExitStack() as stack:
        origin, host = stack.enter_context(
            native_at_port(tmp_path / "plugin", collection_count=401)
        )
        (page,) = browser_pages(stack)
        enter(page, origin, "bob")
        page.on("dialog", lambda dialog: dialog.accept())
        page.locator('[data-tab="collections"]').click()
        expect(page.locator("#collection-list")).not_to_contain_text("他人的私有片单")
        page.locator(".collection-card", has_text="值得重看的故事").get_by_role(
            "button", name="分享给好友", exact=True
        ).click()
        expect(page.locator("#collection-list")).to_contain_text("已分享快照")
        saved = host.state.business.state.store.one(
            "SELECT data FROM collections WHERE source_id=42"
        )
        snapshot = json.loads(saved["data"])
        assert len(snapshot["items"]) == 401
        assert set(snapshot["items"][0]) <= {
            "media_item_id",
            "tmdb_id",
            "kind",
            "title",
            "year",
            "season_number",
            "episode_number",
            "episode_title",
            "poster_path",
            "demo_id",
        }
        assert snapshot["items"][0].get("demo_id") is None
        assert snapshot["items"][0]["poster_path"] == "/native-poster.jpg"
        assert "/private/" not in saved["data"] and "secret" not in saved["data"]
        assert [
            query
            for path, query, _ in host.state.calls
            if path.endswith("/42/items") and query.startswith("limit=200")
        ] == [
            "limit=200&offset=0",
            "limit=200&offset=200",
            "limit=200&offset=400",
        ]
        page.locator(".collection-card", has_text="超过容量的片单").get_by_role(
            "button", name="分享给好友", exact=True
        ).click()
        expect(page.locator("#toast")).to_contain_text("超过 500")
        assert (
            host.state.business.state.store.one("SELECT * FROM collections WHERE source_id=43")
            is None
        )
        host.state.collections_available = False
        page.locator("#refresh-button").click()
        expect(page.locator("#collection-notice")).to_contain_text("已分享的快照仍可停止分享")
        expect(page.locator("#collection-list")).to_contain_text("来源片单已不可用")
        page.get_by_role("button", name="停止分享", exact=True).click()
        expect(page.locator("#collection-list")).not_to_contain_text("已分享快照")
        assert (
            host.state.business.state.store.one("SELECT * FROM collections WHERE source_id=42")
            is None
        )


def test_web_native_virtual_friend_is_labeled_and_reads_no_real_viewing_data(tmp_path):
    with ExitStack() as stack:
        origin, host = stack.enter_context(native_at_port(tmp_path / "plugin", demo_enabled=True))
        (page,) = browser_pages(stack)
        enter(page, origin, "alice")
        expect(page.locator("#friend-list .demo-preview-button")).to_have_count(4)
        expect(page.locator("#friend-list .avatar img")).to_have_count(4)
        assert (
            len(
                set(
                    page.locator("#friend-list .avatar img").evaluate_all(
                        "images => images.map(image => image.getAttribute('src'))"
                    )
                )
            )
            == 4
        )
        expect(page.locator("#friend-detail")).to_contain_text("无需添加好友")
        expect(page.locator("#friend-detail .now-card")).to_be_visible()
        assert page.locator("#friend-detail").get_by_role("button", name="删除好友").count() == 0
        current = page.locator("#friend-detail .now-card")
        poster = current.locator("img")
        expect(poster).to_have_attribute(
            "src",
            re.compile(
                r"^/api/v1/images/proxy\?url=https%3A%2F%2Fimage\.tmdb\.org%2Ft%2Fp%2Fw500%2F"
            ),
        )
        expect(poster).to_be_visible()
        expect(poster).to_have_js_property("complete", True)
        assert poster.evaluate("image => image.complete && image.naturalWidth > 0")
        old_progress = current.locator(".progress-caption").inner_text()
        current_handle = current.element_handle()
        current.locator(".media-title").click()
        expect(page.locator("#media-dialog")).to_contain_text("演示观看")
        expect(page.locator("#media-dialog .media-overview")).not_to_be_empty()
        expect(page.locator("#media-dialog .media-dialog-copy>a[target=_top]")).to_have_attribute(
            "href", re.compile(r"^/media/movie/[0-9]+$")
        )
        page.wait_for_timeout(1300)
        assert current_handle.evaluate(
            "element => element === document.querySelector('#friend-detail .now-card')"
        )
        assert current.locator(".progress-caption").inner_text() != old_progress
        expect(page.locator("#media-dialog")).to_be_visible()
        page.locator("#close-media").click()
        assert page.context.request.get(origin + PREFIX + "/friends").json()["friends"] == []
        page.locator('[data-tab="directory"]').click()
        expect(page.locator("#directory-list .directory-card")).to_have_count(4)
        expect(page.locator("#directory-list .demo-badge")).to_have_count(4)
        expect(page.locator("#directory-list .history-row").first.locator("img")).to_be_visible()
        # Default personal page may synchronize the owner archive (limit=200).
        # Browsing demo/favorites must not request another real sharing snapshot.
        assert not any(
            operation == "playback.history" and args.get("limit") == 50
            for operation, args in host.state.ops.calls
        )
        assert not host.state.calls
        assert any(kind == "poster" for kind, _ in host.state.media_calls)
        assert all(
            value.startswith("tmdb:movie:")
            for kind, value in host.state.media_calls
            if kind == "details"
        ), "片源检查只按真实作品身份读取当前宿主"
        page.screenshot(path=str(tmp_path / "demo-directory-desktop.png"), full_page=True)
        page.locator(".directory-card", has_text="阿川").get_by_role(
            "button", name="添加虚拟演示好友", exact=True
        ).click()
        consent(page, demo=True)
        page.locator('[data-tab="friends"]').click()
        page.locator("#refresh-button").click()
        page.locator(".friend-button", has_text="阿川").click()
        expect(page.locator("#friend-detail .detail-heading")).to_contain_text("阿川")
        expect(page.locator("#friend-detail .now-card")).to_be_visible()
        assert len(page.context.request.get(origin + PREFIX + "/friends").json()["friends"]) == 1
        page.locator('[data-tab="collections"]').click()
        expect(page.locator("#demo-collections")).to_be_visible()
        expect(page.locator("#demo-collections .shared-collection-card")).to_have_count(8)
        expect(page.locator("#demo-collections .collection-author .avatar img")).to_have_count(8)
        page.get_by_role("button", name="片单灵感说明", exact=True).click()
        expect(page.get_by_role("tooltip")).to_contain_text("不属于你的个人片单")
        page.keyboard.press("Escape")
        assert page.locator("#demo-collections button.button:not(.person-link)").count() == 0
        page.locator("#demo-collections summary").first.click()
        expect(page.locator("#demo-collections .media-poster img").first).to_be_visible()
        expect(page.locator("#collection-list .collection-card")).to_have_count(2)
        page.set_viewport_size({"width": 390, "height": 844})
        page.locator('[data-tab="friends"]').click()
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.locator('[data-tab="collections"]').click()
        page.locator("#demo-collections").scroll_into_view_if_needed()
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.screenshot(path=str(tmp_path / "demo-collections-mobile.png"), full_page=True)
        profile = page.context.request.get(origin + PREFIX + "/me").json()["user"]
        assert (
            not profile["share_current"]
            and not profile["share_history"]
            and not profile["discoverable"]
        )


def test_web_native_posters_details_and_private_plugin_lists(tmp_path):
    with ExitStack() as stack:
        origin, host = stack.enter_context(native_at_port(tmp_path / "plugin"))
        (page,) = browser_pages(stack)
        enter(page, origin, "bob")
        expect(page.locator("#friend-list")).not_to_contain_text("演示")
        page.on("dialog", lambda dialog: dialog.accept())
        page.locator('[data-tab="collections"]').click()
        expect(page.locator("#demo-collections")).not_to_be_visible()
        source = page.locator("#collection-list .collection-card", has_text="值得重看的故事")
        source.locator("summary").click()
        expect(source.locator(".collection-contents .media-poster img")).to_have_attribute(
            "src", re.compile(r"^/api/v1/images/proxy\?")
        )
        source.locator(".media-title", has_text="海上钢琴师").click()
        expect(page.locator("#media-dialog")).to_contain_text("在大海与钢琴之间")
        expect(page.locator("#media-dialog")).to_contain_text("<script>不可执行</script>")
        expect(page.locator("#media-dialog")).to_contain_text("165分钟")
        expect(page.locator("#media-dialog")).to_contain_text("朱塞佩·托纳多雷")
        expect(page.locator("#media-dialog")).not_to_contain_text("分钟 分钟")
        expect(page.locator("#media-dialog")).not_to_contain_text("[object Object]")
        expect(page.locator("#media-dialog")).not_to_contain_text("不得展示的库存")
        expect(page.locator("#media-dialog .media-poster img")).to_have_attribute(
            "src", re.compile("detail-poster")
        )
        assert page.locator("#media-dialog script").count() == 0
        expect(page.locator("#media-dialog .media-dialog-copy>a[target=_top]")).to_have_attribute(
            "href", "/media/movie/1900"
        )
        expect(page.locator("#media-dialog .media-dialog-copy>a[target=_top]")).to_have_attribute(
            "target", "_top"
        )
        page.locator("#media-dialog").get_by_role("button", name="加入片单").click()
        page.locator("#list-target-dialog").get_by_role("button", name="新建片单并加入").click()
        expect(page.locator("#list-create-dialog")).to_be_visible()
        expect(page.locator("#list-draft-count")).to_have_text("1")
        page.locator("#list-name").fill("海风与音乐")
        page.locator("#list-description").fill("属于自己的私有电影片单")
        page.set_viewport_size({"width": 390, "height": 844})
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        assert page.locator("#list-create-dialog").evaluate(
            "dialog => dialog.scrollWidth <= dialog.clientWidth"
        )
        page.screenshot(path=str(tmp_path / "create-list-mobile.png"))
        page.set_viewport_size({"width": 1440, "height": 1050})
        page.get_by_role("button", name="创建私有片单", exact=True).click()
        expect(page.locator("#plugin-list-list")).to_contain_text("海风与音乐")
        expect(page.locator("#plugin-list-list")).to_contain_text("仅自己可见")
        saved = page.context.request.get(origin + PREFIX + "/lists").json()["lists"][0]
        assert saved["shared"] is False and saved["items"][0]["tmdb_id"] == 1900
        assert saved["items"][0]["poster_path"] == "/native-poster.jpg"
        assert "poster_url" not in saved["items"][0] and "path" not in saved["items"][0]
        own = page.locator(".plugin-list-card", has_text="海风与音乐")
        own.get_by_role("button", name="分享给好友", exact=True).click()
        expect(own).to_contain_text("已分享给好友")
        own.get_by_role("button", name="停止分享", exact=True).click()
        expect(own).to_contain_text("仅自己可见")
        page.locator("#open-list-creator").click()
        page.locator("#list-name").fill("搜索选片")
        page.locator("#list-search-input").fill("钢琴")
        page.locator("#list-search-button").click()
        page.locator("#list-search-results").get_by_role("button", name="选择", exact=True).click()
        expect(page.locator("#list-draft-count")).to_have_text("1")
        page.get_by_role("button", name="创建私有片单", exact=True).click()
        expect(page.locator("#plugin-list-list")).to_contain_text("搜索选片")
        host.state.search_available = False
        page.locator("#open-list-creator").click()
        page.locator("#list-name").fill("先留一个空片单")
        page.locator("#list-search-input").fill("无搜索权限")
        page.locator("#list-search-button").click()
        expect(page.locator("#list-search-notice")).to_contain_text("没有主程序的影片搜索权限")
        page.get_by_role("button", name="创建私有片单", exact=True).click()
        expect(page.locator("#plugin-list-list")).to_contain_text("先留一个空片单")
        empty = page.locator(".plugin-list-card", has_text="先留一个空片单")
        empty.get_by_role("button", name="删除片单").click()
        expect(page.locator("#plugin-list-list")).not_to_contain_text("先留一个空片单")
        assert ("details", "tmdb:movie:1900") in host.state.media_calls
        assert ("search", "钢琴") in host.state.media_calls


def test_web_native_rejects_untrusted_poster_urls(tmp_path):
    with ExitStack() as stack:
        origin, host = stack.enter_context(native_at_port(tmp_path / "plugin"))
        (page,) = browser_pages(stack)
        external = []
        page.on(
            "request",
            lambda request: (
                external.append(request.url) if not request.url.startswith(origin) else None
            ),
        )
        page.route(
            "**" + PREFIX + "/directory",
            lambda route: route.fulfill(
                json={
                    "users": [
                        {
                            "id": "untrusted",
                            "nickname": "外来资料",
                            "history": [
                                {
                                    "media": {
                                        "title": "无可信身份的影片",
                                        "poster_path": "/../../private.jpg?secret=x",
                                        "poster_url": "https://foreign.invalid/image.jpg?token=private",
                                        "media_item_id": 1900,
                                    }
                                }
                            ],
                        }
                    ]
                }
            ),
        )
        enter(page, origin, "alice")
        page.locator('[data-tab="directory"]').click()
        expect(page.locator("#directory-list")).to_contain_text("无可信身份的影片")
        assert page.locator("#directory-list img").count() == 0
        page.locator("#directory-list .media-title").click()
        expect(page.locator("#media-dialog")).to_contain_text("暂无剧情简介")
        assert not host.state.media_calls, "不得将远端 media_item_id 套用为本机详情编号"
        assert not external, external


def test_web_native_real_friend_takes_precedence_over_readonly_demo(tmp_path):
    with ExitStack() as stack:
        origin, _ = stack.enter_context(native_at_port(tmp_path / "plugin", demo_enabled=True))
        alice, bob = browser_pages(stack, 2)
        enter(alice, origin, "alice")
        enter(bob, origin, "bob")
        expect(alice.locator(".demo-preview-button")).to_have_count(4)
        bob.locator('[data-tab="privacy"]').click()
        bob.locator("#discoverable-input").check()
        bob.get_by_role("button", name="保存设置", exact=True).click()
        bob.locator("#confirm-public").click()
        alice.locator('[data-tab="directory"]').click()
        alice.locator(".directory-card", has_text="周末电影人").get_by_role(
            "button", name="申请添加好友", exact=True
        ).click()
        consent(alice)
        bob.locator('[data-tab="friends"]').click()
        bob.locator("#refresh-button").click()
        bob.locator("#incoming-requests").get_by_role("button", name="接受", exact=True).click()
        consent(bob)
        alice.locator('[data-tab="friends"]').click()
        alice.locator("#refresh-button").click()
        expect(alice.locator(".demo-preview-button")).to_have_count(4)
        alice.locator(".friend-button", has_text="周末电影人").click()
        expect(alice.locator("#friend-detail")).to_contain_text("周末电影人")
        expect(alice.locator("#friend-detail .demo-disclaimer")).to_have_count(0)
        alice.locator('[data-tab="collections"]').click()
        expect(alice.locator("#demo-collections")).to_be_visible()
        expect(alice.locator("#collection-list")).to_contain_text("值得重看的故事")


def test_web_native_rejects_mismatched_host_movie_details(tmp_path):
    with ExitStack() as stack:
        origin, _ = stack.enter_context(native_at_port(tmp_path / "plugin"))
        (page,) = browser_pages(stack)
        page.route(
            "**/api/v1/discover/titles/*",
            lambda route: route.fulfill(
                json={
                    "success": True,
                    "data": {
                        "title": {
                            "title_ref": "tmdb:movie:999",
                            "external_id": "999",
                            "provider": "tmdb",
                            "media_type": "movie",
                            "title": "错误影片",
                            "overview": "错误影片的简介不得展示",
                        },
                        "metadata": {},
                    },
                }
            ),
        )
        enter(page, origin, "bob")
        page.locator('[data-tab="collections"]').click()
        source = page.locator("#collection-list .collection-card", has_text="值得重看的故事")
        source.locator("summary").click()
        source.locator(".media-title").click()
        expect(page.locator("#media-dialog")).to_contain_text("影片身份不匹配")
        expect(page.locator("#media-dialog")).not_to_contain_text("错误影片的简介")
        expect(page.locator("#media-dialog h3")).to_have_text("海上钢琴师")


def test_web_native_demo_preference_persists_recovers_and_preserves_real_data(tmp_path):
    with ExitStack() as stack:
        origin, _ = stack.enter_context(native_at_port(tmp_path / "plugin", demo_enabled=True))
        page, bob = browser_pages(stack, 2)
        enter(page, origin, "alice")
        expect(page.locator("#demo-toggle")).to_be_checked()
        expect(page.locator("#demo-toggle")).not_to_be_visible()
        expect(page.locator(".page-heading #demo-toggle")).to_have_count(0)
        expect(page.locator("#friend-list .presence.is-online")).to_have_count(2)
        expect(page.locator("#friend-list .presence.is-offline")).to_have_count(2)
        page.locator(".demo-preview-button", has_text="小鹿").click()
        expect(page.locator("#friend-detail .presence.is-offline")).to_be_visible()
        expect(page.locator("#friend-detail .now-card")).to_have_count(0)
        demo = page.context.request.get(origin + PREFIX + "/demo").json()
        response = page.context.request.post(
            origin + PREFIX + "/requests",
            headers={"X-Friends-Request": "1"},
            data={"user_id": demo["users"][1]["user"]["id"], "address_consent": True},
        )
        assert response.status == 200
        own = page.context.request.post(
            origin + PREFIX + "/lists",
            headers={"X-Friends-Request": "1"},
            data={"name": "我的真实私有片单", "description": "偏好开关不会影响", "items": []},
        ).json()
        page.locator("#refresh-button").click()
        expect(page.locator("#friend-list .friend-button")).to_have_count(4)
        expect(page.locator("#friend-list .friend-button:not(.demo-preview-button)")).to_have_count(
            1
        )
        page.locator('[data-tab="privacy"]').click()
        page.locator("#demo-toggle").uncheck()
        expect(page.locator("#demo-status")).to_contain_text("已关闭")
        expect(page.locator("#panel-privacy #demo-toggle")).to_be_visible()
        expect(page.locator("#friend-list .friend-button")).to_have_count(0)
        page.reload()
        expect(page.locator("#workspace")).to_be_visible()
        expect(page.locator("#demo-toggle")).not_to_be_checked()
        expect(page.locator("#demo-status")).to_contain_text("已关闭")
        enter(bob, origin, "bob")
        expect(bob.locator("#demo-toggle")).to_be_checked()
        expect(bob.locator("#friend-list .friend-button")).to_have_count(4)
        page.locator('[data-tab="directory"]').click()
        expect(page.locator("#directory-list .directory-card")).to_have_count(0)
        page.locator('[data-tab="collections"]').click()
        expect(page.locator("#demo-collections")).not_to_be_visible()
        expect(page.locator("#plugin-list-list")).to_contain_text("我的真实私有片单")
        expect(page.locator("#collection-list")).to_contain_text("值得重看的故事")
        held = []

        def hold_off_response(route):
            if not held:
                held.append(route)
            else:
                route.continue_()

        page.route("**" + PREFIX + "/friends", hold_off_response)
        expect(page.locator("#refresh-button")).to_be_enabled()
        page.locator("#refresh-button").click()
        page.wait_for_timeout(100)
        assert len(held) == 1
        page.locator('[data-tab="privacy"]').click()
        page.locator("#demo-toggle").check()
        expect(page.locator("#demo-status")).to_contain_text("已开启")
        held[0].fulfill(json={"friends": []})
        expect(page.locator("#friend-list .friend-button:not(.demo-preview-button)")).to_have_count(
            1
        )
        expect(page.locator("#friend-list .friend-button")).to_have_count(4)
        page.route(
            "**" + PREFIX + "/demo/preferences",
            lambda route: route.fulfill(status=503, json={"detail": "模拟保存失败"}),
        )
        page.locator('[data-tab="privacy"]').click()
        page.locator("#demo-toggle").uncheck()
        expect(page.locator("#toast")).to_contain_text("已恢复原设置")
        expect(page.locator("#demo-toggle")).to_be_checked()
        assert page.context.request.get(origin + PREFIX + "/lists").json()["lists"][0] == own
        profile = page.context.request.get(origin + PREFIX + "/me").json()["user"]
        assert not any(profile[key] for key in ("share_current", "share_history", "discoverable"))


def test_web_native_global_demo_gate_is_explicit(tmp_path):
    with ExitStack() as stack:
        origin, _ = stack.enter_context(native_at_port(tmp_path / "plugin", demo_enabled=False))
        (page,) = browser_pages(stack)
        enter(page, origin, "alice")
        expect(page.locator("#demo-toggle")).to_be_disabled()
        expect(page.locator("#demo-toggle")).not_to_be_checked()
        expect(page.locator("#demo-status")).to_contain_text("管理员已关闭")
        expect(page.locator("#friend-list .demo-preview-button")).to_have_count(0)


def test_web_native_favorites_private_edit_and_separate_public_confirmation(tmp_path):
    with ExitStack() as stack:
        origin, host = stack.enter_context(native_at_port(tmp_path / "plugin"))
        page, viewer = browser_pages(stack, 2)
        enter(page, origin, "alice")
        enter(viewer, origin, "bob")
        page.locator('[data-tab="collections"]').click()
        page.locator("#edit-favorites").click()
        expect(page.locator("#list-metadata")).not_to_be_visible()
        page.locator("#list-search-input").fill("钢琴")
        page.locator("#list-search-button").click()
        page.locator("#list-search-results").get_by_role("button", name="选择", exact=True).click()
        page.get_by_role("button", name="保存私人喜欢", exact=True).click()
        expect(page.locator("#favorites-list")).to_contain_text("海上钢琴师")
        expect(page.locator("#favorites-public-notice")).to_contain_text("不会自动公开")
        saved = page.context.request.get(origin + PREFIX + "/favorites").json()
        assert len(saved["items"]) == 1 and saved["public_items"] == []
        page.locator('[data-tab="privacy"]').click()
        page.locator("#discoverable-input").check()
        page.locator("#public-recent-input").uncheck()
        page.locator("#share-favorites-input").check()
        expect(page.locator("#public-card-preview")).to_contain_text("喜欢电影 · 1 部")
        page.get_by_role("button", name="保存设置", exact=True).click()
        expect(page.locator("#preview-content .preview-block").first).to_contain_text("海上钢琴师")
        expect(
            page.locator("#preview-content .preview-block").first.locator(".history-row")
        ).to_have_count(0)
        page.locator("#confirm-public").click()
        expect(page.locator("#preview-dialog")).not_to_be_visible()
        viewer.locator('[data-tab="directory"]').click()
        expect(viewer.locator("#directory-list")).to_contain_text("海上钢琴师")
        page.locator('[data-tab="collections"]').click()
        page.locator("#edit-favorites").click()
        page.locator("#list-draft-items").get_by_role("button", name="移除").click()
        page.get_by_role("button", name="保存私人喜欢", exact=True).click()
        expect(page.locator("#favorites-public-notice")).to_contain_text("上次确认的 1 部")
        saved = page.context.request.get(origin + PREFIX + "/favorites").json()
        assert saved["items"] == [] and len(saved["public_items"]) == 1
        viewer.locator("#refresh-button").click()
        expect(viewer.locator("#directory-list")).to_contain_text("海上钢琴师")
        page.locator('[data-tab="privacy"]').click()
        page.get_by_role("button", name="保存设置", exact=True).click()
        expect(page.locator("#preview-dialog")).to_contain_text("当前喜欢列表为空")
        page.context.request.put(
            origin + PREFIX + "/favorites",
            headers={"X-Friends-Request": "1"},
            data={"items": [{"title": "另一部喜欢的电影", "tmdb_id": 1234, "kind": "movie"}]},
        )
        page.locator("#confirm-public").click()
        expect(page.locator("#preview-dialog")).not_to_be_visible()
        expect(page.locator("#toast")).to_contain_text("变化")
        page.get_by_role("button", name="保存设置", exact=True).click()
        expect(page.locator("#preview-dialog")).to_contain_text("另一部喜欢的电影")
        page.locator("#confirm-public").click()
        expect(page.locator("#preview-dialog")).not_to_be_visible()
        viewer.locator("#refresh-button").click()
        expect(viewer.locator("#directory-list")).to_contain_text("另一部喜欢的电影")
        expect(viewer.locator("#directory-list")).not_to_contain_text("海上钢琴师")
        # Default personal page may synchronize the owner archive (limit=200).
        # Browsing demo/favorites must not request another real sharing snapshot.
        assert not any(
            operation == "playback.history" and args.get("limit") == 50
            for operation, args in host.state.ops.calls
        )
        page.evaluate("document.documentElement.style.setProperty('--mc-accent','#ff0000')")
        assert (
            page.locator("#privacy-form button[type=submit]").evaluate(
                "button => getComputedStyle(button).backgroundColor"
            )
            == "rgb(197, 213, 231)"
        )


def test_web_native_add_movie_to_existing_private_list(tmp_path):
    with ExitStack() as stack:
        origin, _ = stack.enter_context(native_at_port(tmp_path / "plugin"))
        (page,) = browser_pages(stack)
        enter(page, origin, "alice")
        saved = page.context.request.post(
            origin + PREFIX + "/lists",
            headers={"X-Friends-Request": "1"},
            data={"name": "待加入的私人片单", "description": "", "items": []},
        ).json()
        page.locator('[data-tab="collections"]').click()
        source = page.locator("#collection-list .collection-card", has_text="值得重看的故事")
        source.locator("summary").click()
        source.locator(".media-title").click()
        page.locator("#media-dialog").get_by_role("button", name="加入片单", exact=True).click()
        page.locator("#list-target-options").get_by_role(
            "button", name="待加入的私人片单 私人片单"
        ).click()
        expect(page.locator("#list-target-dialog")).not_to_be_visible()
        result = page.context.request.get(origin + PREFIX + "/lists").json()["lists"][0]
        assert result["id"] == saved["id"] and result["shared"] is False
        assert len(result["items"]) == 1 and result["items"][0]["tmdb_id"] == 1900


def test_web_edits_private_list_order_and_detects_revision_conflict(tmp_path):
    """编辑、排序和删片只改私有正文；已有分享必须另行确认，冲突保留草稿。"""
    with ExitStack() as stack:
        origin, _ = stack.enter_context(native_at_port(tmp_path / "plugin"))
        (page,) = browser_pages(stack)
        enter(page, origin, "alice")
        headers = {"X-Friends-Request": "1"}
        items = [
            {"tmdb_id": 100 + i, "kind": "movie", "title": f"影片{i}", "poster_path": "/test.jpg"}
            for i in range(1, 4)
        ]
        created = page.context.request.post(
            origin + PREFIX + "/lists",
            headers=headers,
            data={"name": "原始片单", "description": "原始介绍", "items": items},
        ).json()
        endpoint = origin + PREFIX + "/lists/" + str(created["id"])
        assert page.context.request.post(endpoint + "/share", headers=headers).ok
        page.locator('[data-tab="collections"]').click()
        card = page.locator(".plugin-list-card", has_text="原始片单")
        card.get_by_role("button", name="编辑片单", exact=True).click()
        expect(page.locator("#list-create-title")).to_have_text("编辑片单")
        page.locator("#list-name").fill("新的周末片单")
        page.locator("#list-description").fill("重新整理的介绍")
        page.get_by_role("button", name="上移影片3", exact=True).click()
        page.locator("#list-draft-items .media-item", has_text="影片2").get_by_role(
            "button", name="移除", exact=True
        ).click()
        page.locator("#list-search-input").fill("钢琴")
        page.locator("#list-search-button").click()
        page.locator("#list-search-results").get_by_role("button", name="选择", exact=True).click()
        page.get_by_role("button", name="保存片单修改", exact=True).click()
        expect(page.locator("#list-create-dialog")).not_to_be_visible()
        saved = page.context.request.get(origin + PREFIX + "/lists").json()["lists"][0]
        assert saved["name"] == "新的周末片单" and saved["description"] == "重新整理的介绍"
        assert [item["tmdb_id"] for item in saved["items"]] == [101, 103, 1900]
        assert saved["revision"] == created["revision"] + 1
        shared = page.context.request.get(origin + PREFIX + "/preview").json()["collections"][0]
        assert shared["name"] == "原始片单" and [item["tmdb_id"] for item in shared["items"]] == [
            101,
            102,
            103,
        ]
        page.on("dialog", lambda dialog: dialog.accept())
        card = page.locator(".plugin-list-card", has_text="新的周末片单")
        card.get_by_role("button", name="更新共享快照", exact=True).click()
        expect(page.locator("#toast")).to_contain_text("共享快照已更新")
        assert (
            page.context.request.get(origin + PREFIX + "/preview").json()["collections"][0]["name"]
            == "新的周末片单"
        )
        card.get_by_role("button", name="编辑片单", exact=True).click()
        page.locator("#list-name").fill("不应覆盖其他窗口")
        changed = page.context.request.put(
            endpoint,
            headers=headers,
            data={
                "name": "另一个窗口",
                "description": saved["description"],
                "items": saved["items"],
                "revision": saved["revision"],
            },
        )
        assert changed.ok
        page.get_by_role("button", name="保存片单修改", exact=True).click()
        expect(page.locator("#toast")).to_contain_text("片单已在别处修改")
        expect(page.locator("#list-create-dialog")).to_be_visible()
        expect(page.locator("#list-name")).to_have_value("不应覆盖其他窗口")
        assert (
            page.context.request.get(origin + PREFIX + "/lists").json()["lists"][0]["name"]
            == "另一个窗口"
        )


def test_web_remote_feed_routes_reactions_and_opaque_comments_to_bound_friend(tmp_path):
    """本服和跨服范围分开，远端帖号不送本服举报，评论游标及编号均编码。"""
    with ExitStack() as stack:
        origin, _ = stack.enter_context(native_at_port(tmp_path / "plugin"))
        (page,) = browser_pages(stack)
        calls = []
        remote = {
            "id": "rel-remote",
            "user_id": "peer",
            "nickname": "远方影友",
            "is_remote": True,
            "status": "accepted",
        }
        page.route(
            "**" + PREFIX + "/friends",
            lambda route: route.fulfill(
                json={
                    "friends": [
                        remote,
                        {"id": "local-rel", "nickname": "本服朋友", "status": "accepted"},
                    ]
                }
            ),
        )
        post = {
            "id": 7,
            "author": {"id": "peer", "nickname": "远方影友", "is_remote": True, "is_self": False},
            "media": {
                "tmdb_id": 1900,
                "kind": "movie",
                "title": "海上钢琴师",
                "poster_path": "/test.jpg",
            },
            "body": "跨服主动分享的推荐",
            "created_at": CREATED,
            "like_count": 0,
            "liked": False,
            "comment_count": 2,
            "comments": [],
            "is_remote": True,
        }

        def peer_route(route):
            request = route.request
            calls.append(
                (request.method, request.url, request.post_data_json if request.post_data else None)
            )
            if request.method == "PUT":
                route.fulfill(json={"liked": True, "like_count": 1})
            elif request.method == "DELETE":
                route.fulfill(json={"ok": True})
            elif request.method == "POST":
                assert request.post_data_json["request_id"]
                route.fulfill(
                    json={
                        "id": "r/9",
                        "author": {"id": "me", "nickname": "林栖", "is_self": True},
                        "body": request.post_data_json["body"],
                    }
                )
            elif "/comments" in request.url:
                route.fulfill(
                    json={
                        "comments": [
                            {
                                "id": "r/7",
                                "author": {"id": "me", "nickname": "林栖", "is_self": True},
                                "body": "我参与的评论",
                            }
                        ],
                        "next_before": "opaque/+= value" if "before=" not in request.url else None,
                    }
                )
            else:
                route.fulfill(json={"posts": [post], "next_before": None, "supported": True})

        page.route("**" + PREFIX + "/friends/rel-remote/feed**", peer_route)
        enter(page, origin, "alice")
        page.locator('[data-tab="privacy"]').click()
        page.locator("#share-posts-input").check()
        page.get_by_role("button", name="保存设置", exact=True).click()
        expect(page.locator("#toast")).to_contain_text("分享设置已保存")
        me = page.context.request.get(origin + PREFIX + "/me").json()["user"]
        assert (
            me["share_posts"] is True
            and me["share_current"] is False
            and me["share_history"] is False
        )
        page.locator('[data-tab="feed"]').click()
        page.locator("#feed-scope").select_option("friends")
        expect(page.locator("#feed-friend option")).to_have_count(1)
        expect(page.locator("#feed-posts")).to_contain_text("跨服主动分享的推荐")
        page.get_by_role("button", name="跨服动态范围说明", exact=True).click()
        expect(page.get_by_role("tooltip")).to_contain_text("双方可见")
        page.keyboard.press("Escape")
        page.get_by_role("button", name="点赞 0", exact=True).click()
        expect(page.get_by_role("button", name="已赞 1", exact=True)).to_be_visible()
        page.locator(".feed-menu>summary").click()
        expect(page.get_by_role("button", name="举报这条推荐", exact=True)).to_have_count(0)
        page.locator(".feed-discussion>summary").click()
        page.get_by_role("button", name="查看全部评论", exact=True).click()
        page.get_by_role("button", name="更早的评论", exact=True).click()
        expect(page.locator(".feed-comment")).to_have_count(2)
        page.get_by_role("button", name="删除评论", exact=True).first.click()
        expect(page.locator(".feed-comment")).to_have_count(1)
        page.locator(".feed-comment-form input").fill("跨服评论")
        page.get_by_role("button", name="发送评论", exact=True).click()
        expect(page.locator(".feed-comments")).to_contain_text("跨服评论")
        assert any(method == "DELETE" and "/comments/r%2F7" in url for method, url, _ in calls)
        assert any("before=opaque%2F%2B%3D%20value" in url for _, url, _ in calls)
        assert all("/friends/rel-remote/feed" in url for _, url, _ in calls)
        page.set_viewport_size({"width": 390, "height": 844})
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
