import asyncio
import json
import unittest
from movieclaw_sdk import drives as dv
from cloud_123_personal.backend import Personal123
from tests.fake123 import Fake123
from tests.store import MemoryStore
from tests.test_backend import fingerprint


class JournalTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fake=Fake123();self.store=MemoryStore()
        self.account=dv.DriveAccount('17',{'token':self.fake.token})
        self.backend=Personal123(self.fake.transport(),store=self.store)
    async def asyncTearDown(self):await self.backend.aclose()
    async def restart(self):
        await self.backend.aclose();self.backend=Personal123(self.fake.transport(),store=self.store)
    async def begin(self,body=b'abcdefgh',state=None):
        return await self.backend.begin_upload(self.account,'','a',fingerprint(body),state,None)

    async def test_host_checkpoint_loss_and_restart_restore_ack(self):
        initial=await self.begin()
        await self.backend.upload_part(self.account,initial.state,0,b'abcd')
        await self.restart()
        step=await self.begin(state=initial.state)
        self.assertEqual(step.next_part,1)
        lost_state=await self.begin()  # host lost its checkpoint entirely
        self.assertEqual(lost_state.next_part,1)
        state=await self.backend.upload_part(self.account,step.state,1,b'efgh')
        result=await self.backend.finish_upload(self.account,state)
        await self.restart()
        self.assertEqual((await self.begin()).file,result)
        self.assertEqual(sum(p.endswith('upload_request') for p,_,_ in self.fake.calls),1)

    async def test_lost_put_ack_restart_replays_identical_part(self):
        initial=await self.begin();self.fake.lose_put=True
        with self.assertRaises(dv.Transient):await self.backend.upload_part(self.account,initial.state,0,b'abcd')
        await self.restart();step=await self.begin()
        self.assertEqual(step.next_part,0)
        with self.assertRaises(dv.Conflict):await self.backend.upload_part(self.account,step.state,0,b'xxxx')
        state=await self.backend.upload_part(self.account,step.state,0,b'abcd')
        self.assertEqual(state['next'],1)

    async def test_missing_store_prevents_upload_requests(self):
        await self.backend.aclose();self.backend=Personal123(self.fake.transport())
        with self.assertRaises(dv.ApiChanged):await self.begin()
        self.assertFalse(self.fake.calls)

    async def test_persist_intent_failure_prevents_initialization(self):
        self.store.fail_phase='initializing'
        with self.assertRaises(dv.Transient):await self.begin()
        self.assertFalse(any(p.endswith('upload_request') for p,_,_ in self.fake.calls))

    async def test_init_network_loss_remains_blocked_after_restart(self):
        self.fake.lose_init=True
        with self.assertRaises(dv.Transient):await self.begin()
        await self.restart();before=len(self.fake.calls)
        with self.assertRaises(dv.ApiChanged):await self.begin()
        self.assertEqual(len(self.fake.calls),before)

    async def test_init_ack_then_store_failure_remains_uncertain(self):
        self.store.fail_phase='parts'
        with self.assertRaises(dv.Transient):await self.begin()
        self.store.fail_phase=None;await self.restart()
        with self.assertRaises(dv.ApiChanged):await self.begin()
        self.assertEqual(sum(p.endswith('upload_request') for p,_,_ in self.fake.calls),1)

    async def test_lost_store_response_recovers_committed_record(self):
        self.store.lose_phase='parts'
        with self.assertRaises(dv.Transient):await self.begin()
        self.store.lose_phase=None;await self.restart()
        self.assertEqual((await self.begin()).next_part,0)
        self.assertEqual(sum(p.endswith('upload_request') for p,_,_ in self.fake.calls),1)

    async def test_put_ack_checkpoint_failure_can_retry_after_restart(self):
        step=await self.begin()
        original=self.store.set
        async def fail_ack(key,value,**kwargs):
            if value['next']==1:raise OSError('synthetic ACK persistence failure')
            await original(key,value,**kwargs)
        self.store.set=fail_ack
        with self.assertRaises(dv.Transient):await self.backend.upload_part(self.account,step.state,0,b'abcd')
        self.store.set=original;await self.restart()
        step=await self.begin();self.assertEqual(step.next_part,0)
        self.assertEqual((await self.backend.upload_part(self.account,step.state,0,b'abcd'))['next'],1)

    async def test_lost_merge_ack_blocks_across_restart_and_missing_host_state(self):
        step=await self.begin(b'abc');state=await self.backend.upload_part(self.account,step.state,0,b'abc')
        self.fake.lose_complete=True
        with self.assertRaises(dv.Transient):await self.backend.finish_upload(self.account,state)
        await self.restart();before=len(self.fake.calls)
        with self.assertRaises(dv.ApiChanged):await self.begin(b'abc')
        with self.assertRaises(dv.ApiChanged):await self.backend.finish_upload(self.account,state)
        self.assertEqual(len(self.fake.calls),before)

    async def test_merge_ack_persisted_before_stat_failure_resumes_stat_only(self):
        step=await self.begin(b'abc');state=await self.backend.upload_part(self.account,step.state,0,b'abc')
        self.fake.lose_stat=True
        with self.assertRaises(dv.Transient):await self.backend.finish_upload(self.account,state)
        await self.restart();result=(await self.begin(b'abc')).file
        self.assertEqual(result.size,3)
        self.assertEqual(sum(p.endswith('upload_complete/v2') for p,_,_ in self.fake.calls),1)

    async def test_merge_success_with_failed_commit_checkpoint_stays_uncertain(self):
        step=await self.begin(b'abc');state=await self.backend.upload_part(self.account,step.state,0,b'abc')
        self.store.fail_phase='committed'
        with self.assertRaises(dv.Transient):await self.backend.finish_upload(self.account,state)
        self.store.fail_phase=None;await self.restart()
        before=len(self.fake.calls)
        with self.assertRaises(dv.ApiChanged):await self.begin(b'abc')
        self.assertEqual(len(self.fake.calls),before)

    async def test_alpha3_state_not_migrated_without_confirmation_journal(self):
        step=await self.begin();state={**step.state,'version':1}
        before=len(self.fake.calls)
        with self.assertRaises(dv.Conflict):await self.begin(state=state)
        self.assertEqual(len(self.fake.calls),before)

    async def test_reuse_ack_survives_restart(self):
        self.fake.row(999,'a',3,content=b'abc');self.fake.reuse_id=999;self.fake.lose_stat=True
        with self.assertRaises(dv.Transient):await self.begin(b'abc')
        await self.restart();self.assertEqual((await self.begin(b'abc')).file.remote_id,'999')

    async def test_journal_obeys_real_host_scope_and_size_validation(self):
        from movieclaw_api.services.plugin_data import _check,MAX_VALUE_BYTES
        await self.begin()
        for (scope,key),value in self.store.values.items():
            _check(scope,key)
            self.assertLess(len(json.dumps(value,ensure_ascii=False).encode()),MAX_VALUE_BYTES)

    async def test_records_are_encrypted_and_contain_no_credentials_or_urls(self):
        step=await self.begin();await self.backend.upload_part(self.account,step.state,0,b'abcd')
        wire=json.dumps(self.store.writes)
        for forbidden in (self.fake.token,'https://','password','phone','presignedUrls','authorization'):
            self.assertNotIn(forbidden,wire)
        self.assertTrue(all(secret for _,_,secret in self.store.writes))
        self.assertLess(max(len(json.dumps(r).encode()) for _,r,_ in self.store.writes),60*1024)

    async def test_corrupt_log_does_not_initialize(self):
        step=await self.begin();key=next(iter(self.store.values))
        self.store.values[key]['version']=999
        before=len(self.fake.calls)
        with self.assertRaises(dv.ApiChanged):await self.begin()
        self.assertEqual(len(self.fake.calls),before)

    async def test_state_tampering_and_wrong_account_cannot_reuse_journal(self):
        step=await self.begin()
        bad={**step.state,'upload':{**step.state['upload'],'Key':'other'}}
        with self.assertRaises(dv.Conflict):await self.begin(state=bad)
        with self.assertRaises(dv.Conflict):await self.backend.upload_part(dv.DriveAccount('18',{}),step.state,0,b'abcd')

    async def test_same_file_concurrent_begin_creates_one_session(self):
        one,two=await asyncio.gather(self.begin(),self.begin())
        self.assertEqual(one.state,two.state)
        self.assertEqual(sum(p.endswith('upload_request') for p,_,_ in self.fake.calls),1)
