"""tools.py — the functions a Vedronix agent may call during a test call.

Only functions the visitor enabled are offered to the model (schema_for); a function
the model asks for that isn't enabled is refused here as well, so no prompt can reach
anything else. Each returns a small dict the model reads back; failures come back as
{"ok": false, "say": ...} so a broken backend becomes an honest sentence, not silence.

  key (catalog)        function the model sees     where it runs
  collect_info         save_customer_info           here (kept for the call report)
  capture_lead         capture_lead                 backend (stored, synced, CRM)
  check_availability   check_availability           backend (the agent's calendar)
  book_appointment     book_appointment             backend
  answer_faq           lookup_faq                   here (the visitor's FAQ text)
  transfer_call        request_human                backend (WhatsApps the agent's owner)
  send_to_crm          send_to_crm                  backend (CRM webhook)
  (always)             end_call                     here
"""
from __future__ import annotations

import re
import time
from typing import Any, Optional

from . import backend
from .logging_setup import CallLog, error, info


def _fn(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {"type": "function", "function": {"name": name, "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required}}}


CATALOG: dict[str, dict] = {
    "collect_info": _fn("save_customer_info", "Save details the caller has told you. Only what they actually said.", {
        "name": {"type": "string"}, "phone": {"type": "string"}, "email": {"type": "string"},
        "requirements": {"type": "string", "description": "What the caller needs, in one line"},
    }, []),
    "capture_lead": _fn("capture_lead", "Record the caller as a lead for the business.", {
        "name": {"type": "string"}, "phone": {"type": "string"}, "email": {"type": "string"},
        "requirements": {"type": "string", "description": "What they want, in one line"},
        "interest": {"type": "string", "enum": ["hot", "warm", "cold"]},
    }, ["requirements"]),
    "check_availability": _fn("check_availability", "Free appointment slots on a date.", {
        "date": {"type": "string", "description": "YYYY-MM-DD"},
    }, ["date"]),
    "book_appointment": _fn("book_appointment", "Book a confirmed appointment slot.", {
        "date": {"type": "string", "description": "YYYY-MM-DD"}, "time": {"type": "string", "description": "24-hour HH:MM"},
        "name": {"type": "string"}, "phone": {"type": "string"}, "purpose": {"type": "string"},
    }, ["date", "time", "name"]),
    "answer_faq": _fn("lookup_faq", "Look up the business's FAQ for a question.", {
        "question": {"type": "string"},
    }, ["question"]),
    "transfer_call": _fn("request_human", "The caller wants a human or you can't help. The team will call them back.", {
        "reason": {"type": "string"},
    }, ["reason"]),
    "send_to_crm": _fn("send_to_crm", "Send the confirmed lead details to the business's CRM.", {
        "name": {"type": "string"}, "phone": {"type": "string"}, "email": {"type": "string"},
        "requirements": {"type": "string"},
    }, ["requirements"]),
}
END_CALL = _fn("end_call", "Hang up. Say the goodbye in the same turn first.", {"reason": {"type": "string"}}, [])

NAME_TO_KEY = {spec["function"]["name"]: key for key, spec in CATALOG.items()}
BACKEND_FUNCTIONS = {"capture_lead", "check_availability", "book_appointment", "request_human", "send_to_crm"}
HOLDING_FUNCTIONS = {"check_availability", "book_appointment", "send_to_crm"}


def schema_for(enabled: list[str]) -> list[dict]:
    return [CATALOG[k] for k in enabled if k in CATALOG] + [END_CALL]


def _faq_answer(faq: str, question: str) -> list[str]:
    """Up to three FAQ paragraphs sharing the most words with the question."""
    blocks = [b.strip() for b in re.split(r"\n\s*\n|\n(?=Q[:.])", faq or "") if b.strip()]
    words = {w for w in re.findall(r"[\wऀ-ॿ]{3,}", question.lower())}
    scored = sorted(((len(words & set(re.findall(r"[\wऀ-ॿ]{3,}", b.lower()))), b) for b in blocks), reverse=True)
    return [b[:600] for score, b in scored[:3] if score > 0]


class ToolRunner:
    def __init__(self, call_log: CallLog, token: str, cfg: dict) -> None:
        self.call_log = call_log
        self.token = token
        agent = cfg.get("agent", {})
        self.enabled: list[str] = [f for f in (agent.get("functions") or []) if f in CATALOG]
        self.faq: str = agent.get("faq") or ""
        self.hangup_requested = False
        self.hangup_reason = ""
        self.customer: dict[str, str] = {}
        self.calls: list[dict] = []  # every function call, for the report

    @property
    def schema(self) -> list[dict]:
        return schema_for(self.enabled)

    async def run(self, name: str, args: dict) -> dict:
        started = time.monotonic()
        try:
            result = await self._run(name, args or {})
        except Exception as exc:  # never let a function crash the call
            error("tool", f"{name} failed: {exc}")
            result = {"ok": False, "say": "That didn't work just now."}
        ms = round((time.monotonic() - started) * 1000, 1)
        self.call_log.event("tool_result", name=name, ms=ms, result=result)
        self.calls.append({"name": name, "args": args, "ok": bool(result.get("ok", True)), "result": result,
                           "at": time.time()})
        info("tool", f"{name}({args}) -> {result}")
        return result

    async def _run(self, name: str, args: dict) -> dict:
        if name == "end_call":
            self.hangup_requested = True
            self.hangup_reason = str(args.get("reason") or "agent")[:60]
            return {"ok": True}
        key = NAME_TO_KEY.get(name)
        if not key or key not in self.enabled:
            return {"ok": False, "say": "I can't do that on this call."}
        clean = {k: str(v)[:300] for k, v in args.items() if isinstance(v, (str, int, float)) and str(v).strip()}
        if name in ("save_customer_info", "capture_lead", "send_to_crm"):
            for k in ("name", "phone", "email", "requirements"):
                if clean.get(k):
                    self.customer[k] = clean[k]
        if name == "save_customer_info":
            return {"ok": True, "saved": sorted(self.customer)}
        if name == "lookup_faq":
            hits = _faq_answer(self.faq, clean.get("question", ""))
            return {"ok": True, "answers": hits} if hits else {"ok": False, "say": "That isn't in the FAQ."}
        if name in BACKEND_FUNCTIONS:
            try:
                return await backend.tool(self.token, name, {**self.customer, **clean})
            except backend.BackendError as exc:
                error("tool", f"{name} via backend: {exc}")
                return {"ok": False, "say": "The system didn't respond; the team will follow up."}
        return {"ok": False}

    def lead(self) -> Optional[dict[str, Any]]:
        return dict(self.customer) if self.customer else None
