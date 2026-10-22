"""Real v0.38 SDK subprocess, Unix HTTP gateway, event delivery and local WeCom endpoint."""
import asyncio, json, os, socket, sys, tempfile
from pathlib import Path
import httpx, pytest
import wecom_notify as nh
from test_plugin import CASES

@pytest.mark.asyncio
@pytest.mark.parametrize("uploads", [False, True])
async def test_latest_subprocess_lifecycle(uploads):
 state={};sent=[];declarations={};ready=asyncio.Event();failures=[]
 async def server(reader,writer):
  try:
   head=await reader.readuntil(b'\r\n\r\n')
   lines=head.decode().split('\r\n');path=lines[0].split()[1]
   headers=dict(line.split(': ',1) for line in lines[1:] if ': ' in line)
   size=int(next((v for k,v in headers.items() if k.lower()=='content-length'),'0'))
   raw=await reader.readexactly(size);data=json.loads(raw) if raw else {}
   if path.endswith('/session/ready'):declarations.update(data);ready.set();result=None
   elif path.endswith('/session/failed'):failures.append(data);ready.set();result=None
   elif path.endswith('/ops/operations'):
    result=[dict(operation_id='jobs.list',method='GET',path='/api/v1/jobs',arg_locations={'job_type':'query','status':'query','limit':'query'},required=[],stream=False),dict(operation_id='jobs.events',method='GET',path='/api/v1/jobs/{job_id}/events',arg_locations={'job_id':'path','after':'query','limit':'query'},required=['job_id'],stream=False)]
    result.extend([dict(operation_id='library.list',method='GET',path='/api/v1/libraries',arg_locations={},required=[],stream=False),dict(operation_id='library.items.get',method='GET',path='/api/v1/libraries/{library_id}/items/{media_item_id}',arg_locations={'library_id':'path','media_item_id':'path'},required=['library_id','media_item_id'],stream=False)])
   elif path=='/api/v1/libraries':
    result={'data':[{'id':1,'kind':'movie','created_at':'2020-01-01T00:00:00Z'}]}
   elif path=='/api/v1/libraries/1/items/21':
    from datetime import datetime, timezone
    result={'data':{'media_item_id':21,'kind':'movie','source':'tmdb','title':'快速新片','files':[{'id':101,'source':'scanned','season_number':0,'episode_number':0,'added_at':datetime.now(timezone.utc).isoformat(),'missing':False,'size_bytes':100}]}}
   elif path.startswith('/api/v1/jobs/upload-e2e/events'):
    result={'data':{'items':[{'id':1,'event_type':'progress','payload':{'phase':'uploading','message':'读取「影片甲.mkv」算指纹','details':{'done':0,'failed':0}}},{'id':2,'event_type':'progress','payload':{'phase':'uploading','message':'已上传 1 个','details':{'done':1,'failed':0}}},{'id':3,'event_type':'progress','payload':{'phase':'uploading','message':'读取「影片乙.mkv」算指纹','details':{'done':1,'failed':0}}},{'id':4,'event_type':'progress','payload':{'phase':'uploading','message':'已上传 2 个','details':{'done':2,'failed':0}}}]}}
   elif path.startswith('/api/v1/jobs?'):
    from datetime import datetime, timezone
    result={'data':{'items':[{'id':'upload-e2e','job_type':'drive.upload','status':'succeeded','subject':'电影库','finished_at':datetime.now(timezone.utc).isoformat(),'result':{'uploaded':2,'rapid':1,'failed':0}}]}}
   elif path.endswith('/net/proxy'):result=None
   elif path.endswith('/data/get'):result={'found':data['key'] in state,'value':state.get(data['key'])}
   elif path.endswith('/data/set'):state[data['key']]=data['value'];result=None
   elif path.startswith('/cgi-bin/gettoken'):result={'access_token':'test-token','expires_in':7200}
   elif path.startswith('/cgi-bin/message/send'):sent.append(data);result={'errcode':0}
   else:raise AssertionError(path)
   body=json.dumps(result).encode()
   writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: '+str(len(body)).encode()+b'\r\n\r\n'+body);await writer.drain()
  except Exception as exc:failures.append(repr(exc));ready.set()
  finally:writer.close();await writer.wait_closed()
 with tempfile.TemporaryDirectory(prefix='wc38-',dir='/tmp') as tmp:
  gateway=await asyncio.start_unix_server(server,tmp+'/host.sock')
  wecom=await asyncio.start_server(server,'127.0.0.1',0)
  listener=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);listener.bind(tmp+'/plugin.sock');listener.listen()
  proc=await asyncio.create_subprocess_exec(sys.executable,'-m','movieclaw_sdk.runner','--path',str(Path(nh.__file__).parent.resolve()),'--module','wecom_notify','--entry','notifyhub','--listen-fd',str(listener.fileno()),pass_fds=(listener.fileno(),),stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE,env=os.environ.copy())
  try:
   config=dict(corp_id='test',agent_id=1,corp_secret='test',to_user='tester',enrich_metadata=False,uploads=uploads,library_merge_seconds=1,api_base=f'http://127.0.0.1:{wecom.sockets[0].getsockname()[1]}')
   proc.stdin.write((json.dumps(dict(gateway=tmp+'/host.sock',token='test',config=config,data_dir=tmp))+'\n').encode());await proc.stdin.drain();proc.stdin.close()
   await asyncio.wait_for(ready.wait(),15);assert not failures,failures
   assert declarations['serving'] and declarations['callbacks']
   assert all(row['durable'] for row in declarations['listeners'])
   assert len(declarations['listeners'])==7
   async with httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(uds=tmp+'/plugin.sock'),base_url='http://plugin') as client:
    payload=dict(payload=CASES['subscription.created'],delivery=dict(event_id='e2e',name='subscription.created',occurred_at='2026-10-15T00:00:00Z',origin=dict(kind='system',id=None,chain=[]),attempt=1))
    for _ in range(2):
     result=await client.post('/_mc/listener/notifyhub-subscription-created',json=payload)
     assert result.json()['ok'],result.text
    fast_payload={'payload':{'library_id':1,'files':[{'id':101,'media_item_id':21}]},'delivery':dict(event_id='fast-e2e',name='library.files.added',occurred_at='2026-10-21T00:00:00Z',origin=dict(kind='system',id=None,chain=[]),attempt=1)}
    result=await client.post('/_mc/listener/notifyhub-library-files-added',json=fast_payload)
    assert result.json()['ok'],result.text
    late={'payload':{'library_id':1,'items':[{'media':{'id':21,'kind':'movie','title':'快速新片'},'units':[]}]},'delivery':dict(event_id='late-e2e',name='library.items.arrived',occurred_at='2026-10-21T00:00:00Z',origin=dict(kind='system',id=None,chain=[]),attempt=1)}
    result=await client.post('/_mc/listener/notifyhub-library-items-arrived',json=late)
    assert result.json()['ok'],result.text
   if uploads:
    for _ in range(100):
     if len(sent)==3:break
     await asyncio.sleep(0.02)
   assert len(sent)==(3 if uploads else 2) and all(m['touser']=='tester' for m in sent)
   assert sum('快速新片' in json.dumps(m,ensure_ascii=False) for m in sent)==1
   if uploads:
    assert 'upload-e2e' in state.get('upload-notified-v1',[])
    upload=next(m for m in sent if '影片甲.mkv' in json.dumps(m,ensure_ascii=False))
    assert upload['msgtype']=='text' and '影片乙.mkv' in upload['text']['content']
   proc.terminate();assert await asyncio.wait_for(proc.wait(),10)==0
  finally:
   if proc.returncode is None:proc.kill();await proc.wait()
   errors=(await proc.stderr.read()).decode();listener.close();gateway.close();wecom.close()
   await gateway.wait_closed();await wecom.wait_closed()
   assert 'Traceback' not in errors,errors


def test_sdk_describe_loads_without_removed_backend_module():
 import subprocess
 result=subprocess.run([sys.executable,'-m','movieclaw_sdk.runner','--path',str(Path(nh.__file__).parent.resolve()),'--module','wecom_notify','--entry','notifyhub','--describe'],capture_output=True,text=True,timeout=15)
 assert result.returncode==0,result.stderr
 declaration=json.loads(result.stdout)
 assert declaration['type']=='describe'
 assert declaration['title']=='企业微信直推'
 assert 'corp_secret' in declaration['config']['schema']['properties']
