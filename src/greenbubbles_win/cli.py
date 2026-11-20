"""Command line interface for greenbubbles for Windows."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from . import archive, index as indexmod, keys as keymod, mcp, paths, query, service, snapshot
from .console import configure_stdio
from .store import Store


def log(message: str) -> None:
    print(f"[greenbubbles] {message}", file=sys.stderr)


def parse_time(value: str | None) -> int | None:
    if not value:
        return None
    if value.isdigit():
        return int(value)
    for pattern in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return int(datetime.strptime(value, pattern).replace(tzinfo=query.TZ).timestamp())
        except ValueError:
            continue
    raise SystemExit(f"cannot parse time: {value}")


def emit(payload, fmt: str = "json") -> None:
    if fmt == "jsonl":
        if isinstance(payload, dict) and isinstance(payload.get("items"), list):
            for item in payload["items"]:
                print(json.dumps(item, ensure_ascii=False))
            page = payload.get("page") or {}
            # jsonl has no envelope to carry the cursor, so publish it on stderr
            if page.get("nextCursor"):
                log(f"nextCursor={page['nextCursor']}")
            elif page.get("hasMore") is False:
                log("lastPage=true")
        else:
            print(json.dumps(payload, ensure_ascii=False))
        return
    print(json.dumps(payload, ensure_ascii=False, indent=2 if fmt == "pretty" else None))


def resolve_account(args) -> paths.Account:
    return paths.discover_default_account(getattr(args, "db_root", None))


def cached_keys(account: paths.Account) -> keymod.KeySet:
    state = paths.key_file()
    keyset = None
    if state.exists():
        try:
            keyset = keymod.KeySet.load(state)
        except Exception:
            keyset = None
    if keyset and keyset.entries:
        ok, missing = keymod.verify_existing(keyset, account.db_dir)
        if ok and not missing:
            return keyset
        log(f"stored keys are stale ({ok} valid, {missing} missing); recapturing")

    log("capturing database keys from the running WeChat process")
    keyset, readings = keymod.capture(account.db_dir)
    keyset.save(state)
    for reading in readings:
        log(
            f"pid {reading.pid}: needles={reading.needle_hits} nodes={reading.node_candidates} "
            f"blobs={reading.blob_count} verified={reading.verified}"
        )
    return keyset


def make_store(args) -> Store:
    account = resolve_account(args)
    keyset = cached_keys(account)
    return Store(account, keyset, log=log)


def cmd_source_status(args) -> None:
    account = resolve_account(args)
    keyset = cached_keys(account)
    store = Store(account, keyset, log=None)
    payload = {
        "schema": "greenbubbles.query.v1",
        "formatVersion": 1,
        "operation": "source.status",
        "ok": True,
        "source": {"mode": "live", "identity": paths.account_source_id(account)},
        "account": {
            "accountId": account.account_id,
            "dataRoot": str(account.data_root),
            "dbRoot": str(account.db_dir),
            "wechatVersion": paths.wechat_version(),
        },
        "keys": {"databases": len(keyset), "store": str(paths.key_file())},
        "cache": {"bytes": store.cache.size_bytes(), "path": str(store.cache.root)},
        "media": store.media.summary(),
        "readiness": {
            "conversations": len(store.sessions()),
            "messageShards": len(store.shards()),
            "contacts": len(store.contacts()),
        },
        "warnings": [],
    }
    emit(payload, args.format)


def cmd_key_capture(args) -> None:
    account = resolve_account(args)
    log("scanning Weixin.exe memory (read-only)")
    keyset, readings = keymod.capture(account.db_dir)
    target = Path(args.out) if args.out else paths.key_file()
    keyset.save(target)
    emit(
        {
            "ok": True,
            "databases": len(keyset),
            "matchedSalts": getattr(keyset, "_matched", 0),
            "totalSalts": getattr(keyset, "_total_salts", 0),
            "elapsedSeconds": round(getattr(keyset, "_elapsed", 0.0), 1),
            "writtenTo": str(target),
            "processes": [
                {"pid": r.pid, "needles": r.needle_hits, "blobs": r.blob_count, "verified": r.verified}
                for r in readings
            ],
        },
        args.format,
    )


def cmd_key_verify(args) -> None:
    account = resolve_account(args)
    state = Path(args.key_file) if args.key_file else paths.key_file()
    if not state.exists():
        raise SystemExit(f"no key file at {state}; run 'greenbubbles key capture'")
    keyset = keymod.KeySet.load(state)
    ok, missing = keymod.verify_existing(keyset, account.db_dir)
    emit({"ok": missing == 0, "verified": ok, "missing": missing, "keyFile": str(state)}, args.format)


def cmd_chats(args) -> None:
    store = make_store(args)
    emit(query.conversations(store, args.limit, args.cursor), args.format)


def cmd_chats_rank(args) -> None:
    store = make_store(args)
    emit(query.rank_conversations(store, args.limit, args.minimum_self_messages), args.format)


def cmd_chats_find(args) -> None:
    store = make_store(args)
    emit(query.find_conversations(store, args.name, args.limit), args.format)


def cmd_messages_list(args) -> None:
    store = make_store(args)
    emit(
        query.messages(
            store,
            args.conversation,
            args.limit,
            args.cursor,
            parse_time(args.since),
            parse_time(args.until),
            with_media=getattr(args, "with_media", False),
        ),
        args.format,
    )


def cmd_messages_search(args) -> None:
    store = make_store(args)
    emit(
        query.search(
            store,
            args.query,
            args.conversation,
            args.limit,
            parse_time(args.since),
            with_media=getattr(args, "with_media", False),
            use_index=False if getattr(args, "no_index", False) else None,
        ),
        args.format,
    )


def cmd_message_get(args) -> None:
    store = make_store(args)
    emit(
        query.message_get(
            store, args.conversation, args.message_id, with_media=getattr(args, "with_media", False)
        ),
        args.format,
    )


def cmd_index_build(args) -> None:
    store = make_store(args)
    idx = indexmod.SearchIndex()
    if getattr(args, "rebuild", False):
        idx.conn.execute("delete from msg_text")
        idx.conn.execute("delete from index_state")
        idx.conn.commit()
    conversations = [store.resolve(args.conversation)] if getattr(args, "conversation", None) else None
    result = idx.build(store, conversations, progress=None if getattr(args, "quiet", False) else log)
    result["stats"] = idx.stats()
    emit(result, args.format)


def cmd_index_status(args) -> None:
    path = indexmod.default_path()
    if not path.exists():
        emit(
            {"ok": True, "exists": False, "path": str(path), "rows": 0, "conversations": 0, "complete": False},
            args.format,
        )
        return
    idx = indexmod.SearchIndex()
    stats = idx.stats()
    stats["exists"] = True
    emit({"ok": True, **stats}, args.format)


def cmd_contacts(args) -> None:
    store = make_store(args)
    emit(query.contacts(store, args.limit, args.details, args.kind), args.format)


def cmd_archive_update(args) -> None:
    store = make_store(args)
    targets = [args.conversation] if args.conversation else None
    emit(
        archive.update(
            store,
            args.out,
            targets,
            progress=log,
            full=args.full,
            attachments=getattr(args, "attachments", False),
        ),
        args.format,
    )


def cmd_archive_export(args) -> None:
    store = make_store(args)
    targets = [args.conversation] if args.conversation else None
    formats = tuple(part.strip() for part in args.formats.split(",") if part.strip())
    result = archive.update(
        store,
        args.out,
        targets,
        formats=formats,
        progress=log,
        full=True,
        attachments=getattr(args, "attachments", False),
    )
    if "html" in formats:
        result["html"] = archive.export_html(store, args.out, targets)
    emit(result, args.format)


def cmd_archive_html(args) -> None:
    store = make_store(args)
    targets = [args.conversation] if args.conversation else None
    emit(archive.export_html(store, args.out, targets), args.format)


def cmd_serve(args) -> None:
    account = resolve_account(args)
    keyset = cached_keys(account)
    connector = service.Connector(account, keyset, log=log)
    server = service.serve(connector, args.host, args.port)
    log(f"connector listening on http://{args.host}:{args.port}")
    log(f"token file: {paths.state_dir() / 'token.txt'}")
    emit(
        {
            "ok": True,
            "url": f"http://{args.host}:{args.port}",
            "token": connector.token,
            "routes": service._ROUTES,
            "note": "Ctrl+C to stop; token is also stored in state/token.txt",
        },
        args.format,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("stopping connector")
    finally:
        server.server_close()


def cmd_mcp(args) -> None:
    account = resolve_account(args)
    keyset = cached_keys(account)
    connector = service.Connector(account, keyset, log=None)
    log("mcp server ready on stdio")
    mcp.run_stdio(connector)


def cmd_snapshot_create(args) -> None:
    root = Path(args.out_dir)
    try:
        result = snapshot.create(root, args.file, args.mnemonic or None)
    except snapshot.SnapshotError as exc:
        emit({"ok": False, "error": str(exc)}, args.format)
        raise SystemExit(1)
    emit(result, args.format)


def cmd_snapshot_restore(args) -> None:
    try:
        result = snapshot.restore(args.file, args.mnemonic, args.out_dir, overwrite=args.overwrite)
    except snapshot.SnapshotError as exc:
        emit({"ok": False, "error": str(exc)}, args.format)
        raise SystemExit(1)
    emit(result, args.format)


def cmd_snapshot_info(args) -> None:
    emit({"ok": True, "file": args.file, "header": snapshot.read_header(args.file)}, args.format)


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db-root", help="explicit path to WeChat's db_storage directory")
    common.add_argument("--format", default="json", choices=["json", "pretty", "jsonl"])

    parser = argparse.ArgumentParser(
        prog="greenbubbles",
        description="Read and archive local WeChat 4.x chat history on Windows.",
        parents=[common],
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    source = subparsers.add_parser("source", help="inspect the local WeChat data source")
    source_sub = source.add_subparsers(dest="source_command", required=True)
    status = source_sub.add_parser("status", help="show account, keys and readiness", parents=[common])
    status.set_defaults(func=cmd_source_status)

    key = subparsers.add_parser("key", help="database key management")
    key_sub = key.add_subparsers(dest="key_command", required=True)
    capture = key_sub.add_parser("capture", help="read keys from the running WeChat process", parents=[common])
    capture.add_argument("--out", help="write the key file to this path")
    capture.set_defaults(func=cmd_key_capture)
    verify = key_sub.add_parser("verify", help="re-check a stored key file", parents=[common])
    verify.add_argument("--key-file")
    verify.set_defaults(func=cmd_key_verify)

    chats = subparsers.add_parser("chats", help="list conversations")
    chats_sub = chats.add_subparsers(dest="chats_command")
    chat_list = chats_sub.add_parser("list", help="list conversations", parents=[common])
    chat_list.add_argument("--limit", type=int, default=query.DEFAULT_PAGE_LIMIT)
    chat_list.add_argument("--cursor")
    chat_list.set_defaults(func=cmd_chats)
    rank = chats_sub.add_parser("rank", help="rank conversations by your own message count", parents=[common])
    rank.add_argument("--limit", type=int, default=30)
    rank.add_argument("--minimum-self-messages", type=int, default=0)
    rank.set_defaults(func=cmd_chats_rank)
    find = chats_sub.add_parser("find", help="find a conversation by name or id", parents=[common])
    find.add_argument("name")
    find.add_argument("--limit", type=int, default=30)
    find.set_defaults(func=cmd_chats_find)

    messages = subparsers.add_parser("messages", help="read messages")
    messages_sub = messages.add_subparsers(dest="messages_command", required=True)
    mlist = messages_sub.add_parser("list", help="list messages of a conversation", parents=[common])
    mlist.add_argument("--conversation", required=True)
    mlist.add_argument("--limit", type=int, default=query.DEFAULT_PAGE_LIMIT)
    mlist.add_argument("--cursor")
    mlist.add_argument("--since")
    mlist.add_argument("--until")
    mlist.add_argument("--with-media", action="store_true", help="resolve on-disk attachments")
    mlist.set_defaults(func=cmd_messages_list)
    msearch = messages_sub.add_parser("search", help="search message bodies", parents=[common])
    msearch.add_argument("--query", required=True)
    msearch.add_argument("--conversation")
    msearch.add_argument("--limit", type=int, default=query.DEFAULT_PAGE_LIMIT)
    msearch.add_argument("--since")
    msearch.add_argument("--with-media", action="store_true")
    msearch.add_argument("--no-index", action="store_true", help="force a live scan even if a local index exists")
    msearch.set_defaults(func=cmd_messages_search)

    index_parser = subparsers.add_parser("index", help="local text index for unscoped search")
    index_sub = index_parser.add_subparsers(dest="index_command", required=True)
    ibuild = index_sub.add_parser("build", help="decode messages into the local index", parents=[common])
    ibuild.add_argument("--conversation", help="limit the build to one conversation")
    ibuild.add_argument("--rebuild", action="store_true", help="discard the index and start over")
    ibuild.add_argument("--quiet", action="store_true")
    ibuild.set_defaults(func=cmd_index_build)
    istatus = index_sub.add_parser("status", help="show index size and build time", parents=[common])
    istatus.set_defaults(func=cmd_index_status)

    message = subparsers.add_parser("message", help="single message operations")
    message_sub = message.add_subparsers(dest="message_command", required=True)
    mget = message_sub.add_parser("get", help="fetch one message by id or local id", parents=[common])
    mget.add_argument("--conversation", required=True)
    mget.add_argument("--message-id", required=True)
    mget.add_argument("--with-media", action="store_true")
    mget.set_defaults(func=cmd_message_get)

    contacts = subparsers.add_parser("contacts", help="list contacts")
    contacts_sub = contacts.add_subparsers(dest="contacts_command", required=True)
    contact_list = contacts_sub.add_parser("list", help="list contacts", parents=[common])
    contact_list.add_argument("--limit", type=int, default=query.DEFAULT_PAGE_LIMIT)
    contact_list.add_argument("--details", action="store_true")
    contact_list.add_argument("--kind", choices=["person", "group", "official", "service", "accountHolder"])
    contact_list.set_defaults(func=cmd_contacts)

    arch = subparsers.add_parser("archive", help="build a local archive")
    arch_sub = arch.add_subparsers(dest="archive_command", required=True)
    aupdate = arch_sub.add_parser("update", help="incrementally archive new messages", parents=[common])
    aupdate.add_argument("--out", required=True)
    aupdate.add_argument("--conversation")
    aupdate.add_argument("--full", action="store_true")
    aupdate.add_argument("--attachments", action="store_true", help="copy images/files/videos into the archive")
    aupdate.set_defaults(func=cmd_archive_update)
    aexport = arch_sub.add_parser("export", help="one-shot export (markdown/jsonl/html)", parents=[common])
    aexport.add_argument("--out", required=True)
    aexport.add_argument("--conversation")
    aexport.add_argument("--formats", default="md,jsonl,html")
    aexport.add_argument("--attachments", action="store_true")
    aexport.set_defaults(func=cmd_archive_export)
    ahtml = arch_sub.add_parser("html", help="render an HTML archive", parents=[common])
    ahtml.add_argument("--out", required=True)
    ahtml.add_argument("--conversation")
    ahtml.set_defaults(func=cmd_archive_html)

    serve = subparsers.add_parser("serve", help="run the local read-only HTTP connector", parents=[common])
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8787)
    serve.set_defaults(func=cmd_serve)

    mcp_parser = subparsers.add_parser("mcp", help="run an MCP server on stdio", parents=[common])
    mcp_parser.set_defaults(func=cmd_mcp)

    snap = subparsers.add_parser("snapshot", help="encrypted archive snapshots")
    snap_sub = snap.add_subparsers(dest="snapshot_command", required=True)
    screate = snap_sub.add_parser("create", help="seal an archive directory into a .gbvault", parents=[common])
    screate.add_argument("--out-dir", required=True, help="archive directory to seal")
    screate.add_argument("--file", required=True, help="target .gbvault path")
    screate.add_argument("--mnemonic", help="reuse an existing BIP39 phrase instead of generating one")
    screate.set_defaults(func=cmd_snapshot_create)
    srestore = snap_sub.add_parser("restore", help="open a .gbvault back into a directory", parents=[common])
    srestore.add_argument("--file", required=True)
    srestore.add_argument("--mnemonic", required=True)
    srestore.add_argument("--out-dir", required=True)
    srestore.add_argument("--overwrite", action="store_true")
    srestore.set_defaults(func=cmd_snapshot_restore)
    sinfo = snap_sub.add_parser("info", help="show a vault header without decrypting", parents=[common])
    sinfo.add_argument("--file", required=True)
    sinfo.set_defaults(func=cmd_snapshot_info)

    return parser


def normalize(argv: list[str]) -> list[str]:
    """Let ``chats`` work without an explicit ``list``, mirroring the macOS CLI."""

    if not argv:
        return argv
    for index, token in enumerate(argv):
        if token == "chats":
            nxt = argv[index + 1] if index + 1 < len(argv) else None
            if nxt is None or nxt.startswith("-") or nxt not in {"list", "rank", "find"}:
                return argv[: index + 1] + ["list"] + argv[index + 1 :]
            return argv
    return argv


def main(argv: list[str] | None = None) -> int:
    configure_stdio()
    argv = normalize(list(sys.argv[1:] if argv is None else argv))
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
