"""Voice health, reduced to no-ops for the Vedronix test agent.

The Sakshi agent stops all calling when its voice vendor fails (owner's rule there). A test
agent instead falls back to OpenAI's voice for that call (TTS_FALLBACK_ENABLED), so there is
nothing to track. Kept so tts.py stays a straight copy of the tuned original.
"""


def usable() -> bool:
    return True


def report_ok() -> None:
    pass


def report_failure(exc: Exception) -> None:
    pass
