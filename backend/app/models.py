"""
Day 1 schema lock.

These tables exist so migrations can run and the dashboard shell has
something real to point at. Columns get filled in as each phase needs
them (Phase 1: payment_events; Phase 2: failure taxonomy fields on
revenue_cases; Phase 3: decisions/actions; Phase 6: promises_to_pay /
customer_messages; Phase 7: experiment_cases).

Nothing here is a migration substitute — Alembic owns schema changes
from Phase 1 onward. This file is the source of truth for model shape.
"""
import uuid

from sqlalchemy import (
    Column, String, Integer, Numeric, DateTime, ForeignKey, JSON, Boolean, Text, Index, text
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.core.database import Base
from app.core.time import utc_now


def gen_uuid():
    return str(uuid.uuid4())


class Customer(Base):
    __tablename__ = "customers"

    id = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    external_ref = Column(String, nullable=True)  # e.g. Razorpay customer id
    name = Column(String, nullable=True)
    contact_channel = Column(String, nullable=True)  # email/phone, simulated
    created_at = Column(DateTime, default=utc_now)

    revenue_cases = relationship("RevenueCase", back_populates="customer")


class RevenueCase(Base):
    """One failed payment / overdue receivable being tracked for recovery."""
    __tablename__ = "revenue_cases"
    __table_args__ = (
        Index(
            "uq_revenue_cases_razorpay_payment_id",
            "razorpay_payment_id",
            unique=True,
            postgresql_where=text("source = 'razorpay' AND razorpay_payment_id IS NOT NULL"),
            sqlite_where=text("source = 'razorpay' AND razorpay_payment_id IS NOT NULL"),
        ),
    )

    id = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    customer_id = Column(UUID(as_uuid=False), ForeignKey("customers.id"), nullable=True)

    source = Column(String, nullable=False, default="razorpay")  # razorpay | synthetic
    razorpay_payment_id = Column(String, nullable=True)
    razorpay_subscription_id = Column(String, nullable=True)
    # Set when a CREATE_PAYMENT_LINK action is executed (Phase 4). The
    # payment eventually made through that link gets its own, different
    # razorpay_payment_id — so payment_link.paid webhooks are matched
    # against this column instead.
    razorpay_payment_link_id = Column(String, nullable=True)

    amount = Column(Numeric(12, 2), nullable=False)
    currency = Column(String, default="INR")

    # filled in Phase 2 (failure intelligence)
    failure_category = Column(String, nullable=True)
    error_source = Column(String, nullable=True)
    error_step = Column(String, nullable=True)
    error_reason = Column(String, nullable=True)

    state = Column(String, nullable=False, default="DETECTED")
    # DETECTED -> DIAGNOSED -> DECISION_READY -> WAITING / ACTION_SCHEDULED
    # -> AWAITING_OUTCOME -> RECOVERED / STOPPED / DISPUTED / HUMAN_REVIEW

    created_at = Column(DateTime, default=utc_now)
    updated_at = Column(DateTime, default=utc_now, onupdate=utc_now)

    customer = relationship("Customer", back_populates="revenue_cases")
    events = relationship("PaymentEvent", back_populates="revenue_case")
    decisions = relationship("Decision", back_populates="revenue_case")
    actions = relationship("Action", back_populates="revenue_case")
    promises = relationship("PromiseToPay", back_populates="revenue_case")
    audit_events = relationship("AuditEvent", back_populates="revenue_case")


class PaymentEvent(Base):
    """Immutable log of every incoming Razorpay webhook event. Never overwritten."""
    __tablename__ = "payment_events"

    id = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    revenue_case_id = Column(UUID(as_uuid=False), ForeignKey("revenue_cases.id"), nullable=True)

    razorpay_event_id = Column(String, unique=True, nullable=True)  # x-razorpay-event-id, for idempotency
    event_type = Column(String, nullable=False)  # payment.failed, payment.captured, etc.
    razorpay_payment_id = Column(String, nullable=True, index=True)
    razorpay_subscription_id = Column(String, nullable=True, index=True)
    razorpay_payment_link_id = Column(String, nullable=True, index=True)
    raw_payload = Column(JSON, nullable=True)

    received_at = Column(DateTime, default=utc_now)

    revenue_case = relationship("RevenueCase", back_populates="events")


class Decision(Base):
    """Every policy decision made for a case. Append-only — never overwritten."""
    __tablename__ = "decisions"

    id = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    revenue_case_id = Column(UUID(as_uuid=False), ForeignKey("revenue_cases.id"), nullable=False)

    chosen_action = Column(String, nullable=True)
    expected_value = Column(Numeric(12, 2), nullable=True)
    alternatives = Column(JSON, nullable=True)  # {action: score, ...}
    guardrails_applied = Column(JSON, nullable=True)
    explanation = Column(Text, nullable=True)
    # Provenance for live adaptive dispatch (nullable for legacy baseline rows)
    policy_mode = Column(String, nullable=True)  # baseline | adaptive | shadow | adaptive_fallback
    model_version = Column(String, nullable=True)
    model_fingerprint = Column(String, nullable=True)
    friction_profile = Column(String, nullable=True)
    friction_weight = Column(Numeric(6, 3), nullable=True)

    created_at = Column(DateTime, default=utc_now)

    revenue_case = relationship("RevenueCase", back_populates="decisions")


class Action(Base):
    """What was actually scheduled / executed as a result of a decision."""
    __tablename__ = "actions"
    __table_args__ = (Index("ix_actions_status_scheduled_for", "status", "scheduled_for"),)

    id = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    revenue_case_id = Column(UUID(as_uuid=False), ForeignKey("revenue_cases.id"), nullable=False)
    decision_id = Column(UUID(as_uuid=False), ForeignKey("decisions.id"), nullable=True)
    promise_to_pay_id = Column(UUID(as_uuid=False), ForeignKey("promises_to_pay.id"), nullable=True)

    action_type = Column(String, nullable=False)  # WAIT, CREATE_PAYMENT_LINK, CONTACT_CUSTOMER, ...
    status = Column(String, default="SCHEDULED")  # SCHEDULED | EXECUTING | EXECUTED | CANCELLED | FAILED
    scheduled_for = Column(DateTime, nullable=True)
    executed_at = Column(DateTime, nullable=True)
    result = Column(JSON, nullable=True)
    attempt_count = Column(Integer, nullable=False, default=0)
    max_attempts = Column(Integer, nullable=False, default=3)
    claimed_at = Column(DateTime, nullable=True)
    enqueued_at = Column(DateTime, nullable=True)
    queue_job_id = Column(String, nullable=True)
    last_error = Column(Text, nullable=True)

    created_at = Column(DateTime, default=utc_now)

    revenue_case = relationship("RevenueCase", back_populates="actions")


class PromiseToPay(Base):
    __tablename__ = "promises_to_pay"

    id = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    revenue_case_id = Column(UUID(as_uuid=False), ForeignKey("revenue_cases.id"), nullable=False)

    promised_amount = Column(Numeric(12, 2), nullable=False)
    promised_date = Column(DateTime, nullable=False)
    confidence = Column(Numeric(3, 2), nullable=True)
    status = Column(String, default="PENDING")  # PENDING | KEPT | BROKEN | SUPERSEDED

    created_at = Column(DateTime, default=utc_now)

    revenue_case = relationship("RevenueCase", back_populates="promises")


class CustomerMessage(Base):
    __tablename__ = "customer_messages"

    id = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    revenue_case_id = Column(UUID(as_uuid=False), ForeignKey("revenue_cases.id"), nullable=False)

    direction = Column(String, nullable=False)  # outbound | inbound
    channel = Column(String, nullable=True)
    body = Column(Text, nullable=False)
    extracted = Column(JSON, nullable=True)  # LLM-parsed structured intent, if any

    created_at = Column(DateTime, default=utc_now)


class AuditEvent(Base):
    """Append-only explanation/history trail — the thing judges/compliance can read end to end."""
    __tablename__ = "audit_events"

    id = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    revenue_case_id = Column(UUID(as_uuid=False), ForeignKey("revenue_cases.id"), nullable=False)

    event = Column(String, nullable=False)
    detail = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=utc_now)

    revenue_case = relationship("RevenueCase", back_populates="audit_events")


