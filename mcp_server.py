"""MCP server exposing 6 read-only PG query tools.

Runs as a third background process inside the stock-cron container
(alongside nginx + cron daemon). Listens on 127.0.0.1:8000 — public
access goes through nginx /mcp reverse proxy with SSE-friendly settings.

Auth: GitHub OAuth via fastmcp's built-in GitHubProvider. claude.ai
performs Dynamic Client Registration → user logs in to GitHub → server
issues its own JWT → claude.ai sends JWT in Authorization header.

DB: connects via MCP_DATABASE_URL (read-only `claude_mcp_ro` user).
A separate URL from the cron's admin DATABASE_URL — defense in depth.
"""
from __future__ import annotations

import base64
import collections
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone

import uvicorn
from fastmcp import FastMCP
from fastmcp.server.auth.providers.github import GitHubProvider
from sqlalchemy import create_engine, event

# Path tweak so `import mcp_tools` works whether started from /app or /app/scripts.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mcp_tools  # noqa: E402


# ── Logging ─────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(message)s")
call_log = logging.getLogger("mcp.calls")


# ── Config ──────────────────────────────────────────────────────────
DATABASE_URL = os.environ.get("MCP_DATABASE_URL")
GITHUB_CLIENT_ID = os.environ.get("GITHUB_CLIENT_ID")
GITHUB_CLIENT_SECRET = os.environ.get("GITHUB_CLIENT_SECRET")
BASE_URL = os.environ.get("MCP_BASE_URL", "https://etfedge.xyz")
HOST = os.environ.get("MCP_HOST", "127.0.0.1")
PORT = int(os.environ.get("MCP_PORT", "8000"))

if not DATABASE_URL:
    sys.exit("[mcp_server] MCP_DATABASE_URL is required")
if not GITHUB_CLIENT_ID or not GITHUB_CLIENT_SECRET:
    sys.exit("[mcp_server] GITHUB_CLIENT_ID + GITHUB_CLIENT_SECRET are required")

# Single shared engine. claude_mcp_ro PG side enforces read-only.
engine = create_engine(DATABASE_URL, pool_pre_ping=True, pool_size=4, max_overflow=4)


# Cap every query at 10 s — reduced from 30 s. Enough for all named tools
# (get_consensus_buys CTE runs ~1-3 s on this dataset) while cutting off
# pathological queries 3× faster than before.
@event.listens_for(engine, "connect")
def _set_statement_timeout(dbapi_conn, _record):
    cur = dbapi_conn.cursor()
    cur.execute("SET statement_timeout = 10000")
    cur.close()


# GitHub OAuth: claude.ai handles DCR + the auth handshake. fastmcp issues
# its own JWT after GitHub auth so per-request validation stays local.
auth = GitHubProvider(
    client_id=GITHUB_CLIENT_ID,
    client_secret=GITHUB_CLIENT_SECRET,
    # Root-level base_url so OAuth endpoints are advertised at root and
    # match where fastmcp actually mounts them (regardless of mcp.http_app
    # path). Per GitHubProvider docstring: "Use root-level URL to avoid
    # 404s during discovery when mounting under a path."
    base_url=BASE_URL,
    redirect_path="/auth/callback",
)

mcp = FastMCP("stock-research", auth=auth)


# ── Rate limiter + tool call logger (ASGI middleware) ────────────────
_RATE_WINDOW = 60    # seconds per sliding window
_RATE_LIMIT = 20     # max tool calls per window per user
_MAX_BODY = 64_000   # bytes — safeguard against oversized POST bodies


