"""The US agent end to end, without vendors: Twilio webhooks (signatures, US-only, busy),
browser tokens, the Media Stream bridge, a whole call through the real CallSession, the
appointment book and the prompt."""
from __future__ import annotations

import asyncio
import base64
import json
import time
from datetime import timedelta

import jwt as pyjwt
import pytest
from fastapi.testclient import TestClient
from twilio.request_validator import RequestValidator

from app import audio, prompt, server, session as session_mod, store, twilio_api
from app.twilio_bridge import TwilioBridge

client = TestClient(server.app)
VOICE_URL = "https://us-agent.example.com/twilio/voice"


def twilio_post(path: str, params: dict, *, sign: bool = True):
    url = f"https://us-agent.example.com{path}"
    headers = {}
    if sign:
        headers["X-Twilio-Signature"] = RequestValidator("auth-test-token").compute_signature(url, params)
    return client.post(path, data=params, headers=headers)


# ── fakes for a whole call ───────────────────────────────────────────────────
class FakeSTT:
    instance = None

    def __init__(self, call_log, on_transcript, on_speech_started=None, on_utterance_end=None, language=None):
        self.on_transcript = on_transcript
        self.language = language
        FakeSTT.instance = self

    async def start(self):
        return None

    async def send_audio(self, pcm):
        return None

    async def close(self):
        return None

    async def say(self, text):
        await self.on_transcript(text, True, True)


class FakeTTS:
    spoken: list[str] = []

    def __init__(self, call_log, provider=None, voice=None, language=None):
        FakeTTS.voice = (provider, voice)

    async def prewarm(self):
        return None

    async def stream(self, text):
        FakeTTS.spoken.append(text)
        yield b"\x10\x00" * 800

    def abort(self):
        pass

    async def close(self):
        pass


def scripted_llm(script):
    class FakeLLM:
        def __init__(self, call_log):
            pass

        async def warm(self, messages, tools_schema):
            return None

        async def stream(self, messages, tools_schema):
            step = script.pop(0) if script else ("say", "Okay.")
            text = step[1] if step[0] in ("say", "say+tool") else ""
            if text:
                yield "token", text
            if step[0] != "say":
                yield "tool_calls", [{"id": f"t{len(script)}", "name": step[-2], "arguments": step[-1]}]
            yield "done", text

        async def close(self):
            pass

    return FakeLLM


# ── webhooks ─────────────────────────────────────────────────────────────────
def test_unsigned_or_forged_webhooks_are_refused():
    p = {"CallSid": "CA1", "From": "+15125550100", "FromCountry": "US"}
    assert twilio_post("/twilio/voice", p, sign=False).status_code == 403
    r = client.post("/twilio/voice", data=p, headers={"X-Twilio-Signature": "forged"})
    assert r.status_code == 403


def test_us_phone_caller_is_streamed_to_the_agent_with_a_signed_token():
    r = twilio_post("/twilio/voice", {"CallSid": "CA2", "From": "+15125550100", "FromCountry": "US", "Direction": "inbound"})
    assert r.status_code == 200
    xml = r.text
    assert '<Stream url="wss://us-agent.example.com/twilio/stream">' in xml
    assert f'value="{twilio_api.sign("stream", "CA2")}"' in xml
    assert 'name="channel" value="phone"' in xml
    assert "5550100" not in xml, "the full caller number never travels to the stream"
    assert "<Dial" not in xml


def test_non_us_caller_hears_a_notice_and_no_agent():
    r = twilio_post("/twilio/voice", {"CallSid": "CA3", "From": "+447700900123", "FromCountry": "GB"})
    assert "<Stream" not in r.text and "<Say" in r.text and "<Hangup" in r.text
    assert "United States" in r.text


def test_browser_caller_is_streamed_as_web():
    r = twilio_post("/twilio/voice", {"CallSid": "CA4", "From": "client:web-abc123", "Direction": "inbound"})
    assert 'name="channel" value="web"' in r.text and 'value="web-abc123"' in r.text


