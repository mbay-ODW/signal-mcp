"""Background Signal receive loop.

Subscribes to the signal-cli-rest-api json-rpc **WebSocket** receive stream and
persists every incoming/sync message into the SQLite history store. This is the
piece the existing Signal MCP prototypes lack: without a durable receiver,
``signal-cli receive`` drains the queue destructively and history is lost.

Runs as a daemon thread inside the MCP server process (started from ``main.py``)
so a single container serves both history reads and live capture. Reconnects
automatically; signal-cli queues messages server-side while we're disconnected
and replays them on reconnect.
"""

import json
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any, Optional

import websocket  # websocket-client

import signal_backend
from db import DB_PATH, get_conn, init_db, upsert_chat, upsert_message

log = logging.getLogger("signal-mcp.receiver")


def _ts_to_iso(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).isoformat()


def _first_attachment(attachments: Optional[list]) -> tuple[Optional[str], Optional[str]]:
    """Return (media_type, attachment_id) of the first attachment, if any."""
    if attachments:
        a = attachments[0]
        return a.get("contentType"), a.get("id")
    return None, None


def parse_envelope(obj: dict, own_number: str) -> list[dict]:
    """Turn one signal-cli JSON-RPC envelope into 0..n message rows.

    Handles two kinds of content:
      * ``dataMessage``          — an incoming message (is_from_me = False)
      * ``syncMessage.sentMessage`` — a message we sent from another device
                                       (is_from_me = True)
    Everything else (receipts, typing, read markers) is ignored.
    """
    env = obj.get("envelope") or obj
    if not isinstance(env, dict):
        return []

    source = env.get("sourceNumber") or env.get("source") or env.get("sourceUuid")
    source_name = env.get("sourceName")
    rows: list[dict] = []

    data = env.get("dataMessage")
    if isinstance(data, dict) and _has_content(data):
        ts = int(data.get("timestamp") or env.get("timestamp") or 0)
        media_type, att_id = _first_attachment(data.get("attachments"))
        group = data.get("groupInfo") or {}
        if group.get("groupId"):
            chat_jid, chat_name, is_group = f"group.{group['groupId']}", None, True
        else:
            chat_jid, chat_name, is_group = source, source_name, False
        rows.append(
            _mk_row(ts, chat_jid, chat_name, is_group, source, source_name,
                    data.get("message"), False, media_type, att_id)
        )

    sync = env.get("syncMessage") or {}
    sent = sync.get("sentMessage") if isinstance(sync, dict) else None
    if isinstance(sent, dict) and _has_content(sent):
        ts = int(sent.get("timestamp") or 0)
        media_type, att_id = _first_attachment(sent.get("attachments"))
        group = sent.get("groupInfo") or {}
        if group.get("groupId"):
            chat_jid, chat_name, is_group = f"group.{group['groupId']}", None, True
        else:
            dest = sent.get("destinationNumber") or sent.get("destination")
            chat_jid, chat_name, is_group = dest, None, False
        if chat_jid:
            rows.append(
                _mk_row(ts, chat_jid, chat_name, is_group, own_number, "Me",
                        sent.get("message"), True, media_type, att_id)
            )
    return rows


def _has_content(msg: dict) -> bool:
    return bool(msg.get("message") or msg.get("attachments"))


def _mk_row(ts, chat_jid, chat_name, is_group, sender, sender_name,
            content, is_from_me, media_type, att_id) -> dict:
    return {
        "id": str(ts),
        "chat_jid": chat_jid,
        "chat_name": chat_name,
        "is_group": is_group,
        "sender": sender,
        "sender_name": sender_name,
        "content": content,
        "timestamp": _ts_to_iso(ts) if ts else datetime.now(timezone.utc).isoformat(),
        "is_from_me": is_from_me,
        "media_type": media_type,
        "attachment_id": att_id,
    }


class Receiver:
    def __init__(self, own_number: str, db_path: str = DB_PATH):
        self.own_number = own_number
        self.db_path = db_path
        self._conn = None

    def _conn_lazy(self):
        if self._conn is None:
            self._conn = get_conn(self.db_path)
        return self._conn

    def store(self, rows: list[dict]) -> None:
        if not rows:
            return
        conn = self._conn_lazy()
        new = 0
        for row in rows:
            upsert_chat(conn, row["chat_jid"], row["chat_name"],
                        row["is_group"], row["timestamp"])
            if upsert_message(conn, row):
                new += 1
        conn.commit()
        if new:
            log.info("stored %d new message(s)", new)

    def on_message(self, *args: Any) -> None:
        raw = args[-1]  # tolerate both (ws, msg) and (msg,) callback signatures
        try:
            obj = json.loads(raw)
        except (ValueError, TypeError):
            return
        try:
            for item in obj if isinstance(obj, list) else [obj]:
                if isinstance(item, dict):
                    self.store(parse_envelope(item, self.own_number))
        except Exception:  # never let a single bad frame kill the socket
            log.exception("failed to handle receive frame")

    def run_forever(self) -> None:
        init_db(self.db_path)
        url = signal_backend.receive_ws_url()
        log.info("receiver connecting to %s", url)
        while True:
            try:
                ws = websocket.WebSocketApp(
                    url,
                    on_message=self.on_message,
                    on_error=lambda _ws, e: log.warning("ws error: %s", e),
                    on_close=lambda _ws, *a: log.info("ws closed, will reconnect"),
                    on_open=lambda _ws: log.info("ws connected — receiving"),
                )
                ws.run_forever(ping_interval=30, ping_timeout=10)
            except Exception:
                log.exception("receiver loop crashed")
            time.sleep(5)  # backoff before reconnect


def start_receiver_thread(own_number: str, db_path: str = DB_PATH) -> threading.Thread:
    """Start the receive loop in a daemon thread and return it."""
    receiver = Receiver(own_number, db_path)
    t = threading.Thread(target=receiver.run_forever, name="signal-receiver", daemon=True)
    t.start()
    return t


if __name__ == "__main__":
    import os

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    number = os.getenv("SIGNAL_NUMBER", "")
    if not number:
        raise SystemExit("SIGNAL_NUMBER is required")
    Receiver(number).run_forever()
