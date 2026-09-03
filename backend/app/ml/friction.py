"""
Friction-aware utility — explicit customer-intervention cost.

Hard guardrails (max_contacts, cooldown, max_amount, stopping rule) remain
authoritative and are enforced before scoring. Soft friction is a preference
signal layered on top: it makes repeated or intrusive actions less desirable
without duplicating guardrail blocks.

Units: friction_score is dimensionless (0 = no customer friction). It is
converted to INR-equivalent penalty via ADAPTIVE_FRICTION_WEIGHT, which is a
policy preference, not a measured monetary cost. Dashboards show recovered INR,
action-cost INR, and friction score separately.
"""

from sqlalchemy.orm import Session

from app.models import RevenueCase
from app.services import policy_engine

# Base friction ordering — lowest = least intrusive.
# Rationale:
# - WAIT / WAIT_FOR_NATIVE_RETRY: no direct customer contact, observe native recovery.
# - CREATE_PAYMENT_LINK: one link; customer can ignore.
# - CONTACT_CUSTOMER: direct outreach.
# - COLLECT_PROMISE_TO_PAY: asks for commitment.
# - ESCALATE: human operational cost.
# - STOP: no friction (give up).
BASE_FRICTION = {
    "WAIT": 0.0,
    "WAIT_FOR_NATIVE_RETRY": 2.0,
    "CREATE_PAYMENT_LINK": 25.0,
    "CONTACT_CUSTOMER": 40.0,
    "COLLECT_PROMISE_TO_PAY": 60.0,
    "ESCALATE": 80.0,
    "STOP": 0.0,
}

# Incremental friction per prior contact on the same case — soft preference,
# distinct from hard max_contacts guardrail which blocks at 3.
CONTACT_INCREMENT = 12.0

CONTACT_ACTIONS = {"CREATE_PAYMENT_LINK", "CONTACT_CUSTOMER", "COLLECT_PROMISE_TO_PAY"}

# Policy profiles — explicit, not tuned to look good on one seed.
# Weight converts friction_score to INR penalty. These are INR-equivalent
# per friction point — tuned so that at typical invoice INR 5000, a 40-point
# CONTACT vs 0-point WAIT gap (40*weight) is comparable to a 0.05–0.10
# p*amount gap (250–500 INR). Balanced should visibly reduce contacts vs
# revenue-first while still allowing contact when its p advantage is large.
PROFILE_WEIGHTS = {
    "revenue_first": 4.0,   # favors recovery, tolerates friction (small penalty)
    "balanced": 18.0,       # defensible default — friction matters
    "low_friction": 45.0,   # strongly prefers low-intervention
}


def compute_friction_score(
    action: str,
    case: RevenueCase,
    db: Session,
) -> float:
    """Deterministic friction score — no model, no network.

    Base per action plus soft increment for repeated contacts on same case.
    Uses bounded query (contacts_for_case) — no unbounded history scan.
    """
    base = BASE_FRICTION.get(action, 50.0)
    if action in CONTACT_ACTIONS:
        previous_contacts = len(policy_engine.contacts_for_case(db, case))
        # Second contact is less desirable than first, even though guardrail still allows it.
        return base + (previous_contacts * CONTACT_INCREMENT)
    return base


def get_friction_weight(profile: str | None, explicit_weight: float | None) -> tuple[str, float]:
    """Resolve effective friction weight: explicit overrides profile; default balanced."""
    if explicit_weight is not None:
        return f"custom:{explicit_weight}", float(explicit_weight)
    if profile in PROFILE_WEIGHTS:
        return profile, float(PROFILE_WEIGHTS[profile])
    return "balanced", float(PROFILE_WEIGHTS["balanced"])


def compute_utility(
    p_recovery: float,
    amount: float,
    action_cost: float,
    friction_score: float,
    friction_weight: float,
) -> float:
    """Canonical utility — one place for expected value math.

    expected_recovered_value = p * amount
    utility = expected_recovered_value - action_cost - friction_weight * friction_score
    """
    expected_recovered = float(p_recovery) * float(amount)
    return expected_recovered - float(action_cost) - float(friction_weight) * float(friction_score)
