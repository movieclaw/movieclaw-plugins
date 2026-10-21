"""设置审查：草稿隔离、错误恢复、依赖禁用与移动端导航。"""

from contextlib import ExitStack

import test_web_e2e as web
from test_community_web_e2e import enter_admin
from test_community_web_e2e import native_admin as _native_admin
from test_feed_e2e import plugin_request
from test_network_web_e2e import admin_enable
from test_profile_web_e2e import profile_host as _profile_host

native_admin = _native_admin
profile_host = _profile_host


def settings(page):
    page.locator('[data-tab="privacy"]').click()
    web.expect(page.locator(".watch-resume-sync .settings-draft-bar")).to_have_count(1)


def test_settings_drafts_survive_remount_without_publishing_or_changing_server(
    tmp_path, profile_host
):
    with ExitStack() as stack:
        origin, host = stack.enter_context(web.native_at_port(tmp_path))
        (page,) = web.browser_pages(stack)
        web.enter(page, origin, "alice")
        settings(page)
        page.screenshot(path=str(tmp_path / "settings-audit-desktop.png"), full_page=False)
        before = plugin_request(page, origin, "get", "/me").json()["user"]
        page.locator("#nickname-input").fill("未保存的姓名")
        page.locator("#profile-bio").fill("未保存的简介")
        display = page.locator(".display-policy-form")
        display.get_by_label("不计入观看统计").uncheck()
        page.locator(".watch-auto-sync summary").click()
        page.locator(".watch-resume-sync summary").click()
        page.get_by_label("自动同步检查间隔", exact=True).select_option("30")
        page.get_by_label("续播检查间隔", exact=True).select_option("15")
        page.locator("#network-publish").check()
        page.locator("#network-forward").check()
        page.get_by_role("button", name="刷新设置", exact=True).click()
        web.expect(page.locator("#toast")).to_contain_text("设置状态已刷新")
        page.locator('[data-tab="profile"]').click()
        web.expect(page.locator("#profile-root .personal-hero")).to_be_visible()
        settings(page)
        web.expect(page.locator("#nickname-input")).to_have_value("未保存的姓名")
        web.expect(page.locator("#profile-bio")).to_have_value("未保存的简介")
        web.expect(display.get_by_label("不计入观看统计")).not_to_be_checked()
        web.expect(page.get_by_label("自动同步检查间隔", exact=True)).to_have_value("30")
        web.expect(page.get_by_label("续播检查间隔", exact=True)).to_have_value("15")
        web.expect(page.locator("#network-publish")).to_be_checked()
        web.expect(page.locator("#network-forward")).to_be_checked()
        for form in (
            "#privacy-form",
            "#community-profile-form",
            ".display-policy-form",
            ".watch-auto-sync form",
            ".watch-resume-sync form",
            ".network-publication",
        ):
            web.expect(page.locator(form).locator(".settings-draft-bar")).to_contain_text(
                "有更改待保存"
            )
        assert (
            plugin_request(page, origin, "get", "/me").json()["user"]["nickname"]
            == before["nickname"]
        )
        assert plugin_request(page, origin, "get", "/display-policy").json()["exclude_statistics"]
        assert not plugin_request(page, origin, "get", "/network/settings").json()["published"]
        assert not plugin_request(page, origin, "get", "/watch/auto-sync").json()["enabled"]
        assert not plugin_request(page, origin, "get", "/watch/resume-sync").json()["enabled"]
        page.locator(".network-publication").get_by_role("button", name="放弃更改").click()
        web.expect(page.locator("#network-publish")).not_to_be_checked()
        web.expect(page.locator("#network-forward")).to_be_disabled()
        web.expect(page.get_by_role("button", name="预览联网名片", exact=True)).to_be_disabled()
        assert host.state.marks == []


def test_failed_save_keeps_draft_and_saved_feedback_resets_after_success(tmp_path):
    with ExitStack() as stack:
        origin, _ = stack.enter_context(web.native_at_port(tmp_path))
        (page,) = web.browser_pages(stack)
        web.enter(page, origin, "alice")
        settings(page)
        panel = page.locator(".display-policy-form")
        panel.get_by_label("不计入观看统计").uncheck()

        def failure(route):
            if route.request.method == "PUT":
                route.fulfill(status=503, json={"detail": "受控保存失败"})
            else:
                route.continue_()

        page.route("**/display-policy", failure)
        panel.get_by_role("button", name="保存展示范围", exact=True).click()
        web.expect(page.locator("#toast")).to_contain_text("受控保存失败")
        web.expect(panel.locator(".settings-draft-bar")).to_contain_text("有更改待保存")
        page.locator('[data-tab="friends"]').click()
        settings(page)
        web.expect(panel.get_by_label("不计入观看统计")).not_to_be_checked()
        page.unroute("**/display-policy", failure)
        panel.get_by_role("button", name="保存展示范围", exact=True).click()
        web.expect(panel.locator(".settings-draft-bar")).to_have_text("已保存放弃更改")
        web.expect(panel.get_by_role("button", name="放弃更改")).not_to_be_visible()
        page.reload()
        settings(page)
        web.expect(panel.get_by_label("不计入观看统计")).not_to_be_checked()


