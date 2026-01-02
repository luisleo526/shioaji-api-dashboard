"""
Admin API for tenant management.

This API is used by the SaaS frontend to manage tenants, credentials, and workers.
"""
import os
import secrets
import logging
from typing import Optional, List
from uuid import UUID

from fastapi import FastAPI, HTTPException, Depends, Header, Request, UploadFile, File, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.orm import Session
from sqlalchemy import text
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

from database import get_db
from models.tenant import TenantStatus, PlanTier, CredentialType
from admin.tenant_service import (
    TenantService,
    TenantNotFoundError,
    TenantAlreadyExistsError,
    InvalidSlugError,
    TenantServiceError,
)
from orchestrator.worker_manager import (
    WorkerManager,
    WorkerManagerError,
    WorkerNotFoundError,
    WorkerAlreadyRunningError,
    CredentialsNotFoundError,
)

# Configure sanitized logging (prevents credential leakage)
from utils.log_sanitizer import configure_sanitized_logging, mask_credential
configure_sanitized_logging(level=logging.INFO)
logger = logging.getLogger(__name__)

# Rate limiter - uses client IP address
limiter = Limiter(key_func=get_remote_address)

# Environment configuration
ADMIN_API_TOKEN = os.getenv("ADMIN_API_TOKEN", "admin-changeme")
ALLOWED_ORIGINS = [origin.strip() for origin in os.getenv("ALLOWED_ORIGINS", "*").split(",")]

