"""Life event hints + authoritative scoped enumeration, with periodic recovery."""
import asyncio
import hashlib
import json
import os
import re
import time
import traceback
from dataclasses import dataclass
from pathlib import PurePosixPath
from cronexpr import Cron
from drive_api import DriveError
from sync_engine import SyncEngine,best_effort
from runlog import log_event

def plugin_frames(exc,limit=4):
    """The innermost frames that belong to this plugin, e.g.
    'sync_engine.py:512 fetch_cloud_sidecar ← sync_engine.py:1001 sync_side'."""
    here=os.path.dirname(os.path.abspath(__file__))
    frames=[f for f in traceback.extract_tb(exc.__traceback__) if os.path.abspath(f.filename).startswith(here)]
    parts=['%s:%d %s'%(os.path.basename(f.filename),f.lineno,f.name) for f in reversed(frames[-limit:])]
    return (type(exc).__name__+' @ '+' ← '.join(parts)) if parts else type(exc).__name__

NETWORK_HINTS={'ReadTimeout':'网络读取超时','ConnectTimeout':'网络连接超时','WriteTimeout':'网络发送超时',
               'PoolTimeout':'网络连接池等待超时','ConnectError':'网络连接失败','RemoteProtocolError':'对端提前断开连接',
               'ReadError':'网络读取出错','TimeoutError':'等待超时'}

def describe_failure(exc):
    """A readable one-liner for unexpected exceptions (2.7.8): many network
    exceptions carry an EMPTY message, which used to surface as「无详细信息」."""
    name=type(exc).__name__
    text=(str(exc).strip() or NETWORK_HINTS.get(name) or '无详细信息')[:240]
    return '%s（%s，位置 %s）'%(text,name,plugin_frames(exc,2).split(' @ ',1)[-1])

def uid(cookie):
    m=re.search(r'(?:^|;\s*)UID=(\d+)',cookie or '')
    if not m:raise DriveError('请先扫码登录 115')
    return m[1]

# 115 behavior/detail event types (p115client BEHAVIOR_NAME_TO_TYPE). Read-only
# browse/star ops (3,4,7,8,9,10,19) are dropped client-side, following
# p115client's IGNORE_BEHAVIOR_TYPES: they never change library content, so
# they must not trigger reconciliation nor flood the log. Every other type is
# a mutation hint (upload/move/receive/new folder/delete/rename...); unknown
# future types are kept too -- an extra scoped reconciliation is harmless.
IGNORE_BEHAVIOR_TYPES=frozenset((3,4,7,8,9,10,19))
BEHAVIOR_LABELS={1:'上传图片',2:'上传',5:'移动图片',6:'移动',14:'接收',17:'新建目录',
                 18:'复制目录',20:'目录改名',22:'删除',23:'复制',24:'文件改名'}

def behavior_type(row):
    """Best-effort int type of a behavior row (None when the field is absent)."""
    value=row.get('type')
    try:return int(value)
    except (TypeError,ValueError):return None


def mapping_specs(raw):
    """Parse and validate the mappings JSON (qmediasync SyncPath-style list).

    Returns a normalized list of {name, cloud_cid, strm_directory}; raises
    ValueError with a Chinese message on invalid input; [] when empty.
    """
    raw=(raw or '').strip() if isinstance(raw,str) else ''
    if not raw:return []
    try:specs=json.loads(raw)
    except Exception:raise ValueError('映射列表不是合法 JSON') from None
    if not isinstance(specs,list):raise ValueError('映射列表必须是 JSON 数组')
    if len(specs)>8:raise ValueError('最多支持 8 条映射')
    rows=[];seen=set()
    for spec in specs:
        if not isinstance(spec,dict):raise ValueError('每条映射必须是对象 {name, cloud_cid, strm_directory}')
        name=str(spec.get('name','') or '').strip()
        if len(name)>40:raise ValueError('映射名称过长（最多 40 字）')
        cid=str(spec.get('cloud_cid','') or '')
        if not re.fullmatch(r'[0-9]{1,20}',cid):raise ValueError('映射的 115 媒体库 CID 必须为数字'+('：'+name if name else ''))
        directory=str(spec.get('strm_directory','') or '').strip()
        if directory=='/':raise ValueError('映射目录不能是系统根目录'+('：'+name if name else ''))
        if not directory.startswith('/') or '..' in directory.split('/') or '\\' in directory or '\x00' in directory:
            raise ValueError('映射目录必须为不含 .. 的容器内绝对路径'+('：'+name if name else ''))
        directory=directory.rstrip('/')
        if not directory:raise ValueError('映射目录不能是系统根目录'+('：'+name if name else ''))
        if (cid,directory) in seen:raise ValueError('存在重复映射：'+directory)
        seen.add((cid,directory));rows.append({'name':name,'cloud_cid':cid,'strm_directory':directory})
    for a in rows:
        for b in rows:
            if a is not b and PurePosixPath(a['strm_directory']).is_relative_to(PurePosixPath(b['strm_directory'])):
                raise ValueError('映射目录不能相互嵌套：'+a['strm_directory']+' 与 '+b['strm_directory'])
    return rows

