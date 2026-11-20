# Connector 与 MCP：给其他 Agent 的接入说明

两种接入方式，数据同源、只读、令牌鉴权：

1. **常驻 HTTP 服务**（`greenbubbles serve`）—— 适合任何能发 HTTP 的 Agent / 脚本 / 工作流。
2. **MCP stdio 服务**（`greenbubbles mcp`）—— 适合支持 MCP 的客户端（Claude Desktop、Codex、Cursor 等）。

两者都读同一份本地解密缓存，不会写微信目录。

## 一、常驻 HTTP 服务

```powershell
cd C:\path\to\greenbubbles-win
.\greenbubbles.cmd serve --port 8787
```

- 只监听 `127.0.0.1`（默认），不会暴露到局域网；`start-connector-bg.cmd` 会显式绑 `0.0.0.0`，仅限可信内网。
- 令牌自动生成并落盘：`state\token.txt`。请求时三选一：
  - `Authorization: Bearer <token>`
  - `X-Greenbubbles-Token: <token>`
  - `?token=<token>`

### 端点

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 免鉴权探活，返回路由清单 |
| GET | `/api/v1/source` | 账号、版本、密钥数、分片数、媒体密钥状态 |
| GET | `/api/v1/chats?limit=&cursor=&q=` | 会话列表；带 `q` 时按名字/id 模糊查找 |
| GET | `/api/v1/messages?conversation=&since=&until=&limit=&cursor=&media=` | 指定会话 + 时间范围的消息 |
| GET | `/api/v1/search?q=&conversation=&limit=&since=` | 全文搜索（可限定会话） |
| GET | `/api/v1/contacts?limit=&details=&kind=` | 通讯录 |
| GET | `/api/v1/message?conversation=&id=` | 单条消息 |
| GET | `/api/v1/media?conversation=&id=` | 该消息的附件实体（图片已解密为原格式） |

- `since` / `until` 接受 `2026-11-19`、`2026-11-19 10:00`、ISO 或 Unix 秒。
- 返回体统一是 `greenbubbles.query.v1` envelope：`items` 里每条消息含 `sender`、`senderDisplayName`、`isSelf`、`createdAt`、`text`、`messageSubtypeLabel`。
- `media=1` 时消息里会多一个 `media` 块（`kind/available/extension/size/note`）；取二进制请用 `/api/v1/media`。

### 示例：只取某个群今天 9 点到 12 点的消息

```powershell
$t = (Get-Content state\token.txt -Raw).Trim()
curl.exe -s -H "Authorization: Bearer $t" ^
  "http://127.0.0.1:8787/api/v1/messages?conversation=12345678901@chatroom&since=2026-11-19%2009:00&until=2026-11-19%2012:00&limit=200"
```

## 二、MCP stdio 服务

```powershell
cd C:\path\to\greenbubbles-win
.\greenbubbles.cmd mcp
```

协议：JSON-RPC 2.0 over stdio，版本 `2024-11-05`，工具集：

| 工具 | 作用 | 关键参数 |
| --- | --- | --- |
| `list_conversations` | 列会话 / 按名字找 | `query`、`limit` |
| `get_messages` | 读某会话消息 | `conversation`、`since`、`until`、`limit`、`cursor`、`include_media` |
| `search_messages` | 全文搜索 | `query`、`conversation`、`since`、`limit` |
| `list_contacts` | 通讯录 | `limit`、`details`、`kind` |
| `get_message` | 单条消息 | `conversation`、`id` |
| `source_status` | 数据源体检 | — |

### 注册示例（Claude Desktop / Codex 一类客户端）

```json
{
  "mcpServers": {
    "greenbubbles": {
      "command": "C:\\path\\to\\greenbubbles-win\\python\\python.exe",
      "args": [
        "C:\\path\\to\\greenbubbles-win\\greenbubbles.py",
        "mcp"
      ]
    }
  }
}
```

Codex 的 `config.toml`：

```toml
[mcp_servers.greenbubbles]
command = "C:\\path\\to\\greenbubbles-win\\python\\python.exe"
args = ["C:\\path\\to\\greenbubbles-win\\greenbubbles.py", "mcp"]
```

从源码运行时，把 `python\\python.exe` 换成 `.venv\\Scripts\\python.exe`。

## 三、并发与安全

- HTTP 侧是 `ThreadingHTTPServer` + 单把 `RLock`：并发请求会排队，sqlite 连接按请求串行复用，不会互相踩。
- 只读：所有 sqlite 连接都是 `mode=ro` + `query_only=1`，不写微信目录。
- 令牌是 43 字符 URL-safe 随机串，比较用 `secrets.compare_digest`，避免时序侧信道。
- 不要把这些端点转发到公网；若要跨机访问，走 SSH 隧道或内网反向代理，并自行加一层鉴权。
