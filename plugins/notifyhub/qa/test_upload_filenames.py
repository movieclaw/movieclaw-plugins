import pytest
import templates
import wecom_notify as n
from uploads import UploadMonitor
from test_plugin import context,settings,capture
from test_storage_uploads import job
from datetime import datetime,timezone,timedelta


def progress(id,name=None,done=0,failed=0):
 return {'id':id,'event_type':'progress','payload':{'phase':'uploading','message':f'读取「{name}」算指纹' if name else f'已上传 {done} 个（秒传 0）','details':{'done':done,'failed':failed}}}

async def setup(monkeypatch,events):
 ctx,r=await context(settings(uploads=True));[t.cancel() for t in ctx._tasks]
 sender=n.Sender(ctx,ctx.use(n.PLUGIN_DATA).scoped(ctx),ctx.config)
 monitor=UploadMonitor(sender);monitor.started=(datetime.now(timezone.utc)-timedelta(minutes=1)).isoformat()
 async def call(op,args):return {'items':events} if op=='jobs.events' else {'items':[job(uploaded=2,failed=1)]}
 monkeypatch.setattr(sender.enricher,'call',call)
 return ctx,sender,monitor

@pytest.mark.asyncio
async def test_only_confirmed_successful_names(capture,monkeypatch):
 events=[progress(1,'电影甲.mkv'),progress(2,done=1),progress(3,'失败.mkv',done=1),progress(4,done=1,failed=1),progress(5,'电影乙.mkv',done=1,failed=1),progress(6,done=2,failed=1)]
 ctx,sender,monitor=await setup(monkeypatch,events)
 await monitor.poll();await monitor.poll()
 assert len(capture)==1
 assert '电影甲.mkv' in capture[0]['content'] and '电影乙.mkv' in capture[0]['content']
 assert '失败.mkv' not in capture[0]['content'] and '部分完成' in capture[0]['title']
 await ctx.dispose()

@pytest.mark.asyncio
async def test_paginated_events_and_long_file_list(monkeypatch):
 events=[]
 for i in range(260):events += [progress(2*i+1,f'剧集{i}.mkv',done=i),progress(2*i+2,done=i+1)]
 ctx,sender,monitor=await setup(monkeypatch,events)
 calls=[]
 async def call(op,args):
  calls.append(args['after']);return {'items':[e for e in events if e['id']>args['after']][:500]}
 monkeypatch.setattr(sender.enricher,'call',call)
 result=await monitor.successful_names('job',260)
 assert calls==[0,500] and len(result['filenames'])==20
 assert result['filenames_remaining']==240 and result['filenames_missing']==0
 await ctx.dispose()

@pytest.mark.asyncio
async def test_missing_events_does_not_invent_name(monkeypatch):
 ctx,sender,monitor=await setup(monkeypatch,[progress(1,done=2)])
 result=await monitor.successful_names('job',2)
 assert result['filenames']==[] and result['filenames_missing']==2
 await ctx.dispose()

@pytest.mark.asyncio
async def test_events_failure_defers_notification(capture,monkeypatch):
 ctx,sender,monitor=await setup(monkeypatch,[])
 async def call(op,args):return None if op=='jobs.events' else {'items':[job()]}
 monkeypatch.setattr(sender.enricher,'call',call)
 await monitor.poll();assert not capture
 await ctx.dispose()


def test_saved_default_upgrades_custom_template_preserved():
 context={'title':'电影库','event_icon':'☁️','action':'上传完成','uploaded':1,'rapid':1,'failed':0,'filenames':['作品.mkv']}
 cfg=settings(upload_text=templates.PREVIOUS_UPLOAD_TEXT)
 assert '作品.mkv' in templates.render('drive.upload.completed',context,cfg)[1]
 cfg=settings(upload_text='自定：{{ filenames|join(", ") }}')
 assert templates.render('drive.upload.completed',context,cfg)[1]=='自定：作品.mkv'
 assert n.apply.title=='企业微信直推'
