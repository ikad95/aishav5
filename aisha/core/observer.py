"""Non-episodic observation — passive user profile updates.

Mirrors aisha's memory.observer.Observer but compact: user profiling only
(no human_model / cognitive state). Every inbound message, responding or
not, flows through observe() so her model of each user keeps building.
"""
from __future__ import annotations

import logging
from typing import Optional

from . import memory
from .profiling import UserProfile

log = logging.getLogger(__name__)


def _load_profile(user_id: str) -> UserProfile:
    row = memory.user_get(user_id)
    data = row["profile"] if row else {}
    return UserProfile(user_id, data)


def _save(profile: UserProfile) -> None:
    memory.user_set(profile.user_id, profile.to_dict())


def observe(
    user_id: str,
    text: str,
    *,
    display_name: str = "",
    role: str = "user",
) -> None:
    """Update this user's profile from one message. Records silently."""
    if not user_id:
        return
    profile = _load_profile(user_id)
    # Overwrite when the stored name is empty OR a fallback (equal to the
    # user_id itself). Slack's user cache falls back to the raw ID when
    # profile lookup fails, so we must not lock that value in permanently.
    if display_name and profile.display_name in ("", profile.user_id):
        profile.display_name = display_name
    profile.observe_message(text, role=role)
    _save(profile)


def observe_tool_use(user_id: str, tool_name: str) -> None:
    if not user_id:
        return
    profile = _load_profile(user_id)
    profile.tool_usage[tool_name] = profile.tool_usage.get(tool_name, 0) + 1
    _save(profile)


def context_hint(user_id: Optional[str]) -> str:
    if not user_id:
        return ""
    profile = _load_profile(user_id)
    return profile.get_context_hint()


def mark_session(user_id: str, display_name: str = "") -> None:
    """Bump session_count on a fresh connection."""
    if not user_id:
        return
    profile = _load_profile(user_id)
    if display_name and not profile.display_name:
        profile.display_name = display_name
    profile.session_count += 1
    _save(profile)
