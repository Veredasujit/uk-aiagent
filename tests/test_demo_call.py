"""'Get a call from Ava' on vedronix.com: a texted code must come back before any call."""
from __future__ import annotations

import itertools

import pytest
from fastapi.testclient import TestClient

from app import config, demo_call, outbound, prompt, server, store

client = TestClient(server.app)
WEB = {"Origin": "https://vedronix.com"}
_n = itertools.count(7000)


def num() -> str:
    return f"+1737555{next(_n):04d}"


@pytest.fixture(autouse=True)
def verify(monkeypatch):
    monkeypatch.setattr(config, "DEMO_CALLS_ENABLED", True)
    monkeypatch.setattr(config, "TWILIO_VERIFY_SERVICE_SID", "VA00000000000000000000000000000000")
    monkeypatch.setattr(outbound, "in_window", lambda ts, zones: True)
    demo_call._CODES.clear()
    demo_call._SENT.clear()
    sent = []

    async def fake(path, data):
        sent.append((path, dict(data)))
        if path == "VerificationCheck":
            return {"status": "approved" if data["Code"] == "123456" else "pending"}
        return {"status": "pending"}

    monkeypatch.setattr(demo_call, "_verify", fake)
    return sent


def test_only_our_site_and_needs_consent(verify):
    to = num()
    assert client.post("/web/call-me/start", json={"to": to, "consent": True}).status_code == 403
    assert client.post("/web/call-me/start", json={"to": to, "consent": True},
                       headers={"Origin": "https://evil.example"}).status_code == 403
    r = client.post("/web/call-me/start", json={"to": to}, headers=WEB)
    assert r.status_code == 422 and r.json()["error"]["code"] == "consent_required"
    r = client.post("/web/call-me/start", json={"to": "+14165550123", "consent": True}, headers=WEB)
    assert r.status_code == 422 and r.json()["error"]["code"] == "unsupported_country"
    assert verify == []


def test_code_then_call(verify):
    to = num()
    r = client.post("/web/call-me/start", json={"to": to, "name": "Sam", "consent": True}, headers=WEB)
    assert r.status_code == 200 and verify[-1] == ("Verifications", {"To": to, "Channel": "sms"})
    assert r.headers.get("access-control-allow-origin") == "https://vedronix.com"
    bad = client.post("/web/call-me/verify", json={"to": to, "code": "000000", "name": "Sam"}, headers=WEB)
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "invalid_code"
    with store._lock:
        assert store._db().execute("SELECT COUNT(*) FROM outbound_calls WHERE to_number=?", (to,)).fetchone()[0] == 0
    ok = client.post("/web/call-me/verify", json={"to": to, "code": "123456", "name": "Sam"}, headers=WEB)
    assert ok.status_code == 201, ok.text
    job = outbound._get(ok.json()["call_id"])
    assert job["api_key"] == "web-demo" and job["purpose"] == "demo" and job["max_attempts"] == 1
    st = client.get(ok.json()["status_url"].split("us-agent.example.com")[1])
    assert st.status_code == 200 and st.json()["status"] == "queued"
    assert client.get(f"/web/call-me/status?id={job['id']}&key=forged").status_code == 404
    # the API's own keys never see demo calls, and can't ask for purpose "demo"
    assert client.get(f"/v1/calls/{job['id']}", headers={"Authorization": "Bearer vdx_live_test_office"}).status_code == 404
    r = client.post("/v1/calls", headers={"Authorization": "Bearer vdx_live_test_office"},
                    json={"to": num(), "consent": True, "purpose": "demo"})
    assert r.status_code == 422
    cfg = server._session_cfg("CAx", "phone", "x", "outbound", job)
    assert cfg["agent"]["greeting"].startswith("Hi Sam! It's Ava") and "demo you asked for" in cfg["agent"]["greeting"]
    assert cfg["ends_at"] - __import__("time").time() <= config.MAX_WEB_CALL_SECONDS + 1
    sp = prompt.build_system_prompt(cfg)
    assert "verified this phone number themselves" in sp and "Make sure you're speaking" not in sp


def test_abuse_limits(verify, monkeypatch):
    to = num()
    assert client.post("/web/call-me/start", json={"to": to, "consent": True}, headers=WEB).status_code == 200
    again = client.post("/web/call-me/start", json={"to": to, "consent": True}, headers=WEB)
    assert again.status_code == 429, "30 s between codes to one number"
    client.post("/web/call-me/start", json={"to": num(), "consent": True}, headers=WEB)
    client.post("/web/call-me/start", json={"to": num(), "consent": True}, headers=WEB)
    r = client.post("/web/call-me/start", json={"to": num(), "consent": True}, headers=WEB)
    assert r.status_code == 429 and "hour" in r.json()["error"]["message"], "3 codes an hour per visitor"
    demo_call._CODES.clear()
    monkeypatch.setattr(config, "DEMO_CALLS_PER_DAY", 0)
    r = client.post("/web/call-me/start", json={"to": num(), "consent": True}, headers=WEB)
    assert r.status_code == 429 and r.json()["error"]["code"] == "daily_limit_reached"


def test_opted_out_and_outside_hours(verify, monkeypatch):
    to = num()
    outbound.add_dnc(to, "test", "test")
    r = client.post("/web/call-me/start", json={"to": to, "consent": True}, headers=WEB)
    assert r.status_code == 409 and r.json()["error"]["code"] == "number_opted_out"
    late = num()
    monkeypatch.setattr(outbound, "in_window", lambda ts, zones: False)
    r = client.post("/web/call-me/verify", json={"to": late, "code": "123456"}, headers=WEB)
    assert r.status_code == 422 and r.json()["error"]["code"] == "outside_calling_window"


def test_off_without_a_verify_service(monkeypatch):
    monkeypatch.setattr(config, "TWILIO_VERIFY_SERVICE_SID", "")
    r = client.post("/web/call-me/start", json={"to": num(), "consent": True}, headers=WEB)
    assert r.status_code == 503
