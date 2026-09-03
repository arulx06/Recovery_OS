"""add temporal action runtime

Revision ID: 8d6e24f91a73
Revises: 197e63dfda6f
Create Date: 2026-09-03 00:00:00.000000
"""
from typing import Sequence, Union
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from alembic import op
import sqlalchemy as sa
from app.core.config import settings


revision: str = "8d6e24f91a73"
down_revision: Union[str, Sequence[str], None] = "197e63dfda6f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _event_provider_ids(event_type: str, payload: dict | None) -> tuple[str | None, str | None, str | None]:
    wrappers = (payload or {}).get("payload", {})
    if event_type == "subscription.halted":
        entity = wrappers.get("subscription", {}).get("entity", {}) or {}
    elif event_type == "payment.dispute.created":
        entity = wrappers.get("dispute", {}).get("entity", {}) or {}
        if not entity:
            entity = wrappers.get("payment", {}).get("entity", {}) or {}
    else:
        entity = wrappers.get("payment", {}).get("entity", {}) or {}
    payment_id = entity.get("id") if entity.get("entity") == "payment" else entity.get("payment_id")
    subscription_id = (
        entity.get("id") if entity.get("entity") == "subscription" else entity.get("subscription_id")
    )
    link_id = None
    if event_type == "payment_link.paid":
        link_id = wrappers.get("payment_link", {}).get("entity", {}).get("id")
    return payment_id, subscription_id, link_id


