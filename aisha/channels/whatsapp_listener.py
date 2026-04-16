"""WhatsApp webhook listener — stdlib HTTP server.

Twilio POSTs inbound messages (and status callbacks) to a public URL you
register in the Twilio console. This module exposes that endpoint locally
on ``WHATSAPP_LISTENER_PORT`` (default 9879). Put ngrok / cloudflared /
caddy in front of it to get a public URL.

Security:
- Every request is signature-verified against ``TWILIO_AUTH_TOKEN`` using
  Twilio's HMAC-SHA1 scheme. Disable only for local dev via
  ``WHATSAPP_VERIFY_SIGNATURE=0``.
- Only ``/whatsapp/incoming`` responds; everything else is 404.
- The 200 is returned immediately — actual chat.send runs in a worker
  pool so Twilio doesn't time out (their soft cap is ~10s).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import mimetypes
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional

from ..core import chat as chat_mod
from ..core import memory
from . import whatsapp as wa
from ..core.config import DATA_DIR, settings

log = logging.getLogger(__name__)


# ── Signature verification (Twilio's HMAC-SHA1 over URL + sorted params) ─

def _expected_signature(url: str, params: dict[str, str], auth_token: str) -> str:
    # Twilio signs: url + concat of (k1+v1)(k2+v2)... with keys sorted.
    base = url
    for k in sorted(params.keys()):
        base += k + params[k]
    mac = hmac.new(auth_token.encode("utf-8"), base.encode("utf-8"), hashlib.sha1)
    return base64.b64encode(mac.digest()).decode("ascii")


def _verify(request_url: str, params: dict[str, str], signature_header: str) -> bool:
    token = settings.twilio_auth_token or ""
    if not token:
        return False
    expected = _expected_signature(request_url, params, token)
    return hmac.compare_digest(expected, signature_header or "")


# ── File registry (so outbound tools can hand Twilio a URL to fetch) ─────
#
# Tokens live in SQLite (memory.kv, namespace=`wa_files`) so the sending
# process (Slack daemon / REPL) and the fetching process (wa-daemon) don't
# need to share memory. Files are served read-only, time-bounded, and only
# via unguessable tokens. Paths must resolve under ``WA_MEDIA_DIR``.

WA_MEDIA_DIR = DATA_DIR / "wa_media"
WA_MEDIA_DIR.mkdir(parents=True, exist_ok=True)
WA_INBOUND_DIR = DATA_DIR / "wa_inbound"
WA_INBOUND_DIR.mkdir(parents=True, exist_ok=True)
_FILE_TTL_SECONDS = 1800  # 30 min; Twilio fetches within seconds typically.

# Claude's vision-enabled content types. Anything else is acknowledged but
# not forwarded to the model — returning a polite "not supported" reply.
_VISION_MIMES = frozenset({"image/jpeg", "image/png", "image/gif", "image/webp"})


def register_file(path: Path | str, *, ttl: int = _FILE_TTL_SECONDS, public_url: Optional[str] = None) -> str:
    """Make a local file available at ``<public_url>/files/<token>.<ext>``.

    Copies (or symlinks via rename-into-place) the file into ``WA_MEDIA_DIR``
    so the GET handler's path-whitelist invariant holds. Returns the full
    public URL suitable for Twilio's ``MediaUrl`` param.
    """
    src = Path(path)
    if not src.is_file():
        raise wa.WhatsAppError(0, f"register_file: not a file: {src}")
    token = secrets.token_urlsafe(24)
    ext = src.suffix or ""
    dst = WA_MEDIA_DIR / f"{token}{ext}"
    # Copy (not move) — the generator may still want the original for Slack/local.
    dst.write_bytes(src.read_bytes())
    mime, _ = mimetypes.guess_type(str(src))
    if not mime:
        mime = "application/octet-stream"
    memory.kv_set("wa_files", token, {
        "path": str(dst),
        "mime": mime,
        "expires_at": time.time() + ttl,
    })
    base = (public_url or settings.whatsapp_public_url or "").rstrip("/")
    if not base:
        raise wa.WhatsAppError(0, "register_file: WHATSAPP_PUBLIC_URL not set; cannot build URL")
    return f"{base}/files/{token}{ext}"


def _purge_expired_files() -> None:
    """Delete expired media files. Called on GET; best-effort, path-guarded.

    Only unlinks files whose real path resolves under ``WA_MEDIA_DIR`` — defends
    against a poisoned kv entry (e.g. a test fixture or a future bug) pointing
    the purge at a path outside our own media root.
    """
    now = time.time()
    allowed_root = os.path.realpath(WA_MEDIA_DIR) + os.sep
    for token, entry in list(memory.kv_all("wa_files").items()):
        if not isinstance(entry, dict):
            continue
        if now <= entry.get("expires_at", 0):
            continue
        p = entry.get("path", "")
        if p and os.path.realpath(p).startswith(allowed_root):
            try:
                Path(p).unlink(missing_ok=True)
            except OSError:
                log.exception("wa_files: failed to unlink %s", p)
        # Mark entry dead regardless, so we don't retry forever.
        memory.kv_set("wa_files", token, None)


# ── Handler ──────────────────────────────────────────────────────────────

class _WhatsAppHandler(BaseHTTPRequestHandler):
    # Attached by WhatsAppListener.run() before serve_forever.
    executor: ThreadPoolExecutor = None  # type: ignore[assignment]
    public_url_prefix: str = ""

    def log_message(self, fmt: str, *args) -> None:  # noqa: D401  (silence default stderr spam)
        log.debug("whatsapp http: " + fmt, *args)

    def _reply(self, status: int, body: str = "", content_type: str = "text/plain") -> None:
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", content_type)
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        # Only `/files/<token>[.<ext>]` is exposed. Everything else is 404.
        if not self.path.startswith("/files/"):
            return self._reply(404, "not found")
        tail = self.path[len("/files/"):]
        # token is everything before the first '.' (extensions don't appear in urlsafe b64).
        token = tail.split(".", 1)[0]
        if not token:
            return self._reply(404, "not found")
        _purge_expired_files()
        entry = memory.kv_get("wa_files", token)
        if not isinstance(entry, dict):
            return self._reply(404, "not found")
        if time.time() > entry.get("expires_at", 0):
            return self._reply(410, "gone")
        # Path whitelist — must resolve under WA_MEDIA_DIR, no traversal tricks.
        allowed_root = os.path.realpath(WA_MEDIA_DIR)
        real = os.path.realpath(entry.get("path", ""))
        if not real.startswith(allowed_root + os.sep):
            log.warning("wa_files: path outside media root token=%s path=%s", token, real)
            return self._reply(403, "forbidden")
        if not os.path.isfile(real):
            return self._reply(404, "not found")
        try:
            data = Path(real).read_bytes()
        except OSError as e:
            log.exception("wa_files: read failed token=%s: %s", token, e)
            return self._reply(500, "read failed")
        mime = entry.get("mime") or "application/octet-stream"
        self.send_response(200)
        self.send_header("content-type", mime)
        self.send_header("content-length", str(len(data)))
        self.send_header("cache-control", "private, max-age=60")
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:  # noqa: N802  (BaseHTTPRequestHandler API)
        if self.path != "/whatsapp/incoming":
            return self._reply(404, "not found")

        length = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(length) if length else b""
        params = {k: v[0] for k, v in urllib.parse.parse_qs(raw.decode("utf-8"), keep_blank_values=True).items()}

        # Twilio's signature is computed against the PUBLIC url it posted to,
        # not the internal hostname we're bound on. Trust a configured prefix.
        public_url = (self.public_url_prefix or f"http://{self.headers.get('host','')}").rstrip("/") + self.path

        if settings.whatsapp_verify_signature:
            sig = self.headers.get("X-Twilio-Signature", "")
            if not _verify(public_url, params, sig):
                # Dump enough to reconstruct the mismatch. Twilio params are
                # not secret; the auth_token never appears in this log.
                expected = _expected_signature(public_url, params, settings.twilio_auth_token or "")
                log.warning(
                    "whatsapp: signature check failed\n"
                    "  url=%s\n  received_sig=%s\n  expected_sig=%s\n"
                    "  host_header=%s\n  forwarded=%s\n  param_keys=%s",
                    public_url, sig, expected,
                    self.headers.get("Host", ""),
                    self.headers.get("X-Forwarded-Proto", "") + " / " + self.headers.get("X-Forwarded-Host", ""),
                    sorted(params.keys()),
                )
                return self._reply(403, "signature mismatch")

        from_ = params.get("From", "")
        body  = (params.get("Body") or "").strip()
        msid  = params.get("MessageSid", "")

        # Inbound media: NumMedia=N, then MediaUrl0..N-1 + MediaContentType0..N-1.
        media: list[dict] = []
        try:
            num_media = int(params.get("NumMedia", "0"))
        except ValueError:
            num_media = 0
        for i in range(num_media):
            url = params.get(f"MediaUrl{i}", "")
            mime = params.get(f"MediaContentType{i}", "")
            if url:
                media.append({"index": i, "url": url, "mime": mime})

        if not from_ or (not body and not media):
            log.info("whatsapp: ignoring webhook (from=%r body_len=%d media=%d)",
                     from_, len(body), len(media))
            return self._reply(200, "")

        # Ack fast; do the work (including media download) off-thread.
        self._reply(200, "")
        self.executor.submit(_process_inbound, from_, body, msid, media)


# ── Inbound media download (Twilio hosts each attachment behind Basic auth) ──

def _twilio_basic_auth_header() -> str:
    sid = settings.twilio_account_sid or ""
    token = settings.twilio_auth_token or ""
    return "Basic " + base64.b64encode(f"{sid}:{token}".encode("utf-8")).decode("ascii")


def _download_media(url: str, mime: str, msid: str, index: int) -> Optional[dict]:
    """Fetch a Twilio-hosted attachment to local disk. Returns the attachment
    dict gateway.build_vision_message expects, or None on failure.

    Twilio's ``MediaUrl<N>`` redirects (302) to a pre-signed S3 URL that does
    *not* need auth — but the initial request to api.twilio.com does. urllib
    follows the redirect automatically, and Authorization does not leak to
    the S3 host because S3's host differs.
    """
    req = urllib.request.Request(url, headers={
        "authorization": _twilio_basic_auth_header(),
        "accept": "*/*",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            # Cap at 16 MB (Twilio's own inbound limit) so we can't be forced
            # to burn memory on a runaway response.
            data = resp.read(16 * 1024 * 1024 + 1)
            if len(data) > 16 * 1024 * 1024:
                log.warning("whatsapp: media #%d too large (>16MB) — skipping", index)
                return None
    except urllib.error.HTTPError as e:
        log.warning("whatsapp: media #%d fetch failed: %s %s", index, e.code, e.reason)
        return None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        log.warning("whatsapp: media #%d fetch failed: %s", index, e)
        return None

    ext = mimetypes.guess_extension(mime or "") or ""
    name = f"{msid}_{index}{ext}"
    path = WA_INBOUND_DIR / name
    path.write_bytes(data)
    return {"path": str(path), "mime": mime, "name": name}


# ── Inbound processing ───────────────────────────────────────────────────

def _process_inbound(
    from_: str,
    body: str,
    msid: str,
    media: Optional[list[dict]] = None,
) -> None:
    """Feed the message (and any images) into chat.send and reply via Twilio."""
    # `from_` comes in as "whatsapp:+91...". Use the number as both the stable
    # user_id and, after stripping the prefix, the human-friendly identifier.
    user_id = from_.split(":", 1)[-1] if ":" in from_ else from_
    source  = f"whatsapp:{user_id}"

    attachments: list[dict] = []
    unsupported = 0
    for m in (media or []):
        dl = _download_media(m["url"], m["mime"], msid, m["index"])
        if not dl:
            continue
        if dl["mime"] not in _VISION_MIMES:
            unsupported += 1
            continue
        attachments.append(dl)

    # If the user sent only non-image media (e.g. audio, video, pdf), let them
    # know we got it but can't process it yet, and fall through only if there's
    # also text to work with.
    if unsupported and not attachments and not body.strip():
        try:
            wa.send_text(
                from_,
                "I got your media but I can only read images (jpg/png/gif/webp) for now. Send an image or text instead.",
            )
        except wa.WhatsAppError as e:
            log.warning("whatsapp: unsupported-media reply failed: %s", e)
        return

    # Body may be empty when the user just sends an image; chat.send wants
    # *something* to prompt on, so supply a sensible default caption.
    effective_body = body.strip() or ("[image attached]" if attachments else "")
    if not effective_body:
        return

    try:
        reply, _row = chat_mod.send(
            effective_body,
            source=source,
            user_id=user_id,
            display_name=user_id,
            attachments=attachments or None,
        )
    except Exception:
        log.exception("whatsapp: chat.send failed for msid=%s", msid)
        reply = "Sorry, I hit an error."

    if not reply:
        return
    try:
        wa.send_text(from_, reply)
    except wa.WhatsAppError as e:
        log.warning("whatsapp: reply send failed to=%s: %s", from_, e)


# ── Server lifecycle ─────────────────────────────────────────────────────

class WhatsAppListener:
    def __init__(self, port: Optional[int] = None, public_url: Optional[str] = None) -> None:
        self.port = port if port is not None else settings.whatsapp_listener_port
        self.public_url = public_url or settings.whatsapp_public_url or ""
        self.executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="wa-worker")
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    def run(self) -> None:
        # Early-fail if creds are missing — otherwise inbound replies would all 500.
        if not settings.twilio_account_sid or not settings.twilio_auth_token:
            raise wa.WhatsAppError(0, "TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN not set in .env")
        if settings.whatsapp_verify_signature and not self.public_url:
            log.warning(
                "whatsapp: WHATSAPP_PUBLIC_URL is not set — signature check will fail "
                "unless the request Host header matches the URL Twilio POSTed to."
            )

        handler_cls = type(
            "_Bound",
            (_WhatsAppHandler,),
            {"executor": self.executor, "public_url_prefix": self.public_url},
        )
        self._server = ThreadingHTTPServer(("0.0.0.0", self.port), handler_cls)
        log.info("whatsapp: listening on :%d (public=%s, verify=%s)",
                 self.port, self.public_url or "-", settings.whatsapp_verify_signature)
        try:
            self._server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            self.shutdown()

    def shutdown(self) -> None:
        if self._server is not None:
            try:
                self._server.shutdown()
                self._server.server_close()
            except Exception:
                log.exception("whatsapp: server shutdown error")
            self._server = None
        try:
            self.executor.shutdown(wait=False, cancel_futures=True)
        except Exception:
            log.exception("whatsapp: executor shutdown error")


def run() -> None:
    WhatsAppListener().run()
