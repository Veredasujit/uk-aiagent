"""
server.py — the Vedronix UK voice agent (uk-agent.vedronix.com).

  POST /twilio/voice     Twilio: an incoming call (phone number or browser) → TwiML
  WS   /twilio/stream    Twilio Media Stream: the call's audio, both ways
  POST /twilio/status    Twilio: call status changes (cleans up anything left running)
  POST /twilio/fallback  Twilio: used only if /twilio/voice fails
  POST /web/token        a browser asks to call the agent → short-lived Voice SDK token
  POST /admin/test-call  ring an allowlisted test number (X-Admin-Key); the agent talks
  /v1/calls…             the outbound call API (API key; outbound.py): queue, track, cancel
  GET  /web/events       that browser's live transcript (Server-Sent Events)
  GET  /health

Phone callers reach us through the number's Voice URL; browser callers through a TwiML App
(Voice JS SDK, WebRTC) whose Voice URL is the same /twilio/voice. Both end in the same
<Connect><Stream>, so both get the same agent. Outbound: only calls the /v1 API queued
(their webhooks carry a token we signed for that job) and the allowlisted test call.

Security: every Twilio request is signature-checked; the stream only starts with a token
we signed for that CallSid; browser tokens are origin-checked, rate-limited per IP, and
can only reach our TwiML App; transcript streams need a key signed for that browser.
"""
from __future__ import annotations

import asyncio
import json
import secrets
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse
from twilio.twiml.voice_response import Connect, Stream, VoiceResponse

from . import business, config, notify, store, twilio_api, whatsapp
from . import demo_call
from . import outbound as outbound_mod
from .logging_setup import error, info, setup_logging, warn
from .session import POSTCALL_TASKS, CallSession
from .twilio_bridge import TwilioBridge

LIVE: dict[str, CallSession] = {}        # CallSid → session
CHANNEL: dict[str, str] = {}             # CallSid → "phone" | "web"
WEB_CALLS: dict[str, str] = {}           # browser identity → CallSid
# UK: Polly's UK English neural voice. Amy is the standard UK-English neural voice.
SAY_VOICE = "Polly.Amy-Neural"


@asynccontextmanager
async def lifespan(_: FastAPI):
    setup_logging()
    missing = config.missing_required()
    if missing:
        warn("boot", f"missing settings: {', '.join(missing)}")
    store._db()
    from . import vad

    asyncio.create_task(asyncio.to_thread(vad.preload, config.MAX_CONCURRENT_CALLS), name="vad-preload")
    asyncio.create_task(_cache_greetings(), name="greeting-cache")
    wa_worker = asyncio.create_task(notify.worker(), name="whatsapp-outbox")
    outbound_mod.set_live_counter(lambda: len(LIVE))
    ob_worker = asyncio.create_task(outbound_mod.worker(), name="outbound-dialer")
    info("boot", f"UK agent up on {config.PUBLIC_HOST} ({_voice()['provider']}:{_voice()['voice_id']}, fallback {config.TTS_FALLBACK_PROVIDER})")
    yield
    for s in list(LIVE.values()):
        await s.hangup_now("shutdown")
    if POSTCALL_TASKS:
        await asyncio.wait(list(POSTCALL_TASKS), timeout=20)
    wa_worker.cancel()
    ob_worker.cancel()
    await whatsapp.close()


app = FastAPI(title="Vedronix UK agent", lifespan=lifespan, docs_url=None, redoc_url=None)
app.include_router(outbound_mod.router)
app.include_router(demo_call.router)
app.add_middleware(CORSMiddleware, allow_origins=config.WEB_ALLOWED_ORIGINS, allow_methods=["GET", "POST"],
                   # Authorization lets the vedronix.com/outbound console call /v1 with the key the owner pastes
                   # (kept in their browser only, never in the site's code)
                   allow_headers=["Content-Type", "Authorization"], max_age=600)


# ── helpers ──────────────────────────────────────────────────────────────────
def _public_url(request: Request) -> str:
    q = f"?{request.url.query}" if request.url.query else ""
    return f"https://{config.PUBLIC_HOST}{request.url.path}{q}"


async def _twilio_form(request: Request) -> dict[str, str]:
    form = await request.form()
    params = {k: str(v) for k, v in form.items()}
    sig = request.headers.get("x-twilio-signature", "")
    if not twilio_api.valid_signature(_public_url(request), params, sig):
        warn("twilio", f"bad signature on {request.url.path}")
        raise HTTPException(403, "invalid signature")
    return params


