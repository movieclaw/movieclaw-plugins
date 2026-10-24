# 02 宿主侧设计：站点代请求（已实现）

> 本文档描述在**主程序仓库**（`G:\Github\movieclaw`）的改动。**实现已完成**（2026-10-18），
> 与原设计有一处简化：**不新增 `SITE_PROXY` 服务键**——插件经既有 `HOST_OPS` 操作链路调用
> `site.proxy.request` 即可，少暴露一个进程内服务面，授权/审批/审计全部复用现成机制。
>
> 2026-10-22 评审后的修订（主仓库 PR #983，分支 `pr/sites-proxy-request`）：超时字段改名
> `timeout_seconds`；站点回 3xx 时**不再跟随**；操作标为危险操作。插件侧的对应改动见
> 下文「站点回 3xx」与 `pt-keepalive` 0.2.1。

## 为什么是「代请求」而不是「开放会话」

直接开放 `SITE_ACCESS`（已认证 BaseSite）给第三方 = 插件能拿 cookie 明文、能任意调
`download_torrent`、能枚举站点全部数据——危险面不可收口。主仓库设计文档自己也把它列为缺口
（「主要缺口：站点能力开放给插件」）。

「代请求」（proxy）把信任边界收窄成：**插件声明意图，宿主执行，回传脱敏响应**。
宿主在中间做校验（站点已配置、站内相对路径、每站限流），凭据永远不离开宿主。

## 已实现的内容

### 宿主操作 `site.proxy.request`（`src/movieclaw_api/api/routes/sites.py`）

```
POST /api/v1/sites/{site_id}/proxy
```

请求体（`SiteProxyRequest`，schemas/site.py）：`method`（GET/POST）、`path`（站内相对路径）、
`base`（web/api 域名二选一）、`headers`（插件补充的非敏感头）、`body`（dict 按 JSON / str 按原文）、
`timeout_seconds`（1–120，默认 30）。超时字段**不叫** `timeout`：它和 CLI 内置的 `--timeout`
重名，会让这条命令失去那个标志。本插件没有传超时，一直用默认值。

响应（`SiteProxyResponse`）：`status_code`、`headers`（安全子集，剥 set-cookie/www-authenticate；
站点回 3xx 时目标在 `location`）、`text`（512KB 截断，`truncated` 置真）、
`json_body`（JSON 响应的解析结果）、二进制内容不回传。

### 安全机制（全部带单测）

| 机制 | 实现 |
|---|---|
| 防 SSRF | `proxy_path_error` 挡 `//`、`scheme://`、反斜杠、CRLF；`proxy_same_origin` 拼接后按 scheme+host 复核（最终防线）；完整 URL 一律由宿主按站点注册域名拼 |
| 凭据不出宿主 | 请求头黑名单（cookie/authorization/x-api-key/host）在 `sanitize_proxy_headers` 剥除，认证头由宿主会话附加且优先；响应剥凭据相关头 |
| 不跟随跳转 | `raw_request(..., follow_redirects=False)`。站点客户端默认跟随跳转，而 `x-api-key` 这类认证头挂在客户端的默认头上，站点若有开放重定向，跟到外站就会把它带出去（httpx 跨域跳转只剥 `Authorization` / `Cookie`）。3xx 原样返回，认证头只发给校验过的那一个 URL |
| 危险操作 | 标 `x-cli-dangerous: confirm`：能以用户的站点身份发任意 POST，所以插件的 `permissions` 要逐个列出（通配符不覆盖）、安装时经用户批准，CLI 要 `--yes` |
| 认证免维护 | 经 `SiteAccessManager.get()` 共享会话：Cookie/API-Key 自动附加、过期自动重认证、1800s TTL |
| 限流/代理 | 复用 tracker `HttpClient`（site:<id> 每站最小间隔 + egress 代理标签），与宿主自身站点流量共享节流 |
| 错误语义 | 凭据不可用 → 409（中文原因）；路径/头不合规 → 400；未知站点 → 404；超时/网络 → 503；对插件表现为结构化 `OpsError` |

### 站点回 3xx：宿主不跟，插件自己跟

宿主把站点的 3xx 原样返回，目标在 `headers.location`：

```json
{"status_code": 302, "headers": {"location": "https://hdsky.me/login.php?returnto=index.php"},
 "text": "", "json_body": null, "truncated": false}
```

