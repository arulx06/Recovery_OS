# RecoveryOS — Offline Demo Fallback

> If internet disappears during judging, every core story remains provable locally.

## What still works without internet

| Feature | How |
|---|---|
| Seeded cases A–E (+ D,F with local model) | `python scripts/seed_demo.py --reset-demo` uses only PostgreSQL + local policy — no Razorpay/LLM network |
| Failure diagnosis, guardrails, Decision Inspector | Derived from `payment.failed` → `failure_diagnosis` → `policy_engine` / `ml_policy` |
| Friction-aware candidate comparison | `friction.py` + `scorer.rank_actions_with_friction` local |
| Temporal runtime (WAIT, PTP follow-up) | PostgreSQL `Action.status=SCHEDULED` + RQ scheduler (Redis local) — no internet |
| Payment Link (SIMULATED) | `plink_sim_*` with `https://rzp.io/simulated/*`, `simulated=true`, `reference_id=Action.id` — clearly labeled `SIMULATED LINK` |
| Provider reconciliation (SIMULATED path) | `list?reference_id` returns `[]` in simulation — no network; mismatch/adopt tests are mocked hermetic |
| Customer draft / PTP | Deterministic templates + regex extraction — no LLM network (LLM disabled by default) |
| Synthetic experiments | `POST /experiments` with `seed` — `ground_truth` simulator local, `SYNTHETIC SIMULATION` banner |
| Audit trail / timeline | PostgreSQL `PaymentEvent+Decision+Action+Message+PTP+AuditEvent` — no network |

## What is honestly SIMULATED offline

- Payment Link URL is `https://rzp.io/simulated/plink_sim_*`, not a real `rzp.io` Test Mode link — dashboard shows `SIMULATED LINK`.
- Recovery via `payment_link.paid` can still be triggered locally after assigning the displayed ID: `$linkId = 'plink_sim_actual_id'; python scripts/send_test_webhook.py payment_link.paid --payment-link-id $linkId`.
- No real Razorpay `POST /v1/payment_links` or `GET /v1/payment_links?reference_id=` is performed — provider truth remains `SIMULATED` until TEST MODE enabled.

## How to run entirely offline

```powershell
Set-Location D:\Razorpay
docker compose up -d            # postgres + redis local
Set-Location backend
Copy-Item .env.example .env     # RAZORPAY_API_ENABLED=false, LLM_API_ENABLED=false
alembic upgrade head
python -m app.ml.train         # local training, no network
python scripts/seed_demo.py --reset-demo
```

Then start the long-running processes in separate PowerShell terminals:

```powershell
# Terminal 1, from D:\Razorpay\backend
uvicorn app.main:app --reload --port 8000
```

```powershell
# Terminal 2, from D:\Razorpay\backend
rq worker --worker-class app.worker.WindowsWorker --with-scheduler --url redis://localhost:6379/0 recoveryos
```

```powershell
# Terminal 3
Set-Location D:\Razorpay\frontend
npm install
npm run dev                    # http://localhost:5173
```

Then demonstrate via the 4-minute script — all steps work offline using SIMULATED labels.

## How to signal offline vs TEST MODE

- Dashboard System card: `RAZORPAY: SIMULATED LINK` vs `RAZORPAY TEST MODE` (from `GET /dashboard/summary` `razorpay.mode_label`).
- Case detail provider truth: `SIMULATED LINK` vs `RAZORPAY TEST MODE`; `reference_id` always `Action.id`; `reconciled` badge only when provider adopt occurred.
- Health `/health` and `/ready` report `razorpay.simulated` boolean without leaking secrets.
- Experiments: every surface labeled `SYNTHETIC SIMULATION · NOT PRODUCTION LIFT`.

## Real TEST MODE when online again

When internet returns, enable real link in `backend/.env`:

```
RAZORPAY_API_ENABLED=true
RAZORPAY_KEY_ID=rzp_test_...   # must be rzp_test_*, never live
RAZORPAY_KEY_SECRET=...
```

Restart `uvicorn`, then `payment.failed card_expired` creates a real `plink_*` via `POST https://api.razorpay.com/v1/payment_links` with `reference_id=Action.id`; dashboard flips to `RAZORPAY TEST MODE`. Reconciliation via `GET ?reference_id=` validates amount/currency/notes/status before adopt — all tested hermetically.
