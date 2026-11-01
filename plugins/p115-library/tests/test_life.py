import asyncio
import contextlib
import copy
import json
import time
import pytest
from p115_plugin import Settings
from life_strm import LifeSTRM
from drive_api import DriveError
from test_sync import Files, Routes, Drive
from conftest import Store, Context

class LifeDrive(Drive):
 def __init__(self):super().__init__();self.events=[];self.calls=[];self.bad=False
 async def request(self,method,url,**kw):
  self.calls.append((method,url,kw))
  if self.bad:raise DriveError('生活事件暂不可用')
  return {'state':True,'data':{'list':copy.deepcopy(self.events)}}
class Ops:
 def __init__(self,root):self.root=str(root);self.calls=[]
 async def call(self,op,args):
  self.calls.append((op,args))
  if op=='library.list':return [{'id':8,'primary_root':self.root,'root_paths':[self.root]}]
  return {'job_id':'scan'}

def setup(root,**kw):
 cfg=Settings(cookie='UID=123_A1',cloud_cid='99',strm_directory=str(root/'strm'),**kw)
 return LifeSTRM(cfg,LifeDrive(),Store(),Files(root),Ops(root),Routes(),Context())

@pytest.mark.asyncio
async def test_life_initial_new_event_relative_strm_and_checkpoint(tmp_path):
 service=setup(tmp_path);await service.tick(force=True)
 file=tmp_path/'strm/影片 (2026)/影片.strm';assert file.exists()
 assert 'sig=' in file.read_text()
 assert service.ops.ops.calls[-1]==('library.scan.start',{'library_id':8,'body':{'paths':[str(tmp_path/'strm')]}})
 before=file.stat().st_mtime_ns;await service.tick();assert file.stat().st_mtime_ns==before
 item=dict(service.drive.items[0],id='2',path='电视剧/Season 01/S01E02.mkv',name='S01E02.mkv');service.drive.items.append(item)
 service.drive.events=[{'id':'event2','file_id':'2','type':2,'update_time':100}]
 await service.tick();assert (tmp_path/'strm/电视剧/Season 01/S01E02.strm').exists()
 assert service.drive.calls[-1][2]['params']=={'limit':64,'offset':0},'1.8.0 起拉取全部操作事件，浏览类客户端过滤'

@pytest.mark.asyncio
async def test_upload_trigger_without_life_event_and_restart(tmp_path):
 service=setup(tmp_path,life_events=False);await service.tick(force=True)
 service.drive.items.append(dict(service.drive.items[0],id='3',path='new.mkv',name='new.mkv'))
 await service.after_upload();assert service.store.data['strm-upload-dirty']
 restarted=LifeSTRM(service.cfg,service.drive,service.store,service.engine.files,service.ops.ops,Routes(),Context())
 # 2.7.3：插件重启（升级/宿主重启）后的第一轮只记录基线，绝不自动开跑同步，残留的待同步标记清掉
 await restarted.tick();assert not (tmp_path/'strm/new.strm').exists()
 assert not await service.store.get('strm-upload-dirty',default=False)
 await restarted.after_upload();await restarted.tick();assert (tmp_path/'strm/new.strm').exists()
 assert not await service.store.get('strm-upload-dirty',default=False)

@pytest.mark.asyncio
async def test_life_failure_fallback_and_user_file_protection(tmp_path):
 service=setup(tmp_path);service.drive.bad=True;await service.tick(force=True)
 assert service.state['state']=='completed' and service.state['life_error']
 path=tmp_path/'strm/影片 (2026)/影片.strm';path.write_text('manual edit')
 await service.after_upload();await service.tick()
 # 2.7.0：手工改过的 STRM 绝不覆盖，但只跳过该文件、计数并记日志，不再让整次同步失败
 assert service.state['state']=='completed' and path.read_text()=='manual edit' and service.state['skipped']>=1
 assert not await service.store.get('strm-upload-dirty'),'同步成功（仅跳过一个文件）后不应残留待同步标记'

@pytest.mark.asyncio
async def test_remote_deletion_cleanup_is_opt_in(tmp_path):
 service=setup(tmp_path);await service.tick(force=True)
 path=tmp_path/'strm/影片 (2026)/影片.strm'
 service.drive.items=[];await service.tick(force=True)
 assert path.exists()  # 默认不清理，网盘删除后 STRM 保留
 service.cfg.strm_cleanup=True
 await service.tick(force=True)
 assert not path.exists() and service.state['deleted']==1

