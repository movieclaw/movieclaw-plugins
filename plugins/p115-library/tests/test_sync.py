import copy
import asyncio
import os
import time
from pathlib import Path
from types import SimpleNamespace
import pytest
from p115_plugin import Settings
from sync_engine import SyncEngine,scope_key
from drive_api import DriveError

class Store:
 def __init__(self):self.data={}
 async def get(self,key,default=None):return copy.deepcopy(self.data.get(key,default))
 async def set(self,key,value,**_):self.data[key]=copy.deepcopy(value)
 async def delete(self,key):self.data.pop(key,None)
class Files:
 def __init__(self,root):self.root=Path(root).resolve();self.fail_rename=False
 def check(self,path):
  p=Path(path).resolve();assert p.is_relative_to(self.root),'escape';return p
 async def exists(self,path):return self.check(path).exists()
 async def listdir(self,path):return os.listdir(self.check(path))
 async def makedirs(self,path):self.check(path).mkdir(parents=True,exist_ok=True)
 async def open(self,path,mode):return self.check(path).open(mode)
 async def rename(self,src,dst):
  if self.fail_rename:raise OSError('simulated disk error')
  self.check(src).replace(self.check(dst))
 async def remove(self,path):self.check(path).unlink()
class Ops:
 def __init__(self,root):self.root=str(root);self.calls=[];self.fail=False
 async def call(self,op,args):
  self.calls.append((op,args))
  if op=='library.get':return {'primary_root':self.root}
  if self.fail:raise RuntimeError('busy')
  return {'job_id':'scan1'}
class Routes:
 def __init__(self):self.external_url_missing=False;self.calls=[]
 async def sign(self,ctx,path,absolute):
  # Mirrors the host contract (plugin_routes.py): absolute signing needs the
  # host's「外部访问地址」and raises ValueError when unset; relative links
  # ('/api/v1/...') never do.
  self.calls.append(bool(absolute))
  if absolute and self.external_url_missing:raise ValueError('没有配置外部访问地址（设置 → 应用），无法生成完整链接')
  return ('https://movie.example.com' if absolute else '')+'/api/v1/plugins/p115-library'+path+'?sig=fake-signature'
class Drive:
 def __init__(self):self.items=[{'id':'1','path':'影片 (2026)/影片.mkv','name':'影片.mkv','pickcode':'abcdef123456','size':100,'sha1':'hash','mtime':'1'}];self.fail=False;self.fail_after=None;self.link_calls=[];self.bad_rows=[]
 async def walk(self,cid,cancel,bad_sink=None,skip_dirs=None):
  for entry in self.bad_rows:
   if bad_sink is not None:bad_sink.append(entry)
  for i,item in enumerate(self.items):
   if self.fail or (self.fail_after is not None and i>=self.fail_after):raise DriveError('分页失败')
   d=copy.deepcopy(item);d['parent_cid']=str(cid)
   yield d
  yield {'__end_dir__':True,'cid':str(cid),'parent_cid':None}
 async def link_signed(self,pickcode,ua=None,allow_cookie_url=False,fresh=False):
  self.link_calls.append(pickcode);return 'https://cdn.example.com/dl/'+pickcode,'Mozilla/5.0',{}

def play_records(store):
 """All persisted play records: 2.7.3 shards ('playx:') plus legacy 'play:' keys."""
 out=[]
 for k,v in store.data.items():
  if k.startswith('playx:'):out.extend(v.values())
  elif k.startswith('play:'):out.append(v)
 return out

def setup(root,**cfg):
 root.mkdir(exist_ok=True)
 mapping=SimpleNamespace(cid='0',library_id=1,subdirectory='115',model_dump=lambda:{'cid':'0','library_id':1,'subdirectory':'115'})
 settings=SimpleNamespace(**{**Settings().model_dump(),'parsed_mappings':[mapping],'cleanup_missing':False,**cfg})
 store=Store();files=Files(root);ops=Ops(root);drive=Drive()
 return SyncEngine(settings,drive,store,files,ops,Routes(),None)

@pytest.mark.asyncio
async def test_end_to_end_preview_sync_restart_incremental(tmp_path):
 engine=setup(tmp_path)
 first=await engine.run(dry_run=True);assert first['written']==1 and not list(tmp_path.rglob('*.strm'))
 result=await engine.run(dry_run=False);assert result['written']==1
 target=next(tmp_path.rglob('*.strm'));assert '?sig=' in target.read_text() and 'Cookie' not in target.read_text()
 assert engine.ops.calls[-1][0]=='library.scan.start'  # 默认增量同步：有写入 → 通知扫描
 restarted=SyncEngine(engine.cfg,engine.drive,engine.store,engine.files,engine.ops,engine.routes,None)
 result=await restarted.run();assert result['unchanged']==1 and result['written']==0
 restarted.drive.items[0]['mtime']='2';result=await restarted.run();assert result['written']==1
 assert not list(tmp_path.rglob('*.tmp'))

@pytest.mark.asyncio
async def test_strm_base_url_rewrites_origin(tmp_path):
 engine=setup(tmp_path,strm_base_url='http://www.wenmovie.cloud:9577');await engine.run()
 target=next(tmp_path.rglob('*.strm'));content=target.read_text()
 assert content.startswith('http://www.wenmovie.cloud:9577/api/v1/plugins/p115-library/play/')
 assert '?sig=' in content and 'movie.example.com' not in content,'签名保留、原始主机替换'

@pytest.mark.asyncio
async def test_host_without_external_url_fails_with_guidance_not_bare_valueerror(tmp_path):
 # 2.3.1 现场：宿主「设置 → 应用」没配外部访问地址时，绝对签发以 ValueError 上升。
 # 同步必须以带修复指引的 DriveError 失败，而不是裸的「（ValueError）请查看日志」。
 engine=setup(tmp_path);engine.routes.external_url_missing=True
 with pytest.raises(DriveError,match='外部访问地址') as e:await engine.run()
 assert '设置 → 应用' in str(e.value) and 'strm直连地址' in str(e.value),str(e.value)
 assert 'ValueError' not in str(e.value)
 assert not list(tmp_path.rglob('*.strm')),'报错时不得留下半成品'

