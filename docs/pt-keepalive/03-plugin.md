# 03 插件设计（plugins/pt-keepalive/）

> 实现于 2026-10-18，2026-10-18 复查后修订；2026-10-20 M-Team 改为账密 + TOTP 自登录；
> 2026-10-22 宿主不再跟随站点 3xx，插件自己处理跳转（0.2.1）。
> 以代码为准，本文记录结构与判据。

## 目录结构（本仓库）

```
plugins/pt-keepalive/
├── movieclaw-plugin.toml        # 清单（extra=forbid，字段收着写）
├── store.json                   # 本仓库索引元数据（作者/主页/许可证=见仓库许可证）
├── README.md                    # 面向用户：做什么、设置、通知、风险
├── CHANGELOG.md
├── pt_keepalive/
│   ├── __init__.py              # @plugin 入口 + apply()（只登记）
│   ├── events.py                # 事件与载荷（清单 [[events]] 一一对应）
│   ├── settings.py              # Settings(pydantic)：三个开关 + M-Team 账号/密码/TOTP
│   ├── runner.py                # 保活轮：site.list → 逐站（代请求 / 自登录）→ 状态机 → 事件
│   ├── mteam.py                 # M-Team：签名、TOTP、登录、刷新最后浏览时间
│   ├── result.py                # 结果模型 + 页面证据判定纯函数（可单测）
│   ├── sites/__init__.py        # 分派 + NexusPHP 模板 + 3xx Location 的判定（M-Team 不走这里）
│   └── state.py                 # PLUGIN_DATA 状态机读写
└── tests/
    ├── test_result.py           # 页面判定正反例 + 状态机
    ├── test_mteam.py            # 签名/TOTP/响应分类/登录流程（全假响应）
    ├── test_runner.py           # 适配器 + mock 宿主操作的整轮
    └── test_redirects.py        # 3xx 跳转：登录页 / 同站跟随 / 跨主机、成环、超限的拒绝
```

零 vendor、零 `[dependencies]`：只用宿主自带库（`parsel` 用于剥 script/hidden 后的证据匹配；
`httpx` 用于 M-Team 的直连；`pydantic` 用于设置与事件载荷；`fastapi` 用于状态路由）。
包体约 56 KB（`check_plugin.py` 的口径，含 tests）。

## 清单（movieclaw-plugin.toml）要点

```toml
[requires]
"scheduled-tasks" = "^1.0"
"plugin-events" = "^1.0"
"plugin-data" = "^1.0"
"plugin-routes" = "^1.0"

[permissions]
operations = ["site.list", "site.catalog", "site.proxy.request"]
network = true
```

- `site.list`：拿已配置且 `usable` 的站点。
- `site.catalog`：`site.list` 不带展示名也不带域名，通知文案用 catalog 的 `display_name`（拿不到退回
  site_id），判断 3xx 是不是同站用 `base_url`（拿不到时只跟相对路径的跳转）。
- `site.proxy.request`：NexusPHP 系站点发请求的唯一通道。
- **`network = true`（2026-10-20 起）**：M-Team 的登录链路是插件自己直连 `api.m-team.io`，
  不再经宿主会话，所以这个权限现在是真需要的（此前「不声明」的理由已不成立）。
- 三个事件：`auth.expired`（失效，active）、`report.daily`（日报，passive，默认关）、
  `report.failures`（连续失败升级，active，默认开）。

## apply()：只登记

```python
@plugin(
    "pt-keepalive",
    inject=(HOST_OPS, PLUGIN_DATA, PLUGIN_EVENTS, PLUGIN_ROUTES),
    permissions=("site.list", "site.catalog", "site.proxy.request"),
    config=Settings,
)
async def apply(ctx: Context) -> None:
    # 1) 自检：HOST_OPS 目录里有 site.list + site.proxy.request 吗？没有 → 记日志、不登记任务
    # 2) 登记三个事件，拿 Publisher
    # 3) get /status 路由（admin 区）
    # 4) 贡献定时任务 pt-keepalive.run
```

- 自检只要求 `site.list` + `site.proxy.request`（真正必需的）；`site.catalog` 缺了只是通知里
  显示 site_id、且只跟相对路径的跳转，不降级整个插件。
- 定时任务默认 CRON `17 9 * * *`；用户在「设置→定时任务」改，配置模型里不放间隔。
- 30 秒上限内完成登记；保活轮本体是任务处理器，跑多久都行。

## 站点适配框架

### 分派

```python
SELF_LOGIN_SITES = ("mteam",)          # runner.py：先分流，走自己的登录通道

def adapter_for(site: SiteInfo) -> Adapter | None:
    if site.auth_type.strip().lower() == "apikey":  # sunnypt 等纯 API 站
        return None                                  # 本轮跳过，不拿 HTML 判定器读 JSON
    return nexusphp_adapter
```

`runner.run_site` 的判断顺序（**这就是「什么时候插件才自己登录」那条边界**）：

