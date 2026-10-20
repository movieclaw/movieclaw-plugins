# 123 云盘个人账号

版本 `0.1.0-beta.1`。通过 `p123client` 的手机号、密码登录链路访问个人网盘，无需 OpenAPI 开发者权益。插件 ID 为 `cloud-123-personal`，Python 包为 `cloud_123_personal`，网盘类型为 `123-personal`，与 `cloud-123` OpenAPI 插件独立。

## 使用要求

需要 Python 3.12、MovieClaw v0.41.0 的正式 SDK 1.0、`drive-backends ^1.5` 和 `plugin-data ^1.0`。插件以独立进程运行，仅请求网络权限。安装后，在宿主的网盘表单中输入手机号和密码；不提供命令行凭据或明文配置方式。

登录使用正式 `Login(form=...)` / `login_with_form`。手机号和密码均为 `SecretStr`，认证结果交由宿主加密认证存储，不写入插件日志或普通存储。有有效 JWT `exp` 时提前120秒重新认证并核验 UID；未知有效期不猜测。验证码返回可恢复的 `RiskControl`，当前不支持挑战续交，也不绕过验证码。

下载链接的默认有效期为300秒，由宿主缓存，不在每次请求时强制刷新。远端未取得可信 SHA1，文件返回 `sha1=None`、能力声明 `list_hashes=False`，不以 MD5 或本地计算值冒充远端 SHA1。

## 扫描与上传边界

扫描按目录完整分页遍历，返回全部文件及子目录 ID（含空目录），不是服务端原生整树接口，也不是原子快照。**这仍偏离 SDK 的原生整树契约，不能声称严格全契约兼容。** 根目录不存在、分页失败或路径冲突时整次失败，不返回部分成功。限制为200000个项目、20000页、256层、8192字符相对路径；超限须缩小扫描根目录。

目录选择保留远端名称的大小写、空格、Unicode 和标点。作为路径段的名称不能含 NUL、正反斜杠，也不能为空、`.` 或 `..`；无法无歧义表示的扫描项明确报错，不静默改名或丢弃。新建文件使用独立的保守名称校验。

上传使用顺序分片。通过正式 `PLUGIN_DATA` 的 `secret=True` 日志保存会话和服务确认（ACK），支持进程重启后继续；日志不保存手机号、密码、token、Cookie、签名 URL 或分片内容。

| 情况 | 处理 |
|---|---|
| 分片成功并已持久化确认 | 从下一片继续 |
| 分片响应丢失或确认未持久化 | 在原会话重传相同片号和字节，刷新签名 URL |
| 合并确认已保存，元数据核验失败 | 只重试元数据核验 |
| 已完成或收到秒传确认 | 重新核验原对象的名称、目录、大小和 MD5 |
| 初始化或合并请求已发出，确认未持久化 | 保留不确定状态并停止，避免重复创建或合并 |
| 恢复日志缺失、会话失效或文件绑定不符 | 明确报错，不猜测进度或创建替代会话 |

恢复依据是持久化 ACK；当前没有经真实非空响应证实的远端已上传分片查询。只支持单个活跃写入实例和顺序重启，不提供多活并发保证。日志不自动过期删除；仍有上传任务时不要清空插件数据。单日志上限60KiB、最多10000片。

首次真实账号验收应使用静止的隔离目录和可丢弃文件，分别检查登录、完整扫描、下载播放及中断上传恢复。自动测试使用模拟网盘与假宿主存储，不能替代真实服务验收。

## 开发与验证

以下从插件仓库根运行，宿主环境使用 MovieClaw v0.41.0（`5b0739f54fc1e22ba2fd9c4037d132d16e85e0c1`）。测试核对正式 `drives.py` 的源码哈希；升级基准需要重新审查契约。插件依赖应装入独立测试目录，先在干净宿主环境执行依赖与官方结构检查：

```bash
.venv/bin/python -m movieclaw_sdk.deps check plugins/cloud-123-personal --constraints host-constraints.txt
.venv/bin/python .movieclaw/src/movieclaw_agent/builtin-skills/movieclaw-plugin-dev/scripts/check_plugin.py plugins/cloud-123-personal
plugin_deps="$(mktemp -d)"
.venv/bin/python -m pip install --target "$plugin_deps" --require-hashes --only-binary=:all: --no-deps -r plugins/cloud-123-personal/requirements.lock
PYTHONPATH="$plugin_deps" .venv/bin/python -m unittest discover -s plugins/cloud-123-personal/tests -t plugins/cloud-123-personal -v
.venv/bin/python plugins/cloud-123-personal/tools/check_contract.py
.venv/bin/python scripts/pack.py plugins/cloud-123-personal --output cloud-123-personal-0.1.0-beta.1.mcplugin
```

`host-constraints.txt` 使用对应宿主发布附件，`.movieclaw` 指上述固定版本的源码目录。117项模拟测试覆盖 SDK/RPC、分页与名称边界、认证更新、错误分类、下载 TTL、上传持久化与跨进程恢复。严格契约检查器当前预期退出码为 **2（阻塞）**，结构检查及模拟测试通过不代表完整语义合规。安装包使用官方打包器及宿主 `read_archive` 校验；发布后的版本内容不得静默替换。

## 许可证

原创适配层采用 [MIT](LICENSE)。运行依赖在安装时按 `requirements.lock` 下载固定版本和哈希的 wheel，插件包不内嵌第三方 wheel 或复制的库源码。34项依赖的归属、完整 LICENSE / NOTICE 原文及未取得原文的明确说明集中在 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)，相同原文仅去重存放，不改变版权归属或许可条款。

未复制 GPL/AGPL 调用者实现，也不重新分发 MovieClaw 核心或 SDK。`store.json` 的 `origin: official` 是目标插件仓库要求的分类，不表示已被接纳或发布。
