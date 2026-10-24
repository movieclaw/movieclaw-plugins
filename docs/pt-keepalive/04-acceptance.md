# 04 验收

> 完成标准（AGENTS.md）：单测过 ≠ 完成。真实服务器安装 + 真实用户场景 + 诊断页无 error +
> 卸载重装，才算完成。以下分宿主侧、插件侧两段。

## 验证状态（2026-10-18 实现完成；同日复查后修订）

**已自动验证：**

- [x] 宿主单测 26 个（`tests/api/test_site_proxy.py`）全绿；
- [x] sites/tracker 回归 115 通过（10 个预存在的 Windows 进程外用例失败与本次无关，见下）；
- [x] 插件单测 57 个全绿（判定正反例、状态机、适配器分派、mock 宿主操作的整轮：
      失效告警去重、告警发送失败重试、连续失败只报一次、跳过语义、签到开关、需人工短路）；
- [x] `check_plugin.py` 通过（含 `site.list` / `site.catalog` / `site.proxy.request` 操作核对）；
- [x] 打包 26 KB，零 vendor / 零依赖；
- [x] uidev 真实服务器（inline 本地插件路径）装载成功：授权操作、定时任务贡献、
      启动告警自动消退；
- [x] 真实保活轮端到端：假 mteam 凭据（ACTIVE）→ 适配器生成 `updateLastBrowse` 签名请求 →
      宿主代请求打到**真实 M-Team API** → 401「Full authentication is required」→
      插件判凭据失效 → 状态机落库（`alerted_expired=true`、失败计数直抵阈值）→
      第二轮去重（不再重复告警）。
- [x] 无站点时空轮不炸、正确写空状态。

**复查后修订（2026-10-18，本轮）：**

- 日报模板引用的 `{detail}` 此前载荷里没有 → 日报正文为空；现在 `DailyReport.detail`
  在插件里拼好，模板可用。
- 连续失败此前只进「默认关闭」的日报 → 用户永远看不到；现在拆出
  `pt-keepalive.report.failures`（默认开），每段失败只报一次。
- 未适配的纯 API 站（sunnypt）此前套 NexusPHP 模板 → 每轮假失败；现在按
  `auth_type=apikey` 判跳过，不发请求。
- NexusPHP 此前只访问 `attendance.php`（没有该页的站 = 404 假失败）→ 现在先 `index.php`
  确认登录态，签到页 404 记「登录在线」。
- Cloudflare 特征此前扫整页 HTML（站点 JS 里的字符串会误判）；现在只看 title 与可见文本。
- 宿主 409「未配置 / 已停用 / 尚未验证通过」此前一律当凭据失效 → 现在记跳过，不告警。
- 失效告警此前先置位再发送 → 发送失败就永久丢失；现在先发后置位，失败下一轮重试。
- 移除了用不到的 `network = true` 声明（插件无直连出网）。

**2026-10-20 修正（M-Team 改路 + 真实站点验证）：**

- 原方案「借宿主 x-api-key 调 `/api/member/updateLastBrowse`」**实测不成立**：同一 Key 调
  `/api/member/profile` 返回 `code:"0"`，调该接口一律
  `{"code":401,"Full authentication is required to access this resource"}`——它只认网页会话。
  原实现会把 M-Team 每轮判成「凭据失效」并告警。
- 改为插件自己登录（`pt_keepalive/mteam.py`）：`POST /api/login`（账号+密码+空 turnstile）
  → `code 1001` 时带 TOTP 动态码重发 → 取响应头 JWT/Did → `profile` → `updateLastBrowse`。
  凭据存插件设置（`SecretStr`，宿主 SecretBox 加密），会话存插件数据（`secret=True`）。
