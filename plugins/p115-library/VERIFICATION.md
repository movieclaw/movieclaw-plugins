# 2.8.2 验证报告

## 需求

「（使用说明）加入到网页里面，要不用户看不到。」

## 修改（仅面板）

- 侧边栏顶部新增「使用说明」入口与 `page-guide`：醒目前提说明 + 五步流程（可点击跳到 115账号 / 全量同步 / 运行日志）+ 常见问题表；新增 `.guide` 样式。
- 首次进入：启动时记录 `BOOT_HASH`（in-app 模式下 `go()` 会立刻改写 hash），配置加载后若无 hash 且未配置 `cloud_cid` / `mappings`，自动显示使用说明；已配置时仍落在「核心配置」。支持 `#guide` 直达。
- 「全量同步」页 warn 横幅加「查看完整使用说明 →」。`web/index.html` 重新生成。

## 验证

- 测试 209 项通过（新增 `test_usage_guide_page_in_console`：入口、页面、关键文案、跳转、首次显示逻辑）。
- 本地 HTTP 模拟宿主 `/panel` + `/config`，Playwright 实测：未配置 → 标题「使用说明」、页面可见；已配置（cloud_cid=123）→ 标题「核心配置」、使用说明隐藏；均无 JS 错误。截图核对排版。

### 打包

`p115-library-2.8.2-movieclaw-v0.35.0.zip` / `.mcplugin`；授权不变。

---

# 2.8.1 验证报告

## 需求

「写入使用说明，必须新建媒体库，指定目录才能在特定目录下创建 strm」

## 修改（仅文档与面板文案）

- README 顶部新增「快速上手（必读）：先建媒体库，再同步」：权限原理（`library` 读写授权只覆盖已配置媒体库的根目录及子目录，见 2.0.1 对 `plugin_files.roots()` 的核实）、五步流程（准备目录 → 新建媒体库 → 安装登录 → 配置目录 → 同步与手动扫描）、常见问题表（授权拒绝 / 系统权限 / 看不到内容 / Docker 路径）。
- 面板「全量同步」页顶部新增 warn 横幅；「本地STRM目录」与「多目录映射」说明改写并加粗关键要求；原 info 横幅更新为当前行为（全量后手动扫描、增量自动通知、进度实时保存）。`web/index.html` 重新生成。
- Playwright 渲染核对：横幅与说明显示完整、各行两列对齐（面板结构测试通过）。

## 测试（208 项，全部通过）

### 打包

`p115-library-2.8.1-movieclaw-v0.35.0.zip` / `.mcplugin`；授权不变。

---

# 2.8.0 验证报告

## 需求

「对于正在播放的电视剧，我们能否对后面剧集进行链接缓存。」

## 实现

- `prefetch.py`（新）：
  - `EpisodeIndex`：从各映射 manifest 建立 fid → (文件夹, 序号)；文件夹内按自然排序；`following(fid, n)` 返回后续 n 集，季末仅在文件夹名像季目录（season / s\d / 第…季 / specials）时接续下一个同类兄弟目录；单视频文件夹（电影）返回空。索引 10 分钟过期重建，或遇到未知 fid 时重建。
  - `Prefetcher.schedule(fid, ua, allow_cookie_url)`：按 (集, UA, 模式) 10 分钟去抖，后台任务调用 `Drive.prime_links(..., ua=播放器UA, allow_cookie_url=模式, min_remaining=1200, use_expiry=True)`，异常全部吞掉；成功写一条运行日志。
- `Drive`：
  - `link_expiry(url)`：解析直链 `t`（过期时间戳），扣 5 分钟余量、上限 4 小时，返回单调时钟截止时间；不足 60 s 返回 None。
  - `_link_valid()`：缓存项有 `expires` 时按其判断，否则沿用「直链缓存时间」。`link_signed` 改用它。
  - `prime_links` 增加 `ua` / `allow_cookie_url` / `min_remaining` / `use_expiry`：缓存键 (pickcode, ua, allow)；302 模式（allow=False）下 f=3 链接被 `_url_from_info` 拒绝、不入缓存；协议记忆按 UA 匹配。
- `p115_plugin`：新设置 `prefetch_episodes`（0~5，默认 2）进入 EDITABLE 与面板「STRM配置」；`/play` 成功后（302 用请求 UA、中转用空 UA + allow）调用 `prefetcher.schedule`，不阻塞响应。

## 测试（208 项，202→208）

- 自然排序；E02 → E09、E10；季末接下一季；最后一集为空；电影为空且不跳到其他电影目录。
- 预取用播放器 UA、一次批量、`use_expiry=True`；同集同 UA 去抖；中转模式独立缓存键；设 0 关闭；签发失败不抛异常。
- 真实 m115 批量签发：请求头 UA 为播放器 UA、单次请求；f=3 链接在 302 模式下不入缓存；一小时后（超过 10 分钟缓存时间）仍命中按 `t` 计算的有效期；剩余有效期充足时不重复签发。
- `link_expiry`：余量不足返回 None、正常解析、无 `t` 返回 None。

### 打包

`p115-library-2.8.0-movieclaw-v0.35.0.zip` / `.mcplugin`（新增 `prefetch.py`、`tests/test_prefetch.py`）；授权不变。

---

# 2.7.13 验证报告

## 需求

「如果是增量同步，需要通知扫描媒体库。」（全量同步沿用 2.7.12：不通知。）

## 修改

- `run()` 记录 `_run_mode`；`mark_scan_pending()` 仅在增量模式下写 `manifest:<scope>:scan-pending`（每映射每轮一次），触发点不变（STRM 写入/重写/删除、附属文件写入/删除）。
- `sync_mapping` 末尾：`mode=='incremental'` 且存在待扫描标记时调用 `library.scan.start`；非超时失败重试 1 次；成功清标记，失败保留标记、写 warn 日志（含宿主原因与目录），不抛异常、不影响后续映射。全量模式不读、不清该标记。

## 测试（202 项，201→202）

- 全量（含有变化时）不通知；增量写入新文件 → 通知一次且路径为映射目录；增量无变化 → 不通知。
- 增量通知失败：同步 `completed`、`scan_pending`、重试 1 次、日志含宿主原因；恢复后下一轮增量（无变化）补发并清标记。
- `test_end_to_end`（默认增量）与 `test_life` 两处恢复为断言发出扫描请求；`test_process` 同步恢复（需真实 SDK）。

### 打包

`p115-library-2.7.13-movieclaw-v0.35.0.zip` / `.mcplugin`；授权不变。

---

# 2.7.12 验证报告

## 需求

「不用通知扫描了，仅告诉用户同步完成。」

## 修改

- `sync_mapping` 末尾删除 `library.scan.start` 调用及其重试/日志；改为尽力删除旧版遗留的 `manifest:<scope>:scan-pending` 键。
- `mark_scan_pending()` 改为空操作（不再写存储）；`rewrite_origins` 不再写 scan-pending；`run()` 开始时 `scan_pending=False`。
- 同步完成的提示沿用既有运行日志「全量/增量同步完成 · 耗时 … · 写入 … · 未变化 …」与同步历史。
- 清单 `permissions.operations` 保持 `library.scan.start`、`library.list` 不变（避免升级触发重新批准）；`AutoOps` 保留但同步流程不再调用扫描。

## 测试（201 项）

- 新增：同步全程不调用 `library.scan.start`（宿主扫描接口即使报错也无影响），`scan_pending=False`，旧遗留标记被清理；第二轮同样不调用。
- 删除 3 个基于扫描通知的用例（失败重试 / 多映射不中断 / 扫描 RPC 挂起有上限）；`test_end_to_end`、`test_life` 两处与 `test_process`（需真实 SDK）改为断言不发扫描请求。

### 打包

`p115-library-2.7.12-movieclaw-v0.35.0.zip` / `.mcplugin`；授权不变。

---

# 2.7.11 验证报告

## 反馈

「STRM 已保存，但媒体库扫描未启动；请稍后重试同步或手动扫描。这是已经完成的意思么」

## 分析

该提示来自 `sync_mapping` 末尾：manifest 已保存、meta 已置 clean 之后调用 `library.scan.start`。非超时异常时抛 `DriveError` → 整轮记为失败、`run()` 的映射循环中断（后续映射未执行），宿主原始错误被 `from None` 丢弃。

## 修复

- `library.scan.start` 非超时失败重试 1 次（间隔 `scan_retry_delay`=5 s）；最终失败或超时：保留 `scan_pending`、运行日志 warn 写明宿主原因与目录，**不抛异常**，继续后续映射，本轮 `completed`。成功后清除待扫描标记（与之前一致）。

## 测试（203 项，202→203）

- 扫描通知持续失败：本轮 `completed`、STRM 已写、`scan_pending` 保留、共尝试 2 次、日志含宿主原因「busy」；恢复后下一轮清除标记。
- 两个映射、第一个通知失败：第二个映射照常生成 STRM。
- 既有「扫描 RPC 挂起有上限且不算失败」用例按新日志措辞更新。

### 打包

`p115-library-2.7.11-movieclaw-v0.35.0.zip` / `.mcplugin`；授权不变。

---

# 2.7.10 验证报告

## 需求

「实时进度保存，避免插件意外退出丢失进度」

## 修改

- 进度节奏：`PROGRESS_SAVE_FILES` 2000 → 300，`PROGRESS_SAVE_SECONDS` 60 → 3。
- `append_manifest` 改为「尾节点」：增量记录写入当前尾节点并整体改写，满 60 KB / 400 条才新建尾节点并更新根列表（先写节点、后写根，任何时刻崩溃都可加载）；完整保存时关闭尾节点。meta（`clean/partial/url_sig`）每轮只写一次。
- 播放索引不再随每次进度保存落库，改为每 30 s（`PLAY_FLUSH_SECONDS`）或积累 3000 条时落库；停止/失败/完成时照旧落库。
- 中断恢复：本轮开始时若 meta 为 `partial`，`rebuild_play_records()` 由上次保存的 manifest 重新推导全部播放记录（fid = sha256(scope:id)[:24]，pickcode = fingerprint[2]）。
- 播放路由兜底：`/play/<fid>` 查不到时调用 `engine.play_fallback(fid)`，从各映射已保存的 manifest 建立 fid → 记录映射（10 分钟内最多重建一次），找到即写回播放索引。

## 测试（202 项，200→202）

- 硬退出模拟：同步进行中直接对存储做快照（等同 SIGKILL 后留下的数据），新进程基于快照：已保存进度 ≥ 40/50；未落库的播放记录可经兜底找回；继续同步只为未保存的文件签名，结束后 80 条播放记录齐全。
- 50 次连续保存只产生 1 个尾节点、根列表只写 1 次，读回 50 条。

### 打包

`p115-library-2.7.10-movieclaw-v0.35.0.zip` / `.mcplugin`，结构同 2.7.9；授权不变。

---

# 2.7.9 验证报告

## 反馈

「是否保留文件后缀」的说明文字 UI 歪了。

## 根因与修复

该行是旧结构 `<div class="row"><label/><div class="ctrl"/></div>`（缺内容包裹 div）；2.7.7 在 `.ctrl` 后追加 `.note`，成为 `.row` 网格（190px + 1fr 两列）的第 3 个子元素，换行落到标签列。改为 `<label/><div><div class="ctrl"/><div class="note"/></div>`，`web/index.html` 重新生成。

## 测试（200 项）

- 新增 `test_every_settings_row_has_exactly_label_and_one_content_column`：解析面板 HTML，每个 `.row` 必须恰好两个直接子元素；对 2.7.8 面板运行时失败并指出该行，对本版通过。
- Playwright 渲染「STRM配置」页截图核对：说明位于按钮下方、与其他行对齐。

### 打包

`p115-library-2.7.9-movieclaw-v0.35.0.zip` / `.mcplugin`，仅面板改动；授权不变。

---

# 2.7.8 验证报告

## 现场

「[同步] 同步异常堆栈（末 600 字）：… httpx/_transports/default.py … handle_async_request …」「全量同步失败（耗时 0s，扫描 0）：STRM 生成失败（ReadTimeout）：无详细信息」

## 根因

`SyncEngine.fetch_cloud_sidecar` 直接用 httpx 拉 CDN（经 `net.http_transport` 包装，即堆栈里的 `self._inner`），未捕获网络异常；`sync_side` 只吞 `DriveError`。2.7.5 起附属下载与扫描并行，`httpx.ReadTimeout` 进入 `failure` 使整轮失败。`Drive.request` 路径（115 API）早已把网络异常转为可重试的 `ThrottleError`，不涉及。另：失败时 `record_history(mode, {}, …)` 传空状态，故显示 0s/0；`ReadTimeout` 的 `str()` 为空，故「无详细信息」。

## 修复

- `fetch_cloud_sidecar`：引擎级复用的 `AsyncClient`（connect 15 / read 60 / write 15 / pool 60 s），最多 3 次尝试（间隔 `SIDECAR_RETRY_DELAYS`=1 s、3 s）；401/403/410 → `invalidate_links` + `link_signed(fresh=True)` 重签；408/429/5xx 与网络异常重试；最终统一抛 `DriveError`。
- `sync_side`：下载阶段任何非取消异常 → 计数跳过；结束时运行日志汇总一条 warn。
- `life_strm`：`describe_failure()`（空消息的网络异常给中文原因 + 类型 + 插件内位置）、`plugin_frames()`（只列本插件目录内的最内层帧）；失败的同步历史使用 `engine.status`（真实耗时/扫描数）。

## 测试（199 项，196→199）

- CDN 每次都 `ReadTimeout`：同步 `completed`、STRM 写入、附属跳过、运行日志有汇总；
- 首次超时、第二次成功：附属文件正常落盘；
- `describe_failure` 对空消息 `ReadTimeout` 给出「网络读取超时」且不含「无详细信息」。
- 既有「下载失败软跳过」用例保留（500 重试后跳过）。

### 打包

`p115-library-2.7.8-movieclaw-v0.35.0.zip` / `.mcplugin`，结构同 2.7.7；授权不变。

---

# 2.7.7 验证报告

## 反馈与决定

「STRM 文件名直接 copy 即可，不用保证单一性」→ 讨论后确定：文件名本来就是照搬；同名视频保留确定性的「保留较大」（不做会导致每轮来回改写同一个 STRM 并残留播放记录，且该处理无额外 IO），但移除「中止同步」选项并取消逐条冲突日志。

## 修改

- `Settings.strm_conflict_policy`：仍接受 `auto`/`abort`（兼容旧设置与配置备份），一律归一为 `auto`；面板移除该选项行，并从 STRM 页保存/回显字段中去掉。
- `SyncEngine`：删除 `abort` 分支与 `conflict_abort`；删除逐条冲突 `log_event`，保留 `conflicts` 计数与同步历史前 10 条明细。
- 面板「STRM 保留文件后缀」补充说明（如何保留两个版本）；「本地存在时」说明更新为「跳过该文件并计数」（与 2.7.0 起的行为一致）。

## 测试（196 项）

- `test_conflict_abort_mode_names_both_files` 改为：旧 `abort` 设置归一为 `auto`、同步不中止、保留较大文件、运行日志无「同名 STRM 冲突」逐条记录、同步历史仍有明细。
- `test_process.py` 面板静态断言改为「不再提供该选项」（该用例需真实 SDK，未在此环境运行）。

### 打包

`p115-library-2.7.7-movieclaw-v0.35.0.zip` / `.mcplugin`，结构同 2.7.6；授权不变。

---

# 2.7.6 验证报告

## 反馈

「1000000 单条限制这么多，如果我的文件很多那岂不是会报错？」

## 修复

- `Settings.max_entries` 默认 0（不限制），范围 0~100000000；`Drive.scan` / `Drive.walk` 仅在非 0 时检查。启动迁移：持久化值为 100000 或 1000000（两个历届默认）时改为 0。面板输入框 min=0 并说明「0 = 不限」。
- `Drive.scan` 去重集合只记录 `keep()` 保留的条目（不再为每个列出的文件保存 ID）。
- 可信路径直接复用上一轮的记录对象（需修复时才复制）；附属记录内容未变时同样复用，稳定库同步时内存不再翻倍（实测每条记录约 1 KB）。

## 测试（196 项，195→196）

- 默认值为 0、两个旧默认值迁移代码存在、面板可编辑；
- `max_entries=0` 时扫描任意规模照常产出；
- 既有超限用例（显式设置上限时第一页即 `LimitError`、不回退逐目录扫描）不变。

### 打包

`p115-library-2.7.6-movieclaw-v0.35.0.zip` / `.mcplugin`，结构同 2.7.5；授权不变。

---

# 2.7.5 验证报告

## 反馈

「img 和 nfo 是否能一起同步到文件夹内」

## 现状与根因

附属同步默认开启，但匹配规则只有「与视频同 stem」+ 6 个固定文件名，且限定视频所在目录；常见刮削命名（`<名>-poster.jpg`、`-thumb.jpg`、`landscape.jpg`、`clearlogo.png`、`.webp`）、剧集/季级元数据（`tvshow.nfo`、`season01-poster.jpg`、`season.nfo`）与 `extrafanart/` 全部漏掉；且附属文件在整轮列表结束后才统一下载，停止即全部丢失。

## 修复

- 规则：附属后缀（字幕 + `.sup`、`.nfo`、`.jpg/.jpeg/.png/.webp/.tbn`）且所在目录为「含视频目录或其祖先目录」，或为这些目录下的 `extrafanart` / `extrathumbs`。按视频到达顺序维护 `qualified` 目录集合；未达标的附属文件挂起，所在目录一达标即放行；结束时仍未达标的丢弃。
- 独立记录：manifest 键 `§<相对路径>` → `{kind: side, path, hash, cloud}`；旧版挂在视频记录上的 `sidecars`/`cloud` 作为既有所有权依据读取（迁移期视频记录保留这些字段，清理时若已有独立记录则不按旧字段删除）。
- 流式下载：独立 `side_worker` 与写入并行，每批最多 100 个先 `prime_links` 批量签发，再 8 路并发下载；结果计入进度检查点（2.7.4）。
- 判定：可信路径且网盘 SHA1 未变且本地存在 → 不读不下；本地存在但哈希与记录不符 → 视为用户文件，永不覆盖（计数并写日志）；否则下载，内容不同才写入（新文件直接写，覆盖走原子替换）。
- 清理：开启清理且附属同步开启时，网盘已不存在的 `§` 记录对应文件（哈希一致）删除；否则保留记录。`rewrite_origins` 跳过 `§` 记录。

## 测试（195 项，193→195）

- Emby/Kodi 结构：电影目录（`-poster.jpg`、`landscape.jpg`、`clearlogo.png`、`extrafanart/`、`.zh.ass`、`.nfo`）与剧集/季目录（`tvshow.nfo`、`poster.jpg`、`season01-poster.jpg`、`season.nfo`、`S01E01-thumb.jpg`）全部同步，相册/资料目录不同步；附属文件先于视频到达同样处理；第二轮零下载。
- 附属文件在列表进行中即已落盘；停止后 manifest 含 `§` 记录。
- 既有附属文件用例（同名字幕/海报下载、超限跳过、用户文件不动、下载失败软跳过、清理只删插件文件）全部通过。

### 打包

`p115-library-2.7.5-movieclaw-v0.35.0.zip` / `.mcplugin`，结构同 2.7.4；授权不变。

---

# 2.7.4 验证报告

## 反馈

「中途发生了暂停，整个进度没保存。我要是重新同步要重新来」

## 根因

`sync_mapping` 只在整轮成功时 `save_manifest`；停止/失败/插件关闭时已写出的 STRM 没有记录，且 `meta.clean=False`，下一轮对每个文件走完整校验路径（签名 + 读回 + 比对）。

## 修复

