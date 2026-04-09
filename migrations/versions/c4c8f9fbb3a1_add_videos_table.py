"""add videos table

Revision ID: c4c8f9fbb3a1
Revises: a1f4d2e9b7c3
Create Date: 2026-04-09 16:30:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "c4c8f9fbb3a1"
down_revision = "a1f4d2e9b7c3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "videos",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("captured_at", sa.DateTime(), nullable=False),
        sa.Column("file_path", sa.String(length=1024), nullable=False),
        sa.Column("file_name", sa.String(length=255), nullable=False),
        sa.Column("alarm_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["alarm_id"], ["alarms.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("file_path"),
    )


def downgrade() -> None:
    op.drop_table("videos")
