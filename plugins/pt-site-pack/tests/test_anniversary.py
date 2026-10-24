import json
import unittest
import hashlib
import httpx
from unittest.mock import AsyncMock
from datetime import datetime, timezone
from movieclaw_tracker.models import UserProfile
from pt_pack.anniversary import age_view, registration_date, extract_registration, own_profile_id
from pt_pack.profiles import enrich, api_fields, ProfileEvidence, AUTH_CHECK
from pt_pack.overview import profile_view, Overview

NOW = datetime(2026, 10, 24, tzinfo=timezone.utc)


class AnniversaryTests(unittest.TestCase):
    def test_calendar_dates_and_invalid_values(self):
        self.assertEqual(age_view('2026-10-24', NOW)['age_days'], 0)
        self.assertEqual(age_view('2026-10-23', NOW)['age_days'], 1)
        self.assertEqual(registration_date('2016年2月29日 12:30:00', NOW).date().isoformat(), '2016-02-29')
        for value in ['2026-10-25', '2019-02-29', '1999-12-31', 123, True, None, '2020-13-01', 'nonsense']:
            with self.subTest(value=value):self.assertIsNone(age_view(value, NOW)['age_days'])

    def test_only_own_profile_registration_label_counts(self):
        p=UserProfile(user_id='9', username='fixture')
        html='<h1>fixture</h1><table><tr><td class="rowhead">注册日期</td><td><span>2016-02-29 12:30:00</span></td></tr></table>'
        self.assertEqual(extract_registration([('/userdetails.php?id=9', html)], p).year, 2016)
        self.assertIsNone(extract_registration([('/userdetails.php?id=8', html)], p))
        self.assertIsNone(extract_registration([('/index.php', html)], p))
        self.assertIsNone(extract_registration([('/userdetails.php', html.replace('fixture', 'other'))], p))
        self.assertIsNone(extract_registration([('/userdetails.php?id=9', html.replace('注册日期', '站点建立日期'))], p))
        result, fields, _ = enrich(p, [('/userdetails.php?id=9', html)])
        self.assertEqual(result.join_date.year, 2016)
        self.assertNotIn('join_date', fields)  # six numerical evidence fields remain unchanged.
        old=p.model_copy(update={'join_date':datetime(2010,1,1,tzinfo=timezone.utc)})
        self.assertEqual(enrich(old, [('/userdetails.php?id=9', html)])[0].join_date.year, 2010)

    def test_age_does_not_use_uploads_or_server_account_creation(self):
        data=profile_view({'created_at':'2010-01-01','profile':{'uploaded_bytes':10**16}})
        self.assertIsNone(data['age_days'])

    def test_modern_labels_exact_dates_and_account_boundaries(self):
        p=UserProfile(user_id='9',username='fixture')
        examples=[
            '<table><tr><th>Joined</th><td><time datetime="2016-02-29">10 years ago</time></td></tr></table>',
            '<dl><dt>加入日期</dt><dd><span title="2016-02-29 12:30:00">10年前</span></dd></dl>',
            '<div><span class="font-bold">加入日期：</span><span title="2016-02-29 12:30:00">10年前</span></div>',
            '<span>加入日期：2016-02-29 12:30:00 (<span>10年前</span>)</span><span>最近动向：2026-10-24</span>',
        ]
        for html in examples:
            with self.subTest(html=html):
                self.assertEqual(extract_registration([('/userdetails.php?id=9',html)],p).year,2016)
        self.assertIsNone(extract_registration([('/userdetails.php','<h1>not-fixture</h1>'+examples[0])],p))
        self.assertIsNone(extract_registration([('/userdetails.php?id=9','<dl><dt>Joined</dt><dd>10年前</dd></dl>')],p))
        self.assertIsNone(extract_registration([('/userdetails.php?id=9','<div>注册用户：1000；开放注册日期：2016-02-29</div>')],p))

    def test_own_profile_link_resolution_rejects_other_users_and_domains(self):
        p=UserProfile(user_id='',username='fixture')
        own='<a href="userdetails.php?id=9">fixture</a>'
        self.assertEqual(own_profile_id([('/index.php',own)],p,'https://example.org'),'9')
        for html in [own.replace('fixture','not-fixture'),own.replace('userdetails.php','https://evil.example/userdetails.php'),own+own.replace('id=9','id=8')]:
            self.assertIsNone(own_profile_id([('/index.php',html)],p,'https://example.org'))
        self.assertIsNone(own_profile_id([('/index.php',own)],p.model_copy(update={'user_id':'8'}),'https://example.org'))

    def test_api_registration_only_known_successful_account_fields(self):
        p=UserProfile(username='fixture',user_id='9')
        payload={'code':'0','message':'SUCCESS','data':{'id':9,'username':'fixture','createdDate':'2016-02-29 12:30:00','memberCount':{'createdDate':'2020-01-01'}}}
        fields=api_fields('/api/member/profile',payload)
        self.assertEqual(extract_registration([('/api/member/profile',fields)],p).year,2016)
        self.assertIsNone(extract_registration([('/api/member/profile',fields)],p.model_copy(update={'user_id':'8'})))
        self.assertIsNone(extract_registration([('/api/member/profile',fields)],p.model_copy(update={'username':'other'})))
        self.assertEqual(api_fields('/api/member/profile',{**payload,'code':'1'}),{})
        self.assertNotIn('_joined_at',api_fields('/api/member/profile',{**payload,'data':{'id':9,'username':'fixture','memberCount':{'createdDate':'2020-01-01'}}}))
        gazelle=api_fields('/ajax.php',{'status':'success','response':{'username':'fixture','stats':{'joinedDate':'2016-02-29'}}})
        self.assertEqual(extract_registration([('/ajax.php',gazelle)],p).year,2016)
        zhuque={'status':200,'data':{'id':9,'username':'fixture','regTime':1679223004,'seedTime':1234567890}}
        self.assertEqual(extract_registration([('/api/user/getInfo',api_fields('/api/user/getInfo',zhuque))],p).year,2023)
        for bad in [True,0,-1,9999999999999,'unknown']:
            self.assertNotIn('_joined_at',api_fields('/api/user/getInfo',{**zhuque,'data':{**zhuque['data'],'regTime':bad}}))

    def test_default_privacy_omits_exact_registration_date(self):
        p=profile_view({'profile':{'join_date':'2012-02-03','username':'private-user'}})
        raw={'sites':[{'site_id':'samplept','name':'Sample','profile':p}], 'tasks':[]}
        service=Overview(None,None,None)
        private=service.project(raw,True,False,False)
        self.assertNotIn('join_date',private['sites'][0]['profile'])
        self.assertNotIn('2012-02-03',json.dumps(private))
        self.assertGreater(private['sites'][0]['profile']['age_days'],365)
        self.assertEqual(service.project(raw,False,False,False)['sites'][0]['profile']['join_date'],'2012-02-03')


class RegistrationEvidenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_encrypted_date_survives_missing_optional_page_but_not_account_change(self):
        data=type('Data',(),{'set':AsyncMock()})()
        proof=ProfileEvidence(data)
        p=UserProfile(user_id='9',username='fixture',join_date=datetime(2016,2,29,tzinfo=timezone.utc))
        await proof.observe('sample',p,{}, {})
        record=proof.values['sample']
        self.assertEqual(record['join_date'],'2016-02-29')
        self.assertTrue(data.set.call_args.kwargs['secret'])
        self.assertNotIn('fixture',json.dumps(record))
        native={'username':'fixture','fetched_at':record['observed_at']}
        self.assertIsNotNone(profile_view({'profile':native},proof.lookup('sample',native))['age_days'])
        self.assertIsNone(proof.lookup('sample',{**native,'username':'different'}))
        await proof.observe('sample',p.model_copy(update={'join_date':None}),{}, {})
        self.assertEqual(proof.values['sample']['join_date_observed_at'],record['join_date_observed_at'])
        await proof.observe('sample',p.model_copy(update={'join_date':None,'username':'different'}),{}, {})
        self.assertNotIn('join_date',proof.values['sample'])

    async def test_old_date_cannot_transfer_to_a_reused_username_with_different_uid(self):
        data=type('Data',(),{'set':AsyncMock()})()
        proof=ProfileEvidence(data)
        p=UserProfile(user_id='9',username='fixture',join_date=datetime(2016,2,29,tzinfo=timezone.utc))
        await proof.observe('sample',p,{}, {})
        await proof.observe('sample',p.model_copy(update={'user_id':'10','join_date':None}),{}, {})
        self.assertNotIn('join_date',proof.values['sample'])

    async def test_one_own_profile_request_never_during_auth_or_when_already_fetched(self):
        from movieclaw_tracker.frameworks.nexusphp import NexusPHPSite
        from movieclaw_tracker.selectors import NexusPHPSelectors
        from movieclaw_tracker.http import HttpClient
        from pt_pack.identity import with_identity
        requests=[]
        def response(request):
            requests.append(str(request.url))
            html='<a href="userdetails.php?id=9">fixture</a>'
            if request.url.params.get('id')=='9':html+='<table><tr><td>加入日期</td><td>2016-02-29</td></tr></table>'
            return httpx.Response(200,text=html)
        cls=with_identity(NexusPHPSite,lambda sid:'')
        client=HttpClient(transport=httpx.MockTransport(response))
        site=cls(site_id='fixture',base_url='https://example.org',client=client,auth_manager=None,
                 selectors=NexusPHPSelectors().replace(profile_username_css="a[href*='userdetails.php']",profile_uid_css=''))
        try:
            p=await site.get_user_profile()
            self.assertEqual(p.join_date.year,2016)
            self.assertEqual(len(requests),2)
            self.assertIn('id=9',requests[-1])
            requests.clear();token=AUTH_CHECK.set(True)
            try:await site.get_user_profile()
            finally:AUTH_CHECK.reset(token)
            self.assertEqual(len(requests),1)
            requests.clear();p=await site.get_user_profile('9')
            self.assertEqual(len(requests),1);self.assertEqual(p.join_date.year,2016)
        finally:await client.close()
