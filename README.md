<div align="center">

# aisha

A small, opinionated AI assistant with persistent memory and a thin tool surface. Talk to it from your terminal, Slack, WhatsApp, or Telegram — it remembers across all of them.

[![python](https://img.shields.io/badge/python-3.10+-blue?style=flat-square)](https://www.python.org/)
[![license](https://img.shields.io/badge/license-MIT-green?style=flat-square)](LICENSE)

</div>

---

## What it is

aisha is a single process with four moving parts:

- **Chat loop** — streams Claude responses, runs tools, records every turn.
- **Memory** — SQLite (conversations, knowledge triples, users, scratchpad) + ChromaDB (semantic recall). Nothing leaves your machine.
- **Identity** — the files in [`md/`](md/) concatenate into the system prompt. Edit them to change how aisha behaves.
- **Channels** — terminal REPL by default, plus Slack (Socket Mode), WhatsApp (Twilio webhook), and Telegram (long-polling). Same brain, different surfaces.

Tools are registered, risk-tagged, and auditable. The default set is small on purpose: full-text search over history, a knowledge-graph writer, file read/write, shell commands, web fetch, and document generation (`.pptx` / `.docx` / `.pdf`).

---

## Install

```bash
git clone https://github.com/ikad95/aishav5.git aisha
cd aisha
make install
cp .env.example .env   # fill in your proxy URL + any channel tokens
```

aisha talks to Claude through a completion proxy — point `COMPLETION_PROXY_URL` at your own or at Anthropic directly.

## Run

```bash
make repl        # interactive REPL
make slack       # Slack listener (needs SLACK_APP_TOKEN + SLACK_BOT_TOKEN)
make whatsapp    # WhatsApp webhook (needs Twilio creds + public URL)
make telegram    # Telegram bot (needs TELEGRAM_BOT_TOKEN)
make test        # pytest
make clean       # wipe data/ and logs/
```

---

## Memory

All state lives under [`data/`](data/):

- `aisha.db` — SQLite, WAL mode. Tables: `conversations` (+ FTS5), `knowledge`, `entities`, `users`, `kv`.
- `chroma/` — semantic index over conversation turns. Rebuildable from SQLite.
- `migrations/` — numbered SQL files, applied once, never edited in place.

The memory API is [`aisha/core/memory.py`](aisha/core/memory.py). It is the only module that touches SQL — every other module goes through it.

## Identity

[`md/`](md/) holds the five files loaded into every system prompt, in order:

```
SOUL → VALUES → PRINCIPLES → PERSONALITY → HUMANS
```

Missing files are skipped silently. Edit them to reshape aisha's behavior — no code changes needed.

## Channels

| Channel | Transport | Token |
|---|---|---|
| Terminal | stdin/stdout | — |
| Slack | Socket Mode (WebSocket) | `SLACK_APP_TOKEN`, `SLACK_BOT_TOKEN` |
| WhatsApp | Twilio webhook | `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_WHATSAPP_FROM` |
| Telegram | Long-poll | `TELEGRAM_BOT_TOKEN` |

Each channel is a single file in [`aisha/channels/`](aisha/channels/). Adding a new one is a matter of implementing `run()` and calling `aisha.core.chat.send()`.

## Tools

Registered in [`aisha/forge/`](aisha/forge/). The full set is intentionally small; the model reaches for tools only when passive retrieval doesn't cover the question.

| Tool | Purpose |
|---|---|
| `search_memory` | FTS5 + semantic recall over conversation history |
| `remember` | Write a `(subject, predicate, object)` triple to the knowledge graph |
| `shell` | Execute a shell command (allowlisted, logged) |
| `file_read` / `file_write` | Read/write local files |
| `web_fetch` | Fetch and convert a URL to plain text |
| `generate_pptx` | Build a `.pptx` from a structured outline |
| `generate_docx` | Build a `.docx` from sections |
| `generate_pdf` | Build / convert to `.pdf` |

## Configuration

See [`.env.example`](.env.example) for the full list. The defaults boot a minimal terminal REPL; every channel and tool is off until you set its credential.

---

## Requirements

- Python 3.10+
- A completion proxy reachable at `COMPLETION_PROXY_URL` (default `http://127.0.0.1:9878`)
- ~300 MB disk for the default embedding model

## License

MIT — see [LICENSE](LICENSE).
