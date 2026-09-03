"""add llm provenance

Revision ID: c9d0e1f2a3b4
Revises: a1b2c3d4e5f6
Create Date: 2026-09-04

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "c9d0e1f2a3b4"
down_revision: Union[str, Sequence[str], None] = "b2c3d4e5f6a7b8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("promises_to_pay") as batch_op:
        batch_op.add_column(sa.Column("extraction_method", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("llm_provider", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("llm_model", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("prompt_version", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("schema_version", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("amount_method", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("reasoning_code", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("source_message_id", sa.String(), nullable=True))

    with op.batch_alter_table("customer_messages") as batch_op:
        batch_op.add_column(sa.Column("generation_method", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("llm_provider", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("llm_model", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("prompt_version", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("schema_version", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("status", sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("customer_messages") as batch_op:
        batch_op.drop_column("status")
        batch_op.drop_column("schema_version")
        batch_op.drop_column("prompt_version")
        batch_op.drop_column("llm_model")
        batch_op.drop_column("llm_provider")
        batch_op.drop_column("generation_method")

    with op.batch_alter_table("promises_to_pay") as batch_op:
        batch_op.drop_column("source_message_id")
        batch_op.drop_column("reasoning_code")
        batch_op.drop_column("amount_method")
        batch_op.drop_column("schema_version")
        batch_op.drop_column("prompt_version")
        batch_op.drop_column("llm_model")
        batch_op.drop_column("llm_provider")
        batch_op.drop_column("extraction_method")
