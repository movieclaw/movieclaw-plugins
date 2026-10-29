# 正式版发布到插件市场的自动 PR

这套自动流程已有代码与隔离回归，暂未做真实跨仓库自动提交验证。`marketplace/release.toml` 默认 `enabled = false`；手工提交市场更新不会自动开启它。它独立于历史同步功能，不能用来绕过缺失的 MovieClaw 接口。0.3.0 的目标正式宿主为 v0.44.0。

## 每版的发布标记

提交在**待发布版本的源码**里：

```toml
# marketplace/release.toml
enabled = false
movieclaw_release = "v0.41.0"
```

`movieclaw_release` 是这个插件版本已经验证的 MovieClaw Server 正式版，不是随意填写的最低版本。不要把这些自定义字段放进 `movieclaw-plugin.toml`，宿主会拒绝未知清单字段。

启用后：本地发布脚本或人工把版本推到 GitHub，发布该 tag 的 Release，即触发 `marketplace-sync.yml`。单纯在本地打 tag、推送 tag 或提交代码不会提交市场 PR。不需要定时轮询。

如果以后改为另一个 Actions 工作流使用其默认 `GITHUB_TOKEN` 创建 Release，GitHub 的防递归规则可能阻止后续 release 事件工作流。那时应在发版任务里显式调用本流程的 `workflow_dispatch`，或使用能触发事件的专用身份；不能只看到 Release 存在就认定同步任务跑过。

插件与 MovieClaw 两边都要求 `v主.次.修订`、存在已发布的 Release、`draft=false`、`prerelease=false`；清单与 pyproject 版本相同，源码必须是该 tag 的确切干净提交。没有 beta 后缀仍可能是预发布，例如当前 v0.2.1，因此不能只凭名字判断。不会因测试这套流程把 v0.2.1 改成正式版，也不会把开发中的 0.3.0 提交到市场。

## 只读演练

先把本工作流合并到本插件仓库的默认分支。以后待测的正式版 tag 也必须包含 `marketplace/` 配置、store.json 与 tests。

在 Actions → **Sync release to plugin market** → Run workflow：

1. 填已发布的插件正式版 tag。
2. 保持 `apply` 不勾选（默认）。演练允许配置中的 `enabled=false`。
3. 查看任务总结和 `market-candidate` 附件：候选目录、来源提交、PR 正文、生成的索引及测试包。附件保留 7 天。

没有符合条件的正式版时，先运行仓库中的离线测试；不要为了演练伪造正式 Release。真实演练必须选择已正式发布且含上述配置的版本。

演练在无写入凭据的任务里进行：检查指定 MovieClaw 正式版、市场 ci.yml 与 release.yml 的固定基线（要求两者一致），在指定正式版跑原插件 pytest，在两套宿主上跑清单/入口检查和市场 unittest；再运行市场 lint、索引生成/复现和打包。源源码、tag、基线或测试不符就失败，不把流程成功当成用户设备验收。

## 后续开启自动提交

以下步骤留待真实测试时执行：

1. 给本仓库配置 Secret **MARKETPLACE_PR_TOKEN**。它需要能向 `o1xhack/movieclaw-plugins` 的新分支写入，并以该身份向公共上游 `movieclaw/movieclaw-plugins` 创建 PR。默认 `GITHUB_TOKEN` 只限当前仓库，不能代替。可使用专用账号的 classic PAT（`public_repo`），但该 scope 涵盖该账号有权限的公共仓库，应单独管理和设置过期时间；不要把本机 `gh` 登录令牌直接上传。GitHub App 等方案需实际具备上游 PR 创建权限，不能只因装在 fork 就假定足够。
2. 在下一版源码中把 `enabled` 改为 true，填写真实兼容的宿主正式版并提交。旧 tag 的配置不改，也不移动 tag。
3. 发布该版本的正式 Release，或手动 Run workflow 并勾选 apply。仍然必须通过全部资格与兼容性检查。

写入任务与测试任务分开：只下载已检查的候选，不执行候选插件代码；专用 token 只交给最后的提交步骤。不会改变 GitHub 全局权限、自动批准或合并 PR。

## 同步与重试

- 只更新 `plugins/trakt-sync/` 与市场脚本生成的 `index.json`。市场资料来自发布源码的 `marketplace/store.json`，测试来自 `marketplace/tests/`；运行时文件按明确允许列表复制。未发布的新版本不会被伪造成索引里的下载记录。
- 每个版本使用确定分支 `sync-trakt-sync-v<版本>`，已有相同候选的开放 PR 就复用。不覆盖维护者修改，不 force-push；同版本候选不同或 PR 被关闭时需人工处理。
- 推分支成功而创建 PR 请求失败，可以重跑；验证分支内容一致后只重试创建 PR。市场 main 或发布 tag 在检查后变化时停止，重新演练后再重试。
- 如果市场已经有同版或更高版本，跳过提交；不自动降级，也不更换已发布版本的运行时代码。
- 上游的合并、CI 审批与 `trakt-sync-v<版本>` 发版仍走其现有机制。我们自己的 Release、候选包和成功创建 PR 都不等于已经上架。

## 本地验证

在已安装对应 MovieClaw SDK 的开发环境里运行 `python -m pytest tests/test_marketplace_sync.py -q`。它只使用临时本地 Git 仓库与模拟 GitHub 响应，覆盖预发布标记、明确 opt-in、发布允许列表、软链接、未提交修改、候选校验、推送后恢复以及同版本去重，不触发真实发布。

维护入口：`scripts/marketplace_sync.py`；准备与提交分成子命令，`prepare` 本身只读 GitHub、写本地候选，绝不会调用创建分支或 PR 的接口。未完成检查时不要手工调用 `record-validation` 把候选冒充成已验证。
