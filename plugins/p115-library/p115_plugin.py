"""MovieClaw v0.35.0: 115 browser and STRM library integration, isolated process."""
import asyncio
import json
import logging
import re
import time
from datetime import datetime
from pathlib import PurePosixPath
from urllib.parse import urlsplit,parse_qs
import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse, HTMLResponse, Response, StreamingResponse
from pydantic import BaseModel, Field, SecretStr, ValidationError, field_validator, model_validator
from movieclaw_sdk import plugin,net
from cronexpr import Cron
from login import Login,DEVICES,RETIRED_DEVICES,mint_ua
from life_strm import LifeSTRM,mapping_specs
from movieclaw_api.plugins.keys import HOST_OPS, PLUGIN_DATA, PLUGIN_FILES, PLUGIN_ROUTES
from drive_api import Drive, DriveError, rebase_url
from runlog import log_event
from sync_engine import scope_key
from panel import PANEL
from prefetch import Prefetcher

OPS=('library.scan.start','library.list')

DEFAULT_SUFFIXES='mkv,mp4,avi,mov,m4v,ts,m2ts,wmv,flv,webm,mpg,mpeg,iso'

def fmt_size(n):
    if n>=1024**3:return '%.1f GB'%(n/1024**3)
    if n>=1024**2:return '%.1f MB'%(n/1024**2)
    if n>=1024:return '%.1f KB'%(n/1024)
    return '%d B'%n