- **真实站点验证（2026-10-20，NAS 容器网络直连）**：
  - 首轮：`signin_ok`「最后浏览时间已刷新」，耗时 1.4s，拿到 JWT（299 字节）与 Did/visitorid；
  - 次轮：复用会话，站点回 `{"code":"1","message":"FAIL"}` → 判 `login_only`（无需刷新），
    **不**报警；
  - 另测：错签名 → 「簽名錯誤」；API Key 调 update → 401；连发登录 → 「請求過於頻繁」；
    既有 JWT 时再登录被拒（说明缓存会话是必要的，不是可选优化）。
- 插件单测 80 个全绿；`check_plugin.py` 通过（41 KB，声明 `network = true`）。

**2026-10-22/23 修正（宿主不再跟随站点 3xx，0.2.1）：**

- 背景：主仓库 PR #983 评审要求 `site.proxy.request` 不跟随跳转（防认证头经站点的开放重定向外泄）。
  NexusPHP 会话失效时 `index.php` / `attendance.php` 会 302 到 `login.php`；宿主不再替插件跟到登录页
  HTML，旧版插件把这个空正文的 302 判成「页面未见登录证据，无法确认会话有效」——失败，但**不置
  `auth_expired`**，凭据失效告警静默丢了。0.2.1 补上插件侧的跳转处理（规则见 `02-host-site-proxy.md`）。
- 单测 113 个全绿（新增 28 个，`tests/test_redirects.py`）。把实现逐个改坏做了变异检查，11 种都被测试
  抓住：关掉登录判定、放行跨主机、去掉成环检测、改回「到过同一页就算环」、跳数差一、POST 也跟、
  丢掉目录里的 `base_url`、`Location` 头名区分大小写、拿不到域名也放行绝对 URL、无 `Location` 的 3xx
  当页面、`..` 吃掉开头的 `/`。另对 `Location` 解析函数做了 20 万次随机输入：没有异常，产出的路径
  一律以 `/` 开头、不带片段（这一轮抓出了 `urljoin` 在根目录遇到 `..` 会丢掉开头 `/` 的问题，已修）。
  `check_plugin.py` 通过（对 `pr-sites` 源码跑，多一条「申请了危险操作：site.proxy.request」的提示——
  那是宿主新加的 `x-cli-dangerous: confirm` 标记，插件本来就逐个声明了）。
