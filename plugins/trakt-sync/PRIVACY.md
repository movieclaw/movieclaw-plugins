# 隐私与授权 / Privacy

本插件在用户自己的 MovieClaw 服务器运行，无开发者运营的中转服务器、遥测或数据收集服务。

本插件内置由 MovieClaw 项目账号维护的公开 Client ID，仅用于识别应用，不包含任何用户令牌或 Client Secret。每个用户仍须明确授权，账号凭据由自己的 MovieClaw 服务器保存，插件开发者不会收到这些凭据。高级用户可覆盖为自己注册的应用。

连接时访问 Trakt 官方 OAuth 与用户设置接口。服务器将 Client Secret（若提供）、访问及刷新令牌、待授权设备码保存在 MovieClaw 插件加密存储；管理员状态页只返回连接状态和用户名，不返回令牌。

播放同步向 `api.trakt.tv` 发送影片外部编号、剧集季集编号、进度；延迟完成事件附带原观看时间。重试可能读取该时间附近的 Trakt 观看记录以避免重复。本地保留有限的事件 ID、播放会话、待同步及失败队列。重试通过宿主 `library.list` 和 `library.items.get` 只读查询媒体元数据；队列只保存媒体/文件 ID 和 Trakt 标识，不保存返回的文件路径，不访问视频文件。另保留每成员最近最多 80 条发送展示记录，包含影片标题、季集、观看和处理时间、发送结果；不包含文件路径、设备名称或授权令牌。展示记录只供本服务器管理员查看，断开该成员连接时一并清除。凭据、媒体数据不会发送给 GitHub 或插件开发者。

账号和同步偏好按成员隔离，但管理接口仅管理员可用，沿用宿主已有的管理员鉴权。普通成员自助管理尚未提供，不依赖网页自报身份。请只绑定成员明确同意使用的账号。

断开连接会移除该成员的本地凭据、授权请求与同步队列，并尝试撤销 Trakt 授权。网络失败时需在 Trakt 的已连接应用中手动撤销。断开不会删除 Trakt 已有观看记录。MovieClaw 备份及日志保留遵循服务器自己的管理策略；向公开 issue 提供日志前请脱敏。

This plugin runs on your own MovieClaw server. It bundles only the public Client ID of the MovieClaw-maintained application, never a Client Secret or user tokens. Each user must authorize separately; tokens remain on their own server and are not sent to the plugin developer. Custom application settings are optional. It sends authorized playback identifiers, progress, and (for delayed completion) original timestamps directly to Trakt. It may read nearby history for deduplication. The server retains up to 80 recent delivery display records per member, including titles, episodes, timestamps and outcomes, without file paths, device names or tokens. They are administrator-only and are removed when that member disconnects. OAuth credentials are encrypted by the host. No developer-operated relay, analytics, or telemetry is used. Disconnect removes the member's local connection and queue and attempts remote revocation; existing Trakt history is retained.

Retries use two read-only host metadata operations (`library.list`, `library.items.get`). Local media/file IDs are retained for matching; returned filesystem paths are not stored or accessed.

## 0.3 历史与手动操作 / History and manual actions

三个实时同步偏好在每个成员的插件 profile 中保存，默认关闭。开启后，手动标记向 Trakt 写入观看日期，手动取消已看删除对应电影／单集的全部 Trakt 观看记录，新增收藏加入 Watchlist。取消收藏不移除 Watchlist。

管理员在独立历史页手动扫描，经完整预览和 Confirm 后，网页逐批读取或写入。`playback.sync.catalog` 读取所选成员可见的库内影片、单集及观看事实；`playback.sync.import` 将匹配的 Trakt 原日期和重看事实导入所选成员。插件保存扫描计划、标题、日期、批次状态及去重回执供 History 查询；这些批次不属于最近 80 条活动的容量限制，断开账号后保留用于核对，但旧任务无法写入新绑定。另保存按成员、条目编号哈希与绑定代次隔离的取消已看及最近成功观看时间标记，防止旧事件或旧重试覆盖较新的操作；不包含文件路径或凭据。无文件系统权限，不读写视频。

The three member-scoped preferences default off. Enabled manual unwatched actions remove all Trakt watches for the selected movie/episode; new favorites add to Watchlist. Administrator-triggered history transfers read local holdings/watch facts and Trakt history, preserve original dates/rewatches, and import only matched local units after preview and Confirm. Plans, titles, dates, batch outcomes and deduplication receipts persist separately from the bounded activity feed; disconnect retains this audit history while preventing old tasks from writing through a new binding. No filesystem access is granted.
