"""个人主页及从导入到本人已看标记的真实浏览器验收。只使用本机测试账号。"""

import io
import re
from contextlib import ExitStack

import pytest
import test_web_e2e as web
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, Response
from PIL import Image
from test_watch_archive import fact


@pytest.fixture
def profile_host(monkeypatch):
    original = web.native_host

    def build(*args, **kwargs):
        host = original(*args, **kwargs)
        host.state.marks = []
        host.state.fail_episode_once = False
        host.state.with_avatar = True
        pictures = {}
        for user, color in (("alice", "#608fa9"), ("bob", "#8968a9")):
            stream = io.BytesIO()
            Image.new("RGB", (100, 100), color).save(stream, format="JPEG")
            pictures[user] = stream.getvalue()

        async def archive(_):
            return []

        async def external(platform, base, token, progress, **kwargs):
            assert token == "personal-test-token" and base == "https://external.test"
            progress(4)
            if getattr(host.state, "pending_series", False):
                pending = [fact(None, kind="tv", season=1, episode=i) for i in (1, 2)]
                for i, row in enumerate(pending):
                    row["external_id"] = str(i + 1)
                    row["media"].update(title="绝命毒师", year=2008)
                return {
                    "account_id": "personal",
                    "account_name": "我的外部账号",
                    "records": pending,
                }
            return {
                "account_id": "personal",
                "account_name": "我的外部账号",
                "records": [
                    fact(550, date="2026-10-01T12:00:00Z"),
                    fact(550, external="repeated-movie"),
                    fact(1396, kind="tv", season=1, episode=1),
                    fact(1396, kind="tv", season=1, episode=2),
                ],
            }

        host.state.business.state.upstream.archive = archive
        host.state.business.state.watch_archive.external.read = external

        @host.get("/api/v1/auth/me")
        async def me(request: Request):
            user = request.cookies.get(web.HOST_COOKIE)
            if user not in web.USERS:
                raise HTTPException(401)
            return {
                "success": True,
                "data": {
                    "username": user,
                    "nickname": web.USERS[user][1],
                    "avatar_url": "/api/v1/auth/avatar?v=test" if host.state.with_avatar else None,
                },
            }

        @host.get("/api/v1/auth/avatar")
        async def avatar(request: Request):
            return Response(pictures[request.cookies.get(web.HOST_COOKIE)], media_type="image/jpeg")

        @host.middleware("http")
        async def inventory(request, call_next):
            if request.url.path == "/api/v1/search/titles" and getattr(
                host.state, "pending_series", False
            ):
                payload = await request.json()
                assert payload["save_history"] is False
                return JSONResponse(
                    {
                        "success": True,
                        "data": {
                            "titles": [
                                {
                                    "media_type": "tv",
                                    "external_id": "1396",
                                    "title": "绝命毒师",
                                    "release_year": 2008,
                                }
                            ]
                        },
                    }
                )
            if request.url.path.startswith("/api/v1/discover/titles/"):
                key = request.url.path.split("/")[-1]
                kind, number = key.split(":")[1:]
                return JSONResponse(
                    {
                        "success": True,
                        "data": {
                            "title": {
                                "title_ref": key,
                                "provider": "tmdb",
                                "external_id": number,
                                "media_type": kind,
                                "title": "搏击俱乐部" if kind == "movie" else "绝命毒师",
                                "poster_url": "https://image.tmdb.org/t/p/w500/test-poster.jpg",
                                "library_status": {
                                    "media_item_id": int(number) + 100,
                                    "file_count": 2,
                                },
                            },
                            "metadata": {},
                            "library_links": [
                                {"library_id": 1, "media_item_id": int(number) + 100}
                            ],
                        },
                    }
                )
            return await call_next(request)

        @host.get("/api/v1/playback/marks")
        async def marks(request: Request):
            return {"success": True, "data": {"played": False, "is_favorite": True}}

        @host.post("/api/v1/playback/marks")
        async def mark(request: Request):
            payload = await request.json()
            assert request.cookies.get(web.HOST_COOKIE) == "alice"
            assert (
                "authorization" not in request.headers
                and "x-friends-request" not in request.headers
            )
            assert payload["played"] is True and "favorite" not in payload
            if host.state.fail_episode_once and payload.get("episode_number") == 2:
                host.state.fail_episode_once = False
                raise HTTPException(500, "受控失败")
            host.state.marks.append(payload)
            return {"success": True, "data": {"played": True, "is_favorite": True}}

        return host

    monkeypatch.setattr(web, "native_host", build)


