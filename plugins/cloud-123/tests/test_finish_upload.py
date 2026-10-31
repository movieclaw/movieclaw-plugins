"""合并及 P/D 两条完成路径；复用真实 SDK/P123Client 和禁止联网的内存夹具。"""
from __future__ import annotations

import asyncio
import copy
import json
import time
from dataclasses import replace
from unittest.mock import patch

import httpx
from movieclaw_sdk import drives as dv, net
from cloud_123.backend import Pan123
from cloud_123.upload_store import UploadStore, RETENTION, GRACE
from tests.test_upload_part import UploadPartFixture


class FinishUploadTest(UploadPartFixture):
    def setUp(self):
        super().setUp()
        self.complete_data = {'file_info':{'FileId':99,'AbnormalAlert':1},'FileId':77}
        self.complete_code = 0
        self.complete_status = 200
        self.complete_visible = True
        self.complete_hook = None

    async def handler(self, request):
        if request.url.path.endswith('/upload_complete/v2'):
            self.requests.append(request)
            self.assertEqual(request.headers['authorization'],'Bearer '+self.account.credentials['token'])
            self.assertEqual(self.stored('merge_requested'),self.state)
            if self.complete_visible:self.listed = True
            if self.complete_hook:
                result = await self.complete_hook(request)
                if result is not None:return result
            return httpx.Response(self.complete_status,json={'code':self.complete_code,'data':self.complete_data,
                                                            'message':'private upstream message'})
        return await super().handler(request)

    async def ready(self, multipart=False):
        await self.start(multipart)
        await self.put()
        if multipart:await self.put(1)

    async def finish(self, state=None):
        return await self.backend.finish_upload(self.account,self.state if state is None else state)

    def merges(self):
        return [r for r in self.requests if r.url.path.endswith('/upload_complete/v2')]

    async def test_single_process_path_final_id_without_mandatory_terminal_write(self):
        await self.ready()
        del self.file['Etag']; del self.file['S3KeyFlag']
        before_parts = self.count('/s3_list_upload_parts')
        async def hook(method,scope,key,value):
            if method == 'set' and key.startswith('retired:'):raise TimeoutError('optional maintenance')
            if method == 'set' and key != 'merge_requested':raise AssertionError('unexpected terminal write')
        self.store.hook = hook
        result = await self.finish()
        self.assertEqual(result,dv.RemoteFile('99','movie.mkv',len(self.body),parent_id='1',sha1=None))
        self.assertEqual(dv.remote_file_from(dv.remote_file_dict(result)),result)
        payload = json.loads(self.merges()[0].content)
        self.assertEqual(payload,{'bucket':'bucket','key':'key','storagenode':'node',
                                  'uploadid':'upload-secret','fileid':'77','ismultipart':False})
        self.assertEqual(self.count('/s3_list_upload_parts'),before_parts)
        self.assertEqual(self.count('/download_info'),0)
        self.assertEqual(self.info_count,1)
        self.assertEqual(self.paced,len(self.requests))
        self.assertEqual(self.stored('merge_requested'),self.state)

    async def test_multipart_process_path_requires_complete_reconciliation(self):
        await self.ready(True)
        before = self.count('/s3_list_upload_parts')
        result = await self.finish()
        self.assertEqual(result.remote_id,'99')
        self.assertEqual(result.size,len(self.body))
        self.assertGreater(self.count('/s3_list_upload_parts'),before)
        self.assertTrue(json.loads(self.merges()[0].content)['ismultipart'])
        self.assertEqual(self.count('/download_info'),0)
        self.assertEqual(self.paced,len(self.requests))

    async def test_existing_product_ignores_unreadable_parts_pending_writes_and_merge_marker(self):
        await self.start(); self.listed = True
        scope = 'upload_data:'+self.state['generation']
        self.backend._uploads.data.pending[(scope,'part:0')] = {**self.state,'broken':True}
        self.store.rows[(scope,'merge_requested')] = {'broken':True}
        async def hook(method,s,key,value):
            if (method in ('delete','items') or method == 'set' and not key.startswith('retired:')
                    or key and key.startswith(('part:','put_requested:','merge_requested','activity'))):
                raise AssertionError('product path must not touch parts or maintenance')
        self.store.hook = hook
        result = await self.finish()
        self.assertEqual(result.remote_id,'99')
        self.assertEqual(self.info_count,2)
        self.assertEqual(self.count('/download_info'),1)
        self.assertEqual(self.merges(),[])
        self.assertEqual(self.count('/s3_list_upload_parts'),0)
        self.assertTrue(self.backend._uploads.data.pending)

    async def test_existing_product_wrong_md5_never_falls_back_to_merge(self):
        await self.ready(); self.listed = True; self.content_body = b'z'*len(self.body)
        with self.assertRaises(dv.Conflict):await self.finish()
        self.assertEqual(self.merges(),[])
        self.assertIsNone(self.stored('merge_requested'))

    async def test_unknown_merge_empty_s3_recovers_readonly_after_restart(self):
        await self.ready()
        self.store.rows[('upload_data:'+self.state['generation'], 'merge_requested')] = dict(self.state)
        self.file['S3KeyFlag'] = ''; self.listed = True
        self.backend = Pan123(self.store)
        before = len(self.requests)
        result = await self.finish()
        self.assertEqual(result.remote_id, '99')
        self.assertIsNone(result.sha1)
        requests = self.requests[before:]
        download = next(r for r in requests if r.url.path.endswith('/download_info'))
        self.assertEqual(json.loads(download.content)['s3keyflag'], '')
        content = [r for r in requests if r.url.host == 'content.invalid']
        self.assertEqual([r.url.path for r in content], ['/redirect', '/file'])
        self.assertEqual(content[-1].headers['range'], f'bytes=0-{len(self.body)-1}')
        self.assertEqual(self.info_count, 2)
        self.assertEqual(self.merges(), [])
        self.assertFalse(any(r.method == 'PUT' or r.url.path.endswith(
            ('/upload_request', '/s3_list_upload_parts')) for r in requests))

    async def test_existing_product_size_mismatch_stops_before_download(self):
        await self.ready(); self.listed = True; self.file['Size'] += 1
        with self.assertRaises(dv.Conflict):await self.finish()
        self.assertEqual(self.merges(),[])
        self.assertEqual(self.count('/download_info'),0)

    async def test_required_readback_unavailable_never_uses_process_path(self):
        await self.ready(); self.listed = True; self.content_status = 403
        with self.assertRaises(dv.Transient):await self.finish()
        self.assertEqual(self.merges(),[])

    async def test_invalid_reference_stops_before_network(self):
        await self.ready(); before = len(self.requests)
        for state in ({},{**self.state,'schema':True},{**self.state,'generation':'bad'}):
            with self.subTest(state=state),self.assertRaises(dv.Conflict):await self.finish(state)
        self.assertEqual(len(self.requests),before)

    async def test_untrusted_info_or_retired_session_cannot_use_existing_product(self):
        await self.ready(); self.listed = True
        saved = copy.deepcopy(self.store.rows)
        scope = 'upload:'+self.state['session_key']
        for mutation in ('retired','intent','info','md5','binding'):
            with self.subTest(mutation=mutation):
                self.store.rows = copy.deepcopy(saved)
                if mutation == 'retired':self.store.rows[(scope,'retired:'+self.state['generation'])] = self.state
                elif mutation == 'intent':del self.store.rows[(scope,'intent:'+self.state['generation'])]
                elif mutation == 'info':del self.store.rows[('upload_data:'+self.state['generation'],'info')]
                elif mutation == 'md5':del self.info()['expected_md5']
                else:self.info()['name'] = 'other.mkv'
                with self.assertRaises(dv.Conflict):await self.finish()
        self.assertEqual(self.count('/download_info'),0)
        self.assertEqual(self.merges(),[])

    async def test_unresolved_retirement_prevents_merge(self):
        await self.ready()
        scope = 'upload:'+self.state['session_key']
        self.backend._uploads.data.pending[(scope,'retired:'+self.state['generation'])] = self.state
        with self.assertRaises(dv.Conflict):await self.finish()
        self.assertEqual(self.merges(),[])

    async def test_identity_mismatch_stops_before_store_or_merge(self):
        await self.ready(); before = len(self.store.calls)
        self.account = replace(self.account,user_id='67890')
        with self.assertRaises(dv.Conflict):await self.finish()
        self.assertEqual(len(self.store.calls),before)
        self.assertEqual(self.merges(),[])

    async def test_missing_or_bad_local_confirmation_prevents_merge(self):
        await self.start()
        with self.assertRaises(dv.Conflict):await self.finish()
        self.store.rows[('upload_data:'+self.state['generation'],'part:0')] = {'broken':True}
        with self.assertRaises(dv.Conflict):await self.finish()
        self.assertEqual(self.merges(),[])
        self.assertIsNone(self.stored('merge_requested'))

    async def test_confirmed_part_with_conflicting_put_intent_cannot_merge(self):
        await self.ready(); self.stored('put_requested:0')['md5'] = 'f'*32
        with self.assertRaises(dv.Conflict):await self.finish()
        self.assertEqual(self.merges(),[])

    async def test_missing_conflicting_or_malformed_remote_parts_prevents_merge(self):
        await self.ready(True)
        saved = copy.deepcopy(self.parts)
        for parts,error in ((saved[:1],dv.Conflict),([{**saved[0],'ETag':'f'*32},saved[1]],dv.Conflict),
                            ([{**saved[0],'ETag':'bad'},saved[1]],dv.ApiChanged),
                            ([*saved,{**saved[0],'PartNumber':'3'}],dv.ApiChanged)):
            with self.subTest(parts=parts):
                self.parts = parts
                with self.assertRaises(error):await self.finish()
        self.assertEqual(self.merges(),[])
        self.assertIsNone(self.stored('merge_requested'))

    async def test_merge_intent_unknown_without_product_does_not_read_parts(self):
        await self.ready()
        self.store.rows[('upload_data:'+self.state['generation'],'merge_requested')] = self.state
        async def hook(method,scope,key,value):
            if key and key.startswith(('part:','put_requested:')):raise AssertionError('parts must not be read')
        self.store.hook = hook
        with self.assertRaises(dv.Conflict):await self.finish()
        self.assertEqual(self.merges(),[])

    async def test_merge_response_lost_then_restart_uses_product_readback(self):
        await self.ready()
        async def fail(request):raise httpx.ReadTimeout('private signed url')
        self.complete_hook = fail
        with self.assertRaises(dv.Transient) as caught:await self.finish()
        self.assertNotIn('private',str(caught.exception))
        self.complete_hook = None; self.backend = Pan123(self.store)
        self.assertEqual((await self.finish()).remote_id,'99')
        self.assertEqual(len(self.merges()),1)
        self.assertEqual(self.count('/download_info'),1)

    async def test_merge_response_unknown_without_product_never_repeats(self):
        await self.ready(); self.complete_visible = False
        async def fail(request):raise httpx.ReadTimeout('private')
        self.complete_hook = fail
        with self.assertRaises(dv.Transient):await self.finish()
        self.complete_hook = None; self.backend = Pan123(self.store)
        with self.assertRaises(dv.Conflict):await self.finish()
        self.assertEqual(len(self.merges()),1)
        self.assertEqual(self.count('/download_info'),0)

    async def test_no_ticket_is_not_success_or_auth_failure(self):
        await self.ready(); self.complete_visible = False; self.complete_code = -1; self.complete_data = None
        with self.assertRaises(dv.Transient) as caught:await self.finish()
        self.assertNotIn('private',str(caught.exception))
        with self.assertRaises(dv.Conflict):await self.finish()
        self.assertEqual(len(self.merges()),1)

    async def test_missing_or_invalid_final_id_never_falls_back_to_initial_id(self):
        await self.ready(); saved = copy.deepcopy(self.store.rows)
        for code in (0,200):
            for value in ({'FileId':77},{'file_info':None},{'file_info':[]},{'file_info':'private'},
                          {'file_info':{}},*[{'file_info':{'FileId':v}} for v in (None,False,0,-1,1.5,'bad')]):
                with self.subTest(code=code,value=value):
                    self.store.rows = copy.deepcopy(saved); self.backend = Pan123(self.store); self.listed = False
                    self.complete_data = value; self.complete_code = code
                    before = len(self.merges())
                    with self.assertRaises(dv.Transient) as caught:await self.finish()
                    self.assertIs(type(dv.error_from(dv.error_dict(caught.exception))),dv.Transient)
                    self.assertNotIn('private',str(caught.exception))
                    self.assertEqual(self.stored('merge_requested'),self.state)
                    self.assertEqual(len(self.merges()),before+1)
                    self.assertTrue(self.requests[-1].url.path.endswith('/upload_complete/v2'))
        self.assertEqual(self.info_count,0)
        self.assertEqual(self.count('/download_info'),0)

    async def test_final_detail_binding_and_size_are_checked(self):
        await self.ready(); saved = copy.deepcopy(self.store.rows); file = dict(self.file)
        for field,value in (('FileId',77),('ParentFileId',2),('FileName','other'),('Type',1),('Trashed',True),('Size',0)):
            with self.subTest(field=field):
                self.store.rows = copy.deepcopy(saved); self.backend = Pan123(self.store); self.listed = False
                self.file = {**file,field:value}
                with self.assertRaises(dv.Conflict):await self.finish()
        self.assertEqual(self.count('/download_info'),0)

    async def test_detail_temporarily_missing_then_retry_requires_readback(self):
        await self.ready()
        async def reply(request):
            if request.url.path.endswith('/file/info'):
                return httpx.Response(200,json={'code':0,'data':{'infoList':[]}})
        self.reply = reply
        with self.assertRaises(dv.Transient):await self.finish()
        self.reply = None; self.backend = Pan123(self.store)
        self.assertEqual((await self.finish()).remote_id,'99')
        self.assertEqual(self.count('/download_info'),1)
        self.assertEqual(len(self.merges()),1)

    async def test_detail_status_unknown_or_restricted_never_succeeds(self):
        await self.ready(); saved = copy.deepcopy(self.store.rows); file = dict(self.file)
        for value,error in ((1,dv.Transient),(None,dv.ApiChanged)):
            with self.subTest(value=value):
                self.store.rows = copy.deepcopy(saved); self.backend = Pan123(self.store); self.listed = False
                self.file = {**file,'Status':value}
                with self.assertRaises(error):await self.finish()
        self.assertEqual(self.count('/download_info'),0)

    async def test_merge_budget_rejection_leaves_no_intent_and_retry_succeeds(self):
        await self.ready(); before = self.count('/file/list'); original = net.pacer.get()
        async def pace():
            await original()
            if self.count('/file/list') > before:raise dv.RateLimited('host budget')
        ctx = net.pacer.set(pace)
        try:
            with self.assertRaises(dv.RateLimited):await self.finish()
            self.assertIs(net.pacer.get(),pace)
        finally:net.pacer.reset(ctx)
        self.assertIsNone(self.stored('merge_requested'))
        self.assertEqual(self.merges(),[])
        self.assertEqual((await self.finish()).remote_id,'99')
        self.assertEqual(len(self.merges()),1)

    async def test_cancel_waiting_merge_budget_has_no_followup_io(self):
        await self.ready(); entered = asyncio.Event(); before = self.count('/file/list'); original = net.pacer.get()
        async def pace():
            await original()
            if self.count('/file/list') > before:
                entered.set(); await asyncio.Event().wait()
        ctx = net.pacer.set(pace)
        try:
            task = asyncio.create_task(self.finish()); await entered.wait()
            counts = len(self.requests),len(self.store.calls)
            self.close_error = RuntimeError('private close'); task.cancel('external')
            with self.assertRaises(asyncio.CancelledError) as caught:await task
            self.assertEqual(caught.exception.args,('external',))
            self.assertEqual((len(self.requests),len(self.store.calls)),counts)
        finally:net.pacer.reset(ctx)
        self.assertIsNone(self.stored('merge_requested'))
        self.close_error = None
        self.assertEqual((await self.finish()).remote_id,'99')

    async def test_cancel_merge_preserves_intent_and_next_call_reads_product(self):
        await self.ready(); entered = asyncio.Event()
        async def wait(request):
            entered.set(); await asyncio.Event().wait()
        self.complete_hook = wait
        task = asyncio.create_task(self.finish()); await entered.wait()
        counts = len(self.requests),len(self.store.calls)
        self.close_error = RuntimeError('private close'); task.cancel('external')
        with self.assertRaises(asyncio.CancelledError) as caught:await task
        self.assertEqual(caught.exception.args,('external',))
        self.assertEqual((len(self.requests),len(self.store.calls)),counts)
        self.assertIsNotNone(self.stored('merge_requested'))
        self.complete_hook = None; self.close_error = None; self.backend = Pan123(self.store)
        self.assertEqual((await self.finish()).remote_id,'99')
        self.assertEqual(len(self.merges()),1)
        self.assertEqual(self.count('/download_info'),1)

    async def test_merge_intent_write_unknown_stops_even_when_not_yet_visible(self):
        await self.ready()
        async def hook(method,scope,key,value):
            if method == 'set' and key == 'merge_requested':raise TimeoutError('private')
        self.store.hook = hook
        with self.assertRaises(dv.Transient):await self.finish()
        self.store.hook = None
        with self.assertRaises(dv.Conflict):await self.finish()
        self.assertEqual(self.merges(),[])
        self.assertIsNone(self.stored('merge_requested'))
        self.assertTrue(self.backend._uploads.data.pending)

    async def test_merge_intent_commit_response_lost_blocks_after_restart(self):
        await self.ready()
        async def hook(method,scope,key,value):
            if method == 'set' and key == 'merge_requested':
                self.store.rows[(scope,key)] = copy.deepcopy(value)
                raise TimeoutError('private')
        self.store.hook = hook
        with self.assertRaises(dv.Transient):await self.finish()
        self.store.hook = None; self.backend = Pan123(self.store)
        with self.assertRaises(dv.Conflict):await self.finish()
        self.assertEqual(self.merges(),[])

    async def test_cancel_merge_intent_write_never_sends_merge(self):
        await self.ready(); entered = asyncio.Event()
        async def hook(method,scope,key,value):
            if method == 'set' and key == 'merge_requested':
                entered.set(); await asyncio.Event().wait()
        self.store.hook = hook
        task = asyncio.create_task(self.finish()); await entered.wait()
        counts = len(self.requests),len(self.store.calls); task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        self.assertEqual((len(self.requests),len(self.store.calls)),counts)
        self.store.hook = None
        with self.assertRaises(dv.Conflict):await self.finish()
        self.assertEqual(self.merges(),[])

    async def test_pending_confirmation_is_resolved_only_when_merge_needed(self):
        await self.start()
        async def hook(method,scope,key,value):
            if method == 'set' and key == 'part:0':raise TimeoutError('private')
        self.store.hook = hook
        with self.assertRaises(dv.Transient):await self.put()
        self.assertIsNone(self.stored('part:0'))
        self.store.hook = None
        self.assertEqual((await self.finish()).remote_id,'99')
        self.assertIsNotNone(self.stored('part:0'))
        self.assertFalse(self.backend._uploads.data.pending)
        self.assertEqual(len(self.puts()),1)

    async def test_business_error_survives_close_failure(self):
        await self.ready(); self.complete_visible = False; self.complete_code = 401
        async def fail(request):self.close_error = RuntimeError('private close')
        self.complete_hook = fail
        with self.assertRaises(dv.AuthExpired):await self.finish()
        self.assertEqual(self.info_count,0)

    async def test_successful_merge_close_failure_recovers_by_readback(self):
        await self.ready()
        async def fail(request):self.close_error = RuntimeError('private close')
        self.complete_hook = fail
        with self.assertRaises(dv.Transient) as caught:await self.finish()
        self.assertNotIn('private',str(caught.exception))
        self.complete_hook = None; self.close_error = None
        self.assertEqual((await self.finish()).remote_id,'99')
        self.assertEqual(len(self.merges()),1)
        self.assertEqual(self.count('/download_info'),1)

    async def test_concurrent_finish_rejects_retired_reference_then_begin_reads_product(self):
        await self.ready()
        first,second = await asyncio.gather(self.finish(),self.finish(),return_exceptions=True)
        self.assertEqual(first.remote_id,'99')
        self.assertIsInstance(second,dv.Conflict)
        self.assertEqual((await self.begin()).file,first)
        self.assertEqual(len(self.merges()),1)
        self.assertEqual(self.count('/download_info'),1)

    async def test_missing_completion_exception_does_not_relax_response_envelope(self):
        await self.ready(); saved = copy.deepcopy(self.store.rows); self.complete_visible = False
        cases = [(200,b'not json',dv.ApiChanged),(200,b'[]',dv.ApiChanged),
                 (200,b'{"code":true,"data":{}}',dv.ApiChanged),
                 (200,b'{"code":0,"data":null}',dv.ApiChanged),
                 (200,b'{"code":200,"data":[]}',dv.ApiChanged),
                 (200,b'{"code":401,"data":{}}',dv.AuthExpired),
                 (200,b'{"code":429,"data":{}}',dv.RateLimited),
                 (401,b'private',dv.AuthExpired),(429,b'private',dv.RateLimited),
                 (503,b'private',dv.Transient)]
        for status,body,error in cases:
            with self.subTest(status=status,body=body):
                self.store.rows = copy.deepcopy(saved); self.backend = Pan123(self.store)
                async def reply(request):return httpx.Response(status,content=body)
                self.complete_hook = reply
                with self.assertRaises(error) as caught:await self.finish()
                self.assertIs(type(caught.exception),error)
                self.assertEqual(self.stored('merge_requested'),self.state)
                self.assertNotIn('private',str(caught.exception))
        self.assertEqual(self.info_count,0)
        self.assertFalse(any(k.startswith('retired:') for s,k in self.store.rows))

    async def test_unknown_merge_restarts_with_no_checkpoint_and_only_reads_directory(self):
        await self.ready(True); self.complete_visible = False; self.complete_data = {}
        with self.assertRaises(dv.Transient):await self.finish()
        saved = copy.deepcopy(self.store.rows); before = len(self.requests)
        for _ in range(2):
            self.backend = Pan123(self.store)
            for call in (self.begin,self.finish):
                with self.assertRaises(dv.Conflict) as caught:await call()
                self.assertIn('合并结果',str(caught.exception))
        paths = [r.url.path for r in self.requests[before:]]
        self.assertEqual(len(paths),8)
        self.assertTrue(all(p.endswith(('/user/info','/file/list')) for p in paths))
        self.assertEqual(len(self.merges()),1)
        self.assertEqual(len(self.puts()),2)
        self.assertEqual(self.count('/upload_request'),1)
        self.assertEqual({k:v for k,v in self.store.rows.items() if k[0] != 'global'},
                         {k:v for k,v in saved.items() if k[0] != 'global'})

    async def test_late_product_after_unknown_merge_uses_d_for_both_entries(self):
        await self.ready(); self.complete_visible = False; self.complete_data = {}
        with self.assertRaises(dv.Transient):await self.finish()
        saved = copy.deepcopy(self.store.rows); file = dict(self.file)
        for entry in ('begin','finish'):
            for failure in ('size','md5','binding','download',None):
                with self.subTest(entry=entry,failure=failure):
                    self.store.rows = copy.deepcopy(saved); self.backend = Pan123(self.store)
                    self.listed = True; self.file = dict(file); self.content_status = None; self.content_body = None
                    if failure == 'size':self.file['Size'] += 1
                    if failure == 'md5':self.content_body = b'z'*len(self.body)
                    if failure == 'binding':self.after_info = {**file,'ParentFileId':2}
                    else:self.after_info = None
                    if failure == 'download':self.content_status = 403
                    before = len(self.requests)
                    call = self.begin if entry == 'begin' else self.finish
                    if failure:
                        with self.assertRaises(dv.Transient if failure == 'download' else dv.Conflict):await call()
                        self.assertFalse(any(k.startswith('retired:') for s,k in self.store.rows))
                    else:
                        result = await call()
                        self.assertEqual((result.file if entry == 'begin' else result).remote_id,'99')
                        self.assertTrue(any(r.url.host == 'content.invalid' for r in self.requests[before:]))
                    self.assertFalse(any(r.method == 'PUT' or r.url.path.endswith(
                        ('/upload_request','/upload_complete/v2','/s3_list_upload_parts')) for r in self.requests[before:]))
        self.assertEqual(len(self.merges()),1)

    async def test_recovery_requires_all_directory_pages_even_after_finding_candidate(self):
        await self.ready(); self.store.rows[('upload_data:'+self.state['generation'],'merge_requested')] = self.state
        for entry in ('begin','finish'):
            for failure,error in (('network',dv.Transient),('rate',dv.RateLimited),('budget',dv.RateLimited),
                                  ('bad_page',dv.ApiChanged),('cancel',asyncio.CancelledError)):
                with self.subTest(entry=entry,failure=failure):
                    pages = 0; entered = asyncio.Event()
                    async def reply(request):
                        nonlocal pages
                        if not request.url.path.endswith('/file/list'):return
                        pages += 1
                        if pages == 1:return httpx.Response(200,json={'code':0,'data':{'InfoList':[self.file],'Next':1}})
                        if failure == 'network':raise httpx.ReadTimeout('private')
                        if failure == 'rate':return httpx.Response(429)
                        if failure == 'bad_page':return httpx.Response(200,json={'code':0,'data':{'InfoList':[],'Next':2}})
                        entered.set(); await asyncio.Event().wait()
                    self.reply = reply
                    call = self.begin if entry == 'begin' else self.finish
                    original = net.pacer.get()
                    async def pace():
                        if failure == 'budget' and pages == 1:raise dv.RateLimited('host budget')
                        await original()
                    ctx = net.pacer.set(pace)
                    try:
                        if failure == 'cancel':
                            task = asyncio.create_task(call()); await entered.wait()
                            counts = len(self.requests),len(self.store.calls); task.cancel('external')
                            with self.assertRaises(error) as caught:await task
                            self.assertEqual(caught.exception.args,('external',))
                            self.assertEqual((len(self.requests),len(self.store.calls)),counts)
                        else:
                            with self.assertRaises(error):await call()
                    finally:net.pacer.reset(ctx)
                    self.assertEqual(pages,1 if failure == 'budget' else 2)
        self.assertEqual(self.info_count,0)
        self.assertEqual(self.merges(),[])
        self.assertEqual(self.stored('merge_requested'),self.state)

    async def test_merge_marker_null_damage_or_read_failure_cannot_authorize_writes(self):
        await self.ready(); saved = copy.deepcopy(self.store.rows)
        scope = 'upload_data:'+self.state['generation']
        for value in (None,False,[],{'broken':True}):
            for entry in ('begin','part','finish'):
                with self.subTest(value=value,entry=entry):
                    self.store.rows = copy.deepcopy(saved); self.backend = Pan123(self.store)
                    self.store.rows[(scope,'merge_requested')] = value
                    call = {'begin':self.begin,'part':self.put,'finish':self.finish}[entry]
                    with self.assertRaises(dv.Conflict):await call()
        for failure,error in ((TimeoutError,dv.Transient),(ValueError,dv.Conflict)):
            for call in (self.begin,self.finish):
                async def hook(method,s,key,value):
                    if method == 'get' and key == 'merge_requested':raise failure('private')
                self.store.hook = hook
                with self.assertRaises(error):await call()
        self.assertEqual(self.merges(),[])
        self.assertEqual(len(self.puts()),1)
        self.assertEqual(self.count('/upload_request'),1)

    async def test_verified_retirement_failure_or_unknown_never_revokes_p_or_d(self):
        await self.ready(); saved = copy.deepcopy(self.store.rows)
        retired_key = ('upload:'+self.state['session_key'],'retired:'+self.state['generation'])
        for path in ('P','D'):
            for committed in (False,True):
                with self.subTest(path=path,committed=committed):
                    self.store.rows = copy.deepcopy(saved); self.backend = Pan123(self.store); self.listed = path == 'D'
                    async def hook(method,scope,key,value):
                        if method == 'set' and (scope,key) == retired_key:
                            if committed:self.store.rows[(scope,key)] = copy.deepcopy(value)
                            raise TimeoutError('private maintenance')
                    self.store.hook = hook
                    self.assertEqual((await self.finish()).remote_id,'99')
                    self.assertEqual(retired_key in self.store.rows,committed)
                    self.assertTrue(self.backend._uploads.data.pending)
                    self.assertFalse(any(m == 'delete' for m,s,k,v in self.store.calls))
                    if not committed:
                        self.store.hook = None
                        await self.backend._uploads.data.collect()
                        self.assertIsNotNone(self.stored('info'))

    async def test_verified_retirement_is_bounded_and_external_cancel_still_propagates(self):
        await self.ready(); saved = copy.deepcopy(self.store.rows)
        timeout = asyncio.timeout
        for path in ('P','D'):
            for cancel in (False,True):
                with self.subTest(path=path,cancel=cancel):
                    self.store.rows = copy.deepcopy(saved); self.backend = Pan123(self.store); self.listed = path == 'D'
                    entered = asyncio.Event()
                    async def hook(method,scope,key,value):
                        if method == 'set' and key.startswith('retired:'):
                            entered.set(); await asyncio.Event().wait()
                    self.store.hook = hook
                    if cancel:
                        task = asyncio.create_task(self.finish()); await entered.wait()
                        counts = len(self.requests),len(self.store.calls); task.cancel('external')
                        with self.assertRaises(asyncio.CancelledError) as caught:await task
                        self.assertEqual(caught.exception.args,('external',))
                        self.assertEqual((len(self.requests),len(self.store.calls)),counts)
                    else:
                        with patch('cloud_123.upload.asyncio.timeout',side_effect=lambda n:timeout(.01 if n == 1 else n)):
                            self.assertEqual((await self.finish()).remote_id,'99')
                        self.assertTrue(entered.is_set())
                    self.assertFalse(any(k.startswith('retired:') for s,k in self.store.rows))

    async def test_confirmed_retirement_reenters_partial_cleanup_after_restart(self):
        await self.ready(True)
        self.assertEqual((await self.finish()).remote_id,'99')
        info = copy.deepcopy(self.info()); state = self.state
        retired_key = ('upload:'+state['session_key'],'retired:'+state['generation'])
        original_retired = copy.deepcopy(self.store.rows[retired_key])
        other_key = ('upload_data:'+'f'*32,'info')
        self.store.rows[other_key] = {'other':'untouched'}
        collector = UploadStore(self.store); deleted = 0
        async def hook(method,scope,key,value):
            nonlocal deleted
            if method == 'delete':
                deleted += 1
                if deleted == 2:raise TimeoutError('partial cleanup')
        self.store.hook = hook
        with self.assertRaises(dv.Transient):await collector._collect_one(info,[16])
        self.assertEqual(self.store.rows[retired_key],original_retired)
        self.store.hook = None; collector = UploadStore(self.store)
        with patch('cloud_123.upload_store.time.time',return_value=time.time()+GRACE+1):
            for _ in range(3):await collector._collect_one(info,[16])
        self.assertNotIn(retired_key,self.store.rows)
        self.assertFalse(any(s == 'upload_data:'+state['generation'] for s,k in self.store.rows))
        self.assertEqual(self.store.rows[other_key],{'other':'untouched'})
        self.backend = Pan123(self.store)
        self.assertEqual((await self.begin()).file.remote_id,'99')
        self.assertEqual(len(self.merges()),1)

    async def test_begin_product_without_trusted_generation_skips_retirement(self):
        self.listed = True
        async def fail(*args):raise AssertionError('no store required for current fingerprint')
        self.store.hook = fail
        result = await self.begin({'broken':True})
        self.assertEqual(result.file.remote_id,'99')
        self.assertEqual(self.store.calls,[])
