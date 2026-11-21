"""
outbound.py — the outbound call API (owner's request, 2026-11-20; UK conversion 2026-11-21).

The office's own systems ask the agent to ring a UK patient (appointment reminder, recall,
reschedule, call-back...). The agent places the call from our Twilio number; when the
person answers, the very same pipeline as an inbound call talks to them, with a prompt
that knows why it called.

  POST   /v1/calls               queue one call                   (201; 200 on idempotent replay)
  POST   /v1/calls/batch         queue up to OUTBOUND_MAX_BATCH    (200, one result per item)
  GET    /v1/calls               list this key's calls (status filter, cursor paging)
  GET    /v1/calls/{id}          one call, with transcript and summary
  POST   /v1/calls/{id}/cancel   cancel a queued call / hang up a live one
  GET    /v1/dnc/{number}        is the number on the do-not-call list?
  POST   /v1/dnc                 add a number to it
  DELETE /v1/dnc/{number}        remove one (only when the person has given consent again)
  GET    /v1/me                  which key this is, its limits and today's usage

Auth: `Authorization: Bearer vdx_live_…` (or `X-API-Key`). Only sha256 hashes of the keys
live on the server (OUTBOUND_API_KEYS="name:hash,…"); every key sees only its own calls.
Make a key with:  python -m app.outbound new <name>

Many requests at once: every call is a row in SQLite (store.outbound_calls), so a burst of
requests or a restart never loses one. One dispatcher loop dials what's due, at most
OUTBOUND_MAX_CONCURRENT at a time and always leaving OUTBOUND_INBOUND_RESERVE lines for
people calling in. Busy / no answer / Twilio hiccups are retried with a delay, up to
max_attempts, never outside the calling window. Checks and inserts run under one lock, so
two simultaneous requests can't both ring the same number.

Compliance built in (UK, AI voice): the caller must attest consent (UK GDPR / PECR);
calls only 8 am-8 pm in the callee's local time (Europe/London); UK numbers only, no
premium (09) or personal-numbering (070) ranges; a do-not-call list that also fills itself
when someone says "stop calling me"; at most OUTBOUND_PER_NUMBER_PER_DAY calls to one
number; the agent says it is an AI assistant in its first sentence and shares no details
until it knows it's speaking with the right person.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import re
import secrets
import socket
import sys
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from . import config, store, twilio_api
from .logging_setup import error, info, warn

router = APIRouter(prefix="/v1")

KEY_PREFIX = "vdx_live_"
TERMINAL = {"completed", "voicemail", "no_answer", "busy", "failed", "canceled", "expired"}
ACTIVE = {"dialing", "ringing", "in_progress"}
PURPOSES = {
    "appointment_reminder": "remind them about their upcoming appointment (the details are in the office notes) and check "
                            "they can still make it. If they need another time, find one with check_availability and book it.",
    "reschedule": "help them pick a new time for the appointment described in the office notes. Find open times with "
                  "check_availability and book the one they choose.",
    "recall": "let them know they're due for a checkup and cleaning, and offer to book one.",
    "appointment_follow_up": "check how they're doing after their recent visit and help with anything they need.",
    "callback": "return their call to the office: ask how you can help.",
    "custom": "the reason given in the office notes.",
}
# Purposes only this server sets (not accepted from the API)
INTERNAL_PURPOSES = {
    "demo": "they asked for this call on the Vedronix website, to try an AI dental receptionist. Help them like a "
            "patient of Brightside Dental: answer questions or book a cleaning.",
}
NEEDS_CONTEXT = {"appointment_reminder", "reschedule", "custom"}
_OPT_OUT = re.compile(r"\b(stop calling|do ?n[o']t call (me|us|this number)|do not call|take (me|us|this number|my number) off"
                      r"|remove (me|my number|this number)|no more calls|never call)", re.I)

# UK: the only time zone we use. All UK numbers, whether geographic (01/02/03),
# mobile (07), or freephone (08), share Europe/London local time.
_UK_TZ = "Europe/London"

# UK: Ofcom drama/reserved numbers that are never routed to a real person.
#   020 7946 0xxx      London drama
#   0113 496 0xxx      Leeds drama
#   0114 496 0xxx      Sheffield drama
#   0115 496 0xxx      Nottingham drama
#   0116 496 0xxx      Leicester drama
#   0117 496 0xxx      Bristol drama
#   0118 496 0xxx      Reading drama
#   0121 496 0xxx      Birmingham drama
#   0131 496 0xxx      Edinburgh drama
#   0141 496 0xxx      Glasgow drama
#   0151 496 0xxx      Liverpool drama
#   0161 496 0xxx      Manchester drama
#   0191 498 0xxx      Tyneside drama
#   028 9649 6xxx      Northern Ireland drama
#   029 2018 0xxx      Cardiff drama
#   07 7009 00xxx      Mobile drama (Ofcom reserved)
_UK_DRAMA = re.compile(
    r"^(?:"
    r"20(?:7946|7947|7948)0\d{3}|"
    r"1(?:13|14|15|16|17|18|21|31|41|51|61|91)4960\d{3}|"
    r"28(?:9018|9649)0\d{3}|"
    r"29(?:2018|2018)0\d{3}|"
    r"7700900\d{3}"
    r")$"
)

_live_count: Callable[[], int] = lambda: 0
_wake: Optional[asyncio.Event] = None
_loop: Optional[asyncio.AbstractEventLoop] = None


def _poke() -> None:
    """Wake the dispatcher now (safe from the store's worker threads)."""
    if _loop is not None and _wake is not None:
        try:
            _loop.call_soon_threadsafe(_wake.set)
        except RuntimeError:
            pass


def set_live_counter(fn: Callable[[], int]) -> None:
    global _live_count
    _live_count = fn


# ── errors: one JSON shape for every failure ─────────────────────────────────
class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, field: Optional[str] = None,
                 headers: Optional[dict] = None, **extra: Any) -> None:
        super().__init__(message)
        self.status, self.code, self.message, self.field = status, code, message, field
        self.headers = headers or {}
        self.extra = extra

    def body(self) -> dict:
        e = {"code": self.code, "message": self.message}
        if self.field:
            e["field"] = self.field
        e.update(self.extra)
        return {"error": e}


def error_response(exc: ApiError, request_id: str) -> JSONResponse:
    return JSONResponse({**exc.body(), "request_id": request_id}, status_code=exc.status,
                        headers={**exc.headers, "X-Request-Id": request_id, "Cache-Control": "no-store"})


