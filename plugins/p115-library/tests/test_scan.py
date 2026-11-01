"""2.7.0 fast enumeration (flat cur=0 listing + downfolders tree) and the
trusted unchanged-file path. 115 is simulated with an httpx MockTransport."""
import asyncio
import copy
import httpx
import pytest
import drive_api as a
import p115_plugin as p
from sync_engine import SyncEngine
from test_sync import Store, Files, Ops, Routes, setup


def cfg(**values):
    base=dict(cookie='UID=123_A1; CID=secret; SEID=secret',request_interval=.3,retry_max=0)
    base.update(values);return p.Settings(**base)


# Cloud tree under root 100:
#   100/
#     电影/ (200)          A.mkv  A.srt
#       合集/ (201)        B.mp4
#     剧集/ (300)
#       S01/ (301)         E01.mkv
#     root.mkv
DIRS={'200':('电影','100'),'201':('合集','200'),'300':('剧集','100'),'301':('S01','300')}
FILES=[('1','A.mkv','200'),('2','A.srt','200'),('3','B.mp4','201'),('4','E01.mkv','301'),('5','root.mkv','100')]
NAMES={'0':'根目录','100':'媒体','200':'电影','201':'合集','300':'剧集','301':'S01'}
PARENT={'100':'0','200':'100','201':'200','300':'100','301':'300'}

def crumbs(cid):
    out=[];node=cid
    while True:
        out.append({'cid':node,'name':NAMES[node]})
        if node=='0':break
        node=PARENT[node]
    return list(reversed(out))

def fake_115(log,downfolders=True,files=FILES,page=2):
    def respond(r):
        q=r.url.params;log.append((r.url.path,dict(q)))
        if r.url.path=='/files' and q.get('cur')=='0':
            assert q['show_dir']=='0' and q['cid']=='100'
            off=int(q['offset']);rows=[{'fid':fid,'cid':pid,'n':n,'pc':'pc'+fid.zfill(6),'s':'1000','sha':'s'+fid,'te':'1'} for fid,n,pid in files]
            return httpx.Response(200,json={'state':True,'cid':'100','count':len(rows),'data':rows[off:off+page]})
        if r.url.path=='/files':  # cur=1: breadcrumb lookup / direct listing
            cid=q['cid']
            return httpx.Response(200,json={'state':True,'cid':cid,'count':0,'data':[],'path':crumbs(cid)})
        if r.url.path=='/files/get_info':
            return httpx.Response(200,json={'state':True,'data':[{'file_id':q['file_id'],'pick_code':'fdirpc100'}]})
        if r.url.path=='/files/downfolders':
            if not downfolders:return httpx.Response(404)
            assert q['pickcode']=='fdirpc100'
            return httpx.Response(200,json={'state':True,'data':{'has_next_page':False,'list':[{'fid':k,'fn':v[0],'pid':v[1]} for k,v in DIRS.items()]}})
        return httpx.Response(404)
    return respond

async def collect(drive,root='100'):
    bad=[];events=[e async for e in drive.scan(root,bad_sink=bad)];return events,bad

@pytest.mark.asyncio
async def test_scan_builds_paths_from_flat_listing_and_tree(monkeypatch):
    log=[];monkeypatch.setattr(a.net,'http_transport',lambda _:httpx.MockTransport(fake_115(log)))
    drive=a.Drive(cfg(request_interval=.3))
    drive.cfg.request_interval=0.0
    events,bad=await collect(drive)
    assert events[0]=={'__mode__':'stream'} and events[-1]=={'__listing_done__':True}
    files=[e for e in events if 'path' in e]
    assert sorted(f['path'] for f in files)==['root.mkv','剧集/S01/E01.mkv','电影/A.mkv','电影/A.srt','电影/合集/B.mp4']
    assert all(f['parent_cid'] for f in files) and not bad
    # 2.7.2 streaming: files of page 1 are emitted BEFORE page 2 is requested
    kinds=[('file' if 'path' in e else 'progress' if '__progress__' in e else 'other') for e in events]
    assert kinds.index('file')<kinds.index('progress'),'第一页的文件应在继续翻页前就交给写入'
    assert not any(e.get('__end_dir__') for e in events)
    paths=[q for q,_ in log]
    # 3 flat pages + get_info + downfolders; NO per-directory requests
    assert paths.count('/files')==3 and paths.count('/files/downfolders')==1 and len(paths)==5

