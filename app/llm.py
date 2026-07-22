"""LLM client wrapper shared by the ICP gate, research, and copy generation.

Primary route: OpenRouter (OPENROUTER_API_KEY), targeting the same Claude model
the copy is tuned for. This is a separate billing account from the direct
Anthropic key, so an Anthropic credit lapse doesn't take the whole pipeline
down. Falls back to the Anthropic SDK directly if OpenRouter is unset or a
request errors — mirrors the primary/fallback pattern already used elsewhere
in the pipeline (openrouter_client.py).

The Anthropic Messages API takes `system` as a top-level param, so callers that
carry a system role inside a `messages` array (the approved prompt files do)
should use `split_system()` first.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

from . import config

log = logging.getLogger("rb2b.llm")

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

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


def _openrouter_request(system: str, messages: list[dict], model: str,
                        max_tokens: int, temperature: float) -> str:
    import httpx

    or_messages = []
    if system:
        or_messages.append({"role": "system", "content": system})
    for m in messages:
        or_messages.append({"role": m.get("role", "user"), "content": m.get("content", "")})

    payload = {"model": model, "messages": or_messages,
               "max_tokens": max_tokens, "temperature": temperature}
    headers = {"Authorization": f"Bearer {config.OPENROUTER_API_KEY}",
               "Content-Type": "application/json"}

    last_err: Exception | None = None
    for attempt in range(3):
        try:
            with httpx.Client(timeout=90.0) as client:
                resp = client.post(OPENROUTER_URL, json=payload, headers=headers)
            if resp.status_code == 429 and attempt < 2:
                time.sleep(2 * (attempt + 1))
                continue
            if resp.status_code >= 400:
                raise RuntimeError(f"OpenRouter {resp.status_code}: {resp.text[:300]}")
            data = resp.json()
            choice = (data.get("choices") or [{}])[0]
            return (choice.get("message", {}).get("content") or "").strip()
        except Exception as e:
            last_err = e
            if attempt == 2:
                raise
    raise last_err  # type: ignore[misc]


def _anthropic_complete(messages: list[dict], model: str, max_tokens: int,
                        temperature: float, system: str) -> str:
    kwargs = dict(model=model, max_tokens=max_tokens, temperature=temperature, messages=messages)
    if system:
        kwargs["system"] = system
    resp = _client().messages.create(**kwargs)
    return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()


def complete(
    messages: list[dict],
    model: str,
    max_tokens: int = 1024,
    temperature: float = 0.3,
    system: Optional[str] = None,
) -> str:
    """Run one completion and return the text output. Tries OpenRouter first
    (if configured), falls back to Anthropic direct on any OpenRouter error."""
    if system is None:
        system, messages = split_system(messages)

    if config.OPENROUTER_API_KEY:
        try:
            return _openrouter_request(system, messages, config.OPENROUTER_MODEL,
                                       max_tokens, temperature)
        except Exception as e:
            log.warning("OpenRouter request failed (%s); falling back to Anthropic direct", e)
            if not config.ANTHROPIC_API_KEY:
                raise

    return _anthropic_complete(messages, model, max_tokens, temperature, system)


def complete_with_search(
    messages: list[dict],
    model: str,
    system: Optional[str] = None,
    max_tokens: int = 900,
    max_uses: int = 4,
) -> str:
    """Like complete(), but grounds the response in live web search.

    OpenRouter path uses the ":online" model suffix (its web-search plugin).
    Anthropic-direct fallback uses the server-side web_search tool and handles
    the pause_turn continuation loop.
    """
    if system is None:
        system, messages = split_system(messages)

    if config.OPENROUTER_API_KEY:
        try:
            return _openrouter_request(system, messages, f"{config.OPENROUTER_MODEL}:online",
                                       max_tokens, 0.3)
        except Exception as e:
            log.warning("OpenRouter :online request failed (%s); falling back to Anthropic direct", e)
            if not config.ANTHROPIC_API_KEY:
                raise

    client = _client()
    tools = [{"type": "web_search_20260209", "name": "web_search", "max_uses": max_uses}]
    base = dict(model=model, max_tokens=max_tokens, tools=tools)
    if system:
        base["system"] = system
    msgs = list(messages)
    resp = client.messages.create(messages=msgs, **base)
    guard = 0
    while getattr(resp, "stop_reason", "") == "pause_turn" and guard < 3:
        msgs.append({"role": "assistant", "content": resp.content})
        resp = client.messages.create(messages=msgs, **base)
        guard += 1
    return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
