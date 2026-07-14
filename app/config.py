"""Runtime configuration for the Vida RB2B webhook receiver.

All values come from environment variables. Nothing here is a secret at rest —
the actual keys are supplied by Railway (or a local .env). See .env.example for
the full list and README.md for what each one does.
"""
from __future__ import annotations

import os


def _get(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _get_bool(name: str, default: bool = False) -> bool:
    raw = _get(name)
    if not raw:
        return default
    return raw.lower() in ("1", "true", "yes", "on")


def _get_any(names: list[str], default: str = "") -> str:
    """First non-empty of several accepted names (tolerates naming conventions)."""
    for n in names:
        v = _get(n)
        if v:
            return v
    return default


# ── Secrets / credentials (supplied by Railway or a local .env) ──────────────
ANTHROPIC_API_KEY = _get("ANTHROPIC_API_KEY")
# Accept both the addendum's name and the pipeline ecosystem's name.
AI_ARK_API_KEY = _get_any(["AI_ARK_API_KEY", "AIARK_API_KEY"])
# Vida workspace-29 key: EMAILBISON_API_KEY, or the EMAILBISON_API_KEY_<WORKSPACE> form.
EMAILBISON_API_KEY = _get_any(["EMAILBISON_API_KEY", "EMAILBISON_API_KEY_VIDA"])
SLACK_BOT_TOKEN = _get("SLACK_BOT_TOKEN")
RB2B_WEBHOOK_TOKEN = _get("RB2B_WEBHOOK_TOKEN")

# ── EmailBison target ────────────────────────────────────────────────────────
EMAILBISON_WORKSPACE_ID = int(_get("EMAILBISON_WORKSPACE_ID", "29") or "29")
EMAILBISON_BASE_URL = _get("EMAILBISON_BASE_URL", "https://personal.buzzlead.io")
# Target campaign. If EMAILBISON_CAMPAIGN_ID is set we push to that id directly
# (verified + status-checked at push time); otherwise we resolve by exact name.
EMAILBISON_CAMPAIGN_ID = _get("EMAILBISON_CAMPAIGN_ID", "792")
EMAILBISON_CAMPAIGN_NAME = _get("EMAILBISON_CAMPAIGN_NAME", "RB2B Intent Workflow - 07/14")

# ── Copy generation ──────────────────────────────────────────────────────────
# The approved prompt files pin a model in each JSON block. We honor that by
# default; COPY_MODEL lets an operator override without editing approved copy
# (e.g. if the pinned model id is retired). See README "Model note".
COPY_MODEL_OVERRIDE = _get("COPY_MODEL")

# ── Slack ────────────────────────────────────────────────────────────────────
SLACK_CHANNEL = _get("SLACK_CHANNEL", "#vida-rb2b")
# Flush the staged-lead digest on this cadence (seconds). Batches, never per-lead.
SLACK_DIGEST_INTERVAL_SECONDS = int(_get("SLACK_DIGEST_INTERVAL_SECONDS", "900") or "900")

# ── Behavior ─────────────────────────────────────────────────────────────────
# DRY_RUN: run the full pipeline (parse -> ICP -> map -> copy) but DO NOT touch
# EmailBison or Slack, and skip paid AI Ark enrichment. Used by scripts/dry_run.py.
DRY_RUN = _get_bool("DRY_RUN", False)

# Dedupe window: a (LinkedIn URL or Company Name)+Captured URL seen inside this
# many hours is treated as a repeat visit and dropped. Addendum §3.
DEDUPE_WINDOW_HOURS = int(_get("DEDUPE_WINDOW_HOURS", "24") or "24")

# Storage. On Railway the container FS is ephemeral; mount a volume and point
# DB_PATH / DATA_DIR at it to keep the dedupe window + drop log across deploys.
DATA_DIR = _get("DATA_DIR", os.path.join(os.path.dirname(os.path.dirname(__file__)), "data"))
DB_PATH = _get("DB_PATH", os.path.join(DATA_DIR, "rb2b.db"))
MANUAL_REVIEW_CSV = _get(
    "MANUAL_REVIEW_CSV",
    os.path.join(DATA_DIR, "prospects", "vida", "manual", "rb2b-company-only-review.csv"),
)

# RB2B fires a documented test event with this company name. We never push it.
RB2B_TEST_COMPANY_NAME = _get("RB2B_TEST_COMPANY_NAME", "RB2B")


def missing_required(for_push: bool = True) -> list[str]:
    """Return the names of env vars that must be set for real (non-dry-run) work.

    for_push=False checks only what's needed to classify + generate copy.
    """
    missing = []
    if not ANTHROPIC_API_KEY:
        missing.append("ANTHROPIC_API_KEY")
    if not RB2B_WEBHOOK_TOKEN:
        missing.append("RB2B_WEBHOOK_TOKEN")
    if for_push:
        if not AI_ARK_API_KEY:
            missing.append("AI_ARK_API_KEY")
        if not EMAILBISON_API_KEY:
            missing.append("EMAILBISON_API_KEY")
        if not SLACK_BOT_TOKEN:
            missing.append("SLACK_BOT_TOKEN")
    return missing