# FastAPI app
app = FastAPI(
    title="Trading Backend Admin API",
    description="Admin API for managing tenants, credentials, and workers",
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
# Authentication
# =============================================================================

async def verify_admin_token(
    authorization: Optional[str] = Header(None),
) -> bool:
    """Verify admin API token."""
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing authorization header")

    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Invalid authorization format")

    token = authorization[7:]  # Remove "Bearer " prefix

    if not secrets.compare_digest(token, ADMIN_API_TOKEN):
        raise HTTPException(status_code=401, detail="Invalid admin token")

    return True


async def get_current_user_id(
    x_user_id: Optional[str] = Header(None, alias="X-User-ID"),
) -> str:
    """
    Extract user ID from X-User-ID header.

    In production, this should be validated against the auth token.
    The frontend should extract the user ID from the Supabase session
    and pass it in this header.
    """
    if not x_user_id:
        raise HTTPException(
            status_code=401,
            detail="Missing X-User-ID header. User authentication required."
        )
    return x_user_id


async def get_optional_user_id(
    x_user_id: Optional[str] = Header(None, alias="X-User-ID"),
) -> Optional[str]:
    """Get user ID if provided (for admin endpoints that can optionally filter by owner)."""
    return x_user_id


# =============================================================================
# Request/Response Models
# =============================================================================

class CreateTenantRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    slug: Optional[str] = Field(None, min_length=3, max_length=63, pattern=r"^[a-z0-9][a-z0-9-]*[a-z0-9]$")
    email: EmailStr
    plan_tier: PlanTier = PlanTier.FREE
    metadata: Optional[dict] = None
    # Note: owner_id is extracted from X-User-ID header, not from request body


class UpdateTenantRequest(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    email: Optional[EmailStr] = None
    status: Optional[TenantStatus] = None
    plan_tier: Optional[PlanTier] = None
    metadata: Optional[dict] = None


class TenantResponse(BaseModel):
    id: str
    owner_id: str
    name: str
    slug: str
    email: str
    status: str
    plan_tier: str
    created_at: Optional[str]
    updated_at: Optional[str]
    metadata: dict = {}

    class Config:
        from_attributes = True


class CredentialUploadRequest(BaseModel):
    api_key: str = Field(..., min_length=1)
    secret_key: str = Field(..., min_length=1)


class CredentialResponse(BaseModel):
    id: str
    tenant_id: str
    credential_type: str
    status: str
    fingerprint: Optional[str]
    created_at: Optional[str]
    verified_at: Optional[str]


class WorkerStatusResponse(BaseModel):
    id: Optional[str]
    tenant_id: str
    container_id: Optional[str]
    container_name: Optional[str]
    status: str
    redis_db: Optional[int]
    health_status: Optional[str]
    started_at: Optional[str]
    stopped_at: Optional[str]
    error_message: Optional[str]


class ErrorResponse(BaseModel):
    detail: str


# =============================================================================
# Health Check
# =============================================================================

@app.get("/health")
async def health():
    """Health check endpoint."""
    return {"status": "ok", "service": "admin-api"}


# =============================================================================
# Tenant CRUD Endpoints
# =============================================================================

@app.post(
    "/admin/tenants",
    response_model=TenantResponse,
    responses={400: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
)
@limiter.limit("10/minute")
async def create_tenant(
    request: Request,
    body: CreateTenantRequest,
    db: Session = Depends(get_db),
    _: bool = Depends(verify_admin_token),
    user_id: str = Depends(get_current_user_id),
):
    """
    Create a new tenant.

    The tenant will be owned by the user specified in X-User-ID header.
    A secure slug will be auto-generated with a random prefix.
    """
    try:
        service = TenantService(db)
        tenant = service.create_tenant(
            owner_id=user_id,
            name=body.name,
            email=body.email,
            slug=body.slug,  # Will be prefixed with random string
            plan_tier=body.plan_tier,
            metadata=body.metadata,
            ip_address=request.client.host if request.client else None,
        )
        return TenantResponse(**tenant.to_dict())

    except InvalidSlugError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except TenantAlreadyExistsError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except TenantServiceError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/admin/tenants", response_model=List[TenantResponse])
async def list_tenants(
    status: Optional[TenantStatus] = Query(None),
    plan_tier: Optional[PlanTier] = Query(None),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    _: bool = Depends(verify_admin_token),
    user_id: str = Depends(get_current_user_id),
):
    """
    List tenants owned by the current user.

    Users can only see their own tenants.
    """
    service = TenantService(db)
    tenants = service.list_tenants(
        owner_id=user_id,  # Only return tenants owned by this user
        status=status,
        plan_tier=plan_tier,
        limit=limit,
        offset=offset,
    )
    return [TenantResponse(**t.to_dict()) for t in tenants]


@app.get(
    "/admin/tenants/{tenant_id}",
    response_model=TenantResponse,
    responses={404: {"model": ErrorResponse}},
)
async def get_tenant(
    tenant_id: UUID,
    db: Session = Depends(get_db),
    _: bool = Depends(verify_admin_token),
    user_id: str = Depends(get_current_user_id),
):
    """
    Get a tenant by ID.

    Users can only access their own tenants.
    """
    try:
        service = TenantService(db)
        tenant = service.get_tenant(tenant_id, owner_id=user_id)
        return TenantResponse(**tenant.to_dict())
    except TenantNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.get(
    "/admin/tenants/by-slug/{slug}",
    response_model=TenantResponse,
    responses={404: {"model": ErrorResponse}},
)
async def get_tenant_by_slug(
    slug: str,
    db: Session = Depends(get_db),
    _: bool = Depends(verify_admin_token),
):
    """Get a tenant by slug."""
    try:
        service = TenantService(db)
        tenant = service.get_tenant_by_slug(slug)
        return TenantResponse(**tenant.to_dict())
    except TenantNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.patch(
    "/admin/tenants/{tenant_id}",
    response_model=TenantResponse,
    responses={404: {"model": ErrorResponse}},
)
async def update_tenant(
    request: Request,
    tenant_id: UUID,
    body: UpdateTenantRequest,
    db: Session = Depends(get_db),
    _: bool = Depends(verify_admin_token),
):
    """Update a tenant."""
    try:
        service = TenantService(db)
        tenant = service.update_tenant(
            tenant_id,
            name=body.name,
            email=body.email,
            status=body.status,
            plan_tier=body.plan_tier,
            metadata=body.metadata,
            ip_address=request.client.host if request.client else None,
        )
        return TenantResponse(**tenant.to_dict())
    except TenantNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.delete(
    "/admin/tenants/{tenant_id}",
    status_code=204,
    responses={404: {"model": ErrorResponse}},
)
async def delete_tenant(
    tenant_id: UUID,
    req: Request,
    db: Session = Depends(get_db),
    _: bool = Depends(verify_admin_token),
):
    """Delete a tenant (soft delete)."""
    try:
        service = TenantService(db)
        service.delete_tenant(
            tenant_id,
            ip_address=req.client.host if req.client else None,
        )
    except TenantNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.post(
    "/admin/tenants/{tenant_id}/activate",
    response_model=TenantResponse,
    responses={404: {"model": ErrorResponse}},
)
async def activate_tenant(
    tenant_id: UUID,
    req: Request,
    db: Session = Depends(get_db),
    _: bool = Depends(verify_admin_token),
):
    """Activate a pending tenant."""
    try:
        service = TenantService(db)
        tenant = service.activate_tenant(
            tenant_id,
            ip_address=req.client.host if req.client else None,
        )
        return TenantResponse(**tenant.to_dict())
    except TenantNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.post(
    "/admin/tenants/{tenant_id}/suspend",
    response_model=TenantResponse,
    responses={404: {"model": ErrorResponse}},
)
async def suspend_tenant(
    tenant_id: UUID,
    req: Request,
    db: Session = Depends(get_db),
    _: bool = Depends(verify_admin_token),
):
    """Suspend an active tenant."""
    try:
        service = TenantService(db)
        tenant = service.suspend_tenant(
            tenant_id,
            ip_address=req.client.host if req.client else None,
        )
        return TenantResponse(**tenant.to_dict())
    except TenantNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))


# =============================================================================
# Credential Management Endpoints
# =============================================================================

@app.post(
    "/admin/tenants/{tenant_id}/credentials/shioaji",
    response_model=CredentialResponse,
    responses={404: {"model": ErrorResponse}},
)
@limiter.limit("5/minute")
async def upload_shioaji_credentials(
    request: Request,
    tenant_id: UUID,
    body: CredentialUploadRequest,
    db: Session = Depends(get_db),
    _: bool = Depends(verify_admin_token),
):
    """Upload Shioaji API credentials."""
    try:
        service = TenantService(db)
        credential = service.upload_shioaji_credentials(
            tenant_id,
            api_key=body.api_key,
            secret_key=body.secret_key,
            ip_address=request.client.host if request.client else None,
        )
        return CredentialResponse(**credential.to_dict())
    except TenantNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except TenantServiceError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post(
    "/admin/tenants/{tenant_id}/credentials/ca",
    response_model=CredentialResponse,
    responses={404: {"model": ErrorResponse}},
)
@limiter.limit("5/minute")
async def upload_ca_certificate(
    request: Request,
    tenant_id: UUID,
    ca_file: UploadFile = File(...),
    ca_password: str = Query(...),
    db: Session = Depends(get_db),
    _: bool = Depends(verify_admin_token),
):
    """Upload CA certificate for real trading."""
    try:
        file_content = await ca_file.read()
        service = TenantService(db)
        credential = service.upload_ca_certificate(
            tenant_id,
            ca_file=file_content,
            ca_password=ca_password,
            ip_address=request.client.host if request.client else None,
        )
        return CredentialResponse(**credential.to_dict())
    except TenantNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except TenantServiceError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get(
    "/admin/tenants/{tenant_id}/credentials",
    responses={404: {"model": ErrorResponse}},
)
async def get_credential_status(
    tenant_id: UUID,
    db: Session = Depends(get_db),
    _: bool = Depends(verify_admin_token),
):
    """Get credential status for a tenant."""
    try:
        service = TenantService(db)
        return service.get_credential_status(tenant_id)
    except TenantNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.delete(
    "/admin/tenants/{tenant_id}/credentials/{credential_type}",
    status_code=204,
    responses={404: {"model": ErrorResponse}},
)
async def revoke_credentials(
    tenant_id: UUID,
    credential_type: CredentialType,
    req: Request,
    db: Session = Depends(get_db),
    _: bool = Depends(verify_admin_token),
):
    """Revoke and delete credentials."""
    try:
        service = TenantService(db)
        service.revoke_credentials(
            tenant_id,
            credential_type,
            ip_address=req.client.host if req.client else None,
        )
    except TenantNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except TenantServiceError as e:
        raise HTTPException(status_code=400, detail=str(e))


# =============================================================================
# Worker Management Endpoints (Placeholder - will be implemented with WorkerManager)
# =============================================================================

@app.get(
    "/admin/tenants/{tenant_id}/worker",
    response_model=WorkerStatusResponse,
    responses={404: {"model": ErrorResponse}},
)
async def get_worker_status(
    tenant_id: UUID,
    db: Session = Depends(get_db),
    _: bool = Depends(verify_admin_token),
):
    """Get worker status for a tenant."""
    try:
        service = TenantService(db)
        tenant = service.get_tenant(tenant_id)
        instance = service.get_worker_instance(tenant_id)

        if not instance:
            return WorkerStatusResponse(
                id=None,
                tenant_id=str(tenant_id),
                container_id=None,
                container_name=None,
                status="not_created",
                redis_db=None,
                health_status=None,
                started_at=None,
                stopped_at=None,
                error_message=None,
            )

        return WorkerStatusResponse(**instance.to_dict())
    except TenantNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.post(
    "/admin/tenants/{tenant_id}/worker/start",
    response_model=WorkerStatusResponse,
    responses={404: {"model": ErrorResponse}, 400: {"model": ErrorResponse}},
)
async def start_worker(
    tenant_id: UUID,
    req: Request,
    db: Session = Depends(get_db),
    _: bool = Depends(verify_admin_token),
):
    """Start a worker for a tenant."""
    try:
        manager = WorkerManager(db)

        # Check if worker instance exists
        service = TenantService(db)
        instance = service.get_worker_instance(tenant_id)

        if not instance:
            # Create and start
            instance = await manager.create_worker(tenant_id)
            instance = await manager.start_worker(tenant_id)
        elif instance.status in ("stopped", "error", "pending"):
            # Just start existing
            instance = await manager.start_worker(tenant_id)
        elif instance.status == "hibernating":
            # Wake from hibernation
            instance = await manager.wake_worker(tenant_id)
        # else: already running

        return WorkerStatusResponse(**instance.to_dict())

    except TenantNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except CredentialsNotFoundError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except WorkerAlreadyRunningError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except WorkerManagerError as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post(
    "/admin/tenants/{tenant_id}/worker/stop",
    response_model=WorkerStatusResponse,
    responses={404: {"model": ErrorResponse}, 400: {"model": ErrorResponse}},
)
async def stop_worker(
    tenant_id: UUID,
    req: Request,
    db: Session = Depends(get_db),
    _: bool = Depends(verify_admin_token),
):
    """Stop a worker for a tenant."""
    try:
        manager = WorkerManager(db)
        instance = await manager.stop_worker(tenant_id)
        return WorkerStatusResponse(**instance.to_dict())

    except TenantNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except WorkerNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except WorkerManagerError as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post(
    "/admin/tenants/{tenant_id}/worker/restart",
    response_model=WorkerStatusResponse,
    responses={404: {"model": ErrorResponse}, 400: {"model": ErrorResponse}},
)
async def restart_worker(
    tenant_id: UUID,
    req: Request,
    db: Session = Depends(get_db),
    _: bool = Depends(verify_admin_token),
):
    """Restart a worker for a tenant."""
    try:
        manager = WorkerManager(db)

        # Stop first (ignore if not running)
        try:
            await manager.stop_worker(tenant_id)
        except WorkerNotFoundError:
            pass

        # Then start
        service = TenantService(db)
        instance = service.get_worker_instance(tenant_id)

        if not instance:
            instance = await manager.create_worker(tenant_id)

        instance = await manager.start_worker(tenant_id)
        return WorkerStatusResponse(**instance.to_dict())

    except TenantNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except CredentialsNotFoundError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except WorkerManagerError as e:
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# Webhook Management Endpoints
# =============================================================================

class WebhookConfigRequest(BaseModel):
    enabled: bool = True


class WebhookConfigResponse(BaseModel):
    tenant_id: str
    webhook_enabled: bool
    webhook_secret: Optional[str] = None
    webhook_url: str


class WebhookLogResponse(BaseModel):
    id: int
    tenant_id: str
    source_ip: Optional[str]
    status: str
    error_message: Optional[str]
    tv_alert_name: Optional[str]
    tv_ticker: Optional[str]
    tv_action: Optional[str]
    tv_quantity: Optional[int]
    tv_price: Optional[float]
    created_at: Optional[str]
    processed_at: Optional[str]


def generate_webhook_secret() -> str:
    """Generate a secure webhook secret."""
    return secrets.token_urlsafe(32)


@app.get(
    "/admin/tenants/{tenant_id}/webhook",
    response_model=WebhookConfigResponse,
    responses={404: {"model": ErrorResponse}},
)
async def get_webhook_config(
    tenant_id: UUID,
    db: Session = Depends(get_db),
    _: bool = Depends(verify_admin_token),
    user_id: str = Depends(get_current_user_id),
):
    """Get webhook configuration for a tenant."""
    service = TenantService(db)

    try:
        tenant = service.get_tenant(tenant_id)
    except TenantNotFoundError:
        raise HTTPException(status_code=404, detail="Tenant not found")

    # Check ownership
    if tenant.owner_id != user_id:
        raise HTTPException(status_code=403, detail="Not authorized to access this tenant")

    # Build webhook URL
    gateway_url = os.getenv("GATEWAY_URL", "http://localhost:9879")
    webhook_url = f"{gateway_url}/api/v1/{tenant.slug}/webhook"

    return WebhookConfigResponse(
        tenant_id=str(tenant.id),
        webhook_enabled=tenant.webhook_enabled or False,
        webhook_secret=tenant.webhook_secret,
        webhook_url=webhook_url,
    )


@app.post(
    "/admin/tenants/{tenant_id}/webhook/enable",
    response_model=WebhookConfigResponse,
    responses={404: {"model": ErrorResponse}},
)
@limiter.limit("10/minute")
async def enable_webhook(
    tenant_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
    _: bool = Depends(verify_admin_token),
    user_id: str = Depends(get_current_user_id),
):
    """Enable webhook and generate a new secret for a tenant."""
    service = TenantService(db)

    try:
        tenant = service.get_tenant(tenant_id)
    except TenantNotFoundError:
        raise HTTPException(status_code=404, detail="Tenant not found")

    # Check ownership
    if tenant.owner_id != user_id:
        raise HTTPException(status_code=403, detail="Not authorized to access this tenant")

    # Generate new secret if not exists
    if not tenant.webhook_secret:
        tenant.webhook_secret = generate_webhook_secret()

    tenant.webhook_enabled = True
    db.commit()

    # Build webhook URL
    gateway_url = os.getenv("GATEWAY_URL", "http://localhost:9879")
    webhook_url = f"{gateway_url}/api/v1/{tenant.slug}/webhook"

    logger.info(f"Webhook enabled for tenant {tenant.slug}")

    return WebhookConfigResponse(
        tenant_id=str(tenant.id),
        webhook_enabled=True,
        webhook_secret=tenant.webhook_secret,
        webhook_url=webhook_url,
    )


@app.post(
    "/admin/tenants/{tenant_id}/webhook/disable",
    response_model=WebhookConfigResponse,
    responses={404: {"model": ErrorResponse}},
)
async def disable_webhook(
    tenant_id: UUID,
    db: Session = Depends(get_db),
    _: bool = Depends(verify_admin_token),
    user_id: str = Depends(get_current_user_id),
):
    """Disable webhook for a tenant (keeps the secret)."""
    service = TenantService(db)

    try:
        tenant = service.get_tenant(tenant_id)
    except TenantNotFoundError:
        raise HTTPException(status_code=404, detail="Tenant not found")

    # Check ownership
    if tenant.owner_id != user_id:
        raise HTTPException(status_code=403, detail="Not authorized to access this tenant")

    tenant.webhook_enabled = False
    db.commit()

    # Build webhook URL
    gateway_url = os.getenv("GATEWAY_URL", "http://localhost:9879")
    webhook_url = f"{gateway_url}/api/v1/{tenant.slug}/webhook"

    logger.info(f"Webhook disabled for tenant {tenant.slug}")

    return WebhookConfigResponse(
        tenant_id=str(tenant.id),
        webhook_enabled=False,
        webhook_secret=tenant.webhook_secret,
        webhook_url=webhook_url,
    )


@app.post(
    "/admin/tenants/{tenant_id}/webhook/regenerate-secret",
    response_model=WebhookConfigResponse,
    responses={404: {"model": ErrorResponse}},
)
@limiter.limit("5/minute")
async def regenerate_webhook_secret(
    tenant_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
    _: bool = Depends(verify_admin_token),
    user_id: str = Depends(get_current_user_id),
):
    """Regenerate webhook secret for a tenant."""
    service = TenantService(db)

    try:
        tenant = service.get_tenant(tenant_id)
    except TenantNotFoundError:
        raise HTTPException(status_code=404, detail="Tenant not found")

    # Check ownership
    if tenant.owner_id != user_id:
        raise HTTPException(status_code=403, detail="Not authorized to access this tenant")

    # Generate new secret
    tenant.webhook_secret = generate_webhook_secret()
    db.commit()

    # Build webhook URL
    gateway_url = os.getenv("GATEWAY_URL", "http://localhost:9879")
    webhook_url = f"{gateway_url}/api/v1/{tenant.slug}/webhook"

    logger.info(f"Webhook secret regenerated for tenant {tenant.slug}")

    return WebhookConfigResponse(
        tenant_id=str(tenant.id),
        webhook_enabled=tenant.webhook_enabled or False,
        webhook_secret=tenant.webhook_secret,
        webhook_url=webhook_url,
    )


@app.get(
    "/admin/tenants/{tenant_id}/webhook/logs",
    response_model=List[WebhookLogResponse],
    responses={404: {"model": ErrorResponse}},
)
async def get_webhook_logs(
    tenant_id: UUID,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    status: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    _: bool = Depends(verify_admin_token),
    user_id: str = Depends(get_current_user_id),
):
    """Get webhook logs for a tenant."""
    service = TenantService(db)

    try:
        tenant = service.get_tenant(tenant_id)
    except TenantNotFoundError:
        raise HTTPException(status_code=404, detail="Tenant not found")

    # Check ownership
    if tenant.owner_id != user_id:
        raise HTTPException(status_code=403, detail="Not authorized to access this tenant")

    # Query webhook logs
    query = """
        SELECT id, tenant_id, source_ip, status, error_message,
               tv_alert_name, tv_ticker, tv_action, tv_quantity, tv_price,
               created_at, processed_at
        FROM webhook_logs
        WHERE tenant_id = :tenant_id
    """
    params = {"tenant_id": str(tenant.id)}

    if status:
        query += " AND status = :status"
        params["status"] = status

    query += " ORDER BY created_at DESC LIMIT :limit OFFSET :offset"
    params["limit"] = limit
    params["offset"] = offset

    result = db.execute(text(query), params)
    rows = result.fetchall()

    logs = []
    for row in rows:
        logs.append(WebhookLogResponse(
            id=row[0],
            tenant_id=str(row[1]),
            source_ip=row[2],
            status=row[3],
            error_message=row[4],
            tv_alert_name=row[5],
            tv_ticker=row[6],
            tv_action=row[7],
            tv_quantity=row[8],
            tv_price=float(row[9]) if row[9] else None,
            created_at=row[10].isoformat() if row[10] else None,
            processed_at=row[11].isoformat() if row[11] else None,
        ))

    return logs


# =============================================================================
# Entry point for standalone mode
# =============================================================================

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001)