- manifest 改为**有序**加载（前序遍历、子节点按列表顺序，后出现的记录覆盖先前的）；新增 `append_manifest(key, delta)`：只把增量记录写成新节点追加到根列表末尾；根列表超过 400 个节点时合并为一次完整保存。完整保存每片上限 100 → 400 条（仍受 60 KB 限制）。
- `save_progress()`：先落库播放索引，再追加 `dirty_rels`（本轮 core 字段有变化的记录，保留上次的附属文件记录），并把 meta 置为 `{clean: True, url_sig, partial: True}`。触发：每 2000 个文件 / 60 秒；`CancelledError` 与写入/列表失败时（`keep_progress`，60 s 上限）再保存一次并写运行日志。
- 认领（adopt）：meta 的 `url_sig` 与当前一致（即使 clean=False）且无既有记录时，盘上内容为「本文件的 `/play/<fid>?` 单行链接、直连格式一致」的 STRM 直接按内容建立记录，不签名。
- `LifeSTRM.stop()` 等待上限 15 s → 60 s。

## 测试（193 项，190→193）

- 停止于 100 个中的第 60 个：meta `clean=True, partial`，manifest 60 条；继续同步 60 个未变化、只签名并写入 40 个，最终 100 条；
- 运行中按批追加（PROGRESS_SAVE_FILES=25），追加节点按序覆盖旧记录；
- 旧版停止且无记录、meta `clean=False` 同 url_sig：20 个文件全部按内容认领，仅 1 次探针签名；下一轮走快速路径。
- `test_chunked_manifest` 按新的每片 400 条调整。

### 打包

`p115-library-2.7.4-movieclaw-v0.35.0.zip` / `.mcplugin`，结构同 2.7.3；授权不变。

---

# 2.7.3 验证报告

## 反馈

①「写入速度还是过慢」②「插件更新后自己启动全量同步」③「暂停的时候又暂停失败」。

## 根因

- ②：`LifeSTRM.tick` 在同步被取消（停止按钮、插件升级/关闭时进程结束）和失败时都写 `strm-upload-dirty=True`；插件下次启动第一轮 `tick` 读到它就开跑。另外 `due=time>=next_run_after(synced_at)`：上次同步较早时 cron 早已过点，启动即补跑。
- ③：`/strm/stop` 只取消 `life.run_task`（面板按钮发起的同步）；后台循环（cron / 115 事件 / dirty 标记）发起的同步运行在 `life.task` 里，停止接口找不到它，却仍回报「已停止」；即使停掉，取消路径写的 dirty 标记又让循环在 ≤60 s 内重跑。
- ①：每个新 STRM 约 6.6 次宿主调用（签名、`owned:` 写入意图读/写、`play:` 写、每目录 `listdir` + `makedirs`、临时文件 open + rename）；宿主串行处理时增加写入并发无效。

## 修复

- `LifeSTRM`：`boot()`/`restart()` 置 `baseline_pending`；之后第一轮非强制 `tick` 只写事件指纹基线、删除残留 dirty、`next_run_at` 从启动时刻起算后返回。cron `due` 基于 `max(synced_at, started_at)`。取消与失败不再写 dirty。
- 同步在 `self.sync_task` 子任务中运行；`LifeSTRM.stop()` 置 `stop_requested` + `engine.cancelled`（可打断限速冷却）并取消子任务，等待最多 15 s 真正结束；`tick` 将用户停止识别为正常结束（状态 `cancelled`、写运行日志、不记失败）。插件关闭时的取消仍向上传播。`/strm/stop` 返回真实结果；面板按 `ok` 显示对应提示。
- `PlayIndex`：`play` 记录先入内存、按 `fid` 分 1024 片批量写入 `playx:nnn`（`playx-index` 登记已存在分片，新分片不读直接写；登记先于分片写入），播放路由依次查内存 → 分片 → 旧 `play:<fid>`；同步结束保存 manifest 前、停止/失败时都会落库。
- 删除 `owned:` 写入意图：`strm_is_ours()` 依据文件内容判断（记录哈希 / 插件签发的 `/plugins/p115-library/play/<24hex>` 链接 / 待写内容的前缀 / 空文件）。用户自填内容仍判定为非插件文件、跳过不覆盖。
- 本地存在性：目录内容先由上级目录列表推断（不存在的目录不 `listdir`，已存在的目录不 `makedirs`），并发查询共享；新目录 `makedirs` 与签名并发；新文件直接写入（`atomic_write(direct=True)`），覆盖已有文件仍为 temp + rename。

## 测试（190 项，185→190）

- 重启/升级后即使 cron 已错过一天且残留 dirty 标记，也不自动同步，`next_run_at` 在未来；
- 后台循环发起的同步可被 `stop()` 停下，状态 `cancelled`，不留 dirty，无同步时返回 None；
- PlayIndex 内存 → 分片 → 旧键查找与删除；首次同步不写任何 `play:`/`owned:` 逐文件键、每分片每轮最多写一次；停止后已写出文件的播放记录已落库；
- 流式同名冲突两种到达顺序都保留较大者（同时覆盖了「本轮刚写的较小文件被识别为插件文件」这一回归）。
- 既有用例：播放记录断言改为读分片；写入失败注入改到新文件的直接写入；中断恢复用例改为覆盖已有文件时 rename 失败。

## 基准（6000 电影 + 100 剧集，32100 文件 / 8000 视频，真实 1.5 s 间隔，宿主每次调用 5 ms，无附属）

| | 2.7.2 | 2.7.3 |
|---|---|---|
| 首次同步宿主调用 | 53359 | 27338 |
| 首次同步耗时（宿主串行处理） | 291 s | 154 s |
| 首次同步耗时（宿主并行处理） | 48 s | 49 s（均受 115 列表时间限制） |

### 打包

`p115-library-2.7.3-movieclaw-v0.35.0.zip` / `.mcplugin`，结构同 2.7.2；授权不变。

---

# 2.7.2 验证报告

## 反馈

「写入速度过慢，可以同步进行写入么？」

## 根因

2.7.0/2.7.1 的 `Drive.scan` 先把整个 `cur=0` 列表翻完、再按目录分组产出——大库列表期间（受 API 间隔节流，数分钟）写入者全部空闲，写入时间与列表时间串行相加。

## 修复

- `Drive.scan` 改为流式（`__mode__: stream`）：第一页后立即取目录树（`_load_tree`），之后每页文件就地解析路径（树 → 缓存的面包屑兜底）并立刻产出；不再产出 `__end_dir__`；结尾 `__listing_done__`。
- 引擎：写入队列按输出路径 `crc32 % 16` 分片，每个写入者独占一个分片；流式模式下同名冲突按输出路径边到边裁定（`conflict_rank` 全序，较大者后到时入同一分片覆盖），旧的按目录裁定路径保留给 legacy walk 与按目录产出的扫描。
- 慢路径：`sign` 与 `exists` 并发；本地无文件时跳过 `owned:` 读取；`play:` 删除/写入与 `owned:` 写入并发，仍在 STRM 写入之前完成。

## 测试（185 项，183→185）

- 第一页文件在下一页请求之前产出、无 `__end_dir__`；
- 写入在列表结束前已开始（列表中途已落盘 ≥5 个 STRM）；
- 流式同名冲突：两种到达顺序最终都保留较大文件，只剩胜者的 `play:` 记录。

## 基准（模拟：6000 电影 + 100 剧集，32100 文件 / 6301 目录 / 8000 视频；真实 1.5 s 请求间隔、60 ms 网络、5 ms 宿主往返；不含附属文件）

| | 2.7.1 | 2.7.2 |
|---|---|---|
| 首次同步总耗时 | 89.4 s | 48.1 s |
| 115 请求 | 31 | 31 |
| 宿主往返 | 61404 | 53022 |

### 打包

`p115-library-2.7.2-movieclaw-v0.35.0.zip` / `.mcplugin`，结构同 2.7.1；授权不变。

---

# 2.7.1 验证报告

## 现场反馈

运行日志：「[同步] 快速扫描不可用（达到最大条目限制，请缩小同步目录），本次改用逐目录扫描」。

## 根因

1. `cur=0` 整树列表在真实 115 上已正常返回（能翻到 10 万条以上）；报错来自 `max_entries`（旧默认 100000）——该字段在 `EDITABLE` 中、随每次保存持久化，但面板没有输入框，用户无法调整。
2. 超限抛的是普通 `DriveError`，被引擎当成「快速扫描不可用」退回逐目录扫描；逐目录扫描同样计数，会在数小时后撞同一上限。
3. 极速扫描把所有文件（含图片/杂项）都留在内存。

## 修复

- `LimitError(DriveError)`：两种扫描的超限都抛它；引擎遇到它不回退、直接失败并给出原因。
- `Drive.scan`：第一页即用 `count` 判断超限（报出实际数量与建议值）；新增 `keep(name)` 过滤，引擎只保留视频后缀与（开启附属同步时的）附属后缀；计数漂移时不再重来，按 ID 去重继续并发 `__inexact__`，引擎本轮跳过删除清理。
- `Settings.max_entries` 默认 1000000（上限 5000000）；`apply()` 启动时把持久化的旧默认 100000 迁移为 1000000；面板「全量同步」页新增输入框（`web/index.html` 重新生成）。

## 测试（183 项，179→183）

- 第一页超限即 `LimitError`、仅 1 次请求；引擎遇 `LimitError` 不调用 `walk`；`keep` 过滤后只剩视频、进度按实际列出数；计数漂移去重继续且发 `__inexact__`；默认值/面板字段/迁移代码存在。

### 打包

`p115-library-2.7.1-movieclaw-v0.35.0.zip` / `.mcplugin`，结构同 2.7.0；授权不变。

---

# 2.7.0 验证报告

目标：MovieClaw v0.35.0（兼容基线）+ 支持插件网页的新宿主（≥ v0.42.0）。

## 变更：同步速度重做

### 瓶颈定位（2.6.3）

1. **扫描**：`Drive.walk` 逐目录 `GET /files?cur=1`，每次请求都在全局锁里等 `request_interval`（默认 1.5 s）。请求数 ≥ 目录数；1500 部电影的库 ≈ 1500+ 次 ≈ 40 分钟，且每次事件/cron 增量都要再走一遍。
2. **宿主 IPC**：每个文件（含未变化的）都要 签名 + 读写入意图 + 存在性 + 读回 + 写播放记录 + 写写入意图；写入的文件再加 scan-pending + makedirs + open + fsync + rename + exists。
3. **`AutoEngine.report` 未节流**：覆盖基类方法、每次计数都 `store.set` 整份状态（实际生产路径用的就是 AutoEngine）。
4. **附属文件每轮读回**：所有已存在的海报/NFO/字幕每轮经宿主完整读取再算哈希。
5. **收尾丢写**：生产者结束后写入者只有 10 s 宽限，之后被取消、错误被吞，仍报「成功」。

### 修复

- `Drive.list(..., recursive=True)`：`cur=0&show_dir=0&limit=1150`（p115client `iter_files` 契约），每行带父目录 `cid`。
- `Drive.scan()`：整树文件分页列出 → `get_info` 取根目录 pickcode → `GET /files/downfolders`（p115client `download_folders`，每页 5000）取全部目录名；未覆盖的目录用一次面包屑（`/files?cur=1&limit=1` 的 `path`）兜底；根目录（cid=0）或拿不到 pickcode 时按一级子目录逐个取目录树。计数在分页间变化时整体重来（最多 3 次）。事件协议与 `walk()` 相同（另加 `__mode__`/`__progress__`）。
- `SyncEngine`：
  - **可信快速路径**：`manifest:<scope>:meta` 记录 `{clean, url_sig}`；`url_sig` = 探针路径签名 + 直连格式 + 存在策略。上次运行干净结束且 `url_sig` 不变时，身份与路径未变的文件沿用上次记录（不签名、不读写 `owned:`/`play:`）。运行开始即把 `clean` 置 False，中途崩溃的下一轮自动走完整校验。
  - 本地存在性：每目录一次 `listdir`（宿主无此接口时退回 `exists`）。
  - 被手改/外来的 STRM：跳过 + 计数 + 日志，不再中止整轮；空文件视为可修复。
  - 写入者等待队列排空后才结束（修复收尾丢写）；并发 8。
  - `atomic_write` 去掉 fsync 与成功路径的 exists；scan-pending 每映射一次；manifest 未变化不重写；manifest 保存复用加载时的节点列表。
  - 进度日志改为 20 s 一条。
- `AutoEngine` 只改 `STATUS_KEY`，继承基类节流。
- 新配置 `scan_strategy`（fast / legacy），面板「数据同步 → 全量同步」可切换。

## 测试（177 项，166→177；`test_process.py` 需真实 SDK 子进程，未在此环境运行）

- `tests/test_scan.py`（新增 11 项）：整树列表 + 目录树拼路径且无逐目录请求；目录树接口 404 时面包屑兜底；非法目录名跳过并给原因；分页期间计数变化自动重来；未变化库第二轮仅 1 次签名、0 次逐文件记录写入；变化文件照常重写；上轮未干净结束时禁用信任并修复空文件；地址变化全部重写；本地被删文件重建；legacy 策略走 walk；慢宿主下 300 个文件全部落盘。
- 既有用例中 4 处「手改文件 → 整轮失败」的断言改为「跳过并计数、文件不动」，其余语义不变。

### 打包

`p115-library-2.7.0-movieclaw-v0.35.0.zip` / `.mcplugin`（同一文件）。打包前由 `panel.PANEL` 重新生成 `web/index.html`；包内文件位于根目录、按名排序，与 2.6.3 包结构逐项一致（仅新增 `tests/test_scan.py`）；打包后校验：路径安全、无软链、无缓存文件、清单在根且 `entry` 模块存在、版本一致、`web/` 三件套齐全且 `index.html == PANEL`、全部 `.py` 可编译。授权（`library` 读写 + `library.scan.start`/`library.list` + 网络）不变，覆盖升级无需重新批准。

---

# 2.6.3 验证报告

目标：MovieClaw v0.35.0（兼容基线）+ 支持插件网页的新宿主（≥ v0.42.0）。

## 变更：断点实时保存 + 修复启动自动同步（回应用户两点反馈）

### ① 断点「点停止才保存」→ 实时增量落库

排查：断点落库节流 2s + 每次 `save_checkpoint` 全量重写（分 chunk + 删旧 node）。同步一快（<2s）中间那些目录完成的 mark_done 全被节流跳过，只剩结尾的 force 落库——所以看起来「点停止才保存」。

修复：
- 新增 `SyncEngine.append_checkpoint(key, done, flushed_done)`：只把「上次落库后新增的 cid」追加成一个新 node（`{'dirs': {...}}`）+ 更新根记录，**不重写全量**；node 数 >32 时回退到 `save_checkpoint` 全量合并。
- `flush_checkpoint` 去掉节流：每次目录完成（mark_done）都增量追加（一次 node + 一次 root，约 2 次 RPC），断点随扫描实时可见。
- `flushed_done` 快照记录已落库 cid，`save_manifest` 后 / 协调结束仍走全量 `save_checkpoint`（收尾合并）。

### ② 插件启动后没点开始就自动跑同步

根因（`life_strm.tick` 两处「首次」漏洞）：
1. `changed = fingerprint is not None and fingerprint != previous.get('fingerprint')`——首次 `previous` 无指纹基线，`previous.get('fingerprint')` 为 None，事件指纹「无→有」被判成变化；
2. `due = time.time() >= next_run_after(previous.get('synced_at', 0))`——首次 `synced_at=0`，`next_run_after(0)`（epoch）算出过去时间，`due=True`。

两者叠加：插件一加载，`tick(force=False)` 就触发 `engine.run`。

修复：`changed` 需 `prev_fp is not None`；`due` 需 `prev_synced>0`。首次（无基线）只 `store.set` 记录事件指纹基线、不触发同步。

## 测试（167 项，165→167）

- `test_checkpoint_appends_then_compacts`：追加 40 个目录，读回无损、节点数被合并回 <10。
- `test_first_tick_without_force_does_not_sync`：首次 `tick(force=False)` 不产 STRM、只记录指纹基线。
- 既有 `test_cron_not_due / test_cron_due` 等 cron 语义用例继续通过（修复不破坏「到点触发」）。

---

# 2.6.2 验证报告

目标：MovieClaw v0.35.0（兼容基线）+ 支持插件网页的新宿主（≥ v0.42.0）。

## 变更：断点记录分片存储（修复 64 KB 上限）

用户现场报「STRM 生成失败（ValueError）：值超过 64 KB，请存文件、这里存引用」。

根因（源码核实）：宿主的插件数据服务 `plugin_data.py` 对单个 value 有 **64 KB 上限**（`MAX_VALUE_BYTES`，超限抛 `ValueError("值超过 64 KB，请存文件、这里存引用")`）。2.6.0/2.6.1 的断点记录把「已完成目录集合」整体 `store.set(ckpt_key, {'done': {...}})` 存成一个值——一个 cid 约 25 字节，**目录超过约 2600 个就撑爆**，`mark_done` 节流落库时抛错，同步中断。manifest（同步记录）早有分 chunk 机制（`save_manifest` 每片 60 KB），checkpoint 漏了。

修复：
- 新增 `SyncEngine.load_checkpoint / save_checkpoint`：`done` 集合按 cid 排序分片，每片存一个 node `{'dirs': {...}}`（<60 KB），根 key 存 `{'nodes': [...], 'count': N, 'at': ts}`，落库时删除孤儿 node。
- 兼容旧格式：`load_checkpoint` 仍能读 2.6.0/2.6.1 的单值 `{'done': {...}}`（没超限的那些），不丢。
- `/strm/status` 的 `checkpoint_dirs` 直接读根记录的 `count`（不逐片读）；`/strm/clear-checkpoint` 删除根 + 所有 node。

## 测试（165 项，164→165）

- `test_checkpoint_chunking_stays_under_64kb`：8000 个目录的 done 分片后每个 store value 均 <64 KB，读写无损，覆盖写只留最新。
- 三个既有断点续传用例改用 `load_checkpoint` 读断点（适配分片格式），全绿。

---

# 2.6.1 验证报告

目标：MovieClaw v0.35.0（兼容基线）+ 支持插件网页的新宿主（≥ v0.42.0）。

## 变更：断点可见性 + 失败/停止时保留断点（回应用户「还是不跳过」）

排查结论：断点续传逻辑本身正确（见 2.6.0），「不跳过」最可能来自——①第一次全量**没跑完**（断点只积累到已完成目录，未完成的自然重扫）；②点了「增量同步」（增量设计为全枚举捕捉变化，不跳过）；③还在用 2.5.x 星标版（星标 API 与 115 契约不符，本就不会生效）。为让用户能一眼判断是哪一种，本轮补两处：

1. **面板显示「断点记录」**：`/strm/status` 返回 `checkpoint_dirs`（所有映射已记录的目录总数），面板「数据同步」状态区显示「断点记录：已记录 N 个目录，下次全量将跳过」或「暂无——第一次全量完整跑完后才会积累」。为 0 = 没积累，>0 = 已积累（若仍不跳过再排查）。
2. **失败/停止时立即保留断点**：`sync_mapping` 协调结束后、重抛异常**之前**，`best_effort` 落一次 checkpoint——之前只靠 `mark_done` 的 2s 节流落库，失败/停止会丢最近几秒的目录进度。

## 测试（164 项，163→164）

- `test_resume_after_failure_skips_completed_dirs`：中途失败（乙目录枚举报错）后，checkpoint 只记已完成目录（甲），下次全量只枚举未完成目录（乙）并重建其 STRM。
- 其余 163 项全绿；面板新增「断点记录」行纳入 `test_panel` 静态校验。

---

# 2.6.0 验证报告

目标：MovieClaw v0.35.0（兼容基线）+ 支持插件网页的新宿主（≥ v0.42.0）。

## 背景与定位