def test_saving_admin_does_not_discard_publication_and_preview_consent_is_fresh(
    native_admin, tmp_path
):
    with ExitStack() as stack:
        origin, _ = stack.enter_context(web.native_at_port(tmp_path))
        (page,) = web.browser_pages(stack)
        enter_admin(page, origin)
        admin_enable(page)
        display = page.locator(".display-policy-form")
        display.get_by_label("不计入观看统计").uncheck()
        page.locator("#network-publish").check()
        page.locator("#network-forward").check()
        page.locator("#network-favorites").check()
        page.get_by_role("button", name="预览联网名片", exact=True).click()
        web.expect(page.locator("#toast")).to_contain_text("媒体库展示范围有未保存的更改")
        web.expect(page.locator("#network-dialog")).to_have_count(0)
        display.get_by_role("button", name="放弃更改", exact=True).click()
        page.get_by_role("button", name="保存服务器互联设置", exact=True).click()
        web.expect(page.locator("#toast")).to_contain_text("服务器互联设置已保存")
        web.expect(page.locator("#network-publish")).to_be_checked()
        web.expect(page.locator("#network-forward")).to_be_checked()
        web.expect(page.locator("#network-favorites")).to_be_checked()
        page.get_by_role("button", name="预览联网名片", exact=True).click()
        page.locator("#network-public-consent").check()
        page.locator("#network-dialog").get_by_role("button", name="关闭", exact=True).click()
        page.get_by_role("button", name="预览联网名片", exact=True).click()
        web.expect(page.locator("#network-public-consent")).not_to_be_checked()
        page.locator("#network-public-consent").check()
        page.get_by_role("button", name="确认发布", exact=True).click()
        web.expect(page.locator("#network-dialog")).to_have_count(0)
        web.expect(page.locator(".network-publication .settings-draft-bar")).to_contain_text(
            "已保存"
        )
        page.locator("#network-publish").uncheck()
        web.expect(page.get_by_role("button", name="保存并撤下名片", exact=True)).to_be_enabled()
        assert plugin_request(page, origin, "get", "/network/settings").json()["published"]
        page.get_by_role("button", name="保存并撤下名片", exact=True).click()
        web.expect(page.locator("#toast")).to_contain_text("已撤下")
        assert not plugin_request(page, origin, "get", "/network/settings").json()["published"]


def test_sync_scope_dependencies_and_mobile_tabs_have_no_overlapping_labels(tmp_path, profile_host):
    with ExitStack() as stack:
        origin, host = stack.enter_context(web.native_at_port(tmp_path))
        (page,) = web.browser_pages(stack)
        web.enter(page, origin, "alice")
        result = plugin_request(
            page,
            origin,
            "post",
            "/watch/connections",
            {"platform": "plex", "url": "https://external.test", "token": "personal-test-token"},
        )
        assert result.ok
        store = host.state.business.state.store
        source = store.one("SELECT id,user_id FROM watch_sources WHERE platform='plex'")
        store.display_policy.remember(
            source["user_id"],
            source["id"],
            [],
            [{"id": "1", "name": "电影库"}, {"id": "2", "name": "私人库"}],
        )
        settings(page)
        page.locator(".watch-auto-sync summary").click()
        card = page.locator(".auto-sync-connection").filter(has_text="Plex")
        web.expect(card.locator("select")).to_be_disabled()
        web.expect(card.get_by_label("电影库", exact=True)).to_be_disabled()
        card.locator("legend input").check()
        web.expect(card.locator("select")).to_be_enabled()
        web.expect(card.get_by_label("电影库", exact=True)).to_be_enabled()
        card.get_by_label("电影库", exact=True).check()
        card.locator("legend input").uncheck()
        web.expect(card.get_by_label("电影库", exact=True)).to_be_disabled()
        page.set_viewport_size({"width": 390, "height": 844})
        page.locator('[data-tab="friends"]').click()
        settings(page)
        page.screenshot(path=str(tmp_path / "settings-audit-mobile.png"), full_page=False)
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        assert page.locator(".tabs .tab").evaluate_all("""rows=>rows.every((n,i)=>{
            const r=n.getBoundingClientRect();
            return r.width>65&&(!i||r.left>=rows[i-1].getBoundingClientRect().right-1);
        })""")
        assert page.locator(".tab.is-active").evaluate("""n=>{
            const r=n.getBoundingClientRect(),p=n.parentElement.getBoundingClientRect();
            return r.left>=p.left && r.right<=p.right;
        }""")
