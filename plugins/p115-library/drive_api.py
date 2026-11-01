"""115 Cookie API subset, using p115client 0.0.9.7.2's documented wire contracts."""
import asyncio
import json
import random
import re
import time
from urllib.parse import urlsplit, parse_qs
import httpx
from movieclaw_sdk import net
from p115_rsa import rsa_encrypt, rsa_decrypt
from p115_ecdh import ecdh_aes_decrypt
import p115_m115 as m115

class DriveError(RuntimeError): pass


class LimitError(DriveError):
    """The scope is larger than「单次扫描最大条目数」. Same for every scan
    strategy, so it must never trigger a fallback to the (slower) legacy walk."""


class ThrottleError(DriveError):
    """115 asked us to SLOW DOWN rather than signalling a broken request.

    Worth waiting for, never worth failing a whole sync over: enumerating a
    big library costs thousands of paged requests, so before 2.4.0 a single
    "操作太频繁" answer discarded hours of progress and every STRM file the
    walk had already produced had to be re-walked to be finished.

    Distinct from DriveError so the retry loop can tell "wait and retry"
    (throttle / busy / transient network / a throttle interstitial served
    instead of JSON) from "our request or our credential is wrong", which
    must fail immediately with its precise reason.
    """


# errnos 115 answers when throttled or busy (meanings follow p115client
# 0.0.9.7.2's documented errno cases, same source as ERRNO_HINTS) -- the
# honest reaction is to wait, not to abort.
RETRYABLE_ERRNO={590075,990003,990005}
# HTTP statuses that mean "try again later" (429/408 are explicit; the 5xx
# set is what 115's CDN/edge actually serves while shedding load).
RETRYABLE_STATUS={408,429,500,502,503,504}
COOLDOWN_CAP=600.


def safe_url(url):
    if not isinstance(url,str): raise DriveError('115 返回了无效下载地址')
    p=urlsplit(url)
    if p.scheme not in ('http','https') or not p.hostname or p.username or p.password or '\r' in url or '\n' in url:
        raise DriveError('115 返回了无效下载地址')
    return url


def rebase_url(url, base):
    """Swap the origin of a signed absolute URL to a user-provided external origin.
    Path and signature stay intact, so host-side verification still passes as long
    as the external address reverse-proxies back to the same MovieClaw instance."""
    if not base: return url
    u=urlsplit(url);b=urlsplit(base)
    if not (b.scheme in ('http','https') and b.netloc): raise DriveError('STRM 直连地址格式无效')
    return b.scheme+'://'+b.netloc+u.path+(('?'+u.query) if u.query else '')


def normalize(raw):
    """Validate and flatten one 115 listing entry.

    2.2.1: single-entry violations raise DriveError with a PRECISE reason --
    list() turns those into skip-and-log records instead of aborting the whole
    sync (previously one weird cloud entry killed the entire full sync at 0s
    with an unhelpful generic message). Directory detection now matches the
    production contract (115driver File.From): an entry is a file iff its fid
    is NON-EMPTY -- key presence is not the test.
    """
    if not isinstance(raw,dict):raise DriveError('条目不是对象（%s）'%type(raw).__name__)
    fid=raw.get('fid')
    if isinstance(fid,bool):raise DriveError('fid 字段类型异常（bool）')
    if isinstance(fid,(int,float)):fid=str(fid)
    folder=not (isinstance(fid,str) and fid.strip())
    try:
        item_id=str(raw['cid']) if folder else fid
        if not item_id.isdecimal():raise ValueError('ID 非数字（%r）'%item_id[:40])
        name=raw['n']
        if not isinstance(name,str) or not name:raise ValueError('名称为空或不是字符串')
        if name in ('.','..'):raise ValueError('名称为 . 或 ..')
        if any(c in name for c in ('/','\\','\x00')):raise ValueError('名称含 /、\\ 或空字节')
        try:size=int(raw.get('s',0))
        except (TypeError,ValueError):raise ValueError('大小字段无法解析（%r）'%repr(raw.get('s'))[:40]) from None
        return {'id':item_id,'name':name,'is_dir':folder,'size':size,
                'pickcode':str(raw.get('pc') or ''),'mtime':str(raw.get('te') or raw.get('tu') or ''),'sha1':str(raw.get('sha') or '')}
    except KeyError as e:raise DriveError('缺少字段 %s'%e) from None
    except (TypeError,ValueError) as e:raise DriveError(str(e)[:120]) from None


def describe_bad_entry(raw):
    """One-line diagnostic for a skipped listing entry: WHICH fields it had and
    what they looked like, bounded and without dumping whole-entry payloads,
    so the run log points at the exact cloud item and the reason."""
    if not isinstance(raw,dict):return '非对象条目（%s）：%s'%(type(raw).__name__,repr(raw)[:100])
    def field(k):
        v=raw.get(k)
        return repr(v)[:60] if isinstance(v,(str,int,float)) else ('缺失' if v is None else type(v).__name__)
    keys=','.join(sorted(str(k) for k in raw))[:160]
    return 'n=%s fid=%s cid=%s pc=%s s=%s sha=%s；字段=[%s]'%(field('n'),field('fid'),field('cid'),field('pc'),field('s'),field('sha'),keys)


# Meanings follow p115client 0.0.9.7.2's documented errno cases.
ERRNO_HINTS={
 990001:'登陆已超时（Cookie 失效，或被同类型设备登录挤下线）——请重新扫码，并换一个未被其他设备占用的类型',
 990002:'请求参数错误',
 990003:'操作失败，请稍后再试',
 990005:'同类任务处理中，请稍后再试',
 590075:'操作太频繁（疑似风控），请在设置调大 API 请求间隔',
 800001:'目录不存在或已被删除',
 300104:'文件超过 200MB，暂不支持播放',
 320001:'安全密钥不正确',
}