def _twiml(r: VoiceResponse) -> Response:
    return Response(content=str(r), media_type="application/xml")


def _hang_up() -> Response:
    r = VoiceResponse()
    r.hangup()
    return _twiml(r)


def _outbound_busy(job_id: str, token: str, p: dict) -> Response:
    """An API call was answered but every line is taken: apologise, try again later."""
    if outbound_mod.check_token(job_id, token):
        job = outbound_mod._get(job_id)
        if job and job.get("call_sid") == p.get("CallSid") and not job.get("finalized"):
            outbound_mod._retry_or_final(job, "failed", "lines_busy: every line was taken when the call was answered")
    return _say_and_hang_up(f"Hi, this is {config.AGENT_NAME} from {config.BUSINESS_NAME}. Sorry, we'll call you back "
                            "in a little while. Goodbye.")


def _say_and_hang_up(text: str) -> Response:
    r = VoiceResponse()
    r.say(text, voice=SAY_VOICE)
    r.hangup()
    return _twiml(r)


def _mask(number: str) -> str:
    if number.startswith("client:"):
        return "browser"
    d = "".join(ch for ch in number if ch.isdigit())
    return f"+{d[:1]}•••{d[-4:]}" if len(d) >= 8 else ("web" if number.startswith("client:") else "unknown")


def _client_ip(request: Request) -> str:
    return (request.headers.get("x-real-ip") or request.headers.get("x-forwarded-for", "").split(",")[0].strip()
            or (request.client.host if request.client else "?"))


def _count(channel: Optional[str] = None) -> int:
    return sum(1 for sid in LIVE if channel is None or CHANNEL.get(sid) == channel)


# ── Twilio: incoming call ────────────────────────────────────────────────────
@app.post("/twilio/voice")
async def voice(request: Request) -> Response:
    p = await _twilio_form(request)
    call_sid = p.get("CallSid", "")
    caller = p.get("From", "")
    web = caller.startswith("client:")
    info("twilio", f"incoming {call_sid} {'web' if web else 'phone'} from {_mask(caller)} country={p.get('FromCountry', '-')}")

    if not web and not (p.get("Direction") or "").startswith("outbound"):
        # UK: if we can't tell where the caller is, and the number looks like a UK one,
        # allow it (most of our traffic is +44). Non-UK callers get a polite notice.
        country = (p.get("FromCountry") or p.get("CallerCountry") or "").upper()
        if not country:
            # UK numbers start with +44; Twilio sometimes omits FromCountry on test calls.
            country = "GB" if caller.startswith("+44") else "unknown"
        if country not in config.ALLOWED_CALLER_COUNTRIES:
            info("twilio", f"{call_sid}: caller country {country or 'unknown'} not allowed")
            return _say_and_hang_up(f"Thanks for calling {config.BUSINESS_NAME}. This line is only available to callers "
                                    "in the United Kingdom. Goodbye.")
    if _count() >= config.MAX_CONCURRENT_CALLS or (web and _count("web") >= config.MAX_CONCURRENT_WEB_CALLS):
        warn("twilio", f"{call_sid}: all lines busy ({_count()} live)")
        if request.query_params.get("ob") and (p.get("Direction") or "").startswith("outbound"):
            return _outbound_busy(request.query_params.get("ob", ""), request.query_params.get("t", ""), p)
        return _say_and_hang_up(f"Thanks for calling {config.BUSINESS_NAME}. All of our lines are busy right now. "
                                "Please try again in a few minutes. Goodbye.")

    outbound = (p.get("Direction") or "").startswith("outbound")
    job_id = request.query_params.get("ob", "")
    if outbound and job_id:
        if not outbound_mod.check_token(job_id, request.query_params.get("t", "")):
            warn("twilio", f"{call_sid}: outbound job token invalid, hanging up")
            return _hang_up()
        action, job = await store.run(outbound_mod.on_voice, job_id, p)
        if action == "voicemail":
            return _say_and_hang_up(outbound_mod.voicemail_text(job))
        if action != "agent":
            return _hang_up()
    elif outbound and _canonical(p.get("To", "")) not in _test_numbers():
        warn("twilio", f"{call_sid}: outbound to a number not on the test list, hanging up")
        return _say_and_hang_up("Goodbye.")
    channel = "web" if web else "phone"
    if not web:  # for WhatsApp after booking / after the call; kept in memory only
        notify.remember_caller(call_sid, p.get("To", "") if outbound else caller)
    r = VoiceResponse()
    connect = Connect()
    stream = Stream(url=f"wss://{config.PUBLIC_HOST}/twilio/stream")
    stream.parameter(name="token", value=twilio_api.sign("stream", call_sid))
    stream.parameter(name="channel", value=channel)
    stream.parameter(name="caller", value=caller[len("client:"):] if web else _mask(p.get("To", "") if outbound else caller))
    stream.parameter(name="direction", value="outbound" if outbound else "inbound")
    if outbound and job_id:
        stream.parameter(name="job", value=job_id)
    connect.append(stream)
    r.append(connect)
    r.hangup()  # when the stream ends, so does the call
    return _twiml(r)