def enter_profile(page, origin):
    page.context.add_cookies([{"name": web.HOST_COOKIE, "value": "alice", "url": origin}])
    page.goto(origin + "/apps/watch-friends/")
    web.expect(page.locator("#panel-profile")).to_be_visible()
    web.expect(page.locator(".personal-identity h1")).to_have_text("林栖")
    web.expect(page.locator(".personal-identity .avatar img")).to_have_count(1)
    web.expect(page.locator(".personal-identity")).to_contain_text("进入映迹")
    web.expect(page.locator(".personal-private")).to_have_count(0)
    web.expect(page.get_by_role("button", name="观看足迹说明", exact=True)).to_be_visible()


def import_source(page, platform="Emby"):
    page.locator('[data-tab="privacy"]').click()
    page.locator(f".watch-source-{platform.lower()}").get_by_role("button", name="连接账号").click()
    modal = page.locator(".watch-dialog").last
    modal.get_by_label("服务器地址", exact=True).fill("https://external.test")
    modal.get_by_label(
        "Emby 令牌／API 密钥" if platform == "Emby" else "个人令牌", exact=True
    ).fill("personal-test-token")
    if platform == "Emby":
        modal.get_by_label("Emby 用户名／用户 ID", exact=True).fill("my-account")
    modal.locator("input[type=checkbox]").check()
    modal.get_by_role("button", name="连接并生成预览").click()
    preview = page.locator(".watch-dialog").last
    web.expect(preview).to_contain_text("读取完成，等待确认")
    web.expect(preview).to_contain_text("3 个新增已看")
    assert page.locator(".personal-stat strong").all_text_contents() == ["0", "0", "0"]
    preview.get_by_role("button", name="确认导入私人档案", exact=True).click()
    web.expect(preview).to_contain_text("已导入私人档案")
    web.expect(page.locator(".personal-stat strong")).to_have_text(["1", "1", "2"])
    return preview


def test_profile_avatar_import_preview_and_exact_episode_writeback(tmp_path, profile_host):
    with ExitStack() as stack:
        origin, host = stack.enter_context(web.native_at_port(tmp_path))
        (page,) = web.browser_pages(stack)
        enter_profile(page, origin)
        preview = import_source(page)
        assert host.state.marks == []
        preview.get_by_role("button", name="预览主程序已看标记").click()
        write = page.locator(".watch-dialog").last
        web.expect(write).to_contain_text("可标记 3 项")
        web.expect(write.get_by_role("button", name="确认标为主程序已看")).to_be_disabled()
        assert host.state.marks == []
        host.state.fail_episode_once = True
        write.locator("input[type=checkbox]").check()
        write.get_by_role("button", name="确认标为主程序已看").click()
        web.expect(write).to_contain_text("失败 1 项")
        assert len(host.state.marks) == 2
        write.get_by_role("button", name="重试未完成项目").click()
        web.expect(write).to_contain_text("失败 0 项")
        assert len(host.state.marks) == 3
        assert host.state.marks[0] == {"media_item_id": 650, "played": True}
        assert {row["episode_number"] for row in host.state.marks if "episode_number" in row} == {
            1,
            2,
        }
        assert all(
            row.get("season_number") == 1 for row in host.state.marks if "episode_number" in row
        )
        assert "personal-test-token" not in page.locator("body").inner_text()
        write.get_by_role("button", name="关闭", exact=True).click()
        preview.get_by_role("button", name="关闭", exact=True).click()
        page.locator('[data-tab="profile"]').click()
        web.expect(page.locator(".personal-film")).to_have_count(2)
        assert page.locator(".personal-film .media-poster").count() == 2
        web.expect(page.locator(".series-progress-badge")).to_have_text("已看到第 1 季 · 第 2 集")


