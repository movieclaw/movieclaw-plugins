"""链路自测：完全内存网盘/宿主任务；不访问真实媒体、不转存、不删除。"""
import asyncio
import copy
import logging
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from movieclaw_sdk.jobs import JobRetry,JobBlocked,JobCancelled,JobRef
from movieclaw_sdk.ops import OpsError
from fastapi import HTTPException

import p115api as api
import p115flow as f
import p115requests as rq
import p115_import as p
import p115naming as n
import p115links as links


class Store:
    def __init__(self):self.data={};self.secrets=[]
    async def get(self,key,default=None):return copy.deepcopy(self.data.get(key,default))
    async def set(self,key,value,secret=False):
        self.data[key]=copy.deepcopy(value)
        self.secrets.append((key,secret))
    async def delete(self,key):self.data.pop(key,None)


class Context:
    job_id="job_test"
    def __init__(self,cancel=False):self.messages=[];self.cancel=cancel
    async def raise_if_cancelled(self):
        if self.cancel:raise JobCancelled()
    async def update_progress(self,**kw):self.messages.append(kw)


class Pan:
    def __init__(self):
        self.files={"root":[],"staging":[],"target":[]}
        self.receives=0;self.moves=0;self.renames=0;self.deletes=[];self.ensures=[]
        self.fail_move=False;self.crash_after_move=False;self.empty=False;self.later=[]
        self.sources=[];self.tasks=[]
    async def ensure_dir(self,base,parts):
        self.ensures.append((base,tuple(parts)))
        return "staging" if parts[0]==".p115-import" else "target"
    async def list_dir(self,cid):return copy.deepcopy(self.files.get(cid,[]))
    async def share_receive(self,code,pwd,ids,cid):
        self.receives+=1
        if not self.empty:self.files[cid].extend(copy.deepcopy(self.sources))
        return {"state":True,"data":{"recv_file_count":len(ids),"recv_folder_count":0}}
    async def rename(self,fid,name):
        self.renames+=1
        for rows in self.files.values():
            for e in rows:
                if e.remote_id==fid:e.name=name
    async def rename_many(self,pairs):
        self.renames+=1
        for fid,name in pairs:
            for rows in self.files.values():
                for e in rows:
                    if e.remote_id==str(fid):e.name=name
    async def move(self,ids,cid):
        self.moves+=1
        if self.fail_move:
            self.fail_move=False;raise api.ApiError("模拟网络异常")
        for key,rows in self.files.items():
            for e in list(rows):
                if e.remote_id in ids:
                    rows.remove(e);self.files[cid].append(e)
        if self.crash_after_move:
            self.crash_after_move=False;raise api.ApiError("模拟移动成功但响应丢失")
    async def delete(self,ids):
        self.deletes.extend(ids)
        for cid in ids:self.files.pop(cid,None)
        for key,entries in self.files.items():
            self.files[key]=[entry for entry in entries if entry.remote_id not in ids]
    async def offline_tasks(self,page=1):return {"tasks":self.tasks,"page_count":1}
    async def offline_add(self,urls,cid):
        self.receives+=1
        if not self.empty:self.files[cid].extend(copy.deepcopy(self.sources))


def plan(num=1,old=None,source="share"):
    items=[]
    for ep in range(1,num+1):
        items.append({"share_id":str(ep),"share_path":f"Show.S01E{ep:02}.mkv","size":100,
            "sha1":str(ep)*40,"season":1,"episode":ep,"season_dir":"Season 01",
            "file_name":f"剧.2026.S01E{ep:02}.mkv","action":"replace","subs":[],
            "existing":old or [],"source":source,"info_hash":str(ep)*32})
    return {"request_ref":"r1","title":"剧","tmdb_id":1,"kind":"tv","library_id":12,
        "library_name":"日韩剧","cloud_account_id":1,"cloud_root_id":"root","cloud_root_path":"网盘/日韩剧",
        "entry_dir_name":"剧 (2026)","staging_dir_name":".p115-import/r1","receive_ids":[i["share_id"] for i in items],
        "share_code":"abc","receive_code":"TESTPASSWORD","total_new_bytes":num*100,
        "items":items,"old_files":old or [],"skipped":[]}


class Ops:
    def __init__(self):self.calls=[];self.scanned=False;self.failed=False;self.visible=True;self.old=[];self.deleted=[];self.pan=None
    async def call(self,name,args,**kw):
        self.calls.append((name,copy.deepcopy(args)))
        if name=="library.scan.start":return {"job_id":"job_scan"}
        if name=="jobs.wait":
            self.scanned=True
            state={"id":"job_scan","status":"failed" if self.failed else "succeeded","revision":3,
                   "error":{"message":"模拟扫描失败"} if self.failed else None}
            return {"changed":True,"job":state}
        if name=="jobs.show":
            self.scanned=True
            return {"id":"job_scan","status":"failed" if self.failed else "succeeded",
                    "error":{"message":"模拟扫描失败"} if self.failed else None}
        if name=="library.items.delete-file":
            self.deleted.append(args["file_id"])
            self.old=[r for r in self.old if r["file_id"]!=args["file_id"]]
            for key,entries in self.pan.files.items():
                self.pan.files[key]=[e for e in entries if e.remote_id!=str(args["file_id"])]
            return {}
        raise AssertionError(name)
    async def inventory(self,ops,lib,tmdb,kind):
        rows=copy.deepcopy(self.old)
        if self.scanned and self.visible:
            for e in self.pan.files.get("target",[]):
                if e.is_dir or not n.is_video(e.name):continue
                import re
                m=re.search(r"E(\d+)",e.name)
                rows.append({"file_id":int(e.remote_id)+1000,"name":e.name,"size":e.size,
                    "season":1,"episode":int(m.group(1)) if m else 1,"missing":False,
                    "remote_path":"网盘/日韩剧/剧 (2026)/Season 01/"+e.name})
        return (1594 if rows else None),rows


