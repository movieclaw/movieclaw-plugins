# 123 云盘（cloud-123）

把 123 云盘接进 MovieClaw 的媒体库，实现 `movieclaw_sdk.drives.DriveBackend`。
通过 [P123Client](https://github.com/ChenyangGao/p123client) 使用账号接口，不使用需要开发者权益包的开放平台接口。
账号管理、凭据保存、请求限速、上传调度和播放界面由宿主提供；插件负责与网盘交互。

- 登录：扫码或 Token 表单，并核对账号身份。
- 账号：显示容量与会员信息。
- 目录：选择目录、完整列举文件树、逐级创建目录。
- 上传：MD5 秒传、普通分片上传、已有会话恢复；会话和分片确认通过 `PLUGIN_DATA` 保存。
- 文件：查询详情、按播放器 User-Agent 取原文件直链；删除仅移入回收站。

## 使用

在 MovieClaw 的「设置 → 插件」安装插件，批准网络权限及锁定依赖。
随后在网盘账号中选择「123 云盘」，扫码或填写 Token，选择需要绑定的网盘目录。
Token 只填在宿主的凭据表单中，不要放进反馈、截图或日志。

插件运行于独立进程，要求 SDK `^1.0`、`drive-backends ^1.5` 和 `plugin-data ^1.0`。
升级现有安装时保留插件数据，以便恢复未完成的上传会话。

## 上传核验与边界

- 普通上传校验每片 MD5 与 PUT ETag，合并前完整对账分片，合并明确成功且成品绑定一致后返回。
- 秒传、已有成品和证据不足的恢复路径，通过连续 Range 回读全文 MD5，并核对读取前后的文件绑定。
  详情中的 Etag 仅作为取链参数，不当作可信的全文 MD5；`stat` 和列表中的 `sha1` 为 `None`。
- 合并结果未知时先只读回查；没有足够完成证据就报错并保留会话，不重复合并。
  宿主应在核验成功后才释放本地文件。
- 没有插件自定的文件总大小、两片或整会话容量上限。分片长度沿用宿主的 512 MiB 单片能力；
  回读请求按 16 MiB 分段。实际可用规模仍受网盘、宿主内存和账号请求预算约束。
- 回读受宿主预算计量，超时、限流或取消后保留本地，重试时从头核验；不持久化部分 MD5 状态。
- 不提供操作动态增量接口。容量查询是 `account_info`，单个文件查询是 `stat`。

当前为 beta 测试版本。离线测试覆盖多分片、恢复、合并结果未知、全文 MD5、取消和清理重入。
beta.5 已在实际宿主完成账号容量刷新与 59.46 MiB 单集的秒传、核验及库内本地释放；
该次通过定点扫描入库，不代表下载自动监听或浏览器播放已通过。
beta.5 的非秒传大文件与中断续传仍待真实验收，不能用离线通过或早期版本的采样替代。

## 文件组织

```text
cloud-123/
├── movieclaw-plugin.toml   # 宿主清单
├── store.json             # 官方仓库市场元数据
├── CHANGELOG.md
├── LICENSE
├── README.md
├── requirements.in
├── requirements.lock
├── cloud_123/
│   ├── __init__.py        # 注册、存储注入及加载自检
│   ├── backend.py         # SDK 契约入口
│   ├── client.py          # 请求适配与错误分类
│   ├── upload.py          # 上传、对账与恢复
│   ├── upload_store.py    # 会话持久化与清理
│   └── readback.py        # 详情、取链及 MD5 回读
├── tests/                 # unittest 离线回归
└── tools/                 # 可选的手动协议采样工具
```

## 测试与打包

沿用仓库 CI 的 Python 3.12 和 MovieClaw 环境。以下命令在**插件仓库根目录**执行；
先按 [CI](../../.github/workflows/ci.yml) 的 `MOVIECLAW_REF` 安装对应宿主。
插件依赖安装到独立目录，仅供测试使用，避免污染宿主的依赖检查。

```sh
python3.12 -m movieclaw_sdk.deps check plugins/cloud-123 --constraints /path/to/host-constraints.txt
python3.12 /path/to/MovieClaw/src/movieclaw_agent/builtin-skills/movieclaw-plugin-dev/scripts/check_plugin.py plugins/cloud-123

deps_dir="$(mktemp -d)"
python3.12 -m pip install --target "$deps_dir" --require-hashes --only-binary=:all: --no-deps \
  -r plugins/cloud-123/requirements.lock
PYTHONPATH="$deps_dir" python3.12 -m unittest discover \
  -s plugins/cloud-123/tests -t plugins/cloud-123 -v

python3.12 scripts/generate_index.py --lint
python3.12 scripts/pack.py plugins/cloud-123 --output /tmp/cloud-123-0.1.0-beta.5.mcplugin
```

测试使用真实 SDK 类型和请求适配，在 HTTP transport 层模拟网盘响应，不连接真实网盘；
网页面板测试仅访问本地回环。`test_account_info.py` 还覆盖登录与目录契约。
手动协议采样见 [tools/README.md](tools/README.md)，不会随插件加载而自动启动。

依赖变化时按仓库 [GUIDE](../../example/GUIDE.md) 重新生成锁文件；普通打包复用已审核的锁。
安装包、采样数据、私有证据和个人验收截图放在仓库外，不提交到 PR。

## 许可证

本插件自有源码以 **MIT** 许可发布，作者为 **zactony**，见 [LICENSE](LICENSE)。
锁定的 `p123client==0.0.9.4` 的包元数据和随包 LICENSE 均为 MIT，未发现必须采用 cloud-115 的 GPL 许可证的依据。

第三方依赖保留各自许可证，不因本插件采用 MIT 而重新授权。其中 `orjson==3.13.0` 声明
`MPL-2.0 AND (Apache-2.0 OR MIT)`；MPL 的要求针对其覆盖的文件，参见 [Mozilla FAQ](https://www.mozilla.org/en-US/MPL/2.0/FAQ/#q11-how-viral-is-the-mpl-if-i-use-mpl-licensed-code-in-my-proprietary-application-will-i-have-to-give-all-the-source-code-away)。
依赖不内嵌在插件源码或安装包中，由宿主按 `requirements.lock` 下载并保留原有许可文件。
MovieClaw 宿主的许可证独立适用，本插件的 MIT 许可不改变宿主授权条件。
