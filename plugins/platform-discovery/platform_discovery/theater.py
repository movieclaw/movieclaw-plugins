"""Account-scoped library display preferences and public artwork selection."""
from __future__ import annotations
import asyncio
import copy
import json
import math
import re
import time
import uuid
from urllib.parse import urlsplit
from .artwork_cache import digest

TMDB = 'https://api.themoviedb.org/3'
COUNTRIES = re.compile(r'^[A-Z]{2}(?:,[A-Z]{2}){0,26}$')
DEFAULTS = {'kind':'movie','library':'','collection':'','g':'','c':'','d':'','w':'','res':'','rating_gte':'','sort':'added_at'}


def normalize_filters(value):
    if not isinstance(value, dict) or set(value) - set(DEFAULTS):
        raise ValueError('筛选条件不正确')
    value = {**DEFAULTS, **value}
    if any(not isinstance(v,str) or len(v)>240 for v in value.values()):
        raise ValueError('筛选条件不正确')
    allowed = {'kind':{'movie','tv'}, 'sort':{'title','added_at','rating','release_date','last_played'},
               'w':{'','unwatched','watching','played','favorite'}}
    for k, options in allowed.items():
        if value[k] not in options:
            raise ValueError('筛选条件不正确')
    for k in ('library','collection'):
        if value[k] and not re.fullmatch(r'[1-9]\d{0,9}',value[k]):
            raise ValueError('媒体库编号不正确')
    for k, pattern in [('g',r'\d{1,6}(?:,\d{1,6}){0,24}'),('c',COUNTRIES.pattern),
                       ('d',r'(?:\d{3}0s|earlier)(?:,(?:\d{3}0s|earlier)){0,15}'),
                       ('res',r'[A-Za-z0-9]{1,12}(?:,[A-Za-z0-9]{1,12}){0,10}'),
                       ('rating_gte',r'(?:10|[0-9](?:\.\d)?)')]:
        if value[k] and not re.fullmatch(pattern,value[k]):
            raise ValueError('筛选条件不正确')
    return value


def public_image(url):
    try:
        p = urlsplit(url)
        host = p.hostname or ''
        return (isinstance(url,str) and len(url)<2048 and p.scheme=='https' and
                host in {'image.tmdb.org','assets.fanart.tv'} and not p.username and not p.password
                and p.port in {None,443} and not p.query and not p.fragment and
                not any(ord(c)<33 or c=='\\' for c in url))
    except (TypeError,ValueError):
        return False


def number(value, default=0):
    try:
        f=float(value)
        return f if math.isfinite(f) else default
    except (ValueError,TypeError):
        return default


def candidate(url, source, language='', width=1000, height=1500, votes=0, popularity=0):
    width,height=number(width),number(height)
    if not public_image(url) or height<=0 or not .55<=width/height<=.78 or width<200:
        return None
    language=str(language or '')[:12]
    # Votes/likes describe artwork, never the title's film rating.
    score=({'zh':100,'cn':100,'en':45,'':20,'00':20}.get(language,10)
           + min(width,2000)/100 + min(number(votes),100)*.15
           + min(number(popularity),10)*min(number(votes)/5,1))
    return {'id':digest(url)[:24], 'url':url, 'source':source, 'language':language,
            'width':int(width),'height':int(height),'votes':int(max(0,number(votes))), 'score':round(score,2)}


