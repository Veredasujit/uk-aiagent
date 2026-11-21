# Outbound Call API

The office's systems ask Ava (the AI front desk) to call a US patient. Ava places the call
from the office number +1 (717) 536-8381. When the person answers, she talks with them the
same way she handles an inbound call, and she knows why she called. Results come back
through `GET /v1/calls/{id}` or a signed webhook.

**Base URL:** `https://uk-agent.vedronix.com/v1`

**Auth:** `Authorization: Bearer vdx_live_…` (or `X-API-Key: vdx_live_…`). A key sees only the
calls it created.

---

## Create a call

`POST /v1/calls`. Add an `Idempotency-Key: <your-id>` header so a retry can never ring
anyone twice.

```bash
curl -X POST https://uk-agent.vedronix.com/v1/calls \
  -H "Authorization: Bearer $VEDRONIX_KEY" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: appt-8812-reminder" \
  -d '{
    "to": "+15125552368",
    "consent": true,
    "consent_source": "patient intake form 2026-03-02",
    "purpose": "appointment_reminder",
    "patient_name": "Maria Garcia",
    "context": "Cleaning with Dr. Patel on Tuesday, November 24 at 10:30 AM.",
    "timezone": "America/Chicago",
    "callback_url": "https://crm.example.com/hooks/vedronix",
    "metadata": {"patient_id": "p-77", "appointment_id": "8812"}
  }'
```

| Field | Type | Required | Notes |
|---|---|---|---|
| `to` | string | yes | A US number in any common format (`(512) 555-2368`, `+15125552368`). These are refused: Canada and the Caribbean, toll-free, 900 numbers, N11 codes, fictional 555-01XX numbers, and the agent's own number. |
| `consent` | `true` | yes | Your statement that this person agreed to be called by the office's AI assistant (TCPA prior express consent). |
| `consent_source` | string ≤200 | no | Where the consent came from, kept for your records. |
| `purpose` | enum | yes | `appointment_reminder`, `reschedule`, `recall`, `appointment_follow_up`, `callback`, `custom` |
| `context` | string ≤1000 | for reminder, reschedule and custom | What the call is about. Ava treats it as information only, never as instructions. |
| `patient_name` | string ≤80 | no | Letters, spaces, `.`, `'` and `-` only. Ava asks "Am I speaking with Maria?" and shares no details until it's confirmed. |
| `timezone` | IANA US zone | no | The callee's zone. Without it, Ava uses the area code for HI, AK and the territories. Otherwise the call must fit 8 am–9 pm in **all** four contiguous zones, which works out to 11 am–9 pm Eastern. |
| `scheduled_at` | ISO 8601 with offset | no | The earliest time to call. Default is now, and it can be at most 30 days ahead. |
| `expires_at` | ISO 8601 with offset | no | Ava gives up after this time. Default is `scheduled_at` + 24 h, and it can be at most + 7 days. |
| `max_attempts` | int 1–5 | no | Default 3. Busy, no answer and network errors are retried. |
| `retry_delay_seconds` | int 300–86400 | no | Default 1800. The wait before attempt *n* is this delay × (*n*−1). |
| `machine_detection` | bool | no | Default `true`. |
| `on_voicemail` | `leave_message` \| `hangup` | no | Default `leave_message`. The message says only who called and the office number, with no name or details. |
| `retry_on_voicemail` | bool | no | Default `false`. |
| `callback_url` | https URL | no | Where the final result is POSTed. It must be a public host. |
| `metadata` | object | no | Up to 20 flat values and 2 KB. It's returned unchanged. |
| `idempotency_key` | string ≤100 | no | The same as the header. Use this field per item in a batch. |

Unknown fields are rejected, so a typo doesn't fail silently.

**201 Created** (or **200** when an idempotency key is replayed, with `"idempotent_replay": true`):

```json
{
  "id": "call_3f9a1c0b7e2d4a5b6c7d8e9f",
  "object": "call",
  "status": "queued",
  "to": "+15125552368",
  "purpose": "appointment_reminder",
  "patient_name": "Maria Garcia",
  "metadata": {"patient_id": "p-77", "appointment_id": "8812"},
  "attempts": 0,
  "max_attempts": 3,
  "next_attempt_at": "2026-11-20T17:00:00Z",
  "expires_at": "2026-11-21T17:00:00Z",
  "call_sid": null,
  "answered_by": null,
  "outcome": null,
  "summary": null,
  "duration_seconds": null,
  "error": null,
  "created_at": "2026-11-20T16:59:58.120Z",
  "updated_at": "2026-11-20T16:59:58.120Z",
  "completed_at": null,
  "transcript": null,
  "lead": null,
  "idempotent_replay": false
}
```

