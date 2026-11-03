import asyncio
import json
from types import SimpleNamespace
from urllib.parse import parse_qs
from base64 import b64decode, b64encode
import httpx
import pytest
import drive_api as a
import p115_plugin as p
from p115_ecdh import aes_cbc_encrypt, AES_KEY, AES_IV
import p115_m115 as m115


def _m115_read_request(request):
    """Simulated 115 reading a m115 signing request: recover the client key
    from the sealed prefix and undo the xor chain. Requires the RSA seal to be
    an identity stand-in (monkeypatched in the calling test)."""
    raw = b64decode(parse_qs(request.content.decode())['data'][0])
    key = raw[:16]
    body = m115._xor_transform(raw[16:], m115.XOR_CLIENT_KEY)[::-1]
    return key, json.loads(m115._xor_transform(body, m115._xor_derive_key(key, 4)))


def _m115_answer(key, url='https://cdn.example.com/movie.mkv?f=1'):
    """Simulated 115 sealing a m115 response for the client's request key."""
    result = json.dumps({'abcdef123456': {'url': {'url': url}}}).encode()
    server_key = m115.generate_key()
    body = m115._xor_transform(result, m115._xor_derive_key(key, 4))[::-1]
    body = m115._xor_transform(body, m115._xor_derive_key(server_key, 12))
    return httpx.Response(200, json={'state': True, 'data': b64encode(server_key + body).decode()})


def _identity_rsa_seal(monkeypatch):
    monkeypatch.setattr(m115, '_rsa_encrypt', lambda d: d)
    monkeypatch.setattr(m115, '_rsa_decrypt', lambda d: d)


def _ecdh_envelope(obj):
 """Build a REAL p115 response envelope: p115-lz4 (2-byte block len + lz4
 block) then AES-CBC with the fixed ECDH key/iv -- exactly what 115 returns
 for downurl. sign_once must decode this with ecdh_aes_decrypt (1.7.3 fix)."""
 data=json.dumps(obj).encode()
 n=len(data)
 if n<15:block=bytes([n<<4])+data
 else:
  rem=n-15;cont=bytearray()
  while True:
   if rem<255:cont.append(rem);break
   cont.append(255);rem-=255
  block=bytes([15<<4])+bytes(cont)+data
 return bytes(aes_cbc_encrypt(len(block).to_bytes(2,'little')+block,AES_KEY,AES_IV))


def cfg(**values):
 # 2.4.0：默认不重试，让既有「立即失败」的断言保持即时语义（不会被真实冷却拖慢）；
 # 退避行为由 test_throttle_* 单独用显式 retry_max/retry_cooldown 覆盖。
 base=dict(cookie='UID=123_A1; CID=secret; SEID=secret',request_interval=.3,retry_max=0)
 base.update(values);return p.Settings(**base)

@pytest.mark.parametrize('row,expected',[
 ({'cid':'3','n':'目录'},('3',True)),({'fid':'4','cid':'3','n':'影片.mkv','s':'12','pc':'abcdef123456'},('4',False))])
def test_normalize(row,expected):
 item=a.normalize(row);assert (item['id'],item['is_dir'])==expected

@pytest.mark.parametrize('name',['../file','/absolute','..','.','a\\b','a\x00b'])
def test_bad_names(name):
 with pytest.raises(a.DriveError):a.normalize({'fid':'4','n':name})

@pytest.mark.parametrize('value',['file:///etc/passwd','javascript:x','https://user:pass@example.com/x','https://example.com/\r\nx'])
def test_bad_links(value):
 with pytest.raises(a.DriveError):a.safe_url(value)

@pytest.mark.asyncio
async def test_listing_pagination_and_folder_guard(monkeypatch):
 def respond(r):
  assert r.headers['cookie'].startswith('UID=123');assert r.url.params['cur']=='1'
  if r.url.params['cid']=='99':return httpx.Response(200,json={'state':True,'cid':'0','data':[],'count':0})
  return httpx.Response(200,json={'state':True,'path':[{'cid':'0'},{'cid':'2'}],'data':[{'fid':'1','n':'电影.mkv','pc':'abcdef123456','s':'100'}],'count':1})
 monkeypatch.setattr(a.net,'http_transport',lambda _:httpx.MockTransport(respond))
 client=a.Drive(cfg());result=await client.list('2');assert result['items'][0]['size']==100
 with pytest.raises(a.DriveError,match='目录与请求'):await client.list('99')

@pytest.mark.asyncio
async def test_api_failures_redacted(monkeypatch):
 def fail(r):raise httpx.ConnectError('Cookie secret '+str(r.url),request=r)
 monkeypatch.setattr(a.net,'http_transport',lambda _:httpx.MockTransport(fail))
 with pytest.raises(a.DriveError) as err:await a.Drive(cfg()).list('0')
 assert 'secret' not in str(err.value) and 'http' not in str(err.value)

# ---------- 2.4.0：撞到风控自动等待后继续（不再让整次同步失败） ----------
def _ok_page(count=1,cid='2'):
 return httpx.Response(200,json={'state':True,'path':[{'cid':cid}],'data':[{'fid':'1','n':'电影.mkv','pc':'abcdef123456','s':'100'}],'count':count})

