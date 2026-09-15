"""add alarm state and event logs

Revision ID: a1f4d2e9b7c3
Revises: 0b94798d81c5
Create Date: 2026-04-08 20:20:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "a1f4d2e9b7c3"
down_revision = "0b94798d81c5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("alarms", schema=None) as batch_op:
        batch_op.add_column(sa.Column("state", sa.String(length=32), nullable=False, server_default="active"))

    op.create_table(
        "event_logs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("level", sa.String(length=32), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("message", sa.String(length=255), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("event_logs")
    with op.batch_alter_table("alarms", schema=None) as batch_op:
        batch_op.drop_column("state")
