# Changelog

Notable changes to GreenBubbles for Windows. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## Unreleased

### Changed

- Repository reorganised to mirror the upstream layout: the package lives in
  `src/greenbubbles_win/`, diagnostics moved to `scripts/diagnostics/`, and
  community files (`LICENSE`, `CONTRIBUTING.md`, `SECURITY.md`, `PRIVACY.md`,
  `NOTICE.md`, `THIRD_PARTY_NOTICES.md`, `.github/`) were added.

### Fixed

- **Media decryption no longer assumes XOR key `0x37`.** WeChat 4.1.11.24 writes
  the tail XOR key per batch; observed `0x1c` on 2026-11-03/11-17 images. The
  resolver now tries known keys (`0x1c`, `0x37`) and then brute-forces 0–255
  for non-`wxgf` files, validating each candidate structurally (PNG chunk CRC,
  JPEG decode, GIF trailer, BMP/WEBP declared size). `wxgf` uses an `ffmpeg`
  HEVC decode as the oracle; without `ffmpeg` it falls back to NAL counting.
  This fixes PNG CRC failures, JPEGs without EOI and corrupted `wxgf` previews.
- Media that fails validation is now reported instead of served: `/api/v1/media`
  returns `422 mediaValidationFailed` and successful responses carry
  `X-Media-Valid`, `X-Media-Format`, `X-Media-Decrypt` (and
  `X-Original-Format` for `wxgf`).
- `GET /api/v1/messages` now includes a `media` summary by default, so callers
  no longer need one `/api/v1/media` request per attachment; pass `media=0` for
  the old slim payload.
- `/api/v1/source` exposes the conversation count as a top-level
  `conversations` alias next to `readiness.conversations`.
- `messages` and `search` accept `sanitize=1`, which removes `sender`,
  `conversationId` and `id` from every item and adds a `sanitizedOutput`
  warning for redacted exports.
- CLI and MCP now switch stdin/stdout/stderr to UTF-8. Windows pipes used the
  ANSI code page (for example cp936), so messages containing characters outside
  that code page crashed with `UnicodeEncodeError`.
- The connector redacts `?token=` values from its access log instead of writing
  bearer tokens to `state/logs/`.
- `snapshot restore` rejects absolute paths, `..` traversal and link members
  before extracting a vault.
- `state_dir()` resolved to `src/state` after the package moved, which made the
  connector mint a second token and decrypt a duplicate cache. The project root
  is now detected from `pyproject.toml` / `greenbubbles.py`, so state stays at
  `<repo>/state`.
- `requirements.lock` pins `pycparser` 2.23 so the Python 3.10 CI leg can
  resolve; pycparser 3.x requires Python 3.11 or newer.

### Added

- `scripts/build-portable.ps1` builds a portable ZIP with an embedded Python
  3.12 runtime, so end users do not need Python, pip or a compiler. CI builds and
  smoke-tests the ZIP on every run.
- `start-connector-bg.cmd` / `stop-connector.cmd` / `connector-status.cmd` for
  manual background operation (PID file, timestamped logs, no autostart).
- `start-connector-local.cmd` / `stop-connector-local.cmd` for SSH-tunnel mode:
  loopback-only bind, so the service never appears on the LAN.
- `GET /guide` serves `docs/AGENT-PROMPT.md` over the connector, so a remote
  agent only needs the base URL and a token to onboard itself.
- `pyproject.toml` with a `greenbubbles` console script and the previously
  undeclared `mnemonic` / `argon2-cffi` dependencies.
- Test modules now skip cleanly when no captured account is present, so CI can
  run without WeChat data.

### Changed

- `connector-service.ps1` now defaults to `127.0.0.1`. The A-mode script passes
  `-BindHost 0.0.0.0` explicitly (trusted LAN only), while the B-mode script
  stays loopback-only for SSH tunnels.
- Local account ids, customer names, absolute paths and one-off diagnostic
  scripts are no longer part of the published tree. Data-dependent tests read
  their fixture from `GREENBUBBLES_TEST_*` environment variables.
- `THIRD_PARTY_NOTICES.md` now records the reviewed commits and MIT notices for
  `greenbubbles`, `wx-cli` and `wcdb-key-tool`.

## 0.2.1 - 2026-11-19

### Added

- Local plain-text search index (`index build|status|drop`), backing unscoped
  `messages search` and `/api/v1/search`. 846k messages searchable in ~2 s
  instead of 48–65 s.
- Regression suite: 55 cases covering 14 historical bugs, contract paging,
  HTTP/MCP error paths and media export (`docs/REGRESSION-TESTS.md`).

### Fixed

- Index dropped the last partial batch of each conversation (793 rows only 600
  indexed) and advanced the watermark anyway.
- Two distinct messages sharing a `sort_seq` overwrote each other, losing
  matches; the index now keys on `(conversation, sort_seq, local_id)`.
- `messages search --format jsonl` no longer swallows `page.nextCursor`.
- MCP `limit: 0` is clamped to 1 instead of being replaced by the default; the
  server reports the applied limit in `page.limit`.

## 0.2.0 - 2026-11-18

### Added

- Attachment materialisation: images (`.dat` → real extension), files and
  videos copied into the archive tree; `--with-media` adds a `media` block to
  query results.
- Encrypted snapshots (`snapshot create|restore`) using a BIP39 24-word phrase,
  Argon2id (64 MiB, t=3, p=4) and ChaCha20-Poly1305.
- `archive export|update` with Markdown, JSONL and HTML output, watermark-based
  incremental runs.

## 0.1.0 - 2026-11-17

### Added

- Read-only key capture from the running `Weixin.exe` process (32/32 databases
  verified) and SQLCipher page decryption with WAL replay.
- Conversation discovery, message paging by `sort_seq`, time filtering and
  the `greenbubbles.query.v1` response envelope.
- HTTP connector on `127.0.0.1:8787` (8 routes, bearer token) and an MCP stdio
  server (6 tools).