class _MCPMiddleware:
    """ASGI middleware: per-user sliding window rate limit + tool call logging.

    Rate limit: 20 calls / 60 s per GitHub user, keyed on the JWT payload
    "login" (GitHub username) or "sub" (GitHub user ID) claim. State is
    in-process — resets on container restart.

    Logging: one JSON line per tool call on mcp.calls logger, fields:
    ts (ISO8601 UTC), user, tool, ms.
    """

    def __init__(self, app) -> None:
        self._app = app
        self._windows: collections.defaultdict[str, collections.deque] = (
            collections.defaultdict(collections.deque)
        )

    async def __call__(self, scope, receive, send) -> None:
        # Only intercept HTTP POST requests (tool calls + some OAuth flows).
        # All other traffic (GET for OAuth, WebSocket, lifespan) passes through.
        if scope["type"] != "http" or scope.get("method") != "POST":
            await self._app(scope, receive, send)
            return

        user = self._user_from_scope(scope)

        # Rate-limit only the MCP tool call endpoint.
        if scope.get("path") == "/mcp":
            allowed, retry_after = self._check_rate(user)
            if not allowed:
                await self._send_429(send, retry_after)
                return

        # Buffer the request body so we can (a) peek at the tool name for
        # logging and (b) replay it to the downstream app unchanged.
        body = await self._read_body(receive)
        tool_name = self._extract_tool_name(body)

        # Replay the buffered body as a single chunk.
        replayed = False

        async def replay_receive():
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": body, "more_body": False}
            return {"type": "http.disconnect"}

        start = time.monotonic()
        await self._app(scope, replay_receive, send)
        ms = int((time.monotonic() - start) * 1000)

        if tool_name:
            call_log.info(json.dumps({
                "ts": datetime.now(timezone.utc).isoformat(),
                "user": user,
                "tool": tool_name,
                "ms": ms,
            }))

    # ── helpers ────────────────────────────────────────────────────

    @staticmethod
    async def _read_body(receive) -> bytes:
        body = b""
        more = True
        while more:
            msg = await receive()
            chunk = msg.get("body", b"")
            if len(body) + len(chunk) <= _MAX_BODY:
                body += chunk
            more = msg.get("more_body", False)
        return body

    @staticmethod
    def _extract_tool_name(body: bytes) -> str | None:
        try:
            data = json.loads(body)
            if data.get("method") == "tools/call":
                return data.get("params", {}).get("name")
        except Exception:
            pass
        return None

    @staticmethod
    def _user_from_scope(scope: dict) -> str:
        """Extract GitHub username (or user ID) from the Bearer JWT payload.

        Does NOT verify the JWT signature — fastmcp verifies it before tool
        execution. We only need to identify the user for rate limiting and
        logging; a forged payload would still be rejected by fastmcp.
        """
        headers = {k: v for k, v in scope.get("headers", [])}
        auth = headers.get(b"authorization", b"").decode("ascii", errors="ignore")
        if not auth.startswith("Bearer "):
            return "anonymous"
        token = auth[7:]
        try:
            parts = token.split(".")
            if len(parts) < 2:
                return token[:8]
            payload_b64 = parts[1]
            # Fix base64 padding.
            payload_b64 += "=" * (-len(payload_b64) % 4)
            data = json.loads(base64.urlsafe_b64decode(payload_b64))
            # Prefer readable GitHub login; fall back to numeric sub.
            return str(data.get("login") or data.get("sub") or token[:8])
        except Exception:
            return token[:8] if token else "anonymous"

    def _check_rate(self, user: str) -> tuple[bool, int]:
        """Sliding window check. Returns (allowed, retry_after_seconds)."""
        now = time.monotonic()
        dq = self._windows[user]
        cutoff = now - _RATE_WINDOW
        while dq and dq[0] < cutoff:
            dq.popleft()
        if len(dq) >= _RATE_LIMIT:
            retry_after = max(1, int(_RATE_WINDOW - (now - dq[0])) + 1)
            return False, retry_after
        dq.append(now)
        return True, 0

    @staticmethod
    async def _send_429(send, retry_after: int) -> None:
        body = json.dumps(
            {"error": "rate_limit_exceeded", "retry_after": retry_after}
        ).encode()
        await send({
            "type": "http.response.start",
            "status": 429,
            "headers": [
                [b"content-type", b"application/json"],
                [b"retry-after", str(retry_after).encode()],
            ],
        })
        await send({"type": "http.response.body", "body": body, "more_body": False})


# ── Tools ───────────────────────────────────────────────────────────
@mcp.tool()
def list_etfs() -> list[dict]:
    """List all 21 active Taiwan ETFs in the warehouse.

    Returns one row per ETF with `code`, `name`, `aum_billion_twd`,
    `etf_type`, `dividend_policy`. AUM in billions TWD.

    Example: returns 21 rows like
        {"code": "00981A", "name": "...", "aum_billion_twd": 198.5,
         "etf_type": "主動式國內股票型", "dividend_policy": "..."}
    """
    with engine.connect() as conn:
        return mcp_tools.list_etfs(conn)


