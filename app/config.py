"""
config.py — every setting of the Vedronix US voice agent, from the environment.

Forked from the Vedronix test agent (itself from the Sakshi agent) on 2026-11-14. The audio
and vendor tunables below were tuned on live calls; keep them. What changed for the US:
Twilio instead of FreJun/Teler (Media Streams for phone calls, the Voice JS SDK for browser
calls), American English speech recognition, and Deepgram Aura-2 as the voice.
Secrets live only in the server's .env.production.
"""
from __future__ import annotations

import os

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # python-dotenv is optional
    pass


def _b(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "y", "on"}


def _i(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


def _f(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


def _s(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _list(name: str, default: str) -> list[str]:
    return [x.strip() for x in _s(name, default).split(",") if x.strip()]


def _pipe(name: str, default: str) -> list[str]:
    """Pipe-separated, because the items themselves contain commas."""
    return [x.strip() for x in _s(name, default).split("|") if x.strip()]


def _hhmm(name: str, default: str) -> tuple[int, int]:
    raw = _s(name, default)
    try:
        h, m = raw.split(":")
        return int(h), int(m)
    except ValueError:
        h, m = default.split(":")
        return int(h), int(m)


def _days(name: str, default: str) -> set[int]:
    """'mon,tue,...' -> {0,1,...} (Monday = 0, as datetime.weekday())."""
    names = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    out = set()
    for d in _list(name, default):
        d = d.lower()[:3]
        if d in names:
            out.add(names.index(d))
    return out or {0, 1, 2, 3, 4, 5}


# ─────────────────────────── logging ────────────────────────────────────────
isLogging: bool = _b("IS_LOGGING", True)
IS_LOGGING = isLogging

LOG_LEVEL = _s("LOG_LEVEL", "DEBUG" if isLogging else "WARNING")
LOG_DIR = _s("LOG_DIR", "./logs")
LOG_AUDIO_FRAMES = _b("LOG_AUDIO_FRAMES", False)
LOG_JSONL = _b("LOG_JSONL", True)


# ─────────────────────────── service ────────────────────────────────────────
# Where Twilio and browsers reach us. Every webhook URL is built from this, and Twilio's
# request signature is checked against exactly that URL.
PUBLIC_HOST = _s("PUBLIC_HOST", "cataract-unleveled-confess.ngrok-free.dev")
AGENT_NAME = _s("AGENT_NAME", "Amelia")
BUSINESS_NAME = _s("BUSINESS_NAME", "Brightside Dental")
OFFICE_TIMEZONE = _s("OFFICE_TIMEZONE", "Europe/London")
DATA_DIR = _s("DATA_DIR", "./data")
# Signs stream tokens and transcript-viewer keys (openssl rand -hex 32)
APP_SECRET = _s("APP_SECRET")
# Per-call caps: no call may run on forever or burn the TTS budget
MAX_CALL_SECONDS = _i("MAX_CALL_SECONDS", 600)
MAX_WEB_CALL_SECONDS = _i("MAX_WEB_CALL_SECONDS", 300)
MAX_TTS_CHARS_PER_SESSION = _i("MAX_TTS_CHARS_PER_SESSION", 6000)
# Calls handled at once (phone + browser). Beyond this, callers hear "all lines are busy".
MAX_CONCURRENT_CALLS = _i("MAX_CONCURRENT_CALLS", 6)
MAX_CONCURRENT_WEB_CALLS = _i("MAX_CONCURRENT_WEB_CALLS", 3)
# Browser-call tokens: per-IP budget, and which sites may ask for one
WEB_TOKENS_PER_IP_PER_HOUR = _i("WEB_TOKENS_PER_IP_PER_HOUR", 12)
WEB_ALLOWED_ORIGINS = _list("WEB_ALLOWED_ORIGINS", "https://vedronix.com,https://www.vedronix.com,http://localhost:5173")
SUMMARY_MODEL = _s("SUMMARY_MODEL", "gpt-4o-mini")

# ─────────────────────────── telephony (Twilio) ─────────────────────────────
# The number's Voice URL and the TwiML App (browser calls) both point at /twilio/voice,
# which answers with <Connect><Stream> to /twilio/stream. Outbound calls only through the
# authenticated /v1/calls API (outbound.py) and the allowlisted /admin/test-call.
TWILIO_ACCOUNT_SID = _s("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = _s("TWILIO_AUTH_TOKEN")      # validates X-Twilio-Signature
TWILIO_API_KEY_SID = _s("TWILIO_API_KEY_SID")    # SK…: signs browser access tokens, REST calls
TWILIO_API_KEY_SECRET = _s("TWILIO_API_KEY_SECRET")
TWILIO_TWIML_APP_SID = _s("TWILIO_TWIML_APP_SID")  # AP…: where browser calls go
TWILIO_PHONE_NUMBER = _s("TWILIO_PHONE_NUMBER", "+447427893469")
# Callers outside these countries hear a short notice and the call ends (US customers only)
ALLOWED_CALLER_COUNTRIES = _list("ALLOWED_CALLER_COUNTRIES", "GB")
WEB_TOKEN_TTL_S = _i("WEB_TOKEN_TTL_S", 600)
# Test calls OUT (owner's request, 2026-11-14): only to these numbers, only with ADMIN_API_KEY,
# a few per hour. This is not a dialer: there is no way to call a number not on this list.
ADMIN_API_KEY = _s("ADMIN_API_KEY")
OUTBOUND_TEST_NUMBERS = _list("OUTBOUND_TEST_NUMBERS", "")
OUTBOUND_TEST_PER_HOUR = _i("OUTBOUND_TEST_PER_HOUR", 6)

# Outbound call API (owner's request, 2026-11-20): POST /v1/calls rings US patients for the
# office (reminders, recalls, call-backs). Keys are stored only as sha256 hashes:
#   OUTBOUND_API_KEYS=name:sha256hex,name2:sha256hex      (python -m app.outbound_keys new NAME)
OUTBOUND_API_KEYS = _list("OUTBOUND_API_KEYS", "")
# Signs the result webhooks we POST to a request's callback_url (header X-Vedronix-Signature)
OUTBOUND_WEBHOOK_SECRET = _s("OUTBOUND_WEBHOOK_SECRET")
OUTBOUND_MAX_CONCURRENT = _i("OUTBOUND_MAX_CONCURRENT", 3)       # outbound calls ringing/talking at once
OUTBOUND_INBOUND_RESERVE = _i("OUTBOUND_INBOUND_RESERVE", 2)     # lines always left free for callers
OUTBOUND_REQUESTS_PER_MINUTE = _i("OUTBOUND_REQUESTS_PER_MINUTE", 60)   # per API key
OUTBOUND_CALLS_PER_DAY = _i("OUTBOUND_CALLS_PER_DAY", 500)               # per API key, rolling 24 h
OUTBOUND_PER_NUMBER_PER_DAY = _i("OUTBOUND_PER_NUMBER_PER_DAY", 3)       # calls requested to one number
OUTBOUND_MAX_BATCH = _i("OUTBOUND_MAX_BATCH", 100)
OUTBOUND_RING_TIMEOUT_S = _i("OUTBOUND_RING_TIMEOUT_S", 30)
# TCPA: only between these hours in the called person's local time
OUTBOUND_WINDOW_START = _hhmm("OUTBOUND_WINDOW_START", "08:00")
OUTBOUND_WINDOW_END = _hhmm("OUTBOUND_WINDOW_END", "20:00")
# "Get a call from Ava" on vedronix.com (demo_call.py): the visitor proves the number is theirs
# with a texted code (Twilio Verify service VA…) before Ava calls it.
TWILIO_VERIFY_SERVICE_SID = _s("TWILIO_VERIFY_SERVICE_SID")
DEMO_CALLS_ENABLED = _b("DEMO_CALLS_ENABLED", bool(TWILIO_VERIFY_SERVICE_SID))
DEMO_CALLS_PER_DAY = _i("DEMO_CALLS_PER_DAY", 40)
DEMO_CODES_PER_IP_PER_HOUR = _i("DEMO_CODES_PER_IP_PER_HOUR", 3)

# WhatsApp messages to the caller (owner's request, 2026-11-14), through the same Vedronix
# gateway the Sakshi agent uses (Evolution API underneath). Only to the number that called
# (or the allowlisted test number we rang); never to browser callers, never marketing.
WHATSAPP_API_KEY = _s("WHATSAPP_API_KEY")
WHATSAPP_ENABLED = _b("WHATSAPP_ENABLED", bool(WHATSAPP_API_KEY))
WHATSAPP_API_URL = _s("WHATSAPP_API_URL", "https://whatsappev.vedronix.com/api/v1/messages/text")
WHATSAPP_ASCII_ONLY = _b("WHATSAPP_ASCII_ONLY", True)  # the gateway rejected non-ASCII on the Anya build
WHATSAPP_TIMEOUT_S = _f("WHATSAPP_TIMEOUT_S", 20.0)
WHATSAPP_THANKS = _b("WHATSAPP_THANKS", True)          # "thanks for calling" after every conversation
WHATSAPP_MISSED = _b("WHATSAPP_MISSED", True)          # caller hung up before saying anything
WHATSAPP_REMINDERS = _b("WHATSAPP_REMINDERS", True)    # the day before an appointment
# Calls shorter than this with no words from the caller are treated as misdials: no message.
WHATSAPP_MISSED_MIN_S = _f("WHATSAPP_MISSED_MIN_S", 5.0)
WHATSAPP_FOOTER = _s("WHATSAPP_FOOTER", "(Brightside Dental is a sample office in the Vedronix AI receptionist demo.)")
# Twilio Media Streams are 8 kHz μ-law; the bridge turns them into 16-bit PCM for the pipeline.
TELER_SAMPLE_RATE = _i("TELER_SAMPLE_RATE", 8000)
TELER_CHUNK_SIZE = _i("TELER_CHUNK_SIZE", 500)
TELER_RECORD = _b("TELER_RECORD", False)
# RFC 2586 defines L16 as network byte order (big-endian), but carriers vary and
# getting it wrong turns the call into static. "auto" measures the inbound
# stream and decides; "big" / "little" force it.
# μ-law decoded here is native little-endian PCM: nothing to detect
TELER_ENDIAN = _s("TELER_ENDIAN", "little").lower()
# In auto mode, how long to wait for enough inbound audio to decide before
# speaking the greeting. Keeps the very first words from going out byte-swapped.
ENDIAN_DETECT_TIMEOUT_S = _f("ENDIAN_DETECT_TIMEOUT_S", 0.0)

# Declared in the call flow. FreJun's reference bridge sends the string "8k";
# omitting it entirely lets Teler pick a default that may not match our audio.
TELER_STREAM_SAMPLE_RATE = _s("TELER_STREAM_SAMPLE_RATE", "8k")

# Outbound audio is relayed in buffered chunks, not paced frame by frame —
# Teler runs its own jitter buffer and pacing on top of it starves that buffer.
# Every outbound message is an exact whole number of chunk_size frames — the
# size we declare in the call flow. A ragged tail on each message leaves Teler
# a partial frame to deal with, and a partial frame is a click. This is how
# many frames go in one message (5 x 400 B = 2000 B = 125 ms @ 8 kHz), matching
# the batch-of-5 in FreJun's reference bridge.
PLAYOUT_FRAMES_PER_MESSAGE = _i("PLAYOUT_FRAMES_PER_MESSAGE", 5)
PLAYOUT_FLUSH_BYTES = _i("PLAYOUT_FLUSH_BYTES", 4000)
# Larger cushion for the FIRST flush of an utterance. We synthesise clause by
# clause for latency, which means a short clause can finish playing before the
# next one is ready — an audible gap. Building this much of a head start first
# means the carrier is always playing from a queue (9600 B = 600 ms @ 8 kHz).
PLAYOUT_PRIME_BYTES = _i("PLAYOUT_PRIME_BYTES", 9600)
# ...or once a partial buffer has waited this long, so a short trailing clause
# still goes out promptly.
PLAYOUT_MAX_BUFFER_MS = _i("PLAYOUT_MAX_BUFFER_MS", 120)
# How far ahead of real time the CARRIER is allowed to get.
#
# Without this we hand Teler a whole reply within a couple of seconds, so an
# interruption can only be honoured if Teler acts on our `clear` — and if it
# does not, the caller hears the rest of the sentence no matter how fast we
# detected them. Holding the surplus in our own buffer makes an interrupt
# instant for everything past this window.
#
# 900 ms sits well clear of the 250 ms chunk and the 600 ms prime cushion, so
# it cannot bring back the choppiness that frame-by-frame pacing caused.
PLAYOUT_MAX_LOOKAHEAD_MS = _i("PLAYOUT_MAX_LOOKAHEAD_MS", 900)


# ─────────────────────────── Deepgram (STT) ─────────────────────────────────
DEEPGRAM_API_KEY = _s("DEEPGRAM_API_KEY")
DEEPGRAM_URL = _s("DEEPGRAM_URL", "wss://api.deepgram.com/v1/listen")
# nova-3 + language=multi handles Hinglish code-switching. Use "hi" for pure Hindi.
DEEPGRAM_MODEL = _s("DEEPGRAM_MODEL", "nova-3")
DEEPGRAM_LANGUAGE = _s("DEEPGRAM_LANGUAGE", "en-GB")
DEEPGRAM_KEYTERMS = _pipe("DEEPGRAM_KEYTERMS", "Brightside|Brightside Dental|Delta Dental|MetLife|Cigna|Aetna|Guardian|Invisalign|CareCredit|HSA|FSA")
DEEPGRAM_ENDPOINTING_MS = _i("DEEPGRAM_ENDPOINTING_MS", 300)
DEEPGRAM_UTTERANCE_END_MS = _i("DEEPGRAM_UTTERANCE_END_MS", 1000)
DEEPGRAM_KEEPALIVE_S = _f("DEEPGRAM_KEEPALIVE_S", 5.0)
DEEPGRAM_SMART_FORMAT = _b("DEEPGRAM_SMART_FORMAT", True)


# ─────────────────────────── Sarvam (TTS) ───────────────────────────────────
SARVAM_API_KEY = _s("SARVAM_API_KEY")
SARVAM_WS_URL = _s("SARVAM_WS_URL", "wss://api.sarvam.ai/text-to-speech/ws")
SARVAM_MODEL = _s("SARVAM_MODEL", "bulbul:v3")
SARVAM_VOICE = _s("SARVAM_VOICE", "priya")
SARVAM_LANGUAGE = _s("SARVAM_LANGUAGE", "en-IN")  # fallback voice speaks English
SARVAM_SPEED = _f("SARVAM_SPEED", 1.0)  # bulbul:v3 pace range is 0.5 – 2.0
# Expressiveness. Unset (None) gave a flat read that callers called "basic";
# bulbul:v3 accepted 0.9 on 2026-10-28. Set blank to go back to Sarvam's default.
_sarvam_temp = os.getenv("SARVAM_TEMPERATURE", "0.8").strip()
SARVAM_TEMPERATURE = float(_sarvam_temp) if _sarvam_temp else None
# Lets Sarvam normalise mixed Hinglish ("QR", "UPI") before speaking it.
SARVAM_PREPROCESS = _b("SARVAM_PREPROCESS", True)
# linear16 @ 8000 Hz drops straight onto the wire with zero resampling.
SARVAM_CODEC = _s("SARVAM_CODEC", "linear16")
SARVAM_SAMPLE_RATE = _i("SARVAM_SAMPLE_RATE", 8000)
SARVAM_MIN_BUFFER = _i("SARVAM_MIN_BUFFER", 50)  # 50 is the value Sarvam documents
SARVAM_MAX_CHUNK_LEN = _i("SARVAM_MAX_CHUNK_LEN", 150)
SARVAM_CONNECT_TIMEOUT_S = _f("SARVAM_CONNECT_TIMEOUT_S", 4.0)
SARVAM_FIRST_CHUNK_TIMEOUT_S = _f("SARVAM_FIRST_CHUNK_TIMEOUT_S", 6.0)
# Silence gap after audio has started that we treat as end-of-utterance. Keeps
# us independent of the completion event.
SARVAM_IDLE_END_S = _f("SARVAM_IDLE_END_S", 2.5)
# Ask for the completion event. ON: without it we can only guess that synthesis
# has finished by waiting for a silence gap, and any audio Sarvam sends after
# that guess stays in the socket and gets read as the beginning of the NEXT
# reply — the caller hears sentences spliced together and truncated. FreJun's
# own reference bridge sets this too.
SARVAM_COMPLETION_EVENT = _b("SARVAM_COMPLETION_EVENT", True)
# Log every JSON frame sent to / received from Sarvam. Turn this on the moment
# you see a 4xx from them — it prints the exact payload they rejected.
SARVAM_WIRE_LOG = _b("SARVAM_WIRE_LOG", False)
# Small pause after the config frame so the server has applied it before text
# arrives. Sending text in the same tick can race the config handler.
SARVAM_CONFIG_SETTLE_S = _f("SARVAM_CONFIG_SETTLE_S", 0.12)


# ─────────────────────────── ElevenLabs (TTS) ───────────────────────────────
# Which TTS vendor drives the call: "elevenlabs" or "sarvam".
# Owner, 2026-11-14: "real person voice": ElevenLabs is the most human-sounding voice we have
# (premade voices only on the current plan); Sarvam (English) takes over if ElevenLabs fails,
# e.g. when the monthly character quota runs out. Deepgram Aura-2 remains available.
TTS_PROVIDER = _s("TTS_PROVIDER", "elevenlabs").lower()
TTS_FALLBACK_PROVIDER = _s("TTS_FALLBACK_PROVIDER", "sarvam").lower()
# Deepgram Aura-2: American English voices, telephone-rate audio straight from the API.
# Measured 2026-11-14 from this server: ~340 ms to first audio on a kept-open connection,
# synthesis about 2x faster than real time. Thalia: clear, confident, natural (female).
DEEPGRAM_TTS_VOICE = _s("DEEPGRAM_TTS_VOICE", "aura-2-thalia-en")
DEEPGRAM_TTS_URL = _s("DEEPGRAM_TTS_URL", "wss://api.deepgram.com/v1/speak")

ELEVEN_API_KEY = _s("ELEVENLABS_API_KEY") or _s("ELEVEN_API_KEY")
ELEVEN_WS_BASE = _s("ELEVEN_WS_BASE", "wss://api.elevenlabs.io/v1/text-to-speech")
# Flash v2.5 is their low-latency model (~75 ms) and supports Hindi.
ELEVEN_MODEL = _s("ELEVEN_MODEL", "eleven_flash_v2_5")
# Pick a voice from your Voice Library and put its ID here. The default is the
# public "Alice" voice from their docs — replace it with a male Hindi voice.
ELEVEN_VOICE_ID = _s("ELEVEN_VOICE_ID", "EXAVITQu4vr4xnSDxMaL")  # Sarah: warm, professional, American
ELEVEN_LANGUAGE = _s("ELEVEN_LANGUAGE", "en")  # ISO-639-1; blank to auto-detect
# ElevenLabs has no pcm_8000 for TTS. ulaw_8000 is their telephony format and is
# exactly Teler's rate, so it needs no resampling — only a G.711 table lookup.
ELEVEN_OUTPUT_FORMAT = _s("ELEVEN_OUTPUT_FORMAT", "ulaw_8000")
ELEVEN_STABILITY = _f("ELEVEN_STABILITY", 0.5)
ELEVEN_SIMILARITY = _f("ELEVEN_SIMILARITY", 0.8)
ELEVEN_SPEED = _f("ELEVEN_SPEED", 1.0)
ELEVEN_CONNECT_TIMEOUT_S = _f("ELEVEN_CONNECT_TIMEOUT_S", 5.0)
ELEVEN_FIRST_CHUNK_TIMEOUT_S = _f("ELEVEN_FIRST_CHUNK_TIMEOUT_S", 6.0)
# Only a safety net now; replies normally end on alignment (tts_elevenlabs.py).
# At 0.6 s it was the primary end signal and could clip a reply mid-sentence.
ELEVEN_IDLE_END_S = _f("ELEVEN_IDLE_END_S", 1.5)
ELEVEN_ALIGN_GRACE_S = _f("ELEVEN_ALIGN_GRACE_S", 0.15)
ELEVEN_KEEPALIVE_S = _f("ELEVEN_KEEPALIVE_S", 15.0)
ELEVEN_INACTIVITY_TIMEOUT_S = _i("ELEVEN_INACTIVITY_TIMEOUT_S", 60)
ELEVEN_WIRE_LOG = _b("ELEVEN_WIRE_LOG", False)


def _int_list(name: str, default: list[int]) -> list[int]:
    raw = _s(name)
    if not raw:
        return default
    try:
        return [int(x) for x in raw.replace(" ", "").split(",") if x]
    except ValueError:
        return default


# Characters buffered before they start generating. Lower first value than their
# default [120,160,250,290] so short replies start sooner.
ELEVEN_CHUNK_SCHEDULE = _int_list("ELEVEN_CHUNK_SCHEDULE", [50, 120, 160, 290])


# ─────────────────────────── OpenAI (LLM + fallback TTS) ────────────────────
OPENAI_API_KEY = _s("OPENAI_API_KEY")
OPENAI_BASE_URL = _s("OPENAI_BASE_URL", "https://api.openai.com/v1")
OPENAI_MODEL = _s("OPENAI_MODEL", "gpt-4o-mini")
OPENAI_TEMPERATURE = _f("OPENAI_TEMPERATURE", 0.5)  # 0.3 sounded scripted on the first live call
OPENAI_MAX_TOKENS = _i("OPENAI_MAX_TOKENS", 300)

TTS_FALLBACK_ENABLED = _b("TTS_FALLBACK_ENABLED", True)
# With the fallback off, how often a failing voice vendor is re-tried (tts_health).
TTS_RECHECK_S = _f("TTS_RECHECK_S", 600.0)
OPENAI_TTS_MODEL = _s("OPENAI_TTS_MODEL", "gpt-4o-mini-tts")
OPENAI_TTS_VOICE = _s("OPENAI_TTS_VOICE", "coral")
OPENAI_TTS_SAMPLE_RATE = 24000  # OpenAI "pcm" output is always 24 kHz s16le mono


# ─────────────────────────── VAD (Silero) ───────────────────────────────────
# Silero v5 accepts exactly 256 samples at 8 kHz (= 32 ms, = 512 bytes).
VAD_ENABLED = _b("VAD_ENABLED", True)
VAD_FRAME_SAMPLES = 256
VAD_SAMPLE_RATE = 8000

# Probability above which a frame counts as speech.
VAD_THRESHOLD = _f("VAD_THRESHOLD", 0.55)
# While the bot is talking we get line echo + room noise, so we demand more.
VAD_THRESHOLD_WHILE_SPEAKING = _f("VAD_THRESHOLD_WHILE_SPEAKING", 0.72)

# How much *continuous* speech before we believe a human really started.
VAD_MIN_SPEECH_MS = _i("VAD_MIN_SPEECH_MS", 130)
VAD_MIN_SPEECH_MS_WHILE_SPEAKING = _i("VAD_MIN_SPEECH_MS_WHILE_SPEAKING", 260)
# Silence needed before we call the turn finished.
VAD_MIN_SILENCE_MS = _i("VAD_MIN_SILENCE_MS", 500)

# Energy gate: rejects steady background hum that Silero sometimes scores high.
VAD_USE_ENERGY_GATE = _b("VAD_USE_ENERGY_GATE", True)
VAD_ENERGY_MARGIN_DB = _f("VAD_ENERGY_MARGIN_DB", 8.0)  # dB above the noise floor
VAD_NOISE_FLOOR_INIT_DB = _f("VAD_NOISE_FLOOR_INIT_DB", -50.0)
VAD_NOISE_FLOOR_ALPHA = _f("VAD_NOISE_FLOOR_ALPHA", 0.02)  # slow adaptation

# Barge-in also needs the ASR to agree that words were said, not just noise.
BARGE_IN_ENABLED = _b("BARGE_IN_ENABLED", True)
BARGE_IN_REQUIRE_ASR = _b("BARGE_IN_REQUIRE_ASR", True)
BARGE_IN_MIN_CHARS = _i("BARGE_IN_MIN_CHARS", 2)
# Ignore anything in the first moments of our own utterance (tail echo).
BARGE_IN_GRACE_MS = _i("BARGE_IN_GRACE_MS", 350)


# ─────────────────────────── conversation ───────────────────────────────────
GREETING_DELAY_MS = _i("GREETING_DELAY_MS", 250)
# How long to wait for Teler's start frame (which tells us inbound/outbound and
# who the lead is) before greeting with the neutral line anyway.
START_FRAME_WAIT_S = _f("START_FRAME_WAIT_S", 1.5)
SILENCE_REPROMPT_S = _f("SILENCE_REPROMPT_S", 8.0)
MAX_SILENCE_REPROMPTS = _i("MAX_SILENCE_REPROMPTS", 2)
GOODBYE_HANGUP_DELAY_S = _f("GOODBYE_HANGUP_DELAY_S", 1.2)
# Carrier announcements and voicemail are recognised from the transcript only
# in the opening seconds; after that a human is on the line.
MACHINE_DETECT_WINDOW_S = _f("MACHINE_DETECT_WINDOW_S", 20.0)

# Model used for the post-call analysis (structured JSON, not latency bound).
ANALYSIS_MODEL = _s("ANALYSIS_MODEL", "") or None


def missing_required() -> list[str]:
    need = [("OPENAI_API_KEY", OPENAI_API_KEY), ("DEEPGRAM_API_KEY", DEEPGRAM_API_KEY), ("APP_SECRET", APP_SECRET),
            ("TWILIO_ACCOUNT_SID", TWILIO_ACCOUNT_SID), ("TWILIO_AUTH_TOKEN", TWILIO_AUTH_TOKEN),
            ("TWILIO_API_KEY_SID", TWILIO_API_KEY_SID), ("TWILIO_API_KEY_SECRET", TWILIO_API_KEY_SECRET),
            ("TWILIO_TWIML_APP_SID", TWILIO_TWIML_APP_SID)]
    return [name for name, value in need if not value]
