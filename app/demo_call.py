"""
demo_call.py — "Get a call from Amelia" on vedronix.com (owner's request, 2026-11-20).

A visitor types their US number; we text them a 6-digit code (Twilio Verify) and only when
they type it back does Amelia call them. The code proves they hold that phone and asked for
the call, so nobody can point the AI at someone else's number.

  POST /web/call-me/start    {to, name?, consent: true}   → code texted     (origin-checked)
  POST /web/call-me/verify   {to, code}                   → call queued, {call_id, status_url}
  GET  /web/call-me/status?id=&key=                        → where the call is

The call itself is an ordinary outbound job (outbound.py, purpose "demo", API key "web-demo"),
so it gets the same queue, capacity limits, calling window, do-not-call list and retries
(one attempt only). Limits: codes per IP per hour, demo calls per day overall, and
outbound.py's calls-per-number per day.
"""
from __future__ import annotations

import re
import time
from collections import defaultdict, deque
from typing import Any, Optional

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from . import config, outbound, store, twilio_api
from .logging_setup import info, warn
from .outbound import ApiError

router = APIRouter(prefix="/web/call-me")
DEMO_KEY = "web-demo"
_CODES: dict[str, deque] = defaultdict(deque)     # IP → when codes were sent
_SENT: dict[str, float] = {}                      # number → when its last code was sent


def _ip(request: Request) -> str:
    return (request.headers.get("x-real-ip") or request.headers.get("x-forwarded-for", "").split(",")[0].strip()
            or (request.client.host if request.client else "?"))


def _reply(body: dict, status: int, origin: str = "") -> JSONResponse:
    # CORS headers come from server.py's CORSMiddleware (WEB_ALLOWED_ORIGINS)
    return JSONResponse(body, status_code=status, headers={"Cache-Control": "no-store"})


def _fail(exc: ApiError, origin: str) -> JSONResponse:
    return _reply(exc.body(), exc.status, origin)


def _check_origin(request: Request) -> str:
    origin = request.headers.get("origin", "")
    if origin not in config.WEB_ALLOWED_ORIGINS:
        raise ApiError(403, "forbidden", "This site can't request calls from Ava.")
    if not (config.DEMO_CALLS_ENABLED and config.TWILIO_VERIFY_SERVICE_SID):
        raise ApiError(503, "not_available", "Calls from Ava aren't available right now.")
    return origin


async def _body(request: Request) -> dict:
    try:
        b = await request.json()
    except Exception:
        b = None
    if not isinstance(b, dict):
        raise ApiError(400, "invalid_json", "Send the form as JSON.")
    return b


def _demo_calls_today(now: float) -> int:
    with store._lock:
        return store._db().execute("SELECT COUNT(*) FROM outbound_calls WHERE api_key=? AND created_at > ?",
                                   (DEMO_KEY, now - 86400)).fetchone()[0]


# ── Twilio Verify ────────────────────────────────────────────────────────────
async def _verify(path: str, data: dict) -> dict:
    url = f"https://verify.twilio.com/v2/Services/{config.TWILIO_VERIFY_SERVICE_SID}/{path}"
    try:
        async with httpx.AsyncClient(timeout=12) as c:
            r = await c.post(url, data=data, auth=(config.TWILIO_API_KEY_SID, config.TWILIO_API_KEY_SECRET))
    except httpx.HTTPError as exc:
        warn("demo", f"verify {path}: {exc}")
        raise ApiError(502, "sms_unavailable", "We couldn't send the code just now. Please try again in a minute.")
    body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    if r.status_code == 404 and path == "VerificationCheck":
        raise ApiError(422, "code_expired", "That code has expired. Ask for a new one.")
    if r.status_code == 429 or body.get("code") in (60203, 60212):
        raise ApiError(429, "too_many_codes", "Too many codes for this number. Please try again later.")
    if body.get("code") in (60200, 60205, 21211, 21614):
        raise ApiError(422, "invalid_number", "That number can't receive text messages.", "to")
    if r.status_code >= 300:
        warn("demo", f"verify {path}: {r.status_code} {body.get('code')} {body.get('message')}")
        raise ApiError(502, "sms_unavailable", "We couldn't send the code just now. Please try again in a minute.")
    return body


