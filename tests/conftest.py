"""Test setup: no vendors, no network. Env is set before app.config is imported."""
from __future__ import annotations

import os
import sys
import tempfile

os.environ.update({
    "IS_LOGGING": "false",
    "VAD_ENABLED": "false",
    "OPENAI_API_KEY": "sk-test",
    "DEEPGRAM_API_KEY": "dg-test",
    "PUBLIC_HOST": "us-agent.example.com",
    "APP_SECRET": "app-test-secret",
    "TWILIO_ACCOUNT_SID": "AC00000000000000000000000000000000",
    "TWILIO_AUTH_TOKEN": "auth-test-token",
    "TWILIO_API_KEY_SID": "SK00000000000000000000000000000000",
    "TWILIO_API_KEY_SECRET": "sk-secret-test",
    "TWILIO_TWIML_APP_SID": "AP00000000000000000000000000000000",
    "WEB_ALLOWED_ORIGINS": "https://vedronix.com,http://localhost:5173",
    "WEB_TOKENS_PER_IP_PER_HOUR": "3",
    "MAX_CONCURRENT_CALLS": "2",
    "MAX_CONCURRENT_WEB_CALLS": "1",
    "LOG_DIR": tempfile.mkdtemp(prefix="us-agent-test-logs-"),
    "DATA_DIR": tempfile.mkdtemp(prefix="us-agent-test-data-"),
    "GOODBYE_HANGUP_DELAY_S": "0",
    "TTS_FALLBACK_ENABLED": "false",
    # outbound API: two keys ("office" = vdx_live_test_office, "other" = vdx_live_test_other)
    "OUTBOUND_API_KEYS": "office:7e6a0e05224a63e73f97f1a72aa8bac7f0e38f57ec08bd398ce682f0b07314a4,other:386e292d66f51875d4b11267fe89757d2e745ee9d30e34b4e16510ba06591ef1",
    "OUTBOUND_WEBHOOK_SECRET": "whsec-test",
    "OUTBOUND_REQUESTS_PER_MINUTE": "1000",
    "OUTBOUND_INBOUND_RESERVE": "0",
})
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