用户昨晚在另一次会话里迭代了 7 个产物（`2.4.1-optimized` → `2.4.2-turbo` → `2.5.0-resume` → `2.5.0/2.5.1/2.5.2/2.5.3`），主线是「大库提速 + 断点续传」。审查发现其中两处致命问题，本版（2.6.0）是对这套意图的**正确重做**：

1. **4 个写入 worker 只有 1 个结束信号**：`produce()` 只放 1 个 sentinel、`consume()` 起了 4 个 → 3 个永不正常退出，被强杀时 `except BaseException` 把 `CancelledError` 灌进 `failure`，导致**成功同步也被判失败**，且每次固定多等 10 秒。
2. **星标 API 与 115 契约不符**：2.5.x 用 `webapi.115.com/files/star` + `category/get` + `label_id=4` 记录断点。对照 p115client 0.0.9.7.2：打星是 `fs_star_set(payload={file_id,star})` → 封装 `fs_update_open`（开放平台接口）；`fs_category_get` 是「显示文件/目录统计信息」而非「列星标」；115 **没有**「列出所有已星标目录」的查询接口。用星标存断点方向本身不成立。

本版断点续传改为**本地 manifest 记录已扫描目录**，与 115 账号状态完全解耦。

## 变更

### 1. 多并发写入（正确实现）
- `WRITE_QUEUE 64→256`、新增 `WRITERS=4` 个并发 consumer。
- `produce()` 结束改为 `producer_done.set()` + 单 sentinel；`consume()` 用 `wait_for(pending.get(), timeout=0.5)` 空队列轮询 `producer_done` 退出——**不再强杀 consumer**，`except asyncio.CancelledError: raise` 不灌 `failure`（2.5.3 的「成功判失败 + 固定 +10s」根因）。
- `apply()` 并发安全：冲突裁定已前移到 `produce()`（单线程），且 apply 内部每个共享状态读改写都在首个 await 之前完成（asyncio 协作调度不会交错），计数、`current`、`deferred_sidecars` 均无竞态。

### 2. 断点续传（本地 checkpoint）
- `drive_api.walk` 增加：文件附 `parent_cid`；每个目录枚举完 yield `{'__end_dir__': cid, parent_cid}` 哨兵；`skip_dirs` 参数跳过已完成子树。
- `sync_engine`：目录级 `dir_pending` 计数（直接文件 + 子目录占位），consumer 每写完一个文件递减、归零时 `mark_done` 沿 parent 迭代上传播（子目录完成 → 父目录计数 -1 → 链式完成）；`checkpoint:<scope_key>` = `{'done': {cid...}, 'at'}`，扫描中节流落库（每 2s），同步结束强制落最终。
- 全量同步（`mode=='full'`）启用 `skip_dirs` 续传；增量同步全枚举（捕捉变化）并更新 checkpoint。
- 同名冲突在 `dir_end` 时**两遍消解**（先纯判定、abort 时零入队，再只入队胜者）——并发写不再有两个候选争同一个 STRM 路径。

### 3. 停止 / 清断点 / 进度日志 / 启动行为
- 新增 `/strm/stop`（取消当前同步，保留 STRM 与断点）、`/strm/clear-checkpoint`（清断点，下次全量从头）。
- 面板：全量/增量页加「⏸ 停止同步」按钮（`syncRunning` 时显示）、「🗑️ 清理断点」；全量页说明断点续传与清理用法；进度日志每 10 文件一条（`self._last_logged` 去重）。
- `life_strm.boot` 保留（事件监控 + cron 兜底照常），但 `loop` 的 first 分支 `tick(force=True)` → `force=False`：**启动不再强制全量**，避免大库一加载就卡；这与 2.5.3 把 `boot()` 整个改成 `pass`（连事件监控一起废掉）不同。

## 测试（163 项，160→163）

- `test_walk_skip_dirs_skips_whole_subtree`：skip 目录不产文件也不发 dir_end。
- `test_walk_nested_and_inconsistent_pages`（改）：校验 `parent_cid` 与 dir_end 哨兵的 parent 链。
- `test_full_resume_skips_completed_dirs`：首次全量写 checkpoint（整棵树），二次全量把已完成目录作为 skip_dirs 传入且零枚举零写入，增量仍全枚举。
- `test_streaming_writes_tree_as_it_scans`（改）：目录粒度——已完成目录随扫随写、失败目录不落盘、断点只记已完成目录。
- `test_conflict_auto_keeps_larger_file`（改）：只写胜者一次（written==1 而非 2），淘汰者 `play:` 记录不残留。
- `test_conflict_abort_mode_names_both_files`（改）：abort 时该目录零半成品（两遍消解）。
- `test_scan_and_write_overlap_instead_of_serializing`（改）：多目录并行判据，阈值放宽到 ×0.85。
- `test_panel_exposes_stop_and_clear_checkpoint`：停止/清断点按钮与接口路径。
- 既有 160 项全部保留通过（风控退避、签发降级链、权益保护、防误删、E2E 子进程全链路等）。

### 风险边界

- 断点续传是**目录级**：跳过「上次确认完成」的目录后，这些目录里「上次同步之后新增的文件」要等增量或「清理断点后全量」才补——这是跳过重扫的必然取舍，面板与安装说明均已写明。
- 目录粒度流式意味着「中断时当前目录的已扫文件不落盘」（要等该目录枚举完才写）——换取的是同名冲突的正确消解；对多目录大库几乎无感（按目录推进），断点续传保证已完成目录不重扫。

---

# 2.4.1 验证报告

目标：MovieClaw v0.35.0（兼容基线）+ 支持插件网页的新宿主（≥ v0.42.0）。

## 变更：网页入口收敛为一个（侧边栏「115网盘」）

用户意见：「自带网页留 115网盘 /apps/p115-library 就够了」。2.2.0 的网页清单里额外声明了三个
`shortcuts`（全量同步 `#full` / 网盘文件 `#files` / 运行日志 `#logs`），宿主会把它们渲染成侧边栏的
多个入口；而网页内部本来就有 CMS 左侧导航直达同样三页——多余入口是噪音。另外插件清单 `description`
仍写着旧的 `/api/v1/plugins/p115-library/panel` 面板地址，宿主批准页直接展示这段文字，等于把已退居
次选的地址继续推荐给用户。

### 改动

1. **`web/manifest.webmanifest` 去掉 `shortcuts`**：只剩 `name` / `short_name:"115网盘"` / `start_url:"."`
   / monochrome 图标——一份清单、一个入口。
2. **`movieclaw-plugin.toml` `description` 改为唯一入口**：「左侧边栏「115网盘」（/apps/p115-library）」，
   不再出现 `/api/v1/plugins/.../panel`。
3. **设置项说明同步**：`cloud_cid` 与 `cookie` 两个字段的 description 里那句「扫码入口：面板地址」改写成
   「MovieClaw 左侧边栏「115网盘」」（这是宿主设置页唯一能给用户的文字提示位）。
4. **保留的能力**：面板路由 `/panel` 与双挂载 base 正则**一行未动**，老宿主（v0.35.0）照样能开；
   hash 直达（`#full` / `#files` / `#logs`）也仍可用，只是不再向宿主声明入口。

### 测试（160 项，159→160）

- `test_single_sidebar_entry_no_shortcuts`：manifest 不得再声明 `shortcuts`、`short_name` 固定为「115网盘」、
  `start_url` 落在网页根；
- `test_drive.py::test_manifest_permissions`（既有）追加断言：`description` 必须含 `/apps/p115-library`
  且不得含旧面板地址——防止下次改描述又把老入口推荐回来；
- `test_dual_mount_base_and_theme_follow` 里的 hash 两条断言保留并改写注释（地址直达仍受支持）；
- 删除原 manifest 用例中依赖 `shortcuts` 列表的循环断言。
- 版本号 2.4.0 → 2.4.1（清单 / 配置导出 / 安装说明 / 清单断言）。

### 兼容

- 清单零新增字段、`permissions` 完全不变，**无需重新批准**；已加入侧边栏的旧三个快捷入口在新包安装后
  由宿主按新清单重算（现场观察一次即可）。

---

# 2.4.0 验证报告

目标：MovieClaw v0.35.0（兼容基线）+ 支持插件网页的新宿主（≥ v0.42.0）。

## 变更：撞到 115 风控自动等待后继续 + 写入与扫描并行

用户现场反馈：「strm 在生成的一定数额就不动了，但扫描还在持续」——期望「STRM 随着扫描持续生成；到达风控线后等一段时间继续扫描」。

### 根因（源码级定位）

1. **限流会让整次同步报废**：`drive_api.request()` 对任何失败一律 `raise DriveError`——包括 `errno 590075 操作太频繁`（已登记在 `ERRNO_HINTS`，来自 p115client 0.0.9.7.2 的 errno 语义）、HTTP 5xx/429、网络异常，以及被限速页替换掉 JSON 的响应体。大库全量要发数千次分页请求，**撞一次限流 = 本轮所有产出作废**，下轮从头再枚举一遍 1.5s/页 的风控成本。
2. **写入串行挂在枚举里**：2.3.0 已实现边扫边写，但 `await apply(...)` 在生产者协程内同步等待——每写一个 STRM 要 6~8 次宿主往返（sign + 两条 `store.set` + `makedirs` + `open/write/fsync` + `rename`，且当时每个文件都要重复一次目录创建），这段时间网盘侧一页都翻不了：文件树明显跟不上扫描，看起来就是「生成到一定数额就停了」。
3. **"写入数不涨"缺乏解释**：已存在且内容与同步记录一致的文件计入「未变化」而非重写（2.0.2 起的设计），加上写意图（proof）在无写盘时也会保存，重跑一次就在盘文件的写入数自然为 0——用户只能看到计数器不动。

### 修复设计

1. **可重试分类（新的 `ThrottleError`）**：`RETRYABLE_ERRNO={590075,990003,990005}`、`RETRYABLE_STATUS={408,429,500,502,503,504}`、非 JSON 响应体（限速页特征）与网络异常，统一归类为「值得等」的错误；**凭据类错误（990001 等）与结构性错误保持 DriveError，立即失败**（等待它们只会拖延报错）。`DriveError` 与 `ThrottleError` 的父子关系让既有 `except DriveError` 分支语义不变。
2. **退避而非失败**：`request()` 成为薄重试壳，`_once()` 保留原有单次请求与 Cookie 保密处理；等待时长 = 基础值 ×2^(n-1)（默认基础 30s，单次硬上限 600s，±10% 抖动避免多实例同步踩点），最多 5 次（`retry_max` / `retry_cooldown` 可在控制台调整）。耗尽后报错带明确次数与可操作建议。**冷却在锁外等待**，不占用 API 节流队列；**可被「停止同步」打断**（0.5s 分片轮询 `cancel_check`）。
3. **扫描 ⇄ 写入双任务**：`sync_mapping` 内 `produce()`（网络枚举 + 冲突裁定）与 `consume()`（校验 + 落盘）由深度 64 的有界队列连接，谁先崩就把原始异常传给对方并整体中止（首个故障优先抛出，保持 2.3.1 的「消息不被吞」语义）；队列限深保证枚举领先时内存不涨。
4. **少跑一次 IPC**：`atomic_write` 复用本轮已建目录集合（`self._dirs`），同目录的后续文件不再重复 makedirs（目录被外力删除时会清缓存重建一次）；状态上报合并为 ≤1.5 次/秒（计数器在内存保持精确，状态跃变立即落库）。
5. **等待看得见**：新的 `throttle_begin/throttle_end` 回调写入 `status.throttle_until/count/limit/reason`，`/strm/status` 用**宿主时钟**计算剩余秒数（不依赖客户端时钟），面板橙色显示「⏸ 115 限速冷却中：剩余 Xs」；写入数长期不涨时补充灰色说明「正在边扫描边核对」；每次限速与结束汇总写入运行日志。

### 测试（159 项，149→159，三轮全绿）

- `test_throttle_waits_then_continues`：`errno 590075` 连续两次 → 等待 30s/60s 后第三次成功（不用真睡，注入 `Drive.sleep` 记录），`list()` 正常返回；
- `test_throttle_retry_exhausted_message`：重试耗尽后消息保留「操作太频繁」并追加「已等待重试 2 次」与调大 API 间隔的建议；
- `test_login_expiry_is_not_retried`：990001 登录超时只请求一次、零等待——凭据类错误不进退避；
- `test_server_status_retry_matrix`：503/429 走冷却后恢复，403 立即失败且不重试；
- `test_cooldown_can_be_interrupted_by_stop`：`cancel_check` 为真时冷却立即抛 `CancelledError`（点停止不用干等 10 分钟）；
- `test_cooldown_never_exceeds_cap`：基础 600s 时每次等待都贴 600s 上限且**抖动不会越界**（先抖动后封顶）；
- `test_scan_and_write_overlap_instead_of_serializing`：写入逐个放慢 0.05s、枚举每步 0.04s（20 个文件），总耗时必须小于「两者之和 ×0.75」——串行实现约 1.8s 会红，当前约 1.0s；
- `test_writer_failure_stops_the_walk_immediately`：写盘异常立刻中止整轮（不会卡在队列或 Grace 等待上），错误消息进入 status；
- `test_throttle_wait_is_visible_then_cleared`：冷却状态字段与运行日志 warn 正确产生、恢复后清零，且引擎构造时已把回调挂到 drive；
- 面板静态：`PAGES/NUM` 覆盖两个新配置项、状态区含 `throttle_wait` 文案（避免下次改动把它删掉）；既有权益保护、清理防误删、2.2.1 容错、2.3.x 签发矩阵与 E2E 子进程全链路全部原样通过。

### 本次改动的风险边界

- 单次最长等待 600s、最多 5 次 = 最坏约 0.5 小时才判失败；凭据类/结构性错误不受影响，仍立即失败。等待期间的用户体验靠面板「⏸ 115 限速冷却中」与运行日志兜底；不想长等待可把 `retry_max` 调小或设 0 恢复旧行为。
- 写→扫并行后，同一个相同名称冲突的两个候选可能在不同任务里先后落盘；`apply` 的 `current.get(relative)` 替换逻辑与 `conflict_rank` 全序保证最终态与顺序无关（既有冲突用例覆盖）。

---

# 2.3.1 验证报告（历史）

目标：MovieClaw v0.35.0（兼容基线）+ 支持插件网页的新宿主（≥ v0.42.0）。

## 变更：宿主未配置外部访问地址的诊断与降级

### 根因（源码级定位）

用户现场报「失败：STRM 生成失败（ValueError），请查看日志或检查配置」。三步链路核实：

1. 宿主 `plugin_routes.py:167`：`routes.sign(absolute=True)` 在「设置 → 应用」的 `external_url` 为空时 `raise ValueError("没有配置外部访问地址（设置 → 应用），无法生成完整链接")`；
2. SDK `runner.py:684`：宿主 RPC 错误 `kind=="value"` 在插件侧以 `ValueError(message)` 抛出——**真实消息在异常对象里**；
3. 插件两层兜底（`sync_engine.run` 与 `life_strm.tick`）都只保留 `type(e).__name__`——消息被丢弃，用户只看到无用的「（ValueError）请查看日志」。

### 修复设计（三层）

1. **诊断透传（不再吞消息）**：`run()` 兜底与 `tick()` 兜底改为「同步失败（类型）：真实消息」；意外异常把堆栈末 600 字写入运行日志（面板可复制），且不再对 DriveError 之外的类型隐藏文本。
2. **签发降级链（真正的解药）**：`apply()` 中 `strm_base_url` 非空时改用 `routes.sign(absolute=False)`（相对链接）+ `rebase_url` 自行拼接 origin——**完全绕开宿主外部访问地址依赖**；为空时保持 `absolute=True`，并捕获 `ValueError` 转为带双修复路径的 `DriveError`（①宿主设置→应用填外部地址；②插件 STRM配置 填直连地址）。`/strm/rewrite` 的目标地址探测与 `/config` 的示例地址同样适配。
3. **端到端验证**：E2E 假宿主新增 `host_flags['external_url']` 开关，按宿主契约模拟 `absolute=True` 回 `kind=value` 错误、`absolute=False` 回相对链接。

### 测试（149 项，146→149，三轮全绿）

- `test_host_without_external_url_fails_with_guidance_not_bare_valueerror`：无直连地址 + 宿主无外部地址 → 失败消息含「设置 → 应用」「strm直连地址」指引，无裸 ValueError，无半成品文件；
- `test_base_url_override_bypasses_missing_host_external_url`：配置直连地址后 sign 全部走 `absolute=False`，宿主无外部地址也能完整同步；对照场景确认不配直连地址时恢复绝对签发；
- `test_unexpected_exception_text_is_not_swallowed`：非 DriveError 异常（RuntimeError）的真实消息必须进入失败状态；
- E2E 矩阵：①宿主无外部地址 + 无直连地址 → 同步失败带指引、地址替换预览 400 带指引；②配置直连地址 → 相对签发 + 完整同步 + STRM origin 为覆盖地址。

### E2E 踩坑记录

失败态轮询必须绕开**陈旧状态**：`tick` 失败路径只覆盖 `state/error` 两键，`written/synced_at` 等保留上一次成功同步的值；轮询判定条件须为「completed 且 error 为空」，否则会在新一轮同步启动前误读上一轮的 failed 态。

---

# 2.3.0 验证报告（历史）

目标：MovieClaw v0.35.0（兼容基线）+ 支持插件网页的新宿主（≥ v0.42.0）。

## 变更：边扫描边生成 + 快速模式对全量生效

### 设计（关键取舍）

1. **流式写入（边扫边写）**：`sync_mapping` 不再把所有视频收集完再统一写入——walk 每枚举到一个视频立即走「校验→写记录→落盘」（`apply` 内联闭包），文件树随扫描实时生长，写出的文件立即可播（`play:` 记录先于文件写入）。原先「先收集再写」的三个正确性前提逐一保持：
   - **同名冲突**：当场按 `conflict_rank`（体积→mtime→pickcode）消解——较大者重写同一 STRM，被淘汰者进运行日志；被替换者的 `play:` 播放记录同步删除，不留垃圾；
   - **清理（删除网盘已不存在的文件）**：仍等**完整枚举结束**才执行，枚举中途绝不删除任何文件；
   - **附属下载**：仍等完整枚举后统一配对（字幕可能在视频之后才被枚举到），记录在获得 sidecar/cloud 数据后刷新写意图。
2. **中途失败自愈语义**：同步记录（manifest）只在**成功**时保存——枚举中途失败（网络/分页异常/同名冲突中止模式）留下部分文件树在盘，这些文件带写意图（proof）与播放记录；下轮同步重新枚举时按正常校验流程识别为「未变化」或重建缺失部分，无需人工干预。部分文件树永远不会被误当成完整结果（无 manifest 即无清理依据）。
3. **极速模式对全量生效**：读回校验条件由 `fast and mode!='full'` 改为 `fast`——极速模式下全量同样**信任同步记录、跳过未变化文件的读回**；`exists` 检查保留，文件被外部删除仍会重建（快速只省读回，不省修复缺失）。普通模式语义不变：逐个读回校验，可发现并拒绝被外部改动的文件（所有权保护），正确性最强。
4. **耗时结构**：大库全量总耗时≈网盘枚举时间（受 115 风控 1.5 秒/页节流，目录数×1.5s），写入与校验摊入枚举等待中不再单独可见；文件从扫描开始即持续出现。

### 测试（146 项，三轮全绿）

新增 2 项流式核心测试 + 既有冲突测试按流式语义更新：