def test_all_lines_busy():
    server.LIVE.update({"CAx": object(), "CAy": object()})
    try:
        r = twilio_post("/twilio/voice", {"CallSid": "CA5", "From": "+15125550100", "FromCountry": "US"})
        assert "<Stream" not in r.text and "busy" in r.text
    finally:
        server.LIVE.pop("CAx")
        server.LIVE.pop("CAy")


# ── browser tokens ───────────────────────────────────────────────────────────
def test_browser_token_only_reaches_our_app_and_is_rate_limited():
    assert client.post("/web/token", headers={"Origin": "https://evil.example"}).status_code == 403
    r = client.post("/web/token", headers={"Origin": "https://vedronix.com", "X-Real-IP": "203.0.113.9"})
    assert r.status_code == 200
    body = r.json()
    claims = pyjwt.decode(body["token"], options={"verify_signature": False})
    voice = claims["grants"]["voice"]
    assert voice["outgoing"]["application_sid"] == "AP00000000000000000000000000000000"
    assert not voice.get("incoming", {}).get("allow"), "browsers can't receive calls"
    assert claims["grants"]["identity"] == body["identity"] and body["identity"].startswith("web-")
    assert 0 < claims["exp"] - time.time() <= 605, "short-lived token"
    for _ in range(2):
        client.post("/web/token", headers={"Origin": "https://vedronix.com", "X-Real-IP": "203.0.113.9"})
    assert client.post("/web/token", headers={"Origin": "https://vedronix.com", "X-Real-IP": "203.0.113.9"}).status_code == 429
    # transcript stream needs the key signed for that identity
    assert client.get("/web/events", params={"identity": body["identity"], "key": "nope"}).status_code == 403


# ── the bridge ───────────────────────────────────────────────────────────────
class QueueWS:
    async def accept(self):
        return None

    def __init__(self):
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.sent: list[dict] = []

    async def receive_text(self):
        item = await self.inbox.get()
        if item is None:
            raise RuntimeError("closed")
        return item

    async def send_text(self, text):
        self.sent.append(json.loads(text))

    async def close(self, code=1000):
        await self.inbox.put(None)


async def test_bridge_translates_both_ways():
    ws = QueueWS()
    b = TwilioBridge(ws)  # type: ignore[arg-type]
    pcm = b"".join(int(v).to_bytes(2, "little", signed=True) for v in (0, 1000, -1000, 8000, -8000) * 40)
    for m in ({"event": "connected"},
              {"event": "start", "streamSid": "MZ1", "start": {"callSid": "CA9", "customParameters": {"token": "t"}}},
              {"event": "media", "media": {"track": "inbound", "payload": base64.b64encode(audio.pcm_to_mulaw(pcm)).decode()}},
              {"event": "stop"}):
        await ws.inbox.put(json.dumps(m))
    assert await b.wait_start()
    assert b.call_sid == "CA9" and b.params == {"token": "t"}
    assert json.loads(await b.receive_text())["type"] == "start"
    got = json.loads(await b.receive_text())
    back = base64.b64decode(got["data"]["audio_b64"])
    assert len(back) == len(pcm) and abs(int.from_bytes(back[2:4], "little", signed=True) - 1000) < 64
    assert json.loads(await b.receive_text())["type"] == "stop"
    await b.send_text(json.dumps({"type": "audio", "audio_b64": base64.b64encode(pcm).decode()}))
    await b.send_text(json.dumps({"type": "clear"}))
    assert ws.sent[0]["event"] == "media" and ws.sent[0]["streamSid"] == "MZ1"
    assert len(base64.b64decode(ws.sent[0]["media"]["payload"])) == len(pcm) // 2
    assert ws.sent[1] == {"event": "clear", "streamSid": "MZ1"}


