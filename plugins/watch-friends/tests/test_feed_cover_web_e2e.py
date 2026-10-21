"""推荐封面：真实页面选择、键盘导航、持久化与图片故障降级。"""

from contextlib import ExitStack
from pathlib import Path

from movieclaw_friends.demo_catalog import FILMS
from test_community_web_e2e import native_admin as native_admin
from test_feed_e2e import (
    assert_only_local_native_requests,
    feed_card,
    open_feed,
    plugin_request,
    record_browser,
    refresh_feed,
    use_cached_real_posters,
)
from test_web_e2e import browser_pages, enter, expect, native_at_port


def search_interstellar(page):
    page.route(
        "**/api/v1/search/titles",
        lambda route: route.fulfill(
            json={
                "success": True,
                "data": {
                    "titles": [
                        {
                            "provider": "tmdb",
                            "external_id": "157336",
                            "media_type": "movie",
                            "title": "星际穿越",
                            "release_year": 2014,
                            "poster_url": "https://image.tmdb.org/t/p/w500"
                            + FILMS[0]["poster_path"],
                        }
                    ]
                },
            }
        ),
    )
    open_feed(page)
    page.locator(".feed-compose>summary").click()
    page.locator("#feed-search").fill("星际穿越")
    page.get_by_role("button", name="搜索影片", exact=True).click()
    page.locator("#feed-search-results .feed-search-choice").click()


def test_choose_real_cover_preview_publish_and_refresh(tmp_path):
    with ExitStack() as stack:
        origin, host = stack.enter_context(native_at_port(tmp_path))
        alice, bob = browser_pages(stack, 2)
        errors, requests = [], []
        record_browser(alice, errors, requests)
        use_cached_real_posters(host)
        background = Path("/tmp/watch-friends-real-backdrops/157336.jpg")
        if background.exists():
            for path in [f"/cover-{i}.jpg" for i in range(1, 12)] + [FILMS[0]["poster_path"]]:
                for size in ("w500", "w780", "original"):
                    host.state.poster_images[f"https://image.tmdb.org/t/p/{size}{path}"] = (
                        background.read_bytes()
                    )
        enter(alice, origin, "alice")
        enter(bob, origin, "bob")

        def pictures(route):
            payload = route.fetch().json()
            payload["data"]["title"].update(
                title="星际穿越",
                poster_url="https://image.tmdb.org/t/p/w500" + FILMS[0]["poster_path"],
            )
            payload["data"].update(
                backdrops=[
                    {"full_url": f"https://image.tmdb.org/t/p/original/cover-{i}.jpg"}
                    for i in range(1, 12)
                ]
                + [{"full_url": "https://private.test/secret.jpg"}],
                posters=[
                    {"full_url": "https://image.tmdb.org/t/p/original" + FILMS[0]["poster_path"]}
                ],
            )
            route.fulfill(json=payload)

        alice.route("**/api/v1/discover/titles/*", pictures)
        search_interstellar(alice)
        picker = alice.locator("#feed-covers")
        expect(picker.get_by_role("button", name="横版剧照 · 11", exact=True)).to_be_visible()
        for n in range(1, 10):
            picker.get_by_role("checkbox", name=f"横版剧照 {n}", exact=True).check()
            gallery = picker.locator(".artwork-selection-preview .artwork-gallery")
            expect(gallery).to_have_attribute("data-count", str(n))
            expect(gallery).to_have_attribute("data-columns", str(2 if n == 4 else min(3, n)))
            if n in (4, 5, 6, 9):
                rows = gallery.locator(".artwork-tile").evaluate_all(
                    "nodes=>nodes.map(n=>Math.round(n.getBoundingClientRect().top))"
                )
                assert len(set(rows)) == (3 if n == 9 else 2)
        picker.get_by_role("checkbox", name="横版剧照 10", exact=True).click()
        expect(picker.get_by_role("checkbox", name="横版剧照 10", exact=True)).not_to_be_checked()
        assert (
            picker.locator(".feed-cover-choice img").first.evaluate(
                "n=>getComputedStyle(n).objectFit"
            )
            == "contain"
        )
        picker.get_by_role("button", name="前移第 2 张图片", exact=True).click()
        picker.get_by_role("button", name="预览第 1 张图片", exact=True).click()
        expect(alice.get_by_role("dialog", name="图片预览", exact=True)).to_be_visible()
        alice.get_by_role("dialog", name="图片预览", exact=True).get_by_role(
            "button", name="关闭", exact=True
        ).click()
        alice.set_viewport_size({"width": 390, "height": 844})
        assert alice.evaluate("document.documentElement.scrollWidth<=innerWidth+1")
        alice.get_by_role("radio", name="4 星", exact=True).check()
        alice.get_by_role("button", name="发布公开推荐", exact=True).click()
        expect(picker).not_to_be_visible()
        post = plugin_request(alice, origin, "get", "/feed").json()["posts"][0]
        assert len(post["media"]["cover_images"]) == 9
        assert post["media"]["cover_path"] == "/cover-2.jpg"
        refresh_feed(alice)
        expect(feed_card(alice, post["id"]).locator(".artwork-gallery")).to_have_attribute(
            "data-count", "9"
        )
        open_feed(bob)
        expect(feed_card(bob, post["id"]).locator(".artwork-gallery")).to_have_attribute(
            "data-count", "9"
        )
        assert_only_local_native_requests(origin, requests)
        assert not errors