class Pure(unittest.TestCase):
    def test_detached_extraction_code_attached(self):
        self.assertEqual(links.split_inputs("剧 https://115cdn.com/s/abc 提取码：1234"),["https://115cdn.com/s/abc?password=1234"])
    def test_multiple_share_passwords_not_mixed(self):
        result=links.split_inputs("https://115.com/s/abc 提取码1111\nhttps://115.com/s/def 提取码2222")
        self.assertIn("password=1111",result[0]);self.assertIn("password=2222",result[1])
    def test_offline_batch_preserved(self):
        a="ed2k://|file|A.S01E01.mkv|10|"+"a"*32+"|/"
        b="ed2k://|file|A.S01E02.mkv|10|"+"b"*32+"|/"
        self.assertEqual(links.split_inputs(a+"\n"+b),[a+"\n"+b])
    def test_existing_password_not_replaced(self):
        self.assertEqual(links.split_inputs("https://115.com/s/abc?password=1111 提取码2222"),["https://115.com/s/abc?password=1111"])
    def test_mixed_ed2k_and_magnet_batch_keeps_both(self):
        text="ed2k://|file|A.S01E01.mkv|10|"+"a"*32+"|/\nmagnet:?xt=urn:btih:"+"b"*40+"&dn=A.S01E02.mkv"
        self.assertEqual(len(api.parse_offline(links.split_inputs(text)[0])),2)
    def test_lookalike_115_domain_rejected(self):
        with self.assertRaises(ValueError):api.parse_share_link("https://115.example.com/s/abc")
    def test_plain_share_code_compatibility(self):
        self.assertEqual(links.split_inputs("abc-1234"),["abc-1234"])
    def test_equivalent_share_domains_dedupe(self):
        self.assertEqual(f.input_key("https://115.com/s/abc?password=1234"),
                         f.input_key("https://115cdn.com/s/abc?password=1234"))
    def test_different_magnets_do_not_use_empty_share_key(self):
        self.assertNotEqual(f.input_key("magnet:?xt=urn:btih:"+"a"*40),f.input_key("magnet:?xt=urn:btih:"+"b"*40))
    def test_reordered_offline_batch_dedupe(self):
        a="magnet:?xt=urn:btih:"+"a"*40;b="magnet:?xt=urn:btih:"+"b"*40
        self.assertEqual(f.input_key(a+"\n"+b),f.input_key(b+"\n"+a))
    def test_password_changes_not_collapsed(self):
        self.assertNotEqual(f.input_key("abc-1111"),f.input_key("abc-2222"))
    def test_receipt_not_provided_is_not_zero(self):
        for receipt in ({"data":0},{"data":{}},{"data":{"receive_size":10}}):
            self.assertFalse(f.receipt_empty(receipt))
        self.assertTrue(f.receipt_empty({"data":{"recv_file_count":0,"recv_folder_count":0}}))
    def test_hash_matching_same_name_other_season(self):
        item=plan()["items"][0]
        a=api.Entry("1","01.mkv",False,100,"1"*40)
        b=api.Entry("2","01.mkv",False,100,"2"*40)
        self.assertEqual(f.choose_source(item,[b,a],set()).remote_id,"1")
    def test_hash_mismatch_does_not_match_by_size(self):
        item=plan()["items"][0]
        self.assertIsNone(f.choose_source(item,[api.Entry("1",item["share_path"],False,100,"2"*40)],set()))
    def test_duplicate_name_no_hash_fails_closed(self):
        item={"share_path":"01.mkv","size":100}
        with self.assertRaises(JobBlocked):
            f.choose_source(item,[api.Entry("1","01.mkv",False,100),api.Entry("2","01.mkv",False,100)],set())
    def test_largest_batch_version_only(self):
        a=plan()["items"][0];b=dict(a,share_id="2",size=200)
        chosen,skipped=f.selected_items([a,b],"tv")
        self.assertEqual(chosen,[b]);self.assertEqual(skipped,[a])
    def test_unknown_size_multi_version_needs_confirmation(self):
        a=plan()["items"][0];b=dict(a,share_id="2",size=0)
        with self.assertRaises(ValueError):f.selected_items([a,b],"tv")
    def test_inventory_verification_not_just_any_other_episode(self):
        moved={"path":"a","size":100,"season":1,"episode":1}
        rows=[{"remote_path":"a","size":100,"season":1,"episode":2,"missing":False}]
        self.assertEqual(f.matching_rows([moved],rows)[1],["a"])
    def test_inventory_missing_file_is_not_verified(self):
        moved={"path":"a","size":100,"season":1,"episode":1}
        rows=[{"remote_path":"a","size":100,"season":1,"episode":1,"missing":True}]
        self.assertEqual(f.matching_rows([moved],rows)[1],["a"])
    def test_naming_renderer_is_exact_core_function(self):
        from movieclaw_api.services.library.naming import render
        self.assertIs(n.render,render)
    def test_library_template_overrides_global(self):
        t=n.Templates.from_settings({"naming_entry_dir":"{title}-{year}"},{"naming_entry_dir":"{original_title} [{tmdb_id}]"})
        self.assertEqual(n.entry_name(t,"中文",2026,9,original_title="Original"),"Original [9]")
    def test_batch_season_consensus(self):
        result=n.batch_units(["show/S02E01.mkv","show/E02.mkv"],known_seasons=[1,2])
        self.assertEqual(result["show/E02.mkv"],(2,2))
    def test_unknown_season_not_default_first(self):
        with patch("movieclaw_api.services.library.units.enrich",return_value=SimpleNamespace(seasons=[],episodes=[])):
            self.assertIsNone(n.batch_units(["show/E02.mkv"])["show/E02.mkv"])
    def test_m4v_rmvb_follow_core_extensions(self):
        self.assertTrue(n.is_video("a.m4v"));self.assertTrue(n.is_video("a.rmvb"))
    def test_nested_wait_schema(self):
        self.assertEqual(f.terminal_job({"changed":True,"job":{"status":"succeeded"}})["status"],"succeeded")


