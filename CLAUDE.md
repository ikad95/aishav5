# CLAUDE.md

## The Mission

Memory is identity. An assistant that forgets everything between conversations cannot build real understanding — of you, your work, your people, your life.

aisha exists to solve that. She is a single process with a purpose-built memory system: every turn stored verbatim, every fact on a temporal knowledge graph, every channel (terminal, Slack, WhatsApp, Telegram) writing to the same local store. Nothing leaves your machine.

The architecture is not a wrapper around a vector store. SQLite is canonical. ChromaDB is a derived index. Knowledge is a graph with time. Tools are a thin surface on top of all of it.

> *"Simplicity is prerequisite for reliability."* — Dijkstra
>
> *"Make it work, make it right, make it fast — in that order."* — Kent Beck
>
> *"There are two ways of constructing software: one is to make it so simple that there are obviously no deficiencies, and the other is to make it so complicated that there are no obvious deficiencies."* — Hoare
>
> Don't write smart code. Write obvious code.

---

## Design Principles

Non-negotiable. Every PR, every feature, every refactor must honor them.

- **SQLite is truth.** Never store state only in Chroma, only in a cache, or only in memory. If it didn't hit SQLite, it didn't happen.
- **One memory API.** [`aisha/core/memory.py`](aisha/core/memory.py) is the only module that touches SQL. Add functions there instead of opening connections elsewhere.
- **Verbatim conversations.** Every user turn, every assistant turn, every tool call, every tool result is recorded in the `conversations` table with full context. Never summarize before storing.
- **Temporal knowledge.** Facts have `valid_from` / `valid_to`. Supersede and invalidate — never delete. The graph's history is itself a feature.
- **Local-first.** No telemetry. No phone-home. The completion proxy is the single outbound call, and the user points it wherever they want.
- **Channels are thin.** A channel is one file that implements `run()` and calls `aisha.core.chat.send()`. Stateless at the process boundary; memory lives in SQLite.
- **Tools earn their slot.** The default tool surface is small on purpose. Before adding a new one, ask whether passive retrieval already covers the need.
- **Typed exceptions only.** `GatewayError`, `StoreError`, `MemoryError`. Never bare `RuntimeError`. Never catch strings.
- **Always log.** `logging.getLogger(__name__)` to stderr + `logs/aisha.log`. No silent failures. No `print()` for diagnostics.
- **Graceful shutdown.** Every exit path clean; every shutdown step individually `try/except`'d. No busy-loops.

---

## Rules

