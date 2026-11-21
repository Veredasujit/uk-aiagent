"""The outbound call API: keys, validation and limits, the queue and dispatcher, Twilio's
callbacks for an API call, retries, voicemail, cancel, do-not-call, and signed webhooks."""
from __future__ import annotations

import hashlib
import hmac
import itertools
import json
import time
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from twilio.request_validator import RequestValidator

from app import config, outbound, prompt, server, store, twilio_api

client = TestClient(server.app)
OFFICE = {"Authorization": "Bearer vdx_live_test_office"}
OTHER = {"X-API-Key": "vdx_live_test_other"}
_n = itertools.count(2000)
_sid = itertools.count(1)


def num() -> str:
    """A fresh valid US number per call, so per-number limits don't leak between tests."""
    return f"+1512555{next(_n):04d}"


def body(**kw):
    return {"to": num(), "consent": True, "purpose": "recall", "patient_name": "Maria Garcia", **kw}


@pytest.fixture(autouse=True)
def open_window(monkeypatch):
    monkeypatch.setattr(outbound, "in_window", lambda ts, zones: True)
    outbound._REQS.clear()


@pytest.fixture
def dialer(monkeypatch):
    placed = []

    async def fake_place(to, job_id, sig, machine_detection=True, on_voicemail="leave_message", time_limit=0):
        placed.append((to, job_id, sig))
        return f"CA{next(_sid):032d}"

    hung = []

    async def fake_hangup(sid, reason=""):
        hung.append(sid)
        return True

    monkeypatch.setattr(twilio_api, "place_call", fake_place)
    monkeypatch.setattr(twilio_api, "hangup", fake_hangup)
    # nothing else queued from other tests may be dialed here
    with store._lock:
        store._db().execute("UPDATE outbound_calls SET status='canceled', finalized=1 WHERE status IN "
                            "('queued','dialing','ringing','in_progress')")
        store._db().commit()
    return placed, hung


def twilio_post(path: str, params: dict):
    url = f"https://us-agent.example.com{path}"
    sig = RequestValidator("auth-test-token").compute_signature(url, params)
    return client.post(path, data=params, headers={"X-Twilio-Signature": sig})


def tag(job_id: str) -> str:
    return f"ob={job_id}&t={twilio_api.sign('outbound', job_id)}"


# ── auth and the error format ────────────────────────────────────────────────
def test_needs_a_valid_key_and_errors_share_one_shape():
    r = client.post("/v1/calls", json=body())
    assert r.status_code == 401 and r.json()["error"]["code"] == "unauthorized"
    assert r.json()["request_id"].startswith("req_") and r.headers["WWW-Authenticate"] == "Bearer"
    assert client.post("/v1/calls", json=body(), headers={"Authorization": "Bearer vdx_live_nope"}).status_code == 401
    assert client.get("/v1/me", headers=OFFICE).json()["key_name"] == "office"
    assert client.get("/v1/me", headers=OTHER).json()["key_name"] == "other"


