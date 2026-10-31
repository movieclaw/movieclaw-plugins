"""v0.44 适配验收：完全内存网盘/宿主接口，禁止真实转存、扫描、删除与推送。"""
import asyncio
import copy
import logging
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from movieclaw_sdk.jobs import JobBlocked, JobCancelled, JobRef, JobRetry
from movieclaw_sdk.ops import OpsError, build_request

import p115api as api
import p115compat as compat
import p115cleanup as cleanup
import p115flow as flow
import p115_import as plugin
from tests import test_flow as base
Store, Context, plan = base.Store, base.Context, base.plan


class Scopes(unittest.TestCase):
    def test_single_work_only_not_root_or_staging(self):
        value=compat.cloud_scan_body(plan())
        self.assertEqual(value,{"cloud_paths":["网盘/日韩剧/剧 (2026)"]})
        self.assertNotIn("paths",value)
        from movieclaw_api.schemas.library import ScanStartPayload
        self.assertEqual(ScanStartPayload(**value).cloud_paths,value["cloud_paths"])

    def test_invalid_scope_never_becomes_full_scan(self):
        for key,value in (("cloud_root_path",""),("cloud_root_path","/"),("entry_dir_name",""),
                          ("entry_dir_name",".."),("entry_dir_name",".p115-import"),
                          ("entry_dir_name","../其他剧"),("entry_dir_name","abc\\def"),
                          ("cloud_root_path","网盘/../电影")):
            with self.subTest(key=key,value=value),self.assertRaises(JobBlocked):
                compat.cloud_scan_body({**plan(),key:value})

    def test_trailing_root_slash_and_chinese_punctuation(self):
        value=compat.cloud_scan_body({**plan(),"cloud_root_path":"/影视基地/国产剧/","entry_dir_name":"剧·名 [tmdb=1]"})
        self.assertEqual(value["cloud_paths"],["/影视基地/国产剧/剧·名 [tmdb=1]"])

    def test_sdk_optional_body_serializes_cloud_paths(self):
        path,query,body=build_request("/api/v1/libraries/{library_id}/scan",{"library_id":"path","body":"body"},
                                     ("library_id",),{"library_id":12,"body":compat.cloud_scan_body(plan())},label="scan")
        self.assertEqual(path,"/api/v1/libraries/12/scan")
        self.assertEqual(query,{})
        self.assertEqual(body,{"cloud_paths":["网盘/日韩剧/剧 (2026)"]})


