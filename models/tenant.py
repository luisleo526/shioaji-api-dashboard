"""
Multi-tenant models for Managed Dedicated Backend architecture.
"""
from datetime import datetime
from typing import Optional, Dict, Any
from uuid import uuid4
import enum

from sqlalchemy import (
    Column, String, DateTime, Integer, Text, ForeignKey, UniqueConstraint, Boolean
)
from sqlalchemy.dialects.postgresql import UUID, JSONB, INET
from sqlalchemy.orm import relationship

from database import Base


# ============================================================================
# Enums
# ============================================================================

class TenantStatus(str, enum.Enum):
    PENDING = "pending"
    ACTIVE = "active"
    SUSPENDED = "suspended"
    DELETED = "deleted"


class PlanTier(str, enum.Enum):
    FREE = "free"
    PRO = "pro"
    BUSINESS = "business"


class CredentialType(str, enum.Enum):
    SHIOAJI_API = "shioaji_api"
    CA_CERTIFICATE = "ca_certificate"


class CredentialStatus(str, enum.Enum):
    PENDING = "pending"
    VERIFIED = "verified"
    EXPIRED = "expired"
    REVOKED = "revoked"


class WorkerStatus(str, enum.Enum):
    PENDING = "pending"
    STARTING = "starting"
    RUNNING = "running"
    HIBERNATING = "hibernating"
    STOPPING = "stopping"
    STOPPED = "stopped"
    ERROR = "error"


class HealthStatus(str, enum.Enum):
    UNKNOWN = "unknown"
    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"
    DEGRADED = "degraded"


class AuditAction(str, enum.Enum):
    TENANT_CREATED = "tenant_created"
    TENANT_UPDATED = "tenant_updated"
    TENANT_DELETED = "tenant_deleted"
    CREDENTIAL_UPLOADED = "credential_uploaded"
    CREDENTIAL_VERIFIED = "credential_verified"
    CREDENTIAL_REVOKED = "credential_revoked"
    WORKER_STARTED = "worker_started"
    WORKER_STOPPED = "worker_stopped"
    WORKER_HIBERNATED = "worker_hibernated"
    WORKER_WOKEN = "worker_woken"
    WORKER_ERROR = "worker_error"


# ============================================================================
# Models
# ============================================================================

class Tenant(Base):
    """Core tenant information."""
    __tablename__ = "tenants"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    owner_id = Column(String(255), nullable=False, index=True)  # Supabase user ID or external auth ID
    name = Column(String(255), nullable=False)
    slug = Column(String(63), unique=True, nullable=False, index=True)
    email = Column(String(255), nullable=False, index=True)
    status = Column(String(20), nullable=False, default=TenantStatus.PENDING.value)
    plan_tier = Column(String(20), nullable=False, default=PlanTier.FREE.value)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, onupdate=datetime.utcnow)
    deleted_at = Column(DateTime, nullable=True)
    tenant_metadata = Column(JSONB, default=dict)  # 'metadata' is reserved by SQLAlchemy

    # Webhook settings
    webhook_enabled = Column(Boolean, default=False)
    webhook_secret = Column(String(64), nullable=True)

    # Relationships
    credentials = relationship("TenantCredential", back_populates="tenant", cascade="all, delete-orphan")
    worker = relationship("WorkerInstance", back_populates="tenant", uselist=False, cascade="all, delete-orphan")
    audit_logs = relationship("TenantAuditLog", back_populates="tenant")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": str(self.id),
            "owner_id": self.owner_id,
            "name": self.name,
            "slug": self.slug,
            "email": self.email,
            "status": self.status,
            "plan_tier": self.plan_tier,
            "webhook_enabled": self.webhook_enabled or False,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "metadata": self.tenant_metadata or {},
        }

    def is_active(self) -> bool:
        return self.status == TenantStatus.ACTIVE.value


class TenantCredential(Base):
    """Credential reference (not storing actual credentials)."""
    __tablename__ = "tenant_credentials"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    credential_type = Column(String(50), nullable=False)
    storage_path = Column(String(255), nullable=False)  # Path to encrypted storage
    fingerprint = Column(String(64), nullable=True)  # SHA256 hash for verification
    status = Column(String(20), nullable=False, default=CredentialStatus.PENDING.value)
    expires_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, onupdate=datetime.utcnow)
    verified_at = Column(DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint("tenant_id", "credential_type", name="uq_tenant_credential_type"),
    )

    # Relationships
    tenant = relationship("Tenant", back_populates="credentials")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": str(self.id),
            "tenant_id": str(self.tenant_id),
            "credential_type": self.credential_type,
            "status": self.status,
            "fingerprint": self.fingerprint,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "verified_at": self.verified_at.isoformat() if self.verified_at else None,
        }

    def is_valid(self) -> bool:
        if self.status != CredentialStatus.VERIFIED.value:
            return False
        if self.expires_at and self.expires_at < datetime.utcnow():
            return False
        return True


class WorkerInstance(Base):
    """Worker instance tracking."""
    __tablename__ = "worker_instances"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, unique=True)
    container_id = Column(String(64), nullable=True)
    container_name = Column(String(255), nullable=True)
    status = Column(String(20), nullable=False, default=WorkerStatus.PENDING.value)
    redis_db = Column(Integer, nullable=False)  # 0-15
    internal_port = Column(Integer, nullable=True)
    health_status = Column(String(20), default=HealthStatus.UNKNOWN.value)
    last_health_check = Column(DateTime, nullable=True)
    started_at = Column(DateTime, nullable=True)
    stopped_at = Column(DateTime, nullable=True)
    error_message = Column(Text, nullable=True)
    resource_usage = Column(JSONB, default=dict)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, onupdate=datetime.utcnow)

    # Relationships
    tenant = relationship("Tenant", back_populates="worker")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": str(self.id),
            "tenant_id": str(self.tenant_id),
            "container_id": self.container_id,
            "container_name": self.container_name,
            "status": self.status,
            "redis_db": self.redis_db,
            "health_status": self.health_status,
            "last_health_check": self.last_health_check.isoformat() if self.last_health_check else None,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "stopped_at": self.stopped_at.isoformat() if self.stopped_at else None,
            "error_message": self.error_message,
            "resource_usage": self.resource_usage or {},
        }

    def is_running(self) -> bool:
        return self.status == WorkerStatus.RUNNING.value


class TenantAuditLog(Base):
    """Audit log for security-sensitive operations."""
    __tablename__ = "tenant_audit_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="SET NULL"), nullable=True)
    action = Column(String(50), nullable=False, index=True)
    actor_id = Column(String(255), nullable=True)
    actor_type = Column(String(20), nullable=True)  # admin/system/api
    ip_address = Column(INET, nullable=True)
    details = Column(JSONB, default=dict)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)

    # Relationships
    tenant = relationship("Tenant", back_populates="audit_logs")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "tenant_id": str(self.tenant_id) if self.tenant_id else None,
            "action": self.action,
            "actor_id": self.actor_id,
            "actor_type": self.actor_type,
            "ip_address": str(self.ip_address) if self.ip_address else None,
            "details": self.details or {},
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
