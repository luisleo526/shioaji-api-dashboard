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
from sqlalchemy import text
from sqlalchemy.orm import Session
from sqlalchemy.exc import SQLAlchemyError, OperationalError

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
    logger.info(f"[BG] Starting order verification: order_id={order_id}, simulation={simulation}")
    
    # Wait before first check to allow order to reach exchange
    logger.debug(f"[BG] Waiting {ORDER_STATUS_CHECK_DELAY}s before first check...")
    time.sleep(ORDER_STATUS_CHECK_DELAY)
    
    # Database connection with retry logic
    db = None
    db_retry_count = 0
    db_max_retries = 3
    
    def get_db_session():
        """Get a new database session with retry logic."""
        nonlocal db, db_retry_count
        for retry in range(db_max_retries):
            try:
                if db is not None:
                    try:
                        db.close()
                    except Exception:
                        pass
                db = SessionLocal()
                # Test connection
                db.execute(text("SELECT 1"))
                db_retry_count = 0
                return db
            except OperationalError as e:
                logger.warning(f"[BG] DB connection failed (attempt {retry + 1}/{db_max_retries}): {e}")
                time.sleep(2 ** retry)  # Exponential backoff: 1s, 2s, 4s
        raise OperationalError("Failed to connect to database after retries", None, None)
    
    def safe_db_commit():
        """Safely commit with error handling."""
        nonlocal db
        try:
            db.commit()
            return True
        except OperationalError as e:
            logger.error(f"[BG] DB commit failed (connection error): {e}")
            try:
                db.rollback()
            except Exception:
                pass
            # Try to reconnect
            try:
                db = get_db_session()
                return False  # Caller should retry the operation
            except Exception:
                return False
        except SQLAlchemyError as e:
            logger.error(f"[BG] DB commit failed: {e}")
            try:
                db.rollback()
            except Exception:
                pass
            return False
    
    last_status = None
    order_record = None
    
    try:
        # Initialize database connection
        db = get_db_session()
        logger.debug(f"[BG] Database connection established")
        
        api = get_api_client(simulation=simulation)
        logger.info(f"[BG] API client ready, starting status checks (max {ORDER_STATUS_MAX_RETRIES} checks, {ORDER_STATUS_CHECK_INTERVAL}s interval)")
        
        for attempt in range(ORDER_STATUS_MAX_RETRIES):
            status_info = check_order_status(api, trade)
            fill_status = status_info.get("status", "unknown")
            
            # Log status change or periodic update (every 12 checks = ~1 minute)
            if fill_status != last_status:
                logger.info(f"[BG] Order {order_id} status changed: {last_status} -> {fill_status}")
                last_status = fill_status
            elif attempt % 12 == 0 and attempt > 0:
                elapsed = attempt * ORDER_STATUS_CHECK_INTERVAL
                logger.info(f"[BG] Order {order_id} still {fill_status} after {elapsed}s ({attempt}/{ORDER_STATUS_MAX_RETRIES} checks)")
            
            # Log detailed status info at debug level
            logger.debug(
                f"[BG] Check {attempt + 1}/{ORDER_STATUS_MAX_RETRIES}: "
                f"status={fill_status}, "
                f"deal_qty={status_info.get('deal_quantity', 0)}, "
                f"cancel_qty={status_info.get('cancel_quantity', 0)}, "
                f"order_qty={status_info.get('order_quantity', 0)}, "
                f"seqno={status_info.get('seqno')}, "
                f"ordno={status_info.get('ordno')}"
            )
            
            # Log deals if any
            deals = status_info.get("deals", [])
            if deals:
                for deal in deals:
                    logger.info(f"[BG] Order {order_id} DEAL: qty={deal.get('quantity')}, price={deal.get('price')}, ts={deal.get('ts')}")
            
            # Update database record with error handling
            try:
                order_record = db.query(OrderHistory).filter(OrderHistory.id == order_id).first()
            except OperationalError as e:
                logger.warning(f"[BG] DB query failed, reconnecting: {e}")
                try:
                    db = get_db_session()
                    order_record = db.query(OrderHistory).filter(OrderHistory.id == order_id).first()
                except Exception as reconnect_error:
                    logger.error(f"[BG] DB reconnect failed: {reconnect_error}")
                    time.sleep(ORDER_STATUS_CHECK_INTERVAL)
                    continue
            
            if order_record:
                order_record.fill_status = fill_status
                order_record.order_id = status_info.get("order_id")
                order_record.seqno = status_info.get("seqno")
                order_record.ordno = status_info.get("ordno")
                order_record.fill_quantity = status_info.get("deal_quantity", 0)
                order_record.fill_price = status_info.get("fill_avg_price")
                order_record.cancel_quantity = status_info.get("cancel_quantity", 0)
                order_record.updated_at = datetime.utcnow()
                
                # Update main status based on fill status
                if fill_status == "Filled":
                    order_record.status = "filled"
                    if safe_db_commit():
                        logger.info(
                            f"[BG] ✓ Order {order_id} FILLED: "
                            f"qty={status_info.get('deal_quantity')}, "
                            f"avg_price={status_info.get('fill_avg_price')}, "
                            f"deals={len(deals)}"
                        )
                        break
                elif fill_status == "PartFilled":
                    order_record.status = "partial_filled"
                    if safe_db_commit():
                        logger.info(
                            f"[BG] ~ Order {order_id} PARTIAL: "
                            f"filled={status_info.get('deal_quantity')}/{status_info.get('order_quantity')}, "
                            f"avg_price={status_info.get('fill_avg_price')}"
                        )
                    # Continue checking for more fills
                elif fill_status == "Cancelled":
                    order_record.status = "cancelled"
                    if safe_db_commit():
                        logger.info(
                            f"[BG] ✗ Order {order_id} CANCELLED: "
                            f"cancel_qty={status_info.get('cancel_quantity')}, "
                            f"msg={status_info.get('msg')}"
                        )
                        break
                elif fill_status == "Inactive":
                    order_record.status = "cancelled"
                    if safe_db_commit():
                        logger.info(f"[BG] ✗ Order {order_id} INACTIVE (expired/rejected): msg={status_info.get('msg')}")
                        break
                elif fill_status in ("PendingSubmit", "PreSubmitted", "Submitted"):
                    order_record.status = "submitted"
                    safe_db_commit()
                    # Already logged above
                elif fill_status == "Failed":
                    order_record.status = "failed"
                    error_msg = status_info.get("msg") or status_info.get("error", "Order failed at exchange")
                    order_record.error_message = error_msg
                    if safe_db_commit():
                        logger.error(f"[BG] ✗ Order {order_id} FAILED: {error_msg}, status_code={status_info.get('status_code')}")
                        break
                elif fill_status == "error":
                    logger.error(f"[BG] Error checking order {order_id}: {status_info.get('error')}")
                    safe_db_commit()
                else:
                    safe_db_commit()
                    logger.warning(f"[BG] Order {order_id} unknown status: {fill_status}")
            else:
                logger.error(f"[BG] Order record not found in database: order_id={order_id}")
            
            # Wait before next check
            time.sleep(ORDER_STATUS_CHECK_INTERVAL)
        
        # Final status after all retries
        if order_record and order_record.status == "submitted":
            total_time = ORDER_STATUS_CHECK_DELAY + (ORDER_STATUS_MAX_RETRIES * ORDER_STATUS_CHECK_INTERVAL)
            logger.warning(
                f"[BG] ⚠ Order {order_id} timeout: still '{fill_status}' after {total_time}s "
                f"({ORDER_STATUS_MAX_RETRIES} checks). Last status_code={status_info.get('status_code')}"
            )
            
    except LoginError as e:
        logger.error(f"[BG] Failed to login for order verification: {e}")
    except OperationalError as e:
        logger.error(f"[BG] Database connection error for order {order_id}: {e}")
    except SQLAlchemyError as e:
        logger.error(f"[BG] Database error for order {order_id}: {e}")
    except Exception as e:
        logger.exception(f"[BG] Error verifying order {order_id}: {e}")
    finally:
        if db is not None:
            try:
                db.close()
            except Exception as e:
                logger.debug(f"[BG] Error closing DB session: {e}")
        logger.debug(f"[BG] Order {order_id} verification completed")