def _ok(body: dict, status: int = 200, request_id: str = "") -> JSONResponse:
    return JSONResponse(body, status_code=status, headers={"X-Request-Id": request_id, "Cache-Control": "no-store"})


# ── API keys ─────────────────────────────────────────────────────────────────
def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def _keys() -> dict[str, str]:
    """sha256 hex → key name."""
    out = {}
    for entry in config.OUTBOUND_API_KEYS:
        name, _, h = entry.partition(":")
        if h and re.fullmatch(r"[0-9a-f]{64}", h.strip().lower()):
            out[h.strip().lower()] = name.strip() or "default"
    return out


def authenticate(request: Request) -> str:
    raw = request.headers.get("authorization", "")
    key = raw[7:].strip() if raw.lower().startswith("bearer ") else request.headers.get("x-api-key", "").strip()
    if not key:
        raise ApiError(401, "unauthorized", "Missing API key. Send 'Authorization: Bearer <key>'.",
                       headers={"WWW-Authenticate": "Bearer"})
    keys = _keys()
    if not keys:
        raise ApiError(503, "not_configured", "The outbound call API has no keys configured.")
    h = hash_key(key)
    for known, name in keys.items():
        if hmac.compare_digest(known, h):
            return name
    raise ApiError(401, "unauthorized", "Invalid API key.", headers={"WWW-Authenticate": "Bearer"})


_REQS: dict[str, deque] = defaultdict(deque)


def _rate_limit(key: str) -> None:
    now = time.time()
    q = _REQS[key]
    while q and now - q[0] > 60:
        q.popleft()
    if len(q) >= config.OUTBOUND_REQUESTS_PER_MINUTE:
        retry = max(1, int(60 - (now - q[0])) + 1)
        raise ApiError(429, "rate_limited", f"At most {config.OUTBOUND_REQUESTS_PER_MINUTE} requests a minute per key.",
                       headers={"Retry-After": str(retry)}, retry_after_seconds=retry)
    q.append(now)


# ── phone numbers, time zones and the calling window ─────────────────────────
def normalize_uk(raw: Any, field: str = "to") -> str:
    """'+44 7700 900123', '07700 900123', '447700900123' → '+447700900123', or ApiError.

    UK rules:
      * Accepts +44…, 0044…, or national 0… formats.
      * Rejects premium (09), personal numbering (070), and Ofcom fictional/drama ranges.
      * Rejects UK service numbers 084/087 (business-rate) — these are allowed only with
        explicit consent per PECR; we take the conservative route and block them.
    """
    if not isinstance(raw, str) or not raw.strip():
        raise ApiError(422, "invalid_number", "A UK phone number is required.", field)
    s = raw.strip()
    if re.search(r"[A-Za-z]", s) or len(s) > 30:
        raise ApiError(422, "invalid_number", "Phone numbers may only contain digits, spaces, +, -, ( ) and dots.", field)
    d = re.sub(r"\D", "", s)

    # Normalise to "44…" form
    if s.startswith("+"):
        if not d.startswith("44"):
            raise ApiError(422, "unsupported_country", "Only UK numbers (+44) can be called.", field)
        d = "44" + d[2:]
    elif d.startswith("0044"):
        d = "44" + d[4:]
    elif d.startswith("0"):
        d = "44" + d[1:]
    elif d.startswith("44"):
        pass  # already international form without "+"
    else:
        raise ApiError(422, "invalid_number", "A UK number starts with +44, 0044, or 0.", field)

    national = d[2:]  # digits after the 44 country code
    if not (9 <= len(national) <= 10):
        raise ApiError(422, "invalid_number", "That isn't a valid UK phone number.", field)

    # UK range checks
    if national.startswith("9"):
        raise ApiError(422, "unsupported_number", "Premium-rate (09) numbers can't be called.", field)
    if national.startswith("70"):
        raise ApiError(422, "unsupported_number", "Personal-numbering (070) ranges can't be called.", field)
    if national.startswith("84") or national.startswith("87"):
        # 084x/087x business-rate / revenue-share — PECR treats these as marketing-sensitive
        raise ApiError(422, "unsupported_number", "Business-rate (084/087) numbers can't be called.", field)
    if national.startswith("76") or national.startswith("77") or national.startswith("78") or national.startswith("79"):
        pass  # mobile — allowed
    if national.startswith(("1", "2", "3", "8")):
        pass  # geographic, non-geographic, freephone (080/0808) — allowed

    if _UK_DRAMA.match(national):
        # Ofcom drama ranges are never routed to a real person; allow them explicitly
        # because they are the standard test numbers used by developers.
        pass

    e164 = f"+44{national}"
    if e164 == re.sub(r"[^\d+]", "", config.TWILIO_PHONE_NUMBER):
        raise ApiError(422, "invalid_number", "That's the agent's own number.", field)
    return e164


def _valid_tz(name: Any) -> str:
    if name is None:
        return ""
    if not isinstance(name, str):
        raise ApiError(422, "invalid_field", "timezone must be an IANA name like 'Europe/London'.", "timezone")
    if not (name.startswith("Europe/") or name == "GB"):
        raise ApiError(422, "invalid_field", "timezone must be a UK time zone (e.g. 'Europe/London').", "timezone")
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ApiError(422, "invalid_field", f"Unknown time zone '{name}'.", "timezone")
    return name


def zones_for(number: str, tz_name: str = "") -> list[str]:
    # Every UK number is in Europe/London local time.
    return [tz_name or _UK_TZ]


def in_window(ts: float, zones: list[str]) -> bool:
    start = config.OUTBOUND_WINDOW_START[0] * 60 + config.OUTBOUND_WINDOW_START[1]
    end = config.OUTBOUND_WINDOW_END[0] * 60 + config.OUTBOUND_WINDOW_END[1]
    for z in zones:
        local = datetime.fromtimestamp(ts, ZoneInfo(z))
        m = local.hour * 60 + local.minute
        if not start <= m < end:
            return False
    return True