- **假站点 + 真宿主端到端，走完 AGENTS.md 的完成标准**（Linux 宿主，插件是打包安装的、进程外运行的那份）：
  - 宿主 = 主仓库 `pr/sites-proxy-request`（df27d573），装在 WSL2 的 Ubuntu（Python 3.12.3；依赖按
    pyproject 的版本区间装的当前最新版，不是锁文件）里，全新数据库与密钥、独立端口。Windows 的 Python
    没有 `socket.AF_UNIX`，进程外插件起不来（既有限制，见下），所以走 Linux。
  - 假 NexusPHP 站点监听 `http://127.0.0.1:18765`，经宿主自己的 `site.add`（Cookie）流程验证到 `active`。
  - `mclaw plugin dev ./plugins/pt-keepalive --once --yes`：打包 → 上传 → 批准 → 加载，宿主日志
    「插件 pt-keepalive 已在独立进程中运行」。
  - 诊断（`mclaw app plugins list`）：67 条全部 `active`，没有一条带 `error`；`pt-keepalive` 为
    `runtime=process`、`source=package`、权限正是清单里那三项、`error=null`、1 个定时任务、失败 0。
  - 卸载（`app plugins packages uninstall pt-keepalive --yes`，`purged_rows: 0`）→ 条目消失（66 条）；
    重装 → 67 条、`active`、定时任务的周期保留。重装后又跑了「会话失效」与「同站跳转」两个场景。
  - 用宿主的 `app.tasks.update` 把任务改成每分钟，由调度器跑轮。每个场景前都清空了插件状态，
    免得告警位串场；「站点实际收到的请求」来自假站点自己的访问日志。0.2.1 终稿的 12 个场景：

  | 站点怎么回 | 插件结果 | 站点实际收到的请求 |
  |---|---|---|
  | 正常，无跳转 | `signin_ok`「签到成功」 | `/index.php` 200 → `/attendance.php` 200 |
  | 会话失效：`index.php` → 302 绝对 URL `…/login.php?returnto=index.php` | `failed`「站点跳转到了登录页，凭据已失效」，失败计数直抵阈值 3，`alerted_expired=true` | `/index.php` 302；没有后续请求 |
  | 同上，`Location` 是相对的 `login.php?returnto=index.php` | 同上 | 同上 |
  | 首页正常，`attendance.php` → 302 登录页 | 同上 | `/index.php` 200 → `/attendance.php` 302 |
  | 同站绝对跳转：`index.php` → `…/index.php?lang=zh`，`attendance.php` → `…/attendance.php?ref=daily` | `signin_ok` | 4 次请求，**每次都带宿主的 Cookie** |
  | 同站相对跳转：`index.php?lang=zh`、`attendance.php?ref=daily` | `signin_ok` | 同上 |
  | 做完动作跳回同一页：`attendance.php` → 302 `attendance.php` → 200 | `signin_ok` | `/attendance.php` 两次（302、200） |
  | 跳去设 cookie 再跳回来：`index.php` → `/cookiecheck.php` → `/index.php` → 200 | `signin_ok` | `/index.php` 302 → `/cookiecheck.php` 302 → `/index.php` 200 → `/attendance.php` 200 |
  | 跨主机：`index.php` → `http://localhost:18766/collect` | `failed`「代请求失败：站点跳转到外部地址（localhost），已忽略」，**无** `alerted_expired`；18766 上的监听 0 次命中 | `/index.php` 302 |
  | 302 不给 `Location` | `failed`「代请求失败：站点回了 HTTP 302 跳转，却没给目标地址」 | `/index.php` |
  | 成环：`/index.php` → `/a` → `/index.php` → `/a` | `failed`「代请求失败：站点的跳转形成了环，已放弃」 | `/index.php`、`/a`、`/index.php` |
  | 4 跳：`/index.php` → `/c1` → `/c2` → `/c3` → `/c4` | `failed`「代请求失败：站点连续跳转超过 3 次，已放弃」 | `/index.php`、`/c1`、`/c2`、`/c3`；`/c4` 没请求 |

- **没有 `site.catalog` 权限时的降级**（把清单和 `@plugin` 里的权限去掉这一项装一份，宿主日志「获得 2 个宿主操作授权」）：
  展示名退回 `fakept`；绝对 URL 的同站跳转被拒，说明写着「站点跳转到 127.0.0.1，但没拿到站点域名、无法确认是同站，已忽略」；
  相对路径的跳转照跟，`signin_ok`。
- **前后对比与宿主契约探测**（先在 Windows 宿主上做的，插件当时是进程内的本地插件，同一假站点）：
  会话失效的假站点上直接调 `site.proxy.request`，对 `index.php` 回 `status_code: 302`、`headers.location`
  是登录页、正文为空，传 `timeout_seconds` 被接受。同一个「会话失效」场景：改动前的插件 → `failed`
  「页面未见登录证据，无法确认会话有效」，失败计数 1，**没有** `alerted_expired`（告警丢了）；改动后 →
  上表第 2 行。
- **没验证到的**：
  - 假站点不是真 NexusPHP：真实站点 `Location` 的写法只靠宿主测试里的 `/login.php?returnto=…`
    （相对）与单测里的各种写法覆盖。
  - 宿主跑在 WSL2 里，不是 Docker 镜像：root 运行时进程外插件改用非特权用户、从拷贝出来的代码目录
    运行（`plugin_isolation`）这条路没走到；这次改动不碰文件系统，但 NAS 上要看一眼。
  - 没验证通知的实际推送（事件不落 `domain_event` 是设计行为），以 `alerted_expired=true`
    （告警发布成功后才置位）为准。
  - 没跑仍会自己跟随跳转的旧宿主：「看不到 3xx、行为不变」是推出来的，基线场景在新宿主上通过。
  - `mclaw plugin dev` 只用了 `--once`，没试文件监视模式；M-Team 那条路这次没改，也没跑。