class ScopeFlow(unittest.IsolatedAsyncioTestCase):
    def setup_flow(self):
        return base.Pipeline().setup_flow()

    async def test_busy_once_waits_without_spending_retry(self):
        pipeline,pan,ops,store,context=self.setup_flow()
        original=ops.call
        count=0
        async def call(name,args):
            nonlocal count
            if name=="library.scan.start":
                count+=1
                if count==1:
                    raise OpsError(409,"BUSY","正在扫描")
            return await original(name,args)
        ops.call=call
        pipeline.sleep=AsyncMock()
        result=await pipeline.run()
        self.assertTrue(result["inventory_verified"])
        self.assertEqual(pan.receives,1)
        self.assertEqual(count,2)
        self.assertEqual(result["scan_scope"],["网盘/日韩剧/剧 (2026)"])

    async def test_busy_wait_can_be_cancelled_without_another_scan(self):
        pipeline,pan,ops,store,context=self.setup_flow()
        original=ops.call
        async def call(name,args):
            if name=="library.scan.start":
                raise OpsError(409,"BUSY","忙")
            return await original(name,args)
        async def sleep(seconds):
            context.cancel=True
            await context.raise_if_cancelled()
        ops.call=call; pipeline.sleep=sleep
        with self.assertRaises(JobCancelled):
            await pipeline.run()
        self.assertEqual(pan.receives,1)
        self.assertEqual(ops.deleted,[])

    async def test_invalid_cloud_scope_rejected_without_full_scan_fallback(self):
        pipeline,pan,ops,store,context=self.setup_flow()
        original=ops.call
        scan_args=[]
        async def call(name,args):
            if name=="library.scan.start":
                scan_args.append(args)
                raise OpsError(400,"BAD_REQUEST","不在库根下")
            return await original(name,args)
        ops.call=call
        with self.assertRaises(JobBlocked):
            await pipeline.run()
        self.assertEqual(len(scan_args),1)
        self.assertIn("cloud_paths",scan_args[0]["body"])
        self.assertEqual(pan.receives,1)
        self.assertEqual(ops.deleted,[])

    async def test_missing_prior_scan_id_is_replaced(self):
        pipeline,pan,ops,store,context=self.setup_flow()
        pipeline.state={"scan_job_id":"expired","moved":{"one":{"path":"unindexed","size":100,"season":1,"episode":1}}}
        original=ops.call
        async def call(name,args):
            if name=="jobs.show" and args["job_id"]=="expired":
                raise OpsError(404,"NOT_FOUND","过保留期")
            return await original(name,args)
        ops.call=call
        with self.assertRaises(JobRetry):
            await pipeline.verify_inventory()
        self.assertTrue(any(name=="library.scan.start" for name,args in ops.calls))
        self.assertNotIn("scan_job_id",pipeline.state)

    async def test_finished_scan_without_exact_inventory_never_verified(self):
        pipeline,pan,ops,store,context=self.setup_flow()
        ops.visible=False
        with self.assertRaises(JobRetry):await pipeline.run()
        self.assertEqual(ops.deleted,[])
        self.assertNotIn("result",await store.get(pipeline.key))

    async def test_wrong_size_inventory_never_authorizes_deletion(self):
        pipeline,pan,ops,store,context=self.setup_flow()
        original=ops.inventory
        async def inventory(*args):
            media,rows=await original(*args)
            for row in rows:row["size"]+=1
            return media,rows
        pipeline.existing=inventory
        with self.assertRaises(JobRetry):await pipeline.run()
        self.assertEqual(ops.deleted,[])


class Polling(unittest.IsolatedAsyncioTestCase):
    async def test_scan_poll_backs_off_and_resets_on_change(self):
        statuses=[{"status":"running","revision":1}]*3+[{"status":"running","revision":2}]
        statuses+=[{"status":"succeeded","revision":3}]
        ops=SimpleNamespace(call=AsyncMock(side_effect=statuses))
        with patch.object(flow,"cancellable_sleep",new=AsyncMock()) as pause:
            result=await flow.wait_scan(ops,"scan",Context(),deadline=30)
        self.assertEqual(result["status"],"succeeded")
        self.assertEqual([c.args[0] for c in pause.await_args_list],[1,2,4,1])
        self.assertTrue(all(c.args[0]=="jobs.show" for c in ops.call.await_args_list))

    async def test_cancel_before_query_never_calls_host(self):
        ops=SimpleNamespace(call=AsyncMock())
        with self.assertRaises(JobCancelled):await flow.wait_scan(ops,"scan",Context(cancel=True))
        ops.call.assert_not_awaited()

    async def test_cloud_unchanged_interval_is_bounded(self):
        self.assertEqual([compat.poll_interval(i,cloud=True) for i in range(5)],[15,30,45,60,60])
        self.assertEqual(compat.poll_interval(999),10)