@pytest.mark.asyncio
async def test_scan_falls_back_to_breadcrumbs_when_tree_api_missing(monkeypatch):
    log=[];monkeypatch.setattr(a.net,'http_transport',lambda _:httpx.MockTransport(fake_115(log,downfolders=False)))
    drive=a.Drive(cfg());drive.cfg.request_interval=0.0
    events,bad=await collect(drive)
    assert sorted(e['path'] for e in events if 'path' in e)==['root.mkv','剧集/S01/E01.mkv','电影/A.mkv','电影/A.srt','电影/合集/B.mp4']
    lookups=[q['cid'] for path,q in log if path=='/files' and q.get('cur')=='1']
    assert set(lookups)<={'200','201','301'}
    assert len(lookups)<=3,'每个未知目录最多一次面包屑查询，祖先顺带解析'

@pytest.mark.asyncio
async def test_scan_rejects_bad_directory_names(monkeypatch):
    log=[];monkeypatch.setattr(a.net,'http_transport',lambda _:httpx.MockTransport(fake_115(log)))
    monkeypatch.setitem(DIRS,'201',('a/b','200'))
    monkeypatch.setitem(NAMES,'201','a/b')
    drive=a.Drive(cfg());drive.cfg.request_interval=0.0
    events,bad=await collect(drive)
    assert 'B.mp4' not in [e.get('name') for e in events if 'path' in e]
    assert bad and '不合规' in bad[0]['reason']

@pytest.mark.asyncio
async def test_scan_tolerates_count_change_and_flags_inexact(monkeypatch):
    # 2.7.1：大库分页期间 115 计数变化（有上传）不再整轮重来，去重继续并标记 inexact（本轮不做删除）
    log=[];calls={'n':0}
    inner=fake_115(log)
    def respond(r):
        if r.url.path=='/files' and r.url.params.get('cur')=='0':
            calls['n']+=1
            if calls['n']==2:
                resp=inner(r);body=resp.json();body['count']=6
                return httpx.Response(200,json=body)
        return inner(r)
    monkeypatch.setattr(a.net,'http_transport',lambda _:httpx.MockTransport(respond))
    drive=a.Drive(cfg());drive.cfg.request_interval=0.0
    events,_=await collect(drive)
    assert len([e for e in events if 'path' in e])==5
    assert {'__inexact__':True} in events

@pytest.mark.asyncio
async def test_scan_limit_checked_on_first_page_without_fallback(monkeypatch):
    log=[];monkeypatch.setattr(a.net,'http_transport',lambda _:httpx.MockTransport(fake_115(log)))
    drive=a.Drive(cfg(max_entries=100));drive.cfg.request_interval=0.0;drive.cfg.max_entries=4
    with pytest.raises(a.LimitError,match='共有 5 个文件.*调大'):await collect(drive)
    assert len(log)==1,'超限应在第一页就报错，不继续翻页'

@pytest.mark.asyncio
async def test_scan_keep_filter_drops_unwanted_rows(monkeypatch):
    log=[];monkeypatch.setattr(a.net,'http_transport',lambda _:httpx.MockTransport(fake_115(log)))
    drive=a.Drive(cfg());drive.cfg.request_interval=0.0
    events=[e async for e in drive.scan('100',keep=lambda n:n.endswith('.mkv'))]
    assert sorted(e['name'] for e in events if 'path' in e)==['A.mkv','E01.mkv','root.mkv']
    assert [e for e in events if '__progress__' in e][-1]['__progress__']==5,'进度按 115 实际列出数计'

@pytest.mark.asyncio
async def test_limit_error_does_not_fall_back_to_legacy_walk(tmp_path):
    from test_sync import setup as sync_setup
    eng=sync_setup(tmp_path)
    async def scan(*a_,**k):
        yield {'__mode__':'fast'}
        raise a.LimitError('该同步目录共有 300000 个文件，超过')
    async def walk(*a_,**k):
        raise AssertionError('超限不应退回逐目录扫描')
        yield
    eng.drive.scan=scan;eng.drive.walk=walk
    with pytest.raises(a.DriveError,match='300000'):await eng.run()


