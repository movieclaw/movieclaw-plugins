"""任务中心显示阶段、平台和计数，并在页面内控制本人任务。"""

from contextlib import ExitStack

import test_web_e2e as web
from test_feed_e2e import plugin_request


def test_live_task_center_stop_retry_and_pause_resume_controls(tmp_path):
    with ExitStack() as stack:
        origin, host = stack.enter_context(web.native_at_port(tmp_path))
        (page,) = web.browser_pages(stack)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        web.enter(page, origin, "alice")

        async def external(platform, base, token, progress, **kwargs):
            progress(1)
            return {
                "account_id": "personal",
                "account_name": "本人",
                "libraries": [{"id": "1", "name": "电影库"}],
                "records": [],
            }

        host.state.business.state.watch_archive.external.read = external
        for platform in ("plex", "emby"):
            added = plugin_request(
                page,
                origin,
                "post",
                "/watch/connections",
                {
                    "platform": platform,
                    "url": "https://external.test",
                    "token": "test-token",
                    "emby_user": "alice",
                },
            )
            assert added.ok
        connections = plugin_request(page, origin, "get", "/watch/connections").json()
        source = next(c["id"] for c in connections["connections"] if c["platform"] == "plex")
        store = host.state.business.state.store
        owner = store.one("SELECT user_id FROM watch_sources WHERE id=?", (source,))["user_id"]
        for connection in connections["connections"]:
            store.display_policy.catalog(owner, connection["id"], [{"id": "1", "name": "电影库"}])
        saved = plugin_request(
            page,
            origin,
            "put",
            "/watch/auto-sync",
            {
                "enabled": True,
                "consent": True,
                "sources": [
                    {"source_id": c["id"], "direction": "both", "library_ids": ["1"]}
                    for c in connections["connections"]
                ],
            },
        )
        assert saved.ok, saved.text()
        store.execute("UPDATE auto_sync_settings SET next_at='2099-01-01T00:00:00+00:00'")
        store.execute(
            "INSERT INTO auto_sync_runs(id,user_id,status,stage,source_id,read_count,created_at) "
            "VALUES('control-ui',?,'running','reading',?,123,'2026-10-21T04:30:00+00:00')",
            (owner, source),
        )
        page.locator('[data-tab="privacy"]').click()
        card = page.locator(".watch-auto-sync")
        web.expect(card).to_contain_text("已处理 123 条")
        page.get_by_role("button", name="任务中心", exact=True).click()
        modal = page.locator(".task-center-dialog")
        task = modal.locator(".task-card").filter(has_text="后台自动同步")
        web.expect(modal.locator(".task-card").first).to_contain_text("后台自动同步")
        web.expect(task).to_contain_text("正在读取 Plex")
        web.expect(task).to_contain_text("已处理 123 条")
        web.expect(task).not_to_contain_text("0 / 0")
        modal.screenshot(path="/tmp/yingji-task-control-desktop.png")
        task.get_by_role("button", name="停止本轮", exact=True).click()
        web.expect(task).to_contain_text("已停止")
        view = plugin_request(page, origin, "get", "/watch/auto-sync").json()
        assert view["enabled"] and view["sources"] == saved.json()["sources"]
        task.get_by_role("button", name="重试检查", exact=True).click()
        web.expect(modal.locator(".task-card").filter(has_text="已完成")).to_have_count(1)
        page.set_viewport_size({"width": 390, "height": 844})
        modal.screenshot(path="/tmp/yingji-task-control-mobile.png")
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
        modal.get_by_role("button", name="关闭", exact=True).click()
        card.get_by_role("button", name="暂停同步", exact=True).click()
        web.expect(card.get_by_role("button", name="恢复同步", exact=True)).to_be_visible()
        assert not plugin_request(page, origin, "get", "/watch/auto-sync").json()["enabled"]
        card.get_by_role("button", name="恢复同步", exact=True).click()
        web.expect(card.get_by_text("已开启", exact=True)).to_be_visible()
        assert (
            plugin_request(page, origin, "get", "/watch/auto-sync").json()["sources"]
            == saved.json()["sources"]
        )
        assert not errors