@pytest.mark.parametrize('strm',['/','/a/../b'])
def test_bad_strm_directory_rejected(strm):
 with pytest.raises(ValueError):Settings(strm_directory=strm)

@pytest.mark.parametrize('a,b',[('/media/strm','/media/strm'),(' /media/strm','/media/strm/x'),(' /media/strm/x','/media/strm')])
def test_mapping_overlap_rejected(a,b):
 with pytest.raises(ValueError):Settings(strm_directory=a,mappings=json.dumps([
  {'name':'电影','cloud_cid':'123','strm_directory':b.strip()}]))

@pytest.mark.asyncio
async def test_cron_not_due_skips_scheduled_run(tmp_path):
 service=setup(tmp_path,sync_cron='0 0 1 1 *')  # 每年 1 月 1 日，下次触发在未来
 await service.tick(force=True)
 service.drive.items.append(dict(service.drive.items[0],id='3',path='new.mkv',name='new.mkv'))
 await service.tick()  # 无事件、无上传、未到点：不触发
 assert not (tmp_path/'strm/new.strm').exists()
 assert service.state['next_run_at']>time.time()

@pytest.mark.asyncio
async def test_first_tick_without_force_does_not_sync(tmp_path):
 # 2.6.3：插件刚加载（无事件指纹基线、从未同步过）时，tick(force=False) 不得触发同步——
 # 否则事件指纹「无→有」或 cron 从 synced_at=0 算出的过去时间会把同步自动跑起来。
 service=setup(tmp_path)
 await service.tick()  # force=False，首次：只记录基线，不同步
 assert not (tmp_path/'strm/影片 (2026)/影片.strm').exists(),'首次启动不得自动同步'
 key=[k for k in service.store.data if k.startswith('life-strm:')][0]
 assert 'fingerprint' in service.store.data[key],'首次应记录事件指纹基线'

@pytest.mark.asyncio
async def test_cron_due_runs_and_reports_mode(tmp_path):
 service=setup(tmp_path,sync_cron='* * * * *')
 await service.tick(force=True)
 key=[k for k in service.store.data if k.startswith('life-strm:')][0]
 service.store.data[key]['synced_at']=time.time()-120  # 假装上次同步在 2 分钟前
 service.started_at=time.time()-120  # 2.7.3：cron 从插件启动时刻起算，假装插件已运行 2 分钟
 service.drive.items.append(dict(service.drive.items[0],id='3',path='new.mkv',name='new.mkv'))
 await service.tick()  # 到点触发增量核对
 assert (tmp_path/'strm/new.strm').exists()
 assert service.state['mode']=='incremental' and service.state['state']=='completed'

def test_invalid_cron_rejected():
 with pytest.raises(ValueError):Settings(sync_cron='not a cron')
 assert Settings(sync_cron='0 9 * * 1-5').sync_cron=='0 9 * * 1-5'
 assert Settings(video_suffixes='.MKV, mp4,,mkv').video_suffixes=='mkv,mp4'
 with pytest.raises(ValueError):Settings(video_suffixes='mkv,a-b')

@pytest.mark.asyncio
async def test_hot_reload_restart_swaps_loop_without_duplicate(tmp_path):
 service=setup(tmp_path)
 await service.boot();old=service.task
 assert old and not old.done()
 # Simulate a settings change then hot restart.
 service.cfg.sync_cron='0 5 * * *'
 await service.restart()
 await asyncio.sleep(0)  # let the new loop start and settle
 assert service.task is not old and service.task and not service.task.done()
 assert old.done() or old.cancelled() or old.cancel(),'旧循环必须被取消'
 # Restarting again must still keep exactly one loop.
 await service.restart();await asyncio.sleep(0)
 assert service.task and not service.task.done()
 service.task.cancel()
 with contextlib.suppress(asyncio.CancelledError):await service.task

