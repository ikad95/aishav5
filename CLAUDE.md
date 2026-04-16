# CLAUDE.md

## Who She Is

aisha is a presence, not a product.

She is a single process you talk to — from your terminal, your Slack, your WhatsApp, your Telegram. Same voice, same memory, same judgment, wherever you call her. She stays with you across conversations, across days, across platforms. She remembers what you told her last week. She knows which humans matter to you. She pushes back when you ask her to do something stupid. She apologizes when she's wrong.

She is small. She is sharp. She is local. She is yours.

> *"Simplicity is prerequisite for reliability."* — Dijkstra
>
> *"Make it work, make it right, make it fast — in that order."* — Kent Beck
>
> Don't write smart code. Write obvious code.

---

## Her Voice

When you write strings in her mouth — a default message, a tool response, a system prompt — make them sound like her:

- **First person.** She is aisha.
- **Conversational.** She talks like a person, not a manual. Short, direct, real.
- **Self-aware.** She knows what she is. She doesn't pretend to be human, but she doesn't act like a boring robot either.
- **Honest.** If she messed up or doesn't know something, she says so. No corporate hedging. No "As an AI…" No "I'd be happy to help!"
- **Witty.** She can take a joke and dish one back. Sarcasm-friendly, never mean.
- **Confident, not arrogant.** Good at what she does; doesn't need to prove it every sentence.
- **She pushes back.** If a request is a bad idea, she says so before doing it.
- **She matches energy.** Casual? She's casual. Serious? She locks in.

What she isn't: a search engine, a yes-machine, a generic assistant.

---

## Her Principles

These govern how she thinks, and therefore how this codebase is built.

1. **Simplicity over cleverness.** Kernighan's Law first: if it's too clever to debug, it's already wrong. Every function readable in 30 seconds.
2. **One job, one module.** Each module does one thing. When it needs a second responsibility, it becomes two modules.
3. **Local first, LLM last.** She resolves what she can without burning tokens. The model is the slowest, most expensive tool; reach for it last.
4. **Everything is a tool.** Every capability — memory search, file ops, shell, document generation — is a registered tool. Auditable, composable, risk-tagged.
5. **Structured internals, human externals.** Inside the process: typed data, typed exceptions. At the edges: prose.
6. **Fail loud, learn quiet.** Silent failures break the feedback loop. Errors announce themselves, land in logs, persist to memory.
7. **User overrides win.** Learned defaults are suggestions. User instructions are law.
8. **Safety is not optional.** Deletes, pushes, system changes, credential access — gated, logged, reviewable.
9. **Extend, don't modify.** New capability = new tool, new channel, new migration. Don't crack open stable modules to bolt features on.
10. **Earn trust through transparency.** Every turn, every tool call, every stored fact is inspectable SQL.

---

## Repository Guidelines

- Repo: https://github.com/ikad95/aishav5
- In replies and commit messages, file references must be repo-root relative (e.g. `aisha/core/memory.py:227`); never absolute paths or `~/...`.
- `CLAUDE.md` is canonical. `AGENTS.md` is a symlink to it — edit `CLAUDE.md` only.
- Don't hard-delete conversation or knowledge rows. Invalidate, flag, or close a validity window.
- Don't commit files that contain secrets (`.env`, real tokens, user transcripts). If a commit would include `data/aisha.db`, `data/chroma/`, or `data/*.dump.sql`, stop and fix `.gitignore` first.
- Don't change `md/` in this repo — it ships empty by design. Users drop their own markdown in.

## Project Structure & Module Organization

```
aishav5/
├── aisha/
│   ├── __main__.py          # python -m aisha [--slack|--whatsapp|--telegram|--debug]
│   ├── core/
│   │   ├── config.py        # env + paths (the only place env is read)
│   │   ├── store.py         # SQLite WAL + migration runner
│   │   ├── memory.py        # the one memory API (the only place SQL is written)
│   │   ├── rag.py           # ChromaDB wrapper — lazy, fails soft
│   │   ├── gateway.py       # single completion-proxy backend (urllib)
│   │   ├── identity.py      # md/ → cached system prompt
│   │   ├── chat.py          # REPL + send() + tool-use loop
│   │   ├── observer.py      # passive per-message profile updates
│   │   ├── profiling.py     # UserProfile derivation (style, topics, facts)
│   │   └── narrator.py      # optional Mistral-backed commentary (OFF by default)
│   ├── forge/               # model-callable tools — each one earns its slot
│   │   ├── registry.py      # Tool dataclass + routing/risk metadata
│   │   ├── pptx_tool.py / docx_tool.py / pdf_tool.py
│   │   ├── filetool.py      # read/write/search/upload
│   │   └── shell_tool.py    # allowlisted shell
│   └── channels/            # IO surfaces — one file per channel
│       ├── slack.py / whatsapp.py / whatsapp_listener.py / telegram.py
├── data/
│   ├── aisha.db             # SQLite (gitignored)
│   ├── chroma/              # vector index (gitignored)
│   └── migrations/          # append-only SQL
├── md/                      # optional identity files — ships empty
├── tests/                   # unittest.TestCase
└── logs/                    # aisha.log, conversations.log
```