@mcp.tool()
def get_etf_buy_delta(
    etf: str, start_date: str, end_date: str
) -> list[dict]:
    """ETF holdings change between two dates: which stocks were bought / sold.

    Args:
        etf: ETF code, e.g. "00981A".
        start_date: YYYY-MM-DD (inclusive). If not a trading day, snaps
            backward to the most recent trading day on or before this date.
        end_date: YYYY-MM-DD (inclusive). Same snap-back rule.

    Returns up to 50 rows ordered by absolute delta_value_yi desc:
        [{"stock_code": "2330", "stock_name": "台積電",
          "delta_shares": 1234567, "delta_value_yi": 42.9}, ...]
        delta_shares: + = bought, - = sold (in shares, not lots).
        delta_value_yi: estimated NTD value at end_date close, in 億.

    Cash markers (C_NTD/M_NTD/PFUR_NTD/RDI_NTD/DA_*) and futures
    (^[0-9]{6}F) are excluded from results.
    """
    with engine.connect() as conn:
        return mcp_tools.get_etf_buy_delta(conn, etf, start_date, end_date)


@mcp.tool()
def get_stock_history(
    etf: str, stock_code: str, days: int = 30
) -> list[dict]:
    """Time series of (date, share_count, close) for ETF×stock over N days.

    Args:
        etf: ETF code, e.g. "00981A".
        stock_code: stock code, e.g. "2330".
        days: how many most-recent trading days with shares data to return
            (default 30, capped at 365).

    Returns rows ordered by trade_date asc:
        [{"trade_date": "2026-04-01", "share_count": 12345678,
          "close": 1100.0}, ...]
    """
    with engine.connect() as conn:
        return mcp_tools.get_stock_history(conn, etf, stock_code, days)


@mcp.tool()
def get_stock_pnl(etf: str, stock_code: str) -> dict:
    """Current market value of an ETF's holding of a single stock.

    Args:
        etf: ETF code, e.g. "00981A".
        stock_code: stock code, e.g. "2330".

    Returns:
        {"share_count": <latest>, "close": <latest>,
         "market_value_yi": share_count * close / 1e8 (億 NTD),
         "close_as_of": YYYY-MM-DD, "shares_as_of": YYYY-MM-DD}

    NOTE: this is current valuation, NOT realized P&L. Real cost-basis
    P&L needs the cumulative buy series — use get_stock_history then
    compute outside, or use get_stock_history.
    """
    with engine.connect() as conn:
        return mcp_tools.get_stock_pnl(conn, etf, stock_code)


@mcp.tool()
def get_consensus_buys(
    start_date: str, end_date: str, min_etfs: int = 4
) -> list[dict]:
    """Stocks bought by ≥ min_etfs ETFs simultaneously between two dates.

    "Consensus buy" — useful for daily/weekly Threads posts about
    cross-ETF manager flow.

    Args:
        start_date: YYYY-MM-DD (inclusive, snaps backward).
        end_date: YYYY-MM-DD (inclusive, snaps backward).
        min_etfs: minimum number of ETFs that bought (default 4).

    Returns up to 30 rows ordered by total_delta_value_yi desc:
        [{"stock_code": "2330", "stock_name": "台積電",
          "etfs_buying": 5, "total_delta_value_yi": 120.5}, ...]

    Cash markers and futures excluded.
    """
    with engine.connect() as conn:
        return mcp_tools.get_consensus_buys(
            conn, start_date, end_date, min_etfs
        )




# ── Entry ───────────────────────────────────────────────────────────
def main() -> int:
    # Mount at /mcp to match the public path nginx exposes — keeps proxy
    # config trivial (no path rewrite needed). GitHub OAuth endpoints
    # (/.well-known/..., /authorize, /token, /register, /auth/callback)
    # all live under /mcp/* via the same mount, served by GitHubProvider.
    app = mcp.http_app(path="/mcp", transport="http")
    app = _MCPMiddleware(app)
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