class Naming(unittest.TestCase):
    def details(self):
        return {"title":{"title":"英文显示名","original_title":"原始片名"},
                "metadata":{"chinese_title":"中文库名","english_title":"English Library Title"}}

    def test_english_reader_chinese_library_uses_chinese(self):
        self.assertEqual(compat.naming_title(self.details(),["zh-CN","en-US"],"en"),"中文库名")

    def test_chinese_reader_english_library_uses_english(self):
        self.assertEqual(compat.naming_title(self.details(),["en-US"],"zh-Hans"),"English Library Title")

    def test_global_empty_uses_effective_language(self):
        scrape={"setting":{"language_priority":[]},"effective":{"language_priority":["en-US"]}}
        self.assertEqual(compat.library_languages(scrape),["en-US"])

    def test_library_empty_override_follows_global(self):
        scrape={"setting":{"language_priority":["zh-CN","en-US"]},"effective":{"language_priority":["en-US"]}}
        self.assertEqual(compat.library_languages(scrape,{"language_priority":[]}),["zh-CN","en-US"])
        self.assertEqual(compat.library_languages(scrape,{"language_priority":["en-US"]}),["en-US"])

    def test_missing_chinese_translation_uses_configured_fallback(self):
        data=self.details();data["metadata"]["chinese_title"]=None
        self.assertEqual(compat.naming_title(data,["zh-CN","en-US"],"en"),"English Library Title")

    def test_unavailable_translation_never_uses_wrong_display_language(self):
        data={"title":{"title":"Chinese display","original_title":"Original"},"metadata":{}}
        self.assertEqual(compat.naming_title(data,["en-US"],"zh-Hans"),"Original")

    def test_unavailable_third_language_stops_not_wrongly_renames(self):
        for language in ("ja-JP","zh-TW","fr-FR"):
            with self.subTest(language=language),self.assertRaises(JobBlocked):
                compat.naming_title(self.details(),[language,"en-US"],"zh-Hans")
        self.assertEqual(compat.naming_title(self.details(),["ja-JP"],needs_title=False),"原始片名")

    def test_plan_entry_uses_naming_title_not_display(self):
        target={"title":"显示名","naming_title":"English Library Title","kind":"tv","year":2026,
                "tmdb_id":1,"templates":plugin.naming.Templates(),"naming_context":{}}
        result=plugin._plan_entry(target,rel_path="Show.S01E01.mkv",size=100,
                                  name="Show.S01E01.mkv",unit=(1,1))
        self.assertIn("English Library Title",result["file_name"])
        self.assertNotIn("显示名",result["file_name"])


class TargetLanguage(unittest.IsolatedAsyncioTestCase):
    async def test_target_directory_and_files_use_library_language(self):
        details={"title":{"title":"界面中文名","original_title":"Original","release_year":2026},
                 "metadata":{"english_title":"English Name"},"library_links":[]}
        async def call(name,args):
            if name=="library.list":return [{"id":12,"name":"库","scrape_overrides":{"language_priority":["en-US"]},
                 "cloud_folders":[{"id":1,"account_id":1,"backend":"cloud-115:115","remote_dir_id":"root","display_path":"网盘/库","is_primary":True}]}]
            if name=="scrape.show":return {"setting":{"language_priority":["zh-CN"],"naming_entry_dir":"{title}-{year}-[tmdb={tmdb_id}]"}}
            if name=="discover.get-title-details":return details
            raise AssertionError(name)
        result=await plugin._target(SimpleNamespace(locale="zh-Hans"),SimpleNamespace(call=call),kind="tv",title="输入名",year=2026,tmdb_id=1,
                                    resolved={"status":"ready","kind":"tv","tmdb_id":1,"library_id":12,"title":"输入名"})
        self.assertEqual(result["title"],"界面中文名")
        self.assertEqual(result["naming_title"],"English Name")
        self.assertEqual(result["entry_dir_name"],"English Name-2026-[tmdb=1]")
        self.assertEqual(result["naming_languages"],["en-US"])


class ErrorClassification(unittest.IsolatedAsyncioTestCase):
    async def test_http_429_respects_bounded_retry_after(self):
        for value,expected in (("120",120),("90000",3600),("-1",1),("invalid",60)):
            with self.subTest(value=value),self.assertRaises(api.RateLimited) as caught:
                api.check_http(SimpleNamespace(status_code=429,headers={"Retry-After":value}))
            self.assertEqual(caught.exception.retry_after,expected)
            self.assertNotIsInstance(caught.exception,api.AuthExpired)

    async def test_explicit_risk_text_not_login_expired(self):
        with self.assertRaises(api.RiskControl):api._check({"state":False,"errno":911,"message":"请完成安全验证"})
        with self.assertRaises(api.RateLimited):api._check({"state":False,"errno":99,"message":"操作太频繁"})
        with self.assertRaises(api.AuthExpired):api._check({"state":False,"errno":99,"message":"登录失效"})

    async def test_openapi_rate_limit_does_not_refresh_token(self):
        pan=api.Pan(Store())
        pan.open_token=AsyncMock(return_value="fake-non-production-token")
        pan._request=AsyncMock(return_value=SimpleNamespace(status_code=429,headers={"Retry-After":"90"}))
        with self.assertRaises(api.RateLimited):await pan.open_json("GET","/open/user/info")
        pan.open_token.assert_awaited_once_with()
        pan._request.assert_awaited_once()

    async def test_success_and_server_failures_remain_distinct(self):
        self.assertEqual(api._check({"state":True,"data":{}}),{"state":True,"data":{}})
        api.check_http(SimpleNamespace(status_code=200))
        with self.assertRaises(api.ApiError) as caught:api.check_http(SimpleNamespace(status_code=503))
        self.assertNotIsInstance(caught.exception,(api.AuthExpired,api.RateLimited))