@dataclass
class AutoMapping:
    cid:str
    library_id:str
    subdirectory:str
    def model_dump(self):return vars(self)

class AutoConfig:
    def __init__(self,cfg):self.cfg=cfg
    def __getattr__(self,name):return getattr(self.cfg,name)
    @property
    def cleanup_missing(self):return bool(getattr(self.cfg,'strm_cleanup',False))
    @property
    def has_mappings(self):
        # 2.1.1: cookie-free probe for "is any sync mapping configured?".
        # parsed_mappings needs the account UID (record isolation), so boot /
        # tick / status must never touch it before a cookie exists -- that
        # crashed the whole plugin with "请先扫码登录 115" on a fresh install.
        try:specs=mapping_specs(getattr(self.cfg,'mappings',''))
        except ValueError:specs=[]
        return bool(specs or (getattr(self.cfg,'cloud_cid','') and getattr(self.cfg,'strm_directory','')))
    @property
    def parsed_mappings(self):
        # Multi-directory sync (qmediasync SyncPath model). The legacy single pair
        # keeps its exact AutoMapping bytes, so scope_key and every existing
        # manifest/proof record stay valid across the upgrade.
        account=uid(self.cfg.cookie.get_secret_value())
        try:specs=mapping_specs(getattr(self.cfg,'mappings',''))
        except ValueError:specs=[]
        if specs:
            return [AutoMapping(s['cloud_cid'],'auto:'+account+':'+str(PurePosixPath(s['strm_directory']).parent),
                                PurePosixPath(s['strm_directory']).name) for s in specs]
        if getattr(self.cfg,'cloud_cid','') and getattr(self.cfg,'strm_directory',''):
            p=PurePosixPath(self.cfg.strm_directory)
            return [AutoMapping(self.cfg.cloud_cid,'auto:'+account+':'+str(p.parent),p.name)]
        return []

class AutoOps:
    def __init__(self,cfg,ops):self.cfg=cfg;self.ops=ops;self.notice='';self.list_rpc_timeout=30;self.scan_rpc_timeout=60
    async def call(self,operation,args):
        if operation=='library.get':
            library_id=str(args.get('library_id',''))
            if not library_id.startswith('auto:'):
                return await self.ops.call(operation,args)
            # The sync engine derives each mapping's root from its library_id
            # ("auto:<account>:<posix-root>"), so multi-mapping needs no host lookup.
            parts=library_id.split(':',2)
            root=parts[2] if len(parts)==3 and parts[2].startswith('/') else ''
            return {'primary_root':root}
        if operation!='library.scan.start':return await self.ops.call(operation,args)
        paths=args.get('body',{}).get('paths') or [self.cfg.strm_directory]
        target=PurePosixPath(str(paths[0]))
        # Both host RPCs are bounded: a busy media library used to hang this
        # notify step forever, which froze the whole sync state (1.7.9). The
        # notify is best-effort -- a timeout becomes a visible notice, not an error.
        try:
            libraries=await asyncio.wait_for(self.ops.call('library.list',{}),self.list_rpc_timeout)
        except asyncio.TimeoutError:
            self.notice='媒体库查询超时，本次未触发媒体库扫描；请稍后手动扫描或重试同步'
            return {'matched_libraries':[],'timeout':True}
        rows=libraries if isinstance(libraries,list) else libraries.get('items',[])
        matched=[]
        for row in rows:
            roots=[row.get('primary_root')]+list(row.get('root_paths') or [])
            if any(isinstance(r,str) and r.startswith('/') and target.is_relative_to(PurePosixPath(r)) for r in roots):matched.append(row['id'])
        started=[]
        for lib in sorted(set(matched)):
            try:
                await asyncio.wait_for(self.ops.call('library.scan.start',{'library_id':lib,'body':{'paths':[str(target)]}}),self.scan_rpc_timeout)
                started.append(lib)
            except asyncio.TimeoutError:break
        self.notice='' if started else ('媒体库扫描启动超时（宿主可能正忙），请稍后手动扫描或重试同步' if matched
            else 'STRM 已生成；该路径未匹配媒体库，请将它添加到 MovieClaw 媒体库后扫描')
        return {'matched_libraries':started}

