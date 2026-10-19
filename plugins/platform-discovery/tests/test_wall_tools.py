"""Five account tools: bounded private persistence, ranking, receipt CAS and history."""
import asyncio
import copy
import random
import unittest
from unittest.mock import patch
import test_account_sync as accounts
from test_personal import personal, FakeDiscovery, raw_row

class WallToolsTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=accounts.AccountSyncTests.asyncSetUp
    asyncTearDown=accounts.AccountSyncTests.asyncTearDown
    open=accounts.AccountSyncTests.open
    feedback=accounts.AccountSyncTests.feedback

    async def preset(self,client,change):
        return await client.post('/personal/presets',json={'change':change})

    async def test_presets_account_scope_conflicts_archive_restore_and_restart(self):
        await self.open(self.lan)
        before=(await self.lan.get('/personal/profile')).json()
        selection={'media_type':'all','countries':['KR','JP'],'eras':['1990s','2000s'],'genres':['80','9648'],'exclude_genres':['27'],'exploration':'discovery'}
        first=await self.preset(self.lan,{'operation':'save','revision':'','name':'日韩老片','filters':selection})
        self.assertEqual(first.status_code,200,first.text);record=first.json();entry=record['items'][0]
        self.assertIn('no-store',first.headers['cache-control'])
        self.assertEqual((await self.wan.get('/personal/presets')).json(),record)
        self.assertEqual((await self.bob.get('/personal/presets')).json()['items'],[])
        self.assertEqual((await self.preset(self.wan,{'operation':'remove','revision':'','id':entry['id']})).status_code,409)
        archived=(await self.preset(self.wan,{'operation':'remove','revision':record['revision'],'id':entry['id']})).json()
        self.assertEqual(archived['items'],[]);self.assertEqual(archived['archived'][0],entry)
        restored=await self.preset(self.lan,{'operation':'restore','revision':archived['revision'],'id':entry['id']})
        self.assertEqual(restored.status_code,200,restored.text)
        self.assertEqual(restored.json()['items'][0],entry)
        token=next(iter(self.service._account_tokens.values()));new=personal.PersonalRecommendations(FakeDiscovery(),private_store=self.store)
        self.assertEqual(await new.get_presets(token),restored.json());await new.close()
        self.assertEqual((await self.lan.get('/personal/profile')).json(),before)
        self.assertEqual((await self.lan.get('/personal/filters')).json()['filters']['countries'],[],'collecting does not apply')

    async def test_genre_options_exploration_sync_and_legacy_save_preserves_new_fields(self):
        await self.open(self.lan)
        selection={'media_type':'all','countries':['JP','KR'],'eras':['1990s','2000s'],'genres':['18','80'],'exclude_genres':['27'],'exploration':'discovery','revision':''}
        response=await self.lan.post('/personal/filters',json={'selection':selection});self.assertEqual(response.status_code,200,response.text)
        first=response.json();self.assertEqual((await self.wan.get('/personal/filters')).json(),first)
        old={'media_type':'tv','countries':['JP'],'eras':['2000s'],'revision':first['revision']}
        saved=(await self.wan.post('/personal/filters',json={'selection':old})).json()
        self.assertEqual(saved['filters']['genres'],['18','80']);self.assertEqual(saved['filters']['exploration'],'discovery')
        for bad in [dict(selection,genres=['27']),dict(selection,exploration=[]),dict(selection,genres=['999']),dict(selection,genres='18')]:
            self.assertEqual((await self.lan.post('/personal/filters',json={'selection':bad})).status_code,400)
        for q in ['genres=27&exclude_genres=27','genres=999','exploration=bad','exclude_genres=bad']:
            self.assertEqual((await self.lan.get('/personal/recommendations?'+q)).status_code,400)

    async def test_genre_union_exclusion_cache_and_prepare_commit_resume(self):
        await self.open(self.lan)
        params={'limit':24,'nonce':'genres','genres':'80,18','exclude_genres':'27','exploration':'discovery'}
        result=await self.lan.get('/personal/recommendations/prepare',params=params);self.assertEqual(result.status_code,200,result.text)
        value=result.json();self.assertEqual(len(value['items']),24)
        self.assertTrue(all(set(i['metadata']['genre_ids']) & {18,80} and 27 not in i['metadata']['genre_ids'] for i in value['items']))
        self.assertEqual({i['metadata']['genre_ids'][0] for i in value['items']},{18,80})
        committed=(await self.lan.post('/personal/recommendations/commit',json={'ticket':value['prepared_ticket']})).json()
        resumed=(await self.wan.get('/personal/recommendations/resume',params=params)).json()
        self.assertEqual([i['detail_ref'] for i in resumed['items']],[i['detail_ref'] for i in committed['items']])
        wrong=(await self.wan.get('/personal/recommendations/resume',params={**params,'exploration':'familiar'})).json();self.assertEqual(wrong['items'],[])
        default=await self.lan.get('/personal/recommendations',params={'limit':24,'nonce':'genres'});self.assertNotEqual(default.json()['batch_id'],committed['batch_id'])
        queries=self.service.discovery.calls;self.assertTrue(any(q[1].get('with_genres')=='80' for q in queries))

    async def test_exploration_changes_actual_rank_without_rewriting_tests_or_hard_exclusions(self):
        await self.open(self.lan);token=next(iter(self.service._account_tokens.values()))
        item=personal._public_item(raw_row('movie',91,genre=80),'movie')
        vector={'genre:80':1,'country:US':1};plain=dict(personal.DEFAULT_ANSWERS)
        focused=self.service._rank(item,vector,{**plain,'_wall_exploration':'familiar'},random.Random(9),set())[0]
        broad=self.service._rank(item,vector,{**plain,'_wall_exploration':'discovery'},random.Random(9),set())[0]
        self.assertGreater(focused,broad+15)
        before=(await self.lan.get('/personal/profile')).json()
        await self.feedback(self.lan,'watch',item)
        for mode in ['familiar','balanced','discovery']:
            result=(await self.lan.get('/personal/recommendations',params={'nonce':mode,'limit':6,'exploration':mode})).json()
            self.assertNotIn(item['detail_ref'],[i['detail_ref'] for i in result['items']])
        after=(await self.lan.get('/personal/profile')).json()
        for k in ['answers','history','history_count']:self.assertEqual(after[k],before[k])

    async def test_undo_across_devices_restores_prior_state_and_is_idempotent(self):
        await self.open(self.lan);item=personal._public_item(raw_row('movie',99),'movie')
        hidden=await self.feedback(self.lan,'hide',item)
        saved=await self.feedback(self.wan,'save',item);receipt=saved['undo_action'];self.assertFalse(saved['dismissed'])
        self.assertIn(receipt,(await self.lan.get('/personal/actions')).json()['items'])
        # A different film/flag must not prevent undo, and must remain untouched.
        other=personal._public_item(raw_row('movie',100),'movie');await self.feedback(self.lan,'like',other)
        undo=await self.wan.post('/personal/actions/undo',json={'id':receipt['id']});self.assertEqual(undo.status_code,200,undo.text)
        self.assertEqual(undo.json()['dismissed'],hidden['dismissed']);self.assertEqual(undo.json()['saved'],[])
        self.assertEqual(undo.json()['liked'][0]['item']['detail_ref'],other['detail_ref'])
        retry=await self.lan.post('/personal/actions/undo',json={'id':receipt['id']});self.assertEqual(retry.json(),undo.json())
        self.assertEqual((await self.bob.post('/personal/actions/undo',json={'id':receipt['id']})).status_code,409)

    async def test_undo_rejects_changed_mark_old_receipts_and_expired_actions(self):
        await self.open(self.lan);item=personal._public_item(raw_row('movie',101),'movie')
        first=await self.feedback(self.lan,'like',item)
        await self.feedback(self.wan,'unlike',{'detail_ref':item['detail_ref']})
        await self.feedback(self.wan,'like',item)
        self.assertEqual((await self.lan.post('/personal/actions/undo',json={'id':first['undo_action']['id']})).status_code,409)
        current=await self.feedback(self.lan,'watch',item)
        with patch.object(personal.time,'time',return_value=current['undo_action']['expires_at']+1):
            self.assertEqual((await self.wan.post('/personal/actions/undo',json={'id':current['undo_action']['id']})).status_code,409)

    async def test_history_only_actual_views_and_committed_walls_no_resume_or_speculative_records(self):
        await self.open(self.lan)
        before=(await self.lan.get('/personal/history')).json();self.assertEqual(before,{'views':[],'walls':[]})
        prepared=(await self.lan.get('/personal/recommendations/prepare?nonce=history&limit=6')).json()
        self.assertEqual((await self.wan.get('/personal/history')).json(),before)
        committed=await self.lan.post('/personal/recommendations/commit',json={'ticket':prepared['prepared_ticket']});self.assertEqual(committed.status_code,200,committed.text)
        walls=(await self.wan.get('/personal/history')).json()['walls'];self.assertEqual(len(walls),1);self.assertEqual(len(walls[0]['items']),6)
        await self.lan.get('/personal/recommendations/resume?limit=6')
        await self.lan.post('/personal/recommendations/commit',json={'ticket':prepared['prepared_ticket']})
        self.assertEqual(len((await self.wan.get('/personal/history')).json()['walls']),1)
        item=committed.json()['items'][0];profile=(await self.lan.get('/personal/profile')).json()
        self.assertEqual((await self.lan.post('/personal/history',json={'item':{**item,'user_id':'bad','metadata':{**item['metadata'],'library_status':'secret'}}})).status_code,200)
        await self.wan.post('/personal/history',json={'item':item})
        history=(await self.wan.get('/personal/history')).json();self.assertEqual(len(history['views']),1);self.assertNotIn('user_id',str(history));self.assertNotIn('library_status',str(history))
        self.assertEqual((await self.lan.get('/personal/profile')).json(),profile,'view history cannot alter recommendation or taste revisions')
        self.assertEqual((await self.bob.get('/personal/history')).json(),before)

    async def test_new_routes_same_origin_bounded_authenticated_and_invalid_writes_are_read_only(self):
        for path in ['presets','history','actions']:
            self.assertEqual((await self.anon.get('/personal/'+path)).status_code,401)
            r=await self.lan.get('/personal/'+path);self.assertEqual(r.status_code,200,r.text);self.assertIn('no-store',r.headers['cache-control'])
        for path,body in [('history',{'item':{'detail_ref':'bad'}}),('actions/undo',{'id':'bad'}),('presets',{'change':{'operation':'save','revision':'','name':'x','filters':{}}})]:
            self.assertEqual((await self.anon.post('/personal/'+path,json=body)).status_code,401)
            self.assertEqual((await self.lan.post('/personal/'+path,json=body,headers={'sec-fetch-site':'cross-site'})).status_code,403)
            self.assertEqual((await self.lan.post('/personal/'+path,json=body)).status_code,400)
        self.assertEqual((await self.lan.post('/personal/history',json={'item':{'title':'x'*18000}})).status_code,413)

    async def test_history_bounds_concurrent_views_encrypted_storage_and_profile_preservation(self):
        await self.open(self.lan)
        before=(await self.lan.get('/personal/profile')).json()
        for index in range(7):
            response=await self.lan.get('/personal/recommendations',params={'nonce':'bounded'+str(index),'limit':6})
            self.assertEqual(response.status_code,200,response.text)
        async def view(index):
            item=personal._public_item(raw_row('movie',5000+index),'movie')
            return await self.lan.post('/personal/history',json={'item':item})
        responses=await asyncio.gather(*(view(index) for index in range(130)))
        self.assertTrue(all(r.status_code==200 for r in responses))
        record=(await self.wan.get('/personal/history')).json()
        self.assertEqual(len(record['views']),120);self.assertEqual(len(record['walls']),6)
        self.assertEqual(len({r['item']['detail_ref'] for r in record['views']}),120)
        self.assertTrue(all(write['secret'] for write in self.store.writes))
        after=(await self.wan.get('/personal/profile')).json()
        for k in ['answers','history','history_count','learning']:self.assertEqual(before[k],after[k])

    async def test_preset_capacity_invalid_names_and_concurrent_writes_keep_existing_items(self):
        await self.open(self.lan)
        selection={'media_type':'all','countries':[],'eras':[]}
        async def save(name,revision=''):
            return await self.preset(self.lan,{'operation':'save','revision':revision,'name':name,'filters':selection})
        values=await asyncio.gather(save('first'),save('second'))
        self.assertEqual(sorted(r.status_code for r in values),[200,409])
        record=(await self.wan.get('/personal/presets')).json()
        for name in ['', 'a'*33, [], None]:self.assertEqual((await save(name,record['revision'])).status_code,400)
        token=next(iter(self.service._account_tokens.values()));scope=self.service._scope(token)
        record['items']=[{'id':format(i,'016x'),'name':'preset'+str(i),'filters':selection,'created_at':'today'} for i in range(20)]
        await self.service._write(scope,'filter-presets',record)
        self.assertEqual((await save('21st',record['revision'])).status_code,400)
        self.assertEqual((await self.wan.get('/personal/presets')).json(),record)

    async def test_history_projection_retains_original_genres_and_rating_evidence(self):
        item=personal._public_item(raw_row('movie',6000),'movie')
        item['metadata']['genre_ids']=[18,10402]
        projected=self.service._history_item(item)
        self.assertEqual(projected['metadata']['genre_ids'],[18,10402])
        for k in ['minimum_votes','vote_count','language_verified']:
            self.assertEqual(projected['metadata'][k],item['metadata'][k])
        self.assertIsNotNone(self.service._catalog_item(projected))
        blocked={**item,'metadata':{**item['metadata'],'genre_ids':[18,99]}}
        self.assertIsNone(self.service._catalog_item(self.service._history_item(blocked)),'history cannot erase a source tag that would block feedback')