def test_account_switch_stops_old_preview_writeback_and_mobile_layout(tmp_path, profile_host):
    with ExitStack() as stack:
        origin, host = stack.enter_context(web.native_at_port(tmp_path))
        (page,) = web.browser_pages(stack)
        page.set_viewport_size({"width": 390, "height": 844})
        enter_profile(page, origin)
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        preview = import_source(page, "Plex")
        preview.get_by_role("button", name="预览主程序已看标记").click()
        write = page.locator(".watch-dialog").last
        web.expect(write).to_contain_text("可标记 3 项")
        page.context.add_cookies([{"name": web.HOST_COOKIE, "value": "bob", "url": origin}])
        write.locator("input[type=checkbox]").check()
        write.get_by_role("button", name="确认标为主程序已看").click()
        # 后台预览轮询也可能先发现账号切换；两条隔离路径都必须阻止任何写入。
        web.expect(page.locator("#toast")).to_contain_text(
            re.compile("登录账号已改变|没有这次本人的导入任务")
        )
        assert host.state.marks == []


def test_old_emby_connection_repair_uses_saved_key_and_explicit_account(tmp_path, profile_host):
    with ExitStack() as stack:
        origin, host = stack.enter_context(web.native_at_port(tmp_path))
        (page,) = web.browser_pages(stack)
        enter_profile(page, origin)
        store = host.state.business.state.store
        owner = store.one("SELECT id FROM users WHERE role='member'")["id"]
        store.execute(
            "INSERT INTO watch_sources(id,user_id,platform,origin,secret,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (
                "old-emby",
                owner,
                "emby",
                store.encrypt("https://external.test"),
                store.encrypt("personal-test-token"),
                "2026-10-20",
            ),
        )
        page.locator('[data-tab="friends"]').click()
        page.locator('[data-tab="privacy"]').click()
        page.get_by_role("button", name="指定 Emby 账号", exact=True).click()
        modal = page.locator(".watch-dialog").last
        assert modal.locator("input[type=password]").count() == 0
        modal.get_by_label("Emby 用户名／用户 ID", exact=True).fill("my-account")
        modal.locator("input[type=checkbox]").check()
        modal.get_by_role("button", name="绑定并重新预览").click()
        preview = page.locator(".watch-dialog").last
        web.expect(preview).to_contain_text("读取完成，等待确认")
        web.expect(preview).to_contain_text("3 个新增已看")
        assert (
            store.one("SELECT account_selector FROM watch_sources WHERE id='old-emby'")[
                "account_selector"
            ]
            == "my-account"
        )
        assert store.all("SELECT id FROM watch_facts") == []


