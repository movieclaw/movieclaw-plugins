"""通用分片与流式回读的离线回归；真实 SDK/P123Client，禁止 socket。"""
import asyncio
import copy
import hashlib
import json
import time
from dataclasses import replace
from unittest.mock import patch

import httpx
from movieclaw_sdk import drives as dv, net
from cloud_123.backend import Pan123
from cloud_123.upload import MAX_PART_BYTES, layout
from cloud_123.upload_store import UploadStore, GRACE, RETENTION
from tests.test_begin_upload import UploadFixture, PART_SIZE
from tests.test_upload_part import UploadPartFixture


class LargeUploadTests(UploadPartFixture):
    async def configure(self, size, part_size):
        self.init['SliceSize'] = part_size
        self.file['Size'] = size
        self.fp = replace(self.fp, size=size, md5='a'*32)
        self.state = (await self.begin()).state

    async def handler(self, request):
        if request.url.path.endswith('/upload_complete/v2'):
            self.requests.append(request)
            self.assertEqual(self.stored('merge_requested'), self.state)
            if self.reply:
                result = await self.reply(request)
                if result is not None:
                    return result
            return httpx.Response(200, json={'code':0, 'data':{'file_info':{'FileId':99}}})
        return await super().handler(request)

    async def finish(self):
        return await self.backend.finish_upload(self.account, self.state)

    async def test_41_mib_three_parts_and_p_path(self):
        size = 41 * 1024**2
        await self.configure(size, PART_SIZE)
        for index, length in enumerate((PART_SIZE, PART_SIZE, 9*1024**2)):
            await self.put(index, bytes([index])*length)
        self.assertEqual((await self.finish()).size, size)
        self.assertEqual([len(r.content) for r in self.puts()], [PART_SIZE, PART_SIZE, 9*1024**2])
        self.assertEqual(self.count('/upload_complete/v2'), 1)
        self.assertEqual(self.count('/download_info'), 0)

    async def test_variable_slice_and_no_file_size_ceiling(self):
        for size, slice_size in ((41*1024**2, 8*1024**2), (1024**3, 32*1024**2),
                                 (100*1024**3, PART_SIZE), (1024**4, PART_SIZE)):
            with self.subTest(size=size, slice_size=slice_size):
                self.store.rows.clear()
                await self.configure(size, slice_size)
                self.assertEqual(self.info()['part_count'], (size+slice_size-1)//slice_size)
                self.assertEqual(self.info()['part_size'], slice_size)
        # 512 MiB 是宿主单片能力；文件本身可大于它。
        self.assertEqual(layout(3*MAX_PART_BYTES, MAX_PART_BYTES), 3)
        self.assertEqual(layout(1, MAX_PART_BYTES+1), 1)
        for slice_size in (False, 0, -1, MAX_PART_BYTES+1):
            with self.subTest(slice_size=slice_size), self.assertRaises(dv.Conflict):
                layout(3*MAX_PART_BYTES, slice_size)

    async def test_server_slice_larger_than_old_limit_is_uploaded(self):
        slice_size = 17*1024**2
        await self.configure(slice_size+3, slice_size)
        await self.put(0, b'a'*slice_size)
        await self.put(1, b'end')
        self.assertEqual((await self.finish()).size, slice_size+3)
        self.assertEqual([len(r.content) for r in self.puts()], [slice_size, 3])

    async def test_hundreds_of_parts_have_linear_calls_and_paginated_finish(self):
        count, width = 257, 4096
        await self.configure(count*width, width)
        async def pages(request):
            if request.url.path.endswith('/s3_list_upload_parts'):
                marker = json.loads(request.content)['partNumberMarker']
                return httpx.Response(200, json={'code':0, 'data':{
                    'Parts':[p for p in self.parts if int(p['PartNumber'])>marker][:100]}})
        self.reply = pages
        for i in range(count):
            await self.put(i, bytes([i%256])*width)
        # 正常上传只有冷启动对账；不能每片全扫描存储或远端列表。
        self.assertEqual(self.count('/s3_list_upload_parts'), 1)
        self.assertLess(len(self.store.calls), count*20)
        data_items = [c for c in self.store.calls if c[0]=='items' and c[1].startswith('upload_data:')]
        self.assertEqual(len(data_items), 1)
        self.assertGreater(sum(len(json.dumps(v)) for (s,k),v in self.store.rows.items()
                               if s.startswith('upload_data:')), 32*1024)
        result = await self.finish()
        self.assertEqual(result.remote_id, '99')
        markers = [json.loads(r.content)['partNumberMarker'] for r in self.requests
                   if r.url.path.endswith('/s3_list_upload_parts')]
        self.assertEqual(markers, [0, 0, 100, 200, 257])
        self.assertEqual(self.count('/upload_complete/v2'), 1)

    async def test_restart_unknown_confirmation_and_missing_middle(self):
        await self.configure(5*4, 4)
        await self.put(0, b'aaaa')
        async def lost(method, scope, key, value):
            if method=='set' and key=='part:1':
                raise TimeoutError('confirmation not committed')
        self.store.hook = lost
        with self.assertRaises(dv.Transient):
            await self.put(1, b'bbbb')
        self.store.hook = None
        self.backend = Pan123(self.store)
        self.assertEqual((await self.begin()).next_part, 1)
        await self.put(1, b'bbbb')
        self.assertEqual(len(self.puts()), 2)  # 远端已有第二片，核对字节后仅补确认。
        await self.put(2, b'cccc')
        self.parts = [p for p in self.parts if p['PartNumber']!='2']
        self.backend = Pan123(self.store)
        self.assertEqual((await self.begin()).next_part, 1)
        await self.put(1, b'bbbb')
        await self.put(2, b'cccc')
        await self.put(3, b'dddd')
        await self.put(4, b'eeee')
        self.assertEqual(len(self.puts()), 6)
        self.assertEqual((await self.finish()).size, 20)

    async def test_paginated_recovery_handles_overlap_but_rejects_conflicts_and_incomplete_pages(self):
        await self.configure(205*4, 4)
        # 元数据压力无需分配影片正文；每片摘要来自同一实际四字节内容。
        digest = hashlib.md5(b'data').hexdigest()
        info = self.info()
        data_scope = 'upload_data:'+info['generation']
        for i in range(205):
            self.store.rows[(data_scope,f'part:{i}')] = {**self.state,'index':i,'size':4,'md5':digest,'etag':digest}
        all_parts = [{'PartNumber':str(i+1),'Size':'4','ETag':digest} for i in range(205)]
        mode = 'overlap'
        async def pages(request):
            if not request.url.path.endswith('/s3_list_upload_parts'):
                return None
            marker = json.loads(request.content)['partNumberMarker']
            parts = copy.deepcopy(all_parts[marker:marker+100])
            if marker==100:
                if mode=='overlap':parts.insert(0,dict(all_parts[99]))
                elif mode=='conflict':parts.insert(0,{**all_parts[99],'ETag':'f'*32})
                elif mode=='stuck':parts=[dict(all_parts[99])]
                elif mode=='error':return httpx.Response(503)
                elif mode=='cancel':raise asyncio.CancelledError('external')
                elif mode=='truncated_empty':return httpx.Response(200,json={'code':0,'data':{'Parts':[],'IsTruncated':True}})
            if mode=='extra' and marker==200:
                parts.append({'PartNumber':'206','Size':'4','ETag':digest})
            return httpx.Response(200,json={'code':0,'data':{'Parts':parts}})
        self.reply=pages
        self.backend=Pan123(self.store)
        self.assertEqual((await self.begin()).next_part,205)
        for mode,error in (('conflict',dv.Conflict),('stuck',dv.ApiChanged),('error',dv.Transient),
                           ('cancel',asyncio.CancelledError),('truncated_empty',dv.ApiChanged),('extra',dv.Conflict)):
            with self.subTest(mode=mode),self.assertRaises(error):
                await self.begin()
            self.assertIsNone(self.stored('merge_requested'))
        self.assertEqual(self.count('/upload_request'),1)
        self.assertEqual(self.puts(),[])
        self.assertEqual(self.count('/upload_complete/v2'),0)

    async def test_finish_rejects_changed_remote_prefix_even_after_fast_upload(self):
        await self.configure(12,4)
        for i in range(3):
            await self.put(i,b'data')
        self.parts[0]['ETag']='f'*32
        with self.assertRaises(dv.Conflict):
            await self.finish()
        self.assertIsNone(self.stored('merge_requested'))
        self.assertEqual(self.count('/upload_complete/v2'),0)

    async def test_malformed_or_extra_local_part_keys_are_not_ignored(self):
        await self.configure(12,4)
        await self.put(0,b'data')
        scope='upload_data:'+self.state['generation']
        for key in ('part:01','part:-1','part:3'):
            self.store.rows[(scope,key)]=copy.deepcopy(self.stored('part:0'))
            with self.subTest(key=key),self.assertRaises(dv.Conflict):
                await self.begin()
            del self.store.rows[(scope,key)]
        self.assertEqual(self.count('/upload_request'),1)

    async def test_known_future_remote_part_must_not_be_overwritten_by_fast_path(self):
        await self.configure(12, 4)
        self.parts = [{'PartNumber':'3', 'Size':'4', 'ETag':'f'*32}]
        await self.put(0, b'aaaa')
        await self.put(1, b'bbbb')
        with self.assertRaises(dv.Conflict):
            await self.put(2, b'cccc')
        self.assertEqual(len(self.puts()), 2)

    async def test_begin_invalidates_sequential_hint_when_remote_prefix_changes(self):
        await self.configure(12, 4)
        await self.put(0, b'aaaa')
        self.parts = []
        self.assertEqual((await self.begin()).next_part, 0)
        with self.assertRaises(dv.Conflict):
            await self.put(1, b'bbbb')
        self.assertEqual(len(self.puts()), 1)

    async def test_cancelled_later_put_reconciles_before_following_part(self):
        await self.configure(12, 4)
        await self.put(0, b'aaaa')
        entered = asyncio.Event()
        async def wait(request):
            entered.set()
            await asyncio.Event().wait()
        self.put_hook = wait
        task = asyncio.create_task(self.put(1, b'bbbb'))
        await entered.wait()
        before = len(self.requests), len(self.store.calls)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual((len(self.requests),len(self.store.calls)), before)
        self.put_hook = None
        with self.assertRaises(dv.Conflict):
            await self.put(2, b'cccc')
        await self.put(1, b'bbbb')
        await self.put(2, b'cccc')
        self.assertEqual(len(self.puts()), 3)

    async def test_merge_unknown_still_never_remerges_large_session(self):
        await self.configure(12, 4)
        for i in range(3):
            await self.put(i, bytes([i])*4)
        async def missing(request):
            if request.url.path.endswith('/upload_complete/v2'):
                return httpx.Response(200,json={'code':0,'data':{}})
        self.reply = missing
        with self.assertRaises(dv.Transient):
            await self.finish()
        self.backend = Pan123(self.store)
        for retry in (self.begin, self.finish):
            with self.assertRaises(dv.Conflict):
                await retry()
        self.assertEqual(self.count('/upload_complete/v2'), 1)
        self.assertEqual(self.count('/upload_request'), 1)
        self.assertIsNotNone(self.stored('merge_requested'))

    async def test_three_part_unknown_merge_recovers_only_after_full_md5(self):
        self.body = b'aaaabbbbcccc'
        self.init['SliceSize'] = 4
        self.fp = replace(self.fp,size=len(self.body),md5=hashlib.md5(self.body).hexdigest())
        self.file['Size'] = len(self.body)
        self.state = (await self.begin()).state
        for i in range(3):
            await self.put(i,self.body[i*4:(i+1)*4])
        async def missing(request):
            if request.url.path.endswith('/upload_complete/v2'):
                self.listed = True
                return httpx.Response(200,json={'code':0,'data':{}})
        self.reply = missing
        with self.assertRaises(dv.Transient):
            await self.finish()
        self.reply = None; self.backend = Pan123(self.store)
        self.content_body = b'x'*len(self.body)
        before = self.count('/s3_list_upload_parts')
        with self.assertRaises(dv.Conflict):
            await self.finish()
        self.content_body = None
        result = await self.finish()
        self.assertEqual(result.remote_id,'99')
        self.assertEqual(self.count('/download_info'),2)
        self.assertEqual(self.count('/s3_list_upload_parts'),before)
        self.assertEqual((self.count('/upload_request'),len(self.puts()),self.count('/upload_complete/v2')),(1,3,1))

    async def test_many_part_retired_cleanup_reenters_without_info(self):
        await self.configure(100*4, 4)
        for i in range(100):
            await self.put(i, b'data')
        info = copy.deepcopy(self.info())
        scope = 'upload:'+info['session_key']
        self.store.rows[(scope,'retired:'+info['generation'])] = {
            **self.state,'retired_at':time.time()-GRACE-1}
        failed = False
        async def lost(method, s, key, value):
            nonlocal failed
            if method=='delete' and key=='part:10' and not failed:
                failed = True
                self.store.rows.pop((s,key), None)
                raise TimeoutError('delete reply lost')
        self.store.hook = lost
        with self.assertRaises(dv.Transient):
            await self.backend._uploads.data._collect_one(info, [16])
        self.assertNotIn(('upload_data:'+info['generation'],'info'), self.store.rows)
        self.store.hook = None
        for _ in range(20):
            collector = UploadStore(self.store)
            before = len(self.store.calls)
            await collector._collect_one(info, [16])
            self.assertLessEqual(sum(c[0]=='delete' for c in self.store.calls[before:]), 16)
        self.assertFalse(any(s.startswith(('upload:', 'upload_data:', 'upload_gc:')) for s,k in self.store.rows))

    async def test_many_part_unknown_merge_is_retained_past_ttl(self):
        await self.configure(100*4, 4)
        for i in range(100):
            await self.put(i, b'data')
        self.store.rows[('upload_data:'+self.state['generation'],'merge_requested')] = dict(self.state)
        saved = copy.deepcopy(self.store.rows)
        with patch('cloud_123.upload_store.time.time', return_value=time.time()+RETENTION+GRACE+1):
            await self.backend._uploads.data._collect_one(self.info(), [16])
        self.assertEqual(saved, self.store.rows)


class LargeReadbackTests(UploadFixture):
    async def generated_file(self, size):
        self.generated_size = size
        self.file['Size'] = size
        self.listed = True
        self.digest = hashlib.md5()
        block = b'\x00'*(1024*1024)
        for offset in range(0,size,len(block)):
            self.digest.update(block[:min(len(block),size-offset)])
        self.fp = replace(self.fp,size=size,md5=self.digest.hexdigest())
        self.generated_ranges = []
        self.generated_bytes = 0
        self.block_on_range = None
        self.generated_entered = asyncio.Event()

    async def handler(self, request):
        if request.url.host=='content.invalid' and request.url.path=='/file':
            self.requests.append(request)
            self.assertNotIn('authorization', request.headers)
            self.assertNotIn('cookie', request.headers)
            start,end = map(int,request.headers['range'][6:].split('-'))
            self.generated_ranges.append((start,end))
            test = self
            class Stream(httpx.AsyncByteStream):
                async def __aiter__(self):
                    block = b'\x00'*(1024*1024)
                    if test.block_on_range==start:
                        test.generated_entered.set()
                        await asyncio.Event().wait()
                    for offset in range(start,end+1,len(block)):
                        chunk = block[:min(len(block),end-offset+1)]
                        test.generated_bytes += len(chunk)
                        yield chunk
                        await asyncio.sleep(0)
                async def aclose(self):
                    test.stream_closed.append(True)
            return httpx.Response(206,headers={'Content-Range':f'bytes {start}-{end}/{self.generated_size}'},stream=Stream())
        return await super().handler(request)

    async def test_one_gib_stream_full_md5_reuses_final_url_and_binds_after_read(self):
        await self.generated_file(1024**3)
        result = (await self.begin()).file
        self.assertEqual(result.size,1024**3)
        self.assertIsNone(result.sha1)
        self.assertEqual(self.generated_bytes,1024**3)
        self.assertEqual(len(self.generated_ranges),64)
        self.assertEqual(self.generated_ranges[-1],(63*PART_SIZE,1024**3-1))
        self.assertEqual(self.count('/redirect'),1)
        self.assertEqual(self.count('/download_info'),1)
        self.assertEqual(self.info_count,2)
        self.assertEqual(len(self.stream_closed),64)
        self.assertEqual(self.paced,len(self.requests))
        self.assertEqual(self.count('/upload_request'),0)

    async def test_large_readback_md5_mismatch_does_not_verify(self):
        await self.generated_file(41*1024**2)
        self.fp = replace(self.fp,md5='f'*32)
        with self.assertRaises(dv.Conflict):
            await self.begin()
        self.assertEqual(self.generated_bytes,41*1024**2)
        self.assertEqual(self.info_count,1)
        self.assertEqual(self.store.rows,{})

    async def test_large_readback_binding_change_rejects_after_full_digest(self):
        await self.generated_file(41*1024**2)
        self.after_info = {**self.file,'ParentFileId':2}
        with self.assertRaises(dv.Conflict):
            await self.begin()
        self.assertEqual(self.generated_bytes,41*1024**2)
        self.assertEqual(self.info_count,2)
        self.assertEqual(self.store.rows,{})

    async def test_budget_interruption_does_not_save_partial_hash_or_succeed(self):
        await self.generated_file(41*1024**2)
        original = net.pacer.get()
        async def pace():
            await original()
            if self.generated_bytes>=PART_SIZE:
                raise dv.RateLimited('host budget')
        ctx = net.pacer.set(pace)
        try:
            with self.assertRaises(dv.RateLimited):
                await self.begin()
        finally:
            net.pacer.reset(ctx)
        self.assertEqual(self.generated_bytes,PART_SIZE)
        self.assertEqual(self.info_count,1)
        self.assertEqual(self.store.rows,{})
        self.generated_ranges.clear(); self.generated_bytes=0
        self.assertEqual((await self.begin()).file.size,41*1024**2)
        self.assertEqual(self.generated_ranges[0],(0,PART_SIZE-1))

    async def test_cancel_later_range_has_no_followup_requests_or_success(self):
        await self.generated_file(41*1024**2)
        self.block_on_range=PART_SIZE
        task=asyncio.create_task(self.begin())
        await self.generated_entered.wait()
        before=len(self.requests)
        task.cancel('external')
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(len(self.requests),before)
        self.assertEqual(self.info_count,1)
        self.assertEqual(len(self.stream_closed),2)
        self.assertEqual(self.store.rows,{})
