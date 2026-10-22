import asyncio, copy, io, json, threading, zipfile
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
import httpx, pytest
from movieclaw_api.plugins.packages import read_archive, check_compat, PackageError
from movieclaw_kernel import DURABLE_EVENTS
from movieclaw_api.plugins.keys import PLUGIN_DATA, PLUGIN_ROUTES
from movieclaw_sdk.runner import RemoteContext, _current_delivery
from movieclaw_sdk.callbacks import PLUGIN_CALLBACKS, CallbackRequest
from movieclaw_kernel.observe import DeliveryInfo, SYSTEM
import wecom_notify as nh
from wecom_client import WecomClient, WecomError, card_description

def settings(**kwargs):
 values=dict(corp_id="test-corp",agent_id=1,corp_secret="test-secret",to_user="tester",enrich_metadata=False,uploads=False)
 values.update(kwargs)
 return nh.Settings(**values)

def notice(message):
 if message["msgtype"]=="news":
  card=message["news"]["articles"][0];return {"title":card["title"],"content":card["description"],"push_img_url":card["picurl"],"push_link_url":card["url"]}
 if message["msgtype"]=="textcard":
  card=message["textcard"];return {"title":card["title"],"content":card["description"],"push_link_url":card["url"]}
 return {"title":message["text"]["content"].split("\n")[0],"content":message["text"]["content"]}

MEDIA=dict(id=10,kind='tv',title='测试剧集',year=2026,tmdb_id=100)
UNIT=[dict(season=1,episode=1)]
CASES={
 'library.items.arrived':dict(library_id=1,items=[dict(media=MEDIA,units=UNIT)]),
 'subscription.created':dict(media=MEDIA,subscription_id=1,member_id=0),
 'subscription.download_started':dict(media=MEDIA,subscription_id=1,units=UNIT,torrent={'title':'4K资源'}),
 'subscription.fulfilled':dict(media=MEDIA,subscription_id=1,units=UNIT,requested_units=UNIT),
 'subscription.upgraded':dict(media=MEDIA,subscription_id=1,unit=dict(season=1,episode=1),old_label='1080p',new_label='2160p'),
 'download.completed':dict(media=MEDIA,subscription_id=1,units=UNIT,info_hash='abc',downloader_id=1),
 'library.ingest.completed':dict(media=MEDIA,library_id=1,files=[dict(id=1,library_id=1,path='/library/e1.mkv',season=1,episode=1)], imported_names=['e1.mkv'],info_hashes=['abc']),
}
PLAY=dict(type='playback.started',id='play1',data=dict(media=MEDIA,member_id=0,unit=dict(season=1,episode=1),position_ms=120000,duration_ms=1000000,played=False,client=dict(name='网页',device_name='电脑')))

class Runner:
 def __init__(self,values=None):
  self.values=values if values is not None else {};self.sent=[];self.routers=[];self.issues=0
 def declare(self,kind,item):
  self.sent.append(dict(type={'listeners':'on','contributions':'contribute','callbacks':'callback'}.get(kind,kind),**item))
 def service(self,name):
  runner=self
  class Service:
   async def get(self,**params):return SimpleNamespace(**(await runner.rpc('data.get',params)))
   async def set(self,**params):return await runner.rpc('data.set',params)
   async def issue(self,**params):return await runner.rpc('callbacks.issue',params)
  return Service()
 def serve_router(self,router,prefix):self.routers.append(router)
 async def rpc(self,method,params):
  if method=='data.get':return dict(found=params['key'] in self.values,value=copy.deepcopy(self.values.get(params['key'])))
  if method=='data.set':self.values[params['key']]=copy.deepcopy(params['value']);return None
  if method=='callbacks.issue':
   self.issues+=1;return dict(id=1,endpoint='playback',scope='plugin',url='/api/v1/hooks/notifyhub/playback/testkey',absolute=False)
  raise AssertionError(method)

async def context(cfg=None,values=None):
 runner=Runner(values);ctx=RemoteContext(runner,'notifyhub','NotifyHub',cfg or settings(),tuple(k.name for k in nh.apply.inject))
 await nh.apply.apply(ctx);return ctx,runner

async def deliver(ctx,name,data=None,eid='native1'):
 event,handler=ctx._handlers['notifyhub-'+name.replace('.','-')]
 token=_current_delivery.set(DeliveryInfo(eid,name,'2026-10-13T00:00:00Z',SYSTEM))
 try:await handler(event.payload.model_validate(data or CASES[name]))
 finally:_current_delivery.reset(token)

async def callback(ctx,data=None,method='POST'):
 return await ctx._callbacks['playback'](CallbackRequest(method=method,endpoint='playback',body=json.dumps(data if data is not None else PLAY).encode()))

