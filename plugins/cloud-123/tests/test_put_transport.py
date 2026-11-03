"""Real HTTPX/httpcore regression tests against a loopback HTTP server only."""
import asyncio
import hashlib
import socket
import unittest
from unittest.mock import patch

import httpx
from movieclaw_sdk import drives as dv, net

from cloud_123 import client


class PutTransportTests(unittest.IsolatedAsyncioTestCase):
    async def run_put(self, *, slow=False, stalled=False, ack_delay=0):
        data = bytes(range(256)) * (16384 if slow or stalled else 1024)
        digest = hashlib.md5(data).hexdigest()
        observed = {"requests": 0, "received": 0, "intent": 0}
        handlers = []

        async def accept(reader, writer):
            handlers.append(asyncio.current_task())
            try:
                header = await reader.readuntil(b"\r\n\r\n")
                observed["requests"] += 1
                observed["intent_at_request"] = observed["intent"]
                fields = dict(line.split(b":", 1) for line in header.split(b"\r\n")[1:] if b":" in line)
                observed["headers"] = {key.decode().lower(): value.decode().strip() for key, value in fields.items()}
                if stalled:
                    await asyncio.Event().wait()
                md5 = hashlib.md5()
                while observed["received"] < len(data):
                    block = await reader.read(min(65536, len(data) - observed["received"]))
                    if not block:
                        return
                    observed["received"] += len(block)
                    md5.update(block)
                    if slow:
                        await asyncio.sleep(0.02)
                observed["digest"] = md5.hexdigest()
                await asyncio.sleep(ack_delay)
                writer.write((f'HTTP/1.1 200 OK\r\nContent-Length: 0\r\n'
                              f'ETag: "{digest}"\r\nConnection: close\r\n\r\n').encode())
                await writer.drain()
            except (ConnectionError, asyncio.IncompleteReadError):
                pass  # A timeout on the client side closes the connection.
            finally:
                writer.close()
                try:
                    await writer.wait_closed()
                except ConnectionError:
                    pass

        server = await asyncio.start_server(accept, "127.0.0.1", 0, limit=65536)
        port = server.sockets[0].getsockname()[1]
        real_client = httpx.AsyncClient

        def make_client(**kwargs):
            timeout = kwargs["timeout"]
            self.assertEqual((timeout.connect, timeout.write, timeout.pool), (5, 20, 20))
            # Scale only the test's wall time. A read=20 regression gets 1 s;
            # read=60 gets 3 s, with a 2 s response delay between those limits.
            kwargs["timeout"] = httpx.Timeout(0.5, connect=2, read=timeout.read / 20)
            return real_client(**kwargs)

        def transport(_service):
            return net._Paced(httpx.AsyncHTTPTransport(
                retries=0, socket_options=[(socket.SOL_SOCKET, socket.SO_SNDBUF, 16384)]))

        async def prepared():
            observed["intent"] += 1

        try:
            with patch.object(client.net, "http_transport", side_effect=transport), \
                    patch.object(client.httpx, "AsyncClient", side_effect=make_client), \
                    patch.object(client._logger, "warning") as diagnostic:
                try:
                    await asyncio.wait_for(client.put_part(
                        f"http://127.0.0.1:{port}/part", data, digest, before_send=prepared), 12)
                except dv.Transient:
                    observed["error"] = diagnostic.call_args.args[1] if diagnostic.called else "unclassified"
        finally:
            server.close()
            for task in handlers:
                task.cancel()
            results = await asyncio.gather(*handlers, return_exceptions=True)
            await server.wait_closed()
            for result in results:
                if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
                    raise result

        self.assertEqual(observed["requests"], 1)
        self.assertEqual(observed["intent"], 1)
        self.assertEqual(observed["intent_at_request"], 1)
        self.assertEqual(observed["headers"]["content-length"], str(len(data)))
        self.assertNotIn("transfer-encoding", observed["headers"])
        if not stalled:
            self.assertEqual(observed["received"], len(data))
            self.assertEqual(observed["digest"], digest)
        return observed

    async def test_slow_reader_completes_without_whole_part_write_timeout(self):
        self.assertNotIn("error", await self.run_put(slow=True))

    async def test_stalled_reader_still_times_out_without_retry(self):
        self.assertEqual((await self.run_put(stalled=True))["error"], "WriteTimeout")

    async def test_delayed_ack_within_read_wait_completes(self):
        self.assertNotIn("error", await self.run_put(ack_delay=2))

    async def test_ack_beyond_read_wait_still_times_out_without_retry(self):
        self.assertEqual((await self.run_put(ack_delay=6))["error"], "ReadTimeout")
