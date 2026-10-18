# 写一个 MovieClaw 插件

这份说明给要往本仓库加插件的人。它按 MovieClaw **v0.37.0**（提交 `e229717c43b2d48986f8f51c19b86348a527cb5a`）的真实行为写，对照的是服务器里的 `Manifest` / `check_compat`（`src/movieclaw_api/plugins/packages.py`）和内置技能 `movieclaw-plugin-dev` 的参考文档。技能文档里「用 mclaw 工具、不要改主程序源码」是给代理的流程，这里改成作者自己要做的事。

同目录的 `hello-note/` 是一份能打包、能通过上述校验的最小插件：`runtime = "process"`，可靠订阅一次入库事件，用插件数据记一个键，用 pydantic 模型交出设置页能画的表单。它不在 `plugins/` 里，所以不会进索引。写新插件时复制它。

## 从 MovieClaw 复制的参考插件

同目录下其余带 `movieclaw-plugin.toml` 的文件夹，是 MovieClaw 里随应用携带的插件，外加 `plugins/watch-presence`，复制过来供对照。代码保持上游原样，不在这里重构。它们不在 `plugins/`，不会进索引，也不要打 Release。权威副本在 MovieClaw 仓库，并随应用打包。

<!-- sync-examples:begin -->
当前副本的 MovieClaw 提交是 `779712c4d8d2c2c320021ab0e31cde95821dd493`。机器可读的记录在 [`UPSTREAM.json`](UPSTREAM.json)。
权威源码在 [MovieClaw](https://github.com/movieclaw/MovieClaw)，并随应用打包。不要改这里的副本来发版。要更新：

```bash
python3 scripts/sync_examples.py 779712c4d8d2c2c320021ab0e31cde95821dd493
```

| 目录 | 上游路径 | runtime | 示范 |
|---|---|---|---|
| [`watch-presence`](watch-presence/) | `plugins/watch-presence` | `inline` | 进程内观看动态：成员区路由和播放记录，直接用宿主内部模块 |
| [`weixin-channel`](weixin-channel/) | `src/movieclaw_plugins/weixin` | `process` | 向 im-channels 贡献微信通道（扫码绑定、iLink、入站图片） |
| [`telegram-channel`](telegram-channel/) | `src/movieclaw_plugins/telegram` | `process` | 向 im-channels 贡献 Telegram bot（私聊对话和推送） |
| [`discord-channel`](discord-channel/) | `src/movieclaw_plugins/discord` | `process` | 向 im-channels 贡献 Discord bot（Gateway） |
| [`feishu-channel`](feishu-channel/) | `src/movieclaw_plugins/feishu` | `process` | 向 im-channels 贡献飞书群机器人（只推送，不能对话） |
| [`qbittorrent-downloader`](qbittorrent-downloader/) | `src/movieclaw_plugins/qbittorrent` | `process` | 向 downloader-adapters 贡献 qBittorrent |
| [`transmission-downloader`](transmission-downloader/) | `src/movieclaw_plugins/transmission` | `process` | 向 downloader-adapters 贡献 Transmission |
| [`strm-prefetch`](strm-prefetch/) | `src/movieclaw_plugins/strm_prefetch` | `process` | 用任务和敏感播放事件提前探网盘规格，探测仍走宿主操作 |

`watch-presence` 是 `runtime = inline`，import 宿主内部的 `movieclaw_api`、`movieclaw_db`、`movieclaw_playback`、`movieclaw_kernel`，不是只依赖 SDK。下载器依赖宿主提供的 `qbittorrent-api` / `transmission-rpc`，不放进 `vendor/`。

这些目录里的测试不能脱离 MovieClaw 单独跑，同步时不复制。各目录的 `README.md` 写了上游路径、提交号和省略原因。
<!-- sync-examples:end -->

CI 会把上表里的目录和 `hello-note/` 一起交给钉死版本的 `check_plugin.py`。警告（inline、与随应用携带的插件同 id、清单里没写全的契约）不导致失败。这些目录没有 `tests/`，不会跑 unittest。

## 设计文档里已经过时的部分

MovieClaw 仓库的 `docs/design/plugin-phase3.md` 有几处和现在的代码不一致，以代码和本指南为准：

- 新插件的 id **不能带点**。文档里的 `acme.group-blocklist` 这种写法，新包会被拒绝。带点的 id 只留给已经装过的旧包（例如 `channel.weixin`），宿主不会替它改名。
- 版本区间只有 `^主.次`（`^` 可以省略），表示主版本相同、次版本不低于。`>=1.0,<2` 不接受。
- 没有 `current` 符号链接。当前版本记在 `data/plugins/packages/state.json` 的 `version` 字段。磁盘上只留当前版和上一版两个目录。
- 设计里可申请的内存上限（`RLIMIT_AS`）没有清单字段，也没有实现。进程外运行器实际做的是：打开文件数收到 1024、禁止 core dump、`nice(10)`；宿主是 root 时把插件降到 uid 65534（可用 `MOVIECLAW_PLUGIN_UID` 改）。
- `network = true` 只是声明，安装页会显示，运行时不拦流量。
- 插件不能自带 HTML 设置页。`watch-presence` 目录里的 `settings.html` 宿主不加载。设置是 pydantic 模型，主程序收成 JSON Schema 子集再画表单。
- 兼容性看的是 SDK 和契约，不是应用版本。`movieclaw_sdk.SDK_VERSION` 现在是 `1.0`，应用版本（v0.37.0）是另一套数。索引里的 `min_app` / `max_app` 在服务器开始检查之前只是说明。
- 第三方插件包没有签名。zip 校验只防路径和体积。本仓库的索引可以带 sha256 和可选的 Ed25519 签名，那是索引自己的事，服务器安装时还不会拿它来对。

第三阶段明确不做插件市场、远程仓库和「填网址安装」。今天的唯一入口仍是上传文件。

## 插件是什么

一个插件是一个目录：根上有 `movieclaw-plugin.toml`，加上入口代码。打成 zip，扩展名 `.mcplugin`。依赖若有，放在目录里的 `vendor/`，安装时不联网、不跑 pip。

装上之后默认在独立的低权限进程里跑，崩溃只影响它自己。安装、升级、回滚、卸载都不用重启主程序。新版本如果挂不上，或者挂上后 120 秒内失败或进程崩溃到不再重启，服务器自动回到上一版；第一次安装失败则删掉安装记录。

`apply` 只做登记（监听、贡献、挂路由、起后台任务），登记完就返回，有 30 秒上限。耗时的事放进 `ctx.task` 或后台任务。经 `ctx` 登记的东西在卸载时撤掉。自己打开的资源用 `ctx.effect(释放函数)` 登记。

一个插件包只加载一个插件，名字必须等于清单 id。一件用户能说清楚的事做一个插件。互不相关、权限差很多的事拆开。

插件做不到这些：给网页或 App 加按钮、页面、播放器控件或首页卡片；新的下载器类型；新的元数据来源；新的 Webhook 推送格式；新的站点认证方式；替换播放、转码、媒体库扫描或刮削的主流程；改 AI 助手本身。能做的是响应事件、改决策钩子的结果、往注册表加一种实现、开接口、收外部平台的回调、在批准过的范围内调用宿主操作和存自己的数据。

## 目录

```
plugins/media-stats/
├── movieclaw-plugin.toml
├── media_stats.py             或 media_stats/__init__.py
├── store.json                 只给本仓库的索引用
├── CHANGELOG.md
└── vendor/                    可选
```

`store.json` 打进 zip 会被解出来并忽略，现有服务器不会因为它拒装。作者、主页、许可证、图标不要写进 toml：`Manifest` 是 `extra = "forbid"`，多一个字段上传就被拒。

## 清单

```toml
[plugin]
id = "media-stats"             # 必填，并且等于 @plugin 的名字
title = "媒体统计"              # 必填，1～60 字
version = "0.1.0"              # 必填，主.次.修订，可带 - 或 + 后缀
entry = "media_stats"          # 模块名，不是路径，不带 .py。exec 不写
runtime = "process"            # process（默认）| inline | exec
sdk = "^1.0"
description = "设置页详情里给用户看的一句话"   # 可选，500 字以内

[requires]
"library.ingest.completed" = "^1.0"

[permissions]
operations = ["library.list"]
# paths = [{ path = "staging", mode = "read" }]
# network = true
# callbacks = ["push"]
# events = ["playback.*"]

[[events]]
name = "media-stats.weekly-report"    # 必须以插件 id 开头
title = "周报生成了"

[[notifications]]
event = "media-stats.weekly-report"
title = "本周看了 {hours} 小时"
body = "最常看的是《{top_title}》"
audience = "admins"            # admins | member:<字段> | media:<字段>
level = "active"               # active | passive
default = true
open = "plugin"                # plugin | media
```

没有这些字段：作者、主页、许可证、图标、截图、changelog、最低或最高应用版本、依赖别的插件。

`mclaw plugin pack` 的 Go 代码只从 `[plugin]` 里读双引号字符串，用来找入口文件。SDK、契约、权限、事件声明的完整校验在服务器。发布 CI 用的是服务器同一套 `Manifest` / `check_compat`，不能只看打包成功。

### id

小写字母开头，只含小写字母、数字、连字符，长度 3～40，不含点，也不能以连字符结尾。例如 `hello-note`、`group-blocklist`。

清单 id 和 `@plugin("...")` 的第一个参数必须完全一致。本仓库里目录名也必须是这个 id。第三方以后如果另开索引，建议用作者前缀（仍遵守上面的规则），例如 `acme-blocklist`。两套源撞了同一个 id，安装应当失败，而不是互相覆盖。官方插件请占住短 id。

### sdk 和 requires

`sdk` 只写 `^主.次`。`^1.0` 接受 1.0、1.3，不接受 2.0。省略 `^` 含义相同。不写则默认 `^1.0`。

`[requires]` 的值用同一套写法。名字必须是当时那版 MovieClaw 里已经开放的契约（实验或稳定），内部契约第三方不能要。契约主版本变了就是不兼容，插件要改。未知契约、要了内部契约、次版本不够，上传时 `check_compat` 会拒绝。

事件、钩子、注册表写进 `[requires]`，不要写进 `inject`。`inject` 只放服务键，例如 `PLUGIN_DATA`、`DURABLE_EVENTS`、`HOST_OPS`、`PLUGIN_ROUTES`、`PLUGIN_JOBS`、`PLUGIN_FILES`、`PLUGIN_HEALTH`、`PLUGIN_CALLBACKS`、`AGENT`。服务可以不写进 `[requires]`；写了就钉住版本，例如 `"plugin-data" = "^1.0"`。

代码里用到的事件或注册表如果没写进 `[requires]`，`check_plugin.py` 会警告。可靠订阅还必须 `inject` `DURABLE_EVENTS`。

### runtime

| 值 | 含义 |
|---|---|
| `process` | 默认，也是本仓库示例用的。独立进程，`python -s -m movieclaw_sdk.runner`。主进程不导入插件代码。 |
| `inline` | 在主进程里导入，和主程序同一套权限。批准时必须单独确认（`mclaw` 加 `--allow-inline`）。商店以后也不该静默装这种。除非用户明确要求，不要用。 |
| `exec` | 非 Python。不写 `entry`，写 `command`（包内相对路径，如 `bin/bridge`）和 `platforms`（如 `linux/amd64`）。包里要有这个文件，服务器平台必须在列表里，否则装不上。 |

`exec` 的 `platforms` 是允许列表，不是「按架构挑二进制」。一个包只有一个 `command`。列表写了 amd64 和 arm64、里面却是一个 amd64 二进制，arm64 机器会装上然后跑不起来。多架构就发多个附件，每个附件的清单只写一个平台，索引用 `artifacts[].platform` 区分。纯 Python、没有原生扩展时，索引记 `any`。

`exec` 的协议在 MovieClaw 的 `src/movieclaw_sdk/protocol/`。完整例子是技能目录里的 `references/examples/go-a2a-bridge/`。

### 权限

批准时授予的是**清单**里的列表。客户端提交的 `operations` 和 `paths` 必须和清单申请完全一致，不能多也不能少。`@plugin(permissions=...)` 请和清单 `permissions.operations` 保持一致，诊断页显示的是装饰器里的那份，实际授权以清单为准。

宿主操作的 id 和 `mclaw` 命令一一对应：`mclaw subscriptions create` 就是 `subscriptions.create`。调用是 `ops.call("<id>", {...})`，字典是路径参数、查询参数和请求体字段；请求体可选的操作把整个请求体放在 `"body"` 键下。危险操作必须逐个列出，不能靠 `领域.*` 把它们包进来。支持演练（参数里有 `dry_run`）的，开发时先演练。

插件包申请 AI 助手时只能是 `agent:none` 或 `agent:mclaw`。`agent:full` 不会交给下载来的包。要了助手却没 `inject` `AGENT`，或者反过来，检查脚本会警告。

路径授权：

| `path` | 含义 |
|---|---|
| `/mnt/cloud/movieclaw` | 这个绝对路径及其子目录 |
| `staging` | 导入规则里配置的全部自定义（暂存）目录 |
| `library` | 全部媒体库根目录 |
| `library:<id>` | 只批一个库 |

`mode` 是 `read` 或 `rw`。插件自己的私有目录不用申请，用 `files.path("plugin", "子路径")`。

`callbacks` 是要开放的回调端点名，最多 8 个。端点名小写字母开头，字母、数字、连字符。代码里 `endpoint` / `issue` 用到的名字必须在这里声明，否则启动失败。批准时这些名字会单独列给用户看。

`events`（在 `[permissions]` 里，不是 `[[events]]`）是要订阅的敏感事件：谁在什么时候看了什么，例如 `playback.*` 或具体名字。不声明就不能订。`[[events]]` 是插件**自己要发**的事件，名字必须以插件 id 开头，不能占用 `subscription`、`download`、`library`、`playback`、`account`、`agent`、`system`。`[[notifications]]` 把这样的事件变成一条通知。`audience` 用 `member:` 或 `media:` 时会通知到普通成员，批准时要单独确认。通知上会标「来自 插件名」。

`network = true` 见上文：声明给用户看。真要连外网，走用户的代理设置：`httpx.AsyncClient(transport=movieclaw_sdk.net.http_transport("<服务名>"))`。服务名是插件 id（旧的带点 id 用最后一段）。问别人的服务名会得到直连。

## 数据和设置：升级不迁移

宿主没有迁移钩子，也没有 `schema_version`。升级不碰已有数据，因为键是插件 id，不是版本。要改数据形状，在 `apply` 或后台任务里自己读旧键、写新键。

三处存储：

- **插件数据** `PLUGIN_DATA`：表 `plugin_data`，按条目 id、scope、key。单值 JSON 后不超过 64 KB，更大的请存文件、这里存引用。`secret=True` 加密。`scope` 默认全局；`entity_scope("subscription", 35)` 这种挂到实体上。实体删了要自己听删除事件清理。重启、升级都还在。
- **界面设置** `data/plugins/settings/<id>.json`：敏感字段加密，故意不进数据库，这样应用回退不用做迁移。用户在插件详情页保存后，插件按新值重启，`ctx.config` 是 pydantic 模型实例。值只在启动时读一次。
- **私有文件** `data/plugins/data/<id>/`：经 `PLUGIN_FILES` 的 `plugin` 别名，不用在清单里申请。

设置模型只写界面能画的类型：布尔、字符串（可以用枚举）、整数、数字、字符串列表，以及它们的可选形式。敏感字段用 `SecretStr`。多行文本加 `json_schema_extra={"multiline": True}`。嵌套对象和字典界面编辑不了：插件仍能加载，设置页只说明原因。每个字段写 `title` 和 `description`。约束（`ge`、`le`、`min_length`、`pattern`）写在 `Field` 里。没填完（例如还没有 Key）时照常加载，记一条日志说明去哪填，不要抛异常。周期性的间隔不要放进配置，做成定时任务，让用户在「设置 → 定时任务」里改。

卸载默认留下插件数据。`purge_data=true` 时，插件包这条路径**只删** `plugin_data` 里该 id 的行。设置文件和私有目录的删除在官方插件卸载那条路径上，插件包卸载没有走那里。文案不要承诺「连设置和私有目录一起删掉」。它改过的系统数据（建过的订阅、删过的文件）也不会被撤销。

后台任务的类型是 `<插件 id>:<名字>`。停用或卸载会取消它还在进行的任务。任务不是版本数据，升级不会按版本重写任务行。

## 事件

可靠订阅用于「发生了就必须处理」：至少送达一次，重启不丢。必须有稳定的 `id`，并且幂等。改了这个 id 会丢掉投递进度。

```python
ctx.on(LIBRARY_INGEST_COMPLETED, on_imported, id="after-import", durable=True)
```

监听器抛错等于这次投递失败，按退避重试，多次失败进死信。去重用 `ctx.delivery.event_id`（重试和重放会再送来）。自己引起的事件可以用 `ctx.delivery.origin.is_plugin(ctx.entry_id)` 跳过，避免循环。`hello-note` 把去重列表收在插件数据的一个键里，只留最近 500 个 id。

实时订阅是 `ctx.on(EVENT, fn)`，不加 `durable`：发完即忘。插件不在、积压满了就丢。适合「现在可以做点什么」，该做的事要能从数据库补回来。高频事件（播放进度、打开详情）只能实时订阅。

决策钩子也是 `ctx.on`。钩子里只做判断，不产生副作用（不发请求、不写数据、不调宿主操作）。副作用放进可靠事件或宿主操作。监听器抛错、超时或返回类型不对时按「这个监听器不存在」处理。

敏感事件必须写在 `[permissions] events`。版本号以你对着的那一版 `contracts.py` 或 `movieclaw_sdk.surface` 为准，不要照抄旧笔记。`library.ingest.completed` 在 v0.37.0 是 `^1.0`；不少播放和删除事件已经是 `^2.0`。

## 任务

要在任务中心看见、能断点、能取消的工作：用 `JOB_HANDLERS` 登记处理器，用 `PLUGIN_JOBS` 创建。任务类型写全 `<插件 id>:<名字>`。只能创建自己登记过的类型。`dedupe_key` 只在本插件内有效。处理器里在安全点检查取消；临时失败抛 `JobRetry`，要用户处理抛 `JobBlocked`。清单里如果用了带 `title` 的处理器，`[requires]` 写 `"job-handlers" = "^1.1"`。

简单轮询可以用 `ctx.task`。循环里抓住异常、记日志、继续；`CancelledError` 原样抛出。卸载时这个任务会被取消。

想让用户改周期：`ctx.contribute(SCHEDULED_TASKS, ...)`。不要在配置模型里再放一个「间隔分钟数」。

入库流水线步骤只处理「监听导入、目标是自定义暂存目录」的那一批文件。缺了前提就不会触发。

## 路由

```python
routes = ctx.use(PLUGIN_ROUTES)
router = APIRouter()

@router.get("/status", operation_id=f"plugins.{ctx.entry_id}.status", summary="运行状态")
async def status() -> dict:
    return {"ok": True}

routes.mount(ctx, router)                  # 默认 admin：/api/v1/plugins/<id>/status
# routes.mount(ctx, router, zone="member")
# routes.mount(ctx, router, zone="public")  # 必须带本插件签发的签名，否则 404
```

`operation_id` 必须以 `plugins.<条目 id>.` 开头。装好后随便执行一条 mclaw 业务命令，等目录刷新，它就会变成 `mclaw plugins <id> status`。端点参数和返回值的类型要在模块顶层导入。入口模块不要写 `from __future__ import annotations` 又挂路由，否则进程外解析不了那些注解。错误用 `HTTPException`，`detail` 写人话。

公开区里的调用不带工具。要让 AI 助手查库或操作系统，接口放管理员区，并申请 `agent:mclaw`。只聊天用公开区签名链接，申请 `agent:none`。

## 回调

外部平台打这个地址：`<外部访问地址>/api/v1/hooks/<条目 id>/<端点名>/<密钥>[/<子路径>]`。宿主管地址、方法、体积、频率，并剥掉 MovieClaw 的登录 Cookie。它不验签、不解析请求体。插件自己验，用 `CallbackResponse` 原样回。

```python
callbacks.endpoint(ctx, "push", on_push, methods=("POST",))
issued = await callbacks.issue(ctx, "push")
```

端点名必须写在 `[permissions] callbacks`。只能在 `apply` 里登记。处理函数默认 10 秒超时，超时宿主回 504。耗时的事交给后台任务后立刻返回。没配外部访问地址时，发出来的链接不是绝对地址。卸载后端点全部作废；插件停着时打进来是 503，地址还留着。

家里的 NAS 多半没有公网地址，平台推不进来。能选的话，优先服务器主动连出去（长连接、长轮询），其次是只推送的群机器人，需要公网回调的放最后。

## 依赖

主程序自带的库可以直接 import：`httpx`、`websockets`、`pydantic`、`fastapi`、`cryptography`、`parsel`、`pyyaml`。别的依赖放进插件目录的 `vendor/`（纯 Python，运行时加入 `sys.path`）。不要在安装时 pip，也不要提交软链接。

原生 wheel 的平台标签没有人在打包时检查。放错平台要到加载才失败。`exec` 插件把已经编好的二进制放进 `command` 指的路径。

不要 import 主程序的仓储层或内部服务（`DB`、`SECRETS`、`SETTING_STORE` 这类没出现在开放契约里的）。第三方会被拒绝，进程外也拿不到。

日志用 `ctx.logger`，带上关键 id。按英文短词匹配发布名时用词边界，避免 `ts` 命中 `Hits`。

## 第三方依赖

v0.37.0 的服务器只认 `vendor/`。从服务器 v0.40.0 起，`runtime = "process"` 的插件可以声明 PyPI 依赖，由服务器在用户批准时下载安装（`inline` / `exec` 插件不行）：

1. 插件目录放 `requirements.in`，每行一个直接依赖，例如 `p115cipher[full]`。
2. 清单加：

   ```toml
   [dependencies]
   lock = "requirements.lock"
   ```

3. 生成锁文件（需要 MovieClaw 源码，提交 `6df51e35` 或更新，那里才有 `movieclaw_sdk.deps`）：

   ```bash
   PYTHONPATH=<MovieClaw>/src python3 scripts/pack.py plugins/<id> --lock
   ```

   它调用 `python -m movieclaw_sdk.deps lock <目录>`，按最新服务器 Release 附带的 `host-constraints.txt`（主程序自带的包和版本，`--constraints` 可换成本地文件）解析依赖闭包：每个包 `==` 钉死、带 `--hash=sha256`，上方的 `#> 文件名 字节数 sha256:…` 行列出它在各平台、各 Python 版本的轮子。不要手改锁文件，要换版本就改 `requirements.in` 重新生成，锁文件和 `requirements.in` 都提交。

规则：

- 只接受轮子，用户机器上不编译。本机代码（如 `lz4`、`pycryptodome`）可以，但 `linux/amd64` 和 `linux/arm64` 都得有轮子。
- 不能和主程序自带的包重名（`httpx`、`pydantic`、`cryptography` 等直接 import）。生成时它们自动排除；手写进锁文件会被拒绝。
- 锁文件必须在插件目录里、不在点开头的目录下，`scripts/pack.py` 会核对并把它和 `requirements.in` 一起打进包。安装时服务器才联网下载，包里不带轮子。
- 批准页列出依赖包数、大小、哪些含本机代码；授权按锁文件内容的哈希记录，锁文件变了升级时会再问一次。依赖目录排在 `sys.path` 最后，`.pth` 不执行。

CI 对带 `[dependencies]` 的插件额外做两件事：用 `python -m movieclaw_sdk.deps check` 按最新服务器 Release 的 `host-constraints.txt` 校验锁文件（钉死、哈希、只轮子、不与主程序重名）；再在 `python:3.12-slim-bookworm`（服务器镜像的底）里，`linux/amd64` 和 `linux/arm64` 各执行一次 `pip install --require-hashes --only-binary=:all: --no-deps -r <锁文件>`，真下载一遍。

## 打包

本地可以用 `mclaw plugin pack <目录>`，得到 `<id>-<版本>.mcplugin`。它跳过 `__pycache__`、`.git`、`.venv`、`node_modules`、点开头的目录、`*.pyc`、`*.mcplugin`、`.DS_Store`，拒绝软链接，并保留可执行位。上限：压缩包 50 MB，解压 200 MB，5000 个文件。清单必须在 zip 根上。Python 插件必须有 `<entry>.py` 或 `<entry>/__init__.py`。

`mclaw plugin pack` 用的是文件当时的修改时间，同一份源码打两次，sha256 会变。本仓库发版用 `scripts/pack.py`：文件列表排好序，时间戳钉死，普通文件记成 `0644`、带可执行位的记成 `0755`，压缩级别固定，打完打印一行：

```text
hello-note-0.1.0.mcplugin SHA256 <hex> SIZE <字节数>
```

发布出去的哈希以这一行为准，不要用本机 `mclaw plugin pack` 的结果去对索引。`tests/` 如果放在插件目录里，会跟其他源文件一样进包。

```bash
python3 scripts/pack.py example/hello-note --output /tmp/hello-note-0.1.0.mcplugin
```

## 上传、批准、安装

商店做出来以后，下载仍要走现在这条链路，不要假设有「从 URL 安装」。

| 动作 | 命令 |
|---|---|
| 开发安装 | `mclaw plugin dev <目录> --once` |
| 打包 | `mclaw plugin pack <目录>`，或本仓库的 `scripts/pack.py` |
| 上传 | `mclaw app plugins packages upload --file <包>` |
| 批准 | `mclaw app plugins packages approve <id> --version <版本> --operations-json '[...]' --paths-json '[...]' --yes` |
| 放弃待批准 | `mclaw app plugins packages discard <id> --yes` |
| 查看 | `mclaw app plugins packages list`，`mclaw app plugins list` |
| 回滚 | `mclaw app plugins packages rollback <id> --yes` |
| 卸载 | `mclaw app plugins packages uninstall <id> --yes` |

上传只做校验，解到 `data/plugins/packages/<id>/<版本>.pending/`。同一个 id 只留一个待批准包，后传的盖掉先传的。

批准前再跑一次 `check_compat`：id 没有被内置或本地插件占用，`sdk` 罩得住当前 SDK，`requires` 里的契约存在、版本够、不是 internal，宿主操作真实存在，路径别名合法，事件声明合法。`inline` 还要 `--allow-inline`。通过后目录改名为 `data/plugins/packages/<id>/<版本>/`，当场挂载。

网页在「设置 → 插件 → 已安装」。来源记为 `package`，按第三方对待。安全模式会跳过本地插件和插件包。

开发循环会把版本改成 `<版本>-dev.<时间戳>` 再上传，因为服务器不接受把同一个版本字符串再装一次。`mclaw plugin dev` 不要拿去对不是开发用的服务器反复装。正式发布不要用 `-dev` 后缀。

权限没有变大（没有新的操作和路径，也不是新申请 `inline`、也不是新申请给成员发通知）时，将来的商店可以代用户按清单原样批准。权限变大，或 `runtime = inline`，必须打开现在的批准界面。默认不要静默自动升级。

## 升级、回滚，以及同一个版本不能再传

服务器不要求新版本号比当前更大，所以用更旧的包盖住当前版是允许的。它拒绝的是**同一个版本字符串**：已经装着 `1.2.0` 时，不能再上传 `1.2.0` 覆盖。撤包重发必须升 `version`，或者用户先卸载再装。正式商店不能靠 `-dev.<时间戳>` 绕过。

激活失败过的版本记在 `bad` 里，用来提醒，不禁止以后再装。用户再次上传并批准时，会把它从 `bad` 里去掉。

回滚是当前版和上一版对调，不用重新下载。更早的目录已经被删掉。要回到更早的包，就再装那次 Release 的文件（版本号可以变小，只要不等于当前装着的那一个字符串）。

升级不迁数据，见上文。索引和商店都不应承诺「升级会把旧键变成新键」，只能承诺宿主不删。

同 id 的插件包会顶掉随应用携带的内置插件。卸掉之后内置版本回来。

## store.json 和在本仓库发版

`store.json` 的字段：

| 字段 | 必填 | 说明 |
|---|---|---|
| `author` | 是 | 显示名 |
| `homepage` | 是 | 绝对 URL |
| `license` | 是 | 这个插件的许可证。仓库许可证还没选定时写「见仓库许可证」 |
| `origin` | 是 | 本仓库只能是 `official` |
| `icon` | 否 | `null`、绝对 URL，或将要放在 `catalog` Release 上的相对路径 |
| `screenshots` | 否 | 字符串数组，规则同 `icon` |
| `summary` | 否 | 清单没写 `description` 时，索引用它当简介 |
| `min_app` / `max_app` | 否 | 只作说明，服务器不检查 |
| `app` | 否 | 预留，现在写 `null` |

发版：

1. `plugins/<id>/` 合进 `main`，清单 `version` 是这次要发的号。
2. 推 tag `<id>-v<version>`，例如 `hello-note-v0.1.0`。
3. `release.yml` 核对 tag 和清单版本，用钉死的 MovieClaw 再跑一遍 `check_plugin.py`，用 `scripts/pack.py` 打包，创建 Release（`--latest=false`），重生 `index.json`，覆盖 `catalog` Release 上的 `index.json`。有签名密钥时一并上传 `index.json.sig`。

索引地址：

`https://github.com/movieclaw/movieclaw-plugins/releases/download/catalog/index.json`

不要用 `/releases/latest`。每个包的地址形如：

`https://github.com/movieclaw/movieclaw-plugins/releases/download/hello-note-v0.1.0/hello-note-0.1.0.mcplugin`

索引里的 sha256 和字节数是发布时对打出来的那一个文件算的。客户端不要自己用源码重打再对比。

## 读 hello-note

`example/hello-note/movieclaw-plugin.toml` 要了三个契约：`library.ingest.completed`、`plugin-data`、`kernel/durable-events`。没有宿主操作，不联网，不是 inline。

`hello_note.py` 里：

- `Settings` 是设置页的表单：一个开关、一个标题前缀。
- `should_note` 和 `remember` 是纯函数。`tests/test_hello_note.py` 只测它们。
- `apply` 登记 id 为 `note` 的可靠订阅。见过的事件 id 存在插件数据的 `seen`，最近片名存在 `last_title`。

在装了对应 MovieClaw 的环境里：

```bash
python3 /path/to/MovieClaw/src/movieclaw_agent/builtin-skills/movieclaw-plugin-dev/scripts/check_plugin.py example/hello-note
python3 -m unittest discover -s example/hello-note/tests -t example/hello-note -v
python3 scripts/pack.py example/hello-note --output /tmp/hello-note-0.1.0.mcplugin
```

本仓库 CI 对 PR 做的就是前两步，MovieClaw 钉在 v0.40.0。

## 装不上时

先看报错原文，不要为了装上而改成 `inline`。

| 现象 | 先查 |
|---|---|
| 清单某个字段不合规 | 多半是未知字段，或 id / 版本 / sdk 写法不对 |
| 未知契约、契约仅供内置插件使用 | `[requires]` 里的名字或版本 |
| 宿主操作不存在 | 操作 id 是否和 `mclaw <命令> --help` 一致 |
| 找不到入口模块 | `entry` 是模块名；文件要在包根上 |
| `@plugin` 名字和清单 id 不一致 | 两处一起改 |
| `inject` 只能写服务键 | 事件用 `ctx.on`，注册表用 `ctx.contribute` |
| 可靠事件的监听器必须有稳定 id | `ctx.on(..., id="...", durable=True)`，并 `inject` `DURABLE_EVENTS` |
| 回调端点没有在清单里声明 | `[permissions] callbacks` |
| 同一版本不能再装 | 升 `version`，或先卸载 |
| `runtime = inline` 被拒绝 | 批准时加 `--allow-inline`；更好的办法是改回 `process` |
| 插件包只支持某平台 | `exec` 的 `platforms` 没包含这台机器 |

回滚：`mclaw app plugins packages rollback <id> --yes`。卸载：`mclaw app plugins packages uninstall <id> --yes`（数据默认还在）。放弃还没批准的包：`mclaw app plugins packages discard <id> --yes`。
