#!/usr/bin/env python3
"""
Database Utility Script

This script provides various database operations for manual management.

Usage:
    python scripts/db_utils.py stamp-existing    # Mark existing DB as migrated
    python scripts/db_utils.py ensure-columns    # Ensure all columns exist
    python scripts/db_utils.py show-schema       # Show current table schema
"""
import os
import sys

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import OperationalError, ProgrammingError

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/shioaji"
)


def get_engine():
    return create_engine(DATABASE_URL)


def stamp_existing_database():
    """
    For existing databases that were created before Alembic was set up.
    This stamps the database with the initial migration without running it.
    """
    import subprocess
    
    print("Stamping existing database with initial migration...")
    print("This marks the database as already at the initial schema.")
    
    result = subprocess.run(
        ["alembic", "stamp", "20251230_000001"],
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        env={**os.environ, "DATABASE_URL": DATABASE_URL}
    )
    
    if result.returncode == 0:
        print("✓ Database stamped successfully!")
    else:
        print("✗ Failed to stamp database")
        sys.exit(1)


def ensure_columns():
    """
    Ensure all expected columns exist in the order_history table.
    This is useful for manual fixes without full migrations.
    """
    engine = get_engine()
    
    expected_columns = {
        "id": "INTEGER",
        "symbol": "VARCHAR",
        "code": "VARCHAR",
        "action": "VARCHAR",
        "quantity": "INTEGER",
        "status": "VARCHAR",
        "order_result": "VARCHAR",
        "error_message": "VARCHAR",
        "created_at": "TIMESTAMP",
        "order_id": "VARCHAR",
        "seqno": "VARCHAR",
        "ordno": "VARCHAR",
        "fill_status": "VARCHAR",
        "fill_quantity": "INTEGER",
        "fill_price": "FLOAT",
        "cancel_quantity": "INTEGER",
        "updated_at": "TIMESTAMP",
    }
    
    with engine.connect() as conn:
        inspector = inspect(engine)
        
        if "order_history" not in inspector.get_table_names():
            print("✗ Table 'order_history' does not exist!")
            print("  Run migrations first: ./scripts/migrate.sh")
            sys.exit(1)
        
        existing_columns = {col["name"]: col["type"] for col in inspector.get_columns("order_history")}
        
        print("Checking columns in 'order_history' table...")
        
        missing = []
        for col_name, col_type in expected_columns.items():
            if col_name in existing_columns:
                print(f"  ✓ {col_name}")
            else:
                print(f"  ✗ {col_name} (missing)")
                missing.append((col_name, col_type))
        
        if missing:
            print(f"\nAdding {len(missing)} missing column(s)...")
            for col_name, col_type in missing:
                try:
                    conn.execute(text(f"ALTER TABLE order_history ADD COLUMN {col_name} {col_type}"))
                    conn.commit()
                    print(f"  ✓ Added column: {col_name}")
                except (OperationalError, ProgrammingError) as e:
                    if "already exists" in str(e).lower():
                        print(f"  ~ Column {col_name} already exists")
                    else:
                        print(f"  ✗ Failed to add {col_name}: {e}")
        else:
            print("\n✓ All columns present!")


def show_schema():
    """Show the current database schema for order_history table."""
    engine = get_engine()
    inspector = inspect(engine)
    
    if "order_history" not in inspector.get_table_names():
        print("Table 'order_history' does not exist!")
        return
    
    print("=== order_history table schema ===\n")
    
    columns = inspector.get_columns("order_history")
    print("Columns:")
    for col in columns:
        nullable = "NULL" if col.get("nullable", True) else "NOT NULL"
        default = f" DEFAULT {col['default']}" if col.get("default") else ""
        print(f"  {col['name']:20} {str(col['type']):15} {nullable}{default}")
    
    print("\nIndexes:")
    indexes = inspector.get_indexes("order_history")
    for idx in indexes:
        unique = "UNIQUE " if idx.get("unique") else ""
        print(f"  {unique}{idx['name']}: {idx['column_names']}")
    
    pk = inspector.get_pk_constraint("order_history")
    if pk:
        print(f"\nPrimary Key: {pk.get('constrained_columns', [])}")


def show_alembic_version():
    """Show current Alembic version in database."""
    engine = get_engine()
    
    try:
        with engine.connect() as conn:
            result = conn.execute(text("SELECT version_num FROM alembic_version"))
            versions = [row[0] for row in result]
            
            if versions:
                print("Current Alembic version(s):")
                for v in versions:
                    print(f"  {v}")
            else:
                print("No Alembic version found (database not migrated yet)")
    except (OperationalError, ProgrammingError):
        print("Alembic version table does not exist (database not migrated yet)")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    
    command = sys.argv[1]
    
    commands = {
        "stamp-existing": stamp_existing_database,
        "ensure-columns": ensure_columns,
        "show-schema": show_schema,
        "show-version": show_alembic_version,
    }
    
    if command in commands:
        commands[command]()
    else:
        print(f"Unknown command: {command}")
        print(f"Available commands: {', '.join(commands.keys())}")
        sys.exit(1)


if __name__ == "__main__":
    main()

