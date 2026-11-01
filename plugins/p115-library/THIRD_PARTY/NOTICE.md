# Third-party notices

`p115_rsa.py` is an adapted RSA-only subset of p115cipher 0.0.6, copyright ChenyangGao (https://chenyanggao.github.io). Its source header declares GPLv3 and its included LICENSE is GPLv3. This distribution follows GPL-3.0-or-later. The wheel metadata's MIT label conflicts with the included source/license; the GPL license is preserved here rather than relying on that metadata label.

Changes: extracted RSA constants and functions only; replaced filewrap's byte-view helper with built-in memoryview; added deferred annotations. No AES, LZ4, executable tools, binary extension or p115client dependency is included. The complete modified source is supplied in this package; LICENSE is at package root.

115 wire contracts were checked against p115client 0.0.9.7.2 client.py and MoviePilot p115disk's use of that version. MoviePilot modules are not imported and have not been copied wholesale. STRM integration follows MovieClaw v0.35.0's cloud_strm example using host operations, files and signed routes.

Sources:
- https://pypi.org/project/p115cipher/0.0.6/
- https://pypi.org/project/p115client/0.0.9.7.2/
- https://github.com/DDSRem-Dev/MoviePilot-Plugins/tree/main/plugins.v3/p115disk
- https://github.com/DDSRem-Dev/MoviePilot-Plugins/tree/main/docs/p115strmhelper
- https://github.com/movieclaw/MovieClaw/blob/v0.35.0/src/movieclaw_agent/builtin-skills/movieclaw-plugin-dev/references/examples/cloud_strm.py

## 1.1.0 新增

p115_aes.py、p115_upload_crypto.py 来自 p115cipher 0.0.6 的 AES/上传协议代码（ChenyangGao，GPL-3.0-or-later）；提取纯 Python 实现、替换依赖并增加 LZ4 边界检查。原许可证见 p115cipher-LICENSE_zh。
qrcode/ 为 python-qrcode 8.2（BSD 3-Clause），去除测试和缓存；许可证见 qrcode-LICENSE。
上传 wire contract 参考 p115oss 0.1.2（https://pypi.org/project/p115oss/0.1.2/）；插件异步上传和队列实现独立编写。
