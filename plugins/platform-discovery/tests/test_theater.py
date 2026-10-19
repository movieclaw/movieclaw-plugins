"""Inventory permission boundary, correct artwork identity, persistence and bounded cache."""
import asyncio
import copy
import io
import tempfile
import types
import unittest
from pathlib import Path
from PIL import Image
import httpx
from fastapi import FastAPI, HTTPException, Request
from platform_discovery.artwork_cache import ArtworkCache, digest
from platform_discovery.theater import TheaterService, candidate, normalize_filters, public_image
from platform_discovery.theater_routes import build_theater_router

class Store:
    def __init__(self):self.values={}
    async def get(self,key,*,scope='global',default=None):return copy.deepcopy(self.values.get((scope,key),default))
    async def set(self,key,value,*,scope='global',secret=False):self.values[(scope,key)]=copy.deepcopy(value)

class TheaterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.store=Store();self.cache=ArtworkCache(self.temp.name)
        self.item={'id':1,'kind':'movie','tmdb_id':550,'title':'Fight Club','year':1999};self.calls=[]
        buffer=io.BytesIO();Image.new('RGB',(400,600),'navy').save(buffer,'JPEG');self.image=buffer.getvalue()
        async def fetch(request):
            self.calls.append(str(request.url));await asyncio.sleep(.005)
            return httpx.Response(200,content=self.image,headers={'content-type':'image/jpeg'})
        self.client=httpx.AsyncClient(transport=httpx.MockTransport(fetch))
        async def metadata(url,params=None,tmdb=False):
            if url.endswith('/external_ids'):return {'tvdb_id':1234}
            if 'fanart.tv' in url:return {'movieposter':[{'url':'https://assets.fanart.tv/fanart/poster.jpg','lang':'zh','likes':'10'}]}
            return {'posters':[{'file_path':'/Chinese.jpg','iso_639_1':'zh','width':1000,'height':1500,'vote_count':3,'vote_average':8},{'file_path':'/English.jpg','iso_639_1':'en','width':1600,'height':2400,'vote_count':100,'vote_average':9}]}
        self.source=types.SimpleNamespace(_token='server-only-token',_client=self.client,_json=metadata)
        self.config=types.SimpleNamespace(fanart_api_key=None,fanart_client_key=None)
        async def ops():raise ValueError('no fallback needed')
        self.service=TheaterService(self.store,self.source,ops,self.cache,self.config)
    async def asyncTearDown(self):
        await self.service.close();await self.client.aclose();self.temp.cleanup()

    async def test_auto_select_uses_art_votes_language_exact_id_and_cache_reuse(self):
        result=await self.service.optimize('a',self.item)
        self.assertEqual(result['selected']['language'],'zh');self.assertEqual(self.cache.downloads,1)
        second=await self.service.optimize('a',self.item)
        self.assertEqual(result['key'],second['key']);self.assertEqual(len(self.calls),1)
        reopened=ArtworkCache(self.temp.name)
        self.assertEqual((await reopened.get(result['key']))['source'],'TMDB')
        self.assertEqual((await reopened.stats())['images'],1)

    async def test_manual_lock_cas_failed_choice_reset_and_account_isolation(self):
        initial=await self.service.optimize('a',self.item);data=await self.service.metadata(self.item)
        english=next(row for row in data['candidates'] if row['language']=='en')
        chosen=await self.service.optimize('a',self.item,{'operation':'choose','candidate_id':english['id'],'revision':initial['revision']})
        self.assertTrue(chosen['locked']);self.assertEqual((await self.cache.stats())['pinned'],1)
        auto=await self.service.optimize('a',self.item);self.assertEqual(chosen['key'],auto['key'])
        with self.assertRaises(ValueError):await self.service.optimize('a',self.item,{'operation':'reset','revision':initial['revision']})
        self.assertIsNone((await self.service.art('b',self.item))['key'])
        cleared=await self.service.optimize('a',self.item,{'operation':'reset','revision':chosen['revision']})
        self.assertIsNone(cleared['key']);self.assertTrue(cleared['default']);self.assertEqual((await self.cache.stats())['pinned'],0)

    async def test_download_singleflight_and_invalid_html_never_committed(self):
        c=candidate('https://image.tmdb.org/t/p/original/ok.jpg','TMDB')
        result=await asyncio.gather(*(self.cache.ensure(c,self.client) for _ in range(6)))
        self.assertEqual(len(self.calls),1);self.assertEqual(len({r['key'] for r in result}),1)
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req:httpx.Response(200,text='not an image',headers={'content-type':'text/html'}))) as broken:
            c=candidate('https://image.tmdb.org/t/p/original/broken.jpg','TMDB')
            with self.assertRaises(ValueError):await self.cache.ensure(c,broken)
            self.assertIsNone(await self.cache.get(digest(c['url'])))

    async def test_capacity_prune_keeps_pins_across_restarts(self):
        a=await self.cache.ensure(candidate('https://image.tmdb.org/a.jpg','TMDB'),self.client)
        await self.cache.pin('a','1',a['key']);await self.cache.ensure(candidate('https://image.tmdb.org/b.jpg','TMDB'),self.client)
        reopened=ArtworkCache(self.temp.name)
        self.assertEqual(await reopened.prune(),1);self.assertIsNotNone(await reopened.get(a['key']))
        reopened.max_bytes=10
        with self.assertRaises(ValueError):await reopened.ensure(candidate('https://image.tmdb.org/c.jpg','TMDB'),self.client)
        self.assertIsNotNone(await reopened.get(a['key']))

    async def test_filters_cas_account_separation_and_no_profile_rewrites(self):
        self.store.values[('profile','existing')]={'tests':[1,2],'watched':[4]}
        prefs=await self.service.save_preferences('a',{'revision':'','filters':{'kind':'tv','c':'JP,KR','d':'1990s,2000s'}})
        self.assertEqual((await self.service.preferences('a')),prefs)
        self.assertEqual((await self.service.preferences('b'))['filters']['kind'],'movie')
        with self.assertRaises(ValueError):await self.service.save_preferences('a',{'revision':'','filters':{}})
        self.assertEqual(self.store.values[('profile','existing')],{'tests':[1,2],'watched':[4]})
        for bad in [{'kind':'other'},{'c':'http://internal'},{'rating_gte':'11'},{'revision':'bad'}]:
            with self.assertRaises(ValueError):normalize_filters(bad)

    async def test_image_allowlist_and_portrait_validation(self):
        for url in ['http://image.tmdb.org/a.jpg','https://internal/a','https://image.tmdb.org@localhost/a','https://image.tmdb.org/a?token=secret','https://assets.fanart.tv/a#x']:
            self.assertFalse(public_image(url))
        self.assertIsNone(candidate('https://image.tmdb.org/a.jpg','TMDB',width=1920,height=1080))
        self.assertIsNotNone(candidate('https://assets.fanart.tv/a.jpg','Fanart.tv'))

    async def test_fanart_tv_uses_tvdb_mapping_not_tmdb_id(self):
        from pydantic import SecretStr
        self.config.fanart_api_key=SecretStr('private-key')
        urls=[];original=self.source._json
        async def tracked(url,params=None,tmdb=False):
            urls.append(url);return await original(url,params,tmdb)
        self.source._json=tracked
        await self.service.metadata({**self.item,'kind':'tv','tmdb_id':98765})
        self.assertIn('https://webservice.fanart.tv/v3/tv/1234',urls)
        self.assertNotIn('https://webservice.fanart.tv/v3/tv/98765',urls)

    async def test_routes_require_current_account_library_and_item_membership(self):
        async def account(request:Request):
            name=request.headers.get('x-test-user')
            if not name:raise HTTPException(401,'login')
            request.state.platform_account_identity=name;request.state.platform_account_label='member:'+name
        async def session():return None
        async def principal(library_id:int,request:Request):
            if library_id!=2 or request.headers.get('x-test-user')=='blocked':raise HTTPException(404,'not visible')
            return request.headers.get('x-test-user')
        async def admin(request:Request):
            if request.headers.get('x-test-user')!='admin':raise HTTPException(403,'admin only')
        async def visible(session,principal,item):
            if principal=='child' or item!=1:raise HTTPException(404,'not visible')
        async def rows(session,library,item):return types.SimpleNamespace(kind='movie',tmdb_id=550,title='Fight Club',year=1999),[]
        app=FastAPI();app.include_router(build_theater_router('platform-discovery',self.service,account,(session,admin,principal,rows,visible)))
        path='/theater/libraries/2/items/1'
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='https://mc.test') as client:
            self.assertEqual((await client.get(path+'/art')).status_code,401)
            for who in ['blocked','child']:
                self.assertEqual((await client.get(path+'/art',headers={'x-test-user':who})).status_code,404)
            headers={'x-test-user':'a'}
            response=await client.post(path+'/art',headers=headers,json={'operation':'auto'});self.assertEqual(response.status_code,200,response.text)
            key=response.json()['key'];image=await client.get(path+'/images/'+key,headers=headers)
            self.assertEqual(image.status_code,200);self.assertEqual(image.headers['x-poster-cache'],'HIT')
            reused=await client.get(path+'/images/'+key,headers={**headers,'if-none-match':image.headers['etag']})
            self.assertEqual(reused.status_code,304)
            self.assertEqual((await client.get(path+'/images/'+key,headers={'x-test-user':'b'})).status_code,404)
            self.assertEqual((await client.get('/theater/libraries/3/items/1/art',headers=headers)).status_code,404)
            self.assertEqual((await client.post(path+'/art',headers={**headers,'sec-fetch-site':'cross-site'},json={'operation':'reset'})).status_code,403)
            self.assertEqual((await client.post('/theater/preferences',headers=headers,content='x'*5000)).status_code,413)
            self.assertEqual((await client.get('/theater/cache',headers=headers)).status_code,403)
            self.assertEqual((await client.get(path+'/art',headers={**headers,'X-Platform-Account':'member:b'})).status_code,409)
