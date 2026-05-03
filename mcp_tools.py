"""SQL query implementations for the 6 MCP tools.

Each function takes a SQLAlchemy Connection and returns plain
list[dict] / dict — JSON-serialisable for MCP transport.

Kept separate from `mcp_server.py` so the SQL is unit-testable
without needing fastmcp / FastAPI / SSE in the test harness.
"""
from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection


# Stock_code patterns to exclude from "real stock" queries:
# C_NTD/M_NTD/PFUR_NTD/RDI_NTD = cash-style markers
# DA_* = multi-currency cash deductions (00998A)
# ^[0-9]{6}F = futures contracts
_EXCLUDE_NON_STOCK = (
    "stock_code !~ '^(C|M|PFUR|RDI)_NTD$' "
    "AND stock_code NOT LIKE 'DA\\_%' "
    "AND stock_code !~ '^[0-9]{6}F'"
)


def _row_to_dict(row: Any) -> dict:
    """SQLAlchemy Row → plain dict (handles Decimal/date → str)."""
    out = {}
    for k, v in row._mapping.items():
        if isinstance(v, (date,)):
            out[k] = v.isoformat()
        elif hasattr(v, "is_finite"):  # Decimal
            out[k] = float(v)
        else:
            out[k] = v
    return out


def list_etfs(conn: Connection) -> list[dict]:
    sql = text(
        """
        WITH latest_meta AS (
          SELECT etf_code, max(snapshot_year) AS y FROM meta GROUP BY etf_code
        )
        SELECT
          e.code,
          coalesce(m.stock_name, e.name) AS name,
          m.aum_billion_twd,
          m.etf_type,
          m.dividend_policy
        FROM etf e
        LEFT JOIN latest_meta lm ON lm.etf_code = e.code
        LEFT JOIN meta m ON m.etf_code = lm.etf_code AND m.snapshot_year = lm.y
        ORDER BY m.aum_billion_twd DESC NULLS LAST, e.code
        """
    )
    return [_row_to_dict(r) for r in conn.execute(sql)]


def _etf_label(conn: Connection, etf: str) -> dict:
    """Resolve ETF code → {code, name}.

    Prefer meta.stock_name (richer / official) but fall back to etf.name
    when the ETF is too new to have a meta snapshot yet.
    """
    sql = text(
        """
        SELECT coalesce(
          (SELECT m.stock_name FROM meta m
           WHERE m.etf_code = :etf
           ORDER BY m.snapshot_year DESC LIMIT 1),
          (SELECT e.name FROM etf e WHERE e.code = :etf)
        ) AS name
        """
    )
    row = conn.execute(sql, {"etf": etf}).first()
    return {"code": etf, "name": row[0] if row else None}


def get_etf_buy_delta(
    conn: Connection, etf: str, start_date: str, end_date: str
) -> dict:
    sql = text(
        f"""
        WITH start_snap AS (
          SELECT stock_code, share_count
          FROM shares
          WHERE etf_code = :etf
            AND trade_date = (
              SELECT max(trade_date) FROM shares
              WHERE etf_code = :etf AND trade_date <= :start_date
            )
        ),
        end_snap AS (
          SELECT stock_code, stock_name, share_count, trade_date
          FROM shares
          WHERE etf_code = :etf
            AND trade_date = (
              SELECT max(trade_date) FROM shares
              WHERE etf_code = :etf AND trade_date <= :end_date
            )
        ),
        end_close AS (
          SELECT stock_code, close
          FROM prices
          WHERE trade_date = (
            SELECT max(trade_date) FROM prices WHERE trade_date <= :end_date
          )
        )
        SELECT
          e.stock_code,
          e.stock_name,
          (e.share_count - coalesce(s.share_count, 0))::bigint AS delta_shares,
          round(
            (e.share_count - coalesce(s.share_count, 0)) * c.close / 1e8,
            2
          ) AS delta_value_yi,
          e.trade_date AS as_of
        FROM end_snap e
        LEFT JOIN start_snap s USING (stock_code)
        LEFT JOIN end_close c USING (stock_code)
        WHERE {_EXCLUDE_NON_STOCK.replace('stock_code', 'e.stock_code')}
          AND (e.share_count - coalesce(s.share_count, 0)) <> 0
        ORDER BY abs(
          (e.share_count - coalesce(s.share_count, 0)) * coalesce(c.close, 0)
        ) DESC NULLS LAST
        LIMIT 50
        """
    )
    rows = [
        _row_to_dict(r)
        for r in conn.execute(
            sql, {"etf": etf, "start_date": start_date, "end_date": end_date}
        )
    ]
    return {"etf": _etf_label(conn, etf), "deltas": rows}


def get_etf_holdings(conn: Connection, etf: str) -> dict:
    """Latest holdings snapshot — uses CMoney's official weight_pct from shares."""
    sql = text(
        f"""
        WITH latest AS (
          SELECT max(trade_date) AS d FROM shares WHERE etf_code = :etf
        )
        SELECT
          s.stock_code,
          s.stock_name,
          s.share_count::bigint AS shares,
          p.close,
          round((s.share_count * p.close / 1e8)::numeric, 4) AS value_yi,
          s.weight_pct,
          s.trade_date AS as_of
        FROM shares s
        LEFT JOIN prices p USING (stock_code, trade_date)
        WHERE s.etf_code = :etf
          AND s.trade_date = (SELECT d FROM latest)
          AND {_EXCLUDE_NON_STOCK.replace('stock_code', 's.stock_code')}
        ORDER BY s.weight_pct DESC NULLS LAST
        """
    )
    rows = [_row_to_dict(r) for r in conn.execute(sql, {"etf": etf})]
    return {
        "etf": _etf_label(conn, etf),
        "n_holdings": len(rows),
        "as_of": rows[0]["as_of"] if rows else None,
        "holdings": rows,
    }


