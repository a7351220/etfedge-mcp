# etfedge-mcp

MCP server for [etfedge.xyz](https://etfedge.xyz) — 台灣主動 ETF 研究資料庫的 read-only 查詢介面，5 個具名工具。

## Tools

| Tool | Description |
|------|-------------|
| `list_etfs` | 列出所有主動 ETF（代號、名稱、AUM、最新持股數） |
| `get_etf_buy_delta` | 取得某 ETF 今日加減碼股票（張數變化 + 市值） |
| `get_stock_history` | 取得某 ETF 某股票的歷史持股張數 |
| `get_stock_pnl` | 取得某 ETF 某股票的累計損益 |
| `get_consensus_buys` | 跨 ETF 共識加碼股票（≥3 家 ETF 同時加碼） |

## Auth

GitHub OAuth via [fastmcp](https://github.com/jlowin/fastmcp). claude.ai 連線時會引導 GitHub 登入。

## Setup

```bash
pip install -e .
cp .env.example .env
# 填入 .env 後：
python mcp_server.py
```

## Environment Variables

See `.env.example`.

## Rate Limiting

每個 GitHub 使用者 20 calls / 60 秒 sliding window。
