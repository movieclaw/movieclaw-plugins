"""快捷指令专用能力链接：只查电影、确认订阅和向自己的 App 发观看入口。"""
import asyncio
import base64
import re
import secrets
import time
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from movieclaw_sdk import net
from movieclaw_sdk.ops import HOST_OPS
from movieclaw_sdk.agent import AGENT
from movie_match import find_movies
from movieclaw_api.plugins.keys import PLUGIN_ROUTES, PLUGIN_DATA


class SearchMovie(BaseModel):
    model_config = ConfigDict(extra='forbid')
    query: str = Field(min_length=1, max_length=180)
    clues: str = Field(default="", max_length=300)


class Confirmation(BaseModel):
    model_config = ConfigDict(extra='forbid')
    confirm: bool


def escape_vcard(value):
    return str(value or '').replace('\\','\\\\').replace('\n','\\n').replace('\r','').replace(';','\\;').replace(',','\\,')


def fold_vcard(line):
    # 以 UTF-8 字节数折行，中文片名也保持 vCard 3.0 的行宽约束。
    out=[];current='';size=0
    for char in line:
        length=len(char.encode('utf-8'))
        if size+length>72:
            out.append(current);current=' ';size=1
        current+=char;size+=length
    out.append(current)
    return '\r\n'.join(out)


def card(row, url, photo=''):
    label=f"{row['title']}（{row.get('release_year') or '年份待确认'}）"
    lines=['BEGIN:VCARD','VERSION:3.0','FN:'+escape_vcard(label),
           'N:;'+escape_vcard(label)+';;;', 'ORG:'+('已在媒体服务器上' if row.get('owned') else '确认后检查库存与订阅'),
           'URL:'+url, 'NOTE:'+escape_vcard(url)]
    if photo:lines.append('PHOTO;ENCODING=b;TYPE=JPEG:'+photo)
    return '\r\n'.join(fold_vcard(line) for line in lines+['END:VCARD'])+'\r\n'



