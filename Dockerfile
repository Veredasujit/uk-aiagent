# syntax=docker/dockerfile:1

FROM python:3.11-slim

# ffmpeg and the audio libs are needed by the stack; curl is for the
# healthcheck; tini so signals reach uvicorn and the container stops in a
# second rather than waiting out Docker's ten-second kill timer.
RUN apt-get update && apt-get install -y --no-install-recommends \
      ffmpeg \
      libgl1 \
      portaudio19-dev \
      libasound2-dev \
      curl \
      tini \
 && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Requirements first, so a code change does not reinstall torch every build.
# That layer is the slow one — several hundred megabytes.
COPY requirements.txt .
# CPU-only torch first: the default wheel pulls >1 GB of CUDA libraries this server can't use
# (Silero only needs torch's tensor API, in onnx mode).
RUN pip install --upgrade pip \
 && pip install --index-url https://download.pytorch.org/whl/cpu "torch>=2.2.0" \
 && pip install -r requirements.txt

COPY app ./app

# Silero is downloaded on first use. Fetching it at build time means the first
# call of the day is not the one that pays for it.
RUN python -c "from silero_vad import load_silero_vad; load_silero_vad(onnx=True)" || \
    echo "VAD preload skipped; it will download on first boot"

# Non-root. If the process is compromised it should not also own the
# filesystem it sits on.
RUN useradd -u 1001 -m agent && mkdir -p /app/logs /app/data && chown -R agent:agent /app
USER agent

EXPOSE 8823

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8823/health >/dev/null || exit 1

ENTRYPOINT ["/usr/bin/tini", "--"]
# One worker only (Vedronix US agent): the busy-line limit and live transcripts are in-process.
# Each call holds websockets to Twilio, Deepgram and the TTS
# vendor plus an asyncio playout clock; more workers only fragment that state.
CMD ["uvicorn", "app.server:app", "--host", "0.0.0.0", "--port", "8823", "--workers", "1"]