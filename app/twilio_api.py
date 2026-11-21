"""
twilio_api.py — everything the agent asks of Twilio, through the official `twilio` library.

- Request signatures: every webhook is checked with RequestValidator (auth token) against
  the exact public URL Twilio called.
- Browser access tokens: an AccessToken with a VoiceGrant that only allows calling OUR TwiML
  App (which can only stream to this agent). incoming_allow is off: browsers can't be called.
- Hang-up: REST update status=completed, signed with the API key, so a call never lingers
  after the agent says goodbye. Outbound calls: test_call() (allowlisted number, admin key)
  and place_call() (the authenticated /v1/calls API, outbound.py).
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import time
from typing import Optional

import httpx
from twilio.jwt.access_token import AccessToken
from twilio.jwt.access_token.grants import VoiceGrant
from twilio.request_validator import RequestValidator

from . import config
from .logging_setup import info, warn


def is_teler_call(call_id: Optional[str]) -> bool:
    """Name kept for session.py: a Twilio CallSid starts with CA."""
    return bool(call_id) and str(call_id).startswith("CA")


def valid_signature(url: str, params: dict, signature: str) -> bool:
    if not config.TWILIO_AUTH_TOKEN or not signature:
        return False
    return RequestValidator(config.TWILIO_AUTH_TOKEN).validate(url, params, signature)


async def hangup(call_sid: str, reason: str = "") -> bool:
    """End the call (phone or browser). False, never raises, if it couldn't."""
    if not is_teler_call(call_sid):
        return False
    url = f"https://api.twilio.com/2010-04-01/Accounts/{config.TWILIO_ACCOUNT_SID}/Calls/{call_sid}.json"
    try:
        async with httpx.AsyncClient(timeout=8) as c:
            r = await c.post(url, data={"Status": "completed"}, auth=(config.TWILIO_API_KEY_SID, config.TWILIO_API_KEY_SECRET))
        if r.status_code < 300:
            info("twilio", f"hangup sent for {call_sid} ({reason or 'agent'})")
            return True
        # 404 / 400 "not in-progress" just means the call is already over
        warn("twilio", f"hangup {call_sid}: {r.status_code} {r.text[:160]}")
    except Exception as exc:
        warn("twilio", f"hangup {call_sid}: {exc}")
    return False


async def test_call(to: str) -> dict:
    """Ring an allowlisted test number from our Twilio number; Twilio then asks /twilio/voice
    what to do (Direction=outbound-api) and the agent answers. Raises on Twilio errors."""
    url = f"https://api.twilio.com/2010-04-01/Accounts/{config.TWILIO_ACCOUNT_SID}/Calls.json"
    base = f"https://{config.PUBLIC_HOST}"
    data = {
        "To": to, "From": config.TWILIO_PHONE_NUMBER, "Url": f"{base}/twilio/voice", "Method": "POST",
        "FallbackUrl": f"{base}/twilio/fallback", "StatusCallback": f"{base}/twilio/status",
        "StatusCallbackMethod": "POST", "Timeout": "30", "TimeLimit": str(config.MAX_CALL_SECONDS),
    }
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post(url, data=data, auth=(config.TWILIO_API_KEY_SID, config.TWILIO_API_KEY_SECRET))
    body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {"message": r.text[:200]}
    if r.status_code >= 300:
        raise RuntimeError(f"Twilio {r.status_code}: {body.get('message')} (code {body.get('code')})")
    info("twilio", f"test call {body.get('sid')} to …{to[-4:]}")
    return {"call_sid": body.get("sid"), "status": body.get("status")}


class TwilioError(Exception):
    def __init__(self, message: str, retryable: bool, code: Optional[int] = None) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.code = code


async def place_call(to: str, job_id: str, sig: str, machine_detection: bool = True,
                     on_voicemail: str = "leave_message", time_limit: int = 0) -> str:
    """Ring `to` for an outbound API call (outbound.py) and return the CallSid. Twilio asks
    /twilio/voice?ob=<job>&t=<sig> what to do once it's answered, and reports progress to
    /twilio/status with the same tag. TwilioError.retryable tells a hiccup (429, 5xx,
    network) from a refusal (bad number, geo permissions, ...)."""
    url = f"https://api.twilio.com/2010-04-01/Accounts/{config.TWILIO_ACCOUNT_SID}/Calls.json"
    base = f"https://{config.PUBLIC_HOST}"
    tag = f"ob={job_id}&t={sig}"
    data: dict = {
        "To": to, "From": config.TWILIO_PHONE_NUMBER, "Url": f"{base}/twilio/voice?{tag}", "Method": "POST",
        "FallbackUrl": f"{base}/twilio/fallback", "StatusCallback": f"{base}/twilio/status?{tag}",
        "StatusCallbackMethod": "POST", "StatusCallbackEvent": ["initiated", "ringing", "answered", "completed"],
        "Timeout": str(config.OUTBOUND_RING_TIMEOUT_S), "TimeLimit": str(time_limit or config.MAX_CALL_SECONDS),
    }
    if machine_detection:
        # DetectMessageEnd waits for the beep so a voicemail message is recorded whole
        data["MachineDetection"] = "DetectMessageEnd" if on_voicemail == "leave_message" else "Enable"
        data["MachineDetectionTimeout"] = "10"
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(url, data=data, auth=(config.TWILIO_API_KEY_SID, config.TWILIO_API_KEY_SECRET))
    except httpx.HTTPError as exc:
        raise TwilioError(f"network: {type(exc).__name__}", retryable=True)
    body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {"message": r.text[:200]}
    if r.status_code >= 300:
        raise TwilioError(f"Twilio {r.status_code}: {body.get('message')} (code {body.get('code')})",
                          # 401/403 is our credentials, not the callee's number: worth another try
                          retryable=r.status_code in (401, 403, 429) or r.status_code >= 500, code=body.get("code"))
    info("twilio", f"outbound {body.get('sid')} for {job_id} to …{to[-4:]}")
    return str(body.get("sid"))


def browser_token(identity: str) -> str:
    token = AccessToken(config.TWILIO_ACCOUNT_SID, config.TWILIO_API_KEY_SID, config.TWILIO_API_KEY_SECRET,
                        identity=identity, ttl=config.WEB_TOKEN_TTL_S)
    token.add_grant(VoiceGrant(outgoing_application_sid=config.TWILIO_TWIML_APP_SID, incoming_allow=False))
    jwt = token.to_jwt()
    return jwt.decode() if isinstance(jwt, bytes) else jwt


# ── our own short signatures (stream tokens, transcript-viewer keys) ─────────
def sign(*parts: str) -> str:
    msg = "|".join(parts).encode()
    return base64.urlsafe_b64encode(hmac.new(config.APP_SECRET.encode(), msg, hashlib.sha256).digest()[:18]).decode()


def check(sig: str, *parts: str) -> bool:
    return bool(config.APP_SECRET) and bool(sig) and hmac.compare_digest(sign(*parts), sig)


async def sleep_then(seconds: float, coro) -> None:
    await asyncio.sleep(seconds)
    await coro
