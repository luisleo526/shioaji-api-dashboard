# 📈 Shioaji Auto-Trading API

基於 [Shioaji](https://sinotrade.github.io/) 的自動交易 API 服務，專為 TradingView Webhook 設計，可自動執行台灣期貨交易。

## ✨ 功能特色

- 🔗 **TradingView Webhook 整合** - 直接接收 TradingView 警報，自動下單
- 📊 **Web 控制台** - 美觀的中文介面，查看委託紀錄、持倉狀態
- 🔄 **訂單狀態追蹤** - 背景自動檢查訂單成交狀態，支援手動重新查詢
- 🐳 **Docker 部署** - 一鍵部署，包含 PostgreSQL 資料庫
- 🔐 **API 金鑰驗證** - 保護敏感端點
- 📜 **商品查詢** - 查看所有可交易的期貨商品代碼

## 🏗️ 系統架構

```
┌─────────────────┐     ┌─────────────────┐     ┌─────────────────┐
│   TradingView   │────▶│   FastAPI App   │────▶│    Shioaji      │
│    Webhook      │     │   (Port 8000)   │     │   (永豐 API)    │
└─────────────────┘     └────────┬────────┘     └─────────────────┘
                                 │
                                 ▼
                        ┌─────────────────┐
                        │   PostgreSQL    │
                        │   (Port 5432)   │
                        └─────────────────┘
```

## 🚀 快速開始

### 1. 複製專案

```bash
git clone <your-repo-url>
cd s-api
```

### 2. 設定環境變數

```bash
cp example.env .env
```

編輯 `.env` 檔案：

```env
# Shioaji API 金鑰 (從永豐金證券取得)
API_KEY=your_shioaji_api_key_here
SECRET_KEY=your_shioaji_secret_key_here

# 控制台驗證金鑰 (自訂一個安全的密碼)
AUTH_KEY=your_secure_auth_key_here

# CA 憑證 (僅實盤交易需要)
CA_PATH=/app/certs/Sinopac.pfx
CA_PASSWORD=your_ca_password_here

# 資料庫設定
DATABASE_URL=postgresql://postgres:postgres@db:5432/shioaji
POSTGRES_USER=postgres
POSTGRES_PASSWORD=postgres
POSTGRES_DB=shioaji
```

### 3. 啟動服務

```bash
docker compose up -d
```

### 4. 開啟控制台

瀏覽器開啟 http://localhost:8000/dashboard

## 📖 API 端點

### 交易端點

| 端點 | 方法 | 說明 |
|------|------|------|
| `/order` | POST | 下單（TradingView Webhook 使用） |
| `/orders` | GET | 查詢委託紀錄 |
| `/orders/{id}/recheck` | POST | 手動重新查詢訂單狀態 |
| `/orders/export` | GET | 匯出委託紀錄 (CSV/JSON) |
| `/positions` | GET | 查詢目前持倉 |

### 商品資訊

| 端點 | 方法 | 說明 |
|------|------|------|
| `/symbols` | GET | 取得所有可交易商品代碼 |
| `/symbols/{symbol}` | GET | 查詢特定商品詳細資訊 |
| `/contracts` | GET | 取得所有合約資訊 |

### 其他

| 端點 | 方法 | 說明 |
|------|------|------|
| `/dashboard` | GET | Web 控制台 |
| `/health` | GET | 健康檢查 |
| `/docs` | GET | API 文件 (Swagger UI) |

## 🔗 TradingView 設定

### 1. Webhook URL

**模擬模式（測試用）：**
```
http://your-domain.com/order
```

**實盤模式：**
```
http://your-domain.com/order?simulation=false
```

### 2. Webhook 訊息格式

```json
{
    "action": "{{strategy.order.alert_message}}",
    "symbol": "MXFJ5",
    "quantity": {{strategy.order.contracts}}
}
```

### 3. Pine Script 範例

```pinescript
//@version=5
strategy("My Strategy", overlay=true)

// 你的策略邏輯...
if (買入條件)
    strategy.entry("Long", strategy.long, alert_message="long_entry")

if (賣出條件)
    strategy.close("Long", alert_message="long_exit")
```

### 4. 可用的 Action 值

| Action | 說明 |
|--------|------|
| `long_entry` | 做多進場 |
| `long_exit` | 做多出場 |
| `short_entry` | 做空進場 |
| `short_exit` | 做空出場 |

## 🔐 實盤交易設定

實盤交易需要 CA 憑證認證：

### 1. 取得 CA 憑證

從永豐金證券下載您的 `Sinopac.pfx` 憑證檔案。

### 2. 放置憑證

```bash
mkdir certs
cp /path/to/Sinopac.pfx ./certs/
```

### 3. 設定環境變數

```env
CA_PATH=/app/certs/Sinopac.pfx
CA_PASSWORD=您的憑證密碼
```

> ⚠️ **注意：** `person_id`（身分證字號）會自動從您的帳戶取得，無需手動設定。

## 📊 控制台功能

Web 控制台提供以下分頁：

### 📋 委託紀錄
- 查看所有訂單歷史
- 依狀態、動作、商品篩選
- 手動重新查詢訂單狀態
- 匯出 CSV

### 💼 目前持倉
- 查看目前期貨持倉
- 顯示未實現損益

### 📜 可用商品
- 瀏覽所有可交易的商品代碼
- 搜尋功能
- 點擊複製商品代碼

### 🔗 TradingView 設定
- Webhook URL 設定說明
- JSON Payload 格式
- Pine Script 範例

## 🛠️ 開發

### 本地開發

```bash
# 安裝依賴
pip install -r requirements.txt

# 啟動開發伺服器
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

### 專案結構

```
s-api/
├── main.py              # FastAPI 應用程式
├── trading.py           # Shioaji 交易邏輯
├── database.py          # 資料庫連線
├── models.py            # SQLAlchemy 模型
├── static/
│   └── dashboard.html   # Web 控制台
├── certs/               # CA 憑證 (gitignored)
├── docker-compose.yaml  # Docker 編排
├── Dockerfile           # Docker 映像
├── requirements.txt     # Python 依賴
└── example.env          # 環境變數範本
```

## 📝 訂單狀態說明

| 狀態 | 說明 |
|------|------|
| `pending` | 待處理 |
| `submitted` | 已送出至交易所 |
| `filled` | 完全成交 |
| `partial_filled` | 部分成交 |
| `cancelled` | 已取消 |
| `failed` | 失敗 |
| `no_action` | 無需動作（例如：無持倉可平倉） |

## ⚠️ 注意事項

1. **模擬模式優先** - 請先使用模擬模式測試，確認策略正確後再切換實盤
2. **憑證安全** - 請勿將 `.env` 和 `certs/` 資料夾提交至版本控制
3. **網路安全** - 建議使用 HTTPS 和防火牆保護 API 端點
4. **交易風險** - 自動交易有風險，請謹慎使用

## 📚 參考資源

- [Shioaji 官方文件](https://sinotrade.github.io/)
- [TradingView Webhook 文件](https://www.tradingview.com/support/solutions/43000529348)
- [FastAPI 文件](https://fastapi.tiangolo.com/)

## 📄 授權

MIT License

