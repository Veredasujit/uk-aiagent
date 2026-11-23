
"""
whatsapp.py — Vedronix UK WhatsApp gateway (Evolution API underneath).

Gateway:
    POST {WHATSAPP_API_URL}
    X-API-Key: ...
    {
        "number": "447911123456",
        "text": "Your appointment is confirmed."
    }

UK numbers:
    +447911123456 -> 447911123456
    00447911123456 -> accepted only after conversion to E.164
    07911123456 -> rejected (no country guessing)

International E.164 numbers are supported.

ASCII:
    Some gateway deployments reject non-ASCII characters.
    WHATSAPP_ASCII_ONLY controls message sanitisation.

Retrying is handled by notify.py through the persistent outbox.
This module does not retry independently.
"""

from __future__ import annotations

import re
from typing import Optional

import httpx

from . import config
from .logging_setup import info


_ASCII_MAP = {
    "‘": "'",
    "’": "'",
    "“": '"',
    "”": '"',
    "–": "-",
    "—": "-",
    "…": "...",
    " ": " ",
    "•": "-",
    "£": "GBP ",
    "€": "EUR ",
}


class WhatsAppError(RuntimeError):

    def __init__(
        self,
        message: str,
        retryable: bool = True,
    ) -> None:
        super().__init__(message)
        self.retryable = retryable


def ascii_safe(text: str) -> str:
    """Convert WhatsApp message text into safe ASCII."""

    if not text:
        return ""

    for bad, good in _ASCII_MAP.items():
        text = text.replace(bad, good)

    text = text.encode("ascii", "ignore").decode("ascii")

    text = re.sub(r"[ \t]{2,}", " ", text)

    return text.strip()


def wa_number(number: str) -> str:
    """
    Normalise international phone numbers for Evolution API.

    UK examples:
        +447911123456 -> 447911123456
        +442079460000 -> 442079460000

    Other international numbers:
        +15125550100 -> 15125550100

    Only explicit international numbers are accepted.
    No country code is guessed.
    """

    raw = (number or "").strip()

    if not raw:
        return ""

    # Accept E.164 numbers only.
    if not raw.startswith("+"):
        return ""

    # Remove common visual separators.
    digits = "".join(ch for ch in raw if ch.isdigit())

    # E.164: maximum 15 digits.
    if not 10 <= len(digits) <= 15:
        return ""

    # UK-specific validation.
    if digits.startswith("44"):

        # UK national number must not start with zero after country code.
        national = digits[2:]

        if not national or national.startswith("0"):
            return ""

    return digits


_client: Optional[httpx.AsyncClient] = None


def _http() -> httpx.AsyncClient:

    global _client

    if _client is None or _client.is_closed:

        _client = httpx.AsyncClient(
            timeout=config.WHATSAPP_TIMEOUT_S
        )

    return _client


async def close() -> None:

    global _client

    if _client is not None and not _client.is_closed:
        await _client.aclose()

    _client = None


async def send_text(number: str, text: str) -> dict:

    if not config.WHATSAPP_API_KEY:
        raise WhatsAppError(
            "WHATSAPP_API_KEY not configured",
            retryable=False,
        )

    # Always normalise and validate recipient.
    recipient = wa_number("+" + number if number.isdigit() else number)

    if not recipient:
        raise WhatsAppError(
            "invalid international WhatsApp number",
            retryable=False,
        )

    body = (
        ascii_safe(text)
        if config.WHATSAPP_ASCII_ONLY
        else (text or "").strip()
    )

    if not body:
        raise WhatsAppError(
            "empty WhatsApp message",
            retryable=False,
        )

    headers = {
        "X-API-Key": config.WHATSAPP_API_KEY,
        "Content-Type": "application/json",
    }

    payload = {
        "number": recipient,
        "text": body,
    }

    try:

        resp = await _http().post(
            config.WHATSAPP_API_URL,
            json=payload,
            headers=headers,
        )

    except httpx.HTTPError as exc:

        raise WhatsAppError(
            f"gateway unreachable: {exc}"
        ) from exc

    snippet = resp.text[:300]

    info(
        "whatsapp",
        f"{resp.status_code} -> ...{recipient[-4:]}: {snippet}",
    )

    # Authentication errors are not retryable.
    if resp.status_code in (401, 403):

        raise WhatsAppError(
            f"auth rejected ({resp.status_code}): {snippet}",
            retryable=False,
        )

    # Rate limiting and server errors can be retried.
    if resp.status_code == 429 or resp.status_code >= 500:

        raise WhatsAppError(
            f"gateway {resp.status_code}: {snippet}"
        )

    # WhatsApp instance disconnected.
    if (
        resp.status_code == 409
        or "instance_not_connected" in snippet
    ):

        raise WhatsAppError(
            f"gateway not connected ({resp.status_code}): {snippet}"
        )

    # Invalid number or rejected message.
    if resp.status_code >= 400:

        raise WhatsAppError(
            f"rejected {resp.status_code}: {snippet}",
            retryable=False,
        )

    try:
        data = resp.json()

    except ValueError:
        return {"raw": resp.text}

    if isinstance(data, dict):

        status = str(data.get("status", "")).lower()

        if (
            data.get("ok") is False
            or data.get("success") is False
            or status in ("error", "failed", "failure")
        ):

            raise WhatsAppError(
                f"gateway reported failure: {snippet}"
            )

        return data

    return {"data": data}
