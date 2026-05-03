# etfedge-mcp — public MCP server for etfedge.xyz
#
# Read-only Postgres tools (5 endpoints) + GitHub OAuth + per-user rate limit
# + daily quota. Stateless container; usage state persisted to volume.
FROM python:3.13-slim

RUN pip install --no-cache-dir uv

WORKDIR /app

COPY pyproject.toml ./
COPY mcp_server.py mcp_tools.py ./

RUN pip install --no-cache-dir -e .

ENV MCP_USAGE_FILE=/app/state/mcp_usage.json
RUN mkdir -p /app/state

EXPOSE 8000

CMD ["python3", "mcp_server.py"]