def test_media_library_exclusion_ui_persistence_and_mobile_layout(tmp_path, profile_host):
    with ExitStack() as stack:
        origin, host = stack.enter_context(web.native_at_port(tmp_path))

        @host.get("/api/v1/libraries")
        async def libraries():
            return {
                "success": True,
                "data": [
                    {"id": 1, "name": "电影", "viewer_access": True},
                    {"id": 2, "name": "私人库", "viewer_access": True},
                ],
            }

        (page,) = web.browser_pages(stack)
        enter_profile(page, origin)
        preview = import_source(page, "Plex")
        preview.get_by_role("button", name="关闭", exact=True).click()
        page.locator('[data-tab="profile"]').click()
        store = host.state.business.state.store
        owner = store.one("SELECT id FROM users WHERE username='member:1:2026-10-01T00:00:00'")
        if owner is None:
            owner = store.one("SELECT id FROM users WHERE role<>'demo' ORDER BY rowid LIMIT 1")
        source = store.one("SELECT id FROM watch_sources WHERE user_id=?", (owner["id"],))
        records = [
            fact(550),
            fact(1396, kind="tv", season=1, episode=1),
            fact(1396, kind="tv", season=1, episode=2),
        ]
        for row in records:
            row["library_ids"] = ["2" if row["media"]["kind"] == "movie" else "1"]
        store.display_policy.remember(
            owner["id"],
            source["id"],
            records,
            [{"id": "1", "name": "电视剧"}, {"id": "2", "name": "私人库"}],
        )
        page.locator('[data-tab="privacy"]').click()
        panel = page.locator("#display-policy-settings")
        web.expect(panel).to_contain_text("未排除媒体库")
        group = panel.locator("fieldset").filter(has_text="Plex")
        group.get_by_label("私人库").check()
        panel.get_by_role("button", name="保存展示范围").click()
        web.expect(panel).to_contain_text("已排除 1 个媒体库")
        assert group.get_by_label("私人库").is_checked()
        page.locator('[data-tab="profile"]').click()
        web.expect(page.locator(".personal-stat strong")).to_have_text(["0", "1", "2"])
        web.expect(page.locator(".personal-film")).to_have_count(1)
        page.reload()
        page.locator('[data-tab="privacy"]').click()
        web.expect(
            panel.locator("fieldset").filter(has_text="Plex").get_by_label("私人库")
        ).to_be_checked()
        panel.get_by_label("不计入观看统计").uncheck()
        panel.get_by_role("button", name="保存展示范围").click()
        web.expect(panel.get_by_label("不计入观看统计")).not_to_be_checked()
        page.locator('[data-tab="profile"]').click()
        web.expect(page.locator(".personal-stat strong")).to_have_text(["1", "1", "2"])
        web.expect(page.locator(".personal-film")).to_have_count(1)
        page.get_by_role("button", name="观看统计说明", exact=True).click()
        web.expect(page.get_by_role("tooltip")).to_contain_text("统计仍包含这些记录")
        page.keyboard.press("Escape")
        page.locator('[data-tab="privacy"]').click()
        panel.get_by_label("从观看足迹中隐藏").uncheck()
        panel.get_by_label("不计入观看统计").check()
        panel.get_by_role("button", name="保存展示范围").click()
        web.expect(panel.get_by_label("从观看足迹中隐藏")).not_to_be_checked()
        page.locator('[data-tab="profile"]').click()
        web.expect(page.locator(".personal-stat strong")).to_have_text(["0", "1", "2"])
        web.expect(page.locator(".personal-film")).to_have_count(2)
        page.reload()
        page.locator('[data-tab="privacy"]').click()
        web.expect(panel.get_by_label("从观看足迹中隐藏")).not_to_be_checked()
        web.expect(panel.get_by_label("不计入观看统计")).to_be_checked()
        page.set_viewport_size({"width": 390, "height": 844})
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        assert "personal-test-token" not in panel.inner_text()


def test_preview_reads_confirmed_imports_and_local_lucide_navigation(tmp_path, profile_host):
    with ExitStack() as stack:
        origin, host = stack.enter_context(web.native_at_port(tmp_path))
        (page,) = web.browser_pages(stack)
        page.set_viewport_size({"width": 773, "height": 1000})
        enter_profile(page, origin)
        assert page.locator(".tabs").evaluate("nav => nav.scrollWidth <= nav.clientWidth")
        web.expect(page.locator(".tabs .nav-icon")).to_have_count(6)
        preview = import_source(page, "Plex")
        preview.get_by_role("button", name="关闭", exact=True).click()
        page.locator('[data-tab="profile"]').click()

        async def empty(*args):
            return {
                "current": None,
                "history": [],
                "sharing": {"current": False, "history": True},
                "observed_at": "2026-10-20T00:00:00Z",
            }

        host.state.business.state.upstream.snapshot = empty
        page.locator('[data-tab="privacy"]').click()
        page.locator("#discoverable-input").check()
        page.locator("#share-history-input").check()
        page.locator("#preview-button").click()
        dialog = page.locator("#preview-dialog")
        web.expect(dialog).to_be_visible()
        web.expect(dialog).to_contain_text("已合并 MovieClaw")
        web.expect(dialog).to_contain_text("作品 550")
        web.expect(dialog).to_contain_text("作品 1396")
        assert dialog.locator(".media-poster").count() >= 3
        assert (
            host.state.business.state.store.one(
                "SELECT COUNT(*) AS n FROM users WHERE share_imported_history=1"
            )["n"]
            == 0
        )
        assert page.context.request.get(origin + web.PREFIX + "/directory").json()["users"] == []
        page.set_viewport_size({"width": 390, "height": 844})
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        assert "personal-test-token" not in dialog.inner_text()


