"""Slack digest — batches newly staged leads into one message, never per-lead
(addendum §4 Step 6). Posts to #vida-buzzlead-private-channel via chat.postMessage.

This is an internal staging notice, NOT an Aaron brief (those are for confirmed
meetings only, per standing rule).
"""
from __future__ import annotations

import json
import urllib.request
from typing import Iterable

from . import config, store

_POST_URL = "https://slack.com/api/chat.postMessage"


def _post(text: str) -> dict:
    data = json.dumps({"channel": config.SLACK_CHANNEL, "text": text,
                       "unfurl_links": False}).encode("utf-8")
    req = urllib.request.Request(
        _POST_URL, data=data, method="POST",
        headers={"Authorization": f"Bearer {config.SLACK_BOT_TOKEN}",
                 "Content-Type": "application/json; charset=utf-8"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _format(rows: Iterable) -> str:
    rows = list(rows)
    lines = [f"*Vida · RB2B — {len(rows)} new lead(s) staged (paused) for review*", ""]
    for r in rows:
        company = r["company_name"] or r["domain"] or "(unknown company)"
        seg = r["segment"] or "—"
        lines.append(
            f"• *{company}* — {seg} · tier {r['intent_tier']} · variant {r['variant']}\n"
            f"   {r['captured_url']}"
        )
    lines.append("")
    lines.append("Staged paused in EmailBison. Approve/activate in the workspace to send.")
    return "\n".join(lines)


def flush_digest() -> int:
    """Send one digest for all not-yet-notified staged leads. Returns count sent."""
    rows = store.pending_digest_rows()
    if not rows:
        return 0
    if not config.SLACK_BOT_TOKEN:
        # No token configured — mark as sent to avoid an unbounded backlog, but
        # this is logged by the caller. In practice SLACK_BOT_TOKEN is required.
        store.mark_digest_sent([r["id"] for r in rows])
        return 0
    result = _post(_format(rows))
    if result.get("ok"):
        store.mark_digest_sent([r["id"] for r in rows])
        return len(rows)
    raise RuntimeError(f"Slack error: {result.get('error')}")
