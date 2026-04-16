# aisha-spawn

aisha's younger sibling — rebuilt from scratch, opinionated, small.

> Make it work, make it right, make it fast — in that order.

## Install

```bash
make install
cp .env.example .env   # fill in ANTHROPIC_API_KEY
```

## Run

```bash
make repl     # interactive chat
make test     # pytest
make clean    # wipe data/ and logs/
```

## Layout

```
spawn/
├── core/        # chat loop, memory, identity
├── forge/       # tool registry + implementations
└── channels/    # Slack, WhatsApp, etc.
```

## Config

See `.env.example`. Required: `ANTHROPIC_API_KEY`. Everything else is optional.

## License

MIT.
