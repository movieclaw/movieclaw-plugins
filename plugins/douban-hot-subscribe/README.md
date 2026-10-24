# 豆瓣热门自动订阅（`douban-hot-subscribe`）

盯住豆瓣的热门榜单（实时热门电影 / 剧集、口碑榜、分类榜等）：新进榜的片自动变成 MovieClaw 订阅。
不看账号、不需要登录，读的是豆瓣公开的榜单。

## 一轮同步做什么

1. 按设置的片单逐个读榜（可用片单见 `mclaw plugins douban-hot-subscribe collections`）。
2. 按口径过滤：评分低于下限的跳过（可另外关掉「暂无评分的不要」来跳过 0 分条目）、
   每个榜只取前几名、标题命中排除词的跳过、库里已有的跳过。
3. **多榜单交错合并**：轮流从每个榜取一条，两个榜齐头并进。直接拼接会出现「电影榜全订完
   才轮到剧集」，看着像插件漏了剧集。
4. 每条转成订阅：电影直接订；剧集按标题里的季号（第 N 季 / 第 N 部 / Season N / S0N）选季，
   没写季号按第 1 季，按设置决定是否补已播出的集、是否自动续订。
5. 每轮最多订 N 条，没订完的下一轮接着订。豆瓣条目对应多部作品或 TMDB 暂未收录的记进
   「待确认」，用 `pending` 看、`resolve` 定，不自己瞎猜。

第一次同步默认「只订以后新进榜的」，把现在的榜当历史——装上一键就跑，不会一次性订一堆；
想连当前榜上的也订，把 `first_run` 改成 `history`（会分几轮订完）。

## 设置

| 字段 | 标题 | 默认 |
|---|---|---|
| `collections` | 盯哪些片单 | 实时热门电影、实时热门剧集 |
| `first_run` | 第一次同步（`since_now` / `history`） | `since_now` |
| `max_per_run` | 每轮最多订阅几条（1～50） | 5 |
| `min_rating` | 豆瓣评分下限（0 = 不看评分） | 0 |
| `skip_unrated` | 暂无评分的不要 | 关 |
| `max_rank` | 每个片单只取前几名（0 = 整个榜） | 0 |
| `exclude_keywords` | 标题含这些词就跳过 | 空 |
| `tv_backfill` | 剧集补已播出的集 | 开 |
| `tv_follow_future` | 剧集自动续订 | 开 |
| `skip_owned` | 库里已有就跳过 | 开 |

`collections` 可以填中文别名（实时热门电影、影院热映、一周口碑电影、Top250、热门美剧、国内综艺…），
也可以直接粘片单引用（`douban:movie:xxx`、`tmdb:tv:xxx`），tmdb 的榜单同样能盯。
`exclude_keywords` 一行一个，英文词按整词匹配（`TC` 不会命中 `TCA`，`TS` 不会命中 `Hits`）。

## 命令

| 命令 | 作用 |
|---|---|
| `mclaw plugins douban-hot-subscribe status` | 当前配置、统计与最近的记录 |
| `mclaw plugins douban-hot-subscribe check` | 立即同步一次 |
| `mclaw plugins douban-hot-subscribe preview` | 看看现在会订哪些（不真订） |
| `mclaw plugins douban-hot-subscribe collections` | 列出可用的片单 |
| `mclaw plugins douban-hot-subscribe pending` | 列出待确认条目 |
| `mclaw plugins douban-hot-subscribe resolve` | 处理一条待确认：订阅或忽略 |

同步周期不放在设置里，而是一个定时任务，在「设置 → 定时任务」里调整。

## 事件与通知

`douban-hot-subscribe.subscribed`：豆瓣热门条目转成订阅时发出，通知里带片名和所属片单。

## 权限与契约

- 宿主操作：`discover.list-collections`、`discover.browse-collection`、`subscriptions.create`、
  `subscriptions.list`
- 契约：`host-ops`、`plugin-data`、`plugin-routes`、`plugin-events`、`scheduled-tasks`

榜单由宿主的 discover 接口读取，插件自己不发外网请求。

## 说明

热门榜每天都在变，建议先跑一次 `preview` 看看会订什么，再放开 `max_per_run`。
新片常常还没有评分，`min_rating` 一旦设了正值，刚进榜的片子会被一起挡掉——这是它有意的。
