# DriveBackend 1.5 契约矩阵 — 0.1.0-beta.1

基线：MovieClaw v0.41.0 / 5b0739f54fc1e22ba2fd9c4037d132d16e85e0c1，SDK1.0，registry1.5。本插件采用逐目录扫描与插件内持久化恢复；这没有改变SDK条文，也不表示上游已接受差异。

| 契约 | 实现 | 差异与验证边界 |
|---|---|---|
| Login.form/login_with_form | SecretStr手机号密码 → login_passport → user_info UID | 无真实登录；验证码RiskControl，不绕过 |
| begin_login/login_state/cancel_login/login_with_cookie | scan=False、cookie_domains=() | 未启用路径不对外承诺 |
| account_info | UID核验、容量原值，缺省None | 临时空间口径待验收 |
| list_dir | fs_list分页，只返回子目录 | 传Page和真实Next，不以空非终止页结束；普通文件名称/大小不参与目录选择校验，目录标点/Unicode保留 |
| list_tree | 迭代逐目录分页，全部文件相对路径/ID/大小/parent、全部空/非空子目录ID | **明确偏离原生整树条款**；功能已启用，非事务快照 |
| 扫描失败/资源 | 任一页、类型、循环、重复路径/ID/游标、移动、深度/数量超限都报错 | 不返回部分结果；200000项/20000列表页/256层/8192字符路径 |
| changes | change_feed=False | 无可信全量变化流 |
| ensure_dir | 逐级查找或创建，duplicate=0 | 文件碰撞/多同名Conflict，空路径原样返回 |
| begin_upload | 写前意图、同名冲突、服务Reuse确认、远端目录/大小/MD5核验 | 占位元数据不能单独证明完成；零ID秒传暂拒绝 |
| begin_upload(state/None) | 按UID/目录/名称/大小/MD5读取PLUGIN_DATA日志，恢复原会话和确认进度 | 无日志旧会话不猜测；初始化/合并不确定时安全阻塞 |
| upload_part | 顺序、长度与字节摘要校验，申请签名、PUT、持久化确认后返回 | 丢ACK可同键同片同字节重传；不会盲目重试合并；源站会话过期不自动重建 |
| finish_upload | 写committing → complete/v2(fileSize) → 写committed → stat核验 → complete | 提交响应未持久化仍是不确定，不能假装exactly-once；确认已落库后仅重试stat |
| stat / SHA1 | fs_info；远端sha1=None、list_hashes=False | Etag是MD5，不伪造远端SHA1 |
| delete | ≤100，逐文件移入回收站；缺失成功 | 拒绝目录，不永久删除 |
| direct_link | download_info，UA+platform=android，300秒TTL | 宿主缓存；未真实播放验证 |
| authentication update | LoginResult.credentials / account.credentials | 正式认证加密链；不进入上传日志 |
| PLUGIN_DATA | 公开服务注入、scoped(ctx)、scope=upload:v2、secret=True | 正式DataApi/RPC；60KiB≤宿主64KiB；无自建DB；单活写入，不提供CAS |
| lifecycle/network | SDK http_transport逐请求计量，AsyncClient复用、ctx.effect释放 | 取消传播、响应关闭、无默认库session、无monkeypatch |

## 仍需明确保留的边界

1. 逐目录遍历不符合SDK原生整树实现方式；这项实现选择需要在上游审查中明确处理。全契约门槛继续退出2，但不再禁用扫描。
2. 远端非空进度结构尚不能确认；不解析未经证实的parts/Parts。恢复使用持久化ACK；日志丢失无法从裸宿主state推断完成。
3. 初始化/合并跨网盘与插件数据存储没有原子事务。`initializing`/`committing`是有意保留的不确定状态，需要用户隔离验收或后续可靠的服务端判定证据，不能通过删日志“修复”。
4. 用户外部改动和扫描期间并发变化无法被完全观察；检测到的变化全次失败，无法提供服务端快照一致性。
5. 维护者反馈 alpha.5 真实目录选择复测通过；其余服务能力不据此宣称验收。离线测试未访问真实账号，核心/SDK未修改。

错误分类沿用：429→RateLimited，认证401→AuthExpired，验证码/风控→RiskControl，明确缺失→NotFound、空间不足→QuotaExceeded、同名/内容冲突→Conflict，网络5xx→Transient，未知结构/安全边界→ApiChanged。存储签名403为Transient；日志故障不泄露原始异常内容。

## 名称边界（继承 alpha.5 修复）

远端显示字段校验仅验证非空字符串和无NUL；远端路径段额外拒绝点路径段及正反斜杠，保留其余标点/Unicode；新建名称保留独立的保守规则。宿主目录接口会构造display_path，所以目录选择结果也须是无歧义单段名称。文件名问题不能阻断仅取子目录的选择器，但全量扫描仍不得跳过坏项或静默改名。

只读核对核心gateway：用户优先级的list_dir成功后走_record_recovery，原api_broken可恢复并消退通知。此为源码行为说明，不以修改数据库作为恢复步骤。
