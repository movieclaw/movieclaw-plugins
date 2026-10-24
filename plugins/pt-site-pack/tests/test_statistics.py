import json
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
from fastapi import FastAPI
from movieclaw_tracker.http import HttpClient
from movieclaw_tracker.selectors import NexusPHPSelectors

from pt_pack import apply
from pt_pack.adapters import PackNexusSite
from pt_pack.identity import with_identity
from pt_pack.overview import profile_view
from pt_pack.statistics import collection_report, preview_fields, validate_selectors
from .test_assistant import FakeContext

SAMPLE = '<h1>REDACTED</h1><div id="private-stats"><span id="my-bonus">25.5</span><span id="my-seeding">0</span><span id="my-upload">2 TiB</span></div>'
SELECTORS = {'profile_bonus_css': '#my-bonus::text', 'profile_seeding_css': '#my-seeding::text'}


class CollectionReportTests(unittest.TestCase):
    def report(self, account, **kw):
        return collection_report(profile_view(account), account, dict(task_count=0,uploading=0,downloading=0,upspeed_bytes=0,dlspeed_bytes=0), [], tasks_available=True, **kw)

    def test_missing_request_failure_and_old_values_are_distinct(self):
        account={'status':'active','enabled':True,'profile':{'bonus':7,'uploaded_bytes':0,'downloaded_bytes':0}}
        report=self.report(account);rows={r['field']:r for r in report['fields']}
        self.assertEqual(rows['bonus']['status'],'collected')
        self.assertEqual(rows['uploaded_bytes']['status'],'unrecognized')
        self.assertEqual(rows['hr']['status'],'no_tasks')
        failed=self.report({**account,'status':'failed','last_error':'Cookie=SECRET'})
        self.assertEqual(failed['fields'][0]['status'],'request_failed')
        self.assertEqual(failed['fields'][3]['value'],7)
        self.assertIn('失败',failed['attempt_message']);self.assertNotIn('SECRET',json.dumps(failed))
        self.assertEqual(self.report({})['fields'][0]['status'],'unconfigured')
        self.assertEqual(self.report({'status':'verifying'})['fields'][0]['status'],'pending')
        self.assertEqual(self.report({'enabled':False})['fields'][0]['status'],'paused')

    def test_partial_downloader_and_hr_coverage_are_not_complete(self):
        p=profile_view({})
        r=collection_report(p,{},dict(task_count=1,upspeed_bytes=None),[{'hit_and_run':None}],tasks_available=True,partial=True)
        rows={f['field']:f for f in r['fields']}
        self.assertEqual(rows['task_count']['status'],'partial');self.assertEqual(rows['hr']['status'],'partial')
        self.assertEqual(rows['hr']['value'],{'known':0,'total':1})
        r=collection_report(p,{}, {},[],tasks_available=False)
        self.assertEqual(r['fields'][-1]['status'],'unavailable')

    def test_preview_does_not_claim_label_fallback_is_selector_success(self):
        values=preview_fields(SAMPLE,SELECTORS)
        self.assertEqual(values['bonus'],25.5);self.assertEqual(values['seeding_count'],0)
        self.assertIsNone(values['uploaded_bytes'])
        self.assertTrue(all(v is None for v in preview_fields('<table><tr><td class="rowhead">积分</td><td>50</td></tr></table>',{}).values()))
        for bad in [{'login_path':'evil'}, {'profile_bonus_css':'[broken'}, {'profile_bonus_css':'Cookie=secret'}, {'profile_bonus_css':True}]:
            with self.assertRaises(ValueError): validate_selectors(bad)


class StatisticsRoutesTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder=tempfile.TemporaryDirectory();self.ctx=FakeContext(self.folder.name)
        await self.mount()
    async def mount(self):
        await apply.apply(self.ctx);app=FastAPI();app.include_router(self.ctx.routes)
        self.client=httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://testserver',headers={'Origin':'http://testserver'})
    async def asyncTearDown(self):
        await self.client.aclose();self.folder.cleanup()
    async def post(self,kind,**payload):
        config=(await self.client.get('/statistics/ourbits')).json()
        return await self.client.post('/statistics/ourbits/'+kind,json={'revision':config['revision'],**payload})

    async def test_read_preview_and_save_do_not_verify_or_call_model(self):
        before=(await self.client.get('/workspace')).json()
        self.assertNotIn('statistics_overrides',before)
        response=await self.post('preview',html=SAMPLE,selectors=SELECTORS)
        self.assertEqual(response.status_code,200,response.text);self.assertEqual(response.json()['sample_fields']['seeding_count'],0)
        self.assertEqual((await self.client.get('/workspace')).json(),before)
        saved=await self.post('save',html=SAMPLE,selectors=SELECTORS)
        self.assertEqual(saved.status_code,200,saved.text)
        after=(await self.client.get('/workspace')).json()
        for key in ['custom','user_agents','default_user_agent','selected','jobs']:self.assertEqual(before[key],after[key])
        self.assertEqual((await self.client.get('/statistics/ourbits')).json()['selectors'],SELECTORS)
        self.ctx.agent.ask.assert_not_awaited();self.ctx.ops.call.assert_not_awaited()
        self.assertNotIn('private-stats',json.dumps(self.ctx.values))
        # Reload from the persisted private workspace file.
        data=self.ctx.values;await self.client.aclose();self.ctx=FakeContext(self.folder.name,data=data);await self.mount()
        self.assertEqual((await self.client.get('/statistics/ourbits')).json()['selectors'],SELECTORS)
        self.assertEqual((await self.post('rollback')).status_code,200)
        self.assertEqual((await self.client.get('/statistics/ourbits')).json()['selectors'],{})

    async def test_invalid_samples_conflicts_origin_and_api_site_rejected(self):
        self.assertEqual((await self.client.post('/statistics/ourbits/save',headers={'Origin':'https://evil.example'},json={})).status_code,403)
        self.assertFalse((await self.client.get('/statistics/zhuque')).json()['supported'])
        self.assertEqual((await self.client.post('/statistics/zhuque/save',json={'selectors':SELECTORS})).status_code,409)
        self.assertEqual((await self.post('save',html=SAMPLE,selectors={'profile_bonus_css':'#not-there'})).status_code,400)
        self.assertEqual((await self.post('save',html=SAMPLE,selectors=SELECTORS,revision=-1)).status_code,409)
        self.assertEqual((await self.post('preview',html='x'*512001,selectors={})).status_code,400)
        self.assertEqual((await self.client.get('/statistics/no-such-site')).status_code,404)
        self.ctx.agent.ask.assert_not_awaited()

    async def test_model_requires_consent_is_bounded_and_never_applies_automatically(self):
        before=(await self.client.get('/workspace')).json()
        self.assertEqual((await self.post('draft',html=SAMPLE,selectors={},fields=['bonus'])).status_code,400)
        self.ctx.agent.ask.assert_not_awaited()
        self.ctx.agent.ask.return_value=SimpleNamespace(status='done',text=json.dumps({'profile_bonus_css':'#my-bonus::text'}))
        result=await self.post('draft',html=SAMPLE+'<input value="SECRET"/><p>Cookie=SECRET</p>',selectors={},fields=['bonus'],allow_model=True)
        self.assertEqual(result.status_code,200,result.text)
        self.assertEqual(result.json()['sample_fields']['bonus'],25.5)
        self.assertEqual((await self.client.get('/workspace')).json(),before)
        self.assertEqual(self.ctx.agent.ask.call_args.kwargs['tools'],'none')
        self.assertNotIn('SECRET',self.ctx.agent.ask.call_args.args[0])
        self.assertEqual((await self.post('draft',html=SAMPLE,selectors={},fields=['bonus'],allow_model=True)).status_code,429)


class ActualProfileOverrideTests(unittest.IsolatedAsyncioTestCase):
    async def test_saved_selector_callback_is_used_by_real_adapter_and_survives_missing_match(self):
        overrides=dict(SELECTORS);observer=AsyncMock();calls=[]
        def handler(request):
            calls.append(request.url.path)
            return httpx.Response(200,text='<a href="userdetails.php?id=9">fixture</a>' if request.url.path.endswith('index.php') else '<h1>fixture</h1>'+SAMPLE)
        cls=with_identity(PackNexusSite,lambda _:'',observe_profile=observer,statistics_selectors=lambda _:overrides)
        async with HttpClient(transport=httpx.MockTransport(handler)) as client:
            site=cls(site_id='fixture',base_url='https://example.com',client=client,auth_manager=None,selectors=NexusPHPSelectors().replace(profile_uid_css="a[href*='userdetails.php?id=']::attr(href)"))
            profile=await site.get_user_profile()
            self.assertEqual(profile.bonus,25.5);self.assertEqual(profile.seeding_count,0)
            self.assertEqual(observer.call_args.args[2]['seeding_count'],0)
            overrides['profile_bonus_css']='#not-there'
            profile=await site.get_user_profile()
            self.assertNotIn('bonus',observer.call_args.args[2])
        self.assertEqual(calls,['/index.php','/userdetails.php']*2)
