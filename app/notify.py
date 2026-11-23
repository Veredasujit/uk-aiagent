
"""
notify.py — WhatsApp messages for the UK dental practice.

Messages:
  booking     immediately after book_appointment succeeds
  thanks      after every call where the caller spoke
  missed      caller disconnected before speaking (inbound only)
  reminder    the day before an appointment

Who gets them:
  Only the number that called us (or the allowlisted test number).
  Browser callers have no number and receive nothing.

No marketing or follow-up sequences.

Everything goes through the outbox (store.wa_*), with a dedupe key
per call/appointment. Retries cannot send the same message twice.

The caller's full number is kept only in memory for the call
(CALLER_NUMBERS), and in the appointment row when booked.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Optional

from . import business, config, store
from .logging_setup import info, warn
from .whatsapp import WhatsAppError, send_text, wa_number

# CallSid -> caller number, set by /twilio/voice for phone calls only
CALLER_NUMBERS: dict[str, tuple[str, float]] = {}
_NUMBER_TTL_S = 2 * 3600

_BACKOFF_S = [30, 120, 300, 900, 1800]

REMINDER_WINDOW_S = 24 * 3600
REMINDER_MIN_GAP_S = 2 * 3600
REMINDER_MIN_LEAD_S = 20 * 3600


def office_phone() -> str:
    """Format the practice's UK phone number."""
    raw = "".join(ch for ch in config.TWILIO_PHONE_NUMBER if ch.isdigit())

    if raw.startswith("44"):
        national = raw[2:]
    elif raw.startswith("0"):
        national = raw[1:]
    else:
        return config.TWILIO_PHONE_NUMBER

    if len(national) == 10:
        return f"+44 {national[:4]} {national[4:7]} {national[7:]}"

    return f"+44 {national}"


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


# ── UK practice details ───────────────────────────────────────────────

PRACTICE_ADDRESS = (
    "1500 Brightside Avenue, Suite 210, London, SW1A 1AA, United Kingdom"
)

TIMEZONE_LABEL = "UK time (GMT/BST)"


# ── WhatsApp message texts ────────────────────────────────────────────

def booking_text(name: str, day: str, hhmm: str, reason: str = "") -> str:
    hi = f"Hi {_first(name)}," if _first(name) else "Hello,"

    lines = [
        f"{hi} your appointment at {config.BUSINESS_NAME} is confirmed.",
        "",
        f"When: {store.spoken_when(day, hhmm)} ({TIMEZONE_LABEL})",
        *(
            [f"Reason: {reason.strip()[:80]}"]
            if reason and reason.strip()
            else []
        ),
        f"Where: {PRACTICE_ADDRESS}",
        "",
        "For your first appointment, please arrive 15 minutes early.",
        "Please bring photo ID and your dental insurance details, if applicable.",
        "",
        f"Need to cancel or rearrange? Please call us on {office_phone()} at least 24 hours in advance.",
        "",
        "We look forward to seeing you!",
        f"- {config.AGENT_NAME}, {config.BUSINESS_NAME}",
    ]

    return "\n".join(lines) + _footer()


def thanks_text(
    name: str,
    booked: Optional[dict],
    follow_up: bool,
    outbound: bool = False,
) -> str:

    hi = f"Hi {_first(name)}," if _first(name) else "Hello,"

    if booked:
        nxt = (
            f"Your appointment is booked for "
            f"{store.spoken_when(booked['date'], booked['time'])}. "
            "All the details are in the confirmation message above."
        )

    elif follow_up:
        nxt = (
            "We've passed your request to our reception team. "
            "They'll get back to you during opening hours."
        )

    else:
        nxt = (
            f"Whenever you're ready, call us on {office_phone()} "
            "and we'll be happy to help you arrange an appointment."
        )

    thanks = (
        f"thank you for speaking with {config.BUSINESS_NAME}!"
        if outbound
        else f"thank you for calling {config.BUSINESS_NAME}!"
    )

    return (
        f"{hi} {thanks} {nxt}\n\n"
        f"Opening hours: {business.HOURS_SPOKEN} ({TIMEZONE_LABEL}).\n"
        f"Call us on {office_phone()}.\n\n"
        f"Have a lovely day!\n"
        f"- {config.AGENT_NAME}, {config.BUSINESS_NAME}"
    ) + _footer()


def missed_text() -> str:
    return (
        f"Hello, this is {config.AGENT_NAME} from {config.BUSINESS_NAME}. "
        "It looks like we got disconnected before we could speak.\n\n"
        f"Please call us back on {office_phone()} "
        "and we'll be happy to help.\n\n"
        f"Opening hours: {business.HOURS_SPOKEN} ({TIMEZONE_LABEL})."
    ) + _footer()


