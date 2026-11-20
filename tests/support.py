"""Shared helpers for the greenbubbles-win regression suite."""

from __future__ import annotations

import functools
import json
import os
import socket
import subprocess
import sys
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
VENV_PY = ROOT / ".venv" / "Scripts" / "python.exe"
PY = VENV_PY if VENV_PY.exists() else Path(sys.executable)
CMD = ROOT / "greenbubbles.cmd"
# Data-dependent tests need a local fixture.  Nothing about the fixture is
# committed: point these variables at your own account when running them.
GROUP = os.environ.get("GREENBUBBLES_TEST_CONVERSATION", "").strip()
QUERY = os.environ.get("GREENBUBBLES_TEST_QUERY", "").strip()
REPORT_DIR = Path(
    os.environ.get("GREENBUBBLES_TEST_REPORTS", str(ROOT / "reports" / "fixture"))
)

sys.path.insert(0, str(SRC))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def data_available() -> bool:
    """True when a captured account is present (keys + decrypted cache)."""

    if not GROUP:
        return False
    if not (ROOT / "state" / "keys.json").exists():
        return False
    return any((ROOT / "state" / "cache").glob("*/db/message/message_0.db"))


def require_data() -> None:
    """Skip data-dependent tests instead of failing on a clean clone / CI."""

    if not GROUP:
        raise unittest.SkipTest(
            "set GREENBUBBLES_TEST_CONVERSATION to a conversation id to run data-dependent tests"
        )
    if not data_available():
        raise unittest.SkipTest(
            "no captured WeChat account in state/; run `greenbubbles.cmd source status` first"
        )


def require_reports() -> None:
    """Skip report-backed tests unless a fixture directory is configured."""

    if not (REPORT_DIR / "messages-all.jsonl").exists():
        raise unittest.SkipTest(
            "set GREENBUBBLES_TEST_REPORTS to a report directory to run attachment checks"
        )


def require_query() -> None:
    if not QUERY:
        raise unittest.SkipTest("set GREENBUBBLES_TEST_QUERY to a search term with enough hits")


@functools.lru_cache(maxsize=1)
def get_store():
    from greenbubbles_win import keys, paths
    from greenbubbles_win.store import Store

    account = paths.discover_default_account()
    keyset = keys.KeySet.load(paths.key_file())
    return Store(account, keyset, log=None)


def run_cli(*args: str, timeout: int = 240) -> subprocess.CompletedProcess:
    if os.name == "nt":
        command = ["cmd", "/c", str(CMD), *args]
    else:
        command = [str(PY), str(ROOT / "greenbubbles.py"), *args]
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(ROOT),
        timeout=timeout,
    )


def cli_json(*args: str, timeout: int = 240) -> dict:
    proc = run_cli(*args, timeout=timeout)
    if proc.returncode != 0:
        raise AssertionError(f"cli failed ({proc.returncode}): {proc.stderr[-300:]}")
    return json.loads(proc.stdout)


def mcp_session(messages: list[dict], timeout: int = 180) -> tuple[list[dict], str]:
    proc = subprocess.Popen(
        [str(PY), str(ROOT / "greenbubbles.py"), "mcp"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        cwd=str(ROOT),
    )
    payload = "\n".join(json.dumps(message, ensure_ascii=False) for message in messages) + "\n"
    out, err = proc.communicate(payload, timeout=timeout)
    return [json.loads(line) for line in out.splitlines() if line.strip()], err


def mcp_tool(name: str, arguments: dict, ident: int = 2) -> dict:
    responses, _ = mcp_session(
        [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "id": ident, "method": "tools/call",
             "params": {"name": name, "arguments": arguments}},
        ]
    )
    for response in responses:
        if response.get("id") == ident:
            return response
    raise AssertionError(f"no response for {name}: {responses}")


def unpack_tool(response: dict) -> dict:
    payload = response["result"]["content"][0]["text"]
    return json.loads(payload)


def free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class HttpServer:
    def __init__(self) -> None:
        self.port = free_port()
        self.token = (ROOT / "state" / "token.txt").read_text(encoding="utf-8").strip()
        self.proc = subprocess.Popen(
            [str(PY), str(ROOT / "greenbubbles.py"), "serve", "--port", str(self.port)],
            cwd=str(ROOT),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                self.raw("/health", token=None)
                return
            except Exception:
                time.sleep(0.2)
        raise RuntimeError("connector did not become ready")

    def raw(self, path: str, token: str | None = "default") -> tuple[int, bytes, dict]:
        headers = {}
        if token is not None:
            headers["Authorization"] = f"Bearer {self.token if token == 'default' else token}"
        request = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return response.status, response.read(), dict(response.headers)
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read(), dict(exc.headers)

    def json(self, path: str, token: str | None = "default") -> tuple[int, dict]:
        status, body, _ = self.raw(path, token)
        try:
            return status, json.loads(body.decode("utf-8"))
        except Exception:
            return status, {}

    def close(self) -> None:
        self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()


def shard_truth(conversation: str = GROUP) -> dict:
    """Ground truth straight from the decrypted shard tables (no query layer)."""

    store = get_store()
    keys: set[tuple[int, int]] = set()
    since_counts: dict[int, int] = {}
    tables = []
    for rel, table in store.locate_all(conversation):
        conn = store.open(rel)
        tables.append((rel, table))
        for seq, local_id, created in conn.execute(
            f"select sort_seq, local_id, create_time from {table}"
        ):
            keys.add((seq, local_id))
            since_counts[created] = since_counts.get(created, 0) + 1
    return {"messages": len(keys), "keys": keys, "tables": tables, "since_counts": since_counts}
