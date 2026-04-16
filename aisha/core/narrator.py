"""Background narrator — Mistral-generated one-line commentary on aisha's
activity, logged as ``aisha.narrator``.

Strictly cosmetic: events are fire-and-forget onto a bounded queue; a single
daemon thread calls Mistral and logs the result. If Mistral is unreachable,
the key is missing, or the queue overflows, the main tool loop is never
affected — we log a warning once and keep dropping.

Enable/disable via ``AISHA_NARRATOR=1|0``. Requires ``MISTRAL_API_KEY``.
"""
from __future__ import annotations

import atexit
import json
import logging
import queue
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Optional

from .config import settings

log = logging.getLogger("aisha.narrator")

_MISTRAL_URL = "https://api.mistral.ai/v1/chat/completions"
_QUEUE_MAX = 100
_SENTINEL: dict = {"__stop__": True}

# Assumed upper bound on the tool loop, kept in sync with chat._TOOL_LOOP_MAX.
# Only used for ETA projection — a rough heuristic, not authoritative.
_ASSUMED_MAX_ITERS = 20

_SYSTEM_PROMPT = (
    "You narrate what an AI assistant named aisha is doing, in a terse, "
    "observational style. Given an event, write ONE SHORT LINE — at most 15 "
    "words, no preamble, no markdown, no quotes, present tense. Examples:\n"
    "- 'resolving file path from earlier context'\n"
    "- 'generating 5-section PDF of aisha_tales'\n"
    "- 'recovering from tool error, searching logs for file location'\n"
    "- 'reading user message about diabetes analysis'\n"
    "Describe intent or state, not raw parameters."
)

_queue: "queue.Queue[dict]" = queue.Queue(maxsize=_QUEUE_MAX)
_worker: Optional[threading.Thread] = None
_progress_worker: Optional[threading.Thread] = None
_disabled_reason: Optional[str] = None  # set once if Mistral is unreachable

# Tools that typically dominate a turn's wallclock. When one of these is the
# most recent call, we add a buffer to the ETA so it doesn't tell the user
# "~5s" while Claude is still chewing through a big composition.
_HEAVY_TOOLS = frozenset({
    "generate_pdf", "generate_docx", "generate_pptx",
    "shell_exec", "fetch_url", "whatsapp_send_file",
})


@dataclass
class _TurnState:
    source: str
    user_id: str
    started_at: float
    iter_count: int = 0
    last_narration: str = ""
    last_ping_at: float = 0.0
    last_tool_name: str = ""


_turns: dict[str, _TurnState] = {}
_turns_lock = threading.Lock()


def _should_run() -> bool:
    if not settings.narrator_enabled:
        return False
    if not settings.mistral_api_key:
        return False
    return True


