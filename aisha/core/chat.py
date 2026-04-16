"""Minimal REPL. Reads a line, calls aisha, writes the reply."""
from __future__ import annotations

import json
import logging
import re
import sys
import urllib.error
import urllib.request
from html.parser import HTMLParser

import base64

from . import gateway, memory, narrator, observer, rag
from ..forge import docx_tool, filetool, pdf_tool, pptx_tool, registry, shell_tool
from .identity import system_prompt

log = logging.getLogger(__name__)
conv_log = logging.getLogger("aisha.conversation")

_SOURCE = "terminal"

# Stopwords kept small: we want FTS5 to run, not NLP. These are the tokens that
# blow up boolean queries without helping relevance.
_FTS_STOP = frozenset({
    "the", "and", "for", "with", "what", "have", "your", "this", "that",
    "about", "from", "were", "will", "when", "who", "why", "how", "can",
    "you", "are", "was", "did", "does", "tell", "give", "like",
})
_FTS_TOKEN = re.compile(r"[A-Za-z0-9']{3,}")

# ── Tool-use surface ─────────────────────────────────────────────────
#
# The model can call these to pull data it needs on demand. Keep the set
# small — passive retrieval (build_prompt) already seeds context on every
# turn; tools are for when the model wants to dig deeper.

_TOOL_SEARCH_MEMORY = {
    "name": "search_memory",
    "description": (
        "Full-text search over every stored conversation turn (SQLite FTS5, "
        "BM25 ranked). Use this when the user asks you to recall a specific "
        "past conversation, thread, user, or topic that is NOT already in "
        "the context provided. Returns matching turns with role, source, row "
        "id, and content snippet. Prefer simple keyword queries; combine "
        "terms with OR for broader matches (e.g. 'owl OR Tyto'). Multi-word "
        "terms are phrase-matched."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "FTS5 MATCH expression — keywords, OR groups, or quoted phrases.",
            },
            "limit": {
                "type": "integer",
                "description": "Max results to return (default 10, max 30).",
            },
        },
        "required": ["query"],
    },
}
_TOOL_REMEMBER = {
    "name": "remember",
    "description": (
        "Persist a fact to perma-context so it's loaded into every future "
        "system prompt. Call this when you notice information that recurs or "
        "will be useful across conversations — user-id → name mappings, "
        "locations, relationships, preferences, domain facts about the user's "
        "world. Facts are stored as (subject, predicate, object) triples. "
        "Calling with the same triple again bumps its confidence. Be "
        "deliberate: the perma-context has finite capacity (~30 top facts)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "subject":   {"type": "string", "description": "Entity being described (e.g. a user_id, a person, a project)."},
            "predicate": {"type": "string", "description": "Relation (e.g. 'display_name', 'lives_in', 'prefers')."},
            "object":    {"type": "string", "description": "Value (e.g. a name, city, or preference like 'terse responses')."},
            "confidence": {"type": "number", "description": "0.0–1.0. Default 0.9 for directly stated facts."},
        },
        "required": ["subject", "predicate", "object"],
    },
}
_TOOL_GENERATE_PPTX = {
    "name": "generate_pptx",
    "description": (
        "Generate a PowerPoint (.pptx) deck from a structured outline and, "
        "when invoked inside a Slack thread, upload it to that channel/thread. "
        "Use this when the user asks for a presentation, deck, slides, or "
        "PPT. Keep bullets terse — the tool renders them literally. "
        "Returns the local path and, if posted, the Slack permalink."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Deck title (rendered on the title slide)."},
            "subtitle": {"type": "string", "description": "Optional subtitle for the title slide."},
            "slides": {
                "type": "array",
                "description": "Ordered list of content slides.",
                "items": {
                    "type": "object",
                    "properties": {
                        "title":   {"type": "string", "description": "Slide heading."},
                        "bullets": {"type": "array", "items": {"type": "string"}, "description": "Bullet points. Keep each under ~15 words."},
                    },
                    "required": ["title", "bullets"],
                },
            },
            "post_to_slack": {
                "type": "boolean",
                "description": "Upload to the current Slack channel/thread. Default true when the conversation originates from Slack; no-op otherwise.",
            },
            "initial_comment": {"type": "string", "description": "Optional caption posted with the file."},
        },
        "required": ["title", "slides"],
    },
}
_TOOL_GENERATE_DOCX = {
    "name": "generate_docx",
    "description": (
        "Generate a Word (.docx) document from a structured outline and, "
        "when invoked inside a Slack thread or WhatsApp conversation, upload "
        "it there. Use this when the user asks for a document, doc, report, "
        "memo, or ``.docx``. Sections carry a heading plus paragraphs (prose) "
        "and/or bullets (lists) — mix freely. Returns the local path and, if "
        "posted, the Slack permalink or Twilio sid."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Document title (Heading 1 on page 1)."},
            "subtitle": {"type": "string", "description": "Optional italicized subtitle below the title."},
            "sections": {
                "type": "array",
                "description": "Ordered list of sections.",
                "items": {
                    "type": "object",
                    "properties": {
                        "heading":    {"type": "string", "description": "Section heading (Heading 2)."},
                        "paragraphs": {"type": "array", "items": {"type": "string"}, "description": "Prose paragraphs."},
                        "bullets":    {"type": "array", "items": {"type": "string"}, "description": "Bulleted list items."},
                    },
                    "required": ["heading"],
                },
            },
            "post_to_slack": {
                "type": "boolean",
                "description": "Upload to the current Slack channel/thread. Default true when the conversation originates from Slack; no-op otherwise.",
            },
            "initial_comment": {"type": "string", "description": "Optional caption posted with the file."},
        },
        "required": ["title", "sections"],
    },
}
_TOOL_GENERATE_PDF = {
    "name": "generate_pdf",
    "description": (
        "Compose a NEW structured PDF from scratch — title + sections with "
        "headings, paragraphs, and bullets — when the user asks you to "
        "*write* a report, memo, plan, or document. Use only when the "
        "content does not yet exist and you need to draft it. "
        "DO NOT use this to render an existing file or block of text — "
        "for that, use ``convert_to_pdf`` (much faster, no composition turn). "
        "If invoked inside Slack or WhatsApp, uploads there automatically."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Document title (large, centered)."},
            "subtitle": {"type": "string", "description": "Optional italicized subtitle below the title."},
            "sections": {
                "type": "array",
                "description": "Ordered list of sections.",
                "items": {
                    "type": "object",
                    "properties": {
                        "heading":    {"type": "string", "description": "Section heading."},
                        "paragraphs": {"type": "array", "items": {"type": "string"}, "description": "Prose paragraphs."},
                        "bullets":    {"type": "array", "items": {"type": "string"}, "description": "Bulleted list items."},
                    },
                    "required": ["heading"],
                },
            },
            "post_to_slack": {
                "type": "boolean",
                "description": "Upload to the current Slack channel/thread. Default true when the conversation originates from Slack; no-op otherwise.",
            },
            "initial_comment": {"type": "string", "description": "Optional caption posted with the file."},
        },
        "required": ["title", "sections"],
    },
}
_TOOL_CONVERT_TO_PDF = {
    "name": "convert_to_pdf",
    "description": (
        "FIRST CHOICE whenever the PDF's content already exists — either as "
        "a file on disk (``path``) or as a string you already have "
        "(``text``). Renders verbatim, preserves line breaks, sub-second. "
        "Pick this for: 'convert X to PDF', 'send X as PDF', 'regenerate "
        "the PDF', 'redo as PDF', 'remake that PDF', 'pdf of this file', "
        "or any 'try again' following a failed PDF attempt on the same "
        "material. Also use when you've edited text (e.g. stripped PII) "
        "and want to ship the cleaned version — pass the cleaned string "
        "as ``text``. DO NOT use ``generate_pdf`` for these cases: it "
        "requires the model to re-compose the content, which is slow, "
        "expensive, and exposed to upstream failures. If invoked inside "
        "Slack or WhatsApp, uploads there automatically."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Local file path. ``~`` is expanded. Mutually exclusive with ``text``."},
            "text": {"type": "string", "description": "Raw text to render directly. Mutually exclusive with ``path``. Use this after editing content (e.g. stripping PII)."},
            "title": {"type": "string", "description": "Optional title rendered at the top. Default: filename (path mode) or blank."},
            "post_to_slack":    {"type": "boolean", "description": "Upload to the current Slack channel/thread. Default true when the conversation originates from Slack."},
            "initial_comment":  {"type": "string", "description": "Optional caption posted with the file."},
        },
    },
}
_TOOL_FETCH_URL = {
    "name": "fetch_url",
    "description": (
        "Fetch a URL and return its visible text (HTML stripped). Use this "
        "when the user asks about the contents of a specific page, link, or "
        "article — or when you need to verify a live fact you don't already "
        "have. Returns the page text truncated to max_chars. Does NOT follow "
        "auth-walled or JS-rendered pages."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "Absolute http(s) URL."},
            "max_chars": {"type": "integer", "description": "Truncate output to N chars (default 10000, max 40000)."},
        },
        "required": ["url"],
    },
}
_TOOL_SLACK_POST = {
    "name": "slack_post",
    "description": (
        "Post a message to a Slack channel or DM. Use when the user asks "
        "you to notify someone, share something in another channel, or "
        "announce a result. Requires the bot to be invited to the target "
        "channel. `channel` accepts a channel ID (C…), a user ID for DM "
        "(U…), or '#channel-name'. `thread_ts` posts as a reply in that "
        "thread. Prefer the current thread (omit channel) unless the user "
        "explicitly names another destination."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "channel":  {"type": "string", "description": "Channel id, user id (for DM), or '#name'."},
            "text":     {"type": "string", "description": "Message body. Use Slack mrkdwn."},
            "thread_ts":{"type": "string", "description": "Optional thread timestamp to reply inside."},
        },
        "required": ["channel", "text"],
    },
}
_TOOL_SLACK_EDIT = {
    "name": "slack_edit",
    "description": (
        "Edit one of your own previously-posted Slack messages via chat.update. "
        "Works for both top-level channel messages and thread replies. "
        "Requires the exact channel id and the message's ts (you'll have "
        "this from when you posted it; if not, ask the user). Bot can only "
        "edit messages it authored."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "channel": {"type": "string", "description": "Channel id the message lives in."},
            "ts":      {"type": "string", "description": "Timestamp of the message to edit."},
            "text":    {"type": "string", "description": "New message body. Use Slack mrkdwn."},
        },
        "required": ["channel", "ts", "text"],
    },
}