def _fake_sleep(bucket):
 """记录等待时长而非真的睡（测试要秒级完成，不能用真实冷却）。"""
 async def sleep(self,seconds):bucket.append(seconds)
 return sleep

@pytest.mark.asyncio
async def test_throttle_waits_then_continues(monkeypatch):
 # 115 连着两次返回「操作太频繁」：等 30s、60s 后第三次成功，list 正常返回 ——
 # 现场对应的正是「扫到一半被限速，以前整次同步报废、下次从头再来」。
 calls={'n':0}
 def respond(r):
  calls['n']+=1
  if calls['n']<=2:return httpx.Response(200,json={'state':False,'errno':590075})
  return _ok_page()
 monkeypatch.setattr(a.net,'http_transport',lambda _:httpx.MockTransport(respond))
 waited=[]
 client=a.Drive(cfg(retry_max=3,retry_cooldown=30))
 monkeypatch.setattr(a.Drive,'sleep',_fake_sleep(waited))
 result=await client.list('2')
 assert result['items'] and calls['n']==3,'应在第 3 次尝试恢复'
 cooldowns=[w for w in waited if w>1]
 assert len(cooldowns)==2,'两次限流各有一次冷却等待'
 assert 27<=cooldowns[0]<=33 and 54<=cooldowns[1]<=66,'退避应为 30s 基础并翻倍（±10% 抖动）'

@pytest.mark.asyncio
async def test_throttle_retry_exhausted_message(monkeypatch):
 def respond(r):return httpx.Response(200,json={'state':False,'errno':590075})
 monkeypatch.setattr(a.net,'http_transport',lambda _:httpx.MockTransport(respond))
 waited=[]
 client=a.Drive(cfg(retry_max=2,retry_cooldown=5))
 monkeypatch.setattr(a.Drive,'sleep',_fake_sleep(waited))
 with pytest.raises(a.DriveError) as err:await client.list('2')
 message=str(err.value)
 assert '操作太频繁' in message and '已等待重试 2 次' in message,'失败消息要说明已重试次数并给出可操作建议'
 assert len([w for w in waited if w>1])==2,'耗尽前的等待次数应与 retry_max 一致'

@pytest.mark.asyncio
async def test_login_expiry_is_not_retried(monkeypatch):
 # 凭据失效属于「必须立刻告诉用户」的错误：等半天再报只会让人以为插件卡死。
 calls={'n':0}
 def respond(r):
  calls['n']+=1;return httpx.Response(200,json={'state':False,'errno':990001})
 monkeypatch.setattr(a.net,'http_transport',lambda _:httpx.MockTransport(respond))
 waited=[]
 client=a.Drive(cfg(retry_max=4,retry_cooldown=5))
 monkeypatch.setattr(a.Drive,'sleep',_fake_sleep(waited))
 with pytest.raises(a.DriveError,match='登陆已超时'):await client.list('2')
 assert calls['n']==1 and not waited,'登录态错误不得进入退避重试'

@pytest.mark.asyncio
async def test_server_status_retry_matrix(monkeypatch):
 for status,retried in ((503,True),(429,True),(405,True),(403,False)):  # 2.8.3: 405 = 115 风控
  calls={'n':0}
  def respond(r,status=status):
   calls['n']+=1
   # 第一次撞上限流状态，之后恢复——验证「等一会儿就地继续」而不是整次失败。
   return httpx.Response(status,json={'state':False,'errno':1}) if calls['n']==1 else _ok_page()
  monkeypatch.setattr(a.net,'http_transport',lambda _,r=respond:httpx.MockTransport(r))
  waited=[]
  client=a.Drive(cfg(retry_max=1,retry_cooldown=5))
  monkeypatch.setattr(a.Drive,'sleep',_fake_sleep(waited))
  if retried:
   assert (await client.list('2'))['items'],'HTTP %d 属于可重试，应在冷却后恢复'%status
   assert len([w for w in waited if w>1])==1
  else:
   with pytest.raises(a.DriveError,match='HTTP 403'):await client.list('2')
   assert calls['n']==1 and not waited,'HTTP %d 不是「等一等就好」，不得重试'%status

@pytest.mark.asyncio
async def test_cooldown_can_be_interrupted_by_stop(monkeypatch):
 # 停止同步必须能打断冷却（最长可到 600s），否则点停止后要干等好几分钟。
 def respond(r):return httpx.Response(200,json={'state':False,'errno':590075})
 monkeypatch.setattr(a.net,'http_transport',lambda _:httpx.MockTransport(respond))
 client=a.Drive(cfg(retry_max=3,retry_cooldown=600));client.cancel_check=lambda:True
 with pytest.raises(asyncio.CancelledError):await client.list('2')