# ── a whole call ─────────────────────────────────────────────────────────────
def test_stream_with_forged_token_is_refused():
    with client.websocket_connect("/twilio/stream") as ws:
        ws.send_text(json.dumps({"event": "start", "streamSid": "MZ2",
                                 "start": {"callSid": "CA10", "customParameters": {"token": "forged"}}}))
        with pytest.raises(Exception):
            ws.receive_text()
    assert "CA10" not in server.LIVE


async def test_whole_phone_call_books_and_hangs_up(monkeypatch):
    FakeTTS.spoken = []
    monkeypatch.setattr(session_mod, "DeepgramSTT", FakeSTT)
    monkeypatch.setattr(session_mod, "TTSEngine", FakeTTS)
    d = store.office_now() + timedelta(days=1)
    while d.weekday() > 3:  # a Mon-Thu, open 8-5
        d += timedelta(days=1)
    day = d.strftime("%Y-%m-%d")
    monkeypatch.setattr(session_mod, "LLMClient", scripted_llm([
        ("tool", "check_availability", {"date": day}),
        ("say", "I have 10:30 in the morning or 2 in the afternoon. Which works better?"),
        ("tool", "book_appointment", {"date": day, "time": "10:30", "name": "Jamie Carter", "phone": "5125550100"}),
        ("say+tool", "You're all set. Thanks for calling, have a great day!", "end_call", {"reason": "done"}),
    ]))

    async def no_summary(cfg, transcript, functions):
        return {"summary": "Booked a cleaning.", "outcome": "appointment_booked", "lead": None}

    monkeypatch.setattr(session_mod, "summarise", no_summary)
    hung = []

    async def hangup(call_sid, reason=""):
        hung.append(call_sid)
        return True

    monkeypatch.setattr(twilio_api, "hangup", hangup)
    ws = QueueWS()
    for m in ({"event": "connected"},
              {"event": "start", "streamSid": "MZ3", "start": {"callSid": "CA11", "customParameters": {
                  "token": twilio_api.sign("stream", "CA11"), "channel": "phone", "caller": "+1•••0100"}}}):
        await ws.inbox.put(json.dumps(m))
    call = asyncio.create_task(server.media_stream(ws))  # type: ignore[arg-type]
    await asyncio.sleep(1.2)
    assert FakeTTS.spoken[0].startswith("Hi, thanks for calling Brightside Dental")
    assert FakeTTS.voice == ("elevenlabs", "EXAVITQu4vr4xnSDxMaL")
    assert ws.sent and ws.sent[0]["event"] == "media" and ws.sent[0]["streamSid"] == "MZ3"
    await FakeSTT.instance.say("Hi, I'd like a cleaning tomorrow morning")
    await asyncio.sleep(0.4)
    await FakeSTT.instance.say("10:30 works, I'm Jamie Carter")
    await asyncio.wait_for(call, timeout=10)
    for _ in range(50):
        if not session_mod.POSTCALL_TASKS:
            break
        await asyncio.sleep(0.05)
    await asyncio.sleep(0.1)
    assert "CA11" in hung and "CA11" not in server.LIVE
    assert any(t.startswith("You're all set") for t in FakeTTS.spoken)
    assert "10:30" not in store.free_slots(day)["slots"], "the slot is now taken"
    rec = [c for c in store.recent_calls() if c["call_sid"] == "CA11"]
    assert rec and rec[0]["channel"] == "phone" and rec[0]["outcome"] == "appointment_booked"


# ── appointment book ─────────────────────────────────────────────────────────
def test_slots_never_double_book_and_sunday_is_closed():
    d = store.office_now() + timedelta(days=2)
    while d.weekday() > 3:
        d += timedelta(days=1)
    day = d.strftime("%Y-%m-%d")
    first = store.book(day, "14:00", "Ana Lopez", "5125550111", "cleaning", "CA20")
    assert first["ok"] and "2 PM" in first["spoken"]
    again = store.book(day, "14:00", "Ben Ross", "5125550112", "cleaning", "CA21")
    assert not again["ok"]
    assert "14:00" not in store.free_slots(day)["slots"] + [None]
    assert not store.book(day, "07:00", "Early Bird", "", "", "CA22")["ok"], "outside office hours"
    sun = d + timedelta(days=(6 - d.weekday()))
    assert store.free_slots(sun.strftime("%Y-%m-%d"))["closed"]


