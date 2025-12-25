from contextlib import asynccontextmanager
import csv
from datetime import datetime
import io
import os
from typing import Literal, Optional

from fastapi import Depends, FastAPI, HTTPException, Header, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel, Field, model_validator
import shioaji as sj
from sqlalchemy.orm import Session

from database import get_db, init_db
from models import OrderHistory
from trading import (
    LoginError,
    OrderError,
    get_api_client,
    get_contract_from_symbol,
    get_valid_contract_codes,
    get_valid_symbols,
    place_entry_order,
    place_exit_order,
)


ACCEPT_ACTIONS = Literal["long_entry", "long_exit", "short_entry", "short_exit"]
AUTH_KEY = os.getenv("AUTH_KEY", "changeme")


async def verify_auth_key(x_auth_key: str = Header(..., alias="X-Auth-Key")):
    if x_auth_key != AUTH_KEY:
        raise HTTPException(status_code=401, detail="Invalid authentication key")
    return x_auth_key


class OrderRequest(BaseModel):
    action: ACCEPT_ACTIONS
    quantity: int = Field(..., gt=0)
    symbol: str

    @model_validator(mode="after")
    def validate_symbol(self):
        try:
            api = get_api_client()
            if self.symbol not in get_valid_symbols(api):
                raise ValueError(f"Symbol {self.symbol} is not valid")
        except LoginError as e:
            raise ValueError(f"Failed to validate symbol: {e}") from e
        return self


class OrderHistoryResponse(BaseModel):
    id: int
    symbol: str
    action: str
    quantity: int
    status: str
    order_result: Optional[str]
    error_message: Optional[str]
    created_at: datetime

    class Config:
        from_attributes = True


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    init_db()
    yield
    # Shutdown (cleanup if needed)


app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/symbols")
async def list_symbols(
    simulation: bool = Query(True, description="Use simulation mode"),
):
    """Get list of valid trading symbols (e.g., MXF, TXF futures)."""
    try:
        api = get_api_client(simulation=simulation)
        symbols = get_valid_symbols(api)
        return {"symbols": symbols, "count": len(symbols)}
    except LoginError as e:
        raise HTTPException(status_code=503, detail=str(e))


@app.get("/symbols/{symbol}")
async def get_symbol_details(
    symbol: str,
    simulation: bool = Query(True, description="Use simulation mode"),
):
    """Get detailed information about a specific symbol."""
    try:
        api = get_api_client(simulation=simulation)
        contract = get_contract_from_symbol(api, symbol)
        return {
            "symbol": contract.symbol,
            "code": contract.code,
            "name": contract.name,
            "category": contract.category,
            "exchange": str(contract.exchange),
            "delivery_month": contract.delivery_month,
            "underlying_kind": contract.underlying_kind,
            "unit": contract.unit,
            "limit_up": contract.limit_up,
            "limit_down": contract.limit_down,
            "reference": contract.reference,
        }
    except LoginError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.get("/contracts")
async def list_contracts(
    simulation: bool = Query(True, description="Use simulation mode"),
):
    """Get list of valid contract codes."""
    try:
        api = get_api_client(simulation=simulation)
        codes = get_valid_contract_codes(api)
        return {"contracts": codes, "count": len(codes)}
    except LoginError as e:
        raise HTTPException(status_code=503, detail=str(e))


@app.get("/positions")
async def list_positions(
    _: str = Depends(verify_auth_key),
    simulation: bool = Query(True, description="Use simulation mode"),
):
    """Get current futures/options positions. Ref: https://sinotrade.github.io/zh/tutor/accounting/position/"""
    try:
        api = get_api_client(simulation=simulation)
        positions = api.list_positions(api.futopt_account)
        return {
            "positions": [
                {
                    "id": p.id,
                    "code": p.code,
                    "direction": str(p.direction.value) if hasattr(p.direction, 'value') else str(p.direction),
                    "quantity": p.quantity,
                    "price": p.price,
                    "last_price": p.last_price,
                    "pnl": p.pnl,
                }
                for p in positions
            ],
            "count": len(positions),
        }
    except LoginError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/order")
