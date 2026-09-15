"""add alarms_raspberry table

Revision ID: 9d2a1c4f7a01
Revises: c4c8f9fbb3a1
Create Date: 2026-04-30 11:15:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "9d2a1c4f7a01"
down_revision = "c4c8f9fbb3a1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "alarms_raspberry",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("ended_at", sa.DateTime(), nullable=True),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("bcm_pin", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("trigger_level", sa.Integer(), nullable=False),
        sa.Column("hold_sec", sa.Float(), nullable=False),
        sa.Column("description", sa.String(length=255), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("alarms_raspberry")

