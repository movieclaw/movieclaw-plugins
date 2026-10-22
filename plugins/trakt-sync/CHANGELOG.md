# Changelog

## 0.2.1

- 账号与同步活动面板采用 MovieClaw 活动页的宽栏、分组列表及主题变量。
- 每个账号显示连接状态、待发送/失败数量与上次发送时间，成员 ID 与自定义应用收进高级设置。
- 新增有限发送记录：影片、季集、观看/发送时间、具体 HTTP 错误、自动重试时间及单条失败重试。
- 近期记录每成员最多 80 条，独立于可靠队列；兼容旧队列，无标题时显示外部编号。
- Theme-aware account/activity dashboard, per-member delivery details, safe error messages and selective retry.
- Bounded local activity log (up to 80 records per member), separate from the reliable queue; existing bindings retained.

## 0.1.5

- 内置独立的 MovieClaw Trakt 公开应用，安装后直接登录授权，无需申请或填写 Client ID。
- 自定义应用保留为高级选项；空白字段回退默认应用，旧自定义 Secret 不会混入默认请求。
- 保留已有自定义配置及同应用账号绑定，更新中英文安装、页面提示和隐私说明。
- Bundle the dedicated public Trakt application for sign-in without manual Client ID setup.
- Keep custom applications as an optional override; blank values use the default without a stale custom secret.
- Preserve existing custom settings and same-application connections; update bilingual guides and privacy notes.

## 0.1.4

- 默认中文 README，新增完整英文版，明确宿主版本要求、功能和网页安装流程。
- 添加插件包直接下载入口，将开发、权限和队列细节移入中英文技术说明。
- 插件包包含中英文文档；播放同步逻辑与 0.1.3 相同。
- Chinese-first README with a full English version, explicit compatibility, features, and web installation steps.
- Direct plugin download links and separate technical notes in both languages, bundled with the plugin.
- Playback behavior is unchanged from 0.1.3.

## 0.1.3

- Honor explicit completion events even when MovieClaw resets the resume position to zero.
- Preserve the original timestamp for local viewing completions identified by `viewing_id`, including recent uploads.
- Isolate local viewing sessions by UUID; clear matching live watching without creating a second completed record.
- Document the generic v0.41.0 server compatibility fix required for complete offline rewatch and out-of-order replay support.

## 0.1.2

- Treat Trakt HTTP 422 for stops below 1% as the documented short-play no-op, rather than a media-matching failure. Covers queued pause requests from earlier versions too.
- Use the stop endpoint for player exits at every progress value.

## 0.1.1

- Accept the official `auth.trakt.tv` device authorization hostname returned by the live Trakt API, alongside `trakt.tv` and `app.trakt.tv`; keep rejecting unrelated domains.
- Add a regression test for the live device response URL.

## 0.1.0 (initial beta)

- Per-member OAuth device authorization, encrypted credentials, persistent playback event queue and retries.
- Administrator Web connection page; SDK lifecycle, route and deduplication tests.
