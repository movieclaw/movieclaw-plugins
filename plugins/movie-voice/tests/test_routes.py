import re
import base64
import hashlib
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode,urlsplit
from unittest.mock import patch
import httpx
from fastapi import FastAPI
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from shortcut_backend import mount_shortcut
from movie_voice import movie_snapshot,FoundMovie


class RoutesTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.actions=[];self.arguments=[];self.remote_titles=None;self.published=[];self.effects=[];self.data={};self.library_items=[];self.inventory={};app=FastAPI()
        test=self
        class Store:
            def scoped(self,ctx):return self
            async def get(self,key):return test.data.get(key)
            async def set(self,key,value,**kwargs):test.data[key]=value
        class Routes:
            def mount(self,ctx,router,**kwargs):app.include_router(router)
            async def sign(self,ctx,path,params,**kwargs):return 'https://example.test'+path+'?'+urlencode(params)
        class Ops:
            async def client(self,ctx):return self
            async def call(self,name,args,**kwargs):
                test.actions.append(name);test.arguments.append((name,args))
                if name=='search.library':return {'items':test.library_items}
                if name=='library.items.get':return test.inventory
                if name=='search.titles' and args['query']=='没有的电影':return {'titles':[]}
                if name=='search.titles' and test.remote_titles is not None:return {'titles':test.remote_titles}
                if name=='search.titles':return {'titles':[{'title':'测试电影','title_ref':'tmdb:movie:123','media_type':'movie','release_year':2020}]}
                if name=='discover.get-title-details':return {'title':{'title_ref':args['title_ref'],'media_type':'tv' if ':tv:' in args['title_ref'] else 'movie'},'library_links':[]}
                if name=='subscriptions.create':return {'subscription':{'id':9}}
                raise AssertionError(name)
        class Agent:
            def scoped(self,ctx):return self
            async def ask(self,*args,**kwargs):raise RuntimeError('no model')
        instances={'plugin-data':Store(),'plugin-routes':Routes(),'host-ops':Ops(),'agent':Agent()}
        class Context:
            def use(self,key):return instances[key.id if hasattr(key,'id') else str(key)]
            def effect(self,callback,**kwargs):test.effects.append(callback)
        # Contract keys use .name; map by the real key objects instead of assumptions.
        from shortcut_backend import PLUGIN_DATA,PLUGIN_ROUTES,HOST_OPS,AGENT
        mapping={PLUGIN_DATA:instances['plugin-data'],PLUGIN_ROUTES:instances['plugin-routes'],HOST_OPS:instances['host-ops'],AGENT:instances['agent']}
        ctx=Context();ctx.use=lambda key:mapping[key]
        async def publish(*args):test.published.append(args)
        with patch('shortcut_backend.net.http_transport',return_value=httpx.MockTransport(lambda request:httpx.Response(404))):
            await mount_shortcut(ctx,SimpleNamespace(publish=publish),'found',FoundMovie,movie_snapshot)
        self.client=httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='https://example.test')
        pair=(await self.client.post('/shortcut/pair')).json();self.search_url=pair['search_url']

    async def asyncTearDown(self):
        await self.client.aclose()
        for callback in self.effects:await callback()

    async def search(self,query="测试电影"):
        result=await self.client.post(self.search_url,json={'query':query})
        self.assertEqual(result.status_code,200)
        self.assertIn('no-store',result.headers['cache-control'])
        vc=result.json()['vcard'].replace('\r\n ','')
        return result.json(),re.findall(r'^URL:(.+)',vc,re.M)

    async def test_pair_is_private_no_store_and_rejects_cross_site(self):
        result=await self.client.post('/shortcut/pair',json={})
        self.assertEqual(result.status_code,200)
        self.assertIn('no-store',result.headers['cache-control'])
        self.assertEqual(result.json()['search_url'],self.search_url)
        blocked=await self.client.post('/shortcut/pair',json={},headers={'sec-fetch-site':'cross-site'})
        self.assertEqual(blocked.status_code,403)
        self.assertEqual((await self.client.get('/shortcut/pair')).status_code,405)

    def personal_payload(self):
        data=(Path(__file__).resolve().parents[1]/'web/downloads/find-movie-share.shortcut').read_bytes()
        return {'configuration_sha256':hashlib.sha256(self.search_url.encode()).hexdigest(),
                'file_sha256':hashlib.sha256(data).hexdigest(),
                'signed_file_base64':base64.b64encode(data).decode()},data

    async def test_personal_download_is_private_and_revocation_invalidates_it(self):
        self.assertFalse((await self.client.get('/shortcut/personal/status')).json()['available'])
        self.assertEqual((await self.client.get('/shortcut/personal/download')).status_code,409)
        payload,data=self.personal_payload()
        self.assertEqual((await self.client.post('/shortcut/personal/prepare',json=payload)).status_code,200)
        downloaded=await self.client.get('/shortcut/personal/download')
        self.assertEqual(downloaded.content,data)
        self.assertIn('no-store',downloaded.headers['cache-control'])
        self.assertIn('attachment;',downloaded.headers['content-disposition'])
        self.assertNotIn('signed_file_base64',(await self.client.get('/shortcut/personal/status')).text)
        await self.client.post('/shortcut/revoke')
        self.assertEqual((await self.client.get('/shortcut/personal/download')).status_code,409)
        self.assertIsNone(self.data.get('personal-shortcut-v1'))

    async def test_personal_prepare_rejects_wrong_config_corruption_and_cross_site(self):
        payload,_=self.personal_payload()
        self.assertEqual((await self.client.post('/shortcut/personal/prepare',json={**payload,'configuration_sha256':'0'*64})).status_code,409)
        self.assertEqual((await self.client.post('/shortcut/personal/prepare',json={**payload,'file_sha256':'0'*64})).status_code,400)
        self.assertEqual((await self.client.post('/shortcut/personal/prepare',json=payload,headers={'sec-fetch-site':'cross-site'})).status_code,403)
        self.assertFalse((await self.client.get('/shortcut/personal/status')).json()['available'])

    async def test_tools_survive_zero_results_and_have_no_write_action(self):
        body,urls=await self.search('没有的电影')
        self.assertEqual(body['count'],0)
        self.assertEqual(len(urls),5)
        for url,kind in zip(urls,['voice','manual','clues','help','exit']):
            selected=await self.client.get(url.strip())
            self.assertEqual(selected.json(),{'kind':kind})
        self.assertNotIn('subscriptions.create',self.actions)
        self.assertEqual(self.published,[])
        for line in body['vcard'].split('\r\n'):self.assertLessEqual(len(line.encode()),72)

    async def test_confirm_gate_idempotence_and_revoke(self):
        _,urls=await self.search()
        selected=(await self.client.get(urls[0].strip())).json()
        self.assertEqual(selected['kind'],'movie')
        self.assertFalse(selected['owned'])
        self.assertEqual((await self.client.post(selected['action_url'],json={'confirm':False})).status_code,400)
        self.assertNotIn('subscriptions.create',self.actions)
        for _ in range(2):self.assertEqual((await self.client.post(selected['action_url'],json={'confirm':True})).status_code,200)
        self.assertEqual(self.actions.count('subscriptions.create'),1)
        await self.client.post('/shortcut/revoke')
        self.assertEqual((await self.client.get(urls[0].strip())).status_code,404)


    async def test_capability_responses_are_not_cacheable(self):
        body,urls=await self.search()
        response=await self.client.get(urls[0].strip())
        self.assertIn('no-store',response.headers['cache-control'])
        self.assertIn('no-store',(await self.client.get(urls[-1].strip())).headers['cache-control'])
        self.assertIn('no-store',(await self.client.post('/shortcut/revoke')).headers['cache-control'])

    async def test_cross_site_cannot_revoke_existing_authorization(self):
        denied=await self.client.post('/shortcut/revoke',headers={'sec-fetch-site':'cross-site'})
        self.assertEqual(denied.status_code,403)
        self.assertEqual((await self.client.post('/shortcut/pair')).json()['search_url'],self.search_url)


    async def test_owned_tv_can_be_selected_and_notified_but_never_subscribed(self):
        self.library_items=[{'item':{'kind':'tv','title':'库内剧集','year':2026,'tmdb_id':101,'library_id':3,'media_item_id':42}}]
        self.inventory={'kind':'tv','title':'库内剧集','year':2026,'tmdb_id':101,'media_item_id':42,'file_count':34}
        body,urls=await self.search('库内剧集')
        self.assertIn('剧集',body['vcard'])
        selected=(await self.client.get(urls[0].strip())).json()
        self.assertTrue(selected['owned'])
        self.assertIn('剧集',selected['name'])
        self.assertTrue(selected['web_url'].endswith('/library/3/item/42'))
        self.assertIn('/shortcut/notify?',selected['action_url'])
        denied=await self.client.post(selected['action_url'].replace('/notify?','/subscribe?'),json={'confirm':True})
        self.assertEqual(denied.status_code,200)
        self.assertIn('已经入库',denied.json()['message'])
        self.assertNotIn('subscriptions.create',self.actions)
        self.assertEqual((await self.client.post(selected['action_url'],json={'confirm':True})).status_code,200)
        self.assertEqual(self.published[0][1].media.kind,'tv')

    async def test_missing_tv_requires_explicit_seasons_then_confirmation(self):
        self.remote_titles=[{'title':'测试剧集','title_ref':'tmdb:tv:123','media_type':'tv','release_year':2026}]
        body,urls=await self.search('测试剧集')
        self.assertEqual(body['count'],1)
        selected=(await self.client.get(urls[0].strip())).json()
        self.assertEqual(selected['needs_seasons'],'yes')
        self.assertEqual((await self.client.post(selected['action_url'],json={'confirm':True})).status_code,400)
        self.assertNotIn('subscriptions.create',self.actions)
        self.assertEqual((await self.client.post(selected['seasons_url'],json={'seasons':'全部'})).status_code,400)
        planned=await self.client.post(selected['seasons_url'],json={'seasons':'第一季和第二季'})
        self.assertEqual(planned.status_code,200)
        self.assertIn('第1季、第2季',planned.json()['message'])
        self.assertNotIn('subscriptions.create',self.actions)
        url=planned.json()['action_url']
        self.assertEqual((await self.client.post(url,json={'confirm':False})).status_code,400)
        for _ in range(2):self.assertEqual((await self.client.post(url,json={'confirm':True})).status_code,200)
        calls=[args for name,args in self.arguments if name=='subscriptions.create']
        self.assertEqual(calls,[{'title_ref':'tmdb:tv:123','selected_seasons':[1,2],'follow_future':False}])

    async def test_season_plans_have_separate_tickets_so_confirmation_cannot_change(self):
        self.remote_titles=[{'title':'测试剧集','title_ref':'tmdb:tv:123','media_type':'tv','release_year':2026}]
        _,urls=await self.search('测试剧集')
        selected=(await self.client.get(urls[0].strip())).json()
        first=(await self.client.post(selected['seasons_url'],json={'seasons':'1'})).json()
        second=(await self.client.post(selected['seasons_url'],json={'seasons':'2'})).json()
        self.assertNotEqual(first['action_url'],second['action_url'])
        await self.client.post(first['action_url'],json={'confirm':True})
        args=next(args for name,args in self.arguments if name=='subscriptions.create')
        self.assertEqual(args['selected_seasons'],[1])
