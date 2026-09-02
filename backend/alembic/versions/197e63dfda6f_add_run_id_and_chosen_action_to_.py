"""add run_id and chosen_action to experiment_cases

Revision ID: 197e63dfda6f
Revises: d3e3dbb702c4
Create Date: 2026-08-24 09:04:44.423240

Note: autogenerate also proposed a large batch of NUMERIC -> UUID
op.alter_column calls across nearly every table. That's SQLite reflecting
its own generic column affinity back at us, not a real schema drift —
same false positive already noted in d3e3dbb702c4. Stripped down to the
actual change: three new columns + an index on experiment_cases.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '197e63dfda6f'
down_revision: Union[str, Sequence[str], None] = 'd3e3dbb702c4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('experiment_cases', sa.Column('run_id', sa.String(), nullable=False, server_default=''))
    op.add_column('experiment_cases', sa.Column('failure_category', sa.String(), nullable=True))
    op.add_column('experiment_cases', sa.Column('chosen_action', sa.String(), nullable=True))
    op.create_index(op.f('ix_experiment_cases_run_id'), 'experiment_cases', ['run_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_experiment_cases_run_id'), table_name='experiment_cases')
    op.drop_column('experiment_cases', 'chosen_action')
    op.drop_column('experiment_cases', 'failure_category')
    op.drop_column('experiment_cases', 'run_id')
