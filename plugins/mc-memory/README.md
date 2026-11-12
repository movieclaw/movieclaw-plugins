# 记忆与上下文管家（mc-memory）

给 AI 助手补两件长期缺失的能力：**记得住**（跨会话的长期记忆）和**省得下**（会话上下文自动压缩）。
当前版本 **0.4.7**。

## 它做什么

每轮对话结束（可靠订阅 `agent.run.finished`，按事件 id 去重，重复投递不会重复处理）只读一次会话轨迹，同时驱动两件事：

**① 自动压缩上下文**

累计用户消息数或最近一次请求的输入 token 达到阈值就调宿主压缩上下文：保留预算内的用户原话 + 生成交接摘要，**只压不删**，完整轨迹仍保留，后续请求从压缩历史继续。
同一个会话有冷却时间；正在运行的会话会被跳过等下一轮。开关、阈值、冷却都可在「设置」里调。

**② 自动提炼长期记忆**

按 Engramory 协议把值得跨会话记住的事实沉淀成一条条笔记，分五类：

| 类别 | 记什么 |
|---|---|
| `iron` | 工作铁律：凌驾一切的硬边界，排序永远最前 |
| `user` | 用户身份、称呼、长期偏好 |
| `feedback` | 对 AI 工作方式的纠正 |
| `project` | 跨会话的决策、约束、进行中任务 |
| `reference` | 外部资源指针（地址、位置） |

- 活跃笔记进入**有界索引**（80 条 / 8 KB 软上限告警，100 条 / 10 KB 硬上限拦截），索引一行一条指针，AI 开局只拿索引、按需取全文，不把正文刷进上下文；
- 写入前查重（字符 bigram 相似度），近似重复会被拒绝，避免同一件事反复记；
- 冷记忆**归档**出索引（内容保留，随时恢复），不是删除；
- 检测到会话新出现压缩摘要时先做一次「续接同步」，把交接摘要里的进行中任务接进记忆库，赶在上下文折叠前存好；
- 自带体检：近似重复对、超大笔记、失效的 `[[链接]]`、长期没被引用的冷记忆。

**提取模型不绑定一家**：设置里最多填三个 OpenAI 兼容后端（如智谱 GLM 为主、DeepSeek 兜底），按槽位顺序组成「主力→兜底」链——额度用尽或鉴权失效自动切下一个，瞬时抖动只重试不切换；只填一个槽位就是单后端，全空则不自动提炼，只保留手动增删。

## 管理页

左侧栏「记忆管家」（`/apps/mc-memory`）：索引列表与卡片视图、全文检索、新增 / 编辑 / 置顶 / 归档 / 恢复 / 删除、体检报告，以及压缩管家看板（压缩历史与累计省下的输入 token，可手动压缩指定会话）。

网页以打开它的人的身份运行：管理员打开时拥有管理员的全部权限。

## 命令

插件路由会进入 `mclaw` 命令目录：

```
mclaw plugins mc-memory index [--query 关键词] [--detail]
mclaw plugins mc-memory get --id <记忆 id>
mclaw plugins mc-memory add --text "…" --category project --name "短笔记名"
mclaw plugins mc-memory update --id <id> [--text …] [--category …] [--name …]
mclaw plugins mc-memory pin --id <id>          # 或 --unpin
mclaw plugins mc-memory archive --id <id>      # 移出索引，内容保留
mclaw plugins mc-memory restore --id <id>
mclaw plugins mc-memory delete --id <id>       # 彻底删除
mclaw plugins mc-memory doctor
mclaw plugins mc-memory stats
mclaw plugins mc-memory compact-status
mclaw plugins mc-memory compact-now --session-id <id>
```

## 权限与数据

- 宿主操作：`session.get-transcript`（会话结束后读新对话，用于提炼与压缩判断）、`session.compact-context`（达到阈值时压缩上下文）。两个都触及对话内容，安装时会单独列出、需要批准；
- 事件：`agent.run.finished`；
- 联网：调用你在设置里填的提取模型接口（走宿主网络通道）；
- 记忆、压缩记录、索引都存在插件自己的持久数据里；API Key 用 `SecretStr` 存，插件不写日志、不导出。卸载时默认保留数据。

## 与「会话管家（session-keeper）」的关系

`mc-memory` 把 `session-keeper` 的自动压缩能力并了进来，两件事共读同一次会话轨迹，省一次读取。
只想压缩、不需要记忆库的，可以单独装 `session-keeper`；两个都装会重复压缩，**不必同时启用**。
