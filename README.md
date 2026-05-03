# etfedge-mcp

MCP server for [etfedge.xyz](https://etfedge.xyz) — 台灣主動 ETF 研究資料庫的 read-only 查詢介面，5 個具名工具。

## 在 Claude 裡使用

### claude.ai（網頁版）

1. 開啟 [claude.ai](https://claude.ai) → 右上角頭像 → **Settings**
2. 側邊欄選 **Integrations**
3. 點 **Add integration** → 貼上 URL：
   ```
   https://etfedge.xyz/mcp
   ```
4. 儲存後，Claude 會引導你用 **GitHub 帳號登入**授權

### Claude Code（CLI）

在專案根目錄的 `.claude/settings.json`（或全域 `~/.claude/settings.json`）加入：

```json
{
  "mcpServers": {
    "etfedge": {
      "type": "http",
      "url": "https://etfedge.xyz/mcp"
    }
  }
}
```

重啟 Claude Code 後輸入 `/mcp` 確認連線。

### Claude Desktop

Claude Desktop 目前只支援本地 stdio 伺服器，需透過 `mcp-remote` 代理：

```json
{
  "mcpServers": {
    "etfedge": {
      "command": "npx",
      "args": ["mcp-remote", "https://etfedge.xyz/mcp"]
    }
  }
}
```

Config 路徑：
- macOS：`~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows：`%APPDATA%\Claude\claude_desktop_config.json`

---

## Tools

| Tool | Description |
|------|-------------|
| `list_etfs` | 列出所有主動 ETF（代號、名稱、AUM、最新持股數） |
| `get_etf_buy_delta` | 取得某 ETF 今日加減碼股票（張數變化 + 市值） |
| `get_stock_history` | 取得某 ETF 某股票的歷史持股張數 |
| `get_stock_pnl` | 取得某 ETF 某股票的累計損益 |
| `get_consensus_buys` | 跨 ETF 共識加碼股票（≥N 家 ETF 同時加碼） |

## Auth

GitHub OAuth via [fastmcp](https://github.com/jlowin/fastmcp)。連線時會引導 GitHub 登入，每個 GitHub 帳號 20 calls / 60 秒。

---

## 自架（Self-hosting）

```bash
pip install -e .
cp .env.example .env
# 填入 .env 後：
python mcp_server.py
```

See `.env.example` for required environment variables.
