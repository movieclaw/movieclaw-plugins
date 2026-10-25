"""Daily account visits and opt-in attendance, using the host's authenticated client.

No credentials, CAPTCHA answers, historical attendance-card purchases or arbitrary
scripts are accepted. An interrupted write is never automatically replayed.
"""
import asyncio
import copy
import hashlib
import json
import random
import re
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse
from uuid import uuid4
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request
from parsel import Selector
from .management import check_write_origin, redacted_error

UTC = timezone.utc
LABELS = {'unknown':'尚未检查', 'pending':'今日待签到', 'signed':'今日已签到',
          'running':'执行中', 'challenge':'需要人工验证', 'login_required':'需要重新登录',
          'unsupported':'尚无签到适配', 'failed':'执行失败', 'uncertain':'结果未确认',
          'visited':'访问成功 · 保号效果未确认', 'interrupted':'执行中断 · 请检查后重试'}
LABELS.update(blocked='访问被拦截', rate_limited='站点限流')
SUCCESS = re.compile(r'今日(?:已经|已)?签到|(?:已经|已)签到|签到成功|簽到成功|已簽到|already\s+(?:signed|checked|attended)|check[ -]?in\s+(?:successful|complete)', re.I)
CHALLENGE = re.compile(r'cf-chl-|challenge-platform|cf-turnstile|g-recaptcha|h-captcha|captcha-container|imagestring|imagehash|验证码|驗證碼|人机验证', re.I)


def moment(): return datetime.now(UTC)


def minute(value):
    if not isinstance(value, str) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', value):
        raise ValueError('时间请使用 HH:MM 格式')
    return int(value[:2]) * 60 + int(value[3:])


def rule(value):
    if not value: return None
    if not isinstance(value, dict) or set(value) - {'path', 'signed_css'}:
        raise ValueError('签到规则只接受签到路径和已签到选择器')
    path, css = value.get('path', ''), value.get('signed_css', '')
    if not re.fullmatch(r'(?:[A-Za-z0-9_-]+/)*(?:attendance|checkin|check-in|signin|sign-in)(?:\.php)?', path):
        raise ValueError('仅支持本站 attendance / checkin / signin 路径，不含参数')
    if not isinstance(css, str) or len(css) > 500 or '\n' in css:
        raise ValueError('已签到选择器不正确')
    if css:
        try: Selector(text='<html/>').css(css)
        except Exception: raise ValueError('已签到选择器无法解析') from None
    return {'path':path, 'signed_css':css}


def same_url(base, value):
    result = urljoin(base if urlparse(base).path else base+'/', value)
    a, b = urlparse(base), urlparse(result)
    if b.scheme != 'https' or (a.hostname, a.port or 443) != (b.hostname, b.port or 443) or b.username or b.password or b.fragment:
        raise ValueError('站点返回了跨域或不安全的签到链接')
    return result


def identity(account):
    profile = account.get('profile') or {}
    actor = [account.get('site_id'), account.get('auth_type'), account.get('created_at'), account.get('updated_at'), profile.get('user_id'), profile.get('username')]
    return hashlib.sha256(json.dumps(actor, sort_keys=True, default=str).encode()).hexdigest()


def owner_identity(account):
    # 会话更新不能抹掉历史；更换 PT 账号时则不能继承另一人的签到状态。
    profile=account.get('profile') or {}
    actor=[account.get('site_id'),account.get('created_at'),profile.get('user_id') or profile.get('username')]
    return hashlib.sha256(json.dumps(actor,sort_keys=True,default=str).encode()).hexdigest()


def inspect_html(response):
    if response.headers.get('cf-mitigated') == 'challenge':
        return 'challenge'
    if response.status_code in (301,302,303,307,308):
        return 'login_required' if re.search(r'login|auth',response.headers.get('location',''),re.I) else 'uncertain'
    if response.status_code == 401: return 'login_required'
    if response.status_code == 429:return 'rate_limited'
    if len(response.content) > 2*1024*1024: return 'uncertain'
    html = response.text
    doc = Selector(text=html)
    if doc.css('input[type=password]'): return 'login_required'
    # Scripts can contain dormant captcha code; only live markup is evidence.
    markup = ''.join(doc.xpath('//*[not(self::script or self::style)]/@class | //input/@name | //iframe/@src').getall())
    if CHALLENGE.search(markup) or doc.css('input[name=imagestring],input[name=imagehash],.cf-turnstile,.g-recaptcha,.h-captcha'):
        return 'challenge'
    if 'Just a moment' in html[:3000] or 'Checking your browser' in html[:3000]: return 'challenge'
    if response.status_code == 403:return 'blocked'
    if response.status_code >= 400:return 'failed'
    return None


