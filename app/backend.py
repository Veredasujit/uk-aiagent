"""
backend.py — what used to be the Vedronix backend client, done locally.

The US agent has no separate backend: function calls (availability, booking) and the
end-of-call report go to the agent's own SQLite (store.py). The function names and shapes
match the old client so session.py and tools.py stay as they were tuned on live calls.
"""
from __future__ import annotations

import time
from typing import Any

from . import notify, outbound, store
from .logging_setup import info, warn


class BackendError(Exception):
    def __init__(self, message: str, status: int = 0) -> None:
        super().__init__(message)
        self.status = status


async def call_started(token: str, info_: dict) -> None:
    info("call", f"call started {token} ({info_.get('direction')})")


async def tool(token: str, name: str, args: dict) -> dict:
    """`token` is the Twilio CallSid."""
    if name == "check_availability":
        return await store.run(store.free_slots, str(args.get("date", "")))
    if name == "book_appointment":
        name_ = str(args.get("name") or "").strip()
        if not name_:
            return {"ok": False, "say": "Ask for the patient's first and last name first."}
        reason = str(args.get("purpose") or args.get("requirements") or "")
        result = await store.run(store.book, str(args.get("date", "")), str(args.get("time", "")), name_,
                                 str(args.get("phone") or ""), reason, token, notify.number_for(token))
        if result.get("ok") and result.get("booked"):
            await notify.on_booked(token, result["booked"], reason)  # queued; sent by the outbox worker
        return result
    if name in ("capture_lead", "request_human", "send_to_crm"):
        return {"ok": True}
    warn("tool", f"unknown local function {name}")
    return {"ok": False}


async def call_ended(token: str, report: dict[str, Any]) -> None:
    rec = {
        "call_sid": token, "channel": report.get("channel"), "caller": report.get("caller"),
        "started_at": report.get("started_at"), "ended_at": report.get("ended_at") or time.time(),
        "seconds": report.get("seconds"), "turns": report.get("turns"), "end_reason": report.get("end_reason"),
        "summary": report.get("summary"), "outcome": report.get("outcome"), "transcript": report.get("transcript"),
        "functions": report.get("functions"), "lead": report.get("lead"),
    }
    try:
        await store.run(store.save_call, rec)
        info("call", f"call record saved ({token})")
    except Exception as exc:  # a lost record must never crash anything
        warn("call", f"call record not saved: {exc}")
    try:
        await store.run(outbound.on_report, token, report)  # an API call: finish it, send its webhook
    except Exception as exc:
        warn("outbound", f"outbound result not saved: {exc}")
    try:
        await notify.on_call_ended(token, report)
    except Exception as exc:
        warn("whatsapp", f"after-call message not queued: {exc}")


async def session_config(token: str) -> dict:  # kept for interface compatibility
    raise BackendError("no backend")


async def resend_spool() -> int:
    return 0


async def spool_loop() -> None:
    return None
