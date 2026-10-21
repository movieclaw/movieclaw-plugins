"""影友动态的本机浏览器验收；只使用受控宿主 Cookie、临时库和影片 fixture。

复用原生宿主路由 harness，不连接使用者的服务或凭据。真实海报缓存仅用于
本地截图；没有缓存时仍使用 harness 的安全占位图，测试不依赖外网下载。
"""

from contextlib import ExitStack
from pathlib import Path
from urllib.parse import urlsplit

from movieclaw_friends.demo_catalog import FILMS
from test_web_e2e import PREFIX, browser_pages, enter, expect, native_at_port


def plugin_request(page, origin, method, path, body=None):
    """通过浏览器同一 Cookie 上下文准备资料；所有写入仅指向插件自己的 API。"""
    options = {"headers": {"X-Friends-Request": "1", "Origin": origin}}
    if body is not None:
        options["data"] = body
    return getattr(page.context.request, method)(origin + PREFIX + path, **options)


def use_cached_real_posters(host):
    directory = Path("/tmp/watch-friends-real-posters")
    for film in FILMS:
        path = directory / (film["demo_id"] + ".jpg")
        if path.is_file():
            host.state.poster_images["https://image.tmdb.org/t/p/w500" + film["poster_path"]] = (
                path.read_bytes()
            )


def open_feed(page):
    page.locator('[data-tab="feed"]').click()
    expect(page.locator("#panel-feed")).to_be_visible()
    expect(page.locator("#feed-compose-form")).to_be_attached()


def feed_card(page, post_id):
    return page.locator(f'.feed-card[data-post-id="{post_id}"]')


def refresh_feed(page):
    page.locator("#feed-root").get_by_role("button", name="刷新动态", exact=True).click()


def record_browser(page, errors, requests):
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("request", lambda request: requests.append((request.method, request.url)))


def assert_only_local_native_requests(origin, requests):
    assert requests
    assert all(url.startswith(origin + "/") for _, url in requests)
    # 宿主搜索是明确 save_history=false 的只读语义 POST；其余写操作只进插件。
    assert all(
        method in {"GET", "HEAD", "OPTIONS"}
        or urlsplit(url).path.startswith(PREFIX + "/")
        or (
            method == "POST"
            and urlsplit(url).path
            in {"/api/v1/search/titles", "/api/v1/subscriptions/title-preview"}
        )
        for method, url in requests
    )


def test_rating_only_publish_keyboard_clear_and_mobile_layout(tmp_path):
    errors, requests = [], []
    with ExitStack() as stack:
        origin, host = stack.enter_context(native_at_port(tmp_path / "plugin"))
        alice, bob = browser_pages(stack, 2)
        for page in (alice, bob):
            record_browser(page, errors, requests)
        enter(alice, origin, "alice")
        enter(bob, origin, "bob")
        open_feed(alice)
        alice.locator(".feed-compose>summary").click()
        alice.locator("#feed-search").fill("钢琴")
        alice.locator(".feed-compose").get_by_role("button", name="搜索影片", exact=True).click()
        alice.locator("#feed-search-results .feed-search-choice").click()
        expect(alice.locator("#feed-body")).to_have_js_property("required", True)
        alice.get_by_role("radio", name="3 星", exact=True).check()
        alice.get_by_role("radio", name="3 星", exact=True).press("ArrowRight")
        expect(alice.get_by_role("radio", name="4 星", exact=True)).to_be_checked()
        expect(alice.locator("#feed-body")).to_have_js_property("required", False)
        expect(alice.locator(".feed-rating-option.is-filled")).to_have_count(4)
        alice.get_by_role("button", name="清除评分", exact=True).click()
        expect(alice.locator("#feed-body")).to_have_js_property("required", True)
        expect(alice.locator("#feed-rating input:checked")).to_have_count(0)
        alice.get_by_role("radio", name="4 星", exact=True).check()
        with alice.expect_response(
            lambda response: (
                response.url == origin + PREFIX + "/feed" and response.request.method == "POST"
            )
        ) as published:
            alice.get_by_role("button", name="发布公开推荐", exact=True).click()
        assert published.value.status == 201
        post = published.value.json()
        assert post["rating"] == 4 and post["body"] == ""
        own = feed_card(alice, post["id"])
        expect(own.locator(".feed-post-rating")).to_have_attribute("aria-label", "评分：4 / 5 星")
        expect(own.locator(".feed-post-rating .is-filled")).to_have_count(4)
        expect(own.locator(".feed-post-copy")).to_have_count(0)
        expect(alice.locator("#feed-rating input:checked")).to_have_count(0)
        open_feed(bob)
        expect(feed_card(bob, post["id"]).locator(".feed-post-rating")).to_have_text("4 / 5")
        refresh_feed(bob)
        expect(feed_card(bob, post["id"]).locator(".feed-post-rating")).to_have_text("4 / 5")
        alice.set_viewport_size({"width": 390, "height": 844})
        alice.locator(".feed-compose>summary").click()
        alice.get_by_role("radio", name="5 星", exact=True).check()
        assert alice.evaluate("document.documentElement.scrollWidth <= innerWidth")
        assert alice.locator(".feed-compose").evaluate(
            "node => node.scrollWidth <= node.clientWidth"
        )
        assert_only_local_native_requests(origin, requests)
        assert not errors, errors


