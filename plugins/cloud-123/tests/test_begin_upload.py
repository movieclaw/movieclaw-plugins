"""真实 SDK/P123Client + 内存存储/模拟出口；不读凭据，不访问真实网盘。"""
from __future__ import annotations

import asyncio
import ast
import copy
import hashlib
import json
import socket
import time
import unittest
from dataclasses import replace
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

import httpx
from movieclaw_sdk import drives as dv, net
from movieclaw_sdk.services import StoredValue
from movieclaw_sdk.rpc import RemoteServiceError
from cloud_123.backend import Pan123
from cloud_123 import readback
PART_SIZE = 16 * 1024 * 1024  # 默认测试样本布局，服务端可返回其他片大小。
from cloud_123.upload_store import UploadStore, RETENTION, GRACE
from tests.test_account_info import token


class MemoryStore:
    def __init__(self):
        self.rows = {}
        self.calls = []
        self.hook = None

    async def call(self, method, scope, key=None, value=None):
        self.calls.append((method, scope, key, copy.deepcopy(value)))
        if self.hook:
            await self.hook(method, scope, key, value)

    async def get(self, key, *, scope='global', default=None):
        await self.call('get', scope, key)
        return copy.deepcopy(self.rows[(scope, key)]) if (scope, key) in self.rows else default

    async def items(self, *, scope='global'):
        await self.call('items', scope)
        return {k:copy.deepcopy(v) for (s,k),v in self.rows.items() if s == scope}

    async def set(self, key, value, *, scope='global', secret=False):
        assert secret
        await self.call('set', scope, key, value)
        self.rows[(scope,key)] = copy.deepcopy(value)

    async def delete(self, key, *, scope='global'):
        await self.call('delete', scope, key)
        return self.rows.pop((scope,key), None) is not None


