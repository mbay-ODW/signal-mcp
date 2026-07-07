"""Signal MCP server.

Read + search + send over Signal, backed by signal-cli-rest-api (bbernhard) for
outbound actions and a background WebSocket receive loop for durable history.

Transport/auth (``_run_sse``) is adapted from the mbay-ODW whatsapp-mcp server:
Streamable-HTTP + classic SSE, a static ``MCP_API_KEY`` bearer and Authelia OIDC
introspection, plus the RFC 9728/8414 OAuth discovery endpoints Claude.ai needs.
"""

import base64
from typing import Any, Dict, List, Optional

from mcp.server.fastmcp import FastMCP, Image

import signal_backend
import signal_store

mcp = FastMCP("signal")


# --------------------------------------------------------------------------
# Read / history tools
# --------------------------------------------------------------------------
@mcp.tool()
def list_chats(
    query: Optional[str] = None,
    limit: int = 20,
    page: int = 0,
    include_last_message: bool = True,
    sort_by: str = "last_active",
) -> List[Dict[str, Any]]:
    """List Signal conversations (direct chats and groups) from history.

    Args:
        query: Optional term to filter chats by name or jid.
        limit: Max chats to return (default 20).
        page: Page number for pagination (default 0).
        include_last_message: Include each chat's last message (default True).
        sort_by: "last_active" or "name" (default "last_active").
    """
    return signal_store.list_chats(query, limit, page, include_last_message, sort_by)


@mcp.tool()
def get_chat(chat_jid: str) -> Optional[Dict[str, Any]]:
    """Get one chat's metadata by jid (a phone number in E.164, or group.<id>)."""
    return signal_store.get_chat(chat_jid)


@mcp.tool()
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
) -> List[Dict[str, Any]]:
    """Get Signal messages from history matching the given filters.

    Args:
        after: Only messages after this ISO-8601 datetime.
        before: Only messages before this ISO-8601 datetime.
        sender: Filter by sender (phone number in E.164).
        chat_jid: Filter by chat (phone number or group.<id>).
        query: Substring to match in message content.
        limit: Max messages (default 20).
        page: Page number for pagination (default 0).
        include_context: Include surrounding messages for each match (default True).
        context_before: Messages before each match when include_context (default 1).
        context_after: Messages after each match when include_context (default 1).
    """
    return signal_store.list_messages(
        after=after, before=before, sender=sender, chat_jid=chat_jid, query=query,
        limit=limit, page=page, include_context=include_context,
        context_before=context_before, context_after=context_after,
    )


@mcp.tool()
def search_messages(query: str, limit: int = 30) -> List[Dict[str, Any]]:
    """Search the whole Signal message history for a text substring.

    Args:
        query: Text to search for in message content.
        limit: Max messages to return (default 30).
    """
    return signal_store.search_messages(query, limit)


@mcp.tool()
def get_message_context(
    message_id: str,
    chat_jid: str,
    before: int = 5,
    after: int = 5,
) -> Dict[str, Any]:
    """Get messages around a specific message.

    Args:
        message_id: The target message's id (its Signal timestamp).
        chat_jid: The chat the message belongs to (message ids are per-chat).
        before: Messages to include before the target (default 5).
        after: Messages to include after the target (default 5).
    """
    return signal_store.get_message_context(message_id, chat_jid, before, after)


@mcp.tool()
def download_media(attachment_id: str) -> Dict[str, Any]:
    """Download a message attachment by its id and return the local file path.

    The attachment_id comes from a message's `attachment_id` field.
    """
    path = signal_backend.download_attachment(attachment_id)
    if path:
        return {"success": True, "message": "Downloaded", "file_path": path}
    return {"success": False, "message": "Failed to download attachment"}


# --------------------------------------------------------------------------
# Send / interact tools
# --------------------------------------------------------------------------
@mcp.tool()
def send_message(recipient: str, message: str) -> Dict[str, Any]:
    """Send a Signal text message.

    Args:
        recipient: A phone number in E.164 (e.g. +491701234567) for a direct
                   chat, or a group jid "group.<id>" for a group.
        message: The message text.
    """
    if not recipient:
        return {"success": False, "message": "Recipient must be provided"}
    ok, status = signal_backend.send_message(recipient, message)
    return {"success": ok, "message": status}


@mcp.tool()
def send_reaction(
    recipient: str,
    target_author: str,
    target_timestamp: int,
    emoji: str,
    remove: bool = False,
) -> Dict[str, Any]:
    """React to a Signal message with an emoji.

    Args:
        recipient: The chat — phone number in E.164 or group jid "group.<id>".
        target_author: Phone number (E.164) of who sent the message being reacted to.
        target_timestamp: The target message's id/timestamp (integer ms).
        emoji: The reaction emoji, e.g. "👍".
        remove: True to remove a previously sent reaction (default False).
    """
    ok, status = signal_backend.send_reaction(
        recipient, target_author, target_timestamp, emoji, remove
    )
    return {"success": ok, "message": status}