def test_feed_two_members_publish_like_comment_and_save_private_movie(tmp_path):
    errors, requests = [], []
    with ExitStack() as stack:
        origin, host = stack.enter_context(native_at_port(tmp_path / "plugin", demo_enabled=True))
        use_cached_real_posters(host)
        host.state.backdrop_urls["tmdb:movie:1900"] = (
            "https://image.tmdb.org/t/p/w1280/real-title-backdrop.jpg"
        )
        alice, bob = browser_pages(stack, 2)
        for page in (alice, bob):
            record_browser(page, errors, requests)
        enter(alice, origin, "alice")
        enter(bob, origin, "bob")
        open_feed(alice)
        alice.locator(".feed-compose>summary").click()
        body = '<img src=x onerror="window.__feedPostXss=1">海风和琴声里的好故事。'
        alice.locator("#feed-body").fill(body)
        alice.get_by_role("button", name="发布公开推荐", exact=True).click()
        expect(alice.locator("#toast")).to_contain_text("请先搜索并选择")
        assert plugin_request(alice, origin, "get", "/feed").json()["posts"] == []
        alice.locator("#feed-search").fill("钢琴")
        alice.locator(".feed-compose").get_by_role("button", name="搜索影片", exact=True).click()
        choice = alice.locator("#feed-search-results .feed-search-choice")
        expect(choice).to_contain_text("海上钢琴师")
        choice.click()
        expect(alice.locator("#feed-selected")).to_contain_text("已选择")
        # 发布按钮直接明确说明公开范围；选择影片本身不会创建推荐。
        assert plugin_request(alice, origin, "get", "/feed").json()["posts"] == []
        with alice.expect_response(
            lambda response: (
                response.url == origin + PREFIX + "/feed" and response.request.method == "POST"
            )
        ) as published:
            alice.get_by_role("button", name="发布公开推荐", exact=True).click()
        assert published.value.status == 201
        post = published.value.json()
        post_id = post["id"]
        assert post["media"]["tmdb_id"] == 1900 and post["author"]["is_self"] is True
        assert "path" not in post["media"] and "media_item_id" not in post["media"]
        own = feed_card(alice, post_id)
        expect(own.locator(".media-artwork")).to_have_class("media-artwork has-backdrop")
        expect(own.locator(".feed-post-copy")).to_have_text(body)
        assert own.locator(".feed-post-copy img").count() == 0
        assert alice.evaluate("window.__feedPostXss") is None

        open_feed(bob)
        viewed = feed_card(bob, post_id)
        expect(viewed).to_be_visible()
        expect(viewed.locator(".feed-author")).to_contain_text("林栖")
        assert viewed.get_by_role("button", name="删除推荐", exact=True).count() == 0
        viewed.get_by_role("button", name="点赞 0", exact=True).click()
        expect(viewed.get_by_role("button", name="已赞 1", exact=True)).to_have_attribute(
            "aria-pressed", "true"
        )
        for _ in range(2):
            duplicate = plugin_request(bob, origin, "put", f"/feed/{post_id}/like", {"liked": True})
            assert duplicate.json() == {"liked": True, "like_count": 1}
        refresh_feed(bob)
        expect(viewed.get_by_role("button", name="已赞 1", exact=True)).to_be_visible()

        created = plugin_request(
            bob,
            origin,
            "post",
            "/lists",
            {"name": "周末音乐片单", "description": "自己的私人片单", "items": []},
        )
        assert created.status == 201
        list_id = created.json()["id"]
        viewed.get_by_role("button", name="加入片单", exact=True).click()
        expect(bob.locator("#list-target-dialog")).to_be_visible()
        bob.locator("#list-target-options").get_by_role(
            "button", name="周末音乐片单 私人片单", exact=True
        ).click()
        expect(bob.locator("#list-target-dialog")).not_to_be_visible()
        saved = plugin_request(bob, origin, "get", "/lists").json()["lists"]
        assert len(saved) == 1 and saved[0]["id"] == list_id
        assert saved[0]["shared"] is False and saved[0]["items"][0]["tmdb_id"] == 1900
        assert plugin_request(alice, origin, "get", "/lists").json()["lists"] == []
        viewed.get_by_role("button", name="收藏影片", exact=True).click()
        expect(bob.locator("#toast")).to_contain_text("已加入我喜欢的电影")
        favorite = plugin_request(bob, origin, "get", "/favorites").json()
        assert len(favorite["items"]) == 1 and favorite["items"][0]["tmdb_id"] == 1900
        assert favorite["public_items"] == []
        assert plugin_request(alice, origin, "get", "/favorites").json()["items"] == []
        me = plugin_request(bob, origin, "get", "/me").json()["user"]
        assert not me["discoverable"] and not me["share_favorites"]
        assert host.state.business.state.store.all("SELECT * FROM collections") == []

        viewed.locator(".feed-discussion>summary").click()
        comment_body = '<svg onload="window.__feedCommentXss=1">我也喜欢这部电影</svg>'
        viewed.get_by_label("评论林栖的推荐", exact=True).fill(comment_body)
        with bob.expect_response(
            lambda response: (
                response.url == origin + PREFIX + f"/feed/{post_id}/comments"
                and response.request.method == "POST"
            )
        ) as commented:
            viewed.get_by_role("button", name="发送评论", exact=True).click()
        assert commented.value.status == 201
        comment_id = commented.value.json()["id"]
        expect(viewed.locator(".feed-comment")).to_contain_text(comment_body)
        assert viewed.locator(".feed-comment svg").count() == 0
        assert bob.evaluate("window.__feedCommentXss") is None
        refresh_feed(alice)
        own.locator(".feed-discussion>summary").click()
        expect(own.locator(".feed-comment")).to_contain_text(comment_body)
        assert own.get_by_role("button", name="删除评论", exact=True).count() == 0
        assert plugin_request(alice, origin, "delete", f"/feed/comments/{comment_id}").status == 404
        viewed.get_by_role("button", name="删除评论", exact=True).click()
        expect(viewed.locator(".feed-comment")).to_have_count(0)
        expect(viewed.locator(".feed-discussion>summary")).to_have_text("评论 0")
        assert (
            plugin_request(bob, origin, "get", f"/feed/{post_id}/comments").json()["comments"] == []
        )
        links = viewed.locator(".feed-native-actions a")
        expect(links).to_have_count(4)
        for link in links.all():
            href = link.get_attribute("href")
            assert href == "/media/movie/1900" or href.startswith("/search?")
            assert link.get_attribute("target") == "_top"
        viewed.get_by_role("button", name="海上钢琴师", exact=True).click()
        expect(bob.locator("#media-dialog")).to_be_visible()
        expect(
            bob.locator("#media-dialog").get_by_role("link", name="在 MovieClaw 打开", exact=True)
        ).to_have_attribute("href", "/media/movie/1900")
        assert ("search", "钢琴") in host.state.media_calls
        assert ("details", "tmdb:movie:1900") in host.state.media_calls
        assert_only_local_native_requests(origin, requests)
        assert not errors, errors


