# 架构说明

本文说明 GreenBubbles for Windows 从「微信进程内存里的密钥」到「Agent 拿到的一段聊天记录」之间发生了什么，以及为什么这样分层。

## 设计目标

1. **只读**：不写、不改、不删微信目录里的任何文件；解密结果落在本项目自己的 `state/`。
2. **离线可用**：密钥和密文都在本地，查询不依赖网络，也不依赖微信在线（只要有缓存密钥）。
3. **契约稳定**：对外输出沿用上游 macOS 版的 `greenbubbles.query.v1`，Agent 的调用代码不因平台变化而改。
4. **可审计**：每个阶段都能单独验证（密钥 32/32 HMAC、分片行数、页码、附件落盘）。

## 分层

```
┌───────────────────────────── 对外接口层 ─────────────────────────────┐
│  CLI (cli.py)      HTTP Connector (service.py)      MCP stdio (mcp.py)  │
│  archive export/update (archive.py)      encrypted snapshot (snapshot) │
└───────────────────────────────┬──────────────────────────────────────┘
                                │  greenbubbles.query.v1 envelope
┌───────────────────────────────┴──────────────────────────────────────┐
│  查询层  query.py（分页 / since-until / 搜索 / 单条 / 媒体信息）        │
│  索引层  index.py（本地明文索引，只服务“不带 conversation”的搜索）     │
└───────────────────────────────┬──────────────────────────────────────┘
┌───────────────────────────────┴──────────────────────────────────────┐
│  存储层  store.py（定位分片、打开解密库、跨分片按 sort_seq 合并）       │
│  内容层  content.py（zstd 解压、XML/protobuf 字段提取、类型映射）       │
│  媒体层  media.py（.dat 头解析、AES-128-ECB 分段、XOR 尾、扩展名判定） │
└───────────────────────────────┬──────────────────────────────────────┘
┌───────────────────────────────┴──────────────────────────────────────┐
│  解密层  crypto.py（SQLCipher 4 页解密、WAL 回放、manifest 增量刷新）   │
│  密钥层  keys.py（读取运行中 Weixin.exe 的内存，XOR 掩码解出密钥）      │
│  发现层  paths.py（账号目录、db_storage、媒体目录、GREENBUBBLES_HOME）  │
└───────────────────────────────┬──────────────────────────────────────┘
                                │ 只读打开
                    微信本地数据（%数据目录%\db_storage、msg\）
```

模块与上游的对应关系：`Sources/` 下的 Swift 类型在这里被拆成同职责的 Python 模块，命名尽量保持语义一致（`query`、`content`、`media`、`keys`），便于对照阅读。

## 1. 密钥获取（Windows 特有）

macOS 版从 Keychain 里拿密钥；Windows 没有等价的可读存储，密钥只存在于已登录的 `Weixin.exe` 进程堆里。

1. 枚举同名进程，`OpenProcess(PROCESS_VM_READ | PROCESS_QUERY_INFORMATION)`；
2. 用 `VirtualQueryEx` 遍历可读提交页，逐页 `ReadProcessMemory`；
3. 匹配 `x'<64 hex encKey><32 hex salt>'` 形态的候选串；UTF-16 存储还要做一次 XOR 掩码还原；
4. 用候选密钥对 32 个库做 HMAC-SHA512 校验，**32/32 全过才落盘** `state/keys.json`。

这一步全部是读操作：不注入线程、不写目标进程内存、不改文件。

## 2. 数据库解密

微信 4.x 的库是 SQLCipher 4 变体：页大小 4096，每页尾部保留 80 字节（16 字节 IV + 64 字节 HMAC-SHA512），正文 AES-256-CBC。

解密流程：

1. 校验第 1 页 HMAC，确认密钥与库匹配；
2. 按页解密正文，重建标准 SQLite 文件；
3. **回放 `-wal`**：未 checkpoint 的事务只存在于 WAL，必须按 WAL 帧重放到主库，否则会丢最新消息（这也是历史上“消息数比手机少”的主要原因）；
4. 写入 `state/cache/<account>/db/<相对路径>`，并在 `manifest.json` 记录 `source_mtime` / `wal_mtime` / 行数；源文件没变就跳过，变了才重解。

13 个分片库（`message_0…10`、FTS、resource）连同联系人等库共 15 个，全部解密后约 745 MB，首次十几秒到几十秒，之后增量。