@pytest.mark.asyncio
async def test_cooldown_never_exceeds_cap(monkeypatch):
 client=a.Drive(cfg(retry_max=4,retry_cooldown=600))
 delays=[client.cooldown_delay(i,600) for i in range(1,5)]
 assert all(540.<=d<=600. for d in delays),'基础 600s 时每次都应贴着 600s 上限（±10% 抖动），且绝不越过：'+repr(delays)
 ladder=[client.cooldown_delay(i,30) for i in range(1,4)]
 assert all(27<=ladder[0]<=33 and 54<=ladder[1]<=66 and 108<=ladder[2]<=132 for _ in [0]),'默认 30s 基础时长应逐级翻倍'

@pytest.mark.asyncio
async def test_download_wire_signs_empty_user_agent(monkeypatch):
 # RSA 老协议（p115client download_url_app）的 wire：空 UA + RSA 加密的
 # {pickcode, user_id}；m115 协议的 wire 由 test_sign_uses_m115_first 覆盖。
 captured=[]
 def respond(r):
  captured.append(r);return httpx.Response(200,json={'state':True,'data':'encrypted'})
 monkeypatch.setattr(a.net,'http_transport',lambda _:httpx.MockTransport(respond))
 monkeypatch.setattr(a,'rsa_decrypt',lambda _:json.dumps({'1':{'url':{'url':'https://cdn.example.com/movie.mkv?f=1'}}}).encode())
 assert 'f=1' in (await a.Drive(cfg()).sign_once('abcdef123456'))[0]
 assert captured[0].headers['user-agent']=='' and str(captured[0].url)=='https://proapi.115.com/app/chrome/downurl'
 payload=parse_qs(captured[0].content.decode());assert len(payload['data'][0])>=172 and 'secret' not in payload['data'][0]

@pytest.mark.asyncio
async def test_download_cookie_constraint(monkeypatch):
 # 302 直连不能附加 Cookie：f=3 直链必须在 302 路径上被拒绝（多策略也全拒绝）
 monkeypatch.setattr(a.net,'http_transport',lambda _:httpx.MockTransport(lambda r:httpx.Response(200,json={'state':True,'data':'encrypted'})))
 monkeypatch.setattr(a,'rsa_decrypt',lambda _:b'{"1":{"url":{"url":"https://cdn.example.com/movie?f=3"}}}')
 with pytest.raises(a.DriveError,match='额外 Cookie'):await a.Drive(cfg()).link_signed('abcdef123456')

@pytest.mark.asyncio
async def test_link_accepts_f3_for_internal_fetch(monkeypatch):
 # 服务器侧拉取（proxy 播放/附属下载）可以附加账号 Cookie，f=3 直链放行
 monkeypatch.setattr(a.net,'http_transport',lambda _:httpx.MockTransport(lambda r:httpx.Response(200,json={'state':True,'data':'encrypted'})))
 monkeypatch.setattr(a,'rsa_decrypt',lambda _:b'{"1":{"url":{"url":"https://cdn.example.com/movie?f=3"}}}')
 assert 'f=3' in await a.Drive(cfg()).link('abcdef123456')

@pytest.mark.asyncio
async def test_link_falls_back_to_requester_ua_when_empty_rejected(monkeypatch):
 # 115 拒绝的组合（本 mock：空 UA 签发、m115 解封）逐一被跳过，最终按请求方 UA
 # 用可用协议签出 —— alist/OpenList Link() 的生产模型（302 必须绑请求方 UA）。
 def respond(r):
  if r.headers['user-agent']=='':return httpx.Response(200,json={'state':False,'errno':990001})
  return httpx.Response(200,json={'state':True,'data':'encrypted'})
 monkeypatch.setattr(a.net,'http_transport',lambda _:httpx.MockTransport(respond))
 monkeypatch.setattr(a,'rsa_decrypt',lambda _:json.dumps({'1':{'url':{'url':'https://cdn.example.com/movie.mkv?f=1'}}}).encode())
 url,used,_=await a.Drive(cfg()).link_signed('abcdef123456',ua='Infuse/8.0')
 assert url.startswith('https://cdn.example.com/') and used=='Infuse/8.0'

@pytest.mark.asyncio
async def test_memo_same_ua_shortens_later_signings(monkeypatch):
 # 1.7.4：签发成功的 (协议, UA) 组合被记忆，但记忆按 UA 匹配——换一个播放器 UA
 # 绝不能复用旧组合，否则签出的直链绑定到别的播放器的 UA，该播放器拉流必 403。
 # （此 mock 只会 RSA 形态应答，所以第一次走 m115·Infuse 失败 → RSA·Infuse 成功。）
 calls=[]
 def respond(r):
  calls.append(r);return httpx.Response(200,json={'state':True,'data':'encrypted'})
 monkeypatch.setattr(a.net,'http_transport',lambda _:httpx.MockTransport(respond))
 monkeypatch.setattr(a,'rsa_decrypt',lambda _:json.dumps({'1':{'url':{'url':'https://cdn.example.com/movie.mkv?f=1'}}}).encode())
 drive=a.Drive(cfg())
 url,used,_=await drive.link_signed('abcdef123456',ua='Infuse/8.0')
 assert used=='Infuse/8.0' and drive._ok==('rsa','Infuse/8.0')
 first=len(calls)
 url,used,_=await drive.link_signed('abcdef654321',ua='Infuse/8.0')
 assert len(calls)==first+1 and calls[-1].headers['user-agent']=='Infuse/8.0','同 UA 第二次签发应复用记忆只发一次请求'
 # 换 UA（另一个播放器）必须重新走链、按新 UA 签发
 url,used,_=await drive.link_signed('abcdef777777',ua='VLC/3.0')
 assert used=='VLC/3.0' and calls[-1].headers['user-agent']=='VLC/3.0','换播放器 UA 不得复用旧组合签发'

