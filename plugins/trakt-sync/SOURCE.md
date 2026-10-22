# 源码来源与维护

本目录与 [movieclaw-trakt 正式版 v0.2.2](https://github.com/o1xhack/movieclaw-trakt/releases/tag/v0.2.2) 的运行时代码、清单和网页一致，使用本目录的 MIT LICENSE。市场评审修复已回移源仓库，后续不会被旧版源码覆盖。

这是兼容 MovieClaw v0.42.0 的播放追踪正式版。不含 dev / v0.3.0-beta.1 的手动已看、收藏或双向历史导入，不依赖尚未合并的 #973。两项新增宿主操作只读元数据，paths 为空，不访问媒体文件。

真实与模拟验收范围见 docs/validation-2026-10-22.md。完整 iPhone 离线恢复链路尚未验收。应用由 MovieClaw 项目账号维护，公开 Client ID 内置，不包含 Secret。

原项目 Release 不直接更新市场。更新提交 PR，经维护者合并后，按市场的 trakt-sync-v<版本> tag 与签名索引发布流程上架；不得移动已发布 tag 或用同版本替换已发布代码。
