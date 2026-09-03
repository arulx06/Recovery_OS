"""add experiment run provenance

Revision ID: b2c3d4e5f6a7b8
Revises: a1b2c3d4e5f6
Create Date: 2026-09-04

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'b2c3d4e5f6a7b8'
down_revision: Union[str, Sequence[str], None] = 'a1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'experiment_runs',
        sa.Column('run_id', sa.String(), nullable=False),
        sa.Column('resolved_seed', sa.Integer(), nullable=False),
        sa.Column('scenario_count', sa.Integer(), nullable=False),
        sa.Column('model_version', sa.String(), nullable=True),
        sa.Column('model_fingerprint', sa.String(), nullable=True),
        sa.Column('feature_schema_version', sa.String(), nullable=True),
        sa.Column('adaptive_profile', sa.String(), nullable=True),
        sa.Column('friction_weight', sa.Numeric(precision=6, scale=3), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('run_id')
    )


def downgrade() -> None:
    op.drop_table('experiment_runs')