class TheaterService:
    def __init__(self, store, discovery, get_ops, cache, config):
        self.store,self.discovery,self.get_ops,self.cache,self.config=store,discovery,get_ops,cache,config
        self._locks={}; self._metadata={}; self._metadata_tasks={}; self._jobs={}; self._gate=asyncio.Semaphore(3)

    def scope(self, identity):
        return 'theater:' + digest(identity)[:40]

    def lock(self, identity):
        key=self.scope(identity)
        if key not in self._locks:
            self._locks[key]=asyncio.Lock()
        return self._locks[key]

    async def preferences(self, identity):
        record=await self.store.get('filters-v1',scope=self.scope(identity))
        return record if isinstance(record,dict) else {'revision':'','filters':copy.deepcopy(DEFAULTS)}

    async def save_preferences(self, identity, payload):
        if not isinstance(payload,dict) or set(payload)!={'revision','filters'} or not isinstance(payload['revision'],str):
            raise ValueError('筛选格式不正确')
        filters=normalize_filters(payload['filters'])
        async with self.lock(identity):
            old=await self.preferences(identity)
            if old['revision']!=payload['revision']:
                error=ValueError('另一设备已更改录像厅筛选，请重新载入');error.status=409;raise error
            record={'revision':uuid.uuid4().hex[:24],'filters':filters}
            await self.store.set('filters-v1',record,scope=self.scope(identity),secret=True)
            return record

    async def choice(self, identity, item):
        row=await self.store.get('art-'+str(item),scope=self.scope(identity))
        return row if isinstance(row,dict) else {'revision':'','locked':False,'key':None}

    def token(self, name):
        value=getattr(self.config,name,None)
        return value.get_secret_value().strip() if value and hasattr(value,'get_secret_value') else ''

    async def metadata(self, item, selected=None):
        kind = item['kind']; tmdb_id = selected or item.get('tmdb_id')
        if not tmdb_id:
            return {'candidates':[], 'matches':[], 'message':'没有可靠的作品编号；可按片名搜索并确认匹配。'}
        key=(kind,int(tmdb_id))
        cached=self._metadata.get(key)
        if cached and cached[0]>time.monotonic():
            return copy.deepcopy(cached[1])
        if key not in self._metadata_tasks:
            self._metadata_tasks[key]=asyncio.create_task(self._metadata_fetch(kind,int(tmdb_id)))
        task=self._metadata_tasks[key]
        try:
            result=await asyncio.shield(task)
            self._metadata[key]=(time.monotonic()+3600,copy.deepcopy(result))
            if len(self._metadata)>512:
                self._metadata.pop(next(iter(self._metadata)))
            return result
        finally:
            if task.done() and self._metadata_tasks.get(key) is task:
                self._metadata_tasks.pop(key,None)

    async def _metadata_fetch(self, kind, tmdb_id):
        rows=[];messages=[];ext={};token=bool(getattr(self.discovery,'_token',''))
        async with self._gate:
            if token:
                try:
                    data=await self.discovery._json(f'{TMDB}/{kind}/{tmdb_id}/images',params={'include_image_language':'zh,en,null'},tmdb=True)
                    for row in data.get('posters',[])[:120]:
                        path=row.get('file_path','')
                        if re.fullmatch(r'/[A-Za-z0-9]+\.(?:jpg|png|webp)',path):
                            c=candidate('https://image.tmdb.org/t/p/original'+path,'TMDB',row.get('iso_639_1'),row.get('width'),row.get('height'),row.get('vote_count'),row.get('vote_average'))
                            if c: rows.append(c)
                except Exception:
                    messages.append('TMDB 选图暂不可用，保留已有海报')
            else:
                try:
                    ops=await self.get_ops()
                    details=await ops.call('discover.get-title-details',{'title_ref':f'tmdb:{kind}:{tmdb_id}'},timeout=12)
                    if isinstance(details.get('data'),dict): details=details['data']
                    for row in details.get('posters',[])[:60]:
                        url=row.get('full_url') or row.get('preview_url')
                        c=candidate(url,'TMDB',width=row.get('width') or 1000,height=row.get('height') or 1500)
                        if c: rows.append(c)
                except Exception:
                    messages.append('宿主资料暂不可用，可配置插件 TMDB 令牌')
            fanart=self.token('fanart_api_key')
            if fanart:
                try:
                    foreign=tmdb_id
                    if kind=='tv':
                        if not token:
                            raise ValueError('剧集 Fanart 需要 TMDB 外部编号映射')
                        ext=await self.discovery._json(f'{TMDB}/tv/{tmdb_id}/external_ids',tmdb=True)
                        foreign=ext.get('tvdb_id')
                        if not isinstance(foreign,int) or foreign<=0:
                            raise ValueError('缺少 TVDB 编号')
                    params={'api_key':fanart}
                    if self.token('fanart_client_key'): params['client_key']=self.token('fanart_client_key')
                    data=await self.discovery._json(f'https://webservice.fanart.tv/v3/{"movies" if kind=="movie" else "tv"}/{foreign}',params=params)
                    for row in data.get('movieposter' if kind=='movie' else 'tvposter',[])[:100]:
                        c=candidate(row.get('url'),'Fanart.tv',row.get('lang'),row.get('width') or 1000,row.get('height') or 1500,row.get('likes'))
                        if c:rows.append(c)
                except Exception:
                    messages.append('Fanart.tv 暂不可用或缺少 TVDB 映射')
            else:
                messages.append('Fanart.tv 未配置 API Key；当前使用 TMDB')
        unique={r['id']:r for r in rows}
        result=sorted(unique.values(),key=lambda c:(-c['score'],c['id']))[:40]
        return {'candidates':result,'matches':[],'message':'；'.join(messages)}

    async def search(self, item):
        if not getattr(self.discovery,'_token',''):
            return []
        data=await self.discovery._json(f'{TMDB}/search/{item["kind"]}',params={'query':item['title'],'language':'zh-CN'},tmdb=True)
        result=[]
        for row in data.get('results',[])[:8]:
            if not isinstance(row.get('id'),int):continue
            result.append({'id':row['id'],'title':str(row.get('title') or row.get('name') or '')[:300],
                           'year':str(row.get('release_date') or row.get('first_air_date') or '')[:4]})
        return result

    async def art(self, identity, item):
        saved=await self.choice(identity,item['id'])
        current=await self.cache.get(saved.get('key','')) if saved.get('key') else None
        return {'revision':saved.get('revision',''),'locked':saved.get('locked',False),
                'key':current['key'] if current else None,'selected':current,
                'tmdb_id':saved.get('tmdb_id') or item.get('tmdb_id'),
                'default':saved.get('default',False),'fanart_configured':bool(self.token('fanart_api_key'))}

    async def optimize(self, identity, item, action=None):
        action=action or {'operation':'auto'}
        if not isinstance(action,dict) or set(action)-{'operation','revision','candidate_id','tmdb_id'}:
            raise ValueError('选图参数不正确')
        operation=action.get('operation')
        if not isinstance(operation,str) or operation not in {'auto','choose','reset','match','refresh'}:
            raise ValueError('选图操作不正确')
        async with self.lock(identity+":art:"+str(item['id'])):
            previous=await self.choice(identity,item['id'])
            if operation!='auto' and action.get('revision')!=previous.get('revision',''):
                error=ValueError('另一设备已更改海报，请重新打开选图');error.status=409;raise error
            if operation=='reset':
                await self.cache.pin(self.scope(identity),str(item['id']),None)
                saved={'revision':uuid.uuid4().hex[:24],'locked':False,'key':None,'default':True}
                await self.store.set('art-'+str(item['id']),saved,scope=self.scope(identity),secret=True)
                return await self.art(identity,item)
            if operation=='auto' and (previous.get('locked') or (previous.get('key') and await self.cache.get(previous['key']))):
                return await self.art(identity,item)
            selected=previous.get('tmdb_id')
            if operation=='refresh':
                tmdb_id=selected or item.get('tmdb_id')
                if tmdb_id:self._metadata.pop((item['kind'],int(tmdb_id)),None)
            if operation=='match':
                matches=await self.search(item)
                if type(action.get('tmdb_id')) is not int or action.get('tmdb_id') not in {r['id'] for r in matches}:
                    raise ValueError('请从搜索结果中确认作品')
                selected=action['tmdb_id']
            metadata=await self.metadata(item,selected)
            rows=metadata['candidates']
            if operation=='choose':
                rows=[row for row in rows if row['id']==action.get('candidate_id')]
            if not rows:
                return {**await self.art(identity,item),'message':metadata['message'] or '没有适合竖版海报的图片，保留原海报'}
            row=None
            for c in rows[:3]:
                try:
                    row=await self.cache.ensure(c,self.discovery._client);break
                except Exception:
                    pass
            if row is None:
                raise ValueError('海报下载失败或缓存已满，原有海报保留，请稍后重试')
            saved={'revision':uuid.uuid4().hex[:24], 'locked':operation in {'choose','match'},'key':row['key'],'tmdb_id':selected}
            await self.cache.pin(self.scope(identity),str(item['id']),row['key'] if saved['locked'] else None)
            await self.store.set('art-'+str(item['id']),saved,scope=self.scope(identity),secret=True)
            return {**await self.art(identity,item),'message':metadata['message']}

    async def close(self):
        tasks=[*self._metadata_tasks.values(),*self._jobs.values()]
        for task in tasks:task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)
        await self.cache.close()