class ScanDrive:
    """Engine-level fake with a scan() method, mirroring Drive.scan's contract."""
    def __init__(self):
        self.items=[{'id':str(i),'path':'片%d/片%d.mkv'%(i,i),'name':'片%d.mkv'%i,'pickcode':'abcdef%06d'%i,'size':100,'sha1':'h%d'%i,'mtime':'1','parent_cid':'d%d'%i} for i in range(20)]
        self.scans=0
    async def scan(self,cid,cancel,bad_sink=None,skip_dirs=None,keep=None):
        self.scans+=1
        yield {'__mode__':'fast'}
        yield {'__progress__':len(self.items)}
        for it in self.items:
            yield copy.deepcopy(it)
            yield {'__end_dir__':True,'cid':it['parent_cid'],'parent_cid':None}
    async def walk(self,*a,**k):
        raise AssertionError('fast scan must be preferred')
        yield


class CountingStore(Store):
    def __init__(self):super().__init__();self.sets=[];self.gets=0
    async def get(self,key,default=None):self.gets+=1;return await super().get(key,default)
    async def set(self,key,value,**kw):self.sets.append(key);await super().set(key,value,**kw)


def engine_with(tmp_path,**cfgv):
    eng=setup(tmp_path,**cfgv)
    eng.drive=ScanDrive();eng.store=CountingStore()
    from sync_engine import PlayIndex;eng.play_index=PlayIndex(eng.store)
    eng.drive.on_wait=None
    return eng

@pytest.mark.asyncio
async def test_unchanged_files_cost_no_sign_and_no_store_writes(tmp_path):
    eng=engine_with(tmp_path)
    first=await eng.run();assert first['written']==20 and first['seen']==20
    eng.routes.calls.clear();eng.store.sets.clear()
    second=await eng.run()
    assert second['unchanged']==20 and second['written']==0
    assert len(eng.routes.calls)==1,'未变化文件不再逐个签发（只签一次探针）：%r'%eng.routes.calls
    per_file=[k for k in eng.store.sets if k.startswith(('owned:','play:','playx:','manifest:')) and not k.endswith(':meta')]
    assert per_file==[],'未变化的库不应写任何逐文件记录：%r'%per_file[:5]

@pytest.mark.asyncio
async def test_changed_file_is_rewritten_on_trusted_run(tmp_path):
    eng=engine_with(tmp_path);await eng.run()
    eng.drive.items[3]['mtime']='2';eng.drive.items[4]['pickcode']='zzzzzz000004'
    result=await eng.run();assert result['written']==2 and result['unchanged']==18

@pytest.mark.asyncio
async def test_unclean_previous_run_disables_trust(tmp_path):
    eng=engine_with(tmp_path);await eng.run()
    target=tmp_path/'115/片1/片1.strm';target.write_text('')  # torn write
    meta=[k for k in eng.store.data if k.endswith(':meta')][0]
    eng.store.data[meta]['clean']=False   # simulate a crash mid-run
    result=await eng.run()
    assert result['written']==1 and target.read_text().startswith('https://')

@pytest.mark.asyncio
async def test_url_change_disables_trust_and_rewrites_everything(tmp_path):
    eng=engine_with(tmp_path);await eng.run()
    eng.cfg.strm_base_url='http://new.example:9000'
    result=await eng.run();assert result['written']==20
    assert all(f.read_text().startswith('http://new.example:9000/') for f in tmp_path.rglob('*.strm'))

@pytest.mark.asyncio
async def test_missing_local_file_recreated_on_trusted_run(tmp_path):
    eng=engine_with(tmp_path,sync_mode='fast');await eng.run()
    (tmp_path/'115/片7/片7.strm').unlink()
    result=await eng.run();assert result['written']==1
    assert (tmp_path/'115/片7/片7.strm').exists()

