"""analysis.py — one summary request per finished call.

The backend stores leads and conversations; this decides what they say. It reads only
the transcript and the function results, and must not invent anything the caller didn't
say (owner's rule: "Do not invent customer information that was not provided").
"""
from __future__ import annotations

import json

import httpx

from . import config

OUTCOMES = ["interested", "appointment_booked", "information_given", "callback_requested",
            "human_requested", "not_interested", "no_conversation", "unknown"]

INSTRUCTIONS = """You review a finished phone call between a business's AI voice agent and a caller.
Return JSON only:
{"summary": "2-3 plain English sentences on what the caller wanted and what happened",
 "outcome": one of OUTCOMES,
 "lead": {"name": "", "phone": "", "email": "", "requirements": ""} or null}
Rules: use ONLY what the caller actually said or what a function result shows. Leave a field
empty if it wasn't said. lead is null if the caller gave no reason to follow up.""".replace(
    "OUTCOMES", json.dumps(OUTCOMES))


async def summarise(cfg: dict, transcript: list[dict], functions: list[dict]) -> dict:
    agent = cfg.get("agent", {})
    convo = "\n".join(f"{'Agent' if t['who'] == 'agent' else 'Caller'}: {t['text']}" for t in transcript)[-12000:]
    fn = json.dumps([{k: f.get(k) for k in ("name", "args", "result")} for f in functions], ensure_ascii=False)[:3000]
    body = {
        "model": config.SUMMARY_MODEL,
        "response_format": {"type": "json_object"},
        "temperature": 0,
        "messages": [
            {"role": "system", "content": INSTRUCTIONS},
            {"role": "user", "content": f"Agent: {agent.get('name')} ({agent.get('industry') or 'general'})\n\n"
                                        f"Transcript:\n{convo}\n\nFunction calls:\n{fn}"},
        ],
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(f"{config.OPENAI_BASE_URL}/chat/completions", json=body,
                                 headers={"Authorization": f"Bearer {config.OPENAI_API_KEY}"})
    resp.raise_for_status()
    data = json.loads(resp.json()["choices"][0]["message"]["content"])
    outcome = data.get("outcome") if data.get("outcome") in OUTCOMES else "unknown"
    lead = data.get("lead") if isinstance(data.get("lead"), dict) else None
    if lead:
        lead = {k: str(v).strip()[:300] for k, v in lead.items() if k in ("name", "phone", "email", "requirements") and str(v or "").strip()}
    return {"summary": str(data.get("summary") or "")[:1200], "outcome": outcome, "lead": lead or None}