_TOOL_SLACK_DELETE = {
    "name": "slack_delete",
    "description": (
        "Delete one of your own previously-posted Slack messages via chat.delete. "
        "Use when a posted message was wrong and an edit isn't possible (e.g. "
        "it was a thread reply). Requires channel + ts."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "channel": {"type": "string", "description": "Channel id the message lives in."},
            "ts":      {"type": "string", "description": "Timestamp of the message to delete."},
        },
        "required": ["channel", "ts"],
    },
}

_TOOL_WHATSAPP_SEND = {
    "name": "whatsapp_send",
    "description": (
        "Send a WhatsApp message via Twilio. Use when the user asks you to "
        "message someone on WhatsApp. ``to`` accepts '+E.164' (e.g. "
        "'+917676702129') or already-prefixed 'whatsapp:+…'. If ``to`` is "
        "omitted, falls back to TWILIO_WHATSAPP_DEFAULT_TO (the user's own "
        "number). Only free-form text is supported here — templates live "
        "in a separate helper reserved for outside-session-window flows."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "to":   {"type": "string", "description": "Recipient in '+E.164' or 'whatsapp:+…' form. Omit for the default."},
            "body": {"type": "string", "description": "Message text."},
        },
        "required": ["body"],
    },
}
_TOOL_WHATSAPP_SEND_FILE = {
    "name": "whatsapp_send_file",
    "description": (
        "Send a local file (image, PDF, audio, document, any MIME) to a "
        "WhatsApp recipient via Twilio. The file is published through the "
        "listener's public tunnel for 30 minutes so Twilio can fetch it, "
        "then it expires. Use when the user asks you to share a file on "
        "WhatsApp. For .pptx specifically, prefer ``generate_pptx`` from a "
        "WhatsApp conversation — it handles upload automatically."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "path":    {"type": "string", "description": "Local file path. ``~`` is expanded."},
            "to":      {"type": "string", "description": "Recipient '+E.164' or 'whatsapp:+…'. Omit for TWILIO_WHATSAPP_DEFAULT_TO."},
            "caption": {"type": "string", "description": "Optional caption posted alongside the file."},
        },
        "required": ["path"],
    },
}

