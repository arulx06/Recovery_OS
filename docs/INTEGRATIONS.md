# RecoveryOS — Integrations

> Each external provider has its own boundary: credential gates, simulation fallbacks, what data is sent, what is never allowed. `TEST_MODE ≠ production`.

---

## Razorpay

### Verification and ingestion

- **Endpoint:** `POST /webhooks/razorpay` (`app/routers/webhooks.py:34`).
- **Signature:** `x-razorpay-signature` on the **raw request bytes** (`app/core/security.py:11` — HMAC-SHA256 via `hmac.new(secret, raw_body, sha256).hexdigest()`; `compare_digest`). Rationale: re-serialized JSON key ordering would break the signature; raw bytes match Razorpay's documented scheme.
- **Shape validation:** accepted body must be a JSON object; `event` must be a non-empty string (`webhooks.py:47`); `x-razorpay-event-id` must be present. Missing → `400`.
- **Idempotency:** `payment_events.razorpay_event_id` is `UniqueConstraint` (`alembic/versions/709600d2e2b1`), plus an app-level `IntegrityError → 200 {ignored: duplicate_event}` path (`webhooks.py:66`) so concurrent redeliveries do not double-create.
- **Inbound events handled** (`app/services/orchestrator.py:36`):
  - `payment.failed`, `subscription.halted` → `_handle_failure` (+ diagnosis + policy)
  - `payment.captured`, `subscription.charged` → `_handle_recovery` (matched on `RevenueCase.razorpay_payment_id`)
  - `payment_link.paid` → `_handle_payment_link_paid` (matched on `RevenueCase.razorpay_payment_link_id` — a different payment than the failed one — reading `payload.payment_link.entity.id`)
  - `payment.dispute.created` → `_handle_dispute` (matched on `RevenueCase.razorpay_payment_id`)
  - Anything else → `return None` (acknowledged but no case change; historically `unknown` payloads may carry odd wrappers; `_entity_from_payload` iterates `payload.payload.values()`).

**What has been manually verified:**

```
payment.failed { error_reason: "card_expired" }
  → classified INVALID_INSTRUMENT (failure_diagnosis:card_expired)
  → baseline chooses CREATE_PAYMENT_LINK
  → Razorpay Test Mode REST API POST /v1/payment_links
  → response { id: "plink_*", short_url: "https://rzp.io/…", … }
  → GET /cases/{id}.actions[0].result { simulated: false }

payment_link.paid { payload.payment_link.entity.id == <that plink_*> }
  → state RECOVERED, audit case_recovered_silently
```

No other production Razorpay API surface is used. No subscriptions/orders mutation.

### Payment Links — gates and behavior

```python
# app/services/razorpay_client.py:48
if not settings.RAZORPAY_API_ENABLED:
    return _simulated_payment_link(...)      # default

if not credentials_configured():             # key + secret both non-empty
    raise RazorpayAPIError("… incomplete")

if not settings.RAZORPAY_KEY_ID.startswith("rzp_test_"):
    raise RazorpayAPIError("… Test Mode API keys")
# then httpx.post(BASE_URL/payment_links, json={amount (paise), …},
#                 auth=(key, secret), timeout=10s)
```

- **Simulation is the default.** Non-empty `RAZORPAY_KEY_ID`/`RAZORPAY_KEY_SECRET` alone **never** enable network — `RAZORPAY_API_ENABLED=true` is required explicitly (`backend/.env.example:8`) and is independent of webhook signature verification (which fails **closed** when `RAZORPAY_WEBHOOK_SECRET` is empty — security vs missing-integration have different defaults by design).
- **Simulated link** (`_simulated_payment_link`): `id=plink_sim_<uuid14>`, `short_url=https://rzp.io/simulated/<id>`, `simulated=true`, `note="… local simulation, not a real Payment Link …"`. Link IDs are unique per call. Visible at `GET /cases/{id}.actions[0].result` and `case.razorpay_payment_link_id`.
- **Live Test Mode link** (`create_payment_link` under flag): `POST https://api.razorpay.com/v1/payment_links` with `{amount: paise, currency, description: "Recovery for case <uuid>", reference_id: case.id, notes: {}}`. Response is `response.json()` (genuine `plink_*` + `rzp.io`), with no `simulated` key — absence of `simulated` **is** the live signal in inspectors/tests. Failures raise `RazorpayAPIError` → `Action(status=FAILED)` + `HUMAN_REVIEW` in the executor.
- **Link fulfillment:** a `payment_link.paid` webhook carries **two** entities: `payload.payment_link.entity` (the link — what `RevenueCase.razorpay_payment_link_id` matches) and `payload.payment.entity` (the fulfilling payment, a new `pay_*` unrelated to the original failure — `orchestrator.py:61`). Matching on the wrong entity would silently fail.

**Current limitations:**

- Only the Payment Links API is integrated; `payment_link.paid` matching on a `link_id` that was never created is an acknowledged noop (`200 {case_id: null}`).
- The link is created but **never delivered** to a customer — no SMS/email/WhatsApp — so payment depends on the operator sharing the `short_url` or on a future delivery transport (`docs/INTEGRATIONS.md` → LLM/delivery).
- Amount edge cases (e.g., captured ≠ link amount) are not validated; any `payment_link.paid` for the `link_id` recovers.

