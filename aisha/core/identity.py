"""Load aisha's identity from md/ as a single system prompt."""
from __future__ import annotations

import logging
from functools import lru_cache

from .config import MD_DIR

log = logging.getLogger(__name__)

_ORDER = ("SOUL.md", "VALUES.md", "PRINCIPLES.md", "PERSONALITY.md", "HUMANS.md")


@lru_cache(maxsize=1)
def system_prompt() -> str:
    parts: list[str] = []
    for name in _ORDER:
        path = MD_DIR / name
        if not path.exists():
            continue
        body = path.read_text(encoding="utf-8").strip()
        parts.append(f"# {path.stem}\n\n{body}")
    prompt = "\n\n---\n\n".join(parts)
    log.info("identity: loaded %d sections (%d chars)", len(parts), len(prompt))
    return prompt


def reload() -> str:
    system_prompt.cache_clear()
    return system_prompt()
