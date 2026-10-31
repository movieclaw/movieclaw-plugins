"""批量归位正反例：只用内存网盘与模拟HTTP，不发任何真实写操作。"""
import copy
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from movieclaw_sdk.jobs import JobBlocked, JobCancelled, JobRetry

import p115api as api
import p115batch as batch
import p115flow as flow
from tests import test_flow as base


class MemoryPan(base.Pan):
    def __init__(self):
        super().__init__()
        self.rename_calls=[]
        self.move_calls=[]
        self.list_calls=[]
        self.partial_rename=None
        self.partial_move=None
        self.rename_lost=False
        self.move_lost=False
        self.false_rename=False
        self.false_move=False
        self.before_rename=None
        self.before_move=None
        self.cancel_after_rename=None
    async def list_dir(self,cid):
        self.list_calls.append(str(cid))
        return await super().list_dir(cid)
    async def rename_many(self,pairs):
        pairs=list(pairs)
        self.rename_calls.append(copy.deepcopy(pairs))
        if self.before_rename:
            await self.before_rename(pairs)
        chosen=pairs if self.partial_rename is None else pairs[:self.partial_rename]
        if not self.false_rename:
            await super().rename_many(chosen)
        if self.cancel_after_rename:
            self.cancel_after_rename.cancel=True
        if self.rename_lost:
            self.rename_lost=False
            raise api.ApiError("模拟批量改名成功但回执丢失")
    async def move(self,ids,cid):
        self.move_calls.append((list(ids),str(cid)))
        if self.before_move:
            await self.before_move(ids,cid)
        chosen=ids if self.partial_move is None else ids[:self.partial_move]
        if not self.false_move:
            await super().move(chosen,cid)
        if self.move_lost:
            self.move_lost=False
            raise api.ApiError("模拟批量移动成功但回执丢失")


def operation(fid,name=None,source="source",target="target",size=100):
    return {"remote_id":str(fid),"source_id":source,"target_id":target,
            "from_name":f"original-{fid}.mkv","name":name or f"renamed-{fid}.mkv","size":size}


def source_rows(rows):
    return [api.Entry(row["remote_id"],row["from_name"],False,row["size"],parent_id=row["source_id"]) for row in rows]


class BatchPure(unittest.TestCase):
    def test_chunks_are_bounded_and_keep_order(self):
        self.assertEqual([len(x) for x in batch.chunks(list(range(121)))],[50,50,21])
        self.assertEqual([x for part in batch.chunks(list(range(121))) for x in part],list(range(121)))
    def test_duplicate_ids_are_rejected(self):
        with self.assertRaises(JobBlocked):batch.validate_rows([operation(1),operation(1)])
    def test_duplicate_names_same_source_rejected(self):
        with self.assertRaises(JobBlocked):batch.validate_rows([operation(1,"same.mkv"),operation(2,"SAME.mkv")])
    def test_same_name_different_source_can_be_checked(self):
        batch.validate_rows([operation(1,"same.mkv",source="s1"),operation(2,"same.mkv",source="s2")])
    def test_missing_source_empty_size_and_escape_are_rejected(self):
        for changes in ({"source_id":""},{"size":0},{"name":"../bad.mkv"},{"name":"bad\\x.mkv"},{"name":" "}):
            with self.subTest(changes=changes),self.assertRaises(JobBlocked):
                batch.validate_rows([{**operation(1),**changes}])