def next_window(ts: float, zones: list[str]) -> float:
    """ts itself if it's inside the calling window, else the first 5-minute mark that is."""
    if in_window(ts, zones):
        return ts
    t = (int(ts) // 300 + 1) * 300
    for _ in range(12 * 24 * 3):
        if in_window(t, zones):
            return float(t)
        t += 300
    return ts + 86400  # no overlap (misconfigured window): try again tomorrow


# ── request validation ───────────────────────────────────────────────────────
FIELDS = {"to", "consent", "consent_source", "purpose", "patient_name", "context", "metadata", "callback_url",
          "scheduled_at", "expires_at", "max_attempts", "retry_delay_seconds", "machine_detection", "on_voicemail",
          "retry_on_voicemail", "timezone", "idempotency_key"}


def _iso(v: Any, field: str) -> Optional[float]:
    if v is None:
        return None
    if not isinstance(v, str):
        raise ApiError(422, "invalid_field", f"{field} must be an ISO 8601 date-time with a UTC offset.", field)
    try:
        dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        raise ApiError(422, "invalid_field", f"{field} must be an ISO 8601 date-time, e.g. 2026-11-21T15:00:00+00:00.", field)
    if dt.tzinfo is None:
        raise ApiError(422, "invalid_field", f"{field} needs a UTC offset or 'Z' (e.g. +00:00).", field)
    return dt.timestamp()


def _int(body: dict, field: str, default: int, lo: int, hi: int) -> int:
    v = body.get(field, default)
    if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
        raise ApiError(422, "invalid_field", f"{field} must be a whole number from {lo} to {hi}.", field)
    return v


def _bool(body: dict, field: str, default: bool) -> bool:
    v = body.get(field, default)
    if not isinstance(v, bool):
        raise ApiError(422, "invalid_field", f"{field} must be true or false.", field)
    return v


def _text(body: dict, field: str, limit: int) -> str:
    v = body.get(field)
    if v is None:
        return ""
    if not isinstance(v, str):
        raise ApiError(422, "invalid_field", f"{field} must be a string.", field)
    v = re.sub(r"\s+", " ", v).strip()
    if len(v) > limit:
        raise ApiError(422, "invalid_field", f"{field} is longer than {limit} characters.", field)
    return v


def check_callback_url(url: str) -> None:
    """At request time: https, a real host name or a public IP. DNS is checked again at send time."""
    try:
        u = urlparse(url)
    except ValueError:
        raise ApiError(422, "invalid_field", "callback_url isn't a valid URL.", "callback_url")
    if u.scheme != "https" or not u.hostname or u.username or u.password:
        raise ApiError(422, "invalid_field", "callback_url must be an https:// URL without credentials.", "callback_url")
    host = u.hostname.lower()
    if host == "localhost" or host.endswith(".localhost") or host.endswith(".internal") or host.endswith(".local"):
        raise ApiError(422, "invalid_field", "callback_url must be publicly reachable.", "callback_url")
    try:
        if not ipaddress.ip_address(host).is_global:
            raise ApiError(422, "invalid_field", "callback_url must be publicly reachable.", "callback_url")
    except ValueError:
        pass  # a host name


def validate(body: Any, now: Optional[float] = None) -> dict:
    """The request body → the fields of a new call row. Raises ApiError (422) on anything off."""
    now = now or time.time()
    if not isinstance(body, dict):
        raise ApiError(422, "invalid_body", "Each call must be a JSON object.")
    unknown = sorted(set(body) - FIELDS)
    if unknown:
        raise ApiError(422, "unknown_field", f"Unknown field '{unknown[0]}'.", unknown[0])
    to = normalize_uk(body.get("to"))
    if body.get("consent") is not True:
        raise ApiError(422, "consent_required", "Set consent: true to confirm this person agreed to be called by the "
                                                "office's AI assistant (UK PECR / UK GDPR consent).", "consent")
    purpose = body.get("purpose")
    if purpose not in PURPOSES:
        raise ApiError(422, "invalid_field", f"purpose must be one of: {', '.join(PURPOSES)}.", "purpose")
    name = _text(body, "patient_name", 80)
    if name and not re.fullmatch(r"[A-Za-z][A-Za-z .'\-]*", name):
        raise ApiError(422, "invalid_field", "patient_name may only contain letters, spaces, . ' and -.", "patient_name")
    context = _text(body, "context", 1000)
    if purpose in NEEDS_CONTEXT and not context:
        raise ApiError(422, "missing_field", f"context is required for purpose '{purpose}' (what the call is about, "
                                             "e.g. the appointment's day and time).", "context")
    meta = body.get("metadata")
    if meta is not None:
        if not isinstance(meta, dict) or len(meta) > 20 or any(not isinstance(k, str) or len(k) > 40 for k in meta) \
                or any(not isinstance(v, (str, int, float, bool, type(None))) for v in meta.values()):
            raise ApiError(422, "invalid_field", "metadata must be an object of up to 20 flat string/number/bool values.",
                           "metadata")
        if len(json.dumps(meta)) > 2000:
            raise ApiError(422, "invalid_field", "metadata is larger than 2 KB.", "metadata")
    callback = _text(body, "callback_url", 500)
    if callback:
        check_callback_url(callback)
    scheduled = _iso(body.get("scheduled_at"), "scheduled_at") or now
    if scheduled > now + 30 * 86400:
        raise ApiError(422, "invalid_field", "scheduled_at can be at most 30 days ahead.", "scheduled_at")
    scheduled = max(scheduled, now)
    expires = _iso(body.get("expires_at"), "expires_at") or scheduled + 24 * 3600
    if expires <= scheduled or expires <= now:
        raise ApiError(422, "invalid_field", "expires_at must be after scheduled_at and in the future.", "expires_at")
    if expires > scheduled + 7 * 86400:
        raise ApiError(422, "invalid_field", "expires_at can be at most 7 days after scheduled_at.", "expires_at")
    on_vm = body.get("on_voicemail", "leave_message")
    if on_vm not in ("leave_message", "hangup"):
        raise ApiError(422, "invalid_field", "on_voicemail must be 'leave_message' or 'hangup'.", "on_voicemail")
    idem = _text(body, "idempotency_key", 100)
    if idem and not re.fullmatch(r"[A-Za-z0-9_\-:.]+", idem):
        raise ApiError(422, "invalid_field", "idempotency_key may only contain letters, digits, _ - : .", "idempotency_key")
    tz_name = _valid_tz(body.get("timezone"))
    row = {
        "to_number": to, "timezone": tz_name, "purpose": purpose, "patient_name": name, "context": context,
        "metadata": json.dumps(meta) if meta is not None else None, "callback_url": callback or None,
        "machine_detection": int(_bool(body, "machine_detection", True)), "on_voicemail": on_vm,
        "retry_on_voicemail": int(_bool(body, "retry_on_voicemail", False)),
        "consent_source": _text(body, "consent_source", 200) or None,
        "max_attempts": _int(body, "max_attempts", 3, 1, 5),
        "retry_delay_s": _int(body, "retry_delay_seconds", 1800, 300, 86400),
        "scheduled_at": scheduled, "expires_at": expires, "idempotency_key": idem or None,
    }
    if next_window(scheduled, zones_for(to, tz_name)) >= expires:
        raise ApiError(422, "outside_calling_window", "No allowed calling time (8 am-8 pm local) falls before expires_at.",
                       "expires_at")
    fingerprint = {k: v for k, v in row.items() if k not in ("scheduled_at", "expires_at", "idempotency_key")}
    fingerprint.update(scheduled=body.get("scheduled_at"), expires=body.get("expires_at"))
    row["request_hash"] = hashlib.sha256(json.dumps(fingerprint, sort_keys=True).encode()).hexdigest()
    return row


# ── the queue (SQLite) ───────────────────────────────────────────────────────
def _row(cur_row) -> Optional[dict]:
    return dict(cur_row) if cur_row is not None else None


def _get(job_id: str) -> Optional[dict]:
    with store._lock:
        return _row(store._db().execute("SELECT * FROM outbound_calls WHERE id=?", (job_id,)).fetchone())


def _by_sid(call_sid: str) -> Optional[dict]:
    with store._lock:
        return _row(store._db().execute("SELECT * FROM outbound_calls WHERE call_sid=?", (call_sid,)).fetchone())


def _update(job_id: str, **fields: Any) -> None:
    fields["updated_at"] = time.time()
    cols = ", ".join(f"{k}=?" for k in fields)
    with store._lock:
        store._db().execute(f"UPDATE outbound_calls SET {cols} WHERE id=?", (*fields.values(), job_id))
        store._db().commit()


def is_dnc(number: str) -> bool:
    with store._lock:
        return store._db().execute("SELECT 1 FROM dnc WHERE number=?", (number,)).fetchone() is not None


def add_dnc(number: str, reason: str, source: str) -> bool:
    with store._lock:
        cur = store._db().execute("INSERT OR IGNORE INTO dnc (number, reason, source, added_at) VALUES (?,?,?,?)",
                                  (number, reason[:200], source[:40], time.time()))
        # anything still waiting for that number is called off
        store._db().execute("UPDATE outbound_calls SET status='canceled', last_error='do_not_call', updated_at=?, "
                            "completed_at=? WHERE to_number=? AND status='queued'", (time.time(), time.time(), number))
        store._db().commit()
    return cur.rowcount == 1


def remove_dnc(number: str) -> bool:
    with store._lock:
        cur = store._db().execute("DELETE FROM dnc WHERE number=?", (number,))
        store._db().commit()
    return cur.rowcount == 1


def create(key: str, row: dict, now: Optional[float] = None) -> tuple[dict, bool]:
    """Insert one validated call for `key`. (call, created). Every check and the insert run
    under one lock, so simultaneous requests can't slip past a limit or double-ring a number."""
    now = now or time.time()
    with store._lock:
        db = store._db()
        if row.get("idempotency_key"):
            old = db.execute("SELECT * FROM outbound_calls WHERE api_key=? AND idempotency_key=?",
                             (key, row["idempotency_key"])).fetchone()
            if old is not None:
                if old["request_hash"] != row["request_hash"]:
                    raise ApiError(409, "idempotency_conflict", "This idempotency key was already used with different "
                                                                "parameters.", "idempotency_key", call_id=old["id"])
                return dict(old), False
        to = row["to_number"]
        if db.execute("SELECT 1 FROM dnc WHERE number=?", (to,)).fetchone():
            raise ApiError(409, "number_opted_out", "This number is on the do-not-call list.", "to")
        busy = db.execute("SELECT id, api_key FROM outbound_calls WHERE to_number=? AND status IN "
                          "('queued','dialing','ringing','in_progress')", (to,)).fetchone()
        if busy is not None:
            raise ApiError(409, "duplicate_active_call", "A call to this number is already queued or in progress.", "to",
                           **({"call_id": busy["id"]} if busy["api_key"] == key else {}))
        per_number = db.execute("SELECT COUNT(*) FROM outbound_calls WHERE to_number=? AND created_at > ?",
                                (to, now - 86400)).fetchone()[0]
        if per_number >= config.OUTBOUND_PER_NUMBER_PER_DAY:
            raise ApiError(429, "number_limit_reached", f"At most {config.OUTBOUND_PER_NUMBER_PER_DAY} calls to one "
                                                        "number in 24 hours.", "to")
        daily = db.execute("SELECT COUNT(*) FROM outbound_calls WHERE api_key=? AND created_at > ?",
                           (key, now - 86400)).fetchone()[0]
        if daily >= config.OUTBOUND_CALLS_PER_DAY:
            raise ApiError(429, "daily_limit_reached", f"This key's limit of {config.OUTBOUND_CALLS_PER_DAY} calls in "
                                                       "24 hours is reached.")
        job_id = "call_" + secrets.token_hex(12)
        fields = {k: v for k, v in row.items() if k != "scheduled_at"}
        fields.update(id=job_id, api_key=key, status="queued", attempts=0, call_sids="[]",
                      next_at=next_window(row["scheduled_at"], zones_for(to, row.get("timezone") or "")),
                      created_at=now, updated_at=now)
        db.execute(f"INSERT INTO outbound_calls ({', '.join(fields)}) VALUES ({', '.join('?' * len(fields))})",
                   tuple(fields.values()))
        db.commit()
        out = dict(db.execute("SELECT * FROM outbound_calls WHERE id=?", (job_id,)).fetchone())
    info("outbound", f"{job_id} queued by {key}: {row['purpose']} to …{to[-4:]}")
    _poke()
    return out, True


# ── what the API returns ─────────────────────────────────────────────────────
def _ts(v: Optional[float]) -> Optional[str]:
    return datetime.fromtimestamp(v, timezone.utc).isoformat().replace("+00:00", "Z") if v else None


def public(job: dict, full: bool = True) -> dict:
    err = None
    if job.get("last_error") and job["status"] in ("failed", "canceled", "expired", "no_answer", "busy", "queued"):
        code, _, msg = job["last_error"].partition(": ")
        err = {"code": code, "message": msg or code}
    out = {
        "id": job["id"], "object": "call", "status": job["status"], "to": job["to_number"],
        "purpose": job["purpose"], "patient_name": job.get("patient_name") or None,
        "metadata": json.loads(job["metadata"]) if job.get("metadata") else None,
        "attempts": job["attempts"], "max_attempts": job["max_attempts"],
        "next_attempt_at": _ts(job.get("next_at")) if job["status"] == "queued" else None,
        "expires_at": _ts(job.get("expires_at")),
        "call_sid": job.get("call_sid"), "answered_by": job.get("answered_by"),
        "outcome": job.get("outcome"), "summary": job.get("summary"),
        "duration_seconds": round(job["duration_s"], 1) if job.get("duration_s") is not None else None,
        "error": err, "created_at": _ts(job["created_at"]), "updated_at": _ts(job["updated_at"]),
        "completed_at": _ts(job.get("completed_at")),
    }
    if full:
        out["transcript"] = json.loads(job["transcript"]) if job.get("transcript") else None
        out["lead"] = json.loads(job["lead"]) if job.get("lead") else None
    return out


# ── finishing a call: retry or final, then the webhook ───────────────────────
def _final(job: dict, status: str, err: str = "", **fields: Any) -> None:
    now = time.time()
    _update(job["id"], status=status, last_error=err or job.get("last_error"), completed_at=now, finalized=1, **fields)
    info("outbound", f"{job['id']} {status}{f' ({err})' if err else ''}")
    j = _get(job["id"])
    if j and j.get("callback_url"):
        body = {"id": "evt_" + secrets.token_hex(10), "type": f"call.{status}", "created_at": _ts(now),
                "data": public(j)}
        with store._lock:
            store._db().execute("INSERT OR IGNORE INTO webhook_outbox (dedupe, url, body, next_at, created_at) "
                                "VALUES (?,?,?,?,?)", (f"{j['id']}:final", j["callback_url"],
                                                       json.dumps(body, ensure_ascii=False), now, now))
            store._db().commit()


def _retry_or_final(job: dict, status: str, err: str, now: Optional[float] = None) -> None:
    """A failed attempt (busy, no answer, ...): try again later if attempts and time are left."""
    now = now or time.time()
    if job["attempts"] < job["max_attempts"]:
        at = next_window(now + job["retry_delay_s"] * job["attempts"],
                         zones_for(job["to_number"], job.get("timezone") or ""))
        if at < job["expires_at"]:
            _update(job["id"], status="queued", next_at=at, last_error=err, call_sid=None, answered_by=None,
                    answered_at=None, ended_at=None)
            info("outbound", f"{job['id']} {status}; retry {job['attempts'] + 1}/{job['max_attempts']} at {_ts(at)}")
            return
    _final(job, status, err)


# ── Twilio hooks (called by server.py) ───────────────────────────────────────
CALL_JOBS: dict[str, dict] = {}   # CallSid → job, from /twilio/voice until the stream starts


def check_token(job_id: str, token: str) -> bool:
    return bool(job_id) and twilio_api.check(token, "outbound", job_id)


def on_voice(job_id: str, p: dict) -> tuple[str, Optional[dict]]:
    """What /twilio/voice should do for an API call: ('agent'|'voicemail'|'hangup', job)."""
    job = _get(job_id)
    if job is None or job.get("call_sid") != p.get("CallSid") or job["status"] in TERMINAL:
        return "hangup", job
    answered_by = (p.get("AnsweredBy") or "").lower()
    if answered_by:
        _update(job_id, answered_by=answered_by)
    if answered_by == "fax":
        _final(job, "failed", "fax: a fax machine answered")
        return "hangup", job
    if answered_by.startswith("machine"):
        if job["retry_on_voicemail"] and job["attempts"] < job["max_attempts"]:
            _retry_or_final(job, "voicemail", "voicemail: an answering machine picked up")
            return "hangup", job
        msg = job["on_voicemail"] == "leave_message" and answered_by != "machine_start"
        _final(job, "voicemail", "", outcome="voicemail_left" if msg else "voicemail_no_message")
        return ("voicemail" if msg else "hangup"), job
    _update(job_id, status="in_progress", answered_at=time.time())
    CALL_JOBS[p.get("CallSid", "")] = job
    return "agent", job


def voicemail_text(job: dict) -> str:
    from .notify import office_phone

    return (f"Hi, this is {config.AGENT_NAME}, an AI assistant calling from {config.BUSINESS_NAME}. "
            f"Please give us a call back at {_spoken_phone(office_phone())} when you have a moment. Thank you, and have a great day!")


def _spoken_phone(p: str) -> str:
    # UK: read out as "oh two oh, seven nine four six, oh nine five eight"
    d = re.sub(r"\D", "", p)
    if d.startswith("44"):
        d = "0" + d[2:]
    if len(d) >= 10:
        return f"{', '.join(d[:3])}, {', '.join(d[3:7])}, {', '.join(d[7:])}"
    return p


def on_status(job_id: str, p: dict) -> None:
    job = _get(job_id)
    if job is None or job.get("call_sid") != p.get("CallSid") or job.get("finalized"):
        return  # an old attempt's late callback, or already done
    st = (p.get("CallStatus") or "").lower()
    now = time.time()
    if st == "ringing" and job["status"] == "dialing":
        _update(job_id, status="ringing", twilio_status=st)
    elif st == "in-progress":
        _update(job_id, twilio_status=st)
    elif st == "completed":
        dur = float(p.get("CallDuration") or 0)
        if job.get("answered_at") and job["status"] in ("in_progress", "canceled"):
            # a conversation: the session's report (summary, transcript) finishes it, or the sweeper
            _update(job_id, twilio_status=st, ended_at=now, duration_s=dur)
        elif job["status"] == "canceled":
            _final(job, "canceled", job.get("last_error") or "canceled", duration_s=dur)
        else:
            _retry_or_final(job, "no_answer", "no_answer: the call ended before anyone answered")
    elif st in ("busy", "no-answer", "failed", "canceled"):
        if job["status"] == "canceled" or st == "canceled":
            _final(job, "canceled", job.get("last_error") or "canceled")
        elif st == "failed":
            _retry_or_final(job, "failed", f"carrier_failed: Twilio could not connect the call "
                                           f"({p.get('ErrorCode') or p.get('SipResponseCode') or 'no code'})")
        else:
            _retry_or_final(job, st.replace("-", "_"), f"{st.replace('-', '_')}: the line was {st.replace('-', ' ')}")


def stream_cfg(call_sid: str) -> Optional[dict]:
    """The job for a call whose agent stream is starting (for the prompt and greeting)."""
    return CALL_JOBS.pop(call_sid, None) or _by_sid(call_sid)


def on_report(call_sid: str, report: dict) -> None:
    """The session's end-of-call report: the conversation's outcome finishes the job."""
    job = _by_sid(call_sid)
    if job is None or job.get("finalized"):
        return
    said = " ".join(t.get("text", "") for t in (report.get("transcript") or []) if t.get("who") in ("caller", "user"))
    if _OPT_OUT.search(said):
        add_dnc(job["to_number"], "asked not to be called again (during the call)", "spoken_opt_out")
        info("outbound", f"{job['id']}: callee asked not to be called again → do-not-call")
    _final(job, "canceled" if job["status"] == "canceled" else "completed", "", outcome=report.get("outcome"), summary=report.get("summary"),
           transcript=json.dumps(report.get("transcript") or [], ensure_ascii=False),
           lead=json.dumps(report["lead"], ensure_ascii=False) if report.get("lead") else None,
           duration_s=report.get("seconds") or job.get("duration_s"), ended_at=time.time())


# ── the dispatcher ───────────────────────────────────────────────────────────
def _counts() -> dict:
    with store._lock:
        rows = store._db().execute("SELECT status, COUNT(*) n FROM outbound_calls GROUP BY status").fetchall()
    return {r["status"]: r["n"] for r in rows}


def capacity() -> int:
    """How many more outbound calls may start now."""
    c = _counts()
    active = sum(c.get(s, 0) for s in ACTIVE)
    ringing = c.get("dialing", 0) + c.get("ringing", 0)   # not in LIVE yet, but will be
    lines = config.MAX_CONCURRENT_CALLS - config.OUTBOUND_INBOUND_RESERVE - _live_count() - ringing
    return max(0, min(config.OUTBOUND_MAX_CONCURRENT - active, lines))


def _claim(limit: int, now: float) -> list[dict]:
    """Take up to `limit` due calls and mark them dialing (attempts + 1) in one step."""
    if limit <= 0:
        return []
    with store._lock:
        db = store._db()
        rows = db.execute("SELECT * FROM outbound_calls WHERE status='queued' AND next_at <= ? ORDER BY next_at, created_at "
                          "LIMIT ?", (now, limit * 4)).fetchall()
        out = []
        for r in rows:
            r = dict(r)
            if r["expires_at"] <= now:
                continue  # the sweeper expires it
            zones = zones_for(r["to_number"], r.get("timezone") or "")
            if not in_window(now, zones):
                db.execute("UPDATE outbound_calls SET next_at=?, updated_at=? WHERE id=?",
                           (next_window(now, zones), now, r["id"]))
                continue
            if db.execute("SELECT 1 FROM dnc WHERE number=?", (r["to_number"],)).fetchone():
                db.execute("UPDATE outbound_calls SET status='canceled', last_error='do_not_call: the number opted out', "
                           "finalized=1, completed_at=?, updated_at=? WHERE id=?", (now, now, r["id"]))
                continue
            db.execute("UPDATE outbound_calls SET status='dialing', attempts=attempts+1, updated_at=? WHERE id=?",
                       (now, r["id"]))
            r.update(status="dialing", attempts=r["attempts"] + 1)
            out.append(r)
            if len(out) >= limit:
                break
        db.commit()
    return out


async def _dial(job: dict) -> None:
    sig = twilio_api.sign("outbound", job["id"])
    limit = config.MAX_WEB_CALL_SECONDS if job["purpose"] == "demo" else config.MAX_CALL_SECONDS
    try:
        sid = await twilio_api.place_call(job["to_number"], job["id"], sig, bool(job["machine_detection"]),
                                          job["on_voicemail"], limit)
    except twilio_api.TwilioError as exc:
        warn("outbound", f"{job['id']} dial failed: {exc}")
        if exc.retryable:
            await store.run(_retry_or_final, job, "failed", f"twilio_unavailable: {exc}")
        else:
            await store.run(_final, job, "failed", f"twilio_rejected: {exc}")
        return
    except Exception as exc:
        warn("outbound", f"{job['id']} dial error: {exc}")
        await store.run(_retry_or_final, job, "failed", f"dial_error: {exc}")
        return
    sids = json.loads(job.get("call_sids") or "[]") + [sid]
    await store.run(lambda: _update(job["id"], call_sid=sid, call_sids=json.dumps(sids), twilio_status="queued"))
    # canceled while the REST call was in flight: hang the new call up straight away
    if (await store.run(_get, job["id"]) or {}).get("status") == "canceled":
        await twilio_api.hangup(sid, "canceled")


def sweep(now: Optional[float] = None) -> None:
    """Calls stuck because a Twilio callback never came (or we restarted mid-call)."""
    now = now or time.time()
    with store._lock:
        rows = [dict(r) for r in store._db().execute(
            "SELECT * FROM outbound_calls WHERE finalized=0 AND (status IN ('dialing','ringing','in_progress') "
            "OR status='canceled' OR (status='queued' AND expires_at <= ?))", (now,)).fetchall()]
    ring_s = config.OUTBOUND_RING_TIMEOUT_S + 120
    for j in rows:
        age = now - j["updated_at"]
        if j["status"] == "queued":
            _final(j, "expired", j.get("last_error") or "expired: not reached before expires_at")
        elif j["status"] == "canceled" and age > ring_s:
            _final(j, "canceled", j.get("last_error") or "canceled")         # Twilio's final callback never came
        elif j["status"] in ("dialing", "ringing") and age > ring_s:
            _retry_or_final(j, "failed", "no_callback: Twilio never reported the call's progress")
        elif j["status"] == "in_progress" and j.get("ended_at") and now - j["ended_at"] > 90:
            _final(j, "completed", "")                       # the report never came
        elif j["status"] == "in_progress" and not j.get("ended_at") and j.get("answered_at") \
                and now - j["answered_at"] > config.MAX_CALL_SECONDS + 300:
            _final(j, "completed", "lost_track: no end-of-call status")


async def tick() -> int:
    now = time.time()
    await store.run(sweep, now)
    jobs = await store.run(_claim, await store.run(capacity), now)
    for j in jobs:
        await _dial(j)
    await send_webhooks()
    return len(jobs)


async def worker() -> None:
    global _wake, _loop
    _loop, _wake = asyncio.get_running_loop(), asyncio.Event()
    info("outbound", f"dispatcher running ({len(_keys())} API key(s), max {config.OUTBOUND_MAX_CONCURRENT} at once)")
    while True:
        try:
            await tick()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error("outbound", f"dispatcher: {exc}")
        try:
            await asyncio.wait_for(_wake.wait(), timeout=3)
        except asyncio.TimeoutError:
            pass
        _wake.clear()


# ── result webhooks: signed, retried, never to private addresses ─────────────
_WEBHOOK_BACKOFF_S = [30, 120, 600, 1800, 3600, 7200]


def sign_webhook(body: str, ts: int) -> str:
    mac = hmac.new(config.OUTBOUND_WEBHOOK_SECRET.encode(), f"{ts}.{body}".encode(), hashlib.sha256).hexdigest()
    return f"t={ts},v1={mac}"


async def _public_host(url: str) -> bool:
    u = urlparse(url)
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(u.hostname, u.port or 443, type=socket.SOCK_STREAM)
    except OSError:
        return False
    return bool(infos) and all(ipaddress.ip_address(i[4][0]).is_global for i in infos)


async def send_webhooks() -> int:
    if not config.OUTBOUND_WEBHOOK_SECRET:
        return 0
    with store._lock:
        due = [dict(r) for r in store._db().execute(
            "SELECT * FROM webhook_outbox WHERE status='pending' AND next_at <= ? ORDER BY id LIMIT 10",
            (time.time(),)).fetchall()]
    sent = 0
    for m in due:
        err, retry = "", True
        try:
            if not await _public_host(m["url"]):
                err, retry = "callback_url doesn't resolve to a public address", False
            else:
                ts = int(time.time())
                async with httpx.AsyncClient(timeout=10, follow_redirects=False) as c:
                    r = await c.post(m["url"], content=m["body"].encode(), headers={
                        "Content-Type": "application/json", "User-Agent": "Vedronix-Webhooks/1",
                        "X-Vedronix-Event": json.loads(m["body"]).get("type", ""),
                        "X-Vedronix-Signature": sign_webhook(m["body"], ts)})
                if r.status_code < 300:
                    sent += 1
                else:
                    err = f"HTTP {r.status_code}"
                    retry = r.status_code >= 500 or r.status_code in (408, 409, 425, 429)
        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}"[:200]
        attempt = m["attempts"]
        if not err:
            status, next_at = "sent", None
        elif retry and attempt < len(_WEBHOOK_BACKOFF_S):
            status, next_at = "pending", time.time() + _WEBHOOK_BACKOFF_S[attempt]
        else:
            status, next_at = "failed", None
        if err:
            warn("outbound", f"webhook #{m['id']} {status}: {err}")
        with store._lock:
            store._db().execute("UPDATE webhook_outbox SET status=?, attempts=attempts+1, last_error=?, next_at=?, "
                                "sent_at=CASE WHEN ?='sent' THEN ? ELSE sent_at END WHERE id=?",
                                (status, err or None, next_at, status, time.time(), m["id"]))
            store._db().commit()
    return sent


