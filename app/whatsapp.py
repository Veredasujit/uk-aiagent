"""
whatsapp.py — the Vedronix WhatsApp gateway (Evolution API underneath), the same one the
Sakshi agent uses:

    POST {WHATSAPP_API_URL}   X-API-Key: ...   {"number": "15125550100", "text": "..."}

ASCII: on the Anya build the gateway rejected non-ASCII characters in `text`, so everything is
sanitised (WHATSAPP_ASCII_ONLY). The templates in notify.py are plain ASCII already; this is a
guard, not a translator.

Nothing here retries. Retrying is the outbox's job (notify.py), because only the outbox
survives a restart.
"""
from __future__ import annotations

import re
from typing import Optional

import httpx

from . import config
from .logging_setup import info

_ASCII_MAP = {
    "‘": "'", "’": "'", "“": '"', "”": '"',
    "–": "-", "—": "-", "…": "...", " ": " ", "•": "-",
}


class WhatsAppError(RuntimeError):
    def __init__(self, message: str, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


def ascii_safe(text: str) -> str:
    if not text:
        return ""
    for bad, good in _ASCII_MAP.items():
        text = text.replace(bad, good)
    text = text.encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


def wa_number(number: str) -> str:
    """Twilio gives E.164 ('+15125550100'); the gateway wants digits with the country code.
    Anything that isn't a full international number is refused (no guessing a country)."""
    raw = (number or "").strip()
    digits = "".join(ch for ch in raw if ch.isdigit())
    if not raw.startswith("+") or not 10 <= len(digits) <= 15:
        return ""
    return digits


_client: Optional[httpx.AsyncClient] = None


def _http() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=config.WHATSAPP_TIMEOUT_S)
    return _client


async def close() -> None:
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


async def send_text(number: str, text: str) -> dict:
    if not config.WHATSAPP_API_KEY:
        raise WhatsAppError("WHATSAPP_API_KEY not configured", retryable=False)
    body = ascii_safe(text) if config.WHATSAPP_ASCII_ONLY else (text or "").strip()
    if not number or not body:
        raise WhatsAppError("no number or empty message", retryable=False)
    headers = {"X-API-Key": config.WHATSAPP_API_KEY, "Content-Type": "application/json"}
    try:
        resp = await _http().post(config.WHATSAPP_API_URL, json={"number": number, "text": body}, headers=headers)
    except httpx.HTTPError as exc:
        raise WhatsAppError(f"gateway unreachable: {exc}") from exc

    snippet = resp.text[:300]
    info("whatsapp", f"{resp.status_code} -> ...{number[-4:]}: {snippet}")
    if resp.status_code in (401, 403):
        raise WhatsAppError(f"auth rejected ({resp.status_code}): {snippet}", retryable=False)
    if resp.status_code == 429 or resp.status_code >= 500:
        raise WhatsAppError(f"gateway {resp.status_code}: {snippet}")
    if resp.status_code == 409 or "instance_not_connected" in snippet:
        # The gateway's phone is logged out (needs a QR re-scan); the message is fine.
        raise WhatsAppError(f"gateway not connected ({resp.status_code}): {snippet}")
    if resp.status_code >= 400:
        # Bad number (e.g. not on WhatsApp): sending again fails the same way.
        raise WhatsAppError(f"rejected {resp.status_code}: {snippet}", retryable=False)
    try:
        data = resp.json()
    except ValueError:
        return {"raw": resp.text}
    if isinstance(data, dict):
        status = str(data.get("status", "")).lower()
        if data.get("ok") is False or data.get("success") is False or status in ("error", "failed", "failure"):
            raise WhatsAppError(f"gateway reported failure: {snippet}")
    return data if isinstance(data, dict) else {"data": data}
