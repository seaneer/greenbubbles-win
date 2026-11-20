"""Persistent text index that makes unscoped keyword search fast.

WeChat's own ``message_fts.db`` uses a private FTS5 tokenizer (``MMFtsTokenizer``)
that ordinary SQLite builds cannot open, so we materialise our own plain-text
table in ``state``. Every message is decoded once during ``index build``; later
searches are a single ``LIKE`` pass over one table instead of re-decoding 800k+
rows across 12 shards.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable, Iterable

from . import content, paths, query as querymod, store as storemod

INDEX_FILENAME = "search-index.sqlite3"
INDEX_SCHEMA_VERSION = 2

SCHEMA = """
create table if not exists msg_text (
    conversation text not null,
    sort_seq integer not null,
    message_local_id integer not null,
    server_id integer,
    created_at integer not null,
    sender text,
    sender_display text,
    is_self integer default 0,
    message_type integer,
    message_subtype integer,
    type_label text,
    subtype_label text,
    message_id text,
    body text not null,
    primary key (conversation, sort_seq, message_local_id)
);
create index if not exists idx_msg_text_time on msg_text (created_at desc);
create index if not exists idx_msg_text_conv_time on msg_text (conversation, created_at desc);
create table if not exists index_state (
    conversation text primary key,
    max_sort_seq integer not null,
    indexed_at integer not null
);
create table if not exists meta (
    key text primary key,
    value text
);
"""


def default_path() -> Path:
    return paths.state_dir() / INDEX_FILENAME


class SearchIndex:
    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path else default_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.execute("pragma journal_mode=wal")
        self.conn.execute("pragma synchronous=normal")
        self.conn.commit()

    def _migrate(self) -> None:
        """Old layouts keyed on (conversation, sort_seq) and lost colliding rows."""

        row = self.conn.execute("select value from meta where key='schemaVersion'").fetchone()
        try:
            version = int(row[0]) if row else 0
        except ValueError:
            version = 0
        if not version or version >= INDEX_SCHEMA_VERSION:
            return
        self.conn.executescript(
            "drop table if exists msg_text; drop table if exists index_state;"
        )
        self.conn.executescript(SCHEMA)
        self.conn.execute("delete from meta where key='complete'")
        self.conn.commit()

    # -- metadata ---------------------------------------------------------
    def meta(self) -> dict[str, Any]:
        values = {k: v for k, v in self.conn.execute("select key, value from meta")}
        try:
            values["schemaVersion"] = int(values.get("schemaVersion", 0))
        except ValueError:
            values["schemaVersion"] = 0
        for key in ("rows", "conversations"):
            try:
                values[key] = int(values.get(key, 0))
            except ValueError:
                values[key] = 0
        return values

    def set_meta(self, **values: Any) -> None:
        for key, value in values.items():
            self.conn.execute(
                "insert into meta(key, value) values(?, ?) on conflict(key) do update set value=excluded.value",
                (key, str(value)),
            )
        self.conn.commit()

    def stats(self) -> dict[str, Any]:
        meta = self.meta()
        return {
            "path": str(self.path),
            "bytes": self.path.stat().st_size if self.path.exists() else 0,
            "rows": meta.get("rows", 0),
            "conversations": meta.get("conversations", 0),
            "builtAt": meta.get("builtAt"),
            "complete": meta.get("complete") == "1",
            "schemaVersion": meta.get("schemaVersion", 0),
        }

    def exists(self) -> bool:
        return self.path.exists() and self.meta().get("complete") == "1"

    def watermark(self, conversation: str) -> int:
        row = self.conn.execute(
            "select max_sort_seq from index_state where conversation = ?", (conversation,)
        ).fetchone()
        return int(row[0]) if row else -1

    # -- build ------------------------------------------------------------
    def build(
        self,
        store: storemod.Store,
        conversations: Iterable[str] | None = None,
        progress: Callable[[str], None] | None = None,
        batch: int = 200,
    ) -> dict[str, Any]:
        started = time.time()
        targets = list(conversations) if conversations else store.conversation_ids()
        total_new = 0
        indexed_conversations = 0
        pending: list[tuple] = []
        self.conn.commit()

        for position, username in enumerate(targets, 1):
            mark = self.watermark(username)
            newest = mark
            for rel, table in store.locate_all(username):
                conn = store.open(rel)
                names = querymod._names(store, rel)
                rows = conn.execute(
                    querymod.ROW_COLUMNS + table + " where sort_seq > ? order by sort_seq",
                    (mark,),
                ).fetchall()
                for row in rows:
                    item = querymod._message_item(
                        store,
                        username,
                        row,
                        names,
                        self_user=store.self_username(),
                        digest=table[4:],
                        shard=rel,
                        with_media=False,
                    )
                    newest = max(newest, item["sortSequence"])
                    pending.append(
                        (
                            username,
                            item["sortSequence"],
                            item["messageLocalId"],
                            item["serverId"],
                            item["createdAtUnix"],
                            item["sender"],
                            item["senderDisplayName"],
                            1 if item.get("isSelf") else 0,
                            item["messageType"],
                            item["messageSubtype"],
                            item["messageTypeLabel"],
                            item["messageSubtypeLabel"],
                            item["id"],
                            item["text"],
                        )
                    )
                    if len(pending) >= batch:
                        total_new += self._flush(pending)
                        pending = []
            total_new += self._flush(pending)
            pending = []
            if newest > mark:
                self.conn.execute(
                    "insert into index_state(conversation, max_sort_seq, indexed_at) values(?, ?, ?) "
                    "on conflict(conversation) do update set max_sort_seq=excluded.max_sort_seq, indexed_at=excluded.indexed_at",
                    (username, newest, int(time.time())),
                )
                self.conn.commit()
                indexed_conversations += 1
            if progress and (position % 250 == 0 or position == len(targets)):
                progress(f"indexed {position}/{len(targets)} conversations, +{total_new} messages")

        total_new += self._flush(pending)
        counts = self._counts()
        partial = conversations is not None
        meta_values: dict[str, Any] = {
            "schemaVersion": INDEX_SCHEMA_VERSION,
            "rows": counts["rows"],
            "conversations": counts["conversations"],
            "builtAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        if not partial:
            meta_values["complete"] = "1"
        elif self.meta().get("complete") != "1":
            meta_values["complete"] = "0"
        self.set_meta(**meta_values)
        return {
            "ok": True,
            "addedMessages": total_new,
            "conversationsUpdated": indexed_conversations,
            "rows": counts["rows"],
            "conversations": counts["conversations"],
            "partial": partial,
            "elapsedSeconds": round(time.time() - started, 1),
            "path": str(self.path),
        }

    def _flush(self, pending: list[tuple]) -> int:
        if not pending:
            return 0
        self.conn.executemany(
            "insert into msg_text(conversation, sort_seq, message_local_id, server_id, created_at, sender, "
            "sender_display, is_self, message_type, message_subtype, type_label, subtype_label, message_id, body) "
            "values(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "on conflict(conversation, sort_seq, message_local_id) do update set body=excluded.body, "
            "message_id=excluded.message_id",
            pending,
        )
        self.conn.commit()
        return len(pending)

    def _counts(self) -> dict[str, int]:
        rows = self.conn.execute("select count(*) from msg_text").fetchone()[0]
        conversations = self.conn.execute("select count(distinct conversation) from msg_text").fetchone()[0]
        return {"rows": int(rows), "conversations": int(conversations)}

    # -- search -----------------------------------------------------------
    def search(
        self,
        needle: str,
        conversation: str | None = None,
        since: int | None = None,
        limit: int = 100,
    ) -> tuple[list[dict[str, Any]], bool]:
        pattern = "%" + needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        sql = (
            "select conversation, sort_seq, message_local_id, server_id, created_at, sender, sender_display, "
            "is_self, message_type, message_subtype, type_label, subtype_label, message_id, "
            "substr(body, 1, 200) from msg_text where body like ? escape '\\'"
        )
        params: list[Any] = [pattern]
        if conversation:
            sql += " and conversation = ?"
            params.append(conversation)
        if since:
            sql += " and created_at >= ?"
            params.append(since)
        sql += " order by created_at desc, sort_seq desc, message_local_id asc limit ?"
        params.append(limit + 50)
        rows = self.conn.execute(sql, params).fetchall()
        rows = self._dedupe(rows, limit)
        has_more = len(rows) > limit
        items: list[dict[str, Any]] = []
        for row in rows[:limit]:
            items.append(
                {
                    "id": row[12],
                    "conversationId": row[0],
                    "conversationDisplayName": None,
                    "sender": row[5],
                    "senderDisplayName": row[6],
                    "createdAtUnix": row[4],
                    "createdAt": querymod.iso_time(row[4]),
                    "sortSequence": row[1],
                    "messageLocalId": row[2],
                    "messageType": row[8],
                    "messageTypeLabel": row[10],
                    "messageSubtype": row[9],
                    "messageSubtypeLabel": row[11],
                    "snippet": row[13],
                }
            )
        return items, has_more

    @staticmethod
    def _dedupe(rows: list[tuple], limit: int) -> list[tuple]:
        """Two messages can share one sort_seq after a shard migration; keep the first."""

        seen: set[tuple[str, int]] = set()
        kept: list[tuple] = []
        for row in rows:
            key = (row[0], row[1])
            if key in seen:
                continue
            seen.add(key)
            kept.append(row)
            if len(kept) > limit:
                break
        return kept

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass


def open_index() -> SearchIndex | None:
    """Return the index when it exists and is complete, else ``None``."""

    path = default_path()
    if not path.exists():
        return None
    try:
        index = SearchIndex(path)
        if index.meta().get("complete") == "1":
            return index
        index.close()
    except Exception:
        return None
    return None
