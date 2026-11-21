"""
store.py — the agent's own small database (SQLite, WAL): call records for debugging and the
appointment book, so two callers can never be given the same slot. Not a CRM.

Office hours and slot length come from business.py; every time is the office's local time
(config.OFFICE_TIMEZONE). Store calls from async code go through `run()` so the audio path
never blocks on disk.
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import threading
import time
from datetime import date, datetime, timedelta
from typing import Any, Callable, Optional
from zoneinfo import ZoneInfo

from . import business, config

_lock = threading.Lock()
_conn: Optional[sqlite3.Connection] = None


def _db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        os.makedirs(config.DATA_DIR, exist_ok=True)
        _conn = sqlite3.connect(os.path.join(config.DATA_DIR, "us-agent.db"), check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.executescript("""
            CREATE TABLE IF NOT EXISTS calls (
              id INTEGER PRIMARY KEY, call_sid TEXT UNIQUE, channel TEXT, caller TEXT, started_at REAL,
              ended_at REAL, seconds REAL, turns INTEGER, end_reason TEXT, summary TEXT, outcome TEXT,
              transcript TEXT, functions TEXT, lead TEXT);
            CREATE TABLE IF NOT EXISTS appointments (
              id INTEGER PRIMARY KEY, date TEXT NOT NULL, time TEXT NOT NULL, name TEXT NOT NULL, phone TEXT,
              reason TEXT, call_sid TEXT, created_at REAL, UNIQUE(date, time));
            CREATE TABLE IF NOT EXISTS wa_outbox (
              id INTEGER PRIMARY KEY, dedupe TEXT UNIQUE NOT NULL, kind TEXT, number TEXT NOT NULL,
              text TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER DEFAULT 0,
              next_at REAL, last_error TEXT, created_at REAL, sent_at REAL);
            CREATE INDEX IF NOT EXISTS wa_outbox_due ON wa_outbox(status, next_at);
            -- Outbound call API (outbound.py), 2026-11-20
            CREATE TABLE IF NOT EXISTS outbound_calls (
              id TEXT PRIMARY KEY, api_key TEXT NOT NULL, idempotency_key TEXT, request_hash TEXT,
              to_number TEXT NOT NULL, timezone TEXT, purpose TEXT NOT NULL, patient_name TEXT, context TEXT,
              metadata TEXT, callback_url TEXT, machine_detection INTEGER DEFAULT 1, on_voicemail TEXT,
              retry_on_voicemail INTEGER DEFAULT 0, consent_source TEXT,
              status TEXT NOT NULL, attempts INTEGER DEFAULT 0, max_attempts INTEGER DEFAULT 1,
              retry_delay_s INTEGER DEFAULT 600, next_at REAL, expires_at REAL, call_sid TEXT, call_sids TEXT,
              answered_by TEXT, twilio_status TEXT, last_error TEXT, outcome TEXT, summary TEXT, transcript TEXT,
              lead TEXT, duration_s REAL, answered_at REAL, ended_at REAL, finalized INTEGER DEFAULT 0,
              created_at REAL NOT NULL, updated_at REAL NOT NULL, completed_at REAL,
              UNIQUE(api_key, idempotency_key));
            CREATE INDEX IF NOT EXISTS outbound_due ON outbound_calls(status, next_at);
            CREATE INDEX IF NOT EXISTS outbound_sid ON outbound_calls(call_sid);
            CREATE INDEX IF NOT EXISTS outbound_key ON outbound_calls(api_key, created_at);
            CREATE INDEX IF NOT EXISTS outbound_to ON outbound_calls(to_number, created_at);
            CREATE TABLE IF NOT EXISTS dnc (
              number TEXT PRIMARY KEY, reason TEXT, source TEXT, added_at REAL);
            CREATE TABLE IF NOT EXISTS webhook_outbox (
              id INTEGER PRIMARY KEY, dedupe TEXT UNIQUE NOT NULL, url TEXT NOT NULL, body TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER DEFAULT 0, next_at REAL, last_error TEXT,
              created_at REAL, sent_at REAL);
            CREATE INDEX IF NOT EXISTS webhook_due ON webhook_outbox(status, next_at);
        """)
        # Added 2026-11-14 for WhatsApp: the number to message, and when the reminder went out
        cols = {r[1] for r in _conn.execute("PRAGMA table_info(appointments)")}
        if "wa_phone" not in cols:
            _conn.execute("ALTER TABLE appointments ADD COLUMN wa_phone TEXT")
        if "reminded_at" not in cols:
            _conn.execute("ALTER TABLE appointments ADD COLUMN reminded_at REAL")
        _conn.commit()
    return _conn


async def run(fn: Callable[..., Any], *args: Any) -> Any:
    return await asyncio.to_thread(fn, *args)


def tz() -> ZoneInfo:
    return ZoneInfo(config.OFFICE_TIMEZONE)


def office_now() -> datetime:
    return datetime.now(tz())


def free_slots(day: str) -> dict:
    """Free start times on YYYY-MM-DD in office hours, minus booked ones and anything within
    the next hour today."""
    try:
        d = date.fromisoformat(day)
    except ValueError:
        return {"ok": False, "say": "Which date? Please give the day and month."}
    now = office_now()
    if d < now.date():
        return {"ok": False, "say": "That date has already passed."}
    if d > now.date() + timedelta(days=60):
        return {"ok": False, "say": "We only book up to two months ahead."}
    hours = business.HOURS.get(d.weekday())
    if not hours:
        return {"ok": True, "date": day, "weekday": d.strftime("%A"), "slots": [], "closed": True,
                "say": f"The office is closed on {d.strftime('%A')}s."}
    out = _open_times(d, hours, now)
    fmt = lambda hm: datetime(2000, 1, 1, hm[0], hm[1]).strftime("%-I:%M %p").replace(":00", "")
    return {"ok": True, "date": day, "weekday": d.strftime("%A"), "office_hours": f"{fmt(hours[0])} to {fmt(hours[1])}",
            "slots": out[:8], "total_free": len(out),
            **({"say": "No open times left that day."} if not out else {})}


def _open_times(d: date, hours: tuple, now: datetime) -> list[str]:
    with _lock:
        taken = {r["time"] for r in _db().execute("SELECT time FROM appointments WHERE date = ?", (d.isoformat(),))}
    start, end = hours
    out = []
    t = datetime.combine(d, datetime.min.time()).replace(hour=start[0], minute=start[1], tzinfo=tz())
    stop = t.replace(hour=end[0], minute=end[1])
    while t + timedelta(minutes=business.SLOT_MINUTES) <= stop:
        hhmm = t.strftime("%H:%M")
        if hhmm not in taken and t > now + timedelta(hours=1):
            out.append(hhmm)
        t += timedelta(minutes=business.SLOT_MINUTES)
    return out


def book(day: str, hhmm: str, name: str, phone: str, reason: str, call_sid: str, wa_phone: str = "") -> dict:
    avail = free_slots(day)
    if not avail.get("ok"):
        return avail
    d = date.fromisoformat(day)
    hours = business.HOURS.get(d.weekday())
    if not hours or hhmm not in _open_times(d, hours, office_now()):
        return {"ok": False, "say": "That time isn't open. Offer another free time."}
    try:
        with _lock:
            _db().execute("INSERT INTO appointments (date, time, name, phone, reason, call_sid, created_at, wa_phone) "
                          "VALUES (?,?,?,?,?,?,?,?)",
                          (day, hhmm, name.strip()[:80], phone.strip()[:30], reason.strip()[:200], call_sid, time.time(),
                           wa_phone or None))
            _db().commit()
    except sqlite3.IntegrityError:
        return {"ok": False, "say": "Someone just took that time. Offer another free time."}
    return {"ok": True, "booked": {"date": day, "time": hhmm, "name": name},
            "spoken": spoken_when(day, hhmm)}


def spoken_when(day: str, hhmm: str) -> str:
    """'Tuesday, November 18th at 10:30 AM'."""
    when = datetime.combine(date.fromisoformat(day), datetime.min.time()).replace(hour=int(hhmm[:2]), minute=int(hhmm[3:]))
    return when.strftime("%A, %B ") + _ordinal(when.day) + " at " + when.strftime("%-I:%M %p").replace(":00", "")


def _ordinal(n: int) -> str:
    return f"{n}{'th' if 11 <= n % 100 <= 13 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def save_call(rec: dict) -> None:
    with _lock:
        _db().execute(
            """INSERT INTO calls (call_sid, channel, caller, started_at, ended_at, seconds, turns, end_reason, summary,
               outcome, transcript, functions, lead) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(call_sid) DO NOTHING""",
            (rec.get("call_sid"), rec.get("channel"), rec.get("caller"), rec.get("started_at"), rec.get("ended_at"),
             rec.get("seconds"), rec.get("turns"), rec.get("end_reason"), rec.get("summary"), rec.get("outcome"),
             json.dumps(rec.get("transcript") or [], ensure_ascii=False), json.dumps(rec.get("functions") or [], ensure_ascii=False),
             json.dumps(rec.get("lead"), ensure_ascii=False) if rec.get("lead") else None))
        _db().commit()


def recent_calls(limit: int = 20) -> list[dict]:
    with _lock:
        rows = _db().execute("SELECT call_sid, channel, started_at, seconds, turns, end_reason, outcome FROM calls ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


# ── WhatsApp outbox: survives restarts, one message per dedupe key ──────────
def wa_queue(dedupe: str, kind: str, number: str, text: str, not_before: float = 0.0) -> bool:
    """False if a message with this key was already queued (or sent)."""
    now = time.time()
    with _lock:
        cur = _db().execute("INSERT OR IGNORE INTO wa_outbox (dedupe, kind, number, text, next_at, created_at) "
                            "VALUES (?,?,?,?,?,?)", (dedupe, kind, number, text, max(now, not_before), now))
        _db().commit()
    return cur.rowcount == 1


def wa_due(limit: int = 10) -> list[dict]:
    with _lock:
        rows = _db().execute("SELECT * FROM wa_outbox WHERE status='pending' AND next_at <= ? ORDER BY id LIMIT ?",
                             (time.time(), limit)).fetchall()
    return [dict(r) for r in rows]


def wa_mark(msg_id: int, status: str, error: str = "", next_at: float = 0.0) -> None:
    with _lock:
        _db().execute("UPDATE wa_outbox SET status=?, attempts=attempts+1, last_error=?, next_at=?, "
                      "sent_at=CASE WHEN ?='sent' THEN ? ELSE sent_at END WHERE id=?",
                      (status, error[:300] or None, next_at or None, status, time.time(), msg_id))
        _db().commit()


def wa_counts() -> dict:
    with _lock:
        rows = _db().execute("SELECT status, COUNT(*) n FROM wa_outbox GROUP BY status").fetchall()
    return {r["status"]: r["n"] for r in rows}


def appointments_to_remind(now_ts: float, window_s: float, min_lead_s: float, min_gap_s: float) -> list[dict]:
    """Appointments starting within `window_s` (but not within `min_gap_s`), booked at least
    `min_lead_s` before they start (so a same-day booking doesn't get a reminder right after
    its confirmation), with a WhatsApp number and no reminder yet."""
    with _lock:
        rows = _db().execute("SELECT * FROM appointments WHERE wa_phone IS NOT NULL AND reminded_at IS NULL "
                             "AND date >= ?", (office_now().date().isoformat(),)).fetchall()
    out = []
    for r in rows:
        start = datetime.combine(date.fromisoformat(r["date"]), datetime.min.time()).replace(
            hour=int(r["time"][:2]), minute=int(r["time"][3:]), tzinfo=tz()).timestamp()
        if min_gap_s < start - now_ts <= window_s and start - (r["created_at"] or 0) >= min_lead_s:
            out.append(dict(r))
    return out


def mark_reminded(appt_id: int) -> None:
    with _lock:
        _db().execute("UPDATE appointments SET reminded_at=? WHERE id=?", (time.time(), appt_id))
        _db().commit()
