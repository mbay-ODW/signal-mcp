# signal-mcp

A **read + search + send** MCP server for Signal — the equivalent of a
WhatsApp-MCP, for Signal. Backed by the actively maintained, Docker-native
[`bbernhard/signal-cli-rest-api`](https://github.com/bbernhard/signal-cli-rest-api)
for outbound actions and a durable **background WebSocket receive loop** that
persists your message history into SQLite (the piece existing Signal MCP
prototypes lack).

Deployed behind Traefik + Authelia OIDC as a Portainer stack, exposed to
Claude.ai over Streamable-HTTP.

## Architecture

```
Claude.ai ──HTTPS /mcp──► Traefik ──► signal-mcp (this repo, Python/FastMCP)
                                          │  reads/writes SQLite history (/data)
                                          │  REST for send/react/media
                                          ▼
                                     signal-backend (bbernhard/signal-cli-rest-api, MODE=json-rpc)
                                          │  WebSocket /v1/receive  ◄── receiver loop
                                          ▼
                                        Signal
```

- **signal-backend** — `bbernhard/signal-cli-rest-api:latest`, `MODE=json-rpc`
  (persistent signal-cli daemon + WebSocket receive stream). Linked to your
  Signal account as a **secondary device**. Volume holds the linking/session keys.
- **signal-mcp** — this server. Serves MCP tools over Streamable-HTTP/SSE, runs
  the receive loop in a background thread, stores history in SQLite (`/data`).

## Tools

| Tool | What it does |
|------|--------------|
| `list_chats` | List conversations (direct + groups) from history |
| `get_chat` | One chat's metadata by jid |
| `list_messages` | Messages by chat/sender/date/text, paginated, with context |
| `search_messages` | Substring search across all history |
| `get_message_context` | Messages around a target message |
| `download_media` | Fetch an attachment by id → local path |
| `send_message` | Send text to a number or `group.<id>` |
| `send_reaction` | React to a message with an emoji |
| `list_contacts` / `list_groups` | From the backend |
| `link_device` | QR code (PNG) to link as a secondary device |
| `health` | Backend info, linked accounts, stored message count |

Chat identity: a **direct** chat's jid is the contact's phone number in E.164
(`+491701234567`); a **group** jid is `group.<base64 groupId>`. A message `id`
is its Signal timestamp (unique per chat — pass `chat_jid` alongside it).

## Setup

### 1. Link your Signal account (once)
Start the backend, then get a linking QR and scan it in **Signal → Settings →
Linked devices → Link new device**:

```bash
# with the stack running:
curl -s 'http://signal-backend:8080/v1/qrcodelink?device_name=signal-mcp' -o qr.png
# or, once the MCP server is reachable, call the `link_device` tool from Claude.
```

Your real number stays active; the server gets access to your existing chats.

### 2. Configure
Copy `.env.example` → `.env` and fill in `DOMAIN`, `SIGNAL_NUMBER`, `MCP_API_KEY`,
and the OIDC client secret. Add the Authelia OIDC client from
`authelia/signal-mcp-client.yml` (then `docker restart authelia`). No Traefik
file-provider rule is needed — the app serves `/.well-known` itself and
introspects directly against `authelia.<DOMAIN>`.

### 3. Run

```bash
docker compose up -d          # DOMAIN + secrets come from .env / stack env
```

## Local development

```bash
cd signal-mcp-server
uv pip install --system -e .   # or: pip install mcp[cli] httpx requests uvicorn starlette websocket-client
# stdio (for MCP Inspector / Claude Desktop):
python main.py
# HTTP transport:
MCP_TRANSPORT=sse PORT=8000 SIGNAL_NUMBER=+49... SIGNAL_BACKEND_URL=http://localhost:8080 python main.py
```

Set `SIGNAL_DISABLE_RECEIVER=1` to run the tools without the receive loop.

## Deployment

Image is built and pushed to `ghcr.io/mbay-odw/signal-mcp-server:latest` by
`.github/workflows/docker.yml` on push to `main`. The Portainer stack pulls that
image + `bbernhard/signal-cli-rest-api`. Auth uses the Authelia OIDC client
`signal-mcp` (introspection via `client_secret_basic`) — the same pattern as the
hero-mcp / wb-mcp / whatsapp-mcp connectors. Host: `https://signal-mcp.bay-ram.de`.

## Auth model

The server enforces a bearer token on `/mcp`, `/sse`, `/messages`:
- a static `MCP_API_KEY` (for Claude Desktop / direct clients), or
- an Authelia OIDC access token, validated by introspection (for claude.ai).

`/.well-known/oauth-protected-resource` + `/.well-known/oauth-authorization-server`
are public so Claude.ai can bootstrap the OAuth flow. A 401 for an
expired/invalid token carries an RFC 6750 `WWW-Authenticate` hint so the client
runs the silent refresh flow.