- `test_streaming_writes_tree_as_it_scans`：枚举中途失败 → 第一个视频的 STRM 已落盘且 `play:` 记录可播；修复后重跑 → written==1/unchanged==1 自愈补齐；
- `test_fast_full_trusts_manifest_and_heals_deletion`：健康库快速全量零读回（读回计数器为空）、零写入；删除文件后快速全量仍重建（written==1）；
- `test_conflict_auto_keeps_larger_file`（更新）：较小者先写出、较大者当场重写（written==2 诚实计数），最终仅一个 STRM、播放记录独占较大者；
- `test_conflict_abort_mode_names_both_files`（更新）：中止模式报错仍点名两个文件，流式语义下已扫到的文件保留在盘（下轮自愈）。

既有 2.0.2 全量修复语义测试（健康库零写入/缺失重建/配置变更重写/手改拒绝覆盖）、2.2.1 容错测试、附属下载、清理防误删、面板三重静态校验与 E2E 子进程全链路（含 `/strm/rewrite` 地址替换、事件监控、未登录状态机）全部原样通过。

### 打包

`p115-library-2.3.0-movieclaw-v0.35.0.zip` / `.mcplugin`，check_compat PASS，授权不变（仅媒体库读写），无需重新批准。老宿主（v0.35.0）正常安装升级。

## 待现场验证

- 大库全量：文件树应在扫描期间持续出现（媒体库目录实时可见 `.strm` 增长），而非结束后一次性出现；
- 极速模式全量：健康库重跑全量的耗时对比（应≈纯枚举时间）；
- 中途断网/失败后重跑：应自愈且不产生重复文件。

---

# 2.2.1 验证报告

目标：MovieClaw v0.35.0（兼容基线）+ 支持插件网页的新宿主（≥ v0.42.0）。

## 变更：网盘单条数据异常不再中止整个同步

### 契约核实（不猜测，逐条对生产源码）

OpenList（`drivers/115/util.go`、`driver.go`、`types.go`）与 SheltonZhu/115driver（`pkg/driver/file.go`）的条目解析是**全容错**的：`File.From()` 对名称/ID/时间的解析失败一律忽略（`if err == nil` 才赋值），单条异常从不让整个列表失败；**目录判定是 `FileID != ""`（fid 真值），而非 fid 键存在**。此前我们的 `normalize()` 严格校验每一条并在任一条异常时中止整个同步——与生产实现的行为相悖，用户实测全量同步即报「115 文件数据格式不合规，同步已停止」。

### 设计（关键取舍）

1. **单条异常 → 跳过 + 日志 + 计数**：`list()` 逐条 try/except，异常条目收集为 `{index, reason, detail}`；`normalize()` 的失败原因精确到字段（ID 非数字/名称为空/名称含非法字符/缺少字段/大小无法解析），`describe_bad_entry()` 生成有界诊断（n/fid/cid/pc/s/sha 的 repr + 字段名列表，不倾倒整条数据）。walk 分页按**原始页长**（`raw_len`）推进，跳过的条目不使分页错位；经 `bad_sink` 上抛带目录上下文。
2. **同步引擎接入**：`sync_mapping` 结束后 `skipped` 计数入 run 报告（面板「上次同步」行与运行中实时进度均显示），运行日志逐条 warn（上限 10 条 + 汇总行），每条含位置/原因/字段诊断与处理建议。
3. **仍保持中止的异常**：结构性问题（分页响应不合规/提前结束/条目重复/目录与请求不一致）不变；新增**整页全坏跳闸**——一页数据全部无法解析说明 115 系统性更改了响应格式，此时中止并带首条诊断，防止"成功"同步出空库。
4. **目录判定对齐生产**：`fid` 为空串视为目录（此前视为文件后必然报错）；`fid` 为数字类型时转字符串处理（比 115driver 的强类型 unmarshal 更宽容）。

### 测试

- 新增 6 项：`test_normalize_empty_fid_is_directory`（空 fid=目录）、`test_normalize_precise_reasons`（失败原因精确）、`test_list_skips_bad_entries_with_reason`（混页跳过+诊断）、`test_list_all_bad_page_is_fatal`（整页全坏中止）、`test_walk_propagates_bad_entries`（bad_sink 上抛+分页不错位）、`test_bad_cloud_entry_skipped_logged_and_counted`（引擎层计数+运行日志）。
- E2E（`test_process.py`）：网盘 mock 注入名称非法条目（`坏\名字.mkv`），断言同步完成、好文件照常入库、`/strm/status` 的 `skipped==1`、`/log/list` 出现带原因与字段诊断的 warn 条目。
- 全套 **144 项**（138→144）连跑三轮全绿；面板 JS `node --check` 通过（既有校验）；`web/index.html` 与 `panel.PANEL` 逐字节一致（面板文案变更后重新生成）。

### 待现场验证

- 用户实例上复跑全量同步：此前失败的真实原因会在运行日志中精确显示（哪个目录哪一条、什么字段问题）；跳过计数应与日志条数一致；被跳过的文件在 115 端改名或删除后，下一轮同步 skipped 归零。
- 若出现「整页全部无法解析」的中止（系统性格式变化），日志首条诊断可直接反馈开发者定位。

---

# 2.2.0 验证报告

目标：MovieClaw v0.35.0（兼容基线）+ 支持插件网页的新宿主（≥ v0.42.0）。

## 变更：插件网页——控制台进入 MovieClaw 侧边栏

### 契约核实（不猜测，逐条对源码/设计稿）

「插件网页」按 MovieClaw 官方设计稿 `docs/design/plugin-web-pages.md`（v0.42.0 仓库）实施，作者侧契约只有三件事：①网页放 `web/`，入口 `web/index.html`；②侧边栏定制用标准 Web App Manifest（`short_name`/`icons[purpose=monochrome]`/`start_url`/`shortcuts`），**没有一个 MovieClaw 自有字段**；③页面是普通网页，同源 `fetch("/api/v1/plugins/<id>/...")` 调插件 API。宿主在 `/apps/<插件id>/` 用同源 iframe（不加 sandbox）嵌入，注入 `--mc-*` 主题变量，页面以**当前查看者**身份运行。

### 设计（关键取舍）

1. **一份 HTML、两种挂载**：不做第二份网页。`web/index.html` 由打包脚本从 `panel.PANEL` 逐字节生成（单一事实源），老入口 `/panel` 路由照旧。API 前缀推导改为双匹配：`location.pathname.replace(/\/(panel\/?|web(\/.*)?)$/,'')`——iframe（`/api/v1/plugins/p115-library/web/` 或带子路径/hash）与老面板地址都能得出正确 base。
2. **主题跟随（可选能力，带兜底）**：JS 检测 `/web` 挂载 → `html.in-app` class；CSS 把 `--text/--muted/--faint/--accent/--accent-strong/--border/--surface-raised/--glass-fill(--hover)` 映射到宿主 `--mc-*` 变量（`var(--mc-text,#f3f5f9)` 兜底），`body` 背景透明透出外框玻璃。状态色（绿/橙/红/蓝）保留自带值不动。老宿主直接打开时无 `--mc-*`，全部回落原值——**两种挂载零外观回归**。
3. **hash 路由**：`go()` 切页时 `history.replaceState(null,'','#'+page)`（不压历史，返回键仍按外框整页走）；boot 读初始 hash 直达页面——`manifest.webmanifest` 的 `shortcuts`（全量同步 `#full` / 网盘文件 `#files` / 运行日志 `#logs`）靠它落点；外框地址同步监听 hashchange，行为对齐设计稿「插件内部换页不重建 iframe」。
4. **manifest.webmanifest**：`short_name:"115网盘"`（≤8 汉字）、`start_url:"."`、monochrome 云+播放图标（fill-rule evenodd 挖孔，CSS mask 用）、三个 shortcuts（hash 落点，不出 `web/` 范围）。
5. **兼容性（源码核实）**：v0.35.0 宿主 `read_archive` 只校验大小/路径安全/软链/清单（`packages.py:151-181`），**不拒绝未知目录**——带 `web/` 的包在老宿主正常安装（解出不用），在新宿主自动获得侧边栏入口。清单零新增字段，`check_compat` 不变。
6. **打包生成**：`package_p115_220.py` 打包前 `from panel import PANEL` 写 `web/index.html`，打包后断言 zip 内三件套齐全且 `web/index.html == PANEL`。

### 测试

**137 项通过 + 1 项打包期待跳过**（2.1.1 为 135；新增 test_panel 3 项：双挂载正则/in-app 检测与主题 CSS/hash 读写，web 契约 manifest+图标+shortcuts 落点断言，web/index.html 生成一致性[打包后生效]），连跑三轮全绿；面板 node --check 通过。

### 已知边界

- 静态托管 `/api/v1/plugins/<id>/web/*`、侧边栏入口 `/apps/...` 是**宿主侧**能力，v0.35.0 的 SDK runner 无此实现——E2E 只能验证 `/panel` 照常 200 与包内三件套；iframe 实际观感（主题跟随、地址同步、玻璃透出）标注待现场验证。
- 成员可见性由批准页「成员也能看到」勾选控制（用户决定，默认不勾）。
- 浅色主题下页面自带深色侧栏条仍是深色（保守取舍），标注待现场调优。

### 待现场验证

- 新宿主（≥0.42.0）批准页的入口预览与两个勾选；侧边栏「115网盘」入口；shortcuts 直达；主题切换跟随；手机端「我的 → 插件页面」。

---

# 2.1.1 验证报告

目标：MovieClaw v0.35.0。

## 变更：未登录状态机修复（全新安装不再报「请先扫码」异常）

### 需求与语义

用户反馈：「为什么报错请先扫码？你把扫码登录去掉了？」。核实：**扫码登录完好无损**（`login.py`、`/login/*` 全套路由、面板「115账号」扫码区、qrcode 依赖都在包内）。报错来自真实缺陷：`AutoConfig.parsed_mappings` 要从 Cookie 提取 UID 构造账号隔离的 library_id（`auto:<UID>:<root>`），Cookie 为空时抛 `DriveError('请先扫码登录 115')`——而 `boot()` → `active()` 在插件加载早期就访问它，**未登录时整个插件加载即崩**；`/strm/status`、`/strm/rewrite` 同样中招；`tick()` 里 `waiting_login` 分支是永远到不了的死代码（mappings 检查在前、先炸）。

### 设计（关键取舍）

1. **新增 `AutoConfig.has_mappings`**：不依赖 Cookie 的「是否配置了映射」纯配置探查（mappings JSON 或单目录 CID+目录非空）。`active()` 改用它——未登录时循环照常启动，而不是让 boot 崩溃。
2. **`tick()` 检查顺序反转**：Cookie 检查置顶，未登录正确进入 `waiting_login`（错误文案带面板指引：「请先扫码登录 115（面板「115账号」页）」）；`waiting_login` 死代码复活。
3. **`/strm/status` 未登录不炸**：Cookie 为空时不触碰 `parsed_mappings`（mappings=[]、enabled=False），面板状态页显示「等待登录」而非 500。
4. **`/strm/rewrite` 未登录给指引**：HTTP 400「地址替换不访问 115，但同步记录按 115 账号区分，需先登录才能定位记录：请在面板「115账号」页扫码登录或粘贴 Cookie」——替代裸的 500「请先扫码登录 115」。替换**确实需要登录**（manifest 键按账号隔离，无 UID 无法定位记录），这一点是设计约束而非缺陷。
5. **扫码成功后立即同步**：`/login/poll` 登录成功后 `life.signal.set()` 唤醒循环（此前要等下一轮 life_interval 定时周期）——全新安装「装好→扫码→立即出 STRM」。

### 测试

**135 项全部通过**（2.1.0 为 134；新增 1 项回归单测 + E2E 扩段），连跑三轮全绿。新增覆盖：无 Cookie 时 boot/active 不抛、`has_mappings` 为真、`parsed_mappings` 仍正确要求凭据（记录隔离语义不丢）、tick 进入 waiting_login、热更新 Cookie 后同一实例立即恢复同步出 STRM；E2E（真实子进程，init 不带 Cookie）覆盖：`/strm/status` 返回 waiting_login+指引、`/strm/rewrite` 400 明确文案、`/login/info` 未登录、扫码 mock 登录成功→qr-cookie secret 存储→自动开始同步生成 STRM 的完整链路。

### 已知边界

- 未登录时「地址替换」依然不可用（需先登录定位账号记录）——这是账号隔离的设计约束，修复只把报错从 500 异常改为 400 指引。
- `/strm/full`、`/strm/incremental` 未登录时会「假启动」（tick 立即 return，状态转 waiting_login），面板状态区会显示等待登录，无副作用。

### 待现场验证

- 真实全新部署：安装 2.1.1 → 面板打开不报错 → 状态区「等待登录」→ 扫码 → 首次同步自动开始。

---

# 2.1.0 验证报告

目标：MovieClaw v0.35.0。

## 变更：存量 STRM 地址替换（外网地址变更后无需重新同步）

### 需求与语义

用户诉求：「对存量 STRM 进行地址替换，避免更改外网地址后无法播放」。核实链路后确认：STRM 内容 = `rebase(routes.sign('/play/<fid>', absolute), strm_base_url)`——origin 来自**签发时点**的地址，外网地址一变，存量 STRM 全部指向旧 origin。常规同步虽可自愈（expected 变化 → 重写），但要**完整枚举网盘**（每页 1000 条、每页一次受 request_interval 节流的 115 请求），大库耗时数分钟以上。

### 设计（关键取舍）

1. **目标地址 = 当前签发地址**（`rebase(sign, strm_base_url)`，与 `/config` 的 sample_play_url 同源），而不是让用户手填新地址——替换结果与「下一次同步会生成的内容」完全一致：manifest 的 url_hash、防误删凭据（`owned:` proof）同步重写，下次增量核对全部「未变化」，零重复写入。用户要换外网地址时只需先把「strm直连地址」改好并保存，再执行替换。
2. **纯本地驱动，零 115 流量**：按各映射的 manifest 记录逐条处理（exists → 读内容 → sha256 比对记录 url_hash → urlsplit 取 origin），不做目录遍历。
3. **防误删语义不变**：内容哈希与记录不一致（=被手工修改）的文件计数 `modified` 跳过；非 `/play/` URL、不在映射 base 内的记录计 `foreign` 跳过；文件缺失计 `missing`；可选 `old_base` 参数把替换限定在指定旧地址（`mismatched` 计数其余）。
4. **只换 origin**：`target_origin + parts.path + '?'+parts.query`——签名、`?/文件名`（pick_code_name 格式）后缀原样保留（rebase_url 同款语义：宿主验签只看路径与签名）。
5. **并发保护**：与同步共用 engine 锁——同步进行中 409；替换持有锁期间到达的定时/事件核对会以「已有同步正在运行」快速失败（既有语义，下轮自愈）。替换产生写入时设置 scan-pending，下轮同步自动触发媒体库扫描。
6. **面板**：「STRM配置 → 存量STRM地址替换」预览/执行按钮 + 结果徽标（目标地址、记录数、可替换/已替换、已最新、缺失/已修改/其他地址/非本插件文件计数 + 首条例子），未登录门控；预览/执行均写运行日志。

### 测试

**134 项全部通过**（2.0.2 为 128；新增引擎层 6 项 + E2E 扩段），连跑三轮全绿；面板 node --check 与 ID 交叉校验通过。新增覆盖：预览绝不改文件；执行只换 origin（签名与换行保留）；替换后记录对齐（把 strm_base_url 配成同一地址后下次同步 written==0/unchanged==1）；二次执行全「已是最新」；手改文件计数跳过且内容不动；old_base 过滤不匹配时零替换；文件缺失计数；同步持锁期间拒绝（DriveError）；`?/文件名` 后缀原样保留。E2E（真实子进程 + Unix socket）：预览 409 重试 → 未改文件 → 配置新直连地址 → 执行替换 → 两个映射 STRM 均为新地址且 `?sig=test` 保留 → 已停用映射（auto-strm）不被触碰 → 再次预览全「已最新」 → 新地址 302 直达 CDN 播放链路全通。

### 已知边界

- 替换只覆盖**当前生效映射**的 manifest（单目录或已保存的多映射）；已删除/停用映射的存量文件不会被触碰（有意设计——记录已不在当前清单中）。这类文件在恢复对应映射后会由常规同步处理。
- E2E 首轮发现的「执行请求空体默认 dry_run=True」是测试用例问题（pydantic 默认值），产品接口默认预览模式是**有意设计**（防误操作），面板执行按钮显式传 dry_run=false。

### 待现场验证

- 真实外网地址变更场景：改「strm直连地址」→ 保存 → 预览 → 执行 → 播放器按新地址播放。
- 大库（数千 STRM）替换耗时与面板请求超时表现（本地文件 RPC 逐个读写，预期秒级到几十秒）。

---

# 2.0.2 验证报告

目标：MovieClaw v0.35.0。

## 变更：全量同步改为「校验全部、按需重写」

### 背景与语义

用户问「全量同步的快速模式怎么极速」。核实代码后发现两层事实：① 快速模式（sync_mode=fast）只对**增量**生效——记录匹配的文件跳过本地读取与内容校验；全量当时是**无条件重写每个文件**（`needs=mode=='full' or ...` 恒真），快速模式对它完全无效。② 全量/增量的耗时大头都是网盘全量枚举（每页 1000 条、每页一次受 request_interval 节流的 API 请求）——这是风控约束，不可绕过。

改动（sync_engine.py）：`needs` 去掉 `mode=='full'` 项 → 全量仍逐个读校验（fast 的跳过条件本就带 `mode!='full'`，全量永远读），但只有 `not same or not exists or raw!=expected` 才写入。

**修复能力不受损**（逐场景核验）：文件被外部删除 → exists=False → 重建；播放地址/直连格式变化 → expected 变 → 记录不匹配 → 重写为新地址；记录损坏 → same=False → 重写；用户手改文件 → 所有权校验（sha256 与记录不符即视为用户文件）依旧抛「未覆盖」——外部改坏内容与用户手改不可区分，防误删语义优先，**不做**「修复改坏内容」。

### 测试

**128 项全部通过**（test_full_rewrite 改造为 test_full_verifies_and_heals_but_skips_unchanged：健康库全量 written==0/unchanged==1；删除文件后全量重建；strm_base_url 变化后全量重写为新地址）。连跑三轮全绿；面板 node --check 通过。

### 速度模型备忘（回答「怎么极速」）

- 增量+快速：未变化文件零读取，只剩枚举+stat/记录 RPC；
- 增量+普通：每文件一次读+哈希比对；
- 全量：强制读校验（不可跳过——这是全量的存在意义）+ 按需写入；
- 通用瓶颈：网盘枚举请求数 × request_interval（1.5s）+ page_size（1000/1150 可调）。调小间隔提速但升风控风险；真正的日常「极速」是 1.8.0 事件监控的定点核对（单目录、无全量枚举）。

---

# 2.0.1 验证报告

目标：MovieClaw v0.35.0。

## 变更：去掉 `/media/strm` 绝对路径授权（STRM 目录放媒体库内即可）

### 动机与契约核实（逐字核对宿主源码 plugin_files.py，非猜测）

1. 用户诉求：清单中的 `/media/strm`（rw）能否去掉——自己在媒体库目录里新建 STRM 目录，靠 `library`（rw）覆盖即可，不单独自定义路径。
2. **可行**。宿主 `PluginFilesService.roots()`（plugin_files.py:172-190）对 `library` 授权把**所有已配置媒体库**的每个 `root_paths` 注册为授权根；`PluginFiles.check()`（:86-94）用 `os.path.realpath` + `is_relative_to` 判定——授权判定是"路径在根之下"，**与目录何时创建无关**：用户自建的子目录、插件运行中 `makedirs` 新建的剧集目录全部自动覆盖。
3. 历史包袱：`/media/strm` 授权源于 1.6.4 事故（当时用户 STRM 目录在媒体库根**之外**才补的绝对路径）。2.0.1 起 STRM 目录约定放媒体库内，绝对路径授权不再必要。

### 改动