def errno_hint(code):
    try:return ERRNO_HINTS.get(int(code))
    except (TypeError,ValueError):return None


class Drive:
    # 1.7.5: signed links are cached and reused for a short window. Players open
    # many connections when reading MKV indexes / subtitle tracks (each seek is
    # a fresh request to /play), and every fresh sign costs a 115 roundtrip
    # behind the global API-interval lock -- that serialized delay is exactly
    # why subtitle loading felt slow. A CDN URL accepts multiple Range requests
    # (the diagnostic proves this), so reuse is safe for the cache window.
    LINK_CACHE_MAX = 512
    # 2.7.0: pickcodes per batched downurl call (p115client download_url_app:
    # the chrome endpoint takes a comma-separated list, ~50 ms per extra code).
    SIGN_BATCH = 100

    def __init__(self,cfg):
        self.cfg=cfg;self.lock=asyncio.Lock();self.last=0.
        # 115 binds the credential to the UA that minted it, so the plugin keeps
        # this in sync with the stored 'qr-ua' (or the user's api_user_agent).
        self.ua='Mozilla/5.0'
        # Set once a (protocol, UA) signing pair is proven to work; later
        # link_signed() calls then cost a single request instead of re-walking
        # the strategy chain.
        self._ok=None
        # (pickcode, ua, allow_cookie_url) -> {url, ua, headers, proto, at}.
        # f=3 (Cookie-bound) and f=1 (UA-bound) links must never leak across
        # the allow_cookie_url dimension, hence the full key.
        self._links={}
        # Diagnostics for the runtime log: which protocol served the last
        # link_signed() call, from cache or a fresh sign, and how long it took.
        self.last_sign={'cached':False,'proto':'','elapsed':0.}
        # 2.4.0: the drive layer knows nothing about the sync engine, so the
        # throttle wait is announced through optional callbacks -- the engine
        # turns it into panel progress and a run-log line, and cancel_check
        # lets "停止同步" interrupt a multi-minute cooldown.
        self.on_wait=None;self.on_resume=None;self.cancel_check=None
        self.throttle_stats={'waits':0,'waited':0.,'last':''}
    async def sleep(self,seconds):
        """Cooperative wait that stays interruptible: 停止同步 landing on a
        multi-minute cooldown must not make the plugin look frozen."""
        end=time.monotonic()+max(0.,float(seconds))
        while True:
            rest=end-time.monotonic()
            if rest<=0:return
            if self.cancel_check and self.cancel_check():raise asyncio.CancelledError()
            await asyncio.sleep(min(0.5,rest))
    def cooldown_delay(self,attempt,base):
        """Exponential backoff with jitter: base, 2×base, 4×base ... capped.
        The ±10% jitter keeps repeated syncs from retrying in lockstep; the cap
        is applied AFTER jitter so the documented upper bound is a real bound."""
        raw=float(base)*(2**(max(1,attempt)-1))*(1.+random.uniform(-0.1,0.1))
        return max(1.,min(COOLDOWN_CAP,raw))
    async def notify_wait(self,delay,attempt,attempts,reason):
        self.throttle_stats={'waits':self.throttle_stats['waits']+1,'waited':round(self.throttle_stats['waited']+delay,1),'last':str(reason)[:200]}
        if self.on_wait is None:return
        try:await self.on_wait(delay,attempt,attempts,reason)
        except Exception:pass  # observability must never break the request layer
    async def notify_resume(self):
        if self.on_resume is None:return
        try:await self.on_resume()
        except Exception:pass
    def _client(self):
        """2.7.0: one pooled HTTP client per Drive instead of a fresh client
        (fresh TCP + TLS handshake to 115) for every single request. On a big
        walk that handshake alone was often slower than the API call itself."""
        client=getattr(self,'_http',None)
        if client is None or client.is_closed:
            client=httpx.AsyncClient(transport=net.http_transport('p115-library'),trust_env=False,follow_redirects=False)
            self._http=client
        return client
    async def request(self,method,url,**kwargs):
        """One logical 115 call: throttled answers are waited out, everything
        else fails fast with its own reason.

        A full walk issues thousands of paged requests, so hitting the rate
        limit is a matter of time, not of doing something wrong -- before
        2.4.0 one throttled page failed the entire sync and the next run had
        to re-walk everything it had already written.
        """
        attempts=max(0,int(getattr(self.cfg,'retry_max',5) or 0))
        base=max(1.,float(getattr(self.cfg,'retry_cooldown',30) or 30))
        attempt=0
        while True:
            try:return await self._once(method,url,**kwargs)
            except ThrottleError as e:
                if attempt>=attempts:
                    if not attempts:raise
                    raise DriveError(str(e)+'（已等待重试 %d 次仍未恢复；请把「核心配置 → API请求间隔」调大（如 3~5 秒）或减少同步目录后重跑）'%attempts) from None
                attempt+=1
                delay=self.cooldown_delay(attempt,base)
                await self.notify_wait(delay,attempt,attempts,str(e))
                await self.sleep(delay)
                await self.notify_resume()
    async def _once(self,method,url,**kwargs):
        raw=kwargs.pop('raw_content',False)
        secret=self.cfg.cookie.get_secret_value()
        if not secret: raise DriveError('请先在插件设置填写 115 Cookie')
        # One Cookie stays in API requests only. It is never included in redirects or logs.
        async with self.lock:
            delay=self.cfg.request_interval-(time.monotonic()-self.last)
            if delay>0: await self.sleep(delay)
            self.last=time.monotonic()
            try:
                client=self._client()
                r=await client.request(method,url,headers={'Cookie':secret,'User-Agent':kwargs.pop('ua',self.ua)},timeout=self.cfg.timeout,**kwargs)
                if r.status_code!=200:
                    # Explicit throttle / overloaded statuses are waited out
                    # instead of failing the run.
                    raise ThrottleError(f'115 接口 HTTP {r.status_code}') if r.status_code in RETRYABLE_STATUS \
                        else DriveError(f'115 接口 HTTP {r.status_code}')
                if raw:return r.content
                try:value=r.json()
                except Exception:
                    # A throttle interstitial is HTML or empty, not JSON:
                    # classify it as throttle instead of a format failure.
                    raise ThrottleError('115 接口响应不是 JSON（可能被限速页拦截）') from None
                if not isinstance(value,dict):raise ThrottleError('115 接口响应格式不合规')
                if value.get('state') not in (True,1):
                    code=value.get('errno',value.get('code','unknown'))
                    hint=errno_hint(code)
                    msg='115 接口失败，错误码 '+str(code)[:30]
                    if hint:msg+='：'+hint
                    throttled=False
                    try:throttled=int(code) in RETRYABLE_ERRNO
                    except (TypeError,ValueError):pass
                    raise ThrottleError(msg) if throttled else DriveError(msg)
                return value
            except DriveError:raise  # DriveError 与 ThrottleError 均原样上抛
            except Exception as e:raise ThrottleError('115 请求失败（'+type(e).__name__+'）') from None
    async def list(self,cid,offset=0,limit=None,recursive=False):
        """One page of a directory listing.

        recursive=True (2.7.0) is p115client's iter_files contract: ``cur=0`` +
        ``show_dir=0`` makes 115 return EVERY file of the whole subtree in one
        flat, paged listing (up to 1150 per page), each row carrying its parent
        directory id in ``cid``. A 5000-directory library costs ~N/1150 pages
        instead of >=5000 per-directory requests behind the API interval."""
        if recursive:
            params={'aid':1,'cid':cid,'offset':offset,'limit':min(1150,int(limit or self.cfg.page_size)),'cur':0,'show_dir':0,'count_folders':0,'o':'file_id','asc':1,'custom_order':2}
        else:
            params={'cid':cid,'offset':offset,'limit':int(limit or self.cfg.page_size),'cur':1,'show_dir':1,'count_folders':1,'o':'file_id','asc':1,'fc_mix':1}
        value=await self.request('GET','https://webapi.115.com/files',params=params)
        # The API falls back to root for invalid cid; reject that instead of syncing the wrong folder.
        actual=value.get('cid')
        path=value.get('path')
        if actual is None and isinstance(path,list) and path:actual=path[-1].get('cid')
        if actual is None or str(actual)!=str(cid):raise DriveError('115 返回的目录与请求不一致，同步已停止')
        rows=value.get('data')
        try: count=int(value['count'])
        except (KeyError,TypeError,ValueError):raise DriveError('115 缺少有效分页总数') from None
        if not isinstance(rows,list) or count<0:raise DriveError('115 分页响应不合规')
        breadcrumb=[]
        for node in value.get('path') or []:
            if not isinstance(node,dict):continue
            name=node.get('name');node_cid=node.get('cid')
            if isinstance(name,str) and name and node_cid is not None:breadcrumb.append({'cid':str(node_cid),'name':name})
        # 2.2.1: one malformed entry no longer aborts the sync -- skip it and
        # carry a precise reason for the run log. Only a page whose EVERY entry
        # fails still stops the sync: that means 115 changed the wire format
        # wholesale, and "successfully" syncing nothing would be worse.
        items=[];bad=[]
        for i,row in enumerate(rows):
            try:
                item=normalize(row)
                if recursive:
                    if item['is_dir']:continue  # defensive: show_dir=0 never sends dirs
                    parent=row.get('cid')
                    if isinstance(parent,bool) or parent is None or not str(parent).isdecimal():
                        raise DriveError('所在目录 ID 异常（%r）'%str(parent)[:40])
                    item['parent_cid']=str(parent)
                items.append(item)
            except DriveError as reason:bad.append({'index':offset+i,'reason':str(reason),'detail':describe_bad_entry(row)})
        if rows and not items:
            first=bad[0] if bad else {'index':0,'reason':'','detail':''}
            raise DriveError('115 文件数据格式不合规：该页 %d 条记录全部无法解析（首条原因：%s；条目详情：%s）。同步已停止——请把本行反馈给开发者'%(
                len(rows),first['reason'],first['detail']))
        return {'items':items,'bad':bad,'raw_len':len(rows),'count':count,'offset':offset,'cid':str(cid),'path':breadcrumb}
    async def dirs(self,cid):
        """Directories of one level plus the breadcrumb path, for the CID picker. Single request."""
        page=await self.list(cid,0)
        return {'items':[x for x in page['items'] if x['is_dir']],'path':page['path'],'cid':page['cid'],'count':page['count']}
    # ------------------------------------------------------------------
    # 2.7.0 fast enumeration
    # ------------------------------------------------------------------
    TREE_PAGE=5000
    async def dir_pickcode(self,cid):
        """Pickcode of a directory (``downfolders`` is keyed by pickcode).
        Empty string when 115 does not hand it out -- callers then fall back."""
        cid=str(cid)
        if cid=='0':return ''
        try:
            value=await self.request('GET','https://webapi.115.com/files/get_info',params={'file_id':cid})
        except DriveError:return ''
        data=value.get('data')
        node=data[0] if isinstance(data,list) and data else data
        if not isinstance(node,dict):return ''
        if str(node.get('file_id',node.get('cid',cid)))!=cid:return ''
        pc=node.get('pick_code') or node.get('pickcode') or node.get('pc')
        return pc if isinstance(pc,str) and re.fullmatch('[a-zA-Z0-9]{6,40}',pc) else ''
    async def folder_tree(self,pickcode,cancel=None):
        """Every directory below one directory, in ~1 request per 5000 dirs.

        GET webapi.115.com/files/downfolders (p115client download_folders /
        iter_download_nodes(files=False)): data.list = [{fid, fn, pid}],
        data.has_next_page. Returns {cid: (name|None, parent_cid)}; a name
        that is unusable as a path segment maps to None so the files below it
        are skipped with a precise reason instead of escaping the STRM root."""
        nodes={};page=1
        while True:
            if cancel and cancel():raise asyncio.CancelledError()
            value=await self.request('GET','https://webapi.115.com/files/downfolders',
                                     params={'pickcode':pickcode,'page':page,'per_page':self.TREE_PAGE})
            data=value.get('data')
            rows=data.get('list') if isinstance(data,dict) else None
            if not isinstance(rows,list):raise DriveError('115 目录树响应格式异常')
            for row in rows:
                if not isinstance(row,dict):continue
                try:fid=str(int(row['fid']));pid=str(int(row['pid']))
                except (KeyError,TypeError,ValueError):continue
                name=row.get('fn')
                ok=isinstance(name,str) and name and name not in ('.','..') and not any(c in name for c in ('/','\\','\x00'))
                nodes[fid]=(name if ok else None,pid)
            if not rows or not data.get('has_next_page'):return nodes
            page+=1
            if page>1000:raise DriveError('115 目录树分页异常')
    async def breadcrumb(self,cid):
        """Ancestors of one directory from the drive root: [{cid, name}, ...]."""
        return (await self.list(cid,0,limit=1))['path']
    async def _load_tree(self,root,cancel=None):
        """{cid: (name|None, parent)} for every directory below root, or {} if
        115 will not hand it out (breadcrumbs then resolve on demand)."""
        tree={}
        try:
            pc=await self.dir_pickcode(root)
            if pc:return await self.folder_tree(pc,cancel)
            # Drive root (no pickcode) or get_info refused: one tree request
            # per top-level child directory instead.
            offset=0
            while True:
                page=await self.list(root,offset)
                for it in page['items']:
                    if not it['is_dir']:continue
                    ok=it['name'] not in ('.','..')
                    tree[it['id']]=(it['name'] if ok else None,root)
                    if it['pickcode'] and ok:tree.update(await self.folder_tree(it['pickcode'],cancel))
                offset+=page['raw_len']
                if not page['raw_len'] or offset>=page['count']:break
            return tree
        except asyncio.CancelledError:raise
        except DriveError:return tree
    async def scan(self,root,cancel=None,bad_sink=None,skip_dirs=None,keep=None):
        """2.7.0 fast enumeration; 2.7.2 streams.

        1. Files: ONE flat, paged listing of the whole subtree (cur=0,
           show_dir=0, 1150/page). Each row names its parent directory id.
        2. Directory names: ``downfolders`` returns the whole directory tree
           in ~1 request per 5000 directories -- fetched right after the first
           page, BEFORE files are emitted, so every later row can be placed the
           moment its page arrives. Directories the tree does not know
           (created mid-scan, tree API unavailable) cost one breadcrumb lookup
           each, cached.

        2.7.2 streaming: files are yielded page by page while the listing is
        still running, so STRM writing overlaps the (API-interval paced)
        listing instead of waiting for all of it. There are no per-directory
        end markers in this mode (``{'__mode__': 'stream'}``); the engine
        settles same-name conflicts per output path as candidates arrive.

        ``keep(name)`` filters rows (only videos / wanted sidecars matter).
        The total is checked against「单次扫描最大条目数」on the first page
        (LimitError, no fallback). If 115's count moves while paging, paging
        continues with id dedup and ``{'__inexact__': True}`` is emitted so the
        engine skips deletions this run. Ends with ``{'__listing_done__': True}``.
        """
        root=str(root)
        yield {'__mode__':'stream'}
        limit=int(self.cfg.max_entries)
        ids=set();offset=0;expected=None;inexact=False;listed=0
        tree=None;rel={root:''};broken={}  # broken: cid -> reason
        def chain(cid):
            seq=[];node=cid;hops=0
            while node not in rel:
                if node in broken or not tree or node not in tree:return None
                name,parent=tree[node]
                if name is None:broken[node]='上级目录名称不合规';return None
                seq.append((node,name));node=parent;hops+=1
                if hops>256:return None
            base=rel[node]
            for nid,name in reversed(seq):
                base=base+'/'+name if base else name;rel[nid]=base
            return rel[cid]
        async def resolve(cid):
            if cid in rel:return rel[cid]
            if cid in broken:return None
            if chain(cid) is not None:return rel[cid]
            if cid in broken:return None
            reason='所在目录不在同步根目录之下'
            try:
                crumbs=await self.breadcrumb(cid)
                ids_=[c['cid'] for c in crumbs]
                if root in ids_:
                    base=''
                    for c in crumbs[ids_.index(root)+1:]:
                        name=c['name']
                        if name in ('.','..') or any(ch in name for ch in ('/','\\','\x00')):
                            reason='上级目录名称不合规（%r）'%name[:40];break
                        base=base+'/'+name if base else name;rel[c['cid']]=base
            except DriveError as e:
                # Credential trouble / exhausted throttle retries are fatal;
                # anything else means this one directory vanished mid-scan.
                if '990001' in str(e) or '已等待重试' in str(e) or 'Cookie' in str(e):raise
                reason='所在目录已不存在（扫描期间被移动或删除）'
            if cid not in rel:broken[cid]=reason;return None
            return rel[cid]
        while True:
            if cancel and cancel():raise asyncio.CancelledError()
            page=await self.list(root,offset,limit=1150,recursive=True)
            if expected is None:
                expected=page['count']
                if limit and expected>limit:
                    raise LimitError('该同步目录共有 %d 个文件，超过「单次扫描最大条目数」%d。请在「数据同步 → 全量同步」把该项调大（建议 ≥ %d），或把映射改成更小的子目录'%(expected,limit,int(expected*1.2)))
            elif page['count']!=expected:
                inexact=True;expected=max(expected,page['count'])
            if bad_sink is not None:
                for entry in page['bad']:bad_sink.append({**entry,'dir':'/'})
            batch=[]
            for item in page['items']:
                listed+=1
                # Only kept rows are remembered for de-duplication: a drive of
                # millions of files is mostly rows we drop anyway (2.7.6).
                if keep is not None and not keep(item['name']):continue
                if item['id'] in ids:continue
                ids.add(item['id']);batch.append(item)
            if tree is None and any(f['parent_cid']!=root for f in batch):
                tree=await self._load_tree(root,cancel)
            for f in batch:
                p=f['parent_cid']
                base=rel[p] if p in rel else await resolve(p)
                if base is None:
                    if bad_sink is not None:
                        bad_sink.append({'index':0,'reason':broken.get(p,''),'dir':'?','detail':'n=%r fid=%s cid=%s'%(f['name'][:60],f['id'],p)})
                    continue
                yield {**f,'path':base+'/'+f['name'] if base else f['name']}
            offset+=page['raw_len']
            yield {'__progress__':listed,'total':expected}
            if page['raw_len']==0:
                if offset<expected:inexact=True
                break
            if offset>=expected:break
        if inexact:yield {'__inexact__':True}
        yield {'__listing_done__':True}
    async def walk(self,root,cancel=None,bad_sink=None,skip_dirs=None):
        """Depth-first enumeration of the tree below ``root``.

        2.6.0 additions for checkpoint resume: every yielded file carries its
        ``parent_cid``, and after each directory's pages are exhausted a
        ``{'__end_dir__': True, 'cid', 'parent_cid'}`` marker is yielded so the
        engine knows the directory was fully enumerated (its files may still be
        in flight to the writer). ``skip_dirs`` is the set of directories whose
        ENTIRE subtree was already confirmed complete by a previous sync -- the
        walker skips them wholesale (no files, no end marker) so a resumed full
        sync does not re-walk already-finished subtrees.
        """
        skip=set(skip_dirs or ());root=str(root)
        stack=[(root,'',None)];seen=set();total=0
        while stack:
            cid,relative,parent=stack.pop()
            if skip and cid in skip and cid!=root:
                # Resume: this subtree was completed last time; skip it whole.
                continue
            if cid in seen:raise DriveError('网盘目录重复或成环，同步已停止')
            seen.add(cid);offset=0;expected=None;page_ids=set()
            while True:
                if cancel and cancel():raise asyncio.CancelledError()
                page=await self.list(cid,offset)
                if expected is None:expected=page['count']
                elif expected!=page['count']:raise DriveError('同步期间网盘目录数量变化，请重试')
                rows=page['items'];raw_len=page['raw_len']
                # Skipped entries ride along with their directory context; the
                # sync engine logs each one and counts them in the run report.
                if bad_sink is not None and page['bad']:
                    for entry in page['bad']:bad_sink.append({**entry,'dir':relative or '/'})
                # raw_len (not the surviving item count) drives pagination --
                # skipped entries still occupy slots in 115's listing.
                if raw_len==0 and offset<expected:raise DriveError('115 分页提前结束，同步已停止')
                for item in rows:
                    marker=(item['id'],item['is_dir'])
                    if marker in page_ids:raise DriveError('115 分页出现重复条目，请重试')
                    page_ids.add(marker);total+=1
                    if self.cfg.max_entries and total>self.cfg.max_entries:raise LimitError('扫描条目超过「单次扫描最大条目数」%d，请在「数据同步 → 全量同步」调大该项或缩小同步目录'%self.cfg.max_entries)
                    full=relative+'/'+item['name'] if relative else item['name']
                    if item['is_dir']:stack.append((item['id'],full,cid))
                    else:yield {**item,'path':full,'parent_cid':cid}
                offset+=raw_len
                if offset>=expected:break
            yield {'__end_dir__':True,'cid':cid,'parent_cid':parent}
    async def account(self):
        # 115driver GetInfo: GET https://webapi.115.com/files/index_info (no params).
        # data.space_info.{all_total,all_use,all_remain}.size -- size arrives as int OR numeric string.
        value=await self.request('GET','https://webapi.115.com/files/index_info')
        data=value.get('data')
        if not isinstance(data,dict):raise DriveError('115 账号信息格式异常')
        def number(v):
            # 115 sometimes sends sizes as strings ("107374182400"), sometimes as numbers.
            if isinstance(v,bool):return None
            try:n=int(v)
            except (TypeError,ValueError):
                try:n=int(float(str(v).strip()))
                except (TypeError,ValueError):return None
            return n if n>=0 else None
        def sized(node):
            return number(node.get('size')) if isinstance(node,dict) else None
        space=data.get('space_info') if isinstance(data.get('space_info'),dict) else {}
        total=sized(space.get('all_total'));used=sized(space.get('all_use'));remain=sized(space.get('all_remain'))
        if total is None and used is None:
            # Pre-1.6.3 guessed path, kept as a fallback in case 115 varies by account/app.
            legacy=data.get('space') if isinstance(data.get('space'),dict) else {}
            total=number(legacy.get('space_total'));used=number(legacy.get('space_used'))
        if used is not None and total is None and remain is not None:total=used+remain
        free=remain if remain is not None else (max(0,total-used) if total is not None and used is not None else None)
        uid=re.search(r'(?:^|;\s*)UID=(\d+)',self.cfg.cookie.get_secret_value())
        # A state=true response already proves the credential works; missing capacity
        # numbers are cosmetic and must NOT fail the login check (they only hide the gauge).
        return {'uid':uid[1] if uid else '',
                **({'user_name':data['user_name']} if isinstance(data.get('user_name'),str) else {}),
                'space_total':total,'space_used':used,'space_free':free}
    def _parse_sign_content(self,content,ua,allow_cookie_url,label,decode_data):
        """Shared response handling for both signing protocols: body may be an
        ECDH envelope, errno must pass through with its hint, the data field is
        protocol-encrypted, and f=3 links are proxy-only."""
        info_by_key=self._decode_sign_content(content,label,decode_data)
        try:
            info=next(iter(info_by_key.values()))
            url,sign_headers=self._url_from_info(info,allow_cookie_url)
            return url,ua,sign_headers
        except DriveError:raise
        except Exception:raise DriveError('115 下载地址响应结构异常') from None

    def _decode_sign_content(self,content,label,decode_data):
        """Envelope + errno + protocol decryption -> {file_id: info}."""
        if not content:raise DriveError('115 下载签发响应为空')
        # p115client content_maybe_decrypt: a body NOT starting/ending like
        # {} / [] / "" is the ECDH envelope, not plain JSON.
        try:
            body=content if content[:1]+content[-1:] in (b'{}',b'[]',b'""') else ecdh_aes_decrypt(content)
            value=json.loads(body)
            if not isinstance(value,dict):raise ValueError('not a dict')
        except Exception:raise DriveError(label+' 签发响应解密失败（响应体 %d 字节，首字节 0x%02x）——请反馈此行'% (len(content),content[0])) from None
        if value.get('state') not in (True,1):
            code=value.get('errno',value.get('code','unknown'))
            msg='115 拒绝签发直链，错误码 '+str(code)[:30]
            hint=errno_hint(code)
            if hint:msg+='：'+hint
            err=value.get('error')
            if isinstance(err,str) and err:msg+='（'+err[:80]+'）'
            raise DriveError(msg)
        data=value.get('data')
        # p115client: `if json["state"] and (data := json.get("data"))` -- empty
        # data means 115 signed but served no URL; surface it clearly.
        if not data:raise DriveError('115 未返回此文件的下载地址（文件可能已被删除、转码中或被限制下载）')
        try:
            info_by_key=decode_data(data)
        except Exception:
            prefix=data[:8] if isinstance(data,(str,bytes,bytearray)) else repr(data)[:8]
            raise DriveError('115 下载地址解密失败（'+label+' data %s，前缀 %r）——请反馈此行'%(type(data).__name__,prefix)) from None
        if not isinstance(info_by_key,dict) or not info_by_key:raise DriveError('115 下载地址响应结构异常')
        return info_by_key

    def _url_from_info(self,info,allow_cookie_url):
        """One file's {url: {url, header}} node -> (url, sign_headers)."""
        url_node=info.get('url') if isinstance(info,dict) else None
        # 115 sometimes answers url:false instead of an object (115driver
        # info.go Jul 2026 added a tolerant unmarshal); surface it clearly
        # instead of a cryptic decrypt error.
        if not isinstance(url_node,dict) or not isinstance(url_node.get('url'),str) or not url_node['url']:
            raise DriveError('115 未返回此文件的下载地址（文件可能已被删除或被限制下载）')
        url=safe_url(url_node['url'])
        # f=3 links demand a CDN-scoped Cookie on every fetch. A plain 302
        # cannot attach headers, so only the proxy play mode may accept them.
        if parse_qs(urlsplit(url).query).get('f')==['3'] and not allow_cookie_url:
            raise DriveError('此直链需要额外 Cookie，302 直连无法播放；请把「播放方式」切换为服务器中转')
        # 115driver DownloadUrl unmarshals url.header (http.Header) and
        # OpenList's Link() hands it to the consumer -- the CDN often
        # requires exactly these headers. Extract and replay them on our
        # own fetches (proxy mode, sidecar download, diagnostics).
        raw_hdr=url_node.get('header') if isinstance(url_node.get('header'),dict) else {}
        sign_headers={str(k):str(v) for k,v in raw_hdr.items() if isinstance(k,str) and isinstance(v,str)}
        return url,sign_headers

    EXPIRY_MARGIN=300.
    EXPIRY_CAP=4*3600.
    @classmethod
    def link_expiry(cls,url):
        """Monotonic deadline from a 115 CDN link's own ``t`` (unix expiry)
        query parameter, minus a safety margin; None if absent/implausible."""
        try:t=float(parse_qs(urlsplit(url).query).get('t',[''])[0])
        except (TypeError,ValueError):return None
        left=min(t-time.time()-cls.EXPIRY_MARGIN,cls.EXPIRY_CAP)
        return time.monotonic()+left if left>=60 else None
    def _link_valid(self,hit,now,min_remaining=0.):
        if not hit:return False
        if hit.get('expires'):return now+min_remaining<hit['expires']
        return now-hit['at']+min_remaining<max(60,int(getattr(self.cfg,'link_cache_minutes',10))*60)
    async def prime_links(self,pickcodes,cancel=None,ua='',allow_cookie_url=True,min_remaining=0.,use_expiry=False):
        """Batch-sign links (one 115 request per SIGN_BATCH pickcodes) and seed
        the link cache under (pickcode, ua, allow_cookie_url).

        2.7.0: cloud sidecars (empty UA, Cookie URLs allowed).
        2.8.0: next-episode prefetch -- signed with the PLAYER's UA (302 links
        are bound to it) and, with use_expiry, cached until the link's own
        expiry instead of「直链缓存时间」, so a link fetched at the start of a
        45-minute episode is still served when the next one starts.

        Best effort: any failure just leaves the per-file path to do its job.
        Returns how many links were primed."""
        now=time.monotonic();ua=ua or '';allow=bool(allow_cookie_url)
        todo=[]
        for pc in dict.fromkeys(pickcodes):
            if not isinstance(pc,str) or not re.fullmatch('[a-zA-Z0-9]{6,40}',pc):continue
            if self._link_valid(self._links.get((pc,ua,allow)),now,min_remaining):continue
            todo.append(pc)
        primed=0
        protos=['m115','rsa']
        if self._ok is not None and self._ok[1]==ua and self._ok[0] in protos:
            protos.remove(self._ok[0]);protos.insert(0,self._ok[0])
        for i in range(0,len(todo),self.SIGN_BATCH):
            if cancel and cancel():raise asyncio.CancelledError()
            chunk=todo[i:i+self.SIGN_BATCH];infos=None
            for proto in protos:
                try:
                    joined=','.join(chunk)
                    if proto=='m115':
                        key=m115.generate_key()
                        payload=json.dumps({'pickcode':joined},separators=(',',':'))
                        content=await self.request('POST','https://proapi.115.com/app/chrome/downurl',params={'t':str(time.time_ns()//1_000_000)},data={'data':m115.encode(payload.encode(),key)},ua=ua,raw_content=True)
                        infos=self._decode_sign_content(content,'m115',lambda d,key=key:json.loads(m115.decode(d,key)))
                    else:
                        uid=re.search(r'(?:^|;\s*)UID=(\d+)',self.cfg.cookie.get_secret_value())
                        if not uid:continue
                        payload={'pickcode':joined,'user_id':int(uid[1])}
                        content=await self.request('POST','https://proapi.115.com/app/chrome/downurl',data={'data':rsa_encrypt(json.dumps(payload,separators=(',',':')).encode()).decode()},ua=ua,raw_content=True)
                        infos=self._decode_sign_content(content,'rsa',lambda d:json.loads(rsa_decrypt(d)))
                    protos=[proto]+[p for p in protos if p!=proto];break
                except asyncio.CancelledError:raise
                except DriveError:infos=None
            if infos is None:return primed  # batching refused: per-file path takes over
            at=time.monotonic()
            for info in infos.values():
                if not isinstance(info,dict):continue
                pc=info.get('pick_code') or info.get('pickcode')
                if pc not in chunk:continue
                try:url,hdrs=self._url_from_info(info,allow)  # 302: f=3 links are skipped
                except DriveError:continue
                if len(self._links)>=self.LINK_CACHE_MAX:
                    oldest=min(self._links,key=lambda k:self._links[k]['at'])
                    del self._links[oldest]
                entry={'url':url,'ua':ua,'headers':hdrs,'proto':protos[0],'at':at}
                if use_expiry:
                    exp=self.link_expiry(url)
                    if exp:entry['expires']=exp
                self._links[(pc,ua,allow)]=entry;primed+=1
        return primed

    async def sign_m115(self,pickcode,ua='',allow_cookie_url=False):
        """Sign a download URL with the m115 protocol -- the exact wire format
        of alist/OpenList's DownloadWithUA (115driver pkg/crypto/m115 +
        download.go): a fresh random 16-byte key seals {"pickcode": ...} via
        the m115 cipher, and 115 answers with a m115-sealed `data` field
        (DownloadResp's json tag is "data"). The 1.7.1/1.7.2 real-device log
        proved this account's downurl answers are NOT the legacy RSA envelope
        p115client's download_url_app expects (rsa_decrypt failed for every
        UA), so m115 is now the primary protocol, with RSA kept as fallback.
        Returns (url, ua, sign_headers) -- headers from the response's
        url.header field, required by the CDN and replayed on our own fetches."""
        if not re.fullmatch('[a-zA-Z0-9]{6,40}',pickcode):raise DriveError('无效的文件提取码')
        key=m115.generate_key()
        payload=json.dumps({'pickcode':pickcode},separators=(',',':'))
        content=await self.request('POST','https://proapi.115.com/app/chrome/downurl',params={'t':str(time.time_ns()//1_000_000)},data={'data':m115.encode(payload.encode(),key)},ua=ua,raw_content=True)
        return self._parse_sign_content(content,ua,allow_cookie_url,'m115',lambda d:json.loads(m115.decode(d,key)))

    async def sign_once(self,pickcode,ua='',allow_cookie_url=False):
        """Sign a download URL with the legacy RSA protocol (p115client
        download_url_app(app="chrome")): RSA-encrypted {pickcode, user_id}
        request, RSA-encrypted `data` field in the response."""
        if not re.fullmatch('[a-zA-Z0-9]{6,40}',pickcode):raise DriveError('无效的文件提取码')
        uid=re.search(r'(?:^|;\s*)UID=(\d+)',self.cfg.cookie.get_secret_value())
        if not uid:raise DriveError('Cookie 缺少有效 UID')
        payload={'pickcode':pickcode,'user_id':int(uid[1])}
        content=await self.request('POST','https://proapi.115.com/app/chrome/downurl',data={'data':rsa_encrypt(json.dumps(payload,separators=(',',':')).encode()).decode()},ua=ua,raw_content=True)
        return self._parse_sign_content(content,ua,allow_cookie_url,'rsa',lambda d:json.loads(rsa_decrypt(d)))

    def invalidate_links(self,pickcode):
        """Drop every cached link for a file (all UAs). Used when the CDN
        rejects a proxied fetch -- the URL has likely expired, so later
        requests must re-sign instead of looping on the dead link."""
        for key in [k for k in self._links if k[0]==pickcode]:
            del self._links[key]

    async def link_signed(self,pickcode,ua=None,allow_cookie_url=False,fresh=False):
        """Sign a download URL, walking strategies in order (alist/OpenList model).

        Protocol dimension (1.7.3): m115 first -- the wire format of alist's
        DownloadWithUA, and the only one 115 demonstrably answers for real
        accounts (the 1.7.1/1.7.2 RSA-only signing failed on-device) -- with
        the legacy RSA envelope as fallback.

        UA dimension (1.7.4): when the caller passes a non-empty UA (302
        playback -- the player both hits /play and fetches the CDN), sign
        with THAT UA FIRST. The 1.7.3 real-device diagnostic proved f=1
        links are bound to the signing UA (every other UA got 403 from the
        CDN), so a player can only fetch a link minted with its own UA --
        exactly alist/OpenList's Link() model. Empty UA stays as fallback.
        Internal fetches (proxy mode, cloud-sidecar download) pass ua=None:
        they sign empty-UA-first and replay whatever UA came back.

        The (protocol, UA) memo only applies when the UA matches the caller's:
        reusing another player's memo would mint a link this player cannot
        fetch.

        Cache (1.7.5): the signed URL is memoized per (pickcode, ua,
        allow_cookie_url) for cfg.link_cache_minutes. Players re-request
        /play for every seek and subtitle-track read; serving those from
        cache removes the per-request 115 roundtrip (plus the global API
        interval) that made subtitle loading crawl. fresh=True (diagnostics,
        CDN-rejected retry) bypasses and overwrites the cache.
        Returns (url, signing_ua, sign_headers).
        """
        effective='' if ua is None else ua
        now=time.monotonic()
        key=(pickcode,effective,bool(allow_cookie_url))
        hit=self._links.get(key)
        if hit and not fresh and self._link_valid(hit,now):
            self.last_sign={'cached':True,'proto':hit['proto'],'elapsed':0.}
            return hit['url'],hit['ua'],hit['headers']
        if self._ok is not None and not fresh and self._ok[1]==effective:
            pairs=[self._ok]
        elif effective:
            pairs=[('m115',effective),('rsa',effective),('m115',''),('rsa','')]
        else:
            pairs=[('m115',''),('rsa','')]+([('m115',self.ua),('rsa',self.ua)] if self.ua and self.ua!='' else [])
        errors=[]
        for proto,candidate in dict.fromkeys(pairs):
            signer=self.sign_m115 if proto=='m115' else self.sign_once
            try:
                started=time.monotonic()
                url,used,hdrs=await signer(pickcode,ua=candidate,allow_cookie_url=allow_cookie_url)
                self._ok=(proto,candidate)
                if len(self._links)>=self.LINK_CACHE_MAX:
                    oldest=min(self._links,key=lambda k:self._links[k]['at'])
                    del self._links[oldest]
                self._links[key]={'url':url,'ua':used,'headers':hdrs,'proto':proto,'at':time.monotonic()}
                self.last_sign={'cached':False,'proto':proto,'elapsed':time.monotonic()-started}
                return url,used,hdrs
            except DriveError as e:
                errors.append((proto+('空UA' if candidate=='' else 'UA '+candidate[:40]),str(e)))
        if len(errors)==1:raise DriveError(errors[0][1])
        seen=[]
        for label,msg in errors:
            item=label+'：'+msg
            if item not in seen:seen.append(item)
        raise DriveError('直链签发失败（'+'；'.join(seen)[:400]+')')

    async def link(self,pickcode):
        """Backward-compatible single-URL signing (internal fetch strategy)."""
        url,_,_=await self.link_signed(pickcode,allow_cookie_url=True)
        return url
    async def mutate(self,action,file_id='',parent_id='0',name=''):
        if action in ('mkdir','move','copy'):
            await self.list(parent_id)
        endpoints={'mkdir':('/files/add',{'pid':parent_id,'cname':name}),
                   'rename':('/files/batch_rename',{f'files_new_name[{file_id}]':name}),
                   'move':('/files/move',{'pid':parent_id,'fid[0]':file_id}),
                   'copy':('/files/copy',{'pid':parent_id,'fid[0]':file_id}),
                   'delete':('/rb/delete',{'fid[0]':file_id,'pid':parent_id})}
        path,data=endpoints[action]
        return await self.request('POST','https://webapi.115.com'+path,data=data)