宿主没法「替你跟一下」——`path` 只收站内相对路径，域名由宿主按站点注册的地址拼。所以跟不跟、
跟到哪，是插件的事（`runner._proxy_following`；Location 的判定是 `sites/__init__.py` 里的纯函数）：

| `Location` | 插件怎么办 |
|---|---|
| 路径带 `login`（`login.php`、`takelogin.php`、`/login`），不论相对 / 绝对、不论主机 | 会话失效：记失败并置 `auth_expired`（发凭据失效告警） |
| 相对路径，或绝对 URL 且主机与站点一致（只比主机，不比协议和端口） | 对那个路径加查询串再发一次 GET，最多跟 3 跳 |
| 别的主机（含裸域 ↔ `www.`）、成环、超过 3 跳、没有 `Location`、拿不到站点域名时的绝对 URL、非 GET | 不跟，记失败并写明原因；**不**置 `auth_expired` |

- **为什么不处理就会丢告警**：NexusPHP 会话失效时 `index.php` / `attendance.php` 会 302 到
  `login.php`（宿主自己的 tracker 代码也这么假设）。以前宿主跟随跳转，插件拿到登录页 HTML，
  密码框命中即判失效；现在只拿到空正文的 302，插件若还把它当普通响应，就判成「页面未见登录
  证据」——失败，但不是凭据失效，告警丢了。
- **站点域名**：`site.list` 没有域名字段，`site.catalog` 的 `base_url` 有（插件本来就读目录拿
  展示名）。宿主拼 `base="web"` 的请求用 `web_base_url`（配了才有）否则 `base_url`；NexusPHP 类
  站点两者相同，只有 API 类站点（M-Team、sunnypt）不同，它们不走这条路。
- **成环按跳转算，不按页面算**：「同一个从哪跳到哪」第二次出现才判环。做完动作跳回同一页
  （`attendance.php` → `attendance.php`）、跳去 `cookiecheck.php` 设个 cookie 再跳回来，
  第二次到那一页拿到的才是真页面，宿主以前跟随时都能过，不能误判；真环有 3 跳上限兜底。
- **POST**：保活当前所有 NexusPHP 步骤都是 GET。POST 遇 3xx 不跟（307/308 要保留方法和请求体，
  按 GET 重发是错的），不为没有用武之地的场景写一套跟随逻辑。
- **限流**：每一跳都是一次完整的 `site.proxy.request`，照样走宿主的每站限流；一个步骤最多 4 次请求。
- **宿主注册成 `http://` 而站点强制 `https://`**：宿主总按注册的地址发请求，插件换不了协议，
  跳转落回同一个路径，会判成环。内置站点注册的都是 `https://`。
- 宿主仍会自己跟随跳转的旧版本上，插件看不到 3xx，这套逻辑不触发。

### 随带发现并修复的宿主 Windows bug

- **`_ENV_KEEP` 缺 SystemRoot**：子进程环境只保留 `PATH/LANG/LC_ALL/TZ/HOME/TMPDIR`，
  Windows 上剥掉 `SystemRoot` 会让子进程 winsock 初始化失败（getaddrinfo 报 WinError 10106）。
  已修复：按大小写不敏感匹配并保留 `SystemRoot`（`plugin_runtime.py`）。
- **`AF_UNIX` 平台缺口（未修，独立工程）**：进程外插件的 IPC 传输层
  （`plugin_runtime._listen`、`plugin_gateway`、SDK `UnixHttp`/`open_unix_connection`、fd 传递）
  全部建立在 Unix domain socket 上，而 Windows CPython 无 `socket.AF_UNIX`——
  **进程外插件在 Windows 宿主上从未能运行**（官方 `test_plugin_packages.py` 在 Windows 同样全挂）。
  需要引入命名管道 / TCP 回环替代传输层，是独立的宿主工程；Linux（NAS/CI）不受影响。

## 验证状态

- 单测 `tests/api/test_site_proxy.py`：29 个用例全绿（路径校验、头黑名单、截断、
  409/400/404/503 语义、web/api 域名选择；评审后补的：`timeout_seconds` 透传、
  不跟随跳转——用真的站点客户端 + 内存传输层验证「不向跳转目标发任何请求」、危险操作标记）。
  2026-10-22 在 `pr/sites-proxy-request`（df27d573）上重跑，29 通过。
- sites/tracker 回归：115 通过（评审前的数字，评审后没重跑）。
- uidev 真实服务器 + 插件联合验证：见 04-验收（失效链路端到端成功）。