def test_completed_series_collapses_and_expands_without_changing_episode_stats(
    tmp_path, profile_host
):
    with ExitStack() as stack:
        origin, host = stack.enter_context(web.native_at_port(tmp_path))
        host.state.series_catalog["tmdb:tv:1396"] = {
            "status": "ready",
            "media": {"kind": "tv", "tmdb_id": 1396, "status": "Ended"},
            "seasons": [{"season_number": 1, "episode_count": 2, "aired_count": 2}],
        }
        (page,) = web.browser_pages(stack)
        enter_profile(page, origin)
        preview = import_source(page, "Plex")
        preview.get_by_role("button", name="关闭", exact=True).click()
        page.locator('[data-tab="profile"]').click()
        web.expect(page.locator(".series-complete-badge")).to_have_text("全剧已看完")
        web.expect(page.locator("#personal-history-grid .personal-film")).to_have_count(2)
        page.get_by_role("button", name="查看单集记录", exact=True).click()
        details = page.locator(".watch-dialog").last
        web.expect(details).to_contain_text("单集观看记录")
        web.expect(details.locator(".personal-film")).to_have_count(2)
        web.expect(details).to_contain_text("第 1 季 · 第 1 集")
        web.expect(details).to_contain_text("第 1 季 · 第 2 集")
        details.get_by_role("button", name="关闭", exact=True).click()
        web.expect(page.locator(".personal-stat strong")).to_have_text(["1", "1", "2"])
        assert (
            len(page.context.request.get(origin + web.PREFIX + "/watch/records").json()["records"])
            == 3
        )
        host.state.series_catalog["tmdb:tv:1396"]["seasons"][0]["episode_count"] = 3
        page.get_by_role("button", name="核对全集完成情况", exact=True).click()
        web.expect(page.locator("#personal-history-grid .personal-film")).to_have_count(2)
        web.expect(page.locator(".series-complete-badge")).to_have_count(0)
        web.expect(page.locator(".series-progress-badge")).to_have_text("已看到第 1 季 · 第 2 集")
        web.expect(page.locator("#personal-history-grid")).to_contain_text("累计已看 2 集")
        page.get_by_role("button", name="查看单集记录", exact=True).click()
        details = page.locator(".watch-dialog").last
        web.expect(details.locator(".personal-film")).to_have_count(2)
        details.get_by_role("button", name="关闭", exact=True).click()
        web.expect(page.locator(".personal-stat strong")).to_have_text(["1", "1", "2"])
        page.set_viewport_size({"width": 390, "height": 844})
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


