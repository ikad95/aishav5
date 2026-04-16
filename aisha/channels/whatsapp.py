"""WhatsApp send path via Twilio REST API.

Thin wrapper over the Messages resource — no SDK, just urllib and HTTP Basic
auth. Supports plain-body messages (the normal case) and pre-approved Twilio
content templates (``content_sid`` + ``content_variables``) for regulated
flows.

Secrets live in ``.env`` (``TWILIO_ACCOUNT_SID`` / ``TWILIO_AUTH_TOKEN`` /
``TWILIO_WHATSAPP_FROM``). Nothing is hardcoded in source.
"""
from __future__ import annotations

import base64
import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

from ..core.config import settings

log = logging.getLogger(__name__)

_API_BASE = "https://api.twilio.com/2010-04-01/Accounts"

# Twilio WhatsApp accepts only a specific set of outbound media MIME types.
# Everything else causes an asynchronous ``failed`` (error 63019): Twilio's
# initial POST returns a sid, but WhatsApp never delivers. Keep this list
# in sync with https://www.twilio.com/docs/whatsapp/guardrails.
ALLOWED_MEDIA_MIMES = frozenset({
    "image/jpeg", "image/png",
    "audio/mpeg", "audio/ogg", "audio/amr",
    "video/mp4", "video/3gpp",
    "application/pdf",
    "application/msword",
    "application/vnd.ms-excel",
    "application/vnd.ms-powerpoint",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
})


class WhatsAppError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"[{status}] {message}")
        self.status = status
        self.message = message


def _require_creds() -> tuple[str, str]:
    sid = settings.twilio_account_sid
    token = settings.twilio_auth_token
    if not sid or not token:
        raise WhatsAppError(0, "TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN not set in .env")
    return sid, token


def _normalize_to(to: str) -> str:
    """Accept ``+91...``, ``whatsapp:+91...``, or a bare ``91...`` and produce the
    canonical ``whatsapp:+E.164`` form Twilio requires."""
    t = (to or "").strip()
    if not t:
        raise WhatsAppError(0, "to: empty recipient")
    if t.startswith("whatsapp:"):
        return t
    if t.startswith("+"):
        return f"whatsapp:{t}"
    if t.isdigit():
        return f"whatsapp:+{t}"
    raise WhatsAppError(0, f"to: unrecognized format {t!r}")


def _post(path: str, data: dict, *, timeout: int = 20) -> dict:
    sid, token = _require_creds()
    url = f"{_API_BASE}/{sid}/{path}"
    body = urllib.parse.urlencode(data).encode("utf-8")
    auth = base64.b64encode(f"{sid}:{token}".encode("utf-8")).decode("ascii")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "content-type": "application/x-www-form-urlencoded",
            "authorization": f"Basic {auth}",
            "accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace") if e.fp else str(e)
        log.warning("whatsapp: twilio %d %s", e.code, raw[:500])
        raise WhatsAppError(e.code, raw) from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise WhatsAppError(0, str(e)) from e
    try:
        return json.loads(payload)
    except json.JSONDecodeError as e:
        raise WhatsAppError(0, f"non-JSON response: {payload[:200]}") from e


def send_text(to: str, body: str, *, from_: Optional[str] = None) -> str:
    """Send a free-form WhatsApp message. Returns the Twilio message SID.

    ``to`` may be ``+E.164`` or already-prefixed ``whatsapp:+…``. ``from_``
    defaults to ``TWILIO_WHATSAPP_FROM``.
    """
    if not body or not body.strip():
        raise WhatsAppError(0, "body: empty message")
    data = {
        "From": from_ or settings.twilio_whatsapp_from,
        "To":   _normalize_to(to),
        "Body": body,
    }
    resp = _post("Messages.json", data)
    sid = resp.get("sid") or ""
    log.info("whatsapp: sent to=%s sid=%s chars=%d", data["To"], sid, len(body))
    return sid


def send_media(
    to: str,
    media_url: str,
    body: str = "",
    *,
    from_: Optional[str] = None,
) -> str:
    """Send a media message (image / audio / video / document) by URL.

    WhatsApp Business API delivers media by fetching a public URL — there is
    no "file upload" path. Caller is responsible for hosting ``media_url``
    somewhere Twilio can reach it; see ``whatsapp_listener.register_file``
    which serves locally-generated files through the existing public tunnel.

    ``body`` is an optional caption shown alongside the attachment.
    """
    if not media_url or not media_url.startswith(("http://", "https://")):
        raise WhatsAppError(0, f"media_url: must be http(s), got {media_url!r}")
    data = {
        "From":     from_ or settings.twilio_whatsapp_from,
        "To":       _normalize_to(to),
        "MediaUrl": media_url,
    }
    if body:
        data["Body"] = body
    resp = _post("Messages.json", data)
    sid = resp.get("sid") or ""
    log.info("whatsapp: sent media to=%s url=%s sid=%s", data["To"], media_url, sid)
    return sid


def send_template(
    to: str,
    content_sid: str,
    variables: Optional[dict] = None,
    *,
    from_: Optional[str] = None,
) -> str:
    """Send a Twilio pre-approved content template (HX-prefixed SID).

    Required for initiating a conversation outside the 24-hour session window
    per Meta's policy. Free-form ``send_text`` only works inside that window.
    """
    if not content_sid or not content_sid.startswith("HX"):
        raise WhatsAppError(0, f"content_sid: expected HX... got {content_sid!r}")
    data = {
        "From":      from_ or settings.twilio_whatsapp_from,
        "To":        _normalize_to(to),
        "ContentSid": content_sid,
    }
    if variables:
        data["ContentVariables"] = json.dumps(variables, separators=(",", ":"))
    resp = _post("Messages.json", data)
    sid = resp.get("sid") or ""
    log.info("whatsapp: template=%s sent to=%s sid=%s", content_sid, data["To"], sid)
    return sid