@pytest.mark.asyncio
async def test_download_url_field_false_gives_clear_error(monkeypatch):
 # 115 有时在 url 字段返回 false 而不是对象（115driver info.go 2026-07 的兼容修复）
 monkeypatch.setattr(a.net,'http_transport',lambda _:httpx.MockTransport(lambda r:httpx.Response(200,json={'state':True,'data':'encrypted'})))
 monkeypatch.setattr(a,'rsa_decrypt',lambda _:b'{"1":{"url":false}}')
 with pytest.raises(a.DriveError,match='未返回此文件的下载地址'):await a.Drive(cfg()).link('abcdef123456')

@pytest.mark.asyncio
async def test_sign_decodes_ecdh_envelope_response(monkeypatch):
 # 1.7.3 核心回归：真机上 downurl 的响应体是 ECDH 信封（AES-CBC 固定密钥 + p115 lz4），
 # 不是明文 JSON——p115client 的 parse 是 json_maybe_decrypt_loads。1.7.1/1.7.2 直接
 # r.json() 导致所有签发都报「解密或解析失败」。用真实加密信封验证全链路解密。
 monkeypatch.setattr(a.net,'http_transport',lambda _:httpx.MockTransport(lambda r:httpx.Response(200,content=_ecdh_envelope({'state':True,'data':'encrypted'}))))
 monkeypatch.setattr(a,'rsa_decrypt',lambda _:json.dumps({'1':{'url':{'url':'https://cdn.example.com/movie.mkv?f=1'}}}).encode())
 url,used,_=await a.Drive(cfg()).sign_once('abcdef123456')
 assert 'f=1' in url and used==''

@pytest.mark.asyncio
async def test_sign_envelope_errno_gets_hint(monkeypatch):
 # 信封内 state=False：errno 透传 + 释义（凭据失效场景）
 monkeypatch.setattr(a.net,'http_transport',lambda _:httpx.MockTransport(lambda r:httpx.Response(200,content=_ecdh_envelope({'state':False,'errno':990001,'error':'no login'}))))
 with pytest.raises(a.DriveError,match='挤下线'):await a.Drive(cfg()).sign_once('abcdef123456')

@pytest.mark.asyncio
async def test_sign_empty_data_gives_clear_error(monkeypatch):
 # p115client: state=true 但 data 为空 → 115 已签发但未给 URL（删除/转码中/受限）
 monkeypatch.setattr(a.net,'http_transport',lambda _:httpx.MockTransport(lambda r:httpx.Response(200,content=_ecdh_envelope({'state':True,'data':''}))))
 with pytest.raises(a.DriveError,match='未返回此文件的下载地址'):await a.Drive(cfg()).sign_once('abcdef123456')

@pytest.mark.asyncio
async def test_sign_undecodable_body_reports_first_byte(monkeypatch):
 # 解密失败必须带上长度+首字节等现场信息，方便用户回报精确定位
 monkeypatch.setattr(a.net,'http_transport',lambda _:httpx.MockTransport(lambda r:httpx.Response(200,content=b'\x99garbage-not-cipher')))
 with pytest.raises(a.DriveError,match='0x99'):await a.Drive(cfg()).sign_once('abcdef123456')

@pytest.mark.asyncio
async def test_sign_uses_m115_first(monkeypatch):
 # 1.7.3/1.7.4：签发首选 m115 协议（alist/OpenList DownloadWithUA 同款）；302 场景按
 # 请求方 UA 签发（alist Link() 模型——真机诊断证明 f=1 直链绑定签发 UA，播放器只能
 # 拉自己 UA 签出的直链）。模拟服务器验证请求 wire（t 参数、紧凑 pickcode payload）
 # 与响应解封全链路，成功后策略链不再尝试其他候选。
 _identity_rsa_seal(monkeypatch)
 calls=[]
 def respond(r):
  calls.append(r)
  assert 't' in r.url.params and r.url.params['t'].isdigit(),'m115 请求应带毫秒时间戳（115driver download.go）'
  key,payload=_m115_read_request(r)
  assert payload=={'pickcode':'abcdef123456'},'m115 payload 应为紧凑 {"pickcode":...}'
  return _m115_answer(key)
 monkeypatch.setattr(a.net,'http_transport',lambda _:httpx.MockTransport(respond))
 drive=a.Drive(cfg())
 url,used,_=await drive.link_signed('abcdef123456',ua='Infuse/8.0')
 assert 'f=1' in url and used=='Infuse/8.0' and len(calls)==1,'m115·请求方 UA 应是 302 的第一候选'
 assert drive._ok==('m115','Infuse/8.0')

