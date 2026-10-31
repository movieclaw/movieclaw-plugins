"""仅回收文件；内存模拟部分成功与丢失响应，禁止真实网络。"""
from __future__ import annotations

import asyncio
import copy
import json
from dataclasses import replace

import httpx
from movieclaw_sdk import drives as dv, net
from cloud_123.backend import Pan123
from tests.test_begin_upload import UploadFixture


class DeleteTest(UploadFixture):
    def setUp(self):
        super().setUp()
        self.files = {'99': dict(self.file), '100': {**self.file, 'FileId': 100}}
        self.apply_ids = None
        self.trash_hook = None
        self.trash_code = 0
        self.trash_status = 200

    async def handler(self, request):
        path = request.url.path
        if path.endswith(('/file/info', '/file/trash')):
            self.requests.append(request)
            self.assertEqual(request.headers['authorization'], 'Bearer '+self.account.credentials['token'])
            if self.reply:
                result = await self.reply(request)
                if result is not None:return result
            payload = json.loads(request.content)
            if path.endswith('/file/info'):
                remote_id = payload['fileIdList'][0]['FileId']
                row = self.files.get(remote_id)
                return httpx.Response(200, json={'code': 0, 'data': {'infoList': [row] if row else []}})
            self.assertEqual((payload['event'], payload['operation'], payload['driveid']), ('intoRecycle', True, 0))
            for row in payload['filetrashinfolist']:
                remote_id = row['FileId']
                if self.apply_ids is None or remote_id in self.apply_ids:
                    if remote_id in self.files:self.files[remote_id]['Trashed'] = True
            if self.trash_hook:
                result = await self.trash_hook(request)
                if result is not None:return result
            return httpx.Response(self.trash_status, json={'code': self.trash_code, 'data': {}, 'message': 'private'})
        return await super().handler(request)

    async def delete(self, ids=None):
        return await self.backend.delete(self.account, ['99', '100'] if ids is None else ids)

    def writes(self):
        return [r for r in self.requests if r.url.path.endswith('/file/trash')]

    def write_ids(self, number=-1):
        return [r['FileId'] for r in json.loads(self.writes()[number].content)['filetrashinfolist']]

    def read_ids(self):
        return [json.loads(r.content)['fileIdList'][0]['FileId'] for r in self.requests if r.url.path.endswith('/file/info')]

    async def test_mixed_missing_recycled_and_duplicate_ids_only_trash_live_files(self):
        self.backend = Pan123()
        self.assertTrue(self.backend.capabilities.recycle_bin)
        self.assertTrue(self.backend.capabilities.writable)
        self.files['100']['Trashed'] = True
        self.assertIsNone(await self.delete(['00099', '100', '101', '99']))
        self.assertEqual(self.write_ids(), ['99'])
        self.assertEqual(self.read_ids(), ['99', '100', '101', '99'])
        self.assertEqual(self.paced, len(self.requests))
        self.assertEqual(len(self.closed), len(self.requests))
        self.assertEqual(self.store.calls, [])
        await self.delete(['99', '100', '101'])
        self.assertEqual(len(self.writes()), 1)

    async def test_empty_and_invalid_batch_do_no_io(self):
        self.account.credentials = {}
        self.assertIsNone(await self.delete([]))
        for ids in ('99', ('99',), [None], ['0'], ['99', 'bad'], ['99']*101):
            with self.subTest(ids=ids), self.assertRaises(dv.Conflict):await self.delete(ids)
        with self.assertRaises(dv.Conflict):await self.backend.delete(self.account, None)
        self.assertEqual(self.requests, [])

    async def test_one_hundred_files_are_prechecked_and_verified_with_one_batch_write(self):
        ids = [str(n) for n in range(1, 101)]
        self.files = {i: {**self.file, 'FileId': int(i)} for i in ids}
        await self.delete(ids)
        self.assertEqual(self.write_ids(), ids)
        self.assertEqual(self.read_ids(), ids+ids)
        self.assertEqual(len(self.requests), 202)
        self.assertEqual(self.paced, 202)

    async def test_directory_anywhere_in_batch_prevents_all_writes(self):
        for trashed in (False, True):
            self.files['100'].update(Type=1, Trashed=trashed)
            with self.assertRaises(dv.Conflict):await self.delete()
        self.assertFalse(self.files['99']['Trashed'])
        self.assertEqual(self.writes(), [])

    async def test_restricted_file_needs_identity_and_type_not_playback_metadata(self):
        self.files['99'] = {'FileId': 99, 'Type': 0, 'Trashed': False, 'Status': 1}
        await self.delete(['99'])
        self.assertTrue(self.files['99']['Trashed'])
        self.assertEqual(self.write_ids(), ['99'])

    async def test_invalid_identity_type_or_recycle_state_prevents_write(self):
        saved = copy.deepcopy(self.files)
        for field, value, error in (('FileId', 77, dv.Conflict), ('FileId', None, dv.ApiChanged),
                                    ('Type', None, dv.ApiChanged), ('Type', True, dv.ApiChanged),
                                    ('Trashed', None, dv.ApiChanged), ('Trashed', '0', dv.ApiChanged)):
            with self.subTest(field=field, value=value):
                self.files = copy.deepcopy(saved); self.files['100'][field] = value
                with self.assertRaises(error):await self.delete()
        self.assertEqual(self.writes(), [])

    async def test_account_mismatch_or_missing_token_stops_before_file_reads(self):
        self.account = replace(self.account, user_id='67890')
        with self.assertRaises(dv.Conflict):await self.delete()
        self.assertEqual(self.read_ids(), [])
        self.account.credentials = {}
        before = len(self.requests)
        with self.assertRaises(dv.AuthExpired):await self.delete()
        self.assertEqual(len(self.requests), before)

    async def test_explicit_missing_is_idempotent_but_http_failure_is_not(self):
        async def missing(request):
            if request.url.path.endswith('/file/info'):
                return httpx.Response(200, json={'code': 5066, 'data': {}})
        self.reply = missing
        self.assertIsNone(await self.delete())
        for status, error in ((404, dv.Transient), (401, dv.AuthExpired), (429, dv.RateLimited)):
            async def fail(request):
                if request.url.path.endswith('/file/info'):return httpx.Response(status)
            self.reply = fail
            with self.assertRaises(error):await self.delete()
        self.assertEqual(self.writes(), [])

    async def test_partial_success_is_not_whole_success_and_retry_only_trashes_remaining(self):
        self.apply_ids = {'99'}
        with self.assertRaises(dv.Transient):await self.delete()
        self.assertEqual(self.read_ids(), ['99', '100', '99', '100'])
        self.assertEqual(self.write_ids(), ['99', '100'])
        self.apply_ids = None; self.backend = Pan123()
        await self.delete()
        self.assertEqual(self.write_ids(), ['100'])
        self.assertEqual(len(self.writes()), 2)

    async def test_response_lost_after_partial_apply_recovers_after_restart(self):
        self.apply_ids = {'99'}
        async def fail(request):raise httpx.ReadTimeout('private request')
        self.trash_hook = fail
        with self.assertRaises(dv.Transient) as caught:await self.delete()
        self.assertNotIn('private', str(caught.exception))
        self.assertEqual(self.read_ids(), ['99', '100'])
        self.trash_hook = None; self.apply_ids = None; self.backend = Pan123()
        await self.delete()
        self.assertEqual(self.write_ids(), ['100'])

    async def test_response_lost_after_full_apply_does_not_repeat_write(self):
        async def fail(request):raise httpx.ReadTimeout('private')
        self.trash_hook = fail
        with self.assertRaises(dv.Transient):await self.delete()
        self.trash_hook = None; self.backend = Pan123()
        await self.delete()
        self.assertEqual(len(self.writes()), 1)

    async def test_timeout_without_apply_rechecks_and_retries_live_ids_on_next_call(self):
        async def fail(request):
            if request.url.path.endswith('/file/trash'):raise httpx.ReadTimeout('private')
        self.reply = fail
        with self.assertRaises(dv.Transient):await self.delete()
        self.assertEqual(self.read_ids(), ['99', '100'])
        self.assertFalse(self.files['99']['Trashed'])
        self.reply = None
        await self.delete()
        self.assertEqual(self.read_ids(), ['99', '100', '99', '100', '99', '100'])
        self.assertEqual(self.write_ids(), ['99', '100'])
        self.assertEqual(len(self.writes()), 2)

    async def test_write_missing_response_requires_per_file_confirmation(self):
        self.trash_code = 5066; self.apply_ids = {'99'}
        with self.assertRaises(dv.Transient):await self.delete()
        self.assertEqual(self.read_ids(), ['99', '100', '99', '100'])
        self.apply_ids = None
        await self.delete()
        self.assertEqual(self.write_ids(), ['100'])

    async def test_postcheck_missing_rows_are_success_but_malformed_rows_are_not(self):
        async def disappear(request):self.files.clear()
        self.trash_hook = disappear
        await self.delete()
        self.files = {'99': dict(self.file), '100': {**self.file, 'FileId': 100}}
        async def bad_detail(request):
            if request.url.path.endswith('/file/info') and len(self.writes()) > 1:
                return httpx.Response(200, json={'code': 0, 'data': {'infoList': None}})
        self.reply = bad_detail; self.trash_hook = None
        with self.assertRaises(dv.ApiChanged):await self.delete()

    async def test_postcheck_failure_then_retry_rechecks_without_repeating_write(self):
        async def fail(request):
            if request.url.path.endswith('/file/info') and self.writes():raise httpx.ReadTimeout('private')
        self.reply = fail
        with self.assertRaises(dv.Transient):await self.delete()
        self.reply = None
        await self.delete()
        self.assertEqual(len(self.writes()), 1)

    async def test_write_errors_are_not_success_and_close_does_not_mask_them(self):
        for status, code, error in ((401, 0, dv.AuthExpired), (429, 0, dv.RateLimited),
                                    (200, 401, dv.AuthExpired), (200, 1, dv.Transient), (500, 0, dv.Transient)):
            with self.subTest(status=status, code=code):
                self.files['99']['Trashed'] = False; self.files['100']['Trashed'] = False
                self.apply_ids = set(); self.close_error = None
                self.trash_status = status; self.trash_code = code
                async def close_fail(request):self.close_error = RuntimeError('private close')
                self.trash_hook = close_fail
                with self.assertRaises(error) as caught:await self.delete()
                self.assertNotIn('private', str(caught.exception))
                self.assertNotIn('关闭', str(caught.exception))

    async def test_sole_close_failure_keeps_unknown_result_and_retry_skips_applied(self):
        async def close_fail(request):self.close_error = RuntimeError('private close')
        self.trash_hook = close_fail
        with self.assertRaises(dv.Transient) as caught:await self.delete()
        self.assertNotIn('private', str(caught.exception))
        self.assertEqual(self.read_ids(), ['99', '100'])
        self.trash_hook = None; self.close_error = None
        await self.delete()
        self.assertEqual(len(self.writes()), 1)

    async def test_cancellation_during_write_stops_postchecks_and_retry_recovers(self):
        entered = asyncio.Event(); self.apply_ids = {'99'}
        async def wait(request):entered.set(); await asyncio.Event().wait()
        self.trash_hook = wait
        task = asyncio.create_task(self.delete()); await entered.wait()
        before = len(self.requests); self.close_error = RuntimeError('private close'); task.cancel('external')
        with self.assertRaises(asyncio.CancelledError) as caught:await task
        self.assertEqual(caught.exception.args, ('external',))
        self.assertEqual(len(self.requests), before)
        self.assertEqual(len(self.closed), before)
        self.trash_hook = None; self.close_error = None; self.apply_ids = None; self.backend = Pan123()
        await self.delete()
        self.assertEqual(self.write_ids(), ['100'])

    async def test_new_cancellation_from_write_close_stops_postchecks(self):
        async def close_cancel(request):self.close_error = asyncio.CancelledError('new cancel')
        self.trash_hook = close_cancel
        with self.assertRaises(asyncio.CancelledError) as caught:await self.delete()
        self.assertEqual(caught.exception.args, ('new cancel',))
        self.assertEqual(self.read_ids(), ['99', '100'])
        self.assertEqual(len(self.closed), len(self.requests))
        self.trash_hook = None; self.close_error = None
        await self.delete()
        self.assertEqual(len(self.writes()), 1)

    async def test_cancel_precheck_or_postcheck_has_no_followup(self):
        for after_write in (False, True):
            with self.subTest(after_write=after_write):
                self.files['99']['Trashed'] = False; self.files['100']['Trashed'] = False
                self.requests.clear(); self.closed.clear(); entered = asyncio.Event(); self.close_error = None
                async def wait(request):
                    if request.url.path.endswith('/file/info') and bool(self.writes()) == after_write:
                        entered.set(); await asyncio.Event().wait()
                self.reply = wait
                task = asyncio.create_task(self.delete()); await entered.wait()
                before = len(self.requests); self.close_error = RuntimeError('private close'); task.cancel()
                with self.assertRaises(asyncio.CancelledError):await task
                self.assertEqual(len(self.requests), before)
                self.assertEqual(len(self.writes()), int(after_write))

    async def test_budget_denial_or_wait_cancellation_never_sends_write(self):
        original = net.pacer.get(); reads = len(self.read_ids())
        async def deny():
            await original()
            if len(self.read_ids()) >= reads+2:raise dv.RateLimited('host budget')
        ctx = net.pacer.set(deny)
        try:
            with self.assertRaises(dv.RateLimited):await self.delete()
        finally:net.pacer.reset(ctx)
        reads = len(self.read_ids()); entered = asyncio.Event()
        async def wait():
            await original()
            if len(self.read_ids()) >= reads+2:
                entered.set(); await asyncio.Event().wait()
        ctx = net.pacer.set(wait)
        try:
            task = asyncio.create_task(self.delete()); await entered.wait(); task.cancel()
            with self.assertRaises(asyncio.CancelledError):await task
        finally:net.pacer.reset(ctx)
        self.assertEqual(self.writes(), [])
        self.assertFalse(self.files['99']['Trashed'])
        self.assertFalse(self.files['100']['Trashed'])