def _call_mistral(event_text: str) -> Optional[str]:
    """Single blocking POST to Mistral's chat/completions endpoint.

    Returns the generated line on success, None on any failure. Kept small
    (max 40 tokens) so cost and latency stay negligible.
    """
    payload = json.dumps({
        "model": settings.mistral_model,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": event_text},
        ],
        "max_tokens": 40,
        "temperature": 0.3,
    }).encode("utf-8")
    req = urllib.request.Request(
        _MISTRAL_URL,
        data=payload,
        headers={
            "Authorization": f"Bearer {settings.mistral_api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=settings.mistral_timeout) as resp:
            body = resp.read().decode("utf-8")
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
        return None
    try:
        data = json.loads(body)
        text = data["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, ValueError):
        return None
    # Clamp at one line; some models still sneak in stray newlines.
    return text.splitlines()[0].strip() if text else None


def _format_event(ev: dict) -> str:
    """Shape an event dict into a short natural-language line for Mistral."""
    kind = ev.get("kind", "event")
    if kind == "user":
        msg = (ev.get("message") or "")[:200]
        src = ev.get("source", "")
        return f"User message arrived on {src}: {msg!r}"
    if kind == "tool_call":
        args = json.dumps(ev.get("args") or {}, default=str)[:300]
        return f"About to call tool {ev.get('name')!r} with args {args}"
    if kind == "tool_error":
        return (
            f"Tool {ev.get('name')!r} returned error: "
            f"{(ev.get('result') or '')[:200]}"
        )
    if kind == "gateway_retry":
        return (
            f"Gateway failed (status={ev.get('status')}); retrying "
            f"attempt {ev.get('attempt')}."
        )
    if kind == "empty_text":
        return "Model ended turn with no text; forcing synthesis turn."
    return f"Event: {json.dumps(ev, default=str)[:300]}"


def _handle_turn_lifecycle(ev: dict) -> None:
    """Update per-source turn state for progress-ping tracking.

    Runs inline on the narrator worker thread so state mutations stay
    serialized with narration updates.
    """
    kind = ev.get("kind")
    source = ev.get("source") or ""
    if not source:
        return
    if kind == "turn_start":
        with _turns_lock:
            _turns[source] = _TurnState(
                source=source,
                user_id=ev.get("user_id") or "",
                started_at=time.time(),
            )
    elif kind == "turn_end":
        with _turns_lock:
            _turns.pop(source, None)
    elif kind == "tool_call":
        with _turns_lock:
            st = _turns.get(source)
            if st is not None:
                st.iter_count += 1
                st.last_tool_name = ev.get("name") or ""


def _worker_loop() -> None:
    global _disabled_reason
    while True:
        try:
            ev = _queue.get()
        except Exception:
            continue
        if ev is _SENTINEL or ev.get("__stop__"):
            return
        # Turn lifecycle is tracked regardless of Mistral health, so ETAs
        # still work even if narration has been silenced.
        _handle_turn_lifecycle(ev)
        if _disabled_reason is not None:
            continue  # drain silently; we've already warned once
        # Skip Mistral calls for lifecycle-only events — nothing narratable.
        if ev.get("kind") in ("turn_start", "turn_end"):
            continue
        text_in = _format_event(ev)
        line = _call_mistral(text_in)
        if line is None:
            if _disabled_reason is None:
                _disabled_reason = "mistral call failed"
                log.warning(
                    "narrator: Mistral unreachable; silencing narrator for this session"
                )
            continue
        log.info("narrator: %s", line)
        # Stash the line for the progress pinger.
        src = ev.get("source") or ""
        if src:
            with _turns_lock:
                st = _turns.get(src)
                if st is not None:
                    st.last_narration = line


def _format_eta(state: _TurnState) -> str:
    if state.iter_count == 0:
        return "a few seconds"
    elapsed = time.time() - state.started_at
    rate = elapsed / state.iter_count
    remaining = max(0, _ASSUMED_MAX_ITERS - state.iter_count)
    sec = rate * remaining
    if state.last_tool_name in _HEAVY_TOOLS:
        sec += 30  # heavy tool in flight likely dominates what's left
    sec = int(sec)
    if sec <= 5:
        return "wrapping up"
    if sec < 60:
        return f"~{sec}s"
    if sec < 600:
        return f"~{sec // 60}m"
    return "several minutes"


def _send_progress_ping(state: _TurnState) -> None:
    """Send a single WhatsApp progress message. Never raises."""
    if not state.source.startswith("whatsapp:"):
        return
    narration = state.last_narration or "still working"
    eta = _format_eta(state)
    body = f"⏳ {narration}. ETA {eta}."
    try:
        from ..channels import whatsapp as wa
    except Exception as e:
        log.debug("narrator: wa import failed (%s)", e)
        return
    to = state.user_id or state.source.split(":", 1)[1]
    try:
        wa.send_text(to, body)
        log.info("narrator: ping sent to=%s %r", to, body)
    except Exception as e:
        log.debug("narrator: ping send failed (%s)", e)


def _progress_loop() -> None:
    """Wake periodically; ping any turn that's run past the interval."""
    interval = max(10, settings.progress_ping_interval)
    tick = min(10, interval // 2 or 10)
    while True:
        time.sleep(tick)
        if not settings.progress_pings_enabled:
            continue
        now = time.time()
        due: list[_TurnState] = []
        with _turns_lock:
            for st in _turns.values():
                elapsed = now - st.started_at
                since_last = now - st.last_ping_at if st.last_ping_at else elapsed
                if elapsed >= interval and since_last >= interval:
                    due.append(st)
                    st.last_ping_at = now
        for st in due:
            _send_progress_ping(st)


def _ensure_started() -> None:
    global _worker, _progress_worker
    if _worker is not None and _worker.is_alive():
        return
    if not _should_run():
        return
    _worker = threading.Thread(target=_worker_loop, name="aisha-narrator", daemon=True)
    _worker.start()
    log.info("narrator: started (model=%s)", settings.mistral_model)
    # Progress pinger runs independently so ETAs still fire even if Mistral
    # goes silent (narration will just fall back to the generic placeholder).
    if settings.progress_pings_enabled and (
        _progress_worker is None or not _progress_worker.is_alive()
    ):
        _progress_worker = threading.Thread(
            target=_progress_loop, name="aisha-progress", daemon=True,
        )
        _progress_worker.start()
        log.info("narrator: progress-ping loop started (interval=%ds)",
                 settings.progress_ping_interval)
    atexit.register(_shutdown)


def _shutdown() -> None:
    try:
        _queue.put_nowait(_SENTINEL)
    except queue.Full:
        return
    if _worker is not None:
        _worker.join(timeout=2)


def narrate(kind: str, **fields: Any) -> None:
    """Enqueue a narration event. Never blocks, never raises.

    Callers pass a ``kind`` (``user``, ``tool_call``, ``tool_error``,
    ``gateway_retry``, ``empty_text``, or anything else) plus arbitrary
    context fields. On overflow or disabled narrator, the event is dropped.
    """
    if not _should_run() or _disabled_reason is not None:
        return
    _ensure_started()
    try:
        _queue.put_nowait({"kind": kind, **fields})
    except queue.Full:
        pass  # burst; drop silently rather than blocking the caller
