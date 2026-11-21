"""
session.py — one live test call for a Vedronix visitor's agent.

    Teler WS ──► 8 kHz PCM ──┬──► Silero VAD   (is someone talking?)
                             └──► Deepgram     (what did they say?)

    turn end ──► OpenAI (+ the session's enabled functions) ──► TTS ──► playout ──► Teler WS

Adapted on 2026-11-10 from the Sakshi agent's session (vereda-flutter-agent), whose audio
mechanics — barge-in, turn end, clause streaming, playout — were tuned on live calls and
are kept as they are. What changes is who the agent is: prompt, greeting, voice, language
and functions come from the test session (backend.session_config), not from config.

Limits a test call can't exceed:
  * the session's end time (backend `ends_at`), and MAX_CALL_SECONDS per call;
  * the session's speech budget (MAX_TTS_CHARS_PER_SESSION, shared TTS quota).

Kept from Sakshi's live-call fixes: replies stream clause by clause; a goodbye without a
question ends the call even if the model forgets end_call; "cut the call" / "not
interested" counts as the caller ending; the network's hold announcement silences the
agent until a person speaks again.

After hang-up the transcript, summary, functions and lead go to the backend in one
report (backend.call_ended), which is spooled to disk if the backend is down.
"""
from __future__ import annotations

import asyncio
import base64
import json
import re
import time
from typing import Optional

from . import backend, config
from . import twilio_api as teler_api  # same interface: is_teler_call, hangup
from .analysis import summarise
from .audio import EndianDetector
from .llm import LLMClient, SentenceChunker, speakable
from .logging_setup import CallLog, error, info, log, new_call_id, set_call_id, warn
from .playout import Playout
from .prompt import build_system_prompt, greeting_for, line
from .stt_deepgram import DeepgramSTT
from .tools import HOLDING_FUNCTIONS, ToolRunner
from .tts import TTSAborted, TTSEngine, TTSUnavailable
from .vad import SileroVAD, VadEvent

BARGE_ASR_WINDOW_S = 3.0
VAD_TURN_END_QUIET_S = 1.6
PENDING_TEXT_MAX_AGE_S = 10.0
# If the words heard so far haven't changed for this long, the caller has finished. On a noisy
# line Deepgram's own "final" came 4.0 s after the last word (2026-11-11: "ठीक है" heard at
# 13.4 s, final at 17.4 s), which was most of the silence the caller complained about.
STABLE_INTERIM_S = 0.75
# A phrase that ends mid-thought ("And first of", "I want to", "my name is") needs a longer
# pause before we answer it: on 2026-11-14 "Hi. And first of" was answered as a whole turn.
_OPEN_ENDINGS = {"and", "or", "but", "of", "to", "for", "with", "the", "a", "an", "my", "is", "are", "was",
                 "i", "i'm", "im", "so", "because", "um", "uh", "like", "at", "on", "in", "about", "if",
                 "that", "this", "your", "our", "can", "could", "would", "want", "need", "it's"}


def _quiet_needed(text: str) -> float:
    t = text.strip().lower()
    words = re.findall(r"[a-z']+", t)
    last = words[-1] if words else ""
    if last in _OPEN_ENDINGS or t.endswith((",", "-")):
        return 1.7
    if t.endswith((".", "?", "!")):
        return STABLE_INTERIM_S
    return 1.1

def _same_words(a: list[str], b: list[str]) -> bool:
    """Interim and final transcripts differ in punctuation and case, not words."""
    clean = lambda ws: [re.sub(r"[^\w\u0900-\u097F]", "", w).lower() for w in ws]
    return bool(a) and clean(a) == clean(b)

BARGE_ASR_ONLY_WORDS = 3
CALLER_HOLD_MAX_S = 90
WAIT_REQUEST_S = 60

BACKCHANNELS = {
    "हाँ", "हां", "हा", "जी", "हम्म", "हम", "हूँ", "अच्छा", "ठीक", "है", "ओके",
    "उम्म", "ok", "okay", "haan", "han", "ji", "hmm", "hm", "accha", "acha",
    "achha", "theek", "thik", "yes", "right", "yeah", "sure",
    # American English backchannels
    "uh-huh", "uh", "huh", "mm-hmm", "mhm", "mm", "um", "yep", "yup", "alright", "gotcha", "cool", "great",
    # what people say as they pick up; on 2026-11-14 a caller's "Hi?" cut the greeting short
    "hi", "hello", "hey", "hiya",
}
_WORD = re.compile(r"[\wऀ-ॿ]+")