### `TEST MODE ≠ production`

- Razorpay Test Mode replays the same REST shape but moves test rupees, not real money. `RAZORPAY_API_ENABLED=true` with a test key demonstrates the integration loop without settlement.
- `RAZORPAY_API_ENABLED=true` with a `rzp_live_*` key is **rejected before network** — no live-money path exists in this codebase.

### Required settings

| Variable | Purpose | Used where |
|----------|---------|------------|
| `RAZORPAY_WEBHOOK_SECRET` | HMAC key for `x-razorpay-signature` | `webhooks.py:39`, `security.py:11` |
| `RAZORPAY_API_ENABLED` (`false`) | Explicit opt-in gate for live API traffic | `razorpay_client.py:65`, `llm_client.py:109` analogue |
| `RAZORPAY_KEY_ID` | Basic-auth username | `razorpay_client.py:88` |
| `RAZORPAY_KEY_SECRET` | Basic-auth password | `razorpay_client.py:88` |

Do not include actual values in documentation.

---

## LLM (Anthropic-only)

### Provider claim

- **Only Anthropic** is implemented. `app/services/llm_client.py:32` pins `ANTHROPIC_MODEL=claude-3-5-haiku-latest`; `_require_live_config` (`llm_client.py:56`) raises `LLMAPIError: LLM_API_ENABLED currently supports only LLM_PROVIDER=anthropic` when `LLM_PROVIDER != anthropic`. This is **not** provider-agnostic — see `docs/CURRENT_STATE.md` capability matrix status `PARTIAL` and `ARCHITECTURE.md` LLM boundary.
- Switching providers would require changing `ANTHROPIC_API_URL`, the header (`x-api-key`/`anthropic-version`), and the JSON prompt / extraction schema — there is no adapter layer.

### Gates and fallbacks

Identical philosophy to Razorpay: explicit gate, never implicit.

```python
if not settings.LLM_API_ENABLED:    # backend/.env.example:13
    return _simulated_draft / _simulated_extract
_require_live_config()              # provider == anthropic, key non-empty
# then httpx.post(ANTHROPIC_API_URL, …)
```

- **`draft_contact_message(action_type, {amount, failure_category, case_id})`** → `{body: str, simulated: bool}`.
  - Simulated (`llm_client.py:77`): two fixed `_TEMPLATES` (`CONTACT_CUSTOMER` vs `COLLECT_PROMISE_TO_PAY`), interpolated with `amount` + lowercased `failure_category`; labeled `simulated=true` (`note: "templated message, not model-generated"`). No network.
  - Live: `POST https://api.anthropic.com/v1/messages` with `model=claude-3-5-haiku-latest, max_tokens=300, system="<compliance prompt>", messages=[{role:user, content: "Failed payment …"}]`, headers `x-api-key`, `anthropic-version=2023-06-01`, `content-type=application/json` (`llm_client.py:126`). Response text stitched from `content[?type=text].text`.
- **`extract_ptp_intent(message, now)`** → `PTPExtraction(intent ∈ {promise_to_pay, dispute, unclear}, amount?, date?, confidence, simulated, raw)`.
  - Simulated (`llm_client.py:181`): keyword scan over `DISPUTE_PHRASES` (`already paid`, `double charge`, `fraud`, `never made this`, … → `dispute, 0.9`); else regex `AMOUNT_PATTERN`/`BARE_NUMBER_NEAR_PAY_PATTERN` + relative-date/weekday parser (`_parse_relative_date` — tomorrow/today/day-name → ISO date; bare weekday on that weekday means **next occurrence**) → `promise_to_pay` only when **both** amount and date resolve (confidence `0.85`), one-of-two → `unclear 0.4`, neither → `unclear 0.2`.
  - Live: `POST https://api.anthropic.com/v1/messages` with `system="Extract intent … Respond with ONLY a JSON object … intent/payment date/confidence; Today's date is …"` + `max_tokens=200` (`llm_client.py:211`). Response stitched, `json.loads`, return with `simulated=false`. Parse failures (`JSONDecodeError`, `ValueError`, `TypeError`) fall back to `PTPExtraction(intent=unclear, simulated=false, raw={parse_error})` — pipeline continues (`llm_client.py:251`) rather than crashing.

### What data is sent

- **Drafting:** amount (INR), `failure_category`, `action_type` (`llm_client.py:119`). Not the customer name, `razorpay_payment_id`, or full payment entity. The system/user prompts explicitly constrain the model: "Be warm but brief (2–3 sentences), never threatening, never mention penalties. … Output only the message body" (`llm_client.py:113`).
- **Extraction:** the raw customer message body plus today's date in the system prompt (`llm_client.py:216`). No other case data is sent.
- In both paths, `timeout=10s`, `raise_for_status()` on.

### Allowed vs prohibited responsibilities

