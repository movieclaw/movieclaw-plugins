"""Atomic account preset switching preserves profile and rejects stale/cross-account choices."""
import copy
import unittest
import test_account_sync as accounts

class PresetShortcutTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=accounts.AccountSyncTests.asyncSetUp
    asyncTearDown=accounts.AccountSyncTests.asyncTearDown
    open=accounts.AccountSyncTests.open

    async def create(self,client,name,filters):
        current=(await client.get('/personal/presets')).json()
        response=await client.post('/personal/presets',json={'change':{'operation':'save','revision':current['revision'],'name':name,'filters':filters}})
        self.assertEqual(response.status_code,200,response.text)
        return response.json()

    async def apply(self,client,record,filters,id=None):
        return await client.post('/personal/presets/apply',json={'choice':{'id':id or record['items'][-1]['id'],'presets_revision':record['revision'],'filters_revision':filters['revision']}})

    async def test_atomic_switch_sync_old_optional_defaults_profile_preserved_and_queue_invalidated(self):
        await self.open(self.lan)
        before=(await self.lan.get('/personal/profile')).json()
        full={'media_type':'tv','countries':['JP','KR'],'eras':['1990s','2000s'],'genres':['18','80'],'exclude_genres':['27'],'exploration':'discovery'}
        a=await self.create(self.lan,'日韩剧',full)
        initial=(await self.lan.get('/personal/filters')).json()
        queued=(await self.lan.get('/personal/recommendations/prepare?nonce=before-shortcut&limit=6')).json()
        first=await self.apply(self.wan,a,initial);self.assertEqual(first.status_code,200,first.text)
        saved=first.json();self.assertEqual(saved['filters']['countries'],['JP','KR'])
        self.assertEqual(saved['filters']['genres'],['18','80']);self.assertEqual(saved['filters']['exploration'],'discovery')
        self.assertEqual((await self.lan.get('/personal/filters')).json(),saved)
        self.assertEqual((await self.lan.post('/personal/recommendations/commit',json={'ticket':queued['prepared_ticket']})).status_code,409)
        retry=await self.apply(self.wan,a,saved);self.assertEqual(retry.json(),saved,'same active preset does not change revision or recreate the wall')
        basic={'media_type':'movie','countries':['US'],'eras':[]}
        b=await self.create(self.lan,'电影',basic)
        second=await self.apply(self.wan,b,saved);self.assertEqual(second.status_code,200,second.text)
        self.assertEqual(second.json()['filters'],basic,'a legacy preset clears previous genre/exploration rather than retaining them')
        self.assertEqual((await self.wan.get('/personal/profile')).json(),before)
        self.assertIn('no-store',second.headers['cache-control'])

    async def test_stale_removed_or_other_account_presets_never_apply(self):
        await self.open(self.lan)
        a=await self.create(self.lan,'日韩',{'media_type':'all','countries':['JP','KR'],'eras':[]})
        filters=(await self.lan.get('/personal/filters')).json()
        self.assertEqual((await self.apply(self.bob,a,filters)).status_code,409)
        self.assertEqual((await self.apply(self.anon,a,filters)).status_code,401)
        changed=await self.lan.post('/personal/filters',json={'selection':{'media_type':'tv','countries':[],'eras':[],'revision':filters['revision']}})
        self.assertEqual((await self.apply(self.wan,a,filters)).status_code,409)
        removed=await self.lan.post('/personal/presets',json={'change':{'operation':'remove','revision':a['revision'],'id':a['items'][0]['id']}})
        self.assertEqual((await self.apply(self.wan,a,changed.json())).status_code,409)
        self.assertEqual((await self.apply(self.wan,removed.json(),changed.json(),a['items'][0]['id'])).status_code,409)
        self.assertEqual((await self.wan.get('/personal/filters')).json(),changed.json())

    async def test_bounded_validated_same_origin_and_no_invalid_writes(self):
        await self.open(self.lan);before=copy.deepcopy(self.store.values)
        for choice in [{}, {'id':'x','presets_revision':'','filters_revision':''},{'id':'a'*16,'presets_revision':[],'filters_revision':''}]:
            r=await self.lan.post('/personal/presets/apply',json={'choice':choice});self.assertEqual(r.status_code,400)
        self.assertEqual(self.store.values,before)
        body={'choice':{'id':'a'*16,'presets_revision':'','filters_revision':''}}
        self.assertEqual((await self.lan.post('/personal/presets/apply',json=body,headers={'sec-fetch-site':'cross-site'})).status_code,403)
        self.assertEqual((await self.lan.post('/personal/presets/apply',json={'choice':'x'*2000})).status_code,413)