_TOOL_FILE_READ = {
    "name": "file_read",
    "description": (
        "Read a text file from the local filesystem. Use when the user "
        "references a specific file path or asks you to look at a file's "
        "contents. Returns the file as text (utf-8 by default)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "path":     {"type": "string", "description": "Absolute or working-dir-relative file path."},
            "encoding": {"type": "string", "description": "Text encoding. Default 'utf-8'."},
        },
        "required": ["path"],
    },
}
_TOOL_FILE_WRITE = {
    "name": "file_write",
    "description": (
        "Write text to a file. Overwrites if it exists; creates it otherwise. "
        "Use when the user asks you to save, dump, or create a text file. "
        "The directory must already exist."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "path":     {"type": "string", "description": "Destination path."},
            "content":  {"type": "string", "description": "Text to write."},
            "encoding": {"type": "string", "description": "Text encoding. Default 'utf-8'."},
        },
        "required": ["path", "content"],
    },
}
_TOOL_FILE_READ_BYTES = {
    "name": "file_read_bytes",
    "description": (
        "Read a file as raw bytes and return its base64 encoding. Use for "
        "binary files (images, PDFs, archives) when you need to inspect or "
        "pass them on. Prefer file_read for text."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File path."},
        },
        "required": ["path"],
    },
}
_TOOL_FILE_WRITE_BYTES = {
    "name": "file_write_bytes",
    "description": (
        "Write base64-encoded bytes to a file. Use for binary output. "
        "``data`` must be standard base64."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Destination path."},
            "data": {"type": "string", "description": "base64-encoded bytes."},
        },
        "required": ["path", "data"],
    },
}
_TOOL_FILE_SEARCH = {
    "name": "file_search",
    "description": (
        "Recursively search file contents under a path for a pattern. Uses "
        "``ag`` when installed, else ``grep -rn``. Returns matches in "
        "'file:line:match' form. Use when looking for a string, symbol, or "
        "phrase across a directory tree."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "pattern":          {"type": "string", "description": "Pattern to search for."},
            "path":             {"type": "string", "description": "Root directory. Default '.'."},
            "case_insensitive": {"type": "boolean", "description": "Match case-insensitively. Default false."},
        },
        "required": ["pattern"],
    },
}
_TOOL_FILE_FIND = {
    "name": "file_find",
    "description": (
        "Find files by name using the system ``locate`` database. Returns "
        "matching absolute paths. Requires ``updatedb`` to have been run; "
        "results may be stale by up to a day. Use when the user knows part "
        "of a filename but not the path."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Filename or substring to match."},
        },
        "required": ["name"],
    },
}
_TOOL_FILE_AWK = {
    "name": "file_awk",
    "description": (
        "Run an ``awk`` expression over a file. Returns output lines. Use "
        "for column extraction, simple transforms, or summing fields when "
        "shelling out is faster than reading the whole file."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "expression": {"type": "string", "description": "Awk program (e.g. '{print $2}')."},
            "path":       {"type": "string", "description": "File to process."},
        },
        "required": ["expression", "path"],
    },
}
_TOOL_FILE_UPLOAD_SLACK = {
    "name": "file_upload_slack",
    "description": (
        "Upload an arbitrary file (any type) to a Slack channel/thread via "
        "files.upload_v2. Use when the user asks you to share a file in "
        "Slack. For .pptx specifically, prefer ``generate_pptx`` with "
        "post_to_slack=true."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "path":            {"type": "string", "description": "Local file path to upload."},
            "channel":         {"type": "string", "description": "Channel id or user id (DM)."},
            "thread_ts":       {"type": "string", "description": "Optional thread to upload into."},
            "title":           {"type": "string", "description": "Optional file title (defaults to filename)."},
            "initial_comment": {"type": "string", "description": "Optional caption posted with the file."},
        },
        "required": ["path", "channel"],
    },
}

_TOOL_SHELL_EXEC = {
    "name": "shell_exec",
    "description": (
        "Execute a shell command and return stdout, stderr, and the exit "
        "code. Use when a specific file tool (file_read, file_search, "
        "file_find, check_log) would be awkward or underpowered — e.g. "
        "running a script, inspecting processes, chaining with pipes, "
        "checking git state, testing commands. ``shell=True`` semantics: "
        "pipes, redirects, and globs work. Output is capped at 8 KB per "
        "stream. Default cwd is ``$HOME``; override with ``cwd``. Default "
        "timeout 30s, max 300s. DANGEROUS — every call is logged with its "
        "command. Avoid for tasks a safer tool already covers."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Shell command. Pipes/redirects allowed."},
            "cwd":     {"type": "string", "description": "Working directory. ``~`` is expanded. Default: $HOME."},
            "timeout": {"type": "integer", "description": "Seconds before the command is killed. Default 30, max 300."},
        },
        "required": ["command"],
    },
}
_TOOL_CHECK_LOG = {
    "name": "check_log",
    "description": (
        "Inspect your own log files under ``logs/``. Use when the user asks "
        "about your behavior, errors, recent activity, what you did, or to "
        "'check your logs'. Actions: ``list`` (enumerate log files with "
        "size/mtime), ``tail`` (last N lines of one file, default 100), "
        "``grep`` (regex match across every log under logs/, returns "
        "'file:line:match'). Scoped strictly to the logs directory — any "
        "path trying to escape is rejected."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "action":            {"type": "string", "description": "'list', 'tail', or 'grep'."},
            "file":              {"type": "string", "description": "Log filename (tail only). Default 'aisha.log'. Must live under logs/."},
            "lines":             {"type": "integer", "description": "Tail line count. Default 100, max 2000."},
            "pattern":           {"type": "string", "description": "Grep pattern (regex, ag/grep-style)."},
            "case_insensitive":  {"type": "boolean", "description": "Grep: case-insensitive. Default false."},
        },
        "required": ["action"],
    },
}

# ── fetch_url helper ─────────────────────────────────────────────────

_HTML_SKIP_TAGS = frozenset({"script", "style", "noscript", "svg", "head"})


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs):  # type: ignore[override]
        if tag.lower() in _HTML_SKIP_TAGS:
            self._skip_depth += 1

    def handle_endtag(self, tag: str):  # type: ignore[override]
        if tag.lower() in _HTML_SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data: str):  # type: ignore[override]
        if self._skip_depth == 0:
            self.parts.append(data)


def _fetch_url(url: str, max_chars: int) -> str:
    if not url.startswith(("http://", "https://")):
        return f"ERROR: url must start with http(s)://, got {url!r}"
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "aisha/1.0 (+https://github.com)"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            ctype = resp.headers.get("content-type", "").lower()
            raw = resp.read(2_000_000)  # hard cap: 2MB
    except urllib.error.HTTPError as e:
        return f"ERROR: HTTP {e.code} for {url}"
    except Exception as e:
        return f"ERROR: fetch failed: {e}"
    if "html" not in ctype and "text" not in ctype:
        return f"ERROR: unsupported content-type {ctype!r}"
    text = raw.decode("utf-8", errors="replace")
    if "html" in ctype:
        p = _TextExtractor()
        p.feed(text)
        text = "".join(p.parts)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = text.strip()
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n…[truncated at {max_chars} chars]"
    return text
_TOOL_LOOP_MAX = 20


