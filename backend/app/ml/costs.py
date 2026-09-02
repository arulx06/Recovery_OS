"""
Notional cost per action, in the same currency units as case.amount (INR).

These aren't real transaction fees — they're a deliberately simple proxy
for "how much does choosing this action cost, independent of whether it
recovers the money": customer goodwill spent on a contact, a human
reviewer's time on an escalation, a small transaction cost on creating a
Payment Link. WAIT and STOP are free — doing nothing costs nothing.

The expected-value scorer (scorer.py) uses these to rank actions by
`p_recovery * amount - cost`, not by `p_recovery` alone — otherwise the
model would always prefer whichever action has the highest raw recovery
probability regardless of how expensive or intrusive it is.
"""
ACTION_COST = {
    "WAIT": 0.0,
    "WAIT_FOR_NATIVE_RETRY": 0.0,
    "CREATE_PAYMENT_LINK": 5.0,
    "CONTACT_CUSTOMER": 15.0,
    # PTP collection uses the same outbound-contact path as CONTACT_CUSTOMER.
    "COLLECT_PROMISE_TO_PAY": 15.0,
    "ESCALATE": 50.0,
    "STOP": 0.0,
}
