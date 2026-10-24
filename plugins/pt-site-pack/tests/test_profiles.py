import asyncio
import copy
import json
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
from movieclaw_tracker.http import HttpClient
from movieclaw_tracker.models import UserProfile
from movieclaw_tracker.selectors import NexusPHPSelectors
from movieclaw_tracker.sites.custom.mteam import MTeamSite
from pt_pack.adapters import PackNexusSite, GreatPosterWallSite
from pt_pack.identity import with_identity
from pt_pack.overview import profile_view
from pt_pack.profiles import extract, enrich, ProfileEvidence, parse_value, api_fields


class ProfileParsing(unittest.TestCase):
    def test_faq_or_homepage_statistics_are_not_personal_profile_rows(self):
        profile=UserProfile(user_id='9',username='fixture')
        html='<h1>常见问题</h1><table><tr><td class="rowhead">做种数</td><td>1608941</td></tr></table>'
        _,fields,_=enrich(profile,[('/userdetails.php',html)],NexusPHPSelectors())
        self.assertNotIn('seeding_count',fields)
        _,fields,_=enrich(profile,[('/userdetails.php?id=9',html)],NexusPHPSelectors())
        self.assertEqual(fields['seeding_count'],1608941)
        _,fields,_=enrich(profile,[('/userdetails.php?id=8',html.replace('常见问题','fixture'))],NexusPHPSelectors())
        self.assertNotIn('seeding_count',fields)

    def test_audiences_spendable_points_do_not_use_seeding_points(self):
        html='''<a class="site-userbar__compact-metric--bonus" title="爆米花 1,274,492.3">1,274,492.3</a>
        <span class="site-userbar__compact-metric--seeding-bonus" title="做种积分 1,194,028.3">1,194,028.3</span>
        <a class="site-userbar__compact-metric-inline-link--seeding">↑ 0</a><a class="site-userbar__compact-metric-inline-link--leeching">↓ 1</a>'''
        values,_=extract([('/index.php',html)])
        self.assertEqual(values,dict(bonus=1274492.3,seeding_count=0,leeching_count=1))
        html='<div id="info_block">猫粮 [使用 | 站免池]: 1,189,272.7 做种积分： 845,117.5</div>'
        self.assertEqual(extract([('/index.php',html)])[0]['bonus'],1189272.7)

    def test_next_sibling_selector_and_custom_account_strip(self):
        selectors=NexusPHPSelectors().replace(profile_seeding_css="img[alt='做种数']::next_sibling_text",profile_leeching_css="img[alt='下载数']::next_sibling_text")
        html='''<span class="medium"><a href="userdetails.php?id=9">fixture</a>
        上传量: 1 TiB 下载量: 1 GiB 魔力值 [使用]: 4,013,463.9
        <img alt="做种数"/><span>0</span><img alt="下载数"/>0</span>'''
        values,_=extract([('/userdetails.php',html)],selectors,user_id='9')
        self.assertEqual(values['bonus'],4013463.9)
        self.assertEqual(values['seeding_count'],0)
        self.assertEqual(values['leeching_count'],0)
        self.assertNotIn('bonus',extract([('/userdetails.php',html)],None,user_id='another')[0])

    def test_gazelle_personal_bonus_and_nexus_currency_names(self):
        html='<ul class="HeaderStat"><li class="HeaderStat-item is-bp"><a class="HeaderStat-link">546,984</a></li></ul><main>做种: 170,108</main>'
        values,_=extract([('/index.php',html)])
        self.assertEqual(values,{'bonus':546984})
        html='<div id="info_block">电力值 2,279,403.3 <font title="当前做种">⬆</font>0 <font title="当前下载">⬇</font>0</div>'
        values,_=extract([('/index.php',html)])
        self.assertEqual(values,{'bonus':2279403.3,'seeding_count':0,'leeching_count':0})

    def test_api_zero_is_known_only_when_success_and_key_present(self):
        self.assertEqual(api_fields('/api/tracker/myPeerStatus',{'code':'0','message':'SUCCESS','data':{'seeder':'5','leecher':0}}),{'seeding_count':5,'leeching_count':0})
        self.assertEqual(api_fields('/api/tracker/myPeerStatus',{'code':'0','data':{'seeder':5}}),{'seeding_count':5})
        self.assertEqual(api_fields('/api/user/getInfo',{'status':200,'data':{'seeding':0,'leeching':0,'upload':123,'username':'SECRET'}}),{'uploaded_bytes':123,'seeding_count':0,'leeching_count':0})
        self.assertEqual(api_fields('/api/user/getInfo',{'status':401,'data':{'seeding':0}}),{})
        self.assertEqual(api_fields('/api/tracker/myPeerStatus',{'code':'1','message':'ERROR','data':{'seeder':0}}),{})
        self.assertEqual(extract([('/api/tracker/myPeerStatus',{'leeching_count':0})])[0],{'leeching_count':0})

    def test_nested_nexus_profile_and_combined_transfer_row(self):
        html = '''<h1>fixture</h1><table>
        <tr><td class="rowhead">上传量</td><td><b>2.5 TiB</b></td></tr>
        <tr><td class="rowhead">传输</td><td>分享率: 5 上传量: 2.5 TiB 下载量: 512 GiB</td></tr>
        <tr><td class="rowhead">魔力值</td><td><a>1,234.56</a></td></tr>
        <tr><td class="rowhead">当前做种</td><td><span>0</span></td></tr></table>'''
        values, sources = extract([('/userdetails.php', html)], NexusPHPSelectors())
        self.assertEqual(values['uploaded_bytes'], int(2.5 * 1024 ** 4))
        self.assertEqual(values['downloaded_bytes'], 512 * 1024 ** 3)
        self.assertEqual(values['seeding_count'], 0)
        self.assertEqual(values['bonus'], 1234.56)
        self.assertNotIn('leeching_count', values)

    def test_header_fields_and_icons_with_true_zero(self):
        html = '''<div id="info_block">上传量: 0 B 下载量: 0 B
        <img title="Torrents seeding"/> <span>0</span>
        <img alt="Torrents leeching"/> 2 </div>
        <div id="header-bonus-value">42</div>'''
        values, _ = extract([('/index.php', html)])
        self.assertEqual(values, dict(uploaded_bytes=0, downloaded_bytes=0, seeding_count=0, leeching_count=2, bonus=42))

    def test_gazelle_stats_do_not_require_an_extra_request(self):
        html = '<div id="header-uploaded-value">3 TiB</div><div id="header-downloaded-value">1 TiB</div>'
        result, fields, sources = enrich(UserProfile(user_id='1', username='fixture'), [('/index.php', html)])
        self.assertEqual(result.uploaded_bytes, 3 * 1024 ** 4)
        self.assertEqual(result.ratio, 3)
        self.assertEqual(sources['ratio'], 'calculated')

    def test_unrelated_torrent_or_news_text_is_not_account_data(self):
        html = '<main>上传量: 100 TiB 魔力: 9000</main><table><td class="rowhead">上传量</td><td>999 TiB</td></table>'
        self.assertEqual(extract([('/index.php', html)])[0], {})
        self.assertEqual(extract([('/torrents.php', html)])[0], {})

    def test_native_specific_parser_wins_and_invalid_values_stay_unknown(self):
        p = UserProfile(user_id='1', username='fixture', uploaded_bytes=1234, ratio=0)
        result, fields, sources = enrich(p, [('/index.php', '<div id="header-uploaded-value">9 TiB</div>')])
        self.assertEqual(result.uploaded_bytes, 1234)
        self.assertEqual(fields['ratio'], 0)
        self.assertEqual(sources['uploaded_bytes'], 'native')
        for value in ('unknown', '—', '-1', 'nan', '1e8', '1.5'):
            self.assertIsNone(parse_value('seeding_count', value))
        self.assertIsNone(parse_value('uploaded_bytes', '1024'))


