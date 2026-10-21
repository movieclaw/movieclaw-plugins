"""同步卡默认折叠、读取进度、刷新草稿和桌面/移动端对齐。"""

from contextlib import ExitStack

import test_web_e2e as web
from test_feed_e2e import plugin_request


def test_compact_sync_cards_keep_live_progress_and_configuration_drafts(tmp_path):
    with ExitStack() as stack:
        origin, _ = stack.enter_context(web.native_at_port(tmp_path))
        (page,) = web.browser_pages(stack)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        web.enter(page, origin, "alice")
        configs = {
            route: plugin_request(page, origin, "get", f"/watch/{route}").json()
            for route in ("auto-sync", "resume-sync")
        }
        run = {
            "id": "reading-run",
            "status": "running",
            "stage": "reading",
            "source_id": "movieclaw",
            "read_count": 1234,
            "total": 0,
            "processed": 0,
            "remaining": 0,
            "summary": {},
            "counts": [],
            "error": None,
            "created_at": "2026-10-21T02:27:45+00:00",
        }
        for kind, config in configs.items():
            config.update(enabled=True, runs=[run], error="上一轮的错误")
            page.route(
                f"**/watch/{kind}",
                lambda route, request, body=config: route.fulfill(json=body),
            )
        page.route("**/watch/auto-sync/runs/reading-run", lambda route: route.fulfill(json=run))
        page.route(
            "**/watch/auto-sync/runs/reading-run/targets?offset=0",
            lambda route: route.fulfill(json={"targets": [], "next_offset": None}),
        )
        page.locator('[data-tab="privacy"]').click()
        cards = page.locator(".watch-sync-cards")
        web.expect(cards.locator(".sync-card")).to_have_count(2)
        web.expect(cards.get_by_text("已处理", exact=False)).to_have_count(2)
        for card in cards.locator(".sync-card").all():
            web.expect(card.locator("details")).not_to_have_attribute("open", "")
            web.expect(card.locator("form")).not_to_be_visible()
            web.expect(card.locator("progress")).not_to_have_attribute("value", "0")
            web.expect(card.locator(".sync-progress-copy")).to_contain_text("已处理 1234 条")
            web.expect(card).not_to_contain_text("0 / 0")
            web.expect(card).not_to_contain_text("上一轮的错误")
        boxes = [card.bounding_box() for card in cards.locator(".sync-card").all()]
        assert abs(boxes[0]["y"] - boxes[1]["y"]) <= 1
        assert all(box["height"] < 340 for box in boxes)
        icons = [i.bounding_box() for i in cards.locator(".sync-card-icon").all()]
        titles = [i.bounding_box() for i in cards.locator("h3").all()]
        assert abs(icons[0]["y"] - icons[1]["y"]) <= 1
        assert abs(titles[0]["y"] - titles[1]["y"]) <= 1
        cards.screenshot(path="/tmp/yingji-sync-cards-desktop.png")
        auto = cards.locator(".watch-auto-sync")
        auto.get_by_role("button", name="查看明细", exact=True).click()
        detail = page.locator(".auto-sync-dialog")
        web.expect(detail).to_contain_text("已处理 1234 条")
        web.expect(detail).not_to_contain_text("0 / 0")
        detail.get_by_role("button", name="关闭", exact=True).click()
        auto.locator("summary").click()
        auto.get_by_label("自动同步检查间隔").select_option("30")
        auto.locator("summary").click()
        page.get_by_role("button", name="刷新设置", exact=True).click()
        web.expect(page.locator("#toast")).to_contain_text("设置状态已刷新")
        auto.locator("summary").click()
        web.expect(auto.get_by_label("自动同步检查间隔")).to_have_value("30")
        web.expect(auto.locator(".settings-draft-bar")).to_contain_text("有更改待保存")
        # 底部收起不提交表单，也不能丢失刚改的间隔草稿。
        auto.get_by_role("button", name="收起同步设置", exact=True).click()
        web.expect(auto.locator("form")).not_to_be_visible()
        auto.locator("summary").click()
        web.expect(auto.get_by_label("自动同步检查间隔")).to_have_value("30")
        interval = auto.get_by_label("自动同步检查间隔")
        assert interval.evaluate("e=>getComputedStyle(e).borderRadius") == "10px"
        interval.click()
        page.screenshot(path="/tmp/yingji-select-menu-desktop.png")
        interval.press("Escape")
        auto.get_by_role("button", name="收起同步设置", exact=True).click()
        # 一侧有错误、另一侧仍在运行，底部配置入口和操作区仍保持对齐。
        configs["auto-sync"]["runs"] = [
            {
                **run,
                "status": "paused",
                "stage": "finished",
                "error": "读取暂时失败，请在任务中心核对。",
            }
        ]
        page.get_by_role("button", name="刷新设置", exact=True).click()
        web.expect(auto).to_contain_text("读取暂时失败")
        all_cards = cards.locator(".sync-card").all()
        for selector in (
            ".sync-card-header",
            ".sync-state-line",
            ".sync-card-progress",
            ".sync-card-actions",
            ".sync-card-config",
        ):
            rows = [card.locator(selector).bounding_box() for card in all_cards]
            assert abs(rows[0]["y"] - rows[1]["y"]) <= 1, selector
        cards.screenshot(path="/tmp/yingji-sync-aligned-desktop.png")
        page.set_viewport_size({"width": 390, "height": 844})
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
        cards.screenshot(path="/tmp/yingji-sync-cards-mobile.png")
        assert errors == []
