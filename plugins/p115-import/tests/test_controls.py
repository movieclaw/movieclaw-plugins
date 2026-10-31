"""取消链的正反例与交接竞态：仅内存假任务，不操作用户数据。"""
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import HTTPException
from movieclaw_sdk.jobs import JobCancelled, JobRef
from movieclaw_sdk.ops import OpsError

import p115board as board
import p115controls as controls
import p115requests as requests
import p115_import as plugin
from tests.test_flow import Store, Context, plan
from tests.test_board import job


class CancelControls(unittest.IsolatedAsyncioTestCase):
    def setup_case(self, states):
        store = Store()
        calls = []
        async def call(name, args):
            key = args['job_id']
            if name == 'jobs.show':
                return states[key]
            self.assertEqual(name, 'jobs.cancel')
            calls.append(key)
            state = states[key]
            changed = state['status'] not in ('succeeded', 'failed', 'cancelled')
            if changed:
                state['status'] = 'cancelling' if state['status'] == 'running' else 'cancelled'
            return {'cancelled': changed, 'job': state}
        return SimpleNamespace(call=call), store, calls

    async def test_queued_cancelled_immediately(self):
        state = job(status='queued')
        ops, store, calls = self.setup_case({state['id']: state})
        result = await controls.cancel_request(ops, store, state['id'], asyncio.Lock())
        self.assertEqual(result['status'], 'cancelled')
        self.assertEqual(calls, [state['id']])

    async def test_running_waits_for_safe_point(self):
        state = job(status='running')
        ops, store, calls = self.setup_case({state['id']: state})
        result = await controls.cancel_request(ops, store, state['id'], asyncio.Lock())
        self.assertEqual(result['status'], 'cancelling')
        self.assertIn('检查点保留', result['message'])

    async def test_dispatched_parent_cancels_actual_transfer(self):
        parent, child = job(status='succeeded'), job(board.TRANSFER, status='running')
        ops, store, calls = self.setup_case({r['id']: r for r in [parent, child]})
        await store.set('request-state:r1', {'transfer_job_id': child['id']})
        result = await controls.cancel_request(ops, store, parent['id'], asyncio.Lock())
        self.assertEqual(calls, [parent['id'], child['id']])
        self.assertEqual(result['status'], 'cancelling')
        self.assertNotIn('input:', str(result))

    async def test_result_fallback_finds_child(self):
        parent, child = job(status='succeeded'), job(board.TRANSFER, status='queued')
        parent['result']['transfer_job_id'] = child['id']
        ops, store, calls = self.setup_case({r['id']: r for r in [parent, child]})
        await controls.cancel_request(ops, store, parent['id'], asyncio.Lock())
        self.assertEqual(calls, [parent['id'], child['id']])

    async def test_foreign_job_rejected_before_write(self):
        state = job('library:scan', status='running')
        ops, store, calls = self.setup_case({state['id']: state})
        with self.assertRaises(HTTPException) as caught:
            await controls.cancel_request(ops, store, state['id'], asyncio.Lock())
        self.assertEqual(caught.exception.status_code, 400)
        self.assertEqual(calls, [])

    async def test_foreign_child_rejected_before_write(self):
        parent, child = job(status='succeeded'), job('library:scan', status='running')
        ops, store, calls = self.setup_case({r['id']: r for r in [parent, child]})
        await store.set('request-state:r1', {'transfer_job_id': child['id']})
        with self.assertRaises(HTTPException):
            await controls.cancel_request(ops, store, parent['id'], asyncio.Lock())
        self.assertEqual(calls, [])

    async def test_other_batch_child_rejected(self):
        parent, child = job(status='succeeded'), job(board.TRANSFER, ref='other')
        ops, store, calls = self.setup_case({r['id']: r for r in [parent, child]})
        await store.set('request-state:r1', {'transfer_job_id': child['id']})
        with self.assertRaises(HTTPException):
            await controls.cancel_request(ops, store, parent['id'], asyncio.Lock())
        self.assertEqual(calls, [])

    async def test_finished_is_noop(self):
        state = job(status='succeeded')
        ops, store, calls = self.setup_case({state['id']: state})
        result = await controls.cancel_request(ops, store, state['id'], asyncio.Lock())
        self.assertEqual(result['status'], 'noop')

    async def test_cancel_serializes_with_dispatch(self):
        state = job(status='running')
        ops, store, calls = self.setup_case({state['id']: state})
        lock = asyncio.Lock()
        async with lock:
            task = asyncio.create_task(controls.cancel_request(ops, store, state['id'], lock))
            await asyncio.sleep(0)
            self.assertEqual(calls, [])
        await task
        self.assertEqual(calls, [state['id']])

    async def test_show_error_translated(self):
        ops = SimpleNamespace(call=AsyncMock(side_effect=OpsError(404, 'NOT_FOUND', '任务不存在')))
        with self.assertRaises(HTTPException) as caught:
            await controls.cancel_request(ops, Store(), 'missing', asyncio.Lock())
        self.assertEqual(caught.exception.status_code, 404)


