"""Admin-only QR login; tokens never persisted, cookies saved as host secrets."""
import asyncio
import time
import httpx
import qrcode
from pydantic import SecretStr
from movieclaw_sdk import net
from drive_api import DriveError

# QR token/status follow p115client 0.0.9.7.2 (client.py:1580,1552); the CONFIRM
# call that mints the cookie goes to passportapi.115.com with an explicit `app`
# form field -- the channel used by gangz1o/115-offline-helper (popup.js:448-450)
# and SheltonZhu/115driver (qrcode.go, ApiQrcodeLoginWithApp, used by AList/OpenList).
# 115 binds the minted credential to the User-Agent of the confirming request:
# OpenList mints AND calls webapi with one and the same UA for every device type
# (drivers/115/util.go getUA), and its 115driver even keeps download links
# UA-bound. Minting with httpx's default UA and then calling webapi with a
# different one gets errno 990001 on every request, so the mint UA is persisted
# ('qr-ua') and Drive repeats it for all later API calls.
# 115 retired the desktop clients (os_windows/os_mac/os_linux): cookies minted for
# those types are unusable (AList docs: "系统已下架"), so they are not offered.
TOKEN_APP='web'
QR_BASE='https://qrcodeapi.115.com'
CONFIRM_BASE='https://passportapi.115.com'
API_UA='Mozilla/5.0 115Browser/35.6.5'
DEVICES={
 'wechatmini':'微信小程序（推荐，不挤常用端）',
 'alipaymini':'支付宝小程序（推荐，不挤常用端）',
 'qandroid':'115管理 App 安卓端（推荐）',
 'qios':'115管理 App 苹果端（推荐）',
 'tv':'TV 电视端（推荐）',
 'android':'安卓设备（会踢手机 115 App）',
 'ios':'iPhone（会踢手机 115 App）',
 'ipad':'iPad（会踢平板 115 App）',
 'web':'浏览器网页端（最易触发 IP 校验）',
}
RETIRED_DEVICES={'os_windows':'Windows 电脑端','os_mac':'macOS 电脑端','os_linux':'Linux 电脑端'}

def mint_ua(cfg):
    """The UA used when minting the cookie; Drive must repeat it on every call."""
    custom=getattr(cfg,'api_user_agent','') or ''
    return custom.strip() or API_UA

class Login:
    def __init__(self,cfg,store):
        self.cfg=cfg;self.store=store;self.token=None;self.expires=0;self.device='wechatmini';self.lock=asyncio.Lock();self.svg=''
    async def request(self,method,url,**kwargs):
        try:
            headers={**{'User-Agent':mint_ua(self.cfg)},**kwargs.pop('headers',{})}
            async with httpx.AsyncClient(transport=net.http_transport('p115-library'),timeout=40,trust_env=False,follow_redirects=False) as c:
                r=await c.request(method,url,headers=headers,**kwargs)
                if r.status_code!=200:raise DriveError('扫码接口暂不可用')
                d=r.json()
                if not d.get('state'):raise DriveError('扫码请求未成功，请重新生成二维码')
                return d['data']
        except DriveError:raise
        except Exception:raise DriveError('扫码请求超时或响应异常，请重试') from None
    async def start(self,device='wechatmini'):
        async with self.lock:
            if device in RETIRED_DEVICES:raise DriveError('115 已下架电脑客户端（'+RETIRED_DEVICES[device]+'），该类型的凭据不可用；请改选微信小程序、115管理 App 或 TV 等类型')
            if device not in DEVICES:raise DriveError('不支持的设备类型')
            self.device=device
            # Token endpoint is always app=web (matches 115-offline-helper and
            # p115client's own default); the device type only affects the final
            # confirm call that mints the cookie.
            d=await self.request('GET',f'{QR_BASE}/api/1.0/{TOKEN_APP}/1.0/token/')
            self.token={k:d[k] for k in ('uid','time','sign')};self.expires=time.monotonic()+240
            code=qrcode.QRCode(box_size=1,border=4);code.add_data(d.get('qrcode') or 'http://115.com/scan/dg-'+str(d['uid']));code.make(fit=True)
            matrix=code.get_matrix();n=len(matrix)
            cells=''.join(f'<rect x="{x}" y="{y}" width="1" height="1"/>' for y,row in enumerate(matrix) for x,v in enumerate(row) if v)
            self.svg=f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {n} {n}"><rect width="{n}" height="{n}" fill="white"/><g fill="black">{cells}</g></svg>'
            return {'state':'waiting','expires_in':240,'device':device,'device_name':DEVICES[device]}
    async def poll(self):
        async with self.lock:
            if not self.token or time.monotonic()>self.expires:return {'state':'expired'}
            params={**self.token,'_':int(time.time()*1000)}
            d=await self.request('GET',QR_BASE+'/get/status/',params=params)
            status=int(d['status'])
            if status==2:
                # Confirm on passportapi.115.com with the `app` form field, matching
                # 115-offline-helper (popup.js:448-450) and SheltonZhu/115driver
                # (qrcode.go: QRCodeLoginWithApp posts {account, app}). The request
                # carries mint_ua() -- the SAME UA Drive later uses for webapi calls,
                # because 115 binds the credential to the minting UA.
                ua=mint_ua(self.cfg)
                kwargs={'data':{'account':self.token['uid'],'app':self.device},'headers':{'User-Agent':ua}}
                d=await self.request('POST',f'{CONFIRM_BASE}/app/1.0/{self.device}/1.0/login/qrcode/',**kwargs)
                c=d['cookie']
                if not isinstance(c,dict) or not all(c.get(k) for k in ('UID','CID','SEID')):raise DriveError('115 未返回完整登录凭据')
                # KID is optional but included when present (OpenList sends it too).
                keys=('UID','CID','SEID')+(('KID',) if c.get('KID') else ())
                cookie='; '.join(f'{k}={c[k]}' for k in keys)
                if any(x in cookie for x in ('\r','\n')):raise DriveError('115 登录凭据格式异常')
                await self.store.set('qr-cookie',cookie,secret=True)
                await self.store.set('qr-ua',ua)
                self.cfg.cookie=SecretStr(cookie)
                self.token=None;self.svg=''
                return {'state':'logged_in','device':self.device,'device_name':DEVICES[self.device]}
            if status in (-1,-2):self.token=None;self.svg=''
            return {'state':{0:'waiting',1:'scanned',-1:'expired',-2:'cancelled'}.get(status,'waiting')}
