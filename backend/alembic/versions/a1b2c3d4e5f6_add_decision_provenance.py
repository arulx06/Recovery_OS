"""add decision provenance

Revision ID: a1b2c3d4e5f6
Revises: 1a980d6
Create Date: 2026-09-04

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, Sequence[str], None] = '8d6e24f91a73'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("decisions") as batch_op:
        batch_op.add_column(sa.Column("policy_mode", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("model_version", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("model_fingerprint", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("friction_profile", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("friction_weight", sa.Numeric(precision=6, scale=3), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("decisions") as batch_op:
        batch_op.drop_column("friction_weight")
        batch_op.drop_column("friction_profile")
        batch_op.drop_column("model_fingerprint")
        batch_op.drop_column("model_version")
        batch_op.drop_column("policy_mode")