- **Allowed:** produce a draft sentence collection; classify a free-text reply. Both produce text/guess only.
- **Prohibited:** deciding whether money moves. `extract_ptp_intent` output is **always** validated by `ptp_extractor.validate_promise` (`app/services/ptp_extractor.py:38`) — checks confidence ≥ 0.6, amount positive and ≤ `case.amount`, date ∈ [today, today+14d], `intent==promise_to_pay`; anything that fails validation goes to `HUMAN_REVIEW` rather than becoming a `PromiseToPay`. Dispute classifications route through the **same** `_dispute_case` path (`orchestrator.py:248`) as a Razorpay dispute webhook — identical terminal logic.
- **Failure mode:** `LLMAPIError` / `httpx.HTTPError` in the draft path is caught by `action_executor._execute_contact` → `Action(status=FAILED, result={error})` + `HUMAN_REVIEW` (`action_executor.py:112`), flushed — webhook itself still returns `200` so Razorpay stops retrying this event and a human can investigate. Extraction failures fall back to `unclear` rather than retrying in place.

### Current limitations

- Single-provider — `openai`, `bedrock`, etc. are not supported and raise rather than degrade gracefully (by contrast with webhook signature which fails closed, here failing loudly is the right signal that the claimed capability does not exist).
- Simulation is clearly labeled (`simulated`, `channel=simulated`) but calling code must propagate that flag correctly — `orchestrator.handle_customer_reply` does (`CustomerMessage(channel = simulated if extraction.simulated else llm)` — `orchestrator.py:320`), tests assert the flag, but a future router that forgot to preserve it could mis-represent a templated draft as generated.
- No redaction or PII-removal pass is implemented; the message body is forwarded verbatim to Anthropic when enabled.

### Required settings

| Variable | Purpose |
|----------|---------|
| `LLM_API_ENABLED` (`false`) | Explicit opt-in gate |
| `LLM_PROVIDER` (`anthropic`) | Must remain `anthropic` for live traffic |
| `LLM_API_KEY` | Anthropic key |

---

## Redis / RQ

### Actual current state — not planned fiction

| Aspect | Code fact | File |
|--------|-----------|------|
| Dependency installed | `redis==5.0.8`, `rq==1.16.2` | `backend/requirements.txt:10` |
| Config key present | `REDIS_URL=redis://localhost:6379/0` | `app/core/config.py:19` — comment "Reserved for a future delayed-action worker; currently unused." |
| Compose service | `services.redis: image: redis:7-alpine` | `docker-compose.yml:19` — comment "Redis is reserved for future scheduling work" |
| Runtime imports | **None** in `backend/app/**` or `backend/scripts/**` besides a comment documenting the absence | Verified: `grep -r "import redis\|from rq\|import rq" backend/app backend/scripts` → no hit besides `config.py` definition and `ptp_followup.py:6` comment |
| Queue / worker / scheduler process | **Does not exist** | No `rq.Worker`, no `Queue`, no `redis.from_url`, no `app/worker.py` |
| Use in request path | None — `WAIT`/`FOLLOW_UP_PTP` rows are `Action(status=SCHEDULED)` that stay scheduled until healed by `scripts/process_followups.py` or a future inbound webhook | `docs/SYSTEM_FLOWS.md` flows 9–10 |

In other words: **dependency + configuration present, execution runtime not implemented.** Compose brings up a Redis container because local development provisions it for the future runtime, not because the current application connects to it.

Do not describe automatic wake-up behavior as current (`WAIT` is a persisted `SCHEDULED` row that is never woken; see `docs/CURRENT_STATE.md` Runtime reality).

### Future intent (brief)

The intended runtime is: Redis as **transport** (enqueue wake-up jobs / pub/sub heartbeat) with PostgreSQL as the **source of truth** (`Action.scheduled_for` ≤ now drives durability), so crash recovery and invariant checks remain DB-bound. Any worker will need idempotency guarding against double-execution of the same `Action` and terminal-state re-validation before mutating `RevenueCase.state`. This is reserved for the Temporal Recovery Runtime phase and is explicitly **not wired** here.

---

## External-side-effect audit

| Capability | External call today | When it fires | Fallback |
|------------|---------------------|---------------|----------|
| Payment Link create | `POST /v1/payment_links` **only when** `RAZORPAY_API_ENABLED=true` + `rzp_test_*` | `action_executor._execute_create_payment_link`, synchronously in the webhook request | `_simulated_payment_link` |
| LLM draft | `POST https://api.anthropic.com/v1/messages` **only when** `LLM_API_ENABLED=true` + provider=anthropic + key | `action_executor._execute_contact`, synchronously in the webhook request | `_simulated_draft` (`_TEMPLATES`) |
| LLM PTP extract | Same endpoint, same gate | `orchestrator.handle_customer_reply`, synchronously in the `customer-reply` request | `_simulated_extract` (keywords + regex) |
| Redis/RQ enqueue | *(none)* | — | `SCHEDULED` DB rows (see Redis section) |

Every external call path can be **fully exercised under tests without network** via the `simulated` branch and the fake `httpx.post` monkeypatches (`tests/test_razorpay_client.py:53`, `tests/test_llm_client.py:49`).