@pytest.mark.asyncio
async def test_base_url_override_bypasses_missing_host_external_url(tmp_path):
 # 配置了 strm直连地址后，签发走相对链接并自行拼 origin——宿主没配外部地址也能完整同步。
 engine=setup(tmp_path,strm_base_url='http://new.example.com:9000');engine.routes.external_url_missing=True
 result=await engine.run();assert result['written']==1,result
 content=next(tmp_path.rglob('*.strm')).read_text()
 assert content.startswith('http://new.example.com:9000/api/v1/plugins/p115-library/play/') and '?sig=' in content
 assert engine.routes.calls and not any(engine.routes.calls),'直连地址场景应全部使用相对签发：'+repr(engine.routes.calls)
 # 不配直连地址时恢复正常走绝对签发（独立目录，避免所有权保护误伤）
 engine2=setup(tmp_path/'second',strm_base_url='');await engine2.run()
 assert engine2.routes.calls and all(engine2.routes.calls)

# ---------- 2.4.0：撞到风控自动等待后继续；STRM 随扫描同步生长 ----------
@pytest.mark.asyncio
async def test_throttle_wait_is_visible_then_cleared(tmp_path):
 # 现场问题：撞上限流后计数器一段时间不动，看上去像卡死。这里保证等待期间
 # 面板有「冷却剩余秒数」，恢复后立即清除。
 engine=setup(tmp_path)
 await engine.throttle_begin(90,2,5,'115 接口失败，错误码 590075：操作太频繁（疑似风控）')
 assert engine.status['throttle_wait']==90
 assert engine.status['throttle_until']>=int(time.time())
 assert (engine.status['throttle_count'],engine.status['throttle_limit'])==(2,5)
 assert '590075' in engine.status['throttle_reason']
 logs=await engine.store.get('runtime-log',default=[])
 assert any('限速' in row['message'] and row['level']=='warn' for row in logs),logs
 await engine.throttle_end()
 assert engine.status['throttle_until']==0 and engine.status['throttle_wait']==0,'恢复后必须清除冷却提示'
 # 引擎构造时已把回调挂到 drive：真正的限流等待会上报同一套状态
 await engine.drive.on_wait(45,1,5,'boom')
 assert engine.status['throttle_wait']==45 and engine.status['throttle_count']==1
 await engine.drive.on_resume()
 assert engine.status['throttle_until']==0

@pytest.mark.asyncio
async def test_scan_and_write_overlap_instead_of_serializing(tmp_path):
 # 「边扫边写」的真正判据：总耗时接近 max(扫描, 写入) 而不是两者相加。
 # 2.6.0 是目录粒度流式：目录甲写完的同时在扫目录乙。把写入逐个放慢到
 # 0.05s、扫描每目录 0.04s——串行（扫完整个库再写）约 1.8s，并行约 0.9s。
 count=20
 items=[{'id':str(i),'path':'库/目录%02d/影片.mkv'%i,'name':'影片.mkv','pickcode':'abcdef12345%d'%i,
         'size':100,'sha1':'hash%d'%i,'mtime':'1'} for i in range(count)]
 engine=setup(tmp_path)
 async def paced_walk(cid,cancel,bad_sink=None,skip_dirs=None):
  for i,item in enumerate(items):
   await asyncio.sleep(0.04)  # 网络节流下的枚举节奏
   d=dict(item);d['parent_cid']='d%02d'%i;yield d
   yield {'__end_dir__':True,'cid':'d%02d'%i,'parent_cid':str(cid)}
 engine.drive.walk=paced_walk
 original=engine.files.rename
 async def slow_rename(src,dst):
  await asyncio.sleep(0.05)  # 宿主文件 RPC 的成本
  return await original(src,dst)
 engine.files.rename=slow_rename
 loop=asyncio.get_event_loop()
 started=loop.time();result=await engine.run();elapsed=loop.time()-started
 writes=count*0.05;walk=count*0.04
 assert result['written']==count,result
 assert len(list(tmp_path.rglob('*.strm')))==count
 assert elapsed<(writes+walk)*0.85,'耗时 %.2fs 接近扫描+写入之和（%.2fs），说明仍在串行执行'%(elapsed,writes+walk)

@pytest.mark.asyncio
async def test_writer_failure_stops_the_walk_immediately(tmp_path):
 # 写入失败必须立刻中止整轮（而不是把网盘扫完才报错，更不能在队列上卡住）。
 items=[{'id':str(i),'path':'库/%02d.mkv'%i,'name':'%02d.mkv'%i,'pickcode':'abcdef123456',
         'size':100,'sha1':'h','mtime':'1'} for i in range(40)]
 engine=setup(tmp_path)
 original=engine.files.open
 async def flaky_open(path,mode):
  if str(path).endswith(('.tmp','.strm')):raise OSError('simulated disk full')
  return await original(path,mode)
 engine.files.open=flaky_open
 async def paced_walk(cid,cancel,bad_sink=None,skip_dirs=None):
  for item in items:
   await asyncio.sleep(0.01);d=dict(item);d['parent_cid']=str(cid);yield d
  yield {'__end_dir__':True,'cid':str(cid),'parent_cid':None}
 engine.drive.walk=paced_walk
 started=asyncio.get_event_loop().time()
 with pytest.raises(OSError):await engine.run()
 assert asyncio.get_event_loop().time()-started<10,'写入失败后必须迅速收尾（不得干等到扫描结束）'
 assert 'simulated disk full' in engine.status['error'],engine.status['error']

@pytest.mark.asyncio
async def test_unexpected_exception_text_is_not_swallowed(tmp_path):
 # 2.3.1：非 DriveError 异常的真实消息必须进入失败状态（此前只剩异常类名）。
 engine=setup(tmp_path)
 async def boom(op,args):raise RuntimeError('boom detail 42')
 engine.ops.call=boom
 with pytest.raises(RuntimeError):await engine.run()
 assert 'boom detail 42' in engine.status['error'] and 'RuntimeError' in engine.status['error'],engine.status['error']

@pytest.mark.asyncio
async def test_user_files_never_overwritten(tmp_path):
 engine=setup(tmp_path);target=tmp_path/'115/影片 (2026)/影片.strm';target.parent.mkdir(parents=True);target.write_text('https://my-own-url')
 result=await engine.run()  # 2.7.0：跳过并计数，不再中止整个同步
 assert result['skipped']==1 and result['written']==0 and target.read_text()=='https://my-own-url'

@pytest.mark.asyncio
async def test_owned_modified_files_never_overwritten(tmp_path):
 engine=setup(tmp_path);await engine.run();target=next(tmp_path.rglob('*.strm'));target.write_text('user edited')
 result=await engine.run('full')
 assert result['skipped']==1 and target.read_text()=='user edited'