1. `site_id in SELF_LOGIN_SITES` → 自己登录（目前只有 mteam）；
2. 否则宿主没把这个站标成 `usable` → 记「跳过」，带上宿主的 `last_error` 与「改用 Cookie」的
   提示（`status` 是 `pending`/`verifying` 时改说「还在验证中」）；**不发请求、不告警**；
3. 否则走适配器代请求。

`site.list` 没有 `framework` 字段，分发按 `auth_type` 兜底：纯 API 站（`apikey`，宿主回的是
枚举值 `APIKEY`，比对不区分大小写）没有可判定页面，**未适配就跳过**，不冒充失败。
宿主 26 站里 mteam/sunnypt 是 API 框架，其余是 NexusPHP 页面；mteam 由第 1 步提前分流，
sunnypt 落在第 3 步的 `adapter_for` 返回 `None`。

**为什么不让插件也登录 NexusPHP**（用户 2026-10-20 拍板的边界）：宿主自己会维护站点登录态
（Cookie 过期它自动重登、重验），插件再登一次就是同一账号两套会话，容易互顶；而且宿主的
凭据能力（加密表、验证状态机、代理出口、每站限流）远强于插件。所以 NexusPHP 站开了两步
验证时，正确做法是**在「设置 → 站点」把该站改成 Cookie 方式**（宿主的 Cookie 路径从不访问
登录页，天然绕开动态码），而不是让插件去补登录。宿主侧支持 TOTP 的改动**刻意不做**——
「尽量不改主程序」。

### 步骤协议

```python
@dataclass
class Step:
    request: PlannedRequest   # method/path/base/headers/body
    judge: Callable[[StepResponse], SiteResult]
    signin_only: bool = False # 关了签到就整步跳过（连请求都不发）
```

- `nexusphp_adapter`：
  1. `GET index.php`（base=web）——确认登录态：登出链接 = 在线，密码框 = 失效；
  2. `GET attendance.php`（`signin_only=True`）——尝试签到。
  分两步是因为签到页不是每站都有；先确认登录态，签到页 404 只判「登录在线」。
  签到页判定用 `login_confirm=False`（该页通常没有导航栏，不要求登出链接）。

任一步判 `FAILED` / `NEED_HUMAN` 即停止后续步骤。

### 站点回 3xx

宿主的 `site.proxy.request` 不跟随跳转（见 `02-host-site-proxy.md`），所以每一步的请求都经
`runner._proxy_following`，不直接调 `_proxy_request`：

1. 响应不是 3xx → 原样交给步骤的 `judge`；
2. `Location` 的路径带 `login` → `{"_login_redirect": True}`，`run_site` 译成
   `FAILED` + `auth_expired`（同「页面出现登录表单」）；
3. 否则只有 GET 才跟：`redirect_target` 把 `Location` 解析成站内路径（相对路径按当前请求解析；
   绝对 URL 要求主机等于 `SiteInfo.base_url` 的主机，只取路径加查询串），再发一次，
   最多 `MAX_REDIRECTS = 3` 跳；
4. 跨主机、成环（同一个「从哪跳到哪」第二次出现；到过同一页不算，先处理后跳回同一页、
   跳去设 cookie 再跳回来都是正常流程）、超限、没有 `Location`、非 GET →
   `{"_error": 原因}`，`run_site` 记 `FAILED` 并带上原因，**不**置 `auth_expired`。

哨兵键 `_skip` / `_auth_expired` / `_login_redirect` / `_error` 都是 `_proxy_request` 与
`_proxy_following` 回给 `run_site` 的统一响应形态，不会出现在宿主的真实响应里。

## M-Team 自登录（不走代请求）

M-Team 在 `runner.SELF_LOGIN_SITES` 里先被分流，**不经过** `adapter_for`：
它要账号密码 + TOTP 自己登录（`pt_keepalive/mteam.py`），请求由插件进程直接发往
`https://api.m-team.io`（清单声明 `network = true`，出口跟随「设置 → 网络」里的
`pt-keepalive` 代理）。

一轮的顺序：

```
POST /api/login  {username, password, turnstile:"", _timestamp, _sgin}
  └ code 1001 → 再发一次，带 otpCode=<本地算的 TOTP>
      └ message=SUCCESS → 取响应头 Authorization(JWT) 与 Did
POST /api/member/profile          （确认会话有效，非 200 code 时按语义分类）
POST /api/member/updateLastBrowse （刷新最后浏览时间）
```

- 会话（`token` / `did` / `visitorid`）存 `PLUGIN_DATA` 的 `mteam_session` 键，
  `secret=True`（宿主编加密）；下一轮先用缓存令牌，401 再重登一次。
- `visitorid` 生成一次永久复用；`Did` 跟着每次响应的下发值滚动更新（不换设备）。
- 每个请求都带 `_sgin = base64(HMAC-SHA1("POST&{path}&{ms}", "HLkPcWmycL57mfJt"))`、
  `_timestamp`（13 位毫秒）与伪装头（`version=1.1.4` / `webversion=1140` /
  `referer=origin=https://kp.m-team.cc/` / `Ts`）。