@pytest.mark.parametrize("basis", ["catalog", "native", "platform"])
def test_returning_series_can_fold_and_generic_episode_titles_do_not_repeat(
    tmp_path, profile_host, monkeypatch, basis
):
    with ExitStack() as stack:
        origin, host = stack.enter_context(web.native_at_port(tmp_path))
        host.state.series_catalog["tmdb:tv:1396"] = {
            "status": "ready",
            "media": {"kind": "tv", "tmdb_id": 1396, "status": "Returning Series"},
            "seasons": [{"season_number": 1, "episode_count": 2 if basis == "catalog" else 3}],
        }
        if basis == "native":

            @host.get("/api/v1/libraries/1/items/1496")
            async def native_inventory():
                return {
                    "success": True,
                    "data": {
                        "kind": "tv",
                        "tmdb_id": 1396,
                        "files": [
                            {
                                "season_number": 1,
                                "episode_number": e,
                                "missing": False,
                                "state": "in_place",
                            }
                            for e in (1, 2)
                        ],
                    },
                }

        if basis == "platform":

            async def external_inventory(*args):
                return [(1, 1), (1, 2)]

            monkeypatch.setattr(
                "movieclaw_friends.series_footprints.read_catalog", external_inventory
            )
        original = host.state.business.state.watch_archive.external.read

        async def read(*args, **kwargs):
            result = await original(*args, **kwargs)
            for row in result["records"]:
                if row["media"]["kind"] == "tv":
                    row["media"]["episode_title"] = (
                        "第 1 集" if row["media"]["episode_number"] == 1 else "第二集"
                    )
            return result

        host.state.business.state.watch_archive.external.read = read
        (page,) = web.browser_pages(stack)
        enter_profile(page, origin)
        preview = import_source(page, "Plex")
        preview.get_by_role("button", name="关闭", exact=True).click()
        page.locator('[data-tab="profile"]').click()
        web.expect(page.locator(".series-complete-badge")).to_have_text(
            "目录内已看完" if basis == "catalog" else "片库内已看完"
        )
        page.get_by_role("button", name="查看单集记录", exact=True).click()
        details = page.locator(".watch-dialog").last
        web.expect(details.locator(".personal-film>.muted")).to_have_text(
            ["第 1 季 · 第 1 集", "第 1 季 · 第 2 集"]
        )


def test_pending_footprint_search_verify_and_batch_match(tmp_path, profile_host):
    with ExitStack() as stack:
        origin, host = stack.enter_context(web.native_at_port(tmp_path))
        host.state.pending_series = True
        (page,) = web.browser_pages(stack)
        enter_profile(page, origin)
        page.locator('[data-tab="privacy"]').click()
        page.locator(".watch-source-plex").get_by_role("button", name="连接账号").click()
        modal = page.locator(".watch-dialog").last
        modal.get_by_label("服务器地址", exact=True).fill("https://external.test")
        modal.get_by_label("个人令牌", exact=True).fill("personal-test-token")
        modal.locator("input[type=checkbox]").check()
        modal.get_by_role("button", name="连接并生成预览").click()
        preview = page.locator(".watch-dialog").last
        web.expect(preview).to_contain_text("2 条待匹配")
        preview.get_by_role("button", name="确认导入私人档案", exact=True).click()
        web.expect(preview).to_contain_text("已导入私人档案")
        preview.get_by_role("button", name="关闭", exact=True).click()
        page.locator('[data-tab="profile"]').click()
        card = page.locator(".personal-film").first
        web.expect(card).to_contain_text("待匹配")
        web.expect(card.locator(".media-poster img")).to_have_count(0)
        web.expect(page.locator("#personal-history-grid>.personal-film")).to_have_count(1)
        web.expect(card).to_contain_text("累计已看 2 集")
        web.expect(card).to_contain_text("第 2 集")
        card.get_by_role("button", name="查看单集记录").click()
        episodes = page.locator(".watch-dialog").last
        web.expect(episodes.locator(".series-episode-grid>.personal-film")).to_have_count(2)
        episodes.get_by_role("button", name="关闭", exact=True).click()
        card.get_by_role("button", name="校正匹配").click()
        match = page.locator(".watch-dialog").last
        match.get_by_role("button", name="搜索作品", exact=True).click()
        match.get_by_role("button", name="绝命毒师 · 2008 · TMDB 1396", exact=True).click()
        match.get_by_role("button", name="读取并核对影片", exact=True).click()
        web.expect(match.locator(".watch-match-info img")).to_have_count(1)
        assert page.locator(".personal-stat strong").all_text_contents() == ["0", "0", "0"]
        assert host.state.marks == []
        match.get_by_label("同时修正", exact=False).check()
        match.get_by_role("button", name="确认匹配这部作品", exact=True).click()
        web.expect(page.locator(".personal-stat strong")).to_have_text(["0", "1", "2"])
        web.expect(page.locator(".watch-pending")).to_have_count(0)
        web.expect(page.locator(".personal-film .media-poster img").first).to_be_visible()
        assert host.state.marks == []


