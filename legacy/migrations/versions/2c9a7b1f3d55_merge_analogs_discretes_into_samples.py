"""merge analogs/discretes into samples

Revision ID: 2c9a7b1f3d55
Revises: 6c80f66a1324
Create Date: 2026-04-07 12:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "2c9a7b1f3d55"
down_revision = "6c80f66a1324"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "samples",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("date", sa.LargeBinary(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.execute("INSERT INTO samples (created_at, date) SELECT created_at, date FROM analogs")
    op.drop_table("discretes")
    op.drop_table("analogs")


def downgrade() -> None:
    op.create_table(
        "analogs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("date", sa.LargeBinary(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "discretes",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("date", sa.LargeBinary(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.execute("INSERT INTO analogs (created_at, date) SELECT created_at, date FROM samples")
    op.execute("INSERT INTO discretes (created_at, date) SELECT created_at, date FROM samples")
    op.drop_table("samples")
