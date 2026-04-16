"""User profile derivation — style, topics, mood, facts.

Ported verbatim from aisha's memory/user_profile.py so aisha's
profile JSON is byte-compatible with the parent. Persistence layer
is SQLite (via src.memory.user_get/user_set) instead of one-file-per-user.
"""
from __future__ import annotations

import datetime
import re
import time
from typing import Any, Optional


_TERSE_PATTERNS = [
    r"^.{1,20}$",
    r"^\w+$",
    r"^(?:y|n|yes|no|ok|k|yep|nah|sure|yeah|nope)$",
]
_VERBOSE_PATTERNS = [
    r".{200,}",
    r"(?:because|therefore|however|additionally|furthermore)",
]
_TECHNICAL_PATTERNS = [
    r"\b(?:api|http|tcp|udp|dns|ssl|tls|jwt|oauth|cors)\b",
    r"\b(?:async|await|thread|mutex|lock|semaphore|deadlock)\b",
    r"\b(?:docker|k8s|kubernetes|container|pod|deployment)\b",
    r"\b(?:git|branch|merge|rebase|commit|push|pull)\b",
    r"\b(?:sql|nosql|postgres|mongo|redis|kafka)\b",
    r"\b(?:lambda|closure|monad|functor|iterator|generator)\b",
    r"\b(?:kernel|syscall|inode|epoll|mmap|futex)\b",
]

_TERSE_RE = [re.compile(p, re.IGNORECASE) for p in _TERSE_PATTERNS]
_VERBOSE_RE = [re.compile(p, re.IGNORECASE) for p in _VERBOSE_PATTERNS]
_TECHNICAL_RE = [re.compile(p, re.IGNORECASE) for p in _TECHNICAL_PATTERNS]

_TOPIC_KEYWORDS = {
    "infrastructure": ["vm", "docker", "k8s", "kubernetes", "server", "deploy", "ci", "cd", "pipeline"],
    "frontend": ["react", "css", "html", "dom", "component", "ui", "ux", "tailwind", "next"],
    "backend": ["api", "rest", "graphql", "database", "sql", "endpoint", "server", "fastapi", "flask"],
    "ai_ml": ["model", "llm", "gpt", "claude", "training", "inference", "embedding", "vector", "rag"],
    "systems": ["kernel", "memory", "process", "thread", "syscall", "driver", "filesystem", "network"],
    "security": ["auth", "token", "encrypt", "ssl", "tls", "vulnerability", "permission", "firewall"],
    "mobile": ["android", "ios", "swift", "kotlin", "flutter", "react native", "mobile", "app"],
    "data": ["data", "analytics", "pipeline", "etl", "warehouse", "dashboard", "metrics", "chart"],
    "devops": ["git", "ci", "cd", "deploy", "monitor", "log", "alert", "incident", "sre"],
    "productivity": ["todoist", "task", "project", "plan", "schedule", "deadline", "priority"],
}


class UserProfile:
    """Persistent profile for a single user. State mutates in place."""

    def __init__(self, user_id: str, data: Optional[dict] = None):
        self.user_id = user_id
        d = data or {}
        self.display_name: str = d.get("display_name", "")
        self.first_seen: float = d.get("first_seen", time.time())
        self.last_seen: float = d.get("last_seen", time.time())
        self.interaction_count: int = d.get("interaction_count", 0)
        self.session_count: int = d.get("session_count", 0)
        self.verbosity: float = d.get("verbosity", 0.5)
        self.technical_level: float = d.get("technical_level", 0.5)
        self.avg_message_length: float = d.get("avg_message_length", 50.0)
        self.topics: dict[str, int] = d.get("topics", {})
        self.tool_usage: dict[str, int] = d.get("tool_usage", {})
        self.mood_signals: list[dict] = d.get("mood_signals", [])[-50:]
        self.preferences: dict[str, Any] = d.get("preferences", {})
        self.expertise: list[str] = d.get("expertise", [])
        self.learning: list[str] = d.get("learning", [])
        self.patterns: dict[str, Any] = d.get("patterns", {
            "active_hours": {},
            "avg_session_minutes": 0,
            "common_commands": {},
            "response_preference": "auto",
        })
        self.facts: list[dict] = d.get("facts", [])

    def to_dict(self) -> dict:
        return {
            "user_id": self.user_id,
            "display_name": self.display_name,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "interaction_count": self.interaction_count,
            "session_count": self.session_count,
            "verbosity": self.verbosity,
            "technical_level": self.technical_level,
            "avg_message_length": self.avg_message_length,
            "topics": self.topics,
            "tool_usage": self.tool_usage,
            "mood_signals": self.mood_signals[-50:],
            "preferences": self.preferences,
            "expertise": self.expertise,
            "learning": self.learning,
            "patterns": self.patterns,
            "facts": self.facts,
        }

    def observe_message(self, text: str, role: str = "user"):
        if role != "user":
            return
        self.last_seen = time.time()
        self.interaction_count += 1
        self._update_style(text)
        self._update_topics(text)
        hour = str(datetime.datetime.now().hour)
        self.patterns.setdefault("active_hours", {})
        self.patterns["active_hours"][hour] = self.patterns["active_hours"].get(hour, 0) + 1

    def observe_mood(self, signal: str, value: float):
        self.mood_signals.append({"signal": signal, "value": value, "ts": time.time()})
        self.mood_signals = self.mood_signals[-50:]

    def add_fact(self, fact: str, source: str = "conversation"):
        for f in self.facts:
            if f["fact"].lower() == fact.lower():
                f["ts"] = time.time()
                return
        self.facts.append({"fact": fact, "source": source, "ts": time.time()})

    def get_context_hint(self) -> str:
        hints = []
        if self.display_name:
            hints.append(f"Talking to {self.display_name}.")
        if self.verbosity < 0.3:
            hints.append("They prefer very concise responses.")
        elif self.verbosity > 0.7:
            hints.append("They like detailed explanations.")
        if self.technical_level > 0.7:
            hints.append("Highly technical — skip basics.")
        elif self.technical_level < 0.3:
            hints.append("Keep it simple, avoid jargon.")
        if self.expertise:
            hints.append(f"Expert in: {', '.join(self.expertise[:3])}.")
        if self.learning:
            hints.append(f"Currently learning: {', '.join(self.learning[:3])}.")
        for f in self.facts[-3:]:
            hints.append(f["fact"])
        return " ".join(hints) if hints else ""

    def _update_style(self, text: str):
        alpha = 0.1
        is_terse = any(p.search(text) for p in _TERSE_RE)
        is_verbose = any(p.search(text) for p in _VERBOSE_RE)
        if is_terse:
            self.verbosity = self.verbosity * (1 - alpha)
        elif is_verbose:
            self.verbosity = self.verbosity * (1 - alpha) + alpha
        tech_hits = sum(1 for p in _TECHNICAL_RE if p.search(text))
        if tech_hits > 0:
            tech_score = min(tech_hits / 3, 1.0)
            self.technical_level = self.technical_level * (1 - alpha) + tech_score * alpha
        self.avg_message_length = self.avg_message_length * (1 - alpha) + len(text) * alpha

    def _update_topics(self, text: str):
        low = text.lower()
        for topic, kws in _TOPIC_KEYWORDS.items():
            if any(kw in low for kw in kws):
                self.topics[topic] = self.topics.get(topic, 0) + 1