**已知平台限制（非本任务缺陷）：**

- Windows 上进程外插件 IPC 依赖 `AF_UNIX`（宿主整个 process-runtime 传输层），
  本机只能走 inline 路径验证；Linux（NAS/CI）不受影响。独立宿主工程，已报。
  2026-10-23 起本机也可以用 WSL2 的 Ubuntu 起一个 Linux 宿主做完整验证（做法见上一节）。
- 事件不落 `domain_event` 表是**设计行为**（`publish_now` 零开销：没有订阅者不落库），
  通知在真实安装（用户批准后建立偏好）时才会真正推送。

**留待用户（需要真实凭据 / 登录态）：**

1. 在真实环境 `mclaw plugin dev plugins/pt-keepalive --once` 或上传正式包走批准安装；
2. 配置一个真实 NexusPHP 站点，观察一轮真实签到与日报通知；
3. 在插件设置里填 M-Team 账号 / 密码 / TOTP 密钥，观察一轮真实登录与「最后浏览时间」刷新
   （登录链路已在 NAS 容器网络实测通过，但走宿主插件进程的端到端还没跑）。

## 宿主侧（SITE_PROXY）验收

- [x] `site.proxy.request` 进入 OpenAPI 与宿主操作目录（`_operation_index`）；
- [x] 单测：站内相对路径校验（拒绝 `//`、协议、反斜杠、CRLF）；认证头黑名单
      （`cookie`/`authorization`/`x-api-key` 被剥除）；响应剥 `set-cookie`；512KB 截断；
      凭据非 ACTIVE → 409；网络错误 → 503；
- [x] 对已配置站点的真实请求（假凭据 401 响应透传）；
- [x] 既有回归：torrent_sync、site.verify、站点凭据 CRUD 行为不变；
- [x] 站点回 3xx 时不跟随、原样返回（`test_proxy_request_does_not_follow_redirects`；
      2026-10-22 假站点端到端里宿主只回 302，没向跳转目标发过任何请求）。

## 插件侧验收（端到端）

前置：主程序 dev 服务器（`G:\Github\movieclaw\.tmp-uidev\start-uidev.ps1`，API 8001）；
至少 1 个真实 NexusPHP 站点已配置且 ACTIVE；M-Team 账号 / 密码 / TOTP 密钥填在插件设置里。

1. **本地检查**：`check_plugin.py plugins/pt-keepalive` 无 ✗；单测全绿
   （三态判定正反例：成功词/幂等词/否定词优先/密码框/CF 特征/剥 script 后匹配）。
2. **开发安装**：`mclaw plugin dev plugins/pt-keepalive --once` → `已加载`；
   `app plugins list` 里 `pt-keepalive` state=active；`设置→定时任务` 出现
   `pt-keepalive:run`（默认每天 09:17）。
3. **真实保活轮**（等调度或临时把 cron 改近）：
   - NexusPHP 站：日志出现该站签到请求与三态结果；宿主日志可见代请求记录；
   - mteam：先登录（日志出现会话获取），再 `updateLastBrowse`；站内「最后浏览时间」刷新
     （用户肉眼核对，或下一轮 profile 里 lastBrowse 变化）；
   - 站点配置了代理时请求走代理（宿主出口层 / 插件出口日志核对）。
4. **通知链路**：`pt-keepalive.report.daily` 事件触发 → App/IM 收到日报（标「来自 PT 站点保活」）；
   偏好关闭日报 → 不再收；`auth.expired` 与 `report.failures` 独立于日报开关。
5. **失效告警流**：临时改坏某站凭据（或用已失效站点）→ 下一轮判定 auth_expired →
   收到一次告警（不是每轮一条）；修复凭据 + `POST /verify/{site_id}` → 下轮恢复正常，
   失败计数与告警位清零。
