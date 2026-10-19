"""Library artwork routes use the host's current Principal and exact inventory membership."""
from __future__ import annotations
import json
import re
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from .personal_routes import PRIVATE_HEADERS, _browser_request


def build_theater_router(entry_id, theater, account_dependency, host_dependencies=None):
    if host_dependencies is None:
        from movieclaw_api.api.deps import require_admin
        from movieclaw_db.engine import get_session
        from movieclaw_api.api.routes.libraries import require_library_visible, _item_rows
        from movieclaw_api.services.library.access import assert_item_visible
    else:
        get_session,require_admin,require_library_visible,_item_rows,assert_item_visible=host_dependencies
    router=APIRouter(dependencies=[Depends(account_dependency)])
    op=f'plugins.{entry_id}.theater-'
    hidden={'x-cli-hidden':True}

    def owner(request):
        value=getattr(request.state,'platform_account_identity',None)
        label=getattr(request.state,'platform_account_label',None)
        if not value: raise HTTPException(403,'请先登录 MovieClaw')
        if request.headers.get('X-Platform-Account') not in {None,label}:
            raise HTTPException(409,'登录账号已切换，请刷新页面')
        return value

    async def body(request, limit=4096):
        _browser_request(request)
        raw=bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw)>limit:raise HTTPException(413,'请求内容过大')
        try:
            value=json.loads(raw)
            if not isinstance(value,dict):raise ValueError
            return value
        except (ValueError,UnicodeError):raise HTTPException(400,'请求格式不正确') from None

    async def visible(library_id:int, media_item_id:int, session=Depends(get_session), principal=Depends(require_library_visible)):
        await assert_item_visible(session,principal,media_item_id)
        item,rows=await _item_rows(session,library_id,media_item_id)
        if item.kind not in {'movie','tv'}:raise HTTPException(404,'仅支持电影与剧集')
        return {'id':media_item_id,'kind':str(item.kind),'tmdb_id':item.tmdb_id,'title':item.title,'year':getattr(item,'year',None)}

    def output(value):return JSONResponse(value,headers=PRIVATE_HEADERS)

    async def call(method,*args):
        try:return output(await method(*args))
        except ValueError as exc:
            raise HTTPException(getattr(exc,'status',400),str(exc)[:150],headers=PRIVATE_HEADERS) from None
        except Exception:raise HTTPException(503,'录像厅资料暂不可用；已有海报和偏好保留',headers=PRIVATE_HEADERS) from None

    @router.get('/theater/preferences',operation_id=op+'preferences',openapi_extra=hidden)
    async def preferences(request:Request):return await call(theater.preferences,owner(request))

    @router.post('/theater/preferences',operation_id=op+'save-preferences',openapi_extra=hidden)
    async def save_preferences(request:Request):return await call(theater.save_preferences,owner(request),await body(request))

    path='/theater/libraries/{library_id}/items/{media_item_id}'

    @router.get(path+'/art',operation_id=op+'art-state',openapi_extra=hidden)
    async def state(request:Request,item=Depends(visible)):return await call(theater.art,owner(request),item)

    @router.get(path+'/candidates',operation_id=op+'art-candidates',openapi_extra=hidden)
    async def candidates(request:Request,item=Depends(visible)):
        identity=owner(request)
        selected=await theater.choice(identity,item['id'])
        try:
            metadata=await theater.metadata(item,selected.get('tmdb_id'))
            matches=[]
            if not item.get('tmdb_id') and not selected.get('tmdb_id'):
                try:matches=await theater.search(item)
                except Exception:pass
            return output({**metadata,'matches':matches,**await theater.art(identity,item)})
        except Exception:raise HTTPException(503,'暂时无法读取候选海报；现有图片保留') from None

    @router.post(path+'/art',operation_id=op+'art-select',openapi_extra=hidden)
    async def select(request:Request,item=Depends(visible)):
        return await call(theater.optimize,owner(request),item,await body(request))

    @router.get(path+'/images/{key}',operation_id=op+'art-image',openapi_extra=hidden)
    async def image(request:Request,key:str,full:bool=False,item=Depends(visible)):
        if not re.fullmatch(r'[a-f0-9]{64}',key):raise HTTPException(404,'海报不存在')
        choice=await theater.choice(owner(request),item['id'])
        if key!=choice.get('key') or not await theater.cache.get(key):raise HTTPException(404,'海报尚未缓存或已切换')
        headers={'Cache-Control':'private, max-age=0, must-revalidate','ETag':'"'+key+('-full' if full else '-thumb')+'"',
                 'Vary':'Cookie, Authorization','X-Content-Type-Options':'nosniff','X-Poster-Cache':'HIT'}
        if request.headers.get('if-none-match')==headers['ETag']:return Response(status_code=304,headers=headers)
        return FileResponse(theater.cache.root/(key+('.jpg' if full else '-thumb.jpg')),media_type='image/jpeg',headers=headers)

    @router.get('/theater/cache',operation_id=op+'cache-status',openapi_extra=hidden)
    async def cache_status(request:Request,admin=Depends(require_admin)):
        owner(request)
        return await call(theater.cache.stats)

    @router.post('/theater/cache/prune',operation_id=op+'cache-clean',openapi_extra=hidden)
    async def cache_prune(request:Request,admin=Depends(require_admin)):
        owner(request);payload=await body(request)
        if payload!={'confirm':True}:raise HTTPException(400,'请确认清理未锁定的插件海报缓存')
        return output({'removed':await theater.cache.prune()})

    return router