def test_prompt_is_american_and_ends_with_the_length_rule():
    cfg = server._session_cfg("CA30", "phone", "+1•••0100")
    p = prompt.build_system_prompt(cfg)
    assert "911" in p and "TODAY:" in p and "American English" in p
    assert p.rstrip().endswith("ending with at most one question.")
    assert "112" not in p and "Hindi" not in p
    assert prompt.greeting_for(cfg).startswith("Hi, thanks for calling Brightside Dental! This is Ava, the virtual assistant")


def test_guard_strips_booking_claims_until_a_booking_succeeds(monkeypatch):
    monkeypatch.setattr(session_mod, "DeepgramSTT", FakeSTT)
    monkeypatch.setattr(session_mod, "TTSEngine", FakeTTS)
    s = session_mod.CallSession(QueueWS(), "CA40", server._session_cfg("CA40", "phone", "x"))
    said = "You're all set! The front desk will confirm your appointment for today at 4 PM."
    assert s._guard_booking_claims(said) == "Got it! The front desk will confirm your appointment for today at 4 PM."
    assert s._guard_booking_claims("Great, see you then!") == "Great, got it!"
    assert s._guard_booking_claims("Our hours are 8 to 5.") == "Our hours are 8 to 5."
    s.tools.calls.append({"name": "book_appointment", "ok": True})
    assert s._guard_booking_claims(said) == said, "after a real booking the confirmation stands"


def test_test_calls_only_to_allowlisted_numbers_with_the_admin_key(monkeypatch):
    from app import config
    monkeypatch.setattr(config, "ADMIN_API_KEY", "admin-test")
    monkeypatch.setattr(config, "OUTBOUND_TEST_NUMBERS", ["9934601244"])
    placed = []

    async def fake_call(to):
        placed.append(to)
        return {"call_sid": "CAout", "status": "queued"}

    monkeypatch.setattr(twilio_api, "test_call", fake_call)
    assert client.post("/admin/test-call", json={}).status_code == 401
    assert client.post("/admin/test-call", json={"to": "+15125550100"}, headers={"X-Admin-Key": "admin-test"}).status_code == 403
    r = client.post("/admin/test-call", json={}, headers={"X-Admin-Key": "admin-test"})
    assert r.status_code == 200 and placed == ["+919934601244"]
    # Twilio's webhook for that call: the agent answers with the test greeting
    ok = twilio_post("/twilio/voice", {"CallSid": "CAout", "From": "+17175368381", "To": "+919934601244", "Direction": "outbound-api"})
    assert "<Stream" in ok.text and 'name="direction" value="outbound"' in ok.text
    # An outbound call to anything else is hung up even if it was somehow created
    bad = twilio_post("/twilio/voice", {"CallSid": "CAbad", "From": "+17175368381", "To": "+15125550100", "Direction": "outbound-api"})
    assert "<Stream" not in bad.text and "<Hangup" in bad.text
    assert server._session_cfg("CAout", "phone", "x", "outbound")["agent"]["greeting"].startswith("Hi! It's Ava")


def test_turn_end_waits_longer_for_unfinished_thoughts():
    assert session_mod._quiet_needed("Hi. And first of") > 1.5
    assert session_mod._quiet_needed("My name is") > 1.5
    assert session_mod._quiet_needed("What time do you open on Saturday?") < 0.8
    assert 0.8 < session_mod._quiet_needed("I need a cleaning next week") < 1.5


