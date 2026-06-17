import mcp_tools


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


if __name__ == "__main__":
    test_table_safety_helpers()
    test_query_table_uses_safe_sql_parts()
