"""
API Gateway for multi-tenant trading backend.

Routes requests to the correct tenant's worker based on tenant_slug in the URL.
All trading endpoints are under /api/v1/{tenant_slug}/...
"""
import os
import secrets
import logging
from typing import Optional, Dict, Any
from uuid import UUID

from fastapi import FastAPI, HTTPException, Depends, Header, Request, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import text
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
import redis

from database import get_db
from models.tenant import Tenant, TenantStatus, WorkerInstance, WorkerStatus
from trading_queue import TradingQueueClient, TradingOperation

# Configure sanitized logging (prevents credential leakage)
from utils.log_sanitizer import configure_sanitized_logging
configure_sanitized_logging(level=logging.INFO)
logger = logging.getLogger(__name__)

# Rate limiter - uses client IP address
limiter = Limiter(key_func=get_remote_address)

# Environment configuration
ALLOWED_ORIGINS = [origin.strip() for origin in os.getenv("ALLOWED_ORIGINS", "*").split(",")]
REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))

# FastAPI app
app = FastAPI(
    title="Trading API Gateway",
    description="Multi-tenant API Gateway for trading backends",
    version="1.0.0",
)

# Rate limiter setup
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# =============================================================================
# Tenant Queue Client Cache
# =============================================================================

_queue_clients: Dict[str, TradingQueueClient] = {}


def get_queue_client_for_tenant(tenant_id: str, redis_db: int) -> TradingQueueClient:
    """Get or create a queue client for a tenant."""
    cache_key = f"{tenant_id}:{redis_db}"

    if cache_key not in _queue_clients:
        redis_url = f"redis://{REDIS_HOST}:{REDIS_PORT}/{redis_db}"
        _queue_clients[cache_key] = TradingQueueClient(redis_url, tenant_id)

    return _queue_clients[cache_key]


# =============================================================================
# Authentication
# =============================================================================

async def get_tenant_from_slug(
    tenant_slug: str,
    db: Session = Depends(get_db),
) -> Tenant:
    """Get tenant from slug and verify it's active."""
    tenant = db.query(Tenant).filter(
        Tenant.slug == tenant_slug,
        Tenant.deleted_at.is_(None),
    ).first()

    if not tenant:
        raise HTTPException(status_code=404, detail=f"Tenant '{tenant_slug}' not found")

    if tenant.status != TenantStatus.ACTIVE.value:
        raise HTTPException(status_code=403, detail=f"Tenant '{tenant_slug}' is not active")

    return tenant


async def get_worker_for_tenant(
    tenant: Tenant = Depends(get_tenant_from_slug),
    db: Session = Depends(get_db),
) -> WorkerInstance:
    """Get worker instance for a tenant and verify it's running."""
    worker = db.query(WorkerInstance).filter(
        WorkerInstance.tenant_id == tenant.id,
    ).first()

    if not worker:
        raise HTTPException(
            status_code=503,
            detail=f"No worker configured for tenant '{tenant.slug}'"
        )

    if worker.status != WorkerStatus.RUNNING.value:
        raise HTTPException(
            status_code=503,
            detail=f"Worker for tenant '{tenant.slug}' is not running (status: {worker.status})"
        )

    return worker


async def verify_tenant_token(
    authorization: Optional[str] = Header(None),
    tenant: Tenant = Depends(get_tenant_from_slug),
) -> bool:
    """
    Verify tenant-specific API token.

    The token should be stored in tenant.tenant_metadata['api_token'].
    """
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing authorization header")

    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Invalid authorization format")

    token = authorization[7:]

    # Get expected token from tenant metadata
    expected_token = (tenant.tenant_metadata or {}).get("api_token")

    if not expected_token:
        # No token configured - allow any token (for development)
        logger.warning(f"No API token configured for tenant {tenant.slug}")
        return True

    if not secrets.compare_digest(token, expected_token):
        raise HTTPException(status_code=401, detail="Invalid API token")

    return True


# =============================================================================
# Request/Response Models
# =============================================================================

class OrderRequest(BaseModel):
    symbol: str
    quantity: int
    action: str  # long_entry, long_exit, short_entry, short_exit


class SymbolInfoRequest(BaseModel):
    symbol: str


# =============================================================================
# Health Check
# =============================================================================

@app.get("/health")
async def health():
    """Gateway health check."""
    return {"status": "ok", "service": "gateway"}


# =============================================================================
# BYOB Compatibility Endpoints (no tenant required)
# =============================================================================

