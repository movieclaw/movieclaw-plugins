# Changelog

## 0.3.0 (2026-10-29)

- Resolve and persist Trakt's stable user UUID before history import. Keep a member-scoped UUID-to-source namespace across reconnections and username changes, migrate retained beta bindings without changing their existing source IDs, and require a fresh preview for unfinished legacy imports. Identity lookup failures back off or pause before host reads/writes; interrupted persistence and token refresh retain safe retry behavior.

- Target official MovieClaw Server v0.44.0, including the shared watch-source IDs from upstream #1063. Earlier servers without playback stopped/completed contracts 1.1 cannot install this version.
- Add independent, default-off preferences for manual watched, manual unwatched and new favorites to Trakt Watchlist. Preserve existing accounts and preferences on same-ID updates; ratings and member self-service are not included.
- Add a separate foreground history page: scan local holdings or paginated Trakt history, review movie/per-season counts, explicitly confirm, then process resumable batches. Opening the page never starts or resumes a transfer.
- Preserve API-returned import times and local source precision. Trakt history uses UTC minutes; disclose same-minute collisions instead of reporting them as distinct successful watches.
- Share durable receipts across real-time delivery and history export, including response loss, process recovery and resume-source aliases. Protect newer watches from delayed unwatch events and preserve incremental imports after cutoff exclusions.
- Retain stable metadata correction, retry/dismissal controls and activity filters. No filesystem access, additional server port or automatic bulk synchronization.

## 0.3.0-beta.3 (unreleased)

- Share durable source receipts between real-time completions and history export using the watch-log IDs proposed in upstream MovieClaw #1063. Keep each member, binding and media identity separate; receipts remain valid when the catalog stop timestamp or resume-group anchor changes.
- Persist approved export intents before remote reconciliation, so late native events recover the same explicit minute after a lost export response or receipt. Preserve exported-minute ownership across aliases and interrupted merge accounting.
- Wait for pending delivery of the same source before exporting; late native events reuse successful export receipts. Preserve skipped same-minute outcomes and persist receipts before removing delivery intent.
- Record successfully reconciled exports in the watch/unwatch ordering barrier before committing receipts. Restore it from fingerprint/shared receipts, including native recovery of an export intent, using precise original source times so delayed older removals cannot erase newer exported watches.
- Require playback.stopped/completed contracts ^1.1. Upstream #1063 is merged and pinned at its official merge commit, pending a formal release; official v0.43.0 is insufficient. Stable main and market remain 0.2.3, with no production update.

## 0.3.0-beta.2 (unreleased)

- Follow Trakt #694 UTC minute precision for history writes and deduplication; reconcile lost responses without confusing adjacent minutes. Keep local seconds/microseconds and event ordering intact. Disclose and skip extra same-minute watches rather than claiming lossless export; imported timestamps retain only precision actually returned by Trakt.
- Reconcile uncertain native scrobbles using a persisted dispatch window after authorization refresh, since Trakt assigns their time. Preserve source-minute matching when switching to explicit history, clear definite-rejection windows, and guard capacity trimming before dispatch.
- Clear a 401-rejected native dispatch window durably before refreshing OAuth tokens, so an uncertain refresh response cannot falsely reconcile an unrelated watch.
- Integrate all stable 0.2.3 authentication, queue-capacity, metadata recovery and UI repairs into the separate dev track.
- Target released MovieClaw v0.43.0 for catalog/import APIs and CI; keep marketplace automation disabled.
- Add three independent default-off member preferences, activity filters and confirmed foreground history import/export with original movie/episode dates, rewatches, grouped previews and durable incremental receipts. No ratings, member self-service or background bulk transfer.
- Treat ordinary HTTP 400 as an item error, continue after terminal action failures, and block only the affected Trakt account on authorization rejection. Missing host grants pause a transfer without invalidating Trakt credentials.
- Apply unwatch cutoffs to retries of older manual watched actions too; display superseded actions as skipped. Newer queued playback waits for a pending removal, and older delayed/removal retries cannot erase a newer successful native/manual watch.
- Coordinate manual unwatches with older playback queues and durable late events through member/unit/binding-scoped persisted cutoffs; newer watches remain eligible.
- Skip exports when external media identity changes after preview, requiring a fresh scan and confirmation.
- Retain previews without starting writes when confirmation-time Trakt revalidation fails; respect Retry-After and block rejected accounts.
- Retain stable retry/correction/dismissal controls and verify real host durable events, admin-only routes and isolated dated import/export without production account writes.

## 0.2.3 (2026-10-22)

- Prevent a completion evicted by capacity trimming after metadata revalidation from being sent without a persisted recovery record.
- Remove delivered or failed jobs by identity, so capacity trimming cannot silently discard the following completion or pop an empty queue.
- Add regressions for near-limit two-job and single-job queues, plus remote-success/process-exit recovery while capacity trimming removes a heartbeat. Capacity evictions retain the existing explicit loss count.

## 0.2.2 (2026-10-22)

- Revalidate failed deliveries using current library metadata and stable file IDs; retire deleted entries and expired live events, and retry transient metadata failures.
- Check paginated Trakt history before replaying a completion, preserving its original watch time; add season/episode correction and dismissal for legacy failures.
- Display retained failure counts, accurate matching errors and retry deadlines. Request only two read-only metadata operations, with no filesystem access.
- Keep server-dependent features on dev / v0.3.0-beta.1, separate from stable main and marketplace releases.

- Prevent a full queue or rejected authorization from blocking the host's shared durable consumer; bound the ledger, prefer expiring live jobs and discarding heartbeats, and show dropped completion counts.
- Pause members with rejected authorization until reconnect, clear unusable queued work, and preserve freshly rotated credentials.
- Wake delivery only for pending work using a plaintext scheduling index; keep heartbeat requests out of the activity log.
- Use the manifest version in User-Agent and add mocked OAuth, overflow, migration and retry regression coverage.
- Install/update guidance now follows the official plugin market. Remove server-fork recommendations and unsupported offline-history guarantees.
- Use the MovieClaw-owned public application with secretless device authorization and OOB refresh.
- Treat item-write 400 as a retryable item failure; distinguish OAuth refresh rejection. Add loss-warning acknowledgement with preserved totals and independent connection health.
- Isolate corrupt legacy credentials and back off unexpected delivery errors per member. Specify UTF-8 manifest decoding.
- Validate against the repository's pinned MovieClaw v0.42.0 host; see APPLICATION.md for application maintenance.

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
- Add optional handling for host extensions carrying `viewing_id`; the public playback contract does not guarantee that field. Complete offline rewatch and out-of-order history are not supported guarantees.

## 0.1.2

- Treat Trakt HTTP 422 for stops below 1% as the documented short-play no-op, rather than a media-matching failure. Covers queued pause requests from earlier versions too.
- Use the stop endpoint for player exits at every progress value.

## 0.1.1

- Accept the official `auth.trakt.tv` device authorization hostname returned by the live Trakt API, alongside `trakt.tv` and `app.trakt.tv`; keep rejecting unrelated domains.
- Add a regression test for the live device response URL.

## 0.1.0 (initial beta)

- Per-member OAuth device authorization, encrypted credentials, persistent playback event queue and retries.
- Administrator Web connection page; SDK lifecycle, route and deduplication tests.