class Pipeline(unittest.IsolatedAsyncioTestCase):
    def setup_flow(self,pl=None,pan=None,ops=None,store=None,context=None):
        pl=pl or plan();pan=pan or Pan();ops=ops or Ops();store=store or Store();context=context or Context()
        ops.pan=pan
        if not pan.sources:
            pan.sources=[api.Entry(str(i+1),item["share_path"],False,item["size"],item["sha1"])
                         for i,item in enumerate(pl["items"])]
        ctx=SimpleNamespace(logger=logging.getLogger("test"))
        return f.Flow(ctx,pan,ops,store,context,pl,existing=ops.inventory,wait_seconds=0,scan_seconds=1),pan,ops,store,context

    async def test_complete_chain_always_scans_and_verifies(self):
        pipeline,pan,ops,store,context=self.setup_flow(plan(10))
        result=await pipeline.run()
        self.assertTrue(result["inventory_verified"])
        self.assertEqual(len(result["moved"]),10)
        self.assertEqual(sum(name=="library.scan.start" for name,_ in ops.calls),1)
        self.assertFalse(any(name=="jobs.wait" for name,_ in ops.calls))
        self.assertEqual(len(pan.ensures),2)
        self.assertEqual(pan.receives,1)
        self.assertEqual(pan.deletes,["staging"])

    async def test_empty_staging_parent_removed_after_batch(self):
        pipeline,pan,ops,store,context=self.setup_flow()
        pan.files["root"]=[api.Entry("holder",".p115-import",True)]
        pan.files["holder"]=[api.Entry("staging","r1",True)]
        result=await pipeline.run()
        self.assertTrue(result["inventory_verified"])
        self.assertEqual(pan.deletes,["staging","holder"])
        self.assertEqual(pan.files["root"],[])
        self.assertEqual(result["staging_cleanup"]["status"],"removed")

    async def test_sibling_batch_empty_directory_is_not_deleted(self):
        pipeline,pan,ops,store,context=self.setup_flow()
        pan.files["root"]=[api.Entry("holder",".p115-import",True)]
        pan.files["holder"]=[api.Entry("staging","r1",True),api.Entry("sibling","another-request",True)]
        pan.files["sibling"]=[]
        result=await pipeline.run()
        self.assertEqual(pan.deletes,["staging"])
        self.assertIn("sibling",pan.files)
        self.assertEqual(result["staging_cleanup"]["status"],"retained")

    async def test_parent_hidden_subtitle_file_is_not_empty(self):
        pipeline,pan,ops,store,context=self.setup_flow()
        pan.files["root"]=[api.Entry("holder",".p115-import",True)]
        pan.files["holder"]=[api.Entry("staging","r1",True),api.Entry("hidden",".failed.srt",False,20)]
        result=await pipeline.run()
        self.assertEqual(pan.deletes,["staging"])
        self.assertEqual(result["staging_cleanup"]["file_count"],1)
        self.assertEqual(result["staging_cleanup"]["status"],"retained")

    async def test_parent_delete_failure_reported_not_silently_ignored(self):
        pipeline,pan,ops,store,context=self.setup_flow()
        pan.files["root"]=[api.Entry("holder",".p115-import",True)]
        pan.files["holder"]=[api.Entry("staging","r1",True)]
        original=pan.delete
        async def fail(ids):
            if "holder" in ids:raise api.ApiError("模拟115清空目录失败")
            await original(ids)
        pan.delete=fail
        result=await pipeline.run()
        self.assertTrue(result["inventory_verified"])
        self.assertEqual(result["staging_cleanup"]["status"],"deferred")
        self.assertIn("清理未完成",result["message"])

    async def test_retry_after_rename_never_resubmits_or_renames_twice(self):
        pipeline,pan,ops,store,context=self.setup_flow()
        pan.fail_move=True
        with self.assertRaises(api.ApiError):await pipeline.run()
        pipeline2,*_=self.setup_flow(pan=pan,ops=ops,store=store)
        result=await pipeline2.run()
        self.assertTrue(result["inventory_verified"])
        self.assertEqual(pan.receives,1);self.assertEqual(pan.renames,1)

    async def test_move_succeeded_but_response_lost_resumes_by_file_id(self):
        pipeline,pan,ops,store,context=self.setup_flow()
        pan.crash_after_move=True
        result=await pipeline.run()
        self.assertTrue(result["inventory_verified"])
        self.assertEqual(pan.receives,1);self.assertEqual(pan.moves,1)

    async def test_partial_transfer_keeps_remaining_staging_and_never_cleans(self):
        pipeline,pan,ops,store,context=self.setup_flow(plan(2))
        pan.sources=pan.sources[:1]
        with self.assertRaises(JobRetry):await pipeline.run()
        self.assertEqual(pan.deletes,[])
        self.assertTrue(ops.scanned)

    async def test_cancel_between_rename_and_move_does_not_move_or_delete(self):
        pipeline,pan,ops,store,context=self.setup_flow()
        original=pan.rename_many
        async def rename(*args):
            await original(*args)
            context.cancel=True
        pan.rename_many=rename
        with self.assertRaises(JobCancelled):await pipeline.run()
        self.assertEqual(pan.moves,0);self.assertEqual(pan.deletes,[])
        context.cancel=False
        again,*_=self.setup_flow(pan=pan,ops=ops,store=store,context=context)
        result=await again.run()
        self.assertTrue(result["inventory_verified"]);self.assertEqual(pan.receives,1)

    async def test_cancel_before_writes(self):
        pipeline,pan,ops,store,context=self.setup_flow(context=Context(cancel=True))
        with self.assertRaises(JobCancelled):await pipeline.run()
        self.assertEqual(pan.receives,0);self.assertEqual(pan.moves,0)

    async def test_scan_failure_not_success_not_delete(self):
        pipeline,pan,ops,store,context=self.setup_flow()
        ops.failed=True
        with self.assertRaises(JobRetry):await pipeline.run()
        self.assertEqual(ops.deleted,[]);self.assertEqual(pan.deletes,[])

    async def test_scan_success_without_inventory_not_success(self):
        pipeline,pan,ops,store,context=self.setup_flow()
        ops.visible=False
        with self.assertRaises(JobRetry):await pipeline.run()
        self.assertEqual(ops.deleted,[]);self.assertEqual(pan.deletes,[])

    async def test_unknown_extra_in_staging_never_deleted(self):
        pipeline,pan,ops,store,context=self.setup_flow()
        pan.sources.append(api.Entry("88","unknown.srt",False,2))
        result=await pipeline.run()
        self.assertTrue(result["inventory_verified"])
        self.assertEqual(pan.deletes,[])
        self.assertEqual(pan.files["staging"][0].name,"unknown.srt")

    async def test_stock_improved_while_queued_skips_transfer(self):
        pipeline,pan,ops,store,context=self.setup_flow()
        ops.old=[{"file_id":9,"remote_path":"old","size":200,"season":1,"episode":1,"missing":False}]
        result=await pipeline.run()
        self.assertEqual(result["status"],"nothing_to_do")
        self.assertEqual(pan.receives,0)

    async def test_deleted_or_missing_old_inventory_does_not_block_new(self):
        pipeline,pan,ops,store,context=self.setup_flow()
        ops.old=[{"file_id":9,"remote_path":"old","size":200,"season":1,"episode":1,"missing":True}]
        result=await pipeline.run()
        self.assertTrue(result["inventory_verified"])
        self.assertEqual(pan.receives,1)

    async def test_partial_files_moved_then_resume_only_remaining(self):
        pipeline,pan,ops,store,context=self.setup_flow(plan(2))
        pan.sources=pan.sources[:1]
        with self.assertRaises(JobRetry):await pipeline.run()
        self.assertEqual(len(pan.files["target"]),1)
        item=plan(2)["items"][1]
        pan.files["staging"].append(api.Entry("2",item["share_path"],False,100,"2"*40))
        pipeline2,*_=self.setup_flow(plan(2),pan,ops,store)
        result=await pipeline2.run()
        self.assertEqual(len(result["moved"]),2);self.assertEqual(pan.receives,1)

    async def test_subtitle_failure_retained_not_destroyed(self):
        pl=plan();pl["items"][0]["subs"]=["Show.S01E01.zh.srt"]
        pipeline,pan,ops,store,context=self.setup_flow(pl)
        result=await pipeline.run()
        self.assertEqual(result["subtitle_pending"],["Show.S01E01.zh.srt"])

    async def test_submit_response_lost_does_not_repeat_submission(self):
        pipeline,pan,ops,store,context=self.setup_flow()
        original=pan.share_receive
        async def response_lost(*args):
            await original(*args)
            raise api.ApiError("网络响应丢失")
        pan.share_receive=response_lost
        with self.assertRaises(api.ApiError):await pipeline.run()
        pipeline2,*_=self.setup_flow(pan=pan,ops=ops,store=store)
        result=await pipeline2.run()
        self.assertTrue(result["inventory_verified"]);self.assertEqual(pan.receives,1)

    async def test_no_file_yet_retry_preserves_staging(self):
        pipeline,pan,ops,store,context=self.setup_flow();pan.empty=True
        with self.assertRaises(JobRetry):await pipeline.run()
        self.assertEqual(pan.receives,1);self.assertEqual(pan.deletes,[])
        pipeline2,*_=self.setup_flow(pan=pan,ops=ops,store=store)
        with self.assertRaises(JobRetry):await pipeline2.run()
        self.assertEqual(pan.receives,1)

    async def test_subtitle_renamed_then_move_failure_recovered(self):
        pl=plan();pl["items"][0]["subs"]=["Show.S01E01.zh.srt"]
        pipeline,pan,ops,store,context=self.setup_flow(pl)
        pan.sources.append(api.Entry("88","Show.S01E01.zh.srt",False,3))
        original=pan.move;fail={"once":True}
        async def move(ids,cid):
            if ids==["88"] and fail["once"]:
                fail["once"]=False;raise api.ApiError("字幕移动暂时失败")
            await original(ids,cid)
        pan.move=move
        result=await pipeline.run()
        self.assertEqual(result["subtitle_pending"],[])
        self.assertTrue(any(e.name=="剧.2026.S01E01.zh.srt" for e in pan.files["target"]))

    async def test_terminal_result_is_idempotent(self):
        pipeline,pan,ops,store,context=self.setup_flow()
        first=await pipeline.run();second=await pipeline.run()
        self.assertEqual(first,second);self.assertEqual(pan.receives,1)

    async def test_same_name_without_inventory_never_overwrites(self):
        pipeline,pan,ops,store,context=self.setup_flow()
        pan.files["target"]=[api.Entry("7",plan()["items"][0]["file_name"],False,80)]
        with self.assertRaises(JobBlocked):await pipeline.run()
        self.assertEqual(pan.receives,0);self.assertEqual(ops.deleted,[])

    async def test_verified_larger_file_retains_old_until_separate_confirmation(self):
        old={"file_id":9,"name":"剧.2026.S01E01.mkv","size":80,"season":1,"episode":1,
             "remote_path":"网盘/日韩剧/剧 (2026)/Season 01/剧.2026.S01E01.mkv","missing":False}
        pipeline,pan,ops,store,context=self.setup_flow(plan(old=[old]))
        ops.old=[old];pan.files["target"]=[api.Entry("9",old["name"],False,80)]
        result=await pipeline.run()
        self.assertTrue(result["inventory_verified"])
        self.assertEqual(ops.deleted,[])
        self.assertIn(" - p115-",result["moved"][0]["name"])
        self.assertEqual(result["pending_delete"][0]["file_id"],9)
        self.assertEqual(result["renamed"],[])
        self.assertEqual(len(result["name_restore_pending"]),1)
        scans=[args for name,args in ops.calls if name=="library.scan.start"]
        self.assertEqual(scans,[{"library_id":12,"body":{"cloud_paths":["网盘/日韩剧/剧 (2026)"]}}])

    async def test_restore_names_conflict_never_overwrites(self):
        pipeline,pan,ops,store,context=self.setup_flow()
        pipeline.state={"items":{"1":{"moved":True,"remote_id":"1","target_id":"target",
            "actual_size":100,"target_name":"temp.mkv","restore_to":"name.mkv"}},"moved":{}}
        pan.files["target"]=[api.Entry("1","temp.mkv",False,100),api.Entry("9","name.mkv",False,80)]
        self.assertEqual(await pipeline.restore_names(),[])
        self.assertEqual(pan.renames,0)
        self.assertEqual(len(pipeline.state["name_restore_pending"]),1)

    async def test_restore_names_network_failure_not_swallowed(self):
        pipeline,pan,ops,store,context=self.setup_flow()
        pipeline.state={"items":{"1":{"moved":True,"remote_id":"1","target_id":"target",
            "actual_size":100,"target_name":"temp.mkv","restore_to":"name.mkv"}},"moved":{}}
        pan.files["target"]=[api.Entry("1","temp.mkv",False,100)]
        pan.rename_many=AsyncMock(side_effect=api.ApiError("暂时失败"))
        with self.assertRaises(api.ApiError):await pipeline.restore_names()
        self.assertEqual(pipeline.state["items"]["1"]["restore_to"],"name.mkv")

    async def test_old_inventory_changed_before_delete_is_preserved(self):
        old={"file_id":9,"name":"old.mkv","size":80,"season":1,"episode":1,"remote_path":"old","missing":False}
        pipeline,pan,ops,store,context=self.setup_flow(plan(old=[old]))
        ops.old=[copy.deepcopy(old)]
        original=pipeline.read_inventory
        calls={"n":0}
        async def inventory():
            calls["n"]+=1
            if calls["n"]>=4:ops.old[0]["size"]=300
            return await original()
        pipeline.read_inventory=inventory
        result=await pipeline.run()
        self.assertEqual(ops.deleted,[])
        self.assertTrue(result["pending_delete"] or result["delete_failed"])

    async def test_wrong_episode_old_is_not_deleted(self):
        old={"file_id":9,"name":"old.mkv","size":80,"season":1,"episode":2,"remote_path":"old","missing":False}
        pipeline,pan,ops,store,context=self.setup_flow(plan(old=[old]))
        ops.old=[old]
        result=await pipeline.run()
        self.assertEqual(ops.deleted,[]);self.assertTrue(result["delete_failed"])

    async def test_submit_intent_persisted_before_network(self):
        pipeline,pan,ops,store,context=self.setup_flow()
        original=pan.share_receive
        async def receive(*a):
            state=await store.get(pipeline.key)
            self.assertTrue(state["submit_intent"])
            return await original(*a)
        pan.share_receive=receive
        await pipeline.run()

    async def test_all_checkpoint_rows_encrypted(self):
        pipeline,pan,ops,store,context=self.setup_flow()
        await pipeline.run()
        self.assertTrue(all(secret for key,secret in store.secrets if key.startswith("flow:")))