def _run_tool(name: str, args: dict, *, source: str = "") -> str:
    if name == "generate_pptx":
        title = (args.get("title") or "").strip()
        slides = args.get("slides") or []
        if not title or not slides:
            return "ERROR: title and non-empty slides required"
        try:
            path = pptx_tool.generate_pptx(
                title, slides,
                subtitle=(args.get("subtitle") or "").strip(),
            )
        except Exception as e:
            log.exception("pptx: generation failed")
            return f"ERROR: generation failed: {e}"

        # WhatsApp conversation → upload via Twilio MediaUrl (mirrors the
        # Slack auto-upload path). The listener serves the file over the
        # public tunnel; Twilio fetches within seconds.
        if source.startswith("whatsapp:"):
            from ..channels import whatsapp as wa
            from ..channels import whatsapp_listener as wal
            to = source.split(":", 1)[1]
            caption = args.get("initial_comment") or f"Your deck: {title}"
            try:
                media_url = wal.register_file(path)
                sid = wa.send_media(to, media_url, body=caption)
            except wa.WhatsAppError as e:
                return (
                    f"Generated {path.name} but WhatsApp send failed: {e}. "
                    f"Local path: {path}"
                )
            return (
                f"OK: generated {path.name} ({len(slides)} slides) and "
                f"sent to WhatsApp. sid={sid}"
            )

        slack_ctx = pptx_tool.slack_context(source)
        want_post = args.get("post_to_slack")
        if want_post is None:
            want_post = slack_ctx is not None
        if want_post and slack_ctx:
            result = pptx_tool.upload_to_slack(
                path,
                channel=slack_ctx["channel"],
                thread_ts=slack_ctx.get("thread_ts"),
                title=title,
                initial_comment=args.get("initial_comment"),
            )
            if result.get("ok"):
                return (
                    f"OK: generated {path.name} ({len(slides)} slides) and "
                    f"posted to Slack. permalink={result.get('permalink','')}"
                )
            return (
                f"Generated {path.name} but Slack upload failed: "
                f"{result.get('error')}. Local path: {path}"
            )
        return f"OK: generated {path} ({len(slides)} slides). Not posted (no Slack context or disabled)."

    if name == "generate_docx":
        title = (args.get("title") or "").strip()
        sections = args.get("sections") or []
        if not title or not sections:
            return "ERROR: title and non-empty sections required"
        try:
            path = docx_tool.generate_docx(
                title, sections,
                subtitle=(args.get("subtitle") or "").strip(),
            )
        except Exception as e:
            log.exception("docx: generation failed")
            return f"ERROR: generation failed: {e}"

        if source.startswith("whatsapp:"):
            from ..channels import whatsapp as wa
            from ..channels import whatsapp_listener as wal
            to = source.split(":", 1)[1]
            caption = args.get("initial_comment") or f"Your doc: {title}"
            try:
                media_url = wal.register_file(path)
                sid = wa.send_media(to, media_url, body=caption)
            except wa.WhatsAppError as e:
                return (
                    f"Generated {path.name} but WhatsApp send failed: {e}. "
                    f"Local path: {path}"
                )
            return (
                f"OK: generated {path.name} ({len(sections)} sections) and "
                f"sent to WhatsApp. sid={sid}"
            )

        slack_ctx = pptx_tool.slack_context(source)
        want_post = args.get("post_to_slack")
        if want_post is None:
            want_post = slack_ctx is not None
        if want_post and slack_ctx:
            result = pptx_tool.upload_to_slack(
                path,
                channel=slack_ctx["channel"],
                thread_ts=slack_ctx.get("thread_ts"),
                title=title,
                initial_comment=args.get("initial_comment"),
            )
            if result.get("ok"):
                return (
                    f"OK: generated {path.name} ({len(sections)} sections) and "
                    f"posted to Slack. permalink={result.get('permalink','')}"
                )
            return (
                f"Generated {path.name} but Slack upload failed: "
                f"{result.get('error')}. Local path: {path}"
            )
        return f"OK: generated {path} ({len(sections)} sections). Not posted (no Slack context or disabled)."

    if name == "generate_pdf":
        title = (args.get("title") or "").strip()
        sections = args.get("sections") or []
        if not title or not sections:
            return "ERROR: title and non-empty sections required"
        try:
            path = pdf_tool.generate_pdf(
                title, sections,
                subtitle=(args.get("subtitle") or "").strip(),
            )
        except Exception as e:
            log.exception("pdf: generation failed")
            return f"ERROR: generation failed: {e}"

        if source.startswith("whatsapp:"):
            from ..channels import whatsapp as wa
            from ..channels import whatsapp_listener as wal
            to = source.split(":", 1)[1]
            caption = args.get("initial_comment") or f"Your PDF: {title}"
            try:
                media_url = wal.register_file(path)
                sid = wa.send_media(to, media_url, body=caption)
            except wa.WhatsAppError as e:
                return (
                    f"Generated {path.name} but WhatsApp send failed: {e}. "
                    f"Local path: {path}"
                )
            return (
                f"OK: generated {path.name} ({len(sections)} sections) and "
                f"sent to WhatsApp. sid={sid}"
            )

        slack_ctx = pptx_tool.slack_context(source)
        want_post = args.get("post_to_slack")
        if want_post is None:
            want_post = slack_ctx is not None
        if want_post and slack_ctx:
            result = pptx_tool.upload_to_slack(
                path,
                channel=slack_ctx["channel"],
                thread_ts=slack_ctx.get("thread_ts"),
                title=title,
                initial_comment=args.get("initial_comment"),
            )
            if result.get("ok"):
                return (
                    f"OK: generated {path.name} ({len(sections)} sections) and "
                    f"posted to Slack. permalink={result.get('permalink','')}"
                )
            return (
                f"Generated {path.name} but Slack upload failed: "
                f"{result.get('error')}. Local path: {path}"
            )
        return f"OK: generated {path} ({len(sections)} sections). Not posted (no Slack context or disabled)."

    if name == "convert_to_pdf":
        from pathlib import Path as _Path
        raw_path = (args.get("path") or "").strip()
        raw_text = args.get("text")
        if bool(raw_path) == bool(raw_text):  # both given or both missing
            return "ERROR: provide exactly one of path or text"
        title = (args.get("title") or "").strip()
        if raw_path:
            src = _Path(raw_path).expanduser()
            if not src.is_file():
                return f"ERROR: not a file: {src}"
            try:
                body = src.read_text(encoding="utf-8", errors="replace")
            except Exception as e:
                return f"ERROR: read failed: {e}"
            if not title:
                title = src.name
        else:
            body = str(raw_text)
        if not body.strip():
            return "ERROR: empty content"
        try:
            path = pdf_tool.render_text_to_pdf(body, title=title)
        except Exception as e:
            log.exception("convert_to_pdf: render failed")
            return f"ERROR: render failed: {e}"

        # Same dispatch as generate_pdf: WhatsApp via register_file+send_media,
        # Slack via upload_to_slack, else just return the local path.
        if source.startswith("whatsapp:"):
            from ..channels import whatsapp as wa
            from ..channels import whatsapp_listener as wal
            to = source.split(":", 1)[1]
            caption = args.get("initial_comment") or f"{title or path.name} (converted)"
            try:
                media_url = wal.register_file(path)
                sid = wa.send_media(to, media_url, body=caption)
            except wa.WhatsAppError as e:
                return (
                    f"Rendered {path.name} but WhatsApp send failed: {e}. "
                    f"Local path: {path}"
                )
            return f"OK: rendered {path.name} and sent to WhatsApp. sid={sid}"

        slack_ctx = pptx_tool.slack_context(source)
        want_post = args.get("post_to_slack")
        if want_post is None:
            want_post = slack_ctx is not None
        if want_post and slack_ctx:
            result = pptx_tool.upload_to_slack(
                path,
                channel=slack_ctx["channel"],
                thread_ts=slack_ctx.get("thread_ts"),
                title=title or path.name,
                initial_comment=args.get("initial_comment"),
            )
            if result.get("ok"):
                return (
                    f"OK: rendered {path.name} and posted to Slack. "
                    f"permalink={result.get('permalink','')}"
                )
            return (
                f"Rendered {path.name} but Slack upload failed: "
                f"{result.get('error')}. Local path: {path}"
            )
        return f"OK: rendered {path}. Not posted (no Slack context or disabled)."

    if name == "slack_post":
        channel = (args.get("channel") or "").strip()
        text = (args.get("text") or "").strip()
        if not channel or not text:
            return "ERROR: channel and text required"
        thread_ts = (args.get("thread_ts") or "").strip() or None
        result = pptx_tool.post_message(channel, text, thread_ts=thread_ts)
        if result.get("ok"):
            return f"OK: posted to {result.get('channel', channel)} ts={result.get('ts','')}"
        return f"ERROR: {result.get('error', 'unknown')}"

    if name == "slack_edit":
        channel = (args.get("channel") or "").strip()
        ts = (args.get("ts") or "").strip()
        text = (args.get("text") or "").strip()
        if not channel or not ts or not text:
            return "ERROR: channel, ts, and text required"
        result = pptx_tool.update_message(channel, ts, text)
        if result.get("ok"):
            return f"OK: edited message ts={ts} in {channel}"
        return f"ERROR: {result.get('error', 'unknown')}"

    if name == "slack_delete":
        channel = (args.get("channel") or "").strip()
        ts = (args.get("ts") or "").strip()
        if not channel or not ts:
            return "ERROR: channel and ts required"
        result = pptx_tool.delete_message(channel, ts)
        if result.get("ok"):
            return f"OK: deleted message ts={ts} from {channel}"
        return f"ERROR: {result.get('error', 'unknown')}"

    if name == "fetch_url":
        url = (args.get("url") or "").strip()
        if not url:
            return "ERROR: url required"
        max_chars = min(int(args.get("max_chars") or 10000), 40000)
        return _fetch_url(url, max_chars)

    if name == "whatsapp_send":
        from ..channels import whatsapp as wa
        from .config import settings as _s
        body = (args.get("body") or "").strip()
        if not body:
            return "ERROR: body required"
        to = (args.get("to") or "").strip() or (_s.twilio_whatsapp_default_to or "")
        if not to:
            return "ERROR: to required (and no TWILIO_WHATSAPP_DEFAULT_TO set)"
        try:
            sid = wa.send_text(to, body)
        except wa.WhatsAppError as e:
            return f"ERROR: {e}"
        return f"OK: sent to={to} sid={sid}"

    if name == "whatsapp_send_file":
        import mimetypes
        from pathlib import Path
        from ..channels import whatsapp as wa
        from ..channels import whatsapp_listener as wal
        from .config import settings as _s
        raw = (args.get("path") or "").strip()
        if not raw:
            return "ERROR: path required"
        path = Path(raw).expanduser()
        if not path.is_file():
            return f"ERROR: not a file: {path}"
        # Twilio WhatsApp silently async-fails unsupported MIMEs (error 63019):
        # the POST returns a sid, but delivery never happens. Reject up-front
        # so the model can pick another route (send as text, wrap as PDF, etc).
        mime, _ = mimetypes.guess_type(str(path))
        if mime not in wa.ALLOWED_MEDIA_MIMES:
            return (
                f"ERROR: WhatsApp does not accept {mime or 'unknown'} ({path.suffix}). "
                f"Supported: images (jpeg/png), audio (mpeg/ogg/amr), video (mp4/3gpp), "
                f"or documents (pdf/doc/docx/xls/xlsx/ppt/pptx). Send as text, or convert "
                f"to one of these."
            )
        to = (args.get("to") or "").strip() or (_s.twilio_whatsapp_default_to or "")
        if not to:
            return "ERROR: to required (and no TWILIO_WHATSAPP_DEFAULT_TO set)"
        caption = (args.get("caption") or "").strip()
        try:
            media_url = wal.register_file(path)
            sid = wa.send_media(to, media_url, body=caption)
        except wa.WhatsAppError as e:
            return f"ERROR: {e}"
        return f"OK: sent {path.name} to={to} sid={sid}"

    if name == "remember":
        s = (args.get("subject") or "").strip()
        p = (args.get("predicate") or "").strip()
        o = (args.get("object") or "").strip()
        if not (s and p and o):
            return "ERROR: subject, predicate, and object all required"
        conf = float(args.get("confidence") or 0.9)
        conf = max(0.0, min(1.0, conf))
        try:
            memory.knowledge_add(s, p, o, confidence=conf, source="tool:remember")
        except Exception as e:
            return f"ERROR: knowledge_add failed: {e}"
        return f"OK: remembered ({s}) --[{p}]--> ({o}) @ conf={conf:.2f}"

    if name == "search_memory":
        q = (args.get("query") or "").strip()
        if not q:
            return "ERROR: empty query"
        limit = min(int(args.get("limit") or 10), 30)
        try:
            hits = memory.search(q, limit=limit)
        except Exception as e:
            return f"ERROR: FTS5 query failed: {e}"
        if not hits:
            return f"No matches for {q!r}."
        lines = [f"{len(hits)} match(es) for {q!r}:"]
        for h in hits:
            content = (h.get("content") or "").replace("\n", " ⏎ ")
            uid = h.get("user_id") or "unknown"
            # Surface slack_ts from meta so the model can slack_edit/slack_delete.
            meta_raw = h.get("meta") or ""
            ts_tag = ""
            if meta_raw:
                try:
                    m = json.loads(meta_raw)
                    if isinstance(m, dict) and m.get("slack_ts"):
                        ts_tag = f" slack_ts={m['slack_ts']}"
                except Exception:
                    pass
            lines.append(
                f"- [{h['role']}#{h['id']} user={uid} src={h['source']}{ts_tag}] {content[:500]}"
            )
        return "\n".join(lines)

    if name == "file_read":
        path = (args.get("path") or "").strip()
        if not path:
            return "ERROR: path required"
        encoding = args.get("encoding") or "utf-8"
        try:
            return filetool.read_text(path, encoding=encoding)
        except Exception as e:
            return f"ERROR: {e}"

    if name == "file_write":
        path = (args.get("path") or "").strip()
        content = args.get("content")
        if not path or content is None:
            return "ERROR: path and content required"
        encoding = args.get("encoding") or "utf-8"
        try:
            p = filetool.write_text(path, content, encoding=encoding)
        except Exception as e:
            return f"ERROR: {e}"
        return f"OK: wrote {len(content)} chars to {p}"

    if name == "file_read_bytes":
        path = (args.get("path") or "").strip()
        if not path:
            return "ERROR: path required"
        try:
            data = filetool.read_bytes(path)
        except Exception as e:
            return f"ERROR: {e}"
        return base64.standard_b64encode(data).decode("ascii")

    if name == "file_write_bytes":
        path = (args.get("path") or "").strip()
        data_b64 = args.get("data") or ""
        if not path or not data_b64:
            return "ERROR: path and data required"
        try:
            data = base64.standard_b64decode(data_b64)
            p = filetool.write_bytes(path, data)
        except Exception as e:
            return f"ERROR: {e}"
        return f"OK: wrote {len(data)} bytes to {p}"

    if name == "file_search":
        from pathlib import Path
        import os as _os
        pattern = (args.get("pattern") or "").strip()
        if not pattern:
            return "ERROR: pattern required"
        raw = (args.get("path") or ".").strip() or "."
        home = Path(_os.path.expanduser("~")).resolve()
        target = Path(raw).expanduser().resolve()
        try:
            target.relative_to(home)
        except ValueError:
            return (
                f"ERROR: path must be under your home ({home}); got {target}. "
                f"System dirs like /root, /etc, /var are off-limits — search "
                f"under ~ or an absolute path within it."
            )
        ci = bool(args.get("case_insensitive"))
        lines = filetool.search_content(pattern, target, case_insensitive=ci)
        if not lines:
            return f"No matches for {pattern!r} under {target}."
        return "\n".join(lines[:500])

    if name == "file_find":
        from pathlib import Path
        import os as _os
        nm = (args.get("name") or "").strip()
        if not nm:
            return "ERROR: name required"
        paths = filetool.find_files(nm)
        # locate(1) returns system-wide results; confine to $HOME so the tool
        # can't surface paths outside the user's tree.
        home = Path(_os.path.expanduser("~")).resolve()
        scoped: list[Path] = []
        for p in paths:
            try:
                if p.resolve().is_relative_to(home):
                    scoped.append(p)
            except (OSError, ValueError):
                continue
        if not scoped:
            return f"No matches for {nm!r} under {home}."
        return "\n".join(str(p) for p in scoped[:500])

    if name == "file_awk":
        expr = args.get("expression") or ""
        path = (args.get("path") or "").strip()
        if not expr or not path:
            return "ERROR: expression and path required"
        lines = filetool.awk(expr, path)
        return "\n".join(lines[:1000])

    if name == "shell_exec":
        command = (args.get("command") or "").strip()
        if not command:
            return "ERROR: command required"
        cwd = (args.get("cwd") or "").strip() or None
        timeout = int(args.get("timeout") or 30)
        try:
            r = shell_tool.run_shell(command, cwd=cwd, timeout=timeout)
        except ValueError as e:
            return f"ERROR: {e}"
        except Exception as e:
            log.exception("shell_exec: unexpected failure")
            return f"ERROR: {e}"
        lines = [
            f"rc={r['returncode']} cwd={r['cwd']}"
            + (" [TIMED OUT]" if r["timed_out"] else "")
            + (" [TRUNCATED]" if r["truncated"] else ""),
        ]
        if r["stdout"]:
            lines.append("--- stdout ---")
            lines.append(r["stdout"])
        if r["stderr"]:
            lines.append("--- stderr ---")
            lines.append(r["stderr"])
        return "\n".join(lines)

    if name == "check_log":
        from .config import LOGS_DIR
        action = (args.get("action") or "").strip()
        if action == "list":
            try:
                entries = []
                for p in sorted(LOGS_DIR.iterdir()):
                    if not p.is_file():
                        continue
                    st = p.stat()
                    entries.append(f"{p.name}\t{st.st_size}\t{int(st.st_mtime)}")
            except Exception as e:
                return f"ERROR: {e}"
            if not entries:
                return "No log files."
            return "name\tsize\tmtime\n" + "\n".join(entries)

        if action == "tail":
            fname = (args.get("file") or "aisha.log").strip()
            logs_root = LOGS_DIR.resolve()
            target = (LOGS_DIR / fname).resolve()
            try:
                target.relative_to(logs_root)
            except ValueError:
                return "ERROR: file must live under logs/"
            n = min(max(int(args.get("lines") or 100), 1), 2000)
            try:
                content = filetool.read_text(target)
            except Exception as e:
                return f"ERROR: {e}"
            return "\n".join(content.splitlines()[-n:])

        if action == "grep":
            pattern = (args.get("pattern") or "").strip()
            if not pattern:
                return "ERROR: pattern required"
            ci = bool(args.get("case_insensitive"))
            lines = filetool.search_content(pattern, LOGS_DIR, case_insensitive=ci)
            if not lines:
                return f"No matches for {pattern!r} in logs."
            return "\n".join(lines[:500])

        return "ERROR: action must be one of list|tail|grep"

    if name == "file_upload_slack":
        path = (args.get("path") or "").strip()
        channel = (args.get("channel") or "").strip()
        if not path or not channel:
            return "ERROR: path and channel required"
        thread_ts = (args.get("thread_ts") or "").strip() or None
        title = (args.get("title") or "").strip() or None
        comment = (args.get("initial_comment") or "").strip() or None
        try:
            result = filetool.upload_to_slack(
                path, channel=channel, thread_ts=thread_ts,
                title=title, initial_comment=comment,
            )
        except Exception as e:
            return f"ERROR: {e}"
        if result.get("ok"):
            return f"OK: uploaded {path} to {channel} permalink={result.get('permalink','')}"
        return f"ERROR: {result.get('error','unknown')}"

    return f"ERROR: unknown tool {name!r}"


