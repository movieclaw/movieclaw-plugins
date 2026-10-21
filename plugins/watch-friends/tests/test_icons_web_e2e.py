"""统一图标的离线渲染与交互：不以图片字体或远端 CDN 依赖代替真实 SVG。"""

from contextlib import ExitStack

import test_web_e2e as web
from test_feed_e2e import open_feed, use_cached_real_posters


def test_static_empty_icons_accessible_labels_and_real_svg(tmp_path):
    errors, requests = [], []
    with ExitStack() as stack:
        origin, host = stack.enter_context(web.native_at_port(tmp_path))
        (page,) = web.browser_pages(stack)
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("request", lambda request: requests.append(request.url))
        page.route(
            "**/api/v1/collections**",
            lambda route: route.fulfill(json={"success": True, "data": []}),
        )
        web.enter(page, origin, "alice")
        web.expect(page.locator("#refresh-button svg[data-icon=refresh-cw]")).to_have_count(1)
        page.locator('[data-tab="collections"]').click()
        empty = page.locator("#collection-list .empty-state")
        web.expect(empty).to_contain_text("把喜欢的电影，放进片单")
        web.expect(empty.locator('svg[data-icon="list-video"] path')).not_to_have_count(0)
        web.expect(empty.locator("svg")).to_have_attribute("aria-hidden", "true")
        assert "▤" not in empty.inner_text()
        web.expect(page.locator("#favorites-list svg[data-icon=heart]")).to_have_count(1)
        page.screenshot(path=str(tmp_path / "collection-empty-icons.png"))
        page.locator('[data-tab="friends"]').click()
        web.expect(page.locator("#friend-list svg[data-icon=users-round]")).to_have_count(1)
        web.expect(page.locator("#friend-detail svg[data-icon=clapperboard]")).to_have_count(1)
        page.locator('[data-tab="directory"]').click()
        web.expect(page.locator("#directory-list svg[data-icon=users-round]")).to_have_count(1)
        page.set_viewport_size({"width": 390, "height": 844})
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        assert not any("lucide" in url or "jsdelivr" in url or "unpkg" in url for url in requests)
        assert errors == []


def test_rating_and_reactions_keep_svg_after_state_changes(tmp_path):
    with ExitStack() as stack:
        origin, host = stack.enter_context(web.native_at_port(tmp_path, demo_enabled=True))
        use_cached_real_posters(host)
        (page,) = web.browser_pages(stack)
        web.enter(page, origin, "alice")
        open_feed(page)
        card = page.locator(".feed-card[data-demo=true]").first
        # 演示开关由账号自行开启，当前 fixture 默认显示。
        if card.count() == 0:
            page.locator('[data-tab="privacy"]').click()
            page.locator("#demo-toggle").check()
            open_feed(page)
        like = card.get_by_role("button", name="点赞 8", exact=True)
        web.expect(like.locator("svg[data-icon=heart]")).to_have_count(1)
        like.click()
        liked = card.get_by_role("button", name="已赞 9", exact=True)
        web.expect(liked).to_have_attribute("aria-pressed", "true")
        web.expect(liked.locator("svg.is-filled")).to_have_count(1)
        liked.click()
        web.expect(
            card.get_by_role("button", name="点赞 8", exact=True).locator("svg")
        ).to_have_count(1)
        web.expect(page.locator(".feed-rating-option svg[data-icon=star]")).to_have_count(5)
        page.screenshot(path=str(tmp_path / "feed-icons.png"))