@pytest.mark.asyncio
async def test_m115_rejected_falls_back_to_rsa(monkeypatch):
 # m115 全部候选被拒（errno 透传带释义）时回退到 RSA 老协议（p115client）
 _identity_rsa_seal(monkeypatch)
 monkeypatch.setattr(a.net,'http_transport',lambda _:httpx.MockTransport(lambda r:httpx.Response(200,json={'state':False,'errno':990001,'error':'no login'}) if 't' in r.url.params else httpx.Response(200,json={'state':True,'data':'encrypted'})))
 monkeypatch.setattr(a,'rsa_decrypt',lambda _:json.dumps({'1':{'url':{'url':'https://cdn.example.com/movie.mkv?f=1'}}}).encode())
 drive=a.Drive(cfg())
 url,used,_=await drive.link_signed('abcdef123456',ua='Infuse/8.0')
 assert 'f=1' in url and used=='Infuse/8.0' and drive._ok==('rsa','Infuse/8.0'),'应在请求方 UA 上回退到 RSA 协议签发'

@pytest.mark.asyncio
async def test_sign_extracts_url_header_for_cdn_fetch(monkeypatch):
 # 1.7.4：115driver DownloadUrl 解析 url.header（http.Header），OpenList Link() 把它回传
 # 给消费端——CDN 通常要求这组头。我们的签发必须把它提取出来供代理/附属下载重放。
 monkeypatch.setattr(a.net,'http_transport',lambda _:httpx.MockTransport(lambda r:httpx.Response(200,json={'state':True,'data':'encrypted'})))
 monkeypatch.setattr(a,'rsa_decrypt',lambda _:json.dumps({'1':{'url':{'url':'https://cdn.example.com/movie.mkv?f=1','header':{'User-Agent':'UA-X','Cookie':'cdnk=v'}}}}).encode())
 drive=a.Drive(cfg())
 url,ua,hdrs=await drive.sign_once('abcdef123456',ua='')
 assert hdrs=={'User-Agent':'UA-X','Cookie':'cdnk=v'},'url.header 应被提取'
 url,ua,hdrs=await drive.link_signed('abcdef123456',ua='Infuse/8.0')
 assert hdrs=={'User-Agent':'UA-X','Cookie':'cdnk=v'},'策略链应透传 url.header'
 # 非字符串的脏值必须被过滤，不能进入发往 CDN 的头
 monkeypatch.setattr(a,'rsa_decrypt',lambda _:json.dumps({'1':{'url':{'url':'https://cdn.example.com/movie.mkv?f=1','header':{'User-Agent':123,'x-ok':'v'}}}}).encode())
 url,ua,hdrs=await drive.sign_once('abcdef123456',ua='')
 assert hdrs=={'x-ok':'v'},'header 里的非字符串值应被丢弃'

@pytest.mark.asyncio
async def test_link_cache_reuses_and_invalidates(monkeypatch):
 # 1.7.5：签发的直链按 (pickcode, UA, 用途) 缓存——播放器读 MKV 索引/字幕轨时对
 # 同一文件连发多个请求，每个都重新签发要过全局 API 间隔（每次 1~3 秒），正是
 # 字幕加载慢的根因；缓存命中后这些请求零 115 往返。f=1 与 f=3（allow_cookie_url）
 # 的直链键不同，绝不互相串用。
 calls=[]
 def respond(r):
  calls.append(r);return httpx.Response(200,json={'state':True,'data':'encrypted'})
 monkeypatch.setattr(a.net,'http_transport',lambda _:httpx.MockTransport(respond))
 monkeypatch.setattr(a,'rsa_decrypt',lambda _:json.dumps({'1':{'url':{'url':'https://cdn.example.com/movie.mkv?f=1'}}}).encode())
 drive=a.Drive(cfg())
 url,used,_=await drive.link_signed('abcdef123456',ua='Infuse/8.0')
 signed=len(calls);assert signed>=1
 url2,used2,_=await drive.link_signed('abcdef123456',ua='Infuse/8.0')
 assert url2==url and len(calls)==signed,'同 pickcode+UA 应命中缓存，不再请求'
 assert drive.last_sign['cached'] and drive.last_sign['proto']
 # 缓存按 UA 区分：换播放器必须重新签发（f=1 直链绑定签发 UA）
 await drive.link_signed('abcdef123456',ua='VLC/3.0')
 assert len(calls)>signed
 # fresh=True（诊断/重签路径）绕过缓存强制重签
 base=len(calls)
 await drive.link_signed('abcdef123456',ua='Infuse/8.0',fresh=True)
 assert len(calls)>base and not drive.last_sign['cached']
 # 缓存过期后重新签发
 drive._links[('abcdef123456','Infuse/8.0',False)]['at']-=7200
 base=len(calls)
 await drive.link_signed('abcdef123456',ua='Infuse/8.0')
 assert len(calls)>base,'过期缓存必须重新签发'
 # invalidate_links（CDN 拒绝后的重签入口）清掉该文件全部缓存
 drive.invalidate_links('abcdef123456')
 assert not [k for k in drive._links if k[0]=='abcdef123456']
 base=len(calls)
 await drive.link_signed('abcdef123456',ua='Infuse/8.0')
 assert len(calls)>base
 # 用途维度隔离：allow_cookie_url 的 f=3 缓存不得回给 302 路径
 assert ('abcdef123456','',False) not in drive._links

