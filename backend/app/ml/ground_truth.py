"""
Ground-truth recovery simulator.

Nobody has real "did this recovery action work" data yet — that only
exists once RecoveryOS is running against live traffic. Until then, this
module is the honest stand-in: a hand-set table of P(recovery | failure
category, action), modulated by amount, elapsed time, contact fatigue, and
whether the payment is subscription-linked. It's used for two things:

  1. Generating synthetic training history for the recovery-probability
     model (synthetic_history.py) — the model's target IS this simulator,
     which means it will only ever be as good as these assumptions.
  2. Sampling an outcome for whichever action a policy chooses, in the
     offline baseline-vs-ML evaluation (scripts/evaluate_policies.py).

This is not a claim about real recovery rates. Every surface that uses this
module's output must be labeled as a synthetic simulation benchmark; see the
evaluation design in ARCHITECTURE.md.

The specific numbers encode the same intuition as the failure taxonomy in
ARCHITECTURE.md: transient/native-retry categories recover well passively;
customer-actionable categories (auth, invalid instrument) need contact;
permanent/unknown failures rarely recover without a human.
"""
import random

from app.services.policy_engine import ALL_ACTIONS

# Base P(recovery) for every (failure_category, action) pair, before any
# context modifiers are applied. Actions not listed for a category default
# to a low fallback (0.05) — e.g. WAIT almost never resolves an invalid
# instrument on its own.
BASE_PROBABILITY: dict[tuple[str, str], float] = {
    ("TRANSIENT_INFRASTRUCTURE", "WAIT"): 0.78,
    ("TRANSIENT_INFRASTRUCTURE", "WAIT_FOR_NATIVE_RETRY"): 0.70,
    ("TRANSIENT_INFRASTRUCTURE", "CONTACT_CUSTOMER"): 0.55,
    ("TRANSIENT_INFRASTRUCTURE", "COLLECT_PROMISE_TO_PAY"): 0.50,
    ("TRANSIENT_INFRASTRUCTURE", "CREATE_PAYMENT_LINK"): 0.50,
    ("TRANSIENT_INFRASTRUCTURE", "ESCALATE"): 0.40,

    ("SUBSCRIPTION_PENDING_NATIVE_RETRY", "WAIT_FOR_NATIVE_RETRY"): 0.80,
    ("SUBSCRIPTION_PENDING_NATIVE_RETRY", "WAIT"): 0.55,
    ("SUBSCRIPTION_PENDING_NATIVE_RETRY", "CONTACT_CUSTOMER"): 0.45,
    ("SUBSCRIPTION_PENDING_NATIVE_RETRY", "COLLECT_PROMISE_TO_PAY"): 0.42,
    ("SUBSCRIPTION_PENDING_NATIVE_RETRY", "CREATE_PAYMENT_LINK"): 0.40,
    ("SUBSCRIPTION_PENDING_NATIVE_RETRY", "ESCALATE"): 0.35,

    ("INSUFFICIENT_BALANCE", "COLLECT_PROMISE_TO_PAY"): 0.55,
    ("INSUFFICIENT_BALANCE", "CONTACT_CUSTOMER"): 0.50,
    ("INSUFFICIENT_BALANCE", "CREATE_PAYMENT_LINK"): 0.45,
    ("INSUFFICIENT_BALANCE", "WAIT"): 0.35,
    ("INSUFFICIENT_BALANCE", "WAIT_FOR_NATIVE_RETRY"): 0.30,
    ("INSUFFICIENT_BALANCE", "ESCALATE"): 0.30,

    ("CUSTOMER_AUTHENTICATION", "CONTACT_CUSTOMER"): 0.62,
    ("CUSTOMER_AUTHENTICATION", "CREATE_PAYMENT_LINK"): 0.58,
    ("CUSTOMER_AUTHENTICATION", "COLLECT_PROMISE_TO_PAY"): 0.40,
    ("CUSTOMER_AUTHENTICATION", "ESCALATE"): 0.35,
    ("CUSTOMER_AUTHENTICATION", "WAIT"): 0.15,
    ("CUSTOMER_AUTHENTICATION", "WAIT_FOR_NATIVE_RETRY"): 0.15,

    ("INVALID_INSTRUMENT", "CREATE_PAYMENT_LINK"): 0.65,
    ("INVALID_INSTRUMENT", "CONTACT_CUSTOMER"): 0.55,
    ("INVALID_INSTRUMENT", "COLLECT_PROMISE_TO_PAY"): 0.35,
    ("INVALID_INSTRUMENT", "ESCALATE"): 0.30,
    ("INVALID_INSTRUMENT", "WAIT"): 0.08,
    ("INVALID_INSTRUMENT", "WAIT_FOR_NATIVE_RETRY"): 0.08,

    ("MANDATE_ISSUE", "CONTACT_CUSTOMER"): 0.40,
    ("MANDATE_ISSUE", "COLLECT_PROMISE_TO_PAY"): 0.38,
    ("MANDATE_ISSUE", "CREATE_PAYMENT_LINK"): 0.35,
    ("MANDATE_ISSUE", "ESCALATE"): 0.30,
    ("MANDATE_ISSUE", "WAIT"): 0.05,
    ("MANDATE_ISSUE", "WAIT_FOR_NATIVE_RETRY"): 0.05,

    ("PERMANENT_HARD_FAILURE", "ESCALATE"): 0.20,
    ("PERMANENT_HARD_FAILURE", "CONTACT_CUSTOMER"): 0.10,
    ("PERMANENT_HARD_FAILURE", "COLLECT_PROMISE_TO_PAY"): 0.10,
    ("PERMANENT_HARD_FAILURE", "CREATE_PAYMENT_LINK"): 0.08,
    ("PERMANENT_HARD_FAILURE", "WAIT"): 0.03,
    ("PERMANENT_HARD_FAILURE", "WAIT_FOR_NATIVE_RETRY"): 0.03,

    ("UNKNOWN", "ESCALATE"): 0.25,
    ("UNKNOWN", "CONTACT_CUSTOMER"): 0.20,
    ("UNKNOWN", "COLLECT_PROMISE_TO_PAY"): 0.18,
    ("UNKNOWN", "CREATE_PAYMENT_LINK"): 0.18,
    ("UNKNOWN", "WAIT"): 0.12,
    ("UNKNOWN", "WAIT_FOR_NATIVE_RETRY"): 0.10,
}
FALLBACK_PROBABILITY = 0.05  # any (category, action) pair not listed above
STOP_PROBABILITY = 0.0       # STOP means giving up — recovery never happens by definition