1. **manifest**：`paths = [{ library, rw }]`，不再申请任何绝对路径；版本 2.0.1。
2. **写探测报错指引更新**（sync_engine.py）：删掉 1.6.12 时代的 `/media/download`、`/media/link` 过时文案，改为"STRM 目录必须位于已配置媒体库目录之内；库外路径请移入库内或单独授权"。
3. **面板**：本地STRM目录输入框 placeholder 与说明、多目录映射目录输入 placeholder 注明"须在媒体库目录内"。
4. **README/安装说明**：新增 2.0.1 节说明机制与前提；安装步骤权限说明同步更新。

### 测试

**128 项全部通过**（2.0.0 为 127；新增 manifest 回归测试：paths 只含 library(rw)、operations 不变、版本号与清单一致，防止未来误加回绝对路径）。连跑三轮全绿；面板 JS node --check 通过。

### 待现场验证

- 宿主批准页应只出现媒体库读写授权（无 /media/strm）。
- 用户在媒体库根下自建 STRM 目录（如 `/media/strm`）→ 保存配置 → 全量/增量同步实际落盘。
- 反向验证：把 STRM 目录指向库外路径应被拒绝且报错指引正确（报错文案含"媒体库"指引）。
- OS 层权限独立：媒体库目录需允许 MovieClaw 运行用户写入。

---

# 2.0.0 验证报告

目标：MovieClaw v0.35.0。

## 变更：移除上传功能（本地→网盘上传链路整体下线）

### 动机与设计

1. 1.8.0 起事件监控覆盖全部 115 操作类型——任何设备上传/移动文件到监控库都会即时生成 STRM，本地→网盘上传链路已无必要，且持续产生会话过期（1.8.1）、入账确认（1.7.6）等复杂故障面。
2. **移除**：`upload_api.py`、`upload_queue.py`、`directory_watch.py`（整理目录扫描）、上传管理面板页与全部 `/uploads*`、`/watch*` 路由、Settings 上传字段（organized_directory/auto_upload/watch_interval/stable_seconds/upload_excludes）；`p115_upload_crypto.py` 中上传专用部分（token/payload 构造）一并移除，ECDH 响应解密部分抽为新模块 `p115_ecdh.py`（drive_api 的 downurl 信封解密依赖保留）。
3. **附属文件改为唯一网盘侧通道**：删除 `copy_sidecars`（本地整理目录复制），保留 `pull_cloud_sidecars`（115 直链下载）；附属文件直接放网盘与视频同目录即可。
4. `uid()` 从 upload_api 迁入 life_strm（scope_key 命名空间依赖，全部既有 STRM 记录与防误删凭据跨版本不变）。
5. 授权精简：paths 只保留 `library` 读写 + `/media/strm` 读写（staging/download/link 不再申请）；升级需在宿主重新批准。

### 测试

**127 项全部通过**（1.8.1 为 153；删 test_upload.py 28 项、test_watch.py 15 项、上传 E2E 段，改造/新增：移除字段回归参数化 ×9、单目录×映射嵌套校验、云端附属体积上限、云端附属版清理×2、E2E 上传路由 404 与面板移除断言）。连跑三轮全绿。E2E 子进程 mock 改为网盘侧直接返回视频+云端附属——STRM 与附属文件经同一增量同步落盘。

### 待现场验证

- 覆盖升级后宿主对「路径授权减少」的批准流程（预期：包进待批准，确认后生效）。
- 实机用 115 官方客户端上传/移动文件 → 事件监控 → STRM 生成链路（1.8.0 已具备，本版不变）。
- `/media/download`、`/media/link` 授权取消后无残留引用（插件内已无该路径任何代码引用）。

---

# 1.8.1 验证报告

目标：MovieClaw v0.35.0。

## 问题：「失败 · 上传接口 HTTP 404；任务已暂停，可重试」——重试永远 404

### 根因

1. `upload_api.Uploader.http()` 对所有非 2xx 报统一的「上传接口 HTTP N；任务已暂停，可重试」。OSS 分片请求上的 **404 = NoSuchUpload**：持久化的 uploadId 已在网盘侧失效（过期、被 115 清理，或合并已成功而 uploadId 被消费、仅回调丢失）。
2. `process()` 只有「入账确认中断（completing）」的会话有丢弃路径（1.7.6）；**普通陈旧会话没有任何丢弃逻辑**——重试复用同一失效 uploadId → 404 → 失败 → 再重试再 404，死循环，唯一出路是删除记录。
3. 生产对照：p115oss/upload.py 不持久化会话、每次全新 initupload 靠 SHA1 去重——失效会话复用是我们独有的问题面。

### 修复

1. **404 精确分类**：`http()` 中 `aliyuncs.com` 请求的 404 抛新异常 `SessionExpired(DriveError)`（其他接口 404 仍为普通可重试错误，不误伤——initupload 的 404 与会话无关）。
2. **同轮自愈**：`process()` 捕获 `SessionExpired` → 删除旧会话 + 运行日志 warn「OSS 上传会话已失效（文件名），已自动重新初始化上传（网盘已有相同文件会秒传跳过）」+ 进度归零 + **立即用空会话重跑一轮**。initupload SHA1 去重回答「文件是否其实已落账」：已落账 → status 2 秒传零字节收尾；未落账 → 从头重传。**绝不产生重复文件**。
3. **暂时性错误不丢分片**：超时/5xx/网络异常仍走原有失败路径，保留会话断点续传——只有 404（会话确定已死）才丢弃。
4. E14 式「确认中断重驱动」若撞 404 也走该路径：从「下次重试再重建」升级为「当轮立即重建」。

### 测试

**153 项全部通过**（1.8.0 为 150；连跑三轮全绿）。新增 3 项：队列层 404 丢弃旧会话+空会话同轮重建+日志与进度断言；协议层 aliyuncs 404 分类为 SessionExpired 且续传命中旧 uploadId；initupload.php 404 保持普通 DriveError 不误判。

### 待现场验证

- 用户真实任务升级后点「重新上传」：应自动重建会话并完成上传（或秒传收尾），日志出现「OSS 上传会话已失效……已自动重新初始化上传」。
- 115 网盘侧 uploadId 的实际保留时长（过期即触发本路径，行为是重建而非失败）。

---

# 1.8.0 验证报告

目标：MovieClaw v0.35.0。

## 问题：「增加对 115 事件的日志——别的文件移动到监控库也应该新增」

### 根因

1.6.11 引入的事件监控只请求 `behavior/detail?type=upload_file`——单一类型：移动 / 接收 / 新建目录 / 删除 / 改名等操作既不进指纹（不触发目录核对），也不写日志；移动进监控库的文件只能等定时增量（cron）补扫才发现。

### 修复

1. **全类型拉取 + 客户端过滤**：`life_feed()` 去掉 `type` 参数拉取全部操作记录，按 p115client 生产实现 `IGNORE_BEHAVIOR_TYPES`（3,4,7,8,9,10,19——浏览/星标只读操作）在客户端过滤后再算指纹与写日志：读操作不触发同步，写操作全部成为触发信号。未知新类型保守保留——多一次有界的 scoped 核对无害（p115client 同款语义）。仍是每轮单请求（默认 60s 一轮）。
2. **操作名渲染**：新增 `BEHAVIOR_LABELS`（对齐 p115client `BEHAVIOR_NAME_TO_TYPE` 全表）：上传图片/上传/移动图片/移动/接收/新建目录/复制目录/目录改名/删除/复制/文件改名，未知类型显示「变动」；日志行形如 `检测到 115 网盘移动：<名>（体积）`，无文件名回退 file_id（既有防御不变）。
3. **去重键升级**：`life-seen-ids` → `life-seen-ids-v2`。宽口径后的历史移动/删除事件对旧去重记录全是「新」的，升级后首次轮询会一次性回放；v2 键使首次轮询只写「开始监控 115 网盘操作事件（已忽略历史 N 条）」标记，不灌日志。
4. **文案**：设置项「监控 115 生活操作事件」描述、面板增量页说明、README 设置参考同步更新。

### 测试

Python 3.13.12（托管运行时，独立 venv）：**150 项全部通过**（1.7.9 为 140；连跑三轮全绿）。新增 10 项：移动事件（type 6）触发目录核对写入 STRM 且日志含「移动」标签与体积、同步条目衔接（操作流程可见）；浏览/星标（type 8/4）不触发同步、不进日志、不进去重记录；8 类操作标签参数化渲染（含未知类型「变动」）；params 断言改为全量拉取；去重键断言迁移 v2。

### 待现场验证

- `behavior/detail` 不带 `type` 的真实返回速度（p115client 文档：不指定 type 响应较慢但风控更轻；轮询仍单请求/60s，风险可控）与行字段形态（`type` 数字、含 `id`）。
- 移动/接收事件在用户账号上的采集延迟（115 收集存在秒级延迟）。
- 平台限制：115 不采集「复制文件(23)/文件改名(24)」事件（生产文档明确说明）——这两类操作由定时增量核对兜底。

---

# 1.7.9 验证报告

目标：MovieClaw v0.35.0。

## 问题：「同步状态卡住」——长时间停在「同步中」，手动同步被 409 拒绝

### 根因

1. 同步收尾通知宿主 `library.scan.start`（触发媒体库扫描）与上传通知路径的 `library.list` 查询**均无超时**——宿主媒体库繁忙（如大批量上传后正在扫描）时这两个 RPC 可无限不响应。
2. 挂起的 `await` 卡住 `sync_mapping` → `run()` 无法到终态 → `life.state` 停在 running、`life.lock` 永不释放 → 面板状态永远「同步中」，后续手动增量/全量全部 409「同步正在进行」。
3. 同步运行中面板只显示「同步中」三个字，无任何进度——大目录枚举（115 API 全局间隔 1.5s×分页数）耗时数分钟，用户无从分辨「慢」与「死」。

### 修复

1. **RPC 有界**：`sync_engine` 扫描通知 `asyncio.wait_for(...,120)`（`scan_rpc_timeout` 可注入属性）；`AutoOps` 的 `library.list` 限时 30s、`library.scan.start` 限时 60s（同样可注入）。
2. **超时不判失败**：扫描通知超时 → `report(scan_pending=True)` + 运行日志 warn「媒体库扫描启动超时……本次 STRM 已全部写入，同步不受影响」，run() 正常 completed；下轮同步重试扫描。上传通知超时 → 面板 notice「媒体库查询/扫描启动超时……请稍后手动扫描或重试同步」，绝不抛错（notify 是尽力而为）。超时类异常与真实 RPC 异常区分：后者保持原有 DriveError 失败路径。
3. **实时进度**：`run()` 初始 report 带墙钟 `running_at`；`/strm/status` 在 state==running 时合并 engine 的 seen/written/unchanged/sidecars/deleted/conflicts 并计算 `running_for`；面板状态区显示橙色「实时进度：已扫描 N · 写入 N · 未变化 N · 已运行 Ns（每 5 秒自动刷新）」。
4. 升级即解卡：插件升级重启进程，挂起任务与内存锁全部清空。

### 测试

- `test_sync.py` 新增 2 项：① `test_scan_start_hang_is_bounded_and_not_a_failure`——mock scan.start 永久挂起 + `scan_rpc_timeout=.1`，run 应 completed、耗时 <10s、`scan_pending is True`、运行日志含「媒体库扫描启动超时」；② `test_autoops_scan_notify_timeout_degrades_to_notice`——`library.list` 挂起降级为 timeout 提示、`scan.start` 挂起返回空列表不报错、`library.get` auto: 前缀本地解析不受影响。
- 全量 **140 项**，连续 3 轮全绿。

# 1.7.8 验证报告

目标：MovieClaw v0.35.0。

## 问题：「同步历史」汇报内容太简单

### 根因

1. `life_strm.record_history` 每条只落 mode/written/unchanged/sidecars/deleted/error 六个字段，**没有耗时、没有扫描条目数、没有每映射明细、没有冲突文件名**。
2. 面板渲染只有一行拼接文本，多映射运行时各映射的贡献完全不可见；冲突时还要另去运行日志翻明细。
3. `sync_engine.run()` 成功路径不记录 elapsed；`sync_mapping` 不产出独立的每映射统计行。

### 修复

1. **数据层**：`run()` 记录 `elapsed`（成功/失败/取消各终态都带）；`sync_mapping()` 结束时把每映射独立行（name/seen/written/unchanged/sidecars/deleted/conflicts + conflict_detail 前 10 条，每条含胜者/落选者完整路径与体积）追加到 `status['mappings']`；老版本历史行缺新字段时面板自动兼容。
2. **历史行**：`record_history` 行新增 `seen`/`elapsed`/`mappings`；完成日志带「耗时 Ns · 扫描 N」。
3. **面板分层渲染**：同步历史每条记录三层——总览行（时间/模式/耗时/扫描/写入/未变化/附属/清理/冲突，失败红色）、每映射缩进行（映射名 + 各计数）、冲突明细橙色行（胜者 vs 落选者路径与体积）。
4. `report()` 的 mappings/elapsed 通过增量合并写入，取消路径 `best_effort` 保留。

### 测试

- `test_sync.py`：新增详细字段断言（run 结果含 elapsed≥0、mappings 列表、映射行含 name/seen 等全字段）与冲突明细断言（mapping 行 conflict_detail 含双方路径与体积）。
- `test_process.py`：E2E 断言同步历史行含 elapsed/seen/mappings 字段。
- 全量 **138 项**，连续 3 轮全绿（21.7s/21.7s）。

# 1.7.7 验证报告

目标：MovieClaw v0.35.0。

## 问题：增量同步失败「存在同名 STRM 输出冲突，请调整网盘文件名或目录」

### 根因

1. **触发条件**：同一网盘目录下两个不同格式的同名视频（如 `X.mkv` 与 `X.mp4`），`strm_keep_suffix=False` 时 `with_suffix('.strm')` 把两者压到同一个输出路径。
2. **旧语义一刀切**：`sync_mapping` 枚举阶段一发现 `relative in wanted` 就抛 DriveError 中止——异常穿出 `run()`，**后续映射全部不跑**，一条冲突拦死整个增量同步（用户 16:59:55 的报错正是这条）。
3. **报错不指名文件**：只说「请调整网盘文件名或目录」，不说是哪两个文件，用户只能到网盘里盲猜。
4. **潜在假冲突**：上传与同步并发时分页枚举可能重放同一文件（walk 的 `page_ids` 去重只覆盖单目录单页遍历路径），旧逻辑同样按冲突中止。

### 修复

1. **同名冲突策略（`Settings.strm_conflict_policy`，默认 `auto`）**：
   - `auto`：`conflict_rank`（体积 → 修改时间 → 提取码）确定性全序，保留较大文件、跳过落选者；胜者跨运行稳定（manifest 不抖动）；冲突明细（双方完整路径 + 体积 + 保留/跳过结论）写运行日志 warn，`status.conflicts` 计数，完成日志带「同名冲突 N」。
   - `abort`：保持严格中止，但报错**点名两个冲突文件**与各自体积。
2. **分页重放防御**：`id`+`pickcode` 都相同的重复条目视为同一文件重放，静默忽略，不算冲突。
3. `run()` 初始报告重置 `conflicts=0`（防上轮残留）；`sync-history` 行新增 `conflicts` 字段。

### 测试

- `test_sync.py`：① 自动保留较大——胜者 pickcode 落入 play 记录，1 个 STRM、`conflicts==1`；② 跨运行稳定 + 自愈——删除落选者后下轮 `unchanged==1` 零改写；③ `abort` 模式报错同时含 `影片.mkv` 与 `影片.mp4` 且零写入；④ 同文件重放 `conflicts==0` 正常完成。旧「冲突即中止」测试按新语义重写。
- `test_process.py`：`strm_conflict_policy` 非法值 400、auto/abort 双向保存、面板 chips 接入断言（`test_panel.py` 的 PAGES/data-field 交叉校验自动覆盖）。
- 全量 **137 项**，连续 3 轮全绿。

# 1.7.6 验证报告

目标：MovieClaw v0.35.0。

## 问题：E14 上传在最后一步「向 115 确认文件入账」中断后，「重新上传」被永久拒绝（死锁）

### 根因

1. **防歧义锁**：`upload_api` 在最终 OSS 回调（合并完成/小文件直传）**之前**持久化 `session['completing']=True`（防「响应歧义时重复上传」）。
2. **无解锁路径**：回调被打断后，网盘目录里没有该文件。`upload_queue.process()` 对每个重试都看到 `completing=True` → 抛出「上次合并结果未确认，目标目录暂未查到文件；稍后重试核对，插件不会重新上传」→ 任务失败。`requeue` 只重排 failed/completed，重排后同样撞锁——**唯一出路是删除上传记录**（连带删除 session），用户被迫手工自救。

### 修复

1. **重试恢复（`upload_queue.process()`）**：进入会话处理前已做过网盘目录去重核对（同名+大小+SHA1 匹配即短路），因此走到恢复逻辑时**回调必未落账**：
   - 分片会话完整（有 `upload_id`，或 `simple` 单请求路径）：清 `completing` 锁、保存会话、`uploader.run` 用**原会话**重驱动合并（分片未过期时零重传字节）；重驱动抛 DriveError（如 OSS uploadId 已过期）则删除会话，下次重试从头初始化。
   - 裸会话（初始化中途被打断，无 `upload_id`）：不可重驱动，直接删除会话、从空会话开始——`initupload` 以全文件 SHA1 发起，若网盘其实已有该文件（回调迟到落账）会返回秒传（status=2），**绝不产生重复文件**；若没有则全新上传（正是用户手工删除记录后发生的正确行为）。
   - 恢复动作写入运行日志（「已重置确认锁并用原会话重驱动合并」/「原会话不可恢复，将重新初始化上传」）。
2. **顺带修复（既有竞态，被新测试时序暴露）**：worker 完成分三步写（记录→recent→index），`status()` 无锁快照可看到同一 key 同时在 index 与 recent——面板同一行显示两次、批量删除对重复 key 计数不符报错。`status()` 与 `remove()` 均改为 `dict.fromkeys` 有序去重。

### 测试

- `test_upload.py` `test_ambiguous_complete_remote_match_and_conflict` 重写为新语义四分支：① 网盘已有同内容（回调落账）→ 去重短路、不触发恢复；② 分片会话完整 → 清锁、**原 uploadId/parts 复用**重驱动；③ 裸会话 → 删除后从空会话 `run({})`；④ 同名冲突行为不变。
- E2E（`test_process.py`）：注入 E14 上传记录（绕开目录扫描与 `config` 热重启触发的后台立即扫描之间的三重竞写——手动等待「扫描完成」不可靠，指纹记录以存储状态为准仍会被重启扫描覆写）→ mock 首次合并回调返回未确认 → 任务失败（「115 未确认文件入账」）→ **点「重新上传」→ 原会话重驱动合并成功**；子进程侧断言第二次合并不得重新初始化上传会话（`initupload` 计数不变）。
- 全量 **134 项**，连续 3 轮全绿。

### 待真机验证

- 真实中断恢复（分片未过期：重驱动合并秒级完成；分片已过期：重初始化 + 秒传核对）。

---

# 1.7.5 验证报告（历史）

目标：MovieClaw v0.35.0。

## 问题：播放已成功，但字幕加载很慢；且运行日志看不到任何拉取情况

### 根因

1. **无直链缓存（字幕慢的系统性根源）**：`play()` 每次请求都调 `link_signed` 现签直链。播放器读 MKV 索引/字幕轨是对同一文件**连发多个请求**（每个 seek / 字幕轨读取都重新打 `/play`），而每次签发都要过 `Drive.request` 的全局锁 + API 请求间隔（默认 1.5 秒）+ 一次 115 往返——单个请求先耗 1~3 秒，播放器开多个连接读字幕索引时被串行延迟放大，字幕加载自然慢。
2. **成功路径零日志**：`play()`/`download()` 只在 `DriveError` 时写运行日志；302 跳转、中转拉流、签发耗时全程不可见——用户面对「字幕慢」时日志一行都没有。

