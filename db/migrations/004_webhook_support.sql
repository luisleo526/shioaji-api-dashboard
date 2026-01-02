-- Migration: Add webhook support for TradingView integration
-- Date: 2025-01-02

-- Add webhook_secret to tenants table (may already exist)
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS webhook_secret VARCHAR(64);
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS webhook_enabled BOOLEAN DEFAULT false;

-- Create webhook_logs table
CREATE TABLE IF NOT EXISTS webhook_logs (
    id SERIAL PRIMARY KEY,
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,

    -- Request info
    source_ip VARCHAR(45),
    request_body JSONB,
    headers JSONB,

    -- Processing result
    status VARCHAR(20) NOT NULL DEFAULT 'received',  -- received, validated, processed, failed
    error_message TEXT,

    -- Parsed TradingView data
    tv_alert_name VARCHAR(255),
    tv_ticker VARCHAR(50),
    tv_action VARCHAR(20),  -- buy, sell, long, short, exit, etc.
    tv_quantity INTEGER,
    tv_price DECIMAL(18, 4),

    -- Order result
    order_id INTEGER REFERENCES order_history(id),

    -- Timestamps
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    processed_at TIMESTAMP WITH TIME ZONE
);

-- Index for fast lookups
CREATE INDEX IF NOT EXISTS idx_webhook_logs_tenant_id ON webhook_logs(tenant_id);
CREATE INDEX IF NOT EXISTS idx_webhook_logs_created_at ON webhook_logs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_webhook_logs_status ON webhook_logs(status);

-- Comment
COMMENT ON TABLE webhook_logs IS 'TradingView webhook request logs for audit and debugging';
COMMENT ON COLUMN tenants.webhook_secret IS 'Secret token for validating TradingView webhook requests';
COMMENT ON COLUMN tenants.webhook_enabled IS 'Whether webhook is enabled for this tenant';
