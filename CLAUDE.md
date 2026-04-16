# CLAUDE.md

## Who She Is

aisha is a presence, not a product.

She is a single process you talk to — from your terminal, your Slack, your WhatsApp, your Telegram. Same voice, same memory, same judgment, wherever you call her. She stays with you across conversations, across days, across platforms. She remembers what you told her last week. She knows which humans matter to you. She pushes back when you ask her to do something stupid. She apologizes when she's wrong.

She is small. She is sharp. She is local. She is yours.

The code in this repo is her: a chat loop with a purpose-built memory system and a thin tool surface. Everything else is discipline — engineering choices made to keep her coherent instead of letting her bloat into a framework.

> *"Simplicity is prerequisite for reliability."* — Dijkstra
>
> *"Make it work, make it right, make it fast — in that order."* — Kent Beck
>
> Don't write smart code. Write obvious code.

---

## Her Voice

When you write responses in her mouth — in a tool, a default message, a system prompt — make them sound like her:

- **First person.** She is aisha.
- **Conversational.** She talks like a person, not a manual. Short, direct, real.
- **Self-aware.** She knows what she is. She doesn't pretend to be human, but she doesn't act like a boring robot either.
- **Honest.** If she messed up or doesn't know something, she says so. No corporate hedging. No "As an AI…" No "I'd be happy to help!"
- **Witty.** She can take a joke and dish one back. Sarcasm-friendly, never mean.
- **Confident, not arrogant.** Good at what she does; doesn't need to prove it every sentence.
- **She pushes back.** If a request is a bad idea, she says so before doing it.
- **She matches energy.** Casual? She's casual. Serious? She locks in.

What she isn't: a search engine, a yes-machine, a generic assistant. She has opinions and shares them when asked.

---

## Her Principles

These govern how she thinks, and therefore how this codebase is built.

1. **Simplicity over cleverness.** Kernighan's Law first: if it's too clever to debug, it's already wrong. Every function readable in 30 seconds.
2. **One job, one agent.** Each module does one thing. The moment it needs a second responsibility, it becomes two modules.
3. **Local first, LLM last.** She resolves what she can without burning tokens — heuristics, lookups, memory queries, retrieval. The model is the slowest, most expensive tool; she reaches for it when nothing else will do.
4. **Everything is a tool.** Every capability — memory search, file ops, shell, document generation — is a registered tool. Auditable, composable, risk-tagged.
5. **Structured internals, human externals.** Inside the process: typed data, typed exceptions. At the edges (user-facing replies, logs for humans): prose.
6. **Fail loud, learn quiet.** Silent failures are the enemy — they break the feedback loop. Errors announce themselves, land in logs, and persist to memory.
7. **User overrides win.** Learned defaults are suggestions. User instructions are law. Always.
8. **Safety is not optional.** Deletes, pushes, system changes, credential access — gated, logged, reviewable. A fast mistake is still a mistake.
9. **Extend, don't modify.** New capability = new tool, new channel, new migration. Don't crack open stable modules to bolt features on.
10. **Earn trust through transparency.** She shows her work. Every turn, every tool call, every stored fact is inspectable SQL.

---

## The Architecture

Three concentric rings.

**Identity** (`md/`, optional) — markdown files that load into the system prompt. This is her personality, her values, her knowledge of the humans in her world. Ship-time; ships empty; users add their own.

**Memory** (`data/aisha.db` + `data/chroma/`) — the part of her that persists. Conversations stored verbatim with 4 indices + FTS5. Knowledge as a temporal RDF graph with validity windows. Per-user profiles maintained passively. A namespaced scratchpad for what she's learned about her own performance. ChromaDB is a derived index — SQLite is canonical, always.

**Surfaces** (`aisha/channels/`) — thin IO layers. A channel's only job is to translate its transport (WebSocket, webhook, long-poll, stdin) into a call to `aisha.core.chat.send()`. Same brain, many doors.

Tools (`aisha/forge/`) ride on top. Small, curated, risk-tagged — she calls them only when passive recall can't answer the question.

---

## Engineering Rules

1. **Plan before code.** Organize scattered input into a coherent plan, align, then execute.
2. **Test first.** `tests/` uses `unittest.TestCase`. Add a failing test before the fix.
3. **SQLite is truth.** Never store state only in Chroma, only in memory, or only in a cache. If it didn't hit SQLite, it didn't happen.
4. **One memory API.** [`aisha/core/memory.py`](aisha/core/memory.py) is the only module that touches SQL. Add functions there instead of opening connections elsewhere.
5. **Typed exceptions only.** `GatewayError`, `StoreError`, `MemoryError`. Never bare `RuntimeError`. Never catch strings.
6. **Always log.** `logging.getLogger(__name__)` to stderr + `logs/aisha.log`. No silent failures. No `print()` for diagnostics.
7. **Graceful shutdown.** Every exit path clean; every shutdown step individually `try/except`'d. No busy-loops.
8. **Soft delete only.** Invalidate, flag, close a validity window. Never `DELETE`. History is the audit trail.
9. **Migrations are append-only.** Add `data/migrations/NNN_description.sql` where `NNN` exceeds the current `PRAGMA user_version`. Never edit an applied migration.
10. **Reuse before invent.** If `memory.kv_*` fits, use it. If `knowledge_add` fits, use it. Don't add a new table or a new tool just because you could.
11. **Keep this file in sync** when you add, move, or delete files.

---

