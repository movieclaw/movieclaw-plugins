"""仅回收文件；内存模拟部分成功与丢失响应，禁止真实网络。"""
from __future__ import annotations

import asyncio
import copy
import json
from dataclasses import replace
from functools import partial
from unittest.mock import patch

import httpx
from movieclaw_sdk import drives as dv, net
from cloud_123.backend import Pan123
from cloud_123 import client, delete_diagnostics
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
            # 模拟大小写敏感、未知字段仍返回成功但不执行的服务。
            if 'fileTrashInfoList' not in payload or 'driveId' not in payload:
                return httpx.Response(200, json={'code': 0, 'data': {}})
            self.assertEqual((payload['event'], payload['operation'], payload['driveId']), ('intoRecycle', True, 0))
            # 候选协议假设：字符串 ID 返回空列表但不回收；不代表真实服务已证实。
            if any(type(row['FileId']) is not int for row in payload['fileTrashInfoList']):
                return httpx.Response(200, json={'code': 0, 'data': {'InfoList': []}})
            for row in payload['fileTrashInfoList']:
                remote_id = str(row['FileId'])
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
        return [r['FileId'] for r in json.loads(self.writes()[number].content)['fileTrashInfoList']]

    def read_ids(self):
        return [json.loads(r.content)['fileIdList'][0]['FileId'] for r in self.requests if r.url.path.endswith('/file/info')]

    def diagnostics(self, captured):
        return [json.loads(record.getMessage().split('123-delete-diag ', 1)[1]) for record in captured.records]

    async def test_diagnostics_capture_wire_shape_and_pre_post_state_without_extra_requests(self):
        with self.assertLogs('cloud_123.delete_diagnostics', level='INFO') as captured:
            await self.delete(['99'])
        rows = self.diagnostics(captured)
        self.assertEqual(len({r['trace'] for r in rows}), 1)
        self.assertEqual(len(self.requests), 4)
        self.assertEqual(self.paced, 4)
        self.assertEqual(len(self.closed), 4)
        prepared = [r for r in rows if r['event'] == 'request_prepared' and r['phase'] == 'trash']
        self.assertEqual(prepared[0]['fields'], ['driveId', 'event', 'operation', 'fileTrashInfoList'])
        self.assertEqual(prepared[0]['files'], [{'file': 1, 'id_type': 'int'}])
        replies = [r for r in rows if r['event'] == 'api_response']
        self.assertEqual([r['phase'] for r in replies], ['precheck', 'trash', 'postcheck'])
        self.assertEqual([r['code'] for r in replies], [0, 0, 0])
        self.assertEqual(replies[1]['data_fields'], {})
        for row, trashed in ((replies[0], False), (replies[2], True)):
            self.assertEqual(row['rows'], 1)
            self.assertTrue(row['id_match'])
            self.assertEqual(row['response_id_type'], 'int')
            self.assertEqual(row['Trashed'], trashed)
        self.assertEqual(rows[-1]['event'], 'returned')
        self.assertIsNone(delete_diagnostics._current.get())
        with self.assertNoLogs('cloud_123.delete_diagnostics', level='INFO'):
            await client.file_info(self.account.credentials['token'], '99')

    async def test_diagnostics_do_not_treat_success_code_without_state_change_as_success(self):
        self.apply_ids = set()
        with self.assertLogs('cloud_123.delete_diagnostics', level='INFO') as captured:
            with self.assertRaises(dv.Transient):await self.delete(['99'])
        rows = self.diagnostics(captured)
        self.assertEqual(rows[-1]['error_type'], 'Transient')
        self.assertNotIn('returned', [r['event'] for r in rows])
        post = next(r for r in rows if r['phase'] == 'postcheck' and r['event'] == 'api_response')
        self.assertFalse(post['Trashed'])
        self.assertIsNone(delete_diagnostics._current.get())

    async def test_diagnostics_preserve_http_status_and_original_error_for_bad_responses(self):
        for status, body, error in ((200, b'not-json-private', dv.ApiChanged),
                                    (200, b'["private"]', dv.ApiChanged),
                                    (401, b'private', dv.AuthExpired), (500, b'private', dv.Transient)):
            with self.subTest(status=status, body=body):
                async def reply(request):
                    if request.url.path.endswith('/file/trash'):
                        return httpx.Response(status, content=body)
                self.reply = reply
                with self.assertLogs('cloud_123.delete_diagnostics', level='INFO') as captured:
                    with self.assertRaises(error):await self.delete(['99'])
                rows = self.diagnostics(captured)
                http = [r for r in rows if r['phase'] == 'trash' and r['event'] == 'http_response']
                self.assertEqual([r['status'] for r in http], [status])
                self.assertEqual(rows[-1]['error_type'], error.__name__)
                self.assertNotIn('private', '\n'.join(captured.output))
                self.assertIsNone(delete_diagnostics._current.get())

    async def test_diagnostics_never_log_credentials_ids_names_or_unknown_response_values(self):
        remote_id = '123456789012345'
        secret = 'private-cookie-and-signed-url'
        token = self.account.credentials['token']
        self.files = {remote_id: {**self.file, 'FileId': int(remote_id), 'FileName': secret,
                                 'DownloadUrl': secret, 'Token': token}}
        async def reply(request):
            if request.url.path.endswith('/file/trash'):
                return httpx.Response(200, headers={'set-cookie': secret}, json={
                    'code': 0, 'message': secret, 'data': {
                        secret: token, 'FileId': remote_id, 'fileTrashInfoList': [{'Token': token}],
                    },
                })
        self.trash_hook = reply
        with self.assertLogs('cloud_123.delete_diagnostics', level='INFO') as captured:
            await self.delete([remote_id])
        output = '\n'.join(captured.output)
        for value in (secret, token, remote_id, 'authorization', 'Bearer', 'set-cookie'):
            self.assertNotIn(value, output)
        row = next(r for r in self.diagnostics(captured) if r['phase'] == 'trash' and r['event'] == 'api_response')
        self.assertEqual(row['data_fields'], {'FileId': 'str', 'fileTrashInfoList': 'list'})
        self.assertEqual(row['other_data_fields'], 1)
        self.files[remote_id].update(Trashed=secret)
        with self.assertLogs('cloud_123.delete_diagnostics', level='INFO') as captured:
            with self.assertRaises(dv.ApiChanged):await self.delete([remote_id])
        row = next(r for r in self.diagnostics(captured) if r['event'] == 'api_response')
        self.assertIsNone(row['Trashed'])
        self.assertEqual(row['Trashed_type'], 'str')
        self.assertNotIn(secret, '\n'.join(captured.output))

    async def test_diagnostics_cancel_and_close_failure_do_not_log_success_or_send_followups(self):
        entered = asyncio.Event()
        async def wait(request):entered.set(); await asyncio.Event().wait()
        self.trash_hook = wait
        with self.assertLogs('cloud_123.delete_diagnostics', level='INFO') as captured:
            task = asyncio.create_task(self.delete(['99']))
            await entered.wait()
            before = len(self.requests)
            self.close_error = RuntimeError('private close')
            task.cancel('external')
            with self.assertRaises(asyncio.CancelledError) as caught:await task
        self.assertEqual(caught.exception.args, ('external',))
        self.assertEqual(len(self.requests), before)
        self.assertEqual(len(self.closed), before)
        rows = self.diagnostics(captured)
        self.assertEqual(rows[-1]['error_type'], 'CancelledError')
        self.assertNotIn('returned', [r['event'] for r in rows])
        self.assertNotIn('postcheck', [r['phase'] for r in rows])

    async def test_diagnostics_are_isolated_between_overlapping_deletes(self):
        entered = asyncio.Event(); release = asyncio.Event()
        async def wait(request):
            if json.loads(request.content)['fileTrashInfoList'][0]['FileId'] == 99:
                entered.set(); await release.wait()
        self.trash_hook = wait
        with self.assertLogs('cloud_123.delete_diagnostics', level='INFO') as captured:
            task = asyncio.create_task(self.delete(['99']))
            try:
                await entered.wait()
                await self.delete(['100'])
            finally:
                release.set()
                await task
        rows = self.diagnostics(captured)
        traces = {r['trace'] for r in rows}
        self.assertEqual(len(traces), 2)
        for trace in traces:
            events = [r for r in rows if r['trace'] == trace]
            self.assertEqual(events[0]['event'], 'start')
            self.assertEqual(events[-1]['event'], 'returned')
            self.assertEqual([r['phase'] for r in events if r['event'] == 'api_response'],
                             ['precheck', 'trash', 'postcheck'])
            for event in events:
                if 'files' in event:
                    kind = 'int' if event['phase'] == 'trash' else 'str'
                    self.assertEqual(event['files'], [{'file': 1, 'id_type': kind}])
        self.assertIsNone(delete_diagnostics._current.get())

    async def test_diagnostics_logging_failure_preserves_result_errors_and_cancel(self):
        with patch.object(delete_diagnostics._logger, 'info', side_effect=OSError('private log')):
            await self.delete(['99'])
            self.assertTrue(self.files['99']['Trashed'])
            self.apply_ids = set()
            with self.assertRaises(dv.Transient):await self.delete(['100'])
            async def cancel(request):raise asyncio.CancelledError('external')
            self.trash_hook = cancel
            with self.assertRaises(asyncio.CancelledError) as caught:await self.delete(['100'])
            self.assertEqual(caught.exception.args, ('external',))
        self.assertIsNone(delete_diagnostics._current.get())

    async def test_diagnostics_prepared_is_not_a_sent_request_when_budget_denies(self):
        original = net.pacer.get()
        async def deny():
            await original()
            if self.read_ids():raise dv.RateLimited('host budget')
        ctx = net.pacer.set(deny)
        try:
            with self.assertLogs('cloud_123.delete_diagnostics', level='INFO') as captured:
                with self.assertRaises(dv.RateLimited):await self.delete(['99'])
        finally:net.pacer.reset(ctx)
        rows = self.diagnostics(captured)
        self.assertEqual([r['event'] for r in rows if r['phase'] == 'trash'], ['request_prepared', 'failed'])
        self.assertEqual(self.writes(), [])
        self.assertIsNone(delete_diagnostics._current.get())

    async def test_trash_summary_matches_ids_without_leaking_rows_or_collapsing_duplicates(self):
        secret = 'private-name-cookie-url'
        token = self.account.credentials['token']
        remote_id = '123456789012345'
        self.files = {remote_id: {**self.file, 'FileId': int(remote_id)}}
        info = [
            {'FileId': int(remote_id), 'Type': 0, 'Trashed': True, 'FileName': secret, 'Token': token},
            {'FileId': '000'+remote_id, 'Type': 0, 'Trashed': 1},
            {'FileId': 7777777, 'Trashed': False},
            {'FileId': secret, 'Type': token, 'Trashed': {'Cookie': token}},
            {'FileId': None}, {'FileId': True}, {}, secret, None, [token],
        ]
        async def reply(request):return httpx.Response(200, json={'code': 0, 'data': {'InfoList': info}})
        self.trash_hook = reply
        with self.assertLogs('cloud_123.delete_diagnostics', level='INFO') as captured:
            await self.delete([remote_id])
        summary = next(r for r in self.diagnostics(captured) if r['phase'] == 'trash' and r['event'] == 'api_response')
        self.assertTrue(summary['InfoList_present'])
        self.assertEqual(summary['InfoList_type'], 'list')
        self.assertEqual(summary['trash_rows'], len(info))
        items = summary['trash_items']
        self.assertEqual(len(items), len(info))
        self.assertEqual([r['file'] for r in items[:7]], [1, 1, None, None, None, None, None])
        self.assertEqual([r['id_match'] for r in items[:7]], [True, True, False, False, False, False, False])
        self.assertEqual([r['response_id_type'] for r in items[:6]], ['int', 'str', 'int', 'str', 'NoneType', 'bool'])
        self.assertEqual(items[0]['Trashed'], True)
        self.assertEqual(items[1]['Trashed'], 1)
        self.assertFalse(items[2]['Trashed'])
        self.assertIsNone(items[3]['Trashed'])
        self.assertEqual(items[3]['Trashed_type'], 'dict')
        self.assertEqual(items[7:], [{'row_type': 'str'}, {'row_type': 'NoneType'}, {'row_type': 'list'}])
        output = '\n'.join(captured.output)
        for value in (secret, token, remote_id, '7777777', 'Cookie', 'FileName'):
            self.assertNotIn(value, output)
        self.assertEqual(len(self.requests), 4)
        self.assertEqual(self.paced, 4)
        self.assertEqual(len(self.closed), 4)

    async def test_trash_summary_distinguishes_missing_null_invalid_and_empty_list(self):
        for data, present, kind in (({}, False, 'NoneType'), ({'InfoList': None}, True, 'NoneType'),
                                   ({'InfoList': 'private'}, True, 'str'), ({'InfoList': {}}, True, 'dict'),
                                   ({'infoList': []}, False, 'NoneType'), ({'InfoList': []}, True, 'list')):
            with self.subTest(data=data):
                self.files['99']['Trashed'] = False
                async def reply(request):return httpx.Response(200, json={'code': 0, 'data': data})
                self.trash_hook = reply
                with self.assertLogs('cloud_123.delete_diagnostics', level='INFO') as captured:
                    await self.delete(['99'])
                summary = next(r for r in self.diagnostics(captured) if r['phase'] == 'trash' and r['event'] == 'api_response')
                self.assertEqual(summary['InfoList_present'], present)
                self.assertEqual(summary['InfoList_type'], kind)
                if kind == 'list':
                    self.assertEqual(summary['trash_rows'], 0)
                    self.assertEqual(summary['trash_items'], [])
                else:
                    self.assertNotIn('trash_rows', summary)
                    self.assertNotIn('trash_items', summary)
                self.assertNotIn('private', '\n'.join(captured.output))

    async def test_trash_summary_claimed_recycled_never_replaces_postcheck(self):
        self.apply_ids = set()
        async def reply(request):
            return httpx.Response(200, json={'code': 0, 'data': {'InfoList': [{'FileId': 99, 'Trashed': True}]}})
        self.trash_hook = reply
        with self.assertLogs('cloud_123.delete_diagnostics', level='INFO') as captured:
            with self.assertRaises(dv.Transient):await self.delete(['99'])
        events = self.diagnostics(captured)
        summary = next(r for r in events if r['phase'] == 'trash' and r['event'] == 'api_response')
        self.assertTrue(summary['trash_items'][0]['Trashed'])
        post = next(r for r in events if r['phase'] == 'postcheck' and r['event'] == 'api_response')
        self.assertFalse(post['Trashed'])
        self.assertNotIn('returned', [r['event'] for r in events])
        self.assertEqual(events[-1]['error_type'], 'Transient')
        self.assertEqual(self.read_ids(), ['99', '99'])

    async def test_trash_summary_retry_matches_only_ids_in_current_write(self):
        self.apply_ids = {'99'}
        async def reply(request):
            return httpx.Response(200, json={'code': 0, 'data': {'InfoList': [self.files['99'], self.files['100']]}})
        self.trash_hook = reply
        with self.assertRaises(dv.Transient):await self.delete()
        self.apply_ids = None
        with self.assertLogs('cloud_123.delete_diagnostics', level='INFO') as captured:
            await self.delete()
        summary = next(r for r in self.diagnostics(captured) if r['phase'] == 'trash' and r['event'] == 'api_response')
        self.assertEqual(summary['files'], [{'file': 2, 'id_type': 'int'}])
        self.assertEqual([r['file'] for r in summary['trash_items']], [None, 2])
        self.assertEqual([r['id_match'] for r in summary['trash_items']], [False, True])
        self.assertEqual(self.write_ids(), [100])

    async def test_final_http_request_preserves_case_and_only_trashes_target(self):
        await self.delete(['99'])
        request = self.writes()[0]
        self.assertEqual(request.method, 'POST')
        self.assertEqual(str(request.url), 'https://123pan.com/b/api/file/trash')
        self.assertEqual(json.loads(request.content), {
            'driveId': 0, 'event': 'intoRecycle', 'operation': True,
            'fileTrashInfoList': [{'FileId': 99}],
        })
        self.assertTrue(self.files['99']['Trashed'])
        self.assertFalse(self.files['100']['Trashed'])
        self.assertEqual(self.read_ids(), ['99', '99'])
        self.assertEqual(self.paced, len(self.requests))
        self.assertEqual(len(self.closed), len(self.requests))
        await self.delete(['99'])
        self.assertEqual(len(self.writes()), 1)

    async def test_string_trash_id_empty_ack_is_not_success_and_integer_retry_is_verified(self):
        async def string_trash(token, ids):
            await client._call(partial(client._client(token).request, 'file/trash', 'POST', json={
                'driveId': 0, 'event': 'intoRecycle', 'operation': True,
                'fileTrashInfoList': [{'FileId': i} for i in ids],
            }), '移入回收站')
        with patch('cloud_123.backend.trash', string_trash):
            with self.assertRaises(dv.Transient):await self.delete(['99'])
        self.assertEqual(self.write_ids(), ['99'])
        self.assertFalse(self.files['99']['Trashed'])
        self.assertEqual(self.read_ids(), ['99', '99'])
        await self.delete(['99'])
        before, after = [json.loads(r.content) for r in self.writes()]
        self.assertIs(type(after['fileTrashInfoList'][0]['FileId']), int)
        before['fileTrashInfoList'][0]['FileId'] = 99
        self.assertEqual(before, after)
        self.assertTrue(self.files['99']['Trashed'])
        self.assertFalse(self.files['100']['Trashed'])
        self.assertEqual(self.read_ids(), ['99']*4)

    async def test_large_integer_trash_id_is_exact_and_detail_ids_remain_strings(self):
        remote_id = '9007199254740993'
        self.files = {remote_id: {**self.file, 'FileId': int(remote_id)}}
        await self.delete(['000'+remote_id, remote_id])
        self.assertEqual(self.write_ids(), [9007199254740993])
        self.assertIs(type(self.write_ids()[0]), int)
        self.assertEqual(self.read_ids(), [remote_id, remote_id])
        self.assertTrue(self.files[remote_id]['Trashed'])
        self.assertEqual(len(self.requests), 4)

    async def test_dependency_lowercase_request_acknowledged_without_apply_is_not_success(self):
        async def legacy_trash(token, ids):
            await client._call(partial(client._client(token).fs_trash, ids, event='intoRecycle'), '移入回收站')
        with patch('cloud_123.backend.trash', legacy_trash):
            with self.assertRaises(dv.Transient):await self.delete(['99'])
        self.assertEqual(json.loads(self.writes()[0].content), {
            'driveid': 0, 'event': 'intoRecycle', 'operation': True,
            'filetrashinfolist': [{'FileId': '99'}],
        })
        self.assertFalse(self.files['99']['Trashed'])
        self.assertEqual(self.read_ids(), ['99', '99'])
        await self.delete(['99'])
        self.assertEqual(self.write_ids(), [99])
        self.assertTrue(self.files['99']['Trashed'])
        self.assertFalse(self.files['100']['Trashed'])

    async def test_success_code_without_recycle_state_change_is_not_success(self):
        self.apply_ids = set()
        for code in (0, 200):
            with self.subTest(code=code):
                self.trash_code = code
                with self.assertRaises(dv.Transient) as caught:await self.delete()
                self.assertNotIn(self.account.credentials['token'], str(caught.exception))
                self.assertNotIn('private', str(caught.exception))
                self.assertFalse(self.files['99']['Trashed'])
                self.assertFalse(self.files['100']['Trashed'])
        self.assertEqual(len(self.writes()), 2)
        self.assertEqual(self.read_ids(), ['99', '100', '99', '100']*2)

    async def test_mixed_missing_recycled_and_duplicate_ids_only_trash_live_files(self):
        self.backend = Pan123()
        self.assertTrue(self.backend.capabilities.recycle_bin)
        self.assertTrue(self.backend.capabilities.writable)
        self.files['100']['Trashed'] = True
        self.assertIsNone(await self.delete(['00099', '100', '101', '99']))
        self.assertEqual(self.write_ids(), [99])
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
        self.assertEqual(self.write_ids(), [int(i) for i in ids])
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
        self.assertEqual(self.write_ids(), [99])

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
        self.assertEqual(self.write_ids(), [99, 100])
        self.apply_ids = None; self.backend = Pan123()
        await self.delete()
        self.assertEqual(self.write_ids(), [100])
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
        self.assertEqual(self.write_ids(), [100])

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
        self.assertEqual(self.write_ids(), [99, 100])
        self.assertEqual(len(self.writes()), 2)

    async def test_write_missing_response_requires_per_file_confirmation(self):
        self.trash_code = 5066; self.apply_ids = {'99'}
        with self.assertRaises(dv.Transient):await self.delete()
        self.assertEqual(self.read_ids(), ['99', '100', '99', '100'])
        self.apply_ids = None
        await self.delete()
        self.assertEqual(self.write_ids(), [100])

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
        self.assertEqual(self.write_ids(), [100])

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
