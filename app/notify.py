"""
notify.py — WhatsApp messages to the caller (owner's request, 2026-11-14):

  booking     the moment book_appointment succeeds: day, time, address, what to bring
  thanks      after every call where the caller spoke: what happens next
  missed      the caller hung up before saying anything (inbound only, not misdials)
  reminder    the day before an appointment (only if it was booked a day or more ahead)

Who gets them: only the number that called us (or the allowlisted test number we rang).
Browser callers have no number and get nothing. No marketing, no follow-up sequences.

Everything goes through the outbox (store.wa_*): a dedupe key per call/appointment, so a
retry or a restart can never send the same message twice, and failures back off and retry
for about an hour. The caller's full number is kept only in memory for the call
(CALLER_NUMBERS), and in the appointment row when they book.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Optional

from . import business, config, store
from .logging_setup import info, warn
from .whatsapp import WhatsAppError, send_text, wa_number

# CallSid → gateway number ("15125550100"), set by /twilio/voice for phone calls only
CALLER_NUMBERS: dict[str, tuple[str, float]] = {}
_NUMBER_TTL_S = 2 * 3600

_BACKOFF_S = [30, 120, 300, 900, 1800]   # then give up (~53 min of retries)
REMINDER_WINDOW_S = 24 * 3600            # remind within the 24 h before the visit…
REMINDER_MIN_GAP_S = 2 * 3600            # …but not in the last 2 h
REMINDER_MIN_LEAD_S = 20 * 3600          # only for visits booked ~a day or more ahead


def office_phone() -> str:
    d = "".join(ch for ch in config.TWILIO_PHONE_NUMBER if ch.isdigit())
    if len(d) == 11 and d.startswith("1"):
        return f"+1 ({d[1:4]}) {d[4:7]}-{d[7:]}"
    return config.TWILIO_PHONE_NUMBER


def remember_caller(call_sid: str, e164: str) -> None:
    now = time.time()
    for sid, (_, at) in list(CALLER_NUMBERS.items()):
        if now - at > _NUMBER_TTL_S:
            CALLER_NUMBERS.pop(sid, None)
    n = wa_number(e164)
    if call_sid and n:
        CALLER_NUMBERS[call_sid] = (n, now)


def number_for(call_sid: str) -> str:
    v = CALLER_NUMBERS.get(call_sid)
    return v[0] if v else ""


def _first(name: str) -> str:
    return (name or "").strip().split(" ")[0].title() if name else ""


def _footer() -> str:
    return f"\n\n{config.WHATSAPP_FOOTER}" if config.WHATSAPP_FOOTER else ""


# ── message texts (plain ASCII; facts only from business.FAQ) ────────────────
def booking_text(name: str, day: str, hhmm: str, reason: str = "") -> str:
    hi = f"Hi {_first(name)}," if _first(name) else "Hi,"
    lines = [
        f"{hi} your appointment at {config.BUSINESS_NAME} is confirmed.",
        "",
        f"When: {store.spoken_when(day, hhmm)} (Central Time)",
        *( [f"For: {reason.strip()[:80]}"] if reason and reason.strip() else [] ),
        "Where: 1500 Brightside Avenue, Suite 210, Austin, TX 78704 (free parking behind the building)",
        "",
        "First visit? Please arrive 15 minutes early and bring your insurance card and a photo ID.",
        f"Need to reschedule or cancel? Please call us at {office_phone()} at least 24 hours ahead.",
        "",
        "See you soon!",
        f"- {config.AGENT_NAME}, {config.BUSINESS_NAME}",
    ]
    return "\n".join(lines) + _footer()


def thanks_text(name: str, booked: Optional[dict], follow_up: bool, outbound: bool = False) -> str:
    hi = f"Hi {_first(name)}," if _first(name) else "Hi there,"
    if booked:
        nxt = (f"Your appointment is on {store.spoken_when(booked['date'], booked['time'])}. "
               "The details are in the message above.")
    elif follow_up:
        nxt = "We've passed your request to our front desk, and they'll get back to you during office hours."
    else:
        nxt = f"Whenever you're ready, call us at {office_phone()} and we'll get you booked in."
    thanks = f"thanks for talking with us at {config.BUSINESS_NAME}!" if outbound else f"thanks for calling {config.BUSINESS_NAME}!"
    return (f"{hi} {thanks} {nxt}\n\n"
            f"Hours: {business.HOURS_SPOKEN} (Central Time).\n"
            f"Call us anytime at {office_phone()}.\n\n"
            f"Have a great day!\n- {config.AGENT_NAME}, {config.BUSINESS_NAME}") + _footer()


def missed_text() -> str:
    return (f"Hi, this is {config.AGENT_NAME} from {config.BUSINESS_NAME}. It looks like we got cut off "
            f"before we could talk. Call us back anytime at {office_phone()} and we'll be happy to help.\n\n"
            f"Hours: {business.HOURS_SPOKEN} (Central Time).") + _footer()


def reminder_text(name: str, day: str, hhmm: str) -> str:
    hi = f"Hi {_first(name)}," if _first(name) else "Hi,"
    return (f"{hi} a friendly reminder from {config.BUSINESS_NAME}: your appointment is on "
            f"{store.spoken_when(day, hhmm)} (Central Time).\n\n"
            "Where: 1500 Brightside Avenue, Suite 210, Austin, TX 78704.\n"
            f"Can't make it? Please call us at {office_phone()}.\n\n"
            f"See you soon!\n- {config.AGENT_NAME}, {config.BUSINESS_NAME}") + _footer()


# ── what the call does ───────────────────────────────────────────────────────
async def _queue(dedupe: str, kind: str, number: str, text: str) -> None:
    if not config.WHATSAPP_ENABLED or not number:
        return
    try:
        if await store.run(store.wa_queue, dedupe, kind, number, text):
            info("whatsapp", f"queued {kind} to ...{number[-4:]}")
    except Exception as exc:  # a message must never break a call
        warn("whatsapp", f"could not queue {kind}: {exc}")


async def on_booked(call_sid: str, booked: dict, reason: str = "") -> None:
    number = number_for(call_sid)
    if number and booked:
        await _queue(f"booking:{call_sid}:{booked['date']}T{booked['time']}", "booking", number,
                     booking_text(booked.get("name", ""), booked["date"], booked["time"], reason))


async def on_call_ended(call_sid: str, report: dict[str, Any]) -> None:
    number = number_for(call_sid)
    CALLER_NUMBERS.pop(call_sid, None)
    if not number or report.get("channel") != "phone":
        return
    functions = report.get("functions") or []
    bookings = [f["result"]["booked"] for f in functions
                if f.get("name") == "book_appointment" and f.get("ok") and isinstance(f.get("result"), dict)
                and f["result"].get("booked")]
    lead = report.get("lead") or {}
    talked = (report.get("outcome") or "") != "no_conversation"
    if talked and config.WHATSAPP_THANKS:
        name = (bookings[-1].get("name") if bookings else "") or lead.get("name", "")
        follow_up = (report.get("outcome") or "") in ("callback_requested", "human_requested")
        await _queue(f"thanks:{call_sid}", "thanks", number,
                     thanks_text(name, bookings[-1] if bookings else None, follow_up,
                                 report.get("direction") == "outbound"))
    elif (not talked and config.WHATSAPP_MISSED and report.get("direction") != "outbound"
          and (report.get("seconds") or 0) >= config.WHATSAPP_MISSED_MIN_S):
        await _queue(f"missed:{call_sid}", "missed", number, missed_text())


# ── workers ──────────────────────────────────────────────────────────────────
async def send_due() -> int:
    sent = 0
    for m in await store.run(store.wa_due, 10):
        try:
            await send_text(m["number"], m["text"])
            await store.run(store.wa_mark, m["id"], "sent")
            sent += 1
        except WhatsAppError as exc:
            attempt = (m.get("attempts") or 0)
            if exc.retryable and attempt < len(_BACKOFF_S):
                await store.run(store.wa_mark, m["id"], "pending", str(exc), time.time() + _BACKOFF_S[attempt])
                warn("whatsapp", f"{m['kind']} #{m['id']} will retry: {exc}")
            else:
                await store.run(store.wa_mark, m["id"], "failed", str(exc))
                warn("whatsapp", f"{m['kind']} #{m['id']} failed: {exc}")
        except Exception as exc:
            await store.run(store.wa_mark, m["id"], "pending", str(exc), time.time() + 60)
            warn("whatsapp", f"{m['kind']} #{m['id']}: {exc}")
    return sent


async def queue_reminders() -> int:
    if not config.WHATSAPP_REMINDERS:
        return 0
    n = 0
    for a in await store.run(store.appointments_to_remind, time.time(), REMINDER_WINDOW_S,
                             REMINDER_MIN_LEAD_S, REMINDER_MIN_GAP_S):
        await _queue(f"reminder:{a['id']}", "reminder", a["wa_phone"], reminder_text(a["name"], a["date"], a["time"]))
        await store.run(store.mark_reminded, a["id"])
        n += 1
    return n


async def worker() -> None:
    """One loop for the whole process: send what's due every few seconds, look for
    reminders every 5 minutes."""
    if not config.WHATSAPP_ENABLED:
        info("whatsapp", "disabled (no WHATSAPP_API_KEY)")
        return
    info("whatsapp", "outbox worker running")
    last_reminder_check = 0.0
    while True:
        try:
            if time.time() - last_reminder_check > 300:
                last_reminder_check = time.time()
                await queue_reminders()
            await send_due()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            warn("whatsapp", f"worker: {exc}")
        await asyncio.sleep(3)