## 3. 会话与消息模型

- 会话标识是 `username`（群为 `<id>@chatroom`，个人为 `wxid_*`）；消息表名 = `Msg_` + `md5(username)`，同一会话可能因分片迁移散落在多个 `message_*.db`。
- 每条消息的稳定排序键是 `sort_seq`（服务端序号），同一 `sort_seq` 理论上唯一；实测存在迁移造成的重复，因此去重键是 `(sort_seq, local_id)`，列表保留第一条，需要另一条时用 `message get` 按 local_id 精确取。
- 分页游标就是 `sort_seq`，`since` / `until` 是闭区间，时间统一按 `+08:00` 解释。

## 4. 内容解码

`content` 列可能是 zstd 压缩的 XML/文本，也可能是结构化字段。`content.py` 负责：

- 先尝试 zstd 解压，再按消息类型提取 `text`、引用、文件信息、位置、转账等子结构；
- 映射 `msg_type`：1 文本 / 3 图片 / 34 语音 / 43 视频 / 47 表情 / 48 位置 / 49 应用消息（6 文件、57 引用、19 合并转发、2000 转账、2001 红包）/ 10000 系统 / 10002 撤回；
- 文本截断上限 16,000 字符，保留 `rawType` 便于调用方自行判断。

## 5. 附件实体

图片 `.dat` 的结构：15 字节头（V2 魔数 `07 08 56 32 08 07`）+ 若干 AES-128-ECB 加密段 + 尾部 XOR。

- V2 密钥 = `md5(uin + wxid)[:16]` 的 **16 个 ASCII 字节**（不是二进制摘要）；
- 解密后按魔数判定真实扩展名（PNG/JPEG/GIF/WEBP/wxgf），不再输出 `.dat`；
- 文件消息直接取 `msg\file\<YYYY-MM>\<title>`，视频取 `msg\video\<YYYY-MM>\<md5>.mp4`；
- 微信未下载的附件返回 `available:false` / HTTP 404，而不是伪造空文件。

## 6. 本地索引

全库搜索如果逐会话实时解码需要 48–65 秒。`index build` 把所有会话的纯文本写入 `state/search-index.sqlite3`：

- 主键 `(conversation, sort_seq, message_local_id)`，避免 BUG-002 的覆盖；
- 按会话分批写入，每批结束前 flush，watermark 取本批最大 `sort_seq`（BUG-001）；
- 重跑只补新增行；`meta.complete=0` 表示部分索引，搜索会带 `searchIndexUsed` 警告；
- 明文索引只在本机，`state/` 已被 `.gitignore` 排除。

## 7. 服务化

| 入口 | 适合 | 鉴权 |
| --- | --- | --- |
| CLI | 人、脚本、一次性任务 | 本机文件权限 |
| HTTP Connector | 其他 Agent、跨进程、跨语言 | `Authorization: Bearer <state/token.txt>`，仅 `127.0.0.1` |
| MCP stdio | Claude Desktop / Cursor 等宿主 | 进程继承 |

三者共用 `query.py`，因此分页、时间过滤、警告码完全一致；HTTP 只是把 envelope 序列化，MCP 把同一 envelope 放进 `content[0].text`。

## 8. 加密快照

`snapshot create` 用于把归档搬到另一台机器：

- BIP39 24 词作为人类可抄写的口令；
- Argon2id（m=64 MiB, t=3, p=4）派生密钥；
- ChaCha20-Poly1305 加密 tar.gz，头部做 AAD；
- 恢复时必须提供正确助记词，错误短语在 checksum 或 AEAD 阶段失败。

## 9. 为什么用 Python 而不是移植 Swift/Rust

上游是 Swift + SQLCipher C 依赖，Windows 上没有可用的 Swift 工具链组合；Rust 重写需要自建 SQLCipher 与 protobuf 解码，收益低于成本。Python 版本：

- 直接用 `cryptography` / `pycryptodome` 做页级与分段解密，行为可逐字节验证；
- 单文件分发简单（`.cmd` + venv），Agent 侧接入成本最低；
- 性能瓶颈（全库搜索）用索引解决，实测 2 秒级，已满足 Agent 交互。

代价是单次进程启动约 1.3–1.5 秒，因此推荐长驻 HTTP 而不是每条消息起一次 CLI。
