-- Migration: Add tenant ownership and secure slug generation
-- Description: 
--   1. Add owner_id column to track tenant ownership
--   2. Update existing test tenant with a placeholder owner_id

-- Add owner_id column (with default for existing rows)
ALTER TABLE tenants 
ADD COLUMN IF NOT EXISTS owner_id VARCHAR(255);

-- Set default for existing rows
UPDATE tenants 
SET owner_id = 'system-migration' 
WHERE owner_id IS NULL;

-- Make it NOT NULL after setting defaults
ALTER TABLE tenants 
ALTER COLUMN owner_id SET NOT NULL;

-- Add index for owner lookup
CREATE INDEX IF NOT EXISTS ix_tenants_owner_id ON tenants(owner_id);

-- Comment
COMMENT ON COLUMN tenants.owner_id IS 'User ID from auth provider (Supabase, etc.) - used for access control';
