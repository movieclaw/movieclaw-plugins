"""联网广场页面真实浏览器验收：默认关闭、管理员连接、用户预览、手机和撤回。"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack

import test_web_e2e as web
from test_community_web_e2e import enter_admin
from test_community_web_e2e import native_admin as _native_admin
from test_feed_e2e import plugin_request

native_admin = _native_admin


def open_settings(page):
    page.locator('[data-tab="privacy"]').click()
    web.expect(page.locator("#network-settings .network-settings")).to_be_visible()


def admin_enable(page):
    open_settings(page)
    page.locator("#network-admin>summary").click()
    page.locator("#network-server-enabled").check()
    page.locator("#network-admin-consent").check()
    page.get_by_role("button", name="保存服务器互联设置", exact=True).click()
    web.expect(page.locator("#network-settings")).to_contain_text("互联已启用")


def test_network_ui_default_disabled_member_no_admin_and_mobile(tmp_path):
    with ExitStack() as stack:
        origin, _ = stack.enter_context(web.native_at_port(tmp_path / "local"))
        (page,) = web.browser_pages(stack, 1)
        web.enter(page, origin, "alice")
        open_settings(page)
        for name in ("browse", "publish", "forward", "favorites", "recommendations", "recent"):
            web.expect(page.locator("#network-" + name)).not_to_be_checked()
        web.expect(page.locator("#network-admin")).to_have_count(0)
        assert plugin_request(page, origin, "get", "/network/admin").status == 403
        page.locator('[data-tab="directory"]').click()
        page.get_by_role("button", name="联网广场", exact=True).click()
        web.expect(page.locator("#network-directory")).to_contain_text("管理员尚未开启")
        page.get_by_role("button", name="前往互联设置", exact=True).click()
        web.expect(page.locator("#panel-privacy")).to_be_visible()
        page.set_viewport_size({"width": 390, "height": 844})
        web.expect(page.locator("#network-browse")).to_be_visible()
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


def test_two_servers_admin_approval_preview_profile_and_withdraw(native_admin, tmp_path):
    with ExitStack() as stack:
        a_origin, a_host = stack.enter_context(web.native_at_port(tmp_path / "a"))
        b_origin, b_host = stack.enter_context(web.native_at_port(tmp_path / "b"))
        a, b = web.browser_pages(stack, 2)
        enter_admin(a, a_origin)
        enter_admin(b, b_origin)
        admin_enable(a)
        admin_enable(b)
        a.get_by_role("button", name="生成一次性连接码", exact=True).click()
        code = a.get_by_label("一次性服务器连接码").input_value()
        a.locator("#network-dialog").get_by_role("button", name="关闭", exact=True).click()
        b.get_by_label("互联服务器名称").fill("朋友的影厅")
        b.get_by_label("对方服务器连接码").fill(code)
        b.get_by_text("确认信任对方管理员，并允许双方交换网络地址", exact=True).click()
        b.get_by_role("button", name="申请连接", exact=True).click()
        web.expect(b.locator("#toast")).to_contain_text("等待对方管理员批准")
        a.get_by_role("button", name="刷新连接状态", exact=True).click()
        a.get_by_role("button", name="批准连接", exact=True).click()
        web.expect(a.locator(".network-peer")).to_contain_text("本服已批准")
        # 数据仅选择后进入联网预览，不触发本服公开/好友共享。
        favorite = {
            "kind": "movie",
            "tmdb_id": 1900,
            "title": "海上钢琴师",
            "year": 1998,
            "poster_path": "/search-poster.jpg",
        }
        assert plugin_request(a, a_origin, "put", "/favorites", {"items": [favorite]}).status == 200
        a.locator("#network-publish").check()
        a.locator("#network-favorites").check()
        a.locator("#network-forward").check()
        a.get_by_role("button", name="预览联网名片", exact=True).click()
        web.expect(a.locator("#network-dialog")).to_contain_text("海上钢琴师")
        web.expect(a.locator("#network-dialog")).to_contain_text("不是匿名服务")
        web.expect(
            a.locator("#network-dialog").get_by_role("button", name="确认发布", exact=True)
        ).to_be_disabled()
        a.locator("#network-public-consent").check()
        a.get_by_role("button", name="确认发布", exact=True).click()
        web.expect(a.locator("#network-dialog")).to_have_count(0)
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(asyncio.run, b_host.state.business.state.network.refresh()).result(
                timeout=15
            )
        web.enter(b, b_origin, "bob")
        open_settings(b)
        b.locator("#network-browse").check()
        web.expect(b.locator("#toast")).to_contain_text("浏览偏好已保存")
        b.locator('[data-tab="directory"]').click()
        b.get_by_role("button", name="联网广场", exact=True).click()
        web.expect(b.locator(".network-card")).to_contain_text("海上钢琴师")
        web.expect(b.locator(".network-card")).to_contain_text("来自直连服务器")
        assert a_origin not in b.locator("#network-directory").inner_text()
        b.locator(".network-card").get_by_role(
            "button", name="海上钢琴师，查看影片详情", exact=True
        ).click()
        web.expect(b.locator("#media-dialog")).to_be_visible()
        b.locator("#media-dialog").get_by_role("button", name="关闭影片详情", exact=True).click()
        b.set_viewport_size({"width": 390, "height": 844})
        web.expect(b.locator(".network-card")).to_be_visible()
        assert b.evaluate("document.documentElement.scrollWidth <= innerWidth")
        b.screenshot(path=str(tmp_path / "network-plaza-mobile.png"), full_page=True)
        b.set_viewport_size({"width": 1440, "height": 1000})
        b.screenshot(path=str(tmp_path / "network-plaza.png"), full_page=True)
        a.get_by_role("button", name="撤下联网名片", exact=True).click()
        web.expect(a.locator("#toast")).to_contain_text("已撤下")
        b.get_by_role("button", name="刷新联网名片", exact=True).click()
        web.expect(b.locator(".network-card")).to_have_count(0)
        assert plugin_request(a, a_origin, "get", "/me").json()["user"]["discoverable"] is False


def test_network_mutations_reject_stale_account(tmp_path):
    with ExitStack() as stack:
        origin, _ = stack.enter_context(web.native_at_port(tmp_path / "local"))
        (page,) = web.browser_pages(stack, 1)
        web.enter(page, origin, "alice")
        open_settings(page)
        page.context.add_cookies([{"name": web.HOST_COOKIE, "value": "bob", "url": origin}])
        page.locator("#network-browse").check()
        web.expect(page.locator("#toast")).to_contain_text("登录账号已改变")
        assert plugin_request(page, origin, "get", "/network/settings").json()["browse"] is False


def test_network_browser_removes_expired_cards(tmp_path):
    import time

    with ExitStack() as stack:
        origin, _ = stack.enter_context(web.native_at_port(tmp_path / "local"))
        (page,) = web.browser_pages(stack, 1)
        web.enter(page, origin, "alice")
        page.route(
            "**/network/settings",
            lambda route: route.fulfill(
                json={"server_enabled": True, "browse": True, "options": {}, "blocks": []}
            ),
        )
        calls = []

        def directory(route):
            calls.append(1)
            card = {
                "id": "network:test",
                "nickname": "短期名片",
                "bio": "",
                "avatar_data": None,
                "favorites": [],
                "recommendations": [],
                "history": [],
                "hops": 1,
                "expires_at": time.time() + 1,
            }
            route.fulfill(json={"users": [card] if len(calls) == 1 else []})

        page.route("**/network/directory", directory)
        page.locator('[data-tab="directory"]').click()
        page.get_by_role("button", name="联网广场", exact=True).click()
        web.expect(page.locator(".network-card")).to_contain_text("短期名片")
        web.expect(page.locator(".network-card")).to_have_count(0)
        assert len(calls) >= 2


def test_saved_server_state_and_own_card_visible_without_peers(native_admin, tmp_path):
    with ExitStack() as stack:
        origin, _ = stack.enter_context(web.native_at_port(tmp_path / "local"))
        (page,) = web.browser_pages(stack, 1)
        enter_admin(page, origin)
        admin_enable(page)
        web.expect(page.locator(".network-saved-state")).to_have_text("已保存 · 互联已启用")
        web.expect(page.locator("#network-admin-consent")).not_to_be_visible()
        page.reload()
        open_settings(page)
        page.locator("#network-admin>summary").click()
        web.expect(page.locator("#network-server-enabled")).to_be_checked()
        web.expect(page.locator(".network-saved-state")).to_have_text("已保存 · 互联已启用")
        page.get_by_role("button", name="保存服务器互联设置", exact=True).click()
        web.expect(page.locator("#toast")).to_contain_text("服务器互联设置已保存")
        movie = {
            "kind": "movie",
            "tmdb_id": 1900,
            "title": "海上钢琴师",
            "year": 1998,
            "poster_path": "/search-poster.jpg",
        }
        assert plugin_request(page, origin, "put", "/favorites", {"items": [movie]}).status == 200
        page.locator("#network-browse").check()
        page.locator("#network-publish").check()
        page.locator("#network-favorites").check()
        page.get_by_role("button", name="预览联网名片", exact=True).click()
        web.expect(page.locator(".network-movie-copy>.media-title")).to_have_text("海上钢琴师")
        page.locator("#network-public-consent").check()
        page.get_by_role("button", name="确认发布", exact=True).click()
        web.expect(page.locator("#network-dialog")).to_have_count(0)
        page.locator('[data-tab="directory"]').click()
        page.get_by_role("button", name="联网广场", exact=True).click()
        web.expect(page.locator(".network-card--self")).to_contain_text("我的联网名片 · 已发布")
        web.expect(page.locator(".network-card--self")).to_contain_text("海上钢琴师")
        web.expect(
            page.locator(".network-card--self").get_by_role("button", name="申请好友")
        ).to_have_count(0)
        web.expect(
            page.locator(".network-card--self").get_by_role("button", name="屏蔽影友")
        ).to_have_count(0)
        page.set_viewport_size({"width": 390, "height": 844})
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.locator(".network-card--self").get_by_role("button", name="编辑联网名片").click()
        page.get_by_role("button", name="撤下联网名片", exact=True).click()
        web.expect(page.locator("#toast")).to_contain_text("已撤下")
        page.locator('[data-tab="directory"]').click()
        page.get_by_role("button", name="联网广场", exact=True).click()
        web.expect(page.locator(".network-card--self")).to_have_count(0)


def test_public_discovery_ui_default_closed_save_and_reload(native_admin, tmp_path):
    """仅本机 harness 开关验收；没有公共入口时不伪造已联网状态。"""
    with ExitStack() as stack:
        origin, _ = stack.enter_context(web.native_at_port(tmp_path / "discovery"))
        (page,) = web.browser_pages(stack, 1)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        enter_admin(page, origin)
        admin_enable(page)
        panel = page.locator("#discovery-admin")
        web.expect(panel).to_be_visible()
        assert not panel.evaluate("element=>element.open")
        panel.locator("summary").click()
        web.expect(page.locator("#discovery-enabled")).not_to_be_checked()
        web.expect(page.locator("#discovery-serve")).not_to_be_checked()
        web.expect(
            page.get_by_role("button", name="查看本服公共节点接入码", exact=True)
        ).to_be_disabled()
        page.locator("#discovery-enabled").check()
        page.locator("#discovery-admin-consent").check()
        page.get_by_role("button", name="保存公共发现设置", exact=True).click()
        web.expect(page.locator("#discovery-admin")).to_contain_text("尚未接入公共入口")
        page.reload()
        open_settings(page)
        page.locator("#discovery-admin>summary").click()
        web.expect(page.locator("#discovery-enabled")).to_be_checked()
        web.expect(page.locator("#discovery-serve")).not_to_be_checked()
        web.expect(page.locator("#discovery-admin-consent")).not_to_be_checked()
        # 并发读取结束后仍保留展开状态和未保存草稿，确认框不恢复。
        page.locator("#discovery-serve").check()
        page.evaluate("window.MovieFriendsNetwork.settings()")
        assert page.locator("#discovery-admin").evaluate("element=>element.open")
        web.expect(page.locator("#discovery-serve")).to_be_checked()
        web.expect(page.locator("#discovery-admin-consent")).not_to_be_checked()
        page.locator("#network-publish").check()
        web.expect(page.locator("#network-discovery")).to_be_enabled()
        web.expect(page.locator("#network-discovery")).not_to_be_checked()
        page.set_viewport_size({"width": 390, "height": 844})
        assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")
        assert not errors


def test_published_network_card_is_rendered_in_local_square(native_admin, tmp_path):
    with ExitStack() as stack:
        origin, _ = stack.enter_context(web.native_at_port(tmp_path / "local-projection"))
        admin, viewer = web.browser_pages(stack, 2)
        enter_admin(admin, origin)
        admin_enable(admin)
        favorite = {
            "kind": "movie",
            "tmdb_id": 1900,
            "title": "海上钢琴师",
            "year": 1998,
            "poster_path": "/search-poster.jpg",
        }
        assert (
            plugin_request(admin, origin, "put", "/favorites", {"items": [favorite]}).status == 200
        )
        admin.locator("#network-publish").check()
        admin.locator("#network-favorites").check()
        admin.get_by_role("button", name="预览联网名片", exact=True).click()
        web.expect(admin.locator("#network-dialog")).to_contain_text("本服广场")
        admin.locator("#network-public-consent").check()
        admin.get_by_role("button", name="确认发布", exact=True).click()
        web.expect(admin.locator("#toast")).to_contain_text("联网名片已发布")
        web.enter(viewer, origin, "alice")
        viewer.locator('[data-tab="directory"]').click()
        local = viewer.locator("#directory-list .network-card")
        web.expect(local).to_have_count(1)
        web.expect(local).to_contain_text("海上钢琴师")
        web.expect(local.get_by_role("button", name="申请添加好友", exact=True)).to_be_visible()
        viewer.set_viewport_size({"width": 390, "height": 844})
        assert viewer.evaluate("document.documentElement.scrollWidth<=innerWidth")
