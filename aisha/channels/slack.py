"""Slack Socket Mode listener.

Uses Socket Mode (WebSocket) via websocket-client. Auto-reconnects.
Markdown → mrkdwn conversion, per-thread routing, @mention + app_mention
handling, reactions as feedback, image download, active-thread tracking,
passive observation of all channel traffic.

``chat.send`` is stateless, so we dispatch via a bounded ThreadPoolExecutor.
"""
from __future__ import annotations

import io
import json
import logging
import os
import re
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

import websocket

from ..core import chat as chat_mod

log = logging.getLogger(__name__)

_API_BASE = "https://slack.com/api"

# Name-wake: "hi aisha", "aisha can you...", etc.
_NAME_WAKE_RE = re.compile(r"\baisha\b", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Slack context — structured metadata for every inbound message
# ---------------------------------------------------------------------------

@dataclass
class SlackContext:
    channel: str
    thread_ts: str
    user: str
    channel_type: str = ""
    user_name: str = ""
    is_dm: bool = False
    is_mention: bool = False
    attachments: list = field(default_factory=list)

    @property
    def source_tag(self) -> str:
        return f"slack:{self.channel}:{self.thread_ts}"

    def as_dict(self) -> dict:
        return {
            "slack_channel": self.channel,
            "slack_thread": self.thread_ts,
            "slack_user": self.user,
            "slack_user_name": self.user_name,
            "slack_channel_type": self.channel_type,
            "slack_is_dm": self.is_dm,
            "slack_is_mention": self.is_mention,
        }


# ---------------------------------------------------------------------------
# Slack API helpers
# ---------------------------------------------------------------------------

def _http_post(url: str, token: str, body: dict | None = None) -> dict:
    headers = {"Authorization": f"Bearer {token}"}
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode()
    else:
        data = None
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        resp = urllib.request.urlopen(req, timeout=15)
        return json.loads(resp.read().decode())
    except Exception as e:
        log.error("Slack API error: %s", e)
        return {"ok": False, "error": str(e)}


def _get_ws_url(app_token: str) -> str:
    result = _http_post(f"{_API_BASE}/apps.connections.open", app_token)
    if not result.get("ok"):
        raise RuntimeError(f"Failed to open Socket Mode: {result.get('error')}")
    return result["url"]


# ---------------------------------------------------------------------------
# Markdown → Slack mrkdwn
# ---------------------------------------------------------------------------

_MD_CODEBLOCK_RE = re.compile(r"```[a-zA-Z0-9_+-]*\n", re.MULTILINE)
_MD_BOLD_RE = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_MD_HEADER_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$", re.MULTILINE)
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
_MD_BULLET_RE = re.compile(r"^(\s*)[-*](\s+)", re.MULTILINE)
_MD_HRULE_RE = re.compile(r"^\s*[-*_]{3,}\s*$", re.MULTILINE)


def _to_slack_mrkdwn(text: str) -> str:
    """Convert standard markdown to Slack mrkdwn (matches aisha's impl exactly)."""
    if not text:
        return text
    text = _MD_CODEBLOCK_RE.sub("```\n", text)
    parts = text.split("```")
    for i in range(0, len(parts), 2):
        p = parts[i]
        p = _MD_HRULE_RE.sub("", p)
        p = _MD_HEADER_RE.sub(r"*\2*", p)
        p = _MD_BOLD_RE.sub(r"*\1*", p)
        p = _MD_LINK_RE.sub(r"<\2|\1>", p)
        p = _MD_BULLET_RE.sub(r"\1•\2", p)
        parts[i] = p
    return "```".join(parts)


def _send_slack(channel: str, text: str, thread_ts: str | None = None) -> str | None:
    """Post a message; return the posted ``ts`` so callers can enable edits."""
    bot_token = os.environ.get("SLACK_BOT_TOKEN", "")
    if not bot_token:
        log.error("SLACK_BOT_TOKEN not set")
        return None
    text = _to_slack_mrkdwn(text)
    log.info("[SLACK OUT] channel=%s thread=%s text=%s",
             channel, thread_ts or "(top)", text[:200])
    body = {"channel": channel, "text": text}
    if thread_ts:
        body["thread_ts"] = thread_ts
    result = _http_post(f"{_API_BASE}/chat.postMessage", bot_token, body)
    if not result.get("ok"):
        log.error("Failed to send message: %s", result.get("error"))
        return None
    return result.get("ts")


def _get_bot_user_id() -> str:
    bot_token = os.environ.get("SLACK_BOT_TOKEN", "")
    result = _http_post(f"{_API_BASE}/auth.test", bot_token)
    return result.get("user_id", "")


# ---------------------------------------------------------------------------
# User name cache
# ---------------------------------------------------------------------------

class _UserCache:
    def __init__(self, bot_token: str, max_size: int = 200):
        self._token = bot_token
        self._cache: dict[str, str] = {}
        self._max_size = max_size
        self._lock = threading.Lock()

    def resolve(self, user_id: str) -> str:
        with self._lock:
            if user_id in self._cache:
                return self._cache[user_id]
        result = _http_post(f"{_API_BASE}/users.info", self._token, {"user": user_id})
        name = user_id
        if result.get("ok"):
            profile = result.get("user", {}).get("profile", {})
            name = (
                profile.get("display_name")
                or profile.get("real_name")
                or result.get("user", {}).get("name", user_id)
            )
        with self._lock:
            if len(self._cache) >= self._max_size:
                oldest = next(iter(self._cache))
                del self._cache[oldest]
            self._cache[user_id] = name
        return name


# ---------------------------------------------------------------------------
# Main listener
# ---------------------------------------------------------------------------

class SlackListener:
    def __init__(self, pool_size: int = 4):
        self.app_token = os.environ.get("SLACK_APP_TOKEN", "")
        self.bot_token = os.environ.get("SLACK_BOT_TOKEN", "")
        if not self.app_token:
            raise RuntimeError(
                "SLACK_APP_TOKEN not set. Create an app-level token at "
                "api.slack.com/apps > Basic Information > App-Level Tokens "
                "with the connections:write scope, then add to .env"
            )
        if not self.bot_token:
            raise RuntimeError("SLACK_BOT_TOKEN not set. Add it to .env")

        self.bot_user_id = _get_bot_user_id()
        self._executor = ThreadPoolExecutor(max_workers=pool_size, thread_name_prefix="slack-")
        self._user_cache = _UserCache(self.bot_token)
        self._ws = None
        self.always_respond_channels: set[str] = {
            "C0AMY2Z20TF",
        }
        self._active_threads: set[str] = set()
        self._inactive_threads: set[str] = set()

    # ── worker ──────────────────────────────────────────────────────

    def _process_message(self, text: str, ctx: SlackContext) -> None:
        aisha_row: int | None = None
        try:
            response, aisha_row = chat_mod.send(
                text,
                source=ctx.source_tag,
                user_id=ctx.user,
                display_name=ctx.user_name,
                attachments=ctx.attachments or None,
            )
        except Exception as e:
            log.error(
                "chat.send failed user=%s text=%r: %s",
                ctx.user, text, e, exc_info=True,
            )
            response = f"Sorry, I hit an error: {e}"

        if response and isinstance(response, str):
            response = re.sub(r"\x1b\[[0-9;]*m", "", response)
            posted_ts = _send_slack(ctx.channel, response, ctx.thread_ts)
            if posted_ts and aisha_row is not None:
                try:
                    from .. import memory
                    memory.update_meta(aisha_row, {
                        "slack_ts": posted_ts,
                        "slack_channel": ctx.channel,
                    })
                except Exception as e:
                    log.debug("meta update failed: %s", e)

    def _passive_observe(self, text: str, user_id: str, user_name: str) -> None:
        try:
            chat_mod.passive_observe(text, user_id, user_name)
        except Exception:
            log.debug("passive observe failed", exc_info=True)

    # ── helpers ─────────────────────────────────────────────────────

    def _bot_replied_in_thread(self, channel: str, thread_ts: str) -> bool:
        try:
            data = urllib.parse.urlencode(
                {"channel": channel, "ts": thread_ts, "limit": 20}
            ).encode()
            req = urllib.request.Request(
                f"{_API_BASE}/conversations.replies",
                data=data,
                headers={
                    "Authorization": f"Bearer {self.bot_token}",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                method="POST",
            )
            resp = urllib.request.urlopen(req, timeout=10)
            result = json.loads(resp.read().decode())
            if not result.get("ok"):
                return False
            for msg in result.get("messages", []):
                if msg.get("user") == self.bot_user_id or msg.get("bot_id"):
                    return True
        except Exception:
            pass
        return False

    def _build_context(self, event: dict, *, is_mention: bool = False) -> SlackContext | None:
        user = event.get("user", "")
        channel = event.get("channel", "")
        ts = event.get("ts", "")
        thread_ts = event.get("thread_ts", ts)
        channel_type = event.get("channel_type", "")
        if not user or not channel:
            return None
        user_name = self._user_cache.resolve(user)
        return SlackContext(
            channel=channel,
            thread_ts=thread_ts,
            user=user,
            channel_type=channel_type,
            user_name=user_name,
            is_dm=channel_type == "im",
            is_mention=is_mention,
        )

    def _ocr_image(self, path: str) -> str:
        import shutil
        import subprocess
        if not shutil.which("tesseract"):
            return ""
        try:
            result = subprocess.run(
                ["tesseract", path, "stdout", "--oem", "3", "--psm", "3"],
                capture_output=True, text=True, timeout=30,
            )
            return result.stdout.strip()
        except Exception as e:
            log.warning("OCR failed for %s: %s", path, e)
            return ""

    def _download_slack_file(self, file_info: dict) -> str | None:
        url = file_info.get("url_private", "")
        name = os.path.basename(file_info.get("name", "unknown"))
        if not url:
            return None
        try:
            import tempfile
            suffix = os.path.splitext(name)[1] or ""
            fd, dest = tempfile.mkstemp(prefix="slack_upload_", suffix=suffix, dir="/tmp")
            req = urllib.request.Request(url)
            req.add_header("Authorization", f"Bearer {self.bot_token}")
            resp = urllib.request.urlopen(req, timeout=60)
            with os.fdopen(fd, "wb") as f:
                f.write(resp.read())
            log.info("Downloaded Slack file %s -> %s (%d bytes)",
                     name, dest, os.path.getsize(dest))
            return dest
        except Exception as e:
            log.error("Failed to download Slack file %s: %s", name, e)
            return None

    def _extract_text(self, event: dict) -> tuple[str, list]:
        text = event.get("text", "").strip()
        attachments: list[dict] = []
        files = event.get("files", [])
        if not files:
            return text, attachments
        file_parts = []
        for finfo in files:
            local_path = self._download_slack_file(finfo)
            if not local_path:
                continue
            mime = finfo.get("mimetype", "unknown")
            fname = finfo.get("name", "file")
            if mime.startswith("image/") and not mime.endswith("svg+xml"):
                attachments.append({"path": local_path, "mime": mime, "name": fname})
                ocr_text = self._ocr_image(local_path)
                if ocr_text:
                    file_parts.append(f"[Image: {fname} — OCR hint:\n{ocr_text}]")
                else:
                    file_parts.append(f"[Image: {fname} ({mime}) attached]")
            else:
                file_parts.append(f"[File: {fname} ({mime}) saved to {local_path}]")
        if file_parts:
            file_context = "\n".join(file_parts)
            text = f"{text}\n\n{file_context}" if text else file_context
        return text, attachments

    # ── websocket handlers ──────────────────────────────────────────

    def _on_message(self, ws, raw) -> None:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            log.warning("[HISTORY] Failed to parse WS message: %r", raw[:200])
            return

        msg_type = data.get("type")
        log.debug("[HISTORY] WS envelope type=%s keys=%s",
                  msg_type, list(data.keys()))

        envelope_id = data.get("envelope_id")
        if envelope_id:
            ws.send(json.dumps({"envelope_id": envelope_id}))

        if msg_type == "disconnect":
            reason = data.get("reason", "unknown")
            log.info("Slack sent disconnect (reason=%s) — closing to trigger reconnect", reason)
            ws.close()
            return

        if msg_type == "events_api":
            event = data.get("payload", {}).get("event", {})
            self._handle_event(event)
        elif msg_type == "hello":
            log.info("Socket Mode connected")

    def _handle_event(self, event: dict) -> None:
        event_type = event.get("type")
        log.debug("[HISTORY] Event: type=%s event=%s",
                  event_type, json.dumps(event, default=str)[:500])

        if event_type == "message":
            subtype = event.get("subtype")
            if subtype is not None and subtype != "file_share":
                return

            user = event.get("user", "")
            if user == self.bot_user_id:
                return

            text, attachments = self._extract_text(event)
            if not text and not attachments:
                return

            # Passive observation — learn from everything, always
            try:
                user_name = self._user_cache.resolve(user)
                self._passive_observe(text, user, user_name)
            except Exception:
                pass

            channel = event.get("channel", "")
            channel_type = event.get("channel_type", "")
            thread_ts = event.get("thread_ts", "")
            is_dm = channel_type == "im"
            is_mentioned = f"<@{self.bot_user_id}>" in text
            is_always = channel in self.always_respond_channels
            is_active_thread = bool(thread_ts and thread_ts in self._active_threads)
            is_name_called = bool(_NAME_WAKE_RE.search(text))

            if thread_ts and not is_active_thread and thread_ts not in self._inactive_threads:
                was_active = self._bot_replied_in_thread(channel, thread_ts)
                log.info("Thread %s in %s: bot_replied=%s",
                         thread_ts, channel, was_active)
                if was_active:
                    self._active_threads.add(thread_ts)
                    is_active_thread = True
                else:
                    self._inactive_threads.add(thread_ts)

            if is_mentioned:
                # Handled by app_mention event to avoid double-fire
                return
            if not is_dm and not is_always and not is_active_thread and not is_name_called:
                return

            # Name-wake in a channel starts a new active thread so follow-ups
            # don't need to keep saying "Aisha" — mirrors the app_mention path.
            if is_name_called and not is_active_thread:
                thread_key = thread_ts or event.get("ts", "")
                if thread_key:
                    self._active_threads.add(thread_key)
            if not text:
                text = "hello"

            ctx = self._build_context(event, is_mention=is_mentioned)
            if not ctx:
                return
            ctx.attachments = attachments

            log.info("Message from %s (%s) in %s: %s (attachments=%d)",
                     user, ctx.user_name, channel, text[:100], len(attachments))
            self._executor.submit(self._process_message, text, ctx)

        elif event_type in ("reaction_added", "reaction_removed"):
            self._handle_reaction(event, event_type)
            return

        elif event_type == "app_mention":
            user = event.get("user", "")
            text, attachments = self._extract_text(event)
            is_mentioned = f"<@{self.bot_user_id}>" in text
            if is_mentioned:
                text = text.replace(f"<@{self.bot_user_id}>", "").strip()
            if not text:
                text = "hello"

            thread_key = event.get("thread_ts", "") or event.get("ts", "")
            if thread_key:
                self._active_threads.add(thread_key)

            ctx = self._build_context(event, is_mention=True)
            if not ctx:
                return
            ctx.attachments = attachments

            log.info("Mention from %s (%s) in %s: %s",
                     user, ctx.user_name, ctx.channel, text[:100])
            self._executor.submit(self._process_message, text, ctx)

    def _handle_reaction(self, event: dict, event_type: str) -> None:
        reaction = event.get("reaction", "")
        user = event.get("user", "")
        item = event.get("item", {})
        item_user = event.get("item_user", "")

        # Only act on reactions to our own messages, not our own reactions
        if item_user != self.bot_user_id or user == self.bot_user_id:
            return

        channel = item.get("channel", "")
        ts = item.get("ts", "")
        added = event_type == "reaction_added"

        log.info("Reaction %s: :%s: from %s on %s/%s",
                 "added" if added else "removed",
                 reaction, user, channel, ts)

        if not added:
            return

        user_name = self._user_cache.resolve(user)
        text = (
            f"{user_name} reacted with :{reaction}: on my message. "
            f"Respond naturally to what the reaction means in context — "
            f"it could be a request, feedback, or just an emoji."
        )
        ctx = SlackContext(
            channel=channel,
            thread_ts=ts,
            user=user,
            channel_type="channel",
            user_name=user_name,
            is_dm=False,
            is_mention=False,
        )
        log.info("Reaction trigger: :%s: from %s → responding in %s/%s",
                 reaction, user_name, channel, ts)
        self._executor.submit(self._process_message, text, ctx)

    def _on_error(self, ws, error) -> None:
        log.error("WebSocket error: %s", error)

    def _on_close(self, ws, close_status, close_msg) -> None:
        log.info("WebSocket closed: %s %s", close_status, close_msg)

    def _on_open(self, ws) -> None:
        log.info("WebSocket opened")

    # ── lifecycle ───────────────────────────────────────────────────

    def start(self) -> None:
        print("  starting slack listener (socket mode)...")
        print(f"  bot user: {self.bot_user_id or '(unknown)'}")
        print(f"  worker pool: {self._executor._max_workers}")
        print("  listening for DMs and @mentions")
        print("  ctrl+c to stop\n")
        self._run_loop()

    def start_background(self) -> threading.Thread:
        t = threading.Thread(target=self._run_loop, daemon=True, name="slack-listener")
        t.start()
        log.info("Slack listener started in background thread")
        return t

    def _run_loop(self) -> None:
        backoff = 3
        MAX_BACKOFF = 120
        while True:
            try:
                ws_url = _get_ws_url(self.app_token)
                log.info("Connecting to %s", ws_url)
                self._ws = websocket.WebSocketApp(
                    ws_url,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                    on_open=self._on_open,
                )
                self._ws.run_forever(ping_interval=30, ping_timeout=10)
                backoff = 3
            except KeyboardInterrupt:
                log.info("Slack listener shutting down")
                break
            except Exception as e:
                log.error("Connection failed: %s — reconnecting in %ds", e, backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF)
                continue
            log.info("WebSocket disconnected — reconnecting in 3s")
            time.sleep(3)


def run() -> None:
    """Entry point called by __main__ when --slack is passed."""
    SlackListener().start()
