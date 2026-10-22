# 源码来源与维护

本目录基于 [o1xhack/movieclaw-trakt v0.2.1](https://github.com/o1xhack/movieclaw-trakt/releases/tag/v0.2.1)，基线提交 `be9fa0ac636d2cc741a56f5d0f5f139f35dd589d`，使用本目录的 MIT LICENSE。

市场审查后已修改运行时代码，版本改为 `0.2.2-beta.1`，不再与原版逐字节相同：修复队列容量和授权失效阻塞、后台调度、心跳记录、令牌处理及市场文档，并增加标准库 unittest 回归。所有新增 Trakt 自动测试均使用模拟请求。

这是播放追踪版本，不含开发中 0.3.0 的手动已看、收藏及双向历史同步，也不依赖 MovieClaw #973。本仓库 CI 固定的 MovieClaw v0.42.0 用于契约、入口、隔离宿主集成与自动回归验证。真实 iPhone 离线恢复完整链路尚未验收。

默认应用由 MovieClaw 项目账号持有，公开 Client ID 内置，采用无 Secret 的设备码流程。应用维护与验收约定见 APPLICATION.md。

后续更新需要把审核过的源码同步到本目录，修改版本和 CHANGELOG，提交 PR；维护者合并后推送 `trakt-sync-v<版本>` tag，由市场工作流打包和更新签名索引。原项目 Release 不会直接更新市场，自动提 PR 也需要审核与上游发布。原项目的后续同步必须保留本轮修复，不能用旧源码覆盖；不要移动已发布 tag 或以同一版本替换不同运行时代码。
