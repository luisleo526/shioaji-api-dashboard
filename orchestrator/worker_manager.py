"""
Worker Manager for orchestrating per-tenant trading worker containers.

Uses Docker SDK to dynamically create, start, stop, and destroy worker containers.
Each tenant gets an isolated container with their own Shioaji credentials.
"""
import os
import logging
import asyncio
import tempfile
import shutil
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, Any
from uuid import UUID

import docker
from docker.errors import NotFound, APIError
from sqlalchemy.orm import Session

from database import SessionLocal
from models.tenant import (
    Tenant,
    TenantCredential,
    WorkerInstance,
    WorkerStatus,
    HealthStatus,
    CredentialType,
    CredentialStatus,
    TenantAuditLog,
    AuditAction,
)
from credentials.encrypted_storage import get_credential_storage
from admin.tenant_service import TenantService, TenantNotFoundError, TenantServiceError

logger = logging.getLogger(__name__)


class WorkerManagerError(Exception):
    """Base exception for worker manager errors."""
    pass


class WorkerNotFoundError(WorkerManagerError):
    """Worker not found."""
    pass


class WorkerAlreadyRunningError(WorkerManagerError):
    """Worker is already running."""
    pass


class CredentialsNotFoundError(WorkerManagerError):
    """Credentials not found for tenant."""
    pass