def build_collection_router(entry_id, account_dependency):
    """Intersect native collection membership with native filter predicates, then paginate."""
    from fastapi import Query
    from movieclaw_api.api.deps import require_login
    from movieclaw_db.engine import get_session
    from movieclaw_api.api.routes import collections as native
    from movieclaw_api.api.routes import libraries as libraries
    from movieclaw_api.services.library.items import _narrow, _wall_scope
    router=APIRouter(dependencies=[Depends(account_dependency)])

    @router.get('/theater/collections/{collection_id}/items',operation_id=f'plugins.{entry_id}.theater-collection-items',openapi_extra={'x-cli-hidden':True})
    async def items(collection_id:int, kind:str=Query('movie',pattern='^(movie|tv)$'),
                    sort:str=Query('added_at',pattern='^(title|added_at|rating|release_date|last_played)$'),
                    limit:int=Query(24,ge=1,le=100),offset:int=Query(0,ge=0,le=100000),
                    library_id:int|None=Query(None,ge=1),
                    filters=Depends(libraries._filter_params),session=Depends(get_session),principal=Depends(require_login)):
        member,visible,content_limit=await native._scope(session,principal)
        collection=await native._get_or_404(session,collection_id)
        native._guard_visible(collection,member,visible)
        ids=await native.resolve_members(session,collection,member_id=member,visible_library_ids=visible,
                                         content_limit=content_limit,sort=sort)
        if not ids:return JSONResponse({"success":True,"data":[]},headers=PRIVATE_HEADERS)
        libs=await libraries.LibraryConfigService(session).list_all(kind=kind)
        scope=frozenset(lib.id for lib in libs if lib.id in visible and (library_id is None or lib.id==library_id))
        if not scope:return JSONResponse({"success":True,"data":[]},headers=PRIVATE_HEADERS)
        allowed=set()
        for start in range(0,len(ids),400):
            query=libraries.select(libraries.LibraryFile.media_item_id).where(
                *_wall_scope(scope),libraries.LibraryFile.media_item_id.in_(ids[start:start+400]),
                *_narrow(filters,member,library_id=scope,content_limit=content_limit)).distinct()
            allowed.update((await session.execute(query)).scalars().all())
        page=[i for i in ids if i in allowed][offset:offset+limit]
        if not page:return JSONResponse({"success":True,"data":[]},headers=PRIVATE_HEADERS)
        rows=await native._aggregate_wall_views(session,None,page,page,library_ids=set(scope))
        return JSONResponse({'success':True,'data':[row.model_dump(mode='json') for row in rows]},headers=PRIVATE_HEADERS)
    return router
