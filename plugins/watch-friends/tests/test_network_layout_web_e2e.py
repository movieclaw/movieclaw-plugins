"""联网名片层级与启用来源分栏的浏览器回归。"""

import time
from contextlib import ExitStack

import test_web_e2e as web


def test_network_card_metadata_has_separate_title_rating_review_and_date(tmp_path):
    with ExitStack() as stack:
        origin, _ = stack.enter_context(web.native_at_port(tmp_path))
        (page,) = web.browser_pages(stack)
        page.route(
            "**/network/settings",
            lambda route: route.fulfill(
                json={"server_enabled": True, "browse": True, "options": {}, "blocks": []}
            ),
        )
        movie = {
            "kind": "movie",
            "tmdb_id": 550,
            "title": "搏击俱乐部",
            "year": 1999,
            "poster_path": "/test-poster.jpg",
        }
        page.route(
            "**/network/directory",
            lambda route: route.fulfill(
                json={
                    "users": [
                        {
                            "id": "network:test",
                            "nickname": "林栖",
                            "bio": "",
                            "avatar_data": None,
                            "is_self": True,
                            "expires_at": time.time() + 300,
                            "favorites": [],
                            "recommendations": [
                                {"media": movie, "rating": 3, "body": "值得再看一次。"}
                            ],
                            "history": [
                                {
                                    "media": {
                                        **movie,
                                        "title": "一个很长的片名，用于验证名称与时间不会混在一行",
                                    },
                                    "ended_at": "2026-10-20T19:38:00Z",
                                }
                            ],
                        }
                    ]
                }
            ),
        )
        web.enter(page, origin, "alice")
        page.locator('[data-tab="directory"]').click()
        page.get_by_role("button", name="联网广场", exact=True).click()
        web.expect(page.locator(".network-movie-rating")).to_have_attribute(
            "aria-label", "3 / 5 星"
        )
        web.expect(page.locator(".network-movie-review")).to_have_text("值得再看一次。")
        web.expect(page.locator(".network-movie-date")).to_contain_text("观看于")
        for width in (1440, 390):
            page.set_viewport_size({"width": width, "height": 1000})
            web.expect(page.locator(".network-movie-copy")).to_have_count(2)
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            assert page.locator(".network-movie-copy").evaluate_all("""rows => rows.every(row => {
              const parts=[...row.children].map(n=>n.getBoundingClientRect());
              return parts.every((r,i)=>!i || r.top>=parts[i-1].bottom-1);
            })""")
        page.screenshot(path=str(tmp_path / "network-card-mobile.png"), full_page=True)


def test_display_columns_follow_active_sources_and_preserve_disconnected_exclusions(tmp_path):
    with ExitStack() as stack:
        origin, _ = stack.enter_context(web.native_at_port(tmp_path))
        (page,) = web.browser_pages(stack)
        groups = [
            {
                "source_id": p,
                "platform": p,
                "active": True,
                "libraries": [{"id": "1", "name": p + "电影", "excluded": p == "emby"}],
            }
            for p in ("emby", "plex", "movieclaw")
        ]
        saved = []

        def policy(route):
            if route.request.method == "PUT":
                saved.append(route.request.post_data_json)
            route.fulfill(json={"groups": groups, "hide_private": True, "exclude_statistics": True})

        page.route("**/display-policy", policy)
        web.enter(page, origin, "alice")
        page.locator('[data-tab="privacy"]').click()
        grid = page.locator(".display-source-grid")
        for count in (3, 2, 1):
            if count < 3:
                groups[3 - count - 1]["active"] = False
                page.locator('[data-tab="profile"]').click()
                page.locator('[data-tab="privacy"]').click()
            web.expect(grid.locator("fieldset")).to_have_count(count)
            platforms = grid.locator("fieldset").evaluate_all(
                "rows=>rows.map(n=>n.dataset.platform)"
            )
            assert platforms == ["movieclaw", "plex", "emby"][:count]
            assert (
                grid.evaluate("n=>getComputedStyle(n).gridTemplateColumns.split(' ').length")
                == count
            )
            if count == 3:
                positions = grid.locator("fieldset").evaluate_all(
                    "rows=>rows.map(n=>Math.round(n.getBoundingClientRect().top))"
                )
                assert len(set(positions)) == 1
        page.get_by_role("button", name="保存展示范围", exact=True).click()
        web.expect(page.locator("#toast")).to_contain_text("展示范围已保存")
        assert {"source_id": "emby", "library_id": "1"} in saved[-1]["excluded"]
        page.set_viewport_size({"width": 390, "height": 844})
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