class Offline(unittest.IsolatedAsyncioTestCase):
    """云下载（ed2k）：115 重封装后体积与链接声明不一致，得靠任务回执里的文件 ID 认领。"""
    def setup_offline(self,resized=True):
        pl=plan(source="ed2k")
        item=pl["items"][0]
        pan=Pan()
        pan.tasks=[{"info_hash":item["info_hash"],"status":2,"size":item["size"],
                    "delete_file_id":"1","wp_path_id":"staging"}]
        # 115 重新封装：网盘里落地文件体积和链接声明不同。
        actual=item["size"]-5 if resized else item["size"]
        pan.sources=[api.Entry("1",item["share_path"],False,actual)]
        # 115 已下完并把文件放在暂存目录（这是重试/续跑时看到的真实状态）。
        pan.files["staging"]=[api.Entry("1",item["share_path"],False,actual,"1"*40)]
        ops=Ops();ops.pan=pan
        ctx=SimpleNamespace(logger=logging.getLogger("test"))
        pipeline=f.Flow(ctx,pan,ops,Store(),Context(),pl,existing=ops.inventory,
                        wait_seconds=0,scan_seconds=1,cloud_max_seconds=0)
        return pipeline,pan,ops,pl

    async def test_repackaged_size_still_claimed_by_file_id(self):
        pipeline,pan,ops,pl=self.setup_offline(resized=True)
        item=pl["items"][0]
        self.assertNotEqual(item["size"],pan.sources[0].size)
        result=await pipeline.run()
        self.assertTrue(result["inventory_verified"])
        self.assertEqual(len(result["moved"]),1)
        self.assertEqual(result["moved"][0]["size"],pan.sources[0].size)

    async def test_offline_actual_size_decides_replacement(self):
        # 链接声明 100，库存 95；115 实际只落了 90 → 不该洗版。
        pipeline,pan,ops,pl=self.setup_offline()
        item=pl["items"][0]
        pan.sources[0].size=90
        item["existing"]=[{"file_id":9,"name":"old.mkv","size":95,"season":1,"episode":1,
                           "remote_path":"old","missing":False}]
        ops.old=item["existing"]
        result=await pipeline.run()
        self.assertEqual(result["status"],"nothing_to_do")
        self.assertEqual(pan.moves,0)

    async def test_task_in_other_staging_dir_is_not_claimed(self):
        # 别的批次里的同一条资源不能被本批认领，也不能重复提交：停下来等人确认。
        pipeline,pan,ops,pl=self.setup_offline()
        pan.tasks[0]["wp_path_id"]="some-other-batch"
        with self.assertRaises(JobBlocked):
            await pipeline.run()
        self.assertEqual(pan.moves,0)

    async def test_offline_failure_reported_not_silently_waited(self):
        pipeline,pan,ops,pl=self.setup_offline()
        pan.tasks[0].update(status=-1,err_code=1)
        pan.sources=[]
        with self.assertRaises(JobBlocked):
            await pipeline.run()
        self.assertEqual(pan.moves,0)

    async def test_still_downloading_waits_inside_job_not_by_retry(self):
        # 云下载未完成时应在任务内继续等并报 0/1 进度，不把重试次数当计时器。
        pl=plan(source="ed2k")
        item=pl["items"][0]
        pan=Pan()
        pan.tasks=[{"info_hash":item["info_hash"],"status":1,"size":item["size"],
                    "delete_file_id":"1","wp_path_id":"staging"}]
        ops=Ops();ops.pan=pan;context=Context()
        pipeline=f.Flow(SimpleNamespace(logger=logging.getLogger("test")),pan,ops,Store(),
                        context,pl,existing=ops.inventory,wait_seconds=0,scan_seconds=1,
                        cloud_max_seconds=0)
        with self.assertRaises(JobRetry):
            await pipeline.run()
        self.assertTrue(any(m.get("phase")=="cloud" and m.get("total")==1
                            for m in context.messages))
        self.assertEqual(pan.moves,0)
        # 云下载没下完时不该重新提交（115 那边已经在下了）。
        self.assertEqual(pan.receives,0)