def signed_page(response, css='', attendance=False):
    doc = Selector(text=response.text)
    if css and doc.css(css): return True
    nodes = doc.css('a[href*="attendance"],a[href*="checkin"],a[href*="signin"]')
    if any(SUCCESS.search(n.xpath('string(.)').get() or '') for n in nodes): return True
    if attendance:
        for bad in doc.css('script,style'): bad.root.getparent().remove(bad.root)
        text = ' '.join(doc.xpath('//text()').getall())
        if re.search(r'今日(?:已经|已)?签到|今天(?:已经|已)签到|今日已簽到|already\s+(?:attended|checked\s+in|signed\s+in)\s+today', text, re.I): return True
        return any(SUCCESS.search(n.xpath('string(.)').get() or '') for n in doc.css('h1,h2,h3,td.colhead,td.colhead2,.alert-success,.attendance-success'))
    return False


class Maintenance:
    """复用宿主登录会话串行维护站点，分别保留签到和访问的证据。

    执行前持久化意图以防重启后重复提交；成功时间只在获得明确证据时更新，
    失败、跨天和普通刷新均不清空。批量任务不改动用户的每日自动计划。
    """
    def __init__(self, ctx, workspace, snapshot, get_site=None, clock=moment):
        self.ctx, self.workspace, self.snapshot, self.clock = ctx, workspace, snapshot, clock
        self.get_site = get_site or self.native_site
        self.data, self.files = workspace.data, workspace.files
        self.state = None
        self.file = None
        self.lock = asyncio.Lock()
        self.gate = asyncio.Lock()
        self.busy = set()
        self.tasks = set()
        self.worker = None
        self.batch = None

    async def native_site(self, sid):
        from movieclaw_api.services.site_access import get_site_access
        return await get_site_access().get(sid)

    async def start(self):
        saved = await self.data.get('maintenance-v1')
        if saved:
            self.file = saved['file']
            with await self.files.open(self.file, 'rb') as source:
                self.state = json.loads(await asyncio.to_thread(source.read))
        else:
            self.state = {'revision':0,'timezone':'Asia/Shanghai','start':'08:00','end':'22:00','seed':secrets.token_hex(16),'sites':{},'records':{}}
        changed = False
        for rec in self.state['records'].values():
            if rec.get('status') == 'running':
                rec.update(status='interrupted', message='上次执行中断，未自动重复提交，请检查今日状态后重试');changed=True
        if changed: await self.persist()
        if hasattr(self.ctx,'effect'): self.ctx.effect(self.close, label='account-maintenance')
        self.ensure_worker()

    async def persist(self):
        filename = self.files.path('plugin','maintenance-'+uuid4().hex+'.json')
        await self.files.makedirs(str(Path(filename).parent))
        payload = json.dumps(self.state,ensure_ascii=False).encode()
        if len(payload)>1024*1024: raise ValueError('账号维护记录过大')
        with await self.files.open(filename,'wb') as target: await asyncio.to_thread(target.write,payload)
        await self.data.set('maintenance-v1',{'file':filename},secret=True)
        previous,self.file=self.file,filename
        if previous and hasattr(self.files,'remove'):
            try: await self.files.remove(previous)
            except OSError: pass

    def spawn(self, coro, name):
        task = self.ctx.task(coro,name=name)
        if task is not None:
            self.tasks.add(task);task.add_done_callback(self.tasks.discard)
        return task

    def ensure_worker(self):
        if any(x.get('enabled') for x in self.state['sites'].values()) and (self.worker is None or self.worker.done()):
            self.worker=self.spawn(self.loop(),'account-maintenance')

    async def close(self):
        for task in list(self.tasks): task.cancel()
        if self.tasks: await asyncio.gather(*list(self.tasks),return_exceptions=True)

    def local(self, now=None): return (now or self.clock()).astimezone(ZoneInfo(self.state['timezone']))

    def record(self, sid, account):
        rec=copy.deepcopy(self.state['records'].get(sid,{}))
        if rec.get('owner'):
            if rec['owner']!=owner_identity(account):return {}
        elif rec.get('account')!=identity(account):return {}
        if rec.get('account')!=identity(account):
            # 同一账号换 Cookie 后保留成功时间，但必须重新验证今日会话。
            for key in ('signed_day','attempt_day','attempts','day','status','message'):
                rec.pop(key,None)
        return rec

    def schedule_record(self, sid, account):
        # 自动执行按账号保留当日防重；宿主重验和 Cookie 更新都会改变 updated_at，
        # 不能因此重放签到。没有 owner 的旧记录也先保留防重，次日自然恢复计划。
        rec=self.state['records'].get(sid,{})
        if rec.get('owner') and rec['owner']!=owner_identity(account):return {}
        return rec

    def slots(self, day):
        ids=sorted(sid for sid,v in self.state['sites'].items() if v.get('enabled'))
        start,end=minute(self.state['start']),minute(self.state['end'])
        digest=hashlib.sha256((self.state['seed']+day.isoformat()).encode()).digest()
        rng=random.Random(int.from_bytes(digest,'big'))
        values=rng.sample(range(start,end+1),len(ids))
        return dict(zip(ids,values))

    def next_time(self, sid, now=None, record=None):
        now=self.local(now);cfg=self.state['sites'].get(sid,{})
        if not cfg.get('enabled'): return None
        for offset in range(2):
            day=(now+timedelta(days=offset)).date();slot=self.slots(day).get(sid)
            if slot is None: return None
            dt=datetime.combine(day,datetime.min.time(),tzinfo=now.tzinfo)+timedelta(minutes=slot)
            rec=self.state['records'].get(sid,{}) if record is None else record
            if offset==0 and (rec.get('attempt_day')==day.isoformat() or (rec.get('signed_day')==day.isoformat() and cfg['action']=='sign')): continue
            if dt>=now:return dt.isoformat()
            # An offline server catches up once within the same site's calendar day.
            if offset==0:return now.isoformat()
        return None

    async def view(self):
        snap=await self.snapshot()
        today=self.local().date().isoformat();rows=[]
        for site in snap['sites']:
            sid=site['site_id'];cfg=self.state['sites'].get(sid,{'enabled':False,'action':'sign','rule':None})
            rec=self.record(sid,site.get('_account',{}))
            status=rec.get('status','unknown')
            if sid in self.busy: status='running'
            elif rec.get('signed_day')==today: status='signed'
            elif rec.get('day')!=today:
                status='unknown';rec['message']='今日尚未检查；下方历史记录仅供参考'
            rows.append({'site_id':sid,'name':site['name'],'icon_url':site.get('icon_url'),'website_url':site.get('website_url'),
                'usable':site['usable'],'config':cfg,'status':status,'label':LABELS[status],
                'message':rec.get('message',''),'checked_at':rec.get('checked_at'),'signed_at':rec.get('signed_at') if rec.get('signed_day')==today else None,
                'last_signed_at':rec.get('signed_at'),'last_visited_at':rec.get('visited_at'),
                'visit_method':rec.get('visit_method'),'operations':rec.get('operations',{}),
                'next_at':self.next_time(sid,record=self.schedule_record(sid,site.get('_account',{}))),'today_time':self.slots(self.local().date()).get(sid),
                'history':rec.get('history',[])})
        return {'revision':self.state['revision'],'timezone':self.state['timezone'],'start':self.state['start'],'end':self.state['end'],'sites':rows,'today':today,'batch':copy.deepcopy(self.batch)}

    async def configure(self,payload):
        async with self.lock:
            if payload.get('revision')!=self.state['revision']:raise HTTPException(409,'计划已被其他页面更新，请刷新')
            tz=payload.get('timezone','Asia/Shanghai')
            try:ZoneInfo(tz)
            except Exception:raise ValueError('时区不正确') from None
            start,end=payload.get('start','08:00'),payload.get('end','22:00')
            if minute(end)-minute(start)<119:raise ValueError('随机时间范围至少需要两小时，且不能跨天')
            snap=await self.snapshot();known={s['site_id']:s for s in snap['sites']}
            configs=payload.get('sites')
            if not isinstance(configs,dict) or len(configs)>100 or set(configs)-known.keys():raise ValueError('只能设置本服务器已添加的站点')
            values={}
            for sid,v in configs.items():
                if not isinstance(v,dict) or set(v)-{'enabled','action','rule'} or type(v.get('enabled')) is not bool or v.get('action') not in ('sign','visit'):raise ValueError('维护计划格式不正确')
                if v['enabled'] and not known[sid]['usable']:raise ValueError('请先在原生站点设置验证账号：'+known[sid]['name'])
                values[sid]={'enabled':v['enabled'],'action':v['action'],'rule':rule(v.get('rule'))}
            previous=copy.deepcopy(self.state)
            self.state.update(timezone=tz,start=start,end=end,sites=values,revision=self.state['revision']+1)
            if payload.get('randomize'):self.state['seed']=secrets.token_hex(16)
            try:await self.persist()
            except BaseException:self.state=previous;raise
        self.ensure_worker()
        return await self.view()

    async def update(self,sid,operation=None,**fields):
        async with self.lock:
            rec=self.state['records'].setdefault(sid,{})
            rec.update(fields)
            if operation:
                action,status,message,at=operation
                result=rec.setdefault('operations',{}).setdefault(action,{})
                result.update(status=status,message=message,checked_at=at)
                if action=='visit' and fields.get('visited_at'):result['confirmed_at']=fields['visited_at']
            if fields.get('signed_at'):
                rec.setdefault('operations',{}).setdefault('sign',{}).update(
                    status='signed',message=fields.get('message','页面确认今日已签到'),
                    checked_at=fields['signed_at'],confirmed_at=fields['signed_at'])
            if fields.get('status') and fields['status']!='running':
                rec['history']=([{'at':fields.get('checked_at'),'label':LABELS[fields['status']],'message':fields.get('message','')}] + rec.get('history',[]))[:5]
            await self.persist()

    async def request(self,site,url,method='GET',data=None):
        url=same_url(site.base_url,url)
        method_fn=site.client.raw_post if method=='POST' else site.client.raw_get
        kwargs={'follow_redirects':False,'headers':{'Referer':same_url(site.base_url,'index.php')}}
        if data is not None:kwargs['data']=data
        return await asyncio.wait_for(method_fn(url,**kwargs),25)

    async def perform(self,site,action,cfg,on_visit=None):
        is_nexus=any(c.__name__ in ('NexusPHPSite','PackNexusSite') for c in type(site).__mro__)
        if not is_nexus and not cfg.get('rule'):
            if action=='sign':return 'unsupported','此站尚无签到适配，可填写已确认的签到路径与成功选择器'
            profile=await site.get_user_profile()
            if not (profile.username or profile.user_id):return 'uncertain','未确认账号身份'
            if on_visit:await on_visit('api')
            return 'visited','账号 API 访问成功，不能据此确认网页活跃时间或保号效果'
        response=await self.request(site,'index.php')
        bad=inspect_html(response)
        if bad:return bad,'登录或访问受阻，请打开站点处理；不会自动解验证码'
        doc=Selector(text=response.text)
        if not (doc.css('a[href*="userdetails.php?id="],a[href*="logout.php"]')):
            return 'login_required','未确认已登录账号，请在原生设置更新会话'
        if on_visit:await on_visit('web')
        custom=cfg.get('rule') or {};css=custom.get('signed_css','')
        if signed_page(response,css):return 'signed','站点当前页面明确显示已签到'
        if action in ('visit','check'):return ('visited' if action=='visit' else 'pending'),'访问成功；尚未确认今日签到或保号效果'
        path=custom.get('path','attendance.php')
        response=await self.request(site,path)
        if response.status_code==404:return 'unsupported','本站没有该签到页面，需要专门适配'
        bad=inspect_html(response)
        if bad:return bad,'签到页需要重新登录或人工验证，请处理后点击“检查 / 重试今日签到”'
        if signed_page(response,css,True):return 'signed','站点签到页明确确认今日签到完成'
        # Only a same-origin attendance form without CAPTCHA may be submitted.
        doc=Selector(text=response.text);forms=[]
        for form in doc.css('form'):
            target=same_url(str(response.url),form.attrib.get('action') or str(response.url))
            if urlparse(target).path!=urlparse(same_url(site.base_url,path)).path:continue
            if re.search(r'retro|补签|補簽|消耗|购买|購買|purchase|exchange|delete|remove', target+' '+(form.xpath('string(.)').get() or ''),re.I):continue
            if form.css('input:not([type=hidden]):not([type=submit]):not([type=button]),textarea,select'):continue
            if form.attrib.get('method','get').lower()!='post':continue
            if not form.css('input[type=submit],button[type=submit],button:not([type])'):continue
            values={}
            for field in form.css('input[type=hidden][name]'):
                name=field.attrib['name'];value=field.attrib.get('value','')
                if len(name)>80 or len(value)>4096:raise ValueError('签到表单字段超出限制')
                if re.search(r'date|retro|card|cost|price|amount|buy|purchase|passkey|password',name,re.I):
                    values=None;break
                if name.lower()=='action' and value.lower() not in ('attendance','attend','sign','signin','checkin','check-in'):
                    values=None;break
                values[name]=value
            if values is not None:forms.append((target,values))
        if len(forms)!=1:return 'uncertain','未找到唯一且无需验证码的签到表单，未猜测提交'
        response=await self.request(site,forms[0][0],method='POST',data=forms[0][1])
        bad=inspect_html(response)
        if bad and not (bad=='uncertain' and response.status_code in (302,303)):
            return bad,'签到提交未得到明确结果，请先检查站点，避免重复提交'
        if signed_page(response,css,True):return 'signed','站点返回明确的今日签到成功结果'
        # Post/Redirect/Get: read the same attendance page once, never replay POST.
        response=await self.request(site,path)
        bad=inspect_html(response)
        if bad:return bad,'提交后无法确认结果，请手动检查'
        return ('signed','已重新读取签到页，确认今日已签到') if signed_page(response,css,True) else ('uncertain','请求已发送，但没有明确签到成功证据，请手动核实')

    async def run(self,sid,action='sign',automatic=False):
        if sid in self.busy:return False
        self.busy.add(sid)
        try:
            async with self.gate:
                snap=await self.snapshot();row=next((s for s in snap['sites'] if s['site_id']==sid),None)
                if not row or not row['usable']:return False
                now=self.clock();day=self.local(now).date().isoformat();account=identity(row.get('_account',{}))
                cfg=copy.deepcopy(self.state['sites'].get(sid,{'action':'sign','rule':None}))
                if automatic and (not cfg.get('enabled') or cfg['action']!=action):return False
                record=self.record(sid,row.get('_account',{}))
                if automatic and record.get('attempt_day')==day:return False
                if action=='sign' and record.get('signed_day')==day:return False
                attempts=record.get('attempts',{})
                last=attempts.get(action)
                if not automatic and last and (now-datetime.fromisoformat(last)).total_seconds()<60:return False
                async with self.lock:
                    self.state['records'][sid]=record
                attempts[action]=now.isoformat()
                fields={'account':account,'owner':owner_identity(row.get('_account',{})),'attempts':attempts,'day':day,'checked_at':now.isoformat(),'status':'running','message':'正在访问站点'}
                if action=='sign' or automatic:fields['attempt_day']=day
                await self.update(sid,**fields)  # Commit intent BEFORE authenticated side effects.
                try:
                    async def visited(method):
                        # 签到页可能受阻，首页已经验证的访问仍应独立保存；账号变化则拒收。
                        fresh=await self.snapshot()
                        current=next((s for s in fresh['sites'] if s['site_id']==sid),None)
                        if current and identity(current.get('_account',{}))==account:
                            at=self.clock().isoformat()
                            message='已验证登录网页访问' if method=='web' else '已验证账号 API 访问，网页活跃时间未确认'
                            await self.update(sid,visited_at=at,visit_method=method,operation=('visit','visited',message,at))
                    site=await self.get_site(sid)
                    status,message=await asyncio.wait_for(self.perform(site,action,cfg,visited),85)
                except asyncio.CancelledError:raise
                except Exception as exc:
                    # Never expose credential-bearing HTTP exception URLs or response bodies.
                    name=type(exc).__name__
                    status='login_required' if 'Auth' in name or 'Unavailable' in name else 'failed'
                    message='访问失败（'+name+'），请检查会话和网络；未自动重试'
                # Ignore a result if the native account changed while awaiting the request.
                fresh=await self.snapshot();current=next((s for s in fresh['sites'] if s['site_id']==sid),None)
                if not current or identity(current.get('_account',{}))!=account:
                    await self.update(sid,status='uncertain',message='账号已变化，未采用本次结果',signed_day=None);return True
                fields={'status':status,'message':message,'checked_at':self.clock().isoformat()}
                if status=='signed':fields.update(signed_day=self.local().date().isoformat(),signed_at=self.clock().isoformat())
                # 首页的“已签到”是额外证据，访问结果本身仍应说明访问成功。
                opstatus,opmessage=('visited','已验证登录网页访问') if action=='visit' and status=='signed' else (status,message)
                await self.update(sid,operation=(action,opstatus,opmessage,fields['checked_at']),**fields)
                return True
        except asyncio.CancelledError:raise
        except Exception:
            at=self.clock().isoformat();message='执行结果无法保存或核实，请检查站点后重试'
            await self.update(sid,status='uncertain',message=message,checked_at=at,operation=(action,'uncertain',message,at))
            return True
        finally:self.busy.discard(sid)

    async def queue_batch(self,action):
        if action not in ('sign','visit','both'):raise HTTPException(400,'批量操作仅支持签到、访问或两项一起执行')
        async with self.lock:
            if self.batch and self.batch['status'] in ('queued','running'):
                raise HTTPException(409,'批量任务正在执行，请稍候查看结果')
            snap=await self.snapshot()
            ids=[s['site_id'] for s in snap['sites'] if s['usable']]
            self.batch={'action':action,'status':'queued','total':len(snap['sites']),'completed':0,
                        'skipped':len(snap['sites'])-len(ids),'current':None,'started_at':self.clock().isoformat()}
            self.spawn(self.run_batch(action,ids), 'account-maintenance-batch')
        return {'message':'已排队，逐站执行；未验证账号、今日已签到或冷却中的操作会跳过'}

    async def run_batch(self,action,ids=None):
        """逐站执行，单站失败不影响后续；批量过程不持久化待重放的签到队列。"""
        if ids is None:
            snap=await self.snapshot();ids=[s['site_id'] for s in snap['sites'] if s['usable']]
            self.batch={'action':action,'total':len(snap['sites']),'completed':0,'skipped':len(snap['sites'])-len(ids),'started_at':self.clock().isoformat()}
        self.batch.update(status='running',current=None)
        try:
            for sid in ids:
                self.batch['current']=sid
                executed=False
                for step in (('visit','sign') if action=='both' else (action,)):
                    executed=await self.run(sid,step) or executed
                self.batch['completed' if executed else 'skipped']+=1
            self.batch['status']='completed'
        except asyncio.CancelledError:
            self.batch['status']='interrupted';raise
        except Exception:
            self.batch['status']='failed'
        finally:
            self.batch.update(current=None,finished_at=self.clock().isoformat())

    async def tick(self):
        now=self.local();day=now.date().isoformat();slots=self.slots(now.date())
        snap=await self.snapshot();known={s['site_id']:s for s in snap['sites']}
        for sid,slot in sorted(slots.items(),key=lambda x:x[1]):
            if sid not in known or not known[sid]['usable'] or sid in self.busy:continue
            # 自动计划与页面使用同一账号的有效记录，不能继承旧账号今日已执行的标记。
            rec=self.schedule_record(sid,known[sid].get('_account',{}))
            if rec.get('attempt_day')==day:continue
            if now.hour*60+now.minute>=slot:await self.run(sid,self.state['sites'][sid]['action'],True)

    async def loop(self):
        while any(v.get('enabled') for v in self.state['sites'].values()):
            try:await self.tick()
            except asyncio.CancelledError:raise
            except Exception:pass  # A host outage must not crash the plugin; retry the read next minute.
            await asyncio.sleep(30)