@pytest.mark.asyncio
async def test_missing_cleanup_opt_in_only_owned(tmp_path):
 engine=setup(tmp_path,cleanup_missing=True);await engine.run();target=next(tmp_path.rglob('*.strm'))
 arbitrary=target.parent/'others.strm';arbitrary.write_text('other')
 engine.drive.items=[];await engine.run(dry_run=True);assert target.exists()
 await engine.run();assert not target.exists() and arbitrary.exists()

@pytest.mark.asyncio
async def test_cleanup_failure_and_modified_protection(tmp_path):
 engine=setup(tmp_path,cleanup_missing=True);await engine.run();target=next(tmp_path.rglob('*.strm'));target.write_text('user edit')
 engine.drive.items=[{**engine.drive.items[0],'id':'2'}];engine.drive.fail=True
 with pytest.raises(DriveError):await engine.run()
 assert target.exists()
 engine.drive.fail=False;engine.drive.items=[];await engine.run();assert target.read_text()=='user edit'

@pytest.mark.asyncio
async def test_cleanup_disabled(tmp_path):
 engine=setup(tmp_path);await engine.run();target=next(tmp_path.rglob('*.strm'));engine.drive.items=[];await engine.run();assert target.exists()

@pytest.mark.asyncio
async def test_bad_cloud_entry_skipped_logged_and_counted(tmp_path):
 # 2.2.1：网盘单条异常不再中止整个同步——好文件照常入库，异常条目计数进
 # skipped 并写入运行日志（带原因与字段诊断）
 engine=setup(tmp_path)
 engine.drive.bad_rows=[{'dir':'/','index':1,'reason':'名称含 /、\\ 或空字节','detail':'n=坏\\\\名字.mkv fid=9 字段=[fid,cid,n,s]'}]
 result=await engine.run()
 assert result['written']==1 and result['skipped']==1
 target=next(tmp_path.rglob('*.strm'));assert 'sig=fake-signature' in target.read_text()
 log=await engine.store.get('runtime-log',default=[])
 assert any(r['level']=='warn' and '已跳过网盘异常条目' in r['message'] and '坏' in r['message'] for r in log),log

@pytest.mark.asyncio
async def test_streaming_writes_tree_as_it_scans(tmp_path):
 # 2.6.0 目录粒度边扫边写：已完成的目录随扫随写（写好即可播），失败目录的
 # 缓冲文件不落盘（不留半成品）；断点只记已完成目录，下轮跳过它们、重扫补齐。
 engine=setup(tmp_path)
 a=[{'id':'1','path':'库/甲/影片.mkv','name':'影片.mkv','pickcode':'abcdef123456','size':100,'sha1':'hash','mtime':'1'}]
 b=[{'id':'2','path':'库/乙/第二部.mkv','name':'第二部.mkv','pickcode':'second123456','size':50,'sha1':'hash2','mtime':'1'}]
 async def walk(cid,cancel,bad_sink=None,skip_dirs=None):
  d=dict(a[0]);d['parent_cid']='a';yield d
  yield {'__end_dir__':True,'cid':'a','parent_cid':str(cid)}
  d=dict(b[0]);d['parent_cid']='b';yield d
  raise DriveError('分页失败')
 engine.drive.walk=walk
 with pytest.raises(DriveError):await engine.run()
 written=list(tmp_path.rglob('*.strm'))
 assert len(written)==1,'已完成目录甲的文件应已写出：'+str([str(p) for p in written])
 plays=play_records(engine.store)
 assert len(plays)==1 and plays[0]['pickcode']=='abcdef123456','已写出的文件应可立即播放'
 ckpt=[v for k,v in engine.store.data.items() if k.startswith('checkpoint:')]
 done=await engine.load_checkpoint('checkpoint:'+scope_key(engine.cfg.parsed_mappings[0]))
 assert 'a' in done and 'b' not in done,'断点只记已完成目录：'+repr(done)

@pytest.mark.asyncio
async def test_full_resume_skips_completed_dirs(tmp_path):
 # 2.6.0 断点续传核心：第一次全量把「已完成的目录」写进本地 checkpoint；
 # 第二次全量把整棵已完成子树作为 skip_dirs 传给 walk，不再重复枚举；增量仍全枚举。
 engine=setup(tmp_path)
 tree={'0':[{'id':'a','name':'甲','dir':True},{'id':'b','name':'乙','dir':True}],
       'a':[{'id':'1','name':'甲.mkv','dir':False}],
       'b':[{'id':'2','name':'乙.mkv','dir':False}]}
 enumerated=[];skip_log=[]
 def mk(r):return {'id':r['id'],'name':r['name'],'is_dir':r['dir'],'size':100,'pickcode':'pc'+r['id'],'sha1':'h','mtime':'1'}
 async def walk(cid,cancel,bad_sink=None,skip_dirs=None):
  skip_log.append(set(skip_dirs or ()))
  stack=[(str(cid),'',None)]
  while stack:
   cur,rel,parent=stack.pop()
   if skip_dirs and cur in skip_dirs and cur!=str(cid):continue
   for r in tree[cur]:
    full=rel+'/'+r['name'] if rel else r['name']
    if r['dir']:stack.append((r['id'],full,cur))
    else:
     it=mk(r);enumerated.append(full);yield {**it,'path':full,'parent_cid':cur}
   yield {'__end_dir__':True,'cid':cur,'parent_cid':parent}
 engine.drive.walk=walk
 result=await engine.run('full')
 assert result['written']==2,result
 assert sorted(enumerated)==['乙/乙.mkv','甲/甲.mkv'],enumerated
 done=await engine.load_checkpoint('checkpoint:'+scope_key(engine.cfg.parsed_mappings[0]))
 assert set(done)>={'0','a','b'},'整棵树完成都记入断点：'+repr(done)
 enumerated.clear()
 result=await engine.run('full')
 assert result['written']==0 and result['seen']==0,'续传应零枚举、零写入：'+repr(result)
 assert not enumerated,'已完成目录的文件不得再次枚举：'+repr(enumerated)
 assert skip_log and 'a' in skip_log[-1] and 'b' in skip_log[-1],'skip_dirs 应包含已完成目录：'+repr(skip_log[-1])
 enumerated.clear()
 result=await engine.run('incremental')
 assert sorted(enumerated)==['乙/乙.mkv','甲/甲.mkv'],'增量应全枚举捕捉变化：'+repr(enumerated)

