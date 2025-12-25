from contextlib import asynccontextmanager
import csv
from datetime import datetime
import io
import logging
import os
import time
from typing import Literal, Optional

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Header, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field, model_validator
import shioaji as sj
from sqlalchemy.orm import Session

from database import get_db, init_db, SessionLocal
from models import OrderHistory
from trading import (
    LoginError,
    OrderError,
    check_order_status,
    get_api_client,
    get_contract_from_symbol,
    get_valid_contract_codes,
    get_valid_symbols,
    place_entry_order,
    place_exit_order,
)

logger = logging.getLogger(__name__)


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
    order_id: Optional[str] = None
    fill_status: Optional[str] = None
    fill_quantity: Optional[int] = None
    fill_price: Optional[float] = None
    updated_at: Optional[datetime] = None

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


# Background task configuration
ORDER_STATUS_CHECK_DELAY = 2  # seconds to wait before first check
ORDER_STATUS_CHECK_INTERVAL = 5  # seconds between retry checks
ORDER_STATUS_MAX_RETRIES = 120  # max number of status checks (~10 minutes total)


def verify_order_fill(
    order_id: int,
    trade,
    simulation: bool,
):
    """
    Background task to verify order fill status.
    
    According to Shioaji docs, after placing an order, the status is 'PendingSubmit'.
    We need to call update_status to get the actual status from the exchange.
    
    Ref: https://sinotrade.github.io/zh/tutor/order/FutureOption/#_2
    """
    logger.info(f"[BG] Starting order verification for order_id={order_id}")
    
    # Wait before first check to allow order to reach exchange
    time.sleep(ORDER_STATUS_CHECK_DELAY)
    
    # Create a new database session for background task
    db = SessionLocal()
    
    try:
        api = get_api_client(simulation=simulation)
        
        for attempt in range(ORDER_STATUS_MAX_RETRIES):
            logger.debug(f"[BG] Check attempt {attempt + 1}/{ORDER_STATUS_MAX_RETRIES} for order_id={order_id}")
            
            status_info = check_order_status(api, trade)
            fill_status = status_info.get("status", "unknown")
            
            # Update database record
            order_record = db.query(OrderHistory).filter(OrderHistory.id == order_id).first()
            if order_record:
                order_record.fill_status = fill_status
                order_record.order_id = status_info.get("order_id")
                order_record.seqno = status_info.get("seqno")
                order_record.ordno = status_info.get("ordno")
                order_record.fill_quantity = status_info.get("deal_quantity", 0)  # Use deal_quantity from OrderStatus
                order_record.fill_price = status_info.get("fill_avg_price")
                order_record.cancel_quantity = status_info.get("cancel_quantity", 0)
                order_record.updated_at = datetime.utcnow()
                
                # Update main status based on fill status
                if fill_status == "Filled":
                    order_record.status = "filled"
                    db.commit()
                    logger.info(f"[BG] Order {order_id} fully filled: qty={status_info.get('deal_quantity')}, price={status_info.get('fill_avg_price')}")
                    break
                elif fill_status == "PartFilled":
                    order_record.status = "partial_filled"
                    db.commit()
                    logger.info(f"[BG] Order {order_id} partially filled: qty={status_info.get('deal_quantity')}/{status_info.get('order_quantity')}")
                    # Continue checking for more fills
                elif fill_status == "Cancelled":
                    order_record.status = "cancelled"
                    db.commit()
                    logger.info(f"[BG] Order {order_id} cancelled: cancel_qty={status_info.get('cancel_quantity')}")
                    break
                elif fill_status == "Inactive":
                    order_record.status = "cancelled"
                    db.commit()
                    logger.info(f"[BG] Order {order_id} inactive (expired/rejected)")
                    break
                elif fill_status in ("PendingSubmit", "PreSubmitted", "Submitted"):
                    order_record.status = "submitted"
                    db.commit()
                    logger.debug(f"[BG] Order {order_id} still pending: {fill_status}")
                    # Continue checking
                elif fill_status == "Failed":
                    order_record.status = "failed"
                    order_record.error_message = status_info.get("msg") or status_info.get("error", "Order failed at exchange")
                    db.commit()
                    logger.error(f"[BG] Order {order_id} failed at exchange: {status_info.get('msg')}")
                    break
                else:
                    db.commit()
                    logger.debug(f"[BG] Order {order_id} unknown status: {fill_status}")
            
            # Wait before next check
            time.sleep(ORDER_STATUS_CHECK_INTERVAL)
        
        # Final status after all retries
        if order_record and order_record.status == "submitted":
            logger.warning(f"[BG] Order {order_id} still not filled after {ORDER_STATUS_MAX_RETRIES} checks")
            
    except LoginError as e:
        logger.error(f"[BG] Failed to login for order verification: {e}")
    except Exception as e:
        logger.error(f"[BG] Error verifying order {order_id}: {e}")
    finally:
        db.close()


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
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    simulation: bool = Query(True, description="Use simulation mode (default: True)"),
):
    """
    Place a trading order. The order is submitted and a background task verifies
    the actual fill status from the exchange.
    
    According to Shioaji docs, after place_order returns, the status is 'PendingSubmit'.
    The background task calls update_status to get the actual status (Filled, Cancelled, etc.).
    
    Ref: https://sinotrade.github.io/zh/tutor/order/FutureOption/#_2
    """
    order_history = OrderHistory(
        symbol=order_request.symbol,
        action=order_request.action,
        quantity=order_request.quantity,
        status="pending",
        fill_status="PendingSubmit",
    )

    try:
        api = get_api_client(simulation=simulation)
    except LoginError as e:
        order_history.status = "failed"
        order_history.error_message = str(e)
        db.add(order_history)
        db.commit()
        raise HTTPException(status_code=503, detail=str(e))

    result = None
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
        order_history.fill_status = None
        db.add(order_history)
        db.commit()
        return {"status": "no_action", "message": "No position to exit or invalid action"}

    # Extract order info from trade result
    if hasattr(result, 'order') and result.order:
        order_history.order_id = result.order.id if hasattr(result.order, 'id') else None
        order_history.seqno = result.order.seqno if hasattr(result.order, 'seqno') else None
        order_history.ordno = result.order.ordno if hasattr(result.order, 'ordno') else None

    # Initial status is "submitted" (order accepted, pending verification)
    order_history.status = "submitted"
    order_history.order_result = str(result)
    db.add(order_history)
    db.commit()
    db.refresh(order_history)
    
    # Spawn background task to verify fill status
    background_tasks.add_task(
        verify_order_fill,
        order_id=order_history.id,
        trade=result,
        simulation=simulation,
    )

    return {
        "status": "submitted",
        "order_id": order_history.id,
        "message": "Order submitted. Fill status will be verified in background.",
        "order": str(result),
    }


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


STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


@app.get("/dashboard")
async def dashboard():
    """Serve the dashboard HTML page."""
    return FileResponse(os.path.join(STATIC_DIR, "dashboard.html"), media_type="text/html")