@pytest.fixture
def capture(monkeypatch):
 sent=[]
 def send(request):
  if request.url.path.endswith('/gettoken'):return httpx.Response(200,json={'access_token':'fake-token','expires_in':7200})
  sent.append(notice(json.loads(request.content)));return httpx.Response(200,json={'errcode':0})
 monkeypatch.setattr(nh.net,'http_transport',lambda service:httpx.MockTransport(send));return sent

@pytest.mark.asyncio
@pytest.mark.parametrize('event',nh.EVENTS,ids=lambda e:e.name)
async def test_real_native_event(event,capture):
 ctx,runner=await context(settings(download_completed=True));await deliver(ctx,event.name)
 assert len(capture)==1
 assert 'None' not in capture[0]['content'];assert event.durable
 if event.name=='subscription.download_started':assert '4K资源' in capture[0]['content'] and 'S01E01' in capture[0]['content']
 await ctx.dispose()

@pytest.mark.asyncio
async def test_defaults(capture):
 ctx,r=await context();names={m.get('event') for m in r.sent if m['type']=='on'}
 assert all(m['durable'] for m in r.sent if m['type']=='on')
 assert names=={'subscription.created','subscription.download_started','subscription.fulfilled','subscription.upgraded','library.ingest.completed','library.items.arrived','library.files.added'}
 assert (await callback(ctx)).status==200 and capture==[]
 assert any(m['type']=='contribute' and m['registry']=='im-channels' for m in r.sent)
 await ctx.dispose()

@pytest.mark.asyncio
async def test_native_restart_dedupe(capture):
 ctx,r=await context();await deliver(ctx,'subscription.created');await deliver(ctx,'subscription.created')
 ctx2,_=await context(values=r.values);await deliver(ctx2,'subscription.created')
 assert len(capture)==1;await ctx.dispose();await ctx2.dispose()

@pytest.mark.asyncio
async def test_overlap_and_partial_units(capture):
 ctx,r=await context();await deliver(ctx,'library.ingest.completed',eid='a');await deliver(ctx,'subscription.fulfilled',eid='b')
 data=dict(CASES['subscription.fulfilled'],units=[dict(season=1,episode=1),dict(season=1,episode=2)])
 await deliver(ctx,'subscription.fulfilled',data,eid='c');assert len(capture)==2
 assert 'S01E02' in capture[-1]['content'] and 'S01E01' not in capture[-1]['content']
 await ctx.dispose()

@pytest.mark.asyncio
async def test_upgrade_not_suppressed(capture):
 ctx,_=await context();await deliver(ctx,'library.ingest.completed',eid='a')
 await deliver(ctx,'subscription.upgraded',eid='b')
 assert len(capture)==2 and '洗版完成' in capture[-1]['title']
 assert '2160p' in capture[-1]['content'];await ctx.dispose()

@pytest.mark.asyncio
async def test_staging(capture):
 ctx,_=await context();data=dict(CASES['library.ingest.completed'],staging=True)
 await deliver(ctx,'library.ingest.completed',data);assert not capture
 ctx2,_=await context(settings(staging=True));await deliver(ctx2,'library.ingest.completed',data)
 assert '暂存整理完成' in capture[0]['title'];await ctx.dispose();await ctx2.dispose()

@pytest.mark.asyncio
@pytest.mark.parametrize('event',['playback.started','playback.completed','playback.stopped'])
async def test_playback_callback(event,capture):
 ctx,_=await context(settings(playback=True));data=copy.deepcopy(PLAY);data['type']=event
 assert (await callback(ctx,data)).status==200
 assert len(capture)==1 and 'S01E01' in capture[0]['content'] and '2:00' in capture[0]['content']
 assert 'push_img_url' not in capture[0];assert '成员 ID' not in capture[0]['content']
 assert capture[0]['push_link_url']=='https://www.themoviedb.org/tv/100';await ctx.dispose()

@pytest.mark.asyncio
async def test_playback_dedupe_and_filter(capture):
 ctx,r=await context(settings(playback=True));await callback(ctx);await callback(ctx)
 data=copy.deepcopy(PLAY);data['id']='play2';await callback(ctx,data)
 data['type']='playback.progress';await callback(ctx,data)
 data['type']='playback.stopped';data['id']='stop1';await callback(ctx,data)
 assert len(capture)==2;await ctx.dispose()

@pytest.mark.asyncio
@pytest.mark.parametrize('bad',[[],{'type':'playback.started','id':'x','data':[]},{'type':'playback.started','id':'','data':{}},{'type':'playback.started','id':'x','data':{'media':'bad'}}])
async def test_invalid_callback(bad,capture):
 ctx,_=await context(settings(playback=True));assert (await callback(ctx,bad)).status==400
 assert capture==[];await ctx.dispose()

