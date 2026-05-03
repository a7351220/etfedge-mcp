# etfedge-mcp — public MCP server for etfedge.xyz
#
# Read-only Postgres tools (5 endpoints) + GitHub OAuth + per-user rate limit
# + daily quota. Stateless container; usage state persisted to volume.
FROM python:3.13-slim

RUN pip install --no-cache-dir uv

WORKDIR /app

COPY pyproject.toml ./
COPY mcp_server.py mcp_tools.py ./

# Install runtime deps directly (avoid editable install — flat layout has no package).
RUN pip install --no-cache-dir \
    "fastmcp>=2.0" "uvicorn[standard]>=0.30" "sqlalchemy>=2.0" "psycopg[binary]>=3.2"

ENV MCP_USAGE_FILE=/app/state/mcp_usage.json
RUN mkdir -p /app/state

EXPOSE 8000

CMD ["python3", "mcp_server.py"]
