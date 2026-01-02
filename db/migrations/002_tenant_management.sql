-- Tenant management schema for multi-tenant architecture
-- Version: 002

-- Enable UUID extension if not already enabled
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- Tenants table: core tenant information
CREATE TABLE IF NOT EXISTS tenants (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(255) NOT NULL,
    slug VARCHAR(63) UNIQUE NOT NULL,  -- URL-safe identifier for routing
    email VARCHAR(255) NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'pending',  -- pending/active/suspended/deleted
    plan_tier VARCHAR(20) NOT NULL DEFAULT 'free',  -- free/pro/business
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP,
    deleted_at TIMESTAMP,
    tenant_metadata JSONB DEFAULT '{}'::jsonb  -- 'metadata' is reserved by SQLAlchemy
);

CREATE INDEX IF NOT EXISTS ix_tenants_slug ON tenants (slug);
CREATE INDEX IF NOT EXISTS ix_tenants_status ON tenants (status);
CREATE INDEX IF NOT EXISTS ix_tenants_email ON tenants (email);

-- Tenant credentials reference table (NOT storing actual credentials)
CREATE TABLE IF NOT EXISTS tenant_credentials (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    credential_type VARCHAR(50) NOT NULL,  -- shioaji_api / ca_certificate
    storage_path VARCHAR(255) NOT NULL,    -- Path to encrypted storage
    fingerprint VARCHAR(64),               -- SHA256 hash for verification
    status VARCHAR(20) NOT NULL DEFAULT 'pending',  -- pending/verified/expired/revoked
    expires_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP,
    verified_at TIMESTAMP,
    UNIQUE(tenant_id, credential_type)
);

CREATE INDEX IF NOT EXISTS ix_tenant_credentials_tenant ON tenant_credentials (tenant_id);
CREATE INDEX IF NOT EXISTS ix_tenant_credentials_status ON tenant_credentials (status);

-- Worker instances tracking table
CREATE TABLE IF NOT EXISTS worker_instances (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    container_id VARCHAR(64),
    container_name VARCHAR(255),
    status VARCHAR(20) NOT NULL DEFAULT 'pending',  -- pending/starting/running/hibernating/stopping/stopped/error
    redis_db INT NOT NULL,                          -- Redis database number (0-15)
    internal_port INT,
    health_status VARCHAR(20) DEFAULT 'unknown',    -- unknown/healthy/unhealthy/degraded
    last_health_check TIMESTAMP,
    started_at TIMESTAMP,
    stopped_at TIMESTAMP,
    error_message TEXT,
    resource_usage JSONB DEFAULT '{}'::jsonb,       -- CPU, memory stats
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP,
    UNIQUE(tenant_id)  -- One worker per tenant
);

CREATE INDEX IF NOT EXISTS ix_worker_instances_tenant ON worker_instances (tenant_id);
CREATE INDEX IF NOT EXISTS ix_worker_instances_status ON worker_instances (status);
CREATE INDEX IF NOT EXISTS ix_worker_instances_container ON worker_instances (container_id);

-- Audit log for security-sensitive operations
CREATE TABLE IF NOT EXISTS tenant_audit_log (
    id BIGSERIAL PRIMARY KEY,
    tenant_id UUID REFERENCES tenants(id) ON DELETE SET NULL,
    action VARCHAR(50) NOT NULL,        -- credential_uploaded, worker_started, etc.
    actor_id VARCHAR(255),              -- Who performed the action
    actor_type VARCHAR(20),             -- admin/system/api
    ip_address INET,
    details JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS ix_audit_log_tenant ON tenant_audit_log (tenant_id);
CREATE INDEX IF NOT EXISTS ix_audit_log_action ON tenant_audit_log (action);
CREATE INDEX IF NOT EXISTS ix_audit_log_created ON tenant_audit_log (created_at);

-- Add tenant_id to existing order_history table
ALTER TABLE order_history ADD COLUMN IF NOT EXISTS tenant_id UUID REFERENCES tenants(id);
CREATE INDEX IF NOT EXISTS ix_order_history_tenant ON order_history (tenant_id);
