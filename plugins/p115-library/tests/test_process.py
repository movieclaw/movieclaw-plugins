"""Official SDK child process + actual Unix HTTP and file-descriptor transfer.
115 API and MovieClaw host services are simulated; no account or remote writes.
"""
import asyncio
import json
import os
import socket
import sys
import uuid
from pathlib import Path
import httpx
import pytest
import p115_plugin

@pytest.mark.asyncio
async def test_actual_child_process_strm_and_playback(tmp_path):
 root=tmp_path/'library';root.mkdir();state={};messages=[];scans=[];host_flags={'external_url':True}
 parent,child=socket.socketpair()
 sockpath=Path('/tmp')/('p115-'+uuid.uuid4().hex[:12]+'.sock')
 plugin=Path(p115_plugin.__file__).parent.resolve()
 bootstrap=r'''
import sys,json,runpy,httpx
sys.path.insert(0,sys.argv[1])
from movieclaw_sdk import net
import drive_api,hashlib
def respond(request):
 if request.url.path.endswith('/token/'):
  return httpx.Response(200,json={'state':True,'data':{'uid':'qrid','time':123,'sign':'qrsecret','qrcode':'http://115.com/scan/dg-qrid'}})
 if request.url.path=='/get/status/':return httpx.Response(200,json={'state':True,'data':{'status':2}})
 if request.url.path.endswith('/check/sso'):return httpx.Response(200,json={'state':False})
 if request.url.path.endswith('/login/qrcode/'):
  return httpx.Response(200,json={'state':True,'data':{'cookie':{'UID':'123_A1','CID':'secret','SEID':'secret'}}})
 if request.url.path=='/behavior/detail':return httpx.Response(200,json={'state':True,'data':{'list':[]}})
 if request.url.path=='/files/index_info':return httpx.Response(200,json={'state':True,'data':{'user_name':'测试用户','space_info':{'all_total':{'size':107374182400,'size_format':'100GB'},'all_use':{'size':1073741824,'size_format':'1GB'},'all_remain':{'size':107374182400-1073741824,'size_format':'99GB'}}}})
 if request.url.host=='cdn.example.com':
  body=b'movie bytes'
  if request.method=='HEAD':return httpx.Response(200,headers={'content-type':'video/x-matroska','content-length':str(len(body)),'accept-ranges':'bytes'})
  if request.headers.get('range'):
   start,end=request.headers['range'].replace('bytes=','').split('-');start,end=int(start),int(end)
   part=body[start:end+1]
   return httpx.Response(206,headers={'content-type':'video/x-matroska','content-range':f'bytes {start}-{start+len(part)-1}/{len(body)}','accept-ranges':'bytes'},content=part)
  return httpx.Response(200,headers={'content-type':'video/x-matroska','accept-ranges':'bytes'},content=body)
 if request.url.path=='/files' and request.url.params.get('cid')=='99':return httpx.Response(200,json={'state':True,'cid':'99','count':3,'data':[{'fid':'22','cid':'99','n':'Movie.mkv','s':11,'pc':'abcdef123456','sha':hashlib.sha1(b'movie bytes').hexdigest()},{'fid':'23','cid':'99','n':'Movie.srt','s':10,'pc':'sub123456','sha':hashlib.sha1(b'movie bytes').hexdigest()},{'fid':'24','cid':'99','n':'坏\\名字.mkv','s':9,'pc':'zzz'}]})
 if request.url.path=='/files':
  return httpx.Response(200,json={'state':True,'cid':'0','count':2,'data':[{'fid':'10','cid':'0','n':'Sample Movie (2026).mkv','s':'100','pc':'abcdef123456','sha':'abc'},{'fid':'11','cid':'0','n':'Sample Movie.srt','s':'10','pc':'abcdef999999'}]})
 if request.url.path=='/app/chrome/downurl':
  return httpx.Response(200,json={'state':True,'data':'encrypted-test-fixture'})
 return httpx.Response(500)
net.http_transport=lambda service:httpx.MockTransport(respond)
drive_api.rsa_decrypt=lambda data:b'{"10":{"url":{"url":"https://cdn.example.com/movie.mkv?f=1"}},"23":{"url":{"url":"https://cdn.example.com/subtitle.srt?f=1"}}}'
sys.argv=['runner','--path',sys.argv[1],'--module','p115_plugin','--entry','p115-library','--fd-socket',sys.argv[2]]
runpy.run_module('movieclaw_sdk.runner',run_name='__main__')
'''
 proc=await asyncio.create_subprocess_exec(sys.executable,'-c',bootstrap,str(plugin),str(child.fileno()),pass_fds=(child.fileno(),),stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE,env=os.environ.copy())
 child.close();ready=asyncio.Event();disposed=asyncio.Event();failed=[];settings_updates=[]
 async def send(msg):proc.stdin.write((json.dumps(msg)+'\n').encode());await proc.stdin.drain()
 async def pump():
  while line:=await proc.stdout.readline():
   m=json.loads(line);messages.append(m)
   if m['type']=='hello':
    await send({'type':'init','socket':str(sockpath),'data_dir':str(tmp_path),'config':{'request_interval':.3,'strm_directory':str(root/'auto-strm'),'cloud_cid':'99'}})
   elif m['type']=='ready':ready.set()
   elif m['type']=='disposed':disposed.set()
   elif m['type']=='failed':failed.append(m);ready.set()
   elif m['type']=='rpc':
    method,p=m['method'],m['params']
    try:
     if method=='data.get':result={'found':p['key'] in state,'value':state.get(p['key'])}
     elif method=='data.set':state[p['key']]=p['value'];result=None
     elif method=='data.delete':result=state.pop(p['key'],None) is not None
     elif method=='ops.client':result={'operations':['library.get','library.scan.start','library.list']}
     elif method=='ops.call':
      operation=p.get('operation_id',p.get('operation'))
      if operation=='library.get':result={'primary_root':str(root)}
      elif operation=='library.scan.start':scans.append(p);result={'job_id':'scan1'}
      elif operation=='library.list':result=[{'id':1,'primary_root':str(root),'root_paths':[str(root)]}]
      elif operation=='app.plugins.settings.update':settings_updates.append(p);result={'values':p.get('body',{}).get('values',{})}
      else:raise AssertionError(p)
     elif method=='routes.sign':
      # 2.3.1：镜像宿主契约（plugin_routes.py）——absolute=True 需要「设置 → 应用」的
      # 外部访问地址，未配置时回 kind=value 错误；absolute=False 返回相对链接。
      if p.get('absolute') and not host_flags['external_url']:
       await send({'type':'rpc_result','id':m['id'],'ok':False,'error':{'kind':'value','message':'没有配置外部访问地址（设置 → 应用），无法生成完整链接'}});continue
      result=('https://movie.example.com' if p.get('absolute') else '')+'/api/v1/plugins/p115-library'+p['path']+'?sig=test'
     elif method.startswith('files.'):
      path=Path(p.get('path',p.get('src',''))).resolve()
      assert path.is_relative_to(root.resolve())
      if method=='files.stat':result={'size':path.stat().st_size,'is_dir':path.is_dir(),'mtime':path.stat().st_mtime} if path.exists() else None
      elif method=='files.listdir':result=os.listdir(path)
      elif method=='files.makedirs':path.mkdir(parents=True,exist_ok=True);result=None
      elif method=='files.remove':path.unlink();result=None
      elif method=='files.rename':
       dest=Path(p['dst']).resolve();assert dest.is_relative_to(root.resolve());path.replace(dest);result=None
      elif method=='files.open':
       f=path.open(p['mode']);token=uuid.uuid4().hex
       socket.send_fds(parent,[token.encode()],[f.fileno()]);f.close();result={'token':token}
      else:raise AssertionError(method)
     else:raise AssertionError((method,p))
     await send({'type':'rpc_result','id':m['id'],'ok':True,'result':result})
    except Exception as e:
     failed.append(repr(e));await send({'type':'rpc_result','id':m['id'],'ok':False,'error':type(e).__name__})
 pump_task=asyncio.create_task(pump())
 try:
  await asyncio.wait_for(ready.wait(),10);assert not failed,failed
  assert not any(m['type']=='contribute' and m.get('registry')=='scheduled-tasks' for m in messages)
  zones={m['zone'] for m in messages if m['type']=='routes'};assert zones=={'admin','public'}
  async with httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(uds=str(sockpath)),base_url='http://local/api/v1/plugins/p115-library',timeout=10) as client:
   assert not any(m['type']=='on' for m in messages)
   # 2.1.1：全新安装（init 不带 Cookie）不得因未登录而崩溃——
   # 状态机应进入 waiting_login 并给出扫码指引，地址替换给出明确 400 文案。
   st0=None
   for _ in range(50):
    st0=(await client.get('/strm/status')).json()
    if st0.get('state') not in (None,'idle'):break
    await asyncio.sleep(.02)
   assert st0['state']=='waiting_login' and '请先扫码' in st0['error'],st0
   assert st0['enabled'] is False and st0['mapping_count']==0,st0
   rw0=await client.post('/strm/rewrite',json={'dry_run':True})
   assert rw0.status_code==400 and '115账号' in rw0.text and '请先扫码登录 115' not in rw0.text,rw0.text
   assert (await client.get('/login/info')).json()=={'logged_in':False}
   assert (await client.post('/login/start')).status_code==200
   qr=await client.get('/login/qr');assert '<svg' in qr.text and qr.headers['cache-control']=='no-store'
   assert (await client.post('/login/poll')).json()['state']=='logged_in'
   info=(await client.get('/login/info')).json()
   assert info['logged_in'] and info['uid']=='123' and info['user_name']=='测试用户'
   assert info['space_free']==107374182400-1073741824
   assert not (await client.get('/login/info')).json().get('error')
   assert any(m['type']=='rpc' and m['method']=='data.set' and m['params']['key']=='qr-cookie' and m['params']['secret'] for m in messages)
   # 2.0.0：上传链路已移除——/watch 与 /uploads 路由不存在；STRM 生成完全由
   # 网盘枚举驱动（boot 后的自动增量或手动增量同步，网盘 mock 直接返回视频+云端附属）。
   assert (await client.post('/watch/scan')).status_code==404
   assert (await client.get('/uploads')).status_code==404
   for _ in range(300):
    if (root/'auto-strm/Movie.strm').exists():break
    st=(await client.get('/strm/status')).json()
    if st['state']!='running':
     r=await client.post('/strm/incremental');assert r.status_code in (200,409),r.text
    await asyncio.sleep(.03)
   assert (root/'auto-strm/Movie.strm').exists(),(await client.get('/strm/status')).json()
   for _ in range(200):
    if (root/'auto-strm/Movie.srt').exists():break
    await asyncio.sleep(.03)
   assert (root/'auto-strm/Movie.srt').exists() and (root/'auto-strm/Movie.srt').read_bytes() in (b'movie bytes',b'subtitle bytes'),'云端附属应经直链下载到 STRM 目录'
   # 2.2.1：网盘 mock 含一个名称非法的条目（坏\名字.mkv）——同步不得因此中止，
   # 好文件照常入库，异常条目计入 skipped 并写入运行日志（带原因与字段诊断）。
   for _ in range(100):
    st=(await client.get('/strm/status')).json()
    if st.get('skipped'):break
    if st['state']!='running':
     r=await client.post('/strm/incremental');assert r.status_code in (200,409),r.text
    await asyncio.sleep(.03)
   assert st.get('skipped')==1,st
   logs=(await client.get('/log/list')).json()
   assert any(e.get('level')=='warn' and '已跳过网盘异常条目' in e.get('message','') and '名称含' in e.get('message','') and '坏' in e.get('message','') for e in logs.get('events',[])),logs.get('events',[])[:3]

   r=await client.get('/browse');assert r.status_code==200 and r.json()['items'][0]['id']=='10'
   assert (await client.get('/panel')).status_code==200
   r=await client.post('/files',json={'action':'delete','file_id':'10','confirmed':True});assert r.status_code==403
   assert (await client.post('/sync',json={})).status_code==404
   assert (await client.post('/cancel')).status_code==404
   html=(await client.get('/panel')).text;assert '旧版' not in html and "api('/sync'" not in html
   assert 'need-login' in html and 'login-warn' in html and 'applyGates' in html,'未登录门控缺失'
   assert '[hidden]{display:none!important}' in html,'弹窗 hidden 属性会被 display:flex 覆盖，导致弹窗常驻且无法关闭'
   assert '正在向 115 获取二维码' in html and 'qrRefresh' in html,'扫码弹窗缺少加载反馈与过期自动刷新'
   assert 'f-strm_base_url' in html and 'diagNet' in html,'直连地址应可输入且需提供网络诊断'
   assert 'map-list' in html and 'sync-history' in html and 'exportConfig' in html and 'importConfig' in html,'1.6.0 多映射/运行历史/导出导入界面缺失'
   # v1.4.0 控制台配置接口
   cfg=(await client.get('/config')).json()
   assert cfg['values']['sync_cron']=='*/20 8-23 * * *' and 'cookie' in cfg['secrets_set']
   assert cfg['values']['video_suffixes'].startswith('mkv') and cfg['sample_play_url'].startswith('https://movie.example.com/')
   assert 'wechatmini' in cfg['devices'] and cfg['device']=='wechatmini'
   assert 'os_windows' not in cfg['devices'],'115 已下架电脑客户端，不应再出现在设备列表'
   runs=(await client.get('/sync/next-runs?expr=*%2F20+8-23+*+*+*')).json()['runs'];assert len(runs)==5
   assert (await client.get('/sync/next-runs?expr=bad')).status_code==400
   diag=(await client.get('/login/diag')).json();assert diag['ok'] and diag['elapsed']>=0 and set(diag['checks'])=={'token','confirm','cookie'},'网络诊断应同时报告取码、换Cookie与当前Cookie探测'
   r=await client.get('/dirs?cid=99');assert r.status_code==200 and r.json()['cid']=='99'
   assert (await client.post('/config',json={'values':{'sync_cron':'not a cron'}})).status_code==400
   assert (await client.post('/config',json={'values':{'not_a_field':1}})).status_code==400
   r=await client.post('/config',json={'values':{'sync_cron':'0 * * * *','min_video_mb':5}})
   assert r.json()=={'applied':True},r.text
   cfg2=(await client.get('/config')).json()
   assert cfg2['values']['sync_cron']=='0 * * * *' and cfg2['values']['min_video_mb']==5,'配置保存应立即生效'
   assert (await client.post('/config/reset',json={'fields':['sync_cron']})).json()=={'applied':True}
   cfg3=(await client.get('/config')).json();assert cfg3['values']['sync_cron']=='*/20 8-23 * * *'
   r=await client.post('/strm/incremental');assert r.status_code==200 and r.json()['mode']=='incremental'
   r=await client.post('/strm/full');assert r.status_code in (200,409)  # 增量可能仍在运行
   path=root/'auto-strm/Movie.strm';url=path.read_text().strip();assert '/play/' in url and 'secret' not in url
   assert scans  # 首次自动/增量同步有写入 → 通知扫描（2.7.13：全量不通知）
   public=url.split('/p115-library')[1]
   r=await client.get(public,headers={'User-Agent':'Test Player'});assert r.status_code==302 and r.headers['location'].startswith('https://cdn.example.com/')
   assert 'cookie' not in r.headers
   assert (await client.get('/play/unknown')).status_code==404
   # v1.7.4 播放诊断：协议×UA 签发探测 + CDN 拉流矩阵（含服务器中转模拟行、
   # 签发 UA 精确重放——1.7.3 真机全 403 正是探测没显式设空 UA 导致 httpx 发默认 UA）。
   # 此 mock 只会 RSA 形态应答，m115 两路失败后 RSA·空 UA 成功。
   diag=(await client.post('/play/test',json={'pickcode':'abcdef123456'})).json()
   ok=[s for s in diag['sign'] if s['ok']]
   assert ok and ok[0]['f']=='1' and ok[0]['host']=='cdn.example.com',diag
   assert any(not s['ok'] for s in diag['sign'][:2]),'m115 探测失败应留在报告中：'+repr(diag)
   assert diag['cdn'][0]['label'].startswith('服务器中转模拟'),'第一行应是中转模拟探测：'+repr(diag['cdn'][0])
   assert len(diag['cdn'])==7 and all(c['ok'] for c in diag['cdn']),diag
   # 可选播放器 UA 探测（302 真实链路，alist 模型）：按播放器 UA 签发+拉流
   diag2=(await client.post('/play/test',json={'pickcode':'abcdef123456','ua':'Test Player'})).json()
   ok2=[s for s in diag2['sign'] if s['ok']]
   assert ok2 and '播放器 UA' in ok2[0]['label'] and ok2[0]['f']=='1',diag2
   assert len(diag2['cdn'])==9 and all(c['ok'] for c in diag2['cdn']),diag2
   assert (await client.post('/play/test',json={'pickcode':'bad-code!'})).status_code==422
   assert (await client.post('/play/test',json={'pickcode':'abcdef123456','ua':'x\r\ny'})).status_code==422
   # v1.7.1 服务器中转（alist proxy 模式）：302 异常时的兜底播放方式
   r=await client.post('/config',json={'values':{'play_mode':'proxy'}});assert r.json()=={'applied':True},r.text
   r=await client.get(public);assert r.status_code==200 and r.content==b'movie bytes','proxy 模式应经服务器中转拉流'
   r=await client.head(public);assert r.status_code==200 and not r.content
   rng=await client.get(public,headers={'Range':'bytes=0-4'});assert rng.status_code==206 and rng.content==b'movie','Range 应透传给 CDN'
   assert (await client.post('/config',json={'values':{'play_mode':'bad'}})).status_code==400
   assert (await client.post('/config',json={'values':{'strm_conflict_policy':'bad'}})).status_code==400
   r=await client.post('/config',json={'values':{'strm_conflict_policy':'abort'}});assert r.json()=={'applied':True},r.text
   r=await client.post('/config',json={'values':{'strm_conflict_policy':'auto'}});assert r.json()=={'applied':True},r.text
   # v1.7.5 播放拉流日志：成功路径也要进运行日志（此前只记失败，用户看不到拉取情况）；
   # 中转拉流完成事件在响应流结束后异步写入，轮询等待。
   for _ in range(100):
    logs=(await client.get('/log/list')).json()
    if any(e['source']=='播放' and '中转拉流' in e['message'] for e in logs['events']):break
    await asyncio.sleep(.03)
   assert any(e['source']=='播放' and '中转拉流' in e['message'] for e in logs['events']),logs
   assert any(e['source']=='播放' and '302 直连' in e['message'] for e in logs['events']),logs
   await client.post('/config',json={'values':{'play_mode':'302'}})
   # v1.6.0 多目录映射、运行历史与配置导出导入
   await client.post('/config/reset',json={'fields':['min_video_mb']})  # 恢复不限体积，mock 文件只有几字节
   maps=json.dumps([{'name':'样本A','cloud_cid':'99','strm_directory':str(root/'map-a')},
                    {'name':'样本B','cloud_cid':'0','strm_directory':str(root/'map-b')}])
   r=await client.post('/config',json={'values':{'mappings':maps}});assert r.json()=={'applied':True},r.text
   st=(await client.get('/strm/status')).json();assert st['enabled'] and st['mapping_count']==2,st
   assert (await client.post('/config',json={'values':{'mappings':'not json'}})).status_code==400
   r=await client.post('/strm/full');assert r.status_code in (200,409)
   last={}
   for _ in range(150):
    last=(await client.get('/strm/status')).json()
    if (root/'map-a/Movie.strm').exists() and (root/'map-b/Sample Movie (2026).strm').exists():break
    await asyncio.sleep(.03)
   assert (root/'map-a/Movie.strm').exists(),'多映射 A 未生成 STRM：'+repr(last)+repr(r.json())
   assert (root/'map-b/Sample Movie (2026).strm').exists(),'多映射 B 未生成 STRM'
   hist=[]
   for _ in range(100):
    hist=(await client.get('/sync/history')).json()['history']
    if any(h['mode']=='full' for h in hist):break
    await asyncio.sleep(.03)
   full=[h for h in hist if h['mode']=='full' and h['time'] and not h['error']]
   # life-event triggered incremental checks may land in history before the
   # manual full sync (upload completion triggers an immediate reconcile), so
   # only require a successful full record to exist -- not hist[0].
   assert full,hist
   # v1.7.8 同步历史明细：耗时/扫描/分映射行
   detail=[h for h in full if h.get('elapsed') is not None and h.get('seen') and h.get('mappings')]
   assert detail,'成功的历史行应带耗时、扫描数与映射明细：'+repr(full[:2])
   assert all('CID' in m['name'] for m in detail[0]['mappings']),repr(detail[0]['mappings'])
   # v1.6.9 运行日志栏：事件 + 同步历史
   logs=(await client.get('/log/list')).json()
   assert logs['history'],'同步完成后运行日志应包含同步历史'
   assert any(e['source']=='系统' for e in logs['events']) and any(e['source']=='同步' for e in logs['events']),logs
   assert set(logs)=={'events','history'},'2.0.0 起日志接口不再包含上传失败区块'
   html=(await client.get('/panel')).text
   assert 'page-logs' in html and 'runtime-log' in html and '运行日志' in html,'面板缺少独立日志栏'
   assert 'refreshLogs' in html and 'log/list' in html,'日志栏缺少数据拉取'
   assert '监控上传' not in html and 'uploads/requeue' not in html and 'watch/scan' not in html,'2.0.0 上传功能应从面板彻底移除'
   assert 'play_mode' in html and '服务器中转' in html and 'playTest' in html,'1.7.1 播放方式与播放诊断未接入面板'
   assert 'data-field="strm_conflict_policy"' not in html,'2.7.7 同名冲突固定为保留较大，面板不再提供「中止同步」'
   assert 'rewrite-result' in html and 'rewriteStrm' in html and '/strm/rewrite' in html,'2.1.0 存量 STRM 地址替换未接入面板'
   exp=await client.get('/config/export');assert exp.status_code==200 and 'cookie' not in exp.text
   payload=exp.json();payload['values']['min_video_mb']=9
   r=await client.post('/config/import',json={'values':payload['values']});assert r.json()=={'applied':True},r.text
   cfg4=(await client.get('/config')).json()
   assert cfg4['values']['min_video_mb']==9 and json.loads(cfg4['values']['mappings'])==json.loads(maps),'导入应立即生效且保留映射'
   # v2.1.0 存量 STRM 地址替换：外网地址变更后，不重新枚举网盘即可刷新存量 STRM。
   # 预览不改文件；执行只换 origin（路径+签名保留）；已停用映射的目录不被触碰。
   map_a=root/'map-a/Movie.strm';assert map_a.exists()
   before=map_a.read_text()
   prev=None
   for _ in range(50):
    prev=await client.post('/strm/rewrite',json={'dry_run':True})
    if prev.status_code==200:break
    assert prev.status_code==409,prev.text  # 撞上正在运行的同步时等待重试
    await asyncio.sleep(.1)
   assert prev.status_code==200,prev.text
   prevj=prev.json()
   assert prevj['dry_run'] and prevj['unchanged']>=1 and prevj['rewritten']==0,prevj
   assert prevj['target'].startswith('https://movie.example.com'),prevj
   assert map_a.read_text()==before,'预览绝不修改文件'
   r=await client.post('/config',json={'values':{'strm_base_url':'http://new.example.com:9000'}});assert r.json()=={'applied':True},r.text
   rw=None
   for _ in range(50):
    rw=await client.post('/strm/rewrite',json={'dry_run':False})
    if rw.status_code==200:break
    assert rw.status_code==409,rw.text
    await asyncio.sleep(.1)
   assert rw.status_code==200,rw.text
   rwj=rw.json()
   assert rwj['rewritten']>=1 and rwj['target']=='http://new.example.com:9000',rwj
   for p in (root/'map-a/Movie.strm',root/'map-b/Sample Movie (2026).strm'):
    c=p.read_text();assert c.startswith('http://new.example.com:9000/') and '?sig=test' in c,str(p)+'：'+repr(c)
   auto=root/'auto-strm/Movie.strm'
   assert 'movie.example.com' in auto.read_text(),'已停用映射的目录不被地址替换触碰'
   # 替换后再次预览：全部已是最新；新地址播放链路 302 直达 CDN
   again=(await client.post('/strm/rewrite',json={'dry_run':True})).json()
   assert again['rewritten']==0 and again['unchanged']>=1,again
   new_url=map_a.read_text().strip()
   r=await client.get(new_url.split('/p115-library')[1],headers={'User-Agent':'Test Player'})
   assert r.status_code==302 and r.headers['location'].startswith('https://cdn.example.com/'),r.text
   await client.post('/config',json={'values':{'strm_base_url':''}})  # 恢复留空，不影响后续断言
   # v2.3.1 现场矩阵：宿主「设置 → 应用」未配置外部访问地址（此前表现为裸的
   # 「STRM 生成失败（ValueError），请查看日志或检查配置」——真实消息被吞掉）。
   await client.post('/config/reset',json={'fields':['min_video_mb']})  # mock 文件仅数字节，避免被体积过滤挡在签发之前
   host_flags['external_url']=False
   for _ in range(150):
    if (await client.get('/strm/status')).json()['state']!='running':break
    await asyncio.sleep(.03)
   r=await client.post('/strm/incremental');assert r.status_code in (200,409),r.text
   st=None
   for _ in range(150):
    st=(await client.get('/strm/status')).json()
    if st['state']=='failed' and '外部访问地址' in (st.get('error') or ''):break
    await asyncio.sleep(.03)
   assert st and '外部访问地址' in (st.get('error') or ''),'失败原因必须带修复指引：'+repr(st)
   assert 'ValueError' not in (st.get('error') or ''),'不得再显示裸异常类名：'+repr(st)
   rw=await client.post('/strm/rewrite',json={'dry_run':True})
   assert rw.status_code==400 and '外部访问地址' in rw.text,rw.text
   # 配置 strm直连地址后：签发走相对链接自行拼 origin，宿主没配外部地址也能完整同步
   r=await client.post('/config',json={'values':{'strm_base_url':'http://new.example.com:9000'}});assert r.json()=={'applied':True},r.text
   r=await client.post('/strm/incremental');assert r.status_code in (200,409),r.text
   # 轮询判定必须绕开第①步遗留的 failed 态（失败 tick 只覆盖 state/error 两键）：
   # 只有新一轮同步完成（completed 且 error 为空）才算通过。
   ok=None
   for _ in range(200):
    st=(await client.get('/strm/status')).json()
    if st['state']=='completed' and not (st.get('error') or ''):ok=st;break
    await asyncio.sleep(.03)
   assert ok,'配置直连地址后应走相对签发并完整同步：'+repr(st)
   assert any(m['type']=='rpc' and m['method']=='routes.sign' and m['params'].get('absolute') is False for m in messages),'成功同步必须使用相对签发'
   assert (root/'map-a/Movie.strm').read_text().startswith('http://new.example.com:9000/'),'直连地址场景 STRM origin 应为覆盖地址'
   host_flags['external_url']=True  # 还原，避免影响后续用例
  assert not failed,failed
  await send({'type':'dispose'})
  try:await asyncio.wait_for(disposed.wait(),5)
  except TimeoutError:
   proc.kill();print('\nCHILD STDERR:\n',(await proc.stderr.read()).decode()[-4000:]);raise
  assert await asyncio.wait_for(proc.wait(),5)==0
  stderr=(await proc.stderr.read()).decode();assert 'CID=secret' not in stderr
 finally:
  if proc.returncode is None:proc.kill();await proc.wait()
  sockpath.unlink(missing_ok=True)
  parent.close();pump_task.cancel();await asyncio.gather(pump_task,return_exceptions=True)
