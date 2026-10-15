# MovieClaw 插件仓库

这里是 MovieClaw 官方插件的源码和发布目录。每个插件单独发版。一份静态索引告诉应用：每个插件 id 的最新包在哪、校验和是什么。

随应用自带的通道、下载器和观看动态，权威副本仍在 [MovieClaw](https://github.com/movieclaw/MovieClaw)，并随应用打包。`example/` 里有一份对照复制，不进 `plugins/`，不进索引，也不发版。

应用内商店还没做。现在的服务器只接受上传的 `.mcplugin`，不接受「填一个网址就安装」。本仓库先把包和索引准备好。

## 目录

```
├── README.md
├── index.json                 索引源文件，给人审、给 PR 看 diff
├── index.schema.json          索引 schema，版本 1
├── plugins/                   正式插件
│   └── <id>/
│       ├── movieclaw-plugin.toml
│       ├── store.json         作者、主页、许可证、图标。不能写进清单
│       ├── CHANGELOG.md
│       └── <entry>.py
├── example/
│   ├── GUIDE.md               给插件作者的说明
│   ├── UPSTREAM.json          参考副本对应的 MovieClaw 提交
│   ├── hello-note/            能打包、能通过服务器校验的最小示例
│   └── <id>/                  从 MovieClaw 复制的参考插件，不发版
└── scripts/
    ├── pack.py                时间戳钉死的打包，打印 sha256
    ├── generate_index.py      从清单、store.json 和发布记录生成索引
    └── sync_examples.py       从 MovieClaw 重新复制 example/ 里的参考插件
```

`.github/workflows/ci.yml` 在 PR 上校验。`release.yml` 在 tag 上发版。

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
2. 清单只写服务器认识的字段。作者、主页、许可证、图标写在同目录的 `store.json`。
3. 开 PR。CI 用钉死的 MovieClaw 版本（v0.37.0，提交 `e229717c43b2d48986f8f51c19b86348a527cb5a`）跑 `Manifest` / `check_compat`，并跑该目录自己的测试。改了哪个 `plugins/<id>/` 就查哪个。`example/` 有改动时会再查 `hello-note`，并按 `example/UPSTREAM.json` 再查参考副本（只跑 `check_plugin.py`；参考副本没有带上不能单独跑的测试）。
4. 合并之后打 tag：`<id>-v<版本>`，例如 `watch-presence-v1.0.0`。tag 里的版本必须和清单 `version` 逐字相同。

## 版本、tag 和 Release

一个插件一个 Release。附件名用 `<id>-<版本>.mcplugin`，例如 `watch-presence-1.0.0.mcplugin`。

tag 用最后一个 `-v` 加数字版本拆开，所以 id 里的连字符不会和版本打架。

**所有 Release 都带 `--latest=false`，包括下面的索引 Release。** 不要把 `/releases/latest` 当成「某个插件的最新版」，也不要当成「商店索引」。谁最后发布，GitHub 的 latest 就是谁。

每个 id 的最新版写在索引里。带 `-` 后缀的预发布版本不会盖过同号的正式版。

## 索引

| 什么 | 在哪 |
|---|---|
| 给人看的源文件 | 本仓库 `main` 上的 [`index.json`](index.json)，格式见 [`index.schema.json`](index.schema.json)（schema **1**） |
| 每个插件的包 | 该插件自己的 Release 附件，地址跟着 tag 不变 |
| 给应用拉的那一份 | 长期存在的 Release，tag 固定叫 `catalog`，反复覆盖 `index.json` |

应用应下载这一个地址，不要按插件去打 GitHub Releases API：

`https://github.com/movieclaw/movieclaw-plugins/releases/download/catalog/index.json`

发版时 CI 按当时的 `plugins/*/movieclaw-plugin.toml`、`store.json` 和已有发布记录重生索引，再覆盖到 `catalog`。配了 Actions secret `RELEASE_SIGNING_KEY`（Ed25519 私钥 PEM，生成方式与 MovieClaw 的 `scripts/gen-release-signing-key.sh` 相同）时，同时上传 `index.json.sig`：对 `index.json` 的原始字节做 Ed25519，签名是 base64 再加一个换行，和主仓库 `manifest.json.sig` 一样。可选 secret `RELEASE_SIGNING_KEY_ID` 写入索引的 `key_id`，缺省是 `catalog-1`。公钥不写进索引，以后跟 MovieClaw 版本走。没配密钥时只发布 sha256，索引里 `signed` 为 false。

`main` 上的 `index.json` 在发版成功后由同一条工作流提交，方便看 diff。应用以内的下载地址仍是 `catalog` 附件。

## 许可证

**TODO：本仓库还没有选定许可证。** 在选定并加入 `LICENSE` 之前，不要把这里的代码当成已经授予再分发许可。插件的 `store.json` 可以先写「见仓库许可证」，选定后一起改。

## English

Official MovieClaw plugin source and releases. One GitHub Release per plugin, tag `<id>-v<version>`, asset `<id>-<version>.mcplugin`. Every release is published with `--latest=false`. The machine-readable catalog is schema 1: `index.json` on `main`, and the copy clients should fetch at `.../releases/download/catalog/index.json` (optional `index.json.sig`, Ed25519 over the raw file, same construction as MovieClaw app-update manifests). Authoring notes are in `example/GUIDE.md`. `plugins/` is empty on purpose. Reference copies of MovieClaw's bundled plugins and `watch-presence` live under `example/<id>/` (commit recorded in `example/UPSTREAM.json`); they are not indexed or released. `example/hello-note` is the template to copy. The repository license is not chosen yet; see the TODO above.
