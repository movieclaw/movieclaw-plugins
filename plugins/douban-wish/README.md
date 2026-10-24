# 豆瓣想看同步（`douban-wish`）

在豆瓣点了「想看」，MovieClaw 里自动变成订阅。

数据来源是豆瓣移动端「我的收藏」接口（网页和 App 里「想看 / 看过 / 在看」列表用的同一个），
只读公开可见的收藏，**不需要也不接受账号密码**。

```text
GET {base}/user/{用户}/interests?start=0&count=50&for_mobile=1
```

返回按收藏时间倒序，混着电影 / 剧集 / 书 / 游戏，每条自带状态与收藏时间：
`mark` = 想看、`done` = 看过、`doing` = 在看。本插件只处理 `mark` 且类型是电影 / 剧集的条目。

## 一轮同步做什么

1. 从最新往回翻页（每个账号最多 6 页 / 300 条），只收「收藏时间在最近 N 天以内」的想看。
   处理过的豆瓣条目 id 记在插件数据里，不会重复订。
2. 每条转成订阅：电影直接订；剧集按标题里的「第 N 季 / 第 N 部 / Season N / S0N」选季
   （没写季号按第 1 季），并按设置决定是否打开自动续订；已经有订阅的跳过。
3. 对不上的（豆瓣条目在 TMDB 找不到、或对应多部作品）记进「待确认」，用
   `pending` 看、`resolve` 定，不自己瞎猜。

**第一次同步默认只记基线**：把窗口内已有的想看当历史，不订阅，免得一装上就把老想看图订一遍。
想连已有的也订，把设置里的「第一次同步」改成「连已有的也订」。

## 设置

| 字段 | 说明 | 默认 |
|---|---|---|
| 豆瓣 ID `douban_users` | 可以填多个。填主页地址里 `people/` 后面那一段，也可直接粘整条主页链接 | 空 |
| 只同步最近几天的想看 `within_days` | 收藏时间在这么多天以内的才订阅；0 = 不限 | 30 |
| 第一次同步 `first_run` | `since_now` 只订阅以后新点的；`history` 把窗口内现有的也订上 | `since_now` |
| 剧集自动续订 `tv_follow_future` | 订阅剧集时打开「自动续订」 | 开 |
| 每轮最多订阅几条 `max_per_run` | 1～50，剩下的下一轮继续 | 5 |
| 豆瓣接口地址 `api_base` | 留空用官方地址；服务器访问不了豆瓣时填镜像或反代 | 空 |

同步周期不放在设置里，而是一个定时任务「豆瓣想看同步」（默认每 30 分钟），
在「设置 → 定时任务」里调整。

## 命令

| 命令 | 作用 |
|---|---|
| `mclaw plugins douban-wish status` | 当前配置、基线、统计、最近的记录 |
| `mclaw plugins douban-wish check` | 立即同步一次 |
| `mclaw plugins douban-wish pending` | 列出待确认的想看 |
| `mclaw plugins douban-wish resolve` | 处理一条待确认：订阅或忽略 |

## 权限与契约

- 宿主操作：`subscriptions.create`、`subscriptions.list`
- 契约：`host-ops`、`plugin-data`、`plugin-routes`、`plugin-events`、`scheduled-tasks`
- `network = true`：要连豆瓣。走用户在「设置 → 网络」里给 `douban-wish` 配的代理（默认直连）。
- 发出的事件：`douban-wish.subscribed`，配一条被动通知。

## 说明

豆瓣这个接口没有公开文档，字段随时可能变。改动接口相关代码时只动 `parse_wishes` 和
`fetch_page` 两处，其余逻辑都是纯函数并有测试覆盖。