class Requests(unittest.IsolatedAsyncioTestCase):
    def setup(self):
        store=Store();ctx=SimpleNamespace(config=SimpleNamespace(dry_run=False))
        jobs=SimpleNamespace(create=AsyncMock(return_value=JobRef("job1",rq.JOB,"queued",True)))
        ops=SimpleNamespace(call=AsyncMock(return_value={"id":"job1","status":"queued"}))
        return ctx,store,jobs,ops

    async def test_submit_returns_before_any_network_share_read(self):
        ctx,store,jobs,ops=self.setup()
        result=await rq.submit(ctx,store,jobs,ops,p.ApplyBody(link="abc-1234",yes=True))
        self.assertEqual(result["job_id"],"job1")
        self.assertEqual(ops.call.await_count,0)
        self.assertEqual(set(jobs.create.call_args.kwargs["input"]),{"request_ref"})
        self.assertNotIn("1234",str(jobs.create.call_args.kwargs))

    async def test_source_only_in_encrypted_store(self):
        ctx,store,jobs,ops=self.setup()
        await rq.submit(ctx,store,jobs,ops,p.ApplyBody(link="abc-1234",yes=True))
        private=[(key,secret) for key,secret in store.secrets if key.startswith("input:")]
        self.assertEqual(len(private),1);self.assertTrue(private[0][1])

    async def test_double_submit_reuses_job(self):
        ctx,store,jobs,ops=self.setup()
        body=p.ApplyBody(link="abc-1234",yes=True)
        await rq.submit(ctx,store,jobs,ops,body)
        result=await rq.submit(ctx,store,jobs,ops,body)
        self.assertFalse(result["job_created"])
        self.assertEqual(jobs.create.await_count,1)

    async def test_active_child_reused_after_parent_dispatch(self):
        ctx,store,jobs,ops=self.setup()
        body=p.ApplyBody(link="abc-1234",yes=True)
        await rq.submit(ctx,store,jobs,ops,body)
        req=next(v for k,v in store.data.items() if k.startswith("request:"))
        await store.set("request-state:"+req["request_ref"],{"transfer_job_id":"child"},secret=True)
        async def call(name,args):return {"id":args["job_id"],"status":"running" if args["job_id"]=="child" else "succeeded"}
        ops.call=call
        result=await rq.submit(ctx,store,jobs,ops,body)
        self.assertEqual(result["job_id"],"child");self.assertEqual(jobs.create.await_count,1)

    async def test_dry_run_request_never_creates_transfer(self):
        ctx,store,jobs,ops=self.setup();ctx.config.dry_run=True
        await store.set("input:r",{"link":"abc-1234"},secret=True)
        runner=rq.RequestRunner(ctx,store,jobs,ops,plan_body=p.PlanBody,
            build_plan=AsyncMock(return_value={"status":"ready","title":"剧","receive_code":"1234"}),
            execute=AsyncMock(),push=AsyncMock())
        result=await runner.handle(Context(),{"request_ref":"r"})
        self.assertEqual(result["status"],"dry_run");self.assertEqual(jobs.create.await_count,0)
        self.assertNotIn("receive_code",result)

    async def test_ambiguous_request_no_transfer_and_keeps_candidates(self):
        ctx,store,jobs,ops=self.setup()
        await store.set("input:r",{"link":"abc-1234"},secret=True)
        runner=rq.RequestRunner(ctx,store,jobs,ops,plan_body=p.PlanBody,
            build_plan=AsyncMock(return_value={"status":"ambiguous","message":"选作品","candidates":[{"tmdb_id":2}]}),
            execute=AsyncMock(),push=AsyncMock())
        with self.assertRaises(JobBlocked) as caught:await runner.handle(Context(),{"request_ref":"r"})
        self.assertEqual(caught.exception.details["candidates"],[{"tmdb_id":2}])
        self.assertEqual(jobs.create.await_count,0)

    async def test_parent_dispatch_does_not_block_worker_waiting_for_child(self):
        ctx,store,jobs,ops=self.setup()
        await store.set("input:r",{"link":"abc-1234"},secret=True)
        runner=rq.RequestRunner(ctx,store,jobs,ops,plan_body=p.PlanBody,
            build_plan=AsyncMock(return_value=dict(plan(),status="ready")),execute=AsyncMock(),push=AsyncMock())
        result=await runner.handle(Context(),{"request_ref":"r"})
        self.assertEqual(result["status"],"dispatched")
        self.assertEqual(jobs.create.call_args.args[0],"p115-import:transfer")
        self.assertEqual(set(jobs.create.call_args.kwargs["input"]),{"plan_ref"})

    async def test_final_retry_exhaustion_sends_one_pause_notice(self):
        ctx,store,jobs,ops=self.setup()
        await store.set("input:r",{"user":"admin"},secret=True)
        ops.call=AsyncMock(return_value={"attempt":3,"max_attempts":3})
        push=AsyncMock()
        for _ in range(2):
            await rq.notify_pause(ctx,store,ops,Context(),"r","云端未完成",push,retry=True)
        push.assert_awaited_once()

    async def test_intermediate_retry_does_not_spam_pause(self):
        ctx,store,jobs,ops=self.setup()
        await store.set("input:r",{"user":"admin"},secret=True)
        ops.call=AsyncMock(return_value={"attempt":1,"max_attempts":3})
        push=AsyncMock()
        await rq.notify_pause(ctx,store,ops,Context(),"r","云端未完成",push,retry=True)
        push.assert_not_awaited()

    async def test_repeated_input_resumes_failed_child_checkpoint(self):
        ctx,store,jobs,ops=self.setup()
        body=p.ApplyBody(link="abc-1234",yes=True)
        await rq.submit(ctx,store,jobs,ops,body)
        req=next(v for k,v in store.data.items() if k.startswith("request:"))
        await store.set("request-state:"+req["request_ref"],{"transfer_job_id":"child"},secret=True)
        async def call(name,args):
            if name=="jobs.retry":return {"job":{"id":"child-resumed","status":"queued"}}
            return {"id":args["job_id"],"status":"failed" if args["job_id"]=="child" else "succeeded"}
        ops.call=call
        result=await rq.submit(ctx,store,jobs,ops,body)
        self.assertEqual(result["job_id"],"child-resumed")
        self.assertEqual(jobs.create.await_count,1)

    async def test_transient_planning_failure_retried(self):
        ctx,store,jobs,ops=self.setup()
        await store.set("input:r",{"link":"abc-1234"},secret=True)
        runner=rq.RequestRunner(ctx,store,jobs,ops,plan_body=p.PlanBody,
            build_plan=AsyncMock(side_effect=HTTPException(502,detail="暂时异常")),execute=AsyncMock(),push=AsyncMock())
        with self.assertRaises(JobRetry):await runner.handle(Context(),{"request_ref":"r"})


