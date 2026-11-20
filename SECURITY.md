# Security policy

## What this tool touches

- It opens WeChat's own database files **read-only** and never writes, renames or
  deletes anything under the WeChat data directory.
- It captures the SQLCipher key by reading the memory of the already-running
  `Weixin.exe` process (same-account, same-machine, the technique also used by
  the macOS upstream).
- It writes decrypted copies, keys and an optional search index under `state/`
  (or `GREENBUBBLES_HOME`), which is excluded from git.
- The HTTP connector binds `127.0.0.1` by default and requires a bearer token.
  Passing `--host 0.0.0.0` exposes it on the LAN over plain HTTP; do that only on
  a trusted network, and prefer an SSH tunnel for remote access.
- No chat content, keys or metadata are uploaded anywhere; the tool makes no
  outbound network calls.

## Reporting a vulnerability

Open a private security advisory on GitHub, or contact the maintainer directly.
Please do not file a public issue for:

- key-extraction or decryption bypasses;
- token bypass on the connector;
- SQL injection / path traversal in query parameters;
- snapshot format weaknesses (KDF parameters, nonce reuse, MAC handling).

Include reproduction steps and the affected version. We aim to acknowledge within
72 hours.

## Hardening checklist for users

- Keep `state/` on an encrypted volume if the machine is shared.
- Do not expose port 8787 beyond loopback (no port forwarding, no reverse proxy).
- Treat `state/keys.json` as a password: it unlocks the whole message history.
- When moving to a new machine, use `snapshot create` and carry the 24-word
  phrase separately from the vault file.
