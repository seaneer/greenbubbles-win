# Privacy

GreenBubbles for Windows is a local-only tool. This document describes exactly
what stays on your machine and what never leaves it.

## Stays local

| Data | Location | Purpose |
| --- | --- | --- |
| SQLCipher keys | `state/keys.json` | decrypting the local databases |
| Decrypted database copies | `state/cache/<account>/db/` | fast, indexed queries |
| Plain-text search index | `state/search-index.sqlite3` | full-library search in seconds |
| Connector token | `state/token.txt` | authenticating local HTTP clients |
| Exported archives and attachments | wherever you pass `--out` | your own backup |
| Encrypted snapshot | wherever you pass `--file` | portable backup, passphrase protected |

## Leaves the machine

Nothing. The program does not call home, does not embed analytics, and does not
upload chat content, contacts, media or keys. The only network socket it opens is
the loopback HTTP listener you start explicitly with `serve`.

## Third parties

Your use of WeChat itself, and any agent you connect to the local connector, is
governed by those products' own policies. The connector exposes an authenticated
API on `127.0.0.1`; anything you choose to send to a cloud model afterwards is
outside this project's control.

## Your responsibilities

- Only read accounts you own.
- Keep `state/` and exported archives out of cloud-synced folders if you do not
  want them copied.
- Delete `state/` when you no longer need the decrypted cache; it can be rebuilt
  from the live databases as long as the key is still capturable.
