# 寻映

<img src="icon.svg" width="80" height="80" alt="寻映图标：奶油金放映光束、搜索镜头与薄荷绿弧线">

配合 Apple「找电影」快捷指令，用语音或文字同时搜索电影和电视剧，核对海报、标准片名和年份后，再观看或订阅。候选明确标注电影或剧集；剧集订阅需选择季数。插件展示名为**寻映**，内部标识 `movie-voice`，版本 `0.1.2`；快捷指令继续叫**找电影**。

## 图标

深青底搭配奶油金放映光束，光束内嵌搜索镜头，下方以薄荷绿弧线呼应语音与寻找。市场与页面使用彩色图标，侧栏使用随主程序主题着色的单色图标。[查看设计预览](docs/icon-preview.png)。配套快捷指令保留用户设置的图标。

## 功能

- 先查媒体库与电影、电视剧元数据；支持同音、库存别名和模型辅助片名纠错。
- 快捷指令内显示带海报的候选，可重新说、手动输入、补充年份/导演/演员/剧情，或查看帮助。
- 已在库（电影或剧集）：再次核对库存后，向管理员自己的 MovieClaw 官方 App 发观看通知，点击进入这部影片；同时提供网页观看入口。
- 未在库：用户确认后沿用服务器现有线路、质量规则和入库路由订阅，自动搜索、下载与入库。资源是否命中及何时下载取决于宿主配置。
- 插件网页提供备用搜索、生成与复制 SEARCH_URL、通用分享版下载，以及已预签名个人版的私密下载。

当前仅支持管理员本人，不为普通成员生成独立授权。语音输入由 Apple 系统提供；本插件负责对收到的文本纠错，不能保证系统听写准确率。

## 安装与配置

1. 在 MovieClaw“设置 → 插件”安装寻映，或上传 Release 中的 `movie-voice-0.1.2.mcplugin`，核对申请的操作、联网和网页权限后批准。
2. 打开侧栏**寻映**，或访问自己服务器的 `/apps/movie-voice`。手机须能访问这台服务器；建议先配置有效的 HTTPS 外部地址。元数据来源、订阅线路、下载器和默认规则由服务器管理员提前配置。
3. 点击下载按钮旁的 **iCloud 一键添加**，进入 Apple 页面并确认添加；也可点击**下载分享版快捷指令**，在 iPhone / iPad 的“文件”App 或 Mac 中打开 `.shortcut` 添加。然后在寻映页面点击**生成 SEARCH_URL → 复制链接**。
4. 编辑快捷指令顶部的 SEARCH_URL“文本”框，整段替换占位文字后保存；可将“找电影·分享版”改名为“找电影”。首次运行按系统提示允许连接自己的服务器。
5. 说“嘿 Siri，找电影”，再说电影或电视剧名，可带年份。核对候选后确认。未确认不会订阅；取消输入或确认会结束。

唯一必填项是服务器生成的完整 **SEARCH_URL**，不用填写登录密码、管理员 API 令牌或 PT Cookie。不要只填服务器首页、粘贴整个 JSON 或修改链接参数。虚构格式示例（不可直接使用）：

```text
https://media.example.com/api/v1/plugins/movie-voice/shortcut/search?epoch=EXAMPLE_EPOCH_NOT_VALID&sig=EXAMPLE_SIGNATURE_NOT_VALID
```