@app.post("/twilio/status")
async def status(request: Request) -> dict:
    p = await _twilio_form(request)
    call_sid = p.get("CallSid", "")
    st = (p.get("CallStatus") or "").lower()
    info("twilio", f"status {call_sid}: {st} ({p.get('CallDuration', '-')} s)")
    job_id = request.query_params.get("ob", "")
    if job_id and outbound_mod.check_token(job_id, request.query_params.get("t", "")):
        await store.run(outbound_mod.on_status, job_id, p)
    s = LIVE.get(call_sid)
    if s and st in ("completed", "failed", "busy", "no-answer", "canceled"):
        await s.hangup_now(f"twilio_{st}")
    return {"ok": True}


@app.post("/twilio/fallback")
async def fallback(request: Request) -> Response:
    p = await _twilio_form(request)
    error("twilio", f"fallback used for {p.get('CallSid')}: {p.get('ErrorCode')} {p.get('ErrorUrl', '')}")
    return _say_and_hang_up(f"Sorry, {config.BUSINESS_NAME} is having trouble answering right now. "
                            "Please try again in a few minutes. Goodbye.")


# ── Twilio: the call's audio ─────────────────────────────────────────────────
TEST_CALL_GREETING = (f"Hi! It's {config.AGENT_NAME}, the virtual assistant at {config.BUSINESS_NAME}, "
                      "calling for your test. How can I help?")

# The greetings in the agent's voice, made once at startup, so a call starts talking the moment
# it connects (2026-11-14: synthesising it per call added ~1 s to the silence after pickup).
GREETINGS: dict[str, bytes] = {}


def _voice() -> dict:
    vid = config.ELEVEN_VOICE_ID if config.TTS_PROVIDER == "elevenlabs" else config.DEEPGRAM_TTS_VOICE
    # UK: default language tag is en-GB, not en-US.
    return {"provider": config.TTS_PROVIDER, "voice_id": vid, "gender": "female", "language": "en-GB"}


async def _cache_greetings() -> None:
    from .llm import speakable
    from .logging_setup import CallLog
    from .tts import TTSEngine

    for text in (business.GREETING, TEST_CALL_GREETING, outbound_greeting(None)):
        engine = None
        try:
            v = _voice()
            # UK: language="en-GB" for UK English voices.
            engine = TTSEngine(CallLog("greeting-cache"), provider=v["provider"], voice=v["voice_id"], language="en-GB")
            await engine.prewarm()
            buf = bytearray()
            async for pcm in engine.stream(speakable(text)):
                buf.extend(pcm)
            if len(buf) > 16000:
                GREETINGS[text] = bytes(buf)
                info("boot", f"greeting cached ({len(buf) / 16000:.1f} s, {engine.vendor})")
        except Exception as exc:
            warn("boot", f"greeting not cached, it will be synthesised per call: {exc}")
        finally:
            if engine is not None:
                await engine.close()


def outbound_greeting(job: Optional[dict]) -> str:
    """The AI disclosure comes first; nothing about why we're calling until we know who answered."""
    first = ((job or {}).get("patient_name") or "").split(" ")[0].strip().title()
    if (job or {}).get("purpose") == "demo":
        return (f"Hi{f' {first}' if first else ''}! It's {config.AGENT_NAME}, the AI receptionist at {config.BUSINESS_NAME}, "
                "calling for the demo you asked for on the Vedronix website. How can I help you today?")
    return (f"Hi, this is {config.AGENT_NAME}, an AI assistant calling from {config.BUSINESS_NAME}. "
            + (f"Am I speaking with {first}?" if first else "Do you have a quick minute?"))