def test_cover_lookup_failure_keeps_default_publication_available(tmp_path):
    with ExitStack() as stack:
        origin, _ = stack.enter_context(native_at_port(tmp_path))
        (page,) = browser_pages(stack)
        enter(page, origin, "alice")
        page.route(
            "**/api/v1/discover/titles/*",
            lambda route: route.fulfill(status=503, json={"detail": "受控图片故障"}),
        )
        search_interstellar(page)
        expect(page.locator("#feed-covers")).to_contain_text("仍可使用默认封面")
        page.get_by_role("radio", name="5 星", exact=True).check()
        page.get_by_role("button", name="发布公开推荐", exact=True).click()
        expect(page.locator("#feed-covers")).not_to_be_visible()
        post = plugin_request(page, origin, "get", "/feed").json()["posts"][0]
        assert "cover_path" not in post["media"]
        assert "cover_type" not in post["media"]


def test_full_library_artwork_more_than_details_limit_and_private_urls_filtered(
    tmp_path, native_admin
):
    from test_community_web_e2e import enter_admin

    with ExitStack() as stack:
        origin, _ = stack.enter_context(native_at_port(tmp_path))
        (page,) = browser_pages(stack)
        enter_admin(page, origin)

        def metadata(route):
            route.fulfill(
                json={
                    "success": True,
                    "data": {
                        "title": {
                            "title_ref": "tmdb:movie:157336",
                            "provider": "tmdb",
                            "external_id": "157336",
                            "media_type": "movie",
                            "title": "星际穿越",
                            "library_status": {"media_item_id": 77, "file_count": 1},
                        },
                        "metadata": {},
                        "library_links": [{"library_id": 4, "media_item_id": 77}],
                        "backdrops": [],
                        "posters": [],
                    },
                }
            )

        page.route("**/api/v1/discover/titles/*", metadata)
        page.route(
            "**/libraries/4/items/77/artwork/candidates",
            lambda route: route.fulfill(
                json={
                    "success": True,
                    "data": {
                        "backdrops": [
                            {"file_path": f"/full-{i}.jpg", "source": "tmdb"} for i in range(72)
                        ]
                        + [{"file_path": "https://private.test/token.jpg", "source": "tmdb"}],
                        "posters": [
                            {"file_path": f"/poster-{i}.jpg", "source": "tmdb"} for i in range(45)
                        ],
                    },
                }
            ),
        )
        search_interstellar(page)
        picker = page.locator("#feed-covers")
        picker.get_by_role("button", name="读取完整候选图库", exact=True).click()
        expect(picker.get_by_role("button", name="横版剧照 · 72", exact=True)).to_be_visible()
        picker.get_by_role("button", name="横版剧照 · 72", exact=True).click()
        expect(picker.locator(".feed-cover-choice")).to_have_count(36)
        picker.get_by_role("button", name="显示更多图片（剩余 36 张）", exact=True).click()
        expect(picker.locator(".feed-cover-choice")).to_have_count(72)
        assert "private.test" not in picker.inner_html()


def test_list_cover_editor_persists_selected_image_without_publishing(tmp_path):
    with ExitStack() as stack:
        origin, _ = stack.enter_context(native_at_port(tmp_path))
        (page,) = browser_pages(stack)
        enter(page, origin, "alice")
        movie = {
            "tmdb_id": 157336,
            "kind": "movie",
            "title": "星际穿越",
            "poster_path": FILMS[0]["poster_path"],
        }
        result = plugin_request(
            page, origin, "post", "/lists", {"name": "高清片单", "items": [movie]}
        )
        assert result.status == 201
        number = result.json()["id"]
        page.locator('[data-tab="collections"]').click()
        card = page.locator(".plugin-list-card", has_text="高清片单")
        card.get_by_role("button", name="编辑片单", exact=True).click()
        page.locator("#list-cover-editor summary").click()
        page.get_by_role("button", name="从星际穿越选择片单封面", exact=True).click()
        picker = page.locator("#list-cover-editor .artwork-picker")
        expect(picker.get_by_role("radio", name="竖版海报 1", exact=True)).to_be_visible()
        picker.get_by_role("radio", name="竖版海报 1", exact=True).check()
        page.get_by_role("button", name="保存片单修改", exact=True).click()
        expect(page.locator("#list-create-dialog")).not_to_be_visible()
        saved = plugin_request(page, origin, "get", "/lists").json()["lists"][0]
        assert saved["id"] == number and saved["cover"]["cover_type"] == "poster"
        assert not saved["shared"]
        expect(card.locator(".has-custom-cover")).to_be_visible()
