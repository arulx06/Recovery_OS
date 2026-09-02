"""add payment link id to revenue cases

Revision ID: d3e3dbb702c4
Revises: 709600d2e2b1
Create Date: 2026-08-22 08:14:12.889303

Autogenerate was run against a throwaway SQLite DB (no Postgres available
in this environment) and proposed a large batch of spurious `alter_column`
type changes on every UUID primary/foreign key — SQLite reflects
UUID(as_uuid=False) columns back as generic NUMERIC on introspection, which
Alembic then reads as a type diff against Postgres-flavored metadata. None
of that reflects an intended change; the only real change here is the new
column. Hand-trimmed to just that.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd3e3dbb702c4'
down_revision: Union[str, Sequence[str], None] = '709600d2e2b1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('revenue_cases', sa.Column('razorpay_payment_link_id', sa.String(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('revenue_cases', 'razorpay_payment_link_id')