- 响应分类（`Reply`）：`OK` / `NEED_TOTP` / `NO_CHANGE`（`{code:"1",message:"FAIL"}`）/
  `AUTH`（密码错、账号不存在、JWT 无效、HTTP 401）/ `SIGNATURE`（签名错误，插件需更新）/
  `RATE`（限流）/ `UNKNOWN`。只有 `AUTH` 置 `auth_expired`；`NO_CHANGE` 记「登录在线」。
- 登录被拒不重试（站点会扣账号尝试次数）。

## 结果判定（result.py，纯函数、单测重点）

```python
class Outcome(Enum):
    SIGNIN_OK = "signin_ok"     # 签到成功（含「今天已签」幂等）
    LOGIN_ONLY = "login_only"   # 登录有效，签到未确认/未做
    FAILED = "failed"           # 请求失败或证据不明
    NEED_HUMAN = "need_human"   # CF/WAF/验证码/答题，不算失败
    SKIPPED = "skipped"         # 本轮没访问（未适配/签到关闭），不算失败
```

判定纪律（照抄 MoviePilot 踩坑经验，全部纯函数可测）：

1. 先剥 `<script>/<style>/hidden/aria-hidden` 再匹配文本（防模板文本冒充提示）；
2. **登录失效证据优先**：出现密码输入框/登录表单 → `auth_expired`；
3. Cloudflare challenge 特征只看 `<title>` 与**可见**文本 → `need_human`
   （站点自己的 JS 里有 "just a moment" 不算）；HTTP 401/403 且无 challenge 特征 → `need_human`
   （WAF/IP 限制可能性大于凭据失效，不误报告警）；HTTP 5xx → `FAILED`；
4. 签到成功：可见文本命中成功关键词，且没有否定提示；
5. 「今天已签/请勿重复」→ 幂等成功；明确的否定提示（「回答错误」）优先于成功词；
6. 都不认识 → `LOGIN_ONLY`（保守），绝不虚报 `SIGNIN_OK`。

API 判定在 `mteam.py`（`classify` / `classify_update`）：先看 HTTP 401/403 与 `code` 401，
再看 `code 1001`（要 TOTP）、`code 0` / `message=SUCCESS`（成功），然后是签名错误、限流、
凭据类文案，最后落到 `UNKNOWN`（按失败记，不置 `auth_expired`）。

## 状态机（state.py，PLUGIN_DATA）

单键 `keepalive` 存全部站点（<64KB）：

```python
{sid: {outcome, detail, site_name, consecutive_failures,
       last_ok_at, alerted_expired, failure_alerted}}
```

- `SIGNIN_OK`/`LOGIN_ONLY` → 清零失败、清两个告警位；
- `FAILED` → 计数 +1；**连续 3 轮**进失败明细；
- `auth_expired` → 失败计数直接置阈值（失效必须让用户看见），发一次告警；
  **告警发送失败时不置位**，下一轮重试；
- `NEED_HUMAN`/`SKIPPED` → 只更新展示字段，不计数。

## 事件与通知

| 事件 | 触发 | 通知 | 默认 |
|---|---|---|---|
| `pt-keepalive.auth.expired` | 凭据失效（同一失效期一次） | 「{site_name} 登录已失效」 | 开（active） |
| `pt-keepalive.report.daily` | 每轮汇总（`daily_report=true`） | 「PT 保活日报：N 站在线」+ `{detail}` | 关（passive） |
| `pt-keepalive.report.failures` | 有站点连续 3 轮失败，每段失败只发一次 | 「N 站连续失败」+ `{detail}` | 开（active） |

通知模板只做字段替换、不拼接数组，所以 `DailyReport.detail` 在插件里先拼成一行；
`failures` 保留结构化形式给 Webhook / 别的插件。日报默认关闭，失败升级因此走
单独一条默认开的事件——否则关掉日报的用户永远看不到失败。

## 设置模型（settings.py）

```
enabled: bool = True                    # 总开关（False 时任务跑空转，直接返回）
signin_enabled: bool = True             # 签到动作开关（False → 只访问首页确认登录态）
daily_report: bool = False              # 日报开关（失效/失败告警不受它控制）
mteam_username: str = ""                # 馒头账号；留空 = 不做这一站
mteam_password: SecretStr | None        # 加密保存、界面不回显
mteam_totp_secret: SecretStr | None     # 两步验证的 Base32；otpauth:// 整条也认
```

不设「站点选择」：启用范围 = 宿主已配置且 usable 的站点全集。M-Team 的那三个字段是唯一
由插件自己保存的凭据——宿主只支持 API Key，而保活接口又不认 API Key。

## 路由（PLUGIN_ROUTES）

`GET /status` → 各站最近一轮结果（名称/结果/说明/连续失败数/上次成功/更新时间），
`operation_id="plugins.pt-keepalive.status"`（成为 `mclaw plugins pt-keepalive status`）。
返回类型 `dict[str, Any]`，模块顶部真实导入，满足进程外路由的注解要求。

诊断页：apply 自检 + 任务贡献 + 每轮日志（`ctx.logger`，中文，带 site_id）。
