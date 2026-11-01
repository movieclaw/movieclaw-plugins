"""Snapshot-based incremental STRM writes through MovieClaw's approved file interface."""
import asyncio
import hashlib
import json
import os
import re
import time
import uuid
import zlib
from pathlib import PurePosixPath
from urllib.parse import quote,parse_qs,urlsplit
import httpx
from movieclaw_sdk import net
from drive_api import DriveError,LimitError,ThrottleError,rebase_url
from runlog import log_event

VIDEO={'.mkv','.mp4','.avi','.mov','.m4v','.ts','.m2ts','.wmv','.flv','.webm','.mpg','.mpeg','.iso'}
SUBTITLE={'.srt','.ass','.ssa','.vtt','.sub','.idx'}
# Sidecars ride along with a same-stem video; folder-level names match any video in the directory.
SIDECAR=SUBTITLE|{'.sup','.nfo','.jpg','.jpeg','.png','.webp','.tbn'}
# 2.7.5: artwork sub-folders that belong to the folder above them.
EXTRA_DIRS={'extrafanart','extrathumbs'}
# Manifest keys of standalone sidecar records (images/NFO/subtitles).
SIDE='\u00a7'
SPECIAL={'poster.jpg','fanart.jpg','folder.jpg','cover.jpg','movie.nfo','tvshow.nfo'}
SIDECAR_MAX=20*1024*1024


def video_exts(cfg):
    """Configured video suffixes (comma separated); falls back to the built-in set."""
    raw=getattr(cfg,'video_suffixes','') or ''
    exts={'.'+part.strip().lstrip('.').lower() for part in raw.split(',') if part.strip()}
    return exts or VIDEO


def scope_key(mapping):
    return hashlib.sha256(json.dumps(mapping.model_dump(),sort_keys=True).encode()).hexdigest()[:20]


def conflict_rank(item):
    """Deterministic order for same-name conflicts: bigger file wins, then newer, then pickcode.

    Bigger usually means the real feature (a re-encode/sample next to it is smaller);
    the total order keeps the winner stable across runs so the manifest never flaps.
    """
    try:size=int(item.get('size') or 0)
    except (TypeError,ValueError):size=0
    m=item.get('mtime') or 0
    try:m=int(m)
    except (TypeError,ValueError):
        try:m=float(m)
        except (TypeError,ValueError):m=0
    return (size,m,str(item.get('pickcode') or ''))


def human_size(n):
    try:n=float(n or 0)
    except (TypeError,ValueError):n=0
    if n>=1024**3:return '%.1f GB'%(n/1024**3)
    if n>=1024**2:return '%.1f MB'%(n/1024**2)
    if n>=1024:return '%.1f KB'%(n/1024)
    return '%d B'%n


def safe_relative(path):
    p=PurePosixPath(path)
    if p.is_absolute() or not p.parts or any(v in ('.','..') for v in path.split('/')) or '\\' in path or '\x00' in path:
        raise DriveError('文件路径不安全，同步已停止')
    return p


async def atomic_write(files,path,content,dir_cache=None,direct=False):
    """Write one file atomically (temp + rename) through the host file API.

    dir_cache (2.4.0) remembers directories already created during this run:
    every STRM write used to pay its own makedirs roundtrip even though its
    siblings had just created that exact directory, so a directory with 10
    videos cost 10 redundant IPC roundtrips that competed with the walk's own
    requests on a big library.
    """
    directory=os.path.dirname(path)
    cache=dir_cache if dir_cache is not None else set()
    if direct:
        # 2.7.3: a NEW file (nothing there to protect) is written in place --
        # one host call instead of open-temp + rename. A torn write leaves a
        # prefix of our own link, which the next run recognises and heals.
        async def write():
            out=await files.open(path,'wb')
            with out:out.write(content);out.flush()
        if directory not in cache:
            await files.makedirs(directory);cache.add(directory)
        try:await write()
        except (FileNotFoundError,NotADirectoryError):
            cache.discard(directory);await files.makedirs(directory);cache.add(directory);await write()
        return
    async def write():
        # 2.7.0: no fsync (a few hundred bytes; rename is the atomicity
        # guarantee, and a torn/empty STRM is healed by the next run) and no
        # exists() roundtrip on the success path -- 2 host IPCs per file saved.
        temp=os.path.join(directory,'.p115-'+uuid.uuid4().hex+'.tmp')
        ok=False
        try:
            out=await files.open(temp,'wb')
            with out:
                def dump():out.write(content);out.flush()
                # A STRM is a few hundred bytes: write inline. Large sidecars
                # (posters) still go through a worker thread.
                if len(content)>262144:await asyncio.to_thread(dump)
                else:dump()
            await files.rename(temp,path);ok=True
        finally:
            if not ok and await best_effort_exists(files,temp):
                await best_effort(files.remove(temp))
    if directory not in cache:
        await files.makedirs(directory);cache.add(directory)
    try:
        await write()
    except (FileNotFoundError,NotADirectoryError):
        # The tree changed under us mid-run (or the cached assumption went
        # stale): rebuild the directory once, then let a second failure
        # surface honestly instead of silently writing "success".
        cache.discard(directory)
        await files.makedirs(directory);cache.add(directory)
        await write()


async def best_effort_exists(files,path):
    try:return await asyncio.wait_for(files.exists(path),2)
    except Exception:return False
    except asyncio.CancelledError:return False


async def read_small(files,path):
    f=await files.open(path,'rb')
    with f:return await asyncio.to_thread(f.read,65537)


async def read_bytes(files,path,limit):
    f=await files.open(path,'rb')
    with f:return await asyncio.to_thread(f.read,limit)


async def best_effort(coro):
    """Best-effort persist during cancellation. Dispose freezes the runner's message
    loop, so an RPC issued while unwinding can never be answered; cap the wait and
    move on instead of hanging plugin shutdown."""
    try:await asyncio.wait_for(coro,2)
    except Exception:pass
    except asyncio.CancelledError:pass


class PlayIndex:
    """fid -> {pickcode, name} for the /play route (2.7.3).

    Up to 2.7.2 every STRM cost its own host store write ('play:<fid>'). Now
    records collect in memory and are written in 1024 shards
    ('playx:<nnn>'), so a 20k-file first sync costs ~1k shard writes
    instead of 20k, and an incremental run touches a handful. A registry
    ('playx-index') lists shards that exist, so a brand-new shard is written
    without a pointless read first. The /play route
    reads pending records straight from memory, so a freshly written STRM is
    playable before its shard is flushed; legacy 'play:<fid>' keys from older
    versions are still read."""
    FLUSH_AT=3000
    SHARD_MAX=60000
    SHARDS=1024
    def __init__(self,store):
        self.store=store;self.pending={};self.dropped=set();self.lock=asyncio.Lock()
        self.known=None  # shard keys that exist (from 'playx-index')
    @classmethod
    def shard(cls,fid):return 'playx:%03x'%(int(fid[:4],16)%cls.SHARDS)
    def put(self,fid,record):
        self.pending[fid]=record;self.dropped.discard(fid)
    def drop(self,fid):
        self.pending.pop(fid,None);self.dropped.add(fid)
    def due(self):return len(self.pending)+len(self.dropped)>=self.FLUSH_AT and not self.lock.locked()
    async def get(self,fid):
        if fid in self.pending:return self.pending[fid]
        if fid in self.dropped:return None
        shard=await self.store.get(self.shard(fid),default=None)
        if isinstance(shard,dict) and isinstance(shard.get(fid),dict):return shard[fid]
        return await self.store.get('play:'+fid)
    async def flush(self):
        async with self.lock:
            if not self.pending and not self.dropped:return
            pending=dict(self.pending);dropped=set(self.dropped)
            groups={}
            for fid in set(pending)|dropped:groups.setdefault(self.shard(fid),[]).append(fid)
            if self.known is None:
                idx=await self.store.get('playx-index',default=None)
                self.known=set(idx) if isinstance(idx,list) else set()
            known=set(self.known)
            if not set(groups)<=self.known:
                # Register new shards BEFORE writing them: an unregistered shard
                # would later be overwritten without being read.
                self.known|=set(groups)
                await self.store.set('playx-index',sorted(self.known))
            gate=asyncio.Semaphore(8)
            async def one(sk,fids):
                async with gate:
                    cur=(await self.store.get(sk,default={})) if sk in known else {}
                    if not isinstance(cur,dict):cur={}
                    for fid in fids:
                        if fid in pending:cur[fid]=pending[fid]
                        else:cur.pop(fid,None)
                    overflow={}
                    while cur and len(json.dumps(cur,ensure_ascii=False).encode())>self.SHARD_MAX:
                        fid=next(iter(cur));overflow[fid]=cur.pop(fid)
                    await self.store.set(sk,cur)
                    for fid,rec in overflow.items():await self.store.set('play:'+fid,rec)
                    for fid in fids:
                        if fid in dropped:
                            try:await self.store.delete('play:'+fid)
                            except Exception:pass
            await asyncio.gather(*(one(sk,f) for sk,f in groups.items()))
            for fid,rec in pending.items():
                if self.pending.get(fid) is rec:del self.pending[fid]
            self.dropped-=dropped


