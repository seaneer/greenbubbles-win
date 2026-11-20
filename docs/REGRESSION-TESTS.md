# 回归测试设计（历史 bug 逐条对应）

项目：`greenbubbles-win`　版本：`0.2.1`　执行日期：2026-11-19
入口：`run-tests.cmd`（等价于 `.venv\Scripts\python.exe -m unittest discover -s tests -t . -v`）

当前结果：**67 个用例，1 个按设计跳过，其余通过，约 135 秒**（本机、微信已登录、缓存已预热、索引已建）。

---

## 1. 设计原则

1. **一条历史 bug 至少一个用例**，用例名或注释里带 bug 编号，回归时能直接对上事故。
2. **期望值从分片现算，不写死数字**。793 / 294 这类数字只出现在报告归档用例里（那些文件本身是静态产物）；消息分页、时间过滤的期望值都由 `tests/support.py::shard_truth()` 直接扫解密后的 `Msg_*` 表算出，微信收到新消息也不会误报。
3. **接口行为按“契约”而不是“实现”断言**：CLI / HTTP / MCP 三个入口分别断言，同一份期望；避免只测内部函数导致入口回归漏网。
4. **负向路径优先**：错误码、未知会话、未知工具、坏 JSON、错误 token、错误助记词都单独成例——这些是最容易被“优化”掉的分支。
5. **重资源用例不拖慢日常回归**：全库实时扫描（~50 秒）不进默认套件，只在接口一致性用例里用单会话实时扫描做对照；索引性能用 8 秒预算兜底。

---

## 2. 历史 bug → 用例矩阵

