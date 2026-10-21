"""以浏览器本人库存验证可看状态：不借其他账号或远端的条目编号跳播放。"""

from contextlib import ExitStack
from urllib.parse import unquote, urlsplit

import pytest
from test_web_e2e import browser_pages, enter, expect, native_at_port


def stock_response(route, *, available=False, mismatch=False, forbidden=False):
    if forbidden:
        route.fulfill(status=403, json={"detail": "当前成员无权读取"})
        return
    key = unquote(urlsplit(route.request.url).path.split("/")[-1])
    _, kind, number = key.split(":")
    status = {"media_item_id": 701, "library_count": 1, "file_count": 2} if available else None
    route.fulfill(
        json={
            "success": True,
            "data": {
                "title": {
                    "title_ref": key,
                    "provider": "tmdb",
                    "external_id": str(int(number) + mismatch),
                    "media_type": kind,
                    "title": "本人可见的电影",
                    "library_status": status,
                },
                "metadata": {},
                "library_links": [
                    {"library_id": 12, "library_name": "本人可见库", "media_item_id": 701}
                ]
                if available
                else [],
            },
        }
    )


def open_current_movie(page):
    page.locator("#friend-detail .now-card .media-poster").click()
    expect(page.locator("#media-dialog")).to_be_visible()
    return page.locator("#media-dialog .availability-widget")


def test_availability_uses_each_viewers_own_inventory(tmp_path):
    errors = []
    with ExitStack() as stack:
        origin, _host = stack.enter_context(native_at_port(tmp_path / "plugin", demo_enabled=True))
        alice, bob = browser_pages(stack, 2)
        alice.route(
            "**/api/v1/discover/titles/**", lambda route: stock_response(route, available=True)
        )
        bob.route("**/api/v1/discover/titles/**", stock_response)
        for page in (alice, bob):
            page.on("pageerror", lambda error: errors.append(str(error)))
        enter(alice, origin, "alice")
        enter(bob, origin, "bob")
        own = open_current_movie(alice)
        expect(own).to_have_attribute("data-state", "available")
        expect(own.get_by_role("link", name="立即观看", exact=True)).to_have_attribute(
            "href", "/play/701"
        )
        expect(own.get_by_role("link", name="查看本服片源")).to_have_attribute(
            "href", "/library/12/item/701"
        )
        other = open_current_movie(bob)
        expect(other).to_have_attribute("data-state", "missing")
        expect(other).to_contain_text("当前账号暂未找到本服片源")
        assert other.get_by_role("link", name="立即观看", exact=True).count() == 0
        assert not errors


@pytest.mark.parametrize("failure", ["forbidden", "identity"])
def test_availability_unknown_is_not_reported_as_missing(tmp_path, failure):
    with ExitStack() as stack:
        origin, _host = stack.enter_context(native_at_port(tmp_path / "plugin", demo_enabled=True))
        (page,) = browser_pages(stack)
        page.route(
            "**/api/v1/discover/titles/**",
            lambda route: stock_response(
                route,
                available=True,
                forbidden=failure == "forbidden",
                mismatch=failure == "identity",
            ),
        )
        enter(page, origin, "alice")
        widget = open_current_movie(page)
        expect(widget).to_have_attribute("data-state", "unknown")
        assert "暂未找到" not in widget.inner_text()
        assert widget.get_by_role("link", name="立即观看", exact=True).count() == 0
        expect(widget.get_by_role("button", name="重新检查")).to_be_visible()