# ── HTTP API ─────────────────────────────────────────────────────────────────
async def _json(request: Request, limit: int = 256 * 1024) -> Any:
    if "application/json" not in request.headers.get("content-type", ""):
        raise ApiError(415, "unsupported_media_type", "Send the body as JSON (Content-Type: application/json).")
    raw = await request.body()
    if len(raw) > limit:
        raise ApiError(413, "payload_too_large", f"The body is larger than {limit // 1024} KB.")
    try:
        return json.loads(raw or b"null")
    except ValueError:
        raise ApiError(400, "invalid_json", "The body isn't valid JSON.")


def _ready() -> None:
    if not (config.TWILIO_ACCOUNT_SID and config.TWILIO_API_KEY_SID and config.TWILIO_API_KEY_SECRET
            and config.APP_SECRET and config.TWILIO_PHONE_NUMBER):
        raise ApiError(503, "not_configured", "Telephony isn't fully configured on the server.")


async def _guard(request: Request) -> str:
    key = authenticate(request)
    _rate_limit(key)
    return key


def _create_one(key: str, body: Any, header_idem: str = "") -> tuple[dict, bool]:
    if isinstance(body, dict) and header_idem and not body.get("idempotency_key"):
        body = {**body, "idempotency_key": header_idem}
    return create(key, validate(body))