@pytest.mark.asyncio
async def test_link_cache_rejected_by_cdn_invalidates_and_resigns(monkeypatch):
 # 1.7.5：中转拉流遇到 401/403/410 视为直链过期——缓存必须可被失效并重签一次，
 # 不能让播放器在死链上循环（此测试验证 drive 侧的失效原语与重签可用性）。
 state={'fail':False}
 def respond(r):
  if r.url.host=='cdn.example.com':return httpx.Response(403) if state['fail'] else httpx.Response(200,content=b'x')
  return httpx.Response(200,json={'state':True,'data':'encrypted'})
 monkeypatch.setattr(a.net,'http_transport',lambda _:httpx.MockTransport(respond))
 monkeypatch.setattr(a,'rsa_decrypt',lambda _:json.dumps({'1':{'url':{'url':'https://cdn.example.com/movie.mkv?f=1'}}}).encode())
 drive=a.Drive(cfg())
 await drive.link_signed('abcdef123456',ua='')
 assert drive._links
 drive.invalidate_links('abcdef123456')
 assert not drive._links,'CDN 拒绝后缓存必须整体失效'
 url,_,_=await drive.link_signed('abcdef123456',ua='',fresh=True)
 assert url.startswith('https://cdn.example.com/'),'重签应拿到新直链'


@pytest.mark.asyncio
async def test_walk_nested_and_inconsistent_pages():
 client=a.Drive(cfg());pages={'0':[{'id':'2','name':'folder','is_dir':True}], '2':[{'id':'3','name':'video.mkv','is_dir':False}]}
 async def listing(cid,offset=0):return {'count':len(pages[cid]),'items':pages[cid],'bad':[],'raw_len':len(pages[cid])}
 client.list=listing
 found=[item async for item in client.walk('0')]
 files=[x for x in found if '__end_dir__' not in x]
 ends=[x for x in found if '__end_dir__' in x]
 assert [f['path'] for f in files]==['folder/video.mkv']
 assert files[0]['parent_cid']=='2','文件必须携带所在目录 cid（断点续传计数依赖它）'
 # dir_end 哨兵：root(0) 先结束、folder(2) 后结束，folder 的 parent 指向 root
 assert [e['cid'] for e in ends]==['0','2']
 assert ends[0]['parent_cid'] is None and ends[1]['parent_cid']=='0'
 async def missing(cid,offset=0):return {'count':2,'items':[],'bad':[],'raw_len':0}
 client.list=missing
 with pytest.raises(a.DriveError,match='提前结束'):[item async for item in client.walk('0')]

@pytest.mark.asyncio
async def test_walk_skip_dirs_skips_whole_subtree():
 # 2.6.0 断点续传：skip_dirs 里的目录整棵子树跳过（不发文件也不发 dir_end 哨兵）
 client=a.Drive(cfg())
 pages={'0':[{'id':'2','name':'done','is_dir':True},{'id':'4','name':'todo','is_dir':True}],
        '2':[{'id':'3','name':'old.mkv','is_dir':False}],
        '4':[{'id':'5','name':'new.mkv','is_dir':False}]}
 async def listing(cid,offset=0):return {'count':len(pages[cid]),'items':pages[cid],'bad':[],'raw_len':len(pages[cid])}
 client.list=listing
 found=[item async for item in client.walk('0',skip_dirs={'2'})]
 files=[x for x in found if '__end_dir__' not in x]
 ends=[x for x in found if '__end_dir__' in x]
 assert [f['path'] for f in files]==['todo/new.mkv'],'被跳过目录的文件不得出现'
 assert '2' not in [e['cid'] for e in ends],'被跳过目录不应发 dir_end 哨兵'
 assert '2' not in [f.get('parent_cid') for f in files]

@pytest.mark.parametrize('name',['upload_mappings','auto_sync','sync_interval','cleanup_missing',
 'organized_directory','auto_upload','watch_interval','stable_seconds','upload_excludes'])
def test_removed_settings_absent(name):
 from movieclaw_sdk.config_schema import config_schema
 assert name not in config_schema(p.Settings)['properties']
 assert not hasattr(p.Settings(),name)

def test_sdk_ui_supported_and_defaults():
 from movieclaw_sdk.config_schema import config_schema
 schema=config_schema(p.Settings)
 assert schema['properties']['cookie']['writeOnly']
 assert not p.Settings().allow_cloud_changes and not p.Settings().strm_cleanup

@pytest.mark.asyncio
async def test_mutation_protocol(monkeypatch):
 requests=[]
 monkeypatch.setattr(a.net,'http_transport',lambda _:httpx.MockTransport(lambda r:(requests.append(r),httpx.Response(200,json={'state':True}))[1]))
 client=a.Drive(cfg())
 await client.mutate('rename','4','0','新名字.mkv')
 assert requests[0].url.path=='/files/batch_rename'
 assert parse_qs(requests[0].content.decode())=={'files_new_name[4]':['新名字.mkv']}


