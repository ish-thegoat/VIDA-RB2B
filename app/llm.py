"""Thin Anthropic client wrapper shared by the ICP gate and copy generation.

Uses the official `anthropic` SDK. The Messages API takes `system` as a
top-level param, so callers that carry a system role inside a `messages` array
(the approved prompt files do) should use `split_system()` first.
"""
from __future__ import annotations

from typing import Optional

from . import config

_CLIENT = None


def _client():
    global _CLIENT
    if _CLIENT is None:
        import anthropic  # imported lazily so tooling without the dep still loads
        _CLIENT = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    return _CLIENT


def split_system(messages: list[dict]) -> tuple[str, list[dict]]:
    """Pull role=='system' messages out of a messages array into a single system
    string, returning (system, non_system_messages)."""
    system_parts, rest = [], []
    for m in messages:
        if m.get("role") == "system":
            system_parts.append(m.get("content", ""))
        else:
            rest.append(m)
    return "\n\n".join(p for p in system_parts if p), rest


def complete(
    messages: list[dict],
    model: str,
    max_tokens: int = 1024,
    temperature: float = 0.3,
    system: Optional[str] = None,
) -> str:
    """Run one Messages call and return the concatenated text output."""
    if system is None:
        system, messages = split_system(messages)
    kwargs = dict(model=model, max_tokens=max_tokens, temperature=temperature, messages=messages)
    if system:
        kwargs["system"] = system
    resp = _client().messages.create(**kwargs)
    return "".join(block.text for block in resp.content if getattr(block, "type", "") == "text").strip()