_CLOSING = re.compile(
    r"(थैंक\s*यू|थैंक्यू|thank\s*you|thanks|धन्यवाद|शुक्रिया|\bbye\b|बाय|"
    r"(फ़ोन|फोन|phone|call|कॉल)\s*(रख|काट|कट|cut|बंद)|रखता\s*हूँ|रखती\s*हूँ|बस\s*इतना|that'?s\s*all|"
    r"interested\s*(नहीं|नही|nahi)|not\s+interested|(नहीं|नही)\s*चाहिए|बात\s*नहीं\s*करनी|बाद\s*में\s*बात|"
    r"call\s*(मत|ना)\s*कर|disconnect|hang\s*up|i'?m\s+(good|all\s+set)|that'?s\s+it|gotta\s+go|talk\s+(to\s+you\s+)?later|"
    r"no\s+thanks|not\s+right\s+now)",
    re.IGNORECASE,
)
_AGENT_FAREWELL = re.compile(
    r"(दिन\s*(अच्छा|शुभ)\s*रहे|अलविदा|\bbye\b|goodbye|take\s*care|ख़्याल\s*रखिए|ख्याल\s*रखिए|"
    r"(समय|time)\s*देने\s*के\s*लिए\s*धन्यवाद|(कॉल|call)\s*के\s*लिए\s*(बहुत\s*)?धन्यवाद|"
    r"have\s+a\s+(great|good|nice|wonderful|lovely)\s+(day|evening|night|weekend|one)|thank(s| you)\s+for\s+calling|"
    r"see\s+you\s+(then|soon))",
    re.IGNORECASE,
)
_HOLD_ANNOUNCEMENT = re.compile(
    r"(put\s+(your|the)\s+call\s+on\s+hold|has\s+put\s+you\s+on\s+hold|stay\s+on\s+the\s+line|"
    r"please\s+hold|hold\s*पर\s*(रखा|है)|होल्ड\s*पर|line\s*पर\s*बने\s*रह|लाइन\s*पर\s*बने\s*रह|"
    r"कृपया\s*(line|लाइन)\s*पर|कृपया\s*प्रतीक्षा|प्रतीक्षा\s*करें|is\s+on\s+another\s+call|"
    r"दूसरी\s*(call|कॉल)\s*पर\s*(व्यस्त|busy)|आप\s*जिस\s*व्यक्ति\s*से)",
    re.IGNORECASE,
)
_WAIT_REQUEST = re.compile(
    r"(एक\s*(minute|मिनट|मिनिट|second|सेकंड)|(1|one|ek)\s*(minute|min|second|sec)|"
    r"रुकि(ए|एगा|ये)|रुको|hold\s+on|wait\s+(a|one)|just\s+a\s+(minute|second|sec)|hang\s+on|one\s+sec|"
    r"give\s+me\s+a\s+(sec|second|minute|moment)|let\s+me\s+(check|grab|find))",
    re.IGNORECASE,
)
_BACK_WORDS = re.compile(
    r"(hello|हेलो|हैलो|हलो|hi\b|हाँ|हां|haan|ji\b|जी|बोलिए|बोलो|बताइए|sorry|सॉरी|yes|ok\b|ओके|अच्छा)",
    re.IGNORECASE,
)


# "you're all set", "you're booked", "it's confirmed", "see you then": only true after a booking
_BOOKING_CLAIM = re.compile(
    r"\b(you'?re|you are|you'?ve been|it'?s|that'?s|your appointment is|your appointment has been)\s+"
    r"(all\s+set|booked|confirmed|scheduled)\b|\bsee\s+you\s+(then|on\s+\w+)\b",
    re.IGNORECASE,
)


def is_hold_announcement(text: str) -> bool:
    return bool(_HOLD_ANNOUNCEMENT.search(text or ""))


def is_wait_request(text: str) -> bool:
    return bool(_WAIT_REQUEST.search(text or ""))


def sounds_like_goodbye(text: str) -> bool:
    return bool(_CLOSING.search(text or ""))


def is_agent_farewell(text: str) -> bool:
    t = (text or "").strip()
    return bool(t) and "?" not in t and bool(_AGENT_FAREWELL.search(t))


def interruption_words(text: str) -> int:
    words = _WORD.findall((text or "").lower())
    if len("".join(words)) < config.BARGE_IN_MIN_CHARS:
        return 0
    return sum(1 for w in words if w not in BACKCHANNELS)


POSTCALL_TASKS: set[asyncio.Task] = set()