@app.get("/api/v1/ping")
async def ping_compat():
    """
    Compatibility endpoint for BYOB (Bring Your Own Backend) mode.
    Returns a simple health check without requiring tenant authentication.
    """
    return {"success": True, "message": "pong", "service": "gateway"}


@app.get("/api/v1/health")
async def health_compat():
    """
    Compatibility endpoint for BYOB health checks.
    """
    return {"status": "ok", "service": "gateway"}


# =============================================================================
# Tenant-Scoped Trading Endpoints
# =============================================================================

@app.get("/api/v1/{tenant_slug}/ping")
async def ping(
    tenant: Tenant = Depends(get_tenant_from_slug),
    worker: WorkerInstance = Depends(get_worker_for_tenant),
    _: bool = Depends(verify_tenant_token),
    db: Session = Depends(get_db),
):
    """Ping the trading worker."""
    try:
        client = get_queue_client_for_tenant(str(tenant.id), worker.redis_db)
        response = client.submit_request(TradingOperation.PING, timeout=5)
        return {"success": response.success, "tenant": tenant.slug}
    except TimeoutError:
        raise HTTPException(status_code=503, detail="Worker not responding")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/v1/{tenant_slug}/status")
async def get_status(
    tenant: Tenant = Depends(get_tenant_from_slug),
    worker: WorkerInstance = Depends(get_worker_for_tenant),
    _: bool = Depends(verify_tenant_token),
    db: Session = Depends(get_db),
):
    """Get trading backend status."""
    try:
        client = get_queue_client_for_tenant(str(tenant.id), worker.redis_db)
        is_healthy = client.check_worker_health()

        return {
            "tenant": tenant.slug,
            "worker_status": worker.status,
            "worker_healthy": is_healthy,
            "redis_db": worker.redis_db,
        }
    except Exception as e:
        return {
            "tenant": tenant.slug,
            "worker_status": worker.status,
            "worker_healthy": False,
            "error": str(e),
        }


@app.get("/api/v1/{tenant_slug}/symbols")
async def get_symbols(
    tenant: Tenant = Depends(get_tenant_from_slug),
    worker: WorkerInstance = Depends(get_worker_for_tenant),
    _: bool = Depends(verify_tenant_token),
    simulation: bool = Query(True),
    db: Session = Depends(get_db),
):
    """Get available trading symbols."""
    try:
        client = get_queue_client_for_tenant(str(tenant.id), worker.redis_db)
        response = client.get_symbols(simulation=simulation)

        if not response.success:
            raise HTTPException(status_code=500, detail=response.error)

        return response.data
    except TimeoutError:
        raise HTTPException(status_code=503, detail="Worker not responding")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/v1/{tenant_slug}/symbols/{symbol}")
async def get_symbol_info(
    symbol: str,
    tenant: Tenant = Depends(get_tenant_from_slug),
    worker: WorkerInstance = Depends(get_worker_for_tenant),
    _: bool = Depends(verify_tenant_token),
    simulation: bool = Query(True),
    db: Session = Depends(get_db),
):
    """Get detailed info for a symbol."""
    try:
        client = get_queue_client_for_tenant(str(tenant.id), worker.redis_db)
        response = client.get_symbol_info(symbol, simulation=simulation)

        if not response.success:
            raise HTTPException(status_code=500, detail=response.error)

        return response.data
    except TimeoutError:
        raise HTTPException(status_code=503, detail="Worker not responding")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/v1/{tenant_slug}/positions")
async def get_positions(
    tenant: Tenant = Depends(get_tenant_from_slug),
    worker: WorkerInstance = Depends(get_worker_for_tenant),
    _: bool = Depends(verify_tenant_token),
    simulation: bool = Query(True),
    db: Session = Depends(get_db),
):
    """Get current positions."""
    try:
        client = get_queue_client_for_tenant(str(tenant.id), worker.redis_db)
        response = client.get_positions(simulation=simulation)

        if not response.success:
            raise HTTPException(status_code=500, detail=response.error)

        return response.data
    except TimeoutError:
        raise HTTPException(status_code=503, detail="Worker not responding")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/v1/{tenant_slug}/futures")
