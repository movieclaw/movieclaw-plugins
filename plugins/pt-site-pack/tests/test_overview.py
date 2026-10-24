import asyncio
import copy
import json
import unittest
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
from fastapi import FastAPI
from pt_pack.overview import Overview, HREvidence, overview_router, profile_view, number

STAMP = datetime.now(timezone.utc).isoformat()
SITE = dict(site_id='examplept', name='Example PT', source='official', source_label='官方支持', configured=True,
            usable=True, verification='active', verification_label='上次验证通过', icon_url=None,
            website_url='https://example.com', base_url='https://example.com')
PROFILE = dict(username='private-owner', user_class='private-class', uploaded_bytes=1024, downloaded_bytes=512,
               ratio=2, bonus=300, seeding_count=3, leeching_count=2, fetched_at=STAMP)
TASK = dict(id='private-downloader:secret-hash', info_hash='secret-hash', name='Fixture Movie', downloader_id=1,
            downloader_name='private-NAS', site_id='examplept', state='downloading', progress=.5, size_bytes=1024,
            upspeed_bytes=10, dlspeed_bytes=20, uploaded_bytes=50, page_url='https://example.com/details.php?id=9',
            plan={'dest_path':'/private/path'}, cookie='SECRET-COOKIE', save_path='/private/path')


class Ops:
    def __init__(self):
        self.calls=[];self.fail_tasks=False;self.fail_profiles=False
        self.tasks={'items':[TASK,copy.deepcopy(TASK),{**TASK,'info_hash':'external','site_id':None,'state':'paused','upspeed_bytes':0,'dlspeed_bytes':0}],
                    'sources':[dict(status='active', name='private-NAS', task_count=2)]}
    async def call(self, name, args):
        self.calls.append(name)
        if name=='site.list':
            if self.fail_profiles: raise RuntimeError('credentials MUST NOT LEAK')
            return [dict(site_id='examplept', profile=PROFILE)]
        if name=='dl.tasks':
            if self.fail_tasks: raise RuntimeError('credentials MUST NOT LEAK')
            return self.tasks
        if name=='site.verify': return {'status':'verifying'}
        if name=='search.torrents':
            from pt_pack.hr import take,REPORTS
            token,ticket=take(args['keyword'],'examplept')
            REPORTS[token]={'value':False,'source':'site-api'}
            return {'items':[]}
        raise AssertionError(name)