def _session_cfg(call_sid: str, channel: str, caller: str, direction: str = "inbound",
                 job: Optional[dict] = None) -> dict:
    limit = config.MAX_WEB_CALL_SECONDS if channel == "web" or (job or {}).get("purpose") == "demo" else config.MAX_CALL_SECONDS
    greeting = outbound_greeting(job) if job else TEST_CALL_GREETING if direction == "outbound" else business.GREETING
    return {
        "outbound": {"purpose": job["purpose"], "patient_name": job.get("patient_name") or "",
                     "context": job.get("context") or ""} if job else None,
        "session_id": call_sid, "channel": channel, "caller": caller,
        "ends_at": time.time() + limit, "tts_chars_left": config.MAX_TTS_CHARS_PER_SESSION,
        "agent": {
            # UK: language tag is en-GB (was "en").
            "name": config.AGENT_NAME, "business_name": config.BUSINESS_NAME, "language": "en-GB",
            "voice": _voice(),
            # The office facts sit in the prompt (small), so answers need no extra tool round
            "functions": ["collect_info", "check_availability", "book_appointment"],
            "faq": business.FAQ, "greeting": greeting,
        },
        "greeting_pcm": GREETINGS.get(greeting),
    }


@app.websocket("/twilio/stream")
async def media_stream(ws: WebSocket) -> None:
    await ws.accept()
    bridge = TwilioBridge(ws)
    if not await bridge.wait_start():
        await bridge.close()
        return
    call_sid = bridge.call_sid or ""
    if not twilio_api.check(bridge.params.get("token", ""), "stream", call_sid) or call_sid in LIVE:
        warn("stream", f"refused stream for {call_sid or '?'}")
        await bridge.close()
        return
    channel = bridge.params.get("channel", "phone")
    caller = bridge.params.get("caller", "")
    job = await store.run(outbound_mod.stream_cfg, call_sid) if bridge.params.get("job") else None
    session = CallSession(bridge, call_sid, _session_cfg(call_sid, channel, caller, bridge.params.get("direction", "inbound"),
                                                         job))
    LIVE[call_sid] = session
    CHANNEL[call_sid] = channel
    if channel == "web":
        WEB_CALLS[caller] = call_sid
        _relay_transcript(session, caller)
        HUB.publish(caller, {"type": "status", "status": "connected", "call_sid": call_sid})
    try:
        await session.run()
    except Exception as exc:
        error("stream", f"session crashed: {exc}")
        await session.shutdown()
    finally:
        LIVE.pop(call_sid, None)
        CHANNEL.pop(call_sid, None)
        if channel == "web":
            WEB_CALLS.pop(caller, None)
            HUB.publish(caller, {"type": "status", "status": "ended", "reason": session.end_reason})
        await bridge.close()
        # Belt and braces: the call must not outlive the agent
        asyncio.create_task(twilio_api.hangup(call_sid, session.end_reason or "stream_closed"))


# ── browser calls (Twilio Voice JS SDK, WebRTC) ──────────────────────────────
_TOKENS: dict[str, deque] = defaultdict(deque)


@app.post("/web/token")
async def web_token(request: Request) -> JSONResponse:
    origin = request.headers.get("origin", "")
    if origin not in config.WEB_ALLOWED_ORIGINS:
        raise HTTPException(403, "This site can't start calls with the agent.")
    ip = _client_ip(request)
    now = time.time()
    q = _TOKENS[ip]
    while q and now - q[0] > 3600:
        q.popleft()
    if len(q) >= config.WEB_TOKENS_PER_IP_PER_HOUR:
        raise HTTPException(429, "You've made a lot of test calls in the last hour. Please try again a little later.")
    if _count("web") >= config.MAX_CONCURRENT_WEB_CALLS or _count() >= config.MAX_CONCURRENT_CALLS:
        raise HTTPException(503, "All lines are busy right now. Please try again in a minute.")
    if config.missing_required():
        raise HTTPException(503, "The agent isn't fully set up yet.")
    q.append(now)
    identity = f"web-{secrets.token_hex(8)}"
    return JSONResponse({
        "token": twilio_api.browser_token(identity),
        "identity": identity,
        "events_url": f"https://{config.PUBLIC_HOST}/web/events?identity={identity}&key={twilio_api.sign('view', identity)}",
        "expires_in": config.WEB_TOKEN_TTL_S,
        "max_call_seconds": config.MAX_WEB_CALL_SECONDS,
    }, headers={"Cache-Control": "no-store"})


