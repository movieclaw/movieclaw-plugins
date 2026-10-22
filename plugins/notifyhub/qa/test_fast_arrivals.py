import asyncio, copy
from datetime import datetime,timezone,timedelta
import pytest
from arrivals import FastArrivals
import wecom_notify as n
from test_plugin import context,settings,capture


def row(id=1,episode=1,source='scanned'):
 return dict(id=id,season_number=1,episode_number=episode,source=source,
             added_at=datetime.now(timezone.utc).isoformat(),size_bytes=100,missing=False,state='in_place')

async def setup(monkeypatch,files=None,library=None):
 cfg=settings(library_merge_seconds=1).model_copy(update={'library_merge_seconds':0.02})
 ctx,r=await context(cfg)
 sender=n.Sender(ctx,ctx.use(n.PLUGIN_DATA).scoped(ctx),cfg)
 fast=FastArrivals(sender)
 library=library or {'id':1,'kind':'tv','created_at':(datetime.now(timezone.utc)-timedelta(days=2)).isoformat()}
 detail={'kind':'tv','source':'tmdb','title':'快速剧集','files':files if files is not None else [row()]}
 calls=[]
 async def call(op,args):
  calls.append(op)
  return [library] if op=='library.list' else detail
 monkeypatch.setattr(sender.enricher,'call',call)
 return ctx,r,sender,fast,detail,calls


def event(*ids):
 return {'library_id':1,'files':[{'id':id,'media_item_id':10,'season':1,'episode':id,'strm':False} for id in ids]}

@pytest.mark.asyncio
async def test_coalesces_episodes_ack_after_send_and_fallback_dedupe(capture,monkeypatch):
 ctx,r,sender,fast,detail,calls=await setup(monkeypatch,[row(1,1),row(2,2)])
 first=asyncio.create_task(fast.handle(event(1)));await asyncio.sleep(0)
 assert not first.done() and not capture
 second=asyncio.create_task(fast.handle(event(2)))
 await asyncio.gather(first,second)
 assert len(capture)==1 and 'S01E01–E02' in capture[0]['content']
 assert calls.count('library.items.get')==1
 await sender.handle('library.items.arrived',{'library_id':1,'items':[{'media':{'id':10,'kind':'tv','title':'快速剧集'},'units':[{'season':1,'episode':1},{'season':1,'episode':2}]}]},external_id='delayed')
 await fast.handle(event(1,2));assert len(capture)==1
 await ctx.dispose()

@pytest.mark.asyncio
async def test_first_scan_new_library_skipped_imported_allowed(capture,monkeypatch):
 ctx,r,sender,fast,detail,calls=await setup(monkeypatch,library={'id':1,'kind':'tv','created_at':datetime.now(timezone.utc).isoformat()})
 await fast.handle(event(1));assert not capture
 detail['files'][0]['source']='imported'
 await fast.handle(event(1));assert len(capture)==1
 await ctx.dispose()

@pytest.mark.asyncio
async def test_old_version_and_unidentified_not_sent(capture,monkeypatch):
 ctx,r,sender,fast,detail,calls=await setup(monkeypatch,[row(1,1),row(2,1)])
 await fast.handle(event(2));assert not capture
 detail['source']='local'
 await fast.handle(event(1));assert not capture
 await ctx.dispose()

@pytest.mark.asyncio
async def test_large_batch_uses_native_fallback(capture,monkeypatch):
 ctx,r,sender,fast,detail,calls=await setup(monkeypatch)
 await fast.handle(event(*range(201)))
 assert not calls and not capture
 await ctx.dispose()

@pytest.mark.asyncio
async def test_send_failure_propagates_and_retry_succeeds(capture,monkeypatch):
 ctx,r,sender,fast,detail,calls=await setup(monkeypatch)
 send=sender.wecom.send
 async def fail(body):raise OSError('network')
 monkeypatch.setattr(sender.wecom,'send',fail)
 with pytest.raises(OSError):await fast.handle(event(1))
 assert not fast.pending
 monkeypatch.setattr(sender.wecom,'send',send)
 await fast.handle(event(1));assert len(capture)==1
 await ctx.dispose()

@pytest.mark.asyncio
async def test_missing_api_or_missing_file_uses_fallback(capture,monkeypatch):
 ctx,r,sender,fast,detail,calls=await setup(monkeypatch)
 detail['files'][0]['missing']=True
 await fast.handle(event(1));assert not capture
 async def missing(*args):return None
 monkeypatch.setattr(sender.enricher,'call',missing)
 await fast.handle(event(1));assert not capture
 await ctx.dispose()
