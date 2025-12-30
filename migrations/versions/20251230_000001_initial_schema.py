"""Initial schema - order_history table

Revision ID: 20251230_000001
Revises: 
Create Date: 2025-12-30

This migration creates the initial order_history table schema.
For existing databases, use the stamp command to mark as current:
    alembic stamp 20251230_000001
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

# revision identifiers, used by Alembic.
revision: str = "20251230_000001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def table_exists(table_name: str) -> bool:
    """Check if a table exists in the database."""
    bind = op.get_bind()
    inspector = inspect(bind)
    return table_name in inspector.get_table_names()


def upgrade() -> None:
    # Skip if table already exists (for existing databases)
    if table_exists("order_history"):
        print("Table 'order_history' already exists, skipping creation")
        return
    
    op.create_table(
        "order_history",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("symbol", sa.String(), nullable=False),
        sa.Column("code", sa.String(), nullable=True),
        sa.Column("action", sa.String(), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("order_result", sa.String(), nullable=True),
        sa.Column("error_message", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("order_id", sa.String(), nullable=True),
        sa.Column("seqno", sa.String(), nullable=True),
        sa.Column("ordno", sa.String(), nullable=True),
        sa.Column("fill_status", sa.String(), nullable=True),
        sa.Column("fill_quantity", sa.Integer(), nullable=True),
        sa.Column("fill_price", sa.Float(), nullable=True),
        sa.Column("cancel_quantity", sa.Integer(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_order_history_code"), "order_history", ["code"], unique=False)
    op.create_index(op.f("ix_order_history_created_at"), "order_history", ["created_at"], unique=False)
    op.create_index(op.f("ix_order_history_id"), "order_history", ["id"], unique=False)
    op.create_index(op.f("ix_order_history_order_id"), "order_history", ["order_id"], unique=False)
    op.create_index(op.f("ix_order_history_symbol"), "order_history", ["symbol"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_order_history_symbol"), table_name="order_history")
    op.drop_index(op.f("ix_order_history_order_id"), table_name="order_history")
    op.drop_index(op.f("ix_order_history_id"), table_name="order_history")
    op.drop_index(op.f("ix_order_history_created_at"), table_name="order_history")
    op.drop_index(op.f("ix_order_history_code"), table_name="order_history")
    op.drop_table("order_history")