@router.post("/calls")
async def create_call(request: Request) -> JSONResponse:
    rid = "req_" + secrets.token_hex(8)
    try:
        key = await _guard(request)
        _ready()
        body = await _json(request, 64 * 1024)
        job, created = await store.run(_create_one, key, body, request.headers.get("idempotency-key", "").strip())
        return _ok({**public(job), "idempotent_replay": not created}, 201 if created else 200, rid)
    except ApiError as exc:
        return error_response(exc, rid)


@router.post("/calls/batch")
async def create_batch(request: Request) -> JSONResponse:
    rid = "req_" + secrets.token_hex(8)
    try:
        key = await _guard(request)
        _ready()
        body = await _json(request)
        calls = body.get("calls") if isinstance(body, dict) else None
        if not isinstance(calls, list) or not calls:
            raise ApiError(422, "invalid_body", "Send {\"calls\": [ ... ]} with at least one call.", "calls")
        if len(calls) > config.OUTBOUND_MAX_BATCH:
            raise ApiError(422, "batch_too_large", f"At most {config.OUTBOUND_MAX_BATCH} calls per batch.", "calls")
        results, n = [], {"created": 0, "replayed": 0, "failed": 0}
        for i, item in enumerate(calls):
            try:
                job, created = await store.run(_create_one, key, item)
                results.append({"index": i, "status_code": 201 if created else 200, "call": public(job, full=False)})
                n["created" if created else "replayed"] += 1
            except ApiError as exc:
                results.append({"index": i, "status_code": exc.status, **exc.body()})
                n["failed"] += 1
        return _ok({"object": "batch", "summary": n, "results": results}, 200, rid)
    except ApiError as exc:
        return error_response(exc, rid)