async def create_order(
    order_request: OrderRequest,
    db: Session = Depends(get_db),
    simulation: bool = Query(True, description="Use simulation mode (default: True)"),
):
    order_history = OrderHistory(
        symbol=order_request.symbol,
        action=order_request.action,
        quantity=order_request.quantity,
        status="pending",
    )

    try:
        api = get_api_client(simulation=simulation)
    except LoginError as e:
        order_history.status = "failed"
        order_history.error_message = str(e)
        db.add(order_history)
        db.commit()
        raise HTTPException(status_code=503, detail=str(e))

    try:
        if order_request.action == "long_entry":
            result = place_entry_order(
                api, order_request.symbol, order_request.quantity, sj.constant.Action.Buy
            )
        elif order_request.action == "short_entry":
            result = place_entry_order(
                api, order_request.symbol, order_request.quantity, sj.constant.Action.Sell
            )
        elif order_request.action == "long_exit":
            result = place_exit_order(
                api, order_request.symbol, sj.constant.Action.Buy
            )
        elif order_request.action == "short_exit":
            result = place_exit_order(
                api, order_request.symbol, sj.constant.Action.Sell
            )
    except OrderError as e:
        order_history.status = "failed"
        order_history.error_message = str(e)
        db.add(order_history)
        db.commit()
        raise HTTPException(status_code=400, detail=str(e))

    if result is None:
        order_history.status = "no_action"
        db.add(order_history)
        db.commit()
        return {"status": "no_action", "message": "No position to exit or invalid action"}

    order_history.status = "success"
    order_history.order_result = str(result)
    db.add(order_history)
    db.commit()

    return {"status": "success", "order": str(result)}


@app.get("/orders", response_model=list[OrderHistoryResponse])
async def get_orders(
    db: Session = Depends(get_db),
    _: str = Depends(verify_auth_key),
    symbol: Optional[str] = Query(None, description="Filter by symbol"),
    action: Optional[str] = Query(None, description="Filter by action"),
    status: Optional[str] = Query(None, description="Filter by status"),
    start_date: Optional[datetime] = Query(None, description="Filter from date"),
    end_date: Optional[datetime] = Query(None, description="Filter to date"),
    limit: int = Query(100, ge=1, le=1000, description="Limit results"),
    offset: int = Query(0, ge=0, description="Offset for pagination"),
):
    query = db.query(OrderHistory)

    if symbol:
        query = query.filter(OrderHistory.symbol == symbol)
    if action:
        query = query.filter(OrderHistory.action == action)
    if status:
        query = query.filter(OrderHistory.status == status)
    if start_date:
        query = query.filter(OrderHistory.created_at >= start_date)
    if end_date:
        query = query.filter(OrderHistory.created_at <= end_date)

    orders = query.order_by(OrderHistory.created_at.desc()).offset(offset).limit(limit).all()
    return orders