@pytest.mark.asyncio
async def test_resume_after_failure_skips_completed_dirs(tmp_path):
 # 2.6.0：中途失败也要保留已完成目录的断点；下次全量跳过它们、只重扫未完成部分。
 engine=setup(tmp_path)
 # DFS 后进先出：把「甲」放后面让它先枚举完成，「乙」排前面、枚举时失败
 tree={'0':[{'id':'b','name':'乙','dir':True},{'id':'a','name':'甲','dir':True}],
       'a':[{'id':'1','name':'甲.mkv','dir':False}],
       'b':[{'id':'2','name':'乙.mkv','dir':False}]}
 fail_at=[None];enumerated=[]
 def mk(r):return {'id':r['id'],'name':r['name'],'is_dir':r['dir'],'size':100,'pickcode':'pc'+r['id'],'sha1':'h','mtime':'1'}
 async def walk(cid,cancel,bad_sink=None,skip_dirs=None):
  stack=[(str(cid),'',None)]
  while stack:
   cur,rel,parent=stack.pop()
   if skip_dirs and cur in skip_dirs and cur!=str(cid):continue
   if cur==fail_at[0]:raise DriveError('分页失败')
   for r in tree[cur]:
    full=rel+'/'+r['name'] if rel else r['name']
    if r['dir']:stack.append((r['id'],full,cur))
    else:
     it=mk(r);enumerated.append(full);yield {**it,'path':full,'parent_cid':cur}
   yield {'__end_dir__':True,'cid':cur,'parent_cid':parent}
 engine.drive.walk=walk
 fail_at[0]='b'
 with pytest.raises(DriveError):await engine.run('full')
 done=await engine.load_checkpoint('checkpoint:'+scope_key(engine.cfg.parsed_mappings[0]))
 assert 'a' in done and 'b' not in done,'中断后断点只记已完成目录：'+repr(done)
 fail_at[0]=None;enumerated.clear()
 result=await engine.run('full')
 assert sorted(enumerated)==['乙/乙.mkv'],'续传应只枚举未完成目录：'+repr(enumerated)
 assert result['written']==1,'未完成目录应重建 STRM：'+repr(result)

@pytest.mark.asyncio
async def test_checkpoint_chunking_stays_under_64kb(tmp_path):
 # 2.6.2：宿主的插件数据单值上限 64 KB。大库断点记录（上万目录）必须分片存储，
 # 否则 save 直接抛「值超过 64 KB」，导致同步失败。
 import json as _json
 engine=setup(tmp_path)
 key='checkpoint:chunk-scope'
 big_done={str(1000000000000000000+i):'1' for i in range(8000)}
 await engine.save_checkpoint(key,big_done)
 for k,v in engine.store.data.items():
  assert len(_json.dumps(v).encode())<65536,'分片后单值仍超 64 KB：%s (%d 字节)'%(k[:50],len(_json.dumps(v).encode()))
 back=await engine.load_checkpoint(key)
 assert back==big_done,'分片读写应无损：%d 目录'%len(back)
 # 覆盖写（下一轮同步更新断点）应清理旧 node、读回正确
 await engine.save_checkpoint(key,{str(i):'1' for i in range(500)})
 back2=await engine.load_checkpoint(key)
 assert len(back2)==500,'覆盖写后应只保留最新断点：%d'%len(back2)

@pytest.mark.asyncio
async def test_checkpoint_appends_then_compacts(tmp_path):
 # 2.6.3：断点增量追加——每次只写新增目录、不重写全量；节点攒多后合并。
 engine=setup(tmp_path)
 key='checkpoint:append';flushed=set();done={}
 for i in range(40):
  done[str(1000000000+i)]='1'
  await engine.append_checkpoint(key,done,flushed)
 back=await engine.load_checkpoint(key)
 assert back==done,'增量追加后读回应无损：%d 目录'%len(back)
 root=engine.store.data.get(key)
 assert root and len(root['nodes'])<10,'追加过多后应合并回少数节点：%d'%len(root.get('nodes',[]))

@pytest.mark.asyncio
async def test_fast_full_trusts_manifest_and_heals_deletion(tmp_path):
 # 2.3.0：快速模式对全量同步同样生效——健康库全量零读回（信任同步记录）；
 # 文件缺失仍会被 exists 检查发现并重建（快速模式只省读回，不省修复缺失）
 engine=setup(tmp_path);await engine.run()
 engine.cfg.sync_mode='fast'
 reads=[]
 orig=engine.files.open
 async def counting(path,mode):
  if mode=='rb':reads.append(path)
  return await orig(path,mode)
 engine.files.open=counting
 result=await engine.run('full')
 assert result['written']==0 and result['unchanged']==1 and not reads,'快速全量应零读回、零写入：'+repr(result)
 target=next(tmp_path.rglob('*.strm'));target.unlink()
 result=await engine.run('full')
 assert result['written']==1 and target.exists(),'快速模式仍必须修复被删除的文件：'+repr(result)

@pytest.mark.asyncio
async def test_conflict_auto_keeps_larger_file(tmp_path):
 # 1.7.7: X.mkv + X.mp4 in one directory squeeze onto one STRM. Default policy
 # must NOT abort the sync -- keep the bigger file, log the loser, count it.
 engine=setup(tmp_path)
 engine.drive.items.append({**engine.drive.items[0],'id':'2','path':'影片 (2026)/影片.mp4','size':500,'pickcode':'largerpick'})
 result=await engine.run()
 # 2.6.0 目录粒度流式：同名冲突在目录枚举完时当场消解，只有胜者（较大者）
 # 入队写一次——不再先写小文件再被大文件覆盖（少一次落盘与淘汰者播放记录清理）
 assert result['written']==1 and result['conflicts']==1,'冲突应当场消解而不是中止：'+repr(result)
 assert len(list(tmp_path.rglob('*.strm')))==1
 plays=play_records(engine.store)
 assert len(plays)==1 and plays[0]['pickcode']=='largerpick','应保留体积较大的文件且淘汰者播放记录不残留：'+repr(plays)
 row=engine.status['mappings'][0]
 assert row['conflicts']==1 and '影片.mp4' in ''.join(row['conflict_detail']),'冲突明细应点名落选文件：'+repr(row)