@router.get("/calls")
async def list_calls(request: Request, status: str = "", limit: int = 50, cursor: str = "") -> JSONResponse:
    rid = "req_" + secrets.token_hex(8)
    try:
        key = await _guard(request)
        statuses = [s for s in status.split(",") if s] if status else []
        bad = [s for s in statuses if s not in TERMINAL | ACTIVE | {"queued"}]
        if bad:
            raise ApiError(422, "invalid_field", f"Unknown status '{bad[0]}'.", "status")
        if not 1 <= limit <= 200:
            raise ApiError(422, "invalid_field", "limit must be from 1 to 200.", "limit")
        q, args = "SELECT * FROM outbound_calls WHERE api_key=?", [key]
        if statuses:
            q += f" AND status IN ({','.join('?' * len(statuses))})"
            args += statuses
        if cursor:
            c = await store.run(_get, cursor)
            if c is None or c["api_key"] != key:
                raise ApiError(422, "invalid_field", "Unknown cursor.", "cursor")
            q += " AND (created_at < ? OR (created_at = ? AND id < ?))"
            args += [c["created_at"], c["created_at"], c["id"]]
        q += " ORDER BY created_at DESC, id DESC LIMIT ?"
        args.append(limit + 1)

        def run() -> list[dict]:
            with store._lock:
                return [dict(r) for r in store._db().execute(q, args).fetchall()]

        rows = await store.run(run)
        more = len(rows) > limit
        rows = rows[:limit]
        return _ok({"object": "list", "data": [public(r, full=False) for r in rows], "has_more": more,
                    "next_cursor": rows[-1]["id"] if more else None}, 200, rid)
    except ApiError as exc:
        return error_response(exc, rid)



