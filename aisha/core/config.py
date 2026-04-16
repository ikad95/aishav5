"""Lightweight settings loaded from environment / .env."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "data"
MD_DIR = ROOT / "md"
LOGS_DIR = ROOT / "logs"
MIGRATIONS_DIR = DATA_DIR / "migrations"

DB_PATH = DATA_DIR / "aisha.db"
DUMP_PATH = DATA_DIR / "dump.sql"
CHROMA_DIR = DATA_DIR / "chroma"


class Settings:
    completion_proxy_url: str = os.getenv("COMPLETION_PROXY_URL", "http://127.0.0.1:9878")
    completion_proxy_timeout: int = int(os.getenv("COMPLETION_PROXY_TIMEOUT", "300"))
    completion_proxy_retries: int = int(os.getenv("COMPLETION_PROXY_RETRIES", "3"))
    anthropic_api_key: Optional[str] = os.getenv("ANTHROPIC_API_KEY")
    model: str = os.getenv("AISHA_MODEL", "claude-sonnet-4-5")

    slack_app_token: Optional[str] = os.getenv("SLACK_APP_TOKEN")
    slack_bot_token: Optional[str] = os.getenv("SLACK_BOT_TOKEN")

    twilio_account_sid: Optional[str] = os.getenv("TWILIO_ACCOUNT_SID")
    twilio_auth_token: Optional[str] = os.getenv("TWILIO_AUTH_TOKEN")
    twilio_whatsapp_from: str = os.getenv("TWILIO_WHATSAPP_FROM", "whatsapp:+14155238886")
    twilio_whatsapp_default_to: Optional[str] = os.getenv("TWILIO_WHATSAPP_DEFAULT_TO")
    whatsapp_listener_port: int = int(os.getenv("WHATSAPP_LISTENER_PORT", "9879"))
    whatsapp_public_url: Optional[str] = os.getenv("WHATSAPP_PUBLIC_URL")
    whatsapp_verify_signature: bool = os.getenv("WHATSAPP_VERIFY_SIGNATURE", "1") != "0"

    telegram_bot_token: Optional[str] = os.getenv("TELEGRAM_BOT_TOKEN")
    telegram_allowed_chat_ids: Optional[str] = os.getenv("TELEGRAM_ALLOWED_CHAT_IDS")

    embedding_model: str = os.getenv("AISHA_EMBEDDING_MODEL", "all-MiniLM-L6-v2")

    dump_interval_seconds: int = int(os.getenv("AISHA_DUMP_INTERVAL", "1800"))
    max_context_turns: int = int(os.getenv("AISHA_MAX_CONTEXT_TURNS", "40"))

    log_level: str = os.getenv("AISHA_LOG_LEVEL", "INFO")

    narrator_enabled: bool = os.getenv("AISHA_NARRATOR", "0") != "0"
    mistral_api_key: Optional[str] = os.getenv("MISTRAL_API_KEY")
    mistral_model: str = os.getenv("MISTRAL_MODEL", "mistral-small-latest")
    mistral_timeout: int = int(os.getenv("MISTRAL_TIMEOUT", "30"))

    progress_pings_enabled: bool = os.getenv("AISHA_PROGRESS_PINGS", "0") != "0"
    progress_ping_interval: int = int(os.getenv("AISHA_PROGRESS_INTERVAL", "60"))


settings = Settings()

for d in (DATA_DIR, LOGS_DIR, CHROMA_DIR):
    d.mkdir(parents=True, exist_ok=True)
