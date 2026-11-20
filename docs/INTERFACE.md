# greenbubbles-win 接口说明（供其他 Agent 接入）

版本：`0.2.1`　适用：Windows 本机、微信 4.x 已登录并运行
维护：本机只读工具链，不写微信目录
实测日期：2026-11-19（实测记录见附录 A）

---

## 0. 三句话

1. **三种入口，同一份数据契约**：CLI（一次性调用）、HTTP Connector（常驻、推荐给其他 Agent）、MCP（stdio，给支持 MCP 的宿主）。
2. **所有查询返回同一个信封** `greenbubbles.query.v1`：`{schema, formatVersion, operation, ok, source, consistency, page, warnings, items}`。
3. **两条硬约束**：① 全库关键词搜索默认走本地文本索引（`index build` 后实测 **2.1 秒 CLI / 0.8 秒 HTTP**）；索引没建或加 `--no-index` 时退回逐行解码，罕见词 **48–65 秒**，所以**能带 `conversation` 就带**（单会话 1.5 秒、结果最新）；② 每次调用最多 **500 条**，超出静默截断，靠 `page.nextCursor` 翻页。

> 索引建一次约 74–100 秒（846k 条消息，380 MB），之后每次增量建；`warnings[].code=searchIndexUsed` 表示本次结果来自索引，可能不含索引构建后的新消息。

---

## 1. 前置条件

