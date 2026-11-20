# Third-party notices

GreenBubbles for Windows is an independent Python implementation. It does not
vendor the source tree of any of the projects below; their formats and
algorithms informed this implementation.

## Runtime dependencies

See `pyproject.toml` for the pinned minimum versions.

| Package | License | Used for |
| --- | --- | --- |
| `cryptography` | Apache-2.0 / BSD-3-Clause | HMAC-SHA512 verification, AES-CBC page decryption |
| `pycryptodome` | BSD-2-Clause | AES-128-ECB media segments, ChaCha20-Poly1305 snapshots |
| `zstandard` | BSD-3-Clause | Decompressing `content` columns that were zstd-packed |
| `mnemonic` | MIT | BIP39 24-word recovery phrases for encrypted snapshots |
| `argon2-cffi` | MIT | Argon2id key derivation for snapshots |

## Reference implementations

The following MIT-licensed projects were used as references. Copyright notices
are retained here as required by the MIT License.

### GreenBubbles (upstream, macOS)

- Copyright (c) 2026 Bojie Li
- Repository: https://github.com/bojieli/greenbubbles
- Reviewed commit: `69f19c7089d7ef011e9ba43d44c6fb4e6e5be98b`
- Used for: the `greenbubbles.query.v1` envelope, message-type mapping,
  `sortSequence` ordering, read-only design and MCP tool names.
- No Swift source is included in this repository.

### wx-cli

- Copyright (c) 2026 pandorafuture
- Repository: https://github.com/pandorafuture/wx-cli
- Reviewed commit: `2abe708f55bfe135539a385df856fdc58f97fc74`
- Used for: the WeChat 4.x `.dat` image container layout and the V1 fixed key
  documented in `src/greenbubbles_win/media.py`.

### wcdb-key-tool

- Copyright (c) 2025 CloudDreamAI / TANGandXUE
- Repository: https://github.com/TANGandXUE/wcdb-key-tool
- Reviewed commit: `79f1b5b92e12c66aa281b4a60a3c478b5f547dfa`
- Used for: the Windows `com.Tencent.WCDB.Config.Cipher` object layout and XOR
  mask documented in `src/greenbubbles_win/keys.py`. That project in turn
  credits kkocdko, wxchat-export and ylytdeng/wechat-decrypt.

## MIT License text

Permission is hereby granted, free of charge, to any person obtaining a copy of
this software and associated documentation files (the "Software"), to deal in
the Software without restriction, including without limitation the rights to
use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of
the Software, and to permit persons to whom the Software is furnished to do so,
subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

## Trademarks

WeChat, Weixin and Tencent are trademarks of Tencent Holdings Ltd. This project
is not affiliated with or endorsed by Tencent.
