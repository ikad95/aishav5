"""Entry point: ``python -m aisha``."""
from __future__ import annotations

import argparse
import logging
import sys
from logging.handlers import TimedRotatingFileHandler

from .core.config import LOGS_DIR, settings


_NOISY_LOGGERS = (
    "httpx",
    "httpcore",
    "urllib3",
    "websocket",
    "sentence_transformers",
    "huggingface_hub",
    "transformers",
    "filelock",
)


def _setup_logging() -> None:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    main_file = TimedRotatingFileHandler(
        LOGS_DIR / "aisha.log",
        when="midnight", interval=1, backupCount=7, encoding="utf-8",
    )
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[logging.StreamHandler(sys.stderr), main_file],
    )
    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)

    conv_handler = TimedRotatingFileHandler(
        LOGS_DIR / "conversations.log",
        when="midnight", interval=1, backupCount=7, encoding="utf-8",
    )
    conv_handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    conv = logging.getLogger("aisha.conversation")
    conv.setLevel(logging.INFO)
    conv.addHandler(conv_handler)
    conv.propagate = False


def main() -> int:
    parser = argparse.ArgumentParser(prog="aisha")
    parser.add_argument("--slack", action="store_true", help="run the Slack listener")
    parser.add_argument("--whatsapp", action="store_true", help="run the WhatsApp webhook listener")
    parser.add_argument("--telegram", action="store_true", help="run the Telegram bot (polling)")
    parser.add_argument("--debug", action="store_true", help="verbose logging")
    args = parser.parse_args()

    if args.debug:
        settings.log_level = "DEBUG"
    _setup_logging()

    from .core import store
    store.connect()

    if args.slack:
        from .channels import slack
        slack.run()
        return 0

    if args.whatsapp:
        from .channels import whatsapp_listener
        whatsapp_listener.run()
        return 0

    if args.telegram:
        from .channels import telegram
        telegram.run()
        return 0

    from .core import chat
    chat.repl()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