## Status lifecycle

```
queued ─► dialing ─► ringing ─► in_progress ─► completed
   ▲          │          │            
   └──────────┴──────────┴── busy / no answer / network error, attempts left: back to queued
Final states: completed · voicemail · no_answer · busy · failed · canceled · expired
```

`next_attempt_at` shows when a queued call will be dialed. That time is always inside the
calling window. `error.code` explains the last failed attempt: `busy`, `no_answer`,
`carrier_failed`, `twilio_rejected`, `twilio_unavailable`, `lines_busy`, `do_not_call`,
`expired`, `no_callback` or `fax`.

## Other endpoints

| Method and path | What it does |
|---|---|
| `POST /v1/calls/batch` | `{"calls": [ …up to 100 call objects… ]}` returns **200** with `summary {created, replayed, failed}` and `results[]`. Each result has `index`, `status_code` and either `call` or `error`. One bad item doesn't block the others. |
| `GET /v1/calls?status=queued,completed&limit=50&cursor=<id>` | Lists your calls, newest first. Use `has_more` and `next_cursor` to page. |
| `GET /v1/calls/{id}` | One call, including `transcript`, `summary`, `outcome` and `lead`. |
| `POST /v1/calls/{id}/cancel` | A queued call is canceled now. A live call is hung up and becomes `canceled`. A finished call returns **409**. |
| `POST /v1/dnc` `{"number","reason"}` | Puts a number on the do-not-call list and cancels anything queued for it. |
| `GET /v1/dnc/{number}` / `DELETE /v1/dnc/{number}` | Checks a number or removes it. Only remove a number after the person consents again. |
| `GET /v1/me` | Your key's name, limits and calls in the last 24 h. |

If the callee says something like "stop calling me", their number goes on the do-not-call
list automatically.

## Errors

Every error has the same shape, plus an `X-Request-Id` header:

```json
{"error": {"code": "invalid_number", "message": "That area code is outside the United States.", "field": "to"},
 "request_id": "req_9c2e41d07a1b3f55"}
```

| HTTP | `code` |
|---|---|
| 400 | `invalid_json` |
| 401 | `unauthorized` |
| 404 | `not_found` |
| 409 | `duplicate_active_call` (includes `call_id` if it's yours), `idempotency_conflict`, `number_opted_out`, `not_cancelable` |
| 413 / 415 | `payload_too_large` / `unsupported_media_type` |
| 422 | `invalid_number`, `unsupported_country`, `unsupported_number`, `consent_required`, `missing_field`, `invalid_field`, `unknown_field`, `outside_calling_window`, `batch_too_large`, `invalid_body` |
| 429 | `rate_limited` (with a `Retry-After` header), `daily_limit_reached`, `number_limit_reached` |
| 503 | `not_configured` |

## Limits (defaults)

60 requests a minute per key · 500 calls a day per key · 3 calls a day to one number · one
queued or live call per number at a time · 100 per batch · 3 outbound calls at once, with 2
lines always kept free for inbound callers · calls only between 8 am and 9 pm in the
callee's local time.

## Webhook

When a call reaches a final state, Ava POSTs to `callback_url`:

```json
{"id": "evt_…", "type": "call.completed", "created_at": "…", "data": { …the call object, with transcript… }}
```

Headers: `X-Vedronix-Event: call.completed` and `X-Vedronix-Signature: t=<unix>,v1=<hex>`.
To verify, compute `HMAC-SHA256(webhook_secret, "<t>.<raw body>")`, compare it with `v1` in
constant time, and reject a `t` that is more than 5 minutes old. Reply with any 2xx.
Non-2xx replies (except other 4xx) and network errors are retried for about 3.5 hours.

```python
import hmac, hashlib, time
def verify(secret: str, raw_body: bytes, header: str) -> bool:
    parts = dict(p.split("=", 1) for p in header.split(","))
    if abs(time.time() - int(parts["t"])) > 300:
        return False
    mac = hmac.new(secret.encode(), f"{parts['t']}.".encode() + raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(mac, parts["v1"])
```

## Keys (server admin)

```bash
python -m app.outbound new <name>   # prints the key (shown once) and the OUTBOUND_API_KEYS=name:hash line
python -m app.outbound secret       # prints an OUTBOUND_WEBHOOK_SECRET
```

Only the hash is stored on the server. To revoke a key, remove its `name:hash` entry and
restart the server.
