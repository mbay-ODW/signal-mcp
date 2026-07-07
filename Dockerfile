FROM python:3.12-slim
WORKDIR /app

RUN pip install --no-cache-dir uv
RUN uv pip install --system \
    "mcp[cli]>=1.6.0" \
    "httpx>=0.28.1" \
    "requests>=2.32.3" \
    "uvicorn>=0.34.0" \
    "starlette>=0.46.0" \
    "websocket-client>=1.8.0"

COPY signal-mcp-server/ .

ENV MCP_TRANSPORT=sse
ENV PORT=8000
ENV SIGNAL_BACKEND_URL=http://signal-backend:8080
ENV SIGNAL_DB_PATH=/data/signal.db
ENV SIGNAL_ATTACHMENTS_DIR=/data/attachments

EXPOSE 8000
CMD ["python", "main.py"]