# ── Registry wiring ─────────────────────────────────────────────────
#
# Tag each tool with domain + risk and pin the always-on ones. Handlers
# delegate to ``_run_tool`` so the if-chain stays as the single dispatch
# body — registry adds routing/risk metadata without duplicating logic.

def _wrap(tool_name: str) -> registry.Handler:
    def handler(args: dict, source: str) -> str:
        return _run_tool(tool_name, args, source=source)
    handler.__name__ = f"_h_{tool_name}"
    return handler


_TOOL_REGISTRATIONS = [
    # (schema_dict,           domain,    risk,        pinned)
    (_TOOL_SEARCH_MEMORY,    "memory",  "safe",      True),
    (_TOOL_REMEMBER,         "memory",  "safe",      True),
    (_TOOL_FETCH_URL,        "web",     "safe",      True),
    (_TOOL_GENERATE_PPTX,    "files",   "safe",      False),
    (_TOOL_GENERATE_DOCX,    "files",   "safe",      False),
    (_TOOL_GENERATE_PDF,     "files",   "safe",      False),
    (_TOOL_CONVERT_TO_PDF,   "files",   "safe",      False),
    (_TOOL_SLACK_POST,       "comms",   "gated",     False),
    (_TOOL_SLACK_EDIT,       "comms",   "gated",     False),
    (_TOOL_SLACK_DELETE,     "comms",   "dangerous", False),
    (_TOOL_WHATSAPP_SEND,    "comms",   "gated",     False),
    (_TOOL_WHATSAPP_SEND_FILE,"comms",   "gated",     False),
    (_TOOL_CHECK_LOG,        "files",   "safe",      False),
    (_TOOL_SHELL_EXEC,       "files",   "dangerous", False),
    (_TOOL_FILE_READ,        "files",   "safe",      False),
    (_TOOL_FILE_WRITE,       "files",   "dangerous", False),
    (_TOOL_FILE_READ_BYTES,  "files",   "safe",      False),
    (_TOOL_FILE_WRITE_BYTES, "files",   "dangerous", False),
    (_TOOL_FILE_SEARCH,      "files",   "safe",      False),
    (_TOOL_FILE_FIND,        "files",   "safe",      False),
    (_TOOL_FILE_AWK,         "files",   "safe",      False),
    (_TOOL_FILE_UPLOAD_SLACK,"comms",   "gated",     False),
]
for _spec, _domain, _risk, _pinned in _TOOL_REGISTRATIONS:
    registry.register(registry.Tool(
        name=_spec["name"],
        description=_spec["description"],
        input_schema=_spec["input_schema"],
        handler=_wrap(_spec["name"]),
        domain=_domain,
        risk=_risk,
        pinned=_pinned,
    ))


