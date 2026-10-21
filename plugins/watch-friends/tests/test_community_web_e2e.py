"""社区的真实本机浏览器验收；管理员来自受控 require_login Principal 而非请求角色。"""

from contextlib import ExitStack
from types import SimpleNamespace

import pytest
import test_web_e2e as web
from fastapi import Request
from test_feed_e2e import feed_card, open_feed, plugin_request, refresh_feed


@pytest.fixture
def native_admin(monkeypatch):
    """只给本机 harness 增加正式 admin 身份，不修改产品或使用者凭据。"""
    build_routers = web.build_routers
    call = web.HostOps.call

    async def admin_history(self, operation, arguments):
        # 本机管理员没有伪造成员播放记录；后台读取不能抛出测试账号查找异常。
        if operation == "playback.history" and arguments.get("member_id") == 0:
            self.calls.append((operation, arguments.copy()))
            return {"entries": [], "next_cursor": None}
        return await call(self, operation, arguments)

    monkeypatch.setattr(web.HostOps, "call", admin_history)

    def with_admin(app, require_login):
        app.state.testing_admin = True

        def login(request: Request):
            if request.cookies.get(web.HOST_COOKIE) == "test-admin":
                return SimpleNamespace(
                    kind="admin", name="管理员", member_id=0, member=None, plugin=None
                )
            return require_login(request)

        return build_routers(app, login)

    monkeypatch.setattr(web, "build_routers", with_admin)


def enter_admin(page, origin):
    page.context.add_cookies([{"name": web.HOST_COOKIE, "value": "test-admin", "url": origin}])
    page.goto(origin + "/apps/watch-friends/")
    web.expect(page.locator("#workspace")).to_be_visible()
    web.expect(page.locator("#greeting")).to_contain_text("管理员")


def publish(page, origin, body):
    response = plugin_request(
        page,
        origin,
        "post",
        "/feed",
        {
            "media": {"tmdb_id": 157336, "kind": "movie", "title": "星际穿越", "year": 2014},
            "body": body,
            "publish_consent": True,
        },
    )
    assert response.status == 201
    return response.json()


def menu(page, post_id):
    card = feed_card(page, post_id)
    card.locator("summary[aria-label='推荐操作']").click()
    return card


def submit_report(page, reason):
    web.expect(page.locator("#report-dialog")).to_be_visible()
    page.locator("#report-reason").fill(reason)
    page.locator("#report-dialog").get_by_role("button", name="提交举报", exact=True).click()
    web.expect(page.locator("#report-dialog")).not_to_be_visible()