class Settings(BaseModel):
    strm_directory:str=Field('',title='本地 STRM 生成目录',description='115 媒体库 CID 中的视频按相对目录生成到这里；填写容器内路径，必须在已授权的媒体库范围内，与整理目录分开。留空关闭自动生成。')
    life_events:bool=Field(True,title='监控 115 生活操作事件',description='监测网盘的上传/移动/接收/新建目录/删除/改名等操作事件并即时触发目录核对（浏览类事件忽略）；任何设备上传或移动文件到监控库都会立即生成 STRM。不依赖事件完整性，定时增量兜底。')
    life_interval:int=Field(60,ge=30,le=3600,title='生活事件检查间隔（秒）')
    sync_cron:str=Field('*/20 8-23 * * *',title='增量同步 cron 表达式',description='5 段式（分 时 日 月 周），到点执行一次完整增量核对；115 生活事件会即时触发，不依赖该表达式。控制台「数据同步 → 增量同步」可预览最近 5 次运行时间。')
    scan_strategy:str=Field('fast',title='网盘扫描方式',description='fast=极速扫描（2.7.0 默认）：整棵目录树一次性分页列出全部文件（每页 1150 条）+ 目录树接口一次取全部子目录，5 万文件约 50 次请求；不可用时自动退回逐目录扫描。legacy=逐目录扫描（旧方式，每个目录至少一次请求，大库很慢，仅供排查）。')
    @field_validator('scan_strategy')
    @classmethod
    def scan_strategy_ok(cls,v):
        if v not in ('fast','legacy'):raise ValueError('扫描方式仅支持 fast 或 legacy')
        return v
    sync_mode:str=Field('normal',title='同步模式',description='normal=普通模式：校验已有 STRM 内容；fast=快速模式：信任同步记录，跳过未变化文件的内容校验。')
    strm_link_format:str=Field('pick_code',title='STRM 直连格式',description='pick_code=仅播放地址；pick_code_name=地址后追加 ?/文件名，便于播放器识别名称。')
    strm_base_url:str=Field('',title='STRM 直连地址（可选，外网用）',description='留空=按当前访问地址自动签发；填写后 STRM 内播放地址改用该地址开头（如外网域名 http://www.example.com:9577），地址必须能路由回本 MovieClaw 服务，路径与签名保持不变。')
    strm_keep_suffix:bool=Field(False,title='STRM 保留文件后缀',description='开启后生成「影片.mkv.strm」，关闭生成「影片.strm」；切换后旧命名的 STRM 按清理策略处理。')
    strm_exist_policy:str=Field('overwrite',title='STRM 本地存在时',description='overwrite=覆盖生成（仅限插件生成且未被手工修改的文件）；skip=直接跳过。用户改过的文件两种模式都不覆盖。')
    # 2.7.7: kept only so old saved settings / config backups still load.
    # Same-name videos (X.mkv + X.mp4) always keep the bigger one; the
    # former 'abort' choice (stop the whole sync) is gone.
    strm_conflict_policy:str=Field('auto',title='同名 STRM 冲突时（已固定为保留较大）',description='同一目录两个不同格式的同名视频（如 X.mkv 与 X.mp4）生成同一个 STRM 时，固定保留体积较大的那个；如需两个都保留，请开启「STRM 保留文件后缀」。')
    video_suffixes:str=Field(DEFAULT_SUFFIXES,title='视频文件后缀',description='逗号分隔；网盘核对按此识别视频。')
    min_video_mb:int=Field(0,ge=0,le=1048576,title='最小视频体积（MB）',description='小于该体积的视频不生成 STRM；0 表示不限制。')
    sidecar_sync:bool=Field(True,title='同步图片/NFO/字幕',description='视频所在目录、其各级上级目录（剧集/季/合集层）及 extrafanart / extrathumbs 里的 .nfo/.jpg/.jpeg/.png/.webp/.tbn 与字幕，经 115 直链下载到 STRM 目录对应位置，单文件最大 20MB；不含视频的目录不同步；随扫描边下载。')
    strm_cleanup:bool=Field(False,title='清理网盘已删除的 STRM',description='核对确认网盘中已删除的视频后，删除插件生成且未被修改的 .strm 与附属文件；网盘文件重新出现后会自动重建。默认关闭，保持只增不删。')
    mappings:str=Field('',title='多目录映射列表（可选）',description='JSON 数组，每条 {"name":"电影","cloud_cid":"123","strm_directory":"/media/strm/movies"}；填写后忽略上方单目录配置，可把多个 115 目录分别同步到多个本地 STRM 目录。控制台「数据同步 → 全量同步」提供可视化编辑。')
    @model_validator(mode='after')
    def distinct_roots(self):
        specs=[]
        try:specs=mapping_specs(self.mappings)
        except ValueError:specs=[]  # field validator already reports the real error
        roots=[d for d in (self.strm_directory,) if d]
        roots+=[s['strm_directory'] for s in specs]
        for i,a in enumerate(roots):
            for b in roots[i+1:]:
                pa,pb=PurePosixPath(a),PurePosixPath(b)
                if pa.is_relative_to(pb) or pb.is_relative_to(pa):
                    raise ValueError('单目录与各映射目录必须分开，不能相同或嵌套：'+a+' 与 '+b)
        return self
    @field_validator('sync_mode')
    @classmethod
    def mode_ok(cls,v):
        if v not in ('normal','fast'):raise ValueError('同步模式仅支持 normal 或 fast')
        return v
    @field_validator('strm_link_format')
    @classmethod
    def link_format_ok(cls,v):
        if v not in ('pick_code','pick_code_name'):raise ValueError('直连格式仅支持 pick_code 或 pick_code_name')
        return v
    @field_validator('strm_base_url')
    @classmethod
    def base_url_ok(cls,v):
        v=v.strip().rstrip('/')
        if v:
            p=urlsplit(v)
            if p.scheme not in ('http','https') or not p.hostname or p.query or p.fragment or p.path not in ('','/'):
                raise ValueError('直连地址格式：http(s)://域名或IP[:端口]')
            v=f'{p.scheme}://{p.netloc}'
        return v
    @field_validator('strm_exist_policy')
    @classmethod
    def exist_policy_ok(cls,v):
        if v not in ('overwrite','skip'):raise ValueError('本地存在策略仅支持 overwrite 或 skip')
        return v
    @field_validator('strm_conflict_policy')
    @classmethod
    def conflict_policy_ok(cls,v):
        if v not in ('auto','abort'):raise ValueError('同名冲突策略仅支持 auto 或 abort')
        return 'auto'  # 2.7.7: 'abort' is retired; accepted for old backups
    play_mode:str=Field('302',title='STRM 播放方式',description='302=播放器直连 115 CDN 拉流（默认，服务器不中转视频字节）；proxy=MovieClaw 服务器中转拉流（兼容所有播放器与需附加 Cookie 的受限直链，302 播放异常时优先切换此项）。')
    @field_validator('play_mode')
    @classmethod
    def play_mode_ok(cls,v):
        if v not in ('302','proxy'):raise ValueError('播放方式仅支持 302 或 proxy')
        return v
    prefetch_episodes:int=Field(2,ge=0,le=5,title='预取后续剧集直链（集）',description='播放剧集时，用同一播放器的 UA 提前签发后面 N 集的 115 直链并缓存到链接自身的有效期，下一集点开即播；0=关闭。只对同一文件夹里有多个视频（剧集季目录）生效，电影不受影响。')
    link_cache_minutes:int=Field(10,ge=1,le=60,title='直链缓存时间（分钟）',description='签发的 115 直链在此时间内缓存复用：播放器拖动进度、读取字幕轨等连续请求无需逐次签发（每次签发受 API 间隔限制，约 1~3 秒，正是字幕加载慢的根源）。播放中偶发直链过期报错时可调小此值。')
    @field_validator('sync_cron')
    @classmethod
    def cron_ok(cls,v):
        v=' '.join(str(v).split())
        try:Cron(v)
        except ValueError as e:raise ValueError('cron 表达式无效：'+str(e)) from None
        return v
    @field_validator('video_suffixes')
    @classmethod
    def suffixes_ok(cls,v):
        parts=[p.strip().lstrip('.').lower() for p in str(v).replace('，',',').split(',') if p.strip()]
        if not parts:raise ValueError('至少保留一个视频后缀')
        for p in parts:
            if not re.fullmatch(r'[a-z0-9]{1,10}',p):raise ValueError('后缀只能包含字母和数字：'+p)
        return ','.join(dict.fromkeys(parts))
    @field_validator('mappings')
    @classmethod
    def mappings_ok(cls,v):
        specs=mapping_specs(v)  # raises ValueError with a precise Chinese message
        import json as _json
        return _json.dumps(specs,ensure_ascii=False) if specs else ''

    cloud_cid:str=Field('',title='115 媒体库 CID（STRM 源目录）',description='填写 115 网盘中要同步的目录 ID，该目录下（含子目录）的视频按相对结构生成 STRM。扫码与同步都在自带网页里：MovieClaw 左侧边栏「115网盘」。')
    @field_validator('cloud_cid')
    @classmethod
    def cid_ok(cls,v):
        if v and not re.fullmatch(r'[0-9]{1,20}',v):raise ValueError('CID 必须为数字')
        return str(int(v)) if v else ''
    @field_validator('strm_directory')
    @classmethod
    def organized_path(cls,v):
        if v.strip()=='/':raise ValueError('不能使用系统根目录')
        v=v.strip().rstrip('/')
        if v and (not v.startswith('/') or '..' in v.split('/') or '\\' in v or '\x00' in v):raise ValueError('请填写不含 .. 的容器内绝对路径')
        return v

    cookie:SecretStr=Field(default_factory=lambda:SecretStr(''),title='115 Cookie（可选，优先使用扫码登录）',description='保存在宿主秘密设置；不写入 STRM 或日志。扫码登录入口：MovieClaw 左侧边栏「115网盘」，打开「115 扫码登录」区域即可生成二维码。')
    api_user_agent:str=Field('',title='115 API User-Agent（高级，手动填 Cookie 时用）',description='115 把凭据与签发时的 User-Agent 绑定。扫码登录自动使用一致的 UA，无需填写；手动粘贴 Cookie 时请填写「获取该 Cookie 时所用浏览器/客户端的 UA」，留空则使用 Mozilla/5.0（仅适配旧版网页 Cookie）。')
    @field_validator('api_user_agent')
    @classmethod
    def ua_ok(cls,v):
        v=v.strip()
        if len(v)>400 or any(c in v for c in ('\r','\n','\x00')):raise ValueError('User-Agent 过长或含非法字符')
        return v
    request_interval:float=Field(1.5,ge=0.3,le=30,title='115 API 请求间隔（秒）')
    retry_max:int=Field(5,ge=0,le=10,title='115 限速自动重试次数',description='115 返回「操作太频繁 / 请稍后再试 / 同类任务处理中」等限流错误，或网络抖动、HTTP 5xx/429 时，自动等待一段时间后继续（不会让整次同步失败）；达到该次数仍不恢复才报错。设 0 = 不重试（立即失败，等同旧行为）。')
    retry_cooldown:int=Field(30,ge=5,le=600,title='限速等待基础时长（秒）',description='首次等待该时长，之后按 2 倍递增（30 → 60 → 120…，单次最长 600 秒）。等待期间「115同步」页显示剩余时间，已生成的 STRM 保持不变、不会从头重来。')
    timeout:int=Field(30,ge=5,le=120,title='请求超时（秒）')
    page_size:int=Field(1000,ge=20,le=1150,title='每页条目数')
    max_entries:int=Field(0,ge=0,le=100000000,title='单次扫描最大条目数',description='0 = 不限制（默认）。可选的安全阀：单个映射目录内（含子目录）115 统计的文件总数超过该值时，在扫描第一页就停止并提示，防止误把整个网盘当媒体库同步。')
    allow_cloud_changes:bool=Field(False,title='允许修改网盘文件',description='启用后控制台可新建目录、改名、移动、复制或移到回收站；默认只读')
    @field_validator('cookie')
    @classmethod
    def validate_cookie(cls,v):
        if '\r' in v.get_secret_value() or '\n' in v.get_secret_value():raise ValueError('Cookie 不能包含换行')
        return v
