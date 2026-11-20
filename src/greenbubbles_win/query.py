"""Query projections that mirror greenbubbles' ``greenbubbles.query.v1`` shapes."""

from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone
from typing import Any

from . import content, media, paths, store as storemod

SCHEMA = "greenbubbles.query.v1"
FORMAT_VERSION = 1
DEFAULT_PAGE_LIMIT = 100
MAX_PAGE_LIMIT = 500
MAX_TEXT_BYTES = 16_000

TZ = timezone(timedelta(hours=8))

ROW_COLUMNS = (
    "select local_id, server_id, local_type, sort_seq, real_sender_id, create_time, status, "
    "message_content, WCDB_CT_message_content, packed_info_data from "
)


def now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def envelope(
    operation: str,
    source: dict[str, Any],
    items: list[dict[str, Any]],
    *,
    limit: int,
    has_more: bool = False,
    next_cursor: str | None = None,
    warnings: list[dict[str, str]] | None = None,
    database_count: int = 1,
    coverage_complete: bool = True,
) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "formatVersion": FORMAT_VERSION,
        "operation": operation,
        "ok": True,
        "source": source,
        "consistency": {
            "guarantee": "perDatabaseReadStatement",
            "databaseCount": database_count,
            "crossDatabaseAtomic": database_count <= 1,
            "coverageComplete": coverage_complete,
            "observedAtUnixMilliseconds": now_ms(),
        },
        "page": {
            "limit": limit,
            "returned": len(items),
            "hasMore": has_more,
            "nextCursor": next_cursor,
        },
        "warnings": warnings or [],
        "items": items,
    }


def source_description(store: storemod.Store) -> dict[str, Any]:
    return {"mode": "live", "identity": paths.account_source_id(store.account)}


def iso_time(unix_seconds: int) -> str | None:
    if not unix_seconds:
        return None
    return datetime.fromtimestamp(unix_seconds, TZ).isoformat(timespec="seconds")


def encode_cursor(payload: dict[str, Any]) -> str:
    return base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode()


def decode_cursor(value: str | None) -> dict[str, Any] | None:
    if not value:
        return None
    try:
        return json.loads(base64.urlsafe_b64decode(value.encode()))
    except Exception:
        return None


def clamp_limit(limit: int) -> int:
    if limit <= 0:
        return DEFAULT_PAGE_LIMIT
    return min(limit, MAX_PAGE_LIMIT)


def _truncate(text: str) -> tuple[str, bool]:
    if len(text) <= MAX_TEXT_BYTES:
        return text, False
    return text[:MAX_TEXT_BYTES], True


# ---------------------------------------------------------------------------
# Message rows, merged across shards
# ---------------------------------------------------------------------------


def _shard_rows(
    store: storemod.Store,
    username: str,
    rel: str,
    table: str,
    limit: int,
    cursor_sort: int | None,
    since: int | None,
    until: int | None,
) -> list[tuple]:
    conn = store.open(rel)
    sql = ROW_COLUMNS + table
    conditions: list[str] = []
    params: list[Any] = []
    if cursor_sort is not None:
        conditions.append("sort_seq < ?")
        params.append(cursor_sort)
    if since:
        conditions.append("create_time >= ?")
        params.append(since)
    if until:
        conditions.append("create_time <= ?")
        params.append(until)
    if conditions:
        sql += " where " + " and ".join(conditions)
    sql += " order by sort_seq desc limit ?"
    params.append(limit)
    return [(row, rel, table) for row in conn.execute(sql, params).fetchall()]


def merged_rows(
    store: storemod.Store,
    username: str,
    limit: int,
    cursor_sort: int | None = None,
    since: int | None = None,
    until: int | None = None,
) -> list[tuple[tuple, str, str]]:
    """Rows from every shard holding this conversation, newest first, deduped."""

    candidates = store.locate_all(username)
    rows: list[tuple[tuple, str, str]] = []
    for rel, table in candidates:
        rows.extend(_shard_rows(store, username, rel, table, limit, cursor_sort, since, until))
    rows.sort(key=lambda entry: entry[0][3], reverse=True)
    merged: list[tuple[tuple, str, str]] = []
    seen: set[tuple] = set()
    for row, rel, table in rows:
        key = (row[3], row[1])
        if key in seen:
            continue
        seen.add(key)
        merged.append((row, rel, table))
        if len(merged) >= limit:
            break
    return merged