def upgrade() -> None:
    connection = op.get_bind()
    revenue_cases = sa.table(
        "revenue_cases",
        sa.column("source", sa.String()),
        sa.column("razorpay_payment_id", sa.String()),
    )
    duplicate_payment_id = connection.execute(
        sa.select(revenue_cases.c.razorpay_payment_id)
        .where(
            revenue_cases.c.source == "razorpay",
            revenue_cases.c.razorpay_payment_id.is_not(None),
        )
        .group_by(revenue_cases.c.razorpay_payment_id)
        .having(sa.func.count() > 1)
        .limit(1)
    ).first()
    if duplicate_payment_id:
        raise RuntimeError(
            "cannot add Razorpay payment-id uniqueness: resolve duplicate live cases first"
        )

    with op.batch_alter_table("actions") as batch_op:
        batch_op.add_column(sa.Column("promise_to_pay_id", sa.UUID(as_uuid=False), nullable=True))
        batch_op.add_column(sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"))
        batch_op.add_column(sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"))
        batch_op.add_column(sa.Column("claimed_at", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column("enqueued_at", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column("queue_job_id", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("last_error", sa.Text(), nullable=True))

    with op.batch_alter_table("payment_events") as batch_op:
        batch_op.add_column(sa.Column("razorpay_payment_id", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("razorpay_subscription_id", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("razorpay_payment_link_id", sa.String(), nullable=True))
        batch_op.create_index("ix_payment_events_razorpay_payment_id", ["razorpay_payment_id"])
        batch_op.create_index("ix_payment_events_razorpay_subscription_id", ["razorpay_subscription_id"])
        batch_op.create_index("ix_payment_events_razorpay_payment_link_id", ["razorpay_payment_link_id"])

    payment_events = sa.table(
        "payment_events",
        sa.column("id", sa.UUID(as_uuid=False)),
        sa.column("event_type", sa.String()),
        sa.column("raw_payload", sa.JSON()),
        sa.column("razorpay_payment_id", sa.String()),
        sa.column("razorpay_subscription_id", sa.String()),
        sa.column("razorpay_payment_link_id", sa.String()),
    )
    for event_id, event_type, raw_payload in connection.execute(
        sa.select(payment_events.c.id, payment_events.c.event_type, payment_events.c.raw_payload)
    ).all():
        payment_id, subscription_id, link_id = _event_provider_ids(event_type, raw_payload)
        connection.execute(
            sa.update(payment_events)
            .where(payment_events.c.id == event_id)
            .values(
                razorpay_payment_id=payment_id,
                razorpay_subscription_id=subscription_id,
                razorpay_payment_link_id=link_id,
            )
        )

    actions = sa.table(
        "actions",
        sa.column("id", sa.UUID(as_uuid=False)),
        sa.column("revenue_case_id", sa.UUID(as_uuid=False)),
        sa.column("promise_to_pay_id", sa.UUID(as_uuid=False)),
        sa.column("action_type", sa.String()),
        sa.column("status", sa.String()),
        sa.column("scheduled_for", sa.DateTime()),
    )
    promises = sa.table(
        "promises_to_pay",
        sa.column("id", sa.UUID(as_uuid=False)),
        sa.column("revenue_case_id", sa.UUID(as_uuid=False)),
        sa.column("promised_date", sa.DateTime()),
        sa.column("status", sa.String()),
    )
    pending_for_case = (
        sa.select(promises.c.id)
        .where(
            promises.c.revenue_case_id == actions.c.revenue_case_id,
            promises.c.status == "PENDING",
        )
        .limit(1)
        .scalar_subquery()
    )
    pending_count = (
        sa.select(sa.func.count())
        .select_from(promises)
        .where(
            promises.c.revenue_case_id == actions.c.revenue_case_id,
            promises.c.status == "PENDING",
        )
        .scalar_subquery()
    )
    connection.execute(
        sa.update(actions)
        .where(
            actions.c.action_type == "FOLLOW_UP_PTP",
            actions.c.promise_to_pay_id.is_(None),
            pending_count == 1,
        )
        .values(promise_to_pay_id=pending_for_case)
    )

    linked_promises = connection.execute(
        sa.select(actions.c.id, promises.c.promised_date)
        .join(promises, promises.c.id == actions.c.promise_to_pay_id)
        .where(actions.c.action_type == "FOLLOW_UP_PTP", actions.c.status == "SCHEDULED")
    ).all()
    merchant_zone = ZoneInfo(settings.MERCHANT_TIMEZONE)
    for action_id, promised_date in linked_promises:
        deadline = datetime.combine(
            promised_date.date() + timedelta(days=1), time.min, merchant_zone,
        ).astimezone(UTC).replace(tzinfo=None)
        connection.execute(
            sa.update(actions).where(actions.c.id == action_id).values(scheduled_for=deadline)
        )

    with op.batch_alter_table("actions") as batch_op:
        batch_op.create_foreign_key(
            "fk_actions_promise_to_pay_id",
            "promises_to_pay",
            ["promise_to_pay_id"],
            ["id"],
        )
        batch_op.create_index(
            "ix_actions_status_scheduled_for",
            ["status", "scheduled_for"],
            unique=False,
        )

    op.create_index(
        "uq_revenue_cases_razorpay_payment_id",
        "revenue_cases",
        ["razorpay_payment_id"],
        unique=True,
        postgresql_where=sa.text("source = 'razorpay' AND razorpay_payment_id IS NOT NULL"),
        sqlite_where=sa.text("source = 'razorpay' AND razorpay_payment_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_revenue_cases_razorpay_payment_id", table_name="revenue_cases")
    with op.batch_alter_table("actions") as batch_op:
        batch_op.drop_index("ix_actions_status_scheduled_for")
        batch_op.drop_constraint("fk_actions_promise_to_pay_id", type_="foreignkey")
        batch_op.drop_column("last_error")
        batch_op.drop_column("queue_job_id")
        batch_op.drop_column("enqueued_at")
        batch_op.drop_column("claimed_at")
        batch_op.drop_column("max_attempts")
        batch_op.drop_column("attempt_count")
        batch_op.drop_column("promise_to_pay_id")
    with op.batch_alter_table("payment_events") as batch_op:
        batch_op.drop_index("ix_payment_events_razorpay_payment_link_id")
        batch_op.drop_index("ix_payment_events_razorpay_subscription_id")
        batch_op.drop_index("ix_payment_events_razorpay_payment_id")
        batch_op.drop_column("razorpay_payment_link_id")
        batch_op.drop_column("razorpay_subscription_id")
        batch_op.drop_column("razorpay_payment_id")