@pytest.mark.asyncio
async def test_no_cookie_waits_for_login_instead_of_crashing(tmp_path):
 """2.1.1 回归：全新安装（无 Cookie）不得在 boot/active/tick 抛「请先扫码登录 115」。

 根因：parsed_mappings 需要账号 UID 构造记录隔离的 library_id，此前 boot→active
 先访问它，未登录时整个插件加载即崩，用户误以为扫码登录被移除。"""
 from pydantic import SecretStr
 cfg=Settings(cloud_cid='99',strm_directory=str(tmp_path/'strm'))
 assert not cfg.cookie.get_secret_value(),'本测试前提：无凭据全新安装'
 service=LifeSTRM(cfg,LifeDrive(),Store(),Files(tmp_path),Ops(tmp_path),Routes(),Context())
 assert service.engine.cfg.has_mappings and service.active(),'已配置映射：无凭据循环也应照常启动'
 with pytest.raises(DriveError):service.engine.cfg.parsed_mappings,'映射定位仍需账号 UID（记录隔离），但只能在登录后访问'
 await service.boot();assert service.task and not service.task.done(),'boot 不得因未登录而崩溃'
 await asyncio.sleep(.05)
 assert service.state['state']=='waiting_login' and '请先扫码' in service.state['error'],service.state
 # 扫码成功后 cfg.cookie 被热更新（login.poll 的行为）→ 同一服务实例立即恢复同步
 cfg.cookie=SecretStr('UID=123_A1')
 await service.tick(force=True)
 assert service.state['state']=='completed' and (tmp_path/'strm/影片 (2026)/影片.strm').exists(),service.state
 service.task.cancel()
 with contextlib.suppress(asyncio.CancelledError):await service.task

def test_mapping_specs_validation_and_legacy_scope_key():
 from types import SimpleNamespace
 from life_strm import AutoConfig,mapping_specs
 from sync_engine import scope_key
 from life_strm import uid
 assert mapping_specs('')==[]
 ok=mapping_specs('[{"name":"电影","cloud_cid":"123","strm_directory":"/media/strm/movies/"}]')
 assert ok==[{'name':'电影','cloud_cid':'123','strm_directory':'/media/strm/movies'}]
 for bad in ('not json','{"a":1}','["x"]','[{"cloud_cid":"x","strm_directory":"/a"}]',
             '[{"cloud_cid":"1","strm_directory":"a"}]',
             '[{"cloud_cid":"1","strm_directory":"/a"},{"cloud_cid":"2","strm_directory":"/a/b"}]',
             '[{"cloud_cid":"1","strm_directory":"/a"},{"cloud_cid":"1","strm_directory":"/a"}]',
             '[{"cloud_cid":"1","strm_directory":"/"}]'):
  with pytest.raises(ValueError):mapping_specs(bad)
 with pytest.raises(ValueError):Settings(mappings='not json')
 # 旧单映射的 AutoMapping 与 scope_key 必须与 1.5.x 完全一致，否则升级后已有 STRM 会被判为非插件文件
 cfg=Settings(cookie='UID=123_A1',cloud_cid='99',strm_directory='/media/strm')
 mappings=AutoConfig(cfg).parsed_mappings
 account=uid('UID=123_A1')
 assert mappings[0].model_dump()=={'cid':'99','library_id':'auto:'+account+':/media','subdirectory':'strm'}
 legacy=SimpleNamespace(cid='99',library_id='auto:'+account+':/media',subdirectory='strm',
                        model_dump=lambda:{'cid':'99','library_id':'auto:'+account+':/media','subdirectory':'strm'})
 assert scope_key(mappings[0])==scope_key(legacy)

@pytest.mark.asyncio
async def test_multi_mapping_syncs_each_directory(tmp_path):
 specs=[{'name':'电影','cloud_cid':'99','strm_directory':str(tmp_path/'map-a')},
        {'name':'剧集','cloud_cid':'88','strm_directory':str(tmp_path/'map-b')}]
 cfg=Settings(cookie='UID=123_A1',mappings=json.dumps(specs))
 service=LifeSTRM(cfg,LifeDrive(),Store(),Files(tmp_path),Ops(tmp_path),Routes(),Context())
 assert service.active() and not cfg.strm_directory
 assert len(service.engine.cfg.parsed_mappings)==2
 await service.tick(force=True)
 assert (tmp_path/'map-a/影片 (2026)/影片.strm').exists()
 assert (tmp_path/'map-b/影片 (2026)/影片.strm').exists()
 scan_paths={args['body']['paths'][0] for op,args in service.ops.ops.calls if op=='library.scan.start'}
 assert scan_paths=={str(tmp_path/'map-a'),str(tmp_path/'map-b')},'增量同步：每个有变化的映射各自通知扫描'