| 项 | 要求 |
| --- | --- |
| 系统 | Windows（本机微信 4.1.11.24 实测） |
| 微信状态 | **已登录且进程在运行**（取密钥要读 `Weixin.exe` 内存；密钥失效会自动重取） |
| Python | 便携包自带 `python\`；源码开发用 `.venv`；入口都是 `greenbubbles.cmd` |
| 数据 | 默认自动发现账号；异常时用 `--db-root <账号目录>\db_storage` 显式指定 |
| 权限 | 普通用户即可；不需要管理员（只读 `OpenProcess`） |

自检（任何接入前先跑一次）：

```powershell
C:\path\to\greenbubbles-win\greenbubbles.cmd source status
```

关注三个数：`keys.databases`（本机 32）、`readiness.conversations`（2826）、`readiness.messageShards`（13）。任一为 0 说明微信没开或数据源没找对。

---

## 2. 通用数据契约

### 2.1 信封

```json
{
  "schema": "greenbubbles.query.v1",
  "formatVersion": 1,
  "operation": "messages.list",
  "ok": true,
  "source": {"mode": "live", "identity": "acct_<hash>"},
  "consistency": {
    "guarantee": "perDatabaseReadStatement",
    "databaseCount": 1,
    "crossDatabaseAtomic": true,
    "coverageComplete": true,
    "observedAtUnixMilliseconds": 1795000000000
  },
  "page": {"limit": 500, "returned": 500, "hasMore": true, "nextCursor": "eyJzIjoxNzk..."},
  "warnings": [],
  "items": []
}
```

- `consistency.observedAtUnixMilliseconds` **每次都会变**，做结果比对时要先剔除这个字段。
- `coverageComplete=false` + `warnings[].code=conversationNotFound` 表示会话不存在（**`ok` 仍是 true**，不要只看 `ok`）。
- `crossDatabaseAtomic` 仅在单分片内为 true；跨分片是逐库读，不保证同一时刻快照。

### 2.2 消息对象（`items[]`）

| 字段 | 说明 |
| --- | --- |
| `id` | 不透明游标 ID（base64），**原样传回**给 `/message`、`/media`、`message get` |
| `conversationId` | `xxx@chatroom` 或 wxid |
| `sortSequence` | 毫秒排序键，翻页与去重依据 |
| `serverId` | 服务端消息号 |
| `messageType` / `messageTypeLabel` | 1 text、3 image、34 voice、43 video、47 emoji、48 location、49 app、10000 system、10002 revoke |
| `messageSubtype` / `messageSubtypeLabel` | 仅 app 有效：6 file、57 quote、19 merged_messages、2000 transfer、2001 red_envelope 等 |
| `sender` / `senderDisplayName` | 发送者 id / 显示名（群内为成员 wxid） |
| `isSelf` | 是否本人发出 |
| `createdAtUnix` / `createdAt` | 秒 / ISO8601（+08:00） |
| `content` | 结构化：`{"Text": "..."}`、`{"Image": {"md5": "..."}}`、`{"App": {...}}` 等 |
| `text` | 解码后的纯文本（**上限 16,000 字符**，超出时 `contentTruncated=true`） |
| `contentDecodeState` | `complete` / 其它解码异常状态 |
| `shard` | 命中的分片文件（`message/message_0.db` 等） |
| `messageLocalId` | 该分片内的行号，可当数字 id 传给 `message get` |
| `media` | 仅 `media=1/include_media` 时出现：`{kind, available, fileName, extension, size, note}` |

### 2.3 会话对象

`id`、`displayName`、`summary`、`summaryTruncated`、`sortTimestamp`、`sortTime`、`lastMessageType`、`lastMessageSender`、`lastSenderDisplayName`。

### 2.4 搜索对象

`id`、`conversationId`、`conversationDisplayName`、`sender`、`senderDisplayName`、`createdAtUnix`、`createdAt`、`sortSequence`、`messageLocalId`、`messageType`、`messageTypeLabel`、`messageSubtype`、`messageSubtypeLabel`、`snippet`（前 200 字符）、可选 `media`。

### 2.5 时间与游标

- 时间入参：CLI 支持 `YYYY-MM-DD`、`YYYY-MM-DD HH:MM`、`YYYY-MM-DD HH:MM:SS`、unix 秒；HTTP 额外支持 `YYYY-MM-DDTHH:MM:SS`。
- `since` / `until` 均为**闭区间**，按消息 `create_time` 过滤。
- 游标：只能使用上一页返回的 `page.nextCursor`，不要自己拼；`hasMore=false` 即到底。

---

## 3. CLI

入口：

```powershell
C:\path\to\greenbubbles-win\greenbubbles.cmd <命令> [参数]
# 或
C:\path\to\greenbubbles-win\.venv\Scripts\python.exe C:\path\to\greenbubbles-win\greenbubbles.py <命令>
```

### 3.1 命令矩阵

| 命令 | 关键参数 | 说明 |
| --- | --- | --- |
| `source status` | `--db-root` | 账号、密钥数、缓存、就绪度 |
| `key capture` | `--out` | 重新从内存取密钥（一般不用手动跑） |
| `key verify` | `--key-file` | 校验密钥是否仍匹配当前库 |
| `chats list` | `--limit` `--cursor` | 最近会话（`chats` 单独写等价于 `chats list`） |
| `chats rank` | `--limit` `--minimum-self-messages` | 按本人发言量排序 |
| `chats find <名称或id>` | `--limit` | 模糊匹配名称/备注/昵称/wxid |
| `messages list` | `--conversation`（必填）`--since` `--until` `--limit` `--cursor` `--with-media` | 单会话消息，新→旧 |
| `messages search` | `--query`（必填）`--conversation` `--since` `--limit` `--with-media` `--no-index` | 带 `--conversation` 走实时扫描；不带则优先用本地索引 |
| `index build` | `--conversation` `--rebuild` `--quiet` | 建/更新本地文本索引（不写出明文到微信目录） |
| `index status` | — | 索引行数、会话数、构建时间、文件大小 |
| `message get` | `--conversation` `--message-id` | 单条；id 用游标 id 或数字 local id |
| `contacts list` | `--limit` `--details` `--kind` | 联系人/群/公众号 |
| `archive update` | `--out` `--conversation` `--full` `--attachments` | 增量归档（MD/JSONL/HTML/index.sqlite） |
| `archive export` | `--out` `--formats md,jsonl,html` `--attachments` | 一次性导出 |
| `serve` | `--host` `--port` | 启动 HTTP Connector（默认 127.0.0.1:8787） |
| `mcp` | — | stdio MCP server |
| `snapshot create/restore/info` | `--file` `--out-dir` `--mnemonic` | 加密快照（BIP39 + Argon2id + ChaCha20-Poly1305） |

通用输出参数：`--format json`（默认）/`pretty`/`jsonl`。
`--format jsonl` 每行输出一个 `item`；游标不再丢失，会以 `[greenbubbles] nextCursor=<...>` 的形式打到 **stderr**（最后一页打 `lastPage=true`）。翻页仍建议直接读 json 信封里的 `page.nextCursor`。

### 3.2 示例

```powershell
# 找群
greenbubbles.cmd chats find 项目群 --format pretty

# 取某群 8/10 之后的前 100 条
greenbubbles.cmd messages list --conversation 12345678901@chatroom --since 2026-08-10 --limit 100

# 翻页（把 nextCursor 原样传入）
greenbubbles.cmd messages list --conversation 12345678901@chatroom --limit 100 --cursor "eyJzIjo..."

