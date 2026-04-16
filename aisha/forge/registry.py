"""Smart tool registry: namespacing, routing pre-filter, risk tagging.

Each tool registers once with its schema, handler, domain, and risk. At
call time the chat loop asks for the top-k tools relevant to the user
message — pinned tools always appear; the rest compete on keyword overlap
with the message. Keeps the model's tool menu short so selection latency
and mis-routes drop.

Risk is tagged on every tool but **logged only** in v1: hard approval
gating is a per-channel UX problem (terminal can prompt; slack/whatsapp
have no synchronous approval path) and lives behind a separate decision.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Callable, Optional

log = logging.getLogger(__name__)

DOMAINS = ("memory", "files", "comms", "web")
RISKS = ("safe", "gated", "dangerous")

# (args, source) -> result text. source is the same channel tag used elsewhere
# (terminal, slack:CHANNEL:THREAD, whatsapp:USER) so handlers can specialize.
Handler = Callable[[dict, str], str]

_TOKEN_RE = re.compile(r"[a-z0-9]{3,}")
# Tokens that match every tool's description add noise without signal.
_STOP = frozenset({
    "the", "and", "for", "with", "what", "have", "your", "this", "that",
    "from", "when", "who", "why", "how", "use", "uses", "used", "can",
    "you", "are", "tool", "tools", "call", "user", "model", "default",
    "returns", "optional", "required", "string", "integer", "number",
    "boolean", "array", "object", "type", "name", "description",
})


@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict
    handler: Handler
    domain: str
    risk: str = "safe"
    pinned: bool = False
    _tokens: frozenset = field(default_factory=frozenset, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.domain not in DOMAINS:
            raise ValueError(f"unknown domain {self.domain!r}; expected one of {DOMAINS}")
        if self.risk not in RISKS:
            raise ValueError(f"unknown risk {self.risk!r}; expected one of {RISKS}")
        text = f"{self.name} {self.description}".lower()
        toks = {t for t in _TOKEN_RE.findall(text) if t not in _STOP}
        self._tokens = frozenset(toks)

    @property
    def schema(self) -> dict:
        """Anthropic tool-use shape: name, description, input_schema."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


_REGISTRY: dict[str, Tool] = {}


def register(tool: Tool) -> Tool:
    if tool.name in _REGISTRY:
        raise ValueError(f"tool {tool.name!r} already registered")
    _REGISTRY[tool.name] = tool
    log.debug("registry: registered %s (domain=%s risk=%s pinned=%s)",
              tool.name, tool.domain, tool.risk, tool.pinned)
    return tool


def get(name: str) -> Optional[Tool]:
    return _REGISTRY.get(name)


def all_tools() -> list[Tool]:
    return list(_REGISTRY.values())


def clear() -> None:
    """Test-only: drop every registered tool."""
    _REGISTRY.clear()


def _query_tokens(text: str) -> set[str]:
    return {t for t in _TOKEN_RE.findall((text or "").lower()) if t not in _STOP}


def schemas_for(text: str, *, k: int = 12) -> list[dict]:
    """Return up to ``k`` tool schemas relevant to ``text``.

    Pinned tools are always included; remaining slots go to the highest
    keyword-overlap tools. Falls back to alphabetical when no tokens match.
    """
    pinned = [t for t in _REGISTRY.values() if t.pinned]
    rest = [t for t in _REGISTRY.values() if not t.pinned]
    qtoks = _query_tokens(text)
    if qtoks:
        rest.sort(key=lambda t: (-len(t._tokens & qtoks), t.name))
    else:
        rest.sort(key=lambda t: t.name)
    capacity = max(k - len(pinned), 0)
    chosen = pinned + rest[:capacity]
    log.debug("registry: exposing %d/%d tools (pinned=%d, qtoks=%d)",
              len(chosen), len(_REGISTRY), len(pinned), len(qtoks))
    return [t.schema for t in chosen]


def dispatch(name: str, args: dict, *, source: str = "") -> str:
    tool = _REGISTRY.get(name)
    if tool is None:
        return f"ERROR: unknown tool {name!r}"
    if tool.risk in ("gated", "dangerous"):
        log.warning("registry: %s tool=%s source=%s args=%s",
                    tool.risk, name, source, args)
    try:
        return tool.handler(args, source)
    except Exception as e:
        log.exception("registry: handler failed for %s", name)
        return f"ERROR: {e}"