def get_stock_history(
    conn: Connection, etf: str, stock_code: str, days: int = 30
) -> list[dict]:
    days = max(1, min(days, 365))  # cap defensively
    sql = text(
        """
        WITH date_set AS (
          SELECT DISTINCT trade_date
          FROM shares
          WHERE etf_code = :etf AND stock_code = :stock_code
          ORDER BY trade_date DESC
          LIMIT :days
        )
        SELECT
          s.trade_date,
          s.share_count::bigint,
          p.close
        FROM shares s
        LEFT JOIN prices p USING (stock_code, trade_date)
        WHERE s.etf_code = :etf
          AND s.stock_code = :stock_code
          AND s.trade_date IN (SELECT trade_date FROM date_set)
        ORDER BY s.trade_date ASC
        """
    )
    return [
        _row_to_dict(r)
        for r in conn.execute(
            sql, {"etf": etf, "stock_code": stock_code, "days": days}
        )
    ]


def get_stock_pnl(conn: Connection, etf: str, stock_code: str) -> dict:
    sql = text(
        """
        WITH latest_share AS (
          SELECT share_count, trade_date
          FROM shares
          WHERE etf_code = :etf AND stock_code = :stock_code
          ORDER BY trade_date DESC LIMIT 1
        ),
        latest_close AS (
          SELECT close, trade_date
          FROM prices
          WHERE stock_code = :stock_code
          ORDER BY trade_date DESC LIMIT 1
        )
        SELECT
          ls.share_count::bigint,
          lc.close,
          round(ls.share_count * lc.close / 1e8, 2) AS market_value_yi,
          lc.trade_date AS close_as_of,
          ls.trade_date AS shares_as_of
        FROM latest_share ls, latest_close lc
        """
    )
    row = conn.execute(sql, {"etf": etf, "stock_code": stock_code}).first()
    if row is None:
        return {
            "share_count": 0,
            "close": None,
            "market_value_yi": 0,
            "close_as_of": None,
            "shares_as_of": None,
            "note": "no data found for this etf+stock pair",
        }
    return _row_to_dict(row)


def get_consensus_buys(
    conn: Connection,
    start_date: str,
    end_date: str,
    min_etfs: int = 4,
) -> list[dict]:
    sql = text(
        f"""
        WITH per_etf_start AS (
          SELECT s.etf_code, s.stock_code, s.share_count
          FROM shares s
          WHERE s.trade_date = (
            SELECT max(trade_date) FROM shares s2
            WHERE s2.etf_code = s.etf_code AND s2.trade_date <= :start_date
          )
        ),
        per_etf_end AS (
          SELECT s.etf_code, s.stock_code, s.stock_name, s.share_count
          FROM shares s
          WHERE s.trade_date = (
            SELECT max(trade_date) FROM shares s2
            WHERE s2.etf_code = s.etf_code AND s2.trade_date <= :end_date
          )
        ),
        deltas AS (
          SELECT
            e.etf_code, e.stock_code, e.stock_name,
            e.share_count - coalesce(s.share_count, 0) AS delta_shares
          FROM per_etf_end e
          LEFT JOIN per_etf_start s USING (etf_code, stock_code)
          WHERE {_EXCLUDE_NON_STOCK.replace('stock_code', 'e.stock_code')}
        ),
        end_close AS (
          SELECT stock_code, close
          FROM prices
          WHERE trade_date = (
            SELECT max(trade_date) FROM prices WHERE trade_date <= :end_date
          )
        ),
        canonical AS (
          -- Pick the most-common stock_name per code to avoid grouping the same
          -- stock into multiple rows when ETF data sources spell names differently.
          SELECT stock_code,
                 mode() WITHIN GROUP (ORDER BY stock_name) AS stock_name
          FROM per_etf_end
          GROUP BY stock_code
        )
        SELECT
          d.stock_code,
          n.stock_name,
          count(*) FILTER (WHERE d.delta_shares > 0)::int AS etfs_buying,
          round(
            sum(d.delta_shares * coalesce(c.close, 0)) / 1e8,
            2
          ) AS total_delta_value_yi
        FROM deltas d
        LEFT JOIN end_close c USING (stock_code)
        LEFT JOIN canonical n USING (stock_code)
        GROUP BY d.stock_code, n.stock_name
        HAVING count(*) FILTER (WHERE d.delta_shares > 0) >= :min_etfs
        ORDER BY total_delta_value_yi DESC NULLS LAST
        LIMIT 30
        """
    )
    return [
        _row_to_dict(r)
        for r in conn.execute(
            sql,
            {
                "start_date": start_date,
                "end_date": end_date,
                "min_etfs": min_etfs,
            },
        )
    ]