def _names(store: storemod.Store, rel: str) -> dict[int, str]:
    conn = store.open(rel)
    return {rowid: name for rowid, name in conn.execute("select rowid, user_name from Name2Id")}


# ---------------------------------------------------------------------------
# Conversations
# ---------------------------------------------------------------------------


def conversations(store: storemod.Store, limit: int = DEFAULT_PAGE_LIMIT, cursor: str | None = None) -> dict[str, Any]:
    limit = clamp_limit(limit)
    state = decode_cursor(cursor)
    sessions = store.sessions()
    if state:
        sessions = [s for s in sessions if s.sort_timestamp < state.get("sortTimestamp", 0)]

    selected = sessions[: limit + 1]
    has_more = len(selected) > limit
    selected = selected[:limit]

    items = []
    for session in selected:
        summary, truncated = _truncate(session.summary)
        display = store.display_name(session.username)
        item: dict[str, Any] = {
            "id": session.username,
            "displayName": display if display != session.username else None,
            "summary": summary or None,
            "summaryDecodeState": "complete",
            "summaryTruncated": truncated,
            "sortTimestamp": session.sort_timestamp,
            "sortTime": iso_time(session.sort_timestamp),
        }
        if session.last_msg_type:
            item["lastMessageType"] = session.last_msg_type
        if session.last_msg_sender:
            item["lastMessageSender"] = session.last_msg_sender
        if session.last_sender_display_name:
            item["lastSenderDisplayName"] = session.last_sender_display_name
        items.append({key: value for key, value in item.items() if value is not None or key == "summary"})

    next_cursor = (
        encode_cursor({"sortTimestamp": selected[-1].sort_timestamp, "id": selected[-1].username})
        if has_more and selected
        else None
    )
    return envelope(
        "chats.list",
        source_description(store),
        items,
        limit=limit,
        has_more=has_more,
        next_cursor=next_cursor,
    )


def rank_conversations(
    store: storemod.Store,
    limit: int = DEFAULT_PAGE_LIMIT,
    minimum_self_messages: int = 0,
) -> dict[str, Any]:
    limit = clamp_limit(limit)
    self_user = store.self_username()
    entries = []
    for session in store.sessions():
        candidates = store.locate_all(session.username)
        if not candidates:
            continue
        total = 0
        self_count = 0
        seen: set[int] = set()
        for rel, table in candidates:
            conn = store.open(rel)
            names = {rowid: name for rowid, name in conn.execute("select rowid, user_name from Name2Id")}
            rows = conn.execute(f"select sort_seq, real_sender_id from {table}").fetchall()
            self_ids = {rowid for rowid, name in names.items() if name == self_user}
            for sort_seq, sender_id in rows:
                if sort_seq in seen:
                    continue
                seen.add(sort_seq)
                total += 1
                if sender_id in self_ids:
                    self_count += 1
        if self_count < minimum_self_messages:
            continue
        entries.append(
            {
                "id": session.username,
                "displayName": store.display_name(session.username),
                "kind": "group" if storemod.is_group(session.username) else "person",
                "messageCount": total,
                "selfMessageCount": self_count,
                "lastMessageAt": iso_time(session.last_timestamp),
                "sortTimestamp": session.sort_timestamp,
            }
        )
    entries.sort(key=lambda item: (item["selfMessageCount"], item["messageCount"]), reverse=True)
    selected = entries[: limit + 1]
    has_more = len(selected) > limit
    selected = selected[:limit]
    return envelope("chats.rank", source_description(store), selected, limit=limit, has_more=has_more)


def find_conversations(store: storemod.Store, query: str, limit: int = DEFAULT_PAGE_LIMIT) -> dict[str, Any]:
    limit = clamp_limit(limit)
    lowered = query.lower()
    items = []
    for session in store.sessions():
        contact = store.contacts().get(session.username)
        display = contact.display_name if contact else session.username
        haystack = " ".join(
            filter(
                None,
                [
                    session.username,
                    display,
                    contact.remark if contact else None,
                    contact.nickname if contact else None,
                    contact.alias if contact else None,
                ],
            )
        ).lower()
        if lowered in haystack:
            items.append(
                {
                    "id": session.username,
                    "displayName": display,
                    "kind": "group" if storemod.is_group(session.username) else "person",
                    "matchedOn": "displayName" if lowered in display.lower() else "id",
                    "shardCount": len(store.locate_all(session.username)),
                    "lastMessageAt": iso_time(session.last_timestamp),
                }
            )
        if len(items) > limit:
            break
    return envelope("chats.find", source_description(store), items[:limit], limit=limit)


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------