class DispatchCancellation(unittest.IsolatedAsyncioTestCase):
    def runner(self, store, builder, jobs=None, ops=None):
        self.jobs = jobs or SimpleNamespace(create=AsyncMock(return_value=JobRef('child', 'p115-import:transfer', 'queued', True)))
        self.ops = ops or SimpleNamespace(call=AsyncMock(return_value={'id':'child', 'status':'queued'}))
        ctx = SimpleNamespace(config=SimpleNamespace(dry_run=False))
        return requests.RequestRunner(ctx, store, self.jobs, self.ops, plan_body=plugin.PlanBody,
                                      build_plan=builder, push=AsyncMock())

    async def test_cancel_during_identification_never_dispatches(self):
        store, context = Store(), Context()
        await store.set('input:r', {'link': 'abc-1234'})
        async def build(*args):
            context.cancel = True
            return dict(plan(), status='ready')
        runner = self.runner(store, build)
        with self.assertRaises(JobCancelled):
            await runner.handle(context, {'request_ref':'r'})
        self.jobs.create.assert_not_awaited()

    async def test_cancel_at_dispatch_cancels_created_child(self):
        store, context = Store(), Context()
        await store.set('input:r', {'link': 'abc-1234'})
        async def create(*args, **kwargs):
            context.cancel = True
            return JobRef('child', 'p115-import:transfer', 'queued', True)
        jobs = SimpleNamespace(create=AsyncMock(side_effect=create))
        runner = self.runner(store, AsyncMock(return_value=dict(plan(), status='ready')), jobs=jobs)
        with self.assertRaises(JobCancelled):
            await runner.handle(context, {'request_ref':'r'})
        self.ops.call.assert_awaited_once_with('jobs.cancel', {'job_id':'child'})

    async def test_cancelled_child_never_auto_retried_with_old_failure_notice(self):
        store = Store()
        await store.set('input:r', {'link': 'abc-1234'})
        await store.set('request-state:r', {'plan': dict(plan(), status='ready'),
                                           'transfer_job_id':'child', 'notified_error':'old error'})
        ops = SimpleNamespace(call=AsyncMock(return_value={'id':'child', 'status':'cancelled'}))
        runner = self.runner(store, AsyncMock(), ops=ops)
        with self.assertRaises(JobCancelled):
            await runner.handle(Context(), {'request_ref':'r'})
        self.assertTrue(all(call.args[0] != 'jobs.retry' for call in ops.call.await_args_list))


class CancelledBoard(unittest.TestCase):
    def test_cancelled_retained_in_recent_and_not_attention(self):
        feed = board.build_feed([job(status='cancelled')])
        self.assertEqual(feed['attention'], [])
        self.assertEqual(feed['recent'][0]['status'], 'cancelled')

    def test_old_failure_not_nagged_after_cancellation(self):
        old = job(status='failed', updated='2026-10-20T06:00:00+00:00')
        new = job(status='cancelled', updated='2026-10-20T07:00:00+00:00')
        new['id'] = 'newer'
        feed = board.build_feed([old, new])
        self.assertEqual(feed['attention'], [])

    def test_library_id_kept_for_detail_link(self):
        row = board.row_of(job(status='succeeded', result={'library_id':12, 'media_item_id':34}))
        self.assertEqual(row['library_id'], 12)
