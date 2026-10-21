"""真实页面开启、保存、后台继续和暂停；只写本机受控的假平台。"""

import asyncio
import threading
from contextlib import ExitStack

from test_feed_e2e import plugin_request
from test_profile_web_e2e import profile_host as _profile_host
from test_watch_archive import fact
from test_web_e2e import browser_pages, enter, expect, native_at_port

profile_host = _profile_host


def test_auto_sync_settings_default_off_background_progress_and_pause(
    tmp_path, profile_host, monkeypatch
):
    errors, writes = [], []
    entered, release = threading.Event(), threading.Event()
    states = {"emby": False, "plex": True}

    async def external(platform, base, token, progress, **kwargs):
        return {
            "account_id": "personal",
            "account_name": "测试本人",
            "libraries": [{"id": "1", "name": "电影库"}, {"id": "2", "name": "私人库"}],
            "records": [
                fact(550, external="10") | {"played": states[platform], "library_ids": ["1"]}
            ],
        }

    async def inspect(client, source, base, token, external_id, expected):
        return {"played": states[source["platform"]], "library_ids": {"1", "folder"}}

    async def mark(base, token, platform, account, external_id):
        entered.set()
        while not release.is_set():
            await asyncio.sleep(0.02)
        writes.append((platform, external_id))
        states[platform] = True

    monkeypatch.setattr("movieclaw_friends.auto_sync.inspect_item", inspect)
    with ExitStack() as stack:
        origin, host = stack.enter_context(native_at_port(tmp_path))
        host.state.business.state.watch_archive.external.read = external
        host.state.business.state.watch_archive.external.mark_played = mark
        page, observer = browser_pages(stack, 2)
        page.on("pageerror", lambda e: errors.append(str(e)))
        enter(page, origin, "alice")
        for platform in ("emby", "plex"):
            response = plugin_request(
                page,
                origin,
                "post",
                "/watch/connections",
                {
                    "platform": platform,
                    "url": "https://external.test",
                    "token": "personal-test-token",
                    "emby_user": "alice",
                },
            )
            assert response.ok
        page.locator('[data-tab="privacy"]').click()
        auto = page.locator(".watch-auto-sync")
        expect(auto.get_by_role("heading", name="后台自动同步已看", exact=False)).to_be_visible()
        auto.locator("summary").click()
        expect(auto.get_by_label("开启后台自动已看同步", exact=True)).not_to_be_checked()
        expect(auto.get_by_text("已关闭", exact=True)).to_be_visible()
        for fieldset in auto.locator("fieldset").all():
            if fieldset.locator("legend input").is_disabled():
                expect(fieldset).to_contain_text("请先验证并绑定账号")
                continue
            fieldset.locator("legend input").check()
            expect(fieldset.get_by_label("私人库", exact=True)).not_to_be_checked()
            fieldset.get_by_label("电影库", exact=True).check()
        auto.get_by_label("开启后台自动已看同步", exact=True).check()
        auto.get_by_label(
            "确认将所选媒体库的已有及今后已看内容，自动补标到所选接收账号", exact=True
        ).check()
        auto.get_by_role("button", name="保存自动同步设置", exact=True).click()
        expect(auto.get_by_text("已开启", exact=True)).to_be_visible()
        auto.get_by_role("button", name="立即检查", exact=True).click()
        assert entered.wait(5)
        # 请求在服务端执行；关闭发起页面不会取消整个任务。
        page.close()
        enter(observer, origin, "alice")
        observer.locator('[data-tab="privacy"]').click()
        current = observer.locator(".watch-auto-sync")
        expect(current.get_by_text("执行中", exact=True)).to_be_visible()
        expect(current).to_contain_text("本轮执行中，结束后安排下一次检查")
        expect(current.get_by_text("已处理 0 / 1 · 剩余 1", exact=True)).to_be_visible()
        release.set()
        expect(current.get_by_text("已完成", exact=True)).to_be_visible(timeout=10000)
        expect(current.get_by_text("已处理 1 / 1 · 剩余 0", exact=True)).to_be_visible()
        current.get_by_role("button", name="查看明细", exact=True).click()
        modal = observer.locator(".auto-sync-dialog")
        expect(modal).to_contain_text("已回读确认")
        modal.get_by_role("button", name="关闭", exact=True).click()
        current.get_by_role("button", name="暂停同步", exact=True).click()
        expect(current.get_by_text("已关闭", exact=True)).to_be_visible()
        current.locator("summary").click()
        expect(current.get_by_label("开启后台自动已看同步", exact=True)).not_to_be_checked()
        observer.set_viewport_size({"width": 390, "height": 844})
        assert observer.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
        assert writes == [("emby", "10")] and errors == []