# --------------------------------------------------------------------------
# Contacts / groups / account
# --------------------------------------------------------------------------
@mcp.tool()
def list_contacts() -> List[Dict[str, Any]]:
    """List known Signal contacts (from the backend)."""
    return signal_backend.list_contacts()


@mcp.tool()
def list_groups() -> List[Dict[str, Any]]:
    """List Signal groups the linked account is a member of (from the backend)."""
    return signal_backend.list_groups()


@mcp.tool()
def link_device(device_name: str = "signal-mcp") -> Image:
    """Return a QR code (PNG) to link this server as a secondary Signal device.

    Scan it in the Signal app under Settings → Linked devices → Link new device.
    Run this once during setup; afterwards the receive loop fills the history.
    """
    b64 = signal_backend.qrcodelink_png_b64(device_name)
    if not b64:
        raise ValueError(
            "Could not obtain a linking QR code from the backend. Is the account "
            "already linked, or is the backend unreachable?"
        )
    return Image(data=base64.b64decode(b64), format="png")


@mcp.tool()
def health() -> Dict[str, Any]:
    """Health check: backend info, linked accounts, and stored message count."""
    import sqlite3

    from db import DB_PATH, get_conn

    accounts = signal_backend.accounts()
    count = None
    try:
        conn = get_conn(DB_PATH)
        try:
            count = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        finally:
            conn.close()
    except sqlite3.Error:
        count = None
    return {
        "backend_url": signal_backend.BACKEND_URL,
        "signal_number": signal_backend.SIGNAL_NUMBER,
        "linked_accounts": accounts,
        "linked": signal_backend.SIGNAL_NUMBER in accounts if accounts else False,
        "about": signal_backend.about(),
        "messages_stored": count,
    }