@pytest.mark.asyncio
async def test_conflict_resolution_stable_and_self_heals(tmp_path):
 engine=setup(tmp_path)
 engine.drive.items.append({**engine.drive.items[0],'id':'2','path':'影片 (2026)/影片.mp4','size':500,'pickcode':'largerpick'})
 await engine.run()
 engine.drive.items=[engine.drive.items[1]]  # user removes the loser (mkv); the winner (mp4) stays
 result=await engine.run()
 assert result['written']==0 and result['unchanged']==1 and result['conflicts']==0,'删除落选者后应零改写：'+repr(result)

@pytest.mark.asyncio
async def test_old_abort_setting_no_longer_stops_sync_and_logs_nothing_per_conflict(tmp_path):
 # 2.7.7：「中止同步」选项已移除——旧设置里残留的 abort 按「保留较大」处理；
 # 冲突不再逐条写运行日志，只计数（同步历史保留前 10 条明细）。
 assert Settings(strm_conflict_policy='abort').strm_conflict_policy=='auto'
 engine=setup(tmp_path,strm_conflict_policy='abort')
 engine.drive.items.append({**engine.drive.items[0],'id':'2','path':'影片 (2026)/影片.mp4','size':500,'pickcode':'largerpick'})
 result=await engine.run()
 assert result['conflicts']==1 and result['written']==1
 assert len(list(tmp_path.rglob('*.strm')))==1
 logs=engine.store.data.get('runtime-log',[])
 assert not any('同名 STRM 冲突' in r['message'] for r in logs),'冲突不应逐条刷运行日志'
 assert '影片.mkv' in ''.join(result['mappings'][0]['conflict_detail'])

@pytest.mark.asyncio
async def test_run_reports_detailed_history_fields(tmp_path):
 # 1.7.8: the sync history needs elapsed/seen plus one detail row per mapping.
 engine=setup(tmp_path);result=await engine.run()
 assert result['elapsed']>=0 and result['seen']==1,'run 结果应带耗时与扫描数：'+repr(result)
 rows=result['mappings'];assert len(rows)==1,'每个映射一行明细：'+repr(rows)
 assert rows[0]['written']==1 and rows[0]['seen']==1 and 'CID 0' in rows[0]['name'],repr(rows[0])
 assert rows[0]['conflict_detail']==[],repr(rows[0])
 result=await engine.run()
 assert result['mappings'][0]['written']==0 and result['mappings'][0]['unchanged']==1,'第二轮明细应体现未变化：'+repr(result['mappings'])

@pytest.mark.asyncio
async def test_pagination_replay_same_file_not_conflict(tmp_path):
 # The same file enumerated twice (listings shift while uploads race the walk)
 # is a pagination replay, not a conflict.
 engine=setup(tmp_path)
 engine.drive.items.append(copy.deepcopy(engine.drive.items[0]))
 result=await engine.run()
 assert result['written']==1 and result['conflicts']==0,'同文件重复枚举应被忽略：'+repr(result)
 assert len(list(tmp_path.rglob('*.strm')))==1

@pytest.mark.asyncio
async def test_only_incremental_syncs_notify_media_library_scan(tmp_path):
 # 2.7.13：全量同步只报完成、不通知扫描；增量同步有变化时通知，无变化不通知。
 scans=lambda e:[a for op,a in e.ops.calls if op=='library.scan.start']
 engine=setup(tmp_path)
 result=await engine.run('full')
 assert result['state']=='completed' and result['written']==1 and scans(engine)==[],'全量同步不通知扫描'
 engine.drive.items.append({**engine.drive.items[0],'id':'2','path':'新片/新片.mkv','name':'新片.mkv','pickcode':'newpick00001'})
 result=await engine.run('incremental')
 assert result['written']==1 and len(scans(engine))==1,'增量同步写入了新文件 → 通知扫描'
 assert scans(engine)[0]['body']['paths']==[str(tmp_path/'115')]
 result=await engine.run('incremental')
 assert result['written']==0 and len(scans(engine))==1,'增量无变化不通知'
 engine.drive.items.append({**engine.drive.items[0],'id':'3','path':'又一部/又一部.mkv','name':'又一部.mkv','pickcode':'newpick00002'})
 await engine.run('full');assert len(scans(engine))==1,'全量即使有变化也不通知'

@pytest.mark.asyncio
async def test_incremental_scan_failure_is_not_a_sync_failure_and_is_resent(tmp_path):
 engine=setup(tmp_path);engine.ops.fail=True;engine.scan_retry_delay=0
 result=await engine.run('incremental')
 assert result['state']=='completed' and result['written']==1 and result['scan_pending']
 assert len([1 for op,_ in engine.ops.calls if op=='library.scan.start'])==2,'失败重试一次'
 logs=[r['message'] for r in engine.store.data.get('runtime-log',[])]
 assert any('通知 MovieClaw 扫描媒体库失败（busy）' in m for m in logs),logs
 engine.ops.fail=False
 result=await engine.run('incremental')
 assert result['written']==0 and not result['scan_pending'] and engine.ops.calls[-1][0]=='library.scan.start','下次增量补发'

@pytest.mark.asyncio
async def test_autoops_scan_notify_timeout_degrades_to_notice(tmp_path):
 # The upload-notify path (AutoOps) must also be bounded: its library.list /
 # library.scan.start calls turn a busy host into a visible notice, never a
 # hang and never a sync failure.
 from life_strm import AutoOps
 from types import SimpleNamespace as NS
 class HangingOps:
  async def call(self,op,args):await asyncio.sleep(999)
 ops=AutoOps(NS(strm_directory='/x'),HangingOps())
 ops.list_rpc_timeout=.05
 result=await asyncio.wait_for(ops.call('library.scan.start',{'body':{'paths':['/x/sub']}}),5)
 assert result.get('timeout') and '媒体库查询超时' in ops.notice,'library.list 挂起应降级为提示：'+repr(ops.notice)
 class ScanHang:
  async def call(self,op,args):
   if op=='library.list':return [{'id':'lib1','primary_root':'/x'}]
   await asyncio.sleep(999)
 ops=AutoOps(NS(strm_directory='/x'),ScanHang());ops.scan_rpc_timeout=.05
 result=await asyncio.wait_for(ops.call('library.scan.start',{'body':{'paths':['/x/sub']}}),5)
 assert result=={'matched_libraries':[]},'scan.start 挂起不应报错：'+repr(result)
 assert '扫描启动超时' in ops.notice
 ops2=AutoOps(NS(strm_directory='/x'),ScanHang());ops2.scan_rpc_timeout=.05
 assert (await ops2.call('library.get',{'library_id':'auto:u:/x'}))=={'primary_root':'/x'}

