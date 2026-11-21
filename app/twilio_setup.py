"""
twilio_setup.py — one-off (and safe to re-run) Twilio configuration for the US agent.

    python -m app.twilio_setup /path/to/.env.production

Reads TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN / PUBLIC_HOST / TWILIO_PHONE_NUMBER from that file
and makes Twilio match this agent, touching only what belongs to it:

1. An API key ("Vedronix US agent") for signing browser tokens and REST hang-ups, so the
   account's auth token isn't used for those. Created once; its secret is written back.
2. A TwiML App ("Vedronix US agent (browser calls)") whose Voice URL is /twilio/voice:
   the only place browser (Voice JS SDK) calls can go. Created once, URLs kept current.
3. The phone number's Voice URL, fallback and status callback → this agent. Messaging and
   every other number are left alone. Nothing is deleted.
"""
from __future__ import annotations

import sys

from twilio.rest import Client


def read_env(path: str) -> dict[str, str]:
    out = {}
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.split(" #", 1)[0].strip()
    return out


def write_env(path: str, updates: dict[str, str]) -> None:
    lines = open(path, encoding="utf-8").read().splitlines()
    seen = set()
    for i, line in enumerate(lines):
        k = line.split("=", 1)[0].strip()
        if k in updates:
            lines[i] = f"{k}={updates[k]}"
            seen.add(k)
    lines += [f"{k}={v}" for k, v in updates.items() if k not in seen]
    open(path, "w", encoding="utf-8").write("\n".join(lines) + "\n")


def main(path: str) -> None:
    env = read_env(path)
    sid, token, host = env["TWILIO_ACCOUNT_SID"], env["TWILIO_AUTH_TOKEN"], env.get("PUBLIC_HOST", "us-agent.vedronix.com")
    number = env.get("TWILIO_PHONE_NUMBER", "+17175368381")
    client = Client(sid, token)
    base = f"https://{host}"
    updates: dict[str, str] = {}

    # 1. API key
    if not env.get("TWILIO_API_KEY_SID") or not env.get("TWILIO_API_KEY_SECRET"):
        key = client.new_keys.create(friendly_name="Vedronix US agent")
        updates["TWILIO_API_KEY_SID"] = key.sid
        updates["TWILIO_API_KEY_SECRET"] = key.secret
        print(f"api key created: {key.sid}")
    else:
        print(f"api key kept: {env['TWILIO_API_KEY_SID']}")

    # 2. TwiML App for browser calls
    app_kwargs = dict(friendly_name="Vedronix US agent (browser calls)", voice_url=f"{base}/twilio/voice", voice_method="POST",
                      voice_fallback_url=f"{base}/twilio/fallback", voice_fallback_method="POST",
                      status_callback=f"{base}/twilio/status", status_callback_method="POST")
    app_sid = env.get("TWILIO_TWIML_APP_SID")
    if app_sid:
        client.applications(app_sid).update(**app_kwargs)
        print(f"twiml app updated: {app_sid}")
    else:
        app = client.applications.create(**app_kwargs)
        updates["TWILIO_TWIML_APP_SID"] = app.sid
        print(f"twiml app created: {app.sid}")

    # 3. The phone number → this agent (inbound voice only)
    nums = client.incoming_phone_numbers.list(phone_number=number, limit=1)
    if not nums:
        sys.exit(f"{number} is not in this account")
    n = nums[0]
    before = n.voice_url
    client.incoming_phone_numbers(n.sid).update(
        voice_url=f"{base}/twilio/voice", voice_method="POST",
        voice_fallback_url=f"{base}/twilio/fallback", voice_fallback_method="POST",
        status_callback=f"{base}/twilio/status", status_callback_method="POST",
        voice_application_sid="",
    )
    print(f"number {number} ({n.sid}): voice url {before or '-'} -> {base}/twilio/voice")

    if updates:
        write_env(path, updates)
        print(f"wrote {', '.join(sorted(updates))} to {path}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else ".env.production")