CONTACT_ACTIONS = {"CONTACT_CUSTOMER", "CREATE_PAYMENT_LINK", "COLLECT_PROMISE_TO_PAY"}


def true_probability(
    category: str,
    action: str,
    amount: float,
    days_overdue: float = 0,
    previous_contacts: int = 0,
    subscription_linked: bool = False,
) -> float:
    """
    P(recovery | category, action, context) under the simulator's
    assumptions. Deterministic given its inputs — all the randomness lives
    in the caller's Bernoulli draw, not in here.
    """
    if action == "STOP":
        return STOP_PROBABILITY

    p = BASE_PROBABILITY.get((category, action), FALLBACK_PROBABILITY)

    # Larger amounts are modestly harder to recover automatically — more
    # scrutiny, more likely the customer hesitates. Mild effect, floors out.
    amount_factor = max(0.7, 1.0 - (float(amount) / 200_000.0) * 0.3)
    p *= amount_factor

    # The longer a case has sat, the less a passive action (WAIT) helps;
    # active outreach decays much more slowly.
    if action in ("WAIT", "WAIT_FOR_NATIVE_RETRY"):
        overdue_factor = max(0.4, 1.0 - float(days_overdue) * 0.03)
        p *= overdue_factor

    # Contact fatigue: each prior contact on this case makes another one
    # less effective — customers who haven't responded yet are less likely
    # to respond to a repeat.
    if action in CONTACT_ACTIONS:
        p *= (0.75 ** max(0, previous_contacts))

    # A subscription payment specifically benefits from waiting for
    # Razorpay's own retry schedule; it's mildly counterproductive to
    # duplicate that effort with an unrelated action.
    if subscription_linked and action != "WAIT_FOR_NATIVE_RETRY":
        p *= 0.9

    return max(0.01, min(0.95, p))


def sample_outcome(
    category: str, action: str, amount: float,
    days_overdue: float = 0, previous_contacts: int = 0,
    subscription_linked: bool = False, rng: random.Random | None = None,
) -> bool:
    """Bernoulli draw from true_probability(...). Pass `rng` for reproducibility."""
    rng = rng or random
    p = true_probability(category, action, amount, days_overdue, previous_contacts, subscription_linked)
    return rng.random() < p


__all__ = [
    "BASE_PROBABILITY", "FALLBACK_PROBABILITY", "true_probability", "sample_outcome",
]

assert set(a for (_, a) in BASE_PROBABILITY) <= set(ALL_ACTIONS), "ground truth references an unsupported action"
