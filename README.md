<div align="center">

# aisha

**An AI assistant built around memory.**
Same brain across your terminal, Slack, WhatsApp, and Telegram — persistent, queryable, local.

[![python](https://img.shields.io/badge/python-3.10+-blue?style=flat-square)](https://www.python.org/)
[![license](https://img.shields.io/badge/license-MIT-green?style=flat-square)](LICENSE)

</div>

---

## What it is

aisha is a single process with a purpose-built memory system and a thin tool surface. Every conversation, every recalled fact, every user profile, every tool invocation is a row in local SQLite — indexed four ways, full-text searchable, with a temporal knowledge graph layered on top.

Memory isn't a vector store bolted on the side. It's the architecture:

- **Conversations** — every turn (user, assistant, tool call, tool result, system, error, self-reflection) stored verbatim with session, source, user, timestamp, role, and a JSON meta bag. Indexed by session, source, user, and tool-surface fingerprint. FTS5 on top, BM25-ranked, scoped by any of those facets.
- **Knowledge graph** — RDF triples (subject, predicate, object) with confidence scores *and validity windows*. Facts have lifespans. Supersede a fact and both versions stay queryable — `alice lives_in Paris` (2023→2025) and `alice lives_in Berlin` (2025→) coexist without conflict.
- **Users** — per-user profile (style, topics, facts, mood), updated passively from every message. No prompts, no forms — the profile keeps itself current.
- **Scratchpad** — namespaced key/value for routing affinities, pattern stats, calibration, and anything else the agent wants to remember about itself.
- **Vector recall** — ChromaDB index over conversation content. Rebuildable from SQLite. Canonical state lives in exactly one place.

Tools are curated, not exhaustive. The model reaches for them only when passive retrieval doesn't cover the question.

---

## Memory, concretely

The schema is intentionally flat and typed. You can `sqlite3 data/aisha.db` and inspect everything — nothing is hidden in opaque blobs.

### Conversations

```sql
conversations(id, session_id, source, user_id, ts, role, content, meta, tool_fingerprint)
```

- `source` is the origin (`terminal`, `slack:C0XXXX:...`, `whatsapp:+14155...`, `telegram:123456`, etc.) — same field across all channels.
- `role` includes `user`, `assistant`, `tool`, `system`, `error`, **and `reflection`** — aisha writes notes to herself after each turn.
- `conversations_fts` is an FTS5 shadow table kept in sync by triggers. You can query `"owl OR Tyto"` scoped to a single Slack channel or user in a single SQL call.
- `tool_fingerprint` records which tool surface was active when the turn happened — so you can retrieve sessions that ran with or without a given capability.

### Knowledge graph

```sql
knowledge(id, subject, predicate, object, confidence, source, ts, valid_from, valid_to)
```

- A unique index on `(subject, predicate, object) WHERE valid_to IS NULL` means at most one *open* triple per fact. Re-asserting bumps the timestamp and confidence.
- `knowledge_invalidate()` closes a fact (sets `valid_to`) without deleting it. History stays intact.
- `knowledge_supersede()` closes the old fact and opens a new one in one transaction.
- `knowledge_timeline(entity)` returns every triple involving an entity, ordered by validity — a full history of what you (or aisha) believed, when.
- `knowledge_about(entity, as_of=T)` returns every *open* triple at a point in time.

This is not a bag of "memories." It's a graph with time.

### Users & scratchpad

```sql
users(user_id, profile, updated_at)    -- JSON profile, passively updated
kv(namespace, key, value, updated_at)  -- namespaced small state
```

The `kv` table is how aisha remembers herself — routing decisions, calibration curves, human-model intent shortcuts, pattern success/failure counts. Everything an agent learns about its own performance is inspectable SQL.

---

## Channels

Every channel writes to the same memory. A conversation started in Slack is searchable from the terminal. A fact learned over WhatsApp is available to the Telegram bot.

| Channel | Transport | Credentials |
|---|---|---|
| Terminal | stdin/stdout | — |
| Slack | Socket Mode (WebSocket) | `SLACK_APP_TOKEN`, `SLACK_BOT_TOKEN` |
| WhatsApp | Twilio webhook | `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_WHATSAPP_FROM` |
| Telegram | Long-poll | `TELEGRAM_BOT_TOKEN` (+ optional `TELEGRAM_ALLOWED_CHAT_IDS`) |

Each channel is a single file in [`aisha/channels/`](aisha/channels/). Adding a new one: implement `run()`, call `aisha.core.chat.send()`, done.

## Tools

Small by design. Registered in [`aisha/forge/`](aisha/forge/), risk-tagged, and individually gated.

| Tool | Purpose |
|---|---|
| `search_memory` | FTS5 + semantic search over every stored turn |
| `remember` | Write a `(subject, predicate, object)` triple with confidence |
| `shell` | Allowlisted shell commands, logged |
| `file_read` / `file_write` / `file_search` | Local filesystem |
| `web_fetch` | Fetch a URL and convert to plain text |
| `generate_pptx` / `generate_docx` / `generate_pdf` | Structured document generation |

The brain is stateless at the process boundary — tools don't hold state, memory does.

---

## Install

```bash
git clone https://github.com/ikad95/aishav5.git aisha
cd aisha
make install
cp .env.example .env   # fill in proxy URL + any channel tokens
```

aisha talks to Claude through a completion proxy. Point `COMPLETION_PROXY_URL` at your own proxy or at Anthropic directly.

## Run

```bash
make repl        # interactive REPL
make slack       # Slack Socket Mode listener
make whatsapp    # WhatsApp webhook
make telegram    # Telegram long-poll bot
make test        # pytest
```

## Layout

```
aisha/
├── core/        chat loop, memory, gateway, identity, rag, store,
│                observer, profiling, narrator
├── forge/       tool registry + implementations
└── channels/    slack, whatsapp, telegram
data/
├── aisha.db     SQLite — canonical state (WAL, gitignored)
├── chroma/      vector index — derived, rebuildable
└── migrations/  numbered SQL, applied once, never edited
```

## Requirements

- Python 3.10+
- A completion proxy reachable at `COMPLETION_PROXY_URL` (default `http://127.0.0.1:9878`)
- ~300 MB disk for the default embedding model (`all-MiniLM-L6-v2`)

## License

MIT — see [LICENSE](LICENSE).
