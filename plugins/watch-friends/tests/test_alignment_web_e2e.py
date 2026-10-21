"""三平台手动对齐真实浏览器流程；全部连接和写入只落在本机受控测试端。"""

import asyncio
import re
from contextlib import ExitStack

import pytest
from fastapi.responses import JSONResponse
from movieclaw_friends.peers import PeerError
from starlette.middleware.base import BaseHTTPMiddleware
from test_feed_e2e import plugin_request
from test_profile_web_e2e import profile_host as _profile_host
from test_watch_archive import fact
from test_web_e2e import HOST_COOKIE, browser_pages, enter, expect, native_at_port

profile_host = _profile_host


@pytest.mark.parametrize("failure", [False, True, "close", "account"])
def test_three_platform_preview_confirmation_and_retry(
    tmp_path, profile_host, monkeypatch, failure
):
    with ExitStack() as stack:
        origin, host = stack.enter_context(native_at_port(tmp_path))
        (page,) = browser_pages(stack)
        errors, external_writes, native_writes = [], [], []
        page.on("pageerror", lambda error: errors.append(str(error)))
        native_marks = {(650, None, None): True, (1496, 1, 1): False, (1496, 1, 2): True}
        inventories = {
            "emby": [
                fact(550, external="movie"),
                fact(1396, kind="tv", season=1, episode=1, external="one"),
                fact(1396, kind="tv", season=1, episode=2, external="two"),
            ],
            "plex": [
                fact(550, external="movie"),
                fact(1396, kind="tv", season=1, episode=1, external="one"),
                fact(1396, kind="tv", season=1, episode=2, external="two"),
            ],
        }
        states = {
            ("emby", "movie"): False,
            ("emby", "one"): True,
            ("emby", "two"): False,
            ("plex", "movie"): True,
            ("plex", "one"): False,
            ("plex", "two"): False,
        }

        async def external(platform, base, token, progress, **kwargs):
            records = [
                {
                    **row,
                    "media": dict(row["media"]),
                    "played": states[(platform, row["external_id"])],
                    "library_ids": ["1"],
                }
                for row in inventories[platform]
            ]
            return {"account_id": "personal", "account_name": "测试本人", "records": records}

        async def inspect(client, source, base, token, external_id, expected):
            if failure is True and not failed_once:
                failed_once.append(True)
                raise PeerError("核验时暂时连接失败")
            if failure == "close":
                await asyncio.sleep(0.3)
            return {"played": states[(source["platform"], external_id)], "library_ids": {"1"}}

        failed_once = []

        async def mark(base, token, platform, account, external_id):
            external_writes.append((platform, external_id))
            states[(platform, external_id)] = True

        host.state.business.state.watch_archive.external.read = external
        host.state.business.state.watch_archive.external.mark_played = mark
        monkeypatch.setattr("movieclaw_friends.watch_alignment.inspect_item", inspect)

        async def inventory(request, call_next):
            path, params = request.url.path, request.query_params
            if path == "/api/v1/libraries":
                return JSONResponse(
                    {"success": True, "data": [{"id": 1, "kind": "movie"}, {"id": 2, "kind": "tv"}]}
                )
            if re.fullmatch(r"/api/v1/libraries/[12]/items", path):
                assert params["w"] == "seen"
                return JSONResponse(
                    {
                        "success": True,
                        "data": [
                            {
                                "media_item_id": 650,
                                "tmdb_id": 550,
                                "kind": "movie",
                                "title": "搏击俱乐部",
                                "seasons": [],
                            }
                        ]
                        if "/1/" in path
                        else [
                            {
                                "media_item_id": 1496,
                                "tmdb_id": 1396,
                                "kind": "tv",
                                "title": "绝命毒师",
                                "seasons": [1],
                            }
                        ],
                    }
                )
            if path == "/api/v1/playback/items/1496/episodes":
                return JSONResponse(
                    {
                        "success": True,
                        "data": {
                            "season_number": 1,
                            "episodes": [
                                {
                                    "episode_number": e,
                                    "owned": True,
                                    "played": native_marks[(1496, 1, e)],
                                }
                                for e in (1, 2)
                            ],
                        },
                    }
                )
            if path.startswith("/api/v1/discover/titles/"):
                ref = path.split("/")[-1]
                kind, number = ref.split(":")[1:]
                media_id = int(number) + 100
                return JSONResponse(
                    {
                        "success": True,
                        "data": {
                            "title": {
                                "title_ref": ref,
                                "media_type": kind,
                                "library_status": {"file_count": 2, "media_item_id": media_id},
                            },
                            "library_links": [
                                {
                                    "library_id": 1 if kind == "movie" else 2,
                                    "media_item_id": media_id,
                                }
                            ],
                        },
                    }
                )
            if path == "/api/v1/playback/marks":
                assert request.cookies.get(HOST_COOKIE) == "alice"
                if request.method == "GET":
                    key = (
                        int(params["media_item_id"]),
                        int(params["season_number"]) if "season_number" in params else None,
                        int(params["episode_number"]) if "episode_number" in params else None,
                    )
                else:
                    payload = await request.json()
                    key = (
                        payload["media_item_id"],
                        payload.get("season_number"),
                        payload.get("episode_number"),
                    )
                    assert payload["played"] is True and key == (1496, 1, 1)
                    native_writes.append(key)
                    native_marks[key] = True
                return JSONResponse({"success": True, "data": {"played": native_marks[key]}})
            return await call_next(request)

        host.middleware_stack = BaseHTTPMiddleware(host.middleware_stack, dispatch=inventory)

        enter(page, origin, "alice")
        for platform in ("emby", "plex"):
            result = plugin_request(
                page,
                origin,
                "post",
                "/watch/connections",
                {
                    "platform": platform,
                    "url": "https://external.test",
                    "token": "personal-test-token",
                    "emby_user": "本人",
                },
            )
            assert result.status in {200, 202}
        page.locator('[data-tab="privacy"]').click()
        page.get_by_role("button", name="预览三平台已看对齐", exact=True).click()
        dialog = page.locator(".alignment-dialog")
        expect(dialog.get_by_role("button", name="开始只读预览", exact=True)).to_be_visible()
        expect(dialog.get_by_role("button", name="关闭", exact=True)).to_be_focused()
        expect(dialog.get_by_role("tooltip")).to_have_count(0)
        dialog.get_by_role("button", name="开始只读预览", exact=True).click()
        expect(dialog.locator(".alignment-row")).to_have_count(5, timeout=15000)
        assert not external_writes and not native_writes
        submit = dialog.get_by_role("button", name="确认同步勾选项", exact=True)
        expect(submit).to_be_disabled()
        dialog.get_by_label(re.compile("确认将勾选的 5 项")).check()
        if failure == "account":
            page.context.add_cookies([{"name": HOST_COOKIE, "value": "bob", "url": origin}])
            submit.click()
            expect(dialog.locator(".watch-status")).to_contain_text("账号已改变")
            assert not external_writes and not native_writes
            return
        if failure == "close":
            with page.expect_request(lambda request: "/apply/" in request.url) as started:
                submit.click()
            with page.expect_response(lambda response: response.url == started.value.url):
                dialog.get_by_role("button", name="关闭", exact=True).click()
            assert len(external_writes) == 1 and not native_writes
            return
        submit.click()
        expect(dialog.locator(".watch-status")).to_contain_text("本次同步已结束", timeout=15000)
        if failure is True:
            expect(dialog.locator(".alignment-row")).to_have_count(1)
            dialog.get_by_label(re.compile("确认将勾选的 1 项")).check()
            submit.click()
            expect(dialog.locator(".alignment-row")).to_have_count(0)
        else:
            expect(dialog.locator(".alignment-row")).to_have_count(0)
        assert native_writes == [(1496, 1, 1)]
        assert len(external_writes) == 4
        assert all(states.values()) and all(native_marks.values())
        dialog.get_by_role("button", name="关闭", exact=True).click()
        page.get_by_role("button", name="预览三平台已看对齐", exact=True).click()
        dialog = page.locator(".alignment-dialog")
        dialog.get_by_role("button", name="查看上次对齐预览", exact=True).click()
        expect(dialog.locator(".alignment-row")).to_have_count(0)
        expect(dialog.locator(".task-progress-panel")).to_contain_text("剩余 0")
        expect(dialog.locator(".task-platform-result")).not_to_have_count(0)
        if failure is False:
            dialog.get_by_label("对齐状态", exact=True).select_option("")
            expect(dialog.locator(".alignment-row")).to_have_count(9)
            dialog.get_by_role("button", name="清空选择", exact=True).click()
            dialog.locator(".alignment-row input").first.check()
            before_recheck = len(external_writes)
            dialog.get_by_role("button", name="重新核对选中项", exact=True).click()
            expect(dialog.locator(".alignment-row")).to_have_count(0, timeout=15000)
            dialog.get_by_label("对齐状态", exact=True).select_option("")
            expect(dialog.locator(".alignment-row")).to_have_count(1)
            expect(dialog.get_by_role("button", name="确认同步勾选项", exact=True)).to_be_disabled()
            assert len(external_writes) == before_recheck
            dialog.get_by_role("button", name="关闭", exact=True).click()
            page.get_by_role("button", name="任务中心", exact=True).click()
            center = page.locator(".task-center-dialog")
            center.get_by_label("任务类型").select_option("alignment")
            expect(center.locator(".task-card")).to_have_count(2)
            expect(center).to_contain_text("已处理 5 / 5")
            center.locator(".task-card").last.get_by_role(
                "button", name="查看对齐详情", exact=True
            ).click()
            dialog = page.locator(".alignment-dialog")
            expect(dialog.locator(".task-progress-panel")).to_contain_text("5 / 5")
        page.set_viewport_size({"width": 390, "height": 844})
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        assert dialog.evaluate("node => node.scrollWidth <= node.clientWidth")
        assert not errors, errors