6. **自检降级**：在**旧宿主**（无 site.proxy.request）上装插件 → 加载成功但不注册任务，
   日志说明「宿主不支持，需升级宿主」，诊断页无 error。
7. **状态查询**：`mclaw plugins pt-keepalive status` 返回各站最近结果表。
8. **诊断页**：`设置→插件→pt-keepalive` 无 error；贡献的定时任务显示在详情页。
9. **卸载重装**：`app plugins packages uninstall pt-keepalive --yes` → 条目消失、
   定时任务撤下；重装 → 任务回来、PLUGIN_DATA 里状态延续（默认不 purge）。

## 关键故障场景（预期行为）

| 场景 | 预期 |
|---|---|
| NexusPHP 站改版，签到页选择器全变 | 判 LOGIN_ONLY（保守）；连续 3 轮进「连续失败」通知；不虚报成功 |
| NexusPHP 站没有签到页（attendance.php 404） | 首页确认登录在线即记 LOGIN_ONLY，**不算失败** |
| mteam 站点轮换 `_sgin` 密钥 | 判 FAILED「签名错误…插件需要更新」，**不**置凭据失效；连续 3 轮进失败通知 |
| mteam 返回 `{"code":"1","message":"FAIL","data":null}` | 站点认为无需刷新（间隔过短/已被 mtlogin 刷过）→ 判 `login_only`，不进失败统计 |
| 某站 Cloudflare challenge / WAF 拦截（401/403） | need_human，不计数、不熔断、不发失效告警；日报提示「需人工」 |
| 未适配的纯 API 站（sunnypt） | 不发请求，记 skipped，不进失败统计 |
| 站点凭据被用户禁用 | `site.list` 回 `enabled=false` → 不收进本轮，什么都不记 |
| 站点在宿主那边没验证通过（典型：开了两步验证 + 账号密码方式） | 记 skipped，**带上宿主的 `last_error`** 与「改用 Cookie」提示，不发请求、不告警 |
| 站点刚配好、宿主还在验证（pending/verifying） | 记 skipped，说「还在验证中」，不渲染成故障 |
| 代请求遇到网络错误 / 5xx | 单站本轮 FAILED，不重试；连续 3 轮进「连续失败」通知（每段失败只报一次） |
| NexusPHP 会话失效，`index.php` / `attendance.php` 302 到登录页（`Location` 路径带 `login`） | 判 FAILED + `auth_expired`，发一次凭据失效告警；不跟到登录页 |
| 站点回同站跳转（相对路径，或主机与站点一致的绝对 URL） | 对站内路径再发一次 GET，最多 3 跳，照常判定结果 |
| 站点跳到别的主机 / 跳转成环 / 超过 3 跳 / 302 不给 `Location` | 不跟；FAILED 并写明原因，**不**置 `auth_expired`；连续 3 轮进「连续失败」通知 |
| 代理配置只给 pt-keepalive | mteam 登录链路走代理（`net.http_transport("pt-keepalive")`），NexusPHP 站仍由宿主 `site:<id>` 出口决定 |
| mteam 账号密码错 / 账号被停用 | 判 `auth_expired` 并发一次告警；**不重试**登录（会扣账号尝试次数），下轮再试 |
| mteam 未填账号密码 | 该站记 `skipped`，不发请求、不告警 |
| 调度器到点但上一轮还没跑完 | APScheduler max_instances=1 + coalesce（宿主既有行为），不并发 |
| 同一版本重复上传 | 服务器拒绝（同版本字符串）；走正常升 version 流程 |

## 风险告知（面向最终用户的文案要点，随插件 description/README）

- 程序化签到/访问未必被站方计为「用户活跃」（MoviePilot 官方警告，馒头是已知例子）；
  插件显示「成功」只代表请求与判据成立，**不承诺免于封号**。
- 签到默认开启，但「仅保活不签到」是一键开关（对站点规则敏感的用户）。
- 频率默认每天一次，刻意不做高频（mtlogin 的每 2 小时不必要——宿主管登录态续期）。