@pytest.mark.asyncio
async def test_legacy_strategy_uses_walk(tmp_path):
    eng=setup(tmp_path,scan_strategy='legacy')
    eng.drive.scan=None  # would explode if called
    result=await eng.run();assert result['written']==1

@pytest.mark.asyncio
async def test_writers_drain_queue_before_finishing(tmp_path):
    # 2.6.x cancelled writers 10 s after the walk ended, silently dropping
    # queued files on a slow host. All files must land.
    eng=engine_with(tmp_path)
    eng.drive.items=[{**eng.drive.items[0],'id':str(i),'path':'d/片%d.mkv'%i,'name':'片%d.mkv'%i,'pickcode':'abcdef%06d'%i,'parent_cid':'d'} for i in range(300)]
    class SlowFiles(Files):
        async def rename(self,src,dst):
            await asyncio.sleep(0.002);await super().rename(src,dst)
    eng.files=SlowFiles(tmp_path)
    result=await eng.run();assert result['written']==300
    assert len(list(tmp_path.rglob('*.strm')))==300


@pytest.mark.asyncio
async def test_prime_links_batches_and_seeds_cache(monkeypatch):
    import json
    from base64 import b64encode
    import p115_m115 as m115
    from test_drive import _m115_read_request,_identity_rsa_seal
    _identity_rsa_seal(monkeypatch)
    batches=[]
    def respond(r):
        key,payload=_m115_read_request(r)
        pcs=payload['pickcode'].split(',');batches.append(len(pcs))
        result=json.dumps({str(i):{'pick_code':pc,'url':{'url':'https://cdn.example.com/'+pc+'?f=1'}} for i,pc in enumerate(pcs)}).encode()
        server_key=m115.generate_key()
        body=m115._xor_transform(result,m115._xor_derive_key(key,4))[::-1]
        body=m115._xor_transform(body,m115._xor_derive_key(server_key,12))
        return httpx.Response(200,json={'state':True,'data':b64encode(server_key+body).decode()})
    monkeypatch.setattr(a.net,'http_transport',lambda _:httpx.MockTransport(respond))
    drive=a.Drive(cfg());drive.cfg.request_interval=0.0
    pcs=['abcdef%06d'%i for i in range(250)]
    assert await drive.prime_links(pcs)==250
    assert batches==[100,100,50],'每 100 个提取码一次签发'
    url,ua,_=await drive.link_signed(pcs[7],allow_cookie_url=True)
    assert url=='https://cdn.example.com/abcdef000007?f=1' and ua=='' and len(batches)==3,'预签后应命中缓存，不再请求 115'
    assert await drive.prime_links(pcs[:10])==0,'已缓存的不重复签发'

@pytest.mark.asyncio
async def test_prime_links_refused_falls_back_quietly(monkeypatch):
    monkeypatch.setattr(a.net,'http_transport',lambda _:httpx.MockTransport(lambda r:httpx.Response(200,json={'state':False,'errno':990002})))
    drive=a.Drive(cfg());drive.cfg.request_interval=0.0
    assert await drive.prime_links(['abcdef000001','abcdef000002'])==0

def test_max_entries_unlimited_by_default_and_old_defaults_migrated():
    import panel
    assert p.Settings().max_entries==0,'默认不限制'
    assert 'data-field="max_entries"' in panel.PANEL and "'max_entries'" in panel.PANEL
    src=open(p.__file__,encoding='utf-8').read()
    assert "stored.get('max_entries') in (100000,1000000)" in src,'两个旧默认值都必须迁移为不限'

@pytest.mark.asyncio
async def test_unlimited_scan_accepts_any_size(monkeypatch):
    log=[];monkeypatch.setattr(a.net,'http_transport',lambda _:httpx.MockTransport(fake_115(log)))
    drive=a.Drive(cfg());drive.cfg.request_interval=0.0;drive.cfg.max_entries=0
    events,_=await collect(drive)
    assert len([e for e in events if 'path' in e])==5


