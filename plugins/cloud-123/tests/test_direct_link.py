"""原文件取链；真实 SDK/P123Client，禁止真实网络，不访问媒体正文。"""
from __future__ import annotations

import asyncio
import json

import httpx
from movieclaw_sdk import drives as dv, net
from cloud_123.backend import Pan123
from tests.test_begin_upload import UploadFixture


class DirectLinkTest(UploadFixture):
    async def link(self, remote_id='99', user_agent='MovieClaw/Test (player)'):
        return await self.backend.direct_link(self.account, remote_id, user_agent)

    def downloads(self):
        return [r for r in self.requests if r.url.path.endswith('/download_info')]

    async def test_final_id_fresh_details_payload_ua_and_sdk_roundtrip(self):
        self.backend = Pan123()
        self.file.update(Size=str(64 * 1024**3), FileId='99', Etag='declared-not-trusted')
        result = await self.link('00099')
        self.assertEqual(result, dv.DirectLink('https://content.invalid/redirect', expires_in=60))
        self.assertEqual(dv.link_from(json.loads(json.dumps(dv.link_dict(result)))), result)
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(json.loads(self.requests[0].content), {'fileIdList': [{'FileId': '99'}]})
        request = self.downloads()[0]
        self.assertEqual((request.method, str(request.url)), ('POST', 'https://123pan.com/b/api/file/download_info'))
        payload = json.loads(request.content)
        for key, value in (('fileid', '99'), ('filename', 'movie.mkv'), ('size', str(64 * 1024**3)),
                           ('etag', 'declared-not-trusted'), ('s3keyflag', '12345-0')):
            self.assertEqual(payload[key], value)
        self.assertEqual(payload['type'], 0)
        self.assertEqual(request.headers.get_list('user-agent'), ['MovieClaw/Test (player)'])
        self.assertEqual(self.paced, 2)
        self.assertEqual(len(self.closed), 2)
        self.assertEqual(self.store.calls, [])
        self.assertFalse(self.body_entered.is_set())

    async def test_zero_size_and_empty_ua_are_preserved(self):
        self.file['Size'] = 0
        await self.link(user_agent='')
        self.assertEqual(json.loads(self.downloads()[0].content)['size'], 0)
        self.assertEqual(self.downloads()[0].headers['user-agent'], '')

    async def test_empty_s3_flag_is_sent_unchanged(self):
        self.file['S3KeyFlag'] = ''
        result = await self.link()
        self.assertEqual(result.url, 'https://content.invalid/redirect')
        payload = json.loads(self.downloads()[0].content)
        self.assertEqual(payload['s3keyflag'], '')
        self.assertEqual(payload['etag'], self.file['Etag'])
        self.assertEqual(payload['fileid'], 99)
        self.assertEqual(len(self.requests), 2)
        self.assertFalse(self.body_entered.is_set())

    async def test_invalid_id_and_unsafe_ua_stop_before_requests(self):
        for remote_id in ('', '0', '-1', '９９', '9'*21, 99, None):
            with self.subTest(remote_id=remote_id), self.assertRaises(dv.NotFound):await self.link(remote_id)
        for ua in (None, 1, '中文', 'agent\r\nAuthorization: private', 'a\x00b', 'a\tb', 'a\x7fb'):
            with self.subTest(ua=ua), self.assertRaises(dv.Conflict) as caught:await self.link(user_agent=ua)
            self.assertNotIn('private', str(caught.exception))
        self.assertEqual(self.requests, [])

    async def test_missing_and_trashed_do_not_request_download(self):
        for code, rows in ((0, []), (5066, []), (0, [{**self.file, 'Trashed': True}])):
            with self.subTest(code=code, rows=rows):
                async def reply(request):return httpx.Response(200, json={'code': code, 'data': {'infoList': rows}})
                self.reply = reply
                with self.assertRaises(dv.NotFound):await self.link()
        self.assertEqual(self.downloads(), [])

    async def test_wrong_id_directory_or_restricted_file_cannot_get_link(self):
        saved = dict(self.file)
        for field, value, error in (('FileId', 77, dv.Conflict), ('Type', 1, dv.Conflict),
                                    ('Status', 1, dv.Transient), ('PunishFlag', 1, dv.Transient),
                                    ('AbnormalAlert', 1, dv.Transient), ('Status', None, dv.ApiChanged)):
            with self.subTest(field=field):
                self.file = {**saved, field: value}
                with self.assertRaises(error):await self.link()
        self.assertEqual(self.downloads(), [])

    async def test_missing_protocol_fields_do_not_fall_back_to_hash_only_download(self):
        saved = dict(self.file)
        for field in ('FileId', 'FileName', 'Size', 'ParentFileId', 'Etag', 'S3KeyFlag'):
            for missing in (True, False):
                with self.subTest(field=field, missing=missing):
                    self.file = dict(saved)
                    if missing:del self.file[field]
                    else:self.file[field] = None
                    with self.assertRaises(dv.ApiChanged):await self.link()
        for field, values in (('Etag', ('', 0, False, [], {})), ('S3KeyFlag', (0, False, [], {}))):
            for value in values:
                with self.subTest(field=field, value=value):
                    self.file = {**saved, field: value}
                    with self.assertRaises(dv.ApiChanged):await self.link()
        self.assertEqual(self.downloads(), [])

    async def test_missing_or_invalid_download_url_is_redacted(self):
        for value in (None, '', 123, 'http://content.invalid/private', '//content.invalid/private',
                      'https://user:private@content.invalid/a', 'https://content.invalid:bad/private',
                      'https://content.invalid/private#fragment', 'https://content.invalid/a\nb'):
            with self.subTest(value=value):
                async def reply(request):
                    if request.url.path.endswith('/download_info'):
                        return httpx.Response(200, json={'code': 0, 'data': {'DownloadUrl': value}})
                self.reply = reply
                with self.assertRaises(dv.ApiChanged) as caught:await self.link()
                self.assertNotIn('private', str(caught.exception))
        self.assertTrue(all(r.url.host == '123pan.com' for r in self.requests))

    async def test_signed_url_is_returned_unchanged_without_probe_or_extra_headers(self):
        url = 'https://content.invalid/original?signature=a%2Bb&download=1'
        async def reply(request):
            if request.url.path.endswith('/download_info'):
                return httpx.Response(200, json={'code': 0, 'data': {'DownloadUrl': url}})
        self.reply = reply
        result = await self.link()
        self.assertEqual(dv.link_dict(result), {'url': url, 'expires_in': 60})
        self.assertEqual(len(self.requests), 2)

    async def test_calls_do_not_cache_details_or_share_user_agents(self):
        await asyncio.gather(self.link(user_agent='Player/A'), self.link(user_agent='Player/B'))
        self.assertEqual(sorted(r.headers['user-agent'] for r in self.downloads()), ['Player/A', 'Player/B'])
        self.assertEqual(self.count('/file/info'), 2)
        self.file['Trashed'] = True
        with self.assertRaises(dv.NotFound):await self.link()
        self.assertEqual(len(self.downloads()), 2)

    async def test_playback_ua_does_not_change_readback_requests(self):
        await self.link(user_agent='Player/unique')
        default_ua = self.requests[0].headers['user-agent']
        self.listed = True
        result = await self.begin()
        self.assertEqual(result.file.remote_id, '99')
        self.assertEqual(self.downloads()[-1].headers['user-agent'], default_ua)
        for request in self.requests:
            if request.url.host != '123pan.com':
                self.assertNotEqual(request.headers['user-agent'], 'Player/unique')
                self.assertNotIn('authorization', request.headers)
                self.assertNotIn('cookie', request.headers)

    async def test_download_errors_keep_classification_and_do_not_leak_response(self):
        for status, code, error in ((401, 0, dv.AuthExpired), (429, 0, dv.RateLimited),
                                    (403, 0, dv.Transient), (500, 0, dv.Transient),
                                    (200, 401, dv.AuthExpired), (200, 429, dv.RateLimited),
                                    (200, 5066, dv.NotFound), (200, 1, dv.Transient)):
            with self.subTest(status=status, code=code):
                async def reply(request):
                    if request.url.path.endswith('/download_info'):
                        return httpx.Response(status, json={'code': code, 'data': {}, 'message': 'private signed url'})
                self.reply = reply
                with self.assertRaises(error) as caught:await self.link()
                self.assertNotIn('private', str(caught.exception))
        self.assertEqual(len(self.requests), 16)

    async def test_timeout_and_auth_error_survive_close_failure(self):
        for error in (dv.Transient, dv.AuthExpired):
            with self.subTest(error=error):
                self.close_error = None
                async def reply(request):
                    if request.url.path.endswith('/download_info'):
                        self.close_error = RuntimeError('private close')
                        if error is dv.Transient:raise httpx.ReadTimeout('private url')
                        return httpx.Response(401)
                self.reply = reply
                with self.assertRaises(error) as caught:await self.link()
                self.assertNotIn('private', str(caught.exception))
                self.assertNotIn('关闭', str(caught.exception))
        self.assertEqual(len(self.closed), 4)

    async def test_sole_close_failure_and_new_cancellation_do_not_return_link(self):
        for failure, error in ((RuntimeError('private close'), dv.Transient),
                               (asyncio.CancelledError('new cancel'), asyncio.CancelledError)):
            with self.subTest(error=error):
                self.close_error = None
                async def reply(request):
                    if request.url.path.endswith('/download_info'):self.close_error = failure
                self.reply = reply
                with self.assertRaises(error) as caught:await self.link()
                self.assertNotIn('private', str(caught.exception))
        self.assertEqual(len(self.closed), 4)

    async def test_cancel_either_request_stops_followup_despite_close_failure(self):
        for endpoint in ('/file/info', '/download_info'):
            with self.subTest(endpoint=endpoint):
                self.close_error = None; entered = asyncio.Event()
                async def reply(request):
                    if request.url.path.endswith(endpoint):
                        entered.set(); await asyncio.Event().wait()
                self.reply = reply
                task = asyncio.create_task(self.link()); await entered.wait()
                before = len(self.requests), len(self.store.calls)
                self.close_error = RuntimeError('private close'); task.cancel('external')
                with self.assertRaises(asyncio.CancelledError) as caught:await task
                self.assertEqual(caught.exception.args, ('external',))
                self.assertEqual((len(self.requests), len(self.store.calls)), before)
        self.assertEqual(len(self.closed), 3)

    async def test_budget_rejection_and_cancellation_before_download_send(self):
        before = self.count('/file/info'); original = net.pacer.get()
        async def deny():
            await original()
            if self.count('/file/info') > before:raise dv.RateLimited('host budget')
        ctx = net.pacer.set(deny)
        try:
            with self.assertRaises(dv.RateLimited):await self.link()
        finally:net.pacer.reset(ctx)
        before = self.count('/file/info'); entered = asyncio.Event()
        async def wait():
            await original()
            if self.count('/file/info') > before:
                entered.set(); await asyncio.Event().wait()
        ctx = net.pacer.set(wait)
        try:
            task = asyncio.create_task(self.link()); await entered.wait(); task.cancel()
            with self.assertRaises(asyncio.CancelledError):await task
        finally:net.pacer.reset(ctx)
        self.assertEqual(self.count('/file/info'), 2)
        self.assertEqual(self.downloads(), [])
        self.assertEqual(len(self.closed), 4)

    async def test_missing_token_stops_before_request(self):
        self.account.credentials = {}
        with self.assertRaises(dv.AuthExpired):await self.link()
        self.assertEqual(self.requests, [])