class Mutation(BaseModel):
    action:str
    file_id:str=''
    parent_id:str='0'
    name:str=''
    confirmed:bool=False
    @field_validator('action')
    @classmethod
    def action_ok(cls,v):
        if v not in ('mkdir','rename','move','copy','delete'):raise ValueError('不支持此操作')
        return v
    @field_validator('file_id','parent_id')
    @classmethod
    def ids(cls,v):
        if v and not re.fullmatch(r'\d{1,20}',v):raise ValueError('ID 必须为数字')
        return v
    @field_validator('name')
    @classmethod
    def name_ok(cls,v):
        if v and (v in ('.','..') or any(c in v for c in ('/','\\','\x00'))):raise ValueError('名称不合规')
        return v

class LoginStart(BaseModel):
    device:str='wechatmini'
    @field_validator('device')
    @classmethod
    def device_ok(cls,v):
        # Retired desktop clients (os_*) are rejected with an actionable message:
        # 115 took those clients offline and their cookies are unusable.
        if v in RETIRED_DEVICES:raise ValueError('115 已下架电脑客户端（'+RETIRED_DEVICES[v]+'），该类型的凭据不可用；请改选微信小程序、115管理 App 或 TV 等类型')
        if v not in DEVICES:raise ValueError('不支持的设备类型')
        return v

# Fields the console may save back through the host settings API. Saving restarts
# the plugin, exactly like editing them on the MovieClaw settings page.
EDITABLE={'cookie','request_interval','timeout','page_size','max_entries','allow_cloud_changes','retry_max','retry_cooldown',
          'cloud_cid','strm_directory',
          'strm_link_format','strm_keep_suffix','strm_exist_policy','strm_conflict_policy','video_suffixes','min_video_mb',
          'sync_mode','scan_strategy','sync_cron','life_events','life_interval','sidecar_sync','strm_cleanup','strm_base_url',
          'mappings','api_user_agent','play_mode','link_cache_minutes','prefetch_episodes'}

class ConfigPayload(BaseModel):
    values:dict=Field(default_factory=dict)

class ResetPayload(BaseModel):
    fields:list[str]=Field(default_factory=list)

class PlayTestPayload(BaseModel):
    pickcode:str=Field(...,title='115 文件提取码',description='在「网盘文件」页点「诊断」自动带入')
    ua:str=Field('',title='播放器 User-Agent（可选）',description='填入播放器实际的 UA 可探测「按播放器 UA 签发+拉流」是否可用（302 的真实链路）；留空则探测通用组合')
    @field_validator('pickcode')
    @classmethod
    def pickcode_ok(cls,v):
        if not re.fullmatch('[a-zA-Z0-9]{6,40}',v):raise ValueError('提取码不合规')
        return v
    @field_validator('ua')
    @classmethod
    def ua_ok(cls,v):
        if '\r' in v or '\n' in v or len(v)>300:raise ValueError('UA 不合规')
        return v

class RewritePayload(BaseModel):
    old_base:str=Field('',title='旧地址（可选）',description='留空=替换所有旧地址；填写 http(s)://域名或IP[:端口] 时只替换来自该地址的 STRM')
    dry_run:bool=Field(True,title='预览模式',description='true=只统计可替换数量，不修改任何文件')
    @field_validator('old_base')
    @classmethod
    def old_base_ok(cls,v):
        v=v.strip().rstrip('/')
        if v:
            p=urlsplit(v)
            if p.scheme not in ('http','https') or not p.hostname or p.query or p.fragment or p.path not in ('','/'):
                raise ValueError('旧地址格式：http(s)://域名或IP[:端口]')
            v=f'{p.scheme}://{p.netloc}'
        return v.lower()