### 修复

1. **直链缓存（`drive_api.py`）**：`link_signed` 按 `(pickcode, ua, allow_cookie_url)` 缓存 `(url, ua, headers, proto, at)`，TTL = 新设置 `link_cache_minutes`（默认 10 分钟，1~60，面板「STRM配置」页可调），上限 64 条按时间淘汰。f=1/f=3 键隔离，绝不串用；`fresh=True`（诊断、重签）绕过并覆盖缓存。缓存命中零 115 往返、零间隔等待。CDN 直链支持缓存期内多次 Range 请求（1.7.4 诊断矩阵同一 URL 连发 7+ 次探测全通过已证明）。
2. **中转过期自愈（`proxy_stream`）**：CDN 返回 401/403/410 时 `invalidate_links` 清缓存 + `fresh=True` 重签一次再重试，并记 warn 日志——不会在死链上循环。
3. **播放拉流日志**：`play()` 302 新签发记「302 直连：名（协议 签发 Xs，f=…，host）」，缓存命中每文件每分钟一条「（复用缓存直链，host）」防刷屏；中转新签发记「中转播放：名（协议 签发 Xs）」；`proxy_stream` 完成时记「中转拉流：名，HTTP 状态，字节，耗时」，中断标「（中断）」（HEAD 不记；日志写入 wait_for 3 秒限时，绝不挂起流回收）；`download()` 记下载直链签发/中转。`drive.last_sign` 回传 cached/proto/elapsed 供日志取用。
4. 缓存对 `fetch_cloud_sidecar`（附属下载）同样生效。

### 测试

- `test_drive.py` 新增 2 项：`test_link_cache_reuses_and_invalidates`（同 pickcode+UA 零请求命中、按 UA 区分重签、fresh 绕过、TTL 过期重签、invalidate 后重签、f=1/f=3 键隔离）、`test_link_cache_rejected_by_cdn_invalidates_and_resigns`（CDN 403 → 缓存整体失效 → fresh 重签拿到新直链）。
- E2E 新增断言：302 播放与中转拉流后 `/log/list` 必须出现「播放」来源的 `302 直连` 与 `中转拉流` 事件（中转完成事件为流结束后异步写入，轮询等待）。
- 全量 **134 项**（132 + 2），连续 2 轮全绿。

### 待真机验证

- 字幕加载实际耗时（缓存命中后 seek/字幕轨请求应毫秒级响应）；
- 115 真实直链有效期与默认 10 分钟缓存的匹配度（若播放中偶发直链过期，调小「直链缓存时间」）；
- 中转模式 CDN 403 自动重签链路。

---

# 1.7.4 验证报告（历史）

目标：MovieClaw v0.35.0。

## 问题：1.7.3 真机播放诊断显示 m115 签发成功（f=1、cdnfhnfile.115cdn.net），但 CDN 拉流探测 6 项全部 403

### 根因

诊断报告本身就是证据链：

1. **签发已通**：`m115·空 UA` 成功签出 f=1 直链——1.7.3 的 m115 协议修复生效。
2. **f=1 直链绑定签发 UA**：播放器 UA（Infuse）、凭据 UA 探测全 403，证明该直链并非 115driver #80 所述「空 UA 签发=不绑 UA」，而是**严格绑定签发 UA（本例为空）**。
3. **「空 UA」探测是假的**：探测代码 `if ua:headers['User-Agent']=ua`——UA 为空字符串时根本不设置头，httpx 补发默认 `python-httpx/0.28.1`（本机 httpx 0.28.1 实测确认），CDN 看到的是 python-httpx UA → 403。三处同款 bug：诊断探测、`proxy_stream`（`if sign_ua:`）、`fetch_cloud_sidecar`（`if sign_ua:`）——**服务器中转模式在 1.7.3 上同样会 403**，只是用户还没试。
4. **遗漏 `url.header`**：downurl 响应的 `url` 对象带 `header` 字段（115driver `DownloadUrl.Header`、OpenList `Link()` 原样回传给消费端），CDN 常要求这组头；插件从未读取。

### 修复

1. **UA 精确重放**：所有服务器侧拉流（`proxy_stream`、`fetch_cloud_sidecar`、诊断探测）始终 `headers['User-Agent']=sign_ua`——空 UA 也显式发空头（httpx 实测：显式空串发 `''`，不设则发 `python-httpx/x`）。同时附加账号 Cookie（alist `buildDownloadHeaders` 生产行为）。
2. **302 按请求方 UA 签发（alist `Link()` 生产模型）**：`link_signed` 收到非空 UA 时策略链改为 `[m115·请求方UA, rsa·请求方UA, m115·空UA, rsa·空UA]`——播放器跟随 302 后用自己的 UA 拉 CDN，与签发 UA 天然一致。内部拉取（ua=None）仍空 UA 优先（我们精确重放签发 UA）。
3. **记忆按 UA 匹配**：`_ok` 只在 UA 一致时复用——否则 A 播放器的记忆会给 B 播放器签出绑定错 UA 的直链。
4. **`url.header` 提取**：`_parse_sign_content` 返回 `(url, ua, sign_headers)`，字符串键值过滤；中转/附属下载/中转模拟探测重放；诊断报告显示响应头字段名。
5. **诊断升级**：CDN 探测新增「服务器中转模拟」（签发 UA＋url.header＋Cookie——中转能否播一目了然）与「签发 UA 精确重放」行；`PlayTestPayload` 新增可选 `ua` 参数，可按真实播放器 UA 实测 302 链路。

### 测试

- `test_drive.py`：新增 `test_sign_extracts_url_header_for_cdn_fetch`（header 提取+脏值过滤+策略链透传）；`test_memo_same_ua_shortens_later_signings`（同 UA 复用、**换 UA 必须重新走链**）；`test_sign_uses_m115_first`/`test_m115_rejected_falls_back_to_rsa` 改按请求方 UA 断言。
- E2E：中转模拟断言为第一行、CDN 行数 6→7；新增播放器 UA 探测断言（9 行、`播放器 UA` 标签、UA 换行注入 422）。
- 全量 **132 项**（131 + 1 净新增），连续 3 轮全绿。

### 待真机验证

- 302：播放器 UA 签发 + 同 UA 拉流（诊断可选参数可直接实测）；
- 服务器中转：签发 UA 精确重放 + url.header + Cookie 组合；
- 诊断报告里「服务器中转模拟」行与真实中转播放的一致性。

---

# 1.7.3 验证报告（历史）

目标：MovieClaw v0.35.0。

## 问题：1.7.2 真机 302 播放仍失败，运行日志显示「直链签发失败（空UA：115 下载响应解密或解析失败；UA MovieClaw-Android/0.1.0...：115 下载响应解密或解析失败）」

### 根因

用户真机日志表明：响应体是可解析的明文 JSON（`r.json()` 成功、`state=true`），失败发生在 `rsa_decrypt(value['data'])`。对照三份生产实现源码后确认 **115 的 `chrome/downurl` 接口有两套请求/响应加密协议**：

| | RSA 协议 | m115 协议 |
|---|---|---|
| 请求加密 | RSA 公钥加密 payload，base64 | 随机 16 字节密钥 + 双重 XOR 变换（密钥派生表 + 固定 client key）+ RSA 信封，base64 |
| 响应解密 | `data` 字段 RSA 解密 | `data` 字段 = X(reverse(X(result, D(keyA,4))), D(keyS,12))，keyS 从 RSA 块头 16 字节取出 |
| 生产使用方 | p115client（全部函数） | **115driver `DownloadWithUA`（alist/OpenList Link() 的实际依赖）** |

插件此前只实现了 RSA 协议；115 服务端按**请求方使用的协议**回对应密文——alist 生态全走 m115，说明该端点对 m115 请求稳定可用。真机上 rsa_decrypt 必失败即因协议不匹配。

### 修复

1. **`p115_m115.py`（新增，逐行移植 115driver m115）**：
   - `generate_key()`：随机 16 字节（`crypto/rand` 等价）；
   - XOR 种子表（64 项固定派生表）、`_xor_derive_key(key, n)`（`D(x,x[i],x[(i+1)%len],n)`）、`_xor_transform`（Go 版头部长度取模边界行为逐字节对齐）、`xorDeriveKey/xorTransform` 公开别名；
   - RSA：与 `p115_rsa.py` 同源的 115 公钥（e=65537），`_rsa_encrypt`（标准加密：零填充至 128 倍数 + 0x00 分隔符）/`_rsa_decrypt`（服务器私钥加密、公钥解：按分隔符提取并去填充）；
   - `encode(payload, key)` / `decode(b64, key)`：完整请求/响应信封编排。
   - **验证方式**：RSA 客户端加密与服务器解密本就不互逆（协议本质），用「e=1 特殊信道」验证块结构/填充/提取逻辑（1/50/117/118/234/300 字节边界全过），用「RSA 恒等替换」模拟服务器全往返验证 XOR 链编排（请求可被服务器正确解出、响应可被客户端正确解出）。
2. **`drive_api.py`**：
   - `sign_once(..., protocol)`：请求按协议加密（m115/RSA）；响应解析完整对齐 p115client 契约——`json_maybe_decrypt_loads`（响应体可能是 ECDH 信封：AES-CBC 固定密钥+LZ4，非 `{`/`[`/`"` 首尾即密文）、`state=true` 且 `data` 为空→明确报错「文件可能已被删除、转码中或被限制下载」、`url:false` 容错、解密失败报错带响应体字节数+首字节；
   - `link_signed` 策略链升级**协议×UA 二维**：m115·空UA → m115·请求方UA → RSA·空UA → RSA·请求方UA（内部拉取回退凭据UA），逐项尝试直到签出；`_sign_ok` 记忆成功组合，后续单请求；
   - errno 透传 + 释义。
3. **`p115_plugin.py` 播放诊断**：签发探测同样按协议矩阵（m115 空 UA/凭据 UA → RSA 空 UA/凭据 UA），日志取**成功项**的 f/host（此前取 `sign[0]` 在非首选组合成功时 KeyError 500）。
4. **附属下载**：`fetch_cloud_sidecar` 经 `link_signed` 自动复用成功协议与签发 UA。

### 测试

- 新增 `tests/test_m115.py`（6 项）：密钥生成长度、XOR 自逆、头部长度取模边界（5 字节）、e=1 信道块结构（含 117/118 填充分界）、模拟服务器全往返（请求解出+响应解出）、RSA 信封加密输出 128 倍数。
- `tests/test_drive.py` 新增：ECDH 信封响应体解析（真实 `aes_cbc_encrypt`+LZ4 块构造，修正 bytearray→bytes）、m115 签发线格式（data 前缀 = RSA 信封）、协议矩阵回退（m115 失败 → RSA 成功，报告含失败项）、策略记忆（成功组合后单请求）。
- E2E：downurl mock 兼容明文 JSON+打桩路径；播放诊断断言按「存在成功项」而非固定下标。
- 全量 **131 项**（119 + 12 新增），连续多轮全绿。

### 待真机验证

- m115 协议签发真实可用性（策略链首选）；
- 302 播放恢复情况——若仍异常，点「网盘文件 → 诊断」，报告将直接显示哪套协议+哪个 UA 可用；
- RSA 协议在真机是否仍被接受（作为兜底保留）。

---

# 1.7.2 验证报告（历史）

目标：MovieClaw v0.35.0。

## 问题：1.7.1 升级后控制台整页失效（扫码不加载、页面无法切换），用户已回退

### 根因

面板「播放诊断」的签发报告渲染行（`for(let s of r.sign)lines.push(...)`）少写一个右括号——JS 语法错误使浏览器拒绝执行**整个**内联 `<script>`。面板的全部交互（初始化、导航、扫码轮询）都在这段脚本里，因此整页功能瘫痪；后端接口本身无恙。以 `node --check` 提取校验面板脚本即可稳定复现：

```
/private/tmp/panel_check.js:178
  for(let s of r.sign)lines.push(...(s.error||'')));
SyntaxError: missing ) after argument list
```

### 修复

1. 该行重写为无深层嵌套括号的等价写法（`✓/✗ + label + f/host 或 error`），`node --check` 通过。
2. **防回归**——新增 `tests/test_panel.py`（3 项静态校验，全部进入默认测试套件）：
   - `test_script_js_syntax`：提取面板内联 JS 用 `node --check` 校验语法（无 node 时跳过，本机 node 22 必跑）；
   - `test_script_element_ids_exist`：JS 中 `querySelector('#id')` 引用的元素 ID 必须都在 HTML 中定义，防止 boot 阶段 `null` 崩溃；
   - `test_pages_fields_have_controls`：JS `PAGES` 声明的字段与 HTML `data-field` 控件双向交叉校验，防止读档/保存丢字段。
3. 顺手修复两处既有 E2E 偶发（时序竞争，非产品缺陷）：
   - 同步历史断言「第一条必须是 full」→「存在一条成功的 full」（上传完成触发的生活事件增量核对可能先落历史）；
   - 上传管理断言盯 `items[0]` → 等待全部记录静默后按 key 定位（items 顺序由队列+历史拼接，requeue 的秒传可能尚未完成时 `items[0]` 是另一条已完成记录）。

### 回归验证

- 全量测试 **119 项**（116 原有 + 3 新增面板校验），**连续 6 轮全绿**（此前 1.7.1 的失败为本轮新测试暴露的既有偶发，已修）。
- `node --check` 面板脚本通过；JS 引用 ID 32 个全部存在于 HTML；PAGES 字段与控件双向一致。
- 打包脚本兼容性检查通过，1.7.1 → 1.7.2 仅一处 JS 行变化，无授权/契约变更。

---

# 1.7.1 验证报告（历史）

目标：MovieClaw v0.35.0。

## 问题：1.7.0 后用户反馈「302 仍不成功」（并提示参考 alist 的 302 实现）

### 复核结论（重新对照三份生产实现源码）

- **alist/OpenList `Link()`**（drivers/115/driver.go）：`DownloadWithUA(file.PickCode, args.Header.Get("User-Agent"))` —— **按请求方 UA 签发**，且把 `DownloadInfo.Header`（请求头 + 响应 Set-Cookie）随链接返回供代理模式附加。这证明 chrome/downurl **接受任意请求方 UA**（否则 alist 的 302 生态早已不可用），1.7.0 「proapi 拒非 115 UA」的推断仅适用于分享接口族（115driver 2026-09 的 `shareDownurlUA` 处置），对自有文件 downurl 并不成立。
- **p115client `download_url_app`**：默认空 UA 签发；`f=1` 直链与签发 UA 绑定（其 download_url 文档明确要求下载请求 UA 与签发一致）。
- **115driver `DownloadWithUA`**：响应的 `Header` 由「实际发送的请求头 + 响应 Cookie」构成——即 CDN 拉流方需要补齐的头部；alist 驱动注释直接写明「115 CDN 要求账号 Cookie……缺 Cookie 的代理下载会 403 no cookie value」。
- **推论**：空 UA 与请求方 UA 两种签发在生产中各有成立场景，无法在真机外断定用户账号拒绝哪一种；失败也可能发生在 302 之后的 CDN 拉流段（UA/Cookie 绑定）——而该段失败此前完全无日志。因此 1.7.1 不再押注单一策略，改为「策略链 + 中转兜底 + 一键取证据」。

### 修复

- **drive_api.py**：拆出 `sign_once(pickcode, ua, allow_cookie_url)`（单次 downurl，返回 `url + 签发 UA`）；`link_signed(pickcode, ua)` 策略链——空 UA 优先（UA-free 直链，任何拉流方可播），被拒回退请求方 UA（alist 模型：302 后同一客户端拉流，UA 一致）；内部拉取回退凭据 UA。空 UA 成功被记忆（`_empty_ua_ok`），后续签发单请求完成。`f=3` 直链仅 `allow_cookie_url`（服务器侧拉取）时放行，302 路径仍拒绝并指引切换中转。`link()` 保留为兼容包装（内部语义，f=3 放行）。
- **sync_engine.py**：`fetch_cloud_sidecar` 改用 `link_signed`，拉取时**复用签发 UA**、f=3 时附加账号 Cookie——修复此前「签发 UA 与拉取 UA 不一致必致 403」的隐性缺陷。
- **p115_plugin.py**：新增设置 `play_mode`（`302` | `proxy`）。`/play/{fid}`：302 模式按请求方 UA 走策略链后 302；`proxy` 模式经 `proxy_stream` 中转（`Range` 透传、签发 UA + f=3 附 Cookie、状态码与 content-* 头透传、HEAD 支持、断连清理）。新增 `POST /play/test` 播放诊断：签发探测（空 UA/凭据 UA）+ CDN 拉流矩阵（3 UA × 2 Cookie，Range 0-1023），结论写运行日志。播放失败日志补记提取码。`/download/{pickcode}` 同策略链，代理模式同样生效。
- **panel.py**：STRM 配置页新增「播放方式」chips；网盘文件页每个文件行新增「诊断」按钮 + 报告弹窗（签发 ✓/✗ 与 CDN 探测矩阵直读）。
- 签名密钥约束确认：宿主 `PluginRoutes.sign` 对**全部 query 参数**做 HMAC，故代理开关只能做成设置项（不能走 `?proxy=1`，否则破坏既有 STRM 签名）。

## 测试

Python 3.13.12（托管运行时，独立 venv），`PYTHONPATH=".:tests:../source-v035/src"`：**116 项全部通过**（1.7.0 为 113 项）。新增：空 UA 被拒回退请求方 UA、空 UA 成功记忆（后续单请求）、f=3 内部放行/302 拒绝、附属下载带签发 UA；E2E 新增 `/play/test`（签发 f/host 断言 + 6 项 CDN 矩阵）、play_mode 切换 proxy 后 GET/HEAD/Range 透传拉流、非法 play_mode 400、面板 play_mode/playTest 断言。check_compat 通过。

## 未验证边界

- 用户账号上 115 对空 UA / 播放器 UA 签发的真实接受度（策略链保证两者都被尝试；运行日志与播放诊断会回报事实）。
- 真实 115 CDN 的 UA/Cookie 绑定矩阵（诊断按钮一键取证据；若拉流探测有 ✗，切换「服务器中转」即可播）。
- 代理模式的长时间大码率拉流稳定性（httpx 流式透传，读超时 120s 按块计算；建议 302 正常后切回直连）。

---

# 1.7.0 验证报告

目标：MovieClaw v0.35.0。

## 问题 1：用户反馈「302 功能异常，无法正常播放」

### 根因

`drive_api.py link()` 用**播放器的 User-Agent** 去签发 `proapi.115.com/app/chrome/downurl`。两份生产实现的证据链表明这是错误设计：

- p115client 0.0.9.7.2 `download_url_app`（我们锚定的契约）默认 `headers["user-agent"] = ""`（空 UA）；
- SheltonZhu/115driver `Download()` → `DownloadWithUA(pickcode, "")` 同样默认空 UA，其 #80 修复明确说明：**空 UA 签发换回的 CDN 直链不绑定 UA**（为让 mediainfo 等无 UA 工具能下载）；若空 UA 请求被 HTTP 库改写成默认 UA，直链就会变成 UA-bound —— 反向证明签发 UA 决定直链绑定；
- 115driver 2026-09 对分享直链接口的处置：**该接口族拒绝非 115 客户端 UA**（普通浏览器/下载器 UA），必须回退 `UA115Browser`，而「返回的 CDN url 不绑定 UA」。

旧实现用播放器 UA（Emby/Infuse/VLC 等，均非 115 UA）→ downurl 请求被拒（990001 族错误）→ `/play` 与 `/download` 全部 502，302 无法播放。1.6.2 引入「凭据与签发 UA 绑定」修复后，该问题从「偶发」变为「必然」（凭据 UA 固定为 115Browser，与播放器 UA 永远不一致）。