class WorkerManager:
    """
    Manages per-tenant trading worker containers.

    Lifecycle:
    1. create_worker() - Allocate Redis DB, create container, inject credentials
    2. start_worker() - Start the container
    3. stop_worker() - Stop the container (preserves state)
    4. hibernate_worker() - Stop but keep allocation
    5. wake_worker() - Restart hibernating container
    6. destroy_worker() - Remove container and release resources
    """

    # Container configuration
    DOCKER_NETWORK = os.getenv("DOCKER_NETWORK", "shioaji-network")
    WORKER_IMAGE = os.getenv("WORKER_IMAGE", "shioaji-worker:latest")
    REDIS_HOST = os.getenv("REDIS_HOST", "redis")
    REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))

    # Resource limits (per container)
    MEMORY_LIMIT = os.getenv("WORKER_MEMORY_LIMIT", "256m")
    CPU_QUOTA = int(os.getenv("WORKER_CPU_QUOTA", "50000"))  # 50% of one CPU

    def __init__(self, db: Optional[Session] = None):
        """
        Initialize the worker manager.

        Args:
            db: Optional database session (creates new if not provided)
        """
        self._db = db
        self._own_db = db is None
        self._docker = docker.from_env()
        self._temp_dirs: Dict[str, Path] = {}

        # Ensure network exists
        self._ensure_network()

    def _get_db(self) -> Session:
        """Get database session."""
        if self._db is None:
            self._db = SessionLocal()
        return self._db

    def _close_db(self) -> None:
        """Close database session if we own it."""
        if self._own_db and self._db is not None:
            self._db.close()
            self._db = None

    def _ensure_network(self) -> None:
        """Ensure Docker network exists."""
        try:
            self._docker.networks.get(self.DOCKER_NETWORK)
        except NotFound:
            logger.info(f"Creating Docker network: {self.DOCKER_NETWORK}")
            self._docker.networks.create(self.DOCKER_NETWORK, driver="bridge")

    def _get_container_name(self, tenant_slug: str) -> str:
        """Generate container name for a tenant."""
        return f"worker-{tenant_slug}"

    def _log_audit(
        self,
        db: Session,
        action: AuditAction,
        tenant_id: UUID,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Log an audit event."""
        log_entry = TenantAuditLog(
            tenant_id=tenant_id,
            action=action.value,
            actor_type="system",
            details=details or {},
        )
        db.add(log_entry)

    # =========================================================================
    # Container Lifecycle
    # =========================================================================

    async def create_worker(
        self,
        tenant_id: UUID,
        build_image: bool = False,
    ) -> WorkerInstance:
        """
        Create a new worker container for a tenant.

        This allocates a Redis DB, prepares credentials, and creates the container
        (but does not start it).

        Args:
            tenant_id: Tenant UUID
            build_image: Whether to build the image first

        Returns:
            WorkerInstance record

        Raises:
            TenantNotFoundError: If tenant doesn't exist
            CredentialsNotFoundError: If credentials aren't uploaded
            WorkerAlreadyRunningError: If worker already exists
        """
        db = self._get_db()

        try:
            service = TenantService(db)
            tenant = service.get_tenant(tenant_id)

            # Check if worker already exists
            existing = service.get_worker_instance(tenant_id)
            if existing and existing.status not in (WorkerStatus.STOPPED.value, WorkerStatus.ERROR.value):
                raise WorkerAlreadyRunningError(f"Worker for tenant {tenant.slug} already exists")

            # Check credentials
            shioaji_cred = db.query(TenantCredential).filter(
                TenantCredential.tenant_id == tenant_id,
                TenantCredential.credential_type == CredentialType.SHIOAJI_API.value,
            ).first()

            if not shioaji_cred:
                raise CredentialsNotFoundError(f"Shioaji credentials not found for tenant {tenant.slug}")

            # Allocate Redis DB
            redis_db = service.allocate_redis_db()

            # Create or update worker instance
            if existing:
                existing.redis_db = redis_db
                existing.status = WorkerStatus.PENDING.value
                existing.error_message = None
                existing.updated_at = datetime.utcnow()
                instance = existing
            else:
                instance = service.create_worker_instance(tenant_id, redis_db)

            # Prepare credentials for injection
            storage = get_credential_storage()
            cred_files = storage.export_for_worker(str(tenant_id))

            if not cred_files:
                raise CredentialsNotFoundError(f"Failed to export credentials for tenant {tenant.slug}")

            # Get the host path for volume mount
            # The admin-api container has HOST_SECRETS_DIR env var pointing to host path
            # e.g., /Users/xxx/project/secrets
            host_base = os.getenv("HOST_SECRETS_DIR")
            if host_base:
                host_secrets_path = os.path.join(host_base, str(tenant_id))
            else:
                # Fallback: try to use local path (for non-containerized runs)
                host_secrets_path = os.path.abspath(
                    os.path.join(os.getcwd(), "secrets", str(tenant_id))
                )

            logger.info(f"Using host secrets path: {host_secrets_path}")
            self._temp_dirs[str(tenant_id)] = Path(host_secrets_path)

            # Create container
            container_name = self._get_container_name(tenant.slug)

            # Environment variables
            env = {
                "TENANT_ID": str(tenant_id),
                "TENANT_SLUG": tenant.slug,
                "REDIS_URL": f"redis://{self.REDIS_HOST}:{self.REDIS_PORT}/{redis_db}",
                "API_KEY_FILE": "/run/secrets/api_key",
                "SECRET_KEY_FILE": "/run/secrets/secret_key",
            }

            # Add CA if exists
            if "ca_file" in cred_files:
                env["CA_PATH_FILE"] = "/run/secrets/ca.pfx"
                env["CA_PASSWORD_FILE"] = "/run/secrets/ca_password"

            # Volume mounts (for secrets) - use host path
            # The secrets are written to ./secrets/{tenant_id}/ on the host
            volumes = {
                host_secrets_path: {"bind": "/run/secrets", "mode": "ro"},
            }

            # Create container
            try:
                container = self._docker.containers.create(
                    image=self.WORKER_IMAGE,
                    name=container_name,
                    command="python trading_worker.py",
                    environment=env,
                    volumes=volumes,
                    network=self.DOCKER_NETWORK,
                    mem_limit=self.MEMORY_LIMIT,
                    cpu_quota=self.CPU_QUOTA,
                    restart_policy={"Name": "unless-stopped"},
                    labels={
                        "tenant.id": str(tenant_id),
                        "tenant.slug": tenant.slug,
                        "managed-by": "worker-manager",
                    },
                )

                # Update instance with container info
                instance.container_id = container.id
                instance.container_name = container_name
                instance.status = WorkerStatus.PENDING.value

            except APIError as e:
                instance.status = WorkerStatus.ERROR.value
                instance.error_message = str(e)
                logger.error(f"Failed to create container for {tenant.slug}: {e}")
                raise WorkerManagerError(f"Failed to create container: {e}")

            self._log_audit(db, AuditAction.WORKER_STARTED, tenant_id, {
                "container_id": instance.container_id,
                "redis_db": redis_db,
            })

            db.commit()
            logger.info(f"Created worker container for tenant: {tenant.slug}")
            return instance

        except (TenantNotFoundError, CredentialsNotFoundError, WorkerAlreadyRunningError):
            raise
        except Exception as e:
            db.rollback()
            logger.error(f"Error creating worker: {e}")
            raise WorkerManagerError(f"Failed to create worker: {e}")
        finally:
            if self._own_db:
                self._close_db()

    async def start_worker(self, tenant_id: UUID) -> WorkerInstance:
        """
        Start a worker container.

        Args:
            tenant_id: Tenant UUID

        Returns:
            Updated WorkerInstance
        """
        db = self._get_db()

        try:
            service = TenantService(db)
            tenant = service.get_tenant(tenant_id)
            instance = service.get_worker_instance(tenant_id)

            if not instance:
                raise WorkerNotFoundError(f"No worker instance for tenant {tenant.slug}")

            if instance.status == WorkerStatus.RUNNING.value:
                return instance

            # Get container
            try:
                container = self._docker.containers.get(instance.container_id)
                container.start()

                instance.status = WorkerStatus.RUNNING.value
                instance.started_at = datetime.utcnow()
                instance.stopped_at = None
                instance.error_message = None

            except NotFound:
                # Container was removed, need to recreate
                instance.status = WorkerStatus.ERROR.value
                instance.error_message = "Container not found, needs recreation"
                raise WorkerNotFoundError("Container not found, call create_worker first")

            except APIError as e:
                instance.status = WorkerStatus.ERROR.value
                instance.error_message = str(e)
                raise WorkerManagerError(f"Failed to start container: {e}")

            self._log_audit(db, AuditAction.WORKER_STARTED, tenant_id)
            db.commit()

            logger.info(f"Started worker for tenant: {tenant.slug}")
            return instance

        finally:
            if self._own_db:
                self._close_db()

    async def stop_worker(self, tenant_id: UUID) -> WorkerInstance:
        """
        Stop a worker container.

        Args:
            tenant_id: Tenant UUID

        Returns:
            Updated WorkerInstance
        """
        db = self._get_db()

        try:
            service = TenantService(db)
            tenant = service.get_tenant(tenant_id)
            instance = service.get_worker_instance(tenant_id)

            if not instance:
                raise WorkerNotFoundError(f"No worker instance for tenant {tenant.slug}")

            if instance.status in (WorkerStatus.STOPPED.value, WorkerStatus.PENDING.value):
                return instance

            # Get and stop container
            try:
                container = self._docker.containers.get(instance.container_id)
                container.stop(timeout=10)

                instance.status = WorkerStatus.STOPPED.value
                instance.stopped_at = datetime.utcnow()

            except NotFound:
                instance.status = WorkerStatus.STOPPED.value
                instance.stopped_at = datetime.utcnow()

            except APIError as e:
                instance.status = WorkerStatus.ERROR.value
                instance.error_message = str(e)
                raise WorkerManagerError(f"Failed to stop container: {e}")

            self._log_audit(db, AuditAction.WORKER_STOPPED, tenant_id)
            db.commit()

            logger.info(f"Stopped worker for tenant: {tenant.slug}")
            return instance

        finally:
            if self._own_db:
                self._close_db()

    async def hibernate_worker(self, tenant_id: UUID) -> WorkerInstance:
        """
        Hibernate a worker (stop but keep allocation).

        Args:
            tenant_id: Tenant UUID

        Returns:
            Updated WorkerInstance
        """
        db = self._get_db()

        try:
            service = TenantService(db)
            tenant = service.get_tenant(tenant_id)
            instance = service.get_worker_instance(tenant_id)

            if not instance:
                raise WorkerNotFoundError(f"No worker instance for tenant {tenant.slug}")

            # Stop container but mark as hibernating
            try:
                container = self._docker.containers.get(instance.container_id)
                container.stop(timeout=10)
            except NotFound:
                pass

            instance.status = WorkerStatus.HIBERNATING.value
            instance.stopped_at = datetime.utcnow()

            self._log_audit(db, AuditAction.WORKER_HIBERNATED, tenant_id)
            db.commit()

            logger.info(f"Hibernated worker for tenant: {tenant.slug}")
            return instance

        finally:
            if self._own_db:
                self._close_db()

    async def wake_worker(self, tenant_id: UUID) -> WorkerInstance:
        """
        Wake a hibernating worker.

        Args:
            tenant_id: Tenant UUID

        Returns:
            Updated WorkerInstance
        """
        db = self._get_db()

        try:
            service = TenantService(db)
            tenant = service.get_tenant(tenant_id)
            instance = service.get_worker_instance(tenant_id)

            if not instance:
                raise WorkerNotFoundError(f"No worker instance for tenant {tenant.slug}")

            if instance.status != WorkerStatus.HIBERNATING.value:
                raise WorkerManagerError(f"Worker is not hibernating (status: {instance.status})")

            # Start container
            try:
                container = self._docker.containers.get(instance.container_id)
                container.start()

                instance.status = WorkerStatus.RUNNING.value
                instance.started_at = datetime.utcnow()
                instance.stopped_at = None

            except NotFound:
                # Need to recreate
                raise WorkerNotFoundError("Container not found, call create_worker first")

            except APIError as e:
                instance.status = WorkerStatus.ERROR.value
                instance.error_message = str(e)
                raise WorkerManagerError(f"Failed to wake container: {e}")

            self._log_audit(db, AuditAction.WORKER_WOKEN, tenant_id)
            db.commit()

            logger.info(f"Woke worker for tenant: {tenant.slug}")
            return instance

        finally:
            if self._own_db:
                self._close_db()

    async def destroy_worker(self, tenant_id: UUID) -> None:
        """
        Destroy a worker container and release resources.

        Args:
            tenant_id: Tenant UUID
        """
        db = self._get_db()

        try:
            service = TenantService(db)
            tenant = service.get_tenant(tenant_id)
            instance = service.get_worker_instance(tenant_id)

            if not instance:
                return  # Nothing to destroy

            # Remove container
            try:
                container = self._docker.containers.get(instance.container_id)
                container.remove(force=True)
            except NotFound:
                pass

            # Clean up temp directory
            if str(tenant_id) in self._temp_dirs:
                temp_dir = self._temp_dirs.pop(str(tenant_id))
                if temp_dir.exists():
                    shutil.rmtree(temp_dir)

            # Release Redis DB
            service.release_redis_db(tenant_id)

            logger.info(f"Destroyed worker for tenant: {tenant.slug}")

        finally:
            if self._own_db:
                self._close_db()

    # =========================================================================
    # Health Monitoring
    # =========================================================================

    async def check_worker_health(self, tenant_id: UUID) -> HealthStatus:
        """
        Check the health of a worker.

        Args:
            tenant_id: Tenant UUID

        Returns:
            HealthStatus enum
        """
        db = self._get_db()

        try:
            service = TenantService(db)
            instance = service.get_worker_instance(tenant_id)

            if not instance:
                return HealthStatus.UNKNOWN

            if instance.status != WorkerStatus.RUNNING.value:
                return HealthStatus.UNKNOWN

            # Check container
            try:
                container = self._docker.containers.get(instance.container_id)

                if container.status == "running":
                    # TODO: Add Redis ping check for trading queue health
                    health = HealthStatus.HEALTHY
                else:
                    health = HealthStatus.UNHEALTHY

            except NotFound:
                health = HealthStatus.UNHEALTHY

            # Update instance
            instance.health_status = health.value
            instance.last_health_check = datetime.utcnow()
            db.commit()

            return health

        finally:
            if self._own_db:
                self._close_db()

    async def get_worker_logs(
        self,
        tenant_id: UUID,
        tail: int = 100,
    ) -> str:
        """
        Get logs from a worker container.

        Args:
            tenant_id: Tenant UUID
            tail: Number of lines to return

        Returns:
            Log content as string
        """
        db = self._get_db()

        try:
            service = TenantService(db)
            instance = service.get_worker_instance(tenant_id)

            if not instance or not instance.container_id:
                return ""

            try:
                container = self._docker.containers.get(instance.container_id)
                logs = container.logs(tail=tail, timestamps=True)
                return logs.decode("utf-8")
            except NotFound:
                return "Container not found"

        finally:
            if self._own_db:
                self._close_db()

    # =========================================================================
    # Bulk Operations
    # =========================================================================

    async def list_all_workers(self) -> list:
        """List all worker containers."""
        containers = self._docker.containers.list(
            all=True,
            filters={"label": "managed-by=worker-manager"},
        )

        return [
            {
                "id": c.id[:12],
                "name": c.name,
                "status": c.status,
                "tenant_id": c.labels.get("tenant.id"),
                "tenant_slug": c.labels.get("tenant.slug"),
            }
            for c in containers
        ]

    async def stop_all_workers(self) -> int:
        """Stop all worker containers. Returns count of stopped containers."""
        containers = self._docker.containers.list(
            filters={"label": "managed-by=worker-manager"},
        )

        for c in containers:
            c.stop(timeout=10)

        return len(containers)

    async def cleanup_orphaned_containers(self) -> int:
        """Remove containers for deleted tenants. Returns count removed."""
        db = self._get_db()

        try:
            containers = self._docker.containers.list(
                all=True,
                filters={"label": "managed-by=worker-manager"},
            )

            removed = 0
            for c in containers:
                tenant_id = c.labels.get("tenant.id")
                if tenant_id:
                    instance = db.query(WorkerInstance).filter(
                        WorkerInstance.tenant_id == tenant_id,
                    ).first()

                    if not instance:
                        c.remove(force=True)
                        removed += 1

            return removed

        finally:
            if self._own_db:
                self._close_db()