async def mount_shortcut(ctx,publisher,found_event,found_model,snapshot):
    """签名能力按版本可吊销；每次候选的操作票据只存活十分钟。"""
    routes=ctx.use(PLUGIN_ROUTES);store=ctx.use(PLUGIN_DATA).scoped(ctx)
    epoch=await store.get('shortcut-epoch')
    if not epoch:
        epoch=secrets.token_urlsafe(18);await store.set('shortcut-epoch',epoch,secret=True)
    tickets={}; gate=asyncio.Lock();last_search=0.0
    cache={}
    agent=ctx.use(AGENT).scoped(ctx)
    client=httpx.AsyncClient(transport=net.http_transport('movie-voice'),timeout=5,follow_redirects=False)
    ctx.effect(client.aclose,label='close-movie-voice-images')

    async def ops(name,args):
        return await (await ctx.use(HOST_OPS).client(ctx)).call(name,args,timeout=10 if name.startswith("search.") else 25)

    async def signed(path,**params):
        return await routes.sign(ctx,path,params={'epoch':epoch,**params},absolute=True)

    def check(request):
        if request.query_params.get('epoch')!=epoch:raise HTTPException(404,'快捷指令授权已失效，请重新安装')
        if request.headers.get('sec-fetch-site')=='cross-site':raise HTTPException(403,'请使用自己的快捷指令')

    def ticket_for(request):
        check(request);ticket=request.query_params.get('ticket','');item=tickets.get(ticket)
        if item is None or item['expires']<time.monotonic():raise HTTPException(410,'候选已过期，请重新搜索')
        return item

    async def photo(row):
        # 只取固定 TMDB 小图地址；不接受任意 URL、不跟随跳转、不转发认证。
        raw=row.get('poster_url','');parts=urlsplit(raw)
        if parts.scheme!='https' or parts.netloc!='image.tmdb.org' or not re.fullmatch(r'/t/p/(?:w\d+|original)/[A-Za-z0-9]+\.jpg',parts.path):return ''
        path=re.sub(r'/t/p/(?:w\d+|original)/','/t/p/w92/',parts.path)
        try:
            async with client.stream('GET','https://image.tmdb.org'+path) as response:
                if response.status_code!=200 or 'image/jpeg' not in response.headers.get('content-type',''):return ''
                data=bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data)>200000:return ''
                return base64.b64encode(data).decode() if data[:2]==b'\xff\xd8' else ''
        except Exception:return ''

    def no_store(response:Response):
        # 候选含临时能力链接，不允许代理或浏览器缓存。
        response.headers['Cache-Control']='private, no-store'
        response.headers['Pragma']='no-cache'

    admin=APIRouter(dependencies=[Depends(no_store)])
    public=APIRouter(dependencies=[Depends(no_store)])

    @admin.post('/shortcut/pair',operation_id='plugins.movie-voice.shortcut-pair',summary='生成仅限找电影的快捷指令能力链接')
    async def pair(request:Request):
        if request.headers.get('sec-fetch-site')=='cross-site':raise HTTPException(403,'请在自己的 MovieClaw 插件页面生成')
        return JSONResponse({'search_url':await signed('/shortcut/search'),'revocable':True},
                            headers={'Cache-Control':'private, no-store','Pragma':'no-cache'})

    @admin.post('/shortcut/revoke',operation_id='plugins.movie-voice.shortcut-revoke',summary='吊销所有已生成的找电影快捷指令能力链接')
    async def revoke(request:Request):
        nonlocal epoch
        if request.headers.get('sec-fetch-site')=='cross-site':
            raise HTTPException(403,'请从自己的服务器撤销授权')
        epoch=secrets.token_urlsafe(18);tickets.clear();await store.set('shortcut-epoch',epoch,secret=True)
        await store.set('personal-shortcut-v1',None,secret=True)
        return {'revoked':True}

    @public.post('/shortcut/search',operation_id='plugins.movie-voice.shortcut-search')
    async def search(payload:SearchMovie,request:Request):
        nonlocal last_search
        check(request)
        async with gate:
            if last_search>time.monotonic()-2:raise HTTPException(429,'请稍候再搜索')
            last_search=time.monotonic()
            raw=payload.query.strip()
            if not raw:raise HTTPException(400,'请说出或输入电影名')
            key=(raw,payload.clues.strip())
            cached=cache.get(key)
            if cached and cached[0]>time.monotonic():
                found=cached[1]
            else:
                try:found=await find_movies(ops,agent,raw,payload.clues.strip())
                except RuntimeError as error:raise HTTPException(503,str(error)) from None
                if len(cache)>=32:cache.clear()
                cache[key]=(time.monotonic()+180,found)
            rows=found['rows']
            now=time.monotonic()
            for key in list(tickets):
                if tickets[key]['expires']<now:del tickets[key]
            if len(tickets)>120:tickets.clear()
            photos=await asyncio.gather(*(photo(row) for row in rows))
            cards=[]
            for row,picture in zip(rows,photos):
                token=secrets.token_urlsafe(24);tickets[token]={'row':row,'expires':now+600,'subscribed':None,'notified':False}
                url=await signed('/shortcut/select',ticket=token)
                cards.append(card(row,url,picture))
            for control,label in [('voice','重新说一次'),('manual','手动输入电影名'),('clues','补充年份、导演、演员或剧情'),('help','配置说明与使用帮助'),('exit','结束找电影')]:
                token=secrets.token_urlsafe(24);tickets[token]={'control':control,'expires':now+600}
                url=await signed('/shortcut/select',ticket=token)
                cards.append('\r\n'.join(fold_vcard(line) for line in ['BEGIN:VCARD','VERSION:3.0','FN:'+label,'N:;'+label+';;;','ORG:搜索工具','URL:'+url,'END:VCARD'])+'\r\n')
            return {'count':len(rows),'vcard':''.join(cards),'message':found['message'],'ai_state':found['ai_state'],
                    'poster_count':sum(bool(p) for p in photos)}

    async def resolve(item):
        row=item['row']
        if row.get('owned'):
            fresh=await ops('library.items.get',{'library_id':row['library_id'],'media_item_id':row['media_item_id']})
            media=snapshot(fresh,row['media_item_id'])
            return row,media
        details=await ops('discover.get-title-details',{'title_ref':row['title_ref']})
        if details.get('title',{}).get('title_ref')!=row['title_ref']:raise HTTPException(503,'影片身份暂时无法确认')
        links=details.get('library_links') or []
        if details['title'].get('library_status',{}):
            if details['title']['library_status'].get('file_count',0)>0 and links:
                row={**row,**links[0],'owned':True};item['row']=row
                fresh=await ops('library.items.get',{'library_id':row['library_id'],'media_item_id':row['media_item_id']})
                return row,snapshot(fresh,row['media_item_id'])
        return row,None

    @public.get('/shortcut/select',operation_id='plugins.movie-voice.shortcut-select')
    async def select(request:Request):
        item=ticket_for(request)
        if item.get('control'):return {'kind':item['control']}
        try:row,media=await resolve(item)
        except Exception:raise HTTPException(503,'暂时无法核对库存，尚未执行订阅') from None
        token=request.query_params['ticket'];name=f"{row['title']}（{row.get('release_year') or '年份待确认'}）"
        return {'kind':'movie','owned':bool(media),'name':name,
                'message':name+('已在媒体服务器上。是否发送到 MovieClaw App？' if media else '还没有入库。是否按默认规则订阅并自动下载？'),
                'action_url':await signed('/shortcut/notify' if media else '/shortcut/subscribe',ticket=token),
                'web_url':'{uri.scheme}://{uri.netloc}'.format(uri=urlsplit(await signed('/shortcut/search')))+f"/library/{row['library_id']}/item/{row['media_item_id']}" if media else ''}

    @public.post('/shortcut/subscribe',operation_id='plugins.movie-voice.shortcut-subscribe')
    async def subscribe(payload:Confirmation,request:Request):
        item=ticket_for(request)
        if item.get('control'):raise HTTPException(400,'请选择电影')
        if not payload.confirm:raise HTTPException(400,'需要先确认影片')
        async with gate:
            if item['subscribed']:return {'message':'这部电影已提交订阅，请等待服务器自动搜索与入库。'}
            try:
                row,media=await resolve(item)
                if media:return {'message':f"{media.title}已经入库，无需再订阅。请重新选择，获取观看通知入口。"}
                result=await ops('subscriptions.create',{'title_ref':row['title_ref']})
                item['subscribed']=result['subscription']['id']
            except Exception:raise HTTPException(503,'订阅未能确认成功。请先在 MovieClaw 查看订阅状态，再重试。') from None
            return {'message':f"{row['title']}已加入订阅，将按现有规则自动搜索、下载并入库。找到资源后会通过原有通知提醒你。"}

    @public.post('/shortcut/notify',operation_id='plugins.movie-voice.shortcut-notify')
    async def notify(payload:Confirmation,request:Request):
        item=ticket_for(request)
        if item.get('control'):raise HTTPException(400,'请选择电影')
        if not payload.confirm:raise HTTPException(400,'需要先确认影片')
        async with gate:
            if item['notified']:return {'message':'已发送观看通知，请查看手机通知。'}
            try:
                row,media=await resolve(item)
                if not media:raise ValueError()
                await publisher.publish(found_event,found_model(media=media));item['notified']=True
            except Exception:raise HTTPException(503,'影片暂不可用，未发送通知。') from None
        return {'message':f"{media.title}的观看通知已发送，点击通知即可打开 MovieClaw 中的影片。"}

    from personal_download import mount_personal
    mount_personal(admin,store,signed)
    routes.mount(ctx,admin,zone='admin');routes.mount(ctx,public,zone='public')
