"""统一通知：只模拟宿主事件和任务，不连接真实通道或网盘。"""
import asyncio
import logging
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

import p115_import as p
import p115notify as n
import p115requests as rq
from movieclaw_sdk.jobs import JobRef
from tests.test_flow import Store, Context


class SystemNotice(unittest.IsolatedAsyncioTestCase):
    def test_redacts_share_urls_and_access_codes(self):
        text = "【MovieClaw】处理失败：https://115.com/s/abc 提取码：aB12 magnet:?xt=urn:btih:abc"
        clean = n.safe_notice(text)
        self.assertNotIn("115.com/s/abc", clean)
        self.assertNotIn("aB12", clean)
        self.assertNotIn("magnet:", clean)
        self.assertIn("处理失败", clean)
        self.assertLessEqual(len(n.safe_notice("x" * 1500)), 1200)
        self.assertEqual(n.safe_notice("【MovieClaw】"), "")

    def test_tmdb_reference_only_from_verified_identity(self):
        plan = {"kind":"tv", "tmdb_id":123}
        verified = {"inventory_verified":True}
        message = n.notice_text("已入库 https://115.com/s/secret 提取码：pass", plan=plan, result=verified)
        self.assertTrue(message.startswith("影视资料：https://www.themoviedb.org/tv/123\n"))
        self.assertNotIn("115.com", message)
        self.assertNotIn("pass", message)
        for bad in ({"kind":"tv", "tmdb_id":0}, {"kind":"movie", "tmdb_id":True},
                    {"kind":"person", "tmdb_id":123}, {"kind":"tv", "tmdb_id":"123/evil"}):
            self.assertNotIn("themoviedb.org", n.notice_text("已入库", plan=bad, result=verified))
        self.assertNotIn("themoviedb.org", n.notice_text("尚未确认", plan=plan, result={"inventory_verified":False}))
        self.assertLessEqual(len(n.notice_text("x"*2000, plan=plan, result=verified)), 1200)

    async def test_publishes_once_with_no_direct_wecom_call(self):
        pub = SimpleNamespace(publish=AsyncMock())
        ok = await n.publish(pub, logging.getLogger("test"), "【MovieClaw】已核验入库")
        self.assertTrue(ok)
        pub.publish.assert_awaited_once()
        event, body = pub.publish.await_args.args
        self.assertIs(event, n.NOTICE)
        self.assertEqual(body.message, "已核验入库")

    async def test_publish_error_never_breaks_import(self):
        pub = SimpleNamespace(publish=AsyncMock(side_effect=RuntimeError("fake")))
        self.assertFalse(await n.publish(pub, logging.getLogger("test"), "失败提醒"))
        self.assertFalse(await n.publish(pub, logging.getLogger("test"), ""))
        pub.publish.assert_awaited_once()

    async def run_request(self, user):
        store = Store()
        await store.set("input:r", {"link": "fake-link", "user": user}, secret=True)
        ctx = SimpleNamespace(config=SimpleNamespace(dry_run=False))
        ops = SimpleNamespace(call=AsyncMock())
        jobs = SimpleNamespace(create=AsyncMock(return_value=JobRef("child", "p115-import:transfer", "queued", True)))
        push = AsyncMock()
        runner = rq.RequestRunner(ctx, store, jobs, ops, plan_body=p.PlanBody,
                                  build_plan=AsyncMock(return_value={"status":"nothing_to_do","message":"无需转存"}),
                                  push=push)
        await runner.handle(Context(), {"request_ref":"r"})
        return push

    async def test_user_selected_notice_keeps_receipt_and_result(self):
        push = await self.run_request("system-notify")
        self.assertEqual(push.await_count, 2)
        self.assertIn("已收到", push.await_args_list[0].args[1])
        self.assertIn("无需转存", push.await_args_list[1].args[1])

    async def test_disabled_notice_does_not_send(self):
        push = await self.run_request("")
        push.assert_not_awaited()

    async def test_legacy_inflight_request_keeps_notice(self):
        push = await self.run_request("old-bound-user")
        self.assertEqual(push.await_count, 2)

    async def test_parent_child_race_sends_only_one_completion(self):
        store = Store()
        await store.set("input:r", {"user":"system-notify"}, secret=True)
        push = AsyncMock()
        lock = asyncio.Lock()
        result = {"message":"测试剧已入库", "inventory_verified":True}
        plan = {"kind":"tv", "tmdb_id":123}
        sent = await asyncio.gather(*(rq.notify_result_once(None, store, lock, "r", result, plan, push) for _ in range(2)))
        self.assertEqual(sorted(sent), [False, True])
        push.assert_awaited_once()
        self.assertTrue((await store.get("request-state:r"))["notified"])

    async def test_failed_publish_does_not_mark_notification_sent(self):
        store = Store()
        await store.set("input:r", {"user":"system-notify"}, secret=True)
        sent = await rq.notify_result_once(None, store, asyncio.Lock(), "r", {"message":"完成"}, {}, AsyncMock(return_value=False))
        self.assertFalse(sent)
        self.assertFalse((await store.get("request-state:r", {})).get("notified", False))

    async def test_completed_parent_passes_verified_identity_to_one_notice(self):
        store = Store()
        await store.set("input:r", {"link":"fake-link", "user":"system-notify"}, secret=True)
        plan = {"title":"测试剧", "kind":"tv", "tmdb_id":123, "cloud_account_id":1, "library_id":2}
        await store.set("request-state:r", {"plan":plan, "transfer_job_id":"child", "receipt_notified":True}, secret=True)
        result = {"message":"测试剧已入库", "inventory_verified":True, "library_name":"测试库"}
        ops = SimpleNamespace(call=AsyncMock(return_value={"status":"succeeded", "result":result}))
        jobs = SimpleNamespace(create=AsyncMock())
        push = AsyncMock()
        runner = rq.RequestRunner(SimpleNamespace(config=SimpleNamespace(dry_run=False)), store, jobs, ops,
                                  plan_body=p.PlanBody, build_plan=AsyncMock(), push=push)
        await runner.handle(Context(), {"request_ref":"r"})
        push.assert_awaited_once()
        self.assertEqual(push.await_args.kwargs["plan"]["tmdb_id"],123)
        self.assertTrue(push.await_args.kwargs["result"]["inventory_verified"])
        jobs.create.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
