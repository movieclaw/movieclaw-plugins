"""Account-scoped multi-select settings, CAS writes and recommendation guards."""
import asyncio
import copy
import unittest
import test_account_sync as accounts
from test_personal import personal, FakeDiscovery

class SyncedFilterTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = accounts.AccountSyncTests.asyncSetUp
    asyncTearDown = accounts.AccountSyncTests.asyncTearDown
    open = accounts.AccountSyncTests.open
    feedback = accounts.AccountSyncTests.feedback

    async def save(self, client, countries, eras, revision="", media_type="all"):
        return await client.post('/personal/filters',json={'selection':{'countries':countries,'eras':eras,'media_type':media_type,'revision':revision}})

    async def test_lan_wan_share_canonical_settings_bob_anon_and_restart_are_isolated(self):
        await self.open(self.lan)
        before=(await self.lan.get('/personal/profile')).json()
        response=await self.save(self.lan,['KR','JP','JP'],['2000s','1990s'])
        self.assertEqual(response.status_code,200,response.text)
        saved=response.json()
        self.assertEqual(saved['filters'],{'media_type':'all','countries':['JP','KR'],'eras':['1990s','2000s']})
        self.assertIn('no-store',response.headers['cache-control'])
        self.assertEqual((await self.wan.get('/personal/filters')).json(),saved)
        self.assertEqual((await self.lan.get('/personal/profile')).json(),before,'filter changes cannot edit taste/history/feedback')
        token=next(iter(self.service._account_tokens.values()))
        replacement=personal.PersonalRecommendations(FakeDiscovery(),private_store=self.store)
        self.assertEqual(await replacement.get_wall_filters(token),saved)
        await replacement.close()
        self.assertEqual((await self.bob.get('/personal/filters')).json()['filters']['countries'],[])
        self.assertEqual((await self.anon.get('/personal/filters')).status_code,401)
        self.assertEqual((await self.save(self.anon,['JP'],[])).status_code,401)
        self.assertEqual((await self.wan.get('/personal/filters',headers={'X-Platform-Account':'member:bob'})).status_code,409)

    async def test_conflicts_do_not_overwrite_other_device_and_retry_is_idempotent(self):
        await self.open(self.lan)
        value=(await self.save(self.lan,['JP'],['1990s'])).json()
        duplicate=await self.save(self.wan,['JP'],['1990s'])
        self.assertEqual(duplicate.json(),value)
        self.assertEqual((await self.save(self.wan,['KR'],['2000s'])).status_code,409)
        self.assertEqual((await self.lan.get('/personal/filters')).json(),value)
        outcomes=await asyncio.gather(self.save(self.lan,['KR'],['2000s'],value['revision']),self.save(self.wan,['US'],['1980s'],value['revision']))
        self.assertEqual(sorted(r.status_code for r in outcomes),[200,409])
        latest=(await self.lan.get('/personal/filters')).json()
        reset=await self.save(self.wan,[],[],latest['revision'])
        self.assertEqual(reset.status_code,200)
        self.assertEqual(reset.json()['filters'],{'media_type':'all','countries':[],'eras':[]})

    async def test_union_within_dimensions_intersection_between_dimensions(self):
        await self.open(self.lan)
        response=await self.lan.get('/personal/recommendations?nonce=union&limit=24&country=KR,JP&era=2000s,1990s')
        self.assertEqual(response.status_code,200,response.text)
        value=response.json();self.assertEqual(len(value['items']),24)
        self.assertEqual(value['filters'],{'media_type':'all','country':'JP,KR','era':'1990s,2000s'})
        self.assertTrue(all(set(i['metadata']['origin_country']) & {'JP','KR'} and 1990<=i['year']<=2009 for i in value['items']))
        self.assertEqual({i['metadata']['origin_country'][0] for i in value['items']},{'JP','KR'})
        self.assertEqual({i['year']//10 for i in value['items']},{199,200})
        self.assertEqual(personal.wall_windows(['1980s','2000s']),[(1980,1989),(2000,2009)])
        self.assertEqual(personal.wall_windows(['classic','recent']),[(None,1999),(2010,None)])
        calls=len(self.service.discovery.calls)
        same=(await self.lan.get('/personal/recommendations?nonce=union&limit=24&country=JP,KR&era=1990s,2000s')).json()
        self.assertEqual(same['batch_id'],value['batch_id'])
        self.assertEqual(len(self.service.discovery.calls),calls)

    async def test_remote_save_invalidates_old_queue_even_without_new_client_header(self):
        await self.open(self.lan)
        old=(await self.lan.get('/personal/recommendations/prepare?nonce=old&limit=6')).json()
        saved=(await self.save(self.wan,['KR','JP'],['1990s','2000s'])).json()
        self.assertEqual((await self.lan.post('/personal/recommendations/commit',json={'ticket':old['prepared_ticket']})).status_code,409)
        for suffix in ['', '/prepare', '/resume']:
            response=await self.lan.get('/personal/recommendations'+suffix+'?nonce=stale&limit=6',headers={'X-Platform-Filter-Revision':''})
            self.assertEqual(response.status_code,409,response.text)
        headers={'X-Platform-Filter-Revision':saved['revision']}
        new=await self.lan.get('/personal/recommendations/prepare?nonce=new&limit=6&country=JP,KR&era=1990s,2000s',headers=headers)
        self.assertEqual(new.status_code,200,new.text)
        committed=await self.wan.post('/personal/recommendations/commit',json={'ticket':new.json()['prepared_ticket']},headers=headers)
        self.assertEqual(committed.status_code,200,committed.text)
        self.assertTrue(all(1990<=i['year']<=2009 for i in committed.json()['items']))

    async def test_invalid_settings_are_rejected_without_writes(self):
        await self.open(self.lan)
        before=copy.deepcopy(self.store.values)
        for countries,eras in [(['all','JP'],[]),(['ZZ'],[]),(['jp'],[]),([],['1999s']),([],['all','1990s']),(['JP']*40,[])]:
            self.assertEqual((await self.save(self.lan,countries,eras)).status_code,400)
        self.assertEqual((await self.lan.post('/personal/filters',json={'selection':{'countries':[],'eras':[],'media_type':'all'}})).status_code,400)
        self.assertEqual(self.store.values,before)