def test_community_profile_published_card_and_notification_destinations(tmp_path):
    with ExitStack() as stack:
        origin, _ = stack.enter_context(web.native_at_port(tmp_path / "plugin"))
        alice, bob = web.browser_pages(stack, 2)
        web.enter(alice, origin, "alice")
        web.enter(bob, origin, "bob")
        alice.locator('[data-tab="privacy"]').click()
        biography = '<img src=x onerror="window.__profileXss=1">喜欢科幻和音乐。'
        web.expect(alice.locator(".account-avatar-note")).to_contain_text("随主程序账号同步")
        assert alice.locator(".avatar-choice").count() == 0
        alice.locator("#profile-bio").fill(biography)
        alice.get_by_role("button", name="保存个人资料", exact=True).click()
        web.expect(alice.locator("#toast")).to_contain_text("个人资料已保存")
        alice.get_by_role("button", name="查看我的主页", exact=True).click()
        web.expect(alice.locator("#person-dialog")).to_contain_text(biography)
        web.expect(alice.locator("#person-dialog .profile-metrics strong")).to_have_count(0)
        alice.get_by_role("button", name="编辑个人资料", exact=True).click()
        web.expect(alice.locator("#person-dialog")).not_to_be_visible()
        web.expect(alice.locator("#panel-privacy")).to_be_visible()
        post = publish(alice, origin, "在电影里多认识一点彼此。")
        open_feed(bob)
        author = feed_card(bob, post["id"]).locator(".feed-author")
        web.expect(author.locator(".avatar")).to_contain_text("林")
        assert author.locator(".avatar img").count() == 0
        author.locator(".person-link").first.click()
        web.expect(bob.locator("#person-dialog")).to_contain_text(biography)
        web.expect(bob.locator("#person-dialog")).to_contain_text("公开推荐")
        web.expect(bob.locator("#person-dialog")).to_contain_text("在电影里多认识一点彼此。")
        web.expect(bob.locator("#person-dialog .profile-empty")).to_have_count(0)
        assert bob.locator("#person-dialog img[src=x]").count() == 0
        assert bob.evaluate("window.__profileXss") is None
        bob.locator("#person-dialog").get_by_role("button", name="关闭", exact=True).click()
        # 私人喜欢只有显式发布后才进入主页，资料编辑本身不改变广场授权。
        assert (
            plugin_request(
                alice,
                origin,
                "put",
                "/favorites",
                {
                    "items": [
                        {
                            "tmdb_id": 157336,
                            "kind": "movie",
                            "title": "星际穿越",
                            "year": 2014,
                        }
                    ]
                },
            ).status
            == 200
        )
        alice.reload()
        web.expect(alice.locator("#workspace")).to_be_visible()
        alice.locator('[data-tab="privacy"]').click()
        web.expect(alice.locator("#profile-bio")).to_have_value(biography)
        alice.locator("#discoverable-input").check()
        alice.locator("#share-favorites-input").check()
        alice.get_by_role("button", name="保存设置", exact=True).click()
        web.expect(alice.locator("#preview-dialog")).to_be_visible()
        alice.locator("#confirm-public").click()
        web.expect(alice.locator("#preview-dialog")).not_to_be_visible()
        author.locator(".person-link").first.click()
        web.expect(bob.locator("#person-dialog")).to_contain_text("林栖的影片1")
        web.expect(bob.locator("#person-dialog")).not_to_contain_text("林栖的影片6")
        web.expect(bob.locator("#person-dialog")).to_contain_text("公开的喜欢电影")
        web.expect(bob.locator("#person-dialog")).to_contain_text("星际穿越")
        bob.locator("#person-dialog").get_by_role("button", name="关闭", exact=True).click()
        feed_card(bob, post["id"]).get_by_role("button", name="点赞 0", exact=True).click()
        alice.locator("#notification-bell").click()
        notice = alice.locator(".notification-row", has_text="赞了你的推荐")
        web.expect(notice).to_be_visible()
        web.expect(alice.locator("#notification-count")).to_have_text("1")
        notice.click()
        web.expect(alice.locator("#notifications-dialog")).not_to_be_visible()
        web.expect(alice.locator("#panel-feed")).to_be_visible()
        web.expect(feed_card(alice, post["id"])).to_have_class("panel feed-card is-focused")
        assert (
            plugin_request(alice, origin, "get", "/community/notifications/unread").json()[
                "unread_count"
            ]
            == 0
        )
        bob.locator('[data-tab="directory"]').click()
        bob.locator(".directory-card", has_text="林栖").get_by_role(
            "button", name="申请添加好友", exact=True
        ).click()
        web.consent(bob)
        alice.locator("#notification-bell").click()
        alice.locator(".notification-row", has_text="申请添加你为好友").click()
        web.expect(alice.locator("#requests-panel")).to_have_attribute("open", "")
        web.expect(alice.locator("#incoming-requests")).to_contain_text("周末电影人")
        assert (
            plugin_request(alice, origin, "get", "/community/notifications/unread").json()[
                "unread_count"
            ]
            == 0
        )