### 修复

- `link(pickcode)`（签名去掉 ua 参数）：以**空 UA** 签发 downurl，取回不绑定 UA 的 CDN 直链，任意播放器可跟随 302。仍保留 `f=3`（需 CDN Cookie 的直链）的明确拒绝。
- 防御 115 的 `url:false` 响应（115driver info.go 2026-07 增加的兼容分支）：给出「115 未返回此文件的下载地址（文件可能已被删除或被限制下载）」而非解密解析失败。
- `/play/{fid}` 与 `/download/{pickcode}` 签发失败写入运行日志（来源「播放」），面板可定位。
- `sync_engine.fetch_cloud_sidecar` 调用点同步改签名。

## 问题 2：上传功能完善（批量 + 单条管理）

- **upload_queue.py**：新增 `requeue(keys)`（失败/已完成 → 重新排队；秒传 SHA1 去重保证安全）与 `remove(keys)`（删记录，不动本地/网盘文件；**running 任务拒绝**，因 worker 会在完成时回写记录）；`retry(key)` 改基于 requeue；记录新增 `enqueued_at`/`done_at`；`status()` 行新增 `relative`/`done_at` 字段（查看上传情况）。
- **p115_plugin.py**：新增 `POST /uploads/delete`、`POST /uploads/requeue`（单条与批量共用，keys ≤100、hex 校验）、`POST /uploads/clear`（按状态批量清除历史 completed/failed）；操作写入运行日志。
- **panel.py**：监控上传页队列升级为可管理列表——状态筛选 chips（全部/等待中/上传中/失败/已完成）、勾选框（**跨自动刷新保持选中**）、批量「重新上传所选 / 删除所选记录」、一键「清除已完成 / 清除失败记录」、单条「重新上传 / 删除」按钮，行内显示相对路径、进度、完成时间与失败原因。

## 测试

Python 3.13.12（托管运行时，独立 venv），`PYTHONPATH=".:tests:../source-v035/src"`：**113 项全部通过**（1.6.12 为 109 项）。新增/调整：downurl 空 UA 线断言（原播放器 UA 断言翻转）、`url:false` 明确报错、f=3 拒绝保留、队列历史 requeue/remove、running 任务保护、E2E 覆盖 requeue→秒传完成→批量删除→clear→入参校验、面板管理界面断言。check_compat 通过。

## 未验证边界

- 真实 115 账号上空 UA 签发的线上行为（两份生产实现默认即空 UA，风险低）；若线上仍异常，运行日志「播放」条目会给出 115 真实错误码。
- 真实播放器跟随 302 后的 CDN 拉流（取决于「空 UA → 不绑定 UA 的直链」契约在用户账号上的表现；`f=1`/`f=3` 的实际出现频率）。
- 大体积视频经 chrome/downurl 的 200MB 限制（`errno 300104`）在用户账号上的表现（ERRNO_HINTS 已有释义；受限时面板日志会显示）。
- 删除「已完成」记录后再次扫描重新入队并秒传的线上行为。

---

# 1.6.12 验证报告

目标：MovieClaw v0.35.0。

## 问题：用户实例目录扫描报「目录没有读取授权……插件不会绕过路径权限」

用户部署布局：`/media/download`（下载）→ `/media/link`（硬链整理）→ `/media/strm`（STRM 输出）。`/media/download` 与 `/media/link` 不在插件授权清单内（1.6.8 只申请了 /media/strm），读取被宿主沙箱拒绝；且旧报错是固定文案，未透传宿主自带的精确路径/授权信息。

## 修复内容

- **manifest paths 新增两个绝对路径 rw 授权**：`/media/download`、`/media/link`。清单现为 library(rw) + staging(read) + /media/strm(rw) + /media/download(rw) + /media/link(rw)。
- **directory_watch.py 读取拒绝报错改为透传式**（与 1.6.4 写探测同款哲学）：保留宿主 PermissionError 的原始信息（前 300 字符），附两条排查指引——① 插件路径授权（1.6.12 已默认申请三路径，升级需批准；其他路径单独授权或改用媒体库根/整理规则目录）；② OS 层 MovieClaw 运行用户可读（chown/chmod）。
- **sync_engine.py 写探测指引同步更新**（提及三个默认申请路径）。
- **推荐配置写入 README**：整理目录配 `/media/link`（硬链接后的稳定成品，避开下载半成品），STRM 目录配 `/media/strm`，下载器写 `/media/download`。

## 用户侧操作（3 步）

1. 上传安装 1.6.12 → 2. 在「设置 → 插件」批准新授权（授权清单应出现 /media/download、/media/link）→ 3. 确认 OS 层三个目录允许 MovieClaw 运行用户读写（`chown -R <运行用户> /media/download /media/link /media/strm`）。

## 测试

109 项全部通过（授权为清单层变更；`test_scan_permissions_and_pause` 断言翻转为「透传原始信息 + 指引含默认路径」）。check_compat 通过。

## 未验证边界

- 用户实例批准新授权后三个目录的实际读写（若 OS 层仍拒绝，报错带 errno 13 与路径）。
- 其余 1.6.11 未验证项不变。

---

# 1.6.11 验证报告

目标：MovieClaw v0.35.0。

## 变更：按用户要求，运行日志加入对 115 事件的监控

- **life_strm.py**：`life_fingerprint()` 拆出 `life_feed()`（返回 fingerprint + 原始事件行，行为不变）；新增 `log_new_life_events(rows)` 与 `life_event_text(row)`；`tick()` 在计算指纹处（同步触发门控**之前**）接入——新事件无论是否触发本轮同步都会写日志，不漏记。
- **记录语义**：store 新键 `life-seen-ids`（最近 64 条事件 id，去重依据）；**首次观察只记一条「开始监控 115 上传事件（已忽略历史 N 条）」**，不回放历史刷屏；之后每轮新增事件逐条 `检测到 115 网盘上传：<文件名>（体积）`（file_name/name 缺失时回退 `文件 #<file_id>`，体积按 GB/MB/KB 自适应）；单轮超过 20 条只逐条显示 20 条 + 一条「另有 N 条」合并计数；写入尽力而为，绝不影响同步主流程。
- **联动**：与「监控 115 生活上传事件」设置同开同关（关闭时不请求、不写日志）；事件仍同时作为增量同步触发信号（既有行为不变）。
- **面板**：运行事件框标题来源清单更新为「系统/登录/配置/同步/上传/115事件」。

## 测试

Python 3.13.12（托管运行时，独立 venv），`PYTHONPATH=".:tests:../source-v035/src"`：**109 项全部通过**（1.6.10 为 106 项）。本版新增 3 项：首次标记不回放历史 + 新事件入日志（含体积格式化）+ seen-ids 更新；无文件名字段时回退 file_id；life_events 关闭时不写任何 115 事件日志。check_compat 通过。

## 未验证边界

- 真实 115 behavior/detail 行的字段名（file_name/file_size 取值做了防御性兜底：name、file_id；线上若用其他字段名，日志仍会有事件条目，仅名称显示为回退形式——不影响触发与同步）。
- 其余 1.6.10 未验证项不变。

---

# 1.6.10 验证报告

目标：MovieClaw v0.35.0。

## 变更：按用户要求，监控上传新增「排除目录」设置

- **Settings 新增 `upload_excludes`**（逗号分隔字符串）：校验只允许整理目录内的相对子目录（拒绝对 / 开头、`..`、反斜杠、超 80 字符），自动去重、容忍中英文逗号与尾部斜杠；加入 EDITABLE（保存热生效）。
- **directory_watch.py**：新增 `parse_excludes()`/`is_excluded()`；`scan()` 目录遍历时命中排除项的子目录整棵跳过（不 stat 其文件、不入队、附属文件同样跳过）。匹配语义：**单级名字在任意层级生效**（如 `未完成` 排除根下与任意深度的同名目录）；**多级路径从整理目录根锚定**（如 `下载/临时` 只排除根下该路径，不影响 `下载/未完成` 这类仅同名的其他路径）。
- **面板**：监控上传页新增「排除目录」标签输入（回车添加、× 删除，与视频后缀同款交互），readField/loadConfig/PAGES 全链路接入。

## 行为边界（有意设计）

- 只影响**新扫描**：保存前已入队的文件仍按原任务处理（不静默丢弃用户已确认的队列任务）。
- 排除只作用于目录；整理目录根下的散文件不受影响。

## 测试

Python 3.13.12（托管运行时，独立 venv），`PYTHONPATH=".:tests:../source-v035/src"`：**106 项全部通过**（1.6.9 为 103 项）。本版新增 3 项：端到端扫描排除（5 个目录中只有非排除子树的 2 个文件入队）、匹配语义单测（单级任意层级/多级根锚定/同名不误伤）、Settings 校验单测（拒绝绝对路径与 ..、去重归一）。check_compat 通过。

## 未验证边界

- 真实实例上排除目录的实际效果（语义与 mock 测试一致，预期直接可用）。
- 其余 1.6.9 未验证项不变（/media/strm 授权批准后的实际落盘、movie.nfo 实传、大文件真实 multipart 等）。

---

# 1.6.9 验证报告

目标：MovieClaw v0.35.0。

## 变更：按用户要求，日志单独归到一个栏，方便发现问题

### 后端

- 新模块 `runlog.py`：`log_event(store, level, source, message)` 写插件私有存储 `runtime-log`（环形缓冲，最近 100 条，level ∈ info/warn/error，source ∈ 系统/登录/配置/同步/上传）；写入尽力而为，永不影响主流程。
- 事件接入点：插件启动（系统）；扫码登录成功/失败（登录）；配置保存与导入（配置，`apply_settings` 增加 origin 参数区分）；同步完成/失败（`life_strm.record_history` 同步双写 sync-history 与 runtime-log）；上传完成/失败（`upload_queue.worker`）。
- 新路由 `GET /log/list`：一次返回 `{events, history, failures}`（failures 取自上传队列 state=='failed' 的任务）。

### 面板

- 侧栏新增「运行日志」导航（网盘文件之下）；新增 `page-logs`：运行事件（时间 + [来源] + 内容，错误红/警告橙）、同步历史（自增量页移入）、上传失败（文件名 + 原因 + 重试按钮，复用 `/uploads/retry/<key>`）。
- 轮询合并：5 秒定时器以 `refreshLogs()`（单次 `/log/list`）替代原 `refreshHistory()`。

## 测试

103 项全部通过。E2E 新增断言：全量同步完成后 `/log/list` 返回非空 history、含 系统/同步/上传 三类事件、failures 为空；面板 HTML 含 `page-logs`/`runtime-log`/`upload-failures`/`refreshLogs`/`log/list`。check_compat 通过。

## 未验证边界

- 真实实例上 runtime-log 环形缓冲的长期增长表现（100 条上限已限制存储体积）。
- 其余 1.6.8 未验证项不变（/media/strm 授权批准后的实际落盘等）。

---

# 1.6.8 验证报告

目标：MovieClaw v0.35.0。

## 问题：用户实例全量同步报「STRM 目录写入被拒绝：插件 p115-library 没有获得访问 /media/strm 的授权」

1.6.4 的快速写探测按设计工作：枚举网盘之前秒级失败，并完整透传宿主沙箱的原始报错——环境层原因就此定位：**STRM 目录 /media/strm 在媒体库根目录之外**，而插件清单此前只申请了 library（媒体库根，rw）+ staging（只读）。

## 修复内容

- **manifest 新增绝对路径授权**（宿主 parse_grants 支持库别名与 `/` 开头绝对路径，逐字核实 `plugin_files.py`）：`{ path = "/media/strm", mode = "rw" }`。
- **升级流程**（宿主 packages.py 核实）：上传的新版包先进入 `<版本>.pending` 待批准状态，用户在「设置 → 插件」批准后，新 paths 授权生效。
- `sync_engine.py` 写探测报错的排查指引同步更新（说明 1.6.8 已默认申请 /media/strm，批准即可；其他根外路径仍需单独授权或改用库内目录）。

## 用户侧仍需确认（OS 层）

`/media/strm` 需允许 MovieClaw 运行用户写入：`chown -R <运行用户> /media/strm` 或 `chmod -R u+w`（Linux 目录属 root 常见）。插件授权解决的是宿主沙箱层，OS 层权限独立。

## 测试

103 项全部通过（授权声明在清单层，逻辑无变化；写探测报错文案更新不触碰既有断言）。check_compat 通过。

## 未验证边界

- 用户实例批准新授权后 /media/strm 的实际写入与 STRM 落盘（若 OS 层仍拒绝，新报错会带 errno 13 与路径）。
- 其余 1.6.7 未验证项不变。

---

# 1.6.7 验证报告

目标：MovieClaw v0.35.0。

## 变更：按用户要求，控制台整页换肤为 MovieClaw 官方页面风格

对照宿主前端源码（apps/web：globals.css 的 :root token、.css-glass/.btn-glass/.btn-accent/.field-shell 材质、settings-ui.tsx 的卡片/行/按钮规范）逐项落地：

- **色彩 token 全面对齐**：纯黑底 #000；文本 #f3f5f9 / muted 62% / faint 36%；描边 white/8%；冷银强调 #cdd6e6（soft 14%、聚焦环 rgba(200,218,255,.55)）；玻璃填充 4%/7.5%/12%；状态色 --ok #4ade80 / --info #7fb0ff / --warn #f5c451 / --danger #ff6b6b。
- **材质复刻**：卡片 = .css-glass（16→12px 圆角、顶部渐隐高光、发丝描边、内嵌高光线）；主操作按钮 = .btn-accent 亮银渐变（#f6f8fc→#ccd6e6、深墨字 #141821、按下 scale .96）；次要按钮 = .btn-glass 胶囊；输入框 = .field-shell 聚焦语义（容器一体冷银指示环、无嵌套双环）。
- **浮层对齐 .menu-surface**：弹窗与 Toast 改纯色近实底 rgb(21,23,29/.96) + 单层投影 + 发丝内线（玻璃 = 界面结构、纯色 = 临时浮层的两层语言）。
- **导航**：侧栏 rgba(22,22,24,.55) + blur(20px)，导航项 muted、hover 玻璃填充、激活 accent-soft + 内嵌发丝环。
- **清理**：JS 中 4 处亮色主题硬编码灰（#4e5560/#8a8f99）改为语义变量。
- **范围控制**：类名与 JS 结构零改动（换皮不动逻辑）；布局、移动端适配、全部功能不变；二维码图片保留白底（保证扫码对比度）。

## 测试

103 项全部通过（换皮无逻辑变化；/panel 200 与 E2E 守卫断言不变）。check_compat 通过。

## 未验证边界

与 1.6.6 相同（movie.nfo 实传、大文件真实 multipart、STRM 写入权限修复后的实际落盘等）。

---

# 1.6.6 验证报告

目标：MovieClaw v0.35.0。

## 变更：按用户要求移除面板「cookie对应设备类型」下方的提示文案

该 note（115 同类型设备互踢建议、电脑端已下架说明、网络诊断用途介绍）按用户要求精简移除。功能性防御保留：选到已下架的电脑端类型（os_windows/os_mac/os_linux）时仍被校验拦截并给出明确报错（login.py / p115_plugin.py，测试覆盖）。

## 测试

103 项全部通过（与 1.6.5 相同——纯文案删除，无逻辑变化）。check_compat 通过。

## 未验证边界

与 1.6.5 相同（movie.nfo 实传、大文件真实 multipart、STRM 写入权限修复后的实际落盘等）。

---

# 1.6.5 验证报告

目标：MovieClaw v0.35.0。

## 问题：用户实例「movie.nfo 上传不成功」

1.6.4 后登录与同步已可用，附属文件（movie.nfo，通常仅几 KB）上传失败。上传模块自 1.0 起仅在 mock 环境验证过，这是首次在真实 115 上运行。

## 根因（p115oss 0.1.2 源码核实，两处与生产实现的偏差）

1. **抽查范围校验过严**：115 对非秒传文件返回 status=7 要求内容抽查（sign_check 'a-b'）。p115oss 的 read_range 是 `seek(start); read(end-start+1)`——**短读到 EOF 照常应答**；我们要求 `b < size` 否则报「115 校验范围不合规」。小文件（几 KB）极可能被要求超出文件大小的抽查范围 → 上传中止。
2. **小文件走了非生产路径**：阿里云 OSS multipart 分片最小 100KB（p115oss determine_partsize 注释明示「分块大小至少 100 KB」）。OpenList（util.go UploadByOSS 用 bucket.PutObject + CallbackResult）与 p115oss（默认 partsize=0 走 oss_upload 单请求 PUT）对小文件都用 **PutObject + 回调**；我们对所有尺寸一律 multipart（init session → 单片 <100KB → complete），小文件正好踩中分片尺寸下限。

## 修复内容

- `upload_api.py init()`：sign_check 只拒绝负数/倒置范围；读取量 `min(b-a+1, size-a)`，EOF 短读即止（p115oss read_range 同款语义）。
- `upload_api.py` 新增 `MIN_PART_SIZE=1024*100` 与 `simple_upload()`：size < 100KB 时单请求 `PUT {bucket}.oss-cn-shenzhen/object`，携带 base64 的 `x-oss-callback`/`x-oss-callback-var`（OpenList/p115oss 同款头），回调响应校验 state；会话标记 `simple`，中断重试按同一对象键幂等重传，不重新 init；发送前持久化 `completing` 防重复上传。≥100KB 文件维持 multipart（可断点续传）不变。
- `test_process.py` E2E mock 适配：直传 PUT 必须带回调头；`uploaded` 标志在直传完成时置位（原由 multipart complete 置位）。

## 测试

Python 3.13.12（托管运行时，独立 venv），`PYTHONPATH=".:tests:../source-v035/src"`：**103 项全部通过**（1.6.4 为 101 项）。本版变化：

- 新增 `test_small_file_uses_simple_put_with_callback`：14 字节文件单请求直传（无 multipart 初始化、无 partNumber），回调头 base64 内容逐项断言，请求体与文件字节一致，进度回调齐全；中断重试同一对象键幂等重传且不再 init。
- 新增 `test_upload_init_challenge_range_beyond_eof`：5 字节文件收到 '2-99999' 抽查范围 → sign_val = sha1(短读内容)，不报错（p115oss 语义）。
- `test_multipart_wire_resume_and_no_cookie_to_oss`：monkeypatch MIN_PART_SIZE=10 保留 multipart 线缆协议测试。
- E2E：小文件直传后云端可见、增量同步生成 STRM、播放链路保持全绿。

## 未验证边界（待真实实例）

- movie.nfo 真实上传（本版两处修复均对齐 p115oss/OpenList 生产实现，预期直接可用）；若仍失败，监控上传页会显示带原始信息的错误，请把具体报错反馈。
- 视频大文件的真实 multipart 上传（走 OSS 真分片，待首次大文件上传验证）。
- 其余 1.6.4 未验证项不变（STRM 写入权限、多映射大目录、CDN 附属下载等）。

---

# 1.6.4 验证报告

目标：MovieClaw v0.35.0。

## 问题：增量同步报「STRM 生成失败（PermissionError），请检查目录写入授权」，写入 0 · 未变化 0

登录链路在用户实例上已完全打通（1.6.3 后同步真实运行），首次到达写入阶段即遇权限失败。但兜底 catch 把宿主 PermissionError 压成笼统一句，丢掉了具体路径与原因，无法定位是「插件授权范围」还是「操作系统权限」问题。

## 根因（宿主源码核实）