@pytest.mark.asyncio
async def test_interrupted_write_recovery(tmp_path):
 # 2.7.3：新文件直接写入（无 temp+rename）；覆盖已有文件仍走 temp+rename，失败不留 .tmp
 engine=setup(tmp_path);await engine.run();target=next(tmp_path.rglob('*.strm'));before=target.read_text()
 engine.drive.items[0]['mtime']='2';engine.drive.items[0]['pickcode']='newpick12345'
 engine.files.fail_rename=True
 with pytest.raises(OSError):await engine.run()
 assert not list(tmp_path.rglob('*.tmp')) and target.read_text()==before
 engine.files.fail_rename=False;result=await engine.run();assert result['written']==1 and len(list(tmp_path.rglob('*.strm')))==1

@pytest.mark.asyncio
async def test_cloud_sidecar_size_limit_skipped(tmp_path,monkeypatch):
 # 2.0.0：附属文件只来自网盘（经直链下载），SIDECAR_MAX 过滤同样生效
 import sync_engine
 monkeypatch.setattr(sync_engine,'SIDECAR_MAX',5)
 engine=setup(tmp_path)
 engine.drive.items.append({'id':'9','path':'影片 (2026)/影片.srt','name':'影片.srt','pickcode':'sub123456','size':10,'sha1':'sha-one','mtime':'3'})
 result=await engine.run()
 assert (tmp_path/'115/影片 (2026)/影片.strm').exists()
 assert not (tmp_path/'115/影片 (2026)/影片.srt').exists()
 assert engine.drive.link_calls==[],'超限附属不应发起直链下载'

@pytest.mark.asyncio
async def test_cleanup_removes_owned_strm_and_sidecars_only(tmp_path,monkeypatch):
 # 附属文件由云端直链下载而来；清理只删插件生成且未被修改的 STRM 与附属副本
 import httpx
 engine=setup(tmp_path,cleanup_missing=True)
 engine.drive.items.append({'id':'9','path':'影片 (2026)/影片.srt','name':'影片.srt','pickcode':'sub123456','size':14,'sha1':'sha-one','mtime':'3'})
 def respond(req):return httpx.Response(200,content=b'cloud subtitle')
 monkeypatch.setattr('sync_engine.net.http_transport',lambda _:httpx.MockTransport(respond))
 await engine.run()
 folder=tmp_path/'115/影片 (2026)'
 other=folder/'others.strm';other.write_text('keep')
 engine.drive.items=[]
 await engine.run(dry_run=True)
 assert (folder/'影片.strm').exists() and (folder/'影片.srt').exists()
 result=await engine.run()
 assert not (folder/'影片.strm').exists() and not (folder/'影片.srt').exists()
 assert other.exists() and result['deleted']==2

@pytest.mark.asyncio
async def test_cleanup_keeps_user_modified_sidecar(tmp_path,monkeypatch):
 import httpx
 engine=setup(tmp_path,cleanup_missing=True)
 engine.drive.items.append({'id':'9','path':'影片 (2026)/影片.srt','name':'影片.srt','pickcode':'sub123456','size':14,'sha1':'sha-one','mtime':'3'})
 def respond(req):return httpx.Response(200,content=b'cloud subtitle')
 monkeypatch.setattr('sync_engine.net.http_transport',lambda _:httpx.MockTransport(respond))
 await engine.run()
 folder=tmp_path/'115/影片 (2026)'
 (folder/'影片.srt').write_text('user edit',encoding='utf-8')
 engine.drive.items=[];await engine.run()
 assert not (folder/'影片.strm').exists()
 assert (folder/'影片.srt').read_text(encoding='utf-8')=='user edit'


@pytest.mark.asyncio
async def test_full_verifies_and_heals_but_skips_unchanged(tmp_path):
 # 2.0.2：全量不再无条件重写每个文件——健康库全量 written=0/unchanged=N。
 # 修复能力保留：文件被外部删除 → 全量发现缺失 → 重写重建。
 # 注意：内容被改的文件无法与用户手改区分，所有权保护仍会拒绝覆盖（防误删语义不变）。
 engine=setup(tmp_path);await engine.run()
 result=await engine.run('full')
 assert result['written']==0 and result['unchanged']==1,'健康库全量应校验但跳过重写：'+repr(result)
 target=next(tmp_path.rglob('*.strm'));good=target.read_bytes()
 target.unlink()
 result=await engine.run('full')
 assert result['written']==1 and target.read_bytes()==good,'全量必须重建缺失文件'
 # 播放地址配置变化 → 全量校验发现内容差异（记录与内容均属插件）→ 重写为新地址
 engine.cfg.strm_base_url='http://rewrite.example:9000'
 result=await engine.run('full')
 assert result['written']==1 and target.read_text().startswith('http://rewrite.example:9000/'),'配置变化后全量必须重写'

@pytest.mark.asyncio
async def test_keep_suffix_and_link_format(tmp_path):
 engine=setup(tmp_path,strm_keep_suffix=True,strm_link_format='pick_code_name')
 await engine.run()
 target=tmp_path/'115/影片 (2026)/影片.mkv.strm'
 assert target.exists()
 content=target.read_text()
 assert '?/%E5%BD%B1%E7%89%87.mkv' in content and content.endswith('\n')

@pytest.mark.asyncio
async def test_exist_policy_skip_then_overwrite_heals(tmp_path):
 engine=setup(tmp_path,strm_exist_policy='skip');await engine.run()
 target=next(tmp_path.rglob('*.strm'));original=target.read_text()
 engine.drive.items=[{**engine.drive.items[0],'id':'2','pickcode':'abcdef999999','sha1':'new','mtime':'2'}]
 result=await engine.run()
 assert result['written']==0 and target.read_text()==original  # 直接跳过，旧内容保留
 engine.cfg.strm_exist_policy='overwrite'
 result=await engine.run()
 assert result['written']==1 and target.read_text()!=original  # 切回覆盖生成后自愈

@pytest.mark.asyncio
async def test_fast_mode_trusts_manifest_without_reading(tmp_path):
 engine=setup(tmp_path,sync_mode='fast');await engine.run()
 target=next(tmp_path.rglob('*.strm'));target.write_text('foreign content')
 result=await engine.run()  # 记录未变化时快速模式不读文件，不报错也不改写
 assert result['unchanged']==1 and target.read_text()=='foreign content'
 engine.cfg.sync_mode='normal'
 result=await engine.run();assert result['skipped']==1 and target.read_text()=='foreign content'

