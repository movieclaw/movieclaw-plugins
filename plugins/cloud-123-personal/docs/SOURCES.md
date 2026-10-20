# Source audit — 2026-10-19 / 2026-10-20

下方2026-10-19记录对应alpha.1–3历史审计；alpha.4最新发现与实现选择见末节。恢复现已使用正式持久化ACK日志，扫描已启用逐目录分页。

审计前读取参考项目及祖先目录可见 AGENTS.md（未发现适用祖先文件）、公开 MovieClaw AGENTS.md，并应用其内置 movieclaw-plugin-dev 技能：完整 SKILL.md、扩展点、清单、网盘配方和 starter。未执行技能建议的真实安装步骤，因为用户明确禁止生产部署及真实账号操作。

## 固定来源

- [SDK](https://github.com/movieclaw/MovieClaw/blob/a4f82961379e6cea37f548de5b0b78d4ae7a10d4/src/movieclaw_sdk/drives.py) — SHA256 `125a5034f493007f81bd5b85a9cad0a86d23aab59639f01b622cf3710aa10d3d`
- [p123client wheel](https://pypi.org/project/p123client/0.0.9.4/) — SHA256 `01013e13429f4c84255a22ce9ee35973f25162e9bc55ee856c121dd41ef4cb29`
- [fork client](https://github.com/jdzjy/p123client/blob/5170035c750598015b35c5ab80afceb0de60f321/p123client/client.py) — SHA256 `b6853bf22e1d5e7ecf4cc5990a4a6158e6208bf8391c5db3f194de9bd03a331f`
- [P123Disk v2 dependency](https://github.com/DDSRem-Dev/MoviePilot-Plugins/blob/3d8c516/plugins.v2/p123disk/requirements.txt) — SHA256 `c2ca20da5f4b098e16de1e0b78e10157aa091ba5f3abbe46339344dc3a7cf865`

- 官方插件仓库基线 `9bc77e3c605703d855c85b89a39be887a7dd7f0f`，使用原样 `scripts/pack.py` / `pluginlib.py`。
- 审计 MovieClaw `a4f82961379e6cea37f548de5b0b78d4ae7a10d4`。准备此PR前，官方插件CI固定的发布 v0.40.0 核心 `ae0015a8381f6f9c3b340462412cce1644b2401d` 为 drive-backends 1.4，尚无表单契约。
- P123Disk v3 的 pyproject 固定 jdzjy fork `5170035c750598015b35c5ab80afceb0de60f321`，v2 浮动该 fork main.zip。它是 MoviePilot GPL-3.0 插件，不是独立 PyPI p123disk 包。
- 上游 ChenyangGao main `1aea0e5` 的 pyproject 标 0.0.9.3.1，与当前 PyPI 0.0.9.4 不同。实现以实物 PyPI 轮子为准；fork 用于逐方法差异核对，显式保留 base_url 与下载 platform 行为。
- OpenList `drivers/123/types.go`、`driver.go`、`util.go`、`upload.go`、`meta.go` 用作独立字段交叉检查（UID/Nickname/SpacePermanent/SpaceUsed、Etag 是 MD5、上传会话字段）。只参考公开接口结构，不复制 GPL/AGPL 实现。

## 发布轮子里的定位

`p123client/client.py`（0.0.9.4）：
- `P123Client.init/login`：明确 passport+password 分支调用 login_passport，取 data.token；避免使用构造器的自动登录与凭据猜测，插件直接调用正式 login_passport。
- `login_passport`：约 9402 行，user/sign_in；`user_info`：约11971行。
- `fs_info`：约8098行，data.infoList；`fs_list_new`：约8358行，分页与10000搜索上限。
- `fs_export_tree`：约7923行，POST写导出TXT、百万条上限；`tool/iterdir.py` 中迭代方法使用deque逐目录遍历。
- `upload_request/upload_auth/upload_prepare/upload_list/upload_complete`：约11200–11400，实物源码提供请求形状。高级 upload_file 仍写着 TODO 断点续传，不能直接当成SDK分片恢复实现。
- `download_info`：约7037行，远端Etag为MD5、原文件链接、文档声称24小时，但本次没有真实期限证据。
- `P123OpenClient.session/async_session` 使用 verify=False、重试transport，不能采用。插件的request注入走自己的SDK AsyncClient；check_for_relogin=False 避免默认重登路径越过注入点。
- `check_response` 把code1内部错误映射成认证异常，插件不照搬，避免误注销账号。

## 正式认证存储证据

MovieClaw `src/movieclaw_db/repositories/drive_account_repo.py` 的 `_encrypt/save_credentials` 使用 SecretBox；`services/drives/accounts.py` 接收 LoginResult.credentials；`services/drives/gateway.py` 的 `_persist_credentials` 在返回调用结果前持久化；SDK Runner 的 `_handle_drive` 在调用失败时仍返回已刷新的credentials，宿主 RemoteDriveBackend 先赋回再还原错误。插件没有导入这些内部仓储，只使用公开认证交接契约。

所有源码均通过公开下载取得。没有读取用户真实凭据、没有登录123、没有对真实网盘做读写、没有恢复旧私有API项目。

## 轮子与固定 fork 的选择和差异

选用 PyPI 0.0.9.4 是为了官方插件依赖工具支持的固定版本、SHA256 和已发布 wheel；P123Disk 的 fork 是 Git 源码依赖（pyproject 0.0.9.3.1），不能把其 main.zip 当稳定轮子加入锁文件。没有仅凭版本号假定 API 一致：对 AST 移除 docstring 后，`login_passport/login/fs_info/fs_list_new/upload_request/upload_auth/upload_prepare/upload_list/upload_complete` 均一致。`upload_file` 的差异是异步 URL reader 导入与类型标注，插件不调用该高级接口。

手机号密码链相同，都是 `user/sign_in`、passport/password/remember、data.token，再 user_info 查 UID。轮子默认 `https://123pan.com/b`，fork 默认 `https://yun.123pan.com/b`；插件用公开 base_url 参数选择后者。两者登录域均为 `https://login.123pan.com`。fork 的个人 `download_info` 第7065行显式设置 `platform=android`，轮子没有；插件通过正式 headers 参数保留该行为，同时带播放器 UA，测试检查实际请求头。没有修改第三方库。**这只证明源码链路及请求构造一致，不证明真实登录、风控或播放成功。**

## 上传响应核查边界（alpha.2）

完整搜索固定 fork（含tests/tools）、PyPI轮子、P123Disk调用者和OpenList五个123驱动文件：个人 `upload_list` 只有 `file/s3_list_upload_parts` 请求封装，找不到响应字段消费；高级上传仍有断点续传TODO。OpenAPI `upload_list_open` 的 parts 解析属于另一接口，不能移植成个人接口证据。故删除 `Parts/PartNumber/Size/IsTruncated` 假定解析，明确禁用恢复。

OpenList `upload.go:60–73` 的 completeS3 向 upload_complete/v2 传 fileSize、fileId、bucket、key、uploadId、StorageNode、isMultipart；`util.go:234` 按 code==0 接受业务确认，没有额外完成状态字段解析。轮子高级 `upload_file` 第11737行直接调用 upload_complete。插件据此传fileSize并接受通用业务确认，再检查远端目录/大小/MD5；不猜测未文档化的status、async或completed字段。未发现个人合并重复调用幂等性或丢响应后的恢复证明，因此发生异常/取消后绝不靠stat元数据判成功或重放合并。

比较对象均固定于上方公开提交及轮子哈希；本目录不包含本地诊断或交付记录。未复制GPL/AGPL实现进插件。

## alpha.4：扫描、PLUGIN_DATA及新的进度证据

扫描以固定MoviePilot提交 `3d8c516084c3adcd60b29e24dbcf35fc3effbee7` 的 `plugins.v3/p123disk/__init__.py:266–295`、`p123_api.py:132–206` 交叉核实：它递归调用个人fs_list，Page及Next都推进。轮子0.0.9.4 `client.py:8157` 明确next为下一批起始ID、Page不可依赖、每页最多100。插件独立写队列和完整性校验，不复制GPL代码，不采用其遇错返回None/空列表或睡眠逻辑。

正式数据服务来自 `movieclaw_api.plugins.keys.PLUGIN_DATA`（官方开放EXPERIMENTAL服务，surface.json收录，版本1.0），这是SDK目前真实导出位置，不能为满足“只import movieclaw_sdk”的自设测试而伪造新键。官方starter也使用此导入。生产仅从该模块导入PLUGIN_DATA，通过 `inject`、`ctx.use(...).scoped(ctx)` 调用；不导入数据库仓储或宿主内部存储实现。

`movieclaw_sdk/services.py` DataApi、`runner.py` _RemoteStore/ServiceProxy 定义正式RPC；宿主 `services/plugin_data.py:27–35,58–85` 明确单值64KiB、scope须global或entity:id，secret=True经SecretBox后单语句upsert。插件使用`upload:v2`、60KiB上限，测试调用真实宿主scope/大小校验并通过真实RPC编解码。没有修改任何核心文件。

新增有限公开检索，固定来源：

- [rclone-123pan](https://github.com/Ljzd-PRO/rclone-123pan/tree/b876dbc54349169aa1a5e8ca85743d9ce197cb7c)，MIT。`protocol/current-web-2026-08-15.json` 保存作者声称已实测的新会话 `Parts:null`；`backend/123pan/api/types.go:120–123` 保持RawMessage并明确非空恢复尚未验证；`multipart_test.go:556` 拒绝未验证非空数组。此材料只支持新会话空值，不能推断已传片大小/顺序/分页或提交状态。
- [123panNextGen](https://github.com/123panNextGen/123pan/tree/ce9dd68bce4c64705a5814041176468d4c8da714)，GPL-3.0。`src/app/service/upload_service.py:373–390` 消费小写data.parts里的PartNumber，`tests/unit/test_upload_resume.py:79` 是mock响应；其后续使用旧合并链路。它证明有调用者这样假定，不能证明当前服务实际返回或完整分页。未复制代码、未引入依赖。

两个来源字段大小写及验证边界不同，因此alpha.4仍不加入猜测的远端进度解析。改为保存PUT确认后连续进度；丢ACK重传同一会话同一片号同一字节。持久化确认与纯内存重试不同，不再把“没有远端parser”视为无法做任何跨进程恢复。

rclone来源另提2026-08网页控制域名变化；本插件仍按选定fork链使用显式yun.123pan.com/b和login.123pan.com。没有真实登录证明该链当前有效，用户隔离验收必须包含登录和请求可达性；本轮不凭另一客户端记录静默更换认证域或协议。

公开来源均固定到上方提交；本地下载日志不属于PR内容。只引用接口事实并独立实现，没有复制第三方表达性代码；运行依赖34项及其许可证闭包不变。

## PR准备验证基线：v0.41.0

正式tag `v0.41.0` 直接指向 `5b0739f54fc1e22ba2fd9c4037d132d16e85e0c1`，本次工具和测试以该版本为准。前文固定提交保留作为历史协议研究来源，不代表当前测试目标。现有插件仓库CI仍固定旧v0.40.0，开发者将自行同步，本次不修改工作流或加入兼容绕行。

[v0.41.0 DriveBackend源码](https://github.com/movieclaw/MovieClaw/blob/5b0739f54fc1e22ba2fd9c4037d132d16e85e0c1/src/movieclaw_sdk/drives.py) 的SHA256仍为 `125a5034f493007f81bd5b85a9cad0a86d23aab59639f01b622cf3710aa10d3d`；drives、Runner、DataApi、deps、packages及官方检查器与前次审计基线字节一致。核心pyproject仅版本从0.40.0变为0.41.0，依赖声明未变。使用v0.41.0的公开host-constraints附件复查，不把旧版本源码复制成新版本。
