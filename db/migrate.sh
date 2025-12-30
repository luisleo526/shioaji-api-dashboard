#!/bin/bash
set -e

# Database Migration Runner
# Runs SQL migrations in order, tracking which have been applied
#
# This script is designed to run AFTER PostgreSQL is ready.
# It should be called from the db-migrate service.

MIGRATIONS_DIR="${MIGRATIONS_DIR:-/migrations}"
DATABASE="${POSTGRES_DB:-shioaji}"
USER="${POSTGRES_USER:-postgres}"

echo "=== Database Migration Runner ==="
echo "Database: $DATABASE"
echo "Migrations dir: $MIGRATIONS_DIR"

# Wait for PostgreSQL to be ready
wait_for_postgres() {
    echo "Waiting for PostgreSQL..."
    local max_attempts=30
    local attempt=1
    
    while [ $attempt -le $max_attempts ]; do
        if pg_isready -U "$USER" -d "$DATABASE" > /dev/null 2>&1; then
            echo "PostgreSQL is ready!"
            return 0
        fi
        echo "  Attempt $attempt/$max_attempts..."
        sleep 1
        attempt=$((attempt + 1))
    done
    
    echo "ERROR: PostgreSQL not ready after $max_attempts attempts"
    exit 1
}

# Run a single migration file
run_migration() {
    local file="$1"
    local version=$(basename "$file" .sql)
    
    # Check if already applied
    local applied=$(psql -U "$USER" -d "$DATABASE" -tAc \
        "SELECT 1 FROM schema_migrations WHERE version = '$version'" 2>/dev/null || echo "")
    
    if [ "$applied" = "1" ]; then
        echo "  ✓ $version (already applied)"
        return 0
    fi
    
    echo "  → Applying: $version"
    
    # Run the migration
    if psql -U "$USER" -d "$DATABASE" -f "$file" > /dev/null 2>&1; then
        # Record it as applied
        psql -U "$USER" -d "$DATABASE" -c \
            "INSERT INTO schema_migrations (version) VALUES ('$version')" > /dev/null 2>&1
        echo "  ✓ $version (applied)"
    else
        echo "  ✗ $version (FAILED)"
        exit 1
    fi
}

# Main migration logic
run_migrations() {
    # First ensure the schema_migrations table exists
    local init_file="$MIGRATIONS_DIR/000_schema_migrations.sql"
    if [ -f "$init_file" ]; then
        psql -U "$USER" -d "$DATABASE" -f "$init_file" > /dev/null 2>&1 || true
    fi
    
    echo "Running migrations..."
    
    # Get all .sql files sorted by name
    for file in $(ls "$MIGRATIONS_DIR"/*.sql 2>/dev/null | sort); do
        run_migration "$file"
    done
    
    echo ""
    echo "=== Migration complete ==="
    
    # Show current state
    echo ""
    echo "Applied migrations:"
    psql -U "$USER" -d "$DATABASE" -c \
        "SELECT version, applied_at FROM schema_migrations ORDER BY version" 2>/dev/null || echo "  (none)"
}

# Main
wait_for_postgres
run_migrations

