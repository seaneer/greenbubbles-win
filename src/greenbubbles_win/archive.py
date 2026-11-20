"""Archive WeChat history into a durable, searchable local store."""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from . import content, media, query
from .store import Store, is_group

SAFE_RE = re.compile(r"[^\w\u4e00-\u9fff.@-]+")


def safe_name(text: str) -> str:
    cleaned = SAFE_RE.sub("_", text)[:80].strip("_") or "conversation"
    return cleaned


def index_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        create table if not exists messages(
            conversation text not null,
            conversation_name text,
            local_id integer not null,
            sort_seq integer not null,
            created_at integer,
            sender text,
            sender_name text,
            is_self integer,
            msg_type integer,
            subtype integer,
            text text,
            primary key(conversation, local_id)
        );
        create index if not exists messages_time on messages(created_at);
        create table if not exists conversations(
            conversation text primary key,
            display_name text,
            kind text,
            watermark_sort_seq integer default 0,
            message_count integer default 0,
            exported_at text
        );
        """
    )


def update(
    store: Store,
    out_dir: Path | str,
    conversations: list[str] | None = None,
    formats: tuple[str, ...] = ("md", "jsonl"),
    progress: Callable[[str], None] | None = None,
    full: bool = False,
    attachments: bool = False,
) -> dict[str, Any]:
    """Incrementally append new messages per conversation into the archive."""

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    conv_dir = out / "conversations"
    conv_dir.mkdir(exist_ok=True)

    conn = sqlite3.connect(out / "index.sqlite")
    index_schema(conn)

    targets = conversations or store.conversation_ids()
    total_new = 0
    summary: list[dict[str, Any]] = []
    self_user = store.self_username()

    for username in targets:
        candidates = store.locate_all(username)
        if not candidates:
            continue
        watermark_row = conn.execute(
            "select watermark_sort_seq from conversations where conversation=?", (username,)
        ).fetchone()
        watermark = 0 if full or not watermark_row else int(watermark_row[0])

        entries = [
            entry
            for entry in query.merged_rows(store, username, 20000)
            if int(entry[0][3]) > watermark
        ]
        entries.reverse()
        rows = entries
        if not rows:
            conn.execute(
                "insert into conversations(conversation, display_name, kind, message_count, exported_at) "
                "values(?,?,?,coalesce((select message_count from conversations where conversation=?),0),?) "
                "on conflict(conversation) do update set display_name=excluded.display_name, exported_at=excluded.exported_at",
                (
                    username,
                    store.display_name(username),
                    "group" if is_group(username) else "person",
                    username,
                    datetime.now().isoformat(timespec="seconds"),
                ),
            )
            continue

        if progress:
            progress(f"archiving {store.display_name(username)} (+{len(rows)})")

        names_by_shard: dict[str, dict[int, str]] = {}
        jsonl_lines: list[str] = []
        md_lines: list[str] = []
        if full:
            md_header = f"# {store.display_name(username)}\n\n`{username}`\n\n"
            md_lines.append(md_header)
        last_day = None
        for row, rel, table in rows:
            if rel not in names_by_shard:
                names_by_shard[rel] = {
                    rowid: name for rowid, name in store.open(rel).execute("select rowid, user_name from Name2Id")
                }
            item = query._message_item(
                store,
                username,
                row,
                names_by_shard[rel],
                self_user=self_user,
                digest=table[4:],
                shard=rel,
            )
            total_new += 1
            conn.execute(
                "insert or replace into messages(conversation, conversation_name, local_id, sort_seq, created_at, "
                "sender, sender_name, is_self, msg_type, subtype, text) values(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    username,
                    store.display_name(username),
                    item["messageLocalId"],
                    item["sortSequence"],
                    item["createdAtUnix"],
                    item["sender"],
                    item["senderDisplayName"] or item["sender"],
                    int(item["isSelf"]),
                    item["messageType"],
                    item["messageSubtype"],
                    item["text"],
                ),
            )
            record = {
                "conversation": username,
                "conversationName": store.display_name(username),
                "sortSequence": item["sortSequence"],
                "serverId": item["serverId"],
                "sender": item["sender"],
                "senderName": item["senderDisplayName"] or item["sender"],
                "isSelf": item["isSelf"],
                "createdAtUnix": item["createdAtUnix"],
                "createdAt": item["createdAt"],
                "messageType": item["messageType"],
                "messageTypeLabel": item["messageTypeLabel"],
                "messageSubtype": item["messageSubtype"],
                "messageSubtypeLabel": item["messageSubtypeLabel"],
                "text": item["text"],
            }
            jsonl_lines.append(json.dumps(record, ensure_ascii=False))

            if attachments:
                raw_text = content.decode_bytes(row[7], row[8])
                found_media = query.resolve_media(
                    store,
                    username,
                    item["messageType"],
                    item["messageSubtype"],
                    raw_text,
                    row[9],
                    item["createdAtUnix"],
                )
                if found_media:
                    saved = media.save(found_media, out / "attachments" / safe_name(store.display_name(username)))
                    rel_link = saved.relative_to(out).as_posix()
                    record["mediaPath"] = rel_link
                    jsonl_lines[-1] = json.dumps(record, ensure_ascii=False)

            when = datetime.fromtimestamp(item["createdAtUnix"], query.TZ)
            day = when.strftime("%Y-%m-%d")
            if day != last_day:
                md_lines.append(f"\n## {day}\n\n")
                last_day = day
            speaker = "我" if item["isSelf"] else (item["senderDisplayName"] or item["sender"])
            text = item["text"].replace("\n", "\n  ")
            if attachments and record.get("mediaPath"):
                text = f"{text} [附件]({record['mediaPath']})"
            md_lines.append(f"- `{when.strftime('%H:%M')}` **{speaker}**: {text}\n")

        slug = safe_name(store.display_name(username))
        if "jsonl" in formats:
            with open(conv_dir / f"{slug}.jsonl", "a", encoding="utf-8") as handle:
                for line in jsonl_lines:
                    handle.write(line + "\n")
        if "md" in formats:
            mode = "w" if full else "a"
            with open(conv_dir / f"{slug}.md", mode, encoding="utf-8") as handle:
                handle.writelines(md_lines)

        new_watermark = int(rows[-1][0][3])
        count_row = conn.execute(
            "select count(*) from messages where conversation=?", (username,)
        ).fetchone()
        conn.execute(
            "insert into conversations(conversation, display_name, kind, watermark_sort_seq, message_count, exported_at) "
            "values(?,?,?,?,?,?) on conflict(conversation) do update set "
            "display_name=excluded.display_name, watermark_sort_seq=excluded.watermark_sort_seq, "
            "message_count=excluded.message_count, exported_at=excluded.exported_at",
            (
                username,
                store.display_name(username),
                "group" if is_group(username) else "person",
                new_watermark,
                int(count_row[0]) if count_row else 0,
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
        summary.append({"id": username, "displayName": store.display_name(username), "newMessages": len(rows)})

    conn.commit()
    conn.close()

    manifest_path = out / "manifest.json"
    manifest = {}
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            manifest = {}
    manifest.update(
        {
            "formatVersion": 1,
            "sourceIdentity": query.source_description(store)["identity"],
            "sourceMode": "live",
            "updatedAt": datetime.now().isoformat(timespec="seconds"),
            "newMessages": total_new,
            "conversations": len(summary) or manifest.get("conversations", 0),
        }
    )
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")

    return {"ok": True, "outDir": str(out), "newMessages": total_new, "conversations": summary}


def export_html(store: Store, out_dir: Path | str, conversations: list[str] | None = None) -> dict[str, Any]:
    """Write a self-contained HTML archive with a searchable index."""

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    targets = conversations or store.conversation_ids()
    self_user = store.self_username()

    pages: list[dict[str, Any]] = []
    for username in targets:
        candidates = store.locate_all(username)
        if not candidates:
            continue
        rows = [
            entry
            for entry in reversed(query.merged_rows(store, username, 20000))
        ]
        names_by_shard: dict[str, dict[int, str]] = {}
        blocks = []
        last_day = None
        for row, rel, table in rows:
            if rel not in names_by_shard:
                names_by_shard[rel] = {
                    rowid: name for rowid, name in store.open(rel).execute("select rowid, user_name from Name2Id")
                }
            item = query._message_item(
                store,
                username,
                row,
                names_by_shard[rel],
                self_user=self_user,
                digest=table[4:],
                shard=rel,
            )
            when = datetime.fromtimestamp(item["createdAtUnix"], query.TZ)
            day = when.strftime("%Y-%m-%d")
            if day != last_day:
                blocks.append(f"<div class='day'>{day}</div>")
                last_day = day
            who = "我" if item["isSelf"] else (item["senderDisplayName"] or item["sender"])
            body = (
                item["text"]
                .replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
                .replace("\n", "<br>")
            )
            side = "self" if item["isSelf"] else "other"
            blocks.append(
                f"<div class='msg {side}'><span class='who'>{who}</span>"
                f"<span class='time'>{when.strftime('%H:%M')}</span><div class='body'>{body}</div></div>"
            )
        slug = safe_name(store.display_name(username))
        html = _page_template(store.display_name(username), username, "\n".join(blocks))
        (out / f"{slug}.html").write_text(html, encoding="utf-8")
        pages.append({"slug": slug, "id": username, "displayName": store.display_name(username), "count": len(rows)})

    index = _index_template(pages)
    (out / "index.html").write_text(index, encoding="utf-8")
    return {"ok": True, "outDir": str(out), "pages": len(pages)}


def _page_template(title: str, username: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} · 微信归档</title>
<style>
:root{{--bg:#f6f6f7;--card:#fff;--ink:#1c1c1e;--muted:#8a8a8e;--self:#d8f5d2;--line:#e5e5ea}}
body{{margin:0;background:var(--bg);color:var(--ink);font:15px/1.6 "Segoe UI","Microsoft YaHei",sans-serif}}
header{{position:sticky;top:0;background:rgba(255,255,255,.92);backdrop-filter:blur(8px);padding:14px 20px;border-bottom:1px solid var(--line)}}
h1{{font-size:18px;margin:0}} .sub{{color:var(--muted);font-size:12px}}
main{{max-width:820px;margin:0 auto;padding:16px 18px 60px}}
.day{{text-align:center;color:var(--muted);font-size:12px;margin:22px 0 10px}}
.msg{{max-width:76%;margin:6px 0;padding:8px 12px;border-radius:12px;background:var(--card);box-shadow:0 1px 2px rgba(0,0,0,.05)}}
.msg.self{{margin-left:auto;background:var(--self)}}
.who{{font-size:12px;color:var(--muted);margin-right:6px}} .time{{font-size:11px;color:var(--muted)}}
.body{{white-space:pre-wrap}}
</style></head><body>
<header><h1>{title}</h1><div class="sub">{username}</div></header>
<main>{body}</main></body></html>"""


def _index_template(pages: list[dict[str, Any]]) -> str:
    rows = "\n".join(
        f"<li><a href='{page['slug']}.html'>{page['displayName']}</a>"
        f"<span class='n'>{page['count']} 条</span></li>"
        for page in sorted(pages, key=lambda item: item["displayName"] or "")
    )
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>微信聊天归档</title>
<style>
body{{margin:0;font:15px/1.6 "Segoe UI","Microsoft YaHei",sans-serif;background:#f6f6f7;color:#1c1c1e}}
main{{max-width:820px;margin:0 auto;padding:24px 18px}}
h1{{font-size:20px}}
ul{{list-style:none;padding:0}}
li{{background:#fff;margin:8px 0;padding:12px 14px;border-radius:10px;display:flex;justify-content:space-between}}
a{{color:#1c1c1e;text-decoration:none;font-weight:600}} .n{{color:#8a8a8e;font-size:12px}}
</style></head><body><main>
<h1>微信聊天归档</h1><ul>{rows}</ul></main></body></html>"""
