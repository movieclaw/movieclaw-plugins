# Trakt 应用维护 / Application maintenance

默认应用「MovieClaw」由 MovieClaw 的 Trakt 项目账号持有，Production 类型，公开 Client ID 为 `aPq49pAxnq5HM6qVYY1vmKfHl6bV6S10aotXT5crbH0`。它不依赖插件作者的个人应用，不需要向作者授予 Manager 或 Owner。

## 授权与配置

- 使用设备码授权，不携带 Client Secret。新应用未签发 Secret，不把 Secret 或用户令牌打包进插件。
- 默认无需用户填写应用配置。自有部署仍可覆盖为自己的 Client ID；只在使用自定义应用时读取对应的自定义 Secret。
- `/oauth/token` 刷新带 `redirect_uri: urn:ietf:wg:oauth:2.0:oob`。应用登记的 `https://movieclaw.io/trakt/callback` 不用于设备码流程；Allowed origins 留空，不需要浏览器直接调用 Trakt API。
- 更换 Client ID 前断开旧绑定，新应用需重新授权；不能拿新应用的凭据刷新或撤销旧应用的授权。
- 收到撤销成功后立即清除本地状态；Trakt 的缓存可能让令牌短时间仍可用，不因即时探测结果恢复连接。刷新后始终使用新令牌。

上游已报告完成两轮无 Secret 的设备授权、读取、刷新和撤销测试，见 [验收回复](https://github.com/movieclaw/movieclaw-plugins/pull/17#issuecomment-6334211735)。本插件的独立真实接口验收与本地回归见 [2026-10-22 验证记录](docs/validation-2026-10-22.md)。

## 后续验收

应用配置或授权协议变化时，使用专门的临时测试授权验证连接、播放同步、刷新轮换与断开。先核对应用名额及已有绑定，不撤销其他应用；只清理本次新建的测试观看记录和授权。日志只记录接口、状态和结论，不记录令牌、Secret、设备码或响应正文。离线真机回传需单独验收，OAuth 成功不代表离线播放链路已经验证。

The default Production application is owned by the MovieClaw Trakt account. Device authorization, refresh and revoke omit the Client Secret; refresh uses the OOB redirect URI above. Custom application overrides remain isolated. Disconnect locally immediately after revoke and allow for remote revocation delay. Validate protocol changes with a dedicated test grant and preserve all existing grants and watch history.
