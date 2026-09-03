# RecoveryOS — Public Webhook Manual Plan

> Vendor-neutral, no dependency. Secret never appears in docs.

## Goal

Expose `POST /webhooks/razorpay` (signature-authenticated, idempotent) for real Razorpay Test Mode events while keeping operator reads and mutations optionally token-protected and the frontend token transient (sessionStorage).

## Steps

### 1. Prepare local backend

```powershell
cd D:\Razorpay\backend
Copy-Item .env.example .env
# set RAZORPAY_WEBHOOK_SECRET to a fresh random string, e.g. (32+ chars)
# leave RAZORPAY_API_ENABLED=false unless you intend to test real Payment Links
alembic upgrade head
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

Verify locally first:

```powershell
$env:RAZORPAY_WEBHOOK_SECRET="<same as .env>"
python scripts/send_test_webhook.py payment.failed --payment-id pay_local_1 --amount 4999 --error-reason card_expired
curl.exe http://localhost:8000/cases | python -m json.tool
```

Expect `200 {status: processed, case_id: <uuid>, case_state: ACTION_SCHEDULED}`.

### 2. Obtain temporary HTTPS endpoint

Choose one — none is a dependency of RecoveryOS:

- `cloudflared tunnel --url http://localhost:8000`
- `ngrok http 8000`
- `ssh -R 80:localhost:8000 serveo.net`
- Deployed backend URL (if using docker-compose.prod.yml on a VPS)

Record the HTTPS URL in the same PowerShell session:

```powershell
$tunnel = 'https://actual-subdomain.trycloudflare.com'
```

Test loopback through tunnel:

```powershell
curl.exe "$tunnel/health"
curl.exe "$tunnel/ready"
```

`/health` must return `{"status":"ok","service":"recoveryos-backend","database":"connected"}`. Do not paste secrets.

### 3. Configure Razorpay Test Mode webhook

In Razorpay Dashboard → **Settings → Webhooks (Test Mode)**:

- URL: `https://<tunnel>/webhooks/razorpay`
- Secret: same value as `RAZORPAY_WEBHOOK_SECRET` (copy securely, never commit)
- Events: enable at least `payment.failed`, `payment.captured`, `payment_link.paid` (optionally `payment.dispute.created`, `subscription.*`)
- Save

Razorpay will send `x-razorpay-signature` (HMAC-SHA256 of raw body with secret) and `x-razorpay-event-id` for idempotency.

### 4. Send Test Mode event and verify

Trigger a test payment failure from Razorpay Dashboard Test Mode, or keep using local signed script through the tunnel:

```powershell
$env:RAZORPAY_WEBHOOK_SECRET="<same>"
python scripts/send_test_webhook.py payment.failed --url "$tunnel/webhooks/razorpay" --payment-id pay_tunnel_1 --amount 4999 --error-reason card_expired
```

Expected response through tunnel:

```json
{"status":"processed","event_type":"payment.failed","case_id":"...","case_state":"ACTION_SCHEDULED"}
```

Then inspect:

```powershell
$caseId = 'actual-case-uuid'
$headers = @{ 'X-Demo-Admin-Token' = $env:DEMO_ADMIN_TOKEN }
Invoke-RestMethod -Uri "$tunnel/cases/$caseId" -Headers $headers | ConvertTo-Json -Depth 10
```

Check:

- `failure_category` = `INVALID_INSTRUMENT`
- `decisions[0].chosen_action` = `CREATE_PAYMENT_LINK`
- `actions[0].status` = `SCHEDULED` then after worker → `EXECUTED` with `simulated` or real `plink_*`

Send duplicate event id:

```powershell
python scripts/send_test_webhook.py payment.failed --url "$tunnel/webhooks/razorpay" --payment-id pay_tunnel_1 --amount 4999 --error-reason card_expired --event-id evt_dup_test
python scripts/send_test_webhook.py payment.failed --url "$tunnel/webhooks/razorpay" --payment-id pay_tunnel_1 --amount 4999 --error-reason card_expired --event-id evt_dup_test
# second should return {"status":"ignored","reason":"duplicate_event"}
```

### 5. WebSocket/intervals are not required

Polling covers judge demo; no `WebSocket` needed.

### 6. Fallback when tunnel unavailable

Use local `send_test_webhook.py` with `DEFAULT_URL=http://localhost:8000/webhooks/razorpay` — signature verification and idempotency semantics are identical, only transport differs. Demo script’s offline fallback describes this.

### 7. Teardown

- Remove webhook URL from Razorpay Dashboard when demo ends.
- `docker compose down` or stop tunnel.
- Rotate `RAZORPAY_WEBHOOK_SECRET` if it was ever exposed.
- Do not leave `DEMO_ADMIN_TOKEN` hard-coded in env or frontend build (sessionStorage only).

## Security notes

- Webhook endpoint is the only public-by-necessity endpoint; it is HMAC-authenticated, fails closed when secret empty, and dedupes via unique `razorpay_event_id`.
- Operator reads (`GET /cases*`, `GET /dashboard/summary`, `GET /experiments*`) and mutations (`POST /experiments`, `POST /cases/{id}/customer-reply`, `POST /admin/demo-reset`) are gated when `DEMO_ADMIN_TOKEN_ENABLED=true`. Token is never baked into the frontend build; it is entered at runtime via `sessionStorage`.
- `/`, `/health`, `/live`, and `/ready` are public but sanitized — no secrets or connection strings.
- Large experiments are bounded by `EXPERIMENT_MAX_COUNT` (default 1000); dashboard demo uses 100.