async def get_futures(
    tenant: Tenant = Depends(get_tenant_from_slug),
    worker: WorkerInstance = Depends(get_worker_for_tenant),
    _: bool = Depends(verify_tenant_token),
    simulation: bool = Query(True),
    db: Session = Depends(get_db),
):
    """Get futures overview."""
    try:
        client = get_queue_client_for_tenant(str(tenant.id), worker.redis_db)
        response = client.get_futures_overview(simulation=simulation)

        if not response.success:
            raise HTTPException(status_code=500, detail=response.error)

        return response.data
    except TimeoutError:
        raise HTTPException(status_code=503, detail="Worker not responding")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/v1/{tenant_slug}/futures/{product}")
async def get_product_contracts(
    product: str,
    tenant: Tenant = Depends(get_tenant_from_slug),
    worker: WorkerInstance = Depends(get_worker_for_tenant),
    _: bool = Depends(verify_tenant_token),
    simulation: bool = Query(True),
    db: Session = Depends(get_db),
):
    """Get contracts for a futures product."""
    try:
        client = get_queue_client_for_tenant(str(tenant.id), worker.redis_db)
        response = client.get_product_contracts(product, simulation=simulation)

        if not response.success:
            raise HTTPException(status_code=500, detail=response.error)

        return response.data
    except TimeoutError:
        raise HTTPException(status_code=503, detail="Worker not responding")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/v1/{tenant_slug}/contracts")
async def get_contract_codes(
    tenant: Tenant = Depends(get_tenant_from_slug),
    worker: WorkerInstance = Depends(get_worker_for_tenant),
    _: bool = Depends(verify_tenant_token),
    simulation: bool = Query(True),
    db: Session = Depends(get_db),
):
    """Get valid contract codes."""
    try:
        client = get_queue_client_for_tenant(str(tenant.id), worker.redis_db)
        response = client.get_contract_codes(simulation=simulation)

        if not response.success:
            raise HTTPException(status_code=500, detail=response.error)

        return response.data
    except TimeoutError:
        raise HTTPException(status_code=503, detail="Worker not responding")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/v1/{tenant_slug}/order")
@limiter.limit("30/minute")
async def place_order(
    request: OrderRequest,
    req: Request,
    tenant: Tenant = Depends(get_tenant_from_slug),
    worker: WorkerInstance = Depends(get_worker_for_tenant),
    _: bool = Depends(verify_tenant_token),
    simulation: bool = Query(True),
    db: Session = Depends(get_db),
):
    """Place a trading order."""
    try:
        client = get_queue_client_for_tenant(str(tenant.id), worker.redis_db)

        if request.action in ("long_entry", "short_entry"):
            response = client.place_entry_order(
                symbol=request.symbol,
                quantity=request.quantity,
                action=request.action,
                simulation=simulation,
            )
        else:
            response = client.place_exit_order(
                symbol=request.symbol,
                position_direction="long" if request.action == "long_exit" else "short",
                simulation=simulation,
            )

        if not response.success:
            raise HTTPException(status_code=400, detail=response.error)

        return response.data
    except TimeoutError:
        raise HTTPException(status_code=503, detail="Worker not responding")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# TradingView Webhook
# =============================================================================

class WebhookLog:
    """Model for webhook_logs table."""
    pass  # Using raw SQL for simplicity


class TradingViewPayload(BaseModel):
    """
    TradingView webhook payload.

    TradingView sends JSON in alert message. Common fields:
    - ticker: Symbol name (e.g., "TXFD5")
    - action: Trading action (buy, sell, long, short, exit_long, exit_short)
    - quantity: Number of contracts (optional, defaults to 1)
    - price: Alert trigger price (optional)
    - alert_name: Name of the alert (optional)
    - time: Alert time (optional)

    Example TradingView alert message:
    {
        "ticker": "{{ticker}}",
        "action": "buy",
        "quantity": 1,
        "price": {{close}},
        "alert_name": "{{strategy.order.alert_message}}"
    }
    """
    ticker: Optional[str] = None
    symbol: Optional[str] = None  # Alternative to ticker
    action: str
    quantity: Optional[int] = 1
    price: Optional[float] = None
    alert_name: Optional[str] = None
    time: Optional[str] = None
    # Allow extra fields from TradingView
    class Config:
        extra = "allow"


