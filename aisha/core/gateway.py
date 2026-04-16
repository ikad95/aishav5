"""Gateway to Claude.

Two modes, picked automatically:

1. **Direct** — if ``ANTHROPIC_API_KEY`` is set, call ``api.anthropic.com``
   directly. The simplest path; no extra processes to run.
2. **Proxy** — if ``COMPLETION_PROXY_URL`` is set, POST to that URL instead.
   Useful when a local proxy adds caching, auth rewriting, or model routing.

Direct mode wins if both are set. Every call returns the raw Anthropic
response dict so ``chat.py``'s tool-use loop can handle ``stop_reason ==
"tool_use"``.
"""
from __future__ import annotations

import base64
import json
import logging
import random
import time
from pathlib import Path
from typing import Optional, Union
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .config import settings

log = logging.getLogger(__name__)

_ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
_ANTHROPIC_VERSION = "2023-06-01"

def _narrate_retry(status: int, attempt: int) -> None:
    # Lazy import to avoid circular dep: narrator → config → settings → gateway.
    try:
        from . import narrator
        narrator.narrate("gateway_retry", status=status, attempt=attempt)
    except Exception:
        pass


# HTTP statuses that indicate an upstream/gateway failure we should retry.
# 502 Bad Gateway — the proxy's upstream (Anthropic) was unreachable.
# 503 Service Unavailable — transient overload.
# 504 Gateway Timeout — upstream took too long.
# 520-524 — Cloudflare-class edge errors.
_RETRIABLE_STATUSES = frozenset({502, 503, 504, 520, 521, 522, 523, 524})

UserMessage = Union[str, list[dict]]


class GatewayError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"[{status}] {message}")
        self.status = status
        self.message = message


def complete_with_tools(
    system_prompt: str,
    messages: list[dict],
    *,
    tools: Optional[list[dict]] = None,
    model: Optional[str] = None,
    max_tokens: int = 16000,
    timeout: Optional[int] = None,
) -> dict:
    """Call Claude with the full Anthropic payload. Returns the raw response dict.

    Used by the tool-use loop in ``chat.py`` — caller is responsible for
    interpreting ``stop_reason`` and appending tool_use / tool_result blocks.
    """
    payload_d: dict = {
        "model": model or settings.model,
        "max_tokens": max_tokens,
        "system": system_prompt,
        "messages": messages,
    }
    if tools:
        payload_d["tools"] = tools
    payload = json.dumps(payload_d).encode("utf-8")

    if settings.anthropic_api_key:
        url = _ANTHROPIC_URL
        headers = {
            "content-type": "application/json",
            "x-api-key": settings.anthropic_api_key,
            "anthropic-version": _ANTHROPIC_VERSION,
        }
    elif settings.completion_proxy_url:
        url = f"{settings.completion_proxy_url.rstrip('/')}/v1/messages"
        headers = {"content-type": "application/json"}
    else:
        raise GatewayError(
            0,
            "No Claude credentials configured. Set ANTHROPIC_API_KEY in .env "
            "(or COMPLETION_PROXY_URL if you're running a proxy).",
        )

    req = Request(url, data=payload, headers=headers, method="POST")
    t = timeout if timeout is not None else settings.completion_proxy_timeout
    # Retry transient upstream failures with exponential backoff + jitter.
    # These requests are non-streaming and non-mutating on the server side, so
    # re-POSTing the same payload is safe when the first attempt dropped before
    # we got any bytes back.
    attempts = max(1, settings.completion_proxy_retries + 1)
    last_exc: Optional[Exception] = None
    for attempt in range(attempts):
        try:
            with urlopen(req, timeout=t) as resp:
                body = resp.read().decode("utf-8")
            return json.loads(body)
        except HTTPError as e:
            msg = e.read().decode("utf-8", errors="replace") if e.fp else str(e)
            if e.code in _RETRIABLE_STATUSES and attempt < attempts - 1:
                delay = 0.5 * (2 ** attempt) + random.uniform(0, 0.25)
                log.warning("gateway: HTTP %d on attempt %d/%d — retrying in %.2fs: %s",
                            e.code, attempt + 1, attempts, delay, msg[:200])
                _narrate_retry(e.code, attempt + 1)
                time.sleep(delay)
                last_exc = e
                continue
            raise GatewayError(e.code, msg) from e
        except (URLError, TimeoutError, OSError) as e:
            if attempt < attempts - 1:
                delay = 0.5 * (2 ** attempt) + random.uniform(0, 0.25)
                log.warning("gateway: connection error on attempt %d/%d — retrying in %.2fs: %s",
                            attempt + 1, attempts, delay, e)
                _narrate_retry(0, attempt + 1)
                time.sleep(delay)
                last_exc = e
                continue
            raise GatewayError(0, str(e)) from e
    # Defensive: loop always returns or raises, but mypy/readers appreciate it.
    raise GatewayError(0, f"retries exhausted: {last_exc}")


# ── Vision helpers ───────────────────────────────────────────────────

def image_block(path: str, mime: Optional[str] = None) -> dict:
    data = Path(path).read_bytes()
    if not mime:
        ext = Path(path).suffix.lower()
        mime = {
            ".png":  "image/png",
            ".jpg":  "image/jpeg",
            ".jpeg": "image/jpeg",
            ".gif":  "image/gif",
            ".webp": "image/webp",
        }.get(ext, "image/jpeg")
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": mime,
            "data": base64.standard_b64encode(data).decode("ascii"),
        },
    }


def text_block(text: str) -> dict:
    return {"type": "text", "text": text}


def build_vision_message(text: str, attachments: list[dict]) -> list[dict]:
    blocks: list[dict] = []
    for att in attachments:
        try:
            blocks.append(image_block(att["path"], att.get("mime")))
        except Exception as e:
            log.warning("vision: failed to attach %s (%s)", att.get("name"), e)
    if text:
        blocks.append(text_block(text))
    return blocks