# 单会话内搜关键词（1.5 秒量级，实时）
greenbubbles.cmd messages search --query keyword --conversation 12345678901@chatroom --limit 20

# 全库搜（先建索引，之后 2 秒量级；不带 conversation 会自动走索引）
greenbubbles.cmd index build
greenbubbles.cmd messages search --query keyword --limit 100

# 带附件元信息
greenbubbles.cmd messages list --conversation 12345678901@chatroom --limit 10 --with-media
```

### 3.3 退出码与错误语义

| 情况 | 退出码 | 表现 |
| --- | --- | --- |
| 正常 | 0 | 信封 `ok:true` |
| 会话不存在 | 0 | `ok:true`、`items:[]`、`warnings:[{code:"conversationNotFound"}]`、`coverageComplete:false` |
| 时间格式错 | 1 | stderr `cannot parse time: xxx`，无 JSON |
| 缺必填参数 | 2 | argparse 报错（如 `the following arguments are required: --conversation`） |
| 密钥过期/微信未开 | 1 或阻塞 | stderr 有 `capturing database keys` / `no WeChat process` 提示 |

**每次进程启动约 1.3–1.5 秒**（加载密钥 + 打开缓存）；批量任务建议走常驻 HTTP。

---

## 4. HTTP Connector（推荐给其他 Agent）

### 4.1 启动

```powershell
C:\path\to\greenbubbles-win\start-connector.cmd
# 等价于
greenbubbles.cmd serve --host 127.0.0.1 --port 8787

# 局域网其他主机要访问时（仅可信内网；务必配防火墙来源白名单）
greenbubbles.cmd serve --host 0.0.0.0 --port 8787
```

- 首次启动会把 token 写入 `C:\path\to\greenbubbles-win\state\token.txt`，并打印路由表。
- 默认只监听回环地址；`--host 0.0.0.0` 才会监听局域网，此时是明文 HTTP，请限制来源 IP。

### 4.2 鉴权

三种方式任选（token 相同）：

```
Authorization: Bearer <token>
X-Greenbubbles-Token: <token>
GET /api/v1/messages?...&token=<token>
```

- `/health` 与 `/` **免鉴权**；其余路由缺 token 或错误一律 `401`。
- `?token=` 便于浏览器/简易客户端，但可能落进代理日志，生产建议用 Header。

### 4.3 路由表

| 方法 | 路径 | 参数 | 成功 | 失败 |
| --- | --- | --- | --- | --- |
| GET | `/health` | — | 200 `{ok, service, routes}` | — |
| GET | `/guide` | — | 200 `text/markdown`（Agent 接入说明原文） | 404 文档缺失 / 401 |
| GET | `/api/v1/source` | — | 200 状态信封 | 401 |
| GET | `/api/v1/chats` | `limit` `cursor` `q` | 200 会话信封 | 401 |
| GET | `/api/v1/messages` | `conversation`(必填) `since` `until` `limit` `cursor` `media`(默认 1，`0` 关闭) `sanitize` | 200 消息信封（默认带 `media` 摘要） | 400 缺 conversation / 400 时间格式 / 401 |
| GET | `/api/v1/search` | `q`(必填) `conversation` `since` `limit` `media` `index` | 200 搜索信封 | 401 |
| GET | `/api/v1/contacts` | `limit` `details` `kind` | 200 联系信封 | 401 |
| GET | `/api/v1/message` | `conversation` `id` | 200 单条信封（`items` 0 或 1） | 401 |
| GET | `/api/v1/media` | `conversation` `id` | 200 **二进制**（`Content-Type` 按扩展名，`X-Media-Valid: true`，`X-Media-Format`、`X-Media-Decrypt` 说明实际 XOR key；wxgf 另带 `X-Original-Format`） | 422 `mediaValidationFailed`（解密后结构校验失败）/ 404 无附件 / 401 |
| * | 其它 | — | — | 404 `{ok:false,error,get routes}` |

`conversation` 可传 `xxx@chatroom`、wxid，或显示名（内部会 `resolve`）。
`/api/v1/search` 不带 `conversation` 时默认走本地索引（返回 `warnings[].code=searchIndexUsed`）；`index=0` 强制实时扫描，`media=1` 逐条回填附件元信息。

### 4.4 示例

> 下面的 `state/token.txt` 是 **Windows 本机** 的相对路径；从其他机器经 SSH 隧道调用时，
> 需先把 token 取到调用方（见 README 的「Agent access」/中文版「跨机：调用方建隧道并取令牌」），
> 不能直接在调用方 `cat` 这个路径。

```bash
TOKEN=$(cat <repo>/state/token.txt)