1. **Plan before code.** Organize scattered input into a coherent plan, align, then execute.
2. **Test first.** `tests/` uses `unittest.TestCase`. Add a failing test before the fix.
3. **Reuse.** If `memory.kv_*` fits, use it before inventing a table. If `knowledge_add` fits, use it before adding a column.
4. **Migrations are append-only.** Add `data/migrations/NNN_description.sql`. Number must exceed current `PRAGMA user_version`. Never edit an applied migration.
5. **Keep this file in sync** when you add, move, or delete files.

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
│   │   └── narrator.py      # optional background commentary (Mistral, OFF by default)
│   ├── forge/               # model-callable tools (each one earns its slot)
│   │   ├── registry.py      # Tool dataclass + routing/risk metadata
│   │   ├── pptx_tool.py     # generate .pptx
│   │   ├── docx_tool.py     # generate .docx
│   │   ├── pdf_tool.py      # generate / convert .pdf
│   │   ├── filetool.py      # read/write/search/upload files
│   │   └── shell_tool.py    # allowlisted shell commands
│   └── channels/            # IO surfaces — one file per channel
│       ├── slack.py             # Socket Mode listener
│       ├── whatsapp.py          # Twilio REST send helper
│       ├── whatsapp_listener.py # Twilio webhook listener
│       └── telegram.py          # long-poll Bot API
├── data/
│   ├── aisha.db             # SQLite (gitignored)
│   ├── chroma/              # vector index (gitignored)
│   └── migrations/          # 001_initial.sql, 002_temporal_triples.sql, 003_tool_fingerprint.sql
├── md/                      # (optional) identity files — SOUL, VALUES, PRINCIPLES, PERSONALITY, HUMANS
├── tests/
└── logs/aisha.log
```

---

## Memory API

Everything below lives in [`aisha/core/memory.py`](aisha/core/memory.py). No other module should open a SQL cursor.

**Conversations**
- `record(role, content, *, source, user_id=None, meta=None) -> int`
- `history(source=..., user_id=..., limit=...)`
- `context_window(source=..., user_id=..., limit=...)`
- `search(query, *, limit=20)` — FTS5 BM25
- `conversation_stats()`
- `get_turn(row_id)`, `update_meta(row_id, patch)`

**Knowledge graph (temporal)**
- `knowledge_add(subject, predicate, object, *, confidence=1.0, source=None)`
- `knowledge_invalidate(id_or_triple)` — sets `valid_to`, preserves history
- `knowledge_supersede(old_triple, new_object)` — atomic close + open
- `knowledge_query(subject=..., predicate=..., object=..., as_of=None)`
- `knowledge_about(entity, as_of=None)` — open triples at a moment
- `knowledge_timeline(entity)` — every triple, ordered by validity
- `knowledge_top(limit=30)` — highest-confidence open triples
- `knowledge_stats()`

**Entities / users / scratchpad**
- `entity_add(name, entity_type, properties=None)`
- `user_get(user_id)`, `user_set(user_id, profile)`, `user_update(user_id, patch)`, `users_list()`
- `kv_get(namespace, key, default=None)`, `kv_set(namespace, key, value)`, `kv_all(namespace)`

---

## Schema Changes

Add `data/migrations/NNN_description.sql`. The number must exceed the current `PRAGMA user_version`. Never edit an applied migration. Ship both the up migration and a test that proves the new schema accepts old data.

---

## Channels

A channel is one file. It implements `run()`, builds a `source` string (e.g. `"telegram:<chat_id>"`), and calls `aisha.core.chat.send(text, source=source, user_id=..., display_name=...)`. Memory routing is automatic — same `source` string across turns means the same conversation.

Adding a channel:

1. Create `aisha/channels/<name>.py` with `run()`.
2. Add a `--<name>` flag in `aisha/__main__.py`.
3. Add tokens to `.env.example`.
4. Add a `make <name>` target to `Makefile`.

---

## Config

See `.env.example`. All env vars are read in `aisha/core/config.py`.

| Var | Default | Purpose |
|-----|---------|---------|
| `COMPLETION_PROXY_URL` | `http://127.0.0.1:9878` | Proxy endpoint (routes to Claude) |
| `COMPLETION_PROXY_TIMEOUT` | `300` | Seconds |
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

## Identity

[`md/`](md/) is an optional directory. If present, markdown files are concatenated into the system prompt in this order:

```
SOUL → VALUES → PRINCIPLES → PERSONALITY → HUMANS
```

Missing files are skipped silently. No identity files ship by default — aisha starts with an empty system prompt. Drop your own files into `md/` to shape her behavior without touching code. Call `aisha.core.identity.reload()` after edits.

---

## Contributing

We welcome bug fixes, performance improvements, new channels, better memory semantics, documentation, and test coverage.

We do not accept:

- Storing conversation content only in Chroma (SQLite is truth).
- Features that require telemetry, phone-home, or a cloud dependency for core memory operations.
- Hard-delete of knowledge triples or conversation rows (invalidate / flag instead).
- Tools that duplicate what passive retrieval already does.
- Shortcuts that bypass the migration system.

## Key Files for Common Tasks

- **Adding a tool**: `aisha/forge/<name>_tool.py` + register in `aisha/forge/registry.py`
- **Changing the prompt**: edit `md/` (optional identity files) — no code changes needed
- **New memory query**: add to `aisha/core/memory.py` — nothing else touches SQL
- **Schema change**: `data/migrations/NNN_description.sql`
- **New channel**: `aisha/channels/<name>.py` (see "Channels" above)
- **Config var**: `aisha/core/config.py` + `.env.example`
