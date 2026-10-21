"""新增设置、统一任务分页和移动端：真实浏览器确认开关默认关闭且不触发写入。"""

from contextlib import ExitStack

import test_web_e2e as web
from test_feed_e2e import plugin_request


def test_resume_scope_default_and_all_history_ui(tmp_path):
    with ExitStack() as stack:
        origin, host = stack.enter_context(web.native_at_port(tmp_path / "completion"))
        (page,) = web.browser_pages(stack, 1)
        web.enter(page, origin, "alice")
        page.locator('[data-tab="privacy"]').click()
        section = page.locator(".watch-resume-sync")
        web.expect(section.get_by_role("heading", name="跨平台续播", exact=False)).to_be_visible()
        section.locator("summary").click()
        web.expect(section.get_by_label("开启本人续播同步")).not_to_be_checked()
        web.expect(section).to_contain_text("请先在上方保存同步连接")
        auto = page.locator(".watch-auto-sync")
        auto.locator("summary").click()
        web.expect(
            auto.get_by_role("button", name="配对 MovieClaw 本人账号", exact=True)
        ).to_be_visible()
        web.expect(auto.get_by_role("button", name="全部同步历史", exact=True)).to_be_visible()
        user = plugin_request(page, origin, "get", "/me").json()["user"]
        business = host.state.business
        for i in range(24):
            business.state.store.execute(
                "INSERT INTO auto_sync_runs(id,user_id,status,stage,total,processed,created_at) "
                "VALUES(?,?,'done','finished',1,1,?)",
                (f"web-run-{i:02}", user["id"], f"2026-10-21T00:{i:02}:00+00:00"),
            )
        auto.get_by_role("button", name="全部同步历史", exact=True).click()
        dialog = page.locator(".task-center-dialog")
        dialog.get_by_label("任务类型").select_option("auto_sync")
        web.expect(dialog.locator(".task-card")).to_have_count(20)
        dialog.get_by_role("button", name="更多历史任务", exact=True).click()
        web.expect(dialog.locator(".task-card")).to_have_count(24)
        dialog.locator(".task-card").first.get_by_role("button", name="查看后台同步详情").click()
        detail = page.locator(".auto-sync-dialog")
        web.expect(detail).to_contain_text("已处理 1 / 1 · 剩余 0")
        detail.get_by_role("button", name="关闭", exact=True).click()
        dialog.get_by_role("button", name="关闭", exact=True).click()
        page.set_viewport_size({"width": 390, "height": 844})
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth+1")
        page.screenshot(path=str(tmp_path / "settings-completion-mobile.png"), full_page=True)
        assert not business.state.store.one("SELECT * FROM resume_settings")
        assert not business.state.store.one("SELECT * FROM native_sync_grants")
