import unittest
from unittest.mock import AsyncMock
import p115repair as r
import p115api as a
from movieclaw_sdk.jobs import JobBlocked

class Repair(unittest.IsolatedAsyncioTestCase):
    def evidence(self):
        old="剧.S01E05 - p115-1234abcd.mkv"
        moved={"name":old,"path":"库/剧/"+old,"size":100}
        state={"items":{"x":{"moved":True,"remote_id":"10","target_id":"20"}},"moved":{"x":moved}}
        result={"inventory_verified":True,"moved":[moved]}
        return old,state,result
    async def test_already_canonical_is_read_only(self):
        old,state,result=self.evidence()
        pan=AsyncMock();pan.list_dir.return_value=[a.Entry("10","剧.S01E05.mkv",False,100)]
        rows=await r.inspect_names(pan,{},state,result)
        self.assertEqual(len(rows),1);self.assertFalse(rows[0]["needs_rename"])
        await r.repair_names(pan,rows)
        pan.rename_many.assert_not_awaited()
    async def test_restore_only_exact_remote_id(self):
        old,state,result=self.evidence()
        pan=AsyncMock();pan.list_dir.return_value=[a.Entry("10",old,False,100)]
        rows=await r.inspect_names(pan,{},state,result)
        async def rename(pairs):
            pan.list_dir.return_value=[a.Entry("10","剧.S01E05.mkv",False,100)]
        pan.rename_many.side_effect=rename
        await r.repair_names(pan,rows)
        pan.rename_many.assert_awaited_once_with([("10","剧.S01E05.mkv")])
    async def test_conflict_does_not_overwrite(self):
        old,state,result=self.evidence()
        pan=AsyncMock();pan.list_dir.return_value=[a.Entry("10",old,False,100),a.Entry("11","剧.S01E05.mkv",False,80)]
        with self.assertRaises(JobBlocked):await r.inspect_names(pan,{},state,result)
        pan.rename_many.assert_not_awaited()
    async def test_same_name_wrong_id_rejected(self):
        old,state,result=self.evidence()
        pan=AsyncMock();pan.list_dir.return_value=[a.Entry("11",old,False,100)]
        with self.assertRaises(JobBlocked):await r.inspect_names(pan,{},state,result)
    async def test_size_changed_rejected(self):
        old,state,result=self.evidence()
        pan=AsyncMock();pan.list_dir.return_value=[a.Entry("10",old,False,80)]
        with self.assertRaises(JobBlocked):await r.inspect_names(pan,{},state,result)
    async def test_unverified_rejected(self):
        old,state,result=self.evidence();result["inventory_verified"]=False
        with self.assertRaises(JobBlocked):await r.inspect_names(AsyncMock(),{},state,result)