# Any link this plugin signs: /api/v1/plugins/p115-library/play/<24 hex>.
OUR_LINK=re.compile(rb'/plugins/p115-library/play/[0-9a-f]{24}(?:[?#\s]|$)')


def strm_is_ours(raw,fid,expected,prior=None,path=None):
    """Ownership without a separate write-intent record (2.7.3): empty, the
    recorded content, our own signed /play/<fid> link for this very file, or
    a torn prefix of what we are about to write."""
    if not raw.strip():return True
    if prior and prior.get('path')==path and hashlib.sha256(raw).hexdigest()==prior.get('url_hash'):return True
    if len(raw)<8192 and (('/play/'+fid).encode() in raw or OUR_LINK.search(raw)):return True
    return bool(expected) and expected.startswith(raw)


class SyncEngine:
    # Depth of the walk → writer queue (2.4.0). Bounded on purpose: a paced but
    # unthrottled enumerator must never buffer the whole library in memory
    # while the writer falls behind. 2.6.0: widened for parallel writers.
    WRITE_QUEUE=256
    # 2.6.0: parallel STRM writers. apply() is safe to run concurrently --
    # conflict arbitration already happens in produce() (single-threaded), and
    # every shared read-modify-write inside apply() completes before its first
    # await, so asyncio's cooperative scheduling never interleaves them.
    # 2.7.2: 16 writers, each owning one shard queue (an output path always
    # hashes to the same writer, so a same-name replacement during streaming
    # can never race the earlier candidate's write).
    WRITERS=16
    SHARD_QUEUE=64
    STATUS_KEY='sync-status'
    # 2.7.0: time-based progress log (was every 10 files, which flooded the
    # 100-entry run log and cost two store roundtrips per 10 files).
    PROGRESS_LOG_SECONDS=20.
    # 2.7.4: progress checkpoint cadence (see sync_mapping.save_progress).
    # 2.7.10: near-real-time -- a hard exit (container killed, OOM, host
    # restart) loses at most a few seconds of work.
    PROGRESS_SAVE_FILES=300
    PROGRESS_SAVE_SECONDS=3.
    PLAY_FLUSH_SECONDS=30.
    SIDECAR_RETRY_DELAYS=(1.,3.,0.)
    async def manifest_nodes(self,key):
        """Node key -> value, in manifest ORDER (pre-order, children left to
        right). 2.7.4: order matters -- progress checkpoints append nodes at
        the end, and a later node's record overrides an earlier one."""
        root=await self.store.get(key,default={'children':[]})
        if not isinstance(root,dict):root={'children':[]}
        nodes={}
        async def visit(level):
            values=await asyncio.gather(*(self.store.get(k,default={}) for k in level))
            for k,data in zip(level,values):
                if not isinstance(data,dict):data={}
                nodes[k]=data
                if data.get('children'):await visit(list(data['children']))
        children=list(root.get('children',[]))
        for i in range(0,len(children),64):await visit(children[i:i+64])
        return nodes
    async def load_manifest(self,key):
        nodes=await self.manifest_nodes(key)
        # Remember which node keys back this manifest so save_manifest does
        # not have to re-read every node just to learn their names.
        self._manifest_keys=getattr(self,'_manifest_keys',{});self._manifest_keys[key]=list(nodes)
        result={}
        for data in nodes.values():result.update(data.get('records',{}))
        return result
    @staticmethod
    def _chunk_records(data):
        chunk={};size=32
        for name,record in data.items():
            extra=len(json.dumps({name:record},ensure_ascii=False).encode())+2
            if extra>60000:raise DriveError('单个同步记录过大')
            if chunk and (size+extra>60000 or len(chunk)>=400):
                yield chunk;chunk={};size=32
            chunk[name]=record;size+=extra
        if chunk:yield chunk
    async def save_manifest(self,key,data):
        self.__dict__.setdefault('_tails',{}).pop(key,None)  # root is rewritten: no open tail
        cached=getattr(self,'_manifest_keys',{}).pop(key,None)
        old=cached if cached is not None else await self.manifest_nodes(key)
        indexes=[];generation=uuid.uuid4().hex;number=0
        async def put(value):
            nonlocal number
            node=key+':'+generation+':'+str(number);number+=1
            await self.store.set(node,value)
            return node
        for chunk in self._chunk_records(data):indexes.append(await put({'records':chunk}))
        while len(indexes)>200:
            indexes=[await put({'children':indexes[i:i+200]}) for i in range(0,len(indexes),200)]
        await self.store.set(key,{'children':indexes})
        for node in old:
            try:await self.store.delete(node)
            except Exception:pass
        self._manifest_keys=getattr(self,'_manifest_keys',{});self._manifest_keys.pop(key,None)
    APPEND_COMPACT=400
    async def append_manifest(self,key,delta):
        """Progress checkpoint (2.7.4; 2.7.10 near-real-time).

        Changed records go into a "tail" node at the END of the manifest (later
        nodes override earlier ones on load). The tail node is REWRITTEN on
        every save until it is full (~60 KB / 400 records); only then is a new
        tail started and the root list updated. So a save every few seconds
        costs one store write (two when a new tail begins) instead of a new
        node + root rewrite each time. Write order is node first, root second,
        so a crash at any instant leaves a loadable manifest."""
        if not delta:return None
        tails=self.__dict__.setdefault('_tails',{})
        tail=tails.get(key)
        if tail is None:
            root=await self.store.get(key,default={'children':[]})
            if not isinstance(root,dict):root={'children':[]}
            tail=tails[key]={'node':None,'records':{},'size':32,'children':list(root.get('children',[]))}
        new_nodes=[]
        for name,record in delta.items():
            extra=len(json.dumps({name:record},ensure_ascii=False).encode())+2
            if extra>60000:raise DriveError('单个同步记录过大')
            full=tail['node'] is None or ((tail['size']+extra>60000 or len(tail['records'])>=400) and name not in tail['records'])
            if full:
                if tail['node'] is not None:await self.store.set(tail['node'],{'records':tail['records']})
                node=key+':a:'+uuid.uuid4().hex
                tail.update(node=node,records={},size=32);tail['children'].append(node);new_nodes.append(node)
            if name not in tail['records']:tail['size']+=extra
            tail['records'][name]=record
        await self.store.set(tail['node'],{'records':tail['records']})
        if new_nodes:
            await self.store.set(key,{'children':list(tail['children'])})
            cache=getattr(self,'_manifest_keys',{})
            if key in cache:cache[key].extend(new_nodes)
        return len(tail['children'])
    async def load_checkpoint(self,key):
        """Read the resume checkpoint (2.6.x). The host caps a plugin-data value
        at 64 KB, so a big library's done-dir set must be chunked: the root
        stores a node list, each node {'dirs': {...}} holds one slice. The
        legacy single-value {'done': {...}} is still read so a 2.6.0/2.6.1
        checkpoint that happened to stay under the cap is not lost."""
        root=await self.store.get(key,default={})
        if not isinstance(root,dict):return {}
        if isinstance(root.get('done'),dict):return dict(root['done'])
        done={}
        for nk in root.get('nodes') or []:
            node=await self.store.get(nk,default={})
            if isinstance(node,dict) and isinstance(node.get('dirs'),dict):
                done.update(node['dirs'])
        return done
    async def save_checkpoint(self,key,done):
        """Persist the done-dir set in chunks so a big library never blows the
        host's 64 KB per-value limit (2.6.2). Sorted for determinism; each
        chunk is a node {'dirs': {...}} under 60 KB; the root stores the node
        list and orphaned nodes are deleted."""
        done=dict(done)
        old=await self.store.get(key,default={})
        old_nodes=(old.get('nodes') or []) if isinstance(old,dict) else []
        nodes=[];chunk={};size=32;gen=uuid.uuid4().hex;num=0
        async def flush():
            nonlocal chunk,size,num
            if not chunk:return
            nk=key+':n:'+gen+':'+str(num);num+=1
            await self.store.set(nk,{'dirs':chunk})
            nodes.append(nk);chunk={};size=32
        for cid in sorted(done):
            extra=len(cid.encode())+8
            if chunk and size+extra>60000:await flush()
            chunk[cid]='1';size+=extra
        await flush()
        await self.store.set(key,{'nodes':nodes,'count':len(done),'at':int(time.time())})
        for nk in old_nodes:
            if nk not in nodes:
                try:await self.store.delete(nk)
                except Exception:pass
    async def append_checkpoint(self,key,done,flushed_done):
        """Incrementally append the dirs that finished since the last flush
        (2.6.3). Rewriting every chunk on each completion was why the progress
        counter only seemed to move when a sync ended; appending one small node
        keeps the checkpoint fresh continuously. When too many nodes pile up,
        fall back to a full rewrite (which also compacts them)."""
        new=[cid for cid in done if cid not in flushed_done]
        if not new:return
        root=await self.store.get(key,default={})
        nodes=list((root.get('nodes') or []) if isinstance(root,dict) else [])
        if len(nodes)>32:
            await self.save_checkpoint(key,done)
            flushed_done.clear();flushed_done.update(done)
            return
        nk=key+':n:'+uuid.uuid4().hex+':'+str(len(nodes))
        await self.store.set(nk,{'dirs':{cid:done[cid] for cid in new}})
        nodes.append(nk)
        await self.store.set(key,{'nodes':nodes,'count':len(done),'at':int(time.time())})
        flushed_done.update(new)
    def __init__(self,cfg,drive,store,files,ops,routes,ctx):
        self.cfg,self.drive,self.store,self.files,self.ops,self.routes,self.ctx=cfg,drive,store,files,ops,routes,ctx
        self.lock=asyncio.Lock();self.cancelled=False;self.status={'state':'idle'}
        self.sidecar_hashes={}
        self.play_index=PlayIndex(store)
        # Directories already created this run -- see atomic_write().
        self._dirs=set()
        # Counters are exact in memory; the store roundtrip is rate limited.
        self.FLUSH_INTERVAL=0.7;self._flushed=0.
        # 2.4.0: the drive layer announces throttled waits through these, so a
        # several-minute cooldown reads as "等待 115 冷却" instead of a hang.
        self.drive.on_wait=self.throttle_begin
        self.drive.on_resume=self.throttle_end
        self.drive.cancel_check=lambda:self.cancelled
        # 2.6.0: dedup marker for the every-10-files progress log line.
        self._last_logged=0
    async def throttle_begin(self,delay,attempt,attempts,reason):
        wait=int(round(delay))
        await self.report(flush=True,throttle_until=int(time.time())+wait,throttle_wait=wait,
                          throttle_count=attempt,throttle_limit=attempts,throttle_reason=str(reason)[:160])
        try:
            await log_event(self.store,'warn','同步','115 限速（%s）：已自动等待 %d 秒后继续扫描并继续生成 STRM（第 %d/%d 次）。频繁出现请把「核心配置 → API请求间隔」调大（如 3~5 秒）'%(str(reason)[:120],wait,attempt,attempts))
        except Exception:pass  # logging must never break the request layer
    async def throttle_end(self):
        await self.report(flush=True,throttle_until=0,throttle_wait=0)
    async def report(self,flush=False,**values):
        self.status.update(values)
        # 2.4.0: every counter bump used to be its own store roundtrip; on a
        # big library those thousands of IPC calls compete with the walk's own
        # requests. Counters stay exact in memory and land at most ~1.5x/s
        # (the panel polls every 5s); state transitions force a flush.
        now=time.monotonic()
        if flush or now-self._flushed>=self.FLUSH_INTERVAL:
            self._flushed=now
            await self.store.set(self.STATUS_KEY,self.status)
        return self.status
    async def run(self,mode='incremental',dry_run=False):
        if self.lock.locked():raise DriveError('已有同步正在运行')
        async with self.lock:
            self.cancelled=False;self.sidecar_hashes={};self._dirs=set();self._tails={};self._flushed=0.;self._last_logged=time.monotonic();self._pending_marked=set();self._run_mode=mode
            self.drive.throttle_stats={'waits':0,'waited':0.,'last':''}
            started=time.monotonic()
            async def finish(**values):
                # Every terminal state carries the elapsed time so the sync history
                # shows how long failed runs took too, not just successful ones.
                values['elapsed']=round(time.monotonic()-started,1)
                await self.report(flush=True,**values)
            await self.report(flush=True,state='running',mode=mode,dry_run=dry_run,running_at=int(time.time()),seen=0,written=0,unchanged=0,deleted=0,sidecars=0,conflicts=0,skipped=0,mappings=[],error='',scan_pending=False,throttle_until=0,throttle_wait=0,throttle_count=0,throttle_limit=0,throttle_reason='')
            try:
                for mapping in self.cfg.parsed_mappings:
                    await self.sync_mapping(mapping,mode,dry_run)
                waits=self.drive.throttle_stats
                if waits['waits']:
                    try:
                        await log_event(self.store,'warn','同步','本次同步遇到 115 限速 %d 次，累计等待 %.0f 秒后自动继续（未失败，STRM 已随扫描持续生成）'%(waits['waits'],waits['waited']))
                    except Exception:pass
                await finish(state='completed')
            except asyncio.CancelledError:
                # Keep what was written playable after「停止同步」: persist the
                # in-memory play records (bounded -- on plugin shutdown the host
                # may no longer answer).
                try:await asyncio.wait_for(self.play_index.flush(),30)
                except BaseException:pass
                await best_effort(finish(state='cancelled'));raise
            except PermissionError as e:
                try:await asyncio.wait_for(self.play_index.flush(),30)
                except Exception:pass
                # Writes that slip past the probe (e.g. a read-only subdirectory) still
                # surface the host's exact path and reason.
                message='同步写入被拒绝：'+(str(e).strip() or '未知路径')[:300]
                await finish(state='failed',error=message)
                raise DriveError(message) from None
            except Exception as e:
                try:await asyncio.wait_for(self.play_index.flush(),30)
                except Exception:pass
                # 2.3.1: keep the exception's own text (host RPC errors arrive as
                # ValueError/FileNotFoundError etc. WITH a precise Chinese message
                # -- dropping it produced the useless "同步失败（ValueError）").
                detail=(str(e).strip() or '无详细信息')[:300]
                await finish(state='failed',error=str(e) if isinstance(e,DriveError) else '同步失败（'+type(e).__name__+'）：'+detail)
                raise
            return dict(self.status)
    async def fetch_cloud_sidecar(self,pickcode):
        """Fetch a small cloud-only sidecar through 115's signed direct link.

        The CDN URL is bound to the UA that minted it (f=1) or to the account
        Cookie (f=3), and we are the fetcher -- so replay the signing UA and,
        for f=3 links, attach the account Cookie like alist's proxy mode.
        """
        # 2.7.8: pooled client, explicit timeouts, retries. Any failure is a
        # DriveError (the caller skips that one file and retries next run);
        # before, a raw httpx.ReadTimeout from one slow CDN file escaped and
        # failed the WHOLE sync.
        client=getattr(self,'_cdn',None)
        if client is None or client.is_closed:
            client=self._cdn=httpx.AsyncClient(transport=net.http_transport('p115-library'),trust_env=False,follow_redirects=True,
                timeout=httpx.Timeout(connect=15.,read=60.,write=15.,pool=60.))
        last='';fresh=False
        for attempt in range(3):
            if self.cancelled:raise asyncio.CancelledError()
            try:
                if fresh:
                    invalidate=getattr(self.drive,'invalidate_links',None)
                    if invalidate:invalidate(pickcode)
                    url,sign_ua,sign_headers=await self.drive.link_signed(pickcode,allow_cookie_url=True,fresh=True)
                else:
                    url,sign_ua,sign_headers=await self.drive.link_signed(pickcode,allow_cookie_url=True)
                # Replay the signing UA EXACTLY -- even when it is empty. Omitting the
                # header would let httpx send its python-httpx default and the CDN,
                # which binds f=1 links to the minting UA, would answer 403.
                headers={'User-Agent':sign_ua}
                for k,v in (sign_headers or {}).items():headers.setdefault(k,v)
                # alist's buildDownloadHeaders always attaches the account Cookie
                # (the CDN 403s with "no cookie value" when it is missing).
                headers['Cookie']=self.cfg.cookie.get_secret_value()
                r=await client.get(url,headers=headers)
                if r.status_code==200:
                    data=r.content
                    if len(data)>SIDECAR_MAX:raise DriveError('附属文件超过大小限制')
                    return data
                last='HTTP %d'%r.status_code
                if r.status_code in (401,403,410):fresh=True  # link expired: re-sign
                elif r.status_code not in (408,429,500,502,503,504):break
            except asyncio.CancelledError:raise
            except DriveError as e:
                last=str(e)[:120]
                if '超过大小' in last:break
            except Exception as e:
                last='网络错误 '+type(e).__name__
            await asyncio.sleep(self.SIDECAR_RETRY_DELAYS[attempt])
        raise DriveError('附属文件下载失败（%s）'%last)
    def rebuild_play_records(self,mapping,records):
        sk=scope_key(mapping);n=0
        for rel,rec in records.items():
            if rel.startswith(SIDE) or not isinstance(rec,dict):continue
            fp=rec.get('fingerprint');rid=rec.get('id')
            if not rid or not isinstance(fp,list) or len(fp)<3 or not fp[2]:continue
            fid=hashlib.sha256((sk+':'+str(rid)).encode()).hexdigest()[:24]
            name=os.path.basename(str(rec.get('path') or rel))
            if name.endswith('.strm'):name=name[:-5]
            self.play_index.put(fid,{'pickcode':str(fp[2]),'name':name[:120]});n+=1
        return n
    async def play_fallback(self,fid):
        """/play/<fid> lookup of last resort (2.7.10). After a hard exit a STRM
        can be on disk (and in the saved progress) before its play record was
        flushed. Find it in the saved manifests, repair the play index and
        answer. Manifests are re-read at most every 10 minutes."""
        now=time.monotonic()
        cache=self.__dict__.get('_fallback')
        if cache is None or (fid not in cache and now-self.__dict__.get('_fallback_at',0)>600):
            cache={}
            try:mappings=list(self.cfg.parsed_mappings)
            except Exception:mappings=[]
            for mapping in mappings:
                sk=scope_key(mapping)
                nodes=await self.manifest_nodes('manifest:'+sk)
                records={}
                for data in nodes.values():records.update(data.get('records',{}))
                for rel,rec in records.items():
                    if rel.startswith(SIDE) or not isinstance(rec,dict):continue
                    fp=rec.get('fingerprint');rid=rec.get('id')
                    if not rid or not isinstance(fp,list) or len(fp)<3 or not fp[2]:continue
                    name=os.path.basename(str(rec.get('path') or rel))
                    cache[hashlib.sha256((sk+':'+str(rid)).encode()).hexdigest()[:24]]={'pickcode':str(fp[2]),'name':(name[:-5] if name.endswith('.strm') else name)[:120]}
            self._fallback=cache;self._fallback_at=now
        rec=cache.get(fid)
        if rec:
            self.play_index.put(fid,rec)
            if not self.lock.locked():
                try:await self.play_index.flush()
                except Exception:pass
        return rec
    async def mark_scan_pending(self,key):
        """Remember "this mapping changed on disk" so the run asks MovieClaw to
        rescan it (once per mapping per run). 2.7.13: only INCREMENTAL syncs
        notify the media library; a full sync just reports completion."""
        if getattr(self,'_run_mode','incremental')!='incremental':return
        marked=getattr(self,'_pending_marked',None)
        if marked is None:marked=self._pending_marked=set()
        if key in marked:return
        await self.store.set(key+':scan-pending',True);marked.add(key)
    async def pull_cloud_sidecars(self,base,folder,stem,record,old,key,dry_run,candidates,exists=None,trust=False):
        """Sync subtitles/NFO/posters that exist only in the cloud (qmediasync
        download_meta). They were usually uploaded by other clients, so the
        organized directory cannot supply them locally; fetch via direct link.
        Ownership rules: user-modified files are never touched,
        and the 115 sha1 identity is recorded so later runs detect upstream changes."""
        if not getattr(self.cfg,'sidecar_sync',False) or not candidates:return
        sc=record.setdefault('sidecars',{});cloud=record.setdefault('cloud',{})
        for rel,item in candidates:
            target_rel=rel.as_posix()
            if target_rel in sc:continue  # local organized copy already covers it
            try:target_path=os.path.join(base,*safe_relative(target_rel).parts)
            except DriveError:continue
            previous=(old or {}).get('sidecars',{}).get(target_rel)
            prior_id=(old or {}).get('cloud',{}) if isinstance(old,dict) else None
            prior_id=prior_id.get(target_rel) if isinstance(prior_id,dict) else None
            if not isinstance(prior_id,dict):prior_id=None
            existing=self.sidecar_hashes.get(target_rel)
            exists_fn=exists or self.files.exists
            # 2.7.0: a clean previous run already recorded this sidecar's local
            # hash AND its 115 identity; when 115 still reports the same sha1
            # and the file is still there, trust the record instead of reading
            # the whole poster/NFO back every run (that read-back was MBs of
            # host IPC per movie on large libraries).
            if (existing is None and trust and previous and prior_id and item['sha1']
                    and prior_id.get('sha1')==item['sha1'] and await exists_fn(target_path)):
                sc[target_rel]=previous
                cloud[target_rel]={'sha1':item['sha1'],'size':item['size'],'mtime':item['mtime']}
                continue
            if existing is None and await exists_fn(target_path):
                try:raw=await read_bytes(self.files,target_path,SIDECAR_MAX+1)
                except OSError:continue
                if len(raw)>SIDECAR_MAX:continue
                existing=hashlib.sha256(raw).hexdigest();self.sidecar_hashes[target_rel]=existing
            if existing is not None and existing!=previous:
                sc[target_rel]=existing;continue  # user file: never overwrite
            data=None
            if existing is None or not prior_id or not item['sha1'] or prior_id.get('sha1')!=item['sha1']:
                try:data=await self.fetch_cloud_sidecar(item['pickcode'])
                except DriveError:continue  # transient: retried on the next run
            if data is not None:
                digest=hashlib.sha256(data).hexdigest()
                if existing is None or digest!=existing:
                    if not dry_run:
                        await self.mark_scan_pending(key)
                        await atomic_write(self.files,target_path,data,self._dirs)
                    await self.report(sidecars=self.status.get('sidecars',0)+1)
                    sc[target_rel]=digest;self.sidecar_hashes[target_rel]=digest
                elif existing is not None:sc[target_rel]=existing
            elif existing is not None:sc[target_rel]=existing
            cloud[target_rel]={'sha1':item['sha1'],'size':item['size'],'mtime':item['mtime']}
    async def rewrite_origins(self,target_origin,old_origin='',dry_run=False):
        """2.1.0: refresh the origin baked into existing STRM files.

        A changed external address (new domain/IP/port) strands every on-disk
        STRM on the old origin -- a normal sync would heal them, but that
        costs a full 115 walk behind the API interval. This runs entirely on
        local files plus the plugin's own sync records: zero 115 traffic,
        seconds even for a big library. Only manifest-recorded files whose
        content still matches the recorded url_hash are touched (user-modified
        or foreign files are counted and skipped); the URL path and signature
        stay intact, and both the manifest and the write-intent proofs are
        re-aligned so the next sync counts these as unchanged instead of
        rewriting them again."""
        if self.lock.locked():raise DriveError('已有同步正在运行，请稍后再试')
        target_origin=(target_origin or '').strip().rstrip('/').lower()
        if urlsplit(target_origin).scheme not in ('http','https'):
            raise DriveError('目标地址无效，应为 http(s)://域名或IP[:端口]')
        async with self.lock:
            stats={'target':target_origin,'dry_run':dry_run,'scanned':0,'rewritten':0,'unchanged':0,
                   'missing':0,'modified':0,'mismatched':0,'foreign':0,'examples':[]}
            for mapping in self.cfg.parsed_mappings:
                library=await self.ops.call('library.get',{'library_id':mapping.library_id})
                root=library.get('primary_root')
                if not root or not os.path.isabs(root):raise DriveError('媒体库没有有效的本地主根目录')
                base=os.path.join(root,*safe_relative(mapping.subdirectory).parts)
                key='manifest:'+scope_key(mapping)
                records=await self.load_manifest(key)
                dirty=False
                for relative,record in records.items():
                    if relative.startswith(SIDE):continue
                    stats['scanned']+=1
                    path=record.get('path','')
                    # Only our prior paths inside the mapping's own base, mirroring cleanup.
                    if not path or not (os.path.dirname(path)==base or path.startswith(base+os.sep)):
                        stats['foreign']+=1;continue
                    if not await self.files.exists(path):
                        stats['missing']+=1;continue
                    raw=await read_small(self.files,path)
                    if hashlib.sha256(raw).hexdigest()!=record.get('url_hash'):
                        stats['modified']+=1;continue  # user hand-edited: never touch
                    parts=urlsplit(raw.decode('utf-8','replace').strip())
                    origin=(parts.scheme+'://'+parts.netloc).lower()
                    if parts.scheme not in ('http','https') or not parts.netloc or '/play/' not in parts.path:
                        stats['foreign']+=1;continue
                    if origin==target_origin:
                        stats['unchanged']+=1;continue
                    if old_origin and origin!=old_origin:
                        stats['mismatched']+=1;continue  # explicit old-address filter
                    if len(stats['examples'])<10:
                        stats['examples'].append(relative+'：'+origin+' → '+target_origin)
                    stats['rewritten']+=1
                    if dry_run:continue
                    expected=(target_origin+parts.path+(('?'+parts.query) if parts.query else '')+'\n').encode()
                    await atomic_write(self.files,path,expected)
                    record['url_hash']=hashlib.sha256(expected).hexdigest()
                    dirty=True
                if dirty:await self.save_manifest(key,records)
            return stats
    async def sync_mapping(self,mapping,mode,dry_run):
        # Per-mapping counters for the sync history (1.7.8): snapshot the run totals
        # now and diff them at the end, so each mapping gets its own detail row.
        counters=('seen','written','unchanged','sidecars','deleted','conflicts','skipped')
        before={k:self.status.get(k,0) for k in counters}
        library=await self.ops.call('library.get',{'library_id':mapping.library_id})
        root=library.get('primary_root')
        if not root or not os.path.isabs(root):raise DriveError('媒体库没有有效的本地主根目录')
        base=os.path.join(root,*safe_relative(mapping.subdirectory).parts)
        if not dry_run:
            # Fail fast: probe the output directory BEFORE walking the cloud, and let the
            # host's PermissionError (which names the exact path and grant problem) reach
            # the user instead of dying mid-sync with a generic message.
            try:
                await self.files.makedirs(base)
                probe=os.path.join(base,'.p115-probe-'+uuid.uuid4().hex)
                out=await self.files.open(probe,'wb')
                with out:pass
                await best_effort(self.files.remove(probe))
            except PermissionError as e:
                detail=str(e).strip() or ('无法写入 '+str(base))
                raise DriveError('STRM 目录写入被拒绝：'+detail[:300]
                    +'。请检查：① STRM 目录必须位于 MovieClaw 已配置媒体库的目录之内（2.0.1 起插件只申请媒体库读写授权）——'
                    '在媒体库根下新建子目录即可被授权覆盖，无需在插件清单单独添加路径；若目录在媒体库之外，请把它移入库内，'
                    '或在 MovieClaw 插件授权中单独添加该绝对路径；'
                    '② 操作系统权限——服务器上该目录需允许 MovieClaw 运行用户写入（Linux 常见：目录属 root，需 chown 或 chmod）。') from None
        key='manifest:'+scope_key(mapping)
        previous=await self.load_manifest(key)
        if not isinstance(previous,dict):raise DriveError('同步记录损坏，请先检查插件私有数据')
        exts=video_exts(self.cfg)
        min_bytes=max(0,getattr(self.cfg,'min_video_mb',0) or 0)*1024*1024
        keep_suffix=bool(getattr(self.cfg,'strm_keep_suffix',False))
        link_format=getattr(self.cfg,'strm_link_format','pick_code')
        exist_policy=getattr(self.cfg,'strm_exist_policy','overwrite')
        fast=getattr(self.cfg,'sync_mode','normal')=='fast'
        # Same-name conflict policy (1.7.7): two different videos in one cloud
        # directory (e.g. X.mkv + X.mp4) both squeeze onto one STRM path when the
        # suffix is dropped.
        # 2.7.7: same-name videos always keep the bigger one -- the former
        # 'abort' policy (stop the whole sync over one duplicate) is retired.
        base_override=(getattr(self.cfg,'strm_base_url','') or '').strip()
        async def sign(path):
            # 2.3.1: with a user-set strm直连地址 a RELATIVE signed link is enough,
            # so the host's「外部访问地址」is not needed at all.
            try:
                signed=await self.routes.sign(self.ctx,path,absolute=not base_override)
            except ValueError:
                raise DriveError('MovieClaw 未配置外部访问地址（设置 → 应用），无法签发完整播放地址。'
                    '修复任选其一：① 在 MovieClaw「设置 → 应用」填写外部访问地址（如 http://IP:端口）；'
                    '② 在本插件「STRM配置 → strm直连地址」填写外网可达地址后重试同步') from None
            return rebase_url(signed,base_override)

        # 2.7.0 trusted fast path. A STRM's bytes are a pure function of
        # (signed /play/<fid> URL, link format). If the signer still produces
        # the same URL for a probe path as it did on the last CLEAN run, and a
        # file's 115 identity (id, size, sha1, pickcode, mtime) and target path
        # are unchanged, its previous record is still exactly right -- no
        # per-file sign RPC, no intent read/write, no play-record rewrite.
        # A run that did not finish clears `clean`, so the next run takes the
        # full verify path everywhere (that is what heals a crashed run).
        meta_key=key+':meta'
        url_sig=hashlib.sha256(json.dumps([await sign('/play/__p115_probe__'),link_format,exist_policy]).encode()).hexdigest()
        meta=await self.store.get(meta_key,default={}) or {}
        trust=isinstance(meta,dict) and meta.get('clean') is True and meta.get('url_sig')==url_sig and bool(previous)
        # 2.7.4: files written by an earlier run with the SAME signing recipe
        # (even one that was stopped before saving any progress) can be
        # adopted from their content alone -- no per-file sign call.
        adopt=isinstance(meta,dict) and meta.get('url_sig')==url_sig
        if not dry_run and isinstance(meta,dict) and meta.get('partial') and previous:
            # 2.7.10: the last run did not finish (stopped, failed or the plugin
            # was killed): records saved as progress may be newer than the last
            # play-index flush. Re-derive every play record from the manifest
            # so nothing written before the interruption is left unplayable.
            self.rebuild_play_records(mapping,previous)
        if not dry_run:await self.store.set(meta_key,{'clean':False,'url_sig':url_sig})

        # Local existence through one listdir per directory instead of one
        # exists() RPC per file. Falls back to exists() if the host lacks it.
        # 2.7.3: a directory's contents are learned from its parent first --
        # one listdir of the STRM root tells which (movie) folders do not
        # exist yet, so a first sync pays no listdir for them at all, and a
        # folder known to exist needs no makedirs. Directory -> set of names,
        # or None when the directory is known to be absent. In-flight lookups
        # are shared between the parallel writers.
        listing={};inflight={};listdir=getattr(self.files,'listdir',None)
        async def dir_names(d):
            nonlocal listdir
            if d in listing:return listing[d]
            if d in inflight:return await asyncio.shield(inflight[d])
            async def load():
                parent=os.path.dirname(d)
                if d!=base and d.startswith(base+os.sep) and parent!=d:
                    pn=await dir_names(parent)
                    if pn is None or os.path.basename(d) not in pn:return None
                try:return set(await listdir(d))
                except (FileNotFoundError,NotADirectoryError):return None
            fut=asyncio.ensure_future(load());inflight[d]=fut
            try:names=await asyncio.shield(fut)
            finally:inflight.pop(d,None)
            listing.setdefault(d,names)
            if listing[d] is not None:self._dirs.add(d)  # exists: no makedirs needed
            return listing[d]
        async def exists(path):
            nonlocal listdir
            if listdir is None:return await self.files.exists(path)
            directory,name=os.path.split(path)
            try:names=await dir_names(directory)
            except (FileNotFoundError,NotADirectoryError):names=None
            except Exception:
                listdir=None;return await self.files.exists(path)
            return names is not None and name in names
        def dir_known_absent(path):
            return listdir is not None and listing.get(os.path.dirname(path),'?') is None
        def remember(path):
            # Record a write: the file, and every directory makedirs created.
            node=path
            while True:
                parent=os.path.dirname(node)
                if parent==node:break
                names=listing.get(parent)
                if names is None:
                    if parent in listing:listing[parent]=set()  # was absent, now created
                    else:break
                    names=listing[parent]
                names.add(os.path.basename(node))
                if parent==base or not parent.startswith(base):break
                node=parent

        winners={};current={};conflicts={};bad_entries=[];entries=0
        deferred_sidecars={};changed=[];foreign=[]
        sidecar_enabled=bool(getattr(self.cfg,'sidecar_sync',False))

        # Checkpoint resume (2.6.x) only matters for the legacy per-directory
        # walk; the 2.7.0 fast scan lists the whole tree in a few dozen requests,
        # so it neither skips nor records directories.
        strategy=getattr(self.cfg,'scan_strategy','fast')
        use_scan=strategy!='legacy' and hasattr(self.drive,'scan')
        fast_walk=False
        ckpt_key='checkpoint:'+scope_key(mapping)
        done=await self.load_checkpoint(ckpt_key) if not (dry_run or use_scan) else {}
        done=dict(done or {})
        resume_skip=set(done) if mode=='full' else set()
        flushed_done=set(done)
        dir_pending={};parent_of={}

        async def mark_done(cid):
            if fast_walk:return
            node=cid;dirty=False
            while node is not None and node not in done:
                done[node]='1';dirty=True
                parent=parent_of.get(node)
                if parent:
                    dir_pending[parent]=dir_pending.get(parent,0)-1
                    if dir_pending.get(parent,0)>0:break
                node=parent
            if dirty and not dry_run:await self.append_checkpoint(ckpt_key,done,flushed_done)

        core=lambda r:{k:v for k,v in (r or {}).items() if k not in ('sidecars','cloud')}
        # 2.7.4 progress checkpoints. Until 2.7.3 the manifest was saved only
        # when a run completed, so a stopped/failed run left every written STRM
        # unrecorded and the next run re-verified all of them from scratch.
        # Now records that changed are appended to the manifest every
        # PROGRESS_SAVE_FILES files / PROGRESS_SAVE_SECONDS, and once more when
        # the run stops or fails -- the next run takes the trusted fast path
        # for everything already done.
        dirty_rels=set();progress={'saved':0,'at':time.monotonic()};progress_lock=asyncio.Lock()
        async def save_progress():
            if dry_run:return 0
            async with progress_lock:
                if not dirty_rels:return 0
                rels=list(dirty_rels)
                delta={}
                for rel in rels:
                    rec=current.get(rel)
                    if rec is None:continue
                    prior=previous.get(rel) or {}
                    rec=dict(rec)
                    if not rec.get('sidecars') and prior.get('sidecars'):rec['sidecars']=prior['sidecars']
                    if 'cloud' not in rec and prior.get('cloud'):rec['cloud']=prior['cloud']
                    delta[rel]=rec
                total=await self.append_manifest(key,delta)
                dirty_rels.difference_update(rels)
                progress['saved']+=len(delta);progress['at']=time.monotonic()
                if not progress.get('meta'):
                    # Once per run: the manifest now matches the disk for every
                    # record it holds (partial = a later run rebuilds play records).
                    await self.store.set(meta_key,{'clean':True,'url_sig':url_sig,'at':int(time.time()),'partial':True})
                    progress['meta']=True
                if total and total>self.APPEND_COMPACT:
                    merged=dict(previous);merged.update({r:current[r] for r in current})
                    await self.save_manifest(key,merged)
                return len(delta)
        async def maybe_save_progress():
            now=time.monotonic()
            if (not dry_run and (self.play_index.pending or self.play_index.dropped) and not self.play_index.lock.locked()
                    and now-progress.get('play_at',0)>=self.PLAY_FLUSH_SECONDS):
                progress['play_at']=now;await self.play_index.flush()
            if not dirty_rels or progress_lock.locked():return
            if len(dirty_rels)>=self.PROGRESS_SAVE_FILES or now-progress['at']>=self.PROGRESS_SAVE_SECONDS:
                await save_progress()
        async def apply(item,relative,source):
            if self.cancelled:raise asyncio.CancelledError()
            path=os.path.join(base,*safe_relative(relative).parts)
            fid=hashlib.sha256((scope_key(mapping)+':'+item['id']).encode()).hexdigest()[:24]
            fingerprint=[item['size'],item['sha1'],item['pickcode'],item['mtime']]
            prior=previous.get(relative)
            replaced=current.get(relative)
            needs=False;old=prior;skipped=False
            if (trust and prior and prior.get('id')==item['id'] and prior.get('path')==path
                    and prior.get('fingerprint')==fingerprint and prior.get('url_hash')
                    and (replaced is None or replaced['id']==item['id']) and await exists(path)):
                # Trusted fast path: identity, path and URL recipe unchanged.
                # 2.7.6: reuse the previous record object (copied only if it
                # must change below) -- a 500k-file library no longer holds
                # two full copies of the manifest in memory.
                record=prior;present=True
                if not fast:
                    # Normal mode still reads every file back (the heal pass),
                    # but compares against the recorded hash -- no signing.
                    raw=await read_small(self.files,path)
                    if hashlib.sha256(raw).hexdigest()!=prior['url_hash']:
                        if strm_is_ours(raw,fid,b''):needs=True  # empty/torn/our own link: heal
                        else:skipped=True
                if needs:
                    record=dict(prior)
                    expected=((await sign('/play/'+fid))+('?/'+quote(item['name'],safe='') if link_format=='pick_code_name' else '')+'\n').encode()
                    record['url_hash']=hashlib.sha256(expected).hexdigest()
            else:
                # sign and the existence check run concurrently (2.7.2); for a
                # directory known not to exist yet, makedirs runs alongside the
                # sign call instead (2.7.3).
                present=await exists(path)
                if present and adopt and prior is None and exist_policy=='overwrite':
                    raw=await read_small(self.files,path)
                    named=b'?/' in raw
                    if (len(raw)<8192 and raw.endswith(b'\n') and raw.count(b'\n')==1 and ('/play/'+fid+'?').encode() in raw
                            and named==(link_format=='pick_code_name')
                            and (not named or raw.rstrip(b'\n').endswith(('?/'+quote(item['name'],safe='')).encode()))):
                        record={'id':item['id'],'path':path,'url_hash':hashlib.sha256(raw).hexdigest(),'fingerprint':fingerprint,'sidecars':{}}
                        if not dry_run:self.play_index.put(fid,{'pickcode':item['pickcode'],'name':item['name'][:120]})
                        current[relative]=record;dirty_rels.add(relative)
                        deferred_sidecars[relative]=(record,prior,source)
                        await self.report(unchanged=self.status['unchanged']+1)
                        p=item.get('parent_cid')
                        if p is not None:
                            dir_pending[p]=dir_pending.get(p,0)-1
                            if dir_pending.get(p,0)<=0:await mark_done(p)
                        if not dry_run and self.play_index.due():await self.play_index.flush()
                        await maybe_save_progress()
                        return
                if not present and not dry_run and dir_known_absent(path) and os.path.dirname(path) not in self._dirs:
                    d=os.path.dirname(path)
                    url,_=await asyncio.gather(sign('/play/'+fid),self.files.makedirs(d))
                    self._dirs.add(d);remember(os.path.join(d,'.'))
                else:
                    url=await sign('/play/'+fid)
                if link_format=='pick_code_name':url+='?/'+quote(item['name'],safe='')
                expected=(url+'\n').encode()
                record={'id':item['id'],'path':path,'url_hash':hashlib.sha256(expected).hexdigest(),'fingerprint':fingerprint,'sidecars':{}}
                old=prior
                raw=None
                same=core(old)==core(record)
                # Fast mode trusts the manifest for unchanged files and skips
                # re-reading them; normal mode re-reads and verifies.
                if present and not (fast and same):
                    raw=await read_small(self.files,path)
                    if not strm_is_ours(raw,fid,expected,prior,path):skipped=True
                if not skipped:
                    needs=not same or not present or (raw is not None and raw!=expected)
                    if present and exist_policy=='skip':
                        # "直接跳过": existing files stay untouched; align the manifest
                        # so switching back to 覆盖生成 heals the content next run.
                        needs=False
                        if raw is not None:record['url_hash']=hashlib.sha256(raw).hexdigest()
                    if not dry_run:
                        if replaced is not None and replaced['id']!=item['id']:
                            self.play_index.drop(hashlib.sha256((scope_key(mapping)+':'+replaced['id']).encode()).hexdigest()[:24])
                        if not (same and prior is not None):
                            # In memory first (the /play route reads it there);
                            # persisted in shards, batched.
                            self.play_index.put(fid,{'pickcode':item['pickcode'],'name':item['name'][:120]})
                            if self.play_index.due():await self.play_index.flush()
            if skipped:
                # 2.7.0: a file that is not ours (or was hand-edited) is left
                # alone and reported -- it no longer aborts the whole library.
                foreign.append(relative)
                await self.report(skipped=self.status.get('skipped',0)+1)
                if prior is not None:current[relative]=prior
            else:
                if needs and not dry_run:
                    await self.mark_scan_pending(key)
                    await atomic_write(self.files,path,expected,self._dirs,direct=not present);remember(path);changed.append(path)
                current[relative]=record
                if core(record)!=core(prior):dirty_rels.add(relative)
                deferred_sidecars[relative]=(record,old,source)
                field='written' if needs else 'unchanged'
                await self.report(**{field:self.status[field]+1})
                await maybe_save_progress()
            p=item.get('parent_cid')
            if p is not None:
                dir_pending[p]=dir_pending.get(p,0)-1
                if dir_pending.get(p,0)<=0:await mark_done(p)
            now=time.monotonic()
            if now-self._last_logged>=self.PROGRESS_LOG_SECONDS:
                self._last_logged=now
                try:
                    await log_event(self.store,'info','同步','进度：已处理 %d 个文件（写入 %d · 未变化 %d），当前 %s'%(
                        self.status.get('written',0)+self.status.get('unchanged',0),self.status.get('written',0),self.status.get('unchanged',0),relative))
                except Exception:pass

        # Walk/scan (network, paced by the API interval) and the STRM writers
        # (host IPC) are concurrent tasks joined by a bounded queue.
        inexact=False  # 115's count moved during a fast listing: no deletions this run
        def wanted_name(name):
            suffix=PurePosixPath(name).suffix.lower()
            return suffix in exts or (sidecar_enabled and suffix in SIDECAR)
        queues=[asyncio.Queue(maxsize=self.SHARD_QUEUE) for _ in range(self.WRITERS)]
        sentinel=object();failure=[];producer_done=asyncio.Event()
        async def enqueue(it,rel,src):
            await queues[zlib.crc32(rel.encode('utf-8'))%len(queues)].put((it,rel,src))
        # ---- 2.7.5 sidecars (images / NFO / subtitles) -------------------
        # Rule: every sidecar-type file in a folder that holds a video, in any
        # folder above one (show / season / collection level), or in an
        # extrafanart / extrathumbs folder of such a folder. Files are
        # downloaded WHILE the scan runs (links batch-signed ~100 per 115
        # request) and tracked as standalone manifest records SIDE+<path>,
        # so a stopped run keeps them too.
        legacy_hash={};legacy_cloud={}
        for rel_,rec_ in previous.items():
            if rel_.startswith(SIDE) or not isinstance(rec_,dict):continue
            for r,h in (rec_.get('sidecars') or {}).items():legacy_hash.setdefault(r,h)
            for r,c in (rec_.get('cloud') or {}).items():
                if isinstance(c,dict):legacy_cloud.setdefault(r,c)
        qualified=set();held={};side_queue=asyncio.Queue();side_routed=set()
        def side_anchor(path):
            d=PurePosixPath(path).parent
            return str(d.parent) if d.name.lower() in EXTRA_DIRS else str(d)
        def route_sidecar(item):
            if item['id'] in side_routed:return
            anchor=side_anchor(item['path'])
            if anchor in qualified:side_routed.add(item['id']);side_queue.put_nowait(item)
            else:held.setdefault(anchor,[]).append(item)
        def qualify(d):
            p=PurePosixPath(d)
            for x in (p,*p.parents):
                k=str(x)
                if k in qualified:break
                qualified.add(k)
                for item in held.pop(k,[]):
                    if item['id'] not in side_routed:side_routed.add(item['id']);side_queue.put_nowait(item)
        side_stats={'skipped_user':0,'failed':0}
        async def sync_side(item):
            if self.cancelled:raise asyncio.CancelledError()
            try:rel=safe_relative(item['path']).as_posix()
            except DriveError:return
            target=os.path.join(base,*PurePosixPath(rel).parts)
            k=SIDE+rel
            prev=previous.get(k) if isinstance(previous.get(k),dict) else {}
            prev_hash=prev.get('hash') or legacy_hash.get(rel)
            prev_cloud=prev.get('cloud') or legacy_cloud.get(rel)
            cloud={'sha1':item['sha1'],'size':item['size'],'mtime':item['mtime']}
            same_cloud=bool(item['sha1']) and isinstance(prev_cloud,dict) and prev_cloud.get('sha1')==item['sha1']
            present=await exists(target)
            def keep(h):
                rec={'kind':'side','path':target,'hash':h,'cloud':cloud}
                old=previous.get(k)
                if rec==old:current[k]=old  # share the object: half the memory
                else:current[k]=rec;dirty_rels.add(k)
            if present and trust and prev_hash and same_cloud:
                keep(prev_hash);return  # trusted: no read, no download
            existing=None
            if present:
                try:raw=await read_bytes(self.files,target,SIDECAR_MAX+1)
                except OSError:return
                if len(raw)>SIDECAR_MAX:return
                existing=hashlib.sha256(raw).hexdigest()
                if existing!=prev_hash:
                    side_stats['skipped_user']+=1;return  # not ours: never touch
                if same_cloud:keep(existing);return
            try:data=await self.fetch_cloud_sidecar(item['pickcode'])
            except asyncio.CancelledError:raise
            except Exception:
                side_stats['failed']+=1;return  # transient: retried next run
            digest=hashlib.sha256(data).hexdigest()
            if existing!=digest:
                if not dry_run:
                    await self.mark_scan_pending(key)
                    await atomic_write(self.files,target,data,self._dirs,direct=not present);remember(target)
                await self.report(sidecars=self.status.get('sidecars',0)+1)
            keep(digest)
            await maybe_save_progress()
        async def side_worker():
            prime=getattr(self.drive,'prime_links',None)
            gate=asyncio.Semaphore(8)  # gentle on the CDN
            async def one(it):
                async with gate:await sync_side(it)
            done_=False
            while not done_:
                try:first=await asyncio.wait_for(side_queue.get(),timeout=0.5)
                except asyncio.TimeoutError:
                    if producer_done.is_set() and side_queue.empty():break
                    continue
                if first is sentinel:break
                batch=[first]
                while len(batch)<100 and not side_queue.empty():
                    x=side_queue.get_nowait()
                    if x is sentinel:done_=True;break
                    batch.append(x)
                if prime is not None:
                    wanted=[]
                    for it in batch:
                        try:rel=safe_relative(it['path']).as_posix()
                        except DriveError:continue
                        pc=(previous.get(SIDE+rel) or {}).get('cloud') or legacy_cloud.get(rel)
                        if not (isinstance(pc,dict) and it['sha1'] and pc.get('sha1')==it['sha1']):wanted.append(it['pickcode'])
                    if wanted:
                        try:await prime(wanted,lambda:self.cancelled)
                        except asyncio.CancelledError:raise
                        except Exception:pass  # per-file signing still works
                for r in await asyncio.gather(*(one(it) for it in batch),return_exceptions=True):
                    if isinstance(r,BaseException):raise r
        # -------------------------------------------------------------------

        async def produce():
            nonlocal entries,fast_walk,inexact
            dir_buf={};streaming=False
            seen_base=self.status.get('seen',0)
            async def events():
                nonlocal fast_walk
                if use_scan:
                    started=False
                    try:
                        async for item in self.drive.scan(mapping.cid,lambda:self.cancelled,bad_sink=bad_entries,keep=wanted_name):
                            if 'path' in item or item.get('__end_dir__'):started=True
                            yield item
                        return
                    except DriveError as e:
                        if started or isinstance(e,(ThrottleError,LimitError)) or '990001' in str(e) or '已等待重试' in str(e):raise
                        # The flat listing itself was refused before anything
                        # was produced: fall back to the legacy walk once.
                        fast_walk=False
                        try:await log_event(self.store,'warn','同步','快速扫描不可用（%s），本次改用逐目录扫描'%str(e)[:160])
                        except Exception:pass
                async for item in self.drive.walk(mapping.cid,lambda:self.cancelled,bad_sink=bad_entries,skip_dirs=resume_skip):
                    yield item
            try:
                async for item in events():
                    if item.get('__mode__'):
                        streaming=item['__mode__']=='stream'
                        fast_walk=item['__mode__'] in ('fast','stream');continue
                    if item.get('__inexact__'):
                        inexact=True;continue
                    if item.get('__listing_done__'):continue
                    if '__progress__' in item:
                        await self.report(seen=seen_base+item['__progress__']);continue
                    if failure:raise failure[0]  # a writer already failed: stop listing
                    if item.get('__end_dir__'):
                        cid=item['cid'];parent=item.get('parent_cid')
                        parent_of[cid]=parent
                        if parent:dir_pending[parent]=dir_pending.get(parent,0)+1
                        # Settle same-name conflicts for this directory BEFORE any
                        # enqueue (first a pure decision, then enqueue winners).
                        buf=dir_buf.pop(cid,[])
                        local={}
                        for it,rel,src in buf:
                            cur=local.get(rel)
                            if cur is None:local[rel]=(it,src);continue
                            cit,_=cur
                            if cit['id']==it['id'] and cit['pickcode']==it['pickcode']:continue
                            if conflict_rank(it)>conflict_rank(cit):
                                conflicts.setdefault(rel,[]).append(cit)
                                local[rel]=(it,src)
                            else:conflicts.setdefault(rel,[]).append(it)
                        for rel,(it,src) in local.items():
                            winners[rel]=it
                            dir_pending[cid]=dir_pending.get(cid,0)+1
                            await enqueue(it,rel,src)
                        if dir_pending.get(cid,0)<=0:await mark_done(cid)
                        continue
                    entries+=1
                    if not fast_walk and entries%25==0:await self.report(seen=self.status['seen']+25)
                    suffix=PurePosixPath(item['path']).suffix.lower()
                    if suffix not in exts:
                        if sidecar_enabled and suffix in SIDECAR and item['pickcode'] and 0<item['size']<=SIDECAR_MAX:
                            route_sidecar(item)
                        continue
                    if min_bytes and item['size']<min_bytes:continue
                    if not item['pickcode']:
                        bad_entries.append({'index':0,'reason':'视频缺少提取码','dir':str(PurePosixPath(item['path']).parent),'detail':'n=%r fid=%s'%(item['name'][:60],item['id'])})
                        continue
                    if sidecar_enabled:qualify(str(PurePosixPath(item['path']).parent))
                    source=safe_relative(item['path'])
                    relative=(source.as_posix()+'.strm') if keep_suffix else source.with_suffix('.strm').as_posix()
                    if not streaming:
                        dir_buf.setdefault(item['parent_cid'],[]).append((item,relative,source));continue
                    # 2.7.2 streaming: settle per output path as candidates
                    # arrive and hand the winner to its writer immediately. A
                    # later, bigger same-name candidate is enqueued to the SAME
                    # writer and overwrites; the final winner is the same total
                    # order (conflict_rank) as the per-directory settlement.
                    cur=winners.get(relative)
                    if cur is not None:
                        if cur['id']==item['id'] and cur['pickcode']==item['pickcode']:continue
                        if conflict_rank(item)<=conflict_rank(cur):
                            conflicts.setdefault(relative,[]).append(item);continue
                        conflicts.setdefault(relative,[]).append(cur)
                    winners[relative]=item
                    await enqueue(item,relative,source)
            except asyncio.CancelledError:
                failure.append(asyncio.CancelledError());raise
            except BaseException as e:
                failure.append(e)
            finally:
                producer_done.set()
                for q in queues:
                    try:q.put_nowait(sentinel)
                    except asyncio.QueueFull:pass
                side_queue.put_nowait(sentinel)

        async def consume(q):
            while True:
                try:
                    msg=await asyncio.wait_for(q.get(),timeout=0.5)
                except asyncio.CancelledError:raise
                except asyncio.TimeoutError:
                    if producer_done.is_set() and q.empty():break
                    continue
                if msg is sentinel:break
                try:await apply(*msg)
                except asyncio.CancelledError:raise
                except BaseException as e:failure.append(e);raise

        async def keep_progress(reason):
            # Stop / failure: persist everything written so far (bounded -- on
            # plugin shutdown the host may stop answering).
            try:
                n=await asyncio.wait_for(save_progress(),60)
                if progress['saved']:
                    await asyncio.wait_for(log_event(self.store,'info','同步','%s：已保存进度（本轮共记录 %d 个已完成文件），下次同步会直接跳过它们，不必从头再来'%(reason,progress['saved'])),5)
                return n
            except BaseException:return 0
        producer=asyncio.create_task(produce())
        consumers=[asyncio.create_task(consume(q)) for q in queues]
        async def side_task():
            try:await side_worker()
            except asyncio.CancelledError:raise
            except BaseException as e:failure.append(e);raise
        all_tasks={producer,asyncio.create_task(side_task())}|set(consumers)
        try:
            try:
                finished,alive=await asyncio.wait(all_tasks,return_when=asyncio.FIRST_COMPLETED)
                while alive and not failure:
                    finished,alive=await asyncio.wait(alive,return_when=asyncio.FIRST_COMPLETED)
                if alive:await asyncio.wait(alive,timeout=10)  # grace: let the peer notice
            finally:
                for task in all_tasks:
                    if not task.done():task.cancel()
                await asyncio.gather(*all_tasks,return_exceptions=True)
        except asyncio.CancelledError:
            await keep_progress('同步已停止');raise
        errors=[t.exception() for t in all_tasks if t.done() and not t.cancelled() and t.exception() is not None]
        # Persist whatever directories finished BEFORE re-raising (legacy walk).
        if not dry_run and done and not fast_walk:
            await best_effort(self.save_checkpoint(ckpt_key,done))
        if failure or errors:
            first=failure[0] if failure else errors[0]
            await keep_progress('同步已停止' if isinstance(first,asyncio.CancelledError) else '同步中断')
            raise first
        if fast_walk:await self.report(flush=True,seen=self.status['seen'])
        else:await self.report(flush=True,seen=self.status['seen']+entries%25)
        if bad_entries:
            await self.report(skipped=self.status.get('skipped',0)+len(bad_entries))
            try:
                for entry in bad_entries[:10]:
                    await log_event(self.store,'warn','同步','已跳过网盘异常条目（位于 %s）：%s；条目：%s。该条目不会生成 STRM，其余文件不受影响；建议到 115 网盘检查或重命名此条目'%(
                        entry.get('dir','/'),entry.get('reason',''),entry.get('detail','')))
                if len(bad_entries)>10:
                    await log_event(self.store,'warn','同步','另有 %d 个异常条目已跳过（均已计数，见同步统计）'%(len(bad_entries)-10))
            except Exception:pass
        if foreign:
            try:
                await log_event(self.store,'warn','同步','%d 个 STRM 不属于插件或已被手工修改，未覆盖（其余文件照常同步）：%s%s'%(
                    len(foreign),'、'.join(foreign[:5]),' 等' if len(foreign)>5 else ''))
            except Exception:pass
        conflict_detail=[]
        if conflicts:
            await self.report(conflicts=len(conflicts))
            try:
                for relative,losers in conflicts.items():
                    winner=winners[relative]
                    conflict_detail.append(relative+'：保留 '+winner['path']+'（'+human_size(winner['size'])+'），跳过 '
                        +'、'.join(l['path']+'（'+human_size(l['size'])+'）' for l in losers))
                # 2.7.7: no per-conflict run-log lines (they flooded the
                # 100-entry log on big libraries); the count shows in the sync
                # result and the first 10 details stay in the sync history.
            except Exception:pass
            conflict_detail=conflict_detail[:10]
        if side_stats['skipped_user']:
            try:await log_event(self.store,'info','同步','%d 个本地已有的图片/NFO/字幕不是插件下载的（或已被修改），保持不动'%side_stats['skipped_user'])
            except Exception:pass
        if side_stats['failed']:
            try:await log_event(self.store,'warn','同步','%d 个图片/NFO/字幕下载失败（115 CDN 超时或拒绝），已跳过，下次同步自动重试；STRM 不受影响'%side_stats['failed'])
            except Exception:pass
        for relative,old in previous.items():
            if relative in current:continue
            if relative.startswith(SIDE):
                # Standalone sidecar no longer in the cloud (or no longer next
                # to a video): delete only what we wrote and nobody changed.
                path=(old or {}).get('path','') if isinstance(old,dict) else ''
                if sidecar_enabled and self.cfg.cleanup_missing and not inexact and path.startswith(base+os.sep):
                    if await self.files.exists(path):
                        raw=await read_bytes(self.files,path,SIDECAR_MAX+1)
                        if hashlib.sha256(raw).hexdigest()==old.get('hash'):
                            if not dry_run:
                                await self.mark_scan_pending(key)
                                await self.files.remove(path)
                            await self.report(deleted=self.status['deleted']+1)
                else:current[relative]=old
                continue
            if self.cfg.cleanup_missing and not inexact:
                path=old.get('path','')
                if os.path.dirname(path)==base or path.startswith(base+os.sep):
                    modified=False
                    if await self.files.exists(path):
                        raw=await read_small(self.files,path)
                        if hashlib.sha256(raw).hexdigest()==old.get('url_hash'):
                            if not dry_run:
                                await self.mark_scan_pending(key)
                                await self.files.remove(path)
                            await self.report(deleted=self.status['deleted']+1)
                        else:modified=True
                    if modified:
                        current[relative]=old
                    elif not dry_run:
                        for rel,sh in (old.get('sidecars') or {}).items():
                            if SIDE+rel in current:continue  # now tracked on its own
                            try:side_path=os.path.join(base,*safe_relative(rel).parts)
                            except DriveError:continue
                            if os.path.dirname(side_path)!=base and not side_path.startswith(base+os.sep):continue
                            if await self.files.exists(side_path):
                                raw=await read_bytes(self.files,side_path,SIDECAR_MAX+1)
                                if len(raw)<=SIDECAR_MAX and hashlib.sha256(raw).hexdigest()==sh:
                                    await self.files.remove(side_path)
                                    await self.report(deleted=self.status['deleted']+1)
            else:current[relative]=old
        if not dry_run:
            # Play records must be durable before the manifest says "done".
            await self.play_index.flush()
            # 2.7.0: an unchanged library no longer rewrites (and deletes) every
            # manifest chunk on every run.
            if current!=previous or not previous:await self.save_manifest(key,current)
            if done and not fast_walk:
                await self.save_checkpoint(ckpt_key,done)
            await self.store.set(meta_key,{'clean':True,'url_sig':url_sig,'at':int(time.time())})
            # 2.7.13: incremental syncs ask MovieClaw to rescan the mapping when
            # they changed something (or a previous incremental's request
            # failed); full syncs never do. A refused/failed request is not a
            # sync failure: retry once, log the host's reason, keep the flag so
            # the next incremental resends it, and continue.
            if mode=='incremental' and await self.store.get(key+':scan-pending',default=False):
                scan_error=None
                for attempt in range(2):
                    try:
                        await asyncio.wait_for(self.ops.call('library.scan.start',
                            {'library_id':mapping.library_id,'body':{'paths':[base]}}),getattr(self,'scan_rpc_timeout',120))
                        scan_error=None;break
                    except asyncio.TimeoutError:
                        scan_error='宿主响应超时（媒体库可能正忙）';break
                    except asyncio.CancelledError:raise
                    except Exception as e:
                        scan_error=(str(e).strip() or type(e).__name__)[:200]
                        if attempt==0:await asyncio.sleep(getattr(self,'scan_retry_delay',5))
                if scan_error is None:
                    await self.store.delete(key+':scan-pending')
                    getattr(self,'_pending_marked',set()).discard(key)
                    await self.report(scan_pending=False)
                else:
                    await self.report(scan_pending=True)
                    try:
                        await log_event(self.store,'warn','同步','增量同步已完成，但通知 MovieClaw 扫描媒体库失败（%s）。下次增量同步会自动重发；也可以在 MovieClaw 里手动扫描：%s'%(scan_error,base))
                    except Exception:pass
        await self.report(flush=True,mappings=self.status.get('mappings',[])+[{
            'name':(str(getattr(mapping,'subdirectory','') or '?')+'（CID '+str(mapping.cid)+'）'),
            **{k:self.status.get(k,0)-before[k] for k in counters},
            'conflict_detail':conflict_detail}])
