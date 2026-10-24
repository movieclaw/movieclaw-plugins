# PT 站点保活（pt-keepalive）设计

## 目标

给 MovieClaw 做一个 PT 站点保活插件：对已配置的 PT 站点，周期性地维持登录态（防 Cookie 过期）、
执行签到（拿每日魔力/奖励）、并刷新「最后活跃时间」（防长期不活跃被删号，M-Team 属于此类）。
站点适配走「框架分型 + 常用站」路线，包体精简（零第三方依赖，仅用宿主自带库）。

参考项目：`ghcr.io/scjtqs2/mtlogin:edge`（Go，M-Team 专用保活）、MoviePilot autosignin、
flexget_qbittorrent_mod（fqm）、pt-tools 的站点探测分类学。

## 现状与痛点

**MovieClaw 宿主现状**（勘察结论，v0.37.0）：

- 站点体系很完整：26 个内置站点 YAML（`src/movieclaw_tracker/sites/configs/`），Cookie/API Key/账密
  三种凭据加密落库（`site_credential` 表，SecretBox/Fernet），`AuthManager` 自动「检查→失效→重认证→持久化」，
  `SiteAccessManager` 共享会话 1800 秒 TTL，`torrent_sync` 定时任务每 5 分钟~6 小时自适应访问各站。
- **防 Cookie 过期**这件事，宿主对已配置站点**客观上已经在做**（torrent_sync 周期访问 + AuthManager 重认证）。
- 但三件事没人做：**每日签到**（attendance.php）、**「最后浏览」触碰**（M-Team updateLastBrowse）、
  **掉线/待删号主动报警**（宿主的 FAILED 状态只亮系统红灯，不区分「凭据失效」与「账号待删」）。
- 主仓库设计文档 `docs/design/plugin-extension-model.md` 第 284 行已把「站点签到」列为已知缺口，原文：
  「主要缺口：站点能力开放给插件」——`SITES`/`SITE_ACCESS`/`SCHEDULER` 服务键是 INTERNAL，第三方拿不到。

**社区现状**（调研结论）：

- 「保活」是三件不同的事，各站需求不同：①防 Cookie 过期（访问即续期）；②每日签到拿魔力；
  ③防长期不活跃删号（M-Team 无签到页，靠 updateLastBrowse 刷新「最后访问」）。
- 社区架构共识（MoviePilot/fqm/pt-tools 三个独立项目收敛）：**框架分型基类 + 每站一适配文件 +
  域名匹配分发**；结果用**三态模型**（签到成功/仅登录成功/失败），不许「登录成功」冒充「签到成功」。
- 风险共识：PT 站普遍禁止脚本登录；程序化活跃未必被站方计为「用户活跃」（MoviePilot 官方警告）。

## 方案结论

**两条腿走路，用户已拍板「主程序先行」：**

1. **宿主侧（先做，主仓库）**：新增一个受控的「站点代请求」开放服务 `SITE_PROXY`
   （EXPERIMENTAL）+ 宿主操作 `site.proxy.request`。插件把「想对站点 X 发什么请求」交给宿主，
   宿主用**自己的已认证会话**（SiteAccessManager 的共享 BaseSite）执行，只把响应（状态码/头/正文）
   交回插件。**插件全程接触不到 Cookie/API Key 明文**——这是安全红线，也是能「复用登录态」的唯一路径。
   配套开放只读的 `site.list`（已配置站点及验证状态，本就脱敏）与 `site.catalog`。
2. **插件侧（本仓库 `plugins/pt-keepalive/`）**：通过 `site.proxy.request` 对已配置站点做
   保活/签到/触碰；通过 `PLUGIN_EVENTS` + 清单通知声明复用宿主推送（App+IM 全部通道一把梭）；
   通过 `SCHEDULED_TASKS` 注册表贡献定时任务（用户可在「设置→定时任务」改周期）；通过
   `movieclaw_sdk.net.http_transport` 走用户代理设置。Cookie 失效**不自动账密重登，只报警请人工更新**
   （自动登录是 PT 站封号风险最高的一档，砍掉它还省掉 TOTP/验证码/设备指纹一整条复杂度）。