class ExperimentRun(Base):
    """Durable provenance for one synthetic experiment — historical runs are immutable."""
    __tablename__ = "experiment_runs"

    run_id = Column(String, primary_key=True)
    resolved_seed = Column(Integer, nullable=False)
    scenario_count = Column(Integer, nullable=False)
    model_version = Column(String, nullable=True)
    model_fingerprint = Column(String, nullable=True)
    feature_schema_version = Column(String, nullable=True)
    adaptive_profile = Column(String, nullable=True)
    friction_weight = Column(Numeric(6, 3), nullable=True)
    created_at = Column(DateTime, default=utc_now)


class ExperimentCase(Base):
    """Control/treatment assignment for the baseline-vs-RecoveryOS batch evaluation (Phase 7)."""
    __tablename__ = "experiment_cases"

    id = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    revenue_case_id = Column(UUID(as_uuid=False), ForeignKey("revenue_cases.id"), nullable=True)

    run_id = Column(String, nullable=False, index=True)
    arm = Column(String, nullable=False)  # baseline | adaptive
    failure_category = Column(String, nullable=True)
    chosen_action = Column(String, nullable=True)
    amount_at_risk = Column(Numeric(12, 2), nullable=True)
    amount_recovered = Column(Numeric(12, 2), nullable=True)
    contacts_made = Column(Integer, default=0)
    recovered = Column(Boolean, default=False)
    hours_to_recovery = Column(Numeric(6, 2), nullable=True)

    created_at = Column(DateTime, default=utc_now)
