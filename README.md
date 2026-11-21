# Vedronix US voice agent

Inbound AI front desk for US dental offices, on Twilio. Phone callers (US numbers only) and
website visitors (Twilio Voice JS SDK over WebRTC) reach the same agent.

- Live: https://us-agent.vedronix.com/health
- Phone: +1 (717) 536-8381 (inbound only, US callers)
- Voice: Deepgram Aura-2 "Thalia" (American English); speech recognition: Deepgram nova-3 en-US;
  language model: OpenAI gpt-4o-mini.

## Endpoints

| | | |
|---|---|---|
| POST | `/twilio/voice` | Twilio incoming call (number or TwiML App) → TwiML `<Connect><Stream>` |
| WS | `/twilio/stream` | the call's audio (Twilio Media Stream, bidirectional) |
| POST | `/twilio/status`, `/twilio/fallback` | call status changes; backup when `/twilio/voice` fails |
| POST | `/web/token` | browser access token (allowed origins only, rate-limited per IP) |
| GET | `/web/events` | live transcript of that browser's call (Server-Sent Events) |
| GET | `/health` | settings check, live calls |

## Run and deploy

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt -r requirements-dev.txt
.venv/bin/python -m pytest -q
./deploy/deploy.sh        # to the VPS; nginx config in deploy/, env in /opt/vedronix-us-agent/.env.production
```

See `.env.example` for settings and `CLAUDE.md` for how it works and what was learned on live calls.



source .venv/bin/activate
python -m app.server
ngrok http 8823
https://cataract-unleveled-confess.ngrok-free.dev



curl -X POST http://localhost:8823/v1/calls \
  -H "Content-Type: application/json" \
  -d '{"to": "+917318079820"}'

  
curl -X POST http://localhost:8823/v1/calls \
  -H "Authorization: Bearer $VEDRONIX_KEY" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: appt-8812-reminder" \
  -d '{
    "to": "+917318079820",
    "consent": true,
    "consent_source": "patient intake form 2026-03-02",
    "purpose": "appointment_reminder",
    "patient_name": "Maria Go",
    "context": "Cleaning with Dr. Patel on Tuesday, November 24 at 10:30 AM.",
    "timezone": "Europe/London",
    "callback_url": "https://crm.example.com/hooks/vedronix",
    "metadata": {"patient_id": "p-77", "appointment_id": "8812"}
  }'