class BatchApi(unittest.IsolatedAsyncioTestCase):
    async def test_batch_rename_uses_one_cookie_web_request_and_not_openapi(self):
        pan=api.Pan(base.Store())
        pan.web_json=AsyncMock(return_value={"state":True})
        pan.open_json=AsyncMock()
        pairs=[("1","剧.S01E01.mkv"),("2","剧.S01E02.mkv"),("3","剧.S01E01.zh.srt")]
        await pan.rename_many(pairs)
        pan.web_json.assert_awaited_once_with("POST","https://webapi.115.com/files/batch_rename",
            form={"files_new_name[1]":"剧.S01E01.mkv","files_new_name[2]":"剧.S01E02.mkv","files_new_name[3]":"剧.S01E01.zh.srt"},action="批量改名")
        pan.open_json.assert_not_awaited()
    async def test_rename_rejects_large_batch_before_network(self):
        pan=api.Pan(base.Store());pan.web_json=AsyncMock()
        with self.assertRaises(ValueError):await pan.rename_many([(str(i),f"{i}.mkv") for i in range(51)])
        pan.web_json.assert_not_awaited()
    async def test_rename_rejects_duplicate_ids_and_paths(self):
        pan=api.Pan(base.Store());pan.web_json=AsyncMock()
        for pairs in ([("1","a.mkv"),("1","b.mkv")],[("1","../x")],[("1","a\\x")]):
            with self.subTest(pairs=pairs),self.assertRaises(ValueError):await pan.rename_many(pairs)
        pan.web_json.assert_not_awaited()
    async def test_move_openapi_carries_all_ids_together(self):
        pan=api.Pan(base.Store());pan._use_open=AsyncMock(return_value=True);pan.open_json=AsyncMock()
        await pan.move(["1","2","3"],"season")
        pan.open_json.assert_awaited_once_with("POST","/open/ufile/move",
            form={"file_ids":"1,2,3","to_cid":"season"},action="移动")
    async def test_empty_rename_has_no_network(self):
        pan=api.Pan(base.Store());pan.web_json=AsyncMock()
        await pan.rename_many([]);pan.web_json.assert_not_awaited()

    async def test_http_transport_encodes_complete_names_and_existing_cookie(self):
        import httpx
        from urllib.parse import parse_qs
        pan=api.Pan(base.Store())
        pan.require_web=AsyncMock(return_value={"UID":"fake-uid","CID":"fake-cid","SEID":"fake-seid","KID":"fake-kid"})
        seen=[]
        async def handler(request):
            seen.append(request)
            form=parse_qs(request.content.decode())
            self.assertEqual(form,{"files_new_name[1]":["剧.S01E01.mkv"],"files_new_name[2]":["剧.S01E01.zh-Hans.srt"]})
            self.assertEqual(request.url.host,"webapi.115.com")
            self.assertIn("UID=fake-uid",request.headers["cookie"])
            return httpx.Response(200,json={"state":True,"data":{"1":"剧.S01E01.mkv","2":"剧.S01E01.zh-Hans.srt"}})
        pan._client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            await pan.rename_many([("1","剧.S01E01.mkv"),("2","剧.S01E01.zh-Hans.srt")])
        finally:
            await pan.close()
        self.assertEqual(len(seen),1)