def maintenance_router(service):
    router=APIRouter()
    @router.get('/maintenance',operation_id='plugins.pt-site-pack.maintenance')
    async def view():return await service.view()
    @router.post('/maintenance/plan',operation_id='plugins.pt-site-pack.maintenance-plan')
    async def plan(request:Request):
        check_write_origin(request)
        try:return await service.configure(await request.json())
        except ValueError as exc:raise HTTPException(400,str(exc)) from None
    @router.post('/maintenance/batch',operation_id='plugins.pt-site-pack.maintenance-batch')
    async def batch(request:Request):
        check_write_origin(request)
        payload=await request.json()
        if not isinstance(payload,dict):raise HTTPException(400,'批量操作格式不正确')
        return await service.queue_batch(payload.get('action'))
    @router.post('/maintenance/{sid}/{action}',operation_id='plugins.pt-site-pack.maintenance-run')
    async def run(sid:str,action:str,request:Request):
        check_write_origin(request)
        if action not in ('sign','check','visit'):raise HTTPException(400,'不支持的维护操作')
        if service.batch and service.batch['status'] in ('queued','running'):raise HTTPException(409,'批量任务正在执行，请稍候查看结果')
        snap=await service.snapshot();row=next((s for s in snap['sites'] if s['site_id']==sid),None)
        if not row or not row['usable']:raise HTTPException(409,'请先添加并验证本站账号')
        if sid in service.busy:raise HTTPException(409,'本站正在执行，请稍后查看结果')
        service.spawn(service.run(sid,action),f'account-{sid}')
        return {'message':'已排队，请稍候查看结果；若刚执行过，会在一分钟冷却后允许重试'}
    return router
