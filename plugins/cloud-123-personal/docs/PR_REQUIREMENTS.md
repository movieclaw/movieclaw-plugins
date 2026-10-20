# 官方仓库要求对照

检查日期：2026-10-20。官方 `main` 基准：`9bc77e3c605703d855c85b89a39be887a7dd7f0f`。

完整阅读及核对的来源：

- [README（添加插件、版本/tag、索引、许可证）](https://github.com/movieclaw/movieclaw-plugins/blob/9bc77e3c605703d855c85b89a39be887a7dd7f0f/README.md)
- [作者指南（清单、权限、生命周期、依赖、打包、测试、发布）](https://github.com/movieclaw/movieclaw-plugins/blob/9bc77e3c605703d855c85b89a39be887a7dd7f0f/example/GUIDE.md)
- [CI](https://github.com/movieclaw/movieclaw-plugins/blob/9bc77e3c605703d855c85b89a39be887a7dd7f0f/.github/workflows/ci.yml)、[发布工作流](https://github.com/movieclaw/movieclaw-plugins/blob/9bc77e3c605703d855c85b89a39be887a7dd7f0f/.github/workflows/release.yml)
- 同基准 `scripts/pack.py`、`pluginlib.py`、`ci_dirs.py`、`check_tag.py`、`generate_index.py`、`scripts/tests/`、`index.schema.json`、`example/hello-note/`。
- [MovieClaw开发技能及引用资料](https://github.com/movieclaw/MovieClaw/blob/5b0739f54fc1e22ba2fd9c4037d132d16e85e0c1/src/movieclaw_agent/builtin-skills/movieclaw-plugin-dev/SKILL.md)：AGENTS、扩展点、清单、网盘配方、starter和实际DriveBackend源码。

该插件仓库没有独立CONTRIBUTING文件、PR模板或AGENTS。README部分示例描述仍旧，GUIDE主体明确基于v0.37.0；以实际工作流、清单检查器和固定SDK源码共同判断，不把旧文案当作新能力的证据。

| 要求 | 本次落实/状态 |
|---|---|
| 新插件位于 `plugins/<id>/`；不改随应用参考副本 | 独立 `cloud-123-personal`；无OpenAPI或核心源码改动 |
| id、目录、装饰器一致；3–40位、小写连字符 | `cloud-123-personal`；Python包 `cloud_123_personal`；注册类型 `123-personal` |
| 清单字段仅宿主允许；元数据分离 | TOML保留运行/契约/权限/锁；作者/主页/MIT/origin在store.json |
| process、最小权限、公开注入 | process；只请求network、无业务操作/路径权限；PLUGIN_DATA正式服务 |
| `apply`快速登记、资源可撤销 | 注册后端并用ctx.effect关闭客户端；延迟导入第三方库 |
| 契约版本真实 | SDK^1.0、drive-backends^1.5、plugin-data^1.0；不降级隐藏表单需求 |
| 检查先于测试依赖安装 | 纯宿主环境check_plugin；插件锁依赖仅加到测试路径 |
| 固定直接依赖和完整闭包 | requirements.in + 官方生成lock；34包版本/轮子哈希，宿主包排除；本轮不改锁 |
| 双Linux架构wheel安装 | 使用官方同款Python3.12 Debian镜像实际安装；结果见VALIDATION |
| 许可证事实及归属 | 原创MIT；33项正文保留，urllib3-future-request的MIT声明缺正文待上游补齐；GPL/AGPL实现不打包 |
| README/CHANGELOG/本目录tests | 自包含开发命令、beta.1记录、117项离线测试、严格差异门槛 |
| 官方目录选择/索引/脚本检查 | 测试选择个人插件及其lock；目录自动发现，无需注册新名单；index不手改 |
| 确定性包/服务器结构校验 | 使用原样pack.py两次比较，再read_archive；交付独立beta.1安装包及SHA256 |
| 版本/tag/发布 | 当前0.1.0-beta.1；同一Draft PR追加普通提交，无tag、Release或自动安装 |
| 私密内容与交付排除 | 只提交明确文件范围；无本机路径、真实凭据/账号、HAR、截图、包或本地证据；历史证据另留存 |
| 严格DriveBackend语义 | **未通过**：递归扫描偏离原生整树；初始化/合并确认丢失窗口安全阻塞；不隐藏差异 |
| 真实服务 | 维护者反馈alpha.5目录选择复测通过；不外推到扫描、上传恢复、下载/播放或风控 |

## 现有CI基准差异（不修改）

现有CI和release均固定 `ae0015a8381f6f9c3b340462412cce1644b2401d`（v0.40.0）。实际检查报 `drive-backends` 需要1.5、当前1.4，并在导入时遇到 `Login(form=...)` 不存在。开发者将自行同步旧CI；本次不改任何工作流、仓库公共脚本或官方根README/GUIDE，也不增加v0.40.0兼容绕行。

本次正式验证目标为 **MovieClaw v0.41.0**，tag指向 `5b0739f54fc1e22ba2fd9c4037d132d16e85e0c1`。使用该版本真实检查器、SDK、服务接口及宿主约束；SDK仍为1.0，drive-backends为1.5。v0.41.0的list_tree原生整树条款并未放宽。

**唯一提交范围：`plugins/cloud-123-personal/**`**（排除本目录.gitignore忽略项）。目录自动发现，不需要注册或预写index记录。现有CI未同步前，原仓库流水线仍会因旧契约版本失败；本地v0.41.0检查通过不代表当前托管CI已经通过。

不提交本地工作区README/REPRODUCE/辅助scripts、evidence、dist、vendor-audit、.venv和交付清单。上游所有现有文件、其他插件和 `index.json` 不变；本地工作区的白名单.gitignore不属于上游补丁。

## 仍然阻塞的事项

1. **原生整树要求未满足。** 递归完整分页可用，但不是SDK指定的原生整树接口，也不是事务快照。主流程预算耗尽或检查失败会全次报错；不是返回部分树。需要上游明确处理此语义差异，不能由插件仓库单方面改写契约。
2. **上传恢复不是服务端进度重建。** ACK日志可跨进程续传；未知初始化或合并结果无法安全自动确认。未证实的非空远端分片列表不解析；日志丢失、会话失效及多活写入仍有限制。
3. **安装检查不等于真实服务验收。** Linux依赖安装、MockTransport测试和目录选择复测分别记录；扫描、真实上传/恢复、下载播放、认证刷新/验证码仍需隔离验收。

4. **依赖许可正文缺口。** urllib3-future-request 0.0.2声明MIT，但wheel/sdist和源码仓库漏附正文；须向上游取得原通知/正文后补齐，不能称完整许可审查通过。

前两项不是本次改核心的理由。源码门槛 `tools/check_contract.py` 继续退出2；此PR材料可审查，但不能宣称严格全契约或正式发布就绪。
