"""RQ transport for durable actions. Redis contains IDs, never case snapshots."""
from datetime import UTC, timedelta

from redis import Redis
from rq import Queue
from rq.registry import ScheduledJobRegistry, StartedJobRegistry

from app.core.config import settings
from app.core.database import SessionLocal
from app.core.time import utc_now
from app.models import Action, AuditEvent, RevenueCase


QUEUEABLE_ACTION_TYPES = {
    "WAIT",
    "WAIT_FOR_NATIVE_RETRY",
    "CREATE_PAYMENT_LINK",
    "CONTACT_CUSTOMER",
    "COLLECT_PROMISE_TO_PAY",
    "FOLLOW_UP_PTP",
}


def get_queue() -> Queue:
    connection = Redis.from_url(settings.REDIS_URL)
    return Queue(settings.RQ_QUEUE_NAME, connection=connection)


def _job_is_registered(queue: Queue, job_id: str) -> bool:
    scheduled = ScheduledJobRegistry(name=queue.name, connection=queue.connection)
    started = StartedJobRegistry(name=queue.name, connection=queue.connection)
    return (
        job_id in queue.job_ids
        or job_id in queue.intermediate_queue.get_job_ids()
        or job_id in scheduled.get_job_ids()
        or job_id in started.get_job_ids()
    )


def enqueue_action(action_id: str) -> bool:
    """Publish the current scheduled generation of an action after DB commit."""
    if not settings.TASK_QUEUE_ENABLED:
        return False

    with SessionLocal() as db:
        action = (
            db.query(Action)
            .join(RevenueCase, RevenueCase.id == Action.revenue_case_id)
            .filter(Action.id == action_id, RevenueCase.source == "razorpay")
            .first()
        )
        if (
            not action
            or action.status != "SCHEDULED"
            or action.action_type not in QUEUEABLE_ACTION_TYPES
        ):
            return False
        scheduled_for = action.scheduled_for or utc_now()
        attempt_count = action.attempt_count
        job_id = f"action-{action.id}-{attempt_count}"
        db.rollback()

    try:
        queue = get_queue()
        existing = queue.fetch_job(job_id)
        if existing is not None and not _job_is_registered(queue, job_id):
            existing.delete()
            existing = None
        if existing is None:
            due_at = scheduled_for.replace(tzinfo=UTC)
            if due_at.microsecond:
                due_at = (due_at + timedelta(seconds=1)).replace(microsecond=0)
            now = utc_now().replace(tzinfo=UTC)
            kwargs = {
                "job_id": job_id,
                "job_timeout": settings.ACTION_JOB_TIMEOUT_SECONDS,
                "result_ttl": 86400,
                "failure_ttl": 86400,
            }
            if due_at > now:
                queue.enqueue_at(due_at, "app.jobs.process_action", action_id, **kwargs)
            else:
                queue.enqueue("app.jobs.process_action", action_id, **kwargs)
    except Exception:
        with SessionLocal.begin() as db:
            action = db.query(Action).filter(Action.id == action_id, Action.status == "SCHEDULED").first()
            if action:
                action.last_error = "queue publish failed"
                db.add(AuditEvent(
                    revenue_case_id=action.revenue_case_id,
                    event="action_enqueue_failed",
                    detail={"action_id": action.id},
                ))
        return False

    with SessionLocal.begin() as db:
        action = db.query(Action).filter(Action.id == action_id, Action.status == "SCHEDULED").first()
        if action:
            action.enqueued_at = utc_now()
            action.queue_job_id = job_id
            action.last_error = None
    return True


def enqueue_case_actions(case_id: str) -> int:
    with SessionLocal() as db:
        action_ids = [
            row[0]
            for row in (
                db.query(Action.id)
                .join(RevenueCase, RevenueCase.id == Action.revenue_case_id)
                .filter(
                    Action.revenue_case_id == case_id,
                    Action.status == "SCHEDULED",
                    Action.action_type.in_(QUEUEABLE_ACTION_TYPES),
                    RevenueCase.source == "razorpay",
                )
                .all()
            )
        ]
        db.rollback()
    return sum(enqueue_action(action_id) for action_id in action_ids)