class UploadFixture(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = MemoryStore()
        self.backend = Pan123(self.store)
        self.account = dv.DriveAccount('12345', {'token': token('upload')})
        self.body = b'correct source bytes'
        self.fp = dv.Fingerprint(len(self.body), 'A'*40, hashlib.md5(self.body).hexdigest().upper(), 'A'*40)
        self.file = {'FileId': 99, 'ParentFileId': 1, 'FileName': 'movie.mkv',
                     'Size':len(self.body), 'Type':0, 'Trashed':False, 'Status':0,
                     'PunishFlag':0, 'AbnormalAlert':0, 'Etag':'declared-not-trusted', 'S3KeyFlag':'12345-0'}
        self.listed = False
        self.init = {'FileId': 77, 'Reuse':False, 'Info':None, 'SliceSize':str(PART_SIZE),
                     'Bucket':'bucket', 'Key':'key', 'StorageNode':'node', 'UploadId':'upload-secret'}
        self.parts = []
        self.requests = []
        self.paced = 0
        self.close_error = None
        self.reply = None
        self.closed = []
        self.stream_closed = []
        self.sent_cookie = False
        self.after_info = None
        self.info_count = 0
        self.api_code = 0
        self.content_status = None
        self.content_headers = {}
        self.content_body = None
        self.consumer_gate = None
        self.body_entered = asyncio.Event()
        test = self

        class Transport(httpx.MockTransport):
            async def aclose(self):
                test.closed.append(True)
                if test.close_error:
                    raise test.close_error

        def transport(service):
            self.assertEqual(service, 'cloud-123')
            return net._Paced(Transport(self.handler))
        async def pace():
            self.paced += 1
        for name in ('connect','connect_ex'):
            guard = patch.object(socket.socket,name,side_effect=AssertionError('network forbidden'))
            guard.start(); self.addCleanup(guard.stop)
        guard = patch.object(net,'http_transport',side_effect=transport)
        guard.start(); self.addCleanup(guard.stop)
        ctx = net.pacer.set(pace)
        self.addCleanup(net.pacer.reset,ctx)

    def stream(self, data):
        test = self
        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                test.body_entered.set()
                if test.consumer_gate:
                    await test.consumer_gate.wait()
                for start in range(0,len(data),65536):
                    yield data[start:start+65536]
            async def aclose(self):
                test.stream_closed.append(True)
                if test.close_error:
                    raise test.close_error
        return Stream()

    async def handler(self, request):
        self.requests.append(request)
        if self.reply:
            result = await self.reply(request)
            if result is not None:
                return result
        path = request.url.path
        if request.url.host != '123pan.com':
            self.assertNotIn('authorization',request.headers)
            self.assertNotIn('cookie',request.headers)
            if path == '/redirect':
                return httpx.Response(302, headers={'Location':'https://content.invalid/file','Set-Cookie':'secret=bad'})
            range_header = request.headers.get('range')
            if range_header:
                start,end = map(int,range_header[6:].split('-'))
                body = self.body[start:end+1]
                headers = {'Content-Range':f'bytes {start}-{end}/{len(self.body)}'}
                status = 206
            else:
                body,headers,status = self.body,{},200
            headers.update(self.content_headers)
            return httpx.Response(self.content_status or status,headers=headers,
                                  stream=self.stream(self.content_body if self.content_body is not None else body))
        self.assertEqual(request.headers.get('authorization'), 'Bearer '+self.account.credentials['token'])
        if path.endswith('/user/info'):
            data = {'UID':12345,'Nickname':'test'}
        elif path.endswith('/file/list/new') or path.endswith('/file/list'):
            data = {'InfoList':[self.file] if self.listed else [],'Next':-1}
        elif path.endswith('/file/info'):
            self.info_count += 1
            data = {'infoList':[self.after_info if self.info_count > 1 and self.after_info else self.file]}
        elif path.endswith('/file/download_info'):
            data = {'DownloadUrl':'https://content.invalid/redirect'}
        elif path.endswith('/file/upload_request'):
            if self.api_code:
                self.listed = True
                return httpx.Response(200,json={'code':self.api_code,'data':{}})
            data = self.init
        elif path.endswith('/file/s3_list_upload_parts'):
            payload = json.loads(request.content)
            marker = payload['partNumberMarker']
            data = {'Parts':[p for p in self.parts if int(p['PartNumber']) > marker]}
        else:
            raise AssertionError('unexpected endpoint '+path)
        return httpx.Response(200,json={'code':0,'data':data})

    async def begin(self, state=None, **kwargs):
        return await self.backend.begin_upload(self.account,kwargs.get('dir_id','1'),
            kwargs.get('name','movie.mkv'),kwargs.get('fp',self.fp),state,kwargs.get('range_data'))

    def info(self):
        return next(v for (s,k),v in self.store.rows.items() if k == 'info')

    def record(self, index, digest='a'*32):
        info = self.info()
        record = {k:info[k] for k in ('schema','session_key','generation')}
        record.update(index=index,size=min(PART_SIZE,info['expected_size']-index*PART_SIZE),md5=digest,etag=digest)
        self.store.rows[('upload_data:'+info['generation'],f'part:{index}')] = record
        return record

    def count(self, suffix):
        return sum(r.url.path.endswith(suffix) for r in self.requests)


class UploadTest(UploadFixture):
    async def test_initialize_order_and_state(self):
        async def before_http(request):
            if request.url.path.endswith('/upload_request'):
                self.assertEqual(self.paced,3)
                self.assertTrue(any(k.startswith('intent:') for s,k in self.store.rows))
                self.assertTrue(any(s.startswith('upload_gc:') for s,k in self.store.rows))
        self.reply = before_http
        step = await self.begin()
        self.assertEqual((step.part_size,step.next_part),(PART_SIZE,0))
        self.assertEqual(set(step.state),{'schema','session_key','generation'})
        self.assertNotIn('upload-secret',json.dumps(step.state))
        self.assertEqual(self.info()['init_file_id'],'77')
        writes = [(s,k) for m,s,k,v in self.store.calls if m == 'set' and k != 'upload_gc_cursor']
        self.assertTrue(writes[0][0].startswith('upload_gc:'))
        self.assertTrue(writes[1][1].startswith('intent:'))
        self.assertEqual(writes[2][1],'info')
        payload = json.loads(next(r.content for r in self.requests if r.url.path.endswith('/upload_request')))
        self.assertEqual((payload['duplicate'],payload['type'],payload['etag']),(0,0,self.fp.md5.lower()))
        self.assertEqual(self.paced,len(self.requests))

    async def test_existing_file_ignores_etag_and_broken_store_state(self):
        self.listed = True
        self.backend = Pan123()
        result = await self.begin(state={'broken':True})
        self.assertEqual(result.file.remote_id,'99')
        self.assertIsNone(result.file.sha1)
        self.assertEqual(dv.remote_file_from(dv.remote_file_dict(result.file)),result.file)
        self.assertEqual(self.count('/upload_request'),0)
        self.assertEqual(self.info_count,2)
        self.assertEqual(self.paced,len(self.requests))
        self.assertEqual(self.store.calls,[])

    async def test_reuse_uses_final_info_id_not_top_id(self):
        self.init.update(Reuse=True,Info={'FileId':99})
        result = await self.begin()
        self.assertEqual(result.file.remote_id,'99')
        self.assertFalse(any(k == 'info' for s,k in self.store.rows))

    async def test_empty_s3_existing_and_reuse_require_full_readback(self):
        self.file['S3KeyFlag'] = ''
        for reuse in (False, True):
            with self.subTest(reuse=reuse):
                self.listed = not reuse
                self.init.update(Reuse=True,Info={'FileId':99})
                before = len(self.requests), self.info_count
                result = await self.begin()
                self.assertEqual(result.file.remote_id, '99')
                self.assertIsNone(result.file.sha1)
                requests = self.requests[before[0]:]
                download = next(r for r in requests if r.url.path.endswith('/download_info'))
                self.assertEqual(json.loads(download.content)['s3keyflag'], '')
                content = [r for r in requests if r.url.host == 'content.invalid']
                self.assertEqual([r.url.path for r in content], ['/redirect', '/file'])
                self.assertEqual(content[-1].headers['range'], f'bytes=0-{len(self.body)-1}')
                self.assertEqual(self.info_count-before[1], 2)
                self.assertEqual(sum(r.url.path.endswith('/upload_request') for r in requests), int(reuse))

    async def test_zero_reuse_reads_empty_body(self):
        self.body = b''
        self.fp = replace(self.fp,size=0,md5=hashlib.md5(b'').hexdigest())
        self.file['Size'] = 0
        self.init.update(Reuse=True,Info={'FileId':99})
        result = await self.begin()
        self.assertEqual(result.file.size,0)
        self.assertFalse(any('range' in r.headers for r in self.requests))
        self.assertTrue(self.stream_closed)

    async def test_5060_rechecks_name_and_reads_content(self):
        self.api_code = 5060
        self.assertEqual((await self.begin()).file.remote_id,'99')
        self.assertEqual(self.count('/upload_request'),1)

    async def test_invalid_inputs_fail_before_network(self):
        for kwargs in ({'name':'../x'}, {'name':'\ud800'}, {'range_data':b''},
                       {'fp':replace(self.fp,md5='bad')},{'fp':replace(self.fp,size=True)},
                       {'fp':replace(self.fp,size=-1)}):
            with self.subTest(kwargs=kwargs),self.assertRaises(dv.Conflict):
                await self.begin(**kwargs)
        self.assertEqual(self.requests,[])

    async def test_identity_mismatch_stops(self):
        self.account.user_id = '56789'
        with self.assertRaises(dv.Conflict):await self.begin()
        self.assertEqual(len(self.requests),1)

    async def test_bad_reuse_ids_no_fallback(self):
        self.init.update(Reuse=True,Info={})
        for value in (None,False,0,'bad'):
            with self.subTest(value=value):
                self.store.rows.clear(); self.init['Info']={'FileId':value}
                with self.assertRaises(dv.ApiChanged):await self.begin()
        self.assertEqual(self.count('/file/info'),0)

    async def test_optional_info_missing_key_is_not_null(self):
        self.init['Info'] = {}
        self.assertEqual((await self.begin()).next_part,0)
        self.assertNotIn('Info',self.info())
        self.store.rows.clear(); self.init['Info'] = {'FileId':None}
        with self.assertRaises(dv.ApiChanged):await self.begin()

    async def test_init_size_layout_and_zero_without_reuse_rejected(self):
        self.init['SliceSize'] = 0
        with self.assertRaises(dv.ApiChanged):await self.begin()
        self.assertFalse(any(k == 'info' for s,k in self.store.rows))
        self.store.rows.clear(); self.init['SliceSize'] = PART_SIZE
        self.fp = replace(self.fp,size=0,md5=hashlib.md5(b'').hexdigest())
        with self.assertRaises(dv.ApiChanged):await self.begin()

    async def test_name_directory_duplicate_and_pagination_failure(self):
        async def reply(request):
            if request.url.path.endswith('/file/list'):
                return httpx.Response(200,json={'code':0,'data':{'InfoList':rows,'Next':next_cursor}})
        self.reply = reply
        for rows,next_cursor,error in (([{**self.file,'Type':1}],-1,dv.Conflict),
              ([self.file,{**self.file,'FileId':100}],-1,dv.Conflict),([self.file],0,dv.ApiChanged)):
            with self.subTest(rows=rows),self.assertRaises(error):await self.begin()
        self.assertEqual(self.count('/download_info'),0)
        self.assertEqual(self.count('/upload_request'),0)

    async def test_size_conflict_does_not_download(self):
        self.listed = True; self.file['Size'] += 1
        with self.assertRaises(dv.Conflict):await self.begin()
        self.assertEqual(self.count('/download_info'),0)

    async def test_actual_md5_mismatch_not_masked_by_metadata(self):
        self.listed = True; self.file['Etag'] = self.fp.md5
        self.file['S3KeyFlag'] = ''
        self.content_body = b'x'*len(self.body)
        with self.assertRaises(dv.Conflict):await self.begin()
        self.assertEqual(self.count('/upload_request'),0)

    async def test_range_protocol_failures_do_not_initialize(self):
        self.listed = True
        for status,headers,body in ((200,{},None),(206,{'Content-Range':'bytes 1-2/3'},None),
              (206,{'Content-Encoding':'gzip'},None),(206,{},b'x'),(206,{},self.body+b'x')):
            with self.subTest(status=status,headers=headers,body=body):
                self.content_status,self.content_headers,self.content_body = status,headers,body
                with self.assertRaises(dv.ApiChanged):await self.begin()
        self.assertEqual(self.count('/upload_request'),0)

    async def test_content_429_and_expired_links(self):
        self.listed = True
        for status,error in ((429,dv.RateLimited),(403,dv.Transient),(404,dv.Transient)):
            self.content_status = status
            with self.assertRaises(error):await self.begin()
        self.assertEqual(self.count('/upload_request'),0)

    async def test_content_budget_rejected_before_send(self):
        self.listed = True
        async def pace():
            if any(r.url.path.endswith('/download_info') for r in self.requests):
                raise dv.RateLimited('host budget')
        ctx = net.pacer.set(pace)
        try:
            with self.assertRaises(dv.RateLimited):await self.begin()
        finally:net.pacer.reset(ctx)
        self.assertFalse(any(r.url.host != '123pan.com' for r in self.requests))

    async def test_read_timeout_and_successful_read_close_failure(self):
        self.listed = True
        async def reply(request):
            if request.url.host == 'content.invalid' and request.url.path == '/file':
                raise httpx.ReadTimeout('private token or url')
        self.reply = reply
        with self.assertRaises(dv.Transient) as caught:await self.begin()
        self.assertNotIn('private',str(caught.exception))
        async def close_failure(request):
            if request.url.host == 'content.invalid' and request.url.path == '/file':
                self.close_error = RuntimeError('private close')
        self.reply = close_failure
        with self.assertRaises(dv.Transient) as caught:await self.begin()
        self.assertNotIn('private',str(caught.exception))

    async def test_unsafe_redirect_and_new_cancel_from_close(self):
        self.listed = True
        async def unsafe(request):
            if request.url.host == 'content.invalid':
                return httpx.Response(302,headers={'Location':'http://unsafe.invalid/file'})
        self.reply = unsafe
        with self.assertRaises(dv.ApiChanged):await self.begin()
        async def cancelled_close(request):
            if request.url.host == 'content.invalid' and request.url.path == '/file':
                self.close_error = asyncio.CancelledError('new cancellation')
        self.reply = cancelled_close
        with self.assertRaises(asyncio.CancelledError):await self.begin()

    async def test_bound_info_changes_after_readback(self):
        self.file['S3KeyFlag'] = ''
        self.listed = True; self.after_info = {**self.file,'FileName':'changed.mkv'}
        with self.assertRaises(dv.Conflict):await self.begin()

    async def test_empty_s3_flag_change_after_readback_is_rejected(self):
        self.file['S3KeyFlag'] = ''; self.listed = True
        self.after_info = {**self.file, 'S3KeyFlag':'changed'}
        with self.assertRaises(dv.Conflict):await self.begin()
        self.assertEqual(self.info_count, 2)
        self.assertEqual(self.count('/upload_request'), 0)

    async def test_two_ranges_reuse_final_url_without_cookies(self):
        self.body = b'x'*(PART_SIZE+1)
        self.fp = replace(self.fp,size=len(self.body),md5=hashlib.md5(self.body).hexdigest())
        self.file['Size'] = len(self.body); self.listed = True
        self.assertIsNotNone((await self.begin()).file)
        content = [r for r in self.requests if r.url.host != '123pan.com']
        self.assertEqual([r.url.path for r in content],['/redirect','/file','/file'])
        self.assertEqual(content[-1].headers['range'],f'bytes={PART_SIZE}-{PART_SIZE}')

    async def test_cancel_during_content_close_error_preserves_cancel_no_requests(self):
        self.file['S3KeyFlag'] = ''
        self.listed = True; self.consumer_gate = asyncio.Event()
        task = asyncio.create_task(self.begin())
        await self.body_entered.wait()
        before = len(self.requests); self.close_error = RuntimeError('secret close')
        task.cancel('external')
        with self.assertRaises(asyncio.CancelledError):await task
        self.assertEqual(len(self.requests),before)
        self.assertTrue(self.stream_closed)

    async def test_read_error_and_close_error_preserve_business(self):
        self.listed = True; self.content_status = 200
        async def reply(request):
            if request.url.host == 'content.invalid' and request.url.path == '/file':
                self.close_error = RuntimeError('secret close')
        self.reply = reply
        with self.assertRaises(dv.ApiChanged):await self.begin()

    async def test_restart_without_checkpoint_and_single_confirm(self):
        first = await self.begin()
        self.record(0)
        self.backend = Pan123(self.store)
        again = await self.begin()
        self.assertEqual(again.state,first.state)
        self.assertEqual(again.next_part,1)
        self.assertEqual(self.count('/upload_request'),1)

    async def test_single_unknown_put_stops(self):
        await self.begin(); info = self.info()
        self.store.rows[('upload_data:'+info['generation'],'put_requested:0')] = {'sent':True}
        with self.assertRaises(dv.Conflict):await self.begin()

    async def test_multipart_prefix_missing_and_remote_only(self):
        self.fp = replace(self.fp,size=PART_SIZE+1)
        step = await self.begin()
        first = self.record(0); second = self.record(1,'b'*32)
        self.parts = [{'PartNumber':str(n+1),'Size':str(r['size']),'ETag':'"'+r['md5']+'"'}
                      for n,r in enumerate((first,second))]
        self.backend = Pan123(self.store)
        self.assertEqual((await self.begin()).next_part,2)
        del self.store.rows[('upload_data:'+step.state['generation'],'part:0')]
        self.assertEqual((await self.begin()).next_part,0)
        self.record(0)
        self.parts = self.parts[1:]
        self.assertEqual((await self.begin()).next_part,0)
        self.assertEqual(self.count('/upload_request'),1)

    async def test_multipart_conflict_rejected(self):
        self.fp = replace(self.fp,size=PART_SIZE+1)
        await self.begin(); record = self.record(0)
        self.parts = [{'PartNumber':'1','Size':str(record['size']),'ETag':'b'*32}]
        with self.assertRaises(dv.Conflict):await self.begin()

    async def test_multipart_extra_parts_and_stuck_marker_rejected(self):
        self.fp = replace(self.fp,size=PART_SIZE+1)
        await self.begin()
        self.parts = [{'PartNumber':'3','Size':'1','ETag':'a'*32}]
        with self.assertRaises(dv.Conflict):await self.begin()
        async def stuck(request):
            if request.url.path.endswith('/s3_list_upload_parts'):
                return httpx.Response(200,json={'code':0,'data':{'Parts':[
                    {'PartNumber':'1','Size':str(PART_SIZE),'ETag':'a'*32}]}})
        self.reply = stuck
        with self.assertRaises(dv.ApiChanged):await self.begin()

    async def test_multiple_active_generations_are_not_selected(self):
        await self.begin(); original = copy.deepcopy(self.info())
        duplicate = {**original,'generation':'f'*32}
        for (scope,key),value in list(self.store.rows.items()):
            if original['generation'] in scope or original['generation'] in key:
                self.store.rows[(scope.replace(original['generation'],'f'*32),key.replace(original['generation'],'f'*32))] = {
                    **value,'generation':'f'*32}
        with self.assertRaises(dv.Conflict):await self.begin()
        self.assertEqual(self.count('/upload_request'),1)

    async def test_merge_unknown_does_not_query_parts_or_initialize(self):
        await self.begin(); info = self.info()
        self.store.rows[('upload_data:'+info['generation'],'merge_requested')] = {'sent':True}
        with self.assertRaises(dv.Conflict):await self.begin()
        self.assertEqual(self.count('/s3_list_upload_parts'),0)
        self.assertEqual(self.count('/upload_request'),1)
        self.listed = True
        self.assertIsNotNone((await self.begin()).file)

    async def test_init_request_unknown_does_not_repeat(self):
        async def reply(request):
            if request.url.path.endswith('/upload_request'):
                raise httpx.ReadTimeout('secret')
        self.reply = reply
        with self.assertRaises(dv.Transient):await self.begin()
        self.reply = None; self.backend = Pan123(self.store)
        with self.assertRaises(dv.Conflict):await self.begin()
        self.assertEqual(self.count('/upload_request'),1)

    async def test_init_budget_rejection_leaves_no_intent_and_retry_succeeds(self):
        original = net.pacer.get()
        async def pace():
            await original()
            if self.count('/file/list'):
                self.close_error = RuntimeError('private close')
                raise dv.RateLimited('host budget')
        ctx = net.pacer.set(pace)
        try:
            with self.assertRaises(dv.RateLimited):await self.begin()
            self.assertIs(net.pacer.get(),pace)
        finally:net.pacer.reset(ctx)
        self.assertEqual(self.count('/upload_request'),0)
        self.assertFalse(any(k.startswith('intent:') for s,k in self.store.rows))
        self.close_error = None; self.backend = Pan123(self.store)
        self.assertEqual((await self.begin()).next_part,0)
        self.assertEqual(self.count('/upload_request'),1)

    async def test_cancel_during_init_budget_wait_leaves_no_intent(self):
        entered = asyncio.Event()
        original = net.pacer.get()
        async def pace():
            await original()
            if self.count('/file/list'):
                entered.set(); await asyncio.Event().wait()
        ctx = net.pacer.set(pace)
        try:
            task = asyncio.create_task(self.begin()); await entered.wait()
            before = len(self.requests),len(self.store.calls)
            self.close_error = RuntimeError('private close')
            task.cancel('external')
            with self.assertRaises(asyncio.CancelledError) as caught:await task
            self.assertEqual(caught.exception.args,('external',))
            self.assertEqual((len(self.requests),len(self.store.calls)),before)
        finally:net.pacer.reset(ctx)
        self.assertFalse(any(k.startswith('intent:') for s,k in self.store.rows))
        self.assertEqual(self.count('/upload_request'),0)
        self.close_error = None; self.backend = Pan123(self.store)
        self.assertEqual((await self.begin()).next_part,0)

    async def test_remote_rate_limit_is_not_unsent(self):
        async def reply(request):
            if request.url.path.endswith('/upload_request'):
                return httpx.Response(429)
        self.reply = reply
        with self.assertRaises(dv.RateLimited):await self.begin()
        self.reply = None; self.backend = Pan123(self.store)
        with self.assertRaises(dv.Conflict):await self.begin()
        self.assertEqual(self.count('/upload_request'),1)

    async def test_cancel_after_init_intent_keeps_unknown_result(self):
        entered = asyncio.Event()
        async def reply(request):
            if request.url.path.endswith('/upload_request'):
                entered.set(); await asyncio.Event().wait()
        self.reply = reply
        task = asyncio.create_task(self.begin()); await entered.wait()
        before = len(self.requests),len(self.store.calls)
        self.close_error = RuntimeError('private close'); task.cancel('external')
        with self.assertRaises(asyncio.CancelledError):await task
        self.assertEqual((len(self.requests),len(self.store.calls)),before)
        self.reply = None; self.close_error = None; self.backend = Pan123(self.store)
        with self.assertRaises(dv.Conflict):await self.begin()
        self.assertEqual(self.count('/upload_request'),1)

    async def test_info_commit_response_lost_recovered_after_restart(self):
        async def hook(method,scope,key,value):
            if method == 'set' and key == 'info':
                self.store.rows[(scope,key)] = copy.deepcopy(value)
                raise TimeoutError('private')
        self.store.hook = hook
        with self.assertRaises(dv.Transient):await self.begin()
        self.store.hook = None; self.backend = Pan123(self.store)
        self.assertEqual((await self.begin()).next_part,0)
        self.assertEqual(self.count('/upload_request'),1)

    async def test_late_info_commit_after_restart_never_reinitializes(self):
        late = []
        async def hook(method,scope,key,value):
            if method == 'set' and key == 'info':
                late.append((scope,key,copy.deepcopy(value))); raise TimeoutError
        self.store.hook = hook
        with self.assertRaises(dv.Transient):await self.begin()
        self.store.hook = None; self.backend = Pan123(self.store)
        with self.assertRaises(dv.Conflict):await self.begin()
        scope,key,value = late[0]; self.store.rows[(scope,key)] = value
        self.assertEqual((await self.begin()).next_part,0)
        self.assertEqual(self.count('/upload_request'),1)

    async def test_info_write_missing_retry_same_immutable_value(self):
        attempts = []
        async def hook(method,scope,key,value):
            if method == 'set' and key == 'info':
                attempts.append(copy.deepcopy(value))
                if len(attempts) == 1:raise TimeoutError
        self.store.hook = hook
        with self.assertRaises(dv.Transient):await self.begin()
        self.assertEqual((await self.begin()).next_part,0)
        self.assertEqual(attempts[0],attempts[1])
        self.assertEqual(self.count('/upload_request'),1)

    async def test_index_failure_stops_before_initialization(self):
        async def hook(method,scope,key,value):
            if method == 'set' and scope.startswith('upload_gc:'):raise TimeoutError
        self.store.hook = hook
        with self.assertRaises(dv.Transient):await self.begin()
        self.assertEqual(self.count('/upload_request'),0)
        self.assertFalse(any(k.startswith('intent:') for s,k in self.store.rows))

    async def test_intent_write_unknown_still_blocks_initialization(self):
        for failure in (TimeoutError,asyncio.CancelledError):
            with self.subTest(failure=failure.__name__):
                self.store = MemoryStore(); self.backend = Pan123(self.store)
                async def hook(method,scope,key,value):
                    if method == 'set' and key.startswith('intent:'):
                        self.store.rows[(scope,key)] = copy.deepcopy(value)
                        self.close_error = RuntimeError('private close')
                        raise failure('external')
                self.store.hook = hook
                with self.assertRaises(asyncio.CancelledError if failure is asyncio.CancelledError else dv.Transient):
                    await self.begin()
                self.assertEqual(self.count('/upload_request'),0)
                self.assertTrue(any(k.startswith('intent:') for s,k in self.store.rows))
                self.store.hook = None; self.close_error = None; self.backend = Pan123(self.store)
                with self.assertRaises(dv.Conflict):await self.begin()
                self.assertEqual(self.count('/upload_request'),0)

    async def test_read_failure_never_means_missing(self):
        for failure,expected in ((ValueError('private'),dv.Conflict),(LookupError('private'),dv.Conflict),
                                 (PermissionError('private'),dv.Conflict),(TimeoutError('private'),dv.Transient),
                                 (RemoteServiceError('private'),dv.Transient)):
            async def hook(method,scope,key,value):raise failure
            self.store.hook = hook
            with self.subTest(error=type(failure).__name__),self.assertRaises(expected) as caught:
                await self.begin()
            self.assertNotIn('private',str(caught.exception))
        self.assertEqual(self.count('/upload_request'),0)

    async def test_cancelled_info_write_stops_and_recovers(self):
        entered = asyncio.Event()
        async def hook(method,scope,key,value):
            if method == 'set' and key == 'info':
                entered.set(); await asyncio.Event().wait()
        self.store.hook = hook
        task = asyncio.create_task(self.begin()); await entered.wait()
        requests,calls = len(self.requests),len(self.store.calls)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        self.assertEqual((len(self.requests),len(self.store.calls)),(requests,calls))
        self.store.hook = None
        self.assertEqual((await self.begin()).next_part,0)
        self.assertEqual(self.count('/upload_request'),1)

    async def test_concurrent_begin_serializes_initialization(self):
        a,b = await asyncio.gather(self.begin(),self.begin())
        self.assertEqual(a.state,b.state)
        self.assertEqual(self.count('/upload_request'),1)

    async def test_bad_info_and_cross_binding_state_are_local_conflict(self):
        step = await self.begin()
        with self.assertRaises(dv.Conflict):await self.begin({**step.state,'session_key':'f'*64})
        del self.info()['expected_md5']
        with self.assertRaises(dv.Conflict):await self.begin()
        self.assertEqual(self.count('/upload_request'),1)

    async def test_retired_partial_generation_allows_new_one(self):
        first = await self.begin(); info = self.info()
        self.store.rows[('upload:'+info['session_key'],'retired:'+info['generation'])] = {**first.state,'retired_at':time.time()}
        del self.store.rows[('upload_data:'+info['generation'],'info')]
        second = await self.begin(first.state)
        self.assertNotEqual(first.state,second.state)
        self.assertEqual((await self.begin(first.state)).state,second.state)

    async def test_fully_cleaned_old_generation_does_not_block_new_session(self):
        first = await self.begin(); old = self.info()
        scope = 'upload:'+old['session_key']
        self.store.rows[(scope,'retired:'+old['generation'])] = {**first.state,'retired_at':time.time()-GRACE-1}
        second = await self.begin(first.state)
        bucket = int(old['session_key'][:2],16)
        self.store.rows[('global','upload_gc_cursor')] = {'bucket':bucket,'after':''}
        await self.backend._uploads.data.collect()
        self.assertFalse(any(old['generation'] in s or old['generation'] in k for s,k in self.store.rows))
        self.backend = Pan123(self.store)
        self.assertEqual((await self.begin(first.state)).state,second.state)
        self.assertEqual(self.count('/upload_request'),2)

    async def test_retired_bad_index_does_not_block_active_recovery(self):
        first = await self.begin(); old = self.info()
        self.store.rows[('upload:'+old['session_key'],'retired:'+old['generation'])] = {
            **first.state,'retired_at':time.time()}
        second = await self.begin(first.state)
        key = ('upload_gc:'+old['session_key'][:2],old['session_key']+':'+old['generation'])
        self.store.rows[key] = {'broken':True}
        self.backend = Pan123(self.store)
        self.assertEqual((await self.begin(second.state)).state,second.state)
        self.assertEqual(self.count('/upload_request'),2)

    async def test_active_bad_index_and_invalid_retirement_still_rejected(self):
        step = await self.begin(); info = self.info()
        index_key = ('upload_gc:'+info['session_key'][:2],info['session_key']+':'+info['generation'])
        self.store.rows[index_key] = {'broken':True}
        with self.assertRaises(dv.Conflict):await self.begin(step.state)
        self.store.rows[('upload:'+info['session_key'],'retired:'+info['generation'])] = {
            **step.state,'session_key':'f'*64}
        with self.assertRaises(dv.Conflict):await self.begin(step.state)
        self.assertEqual(self.count('/upload_request'),1)

    async def test_index_only_cleanup_remainder_is_never_reinitialized(self):
        first = await self.begin(); old = self.info()
        scope = 'upload:'+old['session_key']
        self.store.rows[(scope,'retired:'+old['generation'])] = {**first.state,'retired_at':time.time()-GRACE-1}
        async def hook(method,s,key,value):
            if method == 'delete' and s.startswith('upload_gc:'):raise TimeoutError
        self.store.hook = hook
        self.store.rows[('global','upload_gc_cursor')] = {'bucket':int(old['session_key'][:2],16),'after':''}
        await self.backend._uploads.data.collect()
        self.store.hook = None
        self.assertNotIn((scope,'intent:'+old['generation']),self.store.rows)
        self.assertNotIn((scope,'retired:'+old['generation']),self.store.rows)
        self.backend = Pan123(self.store)
        second = await self.begin()
        self.assertNotEqual(first.state,second.state)
        self.assertNotIn((scope,'intent:'+old['generation']),self.store.rows)
        self.assertEqual((await self.begin(first.state)).state,second.state)

    async def test_index_registration_lost_reply_uses_fresh_generation(self):
        indexes = []
        async def hook(method,scope,key,value):
            if method == 'set' and scope.startswith('upload_gc:'):
                indexes.append(copy.deepcopy(value))
                self.store.rows[(scope,key)] = copy.deepcopy(value)
                raise TimeoutError
        self.store.hook = hook
        with self.assertRaises(dv.Transient):await self.begin()
        self.assertEqual(self.count('/upload_request'),0)
        self.store.hook = None; self.backend = Pan123(self.store)
        step = await self.begin()
        self.assertNotEqual(step.state['generation'],indexes[0]['generation'])
        self.assertEqual(self.count('/upload_request'),1)

    async def test_missing_intent_with_remaining_data_is_not_index_only(self):
        step = await self.begin(); info = self.info()
        del self.store.rows[('upload:'+info['session_key'],'intent:'+info['generation'])]
        with self.assertRaises(dv.Conflict):await self.begin(step.state)
        del self.store.rows[('upload_data:'+info['generation'],'info')]
        self.store.rows[('upload_data:'+info['generation'],'put_requested:0')] = {'sent':True}
        with self.assertRaises(dv.Conflict):await self.begin()
        self.assertEqual(self.count('/upload_request'),1)

    async def test_scope_uses_existing_remote_data_facade(self):
        # 原样执行门面类；不启动依赖 httptools 的宿主 IPC 运行器。
        source = ast.parse(Path(dv.__file__).with_name('runner.py').read_text())
        node = next(n for n in source.body if isinstance(n,ast.ClassDef) and n.name == '_RemoteStore')
        namespace = {}
        exec(compile(ast.Module(body=[node],type_ignores=[]),'runner.py','exec'),namespace)
        memory = self.store
        class Api:
            async def get(self,key,scope):
                value = await memory.get(key,scope=scope)
                return StoredValue(found=(scope,key) in memory.rows,value=value)
            async def set(self,**kwargs):await memory.set(**kwargs)
            async def items(self,**kwargs):return await memory.items(**kwargs)
            async def delete(self,**kwargs):return await memory.delete(**kwargs)
        self.backend = Pan123(namespace['_RemoteStore'](SimpleNamespace(service=lambda name:Api()),'cloud-123'))
        self.assertEqual((await self.begin()).next_part,0)
        data = self.backend._uploads.data
        scope,key = 'upload_data:a','merge_requested'
        self.assertFalse(await data.has(scope,key))
        for value in (None,False,{'broken':True}):
            memory.rows[(scope,key)] = value
            self.assertTrue(await data.has(scope,key))

    async def test_missing_store_blocks_new_sessions_but_not_verified_products(self):
        self.backend = Pan123()
        with self.assertRaises(dv.Conflict):await self.begin()
        self.listed = True
        self.assertEqual((await self.begin()).file.remote_id, '99')
        self.assertEqual(self.count('/upload_request'), 0)

    async def test_unknown_merges_in_full_bucket_do_not_block_new_session_or_get_evicted(self):
        first = await self.begin()
        info = self.info(); scope = 'upload_data:'+info['generation']
        self.store.rows[(scope,'merge_requested')] = first.state
        prefix = info['session_key'][:2]
        for number in range(1, 256):
            ref = {'schema':5, 'session_key':prefix+f'{number:062x}', 'generation':f'{number:032x}',
                   'created_at':time.time()-RETENTION-100}
            self.store.rows[('upload_gc:'+prefix, ref['session_key']+':'+ref['generation'])] = ref
            self.store.rows[('upload:'+ref['session_key'], 'intent:'+ref['generation'])] = dict(ref)
            self.store.rows[('upload_data:'+ref['generation'], 'merge_requested')] = dict(ref)
        original = copy.deepcopy(self.store.rows)
        from cloud_123.upload import binding_for, session_key_for
        for number in range(10000):
            name = f'other-{number}.mkv'
            binding = binding_for(self.account.user_id,'1',name,self.fp,None)
            if session_key_for(binding)[:2] == prefix:break
        else:self.fail('no same-bucket synthetic name')
        self.store.rows[('global','upload_gc_cursor')] = {'bucket':int(prefix,16),'after':''}
        with patch('cloud_123.upload_store.time.time',return_value=time.time()+RETENTION+1):
            new = await self.begin(name=name)
        self.assertNotEqual(new.state['generation'], first.state['generation'])
        self.assertEqual(self.count('/upload_request'), 2)
        self.assertEqual(sum(s == 'upload_gc:'+prefix for s,k in self.store.rows), 257)
        for key,value in original.items():
            if key[0] != 'global':self.assertEqual(self.store.rows[key],value)
        self.assertFalse(any(k.startswith('retired:') for s,k in self.store.rows))
        self.listed = True
        self.assertEqual((await self.begin()).file.remote_id,'99')

    async def test_many_retired_generations_do_not_block_active_session_recovery(self):
        step = await self.begin()
        scope = 'upload:'+step.state['session_key']
        bucket = 'upload_gc:'+step.state['session_key'][:2]
        for number in range(257):
            ref = {**step.state, 'generation':f'{number:032x}', 'created_at':time.time()}
            self.store.rows[(scope,'intent:'+ref['generation'])] = dict(ref)
            self.store.rows[(scope,'retired:'+ref['generation'])] = {**ref,'retired_at':time.time()}
            self.store.rows[(bucket,ref['session_key']+':'+ref['generation'])] = ref
        self.backend = Pan123(self.store)
        self.assertEqual((await self.begin(step.state)).state, step.state)
        self.assertEqual(self.count('/upload_request'), 1)

    async def test_large_target_directory_is_scanned_before_initialize_and_product_recovery(self):
        listed = False
        async def reply(request):
            if request.url.path.endswith(('/file/list/new','/file/list')):
                cursor = int(request.url.params['next'])
                entries = [{'FileId':1000+i, 'FileName':f'other-{i}.mkv', 'Type':0, 'Size':1}
                           for i in range(cursor, cursor+100)]
                if cursor == 10000 and listed:entries.append(self.file)
                return httpx.Response(200,json={'code':0,'data':{
                    'InfoList':entries, 'Next':cursor+100 if cursor < 10000 else -1}})
        self.reply = reply
        step = await self.begin()
        self.assertEqual(step.next_part, 0)
        listed = True
        self.assertEqual((await self.begin(step.state)).file.remote_id, '99')
        self.assertEqual((await self.backend.finish_upload(self.account, step.state)).remote_id, '99')
        self.assertEqual(self.count('/upload_request'), 1)
        self.assertEqual(self.count('/file/list/new') + self.count('/file/list'), 303)
        self.assertEqual(self.count('/file/download_info'), 2)

    async def test_restricted_sibling_does_not_block_initialization_or_verified_product(self):
        async def reply(request):
            if request.url.path.endswith(('/file/list/new','/file/list')):
                items = [{'FileId':100, 'FileName':'other.mkv', 'Type':0, 'Size':1,
                          'Status':1, 'PunishFlag':2}]
                if self.listed:items.append(self.file)
                return httpx.Response(200,json={'code':0,'data':{'InfoList':items,'Next':-1}})
        self.reply = reply
        step = await self.begin()
        self.listed = True
        self.assertEqual((await self.begin(step.state)).file.remote_id, '99')
        self.assertEqual((await self.backend.finish_upload(self.account, step.state)).remote_id, '99')
        self.assertEqual(self.count('/upload_request'), 1)
        self.assertEqual(self.count('/file/download_info'), 2)

    async def test_restricted_target_still_fails_detail_verification(self):
        step = await self.begin()
        self.listed = True
        for field in ('Status', 'PunishFlag'):
            self.file[field] = 1
            with self.subTest(field=field):
                with self.assertRaises(dv.Transient):await self.begin(step.state)
                with self.assertRaises(dv.Transient):await self.backend.finish_upload(self.account, step.state)
            self.file[field] = 0
        self.assertEqual(self.count('/upload_request'), 1)
        self.assertEqual(self.count('/file/download_info'), 0)
        self.assertIn(('upload_data:'+step.state['generation'],'info'), self.store.rows)

    async def test_info_and_part_records_use_sdk_single_value_budget(self):
        for field in ('Bucket','Key','StorageNode','UploadId'):
            self.init[field] = '界'*2048
        step = await self.begin()
        self.assertGreater(len(json.dumps(self.info(),ensure_ascii=False).encode()), 12*1024)
        digest = hashlib.md5(self.body).hexdigest()
        record = self.record(0,digest)
        record['extra'] = 'x'*2048
        scope = 'upload_data:'+step.state['generation']
        del self.store.rows[(scope,'part:0')]
        await self.backend._uploads.data.save(scope,'part:0',record)
        self.backend = Pan123(self.store)
        resumed = await self.begin(step.state)
        self.assertEqual((resumed.state,resumed.next_part),(step.state,1))
        self.assertEqual(self.count('/upload_request'), 1)


class CleanupTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.memory = MemoryStore(); self.store = UploadStore(self.memory)
        self.now = time.time()

    def candidate(self, n, *, old=True):
        ref = {'schema':5,'session_key':'00'+f'{n:062x}','generation':f'{n:032x}',
               'created_at':self.now-RETENTION-100 if old else self.now}
        key = ref['session_key']+':'+ref['generation']
        self.memory.rows[('upload_gc:00',key)] = ref
        self.memory.rows[('upload:'+ref['session_key'],'intent:'+ref['generation'])] = dict(ref)
        self.memory.rows[('upload_data:'+ref['generation'],'info')] = dict(ref)
        return ref

    async def test_index_record_over_old_budget_uses_existing_store(self):
        ref = {'schema':5, 'session_key':'0'*64, 'generation':'1'*32,
               'created_at':self.now, 'extra':'x'*1024}
        await self.store.register(ref)
        self.assertEqual(self.memory.rows[('upload_gc:00',ref['session_key']+':'+ref['generation'])], ref)

    async def test_sdk_single_value_boundary_rejects_before_write_or_pending_intent(self):
        for method in ('save', 'call'):
            for size in (60*1024+1, 64*1024, 64*1024+1):
                value = {'padding':'x'*(size-len(json.dumps({'padding':''}).encode()))}
                key = f'{method}-{size}'
                before = len(self.memory.calls)
                if method == 'save':operation = self.store.save('global',key,value)
                else:operation = self.store.call('set',key=key,value=value)
                with self.subTest(method=method,size=size):
                    if size > 64*1024:
                        with self.assertRaises(dv.Conflict):await operation
                        self.assertEqual(len(self.memory.calls),before)
                        self.assertNotIn(('global',key),self.memory.rows)
                        self.assertFalse(self.store.pending)
                    else:
                        await operation
                        self.assertEqual(self.memory.rows[('global',key)],value)

    async def test_retire_then_reenter_partial_cleanup_after_grace(self):
        ref = self.candidate(1)
        scope = 'upload:'+ref['session_key']; data_scope = 'upload_data:'+ref['generation']
        await self.store.collect()
        self.assertIn((scope,'retired:'+ref['generation']),self.memory.rows)
        self.assertNotIn((data_scope,'info'),self.memory.rows)
        self.memory.rows[(scope,'retired:'+ref['generation'])]['retired_at'] -= GRACE+1
        self.memory.rows[('global','upload_gc_cursor')] = {'bucket':0,'after':''}
        await self.store.collect()
        self.assertFalse(any(s.startswith(('upload:','upload_data:','upload_gc:')) for s,k in self.memory.rows))

    async def test_active_front_does_not_starve_expired_tail(self):
        for n in range(1,5):self.candidate(n,old=False)
        tail = self.candidate(5)
        await self.store.collect(); await self.store.collect()
        self.assertIn(('upload:'+tail['session_key'],'retired:'+tail['generation']),self.memory.rows)

    async def test_unknown_or_damaged_merge_markers_survive_ttl_and_do_not_starve_tail(self):
        protected = []
        for n,value in enumerate((None,False,{'broken':True},[]),1):
            ref = self.candidate(n)
            self.memory.rows[('upload_data:'+ref['generation'],'merge_requested')] = value
            self.memory.rows[('upload_data:'+ref['generation'],'part:0')] = {'confirmed':True}
            protected.append(ref)
        saved = copy.deepcopy(self.memory.rows); tail = self.candidate(5)
        await self.store.collect()
        self.assertEqual(self.memory.rows[('global','upload_gc_cursor')]['after'],
                         protected[-1]['session_key']+':'+protected[-1]['generation'])
        await self.store.collect()
        for key,value in saved.items():self.assertEqual(self.memory.rows[key],value)
        self.assertIn(('upload:'+tail['session_key'],'retired:'+tail['generation']),self.memory.rows)
        self.assertFalse(any(k.startswith('retired:') and s == 'upload:'+ref['session_key']
                             for ref in protected for s,k in self.memory.rows))

    async def test_merge_marker_read_error_preserves_generation_and_moves_on(self):
        for error in (ValueError,TimeoutError):
            with self.subTest(error=error):
                self.memory = MemoryStore(); self.store = UploadStore(self.memory)
                first = self.candidate(1); saved = copy.deepcopy(self.memory.rows); tail = self.candidate(2)
                async def hook(method,scope,key,value):
                    if method == 'get' and scope == 'upload_data:'+first['generation'] and key == 'merge_requested':
                        raise error('private')
                self.memory.hook = hook
                await self.store.collect()
                for key,value in saved.items():self.assertEqual(self.memory.rows[key],value)
                self.assertIn(('upload:'+tail['session_key'],'retired:'+tail['generation']),self.memory.rows)

    async def test_late_merge_marker_keeps_generation_before_and_after_confirmation(self):
        ref = self.candidate(1); saved = copy.deepcopy(self.memory.rows)
        key = ('upload_data:'+ref['generation'],'merge_requested')
        self.store.pending[key] = {k:ref[k] for k in ('schema','session_key','generation')}
        await self.store.collect()
        self.assertIn(key,self.store.pending)
        self.memory.rows[key] = dict(self.store.pending[key])
        self.memory.rows[('global','upload_gc_cursor')] = {'bucket':0,'after':''}
        await self.store.collect()
        self.assertNotIn(key,self.store.pending)
        for key,value in saved.items():self.assertEqual(self.memory.rows[key],value)
        self.assertFalse(any(k.startswith('retired:') for s,k in self.memory.rows))

    async def test_pending_write_blocks_retirement_and_errors_are_optional(self):
        ref = self.candidate(1)
        self.store.pending[('upload_data:'+ref['generation'],'info')] = {**ref,'unconfirmed':True}
        await self.store.collect()
        self.assertNotIn(('upload:'+ref['session_key'],'retired:'+ref['generation']),self.memory.rows)
        async def hook(*args):raise ValueError('bad cipher')
        self.memory.hook = hook
        await self.store.collect()

    async def test_store_value_limit_before_rpc(self):
        with self.assertRaises(dv.Conflict):await self.store.save('upload_data:a','info',{'x':'x'*65536})
        self.assertEqual(self.memory.calls,[])

    async def test_cleanup_tail_unknown_or_cancelled_reenters_after_restart(self):
        for target in ('retired','index'):
            for applied in (False,True):
                for failure in (TimeoutError,asyncio.CancelledError):
                    with self.subTest(target=target,applied=applied,failure=failure.__name__):
                        self.memory = MemoryStore(); self.store = UploadStore(self.memory)
                        ref = self.candidate(1)
                        scope = 'upload:'+ref['session_key']
                        retired_key = (scope,'retired:'+ref['generation'])
                        index_key = ('upload_gc:00',ref['session_key']+':'+ref['generation'])
                        self.memory.rows[retired_key] = {**ref,'retired_at':self.now-GRACE-1}
                        failed_key = retired_key if target == 'retired' else index_key
                        async def hook(method,s,key,value):
                            if method == 'delete' and (s,key) == failed_key:
                                if applied:self.memory.rows.pop((s,key),None)
                                raise failure('external')
                        self.memory.hook = hook
                        if failure is asyncio.CancelledError:
                            with self.assertRaises(asyncio.CancelledError):await self.store.collect()
                            self.assertEqual(self.memory.calls[-1][:3],('delete',*failed_key))
                        else:
                            await self.store.collect()
                        if target == 'retired' or not applied:
                            self.assertIn(index_key,self.memory.rows)
                        self.assertNotIn((scope,'intent:'+ref['generation']),self.memory.rows)
                        self.memory.hook = None; self.store = UploadStore(self.memory)
                        self.memory.rows[('global','upload_gc_cursor')] = {'bucket':0,'after':''}
                        await self.store.collect()
                        self.assertFalse(any(s.startswith(('upload:','upload_data:','upload_gc:'))
                                             for s,k in self.memory.rows))

    async def test_damaged_candidate_and_bad_bucket_advance(self):
        bad = self.candidate(1)
        self.memory.rows[('upload_data:'+bad['generation'],'info')] = {'broken':True}
        good = self.candidate(2)
        await self.store.collect()
        self.assertNotIn(('upload:'+bad['session_key'],'retired:'+bad['generation']),self.memory.rows)
        self.assertIn(('upload:'+good['session_key'],'retired:'+good['generation']),self.memory.rows)
        self.memory.rows[('global','upload_gc_cursor')] = {'bucket':0,'after':''}
        async def hook(method,scope,key,value):
            if method == 'items' and scope == 'upload_gc:00':raise ValueError('bad cipher')
        self.memory.hook = hook
        await self.store.collect()
        self.assertEqual(self.memory.rows[('global','upload_gc_cursor')]['bucket'],1)

    async def test_retirement_commit_response_lost_is_reread(self):
        ref = self.candidate(1)
        async def hook(method,scope,key,value):
            if method == 'set' and key.startswith('retired:'):
                self.memory.rows[(scope,key)] = copy.deepcopy(value)
                raise TimeoutError
        self.memory.hook = hook
        await self.store.collect()
        self.assertTrue(self.store.pending)
        self.memory.hook = None
        self.memory.rows[('global','upload_gc_cursor')] = {'bucket':0,'after':''}
        await self.store.collect()
        self.assertEqual(self.store.pending,{})
        self.assertNotIn(('upload_data:'+ref['generation'],'info'),self.memory.rows)

    async def test_cleanup_timeout_advances_before_slow_candidate(self):
        ref = self.candidate(1)
        async def hook(method,scope,key,value):
            if method == 'get' and key.startswith('retired:'):
                await asyncio.sleep(10)
        self.memory.hook = hook
        await self.store.collect()
        cursor = self.memory.rows[('global','upload_gc_cursor')]
        self.assertEqual(cursor,{'bucket':0,'after':ref['session_key']+':'+ref['generation']})