async def test_cached_greeting_plays_without_synthesis(monkeypatch):
    FakeTTS.spoken = []
    monkeypatch.setattr(session_mod, "DeepgramSTT", FakeSTT)
    monkeypatch.setattr(session_mod, "TTSEngine", FakeTTS)
    monkeypatch.setattr(session_mod, "LLMClient", scripted_llm([]))
    cfg = server._session_cfg("CA50", "phone", "x")
    cfg["greeting_pcm"] = b"\x20\x00" * 16000
    ws = QueueWS()
    bridge = TwilioBridge(ws)  # type: ignore[arg-type]
    await ws.inbox.put(json.dumps({"event": "start", "streamSid": "MZ50", "start": {"callSid": "CA50"}}))
    assert await bridge.wait_start()
    s = session_mod.CallSession(bridge, "CA50", cfg)
    run = asyncio.create_task(s.run())
    await asyncio.sleep(1.0)
    assert FakeTTS.spoken == [], "the greeting came from the cache, not the voice service"
    assert ws.sent and ws.sent[0]["event"] == "media"
    await ws.inbox.put(None)
    await asyncio.wait_for(run, timeout=10)


# ── WhatsApp (booking confirmed, thanks, missed, reminder) ──────────────────
from app import backend, notify, whatsapp  # noqa: E402


def _weekday(days_ahead: int) -> str:
    d = store.office_now() + timedelta(days=days_ahead)
    while d.weekday() > 3:
        d += timedelta(days=1)
    return d.strftime("%Y-%m-%d")


def _outbox(prefix: str) -> list[dict]:
    with store._lock:
        rows = store._db().execute("SELECT * FROM wa_outbox WHERE dedupe LIKE ? ORDER BY id", (prefix + "%",)).fetchall()
    return [dict(r) for r in rows]


def test_webhook_remembers_phone_callers_number_but_not_browsers():
    twilio_post("/twilio/voice", {"CallSid": "CAwa1", "From": "+15125550100", "To": "+17175368381",
                                  "FromCountry": "US", "Direction": "inbound"})
    assert notify.number_for("CAwa1") == "15125550100"
    twilio_post("/twilio/voice", {"CallSid": "CAwa2", "From": "client:web-abc", "To": "", "Direction": "inbound"})
    assert notify.number_for("CAwa2") == ""
    assert whatsapp.wa_number("5125550100") == "", "no guessing a country for a bare number"


async def test_booking_queues_one_confirmation_to_the_caller(monkeypatch):
    monkeypatch.setattr(notify.config, "WHATSAPP_ENABLED", True)
    notify.remember_caller("CAwa3", "+15125550123")
    day = _weekday(3)
    r = await backend.tool("CAwa3", "book_appointment", {"date": day, "time": "11:30", "name": "maria garcia",
                                                         "requirements": "cleaning"})
    assert r["ok"]
    await notify.on_booked("CAwa3", r["booked"], "cleaning")  # a repeat must not send twice
    msgs = _outbox("booking:CAwa3")
    assert len(msgs) == 1 and msgs[0]["number"] == "15125550123"
    text = msgs[0]["text"]
    assert text.startswith("Hi Maria, your appointment at Brightside Dental is confirmed.")
    assert "11:30 AM" in text and "1500 Brightside Avenue" in text and "For: cleaning" in text
    assert text == whatsapp.ascii_safe(text), "plain ASCII for the gateway"
    with store._lock:
        row = store._db().execute("SELECT wa_phone FROM appointments WHERE call_sid='CAwa3'").fetchone()
    assert row["wa_phone"] == "15125550123"


