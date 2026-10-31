"""文件元数据查询；真实 SDK/P123Client，复用禁止联网的内存夹具。"""
from __future__ import annotations

import asyncio
import json

import httpx
from movieclaw_sdk import drives as dv, net
from cloud_123.backend import Pan123
from tests.test_begin_upload import UploadFixture


class StatTest(UploadFixture):
    async def stat(self, remote_id='99'):
        return await self.backend.stat(self.account, remote_id)

    async def test_final_id_request_and_sdk_roundtrip_without_store_or_readback(self):
        self.backend = Pan123()
        del self.file['Etag']; del self.file['S3KeyFlag']
        result = await self.stat('00099')
        self.assertEqual(result, dv.RemoteFile('99', 'movie.mkv', len(self.body), parent_id='1', sha1=None))
        self.assertEqual(dv.remote_file_from(json.loads(json.dumps(dv.remote_file_dict(result)))), result)
        self.assertEqual(len(self.requests), 1)
        request = self.requests[0]
        self.assertEqual((request.method, str(request.url)), ('POST', 'https://123pan.com/b/api/file/info'))
        self.assertEqual(json.loads(request.content), {'fileIdList': [{'FileId': '99'}]})
        self.assertEqual(self.paced, 1)
        self.assertEqual(self.store.calls, [])
        self.assertEqual(self.closed, [True])

    async def test_zero_and_large_sizes_ignore_declared_hash(self):
        for size in (0, '0', 64 * 1024**3, str(64 * 1024**3)):
            with self.subTest(size=size):
                self.file.update(Size=size, ParentFileId='0', FileId='99', Etag='not-a-hash', Sha1='A'*40)
                result = await self.stat()
                self.assertEqual(result.size, int(size))
                self.assertEqual(result.parent_id, '0')
                self.assertIsNone(result.sha1)
        self.assertEqual(self.count('/download_info'), 0)

    async def test_invalid_input_is_notfound_before_request_and_roundtrips(self):
        for value in ('', '0', '000', '-1', '+99', ' 99', '９９', '9'*21, 99, True, None):
            with self.subTest(value=value), self.assertRaises(dv.NotFound) as caught:
                await self.stat(value)
            restored = dv.error_from(json.loads(json.dumps(dv.error_dict(caught.exception))))
            self.assertIsInstance(restored, dv.NotFound)
            self.assertEqual(str(restored), str(caught.exception))
        self.assertEqual(self.requests, [])

    async def test_empty_details_and_explicit_missing_are_notfound(self):
        for code, data in ((0, {'infoList': []}), (5066, {})):
            with self.subTest(code=code):
                async def reply(request):
                    return httpx.Response(200, json={'code': code, 'data': data, 'message': 'private'})
                self.reply = reply
                with self.assertRaises(dv.NotFound) as caught:await self.stat()
                self.assertNotIn('private', str(caught.exception))
                self.assertIsInstance(dv.error_from(dv.error_dict(caught.exception)), dv.NotFound)

    async def test_trashed_file_is_notfound_even_with_restricted_status(self):
        for value in (True, 1):
            self.file.update(Trashed=value, Status=1)
            with self.assertRaises(dv.NotFound):await self.stat()

    async def test_wrong_id_and_directory_are_not_treated_as_missing(self):
        for field, value in (('FileId', 77), ('Type', 1)):
            with self.subTest(field=field):
                saved = self.file[field]; self.file[field] = value
                with self.assertRaises(dv.Conflict):await self.stat()
                self.file[field] = saved

    async def test_restricted_or_processing_status_is_transient(self):
        for field in ('Status', 'PunishFlag', 'AbnormalAlert'):
            with self.subTest(field=field):
                self.file[field] = 1
                with self.assertRaises(dv.Transient):await self.stat()
                self.file[field] = 0

    async def test_malformed_detail_envelope_never_means_missing(self):
        for data in ({}, {'infoList': None}, {'infoList': {}}, {'infoList': [None]},
                     {'infoList': [self.file, self.file]}):
            with self.subTest(data=data):
                async def reply(request):return httpx.Response(200, json={'code': 0, 'data': data})
                self.reply = reply
                with self.assertRaises(dv.ApiChanged):await self.stat()

    async def test_invalid_required_fields_are_protocol_errors(self):
        fields = {
            'FileId': [None, False, 0, -1, 'bad'],
            'ParentFileId': [None, True, -1, 'bad'],
            'FileName': [None, '', '.', '..', 'a/b', 'a\\b', 'a\x00b', 1],
            'Size': [None, True, -1, 1.5, 'bad'],
            'Type': [None, False, '0', 2],
            'Trashed': [None, '0', 2],
            'Status': [None, False, '0'],
            'PunishFlag': [None, False, '0'],
            'AbnormalAlert': [None, False, '0'],
        }
        saved = dict(self.file)
        for field, values in fields.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    self.file = {**saved, field: value}
                    with self.assertRaises(dv.ApiChanged):await self.stat()
            self.file = dict(saved); del self.file[field]
            with self.subTest(missing=field), self.assertRaises(dv.ApiChanged):await self.stat()

    async def test_http_and_business_errors_keep_classification_and_redaction(self):
        for status, code, error in ((401, 0, dv.AuthExpired), (429, 0, dv.RateLimited),
                                   (404, 0, dv.Transient), (500, 0, dv.Transient),
                                   (200, 401, dv.AuthExpired), (200, 429, dv.RateLimited),
                                   (200, 1, dv.Transient)):
            with self.subTest(status=status, code=code):
                async def reply(request):
                    return httpx.Response(status, json={'code': code, 'data': {}, 'message': 'private'})
                self.reply = reply
                with self.assertRaises(error) as caught:await self.stat()
                self.assertNotIn('private', str(caught.exception))
        self.assertEqual(len(self.requests), 7)

    async def test_network_failure_survives_close_failure(self):
        async def reply(request):raise httpx.ReadTimeout('private token')
        self.reply = reply; self.close_error = RuntimeError('private close')
        with self.assertRaises(dv.Transient) as caught:await self.stat()
        self.assertNotIn('private', str(caught.exception))
        self.assertNotIn('关闭', str(caught.exception))
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.closed, [True])

    async def test_business_error_survives_close_failure(self):
        async def reply(request):return httpx.Response(401)
        self.reply = reply; self.close_error = RuntimeError('private close')
        with self.assertRaises(dv.AuthExpired):await self.stat()
        self.assertEqual(self.closed, [True])

    async def test_sole_close_failure_is_transient_and_new_cancellation_propagates(self):
        self.close_error = RuntimeError('private close')
        with self.assertRaises(dv.Transient) as caught:await self.stat()
        self.assertNotIn('private', str(caught.exception))
        self.close_error = asyncio.CancelledError('close cancellation')
        with self.assertRaises(asyncio.CancelledError) as caught:await self.stat()
        self.assertEqual(caught.exception.args, ('close cancellation',))

    async def test_cancel_request_has_no_followup_and_close_does_not_mask_it(self):
        entered = asyncio.Event()
        async def reply(request):
            entered.set(); await asyncio.Event().wait()
        self.reply = reply; self.close_error = RuntimeError('private close')
        task = asyncio.create_task(self.stat()); await entered.wait(); task.cancel('external')
        with self.assertRaises(asyncio.CancelledError) as caught:await task
        self.assertEqual(caught.exception.args, ('external',))
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.closed, [True])
        self.assertEqual(self.store.calls, [])

    async def test_budget_rejection_and_wait_cancellation_never_send(self):
        async def deny():raise dv.RateLimited('host budget')
        ctx = net.pacer.set(deny)
        try:
            with self.assertRaises(dv.RateLimited):await self.stat()
        finally:net.pacer.reset(ctx)
        entered = asyncio.Event()
        async def wait():
            entered.set(); await asyncio.Event().wait()
        ctx = net.pacer.set(wait)
        try:
            task = asyncio.create_task(self.stat()); await entered.wait(); task.cancel()
            with self.assertRaises(asyncio.CancelledError):await task
        finally:net.pacer.reset(ctx)
        self.assertEqual(self.requests, [])
        self.assertEqual(len(self.closed), 2)

    async def test_missing_token_stops_before_request(self):
        self.account.credentials = {}
        with self.assertRaises(dv.AuthExpired):await self.stat()
        self.assertEqual(self.requests, [])