def reminder_text(name: str, day: str, hhmm: str) -> str:
    hi = f"Hi {_first(name)}," if _first(name) else "Hello,"

    return (
        f"{hi} just a friendly reminder from {config.BUSINESS_NAME}: "
        f"your dental appointment is on "
        f"{store.spoken_when(day, hhmm)} ({TIMEZONE_LABEL}).\n\n"
        f"Address: {PRACTICE_ADDRESS}.\n\n"
        f"If you can't make it, please call us on {office_phone()}.\n\n"
        f"We look forward to seeing you!\n"
        f"- {config.AGENT_NAME}, {config.BUSINESS_NAME}"
    ) + _footer()


# ── Queue messages ────────────────────────────────────────────────────

async def _queue(
    dedupe: str,
    kind: str,
    number: str,
    text: str,
) -> None:

    if not config.WHATSAPP_ENABLED or not number:
        return

    try:
        if await store.run(store.wa_queue, dedupe, kind, number, text):
            info("whatsapp", f"queued {kind} to ...{number[-4:]}")

    except Exception as exc:
        warn("whatsapp", f"could not queue {kind}: {exc}")


async def on_booked(
    call_sid: str,
    booked: dict,
    reason: str = "",
) -> None:

    number = number_for(call_sid)

    if number and booked:
        await _queue(
            f"booking:{call_sid}:{booked['date']}T{booked['time']}",
            "booking",
            number,
            booking_text(
                booked.get("name", ""),
                booked["date"],
                booked["time"],
                reason,
            ),
        )


async def on_call_ended(
    call_sid: str,
    report: dict[str, Any],
) -> None:

    number = number_for(call_sid)
    CALLER_NUMBERS.pop(call_sid, None)

    if not number or report.get("channel") != "phone":
        return

    functions = report.get("functions") or []

    bookings = [
        f["result"]["booked"]
        for f in functions
        if (
            f.get("name") == "book_appointment"
            and f.get("ok")
            and isinstance(f.get("result"), dict)
            and f["result"].get("booked")
        )
    ]

    lead = report.get("lead") or {}

    talked = (report.get("outcome") or "") != "no_conversation"

    if talked and config.WHATSAPP_THANKS:

        name = (
            (bookings[-1].get("name") if bookings else "")
            or lead.get("name", "")
        )

        follow_up = (
            (report.get("outcome") or "")
            in ("callback_requested", "human_requested")
        )

        await _queue(
            f"thanks:{call_sid}",
            "thanks",
            number,
            thanks_text(
                name,
                bookings[-1] if bookings else None,
                follow_up,
                report.get("direction") == "outbound",
            ),
        )

    elif (
        not talked
        and config.WHATSAPP_MISSED
        and report.get("direction") != "outbound"
        and (report.get("seconds") or 0)
        >= config.WHATSAPP_MISSED_MIN_S
    ):

        await _queue(
            f"missed:{call_sid}",
            "missed",
            number,
            missed_text(),
        )


# ── Workers ───────────────────────────────────────────────────────────

async def send_due() -> int:
    sent = 0

    for m in await store.run(store.wa_due, 10):

        try:
            await send_text(m["number"], m["text"])

            await store.run(
                store.wa_mark,
                m["id"],
                "sent",
            )

            sent += 1

        except WhatsAppError as exc:

            attempt = m.get("attempts") or 0

            if exc.retryable and attempt < len(_BACKOFF_S):

                await store.run(
                    store.wa_mark,
                    m["id"],
                    "pending",
                    str(exc),
                    time.time() + _BACKOFF_S[attempt],
                )

                warn(
                    "whatsapp",
                    f"{m['kind']} #{m['id']} will retry: {exc}",
                )

            else:

                await store.run(
                    store.wa_mark,
                    m["id"],
                    "failed",
                    str(exc),
                )

                warn(
                    "whatsapp",
                    f"{m['kind']} #{m['id']} failed: {exc}",
                )

        except Exception as exc:

            await store.run(
                store.wa_mark,
                m["id"],
                "pending",
                str(exc),
                time.time() + 60,
            )

            warn(
                "whatsapp",
                f"{m['kind']} #{m['id']}: {exc}",
            )

    return sent


async def queue_reminders() -> int:

    if not config.WHATSAPP_REMINDERS:
        return 0

    n = 0

    for a in await store.run(
        store.appointments_to_remind,
        time.time(),
        REMINDER_WINDOW_S,
        REMINDER_MIN_LEAD_S,
        REMINDER_MIN_GAP_S,
    ):

        await _queue(
            f"reminder:{a['id']}",
            "reminder",
            a["wa_phone"],
            reminder_text(
                a["name"],
                a["date"],
                a["time"],
            ),
        )

        await store.run(
            store.mark_reminded,
            a["id"],
        )

        n += 1

    return n


async def worker() -> None:
    """
    One loop for the whole process:
    - Send pending messages every few seconds.
    - Check appointment reminders every 5 minutes.
    """

    if not config.WHATSAPP_ENABLED:
        info("whatsapp", "disabled (no WHATSAPP_API_KEY)")
        return

    info("whatsapp", "UK WhatsApp outbox worker running")

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