def test_malformed_file_row():
 with pytest.raises(a.DriveError):a.normalize(None)

def test_normalize_empty_fid_is_directory():
 # 2.2.1：目录判定对齐 115driver 生产契约（File.From）——fid 为空串也是目录，
 # 不再因 fid 键存在而误判为文件后炸「格式不合规」
 item=a.normalize({'fid':'','cid':'77','n':'目录'})
 assert item['is_dir'] and item['id']=='77'

def test_normalize_precise_reasons():
 # 2.2.1：失败原因必须精确到字段，供运行日志定位是哪个网盘条目出的问题
 for row,reason in [({'fid':'x1','n':'a.mkv'},'ID 非数字'),({'fid':'5','cid':'9','n':''},'名称'),({'fid':'5','cid':'9'},'缺少字段')]:
  with pytest.raises(a.DriveError,match=reason):a.normalize(row)

@pytest.mark.asyncio
async def test_list_skips_bad_entries_with_reason(monkeypatch):
 # 2.2.1：单条异常降级为跳过+诊断——好文件照常返回，坏条目带原因与字段详情
 client=a.Drive(cfg())
 page={'state':True,'cid':'9','count':3,'data':[
  {'fid':'x','cid':'9','n':'坏ID.mkv','s':5,'pc':'p'},
  {'fid':'22','cid':'9','n':'好文件.mkv','s':9,'pc':'p'},
  'garbage-row']}
 async def fake(method,url,**kw):return page
 monkeypatch.setattr(client,'request',fake)
 result=await client.list('9')
 assert [x['name'] for x in result['items']]==['好文件.mkv']
 assert result['raw_len']==3 and len(result['bad'])==2
 assert result['bad'][0]['index']==0 and 'ID 非数字' in result['bad'][0]['reason']
 assert '非对象条目' in result['bad'][1]['detail']

@pytest.mark.asyncio
async def test_list_all_bad_page_is_fatal(monkeypatch):
 # 整页全坏=115 系统性更改了响应格式：必须中止并带首条诊断，
 # 不能“成功”同步出一个空库
 client=a.Drive(cfg())
 async def fake(method,url,**kw):return {'state':True,'cid':'9','count':1,'data':[{'fid':'5','cid':'9'}]}
 monkeypatch.setattr(client,'request',fake)
 with pytest.raises(a.DriveError,match='格式不合规'):
  await client.list('9')

@pytest.mark.asyncio
async def test_walk_propagates_bad_entries(monkeypatch):
 # 跳过的条目经 bad_sink 上抛（带目录上下文），分页按原始页长推进不错位
 client=a.Drive(cfg())
 page={'state':True,'cid':'0','count':2,'data':[
  {'fid':'22','cid':'0','n':'视频.mkv','s':9,'pc':'p'},
  {'fid':'23','cid':'0','n':'坏\\名字.mkv','s':5,'pc':'p'}]}
 async def fake(method,url,**kw):return page
 monkeypatch.setattr(client,'request',fake)
 bad=[]
 found=[item async for item in client.walk('0',bad_sink=bad)]
 files=[x for x in found if '__end_dir__' not in x]
 assert [x['name'] for x in files]==['视频.mkv']
 assert len(bad)==1 and bad[0]['dir']=='/' and '名称' in bad[0]['reason']

@pytest.mark.asyncio
async def test_errno_hints_in_error_message(monkeypatch):
 monkeypatch.setattr(a.net,'http_transport',lambda _:httpx.MockTransport(lambda r:httpx.Response(200,json={'state':False,'errno':990001})))
 with pytest.raises(a.DriveError,match='挤下线'):await a.Drive(cfg()).account()
 monkeypatch.setattr(a.net,'http_transport',lambda _:httpx.MockTransport(lambda r:httpx.Response(200,json={'state':False,'errno':'590075'})))
 with pytest.raises(a.DriveError,match='风控'):await a.Drive(cfg()).account()

@pytest.mark.asyncio
async def test_account_space_from_index_info(monkeypatch):
 def respond(r):
  assert 'count_space_nums' not in r.url.params,'115driver GetInfo 不带参数；容量在 space_info 下'
  # 真实结构（115driver info.go）：data.space_info.{all_total,all_use,all_remain}.size，数字或字符串
  return httpx.Response(200,json={'state':True,'data':{'user_name':'测试用户','space_info':{'all_total':{'size':107374182400,'size_format':'100GB'},'all_use':{'size':'1073741824','size_format':'1GB'},'all_remain':{'size':106370912256,'size_format':'99GB'}}}})
 monkeypatch.setattr(a.net,'http_transport',lambda _:httpx.MockTransport(respond))
 info=await a.Drive(cfg()).account()
 assert info['uid']=='123' and info['user_name']=='测试用户'
 assert info['space_total']==107374182400 and info['space_used']==1073741824
 assert info['space_free']==106370912256,'剩余容量优先取 115 返回的 all_remain'

