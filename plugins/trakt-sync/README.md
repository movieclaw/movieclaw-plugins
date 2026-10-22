# MovieClaw Trakt 自动追踪

中文 · [English](README.en.md)

在 MovieClaw 看电影、追剧，自动同步到你自己的 Trakt 账号。插件装在 MovieClaw 服务器上，手机和电视无需另外安装插件。

**[下载插件 v0.2.1（测试版）](https://github.com/o1xhack/movieclaw-trakt/releases/download/v0.2.1/trakt-sync-0.2.1.mcplugin)** · [版本说明与所有下载](https://github.com/o1xhack/movieclaw-trakt/releases/tag/v0.2.1)

市场版本可用后，也可在 **设置 → 插件 → 插件市场** 搜索「Trakt 自动追踪」，检查权限并批准安装。已有同 ID、同版本插件的用户不必重装；切换来源不会增加功能。市场包由本仓库打包，校验和以市场签名索引为准；源码来源见 [SOURCE.md](SOURCE.md)。

## 需要什么版本？

| 使用方式 | MovieClaw 要求 |
| --- | --- |
| 联网播放自动追踪 | **已验证 v0.41.0**，可直接安装 |
| 市场兼容性检查 | v0.40.0 的清单、契约、入口与模拟请求检查通过；不代表该版本的完整播放实测 |
| iOS 下载后观看、离线记录补传 | **v0.41.0 + [服务端兼容修复](https://github.com/o1xhack/movieclaw/tree/fix/offline-viewing-completion)**；原版 v0.41.0 的重看、较旧记录补传可能漏报 |
| 其他 MovieClaw 版本 | 尚未验证，不保证兼容 |

需要 MovieClaw 管理员账号、一个 Trakt 账号，以及服务器能连接 Trakt。**插件已内置专用 Trakt 应用，普通用户无需申请或填写 Client ID。**

**连接名额：** Trakt 免费账号目前可连接 2 款第三方应用，MovieClaw Trakt 占其中一个名额；官方 Trakt 应用不计入。达到上限时，新授权或重新授权可能被拒绝，请先查看 [Trakt 已连接应用](https://app.trakt.tv/settings/apps/connected)。不要为了测试随意撤销已有授权；这不会提高配额。具体以 Trakt 当时的账号政策为准，见 [官方说明](https://github.com/trakt/trakt-api/discussions/943)。

> 当前是测试版。离线链路已完成代码检查和隔离联调，真实 iPhone 飞行模式播放、恢复联网的完整测试尚未完成。v0.1.5 起，安装后直接连接 Trakt、登录授权即可使用。

## 能做什么？

- **账号与同步面板**：每个账号显示连接状态、待发送与失败数量、上次发送时间。
- **具体发送记录**：查看影片、季集、发送结果与失败原因，按状态筛选并单条重试。高级配置默认折叠。

- **正在观看**：开始或恢复联网播放后，让 Trakt 显示正在看的电影或剧集。
- **自动记录已看**：上报观看进度与结束状态，由 Trakt 按其规则记录观看历史；不需要先自动播放下一集。
- **电影、电视剧都支持**：根据影片编号及季、集编号匹配内容。
- **每个成员绑定自己的账号**：家人的观看记录分别同步，没有绑定的成员不会上传。
- **网络恢复后重试**：服务器暂时连不上 Trakt 时保留待同步任务，恢复后继续发送，并对重复事件去重。
- **离线观看后补传**：安装上表中的服务端修复后，完成记录保留原观看时间。App 需再次运行并连上 MovieClaw；离线时无法实时显示“正在观看”。

仅追踪**向 MovieClaw 上报播放活动**的客户端。独立连接 Plex 的 Infuse 播放不会经过本插件。暂停按钮能否立即反映到 Trakt 受 MovieClaw 的事件支持限制；当前主要在退出播放器时同步停止状态。

本插件不导入过去的观看历史，不同步收藏或评分，也不会把 Trakt 进度反向写回 MovieClaw。

## 如何安装？

### 1. 下载插件包

点击 **[下载 trakt-sync-0.2.1.mcplugin](https://github.com/o1xhack/movieclaw-trakt/releases/download/v0.2.1/trakt-sync-0.2.1.mcplugin)**，保存到电脑。

文件保持 `.mcplugin` 格式，**不用解压，也不用下载 GitHub 的 Source code**。不需要运行命令或安装 Python。

### 2. 上传到 MovieClaw

用管理员账号打开 MovieClaw **网页端**：

1. 进入 **设置 → 插件**。
2. 上传刚下载的 `.mcplugin` 文件。
3. 查看权限说明，批准安装，确认「Trakt 自动追踪」已启用。

插件需要读取播放活动、连接 Trakt，并保存账号授权和同步队列；不申请删除媒体或访问媒体文件的权限。

### 3. 登录并授权 Trakt

1. 在 MovieClaw 网页侧边栏打开 **Trakt**。
2. 在账号卡片上点击 **连接 Trakt**。
3. 打开页面给出的 Trakt 授权链接，输入显示的授权码并确认。
4. 回到 MovieClaw，看到 **已连接** 即可。

为其他家庭成员连接时，打开 **高级设置**，填写对应的 MovieClaw 成员 ID（管理员为 `0`），点「查看状态」并连接。让该成员授权自己的 Trakt 账号，每个成员都需要单独连接。

**安装完成。** 用已绑定的成员账号播放一部 MovieClaw 已识别的影片，再到 Trakt 查看“正在观看”。服务器只需要向 Trakt 发起 HTTPS 连接，不需要为插件开放公网端口，也不需要 VPN；手机端无需再次登录 Trakt。

## 更新与常见问题

| 问题 | 处理方式 |
| --- | --- |
| 如何更新？ | 下载新版 `.mcplugin`，在同一个插件上传入口安装并批准更新；同 ID 更新会保留绑定和插件数据 |
| 没有同步？ | 选择对应账号，在「同步活动」查看具体影片和失败原因；匹配失败可展开记录单条重试 |
| 为什么离线记录没有立即上传？ | App 需要再次运行并连接 MovieClaw；App 被彻底关闭时不会保证自动补传。完整支持还需要上表的服务端修复 |
| 为什么有的影片被跳过？ | 内容需要可识别的 TMDB / IMDb 等编号；插件不会仅凭标题猜测 |
| 如何断开？ | 在 Trakt 页面点「断开连接」；Trakt 已有观看历史不会被删除 |

**高级选项：** 需要部署自己 Trakt 应用的用户，仍可在插件设置中填写自定义 Client ID；普通用户保持留空。已有自定义配置不会在升级时被覆盖。

不要同时让多个工具向同一 Trakt 账号提交同一次观看，否则仍可能重复记录。

[技术说明、排错与开发](docs/technical-notes.zh-CN.md) · [隐私与授权](PRIVACY.md) · [更新日志](CHANGELOG.md) · [反馈问题](https://github.com/o1xhack/movieclaw-trakt/issues)

本项目是独立社区插件，非 MovieClaw 或 Trakt 官方产品，未获 Trakt 背书或认证。