# --------------------------------------------------------------------------
# HTTP transport + auth (adapted from whatsapp-mcp)
# --------------------------------------------------------------------------
def _run_sse() -> None:
    import contextlib
    import logging
    import os
    import time
    from collections.abc import AsyncIterator

    import httpx as _httpx
    import uvicorn
    from mcp.server.sse import SseServerTransport
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    from starlette.applications import Starlette
    from starlette.middleware import Middleware
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.requests import Request
    from starlette.responses import JSONResponse, Response
    from starlette.routing import Mount, Route

    _log_level = os.getenv("LOG_LEVEL", "INFO").upper()
    _level_int = getattr(logging, _log_level, logging.INFO)
    logging.basicConfig(
        level=_level_int,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        force=True,
    )
    logging.getLogger().setLevel(_level_int)
    log = logging.getLogger("signal-mcp")
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logging.getLogger(name).setLevel(_level_int)

    # Start the durable receive loop (fills the SQLite history) unless disabled.
    if os.getenv("SIGNAL_DISABLE_RECEIVER", "").lower() not in ("1", "true", "yes"):
        number = os.getenv("SIGNAL_NUMBER", "")
        if number:
            import receiver
            receiver.start_receiver_thread(number)
            log.info("receiver thread started for %s", number)
        else:
            log.warning("SIGNAL_NUMBER not set — receive loop NOT started")

    mcp_api_key = os.getenv("MCP_API_KEY", "")
    oidc_introspection_url = os.getenv("OIDC_INTROSPECTION_URL", "")
    oidc_client_id = os.getenv("OIDC_CLIENT_ID", "")
    oidc_client_secret = os.getenv("OIDC_CLIENT_SECRET", "")
    oauth_issuer = os.getenv("OAUTH_ISSUER", "")
    mcp_server_url = os.getenv("MCP_SERVER_URL", "")

    auth_configured = bool(mcp_api_key) or all(
        (oidc_introspection_url, oidc_client_id, oidc_client_secret)
    )
    if not auth_configured:
        log.warning(
            "[auth] NEITHER MCP_API_KEY NOR a complete OIDC triple configured — "
            "ALL requests pass unauthenticated."
        )

    def _auth_preview(auth: str) -> str:
        return "(none)" if not auth else auth[:20] + ("…" if len(auth) > 20 else "")

    async def _is_authorized(request: Request) -> tuple[bool, Optional[str]]:
        tag = f"{request.method} {request.url.path}"
        auth = request.headers.get("Authorization", "")
        if not mcp_api_key and not (
            oidc_introspection_url and oidc_client_id and oidc_client_secret
        ):
            return True, None
        if not auth:
            log.warning("[auth] %s — DENY: no Authorization header", tag)
            return False, "no_header"
        if mcp_api_key and auth == f"Bearer {mcp_api_key}":
            log.info("[auth] %s — OK: static MCP_API_KEY", tag)
            return True, None
        if not auth.startswith("Bearer "):
            return False, "invalid_token"
        if not (oidc_introspection_url and oidc_client_id and oidc_client_secret):
            return False, "invalid_token"
        jwt_token = auth[7:]
        try:
            async with _httpx.AsyncClient(timeout=5.0) as http:
                resp = await http.post(
                    oidc_introspection_url,
                    data={"token": jwt_token},
                    auth=(oidc_client_id, oidc_client_secret),
                )
                if resp.status_code != 200:
                    log.warning("[auth] %s — DENY: introspection HTTP %s", tag, resp.status_code)
                    return False, "invalid_token"
                if bool(resp.json().get("active")):
                    log.info("[auth] %s — OK: OIDC token active", tag)
                    return True, None
                return False, "invalid_token"
        except Exception as e:
            log.error("[auth] %s — introspection error: %s", tag, e)
            return False, "invalid_token"

    def _unauthorized(reason: Optional[str]) -> Response:
        if reason == "invalid_token":
            www = (
                'Bearer realm="signal-mcp", error="invalid_token", '
                'error_description="The access token expired or is invalid"'
            )
            return Response("Unauthorized", status_code=401,
                            headers={"WWW-Authenticate": www})
        return Response("Unauthorized", status_code=401)

    class RequestLogMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):  # type: ignore[override]
            t0 = time.monotonic()
            log.debug("→ %s %s auth=%s", request.method, request.url.path,
                      _auth_preview(request.headers.get("Authorization", "")))
            response = await call_next(request)
            log.debug("← %s %s → %d in %dms", request.method, request.url.path,
                      response.status_code, int((time.monotonic() - t0) * 1000))
            return response

    sse = SseServerTransport("/messages/")
    _server = mcp._mcp_server
    session_manager = StreamableHTTPSessionManager(app=_server, json_response=True)

    class _AlreadySent(Response):
        def __init__(self) -> None:
            super().__init__(content=b"", status_code=200)

        async def __call__(self, scope, receive, send):
            return

    async def handle_streamable_http(request: Request):
        ok, reason = await _is_authorized(request)
        if not ok:
            return _unauthorized(reason)
        await session_manager.handle_request(request.scope, request.receive, request._send)
        return _AlreadySent()

    @contextlib.asynccontextmanager
    async def lifespan(_app: "Starlette") -> AsyncIterator[None]:
        async with session_manager.run():
            log.info("StreamableHTTPSessionManager started")
            yield

    async def handle_sse(request: Request):
        ok, reason = await _is_authorized(request)
        if not ok:
            return _unauthorized(reason)
        async with sse.connect_sse(request.scope, request.receive, request._send) as streams:
            await _server.run(streams[0], streams[1], _server.create_initialization_options())
        return Response()

    async def handle_messages(scope, receive, send):
        req = Request(scope, receive=receive)
        ok, reason = await _is_authorized(req)
        if not ok:
            await _unauthorized(reason)(scope, receive, send)
            return
        await sse.handle_post_message(scope, receive, send)

    async def handle_oauth_protected_resource(request: Request):
        return JSONResponse({
            "resource": mcp_server_url or str(request.base_url).rstrip("/"),
            "authorization_servers": [oauth_issuer] if oauth_issuer else [],
            "bearer_methods_supported": ["header"],
            "scopes_supported": ["openid", "profile", "email"],
        })

    async def handle_oauth_authorization_server(request: Request):
        if oauth_issuer:
            try:
                async with _httpx.AsyncClient(timeout=5.0) as http:
                    up = await http.get(f"{oauth_issuer}/.well-known/oauth-authorization-server")
                    if up.status_code == 200:
                        return JSONResponse(up.json())
            except Exception as e:
                log.warning("[discovery] upstream fetch failed: %s", e)
        return JSONResponse({
            "issuer": oauth_issuer,
            "authorization_endpoint": f"{oauth_issuer}/api/oidc/authorization",
            "token_endpoint": f"{oauth_issuer}/api/oidc/token",
            "jwks_uri": f"{oauth_issuer}/jwks.json",
            "introspection_endpoint": f"{oauth_issuer}/api/oidc/introspection",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "scopes_supported": ["openid", "profile", "email"],
        })

    app = Starlette(
        routes=[
            Route("/.well-known/oauth-protected-resource",
                  endpoint=handle_oauth_protected_resource, methods=["GET"]),
            Route("/.well-known/oauth-authorization-server",
                  endpoint=handle_oauth_authorization_server, methods=["GET"]),
            Route("/sse", endpoint=handle_streamable_http, methods=["POST"]),
            Route("/mcp", endpoint=handle_streamable_http, methods=["POST"]),
            Route("/sse", endpoint=handle_sse, methods=["GET"]),
            Mount("/messages/", app=handle_messages),
        ],
        middleware=[Middleware(RequestLogMiddleware)],
        lifespan=lifespan,
    )

    port = int(os.getenv("PORT", "8000"))
    log.info("signal-mcp listening on :%d (LOG_LEVEL=%s)", port, _log_level)
    uvicorn.run(app, host="0.0.0.0", port=port)


if __name__ == "__main__":
    import os

    if os.getenv("MCP_TRANSPORT", "stdio") == "sse":
        _run_sse()
    else:
        mcp.run(transport="stdio")