@pytest.mark.asyncio
async def test_suffix_filter(tmp_path):
 engine=setup(tmp_path,video_suffixes='mkv')
 engine.drive.items.append({'id':'2','path':'剧集/EP01.mp4','name':'EP01.mp4','pickcode':'abcdef999999','size':100,'sha1':'h','mtime':'1'})
 await engine.run()
 assert {p.name for p in tmp_path.rglob('*.strm')}=={'影片.strm'}

@pytest.mark.asyncio
async def test_min_size_filter(tmp_path):
 engine=setup(tmp_path,min_video_mb=1)
 engine.drive.items.append({'id':'2','path':'big.mkv','name':'big.mkv','pickcode':'abcdef999999','size':2*1024*1024,'sha1':'h','mtime':'1'})
 await engine.run()
 assert {p.name for p in tmp_path.rglob('*.strm')}=={'big.strm'}  # 100B 的影片.mkv 被过滤


@pytest.mark.asyncio
async def test_chunked_manifest(tmp_path):
 engine=setup(tmp_path)
 data={str(i):{'path':str(i)} for i in range(803)}  # 2.7.4：每片最多 400 条（仍受 60 KB 限制）
 await engine.save_manifest('test',data);assert len(engine.store.data['test']['children'])==3
 assert await engine.load_manifest('test')==data
 old=engine.store.data['test']['children'];await engine.save_manifest('test',{'new':{}})
 assert all(key not in engine.store.data for key in old)


@pytest.mark.asyncio
async def test_host_64kb_limit_and_large_manifest(tmp_path):
 import json
 engine=setup(tmp_path)
 data={str(i):{'path':'中文'*1500+str(i)} for i in range(225)}
 await engine.save_manifest('large',data)
 assert await engine.load_manifest('large')==data
 assert all(len(json.dumps(v,ensure_ascii=False).encode())<=65536 for v in engine.store.data.values())

@pytest.mark.asyncio
async def test_cloud_only_sidecar_downloaded_via_direct_link(tmp_path,monkeypatch):
 import httpx
 engine=setup(tmp_path)
 engine.drive.items.append({'id':'9','path':'影片 (2026)/影片.srt','name':'影片.srt','pickcode':'sub123456','size':14,'sha1':'sha-one','mtime':'3'})
 engine.drive.items.append({'id':'10','path':'影片 (2026)/poster.jpg','name':'poster.jpg','pickcode':'pos1234567','size':5,'sha1':'sha-poster','mtime':'4'})
 payload=[b'cloud subtitle']
 def respond(req):
  assert req.url.host=='cdn.example.com','附属文件只应从 115 直链下载'
  return httpx.Response(200,content=payload[0])
 monkeypatch.setattr('sync_engine.net.http_transport',lambda _:httpx.MockTransport(respond))
 result=await engine.run();assert result['sidecars']==2
 assert (tmp_path/'115/影片 (2026)/影片.srt').read_bytes()==b'cloud subtitle'
 assert (tmp_path/'115/影片 (2026)/poster.jpg').read_bytes()==b'cloud subtitle'
 assert sorted(engine.drive.link_calls)==['pos1234567','sub123456']
 # 云端未变化（sha1 一致）：不再下载，也不重写
 links_before=len(engine.drive.link_calls)
 again=await engine.run();assert again['sidecars']==0 and again['unchanged']==1
 assert len(engine.drive.link_calls)==links_before
 # 云端 sha1 变化：重新下载并覆盖（此前为我们写入的文件）
 payload[0]=b'cloud subtitle v2';engine.drive.items[1]['sha1']='sha-two'
 third=await engine.run();assert third['sidecars']==1
 assert (tmp_path/'115/影片 (2026)/影片.srt').read_bytes()==b'cloud subtitle v2'

@pytest.mark.asyncio
async def test_cloud_sidecar_user_file_never_touched(tmp_path,monkeypatch):
 import httpx
 engine=setup(tmp_path)
 engine.drive.items.append({'id':'9','path':'影片 (2026)/影片.srt','name':'影片.srt','pickcode':'sub123456','size':14,'sha1':'sha-one','mtime':'3'})
 def respond(req):return httpx.Response(200,content=b'cloud subtitle')
 monkeypatch.setattr('sync_engine.net.http_transport',lambda _:httpx.MockTransport(respond))
 await engine.run()
 target=tmp_path/'115/影片 (2026)/影片.srt';target.write_bytes(b'user stuff')
 await engine.run()
 assert target.read_bytes()==b'user stuff','用户改过的附属文件不允许被云端版本覆盖'

@pytest.mark.asyncio
async def test_cloud_sidecar_download_failure_is_soft(tmp_path,monkeypatch):
 import httpx
 engine=setup(tmp_path);engine.SIDECAR_RETRY_DELAYS=(0,0,0)
 engine.drive.items.append({'id':'9','path':'影片 (2026)/影片.srt','name':'影片.srt','pickcode':'sub123456','size':14,'sha1':'sha-one','mtime':'3'})
 def respond(req):return httpx.Response(500)
 monkeypatch.setattr('sync_engine.net.http_transport',lambda _:httpx.MockTransport(respond))
 result=await engine.run();assert result['written']==1 and not (tmp_path/'115/影片 (2026)/影片.srt').exists()


@pytest.mark.asyncio
async def test_rewrite_origins_updates_existing_strm(tmp_path):
 # 2.1.0：外网地址变更后，存量 STRM 不重新枚举网盘即可刷新为新地址。
 # 预览不改文件；执行只换 origin、路径与签名保留；记录与防误删凭据同步对齐，
 # 之后把 strm_base_url 配成同一地址，下一次增量同步零改写。
 engine=setup(tmp_path);await engine.run()
 target=next(tmp_path.rglob('*.strm'));old=target.read_text()
 preview=await engine.rewrite_origins('http://new.example.com:9000',dry_run=True)
 assert preview['dry_run'] is True and preview['rewritten']==1 and preview['unchanged']==0,repr(preview)
 assert target.read_text()==old,'预览绝不修改文件'
 result=await engine.rewrite_origins('http://new.example.com:9000')
 assert result['rewritten']==1 and result['unchanged']==0,repr(result)
 content=target.read_text()
 assert content.startswith('http://new.example.com:9000/api/v1/plugins/p115-library/play/'),content
 assert '?sig=' in content and 'movie.example.com' not in content,'签名保留、仅主机替换'
 assert content.endswith('\n')
 assert not list(tmp_path.rglob('*.tmp'))
 # 记录对齐：把配置改成同一地址后，下次同步应视为未变化
 engine.cfg.strm_base_url='http://new.example.com:9000'
 again=await engine.run()
 assert again['written']==0 and again['unchanged']==1,'替换后记录必须与内容一致：'+repr(again)

