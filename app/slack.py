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
        sig = (r["signals"] if "signals" in r.keys() else "") or ""
        flag = "🔥 " if sig else ""
        line = f"• {flag}*{company}* — {seg} · tier {r['intent_tier']} · variant {r['variant']}"
        if sig:
            line += f"\n   signals: {sig}"
        line += f"\n   {r['captured_url']}"
        lines.append(line)
    lines.append("")
    lines.append("Staged paused in EmailBison. Approve/activate in the workspace to send.")
    return "\n".join(lines)


_OUTCOME_EMOJI = {
    "sent": "🟢", "staged": "🟢", "manual_review": "🟡",
    "low_intent_hold": "⏸️", "duplicate": "⏸️", "test_event": "⚪",
    "dropped_icp": "🔴", "error_push": "⛔", "error_copy": "⛔",
    "error_campaign_active": "⛔",
}


def notify_event(status: str, result: dict, sending: bool = False) -> None:
    """Post a single real-time message for one processed hit (SLACK_MODE=realtime).
    Non-fatal: Slack problems must never break the worker."""
    if config.SLACK_MODE != "realtime" or not config.SLACK_BOT_TOKEN:
        return
    emoji = _OUTCOME_EMOJI.get(status, "•")
    company = result.get("company_name") or result.get("domain") or "(unknown)"
    verb = {"staged": "SENDING" if sending else "staged (paused)",
            "sent": "SENDING", "manual_review": "company-only → manual review",
            "low_intent_hold": "held (low intent, no segment)",
            "duplicate": "duplicate (deduped)", "test_event": "RB2B test event",
            "dropped_icp": f"dropped ({result.get('icp_verdict')})"}.get(status, status)
    parts = [f"{emoji} *{company}* — {verb}"]
    if result.get("captured_url"):
        parts.append(f"   {result['captured_url']}")
    meta = []
    if result.get("intent_tier"):
        meta.append(f"tier {result['intent_tier']}")
    if result.get("variant"):
        meta.append(f"variant {result['variant']}")
    if result.get("segment"):
        seg = result["segment"]
        meta.append("seg " + (", ".join(seg) if isinstance(seg, list) else str(seg)))
    if meta:
        parts.append("   " + " · ".join(meta))
    if result.get("signals"):
        parts.append(f"   🔥 {'; '.join(result['signals'])}")
    if result.get("error"):
        parts.append(f"   error: {result['error']}")
    # Note: full Email 1 / Email 2 copy is intentionally NOT posted here (operator
    # wants prospect + signal only). Full copy lives in EmailBison / the one-off
    # /debug/sample-copy preview.
    try:
        _post("\n".join(parts))
    except Exception:
        pass


def post_full_copy(result: dict) -> dict:
    """Post the FULL generated copy for one lead (used by /debug/sample-copy so the
    operator can eyeball the new variants once). Returns the Slack API result."""
    if not config.SLACK_BOT_TOKEN:
        return {"ok": False, "error": "no SLACK_BOT_TOKEN set"}
    company = result.get("company_name") or result.get("domain") or "(unknown)"
    lines = [f"*SAMPLE — {company}* ({result.get('captured_url', '')})",
             f"tier {result.get('intent_tier')} · variant {result.get('variant')}"]
    if result.get("email_1"):
        lines.append(f"\n*Email 1*\n{result['email_1']}")
    if result.get("email_2"):
        lines.append(f"\n*Email 2*\n{result['email_2']}")
    try:
        return _post("\n".join(lines))
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


def send_test(text: str = "Vida · RB2B receiver — Slack wiring test. If you can see this, the digest will post here.") -> dict:
    """Post a one-off message to confirm bot token + channel + membership."""
    if not config.SLACK_BOT_TOKEN:
        return {"ok": False, "error": "no SLACK_BOT_TOKEN set", "channel": config.SLACK_CHANNEL}
    try:
        result = _post(text)
    except Exception as e:
        return {"ok": False, "error": str(e)[:200], "channel": config.SLACK_CHANNEL}
    result["channel_config"] = config.SLACK_CHANNEL
    return result


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