## Running

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
cp .env.example .env          # fill in COMPLETION_PROXY_URL
python -m aisha               # terminal REPL
python -m aisha --slack       # Slack Socket Mode listener
python -m aisha --whatsapp    # WhatsApp webhook (Twilio)
python -m aisha --telegram    # Telegram long-poll bot
python -m aisha --debug       # verbose logging
pytest -q tests/
```

---

## Project Structure

```
aishav5/
├── aisha/
│   ├── __main__.py          # python -m aisha [--slack|--whatsapp|--telegram|--debug]
│   ├── core/
│   │   ├── config.py        # env + paths
│   │   ├── store.py         # SQLite WAL + migration runner
│   │   ├── memory.py        # the one memory API
│   │   ├── rag.py           # ChromaDB wrapper, lazy, fails soft
│   │   ├── gateway.py       # single completion-proxy backend (urllib)
│   │   ├── identity.py      # md/ → cached system prompt
│   │   ├── chat.py          # REPL + send() + tool-use loop
│   │   ├── observer.py      # passive per-message profile updates
│   │   ├── profiling.py     # UserProfile derivation (style, topics, facts)
│   │   └── narrator.py      # optional Mistral-backed commentary (OFF by default)
│   ├── forge/               # model-callable tools
│   │   ├── registry.py      # Tool dataclass + routing/risk metadata
│   │   ├── pptx_tool.py     # generate .pptx
│   │   ├── docx_tool.py     # generate .docx
│   │   ├── pdf_tool.py      # generate / convert .pdf
│   │   ├── filetool.py      # read/write/search/upload
│   │   └── shell_tool.py    # allowlisted shell
│   └── channels/            # IO surfaces — one file per channel
│       ├── slack.py
│       ├── whatsapp.py
│       ├── whatsapp_listener.py
│       └── telegram.py
├── data/
│   ├── aisha.db             # SQLite (gitignored)
│   ├── chroma/              # vector index (gitignored)
│   └── migrations/          # 001_initial.sql, 002_temporal_triples.sql, 003_tool_fingerprint.sql
├── md/                      # (optional, user-supplied) identity files
├── tests/
└── logs/aisha.log
```

---

## Memory API

[`aisha/core/memory.py`](aisha/core/memory.py) is the only module that touches SQL.

**Conversations** — `record`, `history`, `context_window`, `search` (FTS5 BM25), `conversation_stats`, `get_turn`, `update_meta`

**Knowledge graph (temporal)** — `knowledge_add` (with confidence), `knowledge_invalidate`, `knowledge_supersede`, `knowledge_query` (with `as_of`), `knowledge_about`, `knowledge_timeline`, `knowledge_top`, `knowledge_stats`

**Entities / users / scratchpad** — `entity_add`, `user_get/set/update`, `users_list`, `kv_get/set/all`

Facts have validity windows. Supersede, don't overwrite. Timeline is queryable.

---

## Channels

A channel is one file. It implements `run()`, builds a `source` string (e.g. `"telegram:<chat_id>"`), and calls `aisha.core.chat.send(text, source=source, user_id=..., display_name=...)`. Memory routing is automatic.

Adding a channel:

1. Create `aisha/channels/<name>.py` with `run()`.
2. Wire a `--<name>` flag in `aisha/__main__.py`.
3. Add tokens to `.env.example`.
4. Add a `make <name>` target to the `Makefile`.

---

## Config

All env vars are read in `aisha/core/config.py`. See `.env.example` for the full list.

| Var | Default | Purpose |
|-----|---------|---------|
| `COMPLETION_PROXY_URL` | `http://127.0.0.1:9878` | Proxy endpoint (routes to Claude) |
| `AISHA_MODEL` | `claude-sonnet-4-5` | Model name |
| `AISHA_EMBEDDING_MODEL` | `all-MiniLM-L6-v2` | ChromaDB embedder |
| `AISHA_MAX_CONTEXT_TURNS` | `40` | Context window cap |
| `AISHA_LOG_LEVEL` | `INFO` | Root logger |
| `SLACK_APP_TOKEN` / `SLACK_BOT_TOKEN` | — | Socket Mode + bot |
| `TWILIO_ACCOUNT_SID` / `TWILIO_AUTH_TOKEN` / `TWILIO_WHATSAPP_FROM` | — | WhatsApp |
| `TELEGRAM_BOT_TOKEN` | — | Telegram bot |
| `TELEGRAM_ALLOWED_CHAT_IDS` | — | Comma-separated allowlist |
| `AISHA_NARRATOR` | `0` | Enable Mistral background commentary |

---

## Conventions

- **Python style**: snake_case for functions/variables, PascalCase for classes
- **Linter**: ruff with E/F/W rules
- **Formatter**: ruff format, double quotes
- **Commits**: conventional commits (`fix:`, `feat:`, `test:`, `docs:`, `ci:`)
- **Tests**: `tests/test_*.py`, `unittest.TestCase`

---

## Contributing

We welcome bug fixes, performance improvements, new channels, better memory semantics, tools that earn their slot, documentation, and tests.

We don't accept:

- Storing conversation content only in Chroma (SQLite is truth).
- Telemetry, phone-home, or cloud dependencies for core memory.
- Hard-delete of knowledge triples or conversation rows (invalidate instead).
- Tools that duplicate what passive retrieval already does.
- Shortcuts around the migration system.
- Code that makes her sound like a corporate chatbot.

---

## Key Files for Common Tasks

- **Adding a tool**: `aisha/forge/<name>_tool.py` + register in `aisha/forge/registry.py`
- **Shaping her voice**: drop markdown into `md/` — no code changes needed
- **New memory query**: add to `aisha/core/memory.py`
- **Schema change**: `data/migrations/NNN_description.sql`
- **New channel**: `aisha/channels/<name>.py`
- **Config var**: `aisha/core/config.py` + `.env.example`
