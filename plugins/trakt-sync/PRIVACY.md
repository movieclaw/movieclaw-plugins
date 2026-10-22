# 隐私与授权 / Privacy

本插件在用户自己的 MovieClaw 服务器运行，无开发者运营的中转服务器、遥测或数据收集服务。

插件内置专用 Trakt 应用的公开 Client ID，仅用于识别应用；不包含任何用户令牌或 Client Secret。每个用户仍须明确授权，账号凭据由自己的 MovieClaw 服务器保存，插件开发者不会收到这些凭据。高级用户可覆盖为自己注册的应用。

连接时访问 Trakt 官方 OAuth 与用户设置接口。服务器将 Client Secret（若提供）、访问及刷新令牌、待授权设备码保存在 MovieClaw 插件加密存储；管理员状态页只返回连接状态和用户名，不返回令牌。

播放同步向 `api.trakt.tv` 发送影片外部编号、剧集季集编号、进度；延迟完成事件附带原观看时间。重试可能读取该时间附近的 Trakt 观看记录以避免重复。本地保留有限的事件 ID、播放会话、待同步及失败队列。另外保留每成员最近最多 80 条发送展示记录，包含影片标题、季集、观看和处理时间、发送结果；不包含文件路径、设备名称或授权令牌。展示记录只供本服务器管理员查看，断开该成员连接时一并清除。凭据、媒体数据不会发送给 GitHub 或插件开发者。

管理员可为各 MovieClaw 成员设置不同的 Trakt 连接。请只绑定该成员明确同意使用的账号。管理页面使用 MovieClaw 管理员会话，只向管理员开放。

断开连接会移除该成员的本地凭据、授权请求与同步队列，并尝试撤销 Trakt 授权。网络失败时需在 Trakt 的已连接应用中手动撤销。断开不会删除 Trakt 已有观看记录。MovieClaw 备份及日志保留遵循服务器自己的管理策略；向公开 issue 提供日志前请脱敏。

This plugin runs on your own MovieClaw server. It includes only the dedicated application's public Client ID, not a Client Secret or user tokens. Each user must authorize separately; tokens remain on their own server and are not sent to the plugin developer. Custom application settings are optional. It sends authorized playback identifiers, progress, and (for delayed completion) original timestamps directly to Trakt. It may read nearby history for deduplication. The server retains up to 80 recent delivery display records per member, including titles, episodes, timestamps and outcomes, without file paths, device names or tokens. They are administrator-only and are removed when that member disconnects. OAuth credentials are encrypted by the host. No developer-operated relay, analytics, or telemetry is used. Disconnect removes the member's local connection and queue and attempts remote revocation; existing Trakt history is retained.