## 用户已确认的决策

| # | 决策 | 内容 |
|---|---|---|
| 1 | 登录态路线 | **主程序先行**：先给主程序加 `SITE_PROXY` 开放能力（插件借宿主会话发请求、拿不到凭据明文），插件从第一版就零重复填写，完全复用宿主登录态 |
| 2 | 功能范围 | **保活+签到+触碰**：访问续 Cookie + attendance 签到（整体可关）+ M-Team 类「最后浏览」触碰；NexusPHP 系 Cookie 失效只报警不重登 |
| 3 | 站点广度 | **框架分型+常用站**：NexusPHP 通用模板一套（访问续期+签到+三态证据判定）+ 少数站专用动作，首版内置 15~20 个常用站；Cloudflare 拦截/答题/验证码站一律报「需人工处理」并跳过 |

> **2026-10-20 修正（决策 8 推翻）**：M-Team 的「触碰」**不能**借宿主的 API Key。
> 实测同一个 Key 调 `/api/member/profile` 成功、调 `/api/member/updateLastBrowse`
> 一律 `{"code":401,"Full authentication is required..."}`——那个接口只认网页会话 JWT。
> 现在这一站按用户指定的 mtlogin 路线，用**账号密码 + TOTP** 在插件里登录（凭据存在插件
> 设置、SecretStr + 宿主 SecretBox 加密），见 `03-plugin.md` 的「M-Team 自登录」一节。
>
> **2026-10-20 用户拍板的边界（贯穿整个插件的取舍）**：**尽量不改主程序**；只有像馒头这样
> 「宿主手里的凭据类型根本做不了保活」的站点，才在插件里另做一套登录态。其余站点一律借宿主
> 会话，插件不登录、也拿不到凭据。据此：
>
> - 宿主侧支持 NexusPHP 站的两步验证（`site_credential` 加 TOTP 列 + Provider 填动态码）
>   **不做**——用户在「设置 → 站点」把这类站改成 Cookie 方式即可绕开（宿主从不访问登录页）；
> - 插件不新增 NexusPHP 自登录；改为把「宿主因两步验证验证不过」这件事**讲清楚**（记跳过 +
>   带上宿主给的失败原因 + 提示改用 Cookie），而不是静默跳过。

## ⚑ 已拍板决定（替用户做的，实现时按此执行）