def test_body_must_be_json():
    r = client.post("/v1/calls", content=b"to=1", headers={**OFFICE, "Content-Type": "application/x-www-form-urlencoded"})
    assert r.status_code == 415
    r = client.post("/v1/calls", content=b"{nope", headers={**OFFICE, "Content-Type": "application/json"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_json"


@pytest.mark.parametrize("to,code", [
    ("+14165550123", "unsupported_country"),     # Toronto
    ("+447700900123", "unsupported_country"),
    ("+18005550123", "unsupported_number"),      # toll-free
    ("+19005550123", "unsupported_number"),      # premium
    ("+15125550150", "invalid_number"),          # fictional 555-01XX
    ("512555012", "invalid_number"),             # 9 digits
    ("+15115550123", "invalid_number"),          # N11 area code
    ("+17175368381", "invalid_number"),          # the agent's own number
    ("call me", "invalid_number"),
])
def test_only_real_us_numbers(to, code):
    r = client.post("/v1/calls", json=body(to=to), headers=OFFICE)
    assert r.status_code == 422 and r.json()["error"] == {**r.json()["error"], "code": code, "field": "to"}


def test_number_formats_are_normalised():
    for raw in ("(512) 555-2368", "512.555.2368", "1 512 555 2368", "+1-512-555-2368"):
        assert outbound.normalize_us(raw) == "+15125552368"


@pytest.mark.parametrize("extra,code,field", [
    ({"consent": False}, "consent_required", "consent"),
    ({"consent": "true"}, "consent_required", "consent"),
    ({"purpose": "sales"}, "invalid_field", "purpose"),
    ({"purpose": "appointment_reminder"}, "missing_field", "context"),
    ({"surprise": 1}, "unknown_field", "surprise"),
    ({"patient_name": "Robert'); DROP TABLE--"}, "invalid_field", "patient_name"),
    ({"callback_url": "http://example.com/hook"}, "invalid_field", "callback_url"),
    ({"callback_url": "https://127.0.0.1/hook"}, "invalid_field", "callback_url"),
    ({"callback_url": "https://localhost/hook"}, "invalid_field", "callback_url"),
    ({"scheduled_at": "2026-11-21T10:00:00"}, "invalid_field", "scheduled_at"),
    ({"max_attempts": 9}, "invalid_field", "max_attempts"),
    ({"max_attempts": True}, "invalid_field", "max_attempts"),
    ({"retry_delay_seconds": 10}, "invalid_field", "retry_delay_seconds"),
    ({"timezone": "Europe/London"}, "invalid_field", "timezone"),
    ({"metadata": {"nested": {"no": 1}}}, "invalid_field", "metadata"),
    ({"on_voicemail": "sing"}, "invalid_field", "on_voicemail"),
])
def test_validation(extra, code, field):
    r = client.post("/v1/calls", json=body(**extra), headers=OFFICE)
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == code and r.json()["error"]["field"] == field


# ── creating calls ───────────────────────────────────────────────────────────
def test_create_then_get_and_other_keys_cannot_see_it():
    r = client.post("/v1/calls", json=body(metadata={"patient_id": "p-77"}, context="Due since June."), headers=OFFICE)
    assert r.status_code == 201, r.text
    c = r.json()
    assert c["id"].startswith("call_") and c["status"] == "queued" and c["attempts"] == 0 and c["max_attempts"] == 3
    assert c["metadata"] == {"patient_id": "p-77"} and c["idempotent_replay"] is False and c["next_attempt_at"]
    assert client.get(f"/v1/calls/{c['id']}", headers=OFFICE).json()["id"] == c["id"]
    assert client.get(f"/v1/calls/{c['id']}", headers=OTHER).status_code == 404
    assert client.post(f"/v1/calls/{c['id']}/cancel", headers=OTHER).status_code == 404
    assert client.get("/v1/calls/call_../../etc", headers=OFFICE).status_code == 404


def test_idempotency_replays_and_conflicts():
    b = body()
    h = {**OFFICE, "Idempotency-Key": "crm-123"}
    first = client.post("/v1/calls", json=b, headers=h)
    again = client.post("/v1/calls", json=b, headers=h)
    assert first.status_code == 201 and again.status_code == 200
    assert again.json()["id"] == first.json()["id"] and again.json()["idempotent_replay"] is True
    clash = client.post("/v1/calls", json={**b, "purpose": "callback"}, headers=h)
    assert clash.status_code == 409 and clash.json()["error"]["code"] == "idempotency_conflict"
    # the same key under another API key is a different request
    assert client.post("/v1/calls", json=body(), headers={**OTHER, "Idempotency-Key": "crm-123"}).status_code == 201


def test_one_active_call_per_number_and_per_number_daily_limit(monkeypatch):
    b = body()
    first = client.post("/v1/calls", json=b, headers=OFFICE).json()
    dup = client.post("/v1/calls", json=b, headers=OFFICE)
    assert dup.status_code == 409 and dup.json()["error"]["call_id"] == first["id"]
    other = client.post("/v1/calls", json=b, headers=OTHER)
    assert other.status_code == 409 and "call_id" not in other.json()["error"], "never leak another key's call id"
    client.post(f"/v1/calls/{first['id']}/cancel", headers=OFFICE)
    monkeypatch.setattr(config, "OUTBOUND_PER_NUMBER_PER_DAY", 2)
    second = client.post("/v1/calls", json=b, headers=OFFICE).json()
    client.post(f"/v1/calls/{second['id']}/cancel", headers=OFFICE)
    third = client.post("/v1/calls", json=b, headers=OFFICE)
    assert third.status_code == 429 and third.json()["error"]["code"] == "number_limit_reached"


def test_rate_limit_and_daily_quota(monkeypatch):
    monkeypatch.setattr(config, "OUTBOUND_REQUESTS_PER_MINUTE", 2)
    client.get("/v1/me", headers=OTHER)
    client.get("/v1/me", headers=OTHER)
    r = client.get("/v1/me", headers=OTHER)
    assert r.status_code == 429 and int(r.headers["Retry-After"]) >= 1
    monkeypatch.setattr(config, "OUTBOUND_REQUESTS_PER_MINUTE", 1000)
    monkeypatch.setattr(config, "OUTBOUND_CALLS_PER_DAY", 0)
    r = client.post("/v1/calls", json=body(), headers=OTHER)
    assert r.status_code == 429 and r.json()["error"]["code"] == "daily_limit_reached"


def test_batch_reports_each_item():
    dup = num()
    r = client.post("/v1/calls/batch", headers=OFFICE, json={"calls": [
        body(to=dup), body(to=dup), body(to="+18005550000"), "not an object", body(idempotency_key="b-1")]})
    assert r.status_code == 200
    j = r.json()
    assert j["summary"] == {"created": 2, "replayed": 0, "failed": 3}
    assert [x["status_code"] for x in j["results"]] == [201, 409, 422, 422, 201]
    assert j["results"][1]["error"]["code"] == "duplicate_active_call"
    assert client.post("/v1/calls/batch", headers=OFFICE, json={"calls": []}).status_code == 422
    big = client.post("/v1/calls/batch", headers=OFFICE, json={"calls": [body()] * (config.OUTBOUND_MAX_BATCH + 1)})
    assert big.status_code == 422 and big.json()["error"]["code"] == "batch_too_large"


def test_list_pages_with_a_cursor_and_filters():
    ids = [client.post("/v1/calls", json=body(), headers=OTHER).json()["id"] for _ in range(3)]
    p1 = client.get("/v1/calls?limit=2&status=queued", headers=OTHER).json()
    assert len(p1["data"]) == 2 and p1["has_more"] and "transcript" not in p1["data"][0]
    p2 = client.get(f"/v1/calls?limit=2&status=queued&cursor={p1['next_cursor']}", headers=OTHER).json()
    seen = [c["id"] for c in p1["data"] + p2["data"]]
    assert set(ids) <= set(seen) and len(seen) == len(set(seen))
    assert client.get("/v1/calls?status=bogus", headers=OTHER).status_code == 422
    assert client.get("/v1/calls?limit=0", headers=OTHER).status_code == 422


# ── do not call ──────────────────────────────────────────────────────────────
def test_dnc_blocks_new_calls_and_cancels_queued_ones():
    n = num()
    queued = client.post("/v1/calls", json=body(to=n), headers=OFFICE).json()
    assert client.post("/v1/dnc", json={"number": n, "reason": "asked by phone"}, headers=OFFICE).status_code == 201
    assert client.get(f"/v1/calls/{queued['id']}", headers=OFFICE).json()["status"] == "canceled"
    r = client.post("/v1/calls", json=body(to=n), headers=OFFICE)
    assert r.status_code == 409 and r.json()["error"]["code"] == "number_opted_out"
    assert client.get(f"/v1/dnc/{n}", headers=OFFICE).json()["do_not_call"] is True
    assert client.delete(f"/v1/dnc/{n}", headers=OFFICE).status_code == 200
    assert client.delete(f"/v1/dnc/{n}", headers=OFFICE).status_code == 404


# ── the calling window ───────────────────────────────────────────────────────
def test_calling_window_is_8_to_9_local_and_conservative_when_unknown(monkeypatch):
    monkeypatch.undo()  # the real in_window
    at = lambda s: datetime.fromisoformat(s).timestamp()
    chi = ["America/Chicago"]
    assert outbound.in_window(at("2026-11-20T10:00:00-06:00"), chi)
    assert not outbound.in_window(at("2026-11-20T07:59:00-06:00"), chi)
    assert not outbound.in_window(at("2026-11-20T21:00:00-06:00"), chi)
    # unknown zone: 9 am Eastern is 6 am Pacific, so not yet
    assert not outbound.in_window(at("2026-11-20T09:00:00-05:00"), outbound.zones_for("+15125550000"))
    assert outbound.in_window(at("2026-11-20T12:00:00-05:00"), outbound.zones_for("+15125550000"))
    assert outbound.zones_for("+18085550000") == ["Pacific/Honolulu"]
    nxt = outbound.next_window(at("2026-11-20T22:30:00-06:00"), chi)
    assert datetime.fromtimestamp(nxt, outbound.ZoneInfo("America/Chicago")).strftime("%d %H:%M") == "21 08:00"


# ── a call's whole life ──────────────────────────────────────────────────────
async def test_dispatch_answer_report_and_signed_webhook(dialer, monkeypatch):
    placed, _ = dialer
    monkeypatch.setattr(outbound, "check_callback_url", lambda url: None)
    c = client.post("/v1/calls", headers=OFFICE, json=body(callback_url="https://crm.example.com/hook",
                                                           purpose="appointment_reminder",
                                                           context="Cleaning on Tuesday Nov 24 at 10:30 AM.")).json()
    assert await outbound.tick() == 1
    to, job_id, sig = placed[0]
    assert job_id == c["id"] and to == c["to"]
    sid = outbound._get(job_id)["call_sid"]
    assert client.get(f"/v1/calls/{job_id}", headers=OFFICE).json()["status"] == "dialing"
    twilio_post(f"/twilio/status?{tag(job_id)}", {"CallSid": sid, "CallStatus": "ringing"})
    assert outbound._get(job_id)["status"] == "ringing"

    # forged job token: hung up, no agent
    bad = twilio_post(f"/twilio/voice?ob={job_id}&t=forged", {"CallSid": sid, "From": "+17175368381", "To": to,
                                                              "Direction": "outbound-api"})
    assert "<Stream" not in bad.text and "<Hangup" in bad.text
    # a person answers: the agent streams, told which job it is
    ok = twilio_post(f"/twilio/voice?{tag(job_id)}", {"CallSid": sid, "From": "+17175368381", "To": to,
                                                      "Direction": "outbound-api", "AnsweredBy": "human"})
    assert "<Stream" in ok.text and f'value="{job_id}"' in ok.text
    assert outbound._get(job_id)["status"] == "in_progress"
    cfg = server._session_cfg(sid, "phone", "x", "outbound", outbound.stream_cfg(sid))
    assert cfg["agent"]["greeting"] == "Hi, this is Ava, an AI assistant calling from Brightside Dental. Am I speaking with Maria?"
    sp = prompt.build_system_prompt(cfg)
    assert "THIS IS A CALL YOU MADE" in sp and "Cleaning on Tuesday Nov 24" in sp and "<<<NOTES" in sp

    twilio_post(f"/twilio/status?{tag(job_id)}", {"CallSid": sid, "CallStatus": "completed", "CallDuration": "42"})
    assert outbound._get(job_id)["status"] == "in_progress", "waits for the conversation's report"
    outbound.on_report(sid, {"outcome": "appointment_kept", "summary": "Confirmed she'll come.", "seconds": 41.5,
                             "transcript": [{"who": "caller", "text": "Yes I'll be there"}]})
    done = client.get(f"/v1/calls/{job_id}", headers=OFFICE).json()
    assert done["status"] == "completed" and done["outcome"] == "appointment_kept" and done["duration_seconds"] == 41.5
    assert done["transcript"][0]["text"] == "Yes I'll be there" and done["completed_at"]

    with store._lock:
        hook = dict(store._db().execute("SELECT * FROM webhook_outbox WHERE dedupe=?", (f"{job_id}:final",)).fetchone())
    event = json.loads(hook["body"])
    assert event["type"] == "call.completed" and event["data"]["id"] == job_id
    sent = []

    async def public_host(url):
        return True

    class FakeClient:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        async def post(self, url, content, headers):
            sent.append((url, content, headers))

            class R:
                status_code = 200
            return R()

    monkeypatch.setattr(outbound, "_public_host", public_host)
    monkeypatch.setattr(outbound.httpx, "AsyncClient", FakeClient)
    assert await outbound.send_webhooks() >= 1
    url, content, headers = next(s for s in sent if json.loads(s[1])["data"]["id"] == job_id)
    t, v1 = (p.split("=", 1)[1] for p in headers["X-Vedronix-Signature"].split(","))
    assert hmac.compare_digest(v1, hmac.new(b"whsec-test", f"{t}.{content.decode()}".encode(), hashlib.sha256).hexdigest())


async def test_busy_retries_then_gives_up(dialer):
    placed, _ = dialer
    c = client.post("/v1/calls", headers=OFFICE, json=body(max_attempts=2, retry_delay_seconds=300)).json()
    await outbound.tick()
    sid = outbound._get(c["id"])["call_sid"]
    twilio_post(f"/twilio/status?{tag(c['id'])}", {"CallSid": sid, "CallStatus": "busy"})
    j = client.get(f"/v1/calls/{c['id']}", headers=OFFICE).json()
    assert j["status"] == "queued" and j["attempts"] == 1 and j["error"]["code"] == "busy"
    assert datetime.fromisoformat(j["next_attempt_at"].replace("Z", "+00:00")).timestamp() >= time.time() + 290
    # a late callback from the first attempt changes nothing
    twilio_post(f"/twilio/status?{tag(c['id'])}", {"CallSid": sid, "CallStatus": "completed"})
    assert outbound._get(c["id"])["status"] == "queued"
    outbound._update(c["id"], next_at=time.time() - 1)
    await outbound.tick()
    sid2 = outbound._get(c["id"])["call_sid"]
    assert sid2 != sid and len(placed) == 2
    twilio_post(f"/twilio/status?{tag(c['id'])}", {"CallSid": sid2, "CallStatus": "no-answer"})
    j = client.get(f"/v1/calls/{c['id']}", headers=OFFICE).json()
    assert j["status"] == "no_answer" and j["attempts"] == 2 and j["completed_at"]


async def test_voicemail_leaves_a_message_without_details(dialer):
    c = client.post("/v1/calls", headers=OFFICE, json=body()).json()
    await outbound.tick()
    sid = outbound._get(c["id"])["call_sid"]
    r = twilio_post(f"/twilio/voice?{tag(c['id'])}", {"CallSid": sid, "From": "+17175368381", "To": c["to"],
                                                       "Direction": "outbound-api", "AnsweredBy": "machine_end_beep"})
    assert "<Stream" not in r.text and "<Say" in r.text and "AI assistant" in r.text and "Maria" not in r.text
    j = client.get(f"/v1/calls/{c['id']}", headers=OFFICE).json()
    assert j["status"] == "voicemail" and j["answered_by"] == "machine_end_beep" and j["outcome"] == "voicemail_left"


async def test_capacity_leaves_lines_for_inbound(dialer, monkeypatch):
    placed, _ = dialer
    monkeypatch.setattr(config, "OUTBOUND_INBOUND_RESERVE", 1)   # MAX_CONCURRENT_CALLS is 2 in tests
    for _ in range(3):
        client.post("/v1/calls", headers=OFFICE, json=body())
    assert await outbound.tick() == 1 and len(placed) == 1
    assert await outbound.tick() == 0, "the other line stays free for callers"
    monkeypatch.setattr(outbound, "_live_count", lambda: 1)
    monkeypatch.setattr(config, "OUTBOUND_INBOUND_RESERVE", 0)
    assert outbound.capacity() == 0


async def test_cancel_queued_and_live_calls(dialer):
    _, hung = dialer
    q = client.post("/v1/calls", headers=OFFICE, json=body(scheduled_at="2030-01-01T00:00:00Z"))
    assert q.status_code == 422  # more than 30 days ahead
    q = client.post("/v1/calls", headers=OFFICE, json=body()).json()
    live = client.post("/v1/calls", headers=OFFICE, json=body()).json()
    r = client.post(f"/v1/calls/{q['id']}/cancel", headers=OFFICE)
    assert r.status_code == 200 and r.json()["status"] == "canceled"
    assert client.post(f"/v1/calls/{q['id']}/cancel", headers=OFFICE).status_code == 409
    await outbound.tick()
    sid = outbound._get(live["id"])["call_sid"]
    assert client.post(f"/v1/calls/{live['id']}/cancel", headers=OFFICE).json()["status"] == "canceled"
    assert hung == [sid]
    twilio_post(f"/twilio/status?{tag(live['id'])}", {"CallSid": sid, "CallStatus": "canceled"})
    assert outbound._get(live["id"])["finalized"] == 1


async def test_twilio_refusal_fails_hiccup_retries(dialer, monkeypatch):
    errors = [twilio_api.TwilioError("Twilio 400: invalid To (code 21211)", retryable=False),
              twilio_api.TwilioError("Twilio 503", retryable=True)]

    async def failing(*a, **k):
        raise errors.pop(0)

    monkeypatch.setattr(twilio_api, "place_call", failing)
    a = client.post("/v1/calls", headers=OFFICE, json=body()).json()
    await outbound.tick()
    assert outbound._get(a["id"])["status"] == "failed"
    b = client.post("/v1/calls", headers=OFFICE, json=body()).json()
    await outbound.tick()
    assert outbound._get(b["id"])["status"] == "queued" and outbound._get(b["id"])["attempts"] == 1


async def test_spoken_opt_out_goes_on_the_dnc_list(dialer):
    c = client.post("/v1/calls", headers=OFFICE, json=body()).json()
    await outbound.tick()
    sid = outbound._get(c["id"])["call_sid"]
    twilio_post(f"/twilio/voice?{tag(c['id'])}", {"CallSid": sid, "From": "+17175368381", "To": c["to"],
                                                  "Direction": "outbound-api"})
    outbound.on_report(sid, {"outcome": "not_interested", "transcript": [
        {"who": "agent", "text": "Hi, is this Maria?"}, {"who": "caller", "text": "Please stop calling me."}]})
    assert outbound.is_dnc(c["to"])


def test_expired_and_stuck_calls_are_swept(dialer):
    c = client.post("/v1/calls", headers=OFFICE, json=body(max_attempts=1)).json()
    outbound._update(c["id"], expires_at=time.time() - 1)
    outbound.sweep()
    assert outbound._get(c["id"])["status"] == "expired"
    d = client.post("/v1/calls", headers=OFFICE, json=body(max_attempts=1)).json()
    outbound._update(d["id"], status="ringing", attempts=1)
    outbound.sweep(time.time() + config.OUTBOUND_RING_TIMEOUT_S + 200)
    assert outbound._get(d["id"])["status"] == "failed"


def test_key_tool_prints_a_key_whose_hash_matches(capsys):
    assert outbound.main(["new", "crm"]) == 0
    out = capsys.readouterr().out
    key = out.split("shown once):")[1].split()[0]
    assert key.startswith("vdx_live_") and f"crm:{outbound.hash_key(key)}" in out


def test_health_shows_outbound_counts():
    h = client.get("/health").json()
    assert h["outbound"]["keys"] == 2 and "calls" in h["outbound"]


def test_console_on_our_site_can_send_the_key():
    r = client.options("/v1/calls", headers={"Origin": "https://vedronix.com", "Access-Control-Request-Method": "POST",
                                             "Access-Control-Request-Headers": "authorization,content-type"})
    assert r.status_code == 200 and "authorization" in r.headers["access-control-allow-headers"].lower()
    r = client.options("/v1/calls", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST",
                                             "Access-Control-Request-Headers": "authorization"})
    assert r.headers.get("access-control-allow-origin") != "https://evil.example"
