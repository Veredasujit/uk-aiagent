"""
twilio_bridge.py — makes a Twilio Media Stream look like the call socket the pipeline knows.

The session, playout and barge-in code were tuned on live calls against FreJun/Teler's
message shapes. Rather than touch them, this wraps Twilio's websocket:

  Twilio → us   connected | start{streamSid, callSid, customParameters} | media{payload: μ-law}
                | mark | dtmf | stop
  us → Twilio   media{payload: μ-law} | clear (barge-in: drop everything Twilio has queued)

Inbound μ-law is decoded to 16-bit little-endian PCM for the pipeline; outbound PCM is
encoded back to μ-law. Both at 8 kHz, so nothing is resampled.
"""
from __future__ import annotations

import asyncio
import base64
import json
from collections import deque
from typing import Optional

from fastapi import WebSocket

from .audio import mulaw_to_pcm, pcm_to_mulaw
from .logging_setup import log


class TwilioBridge:
    def __init__(self, ws: WebSocket) -> None:
        self.ws = ws
        self.stream_sid: Optional[str] = None
        self.call_sid: Optional[str] = None
        self.params: dict[str, str] = {}
        self._replay: deque[str] = deque()
        self.closed = False

    async def wait_start(self, timeout: float = 10.0) -> bool:
        """Read until Twilio's `start` message; keep it to replay to the session."""
        async def _read() -> bool:
            while True:
                raw = await self.ws.receive_text()
                msg = json.loads(raw)
                ev = msg.get("event")
                if ev == "start":
                    st = msg.get("start") or {}
                    self.stream_sid = msg.get("streamSid") or st.get("streamSid")
                    self.call_sid = st.get("callSid")
                    self.params = {str(k): str(v) for k, v in (st.get("customParameters") or {}).items()}
                    direction = "outbound" if self.params.get("direction") == "outbound" else "inbound"
                    self._replay.append(json.dumps({"type": "start", "data": {"call_id": self.call_sid, "direction": direction}}))
                    return True
                if ev == "stop":
                    return False
        try:
            return await asyncio.wait_for(_read(), timeout=timeout)
        except Exception as exc:
            log("bridge", f"no start message: {exc}")
            return False

    # ── what the session reads ───────────────────────────────────────────────
    async def receive_text(self) -> str:
        if self._replay:
            return self._replay.popleft()
        while True:
            raw = await self.ws.receive_text()
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            ev = msg.get("event")
            if ev == "media":
                media = msg.get("media") or {}
                if media.get("track", "inbound") != "inbound":
                    continue
                try:
                    pcm = mulaw_to_pcm(base64.b64decode(media.get("payload") or ""))
                except Exception:
                    continue
                if not pcm:
                    continue
                return json.dumps({"type": "audio", "data": {"audio_b64": base64.b64encode(pcm).decode("ascii")}})
            if ev == "stop":
                return json.dumps({"type": "stop"})
            # connected / mark / dtmf: nothing the pipeline needs

    # ── what the session and playout send ────────────────────────────────────
    async def send_text(self, text: str) -> None:
        if self.closed or not self.stream_sid:
            return
        obj = json.loads(text)
        kind = obj.get("type")
        if kind == "audio":
            pcm = base64.b64decode(obj.get("audio_b64") or "")
            if not pcm:
                return
            out = {"event": "media", "streamSid": self.stream_sid,
                   "media": {"payload": base64.b64encode(pcm_to_mulaw(pcm)).decode("ascii")}}
        elif kind == "clear":
            out = {"event": "clear", "streamSid": self.stream_sid}
        else:
            return
        await self.ws.send_text(json.dumps(out))

    async def close(self, code: int = 1000) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            await self.ws.close(code=code)
        except Exception:
            pass