@app.get("/orders/export")
async def export_orders(
    db: Session = Depends(get_db),
    _: str = Depends(verify_auth_key),
    symbol: Optional[str] = Query(None, description="Filter by symbol"),
    action: Optional[str] = Query(None, description="Filter by action"),
    status: Optional[str] = Query(None, description="Filter by status"),
    start_date: Optional[datetime] = Query(None, description="Filter from date"),
    end_date: Optional[datetime] = Query(None, description="Filter to date"),
    format: str = Query("csv", description="Export format: csv or json"),
):
    query = db.query(OrderHistory)

    if symbol:
        query = query.filter(OrderHistory.symbol == symbol)
    if action:
        query = query.filter(OrderHistory.action == action)
    if status:
        query = query.filter(OrderHistory.status == status)
    if start_date:
        query = query.filter(OrderHistory.created_at >= start_date)
    if end_date:
        query = query.filter(OrderHistory.created_at <= end_date)

    orders = query.order_by(OrderHistory.created_at.desc()).all()

    if format == "json":
        return [order.to_dict() for order in orders]

    # CSV export
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["id", "symbol", "action", "quantity", "status", "order_result", "error_message", "created_at"])

    for order in orders:
        writer.writerow([
            order.id,
            order.symbol,
            order.action,
            order.quantity,
            order.status,
            order.order_result,
            order.error_message,
            order.created_at.isoformat() if order.created_at else "",
        ])

    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=order_history.csv"},
    )


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    html_content = """
<!DOCTYPE html>
<html lang="zh-TW">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>自動交易控制台</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: 'Microsoft JhengHei', 'Segoe UI', system-ui, sans-serif;
            background: linear-gradient(135deg, #1a1a2e 0%, #16213e 50%, #0f3460 100%);
            min-height: 100vh;
            color: #e4e4e7;
            padding: 2rem;
        }
        .container { max-width: 1400px; margin: 0 auto; }
        h1 {
            font-size: 2.5rem;
            margin-bottom: 2rem;
            background: linear-gradient(90deg, #00d9ff, #00ff88);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            background-clip: text;
        }
        h2 { font-size: 1.5rem; margin-bottom: 1rem; color: #00d9ff; }
        h3 { font-size: 1.1rem; margin: 1.5rem 0 0.75rem; color: #e4e4e7; }
        .auth-section {
            background: rgba(255, 255, 255, 0.05);
            border: 1px solid rgba(255, 255, 255, 0.1);
            border-radius: 12px;
            padding: 1.5rem;
            margin-bottom: 2rem;
            backdrop-filter: blur(10px);
        }
        .auth-section label { display: block; margin-bottom: 0.5rem; color: #a1a1aa; font-size: 0.875rem; }
        .auth-section input {
            width: 300px;
            padding: 0.75rem 1rem;
            border: 1px solid rgba(255, 255, 255, 0.2);
            border-radius: 8px;
            background: rgba(0, 0, 0, 0.3);
            color: #fff;
            font-size: 1rem;
            margin-right: 1rem;
        }
        .auth-section input:focus { outline: none; border-color: #00d9ff; box-shadow: 0 0 0 3px rgba(0, 217, 255, 0.1); }
        button {
            padding: 0.75rem 1.5rem;
            border: none;
            border-radius: 8px;
            font-size: 1rem;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.2s;
        }
        .btn-primary { background: linear-gradient(135deg, #00d9ff, #00ff88); color: #1a1a2e; }
        .btn-primary:hover { transform: translateY(-2px); box-shadow: 0 4px 20px rgba(0, 217, 255, 0.3); }
        .btn-secondary { background: rgba(255, 255, 255, 0.1); color: #e4e4e7; margin-left: 0.5rem; }
        .btn-secondary:hover { background: rgba(255, 255, 255, 0.2); }
        .btn-copy { padding: 0.5rem 1rem; font-size: 0.875rem; }
        
        .tabs { display: flex; gap: 0.5rem; margin-bottom: 2rem; border-bottom: 2px solid rgba(255, 255, 255, 0.1); flex-wrap: wrap; }
        .tab {
            padding: 1rem 1.5rem;
            background: transparent;
            border: none;
            color: #a1a1aa;
            font-size: 1rem;
            font-weight: 600;
            cursor: pointer;
            position: relative;
            transition: all 0.2s;
        }
        .tab:hover { color: #e4e4e7; }
        .tab.active { color: #00d9ff; }
        .tab.active::after {
            content: '';
            position: absolute;
            bottom: -2px;
            left: 0;
            right: 0;
            height: 2px;
            background: linear-gradient(90deg, #00d9ff, #00ff88);
        }
        .tab-content { display: none; }
        .tab-content.active { display: block; }
        
        .filters { display: flex; gap: 1rem; flex-wrap: wrap; margin-bottom: 2rem; }
        .filters select, .filters input {
            padding: 0.5rem 1rem;
            border: 1px solid rgba(255, 255, 255, 0.2);
            border-radius: 8px;
            background: rgba(0, 0, 0, 0.3);
            color: #fff;
            font-size: 0.875rem;
        }
        .filters select option { background: #1a1a2e; }
        .stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 1rem; margin-bottom: 2rem; }
        .stat-card {
            background: rgba(255, 255, 255, 0.05);
            border: 1px solid rgba(255, 255, 255, 0.1);
            border-radius: 12px;
            padding: 1.5rem;
            backdrop-filter: blur(10px);
        }
        .stat-card h3 { font-size: 0.875rem; color: #a1a1aa; margin-bottom: 0.5rem; margin-top: 0; }
        .stat-card .value { font-size: 1.75rem; font-weight: 700; }
        .stat-card.success .value { color: #00ff88; }
        .stat-card.failed .value { color: #ff6b6b; }
        .stat-card.total .value { color: #00d9ff; }
        .stat-card.pnl-positive .value { color: #00ff88; }
        .stat-card.pnl-negative .value { color: #ff6b6b; }
        table { width: 100%; border-collapse: collapse; background: rgba(255, 255, 255, 0.05); border-radius: 12px; overflow: hidden; }
        th, td { padding: 1rem; text-align: left; border-bottom: 1px solid rgba(255, 255, 255, 0.1); }
        th { background: rgba(0, 0, 0, 0.3); font-weight: 600; color: #a1a1aa; font-size: 0.875rem; text-transform: uppercase; letter-spacing: 0.05em; }
        tr:hover { background: rgba(255, 255, 255, 0.05); }
        .status, .direction { display: inline-block; padding: 0.25rem 0.75rem; border-radius: 9999px; font-size: 0.75rem; font-weight: 600; text-transform: uppercase; }
        .status-success { background: rgba(0, 255, 136, 0.2); color: #00ff88; }
        .status-failed { background: rgba(255, 107, 107, 0.2); color: #ff6b6b; }
        .status-no_action { background: rgba(255, 193, 7, 0.2); color: #ffc107; }
        .action { display: inline-block; padding: 0.25rem 0.75rem; border-radius: 6px; font-size: 0.75rem; font-weight: 600; }
        .action-long_entry, .direction-buy { background: rgba(0, 255, 136, 0.2); color: #00ff88; }
        .action-long_exit { background: rgba(0, 217, 255, 0.2); color: #00d9ff; }
        .action-short_entry, .direction-sell { background: rgba(255, 107, 107, 0.2); color: #ff6b6b; }
        .action-short_exit { background: rgba(255, 193, 7, 0.2); color: #ffc107; }
        .pnl-positive { color: #00ff88; font-weight: 600; }
        .pnl-negative { color: #ff6b6b; font-weight: 600; }
        .error-msg { color: #ff6b6b; font-size: 0.875rem; padding: 1rem; background: rgba(255, 107, 107, 0.1); border-radius: 8px; margin-bottom: 1rem; display: none; }
        .loading, .empty { text-align: center; padding: 3rem; color: #a1a1aa; }
        
        /* Webhook Tab Styles */
        .info-card {
            background: rgba(255, 255, 255, 0.05);
            border: 1px solid rgba(255, 255, 255, 0.1);
            border-radius: 12px;
            padding: 1.5rem;
            margin-bottom: 1.5rem;
        }
        .code-block {
            background: rgba(0, 0, 0, 0.4);
            border: 1px solid rgba(255, 255, 255, 0.1);
            border-radius: 8px;
            padding: 1rem;
            font-family: 'Consolas', 'Monaco', monospace;
            font-size: 0.9rem;
            overflow-x: auto;
            position: relative;
            margin: 0.5rem 0;
        }
        .code-block pre { margin: 0; white-space: pre-wrap; }
        .copy-btn {
            position: absolute;
            top: 0.5rem;
            right: 0.5rem;
            padding: 0.25rem 0.5rem;
            font-size: 0.75rem;
            background: rgba(255, 255, 255, 0.1);
            border: none;
            border-radius: 4px;
            color: #a1a1aa;
            cursor: pointer;
        }
        .copy-btn:hover { background: rgba(255, 255, 255, 0.2); color: #fff; }
        .field-table { margin: 1rem 0; }
        .field-table td { padding: 0.75rem 1rem; }
        .field-table td:first-child { font-weight: 600; color: #00d9ff; white-space: nowrap; }
        .field-table td:nth-child(2) { color: #a1a1aa; }
        .badge { display: inline-block; padding: 0.2rem 0.5rem; border-radius: 4px; font-size: 0.75rem; font-weight: 600; margin-left: 0.5rem; }
        .badge-required { background: rgba(255, 107, 107, 0.2); color: #ff6b6b; }
        .badge-optional { background: rgba(255, 193, 7, 0.2); color: #ffc107; }
        .example-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 1rem; margin-top: 1rem; }
        .example-card { background: rgba(0, 0, 0, 0.2); border-radius: 8px; padding: 1rem; }
        .example-card h4 { color: #00ff88; margin-bottom: 0.5rem; font-size: 0.9rem; }
        .alert { padding: 1rem; border-radius: 8px; margin: 1rem 0; }
        .alert-info { background: rgba(0, 217, 255, 0.1); border: 1px solid rgba(0, 217, 255, 0.3); }
        .alert-warning { background: rgba(255, 193, 7, 0.1); border: 1px solid rgba(255, 193, 7, 0.3); }
    </style>
</head>
<body>
    <div class="container">
        <h1>📊 自動交易控制台</h1>
        
        <div class="auth-section">
            <label for="authKey">API 驗證金鑰</label>
            <input type="password" id="authKey" placeholder="請輸入驗證金鑰">
            <button class="btn-primary" onclick="loadCurrentTab()">載入資料</button>
            <button class="btn-secondary" onclick="exportCSV()">匯出 CSV</button>
        </div>
        
        <div class="error-msg" id="errorMsg"></div>
        
        <div class="tabs">
            <button class="tab active" onclick="switchTab('orders')">📋 委託紀錄</button>
            <button class="tab" onclick="switchTab('positions')">💼 目前持倉</button>
            <button class="tab" onclick="switchTab('webhook')">🔗 TradingView 設定</button>
        </div>
        
        <!-- Orders Tab -->
        <div id="orders-tab" class="tab-content active">
            <div class="filters">
                <select id="filterStatus">
                    <option value="">全部狀態</option>
                    <option value="success">成功</option>
                    <option value="failed">失敗</option>
                    <option value="no_action">無動作</option>
                </select>
                <select id="filterAction">
                    <option value="">全部動作</option>
                    <option value="long_entry">做多進場</option>
                    <option value="long_exit">做多出場</option>
                    <option value="short_entry">做空進場</option>
                    <option value="short_exit">做空出場</option>
                </select>
                <input type="text" id="filterSymbol" placeholder="商品代碼...">
                <button class="btn-secondary" onclick="fetchOrders()">套用篩選</button>
            </div>
            
            <div class="stats" id="orderStats">
                <div class="stat-card total"><h3>總委託數</h3><div class="value" id="statTotal">-</div></div>
                <div class="stat-card success"><h3>成功</h3><div class="value" id="statSuccess">-</div></div>
                <div class="stat-card failed"><h3>失敗</h3><div class="value" id="statFailed">-</div></div>
            </div>
            
            <div id="ordersTable"><div class="empty">請輸入驗證金鑰並點擊「載入資料」查看委託紀錄</div></div>
        </div>
        
        <!-- Positions Tab -->
        <div id="positions-tab" class="tab-content">
            <div class="stats" id="positionStats">
                <div class="stat-card total"><h3>持倉數量</h3><div class="value" id="posCount">-</div></div>
                <div class="stat-card" id="pnlCard"><h3>未實現損益</h3><div class="value" id="totalPnl">-</div></div>
            </div>
            
            <div id="positionsTable"><div class="empty">請輸入驗證金鑰並點擊「載入資料」查看目前持倉</div></div>
        </div>
        
        <!-- Webhook Tab -->
        <div id="webhook-tab" class="tab-content">
            <div class="info-card">
                <h2>🔗 TradingView Webhook 設定指南</h2>
                <p style="color: #a1a1aa; margin-top: 0.5rem;">此後端系統專為 TradingView 警報 Webhook 設計，可自動執行期貨交易。</p>
            </div>
            
            <div class="info-card">
                <h3>📍 步驟一：Webhook 網址</h3>
                <p style="color: #a1a1aa; margin: 0.5rem 0;">將以下網址貼到 TradingView 警報的 Webhook URL 欄位：</p>
                <div class="code-block">
                    <button class="copy-btn" onclick="copyToClipboard(this, webhookUrl)">複製</button>
                    <pre id="webhookUrl"></pre>
                </div>
                <div class="alert alert-info">
                    💡 <strong>提示：</strong>請將 <code>your-domain.com</code> 替換為您的實際伺服器網址
                </div>
            </div>
            
            <div class="info-card">
                <h3>📝 步驟二：訊息格式 (Message)</h3>
                <p style="color: #a1a1aa; margin: 0.5rem 0;">在 TradingView 警報的「訊息」欄位中，使用以下 JSON 格式（搭配 Pine Script 策略）：</p>
                <div class="code-block">
                    <button class="copy-btn" onclick="copyToClipboard(this, document.getElementById('payloadTemplate').textContent)">複製</button>
                    <pre id="payloadTemplate">{
    "action": "{{strategy.order.alert_message}}",
    "quantity": {{strategy.order.contracts}},
    "symbol": "MXFR1"
}</pre>
                </div>
                <div class="alert alert-info">
                    💡 <strong>說明：</strong><code>{{strategy.order.alert_message}}</code> 會自動帶入 Pine Script 中設定的 alert_message 值
                </div>
            </div>
            
            <div class="info-card">
                <h3>📜 步驟三：Pine Script 策略範例</h3>
                <p style="color: #a1a1aa; margin: 0.5rem 0;">在您的 Pine Script 策略中，使用 <code>alert_message</code> 參數設定交易動作：</p>
                <div class="code-block">
                    <button class="copy-btn" onclick="copyToClipboard(this, document.getElementById('pinescriptExample').textContent)">複製</button>
                    <pre id="pinescriptExample">//@version=5
strategy("My Strategy", overlay=true)

// 進場條件
longCondition = ta.crossover(ta.sma(close, 14), ta.sma(close, 28))
shortCondition = ta.crossunder(ta.sma(close, 14), ta.sma(close, 28))

// 做多進場
if (longCondition)
    strategy.entry("Long", strategy.long, alert_message="long_entry")

// 做空進場  
if (shortCondition)
    strategy.entry("Short", strategy.short, alert_message="short_entry")

// 做多出場（平倉）
if (shortCondition)
    strategy.close("Long", alert_message="long_exit")

// 做空出場（平倉）
if (longCondition)
    strategy.close("Short", alert_message="short_exit")</pre>
                </div>
            </div>
            
            <div class="info-card">
                <h3>📋 欄位說明</h3>
                <table class="field-table">
                    <tr>
                        <td>action</td>
                        <td>交易動作（使用 <code>{{strategy.order.alert_message}}</code> 自動帶入）</td>
                        <td><span class="badge badge-required">必填</span></td>
                    </tr>
                    <tr>
                        <td>quantity</td>
                        <td>交易口數（可用 <code>{{strategy.order.contracts}}</code> 或固定數字）</td>
                        <td><span class="badge badge-required">必填</span></td>
                    </tr>
                    <tr>
                        <td>symbol</td>
                        <td>商品代碼（如 MXFR1、TXFR1）</td>
                        <td><span class="badge badge-required">必填</span></td>
                    </tr>
                </table>
                
                <h3 style="margin-top: 1.5rem;">🎯 action 可用值</h3>
                <table class="field-table">
                    <tr><td><span class="action action-long_entry">long_entry</span></td><td>做多進場 - 買入開倉</td></tr>
                    <tr><td><span class="action action-long_exit">long_exit</span></td><td>做多出場 - 賣出平倉</td></tr>
                    <tr><td><span class="action action-short_entry">short_entry</span></td><td>做空進場 - 賣出開倉</td></tr>
                    <tr><td><span class="action action-short_exit">short_exit</span></td><td>做空出場 - 買入平倉</td></tr>
                </table>
            </div>
            
            <div class="info-card">
                <h3>🔄 TradingView 可用佔位符</h3>
                <p style="color: #a1a1aa; margin: 0.5rem 0;">以下是 TradingView 警報中可使用的變數：</p>
                <table class="field-table">
                    <tr><td><code>{{strategy.order.alert_message}}</code></td><td>Pine Script 中 alert_message 參數的值</td></tr>
                    <tr><td><code>{{strategy.order.contracts}}</code></td><td>委託的合約數量</td></tr>
                    <tr><td><code>{{strategy.order.action}}</code></td><td>委託動作（buy/sell）</td></tr>
                    <tr><td><code>{{strategy.order.price}}</code></td><td>委託價格</td></tr>
                    <tr><td><code>{{strategy.position_size}}</code></td><td>目前持倉大小</td></tr>
                    <tr><td><code>{{ticker}}</code></td><td>商品代碼</td></tr>
                    <tr><td><code>{{time}}</code></td><td>警報觸發時間</td></tr>
                </table>
            </div>
            
            <div class="info-card">
                <h3>📌 警報訊息範例</h3>
                
                <h4 style="color: #00d9ff; margin: 1rem 0 0.5rem;">✨ 推薦：搭配 Pine Script 策略使用</h4>
                <p style="color: #a1a1aa; margin: 0.5rem 0;">action 由策略自動帶入，quantity 使用策略設定的口數：</p>
                <div class="code-block">
                    <button class="copy-btn" onclick="copyToClipboard(this, this.nextElementSibling.textContent)">複製</button>
                    <pre>{"action": "{{strategy.order.alert_message}}", "quantity": {{strategy.order.contracts}}, "symbol": "MXFR1"}</pre>
                </div>
                
                <h4 style="color: #a1a1aa; margin: 1.5rem 0 0.5rem;">📝 手動設定：固定值</h4>
                <p style="color: #a1a1aa; margin: 0.5rem 0;">適用於簡單警報或指標觸發：</p>
                <div class="example-grid">
                    <div class="example-card">
                        <h4>🟢 小台做多進場 1 口</h4>
                        <div class="code-block">
                            <button class="copy-btn" onclick="copyToClipboard(this, this.nextElementSibling.textContent)">複製</button>
                            <pre>{"action": "long_entry", "quantity": 1, "symbol": "MXFR1"}</pre>
                        </div>
                    </div>
                    <div class="example-card">
                        <h4>🔵 小台做多出場</h4>
                        <div class="code-block">
                            <button class="copy-btn" onclick="copyToClipboard(this, this.nextElementSibling.textContent)">複製</button>
                            <pre>{"action": "long_exit", "quantity": 1, "symbol": "MXFR1"}</pre>
                        </div>
                    </div>
                    <div class="example-card">
                        <h4>🔴 大台做空進場 2 口</h4>
                        <div class="code-block">
                            <button class="copy-btn" onclick="copyToClipboard(this, this.nextElementSibling.textContent)">複製</button>
                            <pre>{"action": "short_entry", "quantity": 2, "symbol": "TXFR1"}</pre>
                        </div>
                    </div>
                    <div class="example-card">
                        <h4>🟡 大台做空出場</h4>
                        <div class="code-block">
                            <button class="copy-btn" onclick="copyToClipboard(this, this.nextElementSibling.textContent)">複製</button>
                            <pre>{"action": "short_exit", "quantity": 2, "symbol": "TXFR1"}</pre>
                        </div>
                    </div>
                </div>
            </div>
            
            <div class="info-card">
                <h3>⚠️ 注意事項</h3>
                <div class="alert alert-warning">
                    <ul style="margin: 0; padding-left: 1.5rem; line-height: 1.8;">
                        <li>預設為<strong>模擬模式</strong>，如需實盤交易請在網址加上 <code>?simulation=false</code></li>
                        <li>商品代碼請使用正確的期貨代碼（可在「委託紀錄」查看歷史使用的代碼）</li>
                        <li>確保 JSON 格式正確，欄位名稱需使用<strong>雙引號</strong></li>
                        <li>quantity 必須為正整數</li>
                    </ul>
                </div>
            </div>
            
            <div class="info-card">
                <h3>🔧 進階設定：實盤交易</h3>
                <p style="color: #a1a1aa; margin: 0.5rem 0;">如需進行實盤交易，請使用以下網址：</p>
                <div class="code-block">
                    <button class="copy-btn" onclick="copyToClipboard(this, webhookUrlReal)">複製</button>
                    <pre id="webhookUrlReal"></pre>
                </div>
                <div class="alert alert-warning">
                    ⚠️ <strong>警告：</strong>實盤模式將執行真實交易，請謹慎使用！
                </div>
            </div>
        </div>
    </div>
    
    <script>
        let orders = [];
        let positions = [];
        let currentTab = 'orders';
        const baseUrl = window.location.origin;
        const webhookUrl = baseUrl + '/order';
        const webhookUrlReal = baseUrl + '/order?simulation=false';
        
        document.getElementById('webhookUrl').textContent = webhookUrl;
        document.getElementById('webhookUrlReal').textContent = webhookUrlReal;
        
        function switchTab(tab) {
            currentTab = tab;
            document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
            document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
            document.querySelector(`.tab[onclick="switchTab('${tab}')"]`).classList.add('active');
            document.getElementById(`${tab}-tab`).classList.add('active');
        }
        
        function loadCurrentTab() {
            if (currentTab === 'orders') fetchOrders();
            else if (currentTab === 'positions') fetchPositions();
        }
        
        async function fetchOrders() {
            const authKey = document.getElementById('authKey').value;
            if (!authKey) { showError('請輸入驗證金鑰'); return; }
            
            const status = document.getElementById('filterStatus').value;
            const action = document.getElementById('filterAction').value;
            const symbol = document.getElementById('filterSymbol').value;
            
            let url = '/orders?limit=500';
            if (status) url += `&status=${status}`;
            if (action) url += `&action=${action}`;
            if (symbol) url += `&symbol=${symbol}`;
            
            document.getElementById('ordersTable').innerHTML = '<div class="loading">載入中...</div>';
            hideError();
            
            try {
                const response = await fetch(url, { headers: { 'X-Auth-Key': authKey } });
                if (!response.ok) throw new Error(response.status === 401 ? '驗證金鑰無效' : '載入失敗');
                orders = await response.json();
                renderOrdersTable();
                updateOrderStats();
            } catch (error) {
                showError(error.message);
                document.getElementById('ordersTable').innerHTML = '<div class="empty">載入失敗</div>';
            }
        }
        
        async function fetchPositions() {
            const authKey = document.getElementById('authKey').value;
            if (!authKey) { showError('請輸入驗證金鑰'); return; }
            
            document.getElementById('positionsTable').innerHTML = '<div class="loading">載入中...</div>';
            hideError();
            
            try {
                const response = await fetch('/positions', { headers: { 'X-Auth-Key': authKey } });
                if (!response.ok) throw new Error(response.status === 401 ? '驗證金鑰無效' : '載入失敗');
                const data = await response.json();
                positions = data.positions;
                renderPositionsTable();
                updatePositionStats();
            } catch (error) {
                showError(error.message);
                document.getElementById('positionsTable').innerHTML = '<div class="empty">載入失敗</div>';
            }
        }
        
        const actionLabels = { long_entry: '做多進場', long_exit: '做多出場', short_entry: '做空進場', short_exit: '做空出場' };
        const statusLabels = { success: '成功', failed: '失敗', no_action: '無動作' };
        const dirLabels = { buy: '買', sell: '賣', Buy: '買', Sell: '賣' };
        
        function renderOrdersTable() {
            if (orders.length === 0) {
                document.getElementById('ordersTable').innerHTML = '<div class="empty">無委託紀錄</div>';
                return;
            }
            let html = `<table><thead><tr><th>ID</th><th>商品</th><th>動作</th><th>口數</th><th>狀態</th><th>錯誤訊息</th><th>時間</th></tr></thead><tbody>`;
            for (const order of orders) {
                const date = new Date(order.created_at).toLocaleString('zh-TW');
                html += `<tr><td>${order.id}</td><td><strong>${order.symbol}</strong></td><td><span class="action action-${order.action}">${actionLabels[order.action] || order.action}</span></td><td>${order.quantity}</td><td><span class="status status-${order.status}">${statusLabels[order.status] || order.status}</span></td><td>${order.error_message || '-'}</td><td>${date}</td></tr>`;
            }
            html += '</tbody></table>';
            document.getElementById('ordersTable').innerHTML = html;
        }
        
        function renderPositionsTable() {
            if (positions.length === 0) {
                document.getElementById('positionsTable').innerHTML = '<div class="empty">目前無持倉</div>';
                return;
            }
            let html = `<table><thead><tr><th>商品代碼</th><th>方向</th><th>口數</th><th>均價</th><th>現價</th><th>損益</th></tr></thead><tbody>`;
            for (const pos of positions) {
                const pnlClass = pos.pnl >= 0 ? 'pnl-positive' : 'pnl-negative';
                const dirClass = pos.direction.toLowerCase() === 'buy' ? 'direction-buy' : 'direction-sell';
                html += `<tr><td><strong>${pos.code}</strong></td><td><span class="direction ${dirClass}">${dirLabels[pos.direction] || pos.direction}</span></td><td>${pos.quantity}</td><td>${pos.price.toLocaleString()}</td><td>${pos.last_price.toLocaleString()}</td><td class="${pnlClass}">${pos.pnl >= 0 ? '+' : ''}${pos.pnl.toLocaleString()}</td></tr>`;
            }
            html += '</tbody></table>';
            document.getElementById('positionsTable').innerHTML = html;
        }
        
        function updateOrderStats() {
            document.getElementById('statTotal').textContent = orders.length;
            document.getElementById('statSuccess').textContent = orders.filter(o => o.status === 'success').length;
            document.getElementById('statFailed').textContent = orders.filter(o => o.status === 'failed').length;
        }
        
        function updatePositionStats() {
            const totalPnl = positions.reduce((sum, p) => sum + p.pnl, 0);
            document.getElementById('posCount').textContent = positions.length;
            document.getElementById('totalPnl').textContent = (totalPnl >= 0 ? '+' : '') + totalPnl.toLocaleString();
            const pnlCard = document.getElementById('pnlCard');
            pnlCard.className = 'stat-card ' + (totalPnl >= 0 ? 'pnl-positive' : 'pnl-negative');
        }
        
        function exportCSV() {
            const authKey = document.getElementById('authKey').value;
            if (!authKey) { showError('請輸入驗證金鑰'); return; }
            window.open('/orders/export?format=csv', '_blank');
        }
        
        function copyToClipboard(btn, text) {
            navigator.clipboard.writeText(text).then(() => {
                const original = btn.textContent;
                btn.textContent = '已複製！';
                btn.style.color = '#00ff88';
                setTimeout(() => { btn.textContent = original; btn.style.color = ''; }, 2000);
            });
        }
        
        function showError(msg) { const el = document.getElementById('errorMsg'); el.textContent = msg; el.style.display = 'block'; }
        function hideError() { document.getElementById('errorMsg').style.display = 'none'; }
        
        document.getElementById('authKey').addEventListener('keypress', (e) => { if (e.key === 'Enter') loadCurrentTab(); });
    </script>
</body>
</html>
    """
    return HTMLResponse(content=html_content)
