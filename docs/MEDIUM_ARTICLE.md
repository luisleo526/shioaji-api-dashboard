# 🚀 打造你的 TradingView 自動交易系統：開源 Shioaji API 整合方案

> 讓 TradingView 警報自動執行台指期交易，告別手動下單的煩惱

![Dashboard Screenshot](https://raw.githubusercontent.com/luisleo526/shioaji-api-dashboard/main/docs/images/dashboard-orders.png)

---

## 前言：為什麼需要自動交易？

如果你是一位使用 TradingView 的台灣期貨交易者，你可能有過這樣的經驗：

- 📱 警報響起時人不在電腦前，錯過最佳進場點
- ⏰ 凌晨盯盤太累，但又怕錯過交易訊號
- 🤦 手動下單時猶豫太久，價格已經跑掉
- 😤 想要程式交易，但 Shioaji API 的設定太複雜

今天，我要分享一個**完全開源**的解決方案，讓你可以：

✅ TradingView 警報 → 自動下單到永豐金證券  
✅ 支援台指期、小台期貨  
✅ Docker 一鍵部署，無需複雜設定  
✅ 美觀的中文 Web 控制台  

**GitHub 專案連結：** [https://github.com/luisleo526/shioaji-api-dashboard](https://github.com/luisleo526/shioaji-api-dashboard)

---

## 系統架構：如何運作？

```
┌─────────────────┐      ┌─────────────────┐      ┌─────────────────┐
│   TradingView   │ ───► │   FastAPI App   │ ───► │      Redis      │
│    Webhook      │      │   (Port 9879)   │      │     (Queue)     │
└─────────────────┘      └────────┬────────┘      └────────┬────────┘
                                  │                        │
                                  ▼                        ▼
                         ┌─────────────────┐      ┌─────────────────┐
                         │   PostgreSQL    │      │  Trading Worker │
                         │    (Orders)     │      │  (Single Conn)  │
                         └─────────────────┘      └────────┬────────┘
                                                           │
                                                           ▼
                                                 ┌─────────────────┐
                                                 │     Shioaji     │
                                                 │    (永豐金證券)   │
                                                 └─────────────────┘
```

### 為什麼這樣設計？

**1. Redis 訊息佇列**
- 解決 Shioaji 的 "Too Many Connections" 限制
- 所有交易請求透過單一連線執行
- 高併發情況下也能穩定運作

**2. 獨立的 Trading Worker**
- 專門負責與 Shioaji 通訊
- 自動重連機制，確保連線穩定
- 背景自動更新訂單狀態

**3. PostgreSQL 資料庫**
- 完整記錄所有交易歷史
- 支援篩選、匯出功能
- 方便後續分析交易績效

---

## 功能特色

### 📊 美觀的 Web 控制台

![TradingView Settings](https://raw.githubusercontent.com/luisleo526/shioaji-api-dashboard/main/docs/images/dashboard-webhook.png)

控制台提供四個主要分頁：

| 分頁 | 功能 |
|------|------|
| 📋 委託紀錄 | 查看所有訂單、篩選、匯出 CSV |
| 💼 目前持倉 | 即時顯示持倉與未實現損益 |
| 📜 可用商品 | 瀏覽所有可交易的期貨代碼 |
| 🔗 TradingView 設定 | 完整的 Webhook 設定指南 |

### 🔄 訂單狀態追蹤

系統會自動追蹤訂單狀態：

| 狀態 | 說明 |
|------|------|
| `pending` | 待處理 |
| `submitted` | 已送出至交易所 |
| `filled` | 完全成交 |
| `partial_filled` | 部分成交 |
| `cancelled` | 已取消 |
| `failed` | 失敗 |

如果訂單狀態卡住，還可以手動點擊「重新查詢」按鈕更新狀態。

### 🔐 安全性設計

- API 金鑰驗證保護所有敏感端點
- 支援模擬模式，安全測試策略
- CA 憑證認證實盤交易

---

## 快速開始：5 分鐘部署

### 前置需求

- Docker & Docker Compose
- 永豐金證券帳戶
- Shioaji API 金鑰（[申請連結](https://www.sinotrade.com.tw/)）

### Step 1：下載專案

```bash
git clone https://github.com/luisleo526/shioaji-api-dashboard.git
cd shioaji-api-dashboard
```

### Step 2：設定環境變數

```bash
cp example.env .env
```

編輯 `.env` 檔案：

```env
# Shioaji API 金鑰
API_KEY=your_shioaji_api_key
SECRET_KEY=your_shioaji_secret_key

# 控制台驗證金鑰（自訂一個安全的密碼）
AUTH_KEY=your_secure_password

# 支援的期貨商品
SUPPORTED_FUTURES=MXF,TXF
```

### Step 3：啟動服務

```bash
docker compose up -d
```

### Step 4：開啟控制台

瀏覽器開啟：**http://localhost:9879/dashboard**

搞定！🎉

---

## TradingView Webhook 設定

### 1. Webhook URL

**模擬模式（推薦先測試）：**
```
http://your-server:9879/order
```

**實盤模式：**
```
http://your-server:9879/order?simulation=false
```

### 2. Alert Message 格式

在 TradingView 警報的「訊息」欄位中填入：

```json
{
    "action": "{{strategy.order.alert_message}}",
    "symbol": "MXFR1",
    "quantity": {{strategy.order.contracts}}
}
```

### 3. Pine Script 策略範例

```pinescript
//@version=5
strategy("My Auto Trading Strategy", overlay=true)

// 參數設定
stopLossPct = input.float(2.0, "止損 %")
takeProfitPct = input.float(4.0, "止盈 %")

// 進場條件
fastMA = ta.sma(close, 14)
slowMA = ta.sma(close, 28)
longCondition = ta.crossover(fastMA, slowMA)
shortCondition = ta.crossunder(fastMA, slowMA)

// 做多進場
if (longCondition)
    strategy.entry("Long", strategy.long, alert_message="long_entry")

// 做空進場
if (shortCondition)
    strategy.entry("Short", strategy.short, alert_message="short_entry")

// 多單止損止盈
if (strategy.position_size > 0)
    strategy.exit("Long Exit", "Long", 
        stop=strategy.position_avg_price * (1 - stopLossPct/100),
        limit=strategy.position_avg_price * (1 + takeProfitPct/100),
        alert_message="long_exit")

// 空單止損止盈
if (strategy.position_size < 0)
    strategy.exit("Short Exit", "Short",
        stop=strategy.position_avg_price * (1 + stopLossPct/100),
        limit=strategy.position_avg_price * (1 - takeProfitPct/100),
        alert_message="short_exit")
```

### 4. Action 對照表

| Action | 說明 | 執行動作 |
|--------|------|----------|
| `long_entry` | 做多進場 | 買入開倉 |
| `long_exit` | 做多出場 | 賣出平倉 |
| `short_entry` | 做空進場 | 賣出開倉 |
| `short_exit` | 做空出場 | 買入平倉 |

---

## 實盤交易設定

⚠️ **重要提醒：請先用模擬模式測試，確認策略運作正常後再切換實盤！**

### 1. 取得 CA 憑證

從永豐金證券下載您的 `Sinopac.pfx` 憑證檔案。

### 2. 放置憑證

```bash
mkdir certs
cp /path/to/Sinopac.pfx ./certs/
```

### 3. 更新環境變數

```env
CA_PATH=/app/certs/Sinopac.pfx
CA_PASSWORD=您的憑證密碼
```

### 4. 重新啟動服務

```bash
docker compose down
docker compose up -d
```

---

## API 端點一覽

系統提供完整的 RESTful API：

### 交易相關

| 端點 | 方法 | 說明 |
|------|------|------|
| `/order` | POST | 下單（Webhook 使用） |
| `/orders` | GET | 查詢委託紀錄 |
| `/orders/{id}/recheck` | POST | 重新查詢訂單狀態 |
| `/orders/export` | GET | 匯出 CSV/JSON |
| `/positions` | GET | 查詢目前持倉 |

### 商品資訊

| 端點 | 方法 | 說明 |
|------|------|------|
| `/symbols` | GET | 所有可交易商品 |
| `/futures` | GET | 期貨商品分類 |
| `/contracts` | GET | 合約資訊 |

### 其他

| 端點 | 方法 | 說明 |
|------|------|------|
| `/dashboard` | GET | Web 控制台 |
| `/health` | GET | 健康檢查 |
| `/docs` | GET | Swagger API 文件 |

---

## 常見問題 FAQ

### Q: 訂單狀態一直顯示 "submitted"？

**A:** 點擊控制台的「🔄」按鈕重新查詢，或呼叫 API：
```bash
curl -X POST http://localhost:9879/orders/{order_id}/recheck \
  -H "X-Auth-Key: your_auth_key"
```

### Q: 出現 "Too Many Connections" 錯誤？

**A:** 這個系統已經透過 Redis 佇列解決此問題。如果仍然發生，請重啟 trading-worker：
```bash
docker compose restart trading-worker
```

### Q: 如何查看日誌？

**A:** 
```bash
# 所有服務
docker compose logs -f

# 只看交易服務
docker compose logs -f trading-worker
```

### Q: 支援哪些期貨商品？

**A:** 預設支援小台（MXF）和大台（TXF），可在 `.env` 中設定 `SUPPORTED_FUTURES` 增加其他商品。

---

## 技術棧

| 技術 | 用途 |
|------|------|
| **FastAPI** | 高效能 Python Web 框架 |
| **Shioaji** | 永豐金證券 API |
| **Redis** | 訊息佇列 |
| **PostgreSQL** | 資料庫 |
| **Docker** | 容器化部署 |
| **TradingView** | 圖表與策略 |

---

## 結語

這個專案是完全**開源免費**的，希望能幫助到想要自動化交易的台灣投資人。

如果你覺得這個專案有幫助，歡迎到 GitHub 給個 ⭐ Star！

**🔗 GitHub：** [https://github.com/luisleo526/shioaji-api-dashboard](https://github.com/luisleo526/shioaji-api-dashboard)

---

## 💼 客製化服務

如果你需要：
- 🔧 客製化功能開發
- 🏢 企業部署支援
- 📊 交易策略整合
- 🛡️ 安全性強化

歡迎聯繫：**luisleo52655@gmail.com**

---

### 📚 參考資源

- [Shioaji 官方文件](https://sinotrade.github.io/)
- [TradingView Webhook 文件](https://www.tradingview.com/support/solutions/43000529348)
- [FastAPI 文件](https://fastapi.tiangolo.com/)

---

*免責聲明：自動交易有風險，投資人應審慎評估自身風險承受能力。本專案僅供學習參考，作者不對任何交易損失負責。*

---

**Tags:** `#TradingView` `#Shioaji` `#自動交易` `#台指期` `#程式交易` `#Python` `#開源`