def _content_json(msg_type: int, sub_type: int, text: str) -> dict[str, Any]:
    if msg_type == content.MSG_TYPE_TEXT:
        return {"Text": text}
    if msg_type == content.MSG_TYPE_IMAGE:
        return {"Image": {"md5": None}}
    if msg_type == content.MSG_TYPE_VOICE:
        return "Voice"
    if msg_type == content.MSG_TYPE_VIDEO:
        return {"Video": {"md5": None}}
    if msg_type == content.MSG_TYPE_EMOJI:
        return {"Emoji": text}
    if msg_type == content.MSG_TYPE_LOCATION:
        return {"Location": text}
    if msg_type == content.MSG_TYPE_APP:
        return {"App": {"subtype": sub_type, "label": content.subtype_label(msg_type, sub_type), "text": text}}
    if msg_type == content.MSG_TYPE_SYSTEM:
        return {"System": text}
    if msg_type == content.MSG_TYPE_REVOKE:
        return {"Revoke": text}
    return {"Unknown": {"msgType": msg_type, "label": content.type_label(msg_type), "raw": text}}


def resolve_media(store: storemod.Store, conversation: str, msg_type: int, sub_type: int, raw_text: str, packed, created_at: int):
    """Return the MediaFile behind a message, or None when nothing is on disk."""

    try:
        resolver = store.media
    except Exception:
        return None

    if msg_type == content.MSG_TYPE_IMAGE:
        md5 = media.image_md5(packed, raw_text)
        return resolver.image(conversation, md5) if md5 else None
    if msg_type == content.MSG_TYPE_VIDEO:
        md5 = media.image_md5(None, raw_text) or media.image_md5(packed, None)
        return resolver.video(md5) if md5 else None
    if msg_type == content.MSG_TYPE_APP and sub_type == 6:
        title = content.extract_tag(raw_text, "title") or ""
        size_text = content.extract_tag(raw_text, "totallen") or ""
        month = datetime.fromtimestamp(created_at, TZ).strftime("%Y-%m") if created_at else None
        return resolver.file(title, int(size_text) if size_text.isdigit() else None, month)
    return None


def _media_info(store: storemod.Store, conversation: str, msg_type: int, sub_type: int, raw_text: str, packed, created_at: int) -> dict[str, Any] | None:
    """Resolve the on-disk attachment for a message when asked to."""

    found = resolve_media(store, conversation, msg_type, sub_type, raw_text, packed, created_at)
    if not found:
        return None
    return {
        "kind": found.kind,
        "available": found.kind != "image" or found.data is not None,
        "fileName": found.name,
        "extension": found.extension,
        "size": found.size,
        "note": found.note,
    }


def _message_item(
    store: storemod.Store,
    conversation: str,
    row: tuple,
    names: dict[int, str],
    *,
    self_user: str,
    digest: str,
    shard: str,
    with_media: bool = False,
) -> dict[str, Any]:
    local_id, server_id, local_type, sort_seq, sender_id, create_time, status, raw, ct, packed = row[:10]
    msg_type, sub_type = content.split_local_type(local_type)
    raw_text = content.decode_bytes(raw, ct)
    is_group = storemod.is_group(conversation)
    sender_hint = names.get(sender_id) or ("" if is_group else conversation)
    text, decoded_sender, state = content.parse_message(
        msg_type, sub_type, raw_text, is_group=is_group, fallback_sender=sender_hint
    )
    text, truncated = _truncate(text)
    sender = decoded_sender or sender_hint or conversation
    display = store.display_name(sender) if sender else None
    message_id = encode_cursor(
        {"k": "message.identity", "c": digest, "s": sort_seq, "t": create_time, "sid": server_id, "r": local_id}
    )
    item = {
        "id": message_id,
        "conversationId": conversation,
        "sortSequence": sort_seq,
        "serverId": server_id,
        "messageType": msg_type,
        "messageTypeLabel": content.type_label(msg_type),
        "messageSubtype": sub_type,
        "messageSubtypeLabel": content.subtype_label(msg_type, sub_type),
        "sender": sender,
        "senderDisplayName": display if display and display != sender else None,
        "isSelf": sender == self_user,
        "createdAtUnix": create_time,
        "createdAt": iso_time(create_time),
        "status": status or 0,
        "content": _content_json(msg_type, sub_type, text),        "text": text,
        "contentDecodeState": state,
        "contentTruncated": truncated,
        "shard": shard,
        "messageLocalId": local_id,
    }
    if with_media:
        info = _media_info(store, conversation, msg_type, sub_type, raw_text, packed, create_time)
        if info:
            item["media"] = info
    if msg_type == content.MSG_TYPE_IMAGE:
        md5 = media.image_md5(packed, raw_text)
        if md5:
            item["content"] = {"Image": {"md5": md5}}
    return item


