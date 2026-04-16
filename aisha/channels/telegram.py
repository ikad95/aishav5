"""Telegram bot channel — long-polling via stdlib HTTP.

Uses the Bot API directly (no SDK). Reads updates with ``getUpdates`` in
long-poll mode, dispatches text messages to :mod:`aisha.core.chat`, and
posts replies with ``sendMessage``.

Set ``TELEGRAM_BOT_TOKEN`` to enable. Optionally restrict with
``TELEGRAM_ALLOWED_CHAT_IDS`` (comma-separated chat IDs).
"""
from __future__ import annotations

import json
import logging
import signal
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from ..core import chat as chat_mod
from ..core.config import settings

log = logging.getLogger(__name__)

_API = "https://api.telegram.org"
_POLL_TIMEOUT = 30  # seconds; Bot API caps at 50
_HTTP_TIMEOUT = _POLL_TIMEOUT + 10
_MAX_MSG = 4096  # Telegram hard limit per message


def _api(method: str, params: dict | None = None) -> dict:
    url = f"{_API}/bot{settings.telegram_bot_token}/{method}"
    data = urllib.parse.urlencode(params or {}).encode("utf-8") if params else None
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    if not body.get("ok"):
        raise RuntimeError(f"telegram {method} failed: {body}")
    return body.get("result", {})


def _send(chat_id: int, text: str, *, reply_to: int | None = None) -> None:
    for chunk in _chunk(text, _MAX_MSG):
        params = {"chat_id": chat_id, "text": chunk}
        if reply_to is not None:
            params["reply_to_message_id"] = reply_to
            reply_to = None  # only anchor the first chunk
        try:
            _api("sendMessage", params)
        except Exception as e:
            log.warning("telegram: send failed (%s)", e)
            break


def _chunk(text: str, n: int) -> list[str]:
    if len(text) <= n:
        return [text]
    out: list[str] = []
    remaining = text
    while len(remaining) > n:
        cut = remaining.rfind("\n", 0, n)
        if cut < n // 2:
            cut = n
        out.append(remaining[:cut])
        remaining = remaining[cut:].lstrip("\n")
    if remaining:
        out.append(remaining)
    return out


def _allowed(chat_id: int) -> bool:
    raw = (settings.telegram_allowed_chat_ids or "").strip()
    if not raw:
        return True
    allowed = {int(x) for x in raw.split(",") if x.strip().lstrip("-").isdigit()}
    return chat_id in allowed


def _process(msg: dict) -> None:
    chat_id = msg["chat"]["id"]
    if not _allowed(chat_id):
        log.info("telegram: ignoring chat_id=%s (not in allowlist)", chat_id)
        return

    text = (msg.get("text") or "").strip()
    if not text:
        return

    if text.startswith("/start"):
        _send(chat_id, "hi. talk to me.")
        return

    user = msg.get("from", {})
    user_id = f"tg:{user.get('id')}"
    display = (user.get("first_name") or user.get("username") or "").strip()
    source = f"telegram:{chat_id}"
    msg_id = msg.get("message_id")

    try:
        reply, _ = chat_mod.send(
            text, source=source, user_id=user_id, display_name=display,
        )
    except Exception as e:
        log.exception("telegram: chat.send failed")
        _send(chat_id, f"sorry — hit an error: {e}", reply_to=msg_id)
        return

    if reply:
        _send(chat_id, reply, reply_to=msg_id)


def run() -> None:
    if not settings.telegram_bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")

    me = _api("getMe")
    log.info("telegram: connected as @%s (id=%s)", me.get("username"), me.get("id"))

    stop = threading.Event()

    def _on_signal(*_):
        log.info("telegram: shutting down")
        stop.set()

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    offset = 0
    pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="tg-worker")
    try:
        while not stop.is_set():
            try:
                updates = _api("getUpdates", {
                    "offset": offset,
                    "timeout": _POLL_TIMEOUT,
                })
            except urllib.error.URLError as e:
                log.warning("telegram: poll error (%s); retrying in 3s", e)
                if stop.wait(3):
                    break
                continue
            except Exception as e:
                log.exception("telegram: unexpected poll error: %s", e)
                if stop.wait(3):
                    break
                continue

            for upd in updates:
                offset = max(offset, upd["update_id"] + 1)
                msg = upd.get("message") or upd.get("edited_message")
                if msg:
                    pool.submit(_process, msg)
    finally:
        try:
            pool.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass
