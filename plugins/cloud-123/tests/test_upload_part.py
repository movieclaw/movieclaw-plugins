"""真实 SDK/P123Client + 内存存储/MockTransport；禁止真实网络。"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import time
from dataclasses import replace

import httpx
from movieclaw_sdk import drives as dv, net
from cloud_123.backend import Pan123
from tests.test_begin_upload import PART_SIZE
from tests.test_begin_upload import UploadFixture


class UploadPartFixture(UploadFixture):
    def setUp(self):
        super().setUp()
        self.put_hook = None
        self.put_status = 200
        self.put_headers = None
        self.accept_put = True
        self.url_override = None

    async def handler(self, request):
        path = request.url.path
        if path.endswith(('/s3_repare_upload_parts_batch','/s3_upload_object/auth')):
            self.requests.append(request)
            self.assertEqual(request.headers['authorization'],'Bearer '+self.account.credentials['token'])
            if self.reply:
                result = await self.reply(request)
                if result is not None:return result
            payload = json.loads(request.content)
            number = payload.get('partNumberStart',1)
            return httpx.Response(200,json={'code':0,'data':{'presignedUrls':{
                str(number): self.url_override or f'https://storage.invalid/part/{number}?signature=private'}}})
        if request.method == 'PUT':
            self.requests.append(request)
            self.assertNotIn('authorization',request.headers)
            self.assertNotIn('cookie',request.headers)
            self.assertEqual(int(request.headers['content-length']),len(request.content))
            number = int(path.rsplit('/',1)[1])
            digest = hashlib.md5(request.content).hexdigest()
            info = self.info()
            self.assertIn(('upload_data:'+info['generation'],f'put_requested:{number-1}'),self.store.rows)
            if self.accept_put and info['multipart']:
                self.parts = [p for p in self.parts if int(p['PartNumber']) != number]
                self.parts.append({'PartNumber':str(number),'Size':str(len(request.content)),'ETag':'"'+digest+'"'})
            if self.put_hook:
                result = await self.put_hook(request)
                if result is not None:return result
            headers = self.put_headers if self.put_headers is not None else {'ETag':'"'+digest.upper()+'"'}
            return httpx.Response(self.put_status,headers=headers,stream=self.stream(b''))
        return await super().handler(request)

    async def start(self, multipart=False):
        if multipart:
            self.body = b'a'*PART_SIZE+b'b'
            self.fp = replace(self.fp,size=len(self.body),md5=hashlib.md5(self.body).hexdigest())
            self.file['Size'] = len(self.body)
        self.state = (await self.begin()).state
        return self.state

    async def put(self, index=0, data=None, state=None):
        if data is None:data = self.body[index*PART_SIZE:(index+1)*PART_SIZE]
        return await self.backend.upload_part(self.account,state if state is not None else self.state,index,data)

    def puts(self):
        return [r for r in self.requests if r.method == 'PUT']

    def stored(self, key):
        return self.store.rows.get(('upload_data:'+self.state['generation'],key))

    def remote(self, index=0):
        data = self.body[index*PART_SIZE:(index+1)*PART_SIZE]
        return {'PartNumber':str(index+1),'Size':str(len(data)),'ETag':'"'+hashlib.md5(data).hexdigest()+'"'}


class UploadPartTest(UploadPartFixture):
    async def test_single_auth_put_md5_confirmation_and_state(self):
        await self.start()
        self.assertEqual(await self.put(),self.state)
        record = self.stored('part:0')
        self.assertEqual(record,{**self.state,'index':0,'size':len(self.body),
                                 'md5':self.fp.md5.lower(),'etag':self.fp.md5.lower()})
        self.assertEqual(self.stored('put_requested:0'),{k:v for k,v in record.items() if k != 'etag'})
        self.assertEqual(len(self.puts()),1)
        self.assertEqual(self.puts()[0].content,self.body)
        self.assertEqual(self.count('/s3_upload_object/auth'),1)
        self.assertEqual(self.count('/s3_repare_upload_parts_batch'),0)
        self.assertEqual(self.paced,len(self.requests))
        self.assertNotIn('signature',json.dumps(list(self.store.rows.values())))
        self.assertNotIn(self.account.credentials['token'],json.dumps(list(self.store.rows.values())))
        self.assertTrue(self.stream_closed)
        self.assertFalse(self.body_entered.is_set())  # PUT 响应正文不参与校验，不读入内存。

    async def test_multipart_one_based_ranges_and_short_last_part(self):
        await self.start(True)
        self.assertEqual(await self.put(),self.state)
        self.assertEqual(await self.put(1),self.state)
        payloads = [json.loads(r.content) for r in self.requests if r.url.path.endswith('/s3_repare_upload_parts_batch')]
        self.assertEqual([(p['partNumberStart'],p['partNumberEnd']) for p in payloads],[(1,2),(2,3)])
        self.assertEqual([len(r.content) for r in self.puts()],[PART_SIZE,1])
        self.assertEqual(self.stored('part:1')['md5'],hashlib.md5(b'b').hexdigest())
        self.assertEqual(self.paced,len(self.requests))
        self.assertEqual((await self.begin()).next_part,2)

    async def test_invalid_input_rejected_before_network(self):
        await self.start()
        before = len(self.requests)
        for index,data,state in ((True,b'x',self.state),(-1,b'x',self.state),(0,b'',self.state),
                                  (0,bytearray(b'x'),self.state),(0,b'x',{})):
            with self.subTest(index=index,state=state),self.assertRaises(dv.Conflict):
                await self.put(index,data,state)
        self.assertEqual(len(self.requests),before)

    async def test_wrong_length_index_or_single_digest_never_put(self):
        await self.start()
        for index,data in ((1,self.body),(0,self.body[:-1]),(0,b'x'*len(self.body))):
            with self.subTest(index=index,length=len(data)),self.assertRaises(dv.Conflict):await self.put(index,data)
        self.assertEqual(self.puts(),[])
        self.assertIsNone(self.stored('put_requested:0'))

    async def test_missing_retired_or_changed_session_never_put(self):
        await self.start()
        original = copy.deepcopy(self.store.rows)
        scope = 'upload:'+self.state['session_key']
        for mutation in ('retired','intent','info','index','binding'):
            with self.subTest(mutation=mutation):
                self.store.rows = copy.deepcopy(original)
                if mutation == 'retired':self.store.rows[(scope,'retired:'+self.state['generation'])] = self.state
                elif mutation == 'intent':del self.store.rows[(scope,'intent:'+self.state['generation'])]
                elif mutation == 'info':del self.store.rows[('upload_data:'+self.state['generation'],'info')]
                elif mutation == 'index':del self.store.rows[('upload_gc:'+self.state['session_key'][:2],self.state['session_key']+':'+self.state['generation'])]
                else:self.info()['expected_md5'] = 'f'*32
                with self.assertRaises(dv.Conflict):await self.put()
        self.assertEqual(self.puts(),[])

    async def test_identity_or_stored_account_mismatch_never_put(self):
        await self.start()
        self.account = replace(self.account,user_id='67890')
        with self.assertRaises(dv.Conflict):await self.put()
        async def reply(request):
            if request.url.path.endswith('/user/info'):
                return httpx.Response(200,json={'code':0,'data':{'UID':67890}})
        self.reply = reply
        with self.assertRaises(dv.Conflict):await self.put()
        self.assertEqual(self.puts(),[])

    async def test_single_confirmation_survives_restart_without_second_put(self):
        await self.start(); await self.put()
        self.backend = Pan123(self.store)
        self.assertEqual(await self.put(),self.state)
        self.assertEqual(len(self.puts()),1)

    async def test_remote_only_matching_part_saves_confirmation_without_put(self):
        await self.start(True); self.parts = [self.remote()]
        self.assertEqual(await self.put(),self.state)
        self.assertIsNotNone(self.stored('part:0'))
        self.assertIsNone(self.stored('put_requested:0'))
        self.assertEqual(self.puts(),[])
        self.backend = Pan123(self.store)
        self.assertEqual((await self.begin()).next_part,1)

    async def test_unknown_put_found_after_restart_is_not_repeated(self):
        await self.start(True)
        async def fail(request):raise httpx.ReadTimeout('private signature')
        self.put_hook = fail
        with self.assertRaises(dv.Transient) as caught:await self.put()
        self.assertNotIn('private',str(caught.exception))
        self.assertIsNone(self.stored('part:0'))
        self.put_hook = None; self.backend = Pan123(self.store)
        self.assertEqual(await self.put(),self.state)
        self.assertEqual(len(self.puts()),1)

    async def test_unknown_missing_multipart_retries_only_identical_bytes(self):
        await self.start(True); self.accept_put = False
        async def fail(request):raise httpx.WriteTimeout('private')
        self.put_hook = fail
        with self.assertRaises(dv.Transient):await self.put()
        self.put_hook = None; self.accept_put = True; self.backend = Pan123(self.store)
        with self.assertRaises(dv.Conflict):await self.put(data=b'z'*PART_SIZE)
        self.assertEqual(len(self.puts()),1)
        self.assertEqual(await self.put(),self.state)
        self.assertEqual(len(self.puts()),2)
        self.assertEqual(self.puts()[0].content,self.puts()[1].content)

    async def test_known_confirmation_but_remote_missing_reputs_same_bytes(self):
        await self.start(True); await self.put()
        before = copy.deepcopy(self.stored('part:0')); self.parts = []
        with self.assertRaises(dv.Conflict):await self.put(data=b'z'*PART_SIZE)
        await self.put()
        self.assertEqual(len(self.puts()),2)
        self.assertEqual(self.stored('part:0'),before)

    async def test_remote_mismatch_or_missing_previous_part_stops(self):
        await self.start(True)
        self.parts = [{**self.remote(),'ETag':'f'*32}]
        with self.assertRaises(dv.Conflict):await self.put()
        self.parts = []
        with self.assertRaises(dv.Conflict):await self.put(1)
        self.assertEqual(self.puts(),[])

    async def test_malformed_remote_list_never_puts(self):
        await self.start(True)
        self.parts = [{**self.remote(),'ETag':'W/"'+'a'*32+'"'}]
        with self.assertRaises(dv.ApiChanged):await self.put()
        self.parts = [{**self.remote(),'PartNumber':'3'}]
        with self.assertRaises(dv.Conflict):await self.put()
        self.assertEqual(self.puts(),[])

    async def test_put_etag_missing_malformed_and_mismatch_never_confirms(self):
        await self.start(True)
        for etag,error in ((None,dv.ApiChanged),('W/"'+'a'*32+'"',dv.ApiChanged),
                           ('"'+'a'*32+'-2"',dv.ApiChanged),('bad',dv.ApiChanged),('f'*32,dv.Conflict)):
            with self.subTest(etag=etag):
                self.parts = []; self.put_headers = {} if etag is None else {'ETag':etag}
                with self.assertRaises(error):await self.put()
                self.assertIsNone(self.stored('part:0'))
        self.assertEqual(len(self.puts()),5)

    async def test_put_http_failures_are_not_auth_errors_or_confirmation(self):
        await self.start(True)
        for status,error in ((302,dv.ApiChanged),(403,dv.Transient),(401,dv.Transient),(429,dv.RateLimited),(503,dv.Transient)):
            with self.subTest(status=status):
                self.parts = []; self.put_status = status
                self.put_headers = {'Location':'https://elsewhere.invalid/private','ETag':self.remote()['ETag']}
                with self.assertRaises(error):await self.put()
                self.assertIsNone(self.stored('part:0'))
        self.assertTrue(all(r.url.host != 'elsewhere.invalid' for r in self.requests))

    async def test_unsafe_presigned_url_never_sets_put_intent(self):
        await self.start()
        for url in ('http://unsafe.invalid/file','https://user:secret@storage.invalid/file',
                    'https://storage.invalid/file#fragment','https://storage.invalid:bad/file'):
            with self.subTest(url=url):
                self.url_override = url
                with self.assertRaises(dv.ApiChanged):await self.put()
                self.assertIsNone(self.stored('put_requested:0'))
        self.assertEqual(self.puts(),[])

    async def test_prepare_errors_or_missing_url_leave_no_put_intent(self):
        await self.start()
        for code,data,error in ((401,{},dv.AuthExpired),(429,{},dv.RateLimited),
                                (0,{},dv.ApiChanged),(0,{'presignedUrls':{'2':'https://storage.invalid/part/2'}},dv.ApiChanged)):
            with self.subTest(code=code,data=data):
                async def reply(request):
                    if request.url.path.endswith('/s3_upload_object/auth'):
                        return httpx.Response(200,json={'code':code,'data':data})
                self.reply = reply
                with self.assertRaises(error):await self.put()
                self.assertIsNone(self.stored('put_requested:0'))
        self.assertEqual(self.puts(),[])

    async def test_unquoted_etag_is_accepted(self):
        await self.start(); self.put_headers = {'ETag':self.fp.md5}
        await self.put()
        self.assertEqual(self.stored('part:0')['etag'],self.fp.md5.lower())

    async def test_confirmation_conflict_and_read_error_never_overwrite_or_put(self):
        await self.start(True)
        self.record(0,'f'*32)
        with self.assertRaises(dv.Conflict):await self.put()
        self.assertEqual(self.stored('part:0')['md5'],'f'*32)
        async def hook(method,scope,key,value):
            if method == 'get' and key == 'part:0':raise TimeoutError('private')
        self.store.hook = hook
        with self.assertRaises(dv.Transient):await self.put()
        self.assertEqual(self.puts(),[])

    async def test_activity_hint_does_not_revoke_confirmation_but_cancel_propagates(self):
        await self.start(); await self.put()
        self.assertGreaterEqual(self.stored('activity')['at'],time.time()-10)
        async def fail(method,scope,key,value):
            if method == 'set' and key == 'activity':raise TimeoutError
        self.store.hook = fail
        self.assertEqual(await self.put(),self.state)
        async def cancel(method,scope,key,value):
            if method == 'set' and key == 'activity':raise asyncio.CancelledError('external')
        self.store.hook = cancel
        with self.assertRaises(asyncio.CancelledError):await self.put()
        self.assertIsNotNone(self.stored('part:0'))
        self.assertEqual(len(self.puts()),1)

    async def test_put_budget_rejection_is_retryable_without_intent(self):
        await self.start()
        original = net.pacer.get()
        async def pace():
            await original()
            if self.count('/s3_upload_object/auth'):raise dv.RateLimited('budget')
        ctx = net.pacer.set(pace)
        try:
            with self.assertRaises(dv.RateLimited):await self.put()
            self.assertIs(net.pacer.get(),pace)
        finally:net.pacer.reset(ctx)
        self.assertIsNone(self.stored('put_requested:0'))
        self.assertEqual(self.puts(),[])
        await self.put()
        self.assertEqual(len(self.puts()),1)

    async def test_cancel_waiting_for_put_budget_leaves_no_intent(self):
        await self.start(); entered = asyncio.Event()
        original = net.pacer.get()
        async def pace():
            await original()
            if self.count('/s3_upload_object/auth'):
                entered.set(); await asyncio.Event().wait()
        ctx = net.pacer.set(pace)
        try:
            task = asyncio.create_task(self.put()); await entered.wait()
            before = len(self.requests),len(self.store.calls)
            self.close_error = RuntimeError('private close'); task.cancel('external')
            with self.assertRaises(asyncio.CancelledError) as caught:await task
            self.assertEqual(caught.exception.args,('external',))
            self.assertEqual((len(self.requests),len(self.store.calls)),before)
        finally:net.pacer.reset(ctx)
        self.assertIsNone(self.stored('put_requested:0'))
        self.close_error = None
        await self.put()
        self.assertEqual(len(self.puts()),1)

    async def test_cancel_during_put_keeps_intent_and_reconciles(self):
        await self.start(True); entered = asyncio.Event()
        async def wait(request):
            entered.set(); await asyncio.Event().wait()
        self.put_hook = wait
        task = asyncio.create_task(self.put()); await entered.wait()
        before = len(self.requests),len(self.store.calls)
        self.close_error = RuntimeError('private close'); task.cancel('external')
        with self.assertRaises(asyncio.CancelledError) as caught:await task
        self.assertEqual(caught.exception.args,('external',))
        self.assertEqual((len(self.requests),len(self.store.calls)),before)
        self.assertIsNone(self.stored('part:0'))
        self.put_hook = None; self.close_error = None; self.backend = Pan123(self.store)
        await self.put()
        self.assertEqual(len(self.puts()),1)

    async def test_put_intent_unknown_prevents_sending(self):
        await self.start()
        async def hook(method,scope,key,value):
            if method == 'set' and key == 'put_requested:0':
                self.store.rows[(scope,key)] = copy.deepcopy(value)
                raise TimeoutError('private')
        self.store.hook = hook
        with self.assertRaises(dv.Transient):await self.put()
        self.assertEqual(self.puts(),[])
        self.store.hook = None; self.backend = Pan123(self.store)
        with self.assertRaises(dv.Conflict):await self.put()
        self.assertEqual(self.puts(),[])

    async def test_confirmation_lost_reply_recovers_after_restart(self):
        await self.start()
        async def hook(method,scope,key,value):
            if method == 'set' and key == 'part:0':
                self.store.rows[(scope,key)] = copy.deepcopy(value)
                raise TimeoutError('private')
        self.store.hook = hook
        with self.assertRaises(dv.Transient):await self.put()
        self.store.hook = None; self.backend = Pan123(self.store)
        self.assertEqual(await self.put(),self.state)
        self.assertEqual(len(self.puts()),1)

    async def test_confirmation_missing_retries_identical_value_in_same_process(self):
        await self.start(); writes = []
        async def hook(method,scope,key,value):
            if method == 'set' and key == 'part:0':
                writes.append(copy.deepcopy(value))
                if len(writes) == 1:raise TimeoutError
        self.store.hook = hook
        with self.assertRaises(dv.Transient):await self.put()
        self.assertEqual(await self.put(),self.state)
        self.assertEqual(writes[0],writes[1])
        self.assertEqual(len(self.puts()),1)

    async def test_cancel_confirmation_no_extra_io_then_recovers(self):
        await self.start(); entered = asyncio.Event()
        async def hook(method,scope,key,value):
            if method == 'set' and key == 'part:0':
                entered.set(); await asyncio.Event().wait()
        self.store.hook = hook
        task = asyncio.create_task(self.put()); await entered.wait()
        before = len(self.requests),len(self.store.calls); task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        self.assertEqual((len(self.requests),len(self.store.calls)),before)
        self.store.hook = None
        await self.put()
        self.assertEqual(len(self.puts()),1)

    async def test_single_unknown_stops_without_product_and_does_not_list_parts(self):
        await self.start()
        async def fail(request):raise httpx.ReadTimeout('private')
        self.put_hook = fail
        with self.assertRaises(dv.Transient):await self.put()
        count = self.count('/s3_list_upload_parts')
        self.put_hook = None; self.backend = Pan123(self.store)
        with self.assertRaises(dv.Conflict):await self.put()
        self.assertEqual(self.count('/s3_list_upload_parts'),count)
        self.assertEqual(len(self.puts()),1)

    async def test_single_unknown_product_readback_returns_state_without_fake_confirmation(self):
        await self.start()
        async def fail(request):raise httpx.ReadTimeout('private')
        self.put_hook = fail
        with self.assertRaises(dv.Transient):await self.put()
        self.put_hook = None; self.listed = True; self.backend = Pan123(self.store)
        self.assertEqual(await self.put(),self.state)
        self.assertEqual(self.info_count,2)
        self.assertIsNone(self.stored('part:0'))
        self.assertEqual(len(self.puts()),1)
        self.content_body = b'z'*len(self.body)
        with self.assertRaises(dv.Conflict):await self.put()
        self.assertEqual(len(self.puts()),1)

    async def test_merge_intent_never_puts_or_merges(self):
        await self.start()
        self.store.rows[('upload_data:'+self.state['generation'],'merge_requested')] = {'sent':True}
        with self.assertRaises(dv.Conflict):await self.put()
        self.assertEqual(self.puts(),[])
        self.assertEqual(self.count('/s3_upload_object/auth'),0)

    async def test_etag_failure_survives_response_and_client_close_errors(self):
        await self.start(); self.put_headers = {}
        async def close_failure(request):self.close_error = RuntimeError('private close')
        self.put_hook = close_failure
        with self.assertRaises(dv.ApiChanged):await self.put()
        self.assertIsNone(self.stored('part:0'))
        self.assertTrue(self.stream_closed)

    async def test_successful_put_close_failure_is_not_confirmation(self):
        await self.start()
        async def close_failure(request):self.close_error = RuntimeError('private close')
        self.put_hook = close_failure
        with self.assertRaises(dv.Transient) as caught:await self.put()
        self.assertNotIn('private',str(caught.exception))
        self.assertIsNone(self.stored('part:0'))

    async def test_client_close_failure_after_valid_response_is_not_confirmation(self):
        await self.start()
        async def close_failure(request):
            self.close_error = RuntimeError('private client close')
            return httpx.Response(200,headers={'ETag':self.fp.md5},content=b'')
        self.put_hook = close_failure
        with self.assertRaises(dv.Transient) as caught:await self.put()
        self.assertIn('连接关闭失败',str(caught.exception))
        self.assertNotIn('private',str(caught.exception))
        self.assertIsNone(self.stored('part:0'))

    async def test_new_cancellation_from_put_close_is_preserved(self):
        await self.start()
        async def close_cancel(request):self.close_error = asyncio.CancelledError('close cancellation')
        self.put_hook = close_cancel
        with self.assertRaises(asyncio.CancelledError):await self.put()
        self.assertIsNone(self.stored('part:0'))

    async def test_concurrent_same_part_has_one_put(self):
        await self.start(True)
        first,second = await asyncio.gather(self.put(),self.put())
        self.assertEqual((first,second),(self.state,self.state))
        self.assertEqual(len(self.puts()),1)
