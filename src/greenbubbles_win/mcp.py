"""MCP (Model Context Protocol) server over stdio for the local WeChat archive.

Exposes a handful of read-only tools so other agents can query conversations,
messages and attachments without shelling out to the CLI.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from . import query
from .console import configure_stdio
from .service import Connector

SERVER_INFO = {"name": "greenbubbles-win", "version": "0.2.1"}
PROTOCOL_VERSION = "2024-11-05"

TOOLS: list[dict[str, Any]] = [
    {
        "name": "list_conversations",
        "description": "List recent WeChat conversations, or search them by name/id.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "fuzzy name or wxid filter"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 30},
            },
        },
    },
    {
        "name": "get_messages",
        "description": "Read messages of one conversation, newest first, optionally limited to a time range.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "conversation": {"type": "string", "description": "wxid/chatroom id or display name"},
                "since": {"type": "string", "description": "ISO time or unix seconds, inclusive"},
                "until": {"type": "string", "description": "ISO time or unix seconds, inclusive"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 50},
                "cursor": {"type": "string", "description": "page.nextCursor of the previous call"},
                "include_media": {"type": "boolean", "default": False},
            },
            "required": ["conversation"],
        },
    },
    {
        "name": "search_messages",
        "description": "Full-text search across messages; scope to one conversation when known.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "conversation": {"type": "string"},
                "since": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 30},
                "include_media": {"type": "boolean", "default": False},
            },
            "required": ["query"],
        },
    },
    {
        "name": "list_contacts",
        "description": "List contacts (people, groups, official accounts) with optional details.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 50},
                "details": {"type": "boolean", "default": False},
                "kind": {"type": "string", "enum": ["person", "group", "official", "service", "accountHolder"]},
            },
        },
    },
    {
        "name": "get_message",
        "description": "Fetch a single message by id (cursor id or numeric local id).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "conversation": {"type": "string"},
                "id": {"type": "string"},
                "include_media": {"type": "boolean", "default": False},
            },
            "required": ["conversation", "id"],
        },
    },
    {
        "name": "source_status",
        "description": "Report the data source: account, WeChat version, key count, message shards.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def _text(payload: Any) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}]}


def _limit(arguments: dict[str, Any], default: int) -> int:
    """MCP clients sometimes send 0 or a string; never let falsy values mean "default"."""

    value = arguments.get("limit")
    if value is None:
        return default
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(500, number))


def call_tool(connector: Connector, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if name == "list_conversations":
        return _text(
            connector.chats(
                _limit(arguments, 30),
                None,
                arguments.get("query"),
            )
        )
    if name == "get_messages":
        params = {
            "conversation": arguments.get("conversation", ""),
            "limit": str(_limit(arguments, 50)),
            "media": "1" if arguments.get("include_media") else "0",
        }
        for key in ("since", "until", "cursor"):
            if arguments.get(key):
                params[key] = str(arguments[key])
        return _text(connector.messages(params))
    if name == "search_messages":
        params = {
            "q": arguments.get("query", ""),
            "limit": str(_limit(arguments, 30)),
            "media": "1" if arguments.get("include_media") else "0",
        }
        for key in ("conversation", "since"):
            if arguments.get(key):
                params[key] = str(arguments[key])
        return _text(connector.search(params))
    if name == "list_contacts":
        params = {"limit": str(_limit(arguments, 50))}
        if arguments.get("details"):
            params["details"] = "1"
        if arguments.get("kind"):
            params["kind"] = str(arguments["kind"])
        return _text(connector.contacts(params))
    if name == "get_message":
        return _text(
            connector.one_message(
                {
                    "conversation": arguments.get("conversation", ""),
                    "id": arguments.get("id", ""),
                    "media": "1" if arguments.get("include_media") else "0",
                }
            )
        )
    if name == "source_status":
        return _text(connector.status())
    raise ValueError(f"unknown tool: {name}")


def handle(connector: Connector, request: dict[str, Any]) -> dict[str, Any] | None:
    request_id = request.get("id")
    method = request.get("method")

    if method == "initialize":
        result = {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER_INFO,
        }
    elif method in {"notifications/initialized", "notifications/cancelled"}:
        return None
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        params = request.get("params") or {}
        try:
            result = call_tool(connector, params.get("name", ""), params.get("arguments") or {})
        except Exception as exc:
            result = {
                "content": [{"type": "text", "text": f"{type(exc).__name__}: {exc}"}],
                "isError": True,
            }
    elif method == "ping":
        result = {}
    else:
        if request_id is None:
            return None
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32601, "message": f"method not found: {method}"},
        }

    if request_id is None:
        return None
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def run_stdio(connector: Connector) -> None:
    configure_stdio()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            response = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": "parse error"},
            }
        else:
            response = handle(connector, request)
        if response is not None:
            sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            sys.stdout.flush()
