"""2.8.0: next-episode direct-link prefetch."""
import asyncio
import json
import time
from base64 import b64encode
import httpx
import pytest
import drive_api as a
import p115_plugin as p
import p115_m115 as m115
from prefetch import Prefetcher, EpisodeIndex, natural_key
from test_sync import setup
from test_drive import _m115_read_request, _identity_rsa_seal


def _items():
    base={'size':100,'sha1':'h','mtime':'1','parent_cid':'x'}
    rows=[]
    eps=['剧/Season 01/S01E01.mkv','剧/Season 01/S01E02.mkv','剧/Season 01/S01E10.mkv','剧/Season 01/S01E09.mkv',
         '剧/Season 02/S02E01.mkv','剧/Season 02/S02E02.mkv','电影/阿凡达 (2009)/阿凡达.mkv','电影/泰坦尼克 (1997)/泰坦尼克.mkv']
    for i,path in enumerate(eps):
        rows.append({**base,'id':str(i+1),'path':path,'name':path.rsplit('/',1)[1],'pickcode':'pick%08d'%(i+1)})
    return rows

async def synced_engine(tmp_path):
    eng=setup(tmp_path);eng.drive.items=_items()
    await eng.run('full');return eng

def fid_of(eng,item_id):
    import hashlib
    from sync_engine import scope_key
    return hashlib.sha256((scope_key(eng.cfg.parsed_mappings[0])+':'+item_id).encode()).hexdigest()[:24]

def test_natural_sort():
    names=['S01E10.strm','S01E02.strm','S01E09.strm','S01E01.strm']
    assert sorted(names,key=natural_key)==['S01E01.strm','S01E02.strm','S01E09.strm','S01E10.strm']

@pytest.mark.asyncio
async def test_following_episodes_natural_order_and_next_season(tmp_path):
    eng=await synced_engine(tmp_path)
    idx=EpisodeIndex();await idx.build(eng)
    nxt=lambda item_id,n:[name for name,_,_ in idx.following(fid_of(eng,item_id),n)]
    assert nxt('2',2)==['S01E09.strm','S01E10.strm'],'自然排序：E02 之后是 E09、E10（不是 E10、E09）'
    assert nxt('3',2)==['S02E01.strm','S02E02.strm'],'季末接下一季'
    assert nxt('6',2)==[],'最后一集没有下一集'
    assert nxt('7',2)==[],'电影（文件夹里只有一个视频）不预取，也不会跳到别的电影文件夹'

class FakeDrive:
    def __init__(self):self.calls=[]
    async def prime_links(self,pcs,ua='',allow_cookie_url=True,min_remaining=0,use_expiry=False,cancel=None):
        self.calls.append((list(pcs),ua,allow_cookie_url,use_expiry));return len(pcs)

@pytest.mark.asyncio
async def test_prefetcher_batches_with_player_ua_and_debounces(tmp_path):
    eng=await synced_engine(tmp_path)
    cfg=p.Settings(prefetch_episodes=2);drive=FakeDrive();logs=[]
    async def log(m):logs.append(m)
    pf=Prefetcher(cfg,drive,eng,log)
    task=pf.schedule(fid_of(eng,'1'),'Infuse/8.0',False);await task
    assert drive.calls==[(['pick00000002','pick00000004'],'Infuse/8.0',False,True)],drive.calls
    assert logs and 'S01E02' in logs[0] and 'S01E09' in logs[0]
    assert pf.schedule(fid_of(eng,'1'),'Infuse/8.0',False) is None,'同一集同一播放器 10 分钟内不重复预取（拖动进度会反复请求 /play）'
    await pf.schedule(fid_of(eng,'1'),'',True)  # 中转模式是另一把缓存键
    assert drive.calls[-1][1:3]==('',True)
    cfg.prefetch_episodes=0
    assert pf.schedule(fid_of(eng,'2'),'x',False) is None,'设为 0 时关闭'

@pytest.mark.asyncio
async def test_prefetch_failure_never_raises(tmp_path):
    eng=await synced_engine(tmp_path)
    class Boom(FakeDrive):
        async def prime_links(self,*args,**kw):raise a.DriveError('115 拒绝')
    pf=Prefetcher(p.Settings(prefetch_episodes=2),Boom(),eng)
    assert await pf.schedule(fid_of(eng,'1'),'ua',False)==0

def _batch_answer(urls):
    def respond(r):
        key,payload=_m115_read_request(r)
        pcs=payload['pickcode'].split(',')
        result=json.dumps({str(i):{'pick_code':pc,'url':{'url':urls(pc)}} for i,pc in enumerate(pcs)}).encode()
        sk=m115.generate_key()
        body=m115._xor_transform(result,m115._xor_derive_key(key,4))[::-1]
        body=m115._xor_transform(body,m115._xor_derive_key(sk,12))
        return httpx.Response(200,json={'state':True,'data':b64encode(sk+body).decode()})
    return respond

@pytest.mark.asyncio
async def test_prime_links_with_player_ua_cached_until_link_expiry(monkeypatch):
    _identity_rsa_seal(monkeypatch)
    expiry=int(time.time())+3*3600
    seen_ua=[]
    inner=_batch_answer(lambda pc:'https://cdn.example.com/%s?t=%d&f=1'%(pc,expiry) if pc!='pick3pc3333' else 'https://cdn.example.com/x?f=3')
    def respond(r):seen_ua.append(r.headers.get('user-agent'));return inner(r)
    monkeypatch.setattr(a.net,'http_transport',lambda _:httpx.MockTransport(respond))
    cfg=p.Settings(cookie='UID=123_A1; CID=x; SEID=x',request_interval=.3,retry_max=0,link_cache_minutes=10)
    drive=a.Drive(cfg);drive.cfg.request_interval=0.0
    n=await drive.prime_links(['pick1pc1111','pick2pc2222','pick3pc3333'],ua='Infuse/8.0',allow_cookie_url=False,use_expiry=True)
    assert n==2,'302 模式下需要额外 Cookie 的 f=3 链接不能预取'
    assert seen_ua==['Infuse/8.0'],'必须用播放器 UA 签发（302 链接与 UA 绑定），且一次请求批量签发'
    hit=drive._links[('pick1pc1111','Infuse/8.0',False)]
    hit['at']-=3600  # 一小时后：早已超过「直链缓存时间」10 分钟
    url,ua,_=await drive.link_signed('pick1pc1111',ua='Infuse/8.0')
    assert url.startswith('https://cdn.example.com/pick1pc1111') and len(seen_ua)==1,'按链接自身有效期缓存：一小时后下一集仍命中，不再签发'
    # 再次预取：剩余有效期充足 → 不重复签发
    assert await drive.prime_links(['pick1pc1111'],ua='Infuse/8.0',allow_cookie_url=False,min_remaining=1200,use_expiry=True)==0

def test_link_expiry_parsing():
    soon=a.Drive.link_expiry('https://cdn/x?t=%d'%(int(time.time())+200))
    assert soon is None,'剩余不足（扣除 5 分钟余量）时不按有效期缓存'
    later=a.Drive.link_expiry('https://cdn/x?t=%d'%(int(time.time())+7200))
    assert later and 6000<later-time.monotonic()<7000
    assert a.Drive.link_expiry('https://cdn/x?f=1') is None
