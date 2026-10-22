# 2.3.0 验证报告

原有 73 项回归 + 7 项去重/上传回归共 80 项，使用 v0.40.0 SDK 全部通过。随后新增带上传监控的真实子进程场景，子进程测试 3 项全部通过，测试总数为 81。

新增覆盖：
- 旧单值累积超过 64 KB 的复现；有效旧记录迁移；3000 条新增记录写入并跨实例读取。
- 强制所有记录落同一分片，确认裁剪后每个值最多 32 KiB。
- 整理入库、订阅完成、媒体库新增、原事件重试，含重启仍仅一条推送。
- 迁移存储失败时不提前发送，避免明知无法记录仍反复发送。
- 上传成功、部分完成、历史任务忽略、空任务/非成功状态忽略、发送失败重试、重启与常规去重过期后不重发。
- v0.40.0 官方 SDK 真子进程：读取宿主操作目录，通过 jobs.list 读取模拟上传成功任务，经本地企业微信 gettoken/message/send 保存已通知任务，正常停止；原有事件和入口配置读取回归也通过。

v0.40.0 SDK 与 kernel 全部文件按官方源码树 Git blob 哈希核对，变动文件从官方固定标签获取；清单验证器与服务键也使用该标签文件。其余未被更新的宿主辅助代码沿用 v0.38.0 本地验证环境，不是完整 v0.40.0 应用集成测试。

上传依据官方 v0.40.0 services/drives/uploads.py：任务类型 drive.upload；仅在任务结束后返回 uploaded、rapid、failed 统计。失败文件可能与成功文件混在一个 succeeded 批次，因此有失败时必须标部分完成。jobs.list 的状态过滤、参数与最大 200 数量已对照 v0.40.0 路由源码。

未验证：实际 Linux 容器安装、用户已有去重数据、真实网盘上传与企业微信投递、公开图片和手机展示。未向真实企业微信发送消息。模拟接口不能代替这些实机验证。

参考：
- https://github.com/movieclaw/MovieClaw/blob/v0.40.0/src/movieclaw_api/services/drives/uploads.py
- https://github.com/movieclaw/MovieClaw/blob/v0.40.0/src/movieclaw_api/api/routes/jobs.py
- https://github.com/movieclaw/MovieClaw/tree/v0.40.0/src/movieclaw_sdk/events
- https://github.com/movieclaw/MovieClaw/blob/v0.40.0/src/movieclaw_sdk/runner.py

2.3.0 增加 5 项回归，86 项全部通过：文件名确认与失败文件排除、分页和清单限制、缺失名称提示、读取失败延后重试、默认模板升级与自定义保留。SDK 子进程场景已加入时间线响应和文件名断言。企业微信接口为本地模拟。

2.3.0 增加 6 项快速新增回归，测试总数 92：合并同剧多集、确认投递顺序、延迟事件去重、首次扫描保护、原有版本过滤、批量兜底、失败重试、接口缺失兜底。SDK 真子进程用原生 FilesAdded 模型接收事件，查询模拟 library.list/library.items.get 并发送，再收到原生 ItemsArrived 时不重复。宿主接口字段对照固定 v0.40.0 源码，未在用户运行环境实测。
