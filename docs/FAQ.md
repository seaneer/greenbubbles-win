# FAQ

**它会修改我的微信数据吗？**
不会。所有数据库都以只读方式打开，解密结果写到本项目的 `state/cache/`，微信目录一个字节都不动。

**需要微信一直开着吗？**
只有“第一次取密钥”需要。密钥落盘后，即使微信退出也能读已缓存的库；但微信重新登录换了密钥时，需要再跑一次 `source status`。

**为什么搜不到刚收到的消息？**
走的是本地索引，索引不会自动跟随微信更新。跑一次 `index build`（增量，几秒到几分钟）即可；或对单个会话用 `messages search --conversation <id>` 走实时扫描。

**支持 macOS / Linux 吗？**
不支持。macOS 请直接用上游 [bojieli/greenbubbles](https://github.com/bojieli/greenbubbles)。

**能读别人的账号吗？**
不能，也不接受这类需求。密钥只能从当前用户正在运行的微信进程里取。

**语音、视频能播放吗？**
视频消息的文件会复制出来（`msg\video\...mp4`）；语音消息目前只有占位信息，尚未解码 silk。

**多个微信号怎么办？**
每个账号有独立的数据目录与库文件。用 `--db-root` 指向对应账号的 `db_storage`，并让 `GREENBUBBLES_HOME` 指向独立的状态目录，避免缓存混用。

**换电脑怎么迁移？**
`snapshot create --out-dir <目录> --file backup.gbvault`，把 24 个助记词单独抄走。新机器 `snapshot restore` 后即可离线查询，不需要微信在线。

**为什么不上 Rust / 直接编译上游？**
Windows 上没有可用的 Swift + SQLCipher 组合；Rust 重写要重建 SQLCipher 与 protobuf 解码，成本高于收益。Python 版逐字节验证过解密，性能瓶颈用索引解决。

**会不会把聊天记录上传？**
不会。程序没有任何主动外连；HTTP 默认只绑定 `127.0.0.1`（A 模式显式开 `0.0.0.0`，仅限可信内网），且需要 `state/token.txt` 里的令牌。

**怎么让 Claude Desktop / Cursor 用上？**
在宿主配置里加一个 stdio MCP server，命令指向 `greenbubbles.cmd mcp`，工作目录设为项目根。详见 `docs/INTERFACE.md` 第 5 节。