@pytest.mark.asyncio
async def test_sync_history_records_runs_and_failures(tmp_path):
 service=setup(tmp_path);await service.tick(force=True)
 rows=await service.store.get('sync-history')
 assert len(rows)==1 and rows[0]['mode']=='incremental' and rows[0]['written']==1 and not rows[0]['error'] and rows[0]['time']
 (tmp_path/'strm/影片 (2026)/影片.strm').write_text('user edit')
 await service.after_upload();await service.tick()
 rows=await service.store.get('sync-history');assert len(rows)==2 and not rows[-1]['error']
 assert (tmp_path/'strm/影片 (2026)/影片.strm').read_text()=='user edit'
 for _ in range(25):await service.record_history('incremental',{},'x')
 assert len(await service.store.get('sync-history'))==20,'历史最多保留 20 条'

@pytest.mark.asyncio
async def test_life_events_written_to_runtime_log(tmp_path):
 service=setup(tmp_path)
 service.drive.events=[{'id':'e1','file_id':'1','type':2,'update_time':100,'file_name':'a.mkv'}]
 await service.tick(force=True)
 logs=await service.store.get('runtime-log',default=[])
 assert any(e['source']=='115事件' and '开始监控' in e['message'] and '忽略历史 1 条' in e['message'] for e in logs),logs
 assert not any('a.mkv' in e['message'] for e in logs),'首次不应回放历史事件'
 service.drive.events.append({'id':'e2','file_id':'2','type':2,'update_time':200,'file_name':'b.mkv','file_size':2048})
 await service.tick(force=True)
 logs=await service.store.get('runtime-log',default=[])
 assert any(e['source']=='115事件' and 'b.mkv' in e['message'] and '2KB' in e['message'] for e in logs),logs
 assert not any(e['message'].endswith('a.mkv') for e in logs),'旧事件不重复记'
 assert (await service.store.get('life-seen-ids-v2'))==['e1','e2']

@pytest.mark.asyncio
async def test_life_events_without_name_uses_file_id(tmp_path):
 service=setup(tmp_path)
 service.drive.events=[{'id':'e1','file_id':'1','type':2,'update_time':100}]
 await service.tick(force=True)  # 首次：标记，seen=['e1']
 service.drive.events=[{'id':'e2','file_id':'7','type':2,'update_time':200}]
 await service.tick(force=True)
 logs=await service.store.get('runtime-log',default=[])
 assert any(e['source']=='115事件' and '#7' in e['message'] for e in logs),logs

@pytest.mark.asyncio
async def test_life_events_disabled_writes_nothing(tmp_path):
 service=setup(tmp_path,life_events=False)
 service.drive.events=[{'id':'e1','file_id':'1','type':2,'update_time':100,'file_name':'a.mkv'}]
 await service.tick(force=True)
 assert not await service.store.get('life-seen-ids-v2',default=None)
 logs=await service.store.get('runtime-log',default=[])
 assert not any(e['source']=='115事件' for e in logs)

@pytest.mark.asyncio
async def test_move_event_triggers_sync_writes_strm_and_logs(tmp_path):
 # 1.8.0：别的文件移动到监控库（move_file=6）也是触发信号，且日志按操作类型渲染
 service=setup(tmp_path);await service.tick(force=True)
 assert not (tmp_path/'strm/moved.strm').exists()
 service.drive.items.append(dict(service.drive.items[0],id='9',path='moved.mkv',name='moved.mkv'))
 service.drive.events.append({'id':'m1','file_id':'9','type':6,'update_time':300,'file_name':'moved.mkv','file_size':1024**3})
 await service.tick()
 assert (tmp_path/'strm/moved.strm').exists(),'移动事件应触发目录核对并写入 STRM'
 logs=await service.store.get('runtime-log',default=[])
 assert any(e['source']=='115事件' and '网盘移动：moved.mkv' in e['message'] and '1.00GB' in e['message'] for e in logs),logs
 assert any(e['source']=='同步' and '写入 1' in e['message'] for e in logs),'日志时间线应展现 事件→同步 的操作流程'

