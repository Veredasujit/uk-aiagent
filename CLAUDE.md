# CLAUDE.md — Vedronix US voice agent ("Ava")

An AI front-desk agent for US dental offices: answers inbound calls, and places outbound calls the office queues through an authenticated API. Live at
**https://us-agent.vedronix.com** (VPS 72.62.229.135, `/opt/vedronix-us-agent`, container and
compose project `vedronix-us-agent`, port 8860 behind nginx). It answers for a sample office,
"Brightside Dental, Austin TX" (`app/business.py`), and is the US demo on vedronix.com.

## How calls reach it

```
US phone caller ──► Twilio number +1 (717) 536-8381 ──┐
                                                      ├─► POST /twilio/voice (signature-checked)
Browser (vedronix.com, Twilio Voice JS SDK, WebRTC) ──┘     → TwiML <Connect><Stream>
     ▲ token: POST /web/token (origin + per-IP limit)         ▼
     └ live transcript: GET /web/events (SSE)          WS /twilio/stream (μ-law 8 kHz, both ways)
                                                             ▼
                twilio_bridge.py → session.py: Deepgram nova-3 en-US → gpt-4o-mini (tools) →
                ElevenLabs "Sarah" (fallback: Sarvam English) → back to Twilio; store.py for slots + records
```

- Phone and browser calls end in the **same** `<Connect><Stream>`, so they get the same agent.
- The browser's TwiML App (`TWILIO_TWIML_APP_SID`) points at `/twilio/voice` and nowhere else;
  access tokens have `incoming_allow=False`. Outbound calls only via `/v1/calls` (see below).
- Callers whose `FromCountry` isn't US hear a short notice and the call ends.
- `MAX_CONCURRENT_CALLS` / `MAX_CONCURRENT_WEB_CALLS` cap load; beyond them callers hear
  "all lines are busy". Calls are capped at 10 min (phone) / 5 min (browser).

## Files

`twilio_bridge.py` makes a Twilio Media Stream look like the FreJun/Teler socket the pipeline
was tuned on, so `session.py`, `playout.py`, barge-in, VAD and the latency work are unchanged
from the Vedronix/Sakshi agents. Treat those as tuned on real calls; don't refactor casually.
New here: `server.py`, `twilio_api.py`, `twilio_bridge.py`, `tts_deepgram.py`, `store.py`,
`business.py`, `prompt.py`, `twilio_setup.py`. `backend.py` is a local stand-in for the old
Vedronix backend client (same function names).

## Rules that matter

- **Booking truth.** The agent may only say "you're all set / booked / confirmed" after
  `book_appointment` succeeded on that call; `session._guard_booking_claims` strips such
  phrases otherwise. It can't see existing appointments and must never vouch for one.
- **Only offer times a `check_availability` result returned for that exact date.** Results
  include `office_hours`, so it can say "we close at 1 on Saturdays" instead of inventing
  "fully booked".
- **No invented facts.** Office facts live in `business.FAQ` only (a sample practice).
- US callers only. Outbound happens two ways only: `POST /admin/test-call` (2026-11-14; rings only
  `OUTBOUND_TEST_NUMBERS`, `X-Admin-Key`) and the **outbound call API** below. `/twilio/voice` hangs up
  any outbound call that carries neither a valid job token (`?ob=&t=`) nor a test-list number.

## Outbound call API (owner, 2026-11-20) — `app/outbound.py`, reference in `docs/OUTBOUND_API.md`

- `POST /v1/calls` (+ `/batch`, `GET`, `/cancel`, `/v1/dnc`, `/v1/me`), `Authorization: Bearer vdx_live_…`.
  Only sha256 hashes of keys on the server (`OUTBOUND_API_KEYS=name:hash`); each key sees only its calls.
- Every call is a row in `store.outbound_calls`; one dispatcher loop dials what's due, max
  `OUTBOUND_MAX_CONCURRENT`, always leaving `OUTBOUND_INBOUND_RESERVE` lines for callers. Busy / no
  answer / Twilio 5xx retry up to `max_attempts`; sweeper fixes calls whose callbacks never came.
- Compliance guardrails, keep them: `consent: true` required; calls only 8 am-9 pm callee-local (all
  four contiguous zones when unknown); US numbers only (no Canada/Caribbean/toll-free/premium); DNC list,
  auto-filled when the callee says "stop calling"; `OUTBOUND_PER_NUMBER_PER_DAY`; greeting discloses
  "AI assistant" and shares nothing until the right person is confirmed; voicemail has no details.