@pytest.mark.asyncio
async def test_streaming_writes_start_before_listing_finishes(tmp_path):
    """2.7.2: STRM files land while 115 is still being listed."""
    eng=engine_with(tmp_path)
    gate=asyncio.Event();written_during_listing=[]
    class StreamDrive(ScanDrive):
        async def scan(self,cid,cancel,bad_sink=None,skip_dirs=None,keep=None):
            yield {'__mode__':'stream'}
            for i,it in enumerate(self.items):
                yield copy.deepcopy(it)
                if i==9:
                    for _ in range(200):  # "waiting for page 2" -- writers must already be busy
                        if len(list(tmp_path.rglob('*.strm')))>=5:break
                        await asyncio.sleep(0.01)
                    written_during_listing.append(len(list(tmp_path.rglob('*.strm'))))
            yield {'__listing_done__':True}
    eng.drive=StreamDrive()
    result=await eng.run();assert result['written']==20
    assert written_during_listing and written_during_listing[0]>=5

@pytest.mark.asyncio
async def test_streaming_same_name_conflict_bigger_wins_regardless_of_order(tmp_path):
    for order in (('small','big'),('big','small')):
        root=tmp_path/order[0];eng=engine_with(root)
        base={'parent_cid':'d','sha1':'x','mtime':'1'}
        small={**base,'id':'1','path':'片/片.mp4','name':'片.mp4','pickcode':'abcdef000001','size':10}
        big={**base,'id':'2','path':'片/片.mkv','name':'片.mkv','pickcode':'abcdef000002','size':999}
        items={'small':small,'big':big}
        class D(ScanDrive):
            async def scan(self,cid,cancel,bad_sink=None,skip_dirs=None,keep=None):
                yield {'__mode__':'stream'}
                for k in order:yield copy.deepcopy(items[k])
                yield {'__listing_done__':True}
        eng.drive=D()
        result=await eng.run();assert result['conflicts']==1
        text=(root/'115/片/片.strm').read_text()
        fid=__import__('hashlib').sha256((__import__('sync_engine').scope_key(eng.cfg.parsed_mappings[0])+':2').encode()).hexdigest()[:24]
        assert '/play/'+fid in text,'无论到达顺序，最终都应保留较大的文件：%r'%(order,)
        from test_sync import play_records
        recs=play_records(eng.store)
        assert [r['pickcode'] for r in recs]==['abcdef000002'],'被替换候选的播放记录必须清理：%r'%recs


@pytest.mark.asyncio
async def test_play_index_memory_shard_and_legacy_lookup():
    from sync_engine import PlayIndex
    store=Store();idx=PlayIndex(store)
    store.data['play:'+'b'*24]={'pickcode':'legacy000001','name':'旧'}
    idx.put('a'*24,{'pickcode':'new000000001','name':'新'})
    assert (await idx.get('a'*24))['pickcode']=='new000000001','未落盘也能立即播放（内存）'
    assert not any(k.startswith('playx:') for k in store.data)
    await idx.flush()
    assert not idx.pending and store.data[PlayIndex.shard('a'*24)]['a'*24]['pickcode']=='new000000001'
    assert (await idx.get('a'*24))['pickcode']=='new000000001'
    assert (await idx.get('b'*24))['pickcode']=='legacy000001','旧版 play: 记录仍可读'
    idx.drop('b'*24);await idx.flush()
    assert await idx.get('b'*24) is None and 'play:'+'b'*24 not in store.data

@pytest.mark.asyncio
async def test_first_sync_store_writes_are_batched(tmp_path):
    eng=engine_with(tmp_path)
    eng.drive.items=[{**eng.drive.items[0],'id':str(i),'path':'d%d/片%d.mkv'%(i,i),'name':'片%d.mkv'%i,'pickcode':'abcdef%06d'%i,'parent_cid':'d%d'%i} for i in range(400)]
    result=await eng.run();assert result['written']==400
    plays=[k for k in eng.store.sets if k.startswith(('play:','owned:'))]
    shards=[k for k in eng.store.sets if k.startswith('playx:')]
    assert plays==[],'不再逐文件写 play:/owned: 记录'
    assert len(shards)<=400 and len(set(shards))==len(shards),'每个分片每轮最多写一次'