MovieClaw v0.35.0 的插件文件接口（`movieclaw_api/services/plugin_files.py`）是授权沙箱：每次操作先 realpath 再检查是否落在批准的根内，越权时抛出的 PermissionError **自带精确信息**（`插件 {id} 没有获得访问 {path} 的授权` / `对 {root} 只有读权限`）；操作系统层面的拒绝（目录属 root、只读挂载）则带 errno 与路径。我们的 `life_strm.tick()` 兜底把这些全部丢弃，替换为固定文案——诊断信息损失 100%。

## 修复内容

- `sync_engine.py sync_mapping()`：**快速写探测**——非 dry-run 时，在枚举网盘之前先 `makedirs(base)` + 写入/删除一个 `.p115-probe-*` 临时文件；PermissionError 转为 DriveError，完整保留宿主原始报错，并附两条排查指引（插件授权 library=读写、STRM 目录在媒体库根之下；操作系统层面 MovieClaw 运行用户可写，Linux 需 chown/chmod）。探测在枚举前执行，大目录场景秒级失败，不再白跑整个网盘。
- `sync_engine.py run()`：新增 `except PermissionError` 分支——探测之后中途出现的写入拒绝（如子目录只读）同样转为带原始信息的 DriveError 上报。
- `life_strm.py tick()`：新增 `except PermissionError` 分支（位于通用 Exception 之前），引擎之外的写入拒绝也完整透传；通用兜底文案去掉误导性的"请检查目录写入授权"。
- 防误删设计不变：探测文件仅在授权目录内创建并立即删除；探测失败时不落任何 STRM。

## 测试

Python 3.13.12（托管运行时，独立 venv），`PYTHONPATH=".:tests:../source-v035/src"`：**101 项全部通过**（1.6.3 为 99 项）。本版新增：

- `test_write_probe_fails_fast_with_host_grant_message`：宿主沙箱拒绝（makedirs 抛「没有获得访问…的授权」）→ 秒级失败、报错含原始授权信息、绝不落盘、不启动媒体库扫描。
- `test_write_probe_fails_fast_with_os_permission_message`：操作系统拒绝（open 抛 `[Errno 13] Permission denied`）→ 同样快速失败并保留 errno 详情。

## 未验证边界（待真实实例）

- 用户实例上 PermissionError 的具体层（插件授权 vs OS 权限）：1.6.4 的报错会直接显示宿主原始信息，一眼定位；据此或补授权、或 chown/chmod 服务器目录。
- 其余 1.6.3 未验证项不变（多映射大目录耗时、真实 CDN 附属下载、实际上传与 Linux 部署等）。

---

# 1.6.3 验证报告

目标：MovieClaw v0.35.0。

## 问题：1.6.2 统一 UA 后，扫码成功、凭据可用，但账号检测报「115 未返回有效的容量信息」

用户实测：990001 已消失（1.6.2 的 UA 绑定修复生效，凭据被 115 接受），但登录后的账号探测失败在容量解析上。这正是 1.6.2 发布时标注「待现场验证：index_info 容量字段线上返回」的已知风险点。

## 根因（115driver 生产源码核实）

**容量字段路径此前是推测的。** 1.6.2 前的 `account()` 按猜测路径 `data.space.space_total/space_used` 解析，而 115 真实返回结构（SheltonZhu/115driver `pkg/driver/info.go`，2026 年仍在维护、真机可用）是：

```
data.space_info.all_total.size / all_use.size / all_remain.size
```

且 `size` 可能是 JSON 数字**或数字字符串**（115driver 为此专门写了 `unmarshalSpaceSize` 兼容两种类型，并在 2026-05 修复过「无限容量」解析异常）。生产实现 `GetInfo()` 调用 `files/index_info` **不带任何参数**，而我们此前多带了 `count_space_nums=0`。

## 修复内容

- `drive_api.py account()`：完全对齐 115driver 契约——无参调用 `files/index_info`，容量取 `space_info.all_total/all_use/all_remain`，`number()` 兼容数字与数字字符串，剩余容量优先取 115 返回的 `all_remain`（比 total-used 精确），`all_remain` 缺失但 total/used 齐全时回退 total-used；保留 1.6.2 前的旧路径 `data.space` 作兜底。
- **容量缺失不再判定登录失败**：`state=true` 的认证响应本身就证明凭据有效——容量只是展示信息。`account()` 在容量字段缺失时返回 `space_total/space_used/space_free = None` 而不再抛错，同步/上传/播放完全不受影响。
- `panel.py`：账号徽标在容量缺失时显示「✔ 账号：…（凭据有效，115 未返回容量明细）」，不再误报红色「检测失败」；`refreshAccount` 与 `checkAccount` 共用同一渲染函数。
- `p115_plugin.py`：`/login/diag` 的「当前 Cookie 探测」同步改为无参调用（与生产实现一致）。

## 测试

Python 3.13.12（托管运行时，独立 venv），`PYTHONPATH=".:tests:../source-v035/src"`：**99 项全部通过**（1.6.2 为 97 项）。本版变化：

- `test_drive.py`：`test_account_space_from_index_info` 改用真实结构（断言无参调用、字符串 size、all_remain 优先）；新增 `test_account_space_falls_back_to_used_plus_remain`（total 缺失时用 used+remain 补全）、`test_account_degrades_when_capacity_missing`（容量缺失返回 None 而非抛错——本版核心修复点）、`test_account_rejects_malformed_data`（data 非 dict 仍防御性报错）。
- `test_process.py` E2E：index_info mock 改为真实 `space_info` 结构，子进程内扫码→登录信息→同步→播放全链路保持全绿。

## 未验证边界（待真实实例）

- 115 线上 `space_info` 各字段的具体返回（本版契约与 115driver 2026-08 版完全一致，预期直接可用）；若线上仍不返回容量明细，插件按「凭据有效」降级展示，不影响任何功能。
- 其余 1.6.2 未验证项不变（多映射大目录耗时、真实 CDN 附属下载、上传与 Linux 部署等）。

---

# 1.6.2 验证报告

目标：MovieClaw v0.35.0。

## 问题：1.6.1 修复确认通道后，扫码成功但接口仍持续 990001

用户实测：换 Cookie 已走 passportapi + app 参数、设备选微信小程序，扫码显示成功，但账号检测仍报 990001。排除通道与设备类型后，剩余的唯一差异是 User-Agent。

## 根因（OpenList 115 驱动源码核实）

**115 把凭据与签发请求的 User-Agent 绑定，签发与后续 API 调用必须使用同一个 UA。**

- 我们 1.6.1 的签发请求对 wechatmini 等设备未设置任何 UA → httpx 默认 `python-httpx/x.y`；而 `drive_api.Drive` 的全部 webapi 调用使用 `Mozilla/5.0` → UA 不一致 → 每次调用被判凭据无效（990001）。
- 生产佐证：OpenList `drivers/115/util.go` 的 `login()` 用 `driver115.UA(d.getUA())` 构造**同一个 client**完成扫码确认与后续所有 webapi 调用（`getUA()` 返回 `Mozilla/5.0 115Browser/<版本>`，对所有设备类型统一）；其 `LinkCacheMode: LinkCacheUA` 与 115driver client.go:55-61（"下载直链与获取它的 UA 绑定"）进一步证明 115 的 UA 绑定机制。p115client 自身可用则是因为它签发与调用都用默认 `Mozilla/5.0`（一致）。
- 通道再确认：SheltonZhu/115driver `api.go` 的文件操作全部走 `webapi.115.com`（与我们相同）→ 小程序类型凭据可调 webapi，与 UA 绑定结论吻合。

## 修复内容

- `login.py`：新增 `API_UA='Mozilla/5.0 115Browser/35.6.5'` 与 `mint_ua(cfg)`；**全部扫码请求（token/轮询/确认）统一携带该 UA**；签发成功后把 UA 持久化到插件存储 `qr-ua`（Cookie 仍存 `qr-cookie` 秘密槽）；确认请求在 passportapi 上按设备值直连路径（与 115-offline-helper 一致，移除 1.6.1 基于 qrcodeapi 契约的 ios 端点映射）；凭据含 KID 时一并写入（OpenList 同款）。
- `drive_api.py`：`Drive` 记录 `self.ua`（默认 `Mozilla/5.0`，兼容旧版网页 Cookie），所有 API 调用默认复用；仅 `link()` 按调用方显式传入的播放器 UA 获取下载直链（直链必须与播放器 UA 匹配）。
- `p115_plugin.py`：新增设置 `api_user_agent`（手动粘贴 Cookie 时填写获取它的 UA，留空回退）；启动/热应用/扫码成功三处把 `drive.ua` 同步为「用户设置 > 存储的签发 UA > Mozilla/5.0」；`/login/diag` 新增第三项**当前 Cookie 探测**（用当前凭据+当前 UA 实调 index_info，直接回报凭据可用性与错误码）。
- `panel.py`：账号页新增「API User-Agent」输入与说明；网络诊断 toast 展示三项结果。

## 测试

Python 3.13.12（托管运行时，独立 venv），`PYTHONPATH=".:tests:../source-v035/src"`：**97 项全部通过**（1.6.1 为 96 项）。本版变化：

- `test_upload.py` 扫码用例：断言全部扫码请求携带统一 UA、签发 UA 持久化到 `qr-ua`、ipad/qios 在 passportapi 直连自身路径、自定义 `api_user_agent` 时签发与存储均用用户 UA。
- `test_drive.py` 新增 `test_drive_repeats_minted_user_agent`：`drive.ua` 必须出现在实际请求头，默认值 `Mozilla/5.0` 回退保留。
- `test_process.py` E2E：网络诊断断言三项 checks（token/confirm/cookie），子进程内扫码→登录信息→同步→播放全链路保持全绿。

## 未验证边界（待真实实例）

- 微信小程序类型 + `Mozilla/5.0 115Browser/35.6.5` UA 组合的线上表现（OpenList 生产同款组合，预期直接可用）；若仍异常，面板「网络诊断」的「当前 Cookie 探测」会直接回报 115 的真实错误码。
- `link()` 直链按播放器 UA 签发在 1.6.2 UA 体系下的播放兼容性（播放器 UA 与凭据 UA 不同是否影响 proapi 直链下发）。

---

# 1.6.1 验证报告

目标：MovieClaw v0.35.0。

## 问题：扫码成功，但凭据立即 990001

用户实测：扫码显示成功、凭据已保存，随后的账号检测报「115 接口失败，错误码 990001（登陆已超时）」。全新签发的凭据不可能"超时"，必然是签发通道或设备类型问题。

## 根因（三源交叉核实）

1. **换 Cookie 走错了确认通道**。1.5.1 沿用 p115client 0.0.9.7.2 `login_qrcode_scan_result`（client.py:1495）的 `qrcodeapi.115.com/app/1.0/{app}/1.0/login/qrcode/`（仅 account 参数）。而三个生产级实现全部走 `passportapi.115.com/app/1.0/{app}/1.0/login/qrcode/` 且 POST 体显式携带 `app` 参数：
   - gangz1o/115-offline-helper（用户指定参考项目）`extension/popup.js:447-451`：`POST https://passportapi.115.com/app/1.0/${selectedApp}/1.0/login/qrcode/`，`data:{account:uid, app:selectedApp}`；
   - SheltonZhu/115driver（AList/OpenList 的 115 驱动库）`pkg/driver/qrcode.go`：`QRCodeLoginWithApp` 以表单 `{account, app}` POST `ApiQrcodeLoginWithApp = "https://passportapi.115.com/app/1.0/%s/1.0/login/qrcode"`（api.go:58）；
   - p115client 作者本人的扫码 gist：同样 passportapi + `{app, account}`。
   qrcodeapi 通道签发的凭据不被 webapi.115.com 接受 → 立即 990001。
2. **设备列表含已被 115 下架的电脑端**。AList 官方文档明确：115 已下架 Windows/macOS/Linux 客户端，这三类设备的 Cookie「系统已下架」不可用；而 1.4.x 的默认设备恰好是 os_windows，面板存留的旧选择会让用户拿到一张天生无效的凭据。
3. 排除项：p115client 默认请求头就是裸 `Mozilla/5.0`（client.py:541），UA 不是问题；tv/小程序等非 web 设备的 Cookie 可正常调用 webapi（AList 生产验证），设备绑定端点也不是问题。

## 修复内容

- `login.py`：确认调用切换到 `passportapi.115.com`，POST 体携带 `{account, app}`（token/状态轮询保持 qrcodeapi + web 端点不变）；ios 系设备仍映射 ios 确认端点并按 UA 区分（p115client client.py:1512-1523 契约），app 参数取原始设备值。
- **下架 os_windows/os_mac/os_linux**：设备下拉不再提供，`LoginStart` 校验器与 `Login.start` 对旧值返回可操作的中文错误（提示改选小程序/115管理 App/TV）；面板存留的旧设备值在 `/config` 读取时自动回退为 wechatmini 并回写。
- `/login/diag` 升级为双通道探测：取码（qrcodeapi token）+ 换 Cookie（passportapi check/sso 连通性），面板 toast 分别展示两个通道的结论。
- 设备清单更新：wechatmini（默认）/alipaymini/qandroid/qios/tv（推荐）+ android/ios/ipad/web（互踢或风控风险注明）。

## 测试

Python 3.13.12（托管运行时，独立 venv），`PYTHONPATH=".:tests:../source-v035/src"`：**96 项全部通过**。本版变化：

- `test_upload.py` 扫码用例重写断言：确认请求 host 必须为 `passportapi.115.com`、路径 `/app/1.0/<app>/1.0/login/qrcode/`、表单必须含 `app=<设备值>`；token 仍为 qrcodeapi `/api/1.0/web/1.0/token/`；ios/ipad/qios 保持 ios 端点 + UPhone/UPad/OfficePhone UA + 原始 app 参数；新增 os_windows 拒绝断言（匹配「下架」提示）。
- `test_process.py` E2E：设备列表不含 os_windows、默认 wechatmini；`/login/diag` 返回 `checks` 含 `token`/`confirm` 两项；子进程 mock 增加 `/check/sso` 规则。
- 既有 95 项（cron、同步引擎、多映射、附属同步、上传队列、防误删、E2E 上传播放链路）无回归。

打包校验：官方 `read_archive`/`check_compat` 通过；operations 仍为 `library.scan.start`、`library.list`。

## 未验证边界（待真实实例）

- passportapi 通道在用户服务器上的真实表现（1.6.1 的核心验证点：扫码后 `/login/info` 应立即返回容量而不是 990001）。
- 设备互踢行为（小程序/TV/115管理 App 与用户常用端不互踢的线上验证）。
- 多映射大目录耗时、网盘附属直链下载在真实 115 CDN 的成功率。

---

# 1.6.0 验证报告

目标：MovieClaw v0.35.0。

## 学习对象与功能对照

本版以开源项目 [qicfan/qmediasync](https://github.com/qicfan/qmediasync)（Go，115/123/百度网盘 STRM 同步，GPLv3）为参照，逐模块对照其 internal 源码（syncstrm / models(SyncPath·Sync·SyncFile) / scrape / backup 等）后确定差距与改进范围：

| qmediasync 能力 | 我们 1.5.1 | 1.6.0 处置 |
|---|---|---|
| SyncPath 多路径映射（每路径独立 cid/本地目录） | 单映射 | **补齐**：mappings 多映射（≤8 条），面板可视化编辑 |
| Sync 运行历史（每次运行状态/数量/失败原因） | 仅最新状态 | **补齐**：持久化最近 20 次运行记录 + `/sync/history` |
| download_meta 网盘元数据下载 | 附属文件只能从整理目录复制，"其他客户端上传的附属无法溯源" | **补齐**：网盘附属经 115 直链下载补齐（≤20MB，sha1 变更跟进） |
| backup 配置备份恢复 | 无 | **补齐**：`/config/export`、`/config/import`（不含 Cookie） |
| 直链 302 播放 / 外网地址 | 已有（/play 验签 302 + strm_base_url） | 保持 |
| 媒体库刷新联动 | 已有（library.scan.start） | 保持 |
| 刮削/TMDB 识别重命名、Emby 联动删除、多网盘（123/百度/openlist）、TG 通知 | 无 | **不做**：超出插件范围或依赖不存在的宿主契约（不猜测端点），列入待办候选 |
| SyncFile 文件级库 | manifest+proof 记录（含 fingerprint/防误删凭据） | 功能等价，保持 |

## 实现要点

- **兼容性优先**：旧单目录映射的 AutoMapping 三元组（cid/library_id/subdirectory）字节不变 → scope_key 不变 → 升级后已有 STRM 与防误删凭据全部有效（专项测试断言）。
- **AutoOps.library.get** 改为从 `auto:<account>:<posix-root>` 形式的 library_id 解析各映射根目录；library.scan.start 按 engine 传入的实际路径匹配媒体库，多映射各自触发扫描。
- **LifeSTRM 门控**从"strm_directory 非空"改为"存在任一映射"（active()）；mappings 非空时单目录配置被忽略。
- **网盘附属下载**：枚举时收集 SIDECAR 后缀、≤20MB、有提取码的候选；同 stem 与目录级（poster.jpg/tvshow.nfo 等）匹配到对应视频；下载走 drive.link 签名直链，所有权规则与 copy_sidecars 一致——用户改过的文件绝不覆盖，记录 115 sha1 身份供后续变更检测；下载失败软跳过、下次重试。manifest 记录新增 `cloud` 身份字段，且与 `sidecars` 一样不参与 STRM 重写判定。
- **映射校验**：JSON 数组、≤8 条、CID 数字、目录绝对路径安全、去重、互不嵌套、与整理目录/单目录不嵌套（model_validator 统一检查）。

## 测试

Python 3.13.12（托管运行时，独立 venv），`PYTHONPATH=".:tests:../source-v035/src"`：**96 项全部通过**（1.5.1 为 91 项）。新增 6 项：

- `test_life.py`
  - `test_mapping_specs_validation_and_legacy_scope_key`：mapping_specs 全部非法输入拒绝（坏 JSON/非数组/坏 CID/相对路径/重复/嵌套/根目录）；**旧单映射 scope_key 与 1.5.x 完全一致**。
  - `test_multi_mapping_syncs_each_directory`：两条映射各自生成 STRM，且 library.scan.start 按映射路径分别触发。
  - `test_sync_history_records_runs_and_failures`：成功/失败运行入库、时间与错误信息、上限 20 条。
- `test_sync.py`
  - `test_cloud_only_sidecar_downloaded_via_direct_link`：同 stem 字幕 + poster.jpg 经直链下载落盘；二次运行 sha1 未变不重复下载；sha1 变化后重新下载覆盖。
  - `test_cloud_sidecar_user_file_never_touched`：用户改过的附属文件不被云端版本覆盖。
  - `test_cloud_sidecar_download_failure_is_soft`：下载失败仅跳过该附属，STRM 正常生成。
- `test_process.py` E2E（真实子进程 + Unix socket + fd 传递）：面板含多映射/历史/导出导入界面；保存 2 条映射 → `/strm/status` mapping_count==2 → 全量同步在两个本地目录分别生成 STRM（cid=99 与根目录 cid=0）；非法 mappings 被拒绝；`/sync/history` 返回带时间与模式的记录；`/config/export` 不含 cookie 字样 → 修改后 `/config/import` 立即生效且映射保留。
- 既有 90 项（cron、同步引擎、附属同步、上传队列、扫码登录、防误删、E2E 上传播放链路）无回归。

打包校验：官方 `read_archive`/`check_compat` 通过；operations 仍为 `library.scan.start`、`library.list`。

## 未验证边界（待真实实例）

- 多映射在大目录下的实际并发耗时（串行逐映射同步，受 max_entries 每映射限制）。
- 网盘附属直链下载在真实 115 CDN 的成功率与限流表现（下载失败软跳过，不影响主流程）。
- 真实扫码（1.5.1 token=web + 小程序端确认）、设备互踢、`index_info` 容量字段线上返回、实际上传与 Linux 部署。
