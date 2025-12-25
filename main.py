import csv
import io
import os
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Literal, Optional

from fastapi import FastAPI, HTTPException, Depends, Query, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, HTMLResponse
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.orm import Session
import shioaji as sj

from database import get_db, init_db
from models import OrderHistory
from utils import (
    get_api_client,
    get_valid_symbols,
    get_valid_contract_codes,
    get_contract_from_symbol,
    place_entry_order,
    place_exit_order,
    LoginError,
    OrderError,
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
            raise ValueError(f"Failed to validate symbol: {e}")
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
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Order History Dashboard</title>
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }
        body {
            font-family: 'Segoe UI', system-ui, sans-serif;
            background: linear-gradient(135deg, #1a1a2e 0%, #16213e 50%, #0f3460 100%);
            min-height: 100vh;
            color: #e4e4e7;
            padding: 2rem;
        }
        .container {
            max-width: 1400px;
            margin: 0 auto;
        }
        h1 {
            font-size: 2.5rem;
            margin-bottom: 2rem;
            background: linear-gradient(90deg, #00d9ff, #00ff88);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            background-clip: text;
        }
        .auth-section {
            background: rgba(255, 255, 255, 0.05);
            border: 1px solid rgba(255, 255, 255, 0.1);
            border-radius: 12px;
            padding: 1.5rem;
            margin-bottom: 2rem;
            backdrop-filter: blur(10px);
        }
        .auth-section label {
            display: block;
            margin-bottom: 0.5rem;
            color: #a1a1aa;
            font-size: 0.875rem;
        }
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
        .auth-section input:focus {
            outline: none;
            border-color: #00d9ff;
            box-shadow: 0 0 0 3px rgba(0, 217, 255, 0.1);
        }
        button {
            padding: 0.75rem 1.5rem;
            border: none;
            border-radius: 8px;
            font-size: 1rem;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.2s;
        }
        .btn-primary {
            background: linear-gradient(135deg, #00d9ff, #00ff88);
            color: #1a1a2e;
        }
        .btn-primary:hover {
            transform: translateY(-2px);
            box-shadow: 0 4px 20px rgba(0, 217, 255, 0.3);
        }
        .btn-secondary {
            background: rgba(255, 255, 255, 0.1);
            color: #e4e4e7;
            margin-left: 0.5rem;
        }
        .btn-secondary:hover {
            background: rgba(255, 255, 255, 0.2);
        }
        .filters {
            display: flex;
            gap: 1rem;
            flex-wrap: wrap;
            margin-bottom: 2rem;
        }
        .filters select, .filters input {
            padding: 0.5rem 1rem;
            border: 1px solid rgba(255, 255, 255, 0.2);
            border-radius: 8px;
            background: rgba(0, 0, 0, 0.3);
            color: #fff;
            font-size: 0.875rem;
        }
        .filters select option {
            background: #1a1a2e;
        }
        .stats {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 1rem;
            margin-bottom: 2rem;
        }
        .stat-card {
            background: rgba(255, 255, 255, 0.05);
            border: 1px solid rgba(255, 255, 255, 0.1);
            border-radius: 12px;
            padding: 1.5rem;
            backdrop-filter: blur(10px);
        }
        .stat-card h3 {
            font-size: 0.875rem;
            color: #a1a1aa;
            margin-bottom: 0.5rem;
        }
        .stat-card .value {
            font-size: 2rem;
            font-weight: 700;
        }
        .stat-card.success .value { color: #00ff88; }
        .stat-card.failed .value { color: #ff6b6b; }
        .stat-card.total .value { color: #00d9ff; }
        table {
            width: 100%;
            border-collapse: collapse;
            background: rgba(255, 255, 255, 0.05);
            border-radius: 12px;
            overflow: hidden;
        }
        th, td {
            padding: 1rem;
            text-align: left;
            border-bottom: 1px solid rgba(255, 255, 255, 0.1);
        }
        th {
            background: rgba(0, 0, 0, 0.3);
            font-weight: 600;
            color: #a1a1aa;
            font-size: 0.875rem;
            text-transform: uppercase;
            letter-spacing: 0.05em;
        }
        tr:hover {
            background: rgba(255, 255, 255, 0.05);
        }
        .status {
            display: inline-block;
            padding: 0.25rem 0.75rem;
            border-radius: 9999px;
            font-size: 0.75rem;
            font-weight: 600;
            text-transform: uppercase;
        }
        .status-success { background: rgba(0, 255, 136, 0.2); color: #00ff88; }
        .status-failed { background: rgba(255, 107, 107, 0.2); color: #ff6b6b; }
        .status-no_action { background: rgba(255, 193, 7, 0.2); color: #ffc107; }
        .action {
            display: inline-block;
            padding: 0.25rem 0.75rem;
            border-radius: 6px;
            font-size: 0.75rem;
            font-weight: 600;
        }
        .action-long_entry { background: rgba(0, 255, 136, 0.2); color: #00ff88; }
        .action-long_exit { background: rgba(0, 217, 255, 0.2); color: #00d9ff; }
        .action-short_entry { background: rgba(255, 107, 107, 0.2); color: #ff6b6b; }
        .action-short_exit { background: rgba(255, 193, 7, 0.2); color: #ffc107; }
        .error-msg {
            color: #ff6b6b;
            font-size: 0.875rem;
            padding: 1rem;
            background: rgba(255, 107, 107, 0.1);
            border-radius: 8px;
            margin-bottom: 1rem;
            display: none;
        }
        .loading {
            text-align: center;
            padding: 3rem;
            color: #a1a1aa;
        }
        .empty {
            text-align: center;
            padding: 3rem;
            color: #a1a1aa;
        }
    </style>
</head>
<body>
    <div class="container">
        <h1>📊 Order History</h1>
        
        <div class="auth-section">
            <label for="authKey">API Authentication Key</label>
            <input type="password" id="authKey" placeholder="Enter your auth key">
            <button class="btn-primary" onclick="fetchOrders()">Load Orders</button>
            <button class="btn-secondary" onclick="exportCSV()">Export CSV</button>
        </div>
        
        <div class="error-msg" id="errorMsg"></div>
        
        <div class="filters">
            <select id="filterStatus">
                <option value="">All Status</option>
                <option value="success">Success</option>
                <option value="failed">Failed</option>
                <option value="no_action">No Action</option>
            </select>
            <select id="filterAction">
                <option value="">All Actions</option>
                <option value="long_entry">Long Entry</option>
                <option value="long_exit">Long Exit</option>
                <option value="short_entry">Short Entry</option>
                <option value="short_exit">Short Exit</option>
            </select>
            <input type="text" id="filterSymbol" placeholder="Symbol...">
            <button class="btn-secondary" onclick="fetchOrders()">Apply Filters</button>
        </div>
        
        <div class="stats" id="stats">
            <div class="stat-card total">
                <h3>Total Orders</h3>
                <div class="value" id="statTotal">-</div>
            </div>
            <div class="stat-card success">
                <h3>Successful</h3>
                <div class="value" id="statSuccess">-</div>
            </div>
            <div class="stat-card failed">
                <h3>Failed</h3>
                <div class="value" id="statFailed">-</div>
            </div>
        </div>
        
        <div id="tableContainer">
            <div class="empty">Enter your auth key and click "Load Orders" to view history</div>
        </div>
    </div>
    
    <script>
        let orders = [];
        
        async function fetchOrders() {
            const authKey = document.getElementById('authKey').value;
            if (!authKey) {
                showError('Please enter your authentication key');
                return;
            }
            
            const status = document.getElementById('filterStatus').value;
            const action = document.getElementById('filterAction').value;
            const symbol = document.getElementById('filterSymbol').value;
            
            let url = '/orders?limit=500';
            if (status) url += `&status=${status}`;
            if (action) url += `&action=${action}`;
            if (symbol) url += `&symbol=${symbol}`;
            
            document.getElementById('tableContainer').innerHTML = '<div class="loading">Loading...</div>';
            hideError();
            
            try {
                const response = await fetch(url, {
                    headers: { 'X-Auth-Key': authKey }
                });
                
                if (!response.ok) {
                    if (response.status === 401) {
                        throw new Error('Invalid authentication key');
                    }
                    throw new Error('Failed to fetch orders');
                }
                
                orders = await response.json();
                renderTable();
                updateStats();
            } catch (error) {
                showError(error.message);
                document.getElementById('tableContainer').innerHTML = '<div class="empty">Failed to load orders</div>';
            }
        }
        
        function renderTable() {
            if (orders.length === 0) {
                document.getElementById('tableContainer').innerHTML = '<div class="empty">No orders found</div>';
                return;
            }
            
            let html = `
                <table>
                    <thead>
                        <tr>
                            <th>ID</th>
                            <th>Symbol</th>
                            <th>Action</th>
                            <th>Quantity</th>
                            <th>Status</th>
                            <th>Error</th>
                            <th>Created At</th>
                        </tr>
                    </thead>
                    <tbody>
            `;
            
            for (const order of orders) {
                const date = new Date(order.created_at).toLocaleString();
                html += `
                    <tr>
                        <td>${order.id}</td>
                        <td><strong>${order.symbol}</strong></td>
                        <td><span class="action action-${order.action}">${order.action.replace('_', ' ')}</span></td>
                        <td>${order.quantity}</td>
                        <td><span class="status status-${order.status}">${order.status}</span></td>
                        <td>${order.error_message || '-'}</td>
                        <td>${date}</td>
                    </tr>
                `;
            }
            
            html += '</tbody></table>';
            document.getElementById('tableContainer').innerHTML = html;
        }
        
        function updateStats() {
            const total = orders.length;
            const success = orders.filter(o => o.status === 'success').length;
            const failed = orders.filter(o => o.status === 'failed').length;
            
            document.getElementById('statTotal').textContent = total;
            document.getElementById('statSuccess').textContent = success;
            document.getElementById('statFailed').textContent = failed;
        }
        
        function exportCSV() {
            const authKey = document.getElementById('authKey').value;
            if (!authKey) {
                showError('Please enter your authentication key');
                return;
            }
            window.open(`/orders/export?format=csv`, '_blank');
        }
        
        function showError(msg) {
            const el = document.getElementById('errorMsg');
            el.textContent = msg;
            el.style.display = 'block';
        }
        
        function hideError() {
            document.getElementById('errorMsg').style.display = 'none';
        }
        
        // Allow Enter key to submit
        document.getElementById('authKey').addEventListener('keypress', (e) => {
            if (e.key === 'Enter') fetchOrders();
        });
    </script>
</body>
</html>
    """
    return HTMLResponse(content=html_content)