class CallSession:
    def __init__(self, ws, token: str, cfg: dict) -> None:
        self.ws = ws
        self.token = token
        self.cfg = cfg
        agent = cfg.get("agent", {})
        self.lang = agent.get("language") or "multi"
        self.call_id = new_call_id()
        self.teler_call_id: Optional[str] = None
        self.direction = "inbound"
        set_call_id(self.call_id)

        self.call_log = CallLog(self.call_id)
        self.playout = Playout(ws, self.call_log)
        self.vad = SileroVAD()
        voice = agent.get("voice") or {}
        self.tts = TTSEngine(self.call_log, provider=voice.get("provider"), voice=voice.get("voice_id"),
                             language=voice.get("language") or ("en-US" if self.lang == "en" else "hi-IN"))
        self.llm = LLMClient(self.call_log)
        self.tools = ToolRunner(self.call_log, token, cfg)
        self.stt = DeepgramSTT(
            self.call_log,
            on_transcript=self._on_transcript,
            on_speech_started=self._on_stt_speech_started,
            on_utterance_end=self._on_stt_utterance_end,
            language={"en": config.DEEPGRAM_LANGUAGE, "hi": "hi"}.get(self.lang, "multi"),
        )

        self.greeting = greeting_for(cfg)
        self.messages: list[dict] = []
        self.transcript: list[dict] = []  # {"who", "text", "at"} for the report
        self._human_words = 0

        self.endian = EndianDetector(config.TELER_ENDIAN)
        self._endian_known = asyncio.Event()
        if self.endian.decided:
            self._endian_known.set()

        self._turn_task: Optional[asyncio.Task] = None
        self._turn_lock = asyncio.Lock()
        self._pending_user_text = ""
        self._pending_since = 0.0
        self._interim_text = ""
        self._interim_task: Optional[asyncio.Task] = None
        self._committed_interim = ""
        self._turn_seq = 0
        self._turns_spoken: set[int] = set()
        self._stream_ready = asyncio.Event()
        self._started = asyncio.Event()
        self._barge_armed = False
        self._utterance_started_at = 0.0
        self._ended = asyncio.Event()
        self._last_activity_at = time.monotonic()
        self._last_transcript_at = time.monotonic()
        self._reprompts = 0
        self._started_at = time.monotonic()
        self._started_wall = time.time()
        self._watchdog: Optional[asyncio.Task] = None
        self._audio_frames_in = 0
        self._shut = False
        self._caller_hold_since = 0.0
        self._wait_until = 0.0
        self.end_reason = ""

        # Time and speech left for this session (it may have had calls already)
        ends_at = float(cfg.get("ends_at") or 0)
        left = (ends_at - time.time()) if ends_at else config.MAX_CALL_SECONDS
        self._deadline = time.monotonic() + max(5.0, min(config.MAX_CALL_SECONDS, left))
        self.tts_chars = 0
        self.tts_budget = int(cfg.get("tts_chars_left") or config.MAX_TTS_CHARS_PER_SESSION)

    # ════════════════════════════════════════════════════════════════════════
    # lifecycle
    # ════════════════════════════════════════════════════════════════════════
    async def run(self) -> None:
        info("call", f"session {self.cfg.get('session_id')} call start ({self.cfg.get('agent', {}).get('name')})")
        self.call_log.event("call_start", session=self.cfg.get("session_id"), lang=self.lang,
                            voice=self.cfg.get("agent", {}).get("voice"))
        self.playout.start()
        stt_task = asyncio.create_task(self.stt.start(), name="stt-connect")
        asyncio.create_task(self._prewarm_tts(), name="tts-prewarm")
        reader = asyncio.create_task(self._read_loop(), name="ws-read")
        try:
            await asyncio.wait_for(self._started.wait(), timeout=config.START_FRAME_WAIT_S)
        except asyncio.TimeoutError:
            warn("call", "no start frame yet — greeting anyway")
        self.gate = bool(self.cfg.get("gate"))
        if not self.gate:
            asyncio.create_task(backend.call_started(self.token, {
                "call_id": self.call_id, "teler_call_id": self.teler_call_id, "direction": self.direction,
                "started_at": self._started_wall,
            }))

        self.messages = [
            {"role": "system", "content": build_system_prompt(self.cfg)},
            {"role": "assistant", "content": self.greeting},
        ]
        if not self.gate:
            asyncio.create_task(self.llm.warm(self.messages, self.tools.schema), name="llm-warm")
        if not self._endian_known.is_set():
            try:
                await asyncio.wait_for(self._endian_known.wait(), timeout=config.ENDIAN_DETECT_TIMEOUT_S)
            except asyncio.TimeoutError:
                warn("call", "endianness undecided (caller silent) — using default")

        self._note("agent", self.greeting)
        cached = self.cfg.get("greeting_pcm")
        if cached:
            await self._play_cached(self.greeting, cached)
        else:
            await self._speak_text(self.greeting, tag="greeting", record=False)
        if self.gate:
            # Someone called the test number without a running session: say so, then hang up
            await self.playout.wait_drained(timeout=20.0)
            await asyncio.sleep(0.5)
            self.end_reason = "gate"
            await self._close_socket()
        if stt_task.done() and stt_task.exception():
            error("call", f"STT connect failed: {stt_task.exception()}")
        self._last_activity_at = time.monotonic()
        self._watchdog = asyncio.create_task(self._watchdog_loop(), name="watchdog")
        try:
            await reader
        finally:
            await self.shutdown()

    async def _prewarm_tts(self) -> None:
        try:
            await self.tts.prewarm()
        except Exception as exc:
            error("call", f"TTS prewarm failed: {exc}")

    def elapsed_seconds(self) -> float:
        return time.monotonic() - self._started_at

    async def hangup_now(self, reason: str = "backend") -> None:
        self.end_reason = self.end_reason or reason
        info("call", f"ending call: {reason}")
        self.playout.clear()
        await self._close_socket()

    def _note(self, who: str, text: str) -> None:
        if text and text.strip():
            self.transcript.append({"who": who, "text": text.strip(), "at": round(time.time(), 2)})

    # ════════════════════════════════════════════════════════════════════════
    # socket
    # ════════════════════════════════════════════════════════════════════════
    async def _read_loop(self) -> None:
        while not self._ended.is_set():
            try:
                raw = await self.ws.receive_text()
            except Exception as exc:
                log("call", f"websocket read ended: {exc}")
                return
            try:
                msg = json.loads(raw)
            except (TypeError, ValueError):
                continue
            mtype = (msg.get("type") or msg.get("event") or "").lower()
            if mtype in ("audio", "media"):
                await self._on_audio(msg)
            elif mtype in ("start", "connected", "stream_started"):
                data = msg.get("data") or msg.get("start") or {}
                self.teler_call_id = data.get("call_id") or msg.get("call_id") or self.teler_call_id
                direction = str(data.get("direction") or msg.get("direction") or "").lower()
                if direction:
                    self.direction = "outbound" if "out" in direction else "inbound"
                self._stream_ready.set()
                self._started.set()
                self.call_log.event("stream_start", teler_call_id=self.teler_call_id, direction=self.direction)
            elif mtype in ("stop", "stream_stopped", "hangup", "disconnected"):
                self.end_reason = self.end_reason or "caller_hung_up"
                return

    async def _on_audio(self, msg: dict) -> None:
        data = msg.get("data") or {}
        b64 = data.get("audio_b64") or data.get("audio") or data.get("payload") or msg.get("audio_b64")
        if not b64:
            return
        try:
            pcm = base64.b64decode(b64)
        except Exception:
            return
        if not pcm:
            return
        self._audio_frames_in += 1
        self._stream_ready.set()
        self._started.set()
        was_decided = self.endian.decided
        pcm = self.endian.feed(pcm)
        if self.endian.decided and not was_decided:
            self.playout.big_endian = self.endian.big_endian
            self.call_log.event("endian_detected", big_endian=self.endian.big_endian)
            self._endian_known.set()
        await self.stt.send_audio(pcm)
        if not config.VAD_ENABLED:
            return
        bot_speaking = self._bot_is_speaking()
        for event, prob in self.vad.feed(pcm, bot_speaking=bot_speaking):
            if event is VadEvent.SPEECH_START:
                self._last_activity_at = time.monotonic()
                if bot_speaking:
                    await self._on_vad_barge_candidate()
            elif event is VadEvent.SPEECH_END:
                self._barge_armed = False
                if not self._bot_is_speaking() and self._pending_user_text:
                    if time.monotonic() - self._last_transcript_at >= VAD_TURN_END_QUIET_S:
                        await self._start_turn("vad_silence")

    def _bot_is_speaking(self) -> bool:
        return self.playout.playing or (self._turn_task is not None and not self._turn_task.done())

    # ════════════════════════════════════════════════════════════════════════
    # barge-in (unchanged from the tuned original)
    # ════════════════════════════════════════════════════════════════════════
    async def _on_vad_barge_candidate(self) -> None:
        if not config.BARGE_IN_ENABLED:
            return
        if self._speaking_ms() < config.BARGE_IN_GRACE_MS:
            return
        if not config.BARGE_IN_REQUIRE_ASR:
            await self._do_barge_in("vad")
            return
        self._barge_armed = True
        asyncio.create_task(self._barge_disarm_after(BARGE_ASR_WINDOW_S))

    async def _maybe_barge_on_words(self, text: str, is_final: bool) -> None:
        if not config.BARGE_IN_ENABLED or not self._bot_is_speaking():
            return
        n = interruption_words(text)
        if n == 0 or self._speaking_ms() < config.BARGE_IN_GRACE_MS:
            return
        if self._barge_armed or not config.BARGE_IN_REQUIRE_ASR:
            await self._do_barge_in("vad+asr")
        elif not self.vad.available:
            await self._do_barge_in("asr_no_vad")
        elif n >= BARGE_ASR_ONLY_WORDS:
            await self._do_barge_in("asr")

    def _speaking_ms(self) -> float:
        if self._utterance_started_at:
            return (time.monotonic() - self._utterance_started_at) * 1000.0
        return self.playout.speaking_for_ms()

    async def _barge_disarm_after(self, seconds: float) -> None:
        await asyncio.sleep(seconds)
        self._barge_armed = False

    async def _do_barge_in(self, source: str) -> None:
        if not self._bot_is_speaking():
            return
        self._barge_armed = False
        self._utterance_started_at = 0.0
        dropped = self.playout.clear()
        self.tts.abort()
        if self._turn_task and not self._turn_task.done():
            self._turn_task.cancel()
        self.call_log.event("barge_in", source=source, dropped_bytes=dropped)
        if self.messages and self.messages[-1].get("role") == "assistant":
            content = self.messages[-1].get("content") or ""
            if content:
                self.messages[-1]["content"] = content + line("interrupted", self.lang)

    # ════════════════════════════════════════════════════════════════════════
    # transcripts and turns
    # ════════════════════════════════════════════════════════════════════════
    async def _on_transcript(self, text: str, is_final: bool, speech_final: bool) -> None:
        self._last_activity_at = self._last_transcript_at = time.monotonic()
        self._reprompts = 0
        if is_hold_announcement(text):
            await self._enter_caller_hold(text)
            return
        if self._caller_hold_since:
            if not is_final or not _BACK_WORDS.search(text):
                return
            self._leave_caller_hold()
        await self._maybe_barge_on_words(text, is_final)
        if not is_final:
            self._note_interim(text)
            return
        self._cancel_interim()
        if self._committed_interim:
            # The turn already started from this text when it went quiet; drop the late copy
            done, self._committed_interim = self._committed_interim.split(), ""
            words = text.split()
            if _same_words(words[:len(done)], done):
                text = " ".join(words[len(done):])
            elif _same_words(words, done[:len(words)]):
                text = ""
            if not text.strip():
                return
        self._human_words += len(text.split())
        if not self._pending_user_text:
            self._pending_since = time.monotonic()
        self._pending_user_text = (self._pending_user_text + " " + text).strip()
        if speech_final:
            await self._start_turn("speech_final")

    def _note_interim(self, text: str) -> None:
        t = text.strip()
        if not t or t == self._interim_text:
            return  # unchanged: the quiet timer keeps running
        self._interim_text = t
        if self._interim_task and not self._interim_task.done():
            self._interim_task.cancel()
        self._interim_task = asyncio.create_task(self._commit_stable_interim(t), name="stable-interim")

    def _cancel_interim(self) -> None:
        self._interim_text = ""
        if self._interim_task and not self._interim_task.done():
            self._interim_task.cancel()
        self._interim_task = None

    async def _commit_stable_interim(self, t: str) -> None:
        try:
            await asyncio.sleep(_quiet_needed(t))
        except asyncio.CancelledError:
            return
        if (self._interim_text != t or self._bot_is_speaking() or self._caller_hold_since
                or self._ended.is_set() or not self.messages):
            return
        self._interim_text = ""
        self._interim_task = None
        self._committed_interim = (self._committed_interim + " " + t).strip()
        self._human_words += len(t.split())
        if not self._pending_user_text:
            self._pending_since = time.monotonic()
        self._pending_user_text = (self._pending_user_text + " " + t).strip()
        self.call_log.event("turn_end_early", text=t[:120], quiet_ms=int(_quiet_needed(t) * 1000))
        await self._start_turn("stable_interim")

    async def _on_stt_speech_started(self, _: str) -> None:
        self._last_activity_at = time.monotonic()

    async def _on_stt_utterance_end(self, _: str) -> None:
        if self._pending_user_text and not self._bot_is_speaking():
            await self._start_turn("utterance_end")

    async def _enter_caller_hold(self, text: str) -> None:
        if not self._caller_hold_since:
            self._caller_hold_since = time.monotonic()
            self.call_log.event("caller_hold", on=True, text=text[:120])
        if self._turn_task and not self._turn_task.done():
            self._turn_task.cancel()
        self.playout.clear()
        self._pending_user_text = ""

    def _leave_caller_hold(self) -> None:
        held = round(time.monotonic() - self._caller_hold_since)
        self._caller_hold_since = 0.0
        self._wait_until = 0.0
        self.call_log.event("caller_hold", on=False, seconds=held)
        self.messages.append({"role": "system", "content": (
            f"[The caller had you on hold for {held} s and is back. The hold recording was not the caller. "
            "Continue briefly from where you stopped; don't repeat everything.]")})

    async def _start_turn(self, trigger: str) -> None:
        async with self._turn_lock:
            text = self._pending_user_text.strip()
            if not text or self._ended.is_set() or not self.messages:
                return
            if time.monotonic() - self._pending_since > PENDING_TEXT_MAX_AGE_S:
                self._pending_user_text = ""
                return
            if self._turn_task and not self._turn_task.done():
                return
            self._pending_user_text = ""
            self._pending_since = 0.0
            self._turn_seq += 1
            seq = self._turn_seq
            self.call_log.event("turn_start", seq=seq, trigger=trigger, user_text=text)
            self.call_log.mark(f"turn_{seq}")
            self.messages.append({"role": "user", "content": text})
            self._note("caller", text)
            if is_wait_request(text):
                self._wait_until = time.monotonic() + WAIT_REQUEST_S
                self.messages.append({"role": "system", "content": "[The caller asked you to wait. Say only a short 'sure, I'm here' and stay quiet.]"})
            elif sounds_like_goodbye(text):
                self.messages.append({"role": "system", "content": (
                    "[The caller seems to be ending the call. Unless they asked something new, say a short thank-you "
                    "and call end_call in this turn.]")})
            self._turn_task = asyncio.create_task(self._run_turn(seq), name=f"turn-{seq}")

    async def _run_turn(self, seq: int, depth: int = 0) -> None:
        if depth > 4:
            warn("turn", "tool-call depth limit reached")
            return
        generation = self.playout.generation
        self.playout.tts_active(True)
        assistant_text = ""
        tool_calls: list[dict] = []
        chunker = SentenceChunker()
        clauses: asyncio.Queue = asyncio.Queue()
        speaker = asyncio.create_task(self._speak_clauses(clauses, generation, seq), name=f"speak-{seq}-{depth}")
        try:
            async for kind, payload in self.llm.stream(self.messages, self.tools.schema):
                if kind == "token":
                    assistant_text += payload
                    for clause in chunker.push(payload):
                        clauses.put_nowait(clause)
                elif kind == "tool_calls":
                    tool_calls = payload
            tail = chunker.flush()
            if tail:
                clauses.put_nowait(tail)
            clauses.put_nowait(None)
            if not await speaker:
                return
        except asyncio.CancelledError:
            speaker.cancel()
            self.playout.tts_active(False)
            if assistant_text.strip():
                self.messages.append({"role": "assistant", "content": assistant_text.strip()})
                self._note("agent", assistant_text)
            raise
        except Exception as exc:
            speaker.cancel()
            self.playout.tts_active(False)
            error("turn", f"#{seq} failed: {exc}")
            await self._speak_text(line("error", self.lang), tag="error")
            return
        self.playout.tts_active(False)

        if assistant_text.strip() or tool_calls:
            entry: dict = {"role": "assistant", "content": assistant_text.strip() or None}
            if tool_calls:
                entry["tool_calls"] = [
                    {"id": tc["id"], "type": "function",
                     "function": {"name": tc["name"], "arguments": json.dumps(tc["arguments"], ensure_ascii=False)}}
                    for tc in tool_calls
                ]
            self.messages.append(entry)
            self._note("agent", assistant_text)
        if not tool_calls:
            if is_agent_farewell(assistant_text):
                self.end_reason = self.end_reason or "farewell"
                await self._finish_and_hangup()
            return

        holding = None
        if (seq not in self._turns_spoken and not assistant_text.strip()
                and any(tc["name"] in HOLDING_FUNCTIONS for tc in tool_calls)):
            holding = asyncio.create_task(self._speak_text(line("holding", self.lang), tag="holding"))
        for tc in tool_calls:
            result = await self.tools.run(tc["name"], tc["arguments"])
            self.messages.append({"role": "tool", "tool_call_id": tc["id"],
                                  "content": json.dumps(result, ensure_ascii=False)})
        if holding is not None:
            await holding
        if self.tools.hangup_requested:
            if seq not in self._turns_spoken:
                await self._speak_text(line("farewell", self.lang), tag="farewell")
            self.end_reason = self.end_reason or (self.tools.hangup_reason or "agent")
            await self._finish_and_hangup()
            return
        if assistant_text.strip() and all(tc["name"] == "save_customer_info" for tc in tool_calls):
            return
        await self._run_turn(seq, depth + 1)

    # ════════════════════════════════════════════════════════════════════════
    # speaking
    # ════════════════════════════════════════════════════════════════════════
    async def _speak_clauses(self, clauses: asyncio.Queue, generation: int, seq: int) -> bool:
        first = True
        while True:
            clause = await clauses.get()
            if clause is None:
                return True
            mark = f"turn_{seq}" if first else None
            first = False
            if not await self._speak_chunk(clause, generation, seq, mark):
                return False

    def _budget_ok(self, text: str) -> bool:
        if self.tts_chars + len(text) > self.tts_budget:
            if not self.end_reason:
                self.end_reason = "speech_budget"
                warn("call", f"speech budget used up ({self.tts_chars}/{self.tts_budget} chars)")
                asyncio.create_task(self._end_with(line("budget", self.lang), "speech_budget"))
            return False
        self.tts_chars += len(text)
        return True

    def _guard_booking_claims(self, text: str) -> str:
        """Never let the agent tell a caller they're booked unless a booking succeeded on THIS
        call. Found on a live call (2026-11-14): after a garbled conversation it said "You're
        all set for today at four" with no booking made, on a day the office was closed."""
        if any(c.get("name") == "book_appointment" and c.get("ok") for c in self.tools.calls):
            return text
        fixed = _BOOKING_CLAIM.sub(
            lambda m: "Got it" if m.start() == 0 or text[:m.start()].rstrip()[-1:] in ".!?" else "got it", text)
        if fixed != text:
            warn("guard", f"removed an unbacked booking claim: {text[:120]!r}")
            self.call_log.event("guard_booking_claim", text=text[:200])
        return fixed

    async def _speak_chunk(self, chunk: str, generation: int, seq: int, latency_mark: Optional[str] = None) -> bool:
        text = speakable(self._guard_booking_claims(chunk))
        if not text:
            return True
        if not self._budget_ok(text):
            return False
        first = True
        try:
            async for pcm in self.tts.stream(text):
                if generation != self.playout.generation:
                    return False
                if first:
                    self._utterance_started_at = time.monotonic()
                    self._turns_spoken.add(seq)
                    if latency_mark:
                        self.call_log.latency("user_speech_to_first_audio", latency_mark, seq=seq)
                    first = False
                self.playout.push(pcm, generation)
            if generation == self.playout.generation:
                await self.playout.flush()
            return True
        except TTSAborted:
            return False
        except TTSUnavailable as exc:
            error("tts", f"voice unavailable: {exc}")
            asyncio.create_task(self.hangup_now("voice_unavailable"))
            return False
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error("tts", f"chunk failed: {exc}")
            return True

    async def _play_cached(self, text: str, pcm: bytes) -> None:
        """The greeting, synthesised once at startup: plays the moment the call connects."""
        generation = self.playout.generation
        self.call_log.event("say", tag="greeting", text=text, cached=True)
        self._utterance_started_at = time.monotonic()
        step = 3200  # 200 ms
        for i in range(0, len(pcm), step):
            if generation != self.playout.generation:
                return
            self.playout.push(pcm[i:i + step], generation)
        if generation == self.playout.generation:
            await self.playout.flush()

    async def _speak_text(self, text: str, tag: str = "say", record: bool = True) -> None:
        """Lines the session speaks itself (greeting, holding line, limits)."""
        generation = self.playout.generation
        self.call_log.event("say", tag=tag, text=text)
        if record and tag not in ("holding",):
            self._note("agent", text)
        self.tts_chars += len(text)
        try:
            first = True
            async for pcm in self.tts.stream(speakable(text)):
                if generation != self.playout.generation:
                    return
                if first:
                    self._utterance_started_at = time.monotonic()
                    first = False
                self.playout.push(pcm, generation)
            if generation == self.playout.generation:
                await self.playout.flush()
        except TTSAborted:
            pass
        except Exception as exc:
            error("say", f"failed: {exc}")

    async def _end_with(self, text: str, reason: str) -> None:
        self.end_reason = self.end_reason or reason
        if self._turn_task and not self._turn_task.done() and asyncio.current_task() is not self._turn_task:
            self._turn_task.cancel()
        self.playout.clear()
        await self._speak_text(text, tag=reason)
        await self.playout.wait_drained(timeout=12.0)
        await self._close_socket()

    async def _finish_and_hangup(self) -> None:
        await self.playout.wait_drained(timeout=25.0)
        await asyncio.sleep(config.GOODBYE_HANGUP_DELAY_S)
        await self._close_socket()

    async def _close_socket(self) -> None:
        # Ask Teler to hang up, then close the stream: closing the socket alone did not always
        # drop the line (Sakshi agent, 2026-11-04).
        if teler_api.is_teler_call(self.teler_call_id):
            try:
                await asyncio.wait_for(teler_api.hangup(self.teler_call_id, self.end_reason or "agent"), 8)
            except Exception as exc:
                warn("call", f"teler hangup: {exc}")
        try:
            await self.ws.close(code=1000)
        except Exception:
            pass
        self._ended.set()

    # ════════════════════════════════════════════════════════════════════════
    # watchdog: time limit, silence, hold
    # ════════════════════════════════════════════════════════════════════════
    async def _watchdog_loop(self) -> None:
        try:
            while not self._ended.is_set():
                await asyncio.sleep(1.0)
                now = time.monotonic()
                if now >= self._deadline:
                    warn("call", "session time is up")
                    await self._end_with(line("time_up", self.lang), "time_up")
                    return
                if self._caller_hold_since:
                    if now - self._caller_hold_since > CALLER_HOLD_MAX_S:
                        await self._end_with(line("hold_timeout", self.lang), "caller_hold_timeout")
                        return
                    continue
                if now < self._wait_until or self._bot_is_speaking():
                    self._last_activity_at = now
                    continue
                if now - self._last_activity_at < config.SILENCE_REPROMPT_S:
                    continue
                if self._reprompts >= config.MAX_SILENCE_REPROMPTS:
                    await self._end_with(line("no_response", self.lang), "no_response")
                    return
                self._reprompts += 1
                self._last_activity_at = now
                await self._speak_text(line("reprompt_1" if self._reprompts == 1 else "reprompt_2", self.lang), tag="reprompt")
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            error("call", f"watchdog crashed: {exc}")

    # ════════════════════════════════════════════════════════════════════════
    # shutdown + report
    # ════════════════════════════════════════════════════════════════════════
    async def shutdown(self) -> None:
        if self._shut:
            return
        self._shut = True
        self._ended.set()
        dur = round(time.monotonic() - self._started_at, 1)
        self.end_reason = self.end_reason or "caller_hung_up"
        info("call", f"call end after {dur}s, {self._turn_seq} turns ({self.end_reason})")
        self.call_log.event("call_end", seconds=dur, turns=self._turn_seq, reason=self.end_reason,
                            tts_chars=self.tts_chars, audio_frames_in=self._audio_frames_in)
        for t in (self._turn_task, self._watchdog):
            if t and not t.done():
                t.cancel()
        await asyncio.gather(self.playout.stop(), self.stt.close(), self.tts.close(), self.llm.close(),
                             return_exceptions=True)
        if hasattr(self.vad, "close"):
            self.vad.close()
        self.call_log.close()
        task = asyncio.create_task(self._report(dur), name=f"report-{self.call_id}")
        POSTCALL_TASKS.add(task)
        task.add_done_callback(POSTCALL_TASKS.discard)

    async def _report(self, seconds: float) -> None:
        set_call_id(self.call_id)
        if self.cfg.get("gate"):
            return  # not a test call: nothing to report
        talked = bool(self._human_words)
        result = {"summary": "", "outcome": "no_conversation", "lead": None}
        if talked:
            try:
                result = await summarise(self.cfg, self.transcript, self.tools.calls)
            except Exception as exc:
                error("report", f"summary failed: {exc}")
                result = {"summary": "", "outcome": "unknown", "lead": None}
        lead = {**(result.get("lead") or {}), **(self.tools.lead() or {})} or None
        await backend.call_ended(self.token, {
            "report_id": f"{self.call_id}",
            "channel": self.cfg.get("channel"),
            "caller": self.cfg.get("caller"),
            "call_id": self.call_id,
            "teler_call_id": self.teler_call_id,
            "direction": self.direction,
            "started_at": self._started_wall,
            "ended_at": time.time(),
            "seconds": seconds,
            "turns": self._turn_seq,
            "end_reason": self.end_reason,
            "transcript": self.transcript,
            "summary": result.get("summary") or "",
            "outcome": result.get("outcome") or ("no_conversation" if not talked else "unknown"),
            "lead": lead,
            "functions": self.tools.calls,
            "usage": {"tts_chars": self.tts_chars, "turns": self._turn_seq},
        })