@pytest.mark.asyncio
async def test_stop_persists_play_records_of_written_files(tmp_path):
    eng=engine_with(tmp_path)
    class Slow(ScanDrive):
        async def scan(self,cid,cancel,bad_sink=None,skip_dirs=None,keep=None):
            yield {'__mode__':'stream'}
            for it in self.items[:5]:yield copy.deepcopy(it)
            await asyncio.sleep(30)
    eng.drive=Slow()
    task=asyncio.create_task(eng.run())
    for _ in range(300):
        if len(list(tmp_path.rglob('*.strm')))==5:break
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):await task
    from test_sync import play_records
    assert len(play_records(eng.store))==5,'停止后已写出的 STRM 必须仍可播放'


@pytest.mark.asyncio
async def test_stopped_sync_keeps_progress_and_resume_skips_done_files(tmp_path):
    """2.7.4: 中途停止后进度已保存，再次同步对已完成文件走快速路径（不再逐个签名/重写）。"""
    eng=engine_with(tmp_path)
    eng.drive.items=[{**eng.drive.items[0],'id':str(i),'path':'d%d/片%d.mkv'%(i,i),'name':'片%d.mkv'%i,'pickcode':'abcdef%06d'%i,'parent_cid':'d%d'%i} for i in range(100)]
    items=eng.drive.items
    class Halting(ScanDrive):
        async def scan(self,cid,cancel,bad_sink=None,skip_dirs=None,keep=None):
            yield {'__mode__':'stream'}
            for it in items[:60]:yield copy.deepcopy(it)
            await asyncio.sleep(60)  # 卡在翻页中途
    eng.drive=Halting()
    task=asyncio.create_task(eng.run())
    for _ in range(500):
        if len(list(tmp_path.rglob('*.strm')))==60:break
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):await task
    meta=[v for k,v in eng.store.data.items() if k.endswith(':meta')][0]
    assert meta['clean'] is True and meta.get('partial'),'停止时必须保存进度'
    key=[k for k in eng.store.data if k.startswith('manifest:') and k.count(':')==1][0]
    assert len(await eng.load_manifest(key))==60
    # 继续同步：前 60 个走快速路径，只签名并写入新的 40 个
    eng.drive=ScanDrive();eng.drive.items=items
    eng.routes.calls.clear()
    result=await eng.run()
    assert result['unchanged']==60 and result['written']==40
    assert len(eng.routes.calls)==1+40,'已完成的文件不应再签名：%d'%len(eng.routes.calls)
    assert len(await eng.load_manifest(key))==100

@pytest.mark.asyncio
async def test_progress_checkpoints_during_run_and_appends_override(tmp_path):
    eng=engine_with(tmp_path);eng.PROGRESS_SAVE_FILES=25
    eng.drive.items=[{**eng.drive.items[0],'id':str(i),'path':'d/片%d.mkv'%i,'name':'片%d.mkv'%i,'pickcode':'abcdef%06d'%i,'parent_cid':'d'} for i in range(100)]
    await eng.run()
    key=[k for k in eng.store.data if k.startswith('manifest:') and k.count(':')==1][0]
    assert len(await eng.load_manifest(key))==100
    appended=[k for k in eng.store.sets if ':a:' in k]
    assert appended,'运行中应按批追加进度'
    # 追加节点按顺序覆盖旧记录
    await eng.append_manifest(key,{'d/片1.strm':{'id':'override'}})
    assert (await eng.load_manifest(key))['d/片1.strm']=={'id':'override'}


@pytest.mark.asyncio
async def test_files_from_a_stopped_run_without_progress_are_adopted_without_signing(tmp_path):
    """升级前（2.7.3）停止的同步没有保存进度：同一签发配置写出的文件按内容直接认领，不再逐个签名。"""
    eng=engine_with(tmp_path);await eng.run()
    key=[k for k in eng.store.data if k.startswith('manifest:') and k.count(':')==1][0]
    # 模拟「旧版停止后没留进度」：同步记录不存在，meta 只有运行开始时写下的 clean=False
    for k in [k for k in eng.store.data if k.startswith(key) and not k.endswith(':meta')]:del eng.store.data[k]
    eng.store.data[key+':meta']['clean']=False
    eng.routes.calls.clear()
    result=await eng.run()
    assert result['unchanged']==20 and result['written']==0
    assert len(eng.routes.calls)==1,'同配置写出的文件应按内容认领，不再签名：%d'%len(eng.routes.calls)
    assert len(await eng.load_manifest(key))==20
    # 认领后的下一轮走快速路径
    eng.routes.calls.clear();result=await eng.run()
    assert result['unchanged']==20 and len(eng.routes.calls)==1


