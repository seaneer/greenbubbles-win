"""Console encoding setup shared by the CLI and the MCP stdio server.

Windows pipes default to the active ANSI code page (for example cp936).
Chat history contains characters outside that code page, so writing JSON to a
pipe used to fail with ``UnicodeEncodeError``.  Both entry points switch their
standard streams to UTF-8 before producing any output.
"""

from __future__ import annotations

import sys


def configure_stdio() -> None:
    """Make stdin/stdout/stderr UTF-8 even when they are pipes."""

    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8")
        except (OSError, ValueError):
            # A host may pass a stream that cannot be reconfigured; keep going
            # with whatever encoding it already has.
            pass