插件安装并批准后，下载入口就在寻映页面，无需另找快捷指令文件。通用分享版随安装包提供；个人版由本服务器管理员完成 Mac 签名准备后提供。通用分享版同时提供 [iCloud 一键添加](https://www.icloud.com/shortcuts/2d45713166744376803113f728896e52)和文件下载；iCloud 入口位于下载按钮旁，打开 Apple 添加页面后仍需用户确认，并填写自己的 SEARCH_URL。浏览器未能跳转时，下载并打开 `.shortcut` 文件添加。安装插件本身不会自动在手机或电脑上安装快捷指令。

iCloud 链接中的通用分享版仍保留配置占位，不含作者服务器或授权；个人版继续通过管理员专用下载接口分发，不使用公开 iCloud 链接。外部入口禁止发送来源地址，也不附加当前服务器信息。文件导入及 iCloud 分享流程见 [Apple 文件导入说明](https://support.apple.com/zh-cn/guide/shortcuts-mac/apd02bffbaac/mac)和 [Apple iCloud 分享说明](https://support.apple.com/zh-cn/guide/shortcuts/apdf01f8c054/ios)。

观看通知需要在官方 App 登录同一管理员账号并允许通知；没有 App 或推送不可用时，使用结果中的网页入口。通知打开的是影片详情，播放还需用户点击。

## 分享版与个人版

两版图标与当前 Mac 设置一致：个人版为青色波形（`waveform.path`），通用分享版为青色慢动作（`slowmo`）；只修改图标元数据，流程动作保持一致。

**通用分享版**随插件放在 `web/downloads/find-movie-share.shortcut`，已签名但保留配置占位，可以公开转发。源码位于 [`tools/shortcut_source.py`](tools/shortcut_source.py)。候选使用临时 vCard 影片卡，不读写通讯录；系统可能把它称为“联系人”。

**已配置个人版**需要每台服务器的管理员先在自己的 Mac 上准备一次，详见 [`tools/README.md`](tools/README.md)。该工具独立包含在本插件目录，无需作者的电脑或本机配置。它获取当前服务器的专用链接，构建并通过 macOS 签名、验签和核对配置，再私密上传。Apple 在系统签名时会接收含专用链接的文件进行验证。

准备成功后，寻映页面可直接下载个人版，添加即可使用，无需粘贴链接。下载时 Mac 不用在线。未准备时按钮不可用，通用分享版仍可使用。Linux 服务端不能即时签名 Apple 快捷指令。

个人版只存于插件加密数据，下载接口仅管理员可用并禁止缓存；不进入 `web/`、公开源码或 `.mcplugin`。服务器地址或授权变化后停止分发旧文件，撤销授权时清除已准备个人版，需要重新签名。

## 纠错与授权

先查原片名、拼音和库存别名，可靠命中时不调用模型。需要补充线索或纠错时，模型最多提出三个查询片名，再向真实元数据核验。模型不能创建影片或直接订阅。模型不可用时保留常规搜索；可能产生宿主已配置模型的用量费用。

电影和剧集使用各自的来源编号，不能仅按相同数字编号合并；未入库的剧集也参与搜索。网页通过宿主预览列出可选季；快捷指令请说出或输入明确季号（如第1季、1,2，特别篇填0），再核对并确认。服务端核验季号，不存在时不会创建订阅；不默认全部季，也不自动追更未来新季。已在库的剧集可能只有部分季集，观看前可进入详情核对。旧版快捷指令需要重新下载安装新版，才能使用季数输入。候选最多八部、票据十分钟有效，搜索缓存三分钟；每次确认重新核对真实身份与库存。连续交互最多二十轮。

**SEARCH_URL 是可撤销凭据**：持有者可搜索、确认订阅并向管理员本人发观看通知。配置后的快捷指令只供自己使用，分享请用保留占位的原版。页面默认隐藏链接、不写浏览器本地存储、离开页面清空；重新生成不会撤销旧授权。

撤销全部专用链接（不取消已有订阅）：

```sh
mclaw plugins movie-voice shortcut-revoke
```

随后重新生成链接并更新快捷指令；已准备个人版也须重做。停用插件会关闭接口。

## 权限与依赖

运行方式为 `process`。申请：`agent:none`、`library.items.get`、`search.library`、`search.titles`、`discover.get-title-details`、`subscriptions.create`；联网用于固定 TMDB HTTPS 小图，不跟随跳转、不转发认证，限时限量。不申请 PT 凭据、文件读写或删除权限。

插件网页以当前登录账号运行；管理员打开时拥有管理员网页会话的权限，这是宿主网页插件的信任边界。后台宿主操作按清单批准。安装时须审阅并正常批准网页，不能把后台六项操作误解成网页全部权限。

使用宿主自带的 FastAPI、Pydantic、HTTPX、pypinyin、MovieClaw SDK，不额外执行 pip；Mac 维护工具的依赖与服务端分开。SDK 与契约兼容性以清单检查为准；已在 0.41/0.42 服务端使用，仓库检查固定为 MovieClaw 0.42.0。

## 开发与发布

在安装了仓库固定 MovieClaw 0.42.0（`bd325adde24e5e035878d3fb668cd0b004c96760`）的 Python 环境中，从仓库根运行：

```sh
python /path/to/MovieClaw/src/movieclaw_agent/builtin-skills/movieclaw-plugin-dev/scripts/check_plugin.py plugins/movie-voice
python -m unittest discover -s plugins/movie-voice/tests -t plugins/movie-voice -v
node --test plugins/movie-voice/tests/core.test.mjs
python scripts/generate_index.py --lint
python scripts/pack.py plugins/movie-voice --output /tmp/movie-voice-0.1.2.mcplugin
```

macOS 上再按 `tools/README.md` 验签公开文件。更新日志见 [`CHANGELOG.md`](CHANGELOG.md)。合并后由仓库流程对 `movie-voice-v0.1.2` tag 发布，不手工编造索引或包哈希，不移动已发布 tag。

许可证暂按仓库允许的模板填写“见仓库许可证”；仓库尚未选定许可证，详见根 README 的许可证说明，不据此声称已获得开放再分发许可。
