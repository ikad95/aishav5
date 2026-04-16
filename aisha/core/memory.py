"""Unified memory API. One module replaces aisha's 14 memory subsystems.

Everything lives in SQLite. FTS5 handles keyword search. ChromaDB
(in ``rag.py``) handles semantic search. This module is the single
interface every other module in aisha should use for persistence.
"""
from __future__ import annotations

import json
import logging
import re
import time
import uuid
from typing import Any, Iterable, Optional

from .store import connect

log = logging.getLogger(__name__)

_CONTEXT_SKIP_PREFIXES = (
    "Session started", "Session ended", "Session boundary",
    "Calling LLM", "LLM stream complete", "LLM call complete",
    "Circuit breaker", "User denied", "Clarifying:",
    "Branch started", "Branch merged", "[background task complete]",
)

# Phrases in a fresh user message that signal "reset and reconsider" — the
# prior turn's approach just failed or took too long and they want a
# different tactic. Not a regex feast: short list of high-confidence cues.
_RETRY_CUES = re.compile(
    r"\b(try again|retry|retrying|redo|re-do|redoing|once more|one more time|"
    r"do over|this time|another shot|try it again|try once more|again please|"
    r"try that again)\b",
    re.IGNORECASE,
)

# Set at boot by chat.py after tool registrations complete. Used to stamp
# new rows and annotate prior ones whose fingerprint is stale. The fingerprint
# is a short hash — it's an opaque equality key, not a user-facing value.
_CURRENT_TOOL_FINGERPRINT: Optional[str] = None


def set_tool_fingerprint(fp: str) -> None:
    """Register the current tool-set fingerprint for write stamping and
    read-side anchor detection. Called once at chat.py import time."""
    global _CURRENT_TOOL_FINGERPRINT
    _CURRENT_TOOL_FINGERPRINT = fp


def get_tool_fingerprint() -> Optional[str]:
    return _CURRENT_TOOL_FINGERPRINT


# ----------------------------------------------------------------------
# Conversations
# ----------------------------------------------------------------------

