---
name: greenbubbles-win
description: 读取和归档本机 Windows 微信 4.x 的聊天记录，并可对外提供只读 HTTP / MCP 接口。用户要求查看微信会话、按联系人或群按时间段取消息、搜索聊天内容、导出/归档聊天记录（含图片文件附件）、生成加密快照，或让其他 agent 接入本地微信数据时使用。仅适用于本机已登录的微信账号，只读、离线。
---

# greenbubbles for Windows

只读访问本机微信 4.x 数据库，输出 `greenbubbles.query.v1` 风格 JSON；也可作为常驻服务供其他 agent 接入。
所有命令在项目目录执行：

```
C:\path\to\greenbubbles-win\greenbubbles.cmd <子命令>
```

## 前置检查

1. 微信必须**登录且运行**（取密钥靠读 `Weixin.exe` 内存；已有 `state/keys.json` 时可直接读旧库）。
2. 先跑 `source status`，确认 `keys.databases > 0`、`readiness.conversations > 0`、`media.v2Key` 为 `derived`。
   数据源缺失时用 `--db-root <账号目录>\db_storage` 显式指定。

## 常用命令

| 目的 | 命令 |
| --- | --- |
| 环境与就绪状态 | `source status` |
| 取/校验密钥 | `key capture` / `key verify` |
| 最近会话 | `chats --limit 20` |
| 按名字找会话 | `chats find 关键词` |
| 按自己的发言量排序 | `chats rank --limit 20 --minimum-self-messages 5` |
| 读指定会话 + 时间段 | `messages list --conversation <id或备注> --since 2026-11-01 --until 2026-11-19 --limit 100` |
| 翻页 | 把返回的 `page.nextCursor` 传给 `--cursor` |
| 全文搜索 | `messages search --query 关键词 [--conversation X] --limit 20` |
| 全库搜索提速 | `index build`（建一次约 74–100 秒 / 380 MB），之后不带 `--conversation` 的搜索为 2 秒级；`index status` 查状态 |
| 单条消息 | `message get --conversation X --message-id <cursor id 或 local_id>` |
| 带附件信息 | 上述命令加 `--with-media`，消息里多出 `media` 块 |
| 通讯录 | `contacts list --limit 50 --details` |
| 一次性导出（含附件） | `archive export --out <目录> --formats md,jsonl,html --attachments [--conversation X]` |
| 增量归档 | `archive update --out <目录> [--conversation X] [--attachments]` |
| 起常驻 HTTP | `serve --port 8787`（令牌在 `state/token.txt`） |
| 起 MCP | `mcp`（stdio，工具：list_conversations/get_messages/search_messages/list_contacts/get_message/source_status） |
| 加密快照 | `snapshot create --out-dir <归档目录> --file <vault>` / `snapshot restore --file <vault> --mnemonic "..." --out-dir <目录>` |

## 给其他 agent 用：优先 HTTP

若用户要的是「让别的 agent 读指定联系/群某段时间的记录」，起 `serve` 后让对方请求：

```
GET http://127.0.0.1:8787/api/v1/messages?conversation=<id>\
    &since=2026-11-19%2009:00&until=2026-11-19%2012:00&limit=200
Authorization: Bearer <state/token.txt 内容>
```

- 会话 id 先用 `/api/v1/chats?q=关键词` 拿到，不要猜。
- 时间参数接受 `YYYY-MM-DD`、`YYYY-MM-DD HH:MM`、ISO 或 Unix 秒；边界含端点。
- 附件二进制：`/api/v1/media?conversation=&id=`；`/health` 不需要令牌。

## 输出约定

- 默认单行 envelope：`{schema, formatVersion, operation, ok, source, consistency, page, warnings, items}`。
- `--format jsonl` 逐条输出 `items`，适合管道。
- 消息关键字段：`conversationId`、`sender`、`senderDisplayName`、`isSelf`、`createdAt`、`messageTypeLabel`、`messageSubtypeLabel`、`text`、`media`。
- 时间统一 `+08:00`；`sortSequence` / `sortTimestamp` 可作稳定排序键。

## 使用建议

- **先定位会话再读消息**：`chats find` / `/api/v1/chats?q=` 得 id，比全库搜索快得多。
- **全库搜索前先建索引**：`index build`，之后无 `conversation` 的搜索 2 秒级（返回 `searchIndexUsed` 警告）；索引可能滞后，要最新结果就带 `conversation` 或加 `--no-index`。
- **跨分片已自动合并**：同一会话可能分布在多个 `message_*.db`，按 `sort_seq` 去重，不要用 `shard` 字段自行拼接。
- **归档用 update 而非反复 export**：`update` 按 watermark 增量写、幂等；`export` 会全量重写。
- **附件耗 IO**：`--attachments` 会复制实体文件，单会话可达数百 MB，先小范围试。
- **快照助记词只显示一次**：`snapshot create` 输出里带 24 词，务必让用户当场抄走，丢了无法恢复。
- **隐私**：回答只引用与问题相关的片段，不要整段导出私人对话，也不要暴露 `keys.json` 内容或原始 wxid。

## 禁止

- 不要修改、删除、移动微信目录下任何文件。
- 不要上传聊天内容或密钥到网络。
- 不要把连接器端口转发到公网；不要读取他人账号数据。
