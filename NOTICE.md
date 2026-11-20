# NOTICE

This repository is an independent Windows implementation of
[GreenBubbles](https://github.com/bojieli/greenbubbles) by Bojie Li, which is
distributed under the MIT License.

- Upstream project: GreenBubbles (macOS, Swift)
- Upstream copyright: Copyright (c) 2026 Bojie Li
- Windows port: Copyright (c) 2026 seaneer (https://github.com/seaneer) and 豆包工作
- Reviewed upstream commit: `69f19c7089d7ef011e9ba43d44c6fb4e6e5be98b`
- Upstream license text: see `LICENSE` in this repository (MIT, with the
  upstream copyright line retained)

No upstream Swift source code is vendored here. The Windows port reproduces the
upstream data model and query contract (`greenbubbles.query.v1`) with an
independent Python implementation. Behavioural details (message type mapping,
`sortSequence` ordering, the read-only guarantee, the MCP tool names) follow the
upstream project so that agents written for the macOS build keep working.

Two additional MIT-licensed projects informed the Windows-specific formats:
`wx-cli` (`.dat` image container) and `wcdb-key-tool` (Windows key-capture
object layout). Their copyright lines and license text are reproduced in
`THIRD_PARTY_NOTICES.md`.

The database, decryption and media notes used while porting came from public
reverse-engineering research; no third-party source code is copied into this
repository.
