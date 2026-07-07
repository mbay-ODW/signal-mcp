"""SQLite history store for the Signal MCP server.

The schema deliberately mirrors the whatsapp-mcp store (``chats`` + ``messages``)
so the read/query functions in ``signal_store.py`` stay close to the proven
WhatsApp implementation. The store is written *only* by ``receiver.py`` (the
Signal receive loop) and read by the MCP tools. WAL mode makes the concurrent
one-writer / many-reader access safe.

A Signal "chat" is either a direct conversation (``jid`` = the contact's phone
number in E.164, e.g. ``+491701234567``) or a group (``jid`` = ``group.<base64
groupId>``). A message ``id`` is the Signal message timestamp (ms since epoch)
as a string; it is only unique *within* a chat, hence the composite primary key
``(id, chat_jid)``.
"""

import os
import sqlite3

DB_PATH = os.getenv("SIGNAL_DB_PATH", "/data/signal.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS chats (
    jid               TEXT PRIMARY KEY,
    name              TEXT,
    is_group          INTEGER NOT NULL DEFAULT 0,
    last_message_time TEXT
);

CREATE TABLE IF NOT EXISTS messages (
    id            TEXT NOT NULL,
    chat_jid      TEXT NOT NULL,
    sender        TEXT,
    sender_name   TEXT,
    content       TEXT,
    timestamp     TEXT NOT NULL,
    is_from_me    INTEGER NOT NULL DEFAULT 0,
    media_type    TEXT,
    attachment_id TEXT,
    PRIMARY KEY (id, chat_jid)
);

CREATE INDEX IF NOT EXISTS idx_messages_chat_time ON messages (chat_jid, timestamp);
CREATE INDEX IF NOT EXISTS idx_messages_time      ON messages (timestamp);
CREATE INDEX IF NOT EXISTS idx_messages_sender    ON messages (sender);
"""


def get_conn(path: str = DB_PATH) -> sqlite3.Connection:
    """Open a connection with WAL enabled and a sane busy timeout."""
    conn = sqlite3.connect(path, timeout=30.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(path: str = DB_PATH) -> None:
    """Create the directory + tables if they don't exist yet."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = get_conn(path)
    try:
        conn.executescript(_SCHEMA)
        conn.commit()
    finally:
        conn.close()


def upsert_chat(
    conn: sqlite3.Connection,
    jid: str,
    name: str | None,
    is_group: bool,
    last_message_time: str | None,
) -> None:
    """Insert or update a chat row.

    ``name`` is only overwritten when a non-empty value is supplied (so a later
    envelope without a name doesn't wipe a name we already learned).
    ``last_message_time`` only advances forward.
    """
    conn.execute(
        """
        INSERT INTO chats (jid, name, is_group, last_message_time)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(jid) DO UPDATE SET
            name = COALESCE(NULLIF(excluded.name, ''), chats.name),
            is_group = excluded.is_group,
            last_message_time = CASE
                WHEN excluded.last_message_time IS NOT NULL
                     AND (chats.last_message_time IS NULL
                          OR excluded.last_message_time > chats.last_message_time)
                THEN excluded.last_message_time
                ELSE chats.last_message_time
            END
        """,
        (jid, name or "", 1 if is_group else 0, last_message_time),
    )


def upsert_message(conn: sqlite3.Connection, msg: dict) -> bool:
    """Insert a message if not already present. Returns True if it was new.

    Expected keys: id, chat_jid, sender, sender_name, content, timestamp,
    is_from_me, media_type, attachment_id.
    """
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO messages
            (id, chat_jid, sender, sender_name, content, timestamp,
             is_from_me, media_type, attachment_id)
        VALUES (:id, :chat_jid, :sender, :sender_name, :content, :timestamp,
                :is_from_me, :media_type, :attachment_id)
        """,
        {
            "id": msg["id"],
            "chat_jid": msg["chat_jid"],
            "sender": msg.get("sender"),
            "sender_name": msg.get("sender_name"),
            "content": msg.get("content"),
            "timestamp": msg["timestamp"],
            "is_from_me": 1 if msg.get("is_from_me") else 0,
            "media_type": msg.get("media_type"),
            "attachment_id": msg.get("attachment_id"),
        },
    )
    return cur.rowcount > 0