def messages(
    store: storemod.Store,
    conversation: str,
    limit: int = DEFAULT_PAGE_LIMIT,
    cursor: str | None = None,
    since: int | None = None,
    until: int | None = None,
    with_media: bool = False,
) -> dict[str, Any]:
    limit = clamp_limit(limit)
    username = store.resolve(conversation)
    candidates = store.locate_all(username)
    if not candidates:
        return envelope(
            "messages.list",
            source_description(store),
            [],
            limit=limit,
            warnings=[
                {
                    "code": "conversationNotFound",
                    "message": f"no message table exists for conversation {username}",
                }
            ],
            coverage_complete=False,
        )
    state = decode_cursor(cursor)
    cursor_sort = state.get("s") if state else None
    rows = merged_rows(store, username, limit + 1, cursor_sort, since, until)
    has_more = len(rows) > limit
    rows = rows[:limit]

    items = []
    names_by_shard: dict[str, dict[int, str]] = {}
    self_user = store.self_username()
    for row, rel, table in rows:
        if rel not in names_by_shard:
            names_by_shard[rel] = _names(store, rel)
        items.append(
            _message_item(
                store,
                username,
                row,
                names_by_shard[rel],
                self_user=self_user,
                digest=table[4:],
                shard=rel,
                with_media=with_media,
            )
        )
    next_cursor = (
        encode_cursor({"s": rows[-1][0][3], "k": "messages.list"}) if has_more and rows else None
    )
    return envelope(
        "messages.list",
        source_description(store),
        items,
        limit=limit,
        has_more=has_more,
        next_cursor=next_cursor,
        database_count=len({rel for _, rel, _ in rows}),
    )


def resolve_media_for(store: storemod.Store, conversation: str, message_id: str):
    """Resolve the attachment for one message id (cursor id or numeric local id)."""

    username = store.resolve(conversation)
    payload = decode_cursor(message_id) or {}
    for rel, table in store.locate_all(username):
        conn = store.open(rel)
        if "s" in payload:
            rows = conn.execute(
                ROW_COLUMNS + table + " where sort_seq = ? limit 1", (payload["s"],)
            ).fetchall()
        elif str(message_id).isdigit():
            rows = conn.execute(
                ROW_COLUMNS + table + " where local_id = ? limit 1", (int(message_id),)
            ).fetchall()
        else:
            rows = []
        if not rows:
            continue
        row = rows[0]
        msg_type, sub_type = content.split_local_type(row[2])
        raw_text = content.decode_bytes(row[7], row[8])
        return resolve_media(store, username, msg_type, sub_type, raw_text, row[9], row[5])
    return None


def message_get(store: storemod.Store, conversation: str, message_id: str, with_media: bool = False) -> dict[str, Any]:
    username = store.resolve(conversation)
    payload = decode_cursor(message_id) or {}
    candidates = store.locate_all(username)
    if not candidates:
        return envelope("message.get", source_description(store), [], limit=1, coverage_complete=False)
    for rel, table in candidates:
        conn = store.open(rel)
        if "s" in payload:
            rows = conn.execute(ROW_COLUMNS + table + " where sort_seq = ? limit 1", (payload["s"],)).fetchall()
        elif message_id.isdigit():
            rows = conn.execute(ROW_COLUMNS + table + " where local_id = ? limit 1", (int(message_id),)).fetchall()
        else:
            rows = []
        if rows:
            item = _message_item(
                store,
                username,
                rows[0],
                _names(store, rel),
                self_user=store.self_username(),
                digest=table[4:],
                shard=rel,
                with_media=with_media,
            )
            return envelope("message.get", source_description(store), [item], limit=1)
    return envelope("message.get", source_description(store), [], limit=1, coverage_complete=False)