async def test_after_call_thanks_missed_and_nothing_for_browsers(monkeypatch):
    monkeypatch.setattr(notify.config, "WHATSAPP_ENABLED", True)
    base = {"channel": "phone", "direction": "inbound", "seconds": 40, "functions": [], "lead": None}

    notify.remember_caller("CAwa4", "+15125550144")
    await notify.on_call_ended("CAwa4", {**base, "outcome": "human_requested", "lead": {"name": "Tom Lee"}})
    t = _outbox("thanks:CAwa4")[0]["text"]
    assert t.startswith("Hi Tom, thanks for calling Brightside Dental!") and "front desk" in t
    assert notify.number_for("CAwa4") == "", "number forgotten after the call"

    notify.remember_caller("CAwa5", "+15125550155")
    await notify.on_call_ended("CAwa5", {**base, "outcome": "no_conversation", "seconds": 12})
    assert "cut off" in _outbox("missed:CAwa5")[0]["text"]

    notify.remember_caller("CAwa6", "+15125550166")   # a 2-second misdial: no message
    await notify.on_call_ended("CAwa6", {**base, "outcome": "no_conversation", "seconds": 2})
    notify.remember_caller("CAwa7", "+919934601244")  # our test call that nobody talked on
    await notify.on_call_ended("CAwa7", {**base, "direction": "outbound", "outcome": "no_conversation"})
    await notify.on_call_ended("CAwa8", {**base, "channel": "web", "outcome": "information_given"})
    assert not _outbox("missed:CAwa6") and not _outbox("missed:CAwa7") and not _outbox("thanks:CAwa8")

    booked = {"date": _weekday(4), "time": "14:00", "name": "Ana Ruiz"}
    notify.remember_caller("CAwa9", "+15125550199")
    await notify.on_call_ended("CAwa9", {**base, "outcome": "appointment_booked", "functions": [
        {"name": "book_appointment", "ok": True, "result": {"ok": True, "booked": booked}}]})
    t = _outbox("thanks:CAwa9")[0]["text"]
    assert t.startswith("Hi Ana,") and "2 PM" in t and "message above" in t


async def test_outbox_sends_retries_and_gives_up(monkeypatch):
    sent = []

    async def fake_send(number, text):
        if number == "1000":
            raise whatsapp.WhatsAppError("gateway 503")
        if number == "2000":
            raise whatsapp.WhatsAppError("rejected 400", retryable=False)
        sent.append(number)
        return {"ok": True}

    monkeypatch.setattr(notify, "send_text", fake_send)
    for n in ("1000", "2000", "3000"):
        store.wa_queue(f"t-outbox:{n}", "test", n, "hello")
    await notify.send_due()
    rows = {r["number"]: r for r in _outbox("t-outbox:")}
    assert rows["3000"]["status"] == "sent" and "3000" in sent
    assert rows["2000"]["status"] == "failed"
    assert rows["1000"]["status"] == "pending" and rows["1000"]["next_at"] > time.time() + 20
    assert not store.wa_queue("t-outbox:3000", "test", "3000", "hello"), "dedupe key"


async def test_reminder_only_for_visits_booked_a_day_ahead(monkeypatch):
    monkeypatch.setattr(notify.config, "WHATSAPP_ENABLED", True)
    now = store.office_now()
    soon = now + timedelta(hours=20)
    day, hhmm = soon.strftime("%Y-%m-%d"), soon.strftime("%H:%M")
    with store._lock:
        db = store._db()
        db.execute("INSERT INTO appointments (date,time,name,phone,reason,call_sid,created_at,wa_phone) VALUES (?,?,?,?,?,?,?,?)",
                   (day, hhmm, "Kim Park", "", "", "CArem1", time.time() - 3 * 86400, "15125550177"))
        db.execute("INSERT INTO appointments (date,time,name,phone,reason,call_sid,created_at,wa_phone) VALUES (?,?,?,?,?,?,?,?)",
                   ((now + timedelta(hours=10)).strftime("%Y-%m-%d"), (now + timedelta(hours=10)).strftime("%H:%M"),
                    "Late Booker", "", "", "CArem2", time.time(), "15125550188"))  # booked only 10 h ahead
        db.commit()
    assert await notify.queue_reminders() == 1
    assert await notify.queue_reminders() == 0, "only once"
    msgs = [m for m in _outbox("reminder:") if m["number"] == "15125550177"]
    assert len(msgs) == 1 and msgs[0]["text"].startswith("Hi Kim, a friendly reminder from Brightside Dental")
    assert not [m for m in _outbox("reminder:") if m["number"] == "15125550188"]