@app.get("/futures")
async def list_futures_products(
    simulation: bool = Query(True, description="Use simulation mode"),
):
    """
    Get all available futures products (first level).
    
    Returns a list of product codes (e.g., TXF, MXF, EXF) with their names.
    Use /futures/{code} to see all contracts for a specific product.
    """
    try:
        api = get_api_client(simulation=simulation)
        futures = api.Contracts.Futures
        
        products = []
        for attr in dir(futures):
            if attr.startswith('_'):
                continue
            product = getattr(futures, attr, None)
            if product and hasattr(product, '__iter__'):
                contracts = list(product)
                if contracts:
                    first = contracts[0]
                    products.append({
                        "code": attr,
                        "name": getattr(first, 'name', 'N/A'),
                        "contract_count": len(contracts),
                    })
        
        # Sort by code
        products.sort(key=lambda x: x['code'])
        
        return {
            "products": products,
            "count": len(products),
        }
    except LoginError as e:
        raise HTTPException(status_code=503, detail=str(e))


@app.get("/futures/{code}")
async def list_futures_contracts(
    code: str,
    simulation: bool = Query(True, description="Use simulation mode"),
):
    """
    Get all contracts for a specific futures product (second level).
    
    Example: /futures/TXF returns all TXF contracts (TXFK5, TXFL5, etc.)
    """
    try:
        api = get_api_client(simulation=simulation)
        futures = api.Contracts.Futures
        
        # Get the product by code (case-insensitive)
        product = getattr(futures, code.upper(), None)
        if not product:
            raise HTTPException(
                status_code=404, 
                detail=f"Futures product '{code}' not found. Use /futures to see available products."
            )
        
        contracts = []
        for contract in product:
            contracts.append({
                "symbol": contract.symbol,
                "code": contract.code,
                "name": contract.name,
                "delivery_month": contract.delivery_month,
                "delivery_date": contract.delivery_date,
                "underlying_kind": contract.underlying_kind,
                "unit": contract.unit,
                "limit_up": contract.limit_up,
                "limit_down": contract.limit_down,
                "reference": contract.reference,
            })
        
        return {
            "product_code": code.upper(),
            "product_name": contracts[0]['name'] if contracts else 'N/A',
            "contracts": contracts,
            "count": len(contracts),
        }
    except LoginError as e:
        raise HTTPException(status_code=503, detail=str(e))