| 编号 | 历史故障 | 根因 | 对应用例 | 断言要点 |
| --- | --- | --- | --- | --- |
| BUG-001 | 索引每段对话最后不足一批的消息被丢弃（793 只入库 600） | 每段结尾 `del pending` 把未 flush 的尾巴清掉，watermark 却已前移 | `test_index_regressions.IndexBuildCompletenessTests::test_every_shard_row_is_indexed`、`test_watermark_matches_newest_message`、`test_incremental_build_adds_nothing_when_unchanged` | 索引行数 == 分片 distinct `(sort_seq, local_id)` 数；watermark == 最大 `sort_seq`；重复构建新增 0 |
| BUG-002 | 两条不同消息共用 `sort_seq`，后者覆盖前者（丢 2 条命中） | 索引主键 `(conversation, sort_seq)` | `test_index_regressions.SortSeqCollisionTests::test_all_colliding_rows_survive` + `test_units.IndexDedupeTests` | 数据驱动地找真实冲突会话，断言索引内该 `sort_seq` 的 distinct `local_id` 数与分片一致；`_dedupe` 保留第一条 |
| BUG-003 | 导出的图片被命名成 `.dat`（扩展名丢失） | 解密后没有按magic回填扩展名 | `test_media_cache.MediaEntityTests::test_extension_and_size_come_from_plaintext`、`test_decrypted_bytes_match_the_extension`；`test_units.MediaDetectTests` | `media.extension` 非空且不为 `dat`；字节 magic 与扩展名一致；`size == len(data)` |
| BUG-004 | MCP `limit: 0` 被 `or` 吞成默认值 50/30 | `arguments.get("limit") or 50` | `test_units.McpLimitTests`、`test_http_mcp.McpProtocolTests::test_limit_zero_returns_one` | 0/负数 → 1；字符串数字可解析；9999 → 500；缺省才用默认 |
| BUG-005 | 全库关键词搜索 48–65 秒，Agent 调用超时 | 逐会话逐行解码，无索引 | `test_index_regressions.GlobalIndexPerformanceTests::test_unscoped_search_is_fast_and_warns`、`IndexParityTests::test_index_matches_live_scan` | 索引搜索 < 8 秒；索引命中集合 == 单会话实时扫描集合；返回 `searchIndexUsed` 警告 |
| BUG-006 | 相对路径写错，`message/message_0.db` 被拼到 `db_storage` 下（FileNotFoundError） | 基准目录用了 `cache.parent` 而非 `cache/db` | `test_media_cache.CachePathTests::test_shard_relative_path_resolves_under_cache_db`、`test_all_shards_open` | 解密库真实存在于 `cache/db/<rel>`；13 个分片全部可打开并查询 |
| BUG-007 | 第一版报告把 `read.txt` 指向了 README（附件错配） | 人工映射，未做落盘校验 | `test_media_cache.AttachmentArtifactTests::test_attachment_index_rows_match_disk`、`test_message_dump_is_parseable_and_unique` | 索引里每个附件路径都存在且非空；`file-missing` 恰好 1 条；294 条消息可解析且 `(会话, sort_seq)` 唯一 |
| BUG-008 | 误把 MCP 的 `-32601` / `isError=true` 当成故障 | 负向测试结果被当成缺陷 | `test_http_mcp.McpProtocolTests::test_unknown_method_returns_minus_32601`、`test_unknown_tool_sets_is_error`、`test_parse_error_returns_minus_32700` | 未知方法 → `-32601`；未知工具 → `isError`；坏 JSON → `-32700`；通知不回包 |
| BUG-009 | jsonl 模式丢 `page.nextCursor`，Agent 无法翻页 | `emit` 只打印 items | `test_cli_contract.EmitFormatTests::test_jsonl_emits_cursor_on_stderr`、`test_jsonl_last_page_marks_completion` | stderr 含 `nextCursor=`，用该游标能取到下一页 100 条；末页 `lastPage=true` |
| BUG-010 | `limit=2000` 静默截断到 500 却不告知 | 只做上限截断 | `test_units.LimitClampTests`、`test_cli_contract.ClampAndErrorTests::test_oversized_limit_is_clamped` | `page.limit == 500`，`returned <= 500`；0/负数回落默认 100 |
| BUG-011 | 会话不存在仍 `ok:true`、exit 0，Agent 误判成功 | 契约设计（软失败） | `test_cli_contract.ClampAndErrorTests::test_unknown_conversation_is_soft_failure`、`test_bad_time_exits_one`、`test_missing_required_argument_exits_two` | `warnings[0].code=conversationNotFound`、`coverageComplete=false`；时间错 exit 1；缺参 exit 2 |
| BUG-012 | 加密快照用错误助记词也可能“恢复成功”的担心 | 需要显式校验 checksum / AEAD | `test_units.SnapshotTests::test_roundtrip_and_wrong_phrase` | 正确短语可还原文件；另一组合法 24 词必须 `SnapshotError`；目标目录已存在时拒绝覆盖 |
| BUG-013 | 编码导致中文/emoji 在管道里乱码或崩进程 | Windows 控制台编码 | `test_cli_contract.ClampAndErrorTests::test_emoji_survives_the_pipe`、`test_http_mcp.McpProtocolTests::test_unicode_payload_survives` | 11-03 的 👌 在 CLI 与 MCP 输出里原样存在 |
| BUG-014 | MCP 协议版本不匹配（客户端请求 2025-06-18） | 服务端固定 2024-11-05 | `test_http_mcp.McpProtocolTests::test_handshake_and_tool_list` | 回 `2024-11-05`、`serverInfo.version=0.2.1`、6 个工具、通知无响应 |
| BUG-015 | V2 图片尾部 XOR key 硬编码 0x37，4.1.11 实际为 0x1c，PNG/JPEG/wxgf 批量损坏 | 单一 key，无格式校验 | `test_media_validation.AdaptiveXorKeyTests` | 同一明文分别用 0x37 / 0x1c 封装，均能自动选对 key 并还原；未知 key 标 `unverified` |
| BUG-016 | 解密失败的图片仍以 200 + `image/jpeg` 返回 | 无结构校验 | `test_media_validation.ValidationTests`、`test_http_mcp.HttpContractTests` | 截断 PNG / 缺 EOI JPEG / 无 NAL wxgf 均判 invalid；HTTP 对 invalid 图片回 422 `mediaValidationFailed` |
| BUG-017 | `/api/v1/source` 会话数只在 `readiness.conversations`，文档按顶层取值得到 null；原始 JSON 无法脱敏 | 字段层级与导出模式缺失 | `test_http_mcp.HttpContractTests::test_source_exposes_conversations_alias`、`test_sanitize_strips_internal_ids` | 顶层 `conversations` == `readiness.conversations`；`sanitize=1` 去掉 `sender/conversationId/id` 并加 `sanitizedOutput` |

---

## 3. 非 bug 的核心契约用例（防新回归）