class Hub:
    """Live transcript fan-out, per browser identity."""

    def __init__(self) -> None:
        self.subs: dict[str, set[asyncio.Queue]] = defaultdict(set)

    def publish(self, identity: str, event: dict) -> None:
        for q in list(self.subs.get(identity, ())):
            if q.qsize() < 200:
                q.put_nowait(event)

    def subscribe(self, identity: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self.subs[identity].add(q)
        return q

    def unsubscribe(self, identity: str, q: asyncio.Queue) -> None:
        self.subs[identity].discard(q)
        if not self.subs[identity]:
            self.subs.pop(identity, None)


HUB = Hub()


def _relay_transcript(session: CallSession, identity: str) -> None:
    original = session._note

    def note(who: str, text: str) -> None:
        original(who, text)
        if text and text.strip():
            HUB.publish(identity, {"type": "transcript", "who": who, "text": text.strip()})

    session._note = note  # type: ignore[method-assign]


@app.get("/web/events")
async def web_events(identity: str, key: str) -> StreamingResponse:
    if not identity.startswith("web-") or not twilio_api.check(key, "view", identity):
        raise HTTPException(403, "invalid key")
    q = HUB.subscribe(identity)

    async def gen():
        try:
            yield "retry: 2000\n\n"
            if identity in WEB_CALLS:
                yield f"data: {json.dumps({'type': 'status', 'status': 'connected'})}\n\n"
            deadline = time.monotonic() + config.MAX_WEB_CALL_SECONDS + 120
            while time.monotonic() < deadline:
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
                if ev.get("type") == "status" and ev.get("status") == "ended":
                    return
        finally:
            HUB.unsubscribe(identity, q)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


# ── test calls out (allowlisted numbers only) ─────────────────────────────────
def _canonical(number: str) -> str:
    """UK canonicalisation:
      * '+44…' stays as-is
      * '0044…' → '+44…'
      * '0…' (national) → '+44…'
      * '44…' (no plus) → '+44…'
    """
    d = "".join(ch for ch in number if ch.isdigit())
    if number.startswith("+") and d.startswith("44"):
        return f"+{d}"
    if d.startswith("0044"):
        return f"+44{d[4:]}"
    if d.startswith("44"):
        return f"+{d}"
    if d.startswith("0"):
        # UK national: strip leading 0, add 44
        return f"+44{d[1:]}"
    # UK: an Indian number without country code was previously auto-prefixed with 91.
    # Now, anything that isn't clearly UK stays as-is and will fail the test-list check.
    return f"+{d}" if d else ""


def _test_numbers() -> set[str]:
    return {_canonical(n) for n in config.OUTBOUND_TEST_NUMBERS if _canonical(n)}


_TEST_CALLS: deque = deque()


@app.post("/admin/test-call")
async def admin_test_call(request: Request) -> JSONResponse:
    key = request.headers.get("x-admin-key", "")
    if not config.ADMIN_API_KEY or not secrets.compare_digest(key, config.ADMIN_API_KEY):
        raise HTTPException(401, "admin key required")
    try:
        body = await request.json()
    except Exception:
        body = {}
    allowed = _test_numbers()
    to = _canonical(str((body or {}).get("to") or "")) or (sorted(allowed)[0] if len(allowed) == 1 else "")
    if not to or to not in allowed:
        raise HTTPException(403, "That number isn't on the test-call list (OUTBOUND_TEST_NUMBERS).")
    now = time.time()
    while _TEST_CALLS and now - _TEST_CALLS[0] > 3600:
        _TEST_CALLS.popleft()
    if len(_TEST_CALLS) >= config.OUTBOUND_TEST_PER_HOUR:
        raise HTTPException(429, f"At most {config.OUTBOUND_TEST_PER_HOUR} test calls an hour.")
    if _count() >= config.MAX_CONCURRENT_CALLS:
        raise HTTPException(503, "All lines are busy right now.")
    try:
        result = await twilio_api.test_call(to)
    except Exception as exc:
        error("twilio", f"test call failed: {exc}")
        raise HTTPException(502, str(exc))
    _TEST_CALLS.append(now)
    return JSONResponse({"ok": True, "to": f"…{to[-4:]}", **result})


@app.get("/health")
async def health() -> dict:
    missing = config.missing_required()
    return {"ok": not missing, "missing_env": missing, "live_calls": len(LIVE), "web_calls": _count("web"),
            "voice": f"{_voice()['provider']}:{_voice()['voice_id']}", "fallback_voice": config.TTS_FALLBACK_PROVIDER,
            "greetings_cached": len(GREETINGS), "number": config.TWILIO_PHONE_NUMBER,
            "outbound": await store.run(outbound_mod.health),
            "whatsapp": {"enabled": config.WHATSAPP_ENABLED, **(await store.run(store.wa_counts))},
            "service": "vedronix-uk-agent", "host": config.PUBLIC_HOST}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "app.server:app",
        host="0.0.0.0",
        port=8823,
        workers=1
    )