class AutoEngine(SyncEngine):
    # 2.7.0: this override used to write the whole status to the host store on
    # EVERY counter bump (one IPC per file), silently bypassing the base
    # class's 0.7 s throttle. Only the storage key differs now.
    STATUS_KEY='auto-strm-status'

class LifeSTRM:
    def __init__(self,cfg,drive,store,files,ops,routes,ctx):
        self.cfg=cfg;self.drive=drive;self.store=store;self.ctx=ctx
        self.ops=AutoOps(cfg,ops);self.engine=AutoEngine(AutoConfig(cfg),drive,store,files,self.ops,routes,ctx)
        self.signal=asyncio.Event();self.lock=asyncio.Lock();self.state={'state':'idle','life_error':'','error':''}
        self.crons={};self.task=None;self.run_task=None
        # 2.7.3: the engine run lives in its own task so「停止同步」can stop it
        # no matter who started it (panel button, cron, 115 event).
        self.sync_task=None;self.stop_requested=False
        # The first tick after boot / config reload only records baselines.
        self.started_at=time.time();self.baseline_pending=True
    def cron(self):
        expression=getattr(self.cfg,'sync_cron','') or '*/20 8-23 * * *'
        if expression not in self.crons:
            try:self.crons[expression]=Cron(expression)
            except ValueError:self.crons[expression]=None
            if len(self.crons)>8:self.crons.clear()
        return self.crons[expression]
    def next_run_after(self,synced_at):
        cron=self.cron()
        if cron is None:return synced_at+3600
        try:return cron.next_after(synced_at)
        except ValueError:return synced_at+3600
    async def boot(self):
        self.started_at=time.time();self.baseline_pending=True
        if self.active():self.task=self.ctx.task(self.loop(),name='115-life-strm')
    def active(self):
        """True when at least one sync mapping is configured (legacy pair included).

        Cookie-free by design (2.1.1): the loop must start before login so tick
        can park the state machine in waiting_login instead of crashing the
        plugin with "请先扫码登录 115" on a fresh install."""
        return bool(self.engine.cfg.has_mappings)
    async def restart(self):
        """Hot config reload: stop the current loops, start them again under the new settings."""
        for handle in (self.run_task,self.task):
            if handle and not handle.done():
                handle.cancel()
                try:await asyncio.wait_for(asyncio.shield(handle),10)
                except (asyncio.CancelledError,asyncio.TimeoutError,Exception):pass
        cancelled_stuck=any(h and not h.done() for h in (self.run_task,self.task))
        self.run_task=None;self.task=None
        if cancelled_stuck:
            self.state.update(state='failed',error='配置已保存，但旧的同步任务未能停止；为避免重复运行，本轮循环暂不启动，重启插件后恢复')
            return
        self.started_at=time.time();self.baseline_pending=True
        if self.active():self.task=self.ctx.task(self.loop(first=False),name='115-life-strm')
    async def after_upload(self,record=None):
        # Persist before upload completion is acknowledged. A restart cannot lose the trigger.
        if self.active():
            await self.store.set('strm-upload-dirty',True);self.signal.set()
    async def life_feed(self):
        """Return (fingerprint, rows) of the 115 behavior feed (all mutation types).

        This endpoint is the life behavior feed used by p115client.tool.life (web variant).
        Without a `type` filter it returns every operation; read-only browse/star events
        are dropped client-side (p115client IGNORE_BEHAVIOR_TYPES) so that watching
        videos never triggers a sync, while uploads, moves, receives, new folders,
        deletions and renames all become hints. Events are hints only; full scoped
        reconciliation makes capped pages/gaps safe. The kept rows feed both the
        fingerprint and the runtime log (new-event monitoring).
        """
        data=await self.drive.request('GET','https://webapi.115.com/behavior/detail',params={'limit':64,'offset':0})
        rows=data.get('data',{}).get('list')
        if not isinstance(rows,list):raise DriveError('115 生活事件格式异常')
        for r in rows:
            if not isinstance(r,dict) or 'id' not in r:raise DriveError('115 生活事件缺少 ID')
        rows=[r for r in rows if behavior_type(r) not in IGNORE_BEHAVIOR_TYPES]
        values=[]
        for r in rows:
            values.append([r.get(k) for k in ('id','file_id','type','update_time')])
        return hashlib.sha256(json.dumps(values,sort_keys=True).encode()).hexdigest(),rows

    async def life_fingerprint(self):
        return (await self.life_feed())[0]

    @staticmethod
    def life_event_text(row):
        """Defensive rendering with the operation name (1.8.0: all mutation types)."""
        label=BEHAVIOR_LABELS.get(behavior_type(row),'变动')
        name=row.get('file_name') or row.get('name') or ''
        if not isinstance(name,str) or not name:
            return '检测到 115 网盘'+label+'事件（文件 #'+str(row.get('file_id') or '?')+'）'
        text='检测到 115 网盘'+label+'：'+name
        size=row.get('file_size') or row.get('size')
        if isinstance(size,(int,float)) and size>0:
            text+='（'+(f'{size/1024/1024/1024:.2f}GB' if size>=1024**3 else f'{size/1024/1024:.1f}MB' if size>=1024**2 else f'{size/1024:.0f}KB')+'）'
        return text

    async def log_new_life_events(self,rows):
        """Write newly appeared 115 behavior events to the runtime log (best effort).

        First observation only records a start marker, so the log is not flooded
        with up to 64 historical entries. The v2 store key resets the seen set on
        upgrade from 1.7.x: the wider feed (moves/deletes/renames...) would
        otherwise replay its whole history as "new" events exactly once.
        """
        try:
            seen=await self.store.get('life-seen-ids-v2',default=None)
            ids=[str(r.get('id')) for r in rows]
            if seen is None:
                await log_event(self.store,'info','115事件',f'开始监控 115 网盘操作事件（已忽略历史 {len(rows)} 条）' if rows else '开始监控 115 网盘操作事件')
            else:
                fresh=[r for r in rows if str(r.get('id')) not in set(seen)]
                for r in fresh[:20]:
                    await log_event(self.store,'info','115事件',self.life_event_text(r))
                if len(fresh)>20:
                    await log_event(self.store,'info','115事件',f'另有 {len(fresh)-20} 条新操作事件未逐条显示')
            await self.store.set('life-seen-ids-v2',ids[:64])
        except Exception:pass
    async def record_history(self,mode,status,error=''):
        """Append to the persistent run log (qmediasync Sync-style), keeping the last 20."""
        try:
            rows=await self.store.get('sync-history',default=[]) or []
            rows.append({'time':time.strftime('%Y-%m-%d %H:%M:%S'),'mode':mode,
                         'written':(status or {}).get('written',0),'unchanged':(status or {}).get('unchanged',0),
                         'sidecars':(status or {}).get('sidecars',0),'deleted':(status or {}).get('deleted',0),
                         'conflicts':(status or {}).get('conflicts',0),
                         'seen':(status or {}).get('seen',0),
                         'elapsed':(status or {}).get('elapsed',0),
                         'mappings':(status or {}).get('mappings',[]),
                         'error':error})
            await self.store.set('sync-history',rows[-20:])
        except Exception:pass
        label='全量' if mode=='full' else '增量'
        if error:
            await log_event(self.store,'error','同步',f'{label}同步失败（耗时 {(status or {}).get("elapsed",0)}s，扫描 {(status or {}).get("seen",0)}）：{error}')
        else:
            conflicts=(status or {}).get('conflicts',0)
            await log_event(self.store,'info','同步',
                f"{label}同步完成 · 耗时 {(status or {}).get('elapsed',0)}s · 扫描 {(status or {}).get('seen',0)}"
                f" · 写入 {(status or {}).get('written',0)} · 未变化 {(status or {}).get('unchanged',0)}"
                f" · 附属 {(status or {}).get('sidecars',0)} · 清理 {(status or {}).get('deleted',0)}"
                + (f" · 同名冲突 {conflicts}（详见运行日志）" if conflicts else ''))
    async def tick(self,force=False,mode='incremental'):
        async with self.lock:
            # 2.1.1: cookie first, mappings second. parsed_mappings derives the
            # account-scoped library_id from the cookie, so probing mappings
            # before a login exists raises instead of guiding the user.
            if not self.cfg.cookie.get_secret_value():
                self.state.update(state='waiting_login',error='请先扫码登录 115（面板「115账号」页）');return
            if not self.engine.cfg.parsed_mappings:
                self.state.update(state='waiting_config',error='请先配置同步映射（多目录映射或单目录 CID + STRM 目录）');return
            namespace=hashlib.sha256((uid(self.cfg.cookie.get_secret_value())+'|'+self.cfg.cloud_cid+'|'+self.cfg.strm_directory).encode()).hexdigest()[:24]
            key='life-strm:'+namespace;previous=await self.store.get(key,default={})
            fingerprint=None;self.state['life_error']=''
            if self.cfg.life_events:
                try:
                    fingerprint,rows=await self.life_feed()
                    await self.log_new_life_events(rows)
                except Exception as e:self.state['life_error']=str(e) if isinstance(e,DriveError) else '生活事件暂不可用，仍会定时补扫'
            dirty=await self.store.get('strm-upload-dirty',default=False)
            prev_fp=previous.get('fingerprint') if isinstance(previous,dict) else None
            prev_synced=previous.get('synced_at',0) if isinstance(previous,dict) else 0
            if self.baseline_pending and not force:
                # 2.7.3: plugin (re)start -- install/upgrade, host restart, config
                # save -- never starts a sync by itself. Record the event baseline,
                # drop a leftover "needs sync" flag from an interrupted/stopped run,
                # and let cron count from NOW instead of catching up a missed slot.
                self.baseline_pending=False
                if fingerprint is not None and fingerprint!=prev_fp:
                    await self.store.set(key,{'synced_at':prev_synced,'fingerprint':fingerprint})
                if dirty:await self.store.delete('strm-upload-dirty')
                self.state['polled_at']=time.time()
                self.state['next_run_at']=self.next_run_after(max(prev_synced,self.started_at))
                return
            self.baseline_pending=False
            # 2.6.3：首次启动（尚无事件指纹基线 / 从未同步过）不得自动触发同步——
            # 否则插件一加载，事件指纹「无→有」，或 cron 从 synced_at=0（epoch）算出
            # 的过去时间，都会把同步自动跑起来（用户没点「开始」却看到全量在跑）。
            changed=fingerprint is not None and prev_fp is not None and fingerprint!=prev_fp
            next_run=self.next_run_after(max(prev_synced,self.started_at)) if prev_synced else 0
            due=bool(prev_synced) and time.time()>=next_run
            self.state['polled_at']=time.time();self.state['next_run_at']=next_run
            if not (force or dirty or changed or due):
                # 只记录事件指纹基线（不触发同步），让下一次真正的事件变化才触发
                if fingerprint is not None and prev_fp is None:
                    await self.store.set(key,{'synced_at':prev_synced,'fingerprint':fingerprint})
                return
            self.state.update(state='running',error='')
            # Consume first; another upload during enumeration sets a fresh dirty flag.
            await self.store.delete('strm-upload-dirty')
            self.stop_requested=False
            self.sync_task=asyncio.ensure_future(self.engine.run(mode=mode))
            try:
                try:
                    result=await self.sync_task
                except asyncio.CancelledError:
                    if not self.stop_requested:
                        self.sync_task.cancel();raise  # plugin shutdown / reload
                    # User pressed「停止同步」: a normal end, not a failure, and
                    # nothing is queued to restart it.
                    status=dict(self.engine.status)
                    self.state.update(state='cancelled',error='',mode=mode)
                    try:await log_event(self.store,'info','同步','已手动停止%s同步（已运行 %ss，写入 %d，未变化 %d；已写入的 STRM 保留）'%(
                        '全量' if mode=='full' else '增量',status.get('elapsed',0),status.get('written',0),status.get('unchanged',0)))
                    except Exception:pass
                    return
                checkpoint={'synced_at':time.time(),'fingerprint':fingerprint if fingerprint is not None else previous.get('fingerprint')}
                await self.store.set(key,checkpoint)
                self.state.update(state='completed',mode=mode,written=result['written'],unchanged=result['unchanged'],sidecars=result.get('sidecars',0),deleted=result.get('deleted',0),skipped=result.get('skipped',0),synced_at=checkpoint['synced_at'],next_run_at=self.next_run_after(checkpoint['synced_at']),scan_notice=self.ops.notice)
                await self.record_history(mode,result,'')
            except asyncio.CancelledError:
                # 2.7.3: no "needs sync" flag on cancellation -- that flag is what
                # made a stopped sync restart within a minute and an upgraded
                # plugin start a sync on boot.
                raise
            except PermissionError as e:
                # The host sandbox names the exact path and grant problem -- keep it.
                message='STRM 生成失败，写入被拒绝：'+(str(e).strip() or '未知路径')[:300]
                self.state.update(state='failed',error=message)
                await self.record_history(mode,dict(self.engine.status),message)
            except Exception as e:
                # Failures are retried by the next cron slot / 115 event, not in a
                # tight loop (2.7.3).
                # 2.3.1: host RPC failures arrive as ValueError etc. WITH a precise
                # message (e.g. "没有配置外部访问地址…") -- surface it instead of the
                # old bare "（ValueError），请查看日志" that hid the actual cause.
                message=str(e) if isinstance(e,DriveError) else 'STRM 生成失败：'+describe_failure(e)
                self.state.update(state='failed',error=message)
                # 2.7.8: the history row carries the real elapsed/scanned counts
                # (it used to say「耗时 0s，扫描 0」for every failure).
                await self.record_history(mode,dict(self.engine.status),message)
                if not isinstance(e,DriveError):
                    # Unexpected exception types: log where in THIS plugin it
                    # happened (the old 600-char tail showed only httpx internals).
                    try:await log_event(self.store,'error','同步','同步异常位置：'+plugin_frames(e))
                    except Exception:pass
    async def stop(self,timeout=60.):
        """Stop the running sync, whoever started it. Returns True once it has
        actually ended."""
        task=self.sync_task
        if task is None or task.done():return None
        self.stop_requested=True
        self.engine.cancelled=True  # also breaks a throttle cooldown wait
        task.cancel()
        done,_=await asyncio.wait({task},timeout=timeout)
        return bool(done)
    async def loop(self,first=True):
        while True:
            self.signal.clear()
            if first:
                # 2.6.0: boot no longer force-runs a full sync. A big library used
                # to pay the whole enumeration the moment the plugin loaded, which
                # is exactly the "stuck from the start" feeling on upload. The loop
                # still starts (event monitoring + cron recovery stay alive); a sync
                # happens on the first upload/move event or the cron schedule, and
                # the user can always trigger one from the panel.
                first=False
                try:
                    await self.tick(force=False)
                except asyncio.CancelledError:raise
                except Exception as e:self.state.update(state='failed',error='STRM 检查异常（'+type(e).__name__+'）')
            try:await asyncio.wait_for(self.signal.wait(),timeout=self.cfg.life_interval)
            except asyncio.TimeoutError:continue
            # Coalesce an upload burst, without waiting for the next life poll.
            await asyncio.sleep(3)
            try:
                await self.tick(force=False)
            except asyncio.CancelledError:raise
            except Exception as e:self.state.update(state='failed',error='STRM 检查异常（'+type(e).__name__+'）')