def test_settings_owns_import_alignment_and_order_preserves_unsaved_sharing(tmp_path, profile_host):
    with ExitStack() as stack:
        origin, host = stack.enter_context(web.native_at_port(tmp_path))
        (page,) = web.browser_pages(stack)
        enter_profile(page, origin)
        web.expect(page.locator("#profile-root .personal-sources")).to_have_count(0)
        web.expect(page.locator("#profile-root")).not_to_contain_text("把观影记忆带回来")
        settings = page.get_by_role("button", name="设置", exact=True)
        settings.click()
        web.expect(page.locator("#privacy-title")).to_have_text("设置")
        assert page.locator("#profile-bio").evaluate(
            "node => node.getBoundingClientRect().width >= 300"
        )
        web.expect(page.locator("#watch-source-settings .watch-source")).to_have_count(2)
        web.expect(page.locator("#watch-source-settings")).to_contain_text("三平台已看对齐")
        assert page.locator(".settings-sections").evaluate(
            "node => [...node.children].map(child => child.id)"
        ) == [
            "community-settings",
            "display-policy-settings",
            "sharing-settings",
            "watch-source-settings",
            "network-settings",
            "demo-settings",
            "community-admin",
        ]
        # 进入、切换设置仅加载数据，不能替用户保存尚未确认的分享草稿。
        saved = page.context.request.get(origin + web.PREFIX + "/me").json()["user"]
        page.get_by_label("显示姓名（昵称亦可）", exact=True).fill("尚未保存的姓名")
        page.locator('[data-tab="profile"]').click()
        settings.click()
        web.expect(page.get_by_label("显示姓名（昵称亦可）", exact=True)).to_have_value(
            "尚未保存的姓名"
        )
        assert (
            page.context.request.get(origin + web.PREFIX + "/me").json()["user"]["nickname"]
            == saved["nickname"]
        )
        page.get_by_role("button", name="预览三平台已看对齐", exact=True).click()
        web.expect(page.locator(".alignment-dialog")).to_be_visible()
        page.locator(".alignment-dialog").get_by_role("button", name="关闭", exact=True).click()
        page.set_viewport_size({"width": 390, "height": 844})
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        assert page.locator("#profile-bio").evaluate(
            "node => node.getBoundingClientRect().width >= 250"
        )
        assert host.state.marks == []


def test_information_tooltips_hover_keyboard_touch_and_hidden_banners(tmp_path, profile_host):
    with ExitStack() as stack:
        origin, host = stack.enter_context(web.native_at_port(tmp_path))
        (page,) = web.browser_pages(stack)
        enter_profile(page, origin)
        root = page.locator("#profile-root")
        assert "按观看时间合并排列" not in root.inner_text()
        assert "主程序上次同步" not in root.inner_text()
        info = page.get_by_role("button", name="观看足迹说明", exact=True)
        tip = page.locator("#" + info.get_attribute("aria-describedby"))
        web.expect(tip).not_to_be_visible()
        info.hover()
        web.expect(tip).to_be_visible()
        web.expect(tip).to_contain_text("平台标记表示记录来源")
        page.mouse.move(0, 0)
        web.expect(tip).not_to_be_visible()
        info.focus()
        # 自动读取完成后主页会重绘，按当前按钮重新解析提示，避免引用已移除的旧ID。
        tip = page.locator("#" + info.get_attribute("aria-describedby"))
        web.expect(tip).to_be_visible()
        page.keyboard.press("Escape")
        web.expect(tip).not_to_be_visible()
        page.locator('[data-tab="privacy"]').click()
        web.expect(page.locator("#privacy-title")).to_contain_text("设置")
        scope = page.get_by_role("button", name="媒体库展示范围说明", exact=True)
        scope_tip = page.locator("#" + scope.get_attribute("aria-describedby"))
        page.set_viewport_size({"width": 390, "height": 844})
        scope.click()
        web.expect(scope_tip).to_be_visible()
        assert scope_tip.evaluate(
            "node => { const r=node.getBoundingClientRect();"
            "return r.left>=0&&r.right<=innerWidth&&r.top>=0&&r.bottom<=innerHeight; }"
        )
        assert "原始私人记录不会删除" not in page.locator("#watch-source-settings").inner_text()
        page.get_by_role("button", name="演示内容说明", exact=True).click()
        web.expect(scope_tip).not_to_be_visible()
        shown = page.locator(".info-help-popover:visible")
        web.expect(shown).to_have_count(1)
        web.expect(shown).to_contain_text("修改后自动保存")
        page.keyboard.press("Escape")
        web.expect(shown).to_have_count(0)
        assert not host.state.marks