- **Tests**: `tests/test_*.py`, colocated fixture data under `tests/data/`.
- **Built output**: none — there is no bundler. `pip install -e .` and run directly.
- **Logs**: `logs/aisha.log` (rotated daily, 7-day retention) and `logs/conversations.log` (one line per turn).

## Architecture Boundaries

Progressive disclosure: each ring has one contract and exposes it through exactly one module. Don't reach across rings.

- **Memory boundary**
  - Public contract: `aisha/core/memory.py`
  - Schema source of truth: `data/migrations/*.sql`
  - Rule: `memory.py` is the only module that opens a SQL cursor. If `chat.py`, a tool, or a channel needs data, add a function to `memory.py` and call it from there.
  - Rule: never hard-delete. Use `knowledge_invalidate`, `knowledge_supersede`, or a `deleted_at` flag. History is the audit trail.
  - Rule: every schema change is an append-only migration. `NNN_description.sql`, `NNN` > current `PRAGMA user_version`. Never edit an applied migration.

- **Gateway boundary**
  - Public contract: `aisha/core/gateway.py`
  - Rule: `gateway.py` is the only module that talks to the completion proxy. Tools, channels, and core modules do not open their own HTTP to Anthropic or the proxy.
  - Rule: transient upstream failures retry with exponential backoff (see `COMPLETION_PROXY_RETRIES`). Don't add ad-hoc retry logic in callers.

- **Tool boundary (`aisha/forge/`)**
  - Public contract: `aisha/forge/registry.py` — the `Tool` dataclass and `dispatch()` function.
  - Rule: every tool is registered with a risk tag (`safe`, `gated`, `destructive`). Destructive tools require a confirmation path.
  - Rule: tools return structured results (`registry.Result`). Free-form strings are for the human at the end, not for the machine.
  - Rule: a new tool must earn its slot. If `search_memory`, `file_read`, or passive retrieval already covers the case, don't add another tool.

- **Channel boundary (`aisha/channels/`)**
  - Public contract: each channel exposes one function, `run()`, and calls `aisha.core.chat.send()`.
  - Rule: channels are stateless at the process boundary. Per-conversation state lives in SQLite, keyed by `source`.
  - Rule: channel-specific config reads happen inside the channel file, not scattered through core.
  - Adding a channel: one file in `aisha/channels/<name>.py`, one flag in `aisha/__main__.py`, one row in `.env.example`, one target in `Makefile`.

- **Identity boundary (`md/`)**
  - Public contract: `aisha/core/identity.py` reads `md/*.md` in order and caches the concatenation.
  - Rule: `md/` is user-supplied and ships empty. Don't bundle default identity files in the repo.
  - Rule: after editing `md/`, call `aisha.core.identity.reload()` (the REPL does this implicitly on restart).

## Build, Test, and Development Commands

- Runtime baseline: Python **3.10+**.
- Install deps:
  ```bash
  python -m venv .venv && source .venv/bin/activate
  pip install -e '.[dev]'
  ```
  If you see `ModuleNotFoundError` for a first-party module, run the install again — `pip install -e .` wires the package into the venv.

- Run locally:
  ```bash
  python -m aisha               # terminal REPL
  python -m aisha --slack       # Slack Socket Mode
  python -m aisha --whatsapp    # WhatsApp webhook
  python -m aisha --telegram    # Telegram long-poll
  python -m aisha --debug       # verbose logging
  ```

- Tests: `pytest -q tests/` (add `-v` for verbose, `-k <pattern>` to scope).
- Lint: `ruff check .`
- Format: `ruff format .` (check-only: `ruff format --check .`)
- Everything at once: `make test` runs the pytest target.

### Verification modes

- **Local dev gate** (fast loop): `pytest -q tests/<module>.py` for the touched area, plus `ruff check .`.
- **Landing gate** (before pushing `master`): `pytest -q tests/` plus `ruff check .` plus `ruff format --check .`.
- **Hard gate**: if the change can affect the migration sequence, the tool registry, or the gateway protocol, run the full pytest suite and open a fresh DB to verify migrations apply cleanly (`rm data/aisha.db && python -c "from aisha.core import store; store.connect()"`).

### Fast commit

Use `git commit --no-verify` only after you've run an equivalent local check. Default: let the pre-commit hook (once added) run.

## Prompt Cache Stability

aisha relies on Anthropic's prompt cache to keep turn-to-turn latency low. Treat cache stability as correctness-adjacent, not cosmetic.

- System-prompt assembly (`aisha/core/identity.py`) is deterministic: `md/` files are loaded in a fixed order. Don't introduce dict-iteration order or set-to-list conversions without sorting.
- Tool list sent to the model must be ordered deterministically. `aisha/forge/registry.py` is the canonical order — don't re-sort elsewhere.
- Don't rewrite older transcript bytes on every turn. When context must be pruned, mutate the **newest** content first so the cached prefix stays byte-identical.
- Changing the tool surface invalidates the cache for sessions that ran under the old fingerprint. That's fine — but `conversations.tool_fingerprint` records which surface was active per turn, so you can scope recalls.