@app.get("/symbols")
async def list_symbols(
    simulation: bool = Query(True, description="Use simulation mode"),
):
    """Get list of valid trading symbols from SUPPORTED_FUTURES (configured in ENV)."""
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


@app.post("/orders/{order_id}/recheck")
async def recheck_order_status(
    order_id: int,
    db: Session = Depends(get_db),
    _: str = Depends(verify_auth_key),
    simulation: bool = Query(True, description="Use simulation mode"),
):
    """
    Manually re-check an order's fill status from the exchange.
    
    This performs a single status check (not a background loop) and updates the database.
    Useful for orders where the background task may have timed out or for manual verification.
    """
    # Get order from database
    order_record = db.query(OrderHistory).filter(OrderHistory.id == order_id).first()
    if not order_record:
        raise HTTPException(status_code=404, detail=f"Order {order_id} not found")
    
    # Check if order has the necessary info to re-check
    if not order_record.seqno:
        raise HTTPException(
            status_code=400, 
            detail="Order does not have seqno - cannot re-check status. This may be a failed or no_action order."
        )
    
    try:
        api = get_api_client(simulation=simulation)
    except LoginError as e:
        raise HTTPException(status_code=503, detail=f"Failed to connect to trading API: {e}")
    
    # We need to reconstruct a minimal trade object or use list_trades
    # Since we don't have the original trade object, we'll use api.list_trades() to find it
    try:
        api.update_status(api.futopt_account)
        trades = api.list_trades()
        
        # Find the trade by seqno
        matching_trade = None
        for trade in trades:
            if trade.order.seqno == order_record.seqno:
                matching_trade = trade
                break
        
        if not matching_trade:
            # Trade not found in current session - might have been from previous session
            return {
                "order_id": order_id,
                "status": "not_found_in_session",
                "message": "Trade not found in current API session. The order may be from a previous trading session.",
                "current_db_status": order_record.status,
                "current_fill_status": order_record.fill_status,
            }
        
        # Get status from the matching trade
        status_obj = matching_trade.status
        order_obj = matching_trade.order
        
        # Get status value
        fill_status = status_obj.status.value if hasattr(status_obj.status, 'value') else str(status_obj.status)
        
        # Get deals and calculate average price
        deals = status_obj.deals if status_obj.deals else []
        deal_quantity = status_obj.deal_quantity if hasattr(status_obj, 'deal_quantity') else 0
        total_value = sum(d.price * d.quantity for d in deals) if deals else 0
        total_qty = sum(d.quantity for d in deals) if deals else 0
        fill_avg_price = total_value / total_qty if total_qty > 0 else 0.0
        
        # Update database record
        old_status = order_record.status
        old_fill_status = order_record.fill_status
        
        order_record.fill_status = fill_status
        order_record.order_id = getattr(order_obj, 'id', order_record.order_id)
        order_record.ordno = getattr(order_obj, 'ordno', order_record.ordno)
        order_record.fill_quantity = deal_quantity
        order_record.fill_price = fill_avg_price if fill_avg_price > 0 else order_record.fill_price
        order_record.cancel_quantity = getattr(status_obj, 'cancel_quantity', 0)
        order_record.updated_at = datetime.utcnow()
        
        # Update main status based on fill status
        if fill_status == "Filled":
            order_record.status = "filled"
        elif fill_status == "PartFilled":
            order_record.status = "partial_filled"
        elif fill_status in ("Cancelled", "Inactive"):
            order_record.status = "cancelled"
        elif fill_status == "Failed":
            order_record.status = "failed"
            order_record.error_message = getattr(status_obj, 'msg', '') or order_record.error_message
        elif fill_status in ("PendingSubmit", "PreSubmitted", "Submitted"):
            order_record.status = "submitted"
        
        db.commit()
        
        return {
            "order_id": order_id,
            "previous_status": old_status,
            "previous_fill_status": old_fill_status,
            "current_status": order_record.status,
            "current_fill_status": fill_status,
            "fill_quantity": deal_quantity,
            "fill_price": fill_avg_price,
            "cancel_quantity": getattr(status_obj, 'cancel_quantity', 0),
            "order_quantity": getattr(status_obj, 'order_quantity', 0),
            "deals": [
                {"seq": getattr(d, 'seq', ''), "price": d.price, "quantity": d.quantity}
                for d in deals
            ],
            "message": f"Status updated: {old_status} -> {order_record.status}",
        }
        
    except Exception as e:
        logger.exception(f"Error re-checking order {order_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Error checking order status: {e}")


STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


@app.get("/dashboard")
async def dashboard():
    """Serve the dashboard HTML page."""
    return FileResponse(os.path.join(STATIC_DIR, "dashboard.html"), media_type="text/html")