@pytest.mark.asyncio
async def test_account_space_falls_back_to_used_plus_remain(monkeypatch):
 monkeypatch.setattr(a.net,'http_transport',lambda _:httpx.MockTransport(lambda r:httpx.Response(200,json={'state':True,'data':{'space_info':{'all_use':{'size':40},'all_remain':{'size':60}}}})))
 info=await a.Drive(cfg()).account()
 assert info['space_total']==100 and info['space_used']==40 and info['space_free']==60

@pytest.mark.asyncio
async def test_account_degrades_when_capacity_missing(monkeypatch):
 # state=true 已证明凭据有效；容量明细缺失只影响展示，绝不能判登录失败（1.6.3 修复点）
 monkeypatch.setattr(a.net,'http_transport',lambda _:httpx.MockTransport(lambda r:httpx.Response(200,json={'state':True,'data':{'space':{}}})))
 info=await a.Drive(cfg()).account()
 assert info['uid']=='123' and info['space_total'] is None and info['space_used'] is None and info['space_free'] is None

@pytest.mark.asyncio
async def test_account_rejects_malformed_data(monkeypatch):
 monkeypatch.setattr(a.net,'http_transport',lambda _:httpx.MockTransport(lambda r:httpx.Response(200,json={'state':True,'data':'junk'})))
 with pytest.raises(a.DriveError,match='账号信息格式'):await a.Drive(cfg()).account()

@pytest.mark.asyncio
async def test_dirs_single_request_with_breadcrumb(monkeypatch):
 calls=[]
 def respond(r):
  calls.append(r)
  return httpx.Response(200,json={'state':True,'cid':'7','count':3,'path':[{'cid':'0','name':'根'},{'cid':'7','name':'影视'}],
   'data':[{'cid':'8','n':'电影'},{'fid':'9','cid':'7','n':'视频.mkv','s':'1','pc':'abcdef123456'},{'cid':'10','n':'剧集'}]})
 monkeypatch.setattr(a.net,'http_transport',lambda _:httpx.MockTransport(respond))
 result=await a.Drive(cfg()).dirs('7')
 assert len(calls)==1,'目录选择器应只发一次请求'
 assert [x['name'] for x in result['items']]==['电影','剧集'] and all(x['is_dir'] for x in result['items'])
 assert [x['name'] for x in result['path']]==['根','影视'] and result['cid']=='7'

@pytest.mark.asyncio
async def test_move_invalid_target_never_mutates(monkeypatch):
 calls=[]
 def respond(r):
  calls.append(r)
  return httpx.Response(200,json={'state':True,'cid':'0','count':0,'data':[]})
 monkeypatch.setattr(a.net,'http_transport',lambda _:httpx.MockTransport(respond))
 with pytest.raises(a.DriveError):await a.Drive(cfg()).mutate('move','4','999')
 assert len(calls)==1 and calls[0].method=='GET'

@pytest.mark.asyncio
async def test_drive_repeats_minted_user_agent(monkeypatch):
 captured=[]
 def respond(r):
  captured.append(r)
  if r.url.path=='/files/index_info':return httpx.Response(200,json={'state':True,'data':{'user_name':'u','space_info':{'all_total':{'size':'100'},'all_use':{'size':'40'},'all_remain':{'size':'60'}}}})
  return httpx.Response(200,json={'state':True,'path':[{'cid':'0'}],'data':[],'count':0})
 monkeypatch.setattr(a.net,'http_transport',lambda _:httpx.MockTransport(respond))
 drive=a.Drive(cfg());drive.ua='MintUA/1.0'
 info=await drive.account()
 assert info['space_free']==60 and captured[-1].headers['user-agent']=='MintUA/1.0','115 凭据与签发 UA 绑定：API 调用必须复用同一 UA'
 assert a.Drive(cfg()).ua=='Mozilla/5.0','未记录签发 UA 时回退默认（旧版网页 Cookie 兼容）'

def test_manifest_paths_library_only():
 # 2.0.1：清单不再申请 /media/strm 等绝对路径——用户在媒体库目录内自建子目录
 # 即被 library(rw) 授权覆盖（plugin_files.roots 把所有媒体库 root_paths 注册为
 # 授权根，check 用 realpath+is_relative_to 判断；makedirs 可在库内任意建目录）。
 # 防止未来误加回绝对路径授权。
 import os,tomllib
 manifest_path=os.path.join(os.path.dirname(p.__file__),'movieclaw-plugin.toml')
 with open(manifest_path,'rb') as f:manifest=tomllib.load(f)
 assert manifest['plugin']['version']=='2.8.3'
 # 2.4.1：网页入口收敛为一个——侧边栏「115网盘」/apps/p115-library。
 # 插件描述不再写 /api/v1/plugins/.../panel 那条旧地址（宿主批准页会显示这段话）。
 desc=manifest['plugin']['description']
 assert '/apps/p115-library' in desc and '/api/v1/plugins/p115-library/panel' not in desc,desc
 assert manifest['permissions']['paths']==[{'path':'library','mode':'rw'}],'只应申请媒体库读写授权'
 assert manifest['permissions']['operations']==['library.scan.start','library.list']