@plugin('p115-library',title='115 网盘与 STRM 媒体库',inject=(HOST_OPS,PLUGIN_DATA,PLUGIN_FILES,PLUGIN_ROUTES),permissions=OPS,config=None)
async def apply(ctx):
    logging.getLogger('httpx').setLevel(logging.WARNING)
    logging.getLogger('httpcore').setLevel(logging.WARNING)
    store=ctx.use(PLUGIN_DATA).scoped(ctx);files=ctx.use(PLUGIN_FILES).scoped(ctx)
    ops=await ctx.use(HOST_OPS).client(ctx);routes=ctx.use(PLUGIN_ROUTES)
    # All settings live in plugin-private storage; the host settings page stays empty by design.
    # ctx.config (manifest/host values) is only a migration fallback for installs older than 1.5.0.
    stored=await store.get('panel-settings',default={}) or {}
    # 2.7.6: the limit is an optional safety valve, unlimited (0) by default.
    # Both former defaults (100000 up to 2.7.0, 1000000 in 2.7.1-2.7.5) were
    # persisted with every save without the user choosing them -- lift them.
    if isinstance(stored,dict) and stored.get('max_entries') in (100000,1000000):
        stored['max_entries']=0
        await store.set('panel-settings',stored)
    migrated=dict(ctx.config) if isinstance(ctx.config,dict) else {}
    try:cfg=Settings(**{**migrated,**stored})
    except ValidationError:cfg=Settings()
    saved_cookie=await store.get('qr-cookie')
    if saved_cookie:cfg.cookie=SecretStr(saved_cookie)
    login=Login(cfg,store)
    drive=Drive(cfg)
    # 115 binds the credential to the minting UA: prefer the user's explicit UA,
    # then the UA recorded at QR-login mint time, else the legacy web default.
    drive.ua=cfg.api_user_agent or await store.get('qr-ua',default='') or 'Mozilla/5.0'
    life=LifeSTRM(cfg,drive,store,files,ops,routes,ctx)
    async def prefetch_log(message):
        await log_event(store,'info','播放',message)
    prefetcher=Prefetcher(cfg,drive,life.engine,prefetch_log)
    await life.boot()
    await log_event(store,'info','系统','插件已启动（面板日志栏开始记录）')
    admin=APIRouter()
    @admin.post('/login/start',operation_id='plugins.p115-library.login.start')
    async def login_start(body:LoginStart=LoginStart()):
        try:
            result=await login.start(body.device)
            await store.set('panel-device',body.device)
            return result
        except DriveError as e:raise HTTPException(502,str(e)) from None
    @admin.get('/login/qr',operation_id='plugins.p115-library.login.qr')
    async def login_qr():return Response(login.svg,media_type='image/svg+xml',headers={'Cache-Control':'no-store'})
    login_cache={'at':0.,'data':{}}
    @admin.get('/login/info',operation_id='plugins.p115-library.login.info',summary='当前登录账号与容量')
    async def login_info(refresh:bool=False):
        # Cached for 10 minutes so the panel's periodic refresh never hammers 115.
        if not cfg.cookie.get_secret_value():return {'logged_in':False}
        now=time.monotonic()
        if refresh or now-login_cache['at']>600:
            try:login_cache['data']=await drive.account();login_cache['at']=now
            except DriveError as e:login_cache['data']={'error':str(e)}
        return {'logged_in':True,**login_cache['data']}
    @admin.post('/login/poll',operation_id='plugins.p115-library.login.poll')
    async def login_poll():
        try:
            result=await login.poll()
            if result.get('state')=='logged_in':
                login_cache['at']=0.
                drive.ua=mint_ua(cfg)
                await log_event(store,'info','登录','扫码登录成功')
                # 2.1.1: wake the sync loop right away -- the first boot on a
                # fresh install parks in waiting_login, and without this kick
                # the first sync would wait a full life_interval after scanning.
                life.signal.set()
            return result
        except DriveError as e:
            await log_event(store,'error','登录','扫码登录失败：'+str(e))
            raise HTTPException(502,str(e)) from None
    @admin.get('/strm/status',operation_id='plugins.p115-library.strm.status')
    async def strm_status():
        # 2.1.1: parsed_mappings needs the account UID, so a fresh install
        # without a cookie must not touch it here -- the state line carries
        # waiting_login instead of the panel erroring out.
        has_cookie=bool(cfg.cookie.get_secret_value())
        mappings=life.engine.cfg.parsed_mappings if has_cookie else []
        state=dict(life.state)
        # Live progress while a sync is running: the engine keeps per-run
        # counters (seen/written/...) that the panel shows so a long walk
        # over a big library never looks like a frozen "同步中" again.
        if state.get('state')=='running':
            eng=life.engine.status
            state.update({k:eng.get(k,0) for k in ('seen','written','unchanged','sidecars','deleted','conflicts','skipped')})
            running_at=eng.get('running_at')
            if running_at:state['running_for']=int(time.time())-int(running_at)
            # 2.4.0: remaining cooldown is computed HERE, not in the browser --
            # throttle_until is wall-clock of THIS host, so using the client's
            # clock would drift by however skewed the user's machine is.
            until=eng.get('throttle_until') or 0
            if until:
                state['throttle_wait']=max(0,int(until)-int(time.time()))
                state['throttle_count']=eng.get('throttle_count',0)
                state['throttle_limit']=eng.get('throttle_limit',0)
                state['throttle_reason']=eng.get('throttle_reason','')
        # 2.6.0: surface how many directories are already checkpointed, so the
        # user can tell "断点没积累" (0) from "记录了但不跳过" (>0) at a glance.
        checkpoint_dirs=0
        if has_cookie:
            for mapping in mappings:
                ck=await store.get('checkpoint:'+scope_key(mapping),default={})
                if isinstance(ck,dict):
                    if isinstance(ck.get('done'),dict):checkpoint_dirs+=len(ck['done'])
                    elif isinstance(ck.get('count'),int):checkpoint_dirs+=ck['count']
        return {**state,'directory':cfg.strm_directory,'enabled':bool(mappings),'mapping_count':len(mappings),
                'life_events':cfg.life_events,'cron':cfg.sync_cron,'sync_mode':cfg.sync_mode,
                'checkpoint_dirs':checkpoint_dirs,
                'mapping_specs':mapping_specs(cfg.mappings) if cfg.mappings else []}
    @admin.post('/strm/reconcile',operation_id='plugins.p115-library.strm.reconcile')
    async def strm_reconcile():await life.after_upload();return {'requested':True}
    @admin.post('/strm/full',operation_id='plugins.p115-library.strm.full',summary='开始全量同步')
    async def strm_full():
        if life.lock.locked():raise HTTPException(409,'同步正在进行，请稍后')
        life.run_task=ctx.task(life.tick(force=True,mode='full'),name='115-strm-full')
        return {'started':True,'mode':'full'}
    @admin.post('/strm/incremental',operation_id='plugins.p115-library.strm.incremental',summary='开始增量同步')
    async def strm_incremental():
        if life.lock.locked():raise HTTPException(409,'同步正在进行，请稍后')
        life.run_task=ctx.task(life.tick(force=True,mode='incremental'),name='115-strm-incremental')
        return {'started':True,'mode':'incremental'}
    @admin.post('/strm/stop',operation_id='plugins.p115-library.strm.stop',summary='停止当前同步（已写 STRM 与断点进度保留）')
    async def strm_stop():
        """2.7.3: stops the running sync no matter who started it (panel, cron,
        115 event) and waits until it has really ended. Written STRM files are
        kept; nothing is queued to restart it."""
        result=await life.stop()
        if result is None:
            return {'ok':False,'message':'当前没有正在进行的同步'}
        if not result:
            return {'ok':True,'message':'已发出停止指令，正在收尾（等待当前请求返回），几秒后状态会变为「已停止」'}
        return {'ok':True,'message':'同步已停止，已写入的 STRM 保留'}
    @admin.post('/strm/clear-checkpoint',operation_id='plugins.p115-library.strm.clear-checkpoint',summary='清除断点记录（下次全量从头完整扫描）')
    async def strm_clear_checkpoint():
        """2.6.0: drop the local resume checkpoints so the next full sync walks
        every directory from scratch (use after reorganizing the cloud library
        or when you want a guaranteed-complete pass)."""
        if life.lock.locked():raise HTTPException(409,'同步正在进行，请先停止')
        try:
            mappings=life.engine.cfg.parsed_mappings
        except DriveError as e:raise HTTPException(400,str(e)) from None
        cleared=0
        for mapping in mappings:
            key='checkpoint:'+scope_key(mapping)
            root=await store.get(key,default=None)
            if root is None:continue
            # 2.6.2: checkpoint is chunked -- drop the root plus every node.
            nodes=(root.get('nodes') or []) if isinstance(root,dict) else []
            for nk in nodes:
                try:await store.delete(nk)
                except Exception:pass
            await store.delete(key);cleared+=1
        try:await log_event(store,'info','同步','已清除 %d 个映射的断点记录，下次全量同步将从头完整扫描'%cleared)
        except Exception:pass
        return {'ok':True,'cleared':cleared}
    @admin.post('/strm/rewrite',operation_id='plugins.p115-library.strm.rewrite',summary='存量 STRM 地址替换（外网地址变更后无需重新同步）')
    async def strm_rewrite(body:RewritePayload):
        """2.1.0: address migration for existing STRM files, driven only by the
        plugin's local manifest -- no 115 traffic, so playback recovers in
        seconds after the external address changes. The target origin is
        exactly what a new sync would sign (strm_base_url override included),
        so the rewritten files and a later sync agree and nothing churns."""
        # 2.1.1: the manifest keys are account-scoped, so without a cookie the
        # records cannot be located at all -- answer with guidance instead of
        # the raw "请先扫码登录 115" that made users think QR login was gone.
        if not cfg.cookie.get_secret_value():
            raise HTTPException(400,'地址替换不访问 115，但同步记录按 115 账号区分，需先登录才能定位记录：请在面板「115账号」页扫码登录或粘贴 Cookie')
        if not life.engine.cfg.parsed_mappings:raise HTTPException(400,'请先配置同步映射（多目录映射或单目录 CID + STRM 目录）')
        if life.engine.lock.locked():raise HTTPException(409,'同步正在进行，请稍后再试')
        # 2.3.1: with a strm直连地址 the origin comes entirely from the override,
        # so a RELATIVE link suffices and the host's「外部访问地址」is not required;
        # without one, absolute signing raises the host's ValueError -- guide the
        # user to either fix instead of the old opaque 502.
        base_override=(getattr(cfg,'strm_base_url','') or '').strip()
        try:sample=await routes.sign(ctx,'/play/EXAMPLE',absolute=not base_override)
        except ValueError:
            raise HTTPException(400,'MovieClaw 未配置外部访问地址（设置 → 应用），无法确定替换目标地址。'
                '修复任选其一：① 在 MovieClaw「设置 → 应用」填写外部访问地址；'
                '② 在本插件「STRM配置 → strm直连地址」填写外网可达地址后再预览') from None
        except Exception:raise HTTPException(502,'无法获取当前签发地址，请稍后重试') from None
        sample=rebase_url(sample,base_override)
        u=urlsplit(sample)
        if u.scheme not in ('http','https') or not u.netloc:raise HTTPException(502,'当前签发地址无效：'+sample[:200])
        target=u.scheme+'://'+u.netloc
        try:result=await life.engine.rewrite_origins(target,body.old_base,body.dry_run)
        except DriveError as e:raise HTTPException(502,str(e)) from None
        await log_event(store,'info','同步','STRM 地址替换'+('（预览，未修改文件）' if body.dry_run else '完成')
            +'：目标 '+target+'，'+('可替换 ' if body.dry_run else '已替换 ')+str(result['rewritten'])
            +'，已最新 '+str(result['unchanged'])+'，共 '+str(result['scanned'])+' 条记录'
            +('（文件缺失 '+str(result['missing'])+'，已修改跳过 '+str(result['modified'])+'）' if result['missing'] or result['modified'] else ''))
        return result
    async def apply_settings(patch,origin='保存'):
        """Validate, persist to plugin-private storage, then hot-apply without a host restart."""
        unknown=set(patch)-EDITABLE
        if unknown:raise HTTPException(400,'不支持的配置项：'+','.join(sorted(unknown)))
        candidate={f:getattr(cfg,f) for f in EDITABLE}
        candidate.update({f:v for f,v in patch.items() if f!='cookie'})
        try:new=Settings(**candidate)
        except ValidationError as e:
            detail='；'.join('.'.join(str(x) for x in err['loc'])+'：'+err['msg'] for err in e.errors()[:3])
            raise HTTPException(400,'设置校验未通过：'+detail) from None
        cookie=patch.get('cookie')
        # Persist first: a crash mid-apply must not lose the saved settings.
        # The cookie is deliberately excluded here — it lives in its own secret slot.
        await store.set('panel-settings',{f:getattr(new,f) for f in EDITABLE if f!='cookie'})
        if 'cookie' in patch:
            if cookie:await store.set('qr-cookie',cookie,secret=True)
            else:await store.delete('qr-cookie')
            login_cache['at']=0.
        for f in EDITABLE:setattr(cfg,f,getattr(new,f))
        if 'cookie' in patch:cfg.cookie=SecretStr(cookie) if cookie else SecretStr('')
        drive.ua=cfg.api_user_agent or await store.get('qr-ua',default='') or 'Mozilla/5.0'
        await life.restart()
        await log_event(store,'info','配置',f'配置已{origin}并生效')
        return {'applied':True}
    @admin.get('/config',operation_id='plugins.p115-library.config.show',summary='控制台可见的当前配置')
    async def config_show():
        values={k:getattr(cfg,k) for k in sorted(EDITABLE) if k!='cookie'}
        # 2.3.1: same fallback as the sync engine -- a relative link is enough
        # when strm直连地址 carries the origin, so the panel stays usable on a
        # host that has no external URL configured.
        _base=(getattr(cfg,'strm_base_url','') or '').strip()
        try:sample=await routes.sign(ctx,'/play/EXAMPLE',absolute=not _base)
        except Exception:sample='/api/v1/plugins/p115-library/play/EXAMPLE'
        sample=rebase_url(sample,_base)
        stored_device=await store.get('panel-device',default='wechatmini')
        # Sanitize values persisted by 1.5.x (e.g. the retired os_* desktop types).
        device=stored_device if stored_device in DEVICES else 'wechatmini'
        if device!=stored_device:await store.set('panel-device',device)
        return {'values':values,'secrets_set':['cookie'] if cfg.cookie.get_secret_value() else [],
                'sample_play_url':sample,'devices':DEVICES,
                'device':device,
                'defaults':{'sync_cron':'*/20 8-23 * * *','video_suffixes':DEFAULT_SUFFIXES}}
    @admin.post('/config',operation_id='plugins.p115-library.config.save',summary='保存配置（存入插件私有数据，立即生效，无需重启）')
    async def config_save(body:ConfigPayload):
        if not body.values:return {'applied':True}
        return await apply_settings(dict(body.values))
    @admin.post('/config/reset',operation_id='plugins.p115-library.config.reset',summary='重置指定配置项为默认值（立即生效）')
    async def config_reset(body:ResetPayload):
        unknown=set(body.fields)-EDITABLE
        if unknown:raise HTTPException(400,'不支持的配置项：'+','.join(sorted(unknown)))
        if not body.fields:return {'applied':True}
        defaults=Settings();patch={}
        for name in body.fields:
            value=getattr(defaults,name)
            patch[name]=value.get_secret_value() if isinstance(value,SecretStr) else value
        return await apply_settings(patch)
    @admin.get('/login/diag',operation_id='plugins.p115-library.login.diag',summary='诊断服务器到 115 扫码接口的连通性')
    async def login_diag():
        start=time.monotonic();results={}
        async with httpx.AsyncClient(transport=net.http_transport('p115-library'),timeout=15,trust_env=False,follow_redirects=False) as c:
            # Two channels are both required for a working QR login: qrcodeapi
            # mints the token, passportapi exchanges it for the cookie.
            try:
                r=await c.get('https://qrcodeapi.115.com/api/1.0/web/1.0/token/')
                try:ok=bool(r.json().get('state')) if r.status_code==200 else False
                except Exception:ok=False
                results['token']=({'ok':ok,'http':r.status_code,'detail':'连接正常' if ok else f'扫码接口响应异常（HTTP {r.status_code}）'} if ok
                                  else {'ok':False,'http':r.status_code,'detail':'token 接口不可用（HTTP '+str(r.status_code)+'）'})
            except Exception:
                results['token']={'ok':False,'detail':'连接失败：无法访问 qrcodeapi.115.com（超时/DNS/代理问题）'}
            try:
                r=await c.get('https://passportapi.115.com/app/1.0/web/1.0/check/sso')
                results['confirm']={'ok':r.status_code in (200,401,403),'http':r.status_code,
                                    'detail':'连接正常' if r.status_code in (200,401,403) else f'确认接口返回 HTTP {r.status_code}'}
            except Exception:
                results['confirm']={'ok':False,'detail':'连接失败：无法访问 passportapi.115.com（扫码确认走此域名，不通则无法换取 Cookie）'}
        if cfg.cookie.get_secret_value():
            # Probe the live cookie with the SAME UA the plugin uses everywhere --
            # this directly answers "为什么我扫码成功但接口还是 990001".
            try:
                await drive.request('GET','https://webapi.115.com/files/index_info')
                results['cookie']={'ok':True,'detail':'当前 Cookie 有效（UA 一致，可直接使用）'}
            except DriveError as e:
                results['cookie']={'ok':False,'detail':'当前 Cookie 被拒绝：'+str(e)[:200]}
        return {'ok':all(x['ok'] for x in results.values()),'elapsed':round(time.monotonic()-start,2),'checks':results}
    @admin.get('/dirs',operation_id='plugins.p115-library.dirs',summary='115 目录选择器数据')
    async def dirs(cid:str='0'):
        if not re.fullmatch(r'\d{1,20}',cid):raise HTTPException(400,'目录 ID 不合规')
        try:return await drive.dirs(cid)
        except DriveError as e:raise HTTPException(502,str(e)) from None
    @admin.get('/sync/next-runs',operation_id='plugins.p115-library.sync.next-runs',summary='cron 最近 5 次运行时间')
    async def sync_next_runs(expr:str=''):
        expression=' '.join((expr or cfg.sync_cron).split())
        try:cron=Cron(expression)
        except ValueError as e:raise HTTPException(400,'cron 表达式无效：'+str(e)) from None
        return {'runs':[datetime.fromtimestamp(t).strftime('%Y-%m-%d %H:%M:%S') for t in cron.next_runs(time.time(),5)]}
    @admin.get('/sync/history',operation_id='plugins.p115-library.sync.history',summary='最近 20 次同步运行记录')
    async def sync_history():
        rows=await store.get('sync-history',default=[]) or []
        return {'history':list(reversed(rows))}
    @admin.get('/log/list',operation_id='plugins.p115-library.log.list',summary='运行日志：事件 + 同步历史')
    async def log_list():
        events=list(reversed(await store.get('runtime-log',default=[]) or []))
        history=list(reversed(await store.get('sync-history',default=[]) or []))
        return {'events':events,'history':history}
    @admin.get('/config/export',operation_id='plugins.p115-library.config.export',summary='导出配置备份（不含 Cookie）')
    async def config_export():
        values={f:getattr(cfg,f) for f in sorted(EDITABLE) if f!='cookie'}
        body=json.dumps({'plugin':'p115-library','version':'2.8.2','values':values},ensure_ascii=False,indent=2)
        return Response(body,media_type='application/json',headers={'Content-Disposition':'attachment; filename="p115-library-config.json"'})
    @admin.post('/config/import',operation_id='plugins.p115-library.config.import',summary='导入配置备份（校验后立即生效，Cookie 不受影响）')
    async def config_import(body:ConfigPayload):
        values={k:v for k,v in dict(body.values).items() if k!='cookie'}
        if not values:raise HTTPException(400,'备份文件中没有可导入的配置')
        return await apply_settings(values,'导入')
    @admin.get('/panel' ,operation_id='plugins.p115-library.panel',summary='115 网盘控制台')
    async def panel():return HTMLResponse(PANEL)
    @admin.get('/status',operation_id='plugins.p115-library.status',summary='同步状态')
    async def status():return {'configured':bool(cfg.cookie.get_secret_value()),'allow_cloud_changes':cfg.allow_cloud_changes}
    @admin.get('/browse',operation_id='plugins.p115-library.browse',summary='浏览 115 目录')
    async def browse(cid:str='0',offset:int=0):
        if not re.fullmatch(r'\d{1,20}',cid) or offset<0:raise HTTPException(400,'目录或分页不合规')
        try:return await drive.list(cid,offset)
        except DriveError as e:raise HTTPException(502,str(e)) from None
    @admin.get('/download/{pickcode}',operation_id='plugins.p115-library.download',summary='115 文件下载直链')
    async def download(pickcode:str,request:Request):
        try:
            if cfg.play_mode=='proxy':
                url,sign_ua,sign_headers=await drive.link_signed(pickcode,allow_cookie_url=True)
                await log_event(store,'info','播放','下载直链（中转）：'+pickcode)
                return await proxy_stream(url,sign_ua,sign_headers,request,pickcode=pickcode)
            url,_,_=await drive.link_signed(pickcode,ua=request.headers.get('user-agent',''))
            await log_event(store,'info','播放','下载直链（302 → '+str(urlsplit(url).hostname)+'）：'+pickcode)
            return RedirectResponse(url,status_code=302)
        except DriveError as e:
            await log_event(store,'error','播放','下载直链签发失败（'+pickcode+'）：'+str(e))
            raise HTTPException(502,str(e)) from None
    @admin.post('/play/test',operation_id='plugins.p115-library.play.test',summary='播放诊断：直链签发与 CDN 拉流探测')
    async def play_test(body:PlayTestPayload):
        """One-click ground truth for 302 problems: which signing protocol+UA
        works (m115 is alist's, RSA is p115client's legacy), and which UA/Cookie
        combos the CDN actually accepts for this link."""
        report={'pickcode':body.pickcode,'sign':[],'cdn':[]}
        url=None;sign_ua='';sign_headers={}
        probes=[('m115·空 UA（p115client/115driver 默认）','m115',''),
                ('m115·凭据 UA（当前登录 UA）','m115',drive.ua)]
        if body.ua:probes=[('m115·播放器 UA（302 真实链路，alist 模型）','m115',body.ua),('RSA·播放器 UA','rsa',body.ua)]+probes
        probes+=[('RSA·空 UA（p115client 老协议）','rsa',''),('RSA·凭据 UA','rsa',drive.ua)]
        for label,proto,ua in probes:
            try:
                signer=drive.sign_m115 if proto=='m115' else drive.sign_once
                url,sign_ua,sign_headers=await signer(body.pickcode,ua=ua,allow_cookie_url=True)
                f=parse_qs(urlsplit(url).query).get('f',[''])[0]
                report['sign'].append({'label':label,'ok':True,'f':f,'host':urlsplit(url).hostname,
                                       'url_header':sorted(sign_headers)})
                break
            except DriveError as e:
                report['sign'].append({'label':label,'ok':False,'error':str(e)})
        if url:
            async with httpx.AsyncClient(transport=net.http_transport('p115-library'),timeout=cfg.timeout,trust_env=False,follow_redirects=True) as client:
                # 1.7.4: the signing UA must be replayed EXACTLY -- even when
                # empty. Setting no User-Agent at all makes httpx send its
                # python-httpx default, which the UA-bound CDN link rejects
                # (the 1.7.3 diagnostic's all-403 matrix was exactly this bug).
                ua_label='签发 UA（空）' if sign_ua=='' else '签发 UA（'+sign_ua[:24]+'）'
                cdn_uas=[(ua_label,sign_ua)]
                if sign_ua!='':cdn_uas.append(('空 UA',''))
                if body.ua and body.ua not in (sign_ua,''):cdn_uas.append(('播放器 UA（'+body.ua[:24]+'）',body.ua))
                cdn_uas.append(('典型播放器 UA（Infuse）','Infuse/8.0'))
                if drive.ua and drive.ua not in [u for _,u in cdn_uas]:cdn_uas.append(('凭据 UA',drive.ua))
                # 中转模拟 = 服务器实际会发的头：签发 UA + 响应 url.header + 账号 Cookie
                merged=dict(sign_headers or {});merged['User-Agent']=sign_ua;merged['Cookie']=cfg.cookie.get_secret_value()
                try:
                    probe=await client.get(url,headers={**merged,'Range':'bytes=0-1023'})
                    status=probe.status_code
                except Exception:status=0
                report['cdn'].append({'label':'服务器中转模拟（签发 UA＋响应头＋Cookie）','status':status,'ok':status in (200,206)})
                for label,ua in cdn_uas:
                    for with_cookie in (False,True):
                        headers={'Range':'bytes=0-1023','User-Agent':ua}
                        if with_cookie:headers['Cookie']=cfg.cookie.get_secret_value()
                        try:
                            probe=await client.get(url,headers=headers)
                            status=probe.status_code
                        except Exception:status=0
                        report['cdn'].append({'label':label+('＋Cookie' if with_cookie else ''),'status':status,'ok':status in (200,206)})
        passed=sum(1 for c in report['cdn'] if c['ok'])
        ok_sign=next((s for s in report['sign'] if s['ok']),None)
        await log_event(store,'info' if url else 'error','播放',
                        '播放诊断：签发'+(('成功（'+ok_sign['label']+'，f='+ok_sign['f']+'，'+ok_sign['host']+'）') if url else '全部失败')+
                        ((', 响应头字段：'+','.join(ok_sign['url_header']) if ok_sign and ok_sign.get('url_header') else '') if url else '')+
                        ('；CDN 探测 '+str(passed)+'/'+str(len(report['cdn']))+' 项通过' if url else ''))
        return report
    @admin.post('/files',operation_id='plugins.p115-library.files',summary='修改 115 文件（需设置开启及明确确认）')
    async def mutate(body:Mutation):
        if not cfg.allow_cloud_changes or not body.confirmed:raise HTTPException(403,'网盘修改未开启或未确认')
        if body.action!='mkdir' and not body.file_id:raise HTTPException(400,'缺少文件 ID')
        if body.action in ('mkdir','rename') and not body.name:raise HTTPException(400,'缺少名称')
        try:
            await drive.mutate(body.action,body.file_id,body.parent_id,body.name)
            return {'success':True}
        except DriveError as e:raise HTTPException(502,str(e)) from None
    routes.mount(ctx,admin)
    public=APIRouter()
    play_log_at={}
    async def proxy_stream(url,sign_ua,sign_headers,request,pickcode='',name=''):
        """Relay the 115 CDN through the plugin (alist/OpenList proxy mode).

        The CDN URL is bound to the UA that minted it (the 1.7.3 on-device
        diagnostic: every other UA got 403) and often also to headers from
        the downurl response's url.header field, plus the account Cookie
        (alist's buildDownloadHeaders always attaches it). We are the
        fetcher here, so we replay all of them. Range requests pass
        through so players can seek.

        1.7.5: a CDN 401/403/410 on a cached or fresh link is treated as an
        expired URL -- the link cache is dropped and the link re-signed once
        before giving up. Completion is logged (status, bytes, duration) so
        the runtime log shows what the player actually pulled.
        """
        def build(u,ua,hdrs):
            headers={'User-Agent':ua}
            for k,v in (hdrs or {}).items():headers.setdefault(k,v)
            headers['Cookie']=cfg.cookie.get_secret_value()
            rng=request.headers.get('range')
            if rng:headers['Range']=rng
            return headers
        client=httpx.AsyncClient(transport=net.http_transport('p115-library'),timeout=httpx.Timeout(cfg.timeout,read=120.),trust_env=False,follow_redirects=True)
        try:upstream=await client.send(client.build_request(request.method,url,headers=build(url,sign_ua,sign_headers)),stream=True)
        except Exception:await client.aclose();raise DriveError('115 直链连接失败（网络不可达或直链已过期）') from None
        if upstream.status_code in (401,403,410) and pickcode:
            try:await upstream.aclose()
            except Exception:pass
            drive.invalidate_links(pickcode)
            try:
                url,sign_ua,sign_headers=await drive.link_signed(pickcode,allow_cookie_url=True,fresh=True)
                if name:await log_event(store,'warn','播放','中转直链被拒（HTTP '+str(upstream.status_code)+'），已重新签发重试')
                upstream=await client.send(client.build_request(request.method,url,headers=build(url,sign_ua,sign_headers)),stream=True)
            except DriveError:
                await client.aclose();raise
        relay={}
        for name_ in ('content-type','content-length','content-range','accept-ranges','last-modified','etag'):
            value=upstream.headers.get(name_)
            if value is not None:relay[name_]=value
        status=upstream.status_code;started=time.monotonic();sent=0;complete=False
        async def gen():
            nonlocal sent,complete
            try:
                if request.method!='HEAD':
                    async for chunk in upstream.aiter_bytes():
                        sent+=len(chunk);yield chunk
                complete=True
            finally:
                await upstream.aclose();await client.aclose()
                if name and request.method!='HEAD':
                    # Best-effort: never hang stream teardown on the log write.
                    try:
                        await asyncio.wait_for(log_event(store,'info' if complete else 'warn','播放',
                            '中转拉流：'+name+'，HTTP '+str(status)+'，'+fmt_size(sent)+'，%.1fs%s'%(time.monotonic()-started,'' if complete else '（中断）')),3)
                    except Exception:pass
        return StreamingResponse(gen(),status_code=status,headers=relay)
    @public.api_route('/play/{fid}',methods=['GET','HEAD'],operation_id='plugins.p115-library.play',summary='验签后换取 115 直链播放')
    async def play(fid:str,request:Request):
        # 2.7.3: sharded play index (pending records are served from memory).
        record=await life.engine.play_index.get(fid) if re.fullmatch(r'[0-9a-f]{24}',fid) else None
        if not record and re.fullmatch(r'[0-9a-f]{24}',fid) and cfg.cookie.get_secret_value():
            # 2.7.10: written right before a hard exit, record not flushed yet --
            # recover it from the saved sync progress.
            record=await life.engine.play_fallback(fid)
        if not record:raise HTTPException(404,'文件不存在')
        name=str(record.get('name') or fid);pickcode=str(record.get('pickcode') or '')
        now=time.monotonic()
        try:
            if cfg.play_mode=='proxy':
                # Server-side fetch: sign with our own chain and replay the
                # signing UA + url.header + account Cookie on the CDN fetch.
                # 1.7.5: the signed link is cached, so the many connections a
                # player opens when reading MKV indexes / subtitle tracks no
                # longer re-sign (and re-wait on the API interval) each time.
                url,sign_ua,sign_headers=await drive.link_signed(pickcode,allow_cookie_url=True)
                info=drive.last_sign
                if not info.get('cached'):
                    await log_event(store,'info','播放','中转播放：'+name+'（'+info['proto']+' 签发 '+'%.1fs'%info['elapsed']+'）')
                prefetcher.schedule(fid,'',True)  # 2.8.0: next episodes, in the background
                return await proxy_stream(url,sign_ua,sign_headers,request,pickcode=pickcode,name=name)
            # 302 (alist model): sign with THIS requester's UA -- the 1.7.3
            # on-device diagnostic proved f=1 links are bound to the signing
            # UA, and the player that follows our redirect fetches the CDN with
            # this same UA. Only if 115 rejects it do we fall back to an
            # empty-UA link (which then typically requires proxy mode).
            url,_,_=await drive.link_signed(pickcode,ua=request.headers.get('user-agent',''))
            info=drive.last_sign
            parts=urlsplit(url)
            if not info.get('cached'):
                f=parse_qs(parts.query).get('f',[''])[0]
                await log_event(store,'info','播放','302 直连：'+name+'（'+info['proto']+' 签发 '+'%.1fs'%info['elapsed']+'，f='+f+'，'+str(parts.hostname)+'）')
            elif now-play_log_at.get(fid,0.)>60.:
                # Cache hits repeat for every seek/subtitle read -- one line a
                # minute keeps the log useful without drowning everything else.
                play_log_at[fid]=now
                await log_event(store,'info','播放','302 直连：'+name+'（复用缓存直链，'+str(parts.hostname)+'）')
            # 2.8.0: sign the next episodes with THIS player's UA, in the background
            prefetcher.schedule(fid,request.headers.get('user-agent',''),False)
            return RedirectResponse(url,status_code=302)
        except DriveError as e:
            # Playback failures land in the runtime log so 302 problems are
            # diagnosable from the panel instead of a silent player error.
            await log_event(store,'error','播放','STRM 播放失败（'+name+'，提取码 '+pickcode+'）：'+str(e))
            raise HTTPException(502,str(e)) from None
    routes.mount(ctx,public,zone='public')
