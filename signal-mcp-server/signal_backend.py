"""Thin REST client for bbernhard/signal-cli-rest-api.

All *outbound* actions (send, react, download, list contacts/groups, link)
go through this module. Inbound message history is handled separately by
``receiver.py`` + ``db.py``.

Backend API reference: https://github.com/bbernhard/signal-cli-rest-api
Endpoints used here:
  POST /v2/send                      — send a message (+ attachments)
  POST /v1/reactions/{number}        — send / remove a reaction
  GET  /v1/attachments/{id}          — download an attachment's bytes
  GET  /v1/contacts/{number}         — list contacts
  GET  /v1/groups/{number}           — list groups
  GET  /v1/qrcodelink?device_name=…  — QR PNG to link as a secondary device
  GET  /v1/accounts                  — registered/linked accounts
  GET  /v1/about                     — backend + capability info
"""

import base64
import os
from typing import Any, Optional, Tuple
from urllib.parse import quote

import requests

BACKEND_URL = os.getenv("SIGNAL_BACKEND_URL", "http://signal-backend:8080").rstrip("/")
SIGNAL_NUMBER = os.getenv("SIGNAL_NUMBER", "")
ATTACHMENTS_DIR = os.getenv("SIGNAL_ATTACHMENTS_DIR", "/data/attachments")
HTTP_TIMEOUT = float(os.getenv("SIGNAL_BACKEND_TIMEOUT", "30"))


def _enc(number: str) -> str:
    return quote(number, safe="")


def _strip_group_prefix(jid: str) -> str:
    """Our chat jids for groups are stored as ``group.<id>``. The backend also
    accepts that form as a recipient, but reactions want the bare group id."""
    return jid[len("group.") :] if jid.startswith("group.") else jid


def receive_ws_url() -> str:
    """WebSocket URL for the json-rpc receive stream."""
    scheme = "wss" if BACKEND_URL.startswith("https") else "ws"
    host = BACKEND_URL.split("://", 1)[-1]
    return f"{scheme}://{host}/v1/receive/{_enc(SIGNAL_NUMBER)}"


def send_message(
    recipient: str,
    message: str,
    base64_attachments: Optional[list[str]] = None,
) -> Tuple[bool, str]:
    """Send a text message (optionally with base64 attachments) to a phone
    number or a group jid (``group.<id>``)."""
    if not recipient:
        return False, "Recipient must be provided"
    payload: dict[str, Any] = {
        "number": SIGNAL_NUMBER,
        "recipients": [recipient],
        "message": message,
    }
    if base64_attachments:
        payload["base64_attachments"] = base64_attachments
    try:
        resp = requests.post(f"{BACKEND_URL}/v2/send", json=payload, timeout=HTTP_TIMEOUT)
    except requests.RequestException as e:
        return False, f"Request error: {e}"
    if resp.status_code in (200, 201):
        try:
            ts = resp.json().get("timestamp")
            return True, f"Sent (timestamp={ts})"
        except ValueError:
            return True, "Sent"
    return False, f"Error: HTTP {resp.status_code} - {resp.text}"


def send_reaction(
    recipient: str,
    target_author: str,
    timestamp: int,
    emoji: str,
    remove: bool = False,
) -> Tuple[bool, str]:
    """React to a message. ``recipient`` is the chat (phone number or
    ``group.<id>``); ``target_author`` is who sent the message being reacted to;
    ``timestamp`` is that message's Signal timestamp (ms)."""
    body: dict[str, Any] = {
        "reaction": emoji,
        "target_author": target_author,
        "timestamp": int(timestamp),
        "remove": remove,
    }
    if recipient.startswith("group."):
        body["group_id"] = _strip_group_prefix(recipient)
    else:
        body["recipient"] = recipient
    try:
        resp = requests.post(
            f"{BACKEND_URL}/v1/reactions/{_enc(SIGNAL_NUMBER)}",
            json=body,
            timeout=HTTP_TIMEOUT,
        )
    except requests.RequestException as e:
        return False, f"Request error: {e}"
    if resp.status_code in (200, 201, 204):
        return True, "Reaction sent"
    return False, f"Error: HTTP {resp.status_code} - {resp.text}"


def download_attachment(attachment_id: str) -> Optional[str]:
    """Download an attachment by id, save under ATTACHMENTS_DIR, return the path."""
    try:
        resp = requests.get(
            f"{BACKEND_URL}/v1/attachments/{quote(attachment_id, safe='')}",
            timeout=HTTP_TIMEOUT,
        )
    except requests.RequestException as e:
        print(f"Attachment request error: {e}")
        return None
    if resp.status_code != 200:
        print(f"Attachment download failed: HTTP {resp.status_code}")
        return None
    os.makedirs(ATTACHMENTS_DIR, exist_ok=True)
    ext = _ext_from_content_type(resp.headers.get("Content-Type", ""))
    path = os.path.join(ATTACHMENTS_DIR, f"{attachment_id}{ext}")
    with open(path, "wb") as f:
        f.write(resp.content)
    return path


def _ext_from_content_type(ct: str) -> str:
    ct = (ct or "").split(";")[0].strip().lower()
    return {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/gif": ".gif",
        "image/webp": ".webp",
        "video/mp4": ".mp4",
        "audio/ogg": ".ogg",
        "audio/mpeg": ".mp3",
        "application/pdf": ".pdf",
    }.get(ct, "")


def list_contacts() -> list[dict]:
    try:
        resp = requests.get(
            f"{BACKEND_URL}/v1/contacts/{_enc(SIGNAL_NUMBER)}", timeout=HTTP_TIMEOUT
        )
        if resp.status_code == 200:
            return resp.json()
    except (requests.RequestException, ValueError) as e:
        print(f"list_contacts error: {e}")
    return []


def list_groups() -> list[dict]:
    try:
        resp = requests.get(
            f"{BACKEND_URL}/v1/groups/{_enc(SIGNAL_NUMBER)}", timeout=HTTP_TIMEOUT
        )
        if resp.status_code == 200:
            return resp.json()
    except (requests.RequestException, ValueError) as e:
        print(f"list_groups error: {e}")
    return []


def qrcodelink_png_b64(device_name: str = "signal-mcp") -> Optional[str]:
    """Return the linking QR code as a base64-encoded PNG (data for the user to
    scan under Signal → Linked devices)."""
    try:
        resp = requests.get(
            f"{BACKEND_URL}/v1/qrcodelink",
            params={"device_name": device_name},
            timeout=60,
        )
    except requests.RequestException as e:
        print(f"qrcodelink error: {e}")
        return None
    if resp.status_code == 200:
        return base64.b64encode(resp.content).decode("ascii")
    print(f"qrcodelink failed: HTTP {resp.status_code} - {resp.text}")
    return None


def accounts() -> list[str]:
    try:
        resp = requests.get(f"{BACKEND_URL}/v1/accounts", timeout=HTTP_TIMEOUT)
        if resp.status_code == 200:
            return resp.json()
    except (requests.RequestException, ValueError) as e:
        print(f"accounts error: {e}")
    return []


def about() -> dict:
    try:
        resp = requests.get(f"{BACKEND_URL}/v1/about", timeout=HTTP_TIMEOUT)
        if resp.status_code == 200:
            return resp.json()
    except (requests.RequestException, ValueError) as e:
        print(f"about error: {e}")
    return {}