class OverviewTests(unittest.IsolatedAsyncioTestCase):
    async def test_hr_check_requires_known_origin_and_cleans_ticket(self):
        from pt_pack.hr import TICKETS,REPORTS
        tasks=(await self.client.get('/overview/tasks')).json()['items']
        known=next(t for t in tasks if t['site_id'])
        unknown=next(t for t in tasks if not t['site_id'])
        self.assertTrue(known['can_check_hr']);self.assertFalse(unknown['can_check_hr'])
        headers={'Origin':'http://testserver'}
        blocked=await self.client.post('/overview/hr-check',params={'task_key':known['key']},headers={'Origin':'https://evil.example'})
        self.assertEqual(blocked.status_code,403)
        self.assertEqual((await self.client.post('/overview/hr-check',params={'task_key':unknown['key']},headers=headers)).status_code,409)
        result=await self.client.post('/overview/hr-check',params={'task_key':known['key']},headers=headers)
        self.assertEqual(result.status_code,200);self.assertIs(result.json()['hit_and_run'],False)
        self.assertEqual(self.ops.calls.count('search.torrents'),1)
        self.assertEqual((await self.client.post('/overview/hr-check',params={'task_key':known['key']},headers=headers)).status_code,429)

    async def test_task_key_stays_bound_when_task_order_changes(self):
        a=await self.service.read();keys={r['name']+str(r['site_id']):r['key'] for r in a['tasks']}
        self.ops.tasks['items']=list(reversed(self.ops.tasks['items']));self.service.expiry=0
        b=await self.service.read()
        self.assertEqual(keys,{r['name']+str(r['site_id']):r['key'] for r in b['tasks']})

    async def test_collect_uses_opaque_key_checks_origin_and_limits_repeat(self):
        data=(await self.client.get('/overview')).json()
        key=data['sites'][0]['key']
        blocked=await self.client.post('/overview/collect', params={'site_key':key}, headers={'Origin':'https://evil.example'})
        self.assertEqual(blocked.status_code,403)
        headers={'Origin':'http://testserver'}
        missing=await self.client.post('/overview/collect?site_key=not-a-site',headers=headers)
        self.assertEqual(missing.status_code,404)
        accepted=await self.client.post('/overview/collect',params={'site_key':key},headers=headers)
        self.assertEqual(accepted.status_code,200)
        self.assertTrue(accepted.json()['accepted'])
        self.assertEqual(self.ops.calls.count('site.verify'),1)
        self.assertEqual((await self.client.post('/overview/collect',params={'site_key':key},headers=headers)).status_code,429)

    async def asyncSetUp(self):
        self.ops=Ops();self.values={}
        store=SimpleNamespace(get=AsyncMock(side_effect=self.values.get),set=AsyncMock(side_effect=lambda k,v,**kw:self.values.__setitem__(k,copy.deepcopy(v))))
        self.evidence=HREvidence(store);await self.evidence.start()
        self.ctx=SimpleNamespace(use=lambda key:SimpleNamespace(client=AsyncMock(return_value=self.ops)))
        self.snapshot=AsyncMock(return_value={'sites':[SITE]})
        self.service=Overview(self.ctx,self.snapshot,self.evidence)
        app=FastAPI();app.include_router(overview_router(self.ctx,self.snapshot,self.evidence))
        self.client=httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://testserver')
    async def asyncTearDown(self): await self.client.aclose()

    async def test_default_privacy_and_safe_full_projection(self):
        for private in [True,False]:
            response=await self.client.get('/overview',params={'privacy':str(private).lower()})
            self.assertEqual(response.status_code,200,response.text)
            self.assertEqual('private-owner' in response.text,not private)
            self.assertNotIn('tasks',response.json())
            tasks=await self.client.get('/overview/tasks',params={'privacy':str(private).lower()})
            self.assertEqual('private-NAS' in tasks.text,not private)
            for secret in ['SECRET-COOKIE','secret-hash','private/path','page_url','dest_path','save_path']:
                self.assertNotIn(secret,tasks.text)
        data=(await self.client.get('/overview?hide_sites=true')).json()
        self.assertEqual(data['sites'][0]['name'],'站点 01');self.assertNotIn('examplept',json.dumps(data))
        hidden=(await self.client.get('/overview/tasks?hide_sites=true&hide_names=true&q=Fixture')).json()
        self.assertEqual(hidden['total'],0) # 隐藏名称后不能通过搜索侧漏原始种子名。

    async def test_totals_deduplicate_and_isolate_unassigned_tasks(self):
        data=await self.service.read()
        self.assertEqual(data['summary']['task_count'],2)
        self.assertEqual(data['summary']['upspeed_bytes'],10)
        self.assertEqual(data['summary']['unknown_tasks'],1)
        self.assertEqual(data['sites'][0]['live']['task_count'],1)
        self.assertEqual(data['summary']['uploaded_bytes'],1024)
        self.assertEqual(data['summary']['hr_unknown'],2)
        await asyncio.gather(*[self.service.read() for _ in range(8)])
        self.assertEqual(self.ops.calls.count('dl.tasks'),1)

    async def test_hr_three_states_persist_and_reject_wrong_domain(self):
        await self.evidence.observe('examplept',[SimpleNamespace(torrent_id='9',hit_and_run=True),SimpleNamespace(torrent_id='10',hit_and_run=False,hr_source='site-api'),SimpleNamespace(torrent_id='11',hit_and_run=None)])
        self.assertEqual(self.evidence.lookup('examplept','https://example.com/details.php?id=9','https://example.com')['value'],True)
        self.assertEqual(self.evidence.lookup('examplept','https://example.com/details.php?id=10','https://example.com')['value'],False)
        self.assertIsNone(self.evidence.lookup('examplept','https://evil.com/details.php?id=9','https://example.com'))
        self.assertIsNone(self.evidence.lookup('examplept','https://example.com/details.php?id=11','https://example.com'))
        data=await self.service.read();self.assertTrue(data['tasks'][0]['hit_and_run'])
        again=HREvidence(self.evidence.data);await again.start();self.assertEqual(again.values,self.evidence.values)
        again.values['examplept:9']['observed_at']=(datetime.now(timezone.utc)-timedelta(days=8)).isoformat()
        self.assertIsNone(again.lookup('examplept','https://example.com/details.php?id=9','https://example.com'))

    async def test_pagination_filtering_and_opaque_site_key(self):
        self.ops.tasks['items']=[{**TASK,'info_hash':str(i),'name':'Movie '+str(i),'hit_and_run':bool(i%2)} for i in range(55)]
        data=(await self.client.get('/overview')).json();key=data['sites'][0]['key']
        page=(await self.client.get('/overview/tasks',params={'site_id':key,'page':2})).json()
        self.assertEqual((page['total'],page['pages'],len(page['items'])),(55,3,20))
        hr=(await self.client.get('/overview/tasks?state=hr')).json()
        self.assertEqual(hr['total'],27)
        missing=(await self.client.get('/overview/tasks?site_id=does-not-exist')).json()
        self.assertEqual(missing['total'],0)
        for query in ['page=0','page_size=51','state=bad']:
            self.assertIn((await self.client.get('/overview/tasks?'+query)).status_code,[400,422])

    async def test_downloader_and_profile_failure_keep_last_snapshot(self):
        first=copy.deepcopy(await self.service.read());self.service.expiry=0;self.ops.fail_tasks=True
        failed=await self.service.read()
        self.assertTrue(failed['stale']);self.assertEqual(failed['tasks'],first['tasks'])
        self.assertEqual(failed['tasks_updated_at'],first['tasks_updated_at'])
        self.assertIsNone(failed['summary']['upspeed_bytes'])
        self.service.expiry=0;self.ops.fail_profiles=True
        data=await self.service.read();self.assertTrue(data['stale']);self.assertNotIn('MUST NOT LEAK',str(data))

    async def test_no_downloaders_is_not_zero_bandwidth(self):
        self.ops.tasks={'items':[],'sources':[]}
        data=await self.service.read();self.assertFalse(data['tasks_available']);self.assertIsNone(data['summary']['upspeed_bytes'])
        self.assertIsNone(data['sites'][0]['live']['downloading'])


class ProfileTests(unittest.TestCase):
    def test_zero_defaults_and_invalid_numbers_are_not_claimed_as_actual_statistics(self):
        for value in [True,-1,float('nan'),float('inf'),'100']:
            self.assertIsNone(number(value))
        data=profile_view({'profile':{'uploaded_bytes':0,'downloaded_bytes':0,'ratio':None,'seeding_count':0,'leeching_count':0}})
        self.assertIsNone(data['uploaded_bytes']);self.assertIsNone(data['seeding_count']);self.assertTrue(data['stale'])
        valid=profile_view({'profile':PROFILE});self.assertEqual(valid['ratio'],2);self.assertFalse(valid['stale'])