def test_feed_demo_switch_keeps_real_posts_and_mobile_content_wraps(tmp_path):
    errors, requests = [], []
    with ExitStack() as stack:
        origin, host = stack.enter_context(native_at_port(tmp_path / "plugin", demo_enabled=True))
        use_cached_real_posters(host)
        alice, bob = browser_pages(stack, 2)
        for page in (alice, bob):
            record_browser(page, errors, requests)
        enter(alice, origin, "alice")
        enter(bob, origin, "bob")
        created = plugin_request(
            alice,
            origin,
            "post",
            "/feed",
            {
                "media": {
                    "tmdb_id": 1900,
                    "kind": "movie",
                    "title": "海上钢琴师",
                    "year": 1998,
                    "poster_path": "/search-poster.jpg",
                },
                "body": "在海风与琴声里，记住一个认真生活的故事。",
                "publish_consent": True,
            },
        )
        assert created.status == 201
        post_id = created.json()["id"]
        open_feed(alice)
        expect(alice.locator("#feed-posts .feed-card")).to_have_count(9)
        expect(alice.locator("#feed-posts .demo-badge")).to_have_count(8)
        expect(alice.locator("#feed-posts .feed-card").first).to_have_attribute(
            "data-post-id", str(post_id)
        )
        demo = alice.locator(".feed-card", has=alice.locator(".demo-badge")).first
        expect(demo.locator(".feed-author .avatar img")).to_be_visible()
        demo.locator(".feed-film .media-poster").scroll_into_view_if_needed()
        expect(demo.locator(".feed-film .media-poster img")).to_have_js_property("complete", True)
        assert demo.locator(".feed-film .media-poster img").evaluate(
            "image => image.naturalWidth > 0"
        )
        # 虚拟推荐的互动只在当前页面预览，不产生任何真实互动记录。
        demo.get_by_role("button", name="点赞 8", exact=True).click()
        expect(demo.get_by_role("button", name="已赞 9", exact=True)).to_be_visible()
        assert host.state.business.state.store.all("SELECT * FROM social_likes") == []
        real = feed_card(alice, post_id)
        real.locator(".feed-discussion>summary").click()
        draft = "这条真实推荐下的评论还没写完。"
        real.get_by_label("评论林栖的推荐", exact=True).fill(draft)
        real_handle = real.element_handle()
        alice.locator('[data-tab="privacy"]').click()
        alice.locator("#demo-toggle").uncheck()
        expect(alice.locator("#feed-posts .feed-card")).to_have_count(1)
        expect(feed_card(alice, post_id)).to_have_count(1)
        expect(alice.locator("#feed-posts .demo-badge")).to_have_count(0)
        expect(real.get_by_label("评论林栖的推荐", exact=True)).to_have_value(draft)
        assert real_handle.evaluate(
            "card => card === document.querySelector('#feed-posts .feed-card')"
        )
        open_feed(bob)
        expect(bob.locator("#feed-posts .feed-card")).to_have_count(9)
        assert len(plugin_request(bob, origin, "get", "/feed").json()["posts"]) == 1
        alice.locator("#demo-toggle").check()
        expect(alice.locator("#feed-posts .feed-card")).to_have_count(9)
        expect(real.get_by_label("评论林栖的推荐", exact=True)).to_have_value(draft)
        open_feed(alice)
        expect(alice.locator("#feed-status")).to_be_hidden()
        first = feed_card(alice, post_id)
        first.scroll_into_view_if_needed()
        alice.screenshot(path=str(tmp_path / "feed-social-desktop.png"))
        alice.set_viewport_size({"width": 390, "height": 844})
        first.locator(".feed-discussion>summary").click()
        first.get_by_label("评论林栖的推荐", exact=True).fill("longword" * 60)
        first.get_by_role("button", name="发送评论", exact=True).click()
        expect(first.locator(".feed-comment")).to_contain_text("longword" * 60)
        assert alice.evaluate("document.documentElement.scrollWidth <= innerWidth")
        assert first.evaluate("card => card.scrollWidth <= card.clientWidth")
        first.locator(".feed-discussion>summary").click()
        first.scroll_into_view_if_needed()
        alice.screenshot(path=str(tmp_path / "feed-social-mobile.png"))
        alice.locator(".feed-compose>summary").click()
        alice.locator("#feed-body").fill("unbroken" * 200)
        assert alice.evaluate("document.documentElement.scrollWidth <= innerWidth")
        assert alice.locator(".feed-compose").evaluate(
            "compose => compose.scrollWidth <= compose.clientWidth"
        )
        assert_only_local_native_requests(origin, requests)
        assert not errors, errors


