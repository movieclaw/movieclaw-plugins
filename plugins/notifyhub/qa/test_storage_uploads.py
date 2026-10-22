import copy,json,time
from types import SimpleNamespace
from datetime import datetime,timezone,timedelta
import pytest
from dedupe import Ledger,LIMIT
from uploads import UploadMonitor
import wecom_notify as n
from test_plugin import context,settings,deliver,CASES,capture

class BoundedStore:
 def __init__(self,values=None):self.values=values or {};self.sizes=[]
 async def get(self,key,default=None):return copy.deepcopy(self.values.get(key,default))
 async def set(self,key,value):
  size=len(json.dumps(value,ensure_ascii=False).encode());self.sizes.append(size)
  if size>65536:raise ValueError('值超过 64 KB')
  self.values[key]=copy.deepcopy(value)

@pytest.mark.asyncio
async def test_original_size_failure_and_migration():
 old={'event:'+str(i).zfill(64):time.time()+86400 for i in range(650)}
 store=BoundedStore({'dedupe-v1':old});ledger=Ledger(store)
 await ledger.migrate()
 entries={'event:'+n.digest(i):time.time()+86400 for i in range(3000)}
 assert len(json.dumps({**old,**entries}).encode())>65536
 await ledger.remember(entries)
 assert max(store.sizes)<=LIMIT
 restarted=Ledger(store);await restarted.migrate()
 assert all([await restarted.contains(k) for k in old])
 assert all([await restarted.contains(k) for k in entries])

@pytest.mark.asyncio
async def test_same_shard_never_exceeds_limit(monkeypatch):
 store=BoundedStore();ledger=Ledger(store)
 monkeypatch.setattr(ledger,'bucket',lambda key:'single')
 await ledger.remember({n.digest(i):time.time()+1000 for i in range(2000)})
 assert max(store.sizes)<=LIMIT and len(store.values['single'])<2000
 assert await ledger.contains(n.digest(1999))

@pytest.mark.asyncio
async def test_three_sources_one_notice_after_restart(capture):
 ctx,r=await context()
 await deliver(ctx,'library.ingest.completed',eid='ingest')
 ctx2,r2=await context(values=r.values)
 await deliver(ctx2,'subscription.fulfilled',eid='fulfilled')
 await deliver(ctx2,'library.items.arrived',eid='arrived')
 await deliver(ctx2,'library.ingest.completed',eid='ingest')
 assert len(capture)==1
 await ctx.dispose();await ctx2.dispose()

@pytest.mark.asyncio
async def test_migration_write_failure_prevents_send(capture,monkeypatch):
 ctx,r=await context(values={'dedupe-v1':{'event:old':time.time()+1000}})
 original=r.rpc
 async def rpc(method,params):
  if method=='data.set':raise OSError('storage unavailable')
  return await original(method,params)
 monkeypatch.setattr(r,'rpc',rpc)
 with pytest.raises(OSError):await deliver(ctx,'subscription.created')
 assert not capture
 await ctx.dispose()


def job(id='upload1',uploaded=3,failed=0,finished=None,**kwargs):
 return dict(id=id,job_type='drive.upload',subject='电影库',status='succeeded',result={'uploaded':uploaded,'rapid':2,'failed':failed},finished_at=finished or datetime.now(timezone.utc).isoformat(),**kwargs)

@pytest.mark.asyncio
async def test_upload_success_partial_restart_and_age(capture,monkeypatch):
 ctx,r=await context(settings(uploads=True));ctx._tasks and [task.cancel() for task in ctx._tasks]
 sender=n.Sender(ctx,ctx.use(n.PLUGIN_DATA).scoped(ctx),ctx.config)
 monitor=UploadMonitor(sender);monitor.started=(datetime.now(timezone.utc)-timedelta(minutes=1)).isoformat()
 rows=[job(),job('partial',failed=1)]
 async def call(operation,args):
  if operation=='jobs.events':return {'items':[]}
  assert operation=='jobs.list' and args=={'job_type':'drive.upload','status':'succeeded','limit':200}
  return {'items':rows}
 monkeypatch.setattr(sender.enricher,'call',call)
 await monitor.poll();await monitor.poll()
 assert len(capture)==2
 assert any('部分完成' in m['title'] and '未成功：1' in m['content'] for m in capture)
 # A later process must not repeat jobs even after ordinary 7-day dedupe expires.
 for values in r.values.values():
  if isinstance(values,dict):
   for k in values:values[k]=0
 sender2=n.Sender(ctx,ctx.use(n.PLUGIN_DATA).scoped(ctx),ctx.config)
 monkeypatch.setattr(sender2.enricher,'call',call)
 await UploadMonitor(sender2).poll();assert len(capture)==2
 await ctx.dispose()

@pytest.mark.asyncio
async def test_upload_ignores_history_zero_and_incomplete(capture,monkeypatch):
 ctx,r=await context(settings(uploads=True));[task.cancel() for task in ctx._tasks]
 sender=n.Sender(ctx,ctx.use(n.PLUGIN_DATA).scoped(ctx),ctx.config)
 rows=[job(finished=(datetime.now(timezone.utc)-timedelta(days=1)).isoformat()),job('empty',uploaded=0),job('wrong')]
 rows[-1]['status']='running'
 async def call(*args):return {'items':[] if args[0]=='jobs.events' else rows}
 monkeypatch.setattr(sender.enricher,'call',call)
 await UploadMonitor(sender).poll();assert not capture
 await ctx.dispose()

@pytest.mark.asyncio
async def test_upload_send_failure_retried(capture,monkeypatch):
 ctx,r=await context(settings(uploads=True));[task.cancel() for task in ctx._tasks]
 sender=n.Sender(ctx,ctx.use(n.PLUGIN_DATA).scoped(ctx),ctx.config)
 monitor=UploadMonitor(sender);monitor.started=(datetime.now(timezone.utc)-timedelta(minutes=1)).isoformat()
 async def call(*args):return {'items':[] if args[0]=='jobs.events' else [job()]}
 monkeypatch.setattr(sender.enricher,'call',call)
 original=sender.wecom.send
 async def fail(body):raise OSError('network')
 monkeypatch.setattr(sender.wecom,'send',fail)
 with pytest.raises(OSError):await monitor.poll()
 assert not r.values.get('upload-notified-v1')
 monkeypatch.setattr(sender.wecom,'send',original)
 await monitor.poll();assert len(capture)==1
 await ctx.dispose()