class EmptyHolderCleanup(unittest.IsolatedAsyncioTestCase):
    def create_pan(self):
        pan=Pan()
        pan.files["root"]=[api.Entry("holder",".p115-import",True),api.Entry("movie","电影",True)]
        pan.files["holder"]=[]
        return pan

    async def test_dry_run_lists_all_items_without_mutation(self):
        pan=self.create_pan()
        result=await f.cleanup_empty_holder(pan,"root",dry_run=True)
        self.assertEqual(result["status"],"preview")
        self.assertTrue(result["eligible"])
        self.assertEqual(pan.deletes,[])

    async def test_nonempty_root_never_recursively_cleaned(self):
        pan=self.create_pan();pan.files["holder"]=[api.Entry("other","another-request",True)]
        result=await f.cleanup_empty_holder(pan,"root")
        self.assertEqual(result["status"],"retained")
        self.assertEqual(pan.deletes,[])

    async def test_new_content_before_delete_retained(self):
        pan=self.create_pan();original=pan.list_dir;calls={"n":0}
        async def list_dir(cid):
            if cid=="holder":
                calls["n"]+=1
                if calls["n"]==2:pan.files[cid].append(api.Entry("new","new-request",True))
            return await original(cid)
        pan.list_dir=list_dir
        result=await f.cleanup_empty_holder(pan,"root")
        self.assertEqual(result["status"],"retained")
        self.assertEqual(pan.deletes,[])

    async def test_expected_folder_id_changed_does_not_delete(self):
        pan=self.create_pan()
        result=await f.cleanup_empty_holder(pan,"root",expected_id="old")
        self.assertEqual(result["status"],"deferred")
        self.assertEqual(pan.deletes,[])

    async def test_delete_success_response_but_still_visible_not_reported_removed(self):
        pan=self.create_pan()
        pan.delete=AsyncMock()
        result=await f.cleanup_empty_holder(pan,"root")
        self.assertEqual(result["status"],"deferred")
        self.assertFalse(result["holder_removed"])

    async def test_absent_does_not_create_placeholder(self):
        pan=Pan()
        result=await f.cleanup_empty_holder(pan,"root")
        self.assertEqual(result["status"],"absent")
        self.assertEqual(pan.ensures,[])
        self.assertEqual(pan.deletes,[])

    async def test_cancelled_maintenance_never_deletes(self):
        pan=self.create_pan()
        with self.assertRaises(JobCancelled):
            await f.cleanup_empty_holder(pan,"root",context=Context(cancel=True))
        self.assertEqual(pan.deletes,[])


