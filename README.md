# MovieClaw 插件仓库

这里是 MovieClaw 官方插件的源码和发布目录。每个插件单独发版。一份静态索引告诉应用：每个插件 id 的最新包在哪、校验和是什么。

随应用自带的通道、下载器和观看动态，权威副本仍在 [MovieClaw](https://github.com/movieclaw/MovieClaw)，并随应用打包。`example/` 里有一份对照复制，不进 `plugins/`，不进索引，也不发版。

MovieClaw 的「设置 → 插件 → 插件市场」读这份索引：列出插件和版本，安装时服务器先验索引签名，再按索引里的 sha256 核对下载的包，然后进入和上传一样的批准界面。

## 目录

```
├── README.md
├── index.json                 索引源文件，给人审、给 PR 看 diff
├── index.schema.json          索引 schema，版本 1
├── plugins/                   正式插件，每个进索引、单独发版
│   └── <id>/
│       ├── movieclaw-plugin.toml
│       ├── store.json         市场里的简介、分类、作者、图标、撤回。不能写进清单
│       ├── icon.svg           可选
│       ├── CHANGELOG.md       每版一节「## <版本>」：Release 正文与市场的更新说明
│       └── <entry>.py
├── example/
│   ├── GUIDE.md               给插件作者的说明
│   ├── UPSTREAM.json          参考副本对应的 MovieClaw 提交
│   ├── hello-note/            能打包、能通过服务器校验的最小示例
│   └── <id>/                  从 MovieClaw 复制的参考插件，不发版
└── scripts/
    ├── pack.py                时间戳钉死的打包，打印 sha256
    ├── generate_index.py      从各版本的 tag、store.json 和发布记录生成索引
    ├── release_notes.py       截出某一版的 CHANGELOG
    ├── upload_catalog.sh      上传索引、签名和图片到 catalog Release
    └── sync_examples.py       从 MovieClaw 重新复制 example/ 里的参考插件
```

`.github/workflows/ci.yml` 在 PR 上校验。`release.yml` 在 tag 上发版。`catalog.yml` 在 `main` 的 `index.json` 变了时（只改元数据）签名并上传索引。

## 参考插件

写新插件时复制 `example/hello-note/`。下面这些是从 MovieClaw 同步来的对照，目录名是插件 id。不要把它们改一改就放进 `plugins/`。重新拉取用 `scripts/sync_examples.py`。

<!-- sync-examples:begin -->
当前副本的 MovieClaw 提交是 `779712c4d8d2c2c320021ab0e31cde95821dd493`。机器可读的记录在 [`example/UPSTREAM.json`](example/UPSTREAM.json)。
权威源码在 [MovieClaw](https://github.com/movieclaw/MovieClaw)，并随应用打包。不要改这里的副本来发版。要更新：

```bash
python3 scripts/sync_examples.py 779712c4d8d2c2c320021ab0e31cde95821dd493
```

| 目录 | 上游路径 | runtime | 示范 |
|---|---|---|---|
| [`watch-presence`](example/watch-presence/) | `plugins/watch-presence` | `inline` | 进程内观看动态：成员区路由和播放记录，直接用宿主内部模块 |
| [`weixin-channel`](example/weixin-channel/) | `src/movieclaw_plugins/weixin` | `process` | 向 im-channels 贡献微信通道（扫码绑定、iLink、入站图片） |
| [`telegram-channel`](example/telegram-channel/) | `src/movieclaw_plugins/telegram` | `process` | 向 im-channels 贡献 Telegram bot（私聊对话和推送） |
| [`discord-channel`](example/discord-channel/) | `src/movieclaw_plugins/discord` | `process` | 向 im-channels 贡献 Discord bot（Gateway） |
| [`feishu-channel`](example/feishu-channel/) | `src/movieclaw_plugins/feishu` | `process` | 向 im-channels 贡献飞书群机器人（只推送，不能对话） |
| [`qbittorrent-downloader`](example/qbittorrent-downloader/) | `src/movieclaw_plugins/qbittorrent` | `process` | 向 downloader-adapters 贡献 qBittorrent |
| [`transmission-downloader`](example/transmission-downloader/) | `src/movieclaw_plugins/transmission` | `process` | 向 downloader-adapters 贡献 Transmission |
| [`strm-prefetch`](example/strm-prefetch/) | `src/movieclaw_plugins/strm_prefetch` | `process` | 用任务和敏感播放事件提前探网盘规格，探测仍走宿主操作 |

`watch-presence` 是 `runtime = inline`，import 宿主内部的 `movieclaw_api`、`movieclaw_db`、`movieclaw_playback`、`movieclaw_kernel`，不是只依赖 SDK。下载器依赖宿主提供的 `qbittorrent-api` / `transmission-rpc`，不放进 `vendor/`。

这些目录里的测试不能脱离 MovieClaw 单独跑，同步时不复制。各目录的 `README.md` 写了上游路径、提交号和省略原因。
<!-- sync-examples:end -->

## 怎么加一个插件

先读 [example/GUIDE.md](example/GUIDE.md)。`example/hello-note/` 是一份能工作的 `runtime = "process"` 插件，不在索引里。同目录里从 MovieClaw 同步来的参考插件不是这个模板。

1. 把 `example/hello-note/` 复制到 `plugins/<id>/`，改掉 id、标题、版本和代码。不要从参考插件开副本。id 是小写字母开头，只用小写字母、数字和连字符，3～40 位，不能有点。
2. 清单只写服务器认识的字段。简介、分类、作者、许可证、图标写在同目录的 `store.json`，字段见 [GUIDE](example/GUIDE.md#storejson-和在本仓库发版)。`CHANGELOG.md` 里写好这一版的一节。
3. 开 PR。CI 用钉死的 MovieClaw 版本（v0.42.0，提交 `bd325adde24e5e035878d3fb668cd0b004c96760`）跑 `Manifest` / `check_compat`，并跑该目录自己的测试。改了哪个 `plugins/<id>/` 就查哪个。`example/` 有改动时会再查 `hello-note`，并按 `example/UPSTREAM.json` 再查参考副本（只跑 `check_plugin.py`；参考副本没有带上不能单独跑的测试）。
4. 合并之后打 tag：`<id>-v<版本>`，例如 `watch-presence-v1.0.0`。tag 里的版本必须和清单 `version` 逐字相同。

## 版本、tag 和 Release

一个插件一个 Release。附件名用 `<id>-<版本>.mcplugin`，例如 `watch-presence-1.0.0.mcplugin`。

tag 用最后一个 `-v` 加数字版本拆开，所以 id 里的连字符不会和版本打架。

**所有 Release 都带 `--latest=false`，包括下面的索引 Release。** 不要把 `/releases/latest` 当成「某个插件的最新版」，也不要当成「商店索引」。谁最后发布，GitHub 的 latest 就是谁。

每个 id 的最新版写在索引里。带 `-` 后缀的预发布版本不会盖过同号的正式版。

## 索引

| 什么 | 在哪 |
|---|---|
| 给人看的源文件 | 本仓库 `main` 上的 [`index.json`](index.json)，格式见 [`index.schema.json`](index.schema.json)（schema **2**） |
| 每个插件的包 | 该插件自己的 Release 附件，地址跟着 tag 不变 |
| 给应用拉的那一份 | 长期存在的 Release，tag 固定叫 `catalog`，反复覆盖 `index.json` 与 `index.json.sig`；图标等图片按内容命名也放在这里 |

应用应下载这一个地址，不要按插件去打 GitHub Releases API：

`https://github.com/movieclaw/movieclaw-plugins/releases/download/catalog/index.json`

索引里每个版本的权限、`sdk`、`[requires]`、依赖锁文件、README 和更新说明，都从那一版的 tag 读出；简介、分类、图标、撤回从 `main` 的 `store.json` 读；下载地址、sha256、大小来自发布记录。发版时 `release.yml` 重生索引；只改元数据时在 PR 里运行 `python3 scripts/generate_index.py --update`，合并后 `catalog.yml` 上传。

签名：发布索引必须配置 Actions secret `RELEASE_SIGNING_KEY`（Ed25519 私钥 PEM），没有它工作流直接失败，因为 MovieClaw 不加载未签名的索引。`index.json.sig` 对 `index.json` 的原始字节做 Ed25519，签名是 base64 再加一个换行，和主仓库 `manifest.json.sig` 一样。可选 secret `RELEASE_SIGNING_KEY_ID` 写入索引的 `key_id`，缺省是 `catalog-1`。公钥不写进索引，随 MovieClaw 版本发布（`movieclaw_api/services/plugin_market.py`）。换密钥时先在 MovieClaw 里加上新公钥并发版，再换 secret。

schema 2 之内只加字段，读的一方忽略不认识的字段。要做不兼容的改动，就换一个文件名发布，旧版应用继续读旧文件。

## 许可证

**TODO：本仓库还没有选定许可证。** 在选定并加入 `LICENSE` 之前，不要把这里的代码当成已经授予再分发许可。插件的 `store.json` 可以先写「见仓库许可证」，选定后一起改。

## English

Official MovieClaw plugin source and releases. One GitHub Release per plugin, tag `<id>-v<version>`, asset `<id>-<version>.mcplugin`. Every release is published with `--latest=false`. The machine-readable catalog is schema 2: `index.json` on `main`, and the copy clients fetch at `.../releases/download/catalog/index.json` with `index.json.sig` (Ed25519 over the raw file, same construction as MovieClaw app-update manifests). Each version records what it declared at its own tag; store metadata (summary, category, icon, yanks) comes from `store.json` on main. MovieClaw's in-app plugin market reads it. Authoring notes are in `example/GUIDE.md`. Reference copies of MovieClaw's bundled plugins and `watch-presence` live under `example/<id>/` (commit recorded in `example/UPSTREAM.json`); they are not indexed or released. `example/hello-note` is the template to copy. The repository license is not chosen yet; see the TODO above.