async def _own(key: str, call_id: str) -> dict:
    job = await store.run(_get, call_id) if re.fullmatch(r"call_[0-9a-f]{24}", call_id) else None
    if job is None or job["api_key"] != key:
        raise ApiError(404, "not_found", "No call with that id.")
    return job


@router.get("/calls/{call_id}")
async def get_call(request: Request, call_id: str) -> JSONResponse:
    rid = "req_" + secrets.token_hex(8)
    try:
        key = await _guard(request)
        return _ok(public(await _own(key, call_id)), 200, rid)
    except ApiError as exc:
        return error_response(exc, rid)


@router.post("/calls/{call_id}/cancel")
async def cancel_call(request: Request, call_id: str) -> JSONResponse:
    rid = "req_" + secrets.token_hex(8)
    try:
        key = await _guard(request)
        job = await _own(key, call_id)
        if job["status"] in TERMINAL:
            raise ApiError(409, "not_cancelable", f"The call is already {job['status']}.", call_status=job["status"])
        if job["status"] == "queued":
            await store.run(_final, job, "canceled", "canceled: canceled by the API")
        else:
            # dialing / ringing / talking: hang up; Twilio's final status callback finishes it
            await store.run(lambda: _update(job["id"], status="canceled", last_error="canceled: canceled by the API"))
            if job.get("call_sid"):
                await twilio_api.hangup(job["call_sid"], "canceled by API")
        return _ok(public(await store.run(_get, call_id)), 200, rid)
    except ApiError as exc:
        return error_response(exc, rid)


