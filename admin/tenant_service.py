"""
Tenant CRUD service for multi-tenant management.
"""
import re
import secrets
import string
import logging
from datetime import datetime
from typing import Optional, List, Dict, Any
from uuid import UUID

from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from models.tenant import (
    Tenant,
    TenantStatus,
    PlanTier,
    TenantCredential,
    CredentialType,
    CredentialStatus,
    WorkerInstance,
    WorkerStatus,
    TenantAuditLog,
    AuditAction,
)
from credentials.encrypted_storage import get_credential_storage

logger = logging.getLogger(__name__)


class TenantServiceError(Exception):
    """Base exception for tenant service errors."""
    pass


class TenantNotFoundError(TenantServiceError):
    """Tenant not found."""
    pass


class TenantAlreadyExistsError(TenantServiceError):
    """Tenant with this slug already exists."""
    pass


class InvalidSlugError(TenantServiceError):
    """Invalid slug format."""
    pass


class TenantService:
    """
    Service for managing tenants and their credentials.
    """

    # Slug validation regex: lowercase alphanumeric and hyphens, 3-63 chars
    SLUG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$")

    # Random prefix length (e.g., "x7k9" = 4 chars)
    SLUG_PREFIX_LENGTH = 6

    def __init__(self, db: Session):
        self.db = db

    def _generate_slug_prefix(self) -> str:
        """
        Generate a cryptographically random prefix for slug.

        Uses lowercase letters and digits, avoiding confusing characters (0, o, l, 1).
        Example output: "x7k9m2"
        """
        # Safe alphabet: no 0/o/1/l confusion
        alphabet = "abcdefghjkmnpqrstuvwxyz23456789"
        return "".join(secrets.choice(alphabet) for _ in range(self.SLUG_PREFIX_LENGTH))

    def _generate_secure_slug(self, name: str) -> str:
        """
        Generate a secure slug from tenant name with random prefix.

        Example: "My Company" -> "x7k9m2-my-company"
        """
        # Normalize name to slug format
        base_slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")

        # Limit base slug length to leave room for prefix
        max_base_length = 63 - self.SLUG_PREFIX_LENGTH - 1  # -1 for hyphen
        if len(base_slug) > max_base_length:
            base_slug = base_slug[:max_base_length].rstrip("-")

        # Add random prefix
        prefix = self._generate_slug_prefix()
        return f"{prefix}-{base_slug}" if base_slug else prefix

    def _validate_slug(self, slug: str) -> None:
        """Validate tenant slug format."""
        if not self.SLUG_PATTERN.match(slug):
            raise InvalidSlugError(
                f"Invalid slug '{slug}'. Must be 3-63 characters, "
                "lowercase alphanumeric and hyphens, cannot start/end with hyphen."
            )

    def _log_audit(
        self,
        action: AuditAction,
        tenant_id: Optional[UUID] = None,
        actor_id: Optional[str] = None,
        actor_type: str = "api",
        ip_address: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Log an audit event."""
        log_entry = TenantAuditLog(
            tenant_id=tenant_id,
            action=action.value,
            actor_id=actor_id,
            actor_type=actor_type,
            ip_address=ip_address,
            details=details or {},
        )
        self.db.add(log_entry)

    # =========================================================================
    # Tenant CRUD
    # =========================================================================

    def create_tenant(
        self,
        owner_id: str,
        name: str,
        email: str,
        slug: Optional[str] = None,
        plan_tier: PlanTier = PlanTier.FREE,
        metadata: Optional[Dict[str, Any]] = None,
        actor_id: Optional[str] = None,
        ip_address: Optional[str] = None,
    ) -> Tenant:
        """
        Create a new tenant with secure auto-generated slug.

        Args:
            owner_id: User ID from auth provider (required for access control)
            name: Display name
            email: Contact email
            slug: Optional custom slug (will be prefixed with random string)
            plan_tier: Subscription tier
            metadata: Additional metadata
            actor_id: Who created the tenant
            ip_address: Request IP address

        Returns:
            Created Tenant object

        Raises:
            InvalidSlugError: If slug format is invalid
            TenantAlreadyExistsError: If slug already exists
        """
        if not owner_id:
            raise TenantServiceError("owner_id is required")

        # Generate secure slug with random prefix
        # If user provides slug, use it as base; otherwise use name
        base_name = slug if slug else name
        secure_slug = self._generate_secure_slug(base_name)

        # Validate the generated slug
        self._validate_slug(secure_slug)

        tenant = Tenant(
            owner_id=owner_id,
            name=name,
            slug=secure_slug,
            email=email,
            status=TenantStatus.PENDING.value,
            plan_tier=plan_tier.value,
            tenant_metadata=metadata or {},
        )

        try:
            self.db.add(tenant)
            self.db.flush()  # Get the ID

            self._log_audit(
                AuditAction.TENANT_CREATED,
                tenant_id=tenant.id,
                actor_id=actor_id,
                ip_address=ip_address,
                details={"name": name, "slug": secure_slug, "email": email, "owner_id": owner_id},
            )

            self.db.commit()
            logger.info(f"Created tenant: {secure_slug} ({tenant.id})")
            return tenant

        except IntegrityError:
            self.db.rollback()
            raise TenantAlreadyExistsError(f"Tenant with slug '{secure_slug}' already exists")

    def get_tenant(self, tenant_id: UUID, owner_id: Optional[str] = None) -> Tenant:
        """
        Get a tenant by ID.

        Args:
            tenant_id: Tenant UUID
            owner_id: If provided, verify the tenant belongs to this owner

        Raises:
            TenantNotFoundError: If tenant not found or not owned by owner_id
        """
        query = self.db.query(Tenant).filter(
            Tenant.id == tenant_id,
            Tenant.deleted_at.is_(None),
        )

        # If owner_id provided, enforce ownership check
        if owner_id:
            query = query.filter(Tenant.owner_id == owner_id)

        tenant = query.first()

        if not tenant:
            raise TenantNotFoundError(f"Tenant {tenant_id} not found")

        return tenant

    def verify_ownership(self, tenant_id: UUID, owner_id: str) -> bool:
        """
        Verify that a tenant belongs to the specified owner.

        Returns True if ownership is verified, raises TenantNotFoundError otherwise.
        """
        try:
            self.get_tenant(tenant_id, owner_id=owner_id)
            return True
        except TenantNotFoundError:
            return False

    def get_tenant_by_slug(self, slug: str) -> Tenant:
        """Get a tenant by slug."""
        tenant = self.db.query(Tenant).filter(
            Tenant.slug == slug,
            Tenant.deleted_at.is_(None),
        ).first()

        if not tenant:
            raise TenantNotFoundError(f"Tenant with slug '{slug}' not found")

        return tenant

    def list_tenants(
        self,
        owner_id: Optional[str] = None,
        status: Optional[TenantStatus] = None,
        plan_tier: Optional[PlanTier] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[Tenant]:
        """
        List tenants with optional filters.

        Args:
            owner_id: If provided, only return tenants owned by this user
            status: Filter by tenant status
            plan_tier: Filter by plan tier
            limit: Maximum number of results
            offset: Pagination offset
        """
        query = self.db.query(Tenant).filter(Tenant.deleted_at.is_(None))

        # Filter by owner if provided (for user-facing APIs)
        if owner_id:
            query = query.filter(Tenant.owner_id == owner_id)

        if status:
            query = query.filter(Tenant.status == status.value)
        if plan_tier:
            query = query.filter(Tenant.plan_tier == plan_tier.value)

        return query.order_by(Tenant.created_at.desc()).offset(offset).limit(limit).all()

    def update_tenant(
        self,
        tenant_id: UUID,
        name: Optional[str] = None,
        email: Optional[str] = None,
        status: Optional[TenantStatus] = None,
        plan_tier: Optional[PlanTier] = None,
        metadata: Optional[Dict[str, Any]] = None,
        actor_id: Optional[str] = None,
        ip_address: Optional[str] = None,
    ) -> Tenant:
        """Update a tenant."""
        tenant = self.get_tenant(tenant_id)

        updates = {}
        if name is not None:
            tenant.name = name
            updates["name"] = name
        if email is not None:
            tenant.email = email
            updates["email"] = email
        if status is not None:
            tenant.status = status.value
            updates["status"] = status.value
        if plan_tier is not None:
            tenant.plan_tier = plan_tier.value
            updates["plan_tier"] = plan_tier.value
        if metadata is not None:
            tenant.tenant_metadata = {**(tenant.tenant_metadata or {}), **metadata}
            updates["metadata"] = metadata

        tenant.updated_at = datetime.utcnow()

        self._log_audit(
            AuditAction.TENANT_UPDATED,
            tenant_id=tenant_id,
            actor_id=actor_id,
            ip_address=ip_address,
            details=updates,
        )

        self.db.commit()
        logger.info(f"Updated tenant: {tenant.slug}")
        return tenant

    def delete_tenant(
        self,
        tenant_id: UUID,
        actor_id: Optional[str] = None,
        ip_address: Optional[str] = None,
    ) -> None:
        """
        Soft delete a tenant.

        This marks the tenant as deleted but preserves the record.
        Credentials are securely deleted.
        """
        tenant = self.get_tenant(tenant_id)

        # Delete credentials
        try:
            storage = get_credential_storage()
            storage.delete_credentials(str(tenant_id))
        except Exception as e:
            logger.warning(f"Failed to delete credentials for {tenant_id}: {e}")

        # Soft delete
        tenant.status = TenantStatus.DELETED.value
        tenant.deleted_at = datetime.utcnow()

        self._log_audit(
            AuditAction.TENANT_DELETED,
            tenant_id=tenant_id,
            actor_id=actor_id,
            ip_address=ip_address,
        )

        self.db.commit()
        logger.info(f"Deleted tenant: {tenant.slug}")

    def activate_tenant(
        self,
        tenant_id: UUID,
        actor_id: Optional[str] = None,
        ip_address: Optional[str] = None,
    ) -> Tenant:
        """Activate a pending tenant."""
        return self.update_tenant(
            tenant_id,
            status=TenantStatus.ACTIVE,
            actor_id=actor_id,
            ip_address=ip_address,
        )

    def suspend_tenant(
        self,
        tenant_id: UUID,
        actor_id: Optional[str] = None,
        ip_address: Optional[str] = None,
    ) -> Tenant:
        """Suspend an active tenant."""
        return self.update_tenant(
            tenant_id,
            status=TenantStatus.SUSPENDED,
            actor_id=actor_id,
            ip_address=ip_address,
        )

    # =========================================================================
    # Credential Management
    # =========================================================================

    def upload_shioaji_credentials(
        self,
        tenant_id: UUID,
        api_key: str,
        secret_key: str,
        actor_id: Optional[str] = None,
        ip_address: Optional[str] = None,
    ) -> TenantCredential:
        """
        Upload Shioaji API credentials for a tenant.

        Args:
            tenant_id: Tenant UUID
            api_key: Shioaji API key
            secret_key: Shioaji secret key
            actor_id: Who uploaded the credentials
            ip_address: Request IP address

        Returns:
            TenantCredential record
        """
        tenant = self.get_tenant(tenant_id)
        storage = get_credential_storage()

        # Store encrypted credentials
        result = storage.store_shioaji_credentials(str(tenant_id), api_key, secret_key)

        # Create or update credential record
        credential = self.db.query(TenantCredential).filter(
            TenantCredential.tenant_id == tenant_id,
            TenantCredential.credential_type == CredentialType.SHIOAJI_API.value,
        ).first()

        if credential:
            credential.storage_path = result["storage_path"]
            credential.fingerprint = result["fingerprint"]
            credential.status = CredentialStatus.PENDING.value
            credential.updated_at = datetime.utcnow()
            credential.verified_at = None
        else:
            credential = TenantCredential(
                tenant_id=tenant_id,
                credential_type=CredentialType.SHIOAJI_API.value,
                storage_path=result["storage_path"],
                fingerprint=result["fingerprint"],
                status=CredentialStatus.PENDING.value,
            )
            self.db.add(credential)

        self._log_audit(
            AuditAction.CREDENTIAL_UPLOADED,
            tenant_id=tenant_id,
            actor_id=actor_id,
            ip_address=ip_address,
            details={"type": "shioaji_api", "fingerprint": result["fingerprint"]},
        )

        self.db.commit()
        logger.info(f"Uploaded Shioaji credentials for tenant: {tenant.slug}")
        return credential

    def upload_ca_certificate(
        self,
        tenant_id: UUID,
        ca_file: bytes,
        ca_password: str,
        actor_id: Optional[str] = None,
        ip_address: Optional[str] = None,
    ) -> TenantCredential:
        """
        Upload CA certificate for real trading.

        Args:
            tenant_id: Tenant UUID
            ca_file: CA certificate file content
            ca_password: CA certificate password
            actor_id: Who uploaded the certificate
            ip_address: Request IP address

        Returns:
            TenantCredential record
        """
        tenant = self.get_tenant(tenant_id)
        storage = get_credential_storage()

        # Store encrypted certificate
        result = storage.store_ca_certificate(str(tenant_id), ca_file, ca_password)

        # Create or update credential record
        credential = self.db.query(TenantCredential).filter(
            TenantCredential.tenant_id == tenant_id,
            TenantCredential.credential_type == CredentialType.CA_CERTIFICATE.value,
        ).first()

        if credential:
            credential.storage_path = result["storage_path"]
            credential.fingerprint = result["fingerprint"]
            credential.status = CredentialStatus.PENDING.value
            credential.updated_at = datetime.utcnow()
            credential.verified_at = None
        else:
            credential = TenantCredential(
                tenant_id=tenant_id,
                credential_type=CredentialType.CA_CERTIFICATE.value,
                storage_path=result["storage_path"],
                fingerprint=result["fingerprint"],
                status=CredentialStatus.PENDING.value,
            )
            self.db.add(credential)

        self._log_audit(
            AuditAction.CREDENTIAL_UPLOADED,
            tenant_id=tenant_id,
            actor_id=actor_id,
            ip_address=ip_address,
            details={"type": "ca_certificate", "fingerprint": result["fingerprint"]},
        )

        self.db.commit()
        logger.info(f"Uploaded CA certificate for tenant: {tenant.slug}")
        return credential

    def verify_credentials(
        self,
        tenant_id: UUID,
        credential_type: CredentialType,
        actor_id: Optional[str] = None,
        ip_address: Optional[str] = None,
    ) -> TenantCredential:
        """Mark credentials as verified (after successful Shioaji login)."""
        credential = self.db.query(TenantCredential).filter(
            TenantCredential.tenant_id == tenant_id,
            TenantCredential.credential_type == credential_type.value,
        ).first()

        if not credential:
            raise TenantServiceError(f"No {credential_type.value} credentials found")

        credential.status = CredentialStatus.VERIFIED.value
        credential.verified_at = datetime.utcnow()

        self._log_audit(
            AuditAction.CREDENTIAL_VERIFIED,
            tenant_id=tenant_id,
            actor_id=actor_id,
            ip_address=ip_address,
            details={"type": credential_type.value},
        )

        self.db.commit()
        return credential

    def revoke_credentials(
        self,
        tenant_id: UUID,
        credential_type: CredentialType,
        actor_id: Optional[str] = None,
        ip_address: Optional[str] = None,
    ) -> None:
        """Revoke and delete credentials."""
        tenant = self.get_tenant(tenant_id)
        storage = get_credential_storage()

        # Delete from storage
        if credential_type == CredentialType.SHIOAJI_API:
            # Just delete the specific file
            pass  # TODO: Add specific file deletion
        elif credential_type == CredentialType.CA_CERTIFICATE:
            pass

        # Update credential record
        credential = self.db.query(TenantCredential).filter(
            TenantCredential.tenant_id == tenant_id,
            TenantCredential.credential_type == credential_type.value,
        ).first()

        if credential:
            credential.status = CredentialStatus.REVOKED.value
            credential.updated_at = datetime.utcnow()

        self._log_audit(
            AuditAction.CREDENTIAL_REVOKED,
            tenant_id=tenant_id,
            actor_id=actor_id,
            ip_address=ip_address,
            details={"type": credential_type.value},
        )

        self.db.commit()
        logger.info(f"Revoked {credential_type.value} for tenant: {tenant.slug}")

    def get_credential_status(self, tenant_id: UUID) -> Dict[str, Any]:
        """
        Get credential status for a tenant.

        Returns format compatible with frontend CredentialStatus interface:
        - shioaji_api: Credential object or null
        - ca_certificate: Credential object or null
        - ready_for_trading: true if shioaji_api exists (simulation mode)
        - ready_for_real_trading: true if both credentials exist (real trading)
        """
        tenant = self.get_tenant(tenant_id)

        credentials = self.db.query(TenantCredential).filter(
            TenantCredential.tenant_id == tenant_id,
        ).all()

        # Build credential map by type
        cred_map = {c.credential_type: c.to_dict() for c in credentials}

        shioaji_api = cred_map.get(CredentialType.SHIOAJI_API.value)
        ca_certificate = cred_map.get(CredentialType.CA_CERTIFICATE.value)

        return {
            "shioaji_api": shioaji_api,
            "ca_certificate": ca_certificate,
            "ready_for_trading": shioaji_api is not None,
            "ready_for_real_trading": shioaji_api is not None and ca_certificate is not None,
        }

    # =========================================================================
    # Worker Instance Management
    # =========================================================================

    def get_worker_instance(self, tenant_id: UUID) -> Optional[WorkerInstance]:
        """Get worker instance for a tenant."""
        return self.db.query(WorkerInstance).filter(
            WorkerInstance.tenant_id == tenant_id,
        ).first()

    def create_worker_instance(
        self,
        tenant_id: UUID,
        redis_db: int,
    ) -> WorkerInstance:
        """Create a worker instance record."""
        tenant = self.get_tenant(tenant_id)

        instance = WorkerInstance(
            tenant_id=tenant_id,
            redis_db=redis_db,
            status=WorkerStatus.PENDING.value,
        )

        self.db.add(instance)
        self.db.commit()

        return instance

    def update_worker_instance(
        self,
        tenant_id: UUID,
        container_id: Optional[str] = None,
        container_name: Optional[str] = None,
        status: Optional[WorkerStatus] = None,
        error_message: Optional[str] = None,
    ) -> WorkerInstance:
        """Update worker instance status."""
        instance = self.get_worker_instance(tenant_id)
        if not instance:
            raise TenantServiceError(f"No worker instance for tenant {tenant_id}")

        if container_id is not None:
            instance.container_id = container_id
        if container_name is not None:
            instance.container_name = container_name
        if status is not None:
            instance.status = status.value
            if status == WorkerStatus.RUNNING:
                instance.started_at = datetime.utcnow()
            elif status in (WorkerStatus.STOPPED, WorkerStatus.ERROR):
                instance.stopped_at = datetime.utcnow()
        if error_message is not None:
            instance.error_message = error_message

        instance.updated_at = datetime.utcnow()
        self.db.commit()

        return instance

    def allocate_redis_db(self) -> int:
        """
        Allocate an available Redis database number (0-15).

        Returns the lowest available number, or raises an error if all are used.
        """
        used_dbs = set(
            row[0] for row in self.db.query(WorkerInstance.redis_db).filter(
                WorkerInstance.status.in_([
                    WorkerStatus.PENDING.value,
                    WorkerStatus.STARTING.value,
                    WorkerStatus.RUNNING.value,
                    WorkerStatus.HIBERNATING.value,
                ])
            ).all()
        )

        for db_num in range(16):
            if db_num not in used_dbs:
                return db_num

        raise TenantServiceError("No available Redis database slots (max 15 tenants)")

    def release_redis_db(self, tenant_id: UUID) -> None:
        """Release the Redis database allocation for a tenant."""
        instance = self.get_worker_instance(tenant_id)
        if instance:
            self.db.delete(instance)
            self.db.commit()
