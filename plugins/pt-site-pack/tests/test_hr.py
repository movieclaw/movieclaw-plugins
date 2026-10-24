import copy
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from datetime import datetime,timezone

import httpx
from movieclaw_tracker.http import HttpClient
from movieclaw_tracker.models import SearchQuery
from movieclaw_tracker.selectors import NexusPHPSelectors
from movieclaw_tracker.sites.custom.mteam import MTeamSite
from pt_pack.hr import Capture, target_url, torrent_id, issue, take, TICKETS, REPORTS
from pt_pack.identity import with_identity
from pt_pack.overview import HREvidence,task_view


class HRParsing(unittest.TestCase):
    def test_known_mteam_legacy_origin_is_canonicalized(self):
        site={'site_id':'mteam','website_url':'https://kp.m-team.cc','base_url':'https://api.m-team.cc'}
        self.assertEqual(target_url(site,'https://tp.m-team.cc/detail/9'),'https://kp.m-team.cc/detail/9')
        self.assertIsNone(target_url({**site,'site_id':'another'},'https://tp.m-team.cc/detail/9'))

    def response(self,path,payload):
        return httpx.Response(200,request=httpx.Request('GET','https://example.org'+path),json=payload)

    def test_only_explicit_api_flag_is_evidence(self):
        c=Capture()
        c.response(self.response('/api/torrent/search',{'code':'0','data':{'data':[{'id':1,'status':{'hr':False}},{'id':2,'status':{'hitAndRun':True}},{'id':3,'status':{'seeders':5}}]}}))
        self.assertEqual(c.values['1'],(False,'site-api'))
        self.assertEqual(c.values['2'],(True,'site-api'))
        self.assertNotIn('3',c.values)
        c.response(self.response('/api/user/getInfo',{'status':200,'data':{'id':4,'hr':False}}))
        self.assertNotIn('4',c.values)

    def test_failure_and_conflicts_do_not_claim_exemption(self):
        c=Capture()
        c.response(self.response('/api/torrent/detail',{'code':'401','data':{'id':1,'hr':False}}))
        self.assertEqual(c.values,{})
        c.record('1',True,'site-api');c.record('1',False,'detail-label')
        self.assertIsNone(c.values['1'][0])
        c.record('1',False,'site-api');self.assertIsNone(c.values['1'][0])

    def test_detail_markers_and_user_ledger_are_separate(self):
        def run(html):
            c=Capture(expected='9')
            c.response(httpx.Response(200,request=httpx.Request('GET','https://example.org/details.php?id=9'),text=html))
            return c.values.get('9',(None,None))[0]
        self.assertFalse(run('<table><tr><td class="rowhead">H&amp;R</td><td>无考核</td></tr></table>'))
        self.assertTrue(run('<table><tr><td class="rowhead">H&amp;R</td><td>需要考核</td></tr></table>'))
        self.assertIsNone(run('<div id="info_block">H&amp;R: 当前未完成 0</div>'))
        self.assertIsNone(run('<div>无 H&amp;R 标记</div>'))

    def test_same_origin_details_only_and_credential_query_is_removed(self):
        site={'website_url':'https://example.org','base_url':'https://api.example.org'}
        self.assertEqual(target_url(site,'https://example.org/details.php?id=9&passkey=SECRET#x'),'https://example.org/details.php?id=9')
        self.assertEqual(torrent_id('https://example.org/torrents.php?id=2&torrentid=9'),'9')
        for value in ['https://evil.org/details.php?id=9','http://example.org/details.php?id=9','https://example.org/download.php?id=9','https://u:p@example.org/details.php?id=9','https://example.org:8080/details.php?id=9']:
            self.assertIsNone(target_url(site,value))

    def test_ticket_scope_expiry_and_one_time_consumption(self):
        token,marker=issue('site','https://example.org/details.php?id=9')
        self.assertEqual(take(marker,'site')[1]['url'],'https://example.org/details.php?id=9')
        with self.assertRaises(Exception):take(marker,'site')
        token,marker=issue('site','https://example.org/details.php?id=9')
        TICKETS[token]['expires']=time.monotonic()-1
        with self.assertRaises(Exception):take(marker,'site')


class HRFlow(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_mteam_task_reads_canonical_hr_evidence(self):
        e=HREvidence(None)
        e.values={'mteam:9':{'value':True,'source':'site-api','observed_at':datetime.now(timezone.utc).isoformat()}}
        site={'site_id':'mteam','key':'opaque','website_url':'https://kp.m-team.cc','base_url':'https://api.m-team.cc'}
        row=task_view({'site_id':'mteam','page_url':'https://tp.m-team.cc/detail/9'},0,{'mteam':site},e)
        self.assertIs(row['hit_and_run'],True)

    async def test_ticket_reads_one_detail_without_search_or_download(self):
        observed=AsyncMock();calls=[]
        def handler(request):
            calls.append(request.url.path)
            self.assertEqual(request.url.path,'/api/torrent/detail')
            return httpx.Response(200,json={'code':'0','message':'SUCCESS','data':{'id':9,'name':'Fixture','size':'1024','status':{'hr':False}}})
        cls=with_identity(MTeamSite,lambda _: '',observed)
        async with HttpClient(transport=httpx.MockTransport(handler)) as client:
            site=cls(site_id='fixture',base_url='https://example.org',client=client,auth_manager=None)
            token,marker=issue('fixture','https://example.org/detail/9')
            result=await site.search(SearchQuery(keyword=marker))
        self.assertEqual(result.items,[])
        self.assertEqual(calls,['/api/torrent/detail'])
        self.assertFalse(REPORTS.pop(token)['value'])
        self.assertFalse(observed.call_args.args[1][0].hit_and_run)
        self.assertEqual(observed.call_args.args[1][0].hr_source,'site-api')

    async def test_old_negative_without_explicit_source_is_ignored(self):
        values={}
        store=SimpleNamespace(get=AsyncMock(side_effect=values.get),set=AsyncMock(side_effect=lambda k,v,**kw: values.__setitem__(k,copy.deepcopy(v))))
        evidence=HREvidence(store);await evidence.start()
        await evidence.observe('fixture',[SimpleNamespace(torrent_id='9',hit_and_run=False)])
        self.assertIsNone(evidence.lookup('fixture','https://example.org/details.php?id=9','https://example.org'))
        await evidence.observe('fixture',[SimpleNamespace(torrent_id='9',hit_and_run=False,hr_source='detail-label')])
        evidence.values['fixture:9'].pop('source')
        self.assertIsNone(evidence.lookup('fixture','https://example.org/details.php?id=9','https://example.org'))