# ── endpoints ────────────────────────────────────────────────────────────────
@router.post("/start")
async def start(request: Request) -> JSONResponse:
    origin = request.headers.get("origin", "")
    try:
        origin = _check_origin(request)
        b = await _body(request)
        to = outbound.normalize_us(b.get("to"))
        if b.get("consent") is not True:
            raise ApiError(422, "consent_required", "Please tick the box to agree to a call from Ava.", "consent")
        name = re.sub(r"\s+", " ", str(b.get("name") or "")).strip()[:40]
        if name and not re.fullmatch(r"[A-Za-z][A-Za-z .'\-]*", name):
            raise ApiError(422, "invalid_field", "Please use letters only for your name.", "name")
        now = time.time()
        q = _CODES[_ip(request)]
        while q and now - q[0] > 3600:
            q.popleft()
        if len(q) >= config.DEMO_CODES_PER_IP_PER_HOUR:
            raise ApiError(429, "rate_limited", "You've asked for several codes already. Please try again in an hour.")
        if now - _SENT.get(to, 0) < 30:
            raise ApiError(429, "rate_limited", "A code was just sent. Please wait 30 seconds before asking again.")
        if await store.run(outbound.is_dnc, to):
            raise ApiError(409, "number_opted_out", "This number asked not to be called, so Ava can't call it.", "to")
        if await store.run(_demo_calls_today, now) >= config.DEMO_CALLS_PER_DAY:
            raise ApiError(429, "daily_limit_reached", "Ava has made all of today's demo calls. Please try again tomorrow.")
        await _verify("Verifications", {"To": to, "Channel": "sms"})
        q.append(now)
        _SENT[to] = now
        info("demo", f"code sent to …{to[-4:]}")
        return _reply({"ok": True, "to": to, "expires_in": 600}, 200, origin)
    except ApiError as exc:
        return _fail(exc, origin)


@router.post("/verify")
async def verify(request: Request) -> JSONResponse:
    origin = request.headers.get("origin", "")
    try:
        origin = _check_origin(request)
        b = await _body(request)
        to = outbound.normalize_us(b.get("to"))
        code = str(b.get("code") or "").strip()
        if not re.fullmatch(r"\d{4,10}", code):
            raise ApiError(422, "invalid_code", "Enter the 6-digit code from the text message.", "code")
        res = await _verify("VerificationCheck", {"To": to, "Code": code})
        if res.get("status") != "approved":
            raise ApiError(422, "invalid_code", "That code isn't right. Please check the text and try again.", "code")
        name = re.sub(r"\s+", " ", str(b.get("name") or "")).strip()[:40]
        name = name if re.fullmatch(r"[A-Za-z][A-Za-z .'\-]*", name or "-") else ""
        row = outbound.validate({
            "to": to, "consent": True, "consent_source": f"vedronix.com demo form, code verified, ip {_ip(request)}",
            "purpose": "callback", "patient_name": name or None, "max_attempts": 1,
            "context": "DEMO", "on_voicemail": "hangup",
        })
        row["purpose"] = "demo"
        row["context"] = ("This person asked for a demo call on the Vedronix website, to hear what an AI dental "
                          "receptionist sounds like. Treat them like a patient of Brightside Dental (a sample office): "
                          "offer to answer questions or book a cleaning, and keep it natural.")
        zones = outbound.zones_for(to)
        if not outbound.in_window(time.time(), zones):
            raise ApiError(422, "outside_calling_window", "Ava only calls between 8 am and 9 pm your time. "
                                                          "Please try again then.")
        job, _ = await store.run(outbound.create, DEMO_KEY, row)
        info("demo", f"{job['id']} demo call queued to …{to[-4:]}")
        return _reply({"ok": True, "call_id": job["id"], "status": job["status"],
                       "status_url": f"https://{config.PUBLIC_HOST}/web/call-me/status?id={job['id']}"
                                     f"&key={twilio_api.sign('demo', job['id'])}"}, 201, origin)
    except ApiError as exc:
        return _fail(exc, origin)


@router.get("/status")
async def status(request: Request, id: str = "", key: str = "") -> JSONResponse:
    origin = request.headers.get("origin", "")
    if not twilio_api.check(key, "demo", id):
        return _reply({"error": {"code": "not_found", "message": "Unknown call."}}, 404, origin)
    job: Optional[dict[str, Any]] = await store.run(outbound._get, id)
    if job is None:
        return _reply({"error": {"code": "not_found", "message": "Unknown call."}}, 404, origin)
    return _reply({"call_id": id, "status": job["status"], "summary": job.get("summary") if job["status"] == "completed" else None},
                  200, origin)