class ProfileCapture(unittest.IsolatedAsyncioTestCase):
    async def test_gazelle_own_user_api_does_not_capture_credentials_or_site_totals(self):
        observer=AsyncMock();calls=[]
        def handler(request):
            calls.append(request.url.path)
            if request.url.path=='/index.php':
                return httpx.Response(200,text='<a id="header-username-value" href="user.php?id=9">fixture</a><ul class="HeaderStat"><li class="HeaderStat-item is-bp"><a class="HeaderStat-link">546,984</a></li></ul><li>做种: 170,108</li>')
            self.assertEqual(request.url.path,'/ajax.php');self.assertEqual(dict(request.url.params),{'action':'user','id':'9'})
            return httpx.Response(200,json={'status':'success','response':{'username':'fixture','personal':{'passkey':'SECRET'},'community':{'seeding':0,'leeching':0}}})
        cls=with_identity(GreatPosterWallSite,lambda _: '',observe_profile=observer)
        async with HttpClient(transport=httpx.MockTransport(handler)) as client:
            site=cls(site_id='gpw',base_url='https://example.com',client=client,auth_manager=None)
            self.assertTrue(await site.check_auth())
            observer.assert_not_called()
            p=await site.get_user_profile()
        self.assertEqual(p.bonus,546984)
        self.assertEqual(observer.call_args.args[2]['seeding_count'],0)
        self.assertNotIn('SECRET',str(observer.call_args))
        self.assertEqual(calls,['/index.php','/index.php','/ajax.php'])
        data=api_fields('/ajax.php',{'status':'success','response':{'username':'other','community':{'seeding':100}}})
        _,fields,_=enrich(UserProfile(user_id='9',username='fixture'),[('/ajax.php',data)])
        self.assertNotIn('seeding_count',fields)

    async def test_dedicated_api_zero_survives_post_requests_without_extra_calls(self):
        observer = AsyncMock()
        cls = with_identity(MTeamSite, lambda _: '', observe_profile=observer)
        calls = []
        def handler(request):
            calls.append(request.url.path)
            data = {'id':1,'username':'fixture','memberCount':{'uploaded':'1024','downloaded':'512','bonus':'0','shareRate':'2'}} if request.url.path.endswith('/profile') else {'seeder':5,'leecher':0}
            return httpx.Response(200,json={'code':'0','message':'SUCCESS','data':data})
        async with HttpClient(transport=httpx.MockTransport(handler)) as client:
            site = cls(site_id='mteam',base_url='https://example.com',client=client,auth_manager=None)
            profile = await site.get_user_profile()
        self.assertEqual(profile.seeding_count,5)
        self.assertEqual(observer.call_args.args[2]['leeching_count'],0)
        self.assertEqual(calls,['/api/member/profile','/api/tracker/myPeerStatus'])

    async def test_all_wrapped_nexus_clients_enrich_and_preserve_auth(self):
        observer = AsyncMock()
        site_class = with_identity(PackNexusSite, lambda _: 'fixture-UA', observe_profile=observer)
        calls = []
        def handler(request):
            calls.append(request.url.path)
            self.assertEqual(request.headers['user-agent'], 'fixture-UA')
            self.assertIn('uid=fixture', request.headers['cookie'])
            html = '<a href="userdetails.php?id=1">fixture</a>' if request.url.path == '/index.php' else '<h1>fixture</h1><table><td class="rowhead">传输</td><td>上传量: 2 TiB 下载量: 1 TiB</td></table>'
            return httpx.Response(200, text=html)
        async with HttpClient(cookies={'uid':'fixture'}, transport=httpx.MockTransport(handler)) as client:
            site = site_class(site_id='fixture', base_url='https://example.com', client=client, auth_manager=None,
                selectors=NexusPHPSelectors().replace(profile_uid_css="a[href*='userdetails.php?id=']::attr(href)"))
            profile = await site.get_user_profile()
        self.assertEqual(profile.ratio, 2)
        self.assertEqual(calls, ['/index.php', '/userdetails.php'])
        self.assertEqual(observer.call_args.args[2]['uploaded_bytes'], 2 * 1024 ** 4)

    async def test_gpw_profile_uses_header_and_does_not_store_another_user(self):
        observer = AsyncMock()
        cls = with_identity(GreatPosterWallSite, lambda _: '', observe_profile=observer)
        html = '<a id="header-username-value" href="user.php?id=1">fixture</a><span id="header-uploaded-value">1 GiB</span>'
        async with HttpClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=html))) as client:
            site = cls(site_id='fixture', base_url='https://example.com', client=client, auth_manager=None)
            self.assertEqual((await site.get_user_profile('1')).uploaded_bytes, 1024 ** 3)
            observer.assert_not_called()
            await site.get_user_profile()
            observer.assert_awaited_once()

    async def test_evidence_persists_whitelisted_numbers_and_matches_snapshot(self):
        values = {}
        store = SimpleNamespace(get=AsyncMock(side_effect=values.get), set=AsyncMock(side_effect=lambda k,v,**kw: values.__setitem__(k, copy.deepcopy(v))))
        evidence = ProfileEvidence(store)
        await evidence.start()
        profile = UserProfile(user_id='private-id', username='private-owner')
        await evidence.observe('fixture', profile, dict(uploaded_bytes=0, downloaded_bytes=0, seeding_count=0, cookie='SECRET'), dict(uploaded_bytes='selector'))
        snapshot = dict(username='private-owner', fetched_at=datetime.now(timezone.utc).isoformat())
        proof = evidence.lookup('fixture', snapshot)
        self.assertIsNotNone(proof)
        projected = profile_view({'profile': snapshot}, proof)
        self.assertEqual(projected['uploaded_bytes'], 0)
        self.assertEqual(projected['seeding_count'], 0)
        self.assertIsNone(projected['leeching_count'])
        self.assertNotIn('private-owner', json.dumps(values))
        self.assertNotIn('SECRET', json.dumps(values))
        self.assertIsNone(evidence.lookup('fixture', {**snapshot, 'username':'other-owner'}))
        self.assertIsNone(evidence.lookup('fixture', {**snapshot, 'fetched_at':(datetime.now(timezone.utc)-timedelta(hours=1)).isoformat()}))
        restarted = ProfileEvidence(store)
        await restarted.start()
        self.assertEqual(restarted.values, evidence.values)
