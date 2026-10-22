# 技术与维护说明

本目录的安装和更新入口是「设置 → 插件 → 插件市场」，见 [README](../README.md)。不需要用户切换服务器分支。清单只申请播放事件、网络和插件数据服务，没有媒体删除或文件访问权限。

## 投递、容量与授权

每个成员独立加密保存授权、使用独立锁；Trakt refresh token 轮换后先落盘，临时落盘失败时在内存保存新令牌，不再次兑换已使用的旧令牌。401 会尝试刷新并重放一次；刷新失败或重放后仍拒绝授权则阻断该成员，清空旧队列，在界面提示重新连接。不把授权失效时收到的事件排进队列，不借用其他成员令牌。

队列最多 64 项，整个 ledger 不超过 60 KB；最多 256 个去重 ID、16 个会话和16 个匹配失败记录。满时先移除过期的非完成任务，再移除心跳和其他非完成任务，最后才舍弃旧完成任务。确实舍弃播放记录时累计未同步数量，界面持续提示；不能把它叫成已同步。容量或授权状态不向可靠消费者抛错。底层持久化故障仍会报错，让宿主按其可靠事件机制处理。

宿主可靠消费者按订阅而非成员共享，不能用某成员的永久满队列去让消费者无限重试。有限队列无法同时保证永不丢失和无限断网，这里明确选择隔离成员、保留近期任务并显示损失。

活动记录独立存储，最多 80 条/48 KB；开始和进度心跳不进入展示，旧版心跳也从读取结果中过滤。记录中不包含令牌、请求正文、设备名或路径。

后台启动时仅为旧数据做一次账号索引迁移。之后只枚举未加密的 `delivery` 标记，按队首下次执行时间唤醒；无任务时等待新任务，不每 5 秒解密遍历所有账号。账号列表用 `linked` 标记枚举，需要显示时才读取对应成员状态。删除成员前必须先断开 Trakt；没有删除事件可自动回收身份。

## 播放与离线边界

Trakt 是否产生观看记录依其进度阈值；不足 1% 的 stop 返回 422 时按清除 watching 的短播放处理，匹配失败的 404/422 可单独重试。宿主显式 completion 即使 position 清零也会作为完成处理；已看状态本身不把一次未完成重看变成完成。

超过 5 分钟才收到的完成事件按原始 `occurred_at` 写历史；不是把整份已有历史导入。宿主未发出的事件无法还原。代码保留可选 `viewing_id` 扩展路径，但官方载荷不保证提供它，这不是当前承诺的能力，也没有证明完整离线重看链路可用。对不确定完成写入，在附近历史中核对后再重试；跨 Trakt 和本地存储没有分布式事务，仍可能重复。

## 应用与验收

官方默认应用暂留空，组织归属及真实无 Secret 刷新/撤销待 [APPLICATION.md](../APPLICATION.md) 关闭。源码不放 Client Secret。仅自行部署者可使用自己注册的应用覆盖配置，Secret 由宿主加密保存。换应用前断开旧连接；发现 Client ID 已不匹配时只清除本地绑定，不拿新应用的凭据去撤销旧应用。

## 在此仓库验证

依赖由 MovieClaw 提供，不在 NAS 手动装 pip。使用本仓库 CI 固定的 MovieClaw 源码和 Python 3.12：

```sh
PYTHONPATH=/path/to/MovieClaw/src python /path/to/MovieClaw/src/movieclaw_agent/builtin-skills/movieclaw-plugin-dev/scripts/check_plugin.py plugins/trakt-sync
PYTHONPATH=/path/to/MovieClaw/src python -m unittest discover -s plugins/trakt-sync/tests -t plugins/trakt-sync -v
python scripts/generate_index.py --lint
python scripts/pack.py plugins/trakt-sync --output dist/trakt-sync.mcplugin
```

测试使用虚构凭据和模拟 Trakt 响应，不把它们当成真实授权验收。包、哈希、签名索引均由本仓库发布工作流生成；不要用重新打包的文件去核对别的发行包的哈希。
