# Contributing

Thanks for helping make Windows WeChat history readable by its owner — and
nobody else.

## Scope

This repository is a Windows port of [GreenBubbles](https://github.com/bojieli/greenbubbles).
Useful contributions include:

- Windows WeChat 4.x version compatibility (new `db_storage` layouts, new
  message types, new media containers);
- message/attachment decoding that is currently missing (voice `msg_type 34`,
  truncated `wxgf` frames, `.rar` attachments);
- CLI / HTTP / MCP contract fixes that keep the upstream `greenbubbles.query.v1`
  envelope intact;
- regression tests for any bug you fix.

Out of scope: writing to WeChat's databases, sending messages, bypassing
authentication of accounts you do not own, cloud sync of chat content.

## Development setup

```powershell
git clone <your-fork>
cd greenbubbles-win
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\pip install -e .
```

You need a logged-in Windows WeChat 4.x account for the data-dependent tests.
`state/` is gitignored and must never be committed.

The data-dependent tests skip by default so a clean clone and CI stay green.
To run them against your own account, set a conversation id (and optionally a
search term with enough hits):

```powershell
$env:GREENBUBBLES_TEST_CONVERSATION = "12345678901@chatroom"
$env:GREENBUBBLES_TEST_QUERY = "keyword"
run-tests.cmd
```

Never commit your real ids, search terms or report fixtures; the environment
variables are deliberately the only place they belong.

## Tests

```powershell
run-tests.cmd                      # full suite, ~64 s, needs a captured account
.venv\Scripts\python -m unittest tests.test_units -v    # unit layer, seconds
```

- Every bug fix needs a test that fails before and passes after.
- Register the test in `docs/REGRESSION-TESTS.md` under the bug matrix.
- Never hard-code a message count; derive it from the shards
  (`tests/support.py::shard_truth`).

## Pull requests

1. Keep the change focused; one concern per PR.
2. Explain the root cause, not only the symptom.
3. Run the full suite and paste the last lines (`Ran N tests ... OK`).
4. Do not attach `keys.json`, `state/`, `reports/`, chat logs, screenshots of
   real conversations, or `*.gbvault` files.
5. Follow the existing style: 4-space indents, `from __future__ import
   annotations`, typed public functions, no new runtime dependencies without a
   discussion.

## Reporting bugs

Open an issue with: WeChat version, Windows build, the failing command, the
envelope you got (redact message text), and the expected behaviour. Security
issues go to `SECURITY.md`, not the public tracker.