| 层面 | 用例 | 覆盖点 |
| --- | --- | --- |
| 分页 | `PagingCompletenessTests::test_every_page_is_disjoint_and_complete` | 8 页、跨页零重复、总数 == 分片真值、页数 == ceil(n/100) |
| 时间过滤 | `test_time_filter_matches_shard_truth`、`test_since_boundary_is_inclusive` | `since` 闭区间；边界消息必须出现 |
| CLI/HTTP 一致性 | `HttpContractTests::test_paging_matches_cli` | HTTP 翻页总数与分片真值一致 |
| HTTP 鉴权 | `test_health_is_open`、`test_missing_token_is_401`、`test_wrong_token_is_401`、`test_query_token_is_accepted` | `/health` 免鉴权；Header 与 `?token=` 等价；错误 token 401 |
| HTTP 接入说明 | `test_guide_serves_the_agent_prompt` | `/guide` 无 token 401；有 token 返回 `text/markdown`，含接入流程与路由 |
| HTTP 错误 | `test_unknown_route_is_404`、`test_missing_conversation_is_400`、`test_bad_time_is_400` | 404/400 语义 |
| 媒体下载 | `test_media_returns_binary_with_type` | 200、`image/png`、字节以 PNG magic 开头 |
| 索引开关 | `test_index_toggle` | 默认带 `searchIndexUsed`；`index=0` 实时；索引结果 ⊆ 实时结果 |
| 信封稳定性 | `test_observed_at_changes_between_calls` | `observedAtUnixMilliseconds` 每次变化（比对时须剔除） |

---

## 4. 执行方式与前置

```powershell
# 全量（约 64 秒）
C:\path\to\greenbubbles-win\run-tests.cmd

# 只跑单元层（无需微信/数据库，秒级）
C:\path\to\greenbubbles-win\.venv\Scripts\python.exe -m unittest tests.test_units -v

# 只跑某一层
... -m unittest tests.test_index_regressions -v
... -m unittest tests.test_http_mcp -v
```

前置条件：

1. `state\keys.json` 有有效密钥（微信曾登录并跑过 `source status`）；
2. 解密缓存存在（首次会触发解密，套件会慢几十秒）；
3. `state\search-index.sqlite3` 已构建，否则 `GlobalIndexPerformanceTests` 自动 skip；
4. 微信不需要正在运行（除 `key capture` 相关用例，本套件不测它）。

跳过策略：样例窗口里没有可解密图片、或没有 `sort_seq` 冲突会话时，相应用例 `skipTest` 并打印原因，不会假通过。

---

## 5. 未自动化的部分（手工/下次补）

| 项 | 原因 | 建议 |
| --- | --- | --- |
| 密钥捕获（`key capture`） | 需要真实 `Weixin.exe` 进程与内存布局，不适合 CI | 发布前手工跑 `source status`，看 32/32 |
| wxgf XOR key 与解码 | `ffmpeg` 不一定装在 CI 机器上 | 有 ffmpeg 时按解码结果选 key；无 ffmpeg 时按 tail 内 HEVC NAL 数量判定（`test_media_validation` 覆盖两种分支） |
| 语音（msg_type 34） | 产品尚未实现解码 | 暂无用例；实现后补 silk → wav 与时长断言 |
| 归档大文件（18 MB `write.txt`、`.rar`） | 套件只校验索引与落盘，不做内容复算 | 需要时单独跑日志审计脚本 |
| MCP 宿主（Claude Desktop/Cursor）真实握手 | 套件用 NDJSON 直连，等价但非宿主 | 新增宿主配置改动时手工冒烟 |

---

## 6. 维护约定

- 新增/修改 `query.py`、`index.py`、`mcp.py`、`service.py`、`cli.py` 的行为，**必须同时加/改用例**，并在上表登记编号。
- 当前 BUG-001 / BUG-002 / BUG-009 的断言已在修复前后复现过（索引 600 vs 793、命中 102 vs 104、jsonl 无游标 vs 有游标），用例把这些现象固化成可重复检查。
- 数据相关断言一律走 `shard_truth()` 或 `locate_all()` 现算；禁止把某天的消息总数写死（报告归档用例除外）。
- 套件运行时间超过 2 分钟时，优先把慢用例改成“单会话对照”，而不是删断言。