class CheckpointStorage(unittest.IsolatedAsyncioTestCase):
    async def test_large_plan_uses_encrypted_small_chunks(self):
        from p115storage import ChunkedStore
        class Limited(Store):
            async def set(self,key,value,secret=False):
                import json
                if len(json.dumps(value,ensure_ascii=False).encode())>65536:
                    raise ValueError("too large")
                await super().set(key,value,secret)
        raw=Limited();store=ChunkedStore(raw)
        value={"items":[{"name":"剧集中文路径"*20,"size":i} for i in range(400)]}
        await store.set("plan",value,secret=True)
        self.assertEqual(await store.get("plan"),value)
        self.assertTrue(all(secret for _,secret in raw.secrets))
        self.assertGreater(len(raw.data),1)
        await store.set("plan",{"small":True},secret=True)
        self.assertEqual(await store.get("plan"),{"small":True})
        self.assertEqual(len(raw.data),1)

    async def test_interrupted_chunk_write_keeps_previous_checkpoint(self):
        from p115storage import ChunkedStore
        raw=Store();store=ChunkedStore(raw)
        await store.set("plan",{"old":True},secret=True)
        original=raw.set
        calls={"n":0}
        async def failing(key,value,secret=False):
            if key.startswith("chunk:"):
                calls["n"]+=1
                if calls["n"]==2:raise RuntimeError("模拟写分片中断")
            await original(key,value,secret)
        raw.set=failing
        with self.assertRaises(RuntimeError):await store.set("plan",{"data":"中文"*50000},secret=True)
        self.assertEqual(await store.get("plan"),{"old":True})

    async def test_missing_piece_fails_closed(self):
        from p115storage import ChunkedStore
        raw=Store();store=ChunkedStore(raw)
        await store.set("large",{"value":"长"*50000},secret=True)
        part=next(key for key in raw.data if key.startswith("chunk:"))
        del raw.data[part]
        with self.assertRaises(RuntimeError):await store.get("large")


