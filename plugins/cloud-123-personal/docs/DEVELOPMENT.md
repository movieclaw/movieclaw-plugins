# 开发与验证

以下命令从 `movieclaw-plugins` 仓库根运行。仅访问公开源码/软件包；测试通过 MockTransport 和假宿主服务运行，不登录真实123账号，也不安装到运行中的MovieClaw。Python 3.12。

## 固定环境

本次验证基线为 MovieClaw v0.41.0 / `5b0739f54fc1e22ba2fd9c4037d132d16e85e0c1`，SDK 1.0，`drive-backends 1.5`。测试会核对 `drives.py` 的源码哈希；升级基准时必须重新审查契约，而不是删掉该断言。v0.40.0 的 `drive-backends 1.4` 没有 `Login.form`，不能运行本插件。

```bash
git clone https://github.com/movieclaw/MovieClaw.git .movieclaw
git -C .movieclaw checkout --detach 5b0739f54fc1e22ba2fd9c4037d132d16e85e0c1
python3.12 -m venv .venv
.venv/bin/python -m pip install -e .movieclaw jsonschema
```

本次复验固定使用v0.41.0发布附件；官方CI按最新服务器Release的约束检查。下载失败时，工作流回退到固定宿主环境生成约束；必须记录实际采用的来源，不能把插件依赖装进宿主环境后再生成：

```bash
mkdir -p dist
curl -fLsS https://github.com/movieclaw/MovieClaw/releases/download/v0.41.0/host-constraints.txt -o dist/host-constraints.txt
# 仅当上面的公开附件不可用时：
# .venv/bin/python -m movieclaw_sdk.deps constraints > dist/host-constraints.txt
```

## 先检查，再安装测试依赖

检查器按宿主环境排除已有包。因此先在**没有插件锁依赖**的环境执行：

```bash
.venv/bin/python -m movieclaw_sdk.deps check plugins/cloud-123-personal --constraints dist/host-constraints.txt
.venv/bin/python .movieclaw/src/movieclaw_agent/builtin-skills/movieclaw-plugin-dev/scripts/check_plugin.py plugins/cloud-123-personal
```

生产入口延迟导入 `p123client`，这一步不需要实际导入或安装它。测试依赖安装到独立目录；该目录仅加入测试进程的路径：

```bash
plugin_deps="$(mktemp -d)"
.venv/bin/python -m pip install --target "$plugin_deps" --require-hashes --only-binary=:all: --no-deps -r plugins/cloud-123-personal/requirements.lock
PYTHONPATH="$plugin_deps" .venv/bin/python -m unittest discover -s plugins/cloud-123-personal/tests -t plugins/cloud-123-personal -v
```

测试覆盖正式SDK/RPC编解码、目录分页及名称边界、登录/凭据刷新、错误分类、300秒下载TTL、上传ACK持久化和独立进程退出恢复。所有网盘与持久化后端均为离线fixture；不证明真实网盘的会话期限或数据库断电耐久性。

独立查看严格契约差异：

```bash
.venv/bin/python plugins/cloud-123-personal/tools/check_contract.py
```

**当前预期退出码为2（阻塞），不能记成通过。** 它读取 `tests/contract_baseline.json`，保留非原生整树及上传不确定窗口。常规117项测试和官方结构检查通过不会清除此门槛。

## 官方仓库检查

```bash
.venv/bin/python -m unittest discover -s scripts/tests -t scripts -v
.venv/bin/python scripts/generate_index.py --check index.json
.venv/bin/python -m unittest discover -s example/hello-note/tests -t example/hello-note -v
```

本次不修改上游CI pin。现有CI仍使用v0.40.0，开发者将自行同步；这里明确使用正式v0.41.0检查插件。`scripts/ci_dirs.py` 会选择个人插件及参考副本，无需注册新名单。

CI实际在 `python:3.12-slim-bookworm` 内分别执行 `linux/amd64`、`linux/arm64` 安装。本地等价命令（仅新建一次性测试容器）：

```bash
for platform in linux/amd64 linux/arm64; do
  docker run --rm --platform "$platform" \
    --mount "type=bind,src=$PWD/plugins/cloud-123-personal,dst=/plugin,readonly" \
    python:3.12-slim-bookworm \
    python3 -m pip install --disable-pip-version-check --no-cache-dir --root-user-action=ignore \
      --target /tmp/deps --require-hashes --only-binary=:all: --no-deps -r /plugin/requirements.lock
done
```

这验证目标架构wheel的安装；不等于插件全流程Linux运行或真实服务验收。

## 依赖与许可证维护

`requirements.in` 固定直接依赖 `p123client==0.0.9.4`，`requirements.lock` 由官方resolver生成，34项闭包全部固定版本和轮子SHA256。宿主提供的包不能重复加入。保持锁原样；需要升级时改输入并重新生成：

```bash
.venv/bin/python scripts/pack.py plugins/cloud-123-personal --lock --constraints dist/host-constraints.txt --output dist/dependency-review.zip
```

更新锁后重新检查实际wheel的LICENSE/NOTICE、补齐 `licenses/` 与 `THIRD_PARTY_NOTICES.md`，重跑上述检查与双架构安装。不要直接编辑哈希或复制其他客户端的GPL/AGPL实现。

## 打包、提交与发布

用仓库原样的 `scripts/pack.py`；包根是清单，不额外包一层目录。可将两次构建输出到临时目录比较字节，然后用宿主 `movieclaw_api.plugins.packages.read_archive` 校验。README、测试和许可证也会进入包；不要把诊断、凭据、截图、HAR、虚拟环境或wheel放入插件目录。

```bash
verification_dir="$(mktemp -d)"
.venv/bin/python scripts/pack.py plugins/cloud-123-personal --output "$verification_dir/check.zip"
```

上例只产生临时归档供验证，正式附件应命名为 `<id>-<version>.mcplugin`。alpha.5已有测试分发记录。**不得用相同版本字符串重新发布不同内容**；本轮文档整理记录在Unreleased，下次实际分发必须先递增清单和CHANGELOG版本。本地已有包不要覆盖，旧交付包应移入系统废纸篓，仅保留最新包及校验文件。

提交新增 `plugins/cloud-123-personal/`；现有CI基准差异见 [要求对照](PR_REQUIREMENTS.md)。没有已发布版本时不手填 `index.json`，不伪造下载URL、签名或发布记录。维护者未来处理合入和发布时，tag必须为 `<id>-v<version>`、版本逐字匹配；Release使用 `--latest=false`，预发布不替代同号正式版，索引由官方发布流程生成。本次没有推送、tag、Release或PR。
