"""Read-only access layer over the decrypted WeChat databases."""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from . import content, paths

MESSAGE_SHARD_RE = "message/message_"


@dataclass
class Contact:
    username: str
    remark: str | None
    nickname: str | None
    alias: str | None
    local_type: int

    @property
    def display_name(self) -> str:
        return self.remark or self.nickname or self.alias or self.username

    @property
    def kind(self) -> str:
        return contact_kind(self.username, self.local_type)


@dataclass
class Session:
    username: str
    type: int
    unread_count: int
    summary: str
    sort_timestamp: int
    last_timestamp: int
    last_msg_type: int
    last_msg_sender: str
    last_sender_display_name: str


def contact_kind(username: str, local_type: int) -> str:
    if username.endswith("@chatroom"):
        return "group"
    if username.startswith("gh_"):
        return "official"
    if username in {"weixin", "qqmail", "medianote", "floatbottle", "fmessage", "newsapp", "notifymessage"}:
        return "service"
    if username.startswith("wxid_") or username == "":
        return "person"
    if "@" in username:
        return "official"
    return "person"


def is_group(username: str) -> bool:
    return username.endswith("@chatroom")


class Store:
    """Everything reads through the decrypted cache, never the live files."""

    def __init__(self, account: paths.Account, keyset, log=None) -> None:
        self.account = account
        self.keyset = keyset
        self.cache = paths.ReadCache(account, keyset)
        self.log = log or (lambda _message: None)
        self._conns: dict[str, sqlite3.Connection] = {}
        self._tables: dict[str, set[str]] = {}
        self._contacts: dict[str, Contact] | None = None
        self._self: str | None = None
        self._conversation_map: dict[str, list[tuple[str, str]]] | None = None
        self._newest_cache: dict[str, tuple[str, str]] = {}
        self._media = None

    @property
    def media(self):
        if self._media is None:
            from .media import MediaResolver

            self._media = MediaResolver(self.account)
        return self._media

    # -- connections -------------------------------------------------------
    def open(self, rel: str) -> sqlite3.Connection:
        if rel in self._conns:
            return self._conns[rel]
        plain = self.cache.ensure(rel, progress=self.log)
        conn = sqlite3.connect(f"file:{plain.as_posix()}?mode=ro", uri=True, check_same_thread=False)
        conn.execute("pragma query_only=1")
        self._conns[rel] = conn
        return conn

    def shards(self) -> list[str]:
        found = []
        for rel in self.keyset.entries:
            name = rel.rsplit("/", 1)[-1]
            if rel.startswith("message/") and name.startswith("message_") and name.endswith(".db"):
                found.append(rel)
        return sorted(found)

    def tables(self, rel: str) -> set[str]:
        if rel not in self._tables:
            conn = self.open(rel)
            self._tables[rel] = {
                row[0]
                for row in conn.execute("select name from sqlite_master where type='table' and name like 'Msg_%'")
            }
        return self._tables[rel]

    # -- reference data ----------------------------------------------------
    def contacts(self) -> dict[str, Contact]:
        if self._contacts is not None:
            return self._contacts
        result: dict[str, Contact] = {}
        if "contact/contact.db" in self.keyset.entries:
            conn = self.open("contact/contact.db")
            for username, remark, nickname, alias, local_type in conn.execute(
                "select username, remark, nick_name, alias, local_type from contact"
            ):
                if not username:
                    continue
                result[username] = Contact(username, remark, nickname, alias, local_type or 0)
        self._contacts = result
        return result

    def display_name(self, username: str) -> str:
        contact = self.contacts().get(username)
        if contact and contact.display_name:
            return contact.display_name
        return username

    def sessions(self) -> list[Session]:
        conn = self.open("session/session.db")
        rows = conn.execute(
            """
            select username, type, unread_count, summary, sort_timestamp, last_timestamp,
                   last_msg_type, last_msg_sender, last_sender_display_name
            from SessionTable
            order by sort_timestamp desc
            """
        ).fetchall()
        sessions = []
        for row in rows:
            username = row[0]
            if not username or username == "@placeholder_foldgroup":
                continue
            sessions.append(
                Session(
                    username=username,
                    type=row[1] or 0,
                    unread_count=row[2] or 0,
                    summary=row[3] or "",
                    sort_timestamp=row[4] or 0,
                    last_timestamp=row[5] or 0,
                    last_msg_type=row[6] or 0,
                    last_msg_sender=row[7] or "",
                    last_sender_display_name=row[8] or "",
                )
            )
        return sessions

    def self_username(self) -> str:
        """Identify the account holder's own wxid without guessing."""

        if self._self:
            return self._self
        prefix = self.account.account_dir.name.rsplit("_", 1)[0].lower()
        candidates: list[str] = []
        for rel in self.shards()[:2]:
            conn = self.open(rel)
            candidates.extend(row[0] for row in conn.execute("select user_name from Name2Id"))
        for candidate in candidates:
            if candidate and candidate.lower() == prefix:
                self._self = candidate
                return candidate
        for candidate in candidates:
            if candidate and prefix.startswith(candidate.lower() + "_"):
                self._self = candidate
                return candidate

        # Fallback: the sender that appears in the most one-to-one chats.
        counts: dict[str, int] = {}
        for username in [session.username for session in self.sessions() if not is_group(session.username)][:50]:
            located = self.locate(username)
            if not located:
                continue
            rel, table = located
            conn = self.open(rel)
            names = {rowid: name for rowid, name in conn.execute("select rowid, user_name from Name2Id")}
            for (sender_id,) in conn.execute(f"select distinct real_sender_id from {table}"):
                sender = names.get(sender_id, "")
                if sender and sender != username:
                    counts[sender] = counts.get(sender, 0) + 1
        self._self = max(counts, key=counts.get) if counts else prefix
        return self._self

    # -- conversation routing ---------------------------------------------
    def _build_map(self) -> None:
        mapping: dict[str, list[tuple[str, str]]] = {}
        for rel in self.shards():
            for table in self.tables(rel):
                mapping.setdefault(table[4:].lower(), []).append((rel, table))
        self._conversation_map = mapping
        self._newest_cache = {}

    def locate_all(self, username: str) -> list[tuple[str, str]]:
        """Every shard that holds a copy of this conversation.

        WeChat migrates conversations between ``message_<n>.db`` shards over
        time, so the same ``Msg_<hash>`` table can exist in several shards with
        different coverage.
        """

        if self._conversation_map is None:
            self._build_map()
        digest = hashlib.md5(username.encode("utf-8")).hexdigest()
        return list(self._conversation_map.get(digest, []))

    def locate(self, username: str) -> tuple[str, str] | None:
        """The shard with the newest data for this conversation."""

        candidates = self.locate_all(username)
        if not candidates:
            return None
        cached = self._newest_cache.get(username)
        if cached:
            return cached
        best: tuple[str, str] | None = None
        best_seq = -1
        for rel, table in candidates:
            row = self.open(rel).execute(f"select max(sort_seq) from {table}").fetchone()
            seq = int(row[0] or -1)
            if seq > best_seq:
                best, best_seq = (rel, table), seq
        if best:
            self._newest_cache[username] = best
        return best

    def conversation_ids(self) -> list[str]:
        """All conversation usernames that have a message table."""

        ids: list[str] = []
        seen: set[str] = set()
        for session in self.sessions():
            if session.username not in seen:
                seen.add(session.username)
                ids.append(session.username)
        return ids

    def message_count(self, username: str) -> int:
        located = self.locate(username)
        if not located:
            return 0
        rel, table = located
        row = self.open(rel).execute(f"select count(*) from {table}").fetchone()
        return int(row[0]) if row else 0

    def resolve(self, query: str) -> str:
        """Accept a username, a display name, or a partial match."""

        query = query.strip()
        contacts = self.contacts()
        if query in contacts or query.endswith("@chatroom"):
            return query
        if not query:
            return query
        exact: list[str] = []
        partial: list[str] = []
        lowered = query.lower()
        for username, contact in contacts.items():
            names = [contact.display_name, contact.remark or "", contact.nickname or "", contact.alias or ""]
            for name in names:
                if not name:
                    continue
                if name.lower() == lowered:
                    exact.append(username)
                elif lowered in name.lower():
                    partial.append(username)
        for session in self.sessions():
            if lowered in session.username.lower():
                partial.append(session.username)
        if exact:
            return exact[0]
        if partial:
            unique = list(dict.fromkeys(partial))
            return unique[0]
        return query

    def close(self) -> None:
        for conn in self._conns.values():
            try:
                conn.close()
            except Exception:
                pass
        self._conns.clear()
