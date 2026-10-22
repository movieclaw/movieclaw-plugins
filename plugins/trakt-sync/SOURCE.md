# 源码来源与维护

本目录收录 [o1xhack/movieclaw-trakt v0.2.1](https://github.com/o1xhack/movieclaw-trakt/releases/tag/v0.2.1)，对应提交 `be9fa0ac636d2cc741a56f5d0f5f139f35dd589d`，许可证为本目录的 MIT LICENSE。

`movieclaw-plugin.toml`、`trakt_sync/` 和 `web/` 与该提交逐字节一致。收录时仅增加市场资料、本文和标准库 unittest 回归测试，并补充中英文安装说明。原项目更完整的 pytest 测试在上述提交的 `tests/` 中；这里的 unittest 测试使本仓库现有 CI 可以直接验证关键行为，不引入运行时依赖。

这是已有的播放追踪版本，不含开发中 0.3.0 的手动已看、收藏及双向历史同步功能，也不依赖 MovieClaw #973。保留测试版说明：联网播放曾在 v0.41.0 验证；本仓库固定的 v0.40.0 仅做自动化契约、入口与模拟请求验证。真实 iPhone 离线播放、恢复联网的完整测试仍未完成。

原项目的 GitHub Release 将无后缀的 `0.2.1` 标为 prerelease。本次保留已有版本号；市场简介与 README 均注明测试版。市场当前按版本号后缀区分预发布，因此 `0.2.1` 不会自动显示为语义版本预发布。

后续更新需要把审核过的版本同步到本目录，修改清单版本和 CHANGELOG，提交 PR；合并后由本仓库维护者推送 `trakt-sync-v<版本>` tag，现有工作流负责打包和更新签名索引。原项目仓库的 tag 不会自动更新这里。不要移动已经发布的 tag，也不要用同一版本号替换不同的运行时代码。
