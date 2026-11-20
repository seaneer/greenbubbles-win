"""Local HTTP connector: read-only access to the archive for other agents.

Everything binds to 127.0.0.1 by default and requires a bearer token. The token is
generated once and stored in ``state/token.txt``; pass it as
``Authorization: Bearer <token>``, ``X-Greenbubbles-Token: <token>`` or ``?token=``.
"""

from __future__ import annotations

import json
import re
import secrets
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from . import media, paths, query
from .store import Store


def load_token(state_dir: Path) -> str:
    path = state_dir / "token.txt"
    if path.exists():
        value = path.read_text(encoding="utf-8").strip()
        if value:
            return value
    value = secrets.token_urlsafe(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")
    return value


def parse_when(value: str | None) -> int | None:
    if not value:
        return None
    if value.isdigit():
        return int(value)
    for pattern in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return int(datetime.strptime(value, pattern).replace(tzinfo=query.TZ).timestamp())
        except ValueError:
            continue
    raise ValueError(f"cannot parse time: {value}")


def _truthy(value: str | None) -> bool:
    return str(value).lower() in {"1", "true", "yes", "on"}


_SANITIZE_KEYS = {"sender", "conversationId", "id"}


def sanitize_payload(value: Any) -> Any:
    """Drop internal identifiers while keeping display name, time, type and text."""

    if isinstance(value, dict):
        return {key: sanitize_payload(item) for key, item in value.items() if key not in _SANITIZE_KEYS}
    if isinstance(value, list):
        return [sanitize_payload(item) for item in value]
    return value


def _maybe_sanitize(payload: dict[str, Any], params: dict[str, str]) -> dict[str, Any]:
    if not _truthy(params.get("sanitize")):
        return payload
    result = sanitize_payload(payload)
    if isinstance(result.get("warnings"), list):
        result["warnings"].append({"code": "sanitizedOutput", "message": "sender/conversationId/id removed"})
    return result


def redact_token(message: str) -> str:
    """Remove query-string bearer tokens before a request is logged."""

    return re.sub(r"(?i)([?&]token=)[^&\s\"]+", r"\1<redacted>", message)


class Connector:
    """Wraps one Store behind a lock so HTTP threads never share a sqlite handle."""

    def __init__(self, account, keyset, log=None) -> None:
        self.account = account
        self.keyset = keyset
        self.store = Store(account, keyset, log=log or (lambda _m: None))
        self.lock = threading.RLock()
        self.token = load_token(paths.state_dir())

    # -- query surface ---------------------------------------------------
    def status(self) -> dict[str, Any]:
        with self.lock:
            conversations = len(self.store.sessions())
            return {
                "schema": "greenbubbles.query.v1",
                "formatVersion": 1,
                "operation": "source.status",
                "ok": True,
                "source": {"mode": "live", "identity": paths.account_source_id(self.account)},
                "account": {
                    "accountId": self.account.account_id,
                    "wechatVersion": paths.wechat_version(),
                },
                "keys": {"databases": len(self.keyset)},
                "media": self.store.media.summary(),
                "conversations": conversations,
                "readiness": {
                    "conversations": conversations,
                    "messageShards": len(self.store.shards()),
                    "contacts": len(self.store.contacts()),
                },
            }

    def chats(self, limit: int, cursor: str | None, text: str | None) -> dict[str, Any]:
        with self.lock:
            if text:
                return query.find_conversations(self.store, text, limit)
            return query.conversations(self.store, limit, cursor)

    def messages(self, params: dict[str, str]) -> dict[str, Any]:
        # Media summaries are on by default so callers do not need an N+1
        # request per attachment; `media=0` keeps the payload slim.
        media_param = params.get("media")
        with self.lock:
            return query.messages(
                self.store,
                params.get("conversation") or params.get("chat") or "",
                int(params.get("limit") or query.DEFAULT_PAGE_LIMIT),
                params.get("cursor"),
                parse_when(params.get("since")),
                parse_when(params.get("until")),
                with_media=True if media_param is None else _truthy(media_param),
            )

    def search(self, params: dict[str, str]) -> dict[str, Any]:
        with self.lock:
            return query.search(
                self.store,
                params.get("q") or params.get("query") or "",
                params.get("conversation"),
                int(params.get("limit") or query.DEFAULT_PAGE_LIMIT),
                parse_when(params.get("since")),
                with_media=_truthy(params.get("media")),
                use_index=False if (params.get("index") or "").lower() in {"0", "false", "no"} else None,
            )

    def contacts(self, params: dict[str, str]) -> dict[str, Any]:
        with self.lock:
            return query.contacts(
                self.store,
                int(params.get("limit") or query.DEFAULT_PAGE_LIMIT),
                _truthy(params.get("details")),
                params.get("kind"),
            )

    def one_message(self, params: dict[str, str]) -> dict[str, Any]:
        with self.lock:
            return query.message_get(
                self.store,
                params.get("conversation") or "",
                params.get("id") or params.get("messageId") or "",
                with_media=_truthy(params.get("media")),
            )

    def media_bytes(self, params: dict[str, str]) -> tuple[bytes, str, str, dict[str, str]] | None:
        """Return (bytes, filename, content-type, extra headers) for an attachment."""

        with self.lock:
            conversation = params.get("conversation") or ""
            message_id = params.get("id") or params.get("messageId") or ""
            if not conversation or not message_id:
                return None
            resolved = query.resolve_media_for(self.store, conversation, message_id)
            if resolved is None:
                return None
            if resolved.kind == "image" and resolved.valid is not True:
                raise media.MediaValidationError(resolved.note or "image failed validation")
            payload = resolved.data if resolved.data is not None else resolved.source.read_bytes()
            name = f"{Path(resolved.name).stem}.{resolved.extension}"
            headers = {
                "X-Media-Valid": "true" if resolved.valid is not False else "false",
                "X-Media-Format": resolved.extension,
            }
            if resolved.note:
                headers["X-Media-Decrypt"] = resolved.note
            if resolved.extension == "wxgf":
                headers["X-Original-Format"] = "wxgf"
            return payload, name, _content_type(resolved.extension), headers


def _content_type(extension: str) -> str:
    return {
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "png": "image/png",
        "gif": "image/gif",
        "webp": "image/webp",
        "wxgf": "image/heif",
        "heif": "image/heif",
        "mp4": "video/mp4",
        "pdf": "application/pdf",
    }.get(extension.lower(), "application/octet-stream")


class Handler(BaseHTTPRequestHandler):
    connector: Connector | None = None
    server_version = "greenbubbles-connector/1.0"

    def log_message(self, fmt: str, *args) -> None:  # keep stdout clean
        message = redact_token(fmt % args)
        print(f"[connector] {message}", flush=True)

    # -- helpers ---------------------------------------------------------
    def _authorized(self, parsed) -> bool:
        authorization = self.headers.get("Authorization", "").strip()
        token = authorization[7:].strip() if authorization.lower().startswith("bearer ") else ""
        token = token or self.headers.get("X-Greenbubbles-Token", "").strip()
        token = token or (parse_qs(parsed.query).get("token") or [""])[0]
        return bool(token) and secrets.compare_digest(token, self.connector.token)

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, payload: bytes, filename: str, content_type: str, headers: dict[str, str] | None = None) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Content-Disposition", f'inline; filename="{filename}"')
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(payload)

    def _send_text(self, payload: str, content_type: str = "text/markdown; charset=utf-8") -> None:
        body = payload.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        params = {key: values[0] for key, values in parse_qs(parsed.query).items()}
        route = parsed.path.rstrip("/") or "/"

        if route in {"/health", "/"}:
            self._send(200, {"ok": True, "service": "greenbubbles-connector", "routes": _ROUTES})
            return

        if not self._authorized(parsed):
            self._send(401, {"ok": False, "error": "unauthorized: missing or wrong token"})
            return

        try:
            if route == "/api/v1/source":
                self._send(200, self.connector.status())
            elif route == "/api/v1/chats":
                self._send(
                    200,
                    self.connector.chats(
                        int(params.get("limit") or query.DEFAULT_PAGE_LIMIT),
                        params.get("cursor"),
                        params.get("q"),
                    ),
                )
            elif route == "/api/v1/messages":
                if not (params.get("conversation") or params.get("chat")):
                    self._send(400, {"ok": False, "error": "conversation is required"})
                    return
                self._send(200, _maybe_sanitize(self.connector.messages(params), params))
            elif route == "/api/v1/search":
                self._send(200, _maybe_sanitize(self.connector.search(params), params))
            elif route == "/api/v1/contacts":
                self._send(200, self.connector.contacts(params))
            elif route == "/api/v1/message":
                self._send(200, _maybe_sanitize(self.connector.one_message(params), params))
            elif route == "/api/v1/media":
                try:
                    found = self.connector.media_bytes(params)
                except media.MediaValidationError as exc:
                    self._send(422, {"ok": False, "code": "mediaValidationFailed", "error": str(exc)})
                    return
                if not found:
                    self._send(404, {"ok": False, "error": "no attachment resolved for that message"})
                    return
                payload, name, content_type, extra = found
                self._send_bytes(payload, name, content_type, extra)
            elif route == "/guide":
                doc = paths.project_root() / "docs" / "AGENT-PROMPT.md"
                if not doc.exists():
                    self._send(404, {"ok": False, "error": "guide is not installed"})
                    return
                self._send_text(doc.read_text(encoding="utf-8"))
            else:
                self._send(404, {"ok": False, "error": f"unknown route {route}", "routes": _ROUTES})
        except ValueError as exc:
            self._send(400, {"ok": False, "error": str(exc)})
        except Exception as exc:  # keep the server alive, report the failure
            self._send(500, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})


_ROUTES = [
    "GET /health",
    "GET /guide",
    "GET /api/v1/source",
    "GET /api/v1/chats?limit=&cursor=&q=",
    "GET /api/v1/messages?conversation=&since=&until=&limit=&cursor=&media=",
    "GET /api/v1/search?q=&conversation=&limit=&since=&media=&index=",
    "GET /api/v1/contacts?limit=&details=&kind=",
    "GET /api/v1/message?conversation=&id=",
    "GET /api/v1/media?conversation=&id=",
]


def serve(connector: Connector, host: str = "127.0.0.1", port: int = 8787) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"connector": connector})
    server = ThreadingHTTPServer((host, port), handler)
    return server
