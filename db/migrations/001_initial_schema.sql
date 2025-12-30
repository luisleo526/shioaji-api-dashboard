-- Initial schema: order_history table
-- Version: 001

CREATE TABLE IF NOT EXISTS order_history (
    id SERIAL PRIMARY KEY,
    symbol VARCHAR NOT NULL,
    code VARCHAR,
    action VARCHAR NOT NULL,
    quantity INTEGER NOT NULL,
    status VARCHAR NOT NULL,
    order_result VARCHAR,
    error_message VARCHAR,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    order_id VARCHAR,
    seqno VARCHAR,
    ordno VARCHAR,
    fill_status VARCHAR,
    fill_quantity INTEGER,
    fill_price FLOAT,
    cancel_quantity INTEGER,
    updated_at TIMESTAMP
);

-- Create indexes
CREATE INDEX IF NOT EXISTS ix_order_history_id ON order_history (id);
CREATE INDEX IF NOT EXISTS ix_order_history_symbol ON order_history (symbol);
CREATE INDEX IF NOT EXISTS ix_order_history_code ON order_history (code);
CREATE INDEX IF NOT EXISTS ix_order_history_created_at ON order_history (created_at);
CREATE INDEX IF NOT EXISTS ix_order_history_order_id ON order_history (order_id);