- `context` from the API goes into the prompt between `<<<NOTES`/`NOTES>>>` as data, not instructions.
- Results: GET, or a webhook to `callback_url` signed `X-Vedronix-Signature: t=…,v1=HMAC(secret, "t.body")`,
  via `store.webhook_outbox` (retries, never to private IPs). Not for marketing or cold calls.
- "Get a call from Ava" on vedronix.com (`app/demo_call.py`, `/web/call-me/*`): the visitor's number gets a
  Twilio Verify code (`TWILIO_VERIFY_SERVICE_SID`) and Ava calls only after it comes back, as an outbound
  job with key `web-demo`, purpose `demo` (internal only), 1 attempt, 5-min cap. Limits: 3 codes/IP/hour,
  30 s between codes per number, `DEMO_CALLS_PER_DAY`. Never drop the code check: it's what stops anyone
  pointing the AI at a stranger's phone.
- Secrets live only in the server's `.env.production` (never commit them).

## WhatsApp to callers (owner, 2026-11-14)

Same Vedronix gateway as Sakshi (`whatsappev.vedronix.com`, `WHATSAPP_API_KEY`). `notify.py`:
**booking** confirmation the moment `book_appointment` succeeds; **thanks** after every call the
caller spoke on (front-desk wording when the outcome is callback/human requested); **missed**
when an inbound caller hung up silent after >= 5 s; **reminder** within 24 h of a visit booked
>= 20 h ahead. Only to the number that called (or the test number we rang); browser calls get
nothing. The full number is held in memory per call (`notify.CALLER_NUMBERS`) and in
`appointments.wa_phone`. Everything goes through `store.wa_outbox` (dedupe key per call /
appointment, retries with backoff ~1 h, survives restarts); `/health` shows its counts. Texts are
plain ASCII and state only `business.FAQ` facts; the footer says it's a sample office.

## Twilio

- Account `ACdc1a…`; number `+17175368381` (`PNa4a91…`): Voice URL `/twilio/voice`, fallback
  `/twilio/fallback`, status `/twilio/status`. API key `SK01cd…` ("Vedronix US agent") signs
  browser tokens and REST hang-ups; the auth token only validates webhook signatures.
- `python -m app.twilio_setup /path/.env.production` creates/updates the key, the TwiML App and
  the number (idempotent; touches nothing else in the account).
- **The account auth token was pasted into a chat on 2026-11-14: rotate it** in the Twilio
  console (other numbers' services may use it), then update `TWILIO_AUTH_TOKEN` here.

## Voice and first-word latency (owner, 2026-11-14: "starts talking after 5 s", "sounds like AI")

- Voice: ElevenLabs premade **Sarah** (`ELEVEN_VOICE_ID`), flash v2.5, most human-sounding we have.
  If it fails (e.g. the free plan's ~10k chars/month run out) **Sarvam English** takes over
  (`TTS_FALLBACK_PROVIDER=sarvam`). Deepgram Aura-2 is still wired (`TTS_PROVIDER=deepgram`).
- The 5 s after pickup was: Silero loaded lazily on the first call (~2 s), the voice socket
  opened only then (~0.75 s), then the greeting synthesised. Now: 6 Silero instances preloaded
  at startup (one per concurrent call; the model is stateful, never share one), and both
  greetings are synthesised once at startup (`server.GREETINGS`) and played at once.
  Measured after: greeting 35 ms after the stream starts; Twilio's stream setup to this
  server is ~1.1 s and is the remaining wait.
- Turn-taking: a fragment ending mid-thought ("And first of") waits 1.7 s before we answer,
  a full sentence 0.75 s (`session._quiet_needed`). "hi"/"hello" no longer cut the greeting.

## Facts measured on the live stack (2026-11-14)

- Deepgram Aura-2 over a kept-open websocket: ~300 ms to first audio, synthesis ~2x real time.
  A cold HTTPS request was ~850-930 ms. OpenAI TTS was ~1.5 s (fallback only).
- Real browser call through Twilio: caller's end of speech → agent audio ≈ 1.3 s
  (LLM first token ~770 ms + TTS ~290 ms).
- First real human call: a fake "you're all set" on a closed day → booking-truth rules + guard.
  gpt-4.1-mini was compared on the same transcript and hung up on "I wouldn't come"; gpt-4o-mini kept.
- Docker: install CPU-only torch first, or pip pulls >1 GB of CUDA wheels.

## Commands

```bash
.venv/bin/python -m pytest -q          # 71 tests: webhooks, tokens, bridge, a whole call, booking, guard, WhatsApp, outbound API, demo calls
./deploy/deploy.sh                     # rsync + detached docker build on the VPS + health check
curl https://us-agent.vedronix.com/health
```
