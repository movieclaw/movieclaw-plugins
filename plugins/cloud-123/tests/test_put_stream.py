"""Exercise the lazy request stream without MockTransport buffering it first."""
import asyncio
import hashlib
import unittest
from unittest.mock import patch

import httpx
from movieclaw_sdk import net
from cloud_123 import client


class PutStreamTests(unittest.IsolatedAsyncioTestCase):
    async def test_known_length_single_request_preserves_body_and_intent_order(self):
        data = bytes(range(256)) * 513
        digest = hashlib.md5(data).hexdigest()
        events = []
        closed = []

        class Transport(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request):
                self.requests = getattr(self, 'requests', 0) + 1
                assert events == ['intent']
                assert request.headers['content-length'] == str(len(data))
                assert 'transfer-encoding' not in request.headers
                assert request.extensions['timeout'] == {'connect': 5, 'read': 60, 'write': 20, 'pool': 20}
                chunks = [chunk async for chunk in request.stream]
                assert [len(chunk) for chunk in chunks] == [65536, 65536, 256]
                assert b''.join(chunks) == data
                return httpx.Response(200, headers={'etag': digest})

            async def aclose(self):
                closed.append(True)

        async def intent():
            events.append('intent')

        transport = Transport()
        with patch.object(client.net, 'http_transport', return_value=net._Paced(transport)):
            await client.put_part('https://upload.invalid/part', data, digest, before_send=intent)
        self.assertEqual(transport.requests, 1)
        self.assertEqual(closed, [True])

    async def test_cancel_during_body_closes_transport_without_resend(self):
        entered = asyncio.Event()
        chunks = []
        closed = []
        events = []

        class Transport(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request):
                self.requests = getattr(self, 'requests', 0) + 1
                async for chunk in request.stream:
                    chunks.append(len(chunk))
                    if len(chunks) == 2:
                        entered.set()
                        await asyncio.Event().wait()
                raise AssertionError('Cancelled stream must not complete')

            async def aclose(self):
                closed.append(True)

        async def intent():
            events.append('intent')

        transport = Transport()
        data = b'a' * (4 * 65536)
        with patch.object(client.net, 'http_transport', return_value=net._Paced(transport)), patch.object(client._logger, 'warning') as warning:
            task = asyncio.create_task(client.put_part('https://upload.invalid/part', data, hashlib.md5(data).hexdigest(), before_send=intent))
            await asyncio.wait_for(entered.wait(), 2)
            task.cancel('controlled cancellation')
            with self.assertRaises(asyncio.CancelledError):
                await task
            await asyncio.sleep(0)
            warning.assert_not_called()
        self.assertEqual(chunks, [65536, 65536])
        self.assertEqual(events, ['intent'])
        self.assertEqual(transport.requests, 1)
        self.assertEqual(closed, [True])