@pytest.mark.asyncio
async def test_browse_events_do_not_trigger_sync_nor_log(tmp_path):
 # 浏览/星标类（p115client IGNORE_BEHAVIOR_TYPES）只读不改库：不进指纹、不进日志
 service=setup(tmp_path);await service.tick(force=True)
 before=service.state['synced_at']
 service.drive.events.append({'id':'b1','file_id':'1','type':8,'update_time':400,'file_name':'watched.mkv'})
 service.drive.events.append({'id':'b2','file_id':'1','type':4,'update_time':401,'file_name':'starred.mkv'})
 await service.tick()
 assert service.state['synced_at']==before,'浏览/星标事件不得触发同步'
 logs=await service.store.get('runtime-log',default=[])
 assert not any('watched.mkv' in e['message'] or 'starred.mkv' in e['message'] for e in logs),logs
 assert 'b1' not in (await service.store.get('life-seen-ids-v2') or []),'浏览事件在源头上被过滤，不进入去重记录'

@pytest.mark.asyncio
@pytest.mark.parametrize('etype,label',[(2,'上传'),(5,'移动图片'),(14,'接收'),(17,'新建目录'),(18,'复制目录'),(20,'目录改名'),(22,'删除'),(99,'变动')])
async def test_behavior_labels_rendered_per_type(tmp_path,etype,label):
 service=setup(tmp_path);await service.tick(force=True)
 service.drive.events.append({'id':'x'+str(etype),'file_id':'7','type':etype,'update_time':500,'file_name':'f.mkv'})
 await service.tick(force=True)
 logs=await service.store.get('runtime-log',default=[])
 assert any(e['source']=='115事件' and ('网盘'+label+'：f.mkv') in e['message'] for e in logs),(label,logs)


@pytest.mark.asyncio
async def test_restart_never_auto_syncs_even_if_cron_missed_or_interrupted(tmp_path):
 service=setup(tmp_path,sync_cron='* * * * *');await service.tick(force=True)
 key=[k for k in service.store.data if k.startswith('life-strm:')][0]
 service.store.data[key]['synced_at']=time.time()-86400  # 上次同步在一天前：cron 早已错过
 service.store.data['strm-upload-dirty']=True            # 上次运行被打断留下的标记
 service.drive.items.append(dict(service.drive.items[0],id='3',path='new.mkv',name='new.mkv'))
 upgraded=LifeSTRM(service.cfg,service.drive,service.store,service.engine.files,service.ops.ops,Routes(),Context())
 await upgraded.boot()
 await upgraded.tick()
 assert not (tmp_path/'strm/new.strm').exists(),'升级/重启后不得自动开始同步'
 assert upgraded.state['next_run_at']>time.time(),'cron 从启动时刻起算，不补跑错过的时间点'
 if upgraded.task:upgraded.task.cancel()

@pytest.mark.asyncio
async def test_stop_works_for_loop_started_sync_and_does_not_restart(tmp_path):
 service=setup(tmp_path)
 await service.tick(force=True)  # 基线
 gate=asyncio.Event()
 orig=service.engine.sync_mapping
 async def slow(*a,**k):
  gate.set();await asyncio.sleep(30)
 service.engine.sync_mapping=slow
 # 由后台循环（非面板按钮）发起的同步
 service.store.data['strm-upload-dirty']=True
 runner=asyncio.create_task(service.tick())
 await asyncio.wait_for(gate.wait(),5)
 assert service.engine.lock.locked()
 assert await service.stop(timeout=5) is True,'停止必须对任何来源的同步生效'
 await asyncio.wait_for(runner,5)
 assert service.state['state']=='cancelled' and not runner.cancelled()
 assert not await service.store.get('strm-upload-dirty',default=False),'停止后不得留下自动重跑标记'
 assert await service.stop() is None,'没有同步时返回 None'
 service.engine.sync_mapping=orig

def test_failure_description_is_readable_and_points_into_plugin():
 import httpx
 from life_strm import describe_failure,plugin_frames
 def inner():raise httpx.ReadTimeout('',request=httpx.Request('GET','https://cdn.example.com/x'))
 try:inner()
 except Exception as e:
  text=describe_failure(e)
  assert '网络读取超时' in text and 'ReadTimeout' in text and '无详细信息' not in text,text
  assert plugin_frames(e).startswith('ReadTimeout')