def _search_via_index(
    store: storemod.Store,
    query_text: str,
    limit: int,
    since: int | None,
    with_media: bool,
) -> dict[str, Any] | None:
    """Serve an unscoped search from the local text index, when one is built."""

    from . import index as indexmod

    try:
        idx = indexmod.open_index()
    except Exception:
        idx = None
    if idx is None:
        return None
    try:
        items, has_more = idx.search(query_text, None, since, limit)
        stats = idx.stats()
    except Exception:
        idx.close()
        return None
    idx.close()

    for item in items:
        item["conversationDisplayName"] = store.display_name(item["conversationId"])
        if with_media:
            found = message_get(store, item["conversationId"], item["id"], with_media=True)
            entry = (found.get("items") or [None])[0]
            if entry and entry.get("media"):
                item["media"] = entry["media"]
    return envelope(
        "messages.search",
        source_description(store),
        items,
        limit=limit,
        has_more=has_more,
        warnings=[
            {
                "code": "searchIndexUsed",
                "message": f"served from local text index built at {stats.get('builtAt')}; "
                "run `greenbubbles index build` to pick up newer messages",
            }
        ],
        database_count=len(store.shards()),
    )


def search(
    store: storemod.Store,
    query_text: str,
    conversation: str | None = None,
    limit: int = DEFAULT_PAGE_LIMIT,
    since: int | None = None,
    with_media: bool = False,
    use_index: bool | None = None,
) -> dict[str, Any]:
    limit = clamp_limit(limit)
    if not conversation and use_index is not False:
        indexed = _search_via_index(store, query_text, limit, since, with_media)
        if indexed is not None:
            return indexed
    lowered = query_text.lower()
    self_user = store.self_username()
    items: list[dict[str, Any]] = []
    scanned: list[str] = []
    seen: set[tuple[str, int]] = set()

    targets = [store.resolve(conversation)] if conversation else store.conversation_ids()
    for username in targets:
        candidates = store.locate_all(username)
        for rel, table in candidates:
            if rel not in scanned:
                scanned.append(rel)
            names = _names(store, rel)
            conn = store.open(rel)
            sql = ROW_COLUMNS + table
            params: list[Any] = []
            if since:
                sql += " where create_time >= ?"
                params.append(since)
            sql += " order by sort_seq desc"
            for row in conn.execute(sql, params):
                item = _message_item(
                    store,
                    username,
                    row,
                    names,
                    self_user=self_user,
                    digest=table[4:],
                    shard=rel,
                    with_media=with_media,
                )
                key = (username, item["sortSequence"])
                if key in seen:
                    continue
                if lowered in item["text"].lower():
                    seen.add(key)
                    found = item.get("media")
                    items.append(
                        {
                            "id": item["id"],
                            "conversationId": username,
                            "conversationDisplayName": store.display_name(username),
                            "sender": item["sender"],
                            "senderDisplayName": item["senderDisplayName"],
                            "createdAtUnix": item["createdAtUnix"],
                            "createdAt": item["createdAt"],
                            "sortSequence": item["sortSequence"],
                            "messageLocalId": item["messageLocalId"],
                            "messageType": item["messageType"],
                            "messageTypeLabel": item["messageTypeLabel"],
                            "messageSubtype": item["messageSubtype"],
                            "messageSubtypeLabel": item["messageSubtypeLabel"],
                            "snippet": item["text"][:200],
                        }
                    )
                    if found and items:
                        items[-1]["media"] = found
                if len(items) > limit:
                    break
        if len(items) > limit:
            break

    return envelope(
        "messages.search",
        source_description(store),
        items[:limit],
        limit=limit,
        has_more=len(items) > limit,
        database_count=len(scanned),
    )


# ---------------------------------------------------------------------------
# Contacts
# ---------------------------------------------------------------------------


def contacts(
    store: storemod.Store,
    limit: int = DEFAULT_PAGE_LIMIT,
    details: bool = False,
    kind: str | None = None,
) -> dict[str, Any]:
    limit = clamp_limit(limit)
    self_user = store.self_username()
    items = []
    for contact in store.contacts().values():
        contact_kind = contact.kind
        if kind and kind != contact_kind:
            continue
        if contact.username == self_user:
            contact_kind = "accountHolder"
        item: dict[str, Any] = {
            "id": contact.username,
            "displayName": contact.display_name,
            "kind": contact_kind,
            "isAccountHolder": contact.username == self_user,
        }
        if details:
            item["remark"] = contact.remark
            item["nickname"] = contact.nickname
            item["alias"] = contact.alias
        items.append(item)
    items.sort(key=lambda entry: (entry["kind"] != "accountHolder", entry["displayName"] or ""))
    return envelope(
        "contacts.list",
        source_description(store),
        items[:limit],
        limit=limit,
        has_more=len(items) > limit,
    )