def test_feed_two_favorite_clicks_serialize_read_modify_write(tmp_path):
    with ExitStack() as stack:
        origin, _ = stack.enter_context(native_at_port(tmp_path / "plugin", demo_enabled=True))
        (page,) = browser_pages(stack)
        enter(page, origin, "alice")
        open_feed(page)
        cards = page.locator("#feed-posts .feed-card")
        expect(cards).to_have_count(8)
        expected = {
            row["media"]["tmdb_id"]
            for row in plugin_request(page, origin, "get", "/demo").json()["users"][0]["data"][
                "recommendations"
            ]
        }
        held, reads = [], []

        def gate_first_favorite_read(route):
            if route.request.method == "GET":
                reads.append(route.request.url)
                if len(reads) == 1:
                    held.append(route)
                    return
            route.continue_()

        page.route("**" + PREFIX + "/favorites", gate_first_favorite_read)
        first = cards.nth(0).get_by_role("button", name="收藏影片", exact=True)
        second = cards.nth(1).get_by_role("button", name="收藏影片", exact=True)
        first.click()
        page.wait_for_timeout(100)
        assert len(held) == len(reads) == 1
        second.click()
        # 第一项的 GET 尚未完成；第二项若抢先读空列表，之后就会覆盖第一项。
        page.wait_for_timeout(150)
        assert len(reads) == 1
        held[0].continue_()
        expect(page.locator("#favorites-count")).to_have_text("2")
        favorites = plugin_request(page, origin, "get", "/favorites").json()
        assert {film["tmdb_id"] for film in favorites["items"]} == expected
        assert favorites["public_items"] == []