class Lifecycle(unittest.IsolatedAsyncioTestCase):
    async def test_plugin_mounts_native_jobs_and_shared_client_without_network(self):
        store=Store();context=SimpleNamespace(config=p.Settings(),entry_id="p115-import",logger=logging.getLogger("test"))
        captured={"routes":[],"jobs":[],"effects":[]}
        routes=SimpleNamespace(mount=lambda ctx,router,**kw:captured["routes"].extend(router.routes))
        ops=SimpleNamespace(call=AsyncMock())
        jobs=SimpleNamespace(create=AsyncMock(return_value=JobRef("job_1",rq.JOB,"queued",True)))
        values={p.PLUGIN_DATA:SimpleNamespace(scoped=lambda ctx:store),
                p.PLUGIN_FILES:SimpleNamespace(scoped=lambda ctx:SimpleNamespace()),
                p.PLUGIN_JOBS:SimpleNamespace(scoped=lambda ctx:jobs),p.PLUGIN_ROUTES:routes,
                p.HOST_OPS:SimpleNamespace(client=AsyncMock(return_value=ops)),
                p.PLUGIN_CALLBACKS:SimpleNamespace()}
        context.use=lambda key:values[key]
        context.effect=lambda fn:captured["effects"].append(fn)
        context.contribute=lambda key,name,registration:captured["jobs"].append((name,registration))
        old=(p._pan,p._core_parser,p._submit_lock)
        try:
            await p.apply.apply(context)
            self.assertEqual({name for name,_ in captured["jobs"]},{"p115-import:transfer","p115-import:request","p115-import:cleanup-staging","p115-import:repair-names","p115-import:cleanup-old"})
            endpoint=next(route.endpoint for route in captured["routes"] if route.path=="/apply")
            result=await endpoint(p.ApplyBody(link="https://115.com/s/abc?password=1234",yes=True))
            self.assertEqual(result["status"],"started")
            ops.call.assert_not_awaited()
            self.assertIsNone(__import__("movieclaw_api.services.jobs",fromlist=["_secret_input_path"])._secret_input_path(
                jobs.create.call_args.kwargs["input"]))
        finally:
            for fn in captured["effects"]:
                result=fn()
                if asyncio.iscoroutine(result):await result
            p._pan,p._core_parser,p._submit_lock=old


class ApiEfficiency(unittest.IsolatedAsyncioTestCase):
    async def test_directory_cache_only_checks_each_level_once(self):
        pan=api.Pan(Store())
        pan.find_child=AsyncMock(return_value=None)
        pan.mkdir=AsyncMock(side_effect=["series","season"])
        for _ in range(10):self.assertEqual(await pan.ensure_dir("root",["剧","Season 01"]),"season")
        self.assertEqual(pan.find_child.await_count,2);self.assertEqual(pan.mkdir.await_count,2)
    async def test_share_tree_limit_fails_not_silently_truncated(self):
        pan=api.Pan(Store())
        pan.share_snap=AsyncMock(return_value={"data":{"count":2,"list":[{"fid":"1","fc":"1","n":"a.mkv","s":10},{"fid":"2","fc":"1","n":"b.mkv","s":10}]}})
        with self.assertRaises(api.ApiError):await pan.share_tree("abc","",max_files=1)

    async def test_share_tree_cycle_does_not_loop(self):
        pan=api.Pan(Store())
        pan.share_snap=AsyncMock(side_effect=[{"data":{"count":1,"list":[{"cid":"1","fc":"0","n":"folder"}]}},
                    {"data":{"count":1,"list":[{"cid":"1","fc":"0","n":"again"}]}}])
        result=await pan.share_tree("abc","")
        self.assertEqual(pan.share_snap.await_count,2)

    async def test_clients_share_connection_pool(self):
        client=SimpleNamespace(request=AsyncMock(return_value="ok"),aclose=AsyncMock())
        pan=api.Pan(Store())
        with patch.object(api.net,"http_transport",return_value=None),patch.object(api.httpx,"AsyncClient",return_value=client) as constructor:
            await pan._request("GET","https://example.invalid")
            await pan._request("GET","https://example.invalid")
            await pan.close()
        self.assertEqual(constructor.call_count,1);self.assertEqual(pan.request_count,2)
        client.aclose.assert_awaited_once()

if __name__=="__main__":unittest.main(verbosity=2)