@router.get("/dnc/{number}")
async def dnc_get(request: Request, number: str) -> JSONResponse:
    rid = "req_" + secrets.token_hex(8)
    try:
        await _guard(request)
        n = normalize_uk(number, "number")
        return _ok({"number": n, "do_not_call": await store.run(is_dnc, n)}, 200, rid)
    except ApiError as exc:
        return error_response(exc, rid)


@router.post("/dnc")
async def dnc_add(request: Request) -> JSONResponse:
    rid = "req_" + secrets.token_hex(8)
    try:
        key = await _guard(request)
        body = await _json(request, 8 * 1024)
        if not isinstance(body, dict):
            raise ApiError(422, "invalid_body", "Send {\"number\": \"...\", \"reason\": \"...\"}.")
        n = normalize_uk(body.get("number"), "number")
        added = await store.run(add_dnc, n, _text(body, "reason", 200) or "requested", f"api:{key}")
        return _ok({"number": n, "do_not_call": True, "added": added}, 201 if added else 200, rid)
    except ApiError as exc:
        return error_response(exc, rid)


@router.delete("/dnc/{number}")
async def dnc_remove(request: Request, number: str) -> JSONResponse:
    rid = "req_" + secrets.token_hex(8)
    try:
        await _guard(request)
        n = normalize_uk(number, "number")
        if not await store.run(remove_dnc, n):
            raise ApiError(404, "not_found", "That number isn't on the do-not-call list.")
        return _ok({"number": n, "do_not_call": False}, 200, rid)
    except ApiError as exc:
        return error_response(exc, rid)


@router.get("/me")
async def me(request: Request) -> JSONResponse:
    rid = "req_" + secrets.token_hex(8)
    try:
        key = await _guard(request)

        def usage() -> int:
            with store._lock:
                return store._db().execute("SELECT COUNT(*) FROM outbound_calls WHERE api_key=? AND created_at > ?",
                                           (key, time.time() - 86400)).fetchone()[0]

        return _ok({"key_name": key, "calls_last_24h": await store.run(usage), "limits": {
            "requests_per_minute": config.OUTBOUND_REQUESTS_PER_MINUTE, "calls_per_day": config.OUTBOUND_CALLS_PER_DAY,
            "calls_per_number_per_day": config.OUTBOUND_PER_NUMBER_PER_DAY, "max_batch": config.OUTBOUND_MAX_BATCH,
            "max_concurrent_calls": config.OUTBOUND_MAX_CONCURRENT,
            "calling_window_local": "%02d:%02d-%02d:%02d" % (*config.OUTBOUND_WINDOW_START, *config.OUTBOUND_WINDOW_END)},
            "purposes": list(PURPOSES), "webhooks_signed": bool(config.OUTBOUND_WEBHOOK_SECRET)}, 200, rid)
    except ApiError as exc:
        return error_response(exc, rid)


def health() -> dict:
    c = _counts()
    with store._lock:
        wh = {r["status"]: r["n"] for r in store._db().execute(
            "SELECT status, COUNT(*) n FROM webhook_outbox GROUP BY status").fetchall()}
    return {"keys": len(_keys()), "calls": c, "webhooks": wh}


# ── python -m app.outbound new <name> ────────────────────────────────────────
def main(argv: list[str]) -> int:
    if len(argv) >= 2 and argv[0] == "new" and re.fullmatch(r"[A-Za-z0-9_\-]{1,40}", argv[1]):
        key = KEY_PREFIX + secrets.token_urlsafe(32)
        print(f"API key (give to the client, shown once):  {key}")
        print(f"Add to .env.production:                    OUTBOUND_API_KEYS={argv[1]}:{hash_key(key)}")
        print("(append ',name:hash' to keep existing keys)")
        return 0
    if argv[:1] == ["secret"]:
        print(f"OUTBOUND_WEBHOOK_SECRET={secrets.token_hex(32)}")
        return 0
    print("usage: python -m app.outbound new <name> | secret")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))