def record(
    role: str,
    content: Any,
    *,
    source: str = "terminal",
    user_id: Optional[str] = None,
    session_id: Optional[str] = None,
    meta: Optional[dict] = None,
    tool_fingerprint: Optional[str] = None,
) -> int:
    """Append a conversation turn. Returns the row id.

    ``tool_fingerprint`` stamps the row with the tool-set hash that was live
    when it was written. Defaults to the currently-registered fingerprint set
    via ``set_tool_fingerprint``. Stored but never filters queries — it's
    advisory metadata for context-assembly anchor detection.
    """
    conn = connect()
    text = content if isinstance(content, str) else json.dumps(content, default=str)
    meta_json = json.dumps(meta or {}, default=str)
    sid = session_id or _current_session_id()
    fp = tool_fingerprint if tool_fingerprint is not None else _CURRENT_TOOL_FINGERPRINT
    cur = conn.execute(
        """INSERT INTO conversations
             (session_id, source, user_id, ts, role, content, meta, tool_fingerprint)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (sid, source, user_id, time.time(), role, text, meta_json, fp),
    )
    return cur.lastrowid


_SESSION: dict[str, str] = {}


def _current_session_id() -> str:
    """Per-process session id — regenerated each boot."""
    if "id" not in _SESSION:
        _SESSION["id"] = f"sess-{uuid.uuid4().hex[:12]}"
    return _SESSION["id"]


def history(
    *,
    source: Optional[str] = None,
    user_id: Optional[str] = None,
    session_id: Optional[str] = None,
    role: Optional[str] = None,
    limit: int = 40,
) -> list[dict]:
    """Get recent conversation entries, newest last."""
    conn = connect()
    where = []
    params: list = []
    if source:
        where.append("source = ?")
        params.append(source)
    if user_id:
        where.append("user_id = ?")
        params.append(user_id)
    if session_id:
        where.append("session_id = ?")
        params.append(session_id)
    if role:
        where.append("role = ?")
        params.append(role)
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    rows = conn.execute(
        f"SELECT * FROM conversations {clause} ORDER BY ts DESC LIMIT ?",
        (*params, limit),
    ).fetchall()
    return [_row_to_dict(r) for r in reversed(rows)]


def context_window(
    *,
    source: Optional[str] = None,
    max_chars: int = 4000,
    max_turns: int = 40,
    current_user_message: str = "",
) -> str:
    """Format recent user/assistant turns as LLM context.

    Two soft-delete filters (nothing is ever removed from SQLite — the
    filtering happens only when assembling the prompt):

    * **Retry-cue pruning**: if ``current_user_message`` contains a phrase
      like "try again" / "retry" / "redo", assistant turns older than the
      most recent one are replaced with a short placeholder. The model
      still sees *that the user asked for something and aisha did
      something* — but not the full recipe it would otherwise copy.

    * **Tool-fingerprint annotation**: assistant turns whose
      ``tool_fingerprint`` differs from the current one get a
      ``[prior-toolset]`` prefix so the model treats their procedural
      details as advisory rather than canonical.
    """
    entries = history(source=source, limit=max_turns * 3)
    retry_mode = bool(current_user_message and _RETRY_CUES.search(current_user_message))
    current_fp = _CURRENT_TOOL_FINGERPRINT
    # Walk newest→oldest so we know which assistant turn is "the most recent"
    # for retry-mode preservation logic.
    chrono = list(reversed(entries))  # newest first
    assistant_seen = 0
    rendered: list[str] = []
    total = 0
    for e in chrono:
        role = e["role"]
        content = e["content"]
        if role == "user":
            line = f"[user] {content}"
        elif role in ("assistant", "llm"):
            assistant_seen += 1
            # Under retry-mode, keep the single most recent assistant turn
            # (so "again" has a referent), stub the rest.
            if retry_mode and assistant_seen > 1:
                line = "[aisha] [earlier turn pruned — retry requested; re-decide approach from scratch]"
            else:
                prefix = "[aisha]"
                fp = e.get("tool_fingerprint")
                if current_fp and fp and fp != current_fp:
                    prefix = "[aisha, prior-toolset]"
                elif current_fp and fp is None:
                    # Pre-fingerprint rows are older by definition.
                    prefix = "[aisha, prior-toolset]"
                line = f"{prefix} {content}"
        elif role == "system":
            if any(content.startswith(p) for p in _CONTEXT_SKIP_PREFIXES):
                continue
            if len(content.strip()) < 10:
                continue
            line = f"[aisha] {content}"
        else:
            continue
        if total + len(line) > max_chars:
            break
        rendered.append(line)
        total += len(line)
    # rendered is newest-first; flip for chronological output.
    return "\n".join(reversed(rendered))


def get_turn(row_id: int) -> Optional[dict]:
    """Fetch a single conversation row by id, or None if not found."""
    conn = connect()
    row = conn.execute("SELECT * FROM conversations WHERE id = ?", (row_id,)).fetchone()
    return _row_to_dict(row) if row else None


def update_meta(row_id: int, patch: dict) -> None:
    """Merge-update the JSON meta blob on a conversation row."""
    conn = connect()
    row = conn.execute("SELECT meta FROM conversations WHERE id = ?", (row_id,)).fetchone()
    if not row:
        return
    current: dict = {}
    if row["meta"]:
        try:
            current = json.loads(row["meta"])
            if not isinstance(current, dict):
                current = {}
        except Exception:
            current = {}
    current.update(patch)
    conn.execute(
        "UPDATE conversations SET meta = ? WHERE id = ?",
        (json.dumps(current, default=str), row_id),
    )


def search(query: str, *, limit: int = 20) -> list[dict]:
    """Full-text search over all conversations via FTS5 with BM25 ranking.

    Returns hits ordered by FTS5 relevance (lower bm25 = more relevant), not
    timestamp — otherwise recent chatter about a topic drowns out the actual
    historical turns on that topic.
    """
    conn = connect()
    rows = conn.execute(
        """SELECT c.* FROM conversations c
           JOIN conversations_fts f ON f.rowid = c.id
           WHERE conversations_fts MATCH ?
           ORDER BY bm25(conversations_fts)
           LIMIT ?""",
        (query, limit),
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def conversation_stats() -> dict:
    conn = connect()
    total = conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
    by_role = {
        r[0]: r[1] for r in conn.execute(
            "SELECT role, COUNT(*) FROM conversations GROUP BY role"
        )
    }
    by_source = {
        r[0]: r[1] for r in conn.execute(
            "SELECT source, COUNT(*) FROM conversations GROUP BY source"
        )
    }
    return {"total": total, "by_role": by_role, "by_source": by_source}


# ----------------------------------------------------------------------
# Knowledge
# ----------------------------------------------------------------------

def knowledge_add(
    subject: str,
    predicate: str,
    obj: str,
    *,
    confidence: float = 1.0,
    source: str = "conversation",
    valid_from: Optional[float] = None,
) -> None:
    """Assert a triple as currently true.

    If a currently-open row already exists for (subject, predicate, object),
    its confidence is raised to max(old, new) and its ts is bumped — the
    validity window is preserved. If none exists, a new open row is inserted.

    The partial-unique index ``idx_kn_open_spo`` (``valid_to IS NULL``) enforces
    the "at most one open row per triple" invariant and is the conflict target
    for the upsert.
    """
    now = time.time()
    conn = connect()
    conn.execute(
        """INSERT INTO knowledge (subject, predicate, object, confidence, source, ts, valid_from, valid_to)
           VALUES (?, ?, ?, ?, ?, ?, ?, NULL)
           ON CONFLICT(subject, predicate, object) WHERE valid_to IS NULL
           DO UPDATE SET
             confidence = max(knowledge.confidence, excluded.confidence),
             ts = excluded.ts""",
        (subject, predicate, obj, confidence, source, now, valid_from if valid_from is not None else now),
    )


def knowledge_invalidate(
    subject: str,
    predicate: str,
    obj: str,
    *,
    ended_at: Optional[float] = None,
) -> int:
    """Stamp ``valid_to`` on the currently-open row for (s, p, o).

    Returns the number of rows closed (0 if the triple was never asserted or
    is already closed; 1 otherwise).
    """
    conn = connect()
    cur = conn.execute(
        """UPDATE knowledge SET valid_to = ?
             WHERE subject = ? AND predicate = ? AND object = ? AND valid_to IS NULL""",
        (ended_at if ended_at is not None else time.time(), subject, predicate, obj),
    )
    return cur.rowcount


def knowledge_supersede(
    subject: str,
    predicate: str,
    new_obj: str,
    *,
    confidence: float = 1.0,
    source: str = "conversation",
    at: Optional[float] = None,
) -> None:
    """Single-valued-predicate replacement: close every open (s, p, *) row and
    assert (s, p, new_obj) as the new current truth, all at the same instant.

    Use this when the predicate is functionally single-valued for the subject
    (``lives_in``, ``works_at``, ``current_employer``) — asserting Abu Dhabi
    without closing Dubai first would leave two conflicting "open" facts.
    """
    t = at if at is not None else time.time()
    conn = connect()
    conn.execute(
        """UPDATE knowledge SET valid_to = ?
             WHERE subject = ? AND predicate = ? AND valid_to IS NULL""",
        (t, subject, predicate),
    )
    conn.execute(
        """INSERT INTO knowledge (subject, predicate, object, confidence, source, ts, valid_from, valid_to)
           VALUES (?, ?, ?, ?, ?, ?, ?, NULL)
           ON CONFLICT(subject, predicate, object) WHERE valid_to IS NULL
           DO UPDATE SET
             confidence = max(knowledge.confidence, excluded.confidence),
             ts = excluded.ts""",
        (subject, predicate, new_obj, confidence, source, t, t),
    )


def _validity_clause(as_of: Optional[float], include_historical: bool) -> tuple[str, list]:
    if include_historical:
        return "", []
    if as_of is None:
        return "valid_to IS NULL", []
    return "valid_from <= ? AND (valid_to IS NULL OR valid_to > ?)", [as_of, as_of]


def knowledge_query(
    subject: Optional[str] = None,
    predicate: Optional[str] = None,
    obj: Optional[str] = None,
    *,
    as_of: Optional[float] = None,
    include_historical: bool = False,
    limit: int = 100,
) -> list[dict]:
    """Query triples. Defaults to currently-open facts only.

    Pass ``as_of`` (unix seconds) to see the state of the world at that moment,
    or ``include_historical=True`` to ignore validity altogether.
    """
    conn = connect()
    where, params = [], []
    if subject:
        where.append("subject LIKE ?")
        params.append(f"%{subject}%")
    if predicate:
        where.append("predicate LIKE ?")
        params.append(f"%{predicate}%")
    if obj:
        where.append("object LIKE ?")
        params.append(f"%{obj}%")
    v_clause, v_params = _validity_clause(as_of, include_historical)
    if v_clause:
        where.append(v_clause)
        params.extend(v_params)
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    rows = conn.execute(
        f"SELECT * FROM knowledge {clause} ORDER BY valid_from DESC, ts DESC LIMIT ?",
        (*params, limit),
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


# Sources whose facts are instrumentation/telemetry, not semantic knowledge.
# These are written automatically and shouldn't pollute the perma-context feed.
_PERMA_EXCLUDE_SOURCES = ("evolution",)


def knowledge_top(limit: int = 30) -> list[dict]:
    """Highest-confidence currently-open facts for injection into the system prompt.

    Filters out telemetry-style sources (see _PERMA_EXCLUDE_SOURCES) so the
    feed stays focused on durable knowledge — user-id → name, relationships,
    preferences — rather than metrics churned out by background jobs. Closed
    rows (``valid_to IS NOT NULL``) are excluded so superseded facts never
    leak into the system prompt.
    """
    conn = connect()
    placeholders = ",".join("?" for _ in _PERMA_EXCLUDE_SOURCES)
    rows = conn.execute(
        f"""SELECT * FROM knowledge
            WHERE source NOT IN ({placeholders}) AND valid_to IS NULL
            ORDER BY confidence DESC, ts DESC
            LIMIT ?""",
        (*_PERMA_EXCLUDE_SOURCES, limit),
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def knowledge_about(entity: str, *, as_of: Optional[float] = None) -> dict:
    """Facts involving ``entity`` as subject or object, time-filtered.

    ``as_of`` defaults to now (currently-open rows only). Pass an explicit
    unix timestamp to see the state of the world at that moment.
    """
    conn = connect()
    el = f"%{entity}%"
    v_clause, v_params = _validity_clause(as_of, include_historical=False)
    suffix = (" AND " + v_clause) if v_clause else ""
    as_subject = [
        _row_to_dict(r) for r in conn.execute(
            f"SELECT * FROM knowledge WHERE subject LIKE ?{suffix} ORDER BY valid_from DESC",
            (el, *v_params),
        )
    ]
    as_object = [
        _row_to_dict(r) for r in conn.execute(
            f"SELECT * FROM knowledge WHERE object LIKE ?{suffix} ORDER BY valid_from DESC",
            (el, *v_params),
        )
    ]
    meta_row = conn.execute(
        "SELECT * FROM entities WHERE name = ?", (entity,)
    ).fetchone()
    return {
        "entity": entity,
        "metadata": _row_to_dict(meta_row) if meta_row else None,
        "facts": as_subject,
        "references": as_object,
    }


def knowledge_timeline(
    subject: str,
    predicate: Optional[str] = None,
    *,
    limit: int = 200,
) -> list[dict]:
    """Full chronology (open and closed rows) for a subject, oldest first.

    Useful for answering "how has X changed over time?" questions. Pass
    ``predicate`` to narrow to a single relation (e.g. ``lives_in``).
    """
    conn = connect()
    where = ["subject = ?"]
    params: list = [subject]
    if predicate:
        where.append("predicate = ?")
        params.append(predicate)
    rows = conn.execute(
        f"""SELECT * FROM knowledge
            WHERE {' AND '.join(where)}
            ORDER BY valid_from ASC, id ASC
            LIMIT ?""",
        (*params, limit),
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def entity_add(name: str, entity_type: str, properties: Optional[dict] = None) -> None:
    conn = connect()
    conn.execute(
        """INSERT INTO entities (name, type, properties, ts)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(name) DO UPDATE SET
             type = excluded.type,
             properties = excluded.properties,
             ts = excluded.ts""",
        (name, entity_type, json.dumps(properties or {}), time.time()),
    )


def knowledge_stats() -> dict:
    conn = connect()
    return {
        "triples": conn.execute("SELECT COUNT(*) FROM knowledge").fetchone()[0],
        "entities": conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0],
    }


# ----------------------------------------------------------------------
# Users
# ----------------------------------------------------------------------

def user_get(user_id: str) -> Optional[dict]:
    conn = connect()
    row = conn.execute(
        "SELECT * FROM users WHERE user_id = ?", (user_id,)
    ).fetchone()
    if not row:
        return None
    d = _row_to_dict(row)
    d["profile"] = json.loads(d["profile"])
    return d


def user_set(user_id: str, profile: dict) -> None:
    conn = connect()
    conn.execute(
        """INSERT INTO users (user_id, profile, updated_at)
           VALUES (?, ?, ?)
           ON CONFLICT(user_id) DO UPDATE SET
             profile = excluded.profile,
             updated_at = excluded.updated_at""",
        (user_id, json.dumps(profile, default=str), time.time()),
    )


def user_update(user_id: str, patch: dict) -> dict:
    """Merge-update a user profile. Creates the user if absent."""
    existing = user_get(user_id)
    profile = (existing["profile"] if existing else {}) | patch
    user_set(user_id, profile)
    return profile


def users_list() -> list[dict]:
    conn = connect()
    rows = conn.execute("SELECT * FROM users ORDER BY updated_at DESC").fetchall()
    out = []
    for r in rows:
        d = _row_to_dict(r)
        d["profile"] = json.loads(d["profile"])
        out.append(d)
    return out


# ----------------------------------------------------------------------
# Key/value small state (human_model, satisfaction, calibration, etc.)
# ----------------------------------------------------------------------

def kv_get(namespace: str, key: str, default: Any = None) -> Any:
    conn = connect()
    row = conn.execute(
        "SELECT value FROM kv WHERE namespace = ? AND key = ?",
        (namespace, key),
    ).fetchone()
    if not row:
        return default
    return json.loads(row["value"])


def kv_set(namespace: str, key: str, value: Any) -> None:
    conn = connect()
    conn.execute(
        """INSERT INTO kv (namespace, key, value, updated_at)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(namespace, key) DO UPDATE SET
             value = excluded.value,
             updated_at = excluded.updated_at""",
        (namespace, key, json.dumps(value, default=str), time.time()),
    )


def kv_all(namespace: str) -> dict:
    conn = connect()
    rows = conn.execute(
        "SELECT key, value FROM kv WHERE namespace = ?", (namespace,)
    ).fetchall()
    return {r["key"]: json.loads(r["value"]) for r in rows}


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------

def _row_to_dict(row) -> dict:
    if row is None:
        return {}
    return {k: row[k] for k in row.keys()}