def test_community_report_admin_hide_restore_and_user_block(native_admin, tmp_path):
    with ExitStack() as stack:
        origin, _ = stack.enter_context(web.native_at_port(tmp_path / "plugin"))
        alice, bob, admin = web.browser_pages(stack, 3)
        web.enter(alice, origin, "alice")
        web.enter(bob, origin, "bob")
        enter_admin(admin, origin)
        post = publish(alice, origin, "待审核的推荐正文，管理员应能核对上下文。")
        open_feed(bob)
        menu(bob, post["id"]).get_by_role("button", name="举报这条推荐", exact=True).click()
        submit_report(bob, "请核对是否适合公开展示。")
        bob.locator('[data-tab="privacy"]').click()
        assert bob.locator("#admin-report-panel").count() == 0
        assert plugin_request(bob, origin, "get", "/community/admin/reports").status == 403
        admin.locator('[data-tab="privacy"]').click()
        admin.locator("#admin-report-panel>summary").click()
        report = admin.locator(".report-card")
        web.expect(report).to_contain_text(post["body"])
        web.expect(report).to_contain_text("请核对是否适合公开展示")
        report.get_by_role("button", name="隐藏内容", exact=True).click()
        web.expect(admin.locator(".report-card")).to_have_count(0)
        open_feed(bob)
        web.expect(feed_card(bob, post["id"])).to_have_count(0)
        admin.locator("#admin-report-filter").select_option("resolved")
        web.expect(admin.locator(".report-card")).to_contain_text(post["body"])
        admin.locator(".report-card").get_by_role("button", name="恢复显示", exact=True).click()
        refresh_feed(bob)
        web.expect(feed_card(bob, post["id"])).to_be_visible()
        reviewed = plugin_request(
            admin, origin, "get", "/community/admin/reports?status=all"
        ).json()
        assert [item["action"] for item in reviewed["reports"][0]["audit"]] == ["hide", "unhide"]
        bob.on("dialog", lambda dialog: dialog.accept())
        menu(bob, post["id"]).get_by_role("button", name="屏蔽这位影友", exact=True).click()
        web.expect(feed_card(bob, post["id"])).to_have_count(0)
        assert len(plugin_request(bob, origin, "get", "/community/blocks").json()["blocks"]) == 1
        bob.locator('[data-tab="privacy"]').click()
        bob.locator("#community-settings .community-blocks>summary").click()
        bob.locator("#community-settings .community-blocks").get_by_role("button", name="取消屏蔽", exact=True).click()
        open_feed(bob)
        web.expect(feed_card(bob, post["id"])).to_be_visible()
        assert plugin_request(bob, origin, "get", "/community/blocks").json()["blocks"] == []


def test_community_comment_report_hides_comment_only(native_admin, tmp_path):
    with ExitStack() as stack:
        origin, _ = stack.enter_context(web.native_at_port(tmp_path / "plugin"))
        alice, bob, admin = web.browser_pages(stack, 3)
        web.enter(alice, origin, "alice")
        web.enter(bob, origin, "bob")
        enter_admin(admin, origin)
        post = publish(alice, origin, "这条推荐应在审核评论后继续显示。")
        response = plugin_request(
            bob, origin, "post", f"/feed/{post['id']}/comments", {"body": "需要单独审核的评论"}
        )
        assert response.status == 201
        open_feed(alice)
        card = feed_card(alice, post["id"])
        card.locator(".feed-discussion>summary").click()
        card.get_by_role("button", name="举报评论", exact=True).click()
        submit_report(alice, "仅举报这条评论，不应隐藏整条推荐。")
        admin.locator('[data-tab="privacy"]').click()
        admin.locator("#admin-report-panel>summary").click()
        web.expect(admin.locator(".report-card")).to_contain_text("需要单独审核的评论")
        admin.locator(".report-card").get_by_role("button", name="隐藏内容", exact=True).click()
        web.expect(admin.locator(".report-card")).to_have_count(0)
        refresh_feed(alice)
        web.expect(card).to_be_visible()
        card.locator(".feed-discussion>summary").click()
        web.expect(card.locator(".feed-comment")).to_have_count(0)
        assert len(plugin_request(alice, origin, "get", "/feed").json()["posts"]) == 1