@pytest.mark.asyncio
async def test_callback_failure_and_retry(monkeypatch,capture):
 ctx,r=await context(settings(playback=True))
 monkeypatch.setattr(nh.net,'http_transport',lambda service:httpx.MockTransport(lambda req:httpx.Response(500)))
 assert (await callback(ctx)).status==503 and not any(k.startswith("dedupe-v2-") and isinstance(v,dict) and v for k,v in r.values.items())
 monkeypatch.setattr(nh.net,'http_transport',lambda service:httpx.MockTransport(lambda req:httpx.Response(200,json={'access_token':'fake-token','expires_in':7200,'errcode':0})))
 assert (await callback(ctx)).status==200 and r.values;await ctx.dispose()

@pytest.mark.asyncio
@pytest.mark.parametrize('response',[httpx.Response(500),httpx.Response(200,json={'success':False}),httpx.Response(200,json={'status':'error'}),httpx.Response(200,text='<html>login</html>'),httpx.Response(302)])
async def test_native_failure_does_not_mark(monkeypatch,response):
 monkeypatch.setattr(nh.net,'http_transport',lambda service:httpx.MockTransport(lambda req:response))
 ctx,r=await context()
 with pytest.raises(Exception):await deliver(ctx,'subscription.created')
 assert not any(k.startswith("dedupe-v2-") and isinstance(v,dict) and v for k,v in r.values.items());await ctx.dispose()

@pytest.mark.asyncio
async def test_alert_channel(capture):
 ctx,r=await context();store=ctx.use(PLUGIN_DATA).scoped(ctx);sender=nh.Sender(ctx,store,ctx.config)
 driver=nh.WecomChannel(sender);bound=await driver.validate({})
 assert bound.account_id=='notifyhub' and not driver.capabilities.receive
 await driver.send(None,None,'⚠️ 下载器连接失败');await driver.send(None,None,'⚠️ 下载器连接失败')
 assert len(capture)==1 and '下载器连接失败' in capture[0]['content'];await ctx.dispose()

@pytest.mark.asyncio
async def test_integration_url_issued_once():
 ctx,r=await context();route=next(rt for rt in r.routers[0].routes if rt.path=='/integration')
 first=await route.endpoint();second=await route.endpoint()
 assert first==second and r.issues==1 and first['webhook_url'].startswith('/api/v1/hooks/notifyhub/playback/')
 await ctx.dispose()

@pytest.mark.asyncio
async def test_concurrent_duplicate(capture):
 ctx,_=await context();await asyncio.gather(deliver(ctx,'subscription.created'),deliver(ctx,'subscription.created'))
 assert len(capture)==1;await ctx.dispose()

@pytest.mark.asyncio
async def test_expired_merge(capture):
 ctx,r=await context();await deliver(ctx,'library.ingest.completed',eid='a')
 for name,values in r.values.items():
  if isinstance(values,dict):
   for key in values:
    if key.startswith('import:'):values[key]=0
 for _,handler in ctx._handlers.values():
  sender=handler.__closure__[0].cell_contents if handler.__closure__ else None
  if isinstance(sender,nh.Sender):sender.ledger.buckets.clear()
 await deliver(ctx,'library.ingest.completed',eid='b');assert len(capture)==2;await ctx.dispose()

@pytest.mark.asyncio
async def test_disable_alerts(capture):
 ctx,r=await context(settings(alerts=False));assert not any(m['type']=='contribute' for m in r.sent)
 await nh.WecomChannel(nh.Sender(ctx,ctx.use(PLUGIN_DATA).scoped(ctx),ctx.config)).send(None,None,'告警')
 assert not capture;await ctx.dispose()

def test_real_v038_package_and_compatibility():
 buf=io.BytesIO()
 with zipfile.ZipFile(buf,'w') as z:
  for path in Path(nh.__file__).parent.iterdir():
   if path.is_file():z.write(path,path.name)
 manifest,_=read_archive(buf.getvalue())
 check_compat(manifest,reserved=set(),operations=set(nh.OPERATIONS))
 assert str(nh.SUBSCRIPTION_CREATED.version)=='2.0'

@pytest.mark.asyncio
async def test_real_http(monkeypatch):
 received=[]
 class Handler(BaseHTTPRequestHandler):
  def do_POST(self):
   received.append((self.path,self.headers['Content-Type'],json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
   self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(b'{"errcode":0}')
  def do_GET(self):
   self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(b'{"access_token":"fake-token","expires_in":7200}')
  def log_message(self,*_):pass
 server=ThreadingHTTPServer(('127.0.0.1',0),Handler);threading.Thread(target=server.serve_forever,daemon=True).start()
 async def proxy(service):assert service=='notifyhub';return None
 monkeypatch.setattr(nh.net,'_resolver',proxy)
 try:
  ctx,_=await context(settings(api_base=f'http://127.0.0.1:{server.server_port}'))
  await deliver(ctx,'subscription.created');assert received[0][:2]==('/cgi-bin/message/send?access_token=fake-token','application/json')
  assert received[0][2]['touser']=='tester';await ctx.dispose()
 finally:server.shutdown();server.server_close()