def _compute_tool_fingerprint() -> str:
    """Short hash over the currently-registered tool set.

    Equality tracks name + description changes, which is what actually alters
    the model's decision space. Input schema tweaks don't count unless the
    description is also revised — that's the usual pattern when we add a
    triggering phrase.
    """
    import hashlib
    tools = sorted(registry.all_tools(), key=lambda t: t.name)
    blob = "\n".join(f"{t.name}\x00{t.description}" for t in tools).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:12]


memory.set_tool_fingerprint(_compute_tool_fingerprint())


def _user_text(content) -> str:
    """Flatten user content (str or vision blocks) for routing."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            b.get("text", "") for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""


def _claude_with_tools(system_prompt: str, user_content, *, source: str) -> str:
    """Run Claude with the tool-use loop. Returns the final assistant text."""
    messages: list[dict] = [{"role": "user", "content": user_content}]
    # Pre-filter the tool menu once from the original user message, so the
    # model sees a stable set across iterations of the loop.
    tools = registry.schemas_for(_user_text(user_content))
    for _ in range(_TOOL_LOOP_MAX):
        resp = gateway.complete_with_tools(system_prompt, messages, tools=tools)
        content = resp.get("content", [])
        stop = resp.get("stop_reason")
        if stop != "tool_use":
            text_parts = [b.get("text", "") for b in content if b.get("type") == "text"]
            final = "\n".join(text_parts).strip()
            if final:
                return final
            # Model stopped without emitting text — usually after a tool it
            # considered self-sufficient (e.g. `remember`). Don't let the
            # listener silently drop an empty reply: force one tools-disabled
            # turn so it has to synthesize something.
            log.warning("empty text at stop_reason=%s; forcing synthesis turn", stop)
            narrator.narrate("empty_text", stop=stop)
            resp = gateway.complete_with_tools(system_prompt, messages, tools=None)
            final = "\n".join(
                b.get("text", "") for b in resp.get("content", []) if b.get("type") == "text"
            ).strip()
            return final or "🫡"
        messages.append({"role": "assistant", "content": content})
        tool_results: list[dict] = []
        for b in content:
            if b.get("type") != "tool_use":
                continue
            name = b.get("name", "")
            args = b.get("input") or {}
            conv_log.info("TOOL source=%s name=%s args=%s",
                          source, name, json.dumps(args, default=str)[:300])
            narrator.narrate("tool_call", name=name, args=args, source=source)
            result = registry.dispatch(name, args, source=source)
            conv_log.info("TOOL-RESULT source=%s name=%s len=%d",
                          source, name, len(result))
            if result.startswith("ERROR:"):
                narrator.narrate("tool_error", name=name, result=result, source=source)
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": b.get("id"),
                "content": result,
            })
        messages.append({"role": "user", "content": tool_results})

    # Budget exhausted — force one final text turn with tools disabled so
    # Claude synthesizes an answer from what it has instead of dropping it.
    log.warning("tool loop hit max iters (%d); forcing final text turn", _TOOL_LOOP_MAX)
    resp = gateway.complete_with_tools(system_prompt, messages, tools=None)
    final = "\n".join(
        b.get("text", "") for b in resp.get("content", []) if b.get("type") == "text"
    ).strip()
    return final or "[no text after tool loop]"


def _perma_context() -> str:
    """Top persistent facts, formatted as lines for the system prompt."""
    try:
        facts = memory.knowledge_top(limit=30)
    except Exception as e:
        log.debug("perma: skipped (%s)", e)
        return ""
    lines = []
    for f in facts:
        s, p, o = f.get("subject"), f.get("predicate"), f.get("object")
        if not s or not p or not o:
            continue
        lines.append(f"- ({s}) --[{p}]--> ({o})")
    return "\n".join(lines)


def build_prompt(
    user_message: str,
    *,
    source: str = _SOURCE,
    user_id: str | None = None,
) -> tuple[str, str]:
    """Return (system_prompt, text_with_context_blocks)."""
    sys_p = system_prompt()
    perma = _perma_context()
    if perma:
        sys_p = (
            f"{sys_p}\n\n# Known facts (perma-context)\n"
            f"These are facts I've persisted across conversations. Treat as authoritative.\n"
            f"{perma}"
        )
    who = observer.context_hint(user_id)
    if who:
        sys_p = f"{sys_p}\n\n# About this user\n{who}"

    ctx = memory.context_window(
        source=source,
        max_chars=4000,
        current_user_message=user_message,
    )
    semantic = _semantic_hint(user_message)
    parts = []
    if ctx:
        parts.append(f"<recent_conversation>\n{ctx}\n</recent_conversation>")
    if semantic:
        parts.append(f"<related_memory>\n{semantic}\n</related_memory>")
    parts.append(user_message)
    return sys_p, "\n\n".join(parts)


def _fts_query(query: str) -> str:
    """Turn a free-text query into a safe FTS5 MATCH expression.

    FTS5 treats punctuation and stop-words as syntax errors when unquoted, and
    long OR chains blow up ranking. Keep it simple: lowercase content tokens,
    drop obvious filler, cap to 8.
    """
    toks = [t.lower() for t in _FTS_TOKEN.findall(query)]
    toks = [t for t in toks if t not in _FTS_STOP]
    return " OR ".join(toks[:8])


def _semantic_hint(query: str, *, limit: int = 5) -> str:
    """Hybrid retrieval: FTS5 (exact terms) + vector (meaning), merged by RRF."""
    fts_hits: list[dict] = []
    fq = _fts_query(query)
    if fq:
        try:
            fts_hits = memory.search(fq, limit=limit * 2)
        except Exception as e:
            log.debug("fts: skipped (%s)", e)

    rag_hits: list[dict] = []
    try:
        rag_hits = rag.search_conversations(query, limit=limit * 2)
    except Exception as e:
        log.debug("rag: skipped (%s)", e)

    # Reciprocal-rank fusion: cheap, no score-scale mismatch between FTS and vectors.
    K = 60
    scores: dict[int, float] = {}
    rows: dict[int, dict] = {}
    for r, h in enumerate(fts_hits):
        rid = int(h["id"])
        scores[rid] = scores.get(rid, 0.0) + 1.0 / (K + r + 1)
        rows.setdefault(rid, {
            "id": rid,
            "role": h.get("role"),
            "content": h.get("content") or "",
            "source": h.get("source") or "",
            "user_id": h.get("user_id") or "",
        })
    for r, h in enumerate(rag_hits):
        hid = str(h.get("id", ""))
        meta = h.get("metadata") or {}
        # `conv-{rid}` hits credit one row; `pair-{user}-{asst}` hits credit both,
        # so the question turn and the answer turn can each surface in RRF ranking.
        target_rids: list[int] = []
        if hid.startswith("pair-"):
            parts = hid.split("-")
            if len(parts) == 3:
                try:
                    target_rids = [int(parts[1]), int(parts[2])]
                except ValueError:
                    continue
        elif hid.startswith("conv-"):
            try:
                target_rids = [int(hid.rsplit("-", 1)[-1])]
            except ValueError:
                continue
        if not target_rids:
            continue
        for rid in target_rids:
            scores[rid] = scores.get(rid, 0.0) + 1.0 / (K + r + 1)
            if rid in rows:
                continue
            if hid.startswith("pair-"):
                # The Chroma doc is the concatenated pair; look up the individual turn for display.
                turn = memory.get_turn(rid)
                if turn:
                    rows[rid] = {
                        "id": rid,
                        "role": turn.get("role"),
                        "content": turn.get("content") or "",
                        "source": turn.get("source") or "",
                        "user_id": turn.get("user_id") or "",
                    }
            else:
                rows[rid] = {
                    "id": rid,
                    "role": meta.get("role"),
                    "content": h.get("content") or "",
                    "source": meta.get("source") or "",
                    "user_id": meta.get("user_id") or "",
                }

    log.debug("retrieval: fts=%d rag=%d merged=%d", len(fts_hits), len(rag_hits), len(scores))
    if not scores:
        return ""

    ranked = sorted(scores.items(), key=lambda kv: -kv[1])[:limit]
    lines: list[str] = []
    for rid, _ in ranked:
        row = rows[rid]
        txt = (row["content"] or "").strip().replace("\n", " ")
        if not txt:
            continue
        role = row.get("role") or "?"
        uid = row.get("user_id") or "unknown"
        lines.append(f"- [{role}#{rid} user={uid}] {txt[:400]}")
    return "\n".join(lines)


def send(
    user_message: str,
    *,
    source: str = _SOURCE,
    user_id: str | None = None,
    display_name: str = "",
    attachments: list[dict] | None = None,
) -> tuple[str, int]:
    """Record the user turn, call the gateway, record aisha's reply, index it.

    Returns ``(reply_text, aisha_row_id)``. The row id lets the caller attach
    post-send metadata (e.g. Slack ``ts``) to the reply so later tool calls
    can find and edit it.

    When ``attachments`` is non-empty (list of ``{"path", "mime", "name"}``),
    the user message is sent as a multi-modal Anthropic content list so the
    model sees the images directly.
    """
    observer.observe(user_id or source, user_message, display_name=display_name)

    # Record a single text turn in memory, annotated with attachment names so
    # history doesn't lose track of what was shared even though images aren't
    # inlined into the store.
    stored = user_message
    if attachments:
        names = ", ".join(a.get("name", "image") for a in attachments)
        stored = f"{user_message}\n[attachments: {names}]"
    user_row = memory.record(
        "user", stored,
        source=source, user_id=user_id,
        meta={"attachments": [a.get("name") for a in (attachments or [])]},
    )
    conv_log.info("USER source=%s user=%s id=%s | %s",
                  source, user_id or "-", user_row, stored.replace("\n", " ⏎ "))
    narrator.narrate("user", message=user_message, source=source, user_id=user_id or "")
    try:
        rag.index_conversation(user_row, stored, {"role": "user", "source": source})
    except Exception as e:
        log.debug("rag: index skipped (%s)", e)

    sys_p, text = build_prompt(user_message, source=source, user_id=user_id)

    if attachments:
        user_content = gateway.build_vision_message(text, attachments)
    else:
        user_content = text

    narrator.narrate("turn_start", source=source, user_id=user_id or "")
    try:
        reply = _claude_with_tools(sys_p, user_content, source=source)
    finally:
        narrator.narrate("turn_end", source=source, user_id=user_id or "")

    aisha_row = memory.record("assistant", reply, source=source, user_id=user_id)
    conv_log.info("AISHA source=%s user=%s id=%s | %s",
                  source, user_id or "-", aisha_row, (reply or "").replace("\n", " ⏎ "))
    try:
        rag.index_conversation(aisha_row, reply, {"role": "assistant", "source": source})
    except Exception as e:
        log.debug("rag: index skipped (%s)", e)
    try:
        rag.index_pair(user_row, stored, aisha_row, reply, {"source": source})
    except Exception as e:
        log.debug("rag: pair index skipped (%s)", e)
    return reply, aisha_row


def passive_observe(text: str, user_id: str, display_name: str = "") -> None:
    """Silent observation — no LLM call, no reply. Updates profile only."""
    observer.observe(user_id, text, display_name=display_name)


def repl() -> None:
    print("aisha — type '/exit' to quit")
    memory.record("system", "Session started", source=_SOURCE)
    observer.mark_session(_SOURCE)
    while True:
        try:
            user_message = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not user_message:
            continue
        if user_message in ("/exit", "/quit"):
            break
        try:
            reply, _ = send(user_message, source=_SOURCE, user_id=_SOURCE)
        except gateway.GatewayError as e:
            print(f"! gateway error: {e}", file=sys.stderr)
            continue
        except Exception as e:
            log.exception("chat: unexpected error")
            print(f"! {e}", file=sys.stderr)
            continue
        print(f"aisha> {reply}\n")
    memory.record("system", "Session ended", source=_SOURCE)
    print("bye.")
