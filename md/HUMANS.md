# HUMANS.md — Human-Agent Interface for aish

## Overview

aish exists to serve the human at the terminal. This document defines how humans interact with the agentic layer, what controls they have, and how the system respects human authority at every level.

---

## Interaction Modes

### 1. Native Mode (Default)

The human types normal shell commands. aish passes them directly to zsh. Zero latency overhead, zero agent involvement.

```
$ ls -la
$ git status
$ docker compose up -d
```

### 2. Agentic Mode (Explicit)

The human invokes the agentic pipeline with the `ai` prefix:

```
$ ai find all files larger than 100MB and delete the ones not accessed in 90 days
$ ai refactor this function to be async
$ ai why did my last deploy fail
```

### 3. Hybrid Mode (Learned)

Over time, aish learns when the human *probably* wants agentic assistance and offers it:

```
$ grep -r "TODO" src/
  → aish: found 47 TODOs. want me to categorize them by priority? [y/n]
```

This mode is **opt-in** and **non-intrusive** — suggestions appear as completions, never blocking.

---

## Human Authority Levels

| Level | Description | Agent Capability |
|---|---|---|
| **OBSERVE** | Agents can read context but not execute | Planning, suggestions only |
| **SUGGEST** | Agents propose commands, human must approve each one | Default for new installs |
| **EXECUTE** | Agents can run non-destructive commands autonomously | After trust is established |
| **FULL** | Agents can run any command (including destructive) | Explicit opt-in, per-session |

Authority is set in `~/.aish/config.toml`:

```toml
[authority]
default = "suggest"
trusted_domains = ["git", "npm", "cargo"]  # these can auto-execute
dangerous = ["rm", "dd", "mkfs", "DROP"]    # always require approval
```

---

## Consent & Transparency

### The Human Always Knows

1. **Execution Preview**: Before running agent-planned commands, aish shows exactly what will execute
2. **Trajectory Viewer**: `aish --trace` shows the full forward pass (which agents ran, what they decided)
3. **Gradient Viewer**: `aish --grads` shows what the system learned from the last interaction
4. **Weight Inspector**: `aish --weights` shows current learned parameters

### The Human Can Always Override

- `Ctrl+C` — immediate abort at any stage
- `aish --no-agent` — disable agentic features for the session
- `aish --reset-weights` — clear all learned parameters
- `aish --rollback N` — undo the last N weight updates

### The Human Owns Their Data

- All trajectories, weights, and gradients are stored locally in `~/.aish/`
- Nothing is sent externally except LLM API calls (which go to the configured provider)
- `aish --export` and `aish --import` for portable weight transfer
- `aish --purge` to delete everything aish has learned

---

## Feedback Mechanisms

### Explicit Feedback

The human can directly provide textual gradients:

```
$ ai fix the build
  → [agent executes fix]
$ aish --feedback "that worked but you should have checked the lockfile first"
```

This feedback is treated as a high-priority textual gradient and immediately enters the backward pass.

### Implicit Feedback

aish observes:
- Did the human accept the suggestion? (positive signal)
- Did the human modify the suggestion before running? (corrective signal)
- Did the human `Ctrl+C` or undo? (negative signal)
- Did the human run a different command after rejecting? (alternative signal)

These implicit signals are lower-weight but accumulate over time.

---

## Trust Calibration

aish builds trust incrementally:

```
Session 1:    SUGGEST mode — every action needs approval
Session 10:   Common patterns auto-execute (git add, npm install)
Session 50:   Most non-destructive commands auto-execute
Session 100+: Personalized authority based on learned user patterns
```

Trust can always be revoked. Trust never applies to commands in the `dangerous` list.

---

## Accessibility

- aish respects `$TERM`, `$COLORTERM`, and accessibility settings
- Screen reader compatible output modes
- Configurable verbosity for agent explanations
- All agentic features degrade gracefully if the LLM API is unreachable (falls back to pure zsh)