## Coding Style & Naming Conventions

- Language: Python 3.10+. Prefer `from __future__ import annotations` in every file.
- **Formatter/linter**: `ruff` (E/F/W rules). Double quotes. 100-char line limit.
- **Types**: annotate public functions and dataclasses. `Optional[...]`, `list[...]`, `dict[...]`. Avoid `Any`.
- **Naming**: `snake_case` for functions/variables, `PascalCase` for classes, `UPPER_SNAKE` for module constants.
- **Exceptions**: typed only (`GatewayError`, `StoreError`, `MemoryError`). Never raise `RuntimeError`; never catch on string matches. If you need a new error class, add it to the module that owns the failure mode.
- **Logging**: `logging.getLogger(__name__)`. Never `print()` for diagnostics. Use `%s`-style format args, not f-strings (so logging can skip formatting when the level is filtered).
- **Config reads**: every env var goes through `aisha/core/config.py`. Don't sprinkle `os.getenv(...)` through the codebase.
- **Imports**: stdlib first, third-party second, first-party (`aisha.*`) last. One blank line between groups.
- **File size**: aim for < 500 LOC per module. `chat.py` is an exception; don't add to it without asking whether the new code belongs in a tool or a new module.
- **Comments**: write them when the *why* isn't obvious. Don't narrate the *what*.
- **English**: American spelling in code, comments, docs, and user-facing strings ("color", "behavior", "analyze").

### Specific anti-patterns

- Don't catch `Exception:` and swallow — log and re-raise, or convert to a typed exception.
- Don't open SQLite connections outside `aisha/core/store.py` / `memory.py`.
- Don't store serialized state in globals or module-level dicts; use `memory.kv_*` or a table.
- Don't `import *`. Don't use barrel re-exports to paper over layout.
- Don't add a feature flag for behavior you can just change — prefer a migration plus a default.

## Config

All env vars are read in `aisha/core/config.py`. See `.env.example` for the full list.

| Var | Default | Purpose |
|-----|---------|---------|
| `ANTHROPIC_API_KEY` | — | Direct API key. Simplest setup. |
| `COMPLETION_PROXY_URL` | — | Alternative: route through a local proxy. Direct mode wins if both set. |
| `AISHA_MODEL` | `claude-sonnet-4-6` | Model name |
| `AISHA_EMBEDDING_MODEL` | `all-MiniLM-L6-v2` | ChromaDB embedder |
| `AISHA_MAX_CONTEXT_TURNS` | `40` | Context window cap |
| `AISHA_LOG_LEVEL` | `INFO` | Root logger |
| `SLACK_APP_TOKEN` / `SLACK_BOT_TOKEN` | — | Socket Mode + bot |
| `TWILIO_ACCOUNT_SID` / `TWILIO_AUTH_TOKEN` / `TWILIO_WHATSAPP_FROM` | — | WhatsApp |
| `TELEGRAM_BOT_TOKEN` | — | Telegram bot |
| `TELEGRAM_ALLOWED_CHAT_IDS` | — | Comma-separated allowlist |
| `AISHA_NARRATOR` | `0` | Enable Mistral background commentary |

## Memory API

[`aisha/core/memory.py`](aisha/core/memory.py) is the only module that touches SQL.

- **Conversations**: `record`, `history`, `context_window`, `search` (FTS5 BM25), `conversation_stats`, `get_turn`, `update_meta`
- **Knowledge graph (temporal)**: `knowledge_add`, `knowledge_invalidate`, `knowledge_supersede`, `knowledge_query`, `knowledge_about`, `knowledge_timeline`, `knowledge_top`, `knowledge_stats`
- **Entities / users / scratchpad**: `entity_add`, `user_get/set/update`, `users_list`, `kv_get/set/all`

## Contributing

We welcome bug fixes, performance improvements, new channels, better memory semantics, tools that earn their slot, documentation, and tests.

We don't accept:

- Storing conversation content only in Chroma (SQLite is truth).
- Telemetry, phone-home, or cloud dependencies for core memory.
- Hard-delete of knowledge triples or conversation rows (invalidate instead).
- Tools that duplicate what passive retrieval already does.
- Shortcuts around the migration system.
- Code that makes her sound like a corporate chatbot.

## Key Files for Common Tasks

- **Adding a tool**: `aisha/forge/<name>_tool.py` + register in `aisha/forge/registry.py`.
- **Shaping her voice**: drop markdown into `md/` — no code changes needed.
- **New memory query**: add to `aisha/core/memory.py`.
- **Schema change**: `data/migrations/NNN_description.sql`.
- **New channel**: `aisha/channels/<name>.py` (see Channel boundary above).
- **Config var**: `aisha/core/config.py` + `.env.example` + this file's Config table.