def test_feed_recommendation_from_demo_movie_details_needs_no_new_search(tmp_path):
    errors, requests = [], []
    with ExitStack() as stack:
        origin, host = stack.enter_context(native_at_port(tmp_path / "plugin", demo_enabled=True))
        use_cached_real_posters(host)
        alice, bob = browser_pages(stack, 2)
        for page in (alice, bob):
            record_browser(page, errors, requests)
        enter(alice, origin, "alice")
        enter(bob, origin, "bob")
        expected = plugin_request(alice, origin, "get", "/demo").json()["data"]["current"]["media"]
        alice.locator("#friend-detail .now-card .media-title").click()
        expect(alice.locator("#media-dialog")).to_be_visible()
        expect(alice.locator("#media-dialog")).to_contain_text(expected["title"])
        assert plugin_request(alice, origin, "get", "/feed").json()["posts"] == []
        alice.locator("#media-dialog").get_by_role(
            "button", name="推荐到影友动态", exact=True
        ).click()
        expect(alice.locator("#media-dialog")).not_to_be_visible()
        expect(alice.locator("#panel-feed")).to_be_visible()
        expect(alice.locator(".feed-compose")).to_have_attribute("open", "")
        expect(alice.locator("#feed-selected")).to_contain_text(expected["title"])
        expect(alice.locator("#feed-selected")).to_contain_text("已选择")
        expect(alice.locator("#feed-search")).to_have_value("")
        expect(alice.locator("#feed-search-results")).to_be_empty()
        # 从演示详情只带入公开作品资料，仍由真人主动填写理由并公开发布。
        assert plugin_request(alice, origin, "get", "/feed").json()["posts"] == []
        reason = "想和影友重温这段跨越时间的旅程。"
        alice.locator("#feed-body").fill(reason)
        with alice.expect_response(
            lambda response: (
                response.url == origin + PREFIX + "/feed" and response.request.method == "POST"
            )
        ) as published:
            alice.get_by_role("button", name="发布公开推荐", exact=True).click()
        assert published.value.status == 201
        post = published.value.json()
        assert post["author"]["is_self"] is True and post["body"] == reason
        assert post["media"]["tmdb_id"] == expected["tmdb_id"]
        assert post["media"]["title"] == expected["title"]
        assert post["media"]["poster_path"] == expected["poster_path"]
        assert "demo_id" not in post["media"] and "media_item_id" not in post["media"]
        open_feed(bob)
        received = feed_card(bob, post["id"])
        expect(received).to_be_visible()
        expect(received.locator(".feed-author")).to_contain_text("林栖")
        expect(received.locator(".feed-post-copy")).to_have_text(reason)
        expect(received.locator(".feed-film")).to_contain_text(expected["title"])
        assert received.locator(".demo-badge").count() == 0
        assert not any(kind == "search" for kind, _ in host.state.media_calls)
        assert_only_local_native_requests(origin, requests)
        assert not errors, errors