curl -s "http://127.0.0.1:8787/health"

# 取给 Agent 的接入说明（Markdown，可直接当提示词）
curl -s -H "Authorization: Bearer $TOKEN" "http://127.0.0.1:8787/guide"

curl -s -H "Authorization: Bearer $TOKEN" \
  "http://127.0.0.1:8787/api/v1/messages?conversation=12345678901@chatroom&since=2026-08-10&limit=100"

# 搜关键词（带 conversation，勿全省略）
curl -s -H "Authorization: Bearer $TOKEN" \
  "http://127.0.0.1:8787/api/v1/search?q=overflow&conversation=12345678901@chatroom&limit=20"

# 取某条图片消息的附件（id 来自 messages 的 items[].id）
curl -s -H "Authorization: Bearer $TOKEN" \
  "http://127.0.0.1:8787/api/v1/media?conversation=12345678901@chatroom&id=<id>" -o out.png
```

### 4.5 并发与锁

服务是 `ThreadingHTTPServer` + 单个 `Store` + `RLock`：并发请求安全，但查询**串行执行**。实测 8 个并发 `messages` 请求总耗时 0.04 秒、错误 0。大批量抓取仍建议单线程顺序翻页，避免互相排队。

---

## 5. MCP Server（stdio）

### 5.1 启动与握手

```json
{"mcpServers":{"greenbubbles":{"command":"C:\\path\\to\\greenbubbles-win\\python\\python.exe","args":["C:\\path\\to\\greenbubbles-win\\greenbubbles.py","mcp"]}}}
```

协议：JSON-RPC 2.0，逐行（NDJSON），`protocolVersion: 2024-11-05`，serverInfo `{"name":"greenbubbles-win","version":"0.2.1"}`。
协议版本说明：客户端若请求 `2025-06-18`，服务端仍回 `2024-11-05`（规范允许服务端返回自己支持的版本）；握手后按 2024-11-05 语义工作，主流宿主（Claude Desktop / Cherry Studio / Cursor）均可正常接入。
stderr 只打日志（`[greenbubbles] mcp server ready on stdio`），**stdout 只输出 JSON 响应**。

> 从源码运行时，把 `python\python.exe` 换成 `.venv\Scripts\python.exe`。

握手序列：`initialize` → 通知 `notifications/initialized`（无响应）→ `tools/list` → `tools/call`。

### 5.2 六个工具

| 工具 | 入参 | 返回 |
| --- | --- | --- |
| `list_conversations` | `query?`（模糊名称/wxid）、`limit?`（1–500，默认 30） | 会话信封 |
| `get_messages` | `conversation`（必填）、`since?`、`until?`、`limit?`（默认 50）、`cursor?`、`include_media?` | 消息信封 |
| `search_messages` | `query`（必填）、`conversation?`、`since?`、`limit?`（默认 30）、`include_media?` | 搜索信封 |
| `list_contacts` | `limit?`、`details?`、`kind?`（person/group/official/service/accountHolder） | 联系信封 |
| `get_message` | `conversation`、`id`（必填） | 单条信封 |
| `source_status` | — | 状态信封 |

`limit` 统一语义：缺省取各工具默认值；显式传 `0`、负数或非数字按 **1** 处理（不再被 `or` 吞成默认值）；上限 500。

`tools/call` 的 `result.content[0].text` 是**字符串化的信封 JSON**，需要再 `JSON.parse` 一次。

### 5.3 示例会话

```json
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05"}}
{"jsonrpc":"2.0","method":"notifications/initialized"}
{"jsonrpc":"2.0","id":2,"method":"tools/list"}
{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"get_messages","arguments":{"conversation":"12345678901@chatroom","since":"2026-08-10","limit":50}}}
```

### 5.4 错误语义

| 情况 | 响应 |
| --- | --- |
| 未知方法 | `{"error":{"code":-32601,"message":"method not found: ..."}}` |
| 未知工具 | `result.isError=true`，文本为异常信息 |
| 非法 JSON 行 | `{"jsonrpc":"2.0","id":null,"error":{"code":-32700,"message":"parse error"}}` |
| 通知类消息 | 不回包（不产生 stdout 输出） |

> **注意**：上表的 `-32601` 和 `isError=true` 是**故意调用不存在的方法/工具**得到的负向测试结果，用来证明错误路径可预期，不代表 MCP 故障。正常握手、6 个工具、中文/emoji 文本、翻页均已实测通过（见附录 A.2）。

MCP 层**没有鉴权**（进程由宿主启动，天然本机）；需要跨进程/跨语言时用 HTTP。

---

## 6. 性能实测（本机，缓存已预热）

| 操作 | CLI | HTTP |
| --- | --- | --- |
| `source status` | 1.27 s | 0.17 s |
| 会话查找 | 1.40 s | 0.13 s |
| 793 条消息全量翻页（8 页 × 100） | 11.65 s | 0.43 s |
| 单会话按时间取 294 条 | 1.50 s | 0.24 s |
| 单会话关键词搜索 | 1.49 s | — |
| **全库关键词搜索（有索引，104 命中）** | **1.8–2.1 s**（含 1.3 s 启动） | **0.78 s** |
| 全库关键词搜索（无索引，实时扫描） | 49.5–53.4 s | 同量级 |
| 索引全量构建（846,248 条） | 74 s，380 MB | — |
| 8 并发请求 | — | 0.04 s / 0 错误 |
| 取一张图片附件 | — | 200, `image/png`, 83 KB |

> 0.2.1 起，不带 `conversation` 的搜索默认读本地文本索引（`state\search-index.sqlite3`），从 48–65 秒降到 2 秒量级，且与实时扫描的结果集逐条一致（104 vs 104，零差异）。索引没建、或加 `--no-index` / HTTP `index=0` 时仍走原来的逐行解码路径。

---

## 7. 给 Agent 的推荐用法

**配方 A：读某群某时间段（最常用）**

```
GET /api/v1/chats?q=<群名>            → 拿 conversationId
GET /api/v1/messages?conversation=<id>&since=<起>&until=<止>&limit=500
  → 循环 page.nextCursor 直到 hasMore=false