def test_task_center_import_details_filters_and_frozen_history(tmp_path, profile_host):
    with ExitStack() as stack:
        origin, host = stack.enter_context(web.native_at_port(tmp_path))
        (page,) = web.browser_pages(stack)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        enter_profile(page, origin)
        preview = import_source(page)
        web.expect(preview).to_contain_text("新增原始记录 4 · 更新原始记录 0")
        preview.get_by_label("导入变化类型").select_option("new")
        web.expect(preview.locator(".personal-film")).to_have_count(4)
        preview.get_by_label("搜索导入记录").fill("1396")
        web.expect(preview.locator(".personal-film")).to_have_count(2)
        preview.get_by_role("button", name="关闭", exact=True).click()
        page.get_by_role("button", name="任务中心", exact=True).click()
        center = page.locator(".task-center-dialog")
        center.get_by_label("任务类型").select_option("import")
        task = center.locator(".task-card").filter(has_text="Emby").first
        web.expect(task).to_contain_text("新增去重已看 3")
        task.get_by_role("button", name="查看导入明细", exact=True).click()
        detail = page.locator(".watch-dialog").last
        web.expect(detail).to_contain_text("3 个新增已看")
        web.expect(detail.get_by_role("button", name="确认导入私人档案")).to_have_count(0)
        page.set_viewport_size({"width": 320, "height": 740})
        assert detail.evaluate("node => node.scrollWidth <= node.clientWidth")
        detail.get_by_role("button", name="关闭", exact=True).click()
        assert center.evaluate("node => node.scrollWidth <= node.clientWidth")
        assert not errors, errors


def test_task_center_collapses_identical_native_checks_and_reload_reuses_recent_read(
    tmp_path, profile_host
):
    from test_feed_e2e import plugin_request

    with ExitStack() as stack:
        origin, host = stack.enter_context(web.native_at_port(tmp_path))
        (page,) = web.browser_pages(stack)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        enter_profile(page, origin)
        for _ in range(4):
            assert plugin_request(page, origin, "post", "/watch/refresh").status in {200, 202}
        page.locator('[data-tab="privacy"]').click()
        page.get_by_role("button", name="任务中心", exact=True).click()
        center = page.locator(".task-center-dialog")
        web.expect(center.locator(".task-card")).to_have_count(2)
        web.expect(center).to_contain_text("已合并")
        web.expect(center).to_contain_text("内容无变化")
        count = host.state.business.state.store.one("SELECT COUNT(*) AS n FROM watch_jobs")["n"]
        center.get_by_label("展开重复核对").check()
        web.expect(center.locator(".task-card")).to_have_count(count)
        center.get_by_label("展开重复核对").uncheck()
        web.expect(center.locator(".task-card")).to_have_count(2)
        page.set_viewport_size({"width": 320, "height": 740})
        assert center.evaluate("node => node.scrollWidth <= node.clientWidth")
        page.reload()
        enter_profile(page, origin)
        page.locator('[data-tab="privacy"]').click()
        page.get_by_role("button", name="任务中心", exact=True).click()
        web.expect(page.locator(".task-center-dialog .task-card")).to_have_count(2)
        assert (
            host.state.business.state.store.one("SELECT COUNT(*) AS n FROM watch_jobs")["n"]
            == count
        )
        assert not errors, errors
