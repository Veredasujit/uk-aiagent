"""
tts_deepgram.py — Deepgram Aura-2 (American English) over its streaming WebSocket.

Why this vendor for the US agent (measured from the VPS on 2026-11-14):
- ~340 ms to first audio on a kept-open connection (850 ms on a cold one), synthesis about
  2x faster than real time; OpenAI TTS was ~1.5 s to first audio.
- 8 kHz linear16 straight from the API, so nothing is resampled on the hot path.
- Same account as the speech recogniser; no small monthly character quota.

One socket per call, opened while the call connects (prewarm). Each reply is
Speak + Flush; audio arrives as binary frames until a "Flushed" message. On barge-in we
send Clear and drain to "Cleared", so the next reply never starts with stale audio.
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import AsyncGenerator, Optional

import websockets

from . import config
from .logging_setup import CallLog, error, info, log, warn


class DeepgramTTS:
    def __init__(self, call_log: CallLog, voice: Optional[str] = None) -> None:
        self.call_log = call_log
        self.voice = voice or config.DEEPGRAM_TTS_VOICE
        self.codec = "linear16"
        self.sample_rate = 8000
        self._ws = None
        self._lock = asyncio.Lock()
        self._abort = asyncio.Event()
        # True when a reply was abandoned mid-stream: the socket may still hold its audio
        self._dirty = False

    def _url(self) -> str:
        return f"{config.DEEPGRAM_TTS_URL}?model={self.voice}&encoding=linear16&sample_rate={self.sample_rate}"

    def _open(self) -> bool:
        ws = self._ws
        if ws is None:
            return False
        closed = getattr(ws, "closed", None)
        if closed is None:  # websockets >= 14: state enum
            state = getattr(ws, "state", None)
            return getattr(state, "name", "OPEN") == "OPEN"
        return not closed

    async def connect(self) -> bool:
        if self._open():
            return True
        headers = {"Authorization": f"Token {config.DEEPGRAM_API_KEY}"}
        t0 = time.monotonic()
        try:
            try:
                self._ws = await asyncio.wait_for(
                    websockets.connect(self._url(), additional_headers=headers, ping_interval=20, ping_timeout=20,
                                       close_timeout=2, max_size=8 * 1024 * 1024), timeout=6)
            except TypeError:
                self._ws = await asyncio.wait_for(
                    websockets.connect(self._url(), extra_headers=headers, ping_interval=20, ping_timeout=20,
                                       close_timeout=2, max_size=8 * 1024 * 1024), timeout=6)
        except Exception as exc:
            error("tts", f"deepgram connect failed: {exc}")
            self._ws = None
            return False
        self._dirty = False
        ms = (time.monotonic() - t0) * 1000
        info("tts", f"deepgram socket ready in {ms:.0f} ms ({self.voice})")
        self.call_log.event("tts_connected", vendor="deepgram", ms=round(ms), voice=self.voice)
        return True

    async def prewarm(self) -> bool:
        return await self.connect()

    async def _clear(self) -> None:
        """Throw away whatever the socket still has queued, or reconnect if that fails."""
        ws = self._ws
        if ws is None:
            return
        try:
            await ws.send(json.dumps({"type": "Clear"}))
            deadline = time.monotonic() + 2.0
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise TimeoutError("no Cleared")
                msg = await asyncio.wait_for(ws.recv(), timeout=left)
                if isinstance(msg, str) and json.loads(msg).get("type") == "Cleared":
                    break
            self._dirty = False
        except Exception as exc:
            log("tts", f"deepgram clear failed, reconnecting: {exc}")
            await self._drop()

    async def stream(self, text: str) -> AsyncGenerator[bytes, None]:
        """Yield 8 kHz s16 PCM for `text`. Raises TTSAborted on barge-in."""
        from .tts import TTSAborted

        text = (text or "").strip()
        if not text:
            return
        async with self._lock:
            self._abort.clear()
            if self._dirty:
                await self._clear()
            if not await self.connect():
                raise RuntimeError("deepgram tts not connected")
            ws = self._ws
            done = False
            t0 = time.monotonic()
            first = True
            self.call_log.event("tts_request", vendor="deepgram", chars=len(text), text=text)
            try:
                await ws.send(json.dumps({"type": "Speak", "text": text}))
                await ws.send(json.dumps({"type": "Flush"}))
                while True:
                    if self._abort.is_set():
                        raise TTSAborted()
                    try:
                        msg = await asyncio.wait_for(ws.recv(), timeout=8.0 if first else 5.0)
                    except asyncio.TimeoutError:
                        raise RuntimeError("deepgram tts timed out")
                    if isinstance(msg, (bytes, bytearray)):
                        if first:
                            first = False
                            ms = (time.monotonic() - t0) * 1000
                            info("tts", f"first audio chunk in {ms:.1f} ms")
                            self.call_log.event("tts_first_chunk", vendor="deepgram", ms=round(ms))
                        if self._abort.is_set():
                            raise TTSAborted()
                        yield bytes(msg)
                        continue
                    m = json.loads(msg)
                    kind = m.get("type")
                    if kind == "Flushed":
                        done = True
                        return
                    if kind == "Error":
                        raise RuntimeError(f"deepgram tts error: {m.get('description') or m}")
                    if kind == "Warning":
                        warn("tts", f"deepgram warning: {m.get('description') or m}")
            except TTSAborted:
                raise
            except Exception:
                done = True
                await self._drop()
                raise
            finally:
                if not done:
                    # Abandoned mid-reply (barge-in or the consumer stopped): clear before reuse
                    self._dirty = True

    def abort(self) -> None:
        self._abort.set()

    async def _drop(self) -> None:
        ws, self._ws = self._ws, None
        self._dirty = False
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass

    async def close(self) -> None:
        ws = self._ws
        if ws is not None and self._open():
            try:
                await ws.send(json.dumps({"type": "Close"}))
            except Exception:
                pass
        await self._drop()
