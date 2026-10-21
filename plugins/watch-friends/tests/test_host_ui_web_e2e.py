"""服务端主题、统一弹窗骨架和三平台任务摘要的真实浏览器验收。"""

from contextlib import ExitStack

import test_web_e2e as web
from test_feed_e2e import plugin_request


def test_host_theme_report_close_geometry_and_mobile_overflow(tmp_path):
    with ExitStack() as stack:
        origin, _ = stack.enter_context(web.native_at_port(tmp_path))
        (page,) = web.browser_pages(stack)
        errors, writes = [], []
        page.on("pageerror", lambda e: errors.append(str(e)))
        web.enter(page, origin, "alice")
        # 只模拟宿主注入的主题属性，不改变用户偏好或同步配置。
        page.evaluate("document.documentElement.dataset.mcTheme='netflix'")
        config = plugin_request(page, origin, "get", "/watch/auto-sync").json()
        connections = [
            {"id": kind, "platform": kind, "account_name": "本人测试账号"}
            for kind in ("plex", "emby", "movieclaw")
        ]
        run = {
            "id": "visual-report",
            "status": "done",
            "stage": "finished",
            "created_at": "2026-10-21T04:51:00Z",
            "finished_at": "2026-10-21T04:55:00Z",
            "total": 0,
            "processed": 0,
            "remaining": 0,
            "read_count": 44032,
            "counts": [],
            "error": None,
            "summary": {
                kind: {"read": n}
                for kind, n in zip(
                    ("plex", "emby", "movieclaw"), (43927, 43763, 44032), strict=True
                )
            },
        }
        config.update(connections=connections)
        page.route("**/watch/auto-sync", lambda route: route.fulfill(json=config))
        page.route(
            "**/watch/resume-sync",
            lambda route: route.fulfill(
                json={
                    "enabled": False,
                    "sources": [],
                    "candidate_sources": [],
                    "runs": [run],
                    "interval_minutes": 5,
                }
            ),
        )
        page.route("**/watch/resume-sync/runs/visual-report", lambda route: route.fulfill(json=run))
        page.route(
            "**/watch/resume-sync/runs/visual-report/targets?offset=0",
            lambda route: route.fulfill(json={"targets": [], "next_offset": None}),
        )
        page.on(
            "request",
            lambda request: writes.append(request.url) if request.method != "GET" else None,
        )
        page.locator('[data-tab="privacy"]').click()
        page.get_by_role("button", name="查看续播明细", exact=True).click()
        modal = page.get_by_role("dialog", name="续播同步详情", exact=True)
        web.expect(modal).to_contain_text("核对完成 · 无需新增写入")
        web.expect(modal).not_to_contain_text("0 / 0")
        web.expect(modal.locator(".sync-platform-card")).to_have_count(3)
        for platform in ("Plex", "Emby", "MovieClaw"):
            web.expect(modal.get_by_role("heading", name=platform, exact=True)).to_be_visible()
        web.expect(modal).to_contain_text("43,927")
        web.expect(modal).not_to_contain_text("已有已看")  # 续播没有这个指标，不能借用已看口径。
        close = modal.get_by_role("button", name="关闭", exact=True)
        head = modal.locator(".ui-dialog-head")
        box, title, button = (x.bounding_box() for x in (modal, head.locator("h2"), close))
        assert title["x"] >= box["x"] + 18
        assert button["x"] > title["x"] + title["width"]
        assert abs(title["y"] + title["height"] / 2 - button["y"] - button["height"] / 2) < 1
        assert modal.evaluate("e=>getComputedStyle(e).borderRadius") == "6px"
        web.expect(close.locator("svg[data-icon=x]")).to_have_count(1)
        modal.screenshot(path="/tmp/yingji-host-report-desktop.png")
        page.set_viewport_size({"width": 390, "height": 844})
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
        assert modal.evaluate("e=>e.scrollWidth<=e.clientWidth+1")
        modal.screenshot(path="/tmp/yingji-host-report-mobile.png")
        page.set_viewport_size({"width": 390, "height": 600})
        footer = modal.locator(".ui-dialog-footer")
        panel_box, footer_box = modal.bounding_box(), footer.bounding_box()
        assert footer_box["y"] + footer_box["height"] <= panel_box["y"] + panel_box["height"]
        web.expect(footer.get_by_role("button", name="重试检查", exact=True)).to_be_in_viewport()
        # 很长的内容滚动时，标题与关闭入口仍留在面板内。
        modal.locator(".ui-dialog-body").evaluate("e=>e.scrollTop=e.scrollHeight")
        web.expect(close).to_be_in_viewport()
        close.press("Escape")
        web.expect(modal).not_to_be_visible()
        assert writes == [] and errors == []


def test_static_dialog_cancel_and_silver_theme_share_safe_skeleton(tmp_path):
    with ExitStack() as stack:
        origin, _ = stack.enter_context(web.native_at_port(tmp_path))
        (page,) = web.browser_pages(stack)
        web.enter(page, origin, "alice")
        # 复用实际已绑定的邀请确认弹窗，关闭要走 cancel，不能默认为同意。
        result = page.evaluate("""() => {
            const d=document.getElementById('address-dialog');let cancelled=0;
            d.addEventListener('cancel',()=>cancelled++);
            window.__visualCancelled=()=>cancelled;
            window.MovieFriendsUI.open(d);
            return {name:d.getAttribute('aria-labelledby'),radius:getComputedStyle(d).borderRadius};
        }""")
        assert result["name"] == "address-title" and result["radius"] == "16px"
        modal = page.locator("#address-dialog")
        modal.get_by_role("button", name="关闭", exact=True).click()
        web.expect(modal).not_to_be_visible()
        assert page.evaluate("window.__visualCancelled()") == 1
        page.evaluate("window.MovieFriendsUI.open(document.getElementById('address-dialog'))")
        web.expect(modal.locator(".ui-dialog-head")).to_have_count(1)
        modal.get_by_role("button", name="关闭", exact=True).click()
