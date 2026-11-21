# Vedronix UK voice agent

Inbound AI front desk for UK dental offices, on Twilio. Phone callers (UK numbers only) and
website visitors (Twilio Voice JS SDK over WebRTC) reach the same agent.

- Live: https://uk-agent.vedronix.com/health
- Phone: +447427893469 (inbound only, UK callers)
- Voice: Deepgram Aura-2 "Thalia" (London, English); speech recognition: Deepgram nova-3 en-GB;
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
./deploy/deploy.sh        # to the VPS; nginx config in deploy/, env in /opt/vedronix-uk-agent/.env.production
```

See `.env.example` for settings and `CLAUDE.md` for how it works and what was learned on live calls.