def test_unwatched_movie_source_check_retry_and_excluded_library_remains_blocked(tmp_path):
    with ExitStack() as stack:
        origin, host = stack.enter_context(native_at_port(tmp_path / "plugin"))
        (page,) = browser_pages(stack)
        errors, searches = [], []
        page.on("pageerror", lambda error: errors.append(str(error)))
        original = host.state.ops.call

        async def operations(operation, arguments):
            if operation != "search.library":
                return await original(operation, arguments)
            searches.append(arguments)
            if len(searches) == 1:
                raise RuntimeError("temporary private upstream failure")
            return {
                "items": [
                    {
                        "item": {
                            "kind": "movie",
                            "tmdb_id": 1900,
                            "title": "海上钢琴师",
                            "media_item_id": 777,
                        },
                        "library_ids": [1],
                    }
                ]
            }

        host.state.ops.call = operations
        enter(page, origin, "alice")
        store = host.state.business.state.store
        owner = store.one("SELECT id FROM users WHERE member_id=1")["id"]
        policy = store.display_policy
        policy.catalog(
            owner, "movieclaw", [{"id": "1", "name": "电影"}, {"id": "2", "name": "私人"}]
        )
        result = plugin_request(
            page,
            origin,
            "put",
            "/display-policy",
            {"excluded": [{"source_id": "movieclaw", "library_id": "2"}]},
        )
        assert result.status == 200
        before = store.all("SELECT * FROM watch_facts")
        open_feed(page)
        page.locator(".feed-compose>summary").click()
        page.locator("#feed-search").fill("钢琴")
        page.locator(".feed-compose").get_by_role("button", name="搜索影片", exact=True).click()
        page.locator("#feed-search-results .feed-search-choice").click()
        expect(page.locator("#feed-selected")).to_contain_text("尚未核实")
        expect(page.get_by_role("button", name="发布公开推荐", exact=True)).to_be_disabled()
        page.get_by_role("button", name="重试核验", exact=True).click()
        expect(page.locator("#feed-selected")).to_contain_text("已选择 · 可公开推荐")
        assert store.all("SELECT * FROM watch_facts") == before
        page.get_by_role("radio", name="5 星", exact=True).check()
        page.get_by_role("button", name="发布公开推荐", exact=True).click()
        expect(page.locator(".feed-card")).to_have_count(1)
        assert len(searches) == 2
        # 后续命中排除库时，前端与直接发布请求都必须重新拒绝。
        policy.remember(
            owner,
            "movieclaw",
            [
                {
                    "external_id": "excluded-copy",
                    "media": {"kind": "movie", "tmdb_id": 1900, "title": "海上钢琴师"},
                    "library_ids": ["2"],
                }
            ],
        )
        page.locator(".feed-compose>summary").click()
        page.locator("#feed-search").fill("钢琴")
        page.locator(".feed-compose").get_by_role("button", name="搜索影片", exact=True).click()
        page.locator("#feed-search-results .feed-search-choice").click()
        expect(page.locator("#feed-selected")).to_contain_text("命中了你排除的媒体库")
        expect(page.get_by_role("button", name="发布公开推荐", exact=True)).to_be_disabled()
        assert len(searches) == 2
        page.set_viewport_size({"width": 320, "height": 740})
        assert page.locator(".feed-compose").evaluate(
            "node => node.scrollWidth <= node.clientWidth"
        )
        assert not errors, errors
