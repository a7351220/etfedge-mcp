import mcp_tools
from datetime import date
from decimal import Decimal


class _Conn:
    def execute(self, sql, params=None):
        self.sql = str(sql)
        self.params = params or {}
        return []


def _raises(fn, text):
    try:
        fn()
    except ValueError as e:
        assert text in str(e)
    else:
        raise AssertionError(f"expected ValueError containing {text!r}")


def test_table_safety_helpers():
    _raises(lambda: mcp_tools._table("admin_sessions"), "not allowed")
    _raises(lambda: mcp_tools._where("prices", []), "requires")
    _raises(
        lambda: mcp_tools._where(
            "prices", [{"column": "close", "op": "gt", "value": 100}]
        ),
        "requires",
    )
    _raises(
        lambda: mcp_tools._where(
            "shares", [{"column": "stock_code", "op": "regex", "value": "2330"}]
        ),
        "unsupported",
    )

    where, params = mcp_tools._where(
        "prices", [{"column": "stock_code", "op": "eq", "value": "2330"}]
    )
    assert where == "WHERE stock_code = :v0"
    assert params == {"v0": "2330"}
    assert mcp_tools._limit(999) == 500
    assert mcp_tools._limit(0) == 100


def test_query_table_uses_safe_sql_parts():
    conn = _Conn()
    assert mcp_tools.query_table(
        conn,
        "shares",
        filters=[{"column": "etf_code", "op": "eq", "value": "00981A"}],
        sort_by="trade_date",
        sort_dir="desc",
        limit=999,
    ) == []
    assert "FROM shares WHERE etf_code = :v0 ORDER BY trade_date DESC" in conn.sql
    assert conn.params == {"v0": "00981A", "limit": 500, "offset": 0}


def test_unrealized_pnl_estimate_weighted_average():
    result = mcp_tools._estimate_unrealized_pnl(
        [
            {
                "trade_date": date(2026, 1, 1),
                "share_count": 100_000_000,
                "close": Decimal("10"),
            },
            {
                "trade_date": date(2026, 1, 2),
                "share_count": 150_000_000,
                "close": Decimal("20"),
            },
            {
                "trade_date": date(2026, 1, 3),
                "share_count": 120_000_000,
                "close": Decimal("30"),
            },
        ],
        Decimal("40"),
        date(2026, 1, 4),
    )
    assert result["current_shares"] == 120_000_000
    assert result["estimated_cost_yi"] == 16.0
    assert result["market_value_yi"] == 48.0
    assert result["unrealized_pnl_yi"] == 32.0
    assert result["unrealized_return_pct"] == 200.0
    assert result["estimate_complete"] is True


def test_unrealized_pnl_estimate_uses_latest_non_null_close():
    class Result(list):
        def first(self):
            return self[0] if self else None

    class Row:
        def __init__(self, **values):
            self._mapping = values
            self.__dict__.update(values)

        def __getitem__(self, index):
            return tuple(self._mapping.values())[index]

    class Conn:
        def execute(self, sql, params=None):
            sql = str(sql)
            if "FROM shares s" in sql:
                return Result([
                    Row(
                        trade_date=date(2026, 1, 1),
                        stock_name="Test Stock",
                        share_count=100_000_000,
                        close=Decimal("10"),
                    )
                ])
            if "SELECT close, trade_date" in sql:
                prices = [
                    Row(close=None, trade_date=date(2026, 1, 3)),
                    Row(close=Decimal("20"), trade_date=date(2026, 1, 2)),
                ]
                if "close IS NOT NULL" in sql:
                    prices = [row for row in prices if row.close is not None]
                return Result(prices)
            return Result([Row(name="Test ETF")])

    result = mcp_tools.get_stock_unrealized_pnl_estimate(Conn(), "TEST", "1234")

    assert result["latest_close"] == 20.0
    assert result["latest_close_date"] == "2026-01-02"
    assert result["unrealized_pnl_yi"] == 10.0


if __name__ == "__main__":
    test_table_safety_helpers()
    test_query_table_uses_safe_sql_parts()
    test_unrealized_pnl_estimate_weighted_average()
    test_unrealized_pnl_estimate_uses_latest_non_null_close()