def _side(i,path,size=7,sha=None):
    name=path.rsplit('/',1)[-1]
    return {'id':'s%d'%i,'path':path,'name':name,'pickcode':'side%06d'%i,'size':size,'sha1':sha or 'sha-%d'%i,'mtime':'1','parent_cid':'x'}

def _scraped_library():
    base={'sha1':'v','mtime':'1','size':100,'parent_cid':'x'}
    videos=[{**base,'id':'v1','path':'电影/阿凡达 (2009)/阿凡达.mkv','name':'阿凡达.mkv','pickcode':'abcdef000001'},
            {**base,'id':'v2','path':'剧集/某剧/Season 01/S01E01.mkv','name':'S01E01.mkv','pickcode':'abcdef000002'}]
    wanted=['电影/阿凡达 (2009)/阿凡达.nfo','电影/阿凡达 (2009)/阿凡达-poster.jpg','电影/阿凡达 (2009)/landscape.jpg',
            '电影/阿凡达 (2009)/clearlogo.png','电影/阿凡达 (2009)/extrafanart/fanart1.jpg','电影/阿凡达 (2009)/阿凡达.zh.ass',
            '剧集/某剧/tvshow.nfo','剧集/某剧/poster.jpg','剧集/某剧/season01-poster.jpg',
            '剧集/某剧/Season 01/season.nfo','剧集/某剧/Season 01/S01E01-thumb.jpg','剧集/某剧/Season 01/S01E01.nfo']
    unwanted=['相册/2020/IMG_0001.jpg','资料/说明.nfo']
    sides=[_side(i,p) for i,p in enumerate(wanted+unwanted)]
    return videos,sides,wanted,unwanted

@pytest.mark.asyncio
async def test_scraper_images_and_nfo_follow_videos_including_show_and_season_level(tmp_path,monkeypatch):
    """2.7.5: Emby/Jellyfin/Kodi 刮削产物（带后缀命名的图片、剧集/季级元数据、extrafanart）随视频一起同步；
    不含视频的目录（相册、资料）不同步。"""
    videos,sides,wanted,unwanted=_scraped_library()
    monkeypatch.setattr('sync_engine.net.http_transport',lambda _:httpx.MockTransport(lambda r:httpx.Response(200,content=b'artwork')))
    eng=setup(tmp_path)
    # 侧边文件故意排在视频之前到达（流式扫描中顺序任意）
    eng.drive.items=sides[:8]+videos[:1]+sides[8:]+videos[1:]
    result=await eng.run()
    for p in wanted:assert (tmp_path/'115'/p).read_bytes()==b'artwork',p
    for p in unwanted:assert not (tmp_path/'115'/p).exists(),p
    assert result['sidecars']==len(wanted)
    calls=len(eng.drive.link_calls)
    again=await eng.run()
    assert again['sidecars']==0 and len(eng.drive.link_calls)==calls,'第二轮附属文件未变化时不下载'