def parse_tradingview_action(action: str) -> Optional[str]:
    """
    Parse TradingView action to internal action format.

    TradingView actions: buy, sell, long, short, exit_long, exit_short, close_long, close_short
    Internal actions: long_entry, long_exit, short_entry, short_exit
    """
    action_lower = action.lower().strip()

    mapping = {
        # Entry actions
        "buy": "long_entry",
        "long": "long_entry",
        "long_entry": "long_entry",
        "sell": "short_entry",
        "short": "short_entry",
        "short_entry": "short_entry",
        # Exit actions
        "exit_long": "long_exit",
        "close_long": "long_exit",
        "long_exit": "long_exit",
        "exit_short": "short_exit",
        "close_short": "short_exit",
        "short_exit": "short_exit",
        # Generic exit (close all)
        "exit": None,  # Need position info to determine
        "close": None,
    }

    return mapping.get(action_lower)


@app.post("/api/v1/{tenant_slug}/webhook")
@limiter.limit("60/minute")
async def tradingview_webhook(
    tenant_slug: str,
    request: Request,
    db: Session = Depends(get_db),
):
    """
    TradingView webhook endpoint.

    Receives alerts from TradingView and executes trades.

    Authentication: Webhook secret in query param or X-Webhook-Secret header.

    Example URL: https://gateway.example.com/api/v1/my-tenant/webhook?secret=xxx
    """
    source_ip = request.client.host if request.client else "unknown"
    webhook_log_id = None

    try:
        # 1. Get tenant
        tenant = db.query(Tenant).filter(
            Tenant.slug == tenant_slug,
            Tenant.deleted_at.is_(None),
        ).first()

        if not tenant:
            logger.warning(f"Webhook: Tenant not found: {tenant_slug} from {source_ip}")
            raise HTTPException(status_code=404, detail="Tenant not found")

        # 2. Check if webhook is enabled
        if not tenant.webhook_enabled:
            logger.warning(f"Webhook: Not enabled for tenant {tenant_slug}")
            raise HTTPException(status_code=403, detail="Webhook not enabled for this tenant")

        # 3. Parse request body
        try:
            body_bytes = await request.body()
            body_str = body_bytes.decode("utf-8")

            # Try to parse as JSON
            import json
            try:
                body_json = json.loads(body_str)
            except json.JSONDecodeError:
                # TradingView sometimes sends plain text
                body_json = {"raw": body_str}
        except Exception as e:
            logger.error(f"Webhook: Failed to parse body: {e}")
            raise HTTPException(status_code=400, detail="Invalid request body")

        # 4. Log the webhook request
        headers_dict = dict(request.headers)
        # Remove sensitive headers
        headers_dict.pop("authorization", None)
        headers_dict.pop("x-webhook-secret", None)

        result = db.execute(
            text("""
            INSERT INTO webhook_logs (tenant_id, source_ip, request_body, headers, status)
            VALUES (CAST(:tenant_id AS UUID), :source_ip, CAST(:request_body AS jsonb), CAST(:headers AS jsonb), 'received')
            RETURNING id
            """),
            {
                "tenant_id": str(tenant.id),
                "source_ip": source_ip,
                "request_body": json.dumps(body_json),
                "headers": json.dumps(headers_dict),
            }
        ).fetchone()
        webhook_log_id = result[0] if result else None
        db.commit()

        # 5. Validate webhook secret
        secret_param = request.query_params.get("secret")
        secret_header = request.headers.get("x-webhook-secret")
        provided_secret = secret_param or secret_header

        if not tenant.webhook_secret:
            logger.warning(f"Webhook: No secret configured for tenant {tenant_slug}")
            raise HTTPException(status_code=403, detail="Webhook secret not configured")

        if not provided_secret:
            logger.warning(f"Webhook: No secret provided for tenant {tenant_slug}")
            db.execute(
                text("UPDATE webhook_logs SET status = 'failed', error_message = :error WHERE id = :id"),
                {"id": webhook_log_id, "error": "No secret provided"}
            )
            db.commit()
            raise HTTPException(status_code=401, detail="Webhook secret required")

        if not secrets.compare_digest(provided_secret, tenant.webhook_secret):
            logger.warning(f"Webhook: Invalid secret for tenant {tenant_slug}")
            db.execute(
                text("UPDATE webhook_logs SET status = 'failed', error_message = :error WHERE id = :id"),
                {"id": webhook_log_id, "error": "Invalid secret"}
            )
            db.commit()
            raise HTTPException(status_code=401, detail="Invalid webhook secret")

        # 6. Update log status to validated
        db.execute(
            text("UPDATE webhook_logs SET status = 'validated' WHERE id = :id"),
            {"id": webhook_log_id}
        )
        db.commit()

        # 7. Parse TradingView payload
        try:
            payload = TradingViewPayload(**body_json)
        except Exception as e:
            logger.error(f"Webhook: Failed to parse payload: {e}")
            db.execute(
                text("UPDATE webhook_logs SET status = 'failed', error_message = :error WHERE id = :id"),
                {"id": webhook_log_id, "error": f"Invalid payload: {e}"}
            )
            db.commit()
            raise HTTPException(status_code=400, detail=f"Invalid payload format: {e}")

        # Get symbol (ticker or symbol field)
        symbol = payload.ticker or payload.symbol
        if not symbol:
            db.execute(
                text("UPDATE webhook_logs SET status = 'failed', error_message = :error WHERE id = :id"),
                {"id": webhook_log_id, "error": "No ticker/symbol in payload"}
            )
            db.commit()
            raise HTTPException(status_code=400, detail="Missing ticker or symbol in payload")

        # Parse action
        internal_action = parse_tradingview_action(payload.action)
        if not internal_action:
            db.execute(
                text("UPDATE webhook_logs SET status = 'failed', error_message = :error WHERE id = :id"),
                {"id": webhook_log_id, "error": f"Unknown action: {payload.action}"}
            )
            db.commit()
            raise HTTPException(status_code=400, detail=f"Unknown action: {payload.action}")

        # Update log with parsed data
        db.execute(
            text("""
            UPDATE webhook_logs
            SET tv_alert_name = :alert_name, tv_ticker = :ticker,
                tv_action = :action, tv_quantity = :quantity, tv_price = :price
            WHERE id = :id
            """),
            {
                "id": webhook_log_id,
                "alert_name": payload.alert_name,
                "ticker": symbol,
                "action": internal_action,
                "quantity": payload.quantity,
                "price": payload.price,
            }
        )
        db.commit()

        # 8. Get worker and execute trade
        worker = db.query(WorkerInstance).filter(
            WorkerInstance.tenant_id == tenant.id,
        ).first()

        if not worker or worker.status != WorkerStatus.RUNNING.value:
            error_msg = "Worker not running"
            db.execute(
                text("UPDATE webhook_logs SET status = 'failed', error_message = :error WHERE id = :id"),
                {"id": webhook_log_id, "error": error_msg}
            )
            db.commit()
            raise HTTPException(status_code=503, detail=error_msg)

        # 9. Execute trade via queue
        try:
            client = get_queue_client_for_tenant(str(tenant.id), worker.redis_db)

            quantity = payload.quantity or 1

            if internal_action in ("long_entry", "short_entry"):
                response = client.place_entry_order(
                    symbol=symbol,
                    quantity=quantity,
                    action="Buy" if internal_action == "long_entry" else "Sell",
                    simulation=False,  # Webhooks are for real trading
                )
            else:
                response = client.place_exit_order(
                    symbol=symbol,
                    position_direction="Buy" if internal_action == "long_exit" else "Sell",
                    simulation=False,
                )

            if not response.success:
                db.execute(
                    text("UPDATE webhook_logs SET status = 'failed', error_message = :error, processed_at = NOW() WHERE id = :id"),
                    {"id": webhook_log_id, "error": response.error}
                )
                db.commit()
                raise HTTPException(status_code=400, detail=response.error)

            # Success - update log
            db.execute(
                text("UPDATE webhook_logs SET status = 'processed', processed_at = NOW() WHERE id = :id"),
                {"id": webhook_log_id}
            )
            db.commit()

            logger.info(f"Webhook: Processed for tenant {tenant_slug}: {internal_action} {symbol} x{quantity}")

            return {
                "success": True,
                "message": "Webhook processed",
                "action": internal_action,
                "symbol": symbol,
                "quantity": quantity,
                "order": response.data,
            }

        except TimeoutError:
            db.execute(
                text("UPDATE webhook_logs SET status = 'failed', error_message = :error, processed_at = NOW() WHERE id = :id"),
                {"id": webhook_log_id, "error": "Worker timeout"}
            )
            db.commit()
            raise HTTPException(status_code=503, detail="Worker not responding")

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Webhook: Unexpected error for {tenant_slug}: {e}")
        if webhook_log_id:
            db.execute(
                text("UPDATE webhook_logs SET status = 'failed', error_message = :error, processed_at = NOW() WHERE id = :id"),
                {"id": webhook_log_id, "error": str(e)}
            )
            db.commit()
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# Entry point for standalone mode
# =============================================================================

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