| # | 决定 | 理由 |
|---|---|---|
| 1 | 插件 id 定为 `pt-keepalive` | 符合 id 规则（小写+连字符）；语义直白；与目录名/`@plugin` 名一致 |
| 2 | `runtime = "process"`，只依赖 `movieclaw_sdk`，零 vendor/零第三方依赖 | GUIDE 硬性规范；宿主自带 `httpx`/`parsel`/`pydantic` 够用；签到判定用「HTTP+剥 script/hidden 后证据匹配」，不需要 playwright（包体精简诉求，预计几十 KB） |
| 3 | 触发回路用 `SCHEDULED_TASKS` 注册表贡献定时任务（宿主已有完整调度器、UI 改周期、历史落库），不自写 while 循环 | 复用宿主调度器是本插件「复用主程序」诉求的直接体现；`plugin_detail.py` 已把贡献任务展示在插件详情页 |
| 4 | 站点适配组织：`sites/` 目录每站一个 Python 适配函数注册进分发表，NexusPHP 站共用通用模板 | 社区三项目收敛的共识形态；比纯配置表（PTPP 式 148 站）可维护，比每站一个类轻 |
| 5 | NexusPHP 系 Cookie 失效不自动重登，发 `pt-keepalive.auth.expired` 事件报警，通知文案指引「站点设置→更新凭据」 | PT 站明确禁止脚本登录、封号风险最高（**仅限 NexusPHP 系**；M-Team 按决定 8 的修正走账密+TOTP 自登录） |
| 6 | 通知复用宿主：`[[notifications]]` 声明两条（成功日报可关 + 失效/失败告警默认开），audience=admins | 宿主通知链路（偏好/IM 扇出/APNs）全现成，mtlogin 的 6 套推送渠道一行都不用重写 |
| 7 | 结果三态建模照抄 MoviePilot `SiteResult{success, logged_in, completed}`：签到成功 ≠ 登录成功 ≠ 失败 | 「登录成功，签到未确认」不许冒充「签到成功」，这是社区踩出来的判据纪律 |
| 8 | **（2026-10-20 推翻重写）** M-Team 触碰 = 插件用**账号密码 + TOTP** 登录 `POST /api/login` 拿 JWT，再 `POST /api/member/profile` → `POST /api/member/updateLastBrowse`；凭据存插件设置（SecretStr，宿主 SecretBox 加密），会话存插件数据（`secret=True`）跨轮复用 | 原方案「借宿主 x-api-key 调 updateLastBrowse」实测一律 401（该接口只认网页会话），会把站点每轮误判成凭据失效；mtlogin 路线已实测可用（NAS 容器网络直连，登录→profile→update 全通） |
| 9 | 站点失败不熔断、不重试风暴：每轮只报「本轮结果」，连续 N 轮失败才升级报警（N=3） | 宿主 torrent_sync 已有熔断+红灯；插件重复熔断会双重报警；保活请求本身低频 |
| 10 | 插件数据存 `PLUGIN_DATA`（scope=global）：每站 `{状态, 上次成功时间, 连续失败数}`；M-Team 会话单独一个 `secret=True` 键 | 单值 JSON <64KB 足够；NexusPHP 凭据永不进插件（走 SITE_PROXY），M-Team 凭据进设置文件（加密） |
| 11 | 首版站点清单从宿主 26 站 YAML 里选 NexusPHP 站 + mteam：NexusPHP 站经宿主会话执行，mteam 走插件自登录 | 已配置站点才有宿主会话；宿主目录就是站点全集，插件不再维护独立站点表 |

> **M-Team 的三条判据细节**（都有单测，真站点 2026-10-20 实测）：
> `{"code":"1","message":"FAIL"}` 是「站点认为无需刷新」（间隔过短/已被 mtlogin 刷过），
> 记「登录在线」而不是成功，也不进失败统计；「簽名錯誤」= 前端签名密钥轮换，报失败并提示
> 插件需更新，**不**置凭据失效；「請求過於頻繁」= 限流，同样不置失效。登录被拒（密码错、
> 账号不存在、JWT 无效）才报警，且**不重试**——重试会继续扣账号的尝试次数。

## 文档导航

- [01-架构与数据流](01-architecture.md) — 总体架构、保活/签到/触碰三条动作流、事件与通知、失败路径与安全网
- [02-宿主侧设计：SITE_PROXY 开放能力](02-host-site-proxy.md) — 主程序要加的服务/操作/安全模型/清单契约
- [03-插件设计](03-plugin.md) — 目录结构、站点适配框架、三态判定、调度与配置、通知
- [04-验收](04-acceptance.md) — 端到端验收清单与关键故障场景

## 范围外

- Cloudflare challenge 过盾、答题签到、验证码 OCR（报「需人工」并跳过，不做浏览器渲染）；
- M-Team 的 Turnstile 强制人机验证 / JWT 手工透传模式（账号密码模式失效时只能等社区逆向）；
- CookieCloud 同步、浏览器扩展辅助（宿主凭据体系是 NexusPHP 站的唯一真源，不引入第二真源）；
- 「填一个网址安装」「插件商店」上架流程（按本仓库现状走 tag+Release+CI 生成索引）；
- 修改宿主站点凭据（插件只读宿主状态，不做任何写操作）。