```

**配方 B：找一句话在哪**

先 `chats?q=` 缩小范围，再 `messages?conversation=` 拉下来本地过滤；或 `search?q=&conversation=`。**不要**裸调 `/api/v1/search?q=`。

**配方 C：取附件**

`messages?media=1` 拿 `media.available/extension`；`available=false` 表示微信侧没落盘（别重试）；需要字节就 `GET /media?conversation=&id=`。

判定口径统一：先看 `ok`，再看 `warnings`，最后看 `page.hasMore`；`items` 为空不等于出错。

---

## 8. 安全与只读保证

- 全程只读：密钥从 `Weixin.exe` 内存读取（`OpenProcess` 只读 + `VirtualQueryEx`），数据库页级解密到 `state\cache`，**不修改微信任何文件**。
- HTTP 默认只绑 `127.0.0.1`（`--host 0.0.0.0` 时暴露到局域网，仅限可信内网），token 为 32 字节 URL-safe 随机串，`secrets.compare_digest` 比较。
- 归档快照可选加密：BIP39 24 词 + Argon2id(m=64MiB,t=3,p=4) + ChaCha20-Poly1305，header 作 AAD。
- 不要把 `state\token.txt`、`state\keys.json`、`state\cache` 提交到任何仓库或聊天工具。

---

## 9. 已知限制（接线前必读）

| 限制 | 影响 | 规避 |
| --- | --- | --- |
| 索引未建 / 强制实时搜索时 48–65 s | 超时/被上游杀进程 | 先 `index build`；或带 `conversation`（1.5 s） |
| 索引可能滞后 | 搜不到索引构建后的新消息 | 新增消息后增量 `index build`（只补新行，秒级到分钟级） |
| 索引占磁盘约 380 MB | 磁盘占用 | `index build --rebuild` 可重建；删除 `state\search-index.sqlite3` 即回退实时扫描 |
| 单页上限 500（静默截断） | 以为取全了其实没有 | 必读 `page.hasMore/nextCursor` |
| `--format jsonl` 丢游标 | 无法翻页 | 0.2.1 已修：游标打到 stderr；翻页仍建议用 json |
| 会话不存在仍 `ok:true` | 误判成功 | 检查 `warnings` / `coverageComplete` |
| 同一 `sort_seq` 可能有多条（分片迁移） | 去重口径差异 | 搜索/列表按 `(会话, sort_seq)` 保留第一条；需要另一条用 `message get` 按 local id 取 |
| 语音（msg_type 34）未解实体 | 只有 `"Voice"` 占位 | 暂不支持 |
| wxgf 是 HEVC 容器，需要转码 | 直接打开是二进制 | 返回 `image/heif` 并校验解码；转码自行执行 `ffmpeg -f hevc` |
| 附件可能缺失（微信未下载） | `media.available=false` / `404` | 记录缺口，不要重试 |
| 依赖微信运行 | 微信退出后取不到新密钥 | 缓存密钥在 `state\keys.json`，仍可读旧库 |
| 仅 Windows | 换平台不可用 | — |
| 无增量推送 | 只能轮询 | 定时任务 + `since` |

---

## 10. 版本与变更

- `0.2.1`（当前）：新增本地文本索引 `index build/status`，全库搜索 48–65 s → 2.1 s（HTTP 0.8 s）；`--format jsonl` 游标写到 stderr；MCP `limit=0` 按 1 处理；MCP serverInfo 版本号 0.2.1。
- `0.2.0`：CLI / HTTP / MCP 三入口，6 个 MCP 工具，8 条 HTTP 路由，附件实体（图片/文件/视频），加密快照。
- 契约变更原则：`schema` 字段保持 `greenbubbles.query.v1`；新增字段只加不删；`operation` 值不复用。

---

## 附录 A：2026-11-19 实测记录

被测对象：`C:\path\to\greenbubbles-win`，会话 `12345678901@chatroom`（某个群聊）。

| 检查项 | 结果 |
| --- | --- |
| CLI `source status` | exit 0，32 库 / 2826 会话 / 13 分片 / 18493 联系人，1.27 s |
| CLI 全量翻页 | 8 页，793 条，去重后 793，无丢失（与原始分片表 `Msg_<hash>` 530+263 完全一致） |
| CLI 时间过滤 | `since 2026-08-10` → 294 条，`hasMore=false` |
| CLI 确定性 | 同一命令两次输出一致（剔除 `observedAtUnixMilliseconds`） |
| CLI 单条回取 | `message get` 返回同 `id`，1.45 s |
| CLI 错误码 | 会话不存在→exit 0 + warning；时间错→exit 1；缺参→exit 2 |
| HTTP 鉴权 | `/health` 200；无 token / 错 token 均 401；`?token=` 与 Header 均 200 |
| HTTP 翻页 | 8 页，793 条，去重 793，与 CLI 完全一致 |
| HTTP 媒体 | 200，`image/png`，83,345 字节 |
| HTTP 错误码 | 缺 conversation→400；未知路由→404；时间错→400；图片校验失败→422 `mediaValidationFailed` |
| HTTP 并发 | 8 线程，0 错误，0.04 s |
| MCP 握手 | `initialize` 返回 `2024-11-05`，`tools/list` 6 个工具 |
| MCP 调用 | `source_status`/`get_messages`/`search_messages` 正常；翻页两页 0 重叠 |
| MCP 错误 | 未知方法 `-32601`；未知工具 `isError:true`；通知不输出；非法 JSON `-32700` |
| 排序键唯一性 | 793 条 `sort_seq` 全部唯一（530+263），翻页无并列丢消息风险 |
| 全局搜索成本 | 罕见词 48.15 s / 有命中词 64.74 s；单会话 1.49 s |

### A.2 0.2.1 修复后回归（2026-11-19 下午）

| 检查项 | 结果 |
| --- | --- |
| 索引全量构建 | 846,248 条消息 / 2554 会话，74–100 s，`search-index.sqlite3` 380 MB |
| 索引 vs 实时结果一致性 | 同一关键词：索引 104 条 / 实时 104 条，命中集合零差异 |
| 全库搜索耗时 | CLI 2.07–2.11 s（原 49.5–53.4 s）；HTTP 0.78 s |
| jsonl 游标 | 100 行 item + stderr `nextCursor=...`（可翻页） |
| MCP `limit=0` | 返回 1 条（原为默认 50） |
| MCP 正常调用 | `search_messages` 100 条、`get_messages` 中文/emoji 正常，serverInfo 版本 0.2.1 |
| MCP 协议回退 | 客户端请求 2025-06-18 → 服务端仍回 2024-11-05（规范允许） |
| 会话不存在 | `ok:true` + `conversationNotFound` + `coverageComplete:false`（语义未变） |
| 已知边界 | 分片迁移会产生同 `sort_seq` 的两条不同消息，索引三列主键全保留，搜索按 live 口径去重 |

结论：三入口数据一致、分页无丢失、错误语义可预期；**可发布，但必须带上第 9 节的两条硬约束**。