class CleanupOld(unittest.IsolatedAsyncioTestCase):
    async def setup_case(self):
        self.store=Store();self.context=Context();self.deleted=[];self.dry_calls=0
        self.old={"file_id":9,"name":"old.mkv","remote_path":"网盘/日韩剧/剧 (2026)/Season 01/old.mkv",
                  "size":80,"season":1,"episode":1,"missing":False,"cloud_only":True}
        self.moved={"name":"new.mkv","path":"网盘/日韩剧/剧 (2026)/Season 01/new.mkv","size":100,"season":1,"episode":1}
        self.rows=[copy.deepcopy(self.old),{"file_id":10,"remote_path":self.moved["path"],"name":"new.mkv",
                                            "size":100,"season":1,"episode":1,"missing":False}]
        self.plan=plan(old=[copy.deepcopy(self.old)])
        self.result={"inventory_verified":True,"media_item_id":1594,"moved":[self.moved],"pending_delete":[self.old],"title":"剧"}
        self.state={"moved":{"1":self.moved},"items":{},"deleted":[],"result":self.result}
        await self.store.set("transfer:r1",self.plan,secret=True)
        await self.store.set(cleanup.flow_key(self.plan),self.state,secret=True)
        self.job={"id":"original","job_type":"p115-import:transfer","status":"succeeded",
                  "input_data":{"plan_ref":"transfer:r1"},"result":self.result}
        self.native={"dry_run":True,"removed_paths":[],
                     "rows_deleted":1,"freed_bytes":80,"errors":[]}
        self.folder={"account_id":1,"remote_dir_id":"root","display_path":"网盘/日韩剧"}
        async def call(name,args):
            if name=="jobs.show":return copy.deepcopy(self.job)
            if name=="library.list":return [{"id":12,"cloud_folders":[self.folder]}]
            if name=="library.items.delete-file":
                if args.get("dry_run"):
                    self.dry_calls+=1;return copy.deepcopy(self.native)
                self.deleted.append(args["file_id"])
                self.rows=[r for r in self.rows if r["file_id"]!=args["file_id"]]
                return {"removed_paths":self.native["removed_paths"],"rows_deleted":1,"errors":[]}
            raise AssertionError(name)
        self.ops=SimpleNamespace(call=AsyncMock(side_effect=call))
        self.jobs=SimpleNamespace(create=AsyncMock(return_value=JobRef("cleanup1",cleanup.JOB,"queued",True)))
        async def existing(*args):return 1594,copy.deepcopy(self.rows)
        self.existing=existing
        async def restore(plan,state,context,approved_paths):return [],state
        self.restore=AsyncMock(side_effect=restore)
        return await cleanup.preview(self.ops,self.store,"original",self.existing)

    async def authorize(self,preview):
        return await cleanup.authorize(self.ops,self.store,self.jobs,"original",[9],preview["approval_token"],asyncio.Lock())

    async def execute(self,preview):
        return await cleanup.execute(self.ops,self.store,{"approval_ref":cleanup.approval_ref(preview["approval_token"])},
                                     self.context,self.existing,self.restore)

    async def test_cloud_only_preview_shows_inventory_path_when_native_paths_empty(self):
        preview=await self.setup_case()
        self.assertEqual(preview["status"],"preview")
        self.assertEqual(preview["files"][0]["removed_paths"],[self.old["remote_path"]])
        self.assertEqual(self.deleted,[])
        self.jobs.create.assert_not_awaited()
        self.assertTrue(all(secret for key,secret in self.store.secrets if key.startswith("delete-approval:")))

    async def test_confirmed_cleanup_restores_new_canonical_name_and_reverifies(self):
        await self.setup_case()
        self.old["name"]="剧.2026.S01E01.mkv"
        self.old["remote_path"]="网盘/日韩剧/剧 (2026)/Season 01/"+self.old["name"]
        self.plan=plan(old=[copy.deepcopy(self.old)])
        self.store=Store()
        pipeline,pan,ops,store,context=base.Pipeline().setup_flow(self.plan,store=self.store)
        ops.old=[copy.deepcopy(self.old)]
        pan.files["target"]=[api.Entry("9",self.old["name"],False,80)]
        imported=await pipeline.run()
        self.assertEqual(ops.deleted,[])
        self.assertIn(" - p115-",imported["moved"][0]["name"])
        await store.set("transfer:r1",self.plan,secret=True)
        original=ops.call
        async def call(name,args):
            if name=="jobs.show" and args["job_id"]=="original":
                return {"id":"original","job_type":"p115-import:transfer","status":"succeeded",
                        "input_data":{"plan_ref":"transfer:r1"},"result":imported}
            if name=="library.list":return [{"id":12,"cloud_folders":[self.folder]}]
            if name=="library.items.delete-file" and args.get("dry_run"):
                return {"dry_run":True,"rows_deleted":1,"removed_paths":[],"errors":[]}
            if name=="library.items.delete-file":
                await original(name,args)
                return {"rows_deleted":1,"errors":[]}
            return await original(name,args)
        wrapped=SimpleNamespace(call=call)
        preview=await cleanup.preview(wrapped,store,"original",ops.inventory)
        self.assertEqual(preview["files"][0]["removed_paths"],[self.old["remote_path"]])
        await cleanup.authorize(wrapped,store,self.jobs,"original",[9],preview["approval_token"],asyncio.Lock())
        async def restore(pl,state,ctx,approved_paths):
            resumed=flow.Flow(SimpleNamespace(logger=logging.getLogger("test")),pan,wrapped,store,ctx,pl,existing=ops.inventory)
            resumed.state=state
            renamed=await resumed.restore_names(allowed_paths=approved_paths)
            await resumed.verify_inventory()
            return renamed,resumed.state
        result=await cleanup.execute(wrapped,store,{"approval_ref":cleanup.approval_ref(preview["approval_token"])},
                                     context,ops.inventory,restore)
        self.assertEqual(ops.deleted,[9])
        self.assertEqual(result["renamed"],1)
        self.assertEqual(pan.files["target"][0].name,"剧.2026.S01E01.mkv")
        latest=await store.get("request-result:r1")
        self.assertTrue(latest["inventory_verified"])
        self.assertEqual(latest["pending_delete"],[])
        self.assertEqual(latest["moved"][0]["name"],"剧.2026.S01E01.mkv")
        self.assertEqual(latest["name_restore_pending"],[])

    async def test_local_copy_requires_native_delete_ui_for_full_sidecar_confirmation(self):
        await self.setup_case()
        self.rows[0]["cloud_only"]=False
        preview=await cleanup.preview(self.ops,self.store,"original",self.existing)
        self.assertEqual(preview["status"],"nothing_to_do")
        self.assertIn("原生删除",preview["retained"][0]["reason"])
        self.assertEqual(self.deleted,[])

    async def test_explicit_exact_ids_authorize_and_execute_once(self):
        preview=await self.setup_case()
        await self.authorize(preview)
        self.assertEqual(set(self.jobs.create.call_args.kwargs["input"]),{"approval_ref"})
        self.assertNotIn(preview["approval_token"],str(self.jobs.create.call_args.kwargs["input"]))
        result=await self.execute(preview)
        self.assertEqual(result["deleted"],[9]);self.assertEqual(self.deleted,[9])
        self.assertEqual((await self.execute(preview)),result)
        self.assertEqual(self.deleted,[9])
        saved=await self.store.get("request-result:r1")
        self.assertTrue(saved["inventory_verified"]);self.assertEqual(saved["pending_delete"],[])

    async def test_duplicate_confirmation_does_not_duplicate_task(self):
        preview=await self.setup_case()
        await self.authorize(preview);second=await self.authorize(preview)
        self.assertFalse(second["job_created"])
        self.jobs.create.assert_awaited_once()

    async def test_missing_or_wrong_confirmation_does_not_enqueue(self):
        preview=await self.setup_case()
        for ids,token in (([],preview["approval_token"]),([8],preview["approval_token"]),([9,9],preview["approval_token"]),([9],"wrong")):
            with self.subTest(ids=ids,token=token),self.assertRaises(JobBlocked):
                await cleanup.authorize(self.ops,self.store,self.jobs,"original",ids,token,asyncio.Lock())
        self.jobs.create.assert_not_awaited();self.assertEqual(self.deleted,[])

    async def test_other_request_cannot_reuse_token(self):
        preview=await self.setup_case()
        with self.assertRaises(JobBlocked):
            await cleanup.authorize(self.ops,self.store,self.jobs,"other",[9],preview["approval_token"],asyncio.Lock())
        self.jobs.create.assert_not_awaited()

    async def test_expired_preview_rejected(self):
        preview=await self.setup_case()
        key=cleanup.approval_ref(preview["approval_token"])
        saved=await self.store.get(key);saved["expires_at"]=time.time()-1;await self.store.set(key,saved,secret=True)
        with self.assertRaises(JobBlocked):await self.authorize(preview)
        with self.assertRaises(JobBlocked):await self.execute(preview)
        self.assertEqual(self.deleted,[])

    async def test_changed_old_file_is_never_deleted(self):
        preview=await self.setup_case();await self.authorize(preview)
        self.rows[0]["size"]=200
        with self.assertRaises(JobBlocked):await self.execute(preview)
        self.assertEqual(self.deleted,[])

    async def test_missing_or_wrong_episode_new_file_preserves_old(self):
        for change in ({"missing":True},{"episode":2},{"size":70},{"remote_path":"other"}):
            with self.subTest(change=change):
                preview=await self.setup_case();await self.authorize(preview)
                self.rows[1].update(change)
                with self.assertRaises(JobBlocked):await self.execute(preview)
                self.assertEqual(self.deleted,[])

    async def test_new_sidecar_file_after_preview_is_not_implicitly_authorized(self):
        preview=await self.setup_case();await self.authorize(preview)
        self.native["removed_paths"].append("new-subtitle.srt")
        with self.assertRaises(JobBlocked):await self.execute(preview)
        self.assertEqual(self.deleted,[])

    async def test_changed_library_mount_rejects_old_plan(self):
        preview=await self.setup_case();await self.authorize(preview)
        self.folder["remote_dir_id"]="other-root"
        with self.assertRaises(JobBlocked):await self.execute(preview)
        self.assertEqual(self.deleted,[])

    async def test_cancelled_cleanup_never_deletes(self):
        preview=await self.setup_case();await self.authorize(preview)
        self.context.cancel=True
        with self.assertRaises(JobCancelled):await self.execute(preview)
        self.assertEqual(self.deleted,[])

    async def test_unverified_or_foreign_task_rejected(self):
        await self.setup_case()
        for key,value in (("status","running"),("job_type","library.scan")):
            original=self.job[key];self.job[key]=value
            with self.assertRaises(JobBlocked):await cleanup.preview(self.ops,self.store,"original",self.existing)
            self.job[key]=original
        self.assertEqual(self.deleted,[])

    async def test_new_version_smaller_or_other_season_is_not_cleanup_candidate(self):
        await self.setup_case()
        self.rows[1]["season"]=2
        result=await cleanup.preview(self.ops,self.store,"original",self.existing)
        self.assertEqual(result["status"],"nothing_to_do")
        self.assertEqual(len(result["retained"]),1)
        self.assertEqual(self.deleted,[])

    async def test_response_lost_after_delete_does_not_delete_twice(self):
        preview=await self.setup_case();await self.authorize(preview)
        original=self.ops.call
        first=True
        async def call(name,args):
            nonlocal first
            result=await original(name,args)
            if name=="library.items.delete-file" and not args.get("dry_run") and first:
                first=False;raise OpsError(502,"NETWORK","响应丢失")
            return result
        self.ops=SimpleNamespace(call=call)
        with self.assertRaises(OpsError):await self.execute(preview)
        result=await self.execute(preview)
        self.assertEqual(self.deleted,[9]);self.assertEqual(result["deleted"],[9])


if __name__=="__main__":unittest.main(verbosity=2)