class BatchOperations(unittest.IsolatedAsyncioTestCase):
    def setup_case(self,count=3):
        rows=[operation(i) for i in range(1,count+1)]
        pan=MemoryPan();pan.files["source"]=source_rows(rows)
        return pan,rows,base.Context()
    async def test_40_renames_are_one_request_with_directory_level_verification(self):
        pan,rows,context=self.setup_case(40)
        save=AsyncMock();done=AsyncMock()
        result=await batch.renamed(pan,rows,context=context,save=save,confirmed=done)
        self.assertEqual(len(result),40)
        self.assertEqual(len(pan.rename_calls),1)
        self.assertEqual(len(pan.rename_calls[0]),40)
        self.assertEqual(pan.list_calls.count("source"),2)
        self.assertGreaterEqual(save.await_count,2)
        self.assertEqual(len(done.call_args.args[0]),40)
    async def test_121_renames_split_50_50_21_without_parallelism(self):
        pan,rows,context=self.setup_case(121)
        await batch.renamed(pan,rows,context=context)
        self.assertEqual([len(x) for x in pan.rename_calls],[50,50,21])
    async def test_already_correct_name_does_not_send_again(self):
        pan,rows,context=self.setup_case()
        pan.files["source"][0].name=rows[0]["name"]
        result=await batch.renamed(pan,rows,context=context)
        self.assertEqual(len(result),3)
        self.assertEqual(pan.rename_calls[0],[("2","renamed-2.mkv"),("3","renamed-3.mkv")])
    async def test_same_name_wrong_id_prevents_rename(self):
        pan,rows,context=self.setup_case()
        pan.files["source"].append(api.Entry("unrelated",rows[0]["name"],False,100))
        with self.assertRaises(JobBlocked):await batch.renamed(pan,rows,context=context)
        self.assertEqual(pan.rename_calls,[])
    async def test_wrong_size_or_hash_prevents_rename(self):
        for change in ("size","sha1"):
            pan,rows,context=self.setup_case()
            if change=="size":pan.files["source"][0].size=99
            else:
                rows[0]["sha1"]="a"*40;pan.files["source"][0].sha1="b"*40
            with self.subTest(change=change),self.assertRaises(JobBlocked):await batch.renamed(pan,rows,context=context)
            self.assertEqual(pan.rename_calls,[])
    async def test_rename_response_lost_success_confirmed_without_resend(self):
        pan,rows,context=self.setup_case();pan.rename_lost=True
        self.assertEqual(len(await batch.renamed(pan,rows,context=context)),3)
        self.assertEqual(len(pan.rename_calls),1)
        await batch.renamed(pan,rows,context=context)
        self.assertEqual(len(pan.rename_calls),1)
    async def test_partial_rename_checkpoints_success_and_only_missing_retried(self):
        pan,rows,context=self.setup_case();pan.partial_rename=1
        done=[]
        async def record(items):done.extend(row["remote_id"] for row in items)
        with self.assertRaises(JobRetry):await batch.renamed(pan,rows,context=context,confirmed=record)
        self.assertEqual(done,["1"])
        pan.partial_rename=None
        await batch.renamed(pan,rows,context=context)
        self.assertEqual(pan.rename_calls[1],[("2","renamed-2.mkv"),("3","renamed-3.mkv")])
    async def test_success_receipt_without_changed_names_is_not_success(self):
        pan,rows,context=self.setup_case();pan.false_rename=True
        with self.assertRaises(JobRetry):await batch.renamed(pan,rows,context=context)
    async def test_rate_limit_stops_without_followup_read_or_fallback(self):
        pan,rows,context=self.setup_case()
        pan.rename_many=AsyncMock(side_effect=api.RateLimited())
        with self.assertRaises(api.RateLimited):await batch.renamed(pan,rows,context=context)
        self.assertEqual(pan.list_calls,["source"])
    async def test_cancel_after_batch_rename_prevents_move(self):
        pan,rows,context=self.setup_case();pan.cancel_after_rename=context
        with self.assertRaises(JobCancelled):await batch.renamed(pan,rows,context=context)
        self.assertEqual(pan.move_calls,[])
    async def test_move_grouped_by_target_and_verified_per_id(self):
        pan,rows,context=self.setup_case(4)
        rows[2]["target_id"]=rows[3]["target_id"]="season2"
        for e,row in zip(pan.files["source"],rows):e.name=row["name"]
        pan.files["season2"]=[]
        result=await batch.moved(pan,rows,context=context)
        self.assertEqual(len(result),4)
        self.assertEqual(pan.move_calls,[(["1","2"],"target"),(["3","4"],"season2")])
    async def test_move_partial_success_recorded_then_only_missing_retried(self):
        pan,rows,context=self.setup_case()
        for e,row in zip(pan.files["source"],rows):e.name=row["name"]
        pan.partial_move=1;recorded=[]
        async def record(items):recorded.extend(r["remote_id"] for r in items)
        with self.assertRaises(JobRetry):await batch.moved(pan,rows,context=context,confirmed=record)
        self.assertEqual(recorded,["1"])
        pan.partial_move=None
        await batch.moved(pan,rows,context=context)
        self.assertEqual(pan.move_calls[1],(["2","3"],"target"))
    async def test_move_lost_response_uses_real_destination(self):
        pan,rows,context=self.setup_case();pan.move_lost=True
        for e,row in zip(pan.files["source"],rows):e.name=row["name"]
        self.assertEqual(len(await batch.moved(pan,rows,context=context)),3)
        self.assertEqual(len(pan.move_calls),1)
    async def test_move_false_success_is_not_verified(self):
        pan,rows,context=self.setup_case();pan.false_move=True
        for e,row in zip(pan.files["source"],rows):e.name=row["name"]
        with self.assertRaises(JobRetry):await batch.moved(pan,rows,context=context)
    async def test_move_collision_is_not_overwritten(self):
        pan,rows,context=self.setup_case()
        for e,row in zip(pan.files["source"],rows):e.name=row["name"]
        pan.files["target"].append(api.Entry("unrelated",rows[0]["name"],False,100))
        with self.assertRaises(JobBlocked):await batch.moved(pan,rows,context=context)
        self.assertEqual(pan.move_calls,[])
    async def test_move_duplicate_target_name_in_different_sources_rejected(self):
        pan,rows,context=self.setup_case(2)
        rows[1].update(source_id="other",name=rows[0]["name"])
        with self.assertRaises(JobBlocked):await batch.moved(pan,rows,context=context)
        self.assertEqual(pan.move_calls,[])
    async def test_checkpoint_write_failure_prevents_network_mutation(self):
        pan,rows,context=self.setup_case()
        save=AsyncMock(side_effect=RuntimeError("存盘失败"))
        with self.assertRaises(RuntimeError):await batch.renamed(pan,rows,context=context,save=save)
        self.assertEqual(pan.rename_calls,[])
    async def test_external_name_change_between_rename_and_move_stops(self):
        pan,rows,context=self.setup_case()
        await batch.renamed(pan,rows,context=context)
        pan.files["source"][0].name="external.mkv"
        with self.assertRaises(JobBlocked):await batch.moved(pan,rows,context=context)
        self.assertEqual(pan.move_calls,[])

    async def test_invalid_item_after_rename_does_not_lose_verified_sibling_checkpoints(self):
        pan,rows,context=self.setup_case(2)
        original=pan.rename_many
        async def rename(pairs):
            await original(pairs)
            pan.files["source"][1].size=50
        pan.rename_many=rename
        done=[]
        async def record(items):done.extend(r["remote_id"] for r in items)
        with self.assertRaises(JobBlocked):await batch.renamed(pan,rows,context=context,confirmed=record)
        self.assertEqual(done,["1"])

    async def test_invalid_item_after_move_still_records_verified_siblings(self):
        pan,rows,context=self.setup_case(2)
        for e,row in zip(pan.files["source"],rows):e.name=row["name"]
        original=pan.move
        async def move(ids,cid):
            await original(ids,cid)
            pan.files[cid][1].size=50
        pan.move=move
        done=[]
        async def record(items):done.extend(r["remote_id"] for r in items)
        with self.assertRaises(JobBlocked):await batch.moved(pan,rows,context=context,confirmed=record)
        self.assertEqual(done,["1"])


    async def test_later_chunk_rechecks_collision_before_sending(self):
        pan,rows,context=self.setup_case(51)
        original=pan.rename_many
        async def rename(pairs):
            await original(pairs)
            if len(pan.rename_calls)==1:
                pan.files["source"].append(api.Entry("external","renamed-51.mkv",False,99))
        pan.rename_many=rename
        with self.assertRaises(JobBlocked):await batch.renamed(pan,rows,context=context)
        self.assertEqual(len(pan.rename_calls),1)
        self.assertEqual(len(pan.rename_calls[0]),50)



