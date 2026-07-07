"""Read/query functions over the SQLite history store.

Structurally mirrors whatsapp-mcp's ``whatsapp.py`` (same chats+messages model,
same pagination/context approach) so the behaviour is familiar and predictable.
Search uses indexed ``LIKE`` over message content, which is plenty for a
personal message history.
"""

from datetime import datetime
from typing import Any, Optional

from db import DB_PATH, get_conn

_MSG_COLS = (
    "m.id, m.chat_jid, m.sender, m.sender_name, c.name AS chat_name, "
    "m.content, m.timestamp, m.is_from_me, m.media_type, m.attachment_id"
)


def _row_to_message(row: tuple) -> dict[str, Any]:
    return {
        "id": row[0],
        "chat_jid": row[1],
        "sender": row[2],
        "sender_name": row[3],
        "chat_name": row[4],
        "content": row[5],
        "timestamp": row[6],
        "is_from_me": bool(row[7]),
        "media_type": row[8],
        "attachment_id": row[9],
    }


def _row_to_chat(row: tuple) -> dict[str, Any]:
    return {
        "jid": row[0],
        "name": row[1],
        "is_group": bool(row[2]),
        "last_message_time": row[3],
        "last_message": row[4] if len(row) > 4 else None,
        "last_sender": row[5] if len(row) > 5 else None,
        "last_is_from_me": bool(row[6]) if len(row) > 6 and row[6] is not None else None,
    }


def list_chats(
    query: Optional[str] = None,
    limit: int = 20,
    page: int = 0,
    include_last_message: bool = True,
    sort_by: str = "last_active",
) -> list[dict]:
    conn = get_conn(DB_PATH)
    try:
        parts = [
            "SELECT c.jid, c.name, c.is_group, c.last_message_time, "
            "m.content, m.sender, m.is_from_me FROM chats c"
        ]
        if include_last_message:
            parts.append(
                "LEFT JOIN messages m ON c.jid = m.chat_jid "
                "AND c.last_message_time = m.timestamp"
            )
        params: list[Any] = []
        if query:
            parts.append("WHERE (LOWER(c.name) LIKE LOWER(?) OR c.jid LIKE ?)")
            params.extend([f"%{query}%", f"%{query}%"])
        order = "c.last_message_time DESC" if sort_by == "last_active" else "c.name"
        parts.append(f"ORDER BY {order}")
        parts.append("LIMIT ? OFFSET ?")
        params.extend([limit, page * limit])
        rows = conn.execute(" ".join(parts), tuple(params)).fetchall()
        return [_row_to_chat(r) for r in rows]
    finally:
        conn.close()


def get_chat(chat_jid: str) -> Optional[dict]:
    conn = get_conn(DB_PATH)
    try:
        row = conn.execute(
            "SELECT c.jid, c.name, c.is_group, c.last_message_time, "
            "m.content, m.sender, m.is_from_me FROM chats c "
            "LEFT JOIN messages m ON c.jid = m.chat_jid "
            "AND c.last_message_time = m.timestamp WHERE c.jid = ?",
            (chat_jid,),
        ).fetchone()
        return _row_to_chat(row) if row else None
    finally:
        conn.close()


def list_messages(
    after: Optional[str] = None,
    before: Optional[str] = None,
    sender: Optional[str] = None,
    chat_jid: Optional[str] = None,
    query: Optional[str] = None,
    limit: int = 20,
    page: int = 0,
    include_context: bool = True,
    context_before: int = 1,
    context_after: int = 1,
) -> list[dict]:
    conn = get_conn(DB_PATH)
    try:
        parts = [
            f"SELECT {_MSG_COLS} FROM messages m "
            "JOIN chats c ON m.chat_jid = c.jid"
        ]
        where: list[str] = []
        params: list[Any] = []
        if after:
            where.append("m.timestamp > ?")
            params.append(_iso(after))
        if before:
            where.append("m.timestamp < ?")
            params.append(_iso(before))
        if sender:
            where.append("m.sender = ?")
            params.append(sender)
        if chat_jid:
            where.append("m.chat_jid = ?")
            params.append(chat_jid)
        if query:
            where.append("LOWER(m.content) LIKE LOWER(?)")
            params.append(f"%{query}%")
        if where:
            parts.append("WHERE " + " AND ".join(where))
        parts.append("ORDER BY m.timestamp DESC")
        parts.append("LIMIT ? OFFSET ?")
        params.extend([limit, page * limit])
        rows = conn.execute(" ".join(parts), tuple(params)).fetchall()
        result = [_row_to_message(r) for r in rows]

        if include_context and result:
            seen: set[tuple[str, str]] = set()
            with_ctx: list[dict] = []
            for msg in result:
                ctx = get_message_context(
                    msg["id"], msg["chat_jid"], context_before, context_after
                )
                for m in ctx["before"] + [ctx["message"]] + ctx["after"]:
                    key = (m["id"], m["chat_jid"])
                    if key not in seen:
                        seen.add(key)
                        with_ctx.append(m)
            return with_ctx
        return result
    finally:
        conn.close()


def get_message_context(
    message_id: str,
    chat_jid: str,
    before: int = 5,
    after: int = 5,
) -> dict:
    conn = get_conn(DB_PATH)
    try:
        target = conn.execute(
            f"SELECT {_MSG_COLS} FROM messages m JOIN chats c ON m.chat_jid = c.jid "
            "WHERE m.id = ? AND m.chat_jid = ?",
            (message_id, chat_jid),
        ).fetchone()
        if not target:
            raise ValueError(
                f"Message id={message_id} in chat={chat_jid} not found"
            )
        ts = target[6]
        before_rows = conn.execute(
            f"SELECT {_MSG_COLS} FROM messages m JOIN chats c ON m.chat_jid = c.jid "
            "WHERE m.chat_jid = ? AND m.timestamp < ? "
            "ORDER BY m.timestamp DESC LIMIT ?",
            (chat_jid, ts, before),
        ).fetchall()
        after_rows = conn.execute(
            f"SELECT {_MSG_COLS} FROM messages m JOIN chats c ON m.chat_jid = c.jid "
            "WHERE m.chat_jid = ? AND m.timestamp > ? "
            "ORDER BY m.timestamp ASC LIMIT ?",
            (chat_jid, ts, after),
        ).fetchall()
        return {
            "message": _row_to_message(target),
            "before": [_row_to_message(r) for r in reversed(before_rows)],
            "after": [_row_to_message(r) for r in after_rows],
        }
    finally:
        conn.close()


def search_messages(query: str, limit: int = 30) -> list[dict]:
    """Convenience full-history content search (no context expansion)."""
    return list_messages(query=query, limit=limit, include_context=False)


def _iso(value: str) -> str:
    """Validate/normalise an ISO-8601 date filter, raising on garbage."""
    try:
        return datetime.fromisoformat(value).isoformat()
    except ValueError as e:
        raise ValueError(
            f"Invalid ISO-8601 date: {value!r}. Use e.g. 2026-07-01T00:00:00."
        ) from e