@pytest.mark.asyncio
async def test_sidecars_download_during_scan_and_survive_stop(tmp_path,monkeypatch):
    videos,sides,wanted,_=_scraped_library()
    monkeypatch.setattr('sync_engine.net.http_transport',lambda _:httpx.MockTransport(lambda r:httpx.Response(200,content=b'artwork')))
    eng=setup(tmp_path)
    class D:
        link_calls=[]
        async def link_signed(self,pc,ua=None,allow_cookie_url=False,fresh=False):
            self.link_calls.append(pc);return 'https://cdn.example.com/'+pc,'',{}
        async def scan(self,cid,cancel,bad_sink=None,skip_dirs=None,keep=None):
            yield {'__mode__':'stream'}
            for it in videos[:1]+sides[:6]:yield copy.deepcopy(it)
            await asyncio.sleep(60)  # 115 列表卡住 → 用户停止
    eng.drive=D()
    task=asyncio.create_task(eng.run())
    target=tmp_path/'115'/wanted[1]
    for _ in range(500):
        if target.exists():break
        await asyncio.sleep(0.01)
    assert target.exists(),'附属文件应在扫描进行中就下载，而不是等全部列完'
    task.cancel()
    with pytest.raises(asyncio.CancelledError):await task
    key=[k for k in eng.store.data if k.startswith('manifest:') and k.count(':')==1][0]
    recs=await eng.load_manifest(key)
    assert sum(1 for k in recs if k.startswith('§'))>=1,'停止时已下载的附属文件也要记进度'


@pytest.mark.asyncio
async def test_hard_exit_loses_at_most_seconds_and_play_records_self_heal(tmp_path):
    """2.7.10: 进程被直接杀掉（没有任何收尾）时，存储里留下的就是此刻的快照。"""
    eng=engine_with(tmp_path);eng.PROGRESS_SAVE_SECONDS=0.05;eng.PROGRESS_SAVE_FILES=10
    items=[{**eng.drive.items[0],'id':str(i),'path':'d%d/片%d.mkv'%(i,i),'name':'片%d.mkv'%i,'pickcode':'abcdef%06d'%i,'parent_cid':'d%d'%i} for i in range(80)]
    class Hanging(ScanDrive):
        async def scan(self,cid,cancel,bad_sink=None,skip_dirs=None,keep=None):
            yield {'__mode__':'stream'}
            for it in items[:50]:yield copy.deepcopy(it)
            await asyncio.sleep(60)
    eng.drive=Hanging()
    task=asyncio.create_task(eng.run())
    for _ in range(500):
        if len(list(tmp_path.rglob('*.strm')))==50:break
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.2)  # a couple of save ticks
    snapshot=copy.deepcopy(eng.store.data)  # == what a SIGKILL leaves behind
    task.cancel()
    with pytest.raises(asyncio.CancelledError):await task
    key=[k for k in snapshot if k.startswith('manifest:') and k.count(':')==1][0]
    # fresh process on the snapshot
    eng2=engine_with(tmp_path);eng2.store.data=snapshot
    from sync_engine import PlayIndex;eng2.play_index=PlayIndex(eng2.store)
    saved=await eng2.load_manifest(key)
    assert len(saved)>=40,'硬退出前的进度应已基本落盘：%d/50'%len(saved)
    # 播放：即使播放索引还没来得及落盘，也能从已保存的进度里找回
    import hashlib
    from sync_engine import scope_key
    fid=hashlib.sha256((scope_key(eng2.cfg.parsed_mappings[0])+':'+'3').encode()).hexdigest()[:24]
    rec=await eng2.play_fallback(fid)
    assert rec and rec['pickcode']=='abcdef000003'
    # 继续同步：已保存的直接跳过（不签名），只补剩下的
    eng2.drive=ScanDrive();eng2.drive.items=items
    eng2.routes.calls.clear()
    result=await eng2.run()
    assert result['written']+result['unchanged']==80
    assert len(eng2.routes.calls)<=1+(80-len(saved)),'已保存进度的文件不应再签名'
    from test_sync import play_records
    assert len(play_records(eng2.store))==80,'中断后的下一轮必须补齐全部播放记录'

@pytest.mark.asyncio
async def test_frequent_progress_saves_reuse_one_tail_node(tmp_path):
    eng=engine_with(tmp_path)
    key='manifest:test'
    eng.store.sets.clear()
    for i in range(50):
        await eng.append_manifest(key,{'f%d'%i:{'id':str(i)}})
    nodes=[k for k in eng.store.data if k.startswith(key+':a:')]
    assert len(nodes)==1,'频繁保存应复用同一个尾节点：%d'%len(nodes)
    assert eng.store.sets.count(key)==1,'根列表只在新建尾节点时写一次'
    assert len(await eng.load_manifest(key))==50