def test_community_block_from_local_friend_profile_uses_member_identity(tmp_path):
    with ExitStack() as stack:
        origin, host = stack.enter_context(web.native_at_port(tmp_path / "plugin"))
        alice, bob = web.browser_pages(stack, 2)
        web.enter(alice, origin, "alice")
        web.enter(bob, origin, "bob")
        bob_user = plugin_request(bob, origin, "get", "/me").json()["user"]
        bob_id = bob_user["id"]
        request = plugin_request(
            alice,
            origin,
            "post",
            "/requests",
            {"invite_code": bob_user["invite_code"], "address_consent": True},
        )
        assert request.status == 200
        relation_id = request.json()["id"]
        assert (
            plugin_request(
                bob, origin, "post", f"/requests/{relation_id}/accept", {"address_consent": True}
            ).status
            == 200
        )
        alice.locator("#refresh-button").click()
        alice.locator(".friend-button", has_text="周末电影人").click()
        alice.locator("#friend-detail .detail-person-link").click()
        web.expect(alice.locator("#person-dialog")).to_contain_text("周末电影人")
        alice.on("dialog", lambda dialog: dialog.accept())
        alice.locator("#person-dialog").get_by_role(
            "button", name="屏蔽这位影友", exact=True
        ).click()
        web.expect(alice.locator("#person-dialog")).not_to_be_visible()
        web.expect(alice.locator(".friend-button", has_text="周末电影人")).to_have_count(0)
        blocked = plugin_request(alice, origin, "get", "/community/blocks").json()["blocks"]
        assert len(blocked) == 1 and blocked[0]["user"]["id"] == bob_id
        assert blocked[0]["user"]["is_remote"] is False
        assert (
            host.state.business.state.store.one(
                "SELECT status FROM relationships WHERE id=?", (relation_id,)
            )["status"]
            == "accepted"
        )
        alice.locator('[data-tab="privacy"]').click()
        alice.locator("#community-settings .community-blocks>summary").click()
        alice.locator("#community-settings .community-blocks").get_by_role(
            "button", name="取消屏蔽", exact=True
        ).click()
        alice.locator('[data-tab="friends"]').click()
        alice.locator("#refresh-button").click()
        web.expect(alice.locator(".friend-button", has_text="周末电影人")).to_be_visible()


def test_my_profile_shows_rating_only_recommendation_and_links_to_original(tmp_path):
    with ExitStack() as stack:
        origin, _ = stack.enter_context(web.native_at_port(tmp_path))
        (page,) = web.browser_pages(stack)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        web.enter(page, origin, "alice")
        response = plugin_request(
            page,
            origin,
            "post",
            "/feed",
            {
                "media": {"kind": "movie", "tmdb_id": 157336, "title": "星际穿越", "year": 2014},
                "rating": 4,
                "publish_consent": True,
            },
        )
        assert response.status == 201
        created = response.json()
        open_feed(page)
        page.locator("#feed-root").get_by_role("button", name="查看我的主页", exact=True).click()
        profile = page.locator("#person-dialog")
        web.expect(profile.get_by_role("heading", name="公开推荐", exact=False)).to_be_visible()
        web.expect(profile.locator(".profile-recommendation")).to_have_count(1)
        web.expect(profile.get_by_label("4 / 5 星", exact=True)).to_be_visible()
        web.expect(profile.locator(".profile-recommendation-body")).to_have_count(0)
        web.expect(profile.locator(".profile-empty")).to_have_count(0)
        web.expect(profile.get_by_role("button", name="星际穿越", exact=True)).to_be_visible()
        assert profile.locator(".profile-history-shelf,.profile-favorite-shelf").count() == 0
        page.screenshot(path=str(tmp_path / "profile-recommendations.png"))
        profile.get_by_role("button", name="查看动态与互动", exact=True).click()
        web.expect(profile).not_to_be_visible()
        web.expect(feed_card(page, created["id"])).to_have_class("panel feed-card is-focused")
        assert plugin_request(page, origin, "delete", f"/feed/{created['id']}").ok
        page.locator("#feed-root").get_by_role("button", name="查看我的主页", exact=True).click()
        web.expect(profile.locator(".profile-recommendation")).to_have_count(0)
        web.expect(profile).to_contain_text("暂无向你公开的内容")
        page.set_viewport_size({"width": 390, "height": 844})
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
        assert errors == []