@pytest.mark.asyncio
async def test_rewrite_second_pass_reports_unchanged(tmp_path):
 engine=setup(tmp_path);await engine.run()
 await engine.rewrite_origins('http://new.example.com:9000')
 second=await engine.rewrite_origins('http://new.example.com:9000')
 assert second['rewritten']==0 and second['unchanged']==1,repr(second)

@pytest.mark.asyncio
async def test_rewrite_never_touches_user_modified_files(tmp_path):
 engine=setup(tmp_path);await engine.run()
 target=next(tmp_path.rglob('*.strm'));target.write_text('user edited')
 result=await engine.rewrite_origins('http://new.example.com:9000')
 assert result['rewritten']==0 and result['modified']==1,repr(result)
 assert target.read_text()=='user edited'

@pytest.mark.asyncio
async def test_rewrite_old_base_filter_only_matching_origin(tmp_path):
 engine=setup(tmp_path);await engine.run()
 target=next(tmp_path.rglob('*.strm'))
 result=await engine.rewrite_origins('http://new.example.com:9000',old_origin='http://other.example.com')
 assert result['rewritten']==0 and result['mismatched']==1,repr(result)
 assert 'movie.example.com' in target.read_text(),'旧地址过滤不匹配时绝不替换'

@pytest.mark.asyncio
async def test_rewrite_counts_missing_and_lock_conflict(tmp_path):
 engine=setup(tmp_path);await engine.run()
 target=next(tmp_path.rglob('*.strm'));target.unlink()
 result=await engine.rewrite_origins('http://new.example.com:9000')
 assert result['missing']==1 and result['rewritten']==0,repr(result)
 async with engine.lock:
  with pytest.raises(DriveError,match='同步'):await engine.rewrite_origins('http://new.example.com:9000')

@pytest.mark.asyncio
async def test_rewrite_preserves_name_suffix_format(tmp_path):
 engine=setup(tmp_path,strm_link_format='pick_code_name');await engine.run()
 target=next(tmp_path.rglob('*.strm'))
 result=await engine.rewrite_origins('http://new.example.com:9000')
 assert result['rewritten']==1,repr(result)
 content=target.read_text()
 assert content.startswith('http://new.example.com:9000/') and '?sig=' in content,repr(content)
 assert '%E5%BD%B1%E7%89%87.mkv' in content and content.endswith('\n'),'?/文件名 后缀必须原样保留'


@pytest.mark.asyncio
async def test_write_probe_fails_fast_with_host_grant_message(tmp_path):
 # 宿主沙箱拒绝：探测在枚举网盘之前完成，并把宿主的原始路径/授权信息完整透传
 engine=setup(tmp_path)
 class DeniedFiles(Files):
  async def makedirs(self,path):raise PermissionError(f'插件 p115-library 没有获得访问 {path} 的授权')
 engine.files=DeniedFiles(tmp_path)
 with pytest.raises(DriveError,match='写入被拒绝.*没有获得访问'):
  await engine.run()
 assert not list(tmp_path.rglob('*.strm')),'无权写入时绝不落盘'
 assert engine.ops.calls[-1][0]=='library.get','探测失败在扫描启动之前'

@pytest.mark.asyncio
async def test_write_probe_fails_fast_with_os_permission_message(tmp_path):
 # 操作系统层拒绝（Linux 目录属 root 等）：同样快速失败并保留 errno 详情
 engine=setup(tmp_path)
 class OsDeniedFiles(Files):
  async def open(self,path,mode):raise PermissionError(f"[Errno 13] Permission denied: '{path}'")
 engine.files=OsDeniedFiles(tmp_path)
 with pytest.raises(DriveError,match='写入被拒绝.*Errno 13'):
  await engine.run()
 assert not list(tmp_path.rglob('*.strm'))
 assert engine.ops.calls[-1][0]=='library.get'

@pytest.mark.asyncio
async def test_cdn_read_timeout_on_sidecar_does_not_fail_the_sync(tmp_path,monkeypatch):
 # 2.7.8 现场：「STRM 生成失败（ReadTimeout）」——一个附属文件的 CDN 下载超时让整轮同步失败。
 import httpx
 engine=setup(tmp_path);engine.SIDECAR_RETRY_DELAYS=(0,0,0)
 engine.drive.items.append({'id':'9','path':'影片 (2026)/影片.srt','name':'影片.srt','pickcode':'sub123456','size':14,'sha1':'sha-one','mtime':'3'})
 def respond(req):raise httpx.ReadTimeout('timed out',request=req)
 monkeypatch.setattr('sync_engine.net.http_transport',lambda _:httpx.MockTransport(respond))
 result=await engine.run()
 assert result['state']=='completed' and result['written']==1
 assert not (tmp_path/'115/影片 (2026)/影片.srt').exists()
 logs=[r['message'] for r in engine.store.data.get('runtime-log',[])]
 assert any('下载失败' in m and '下次同步自动重试' in m for m in logs),logs

@pytest.mark.asyncio
async def test_cdn_timeout_then_success_is_retried(tmp_path,monkeypatch):
 import httpx
 engine=setup(tmp_path);engine.SIDECAR_RETRY_DELAYS=(0,0,0)
 engine.drive.items.append({'id':'9','path':'影片 (2026)/影片.srt','name':'影片.srt','pickcode':'sub123456','size':14,'sha1':'sha-one','mtime':'3'})
 calls=[]
 def respond(req):
  calls.append(1)
  if len(calls)==1:raise httpx.ReadTimeout('timed out',request=req)
  return httpx.Response(200,content=b'subtitle')
 monkeypatch.setattr('sync_engine.net.http_transport',lambda _:httpx.MockTransport(respond))
 result=await engine.run()
 assert result['sidecars']==1 and (tmp_path/'115/影片 (2026)/影片.srt').read_bytes()==b'subtitle'