class BatchPipeline(unittest.IsolatedAsyncioTestCase):
    def setup_case(self,count=3,pl=None):
        pan=MemoryPan()
        pipeline,pan,ops,store,context=base.Pipeline().setup_flow(pl or base.plan(count),pan=pan)
        return pipeline,pan,ops,store,context
    async def test_40_episode_pipeline_uses_one_rename_and_one_move(self):
        pipeline,pan,ops,store,context=self.setup_case(40)
        result=await pipeline.run()
        self.assertTrue(result["inventory_verified"])
        self.assertEqual(len(result["moved"]),40)
        self.assertEqual(len(pan.rename_calls),1)
        self.assertEqual(len(pan.move_calls),1)
        self.assertEqual(ops.deleted,[])
        self.assertEqual(pan.receives,1)
    async def test_all_ids_checkpointed_before_first_batch_mutation(self):
        pipeline,pan,ops,store,context=self.setup_case(5)
        async def before(pairs):
            state=await store.get(pipeline.key)
            self.assertEqual(len(state["items"]),5)
            for cp in state["items"].values():
                self.assertTrue(cp["remote_id"]);self.assertEqual(cp["actual_size"],100)
                self.assertEqual(cp["source_id"],"staging");self.assertTrue(cp["source_name"])
        pan.before_rename=before
        await pipeline.run()
    async def test_partial_move_resume_never_retransfers_or_repeats_completed(self):
        pipeline,pan,ops,store,context=self.setup_case(4)
        pan.partial_move=2
        with self.assertRaises(JobRetry):await pipeline.run()
        state=await store.get(pipeline.key)
        self.assertEqual(len(state["moved"]),2)
        pan.partial_move=None
        again,*_=base.Pipeline().setup_flow(base.plan(4),pan,ops,store)
        result=await again.run()
        self.assertTrue(result["inventory_verified"])
        self.assertEqual(len(result["moved"]),4)
        self.assertEqual(pan.receives,1)
        self.assertEqual(pan.move_calls[1],(["3","4"],"target"))
        self.assertEqual(len(pan.rename_calls),1)
    async def test_partial_rename_resumes_remaining_then_group_moves(self):
        pipeline,pan,ops,store,context=self.setup_case(4)
        pan.partial_rename=2
        with self.assertRaises(JobRetry):await pipeline.run()
        self.assertEqual(pan.move_calls,[])
        pan.partial_rename=None
        again,*_=base.Pipeline().setup_flow(base.plan(4),pan,ops,store)
        result=await again.run()
        self.assertTrue(result["inventory_verified"])
        self.assertEqual(pan.rename_calls[1],[("3","剧.2026.S01E03.mkv"),("4","剧.2026.S01E04.mkv")])
        self.assertEqual(len(pan.move_calls),1)
        self.assertEqual(pan.receives,1)
    async def test_nested_staging_uses_actual_parent_not_staging_root(self):
        pipeline,pan,ops,store,context=self.setup_case(2)
        async def receive(code,pwd,ids,cid):
            pan.receives+=1
            pan.files["staging"]=[api.Entry("nested","source-folder",True)]
            pan.files["nested"]=copy.deepcopy(pan.sources)
            return {"state":True,"data":{"recv_file_count":len(ids),"recv_folder_count":0}}
        pan.share_receive=receive
        await pipeline.run()
        state=await store.get(pipeline.key)
        self.assertEqual({cp["source_id"] for cp in state["items"].values()},{"nested"})
        self.assertEqual(len(pan.rename_calls),1)
        self.assertEqual(len(pan.move_calls),1)
    async def test_subtitles_batch_alongside_video_separate_phase(self):
        pl=base.plan(3)
        for i,item in enumerate(pl["items"],1):item["subs"]=[f"Show.S01E{i:02}.zh.srt"]
        pipeline,pan,ops,store,context=self.setup_case(pl=pl)
        for i in range(1,4):pan.sources.append(api.Entry(str(80+i),f"Show.S01E{i:02}.zh.srt",False,5))
        result=await pipeline.run()
        self.assertEqual(result["subtitle_pending"],[])
        self.assertEqual([len(x) for x in pan.rename_calls],[3,3])
        self.assertEqual([len(x[0]) for x in pan.move_calls],[3,3])
    async def test_subtitle_target_conflict_preserves_unrelated_file(self):
        pl=base.plan();pl["items"][0]["subs"]=["Show.S01E01.zh.srt"]
        pipeline,pan,ops,store,context=self.setup_case(pl=pl)
        pan.sources.append(api.Entry("81","Show.S01E01.zh.srt",False,5))
        pan.files["target"].append(api.Entry("unrelated","剧.2026.S01E01.zh.srt",False,8))
        result=await pipeline.run()
        self.assertEqual(result["subtitle_pending"],["Show.S01E01.zh.srt"])
        self.assertTrue(any(e.remote_id=="unrelated" and e.size==8 for e in pan.files["target"]))
        self.assertTrue(any(e.remote_id=="81" for e in pan.files["staging"]))
    async def test_restore_40_names_uses_one_request_and_one_shared_verification(self):
        pipeline,pan,ops,store,context=self.setup_case(40)
        items={};moved={};pan.files["target"]=[]
        for i in range(1,41):
            canonical=f"剧.S01E{i:02}.mkv";temp=f"剧.S01E{i:02} - p115-1234abcd.mkv"
            items[str(i)]={"moved":True,"remote_id":str(i),"target_id":"target","actual_size":100,
                           "target_name":temp,"restore_to":canonical}
            moved[str(i)]={"name":temp,"path":"网盘/日韩剧/剧 (2026)/Season 01/"+temp,"size":100,"season":1,"episode":i}
            pan.files["target"].append(api.Entry(str(i),temp,False,100))
        pipeline.state={"items":items,"moved":moved}
        pipeline.verify_inventory=AsyncMock(return_value=({},[]))
        result=await pipeline.restore_names()
        self.assertEqual(len(result),40)
        self.assertEqual(len(pan.rename_calls),1)
        self.assertEqual(pan.list_calls.count("target"),3)
        pipeline.verify_inventory.assert_awaited_once()
    async def test_restore_partial_success_persists_completed_before_retry(self):
        pipeline,pan,ops,store,context=self.setup_case(2)
        pipeline.state={"items":{},"moved":{}}
        pan.files["target"]=[]
        for i in range(1,3):
            pipeline.state["items"][str(i)]={"moved":True,"remote_id":str(i),"target_id":"target","actual_size":100,
                "target_name":f"temp{i}.mkv","restore_to":f"canonical{i}.mkv"}
            pipeline.state["moved"][str(i)]={"name":f"temp{i}.mkv","path":f"path/temp{i}.mkv","size":100,"season":1,"episode":i}
            pan.files["target"].append(api.Entry(str(i),f"temp{i}.mkv",False,100))
        pan.partial_rename=1
        with self.assertRaises(JobRetry):await pipeline.restore_names()
        state=await store.get(pipeline.key)
        self.assertNotIn("restore_to",state["items"]["1"])
        self.assertIn("restore_to",state["items"]["2"])
        self.assertEqual(state["moved"]["1"]["name"],"canonical1.mkv")
    async def test_cancel_preserves_entire_batch_plan(self):
        pipeline,pan,ops,store,context=self.setup_case(4)
        pan.cancel_after_rename=context
        with self.assertRaises(JobCancelled):await pipeline.run()
        state=await store.get(pipeline.key)
        self.assertEqual(len(state["items"]),4)
        self.assertEqual(pan.move_calls,[])
        self.assertEqual(pan.deletes,[])
        self.assertNotIn("result",state)

    async def test_multi_season_plan_groups_moves_and_keeps_season_ids(self):
        pl=base.plan(4)
        for item in pl["items"][2:]:
            item["season"]=2
            item["season_dir"]="Season 02"
            item["file_name"]=item["file_name"].replace("S01","S02")
        pipeline,pan,ops,store,context=self.setup_case(pl=pl)
        async def ensure(root,parts):
            return "staging" if parts[0]==".p115-import" else "season2" if parts[-1]=="Season 02" else "target"
        pan.ensure_dir=ensure;pan.files["season2"]=[]
        pipeline.verify_inventory=AsyncMock(return_value=({},[]))
        result=await pipeline.run()
        self.assertEqual(pan.move_calls,[(["1","2"],"target"),(["3","4"],"season2")])
        self.assertEqual(len(pan.rename_calls),1)
        self.assertEqual([r["season"] for r in result["moved"]],[1,1,2,2])
        self.assertTrue(result["inventory_verified"])

    async def test_legacy_checkpoint_after_rename_resumes_without_retransferring(self):
        pipeline,pan,ops,store,context=self.setup_case(2)
        pl=base.plan(2)
        cp={"remote_id":"1","actual_size":100,"target_id":"target","target_name":pl["items"][0]["file_name"]}
        await store.set(pipeline.key,{"items":{"1":cp},"moved":{},"deleted":[],"subtitle_pending":[],
            "staging_id":"staging","target_dirs":{"剧 (2026)/Season 01":"target"},"submitted":True},secret=True)
        pan.files["staging"]=[api.Entry("1",cp["target_name"],False,100,"1"*40),
                              api.Entry("2",pl["items"][1]["share_path"],False,100,"2"*40)]
        result=await pipeline.run()
        self.assertTrue(result["inventory_verified"])
        self.assertEqual(pan.receives,0)
        self.assertEqual(pan.rename_calls,[[('2',pl['items'][1]['file_name'])]])
        self.assertEqual(len(pan.move_calls),1)

    async def test_subtitle_partial_move_retry_only_moves_unfinished(self):
        pl=base.plan(2)
        for i,item in enumerate(pl["items"],1):item["subs"]=[f"Show.S01E{i:02}.zh.srt"]
        pipeline,pan,ops,store,context=self.setup_case(pl=pl)
        for i in range(1,3):pan.sources.append(api.Entry(str(80+i),f"Show.S01E{i:02}.zh.srt",False,5))
        original=pan.move;once=True
        async def move(ids,cid):
            nonlocal once
            if ids==["81","82"] and once:
                once=False
                await original(ids[:1],cid)
                return
            await original(ids,cid)
        pan.move=move
        result=await pipeline.run()
        self.assertEqual(result["subtitle_pending"],[])
        self.assertEqual(pan.move_calls[-1],(["82"],"target"))
        self.assertEqual(len(pan.rename_calls),2)


    async def test_move_succeeded_cancel_before_read_resumes_by_destination_id(self):
        pipeline,pan,ops,store,context=self.setup_case(4)
        original=pan.move
        async def move(ids,cid):
            await original(ids,cid)
            context.cancel=True
        pan.move=move
        with self.assertRaises(JobCancelled):await pipeline.run()
        state=await store.get(pipeline.key)
        self.assertEqual(len(state["items"]),4)
        self.assertEqual(len(state["moved"]),0)
        self.assertEqual(len(pan.files["target"]),4)
        context.cancel=False;pan.move=original
        again,*_=base.Pipeline().setup_flow(base.plan(4),pan,ops,store,context)
        result=await again.run()
        self.assertTrue(result["inventory_verified"])
        self.assertEqual(pan.receives,1)
        self.assertEqual(len(pan.move_calls),1)
        self.assertEqual(len(pan.rename_calls),1)



if __name__=="__main__":unittest.main(verbosity=2)
