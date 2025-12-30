#!/bin/bash
set -e

# Database Migration Entrypoint Script
# 
# This script handles database migrations using Alembic.
# It waits for the database to be ready, then runs migrations.
#
# Usage:
#   ./scripts/migrate.sh              # Run all pending migrations (upgrade head)
#   ./scripts/migrate.sh upgrade head # Run all pending migrations
#   ./scripts/migrate.sh upgrade +1   # Run next migration
#   ./scripts/migrate.sh downgrade -1 # Rollback one migration
#   ./scripts/migrate.sh current      # Show current revision
#   ./scripts/migrate.sh history      # Show migration history
#   ./scripts/migrate.sh stamp <rev>  # Mark database at revision without running migrations

echo "=== Database Migration Service ==="
echo "DATABASE_URL: ${DATABASE_URL:-not set}"

# Wait for database to be ready
wait_for_db() {
    echo "Waiting for database to be ready..."
    
    local max_attempts=30
    local attempt=1
    
    while [ $attempt -le $max_attempts ]; do
        if python -c "
from sqlalchemy import create_engine, text
import os
try:
    engine = create_engine(os.environ['DATABASE_URL'])
    with engine.connect() as conn:
        conn.execute(text('SELECT 1'))
    print('Database is ready!')
    exit(0)
except Exception as e:
    print(f'Attempt $attempt/$max_attempts: {e}')
    exit(1)
" 2>/dev/null; then
            return 0
        fi
        
        echo "Database not ready, waiting... (attempt $attempt/$max_attempts)"
        sleep 2
        attempt=$((attempt + 1))
    done
    
    echo "ERROR: Database not available after $max_attempts attempts"
    exit 1
}

# Run migrations
run_migrations() {
    local command="${1:-upgrade}"
    local target="${2:-head}"
    
    case "$command" in
        upgrade|downgrade)
            echo "Running: alembic $command $target"
            alembic "$command" "$target"
            ;;
        current)
            echo "Current migration revision:"
            alembic current
            ;;
        history)
            echo "Migration history:"
            alembic history --verbose
            ;;
        stamp)
            if [ -z "$target" ] || [ "$target" = "head" ]; then
                echo "ERROR: stamp requires a specific revision"
                echo "Usage: ./migrate.sh stamp <revision>"
                exit 1
            fi
            echo "Stamping database at revision: $target"
            alembic stamp "$target"
            ;;
        heads)
            echo "Available migration heads:"
            alembic heads
            ;;
        branches)
            echo "Migration branches:"
            alembic branches
            ;;
        *)
            echo "Unknown command: $command"
            echo "Available commands: upgrade, downgrade, current, history, stamp, heads, branches"
            exit 1
            ;;
    esac
}

# Main
cd /app

wait_for_db

if [ $# -eq 0 ]; then
    # Default: run all pending migrations
    run_migrations upgrade head
else
    run_migrations "$@"
fi

echo "=== Migration complete ==="

