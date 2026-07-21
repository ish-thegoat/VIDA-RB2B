"""ICP gate — runs on every hit before any copy or enrichment spend (addendum §4
Step 1). Classifies against Vida's master rules and returns a verdict, 1-2
segments, and a confidence.

IMPORTANT — sourcing of the rules: the authoritative rule set lives in
`vida_master_workbook_reference.docx`, which is referenced by the package but was
NOT included in the handoff zip. What IS encoded here is the addendum's own
summary of those rules (§2 hard exclusions + §5 segment vocabulary). The gate is
a hybrid:
  1. Deterministic hard exclusions we can decide from RB2B fields alone
     (competitor name match, obvious test event handled upstream).
  2. An LLM classifier for verdict + segment using the summarized rules.

To harden this to the exact Rule 1-7 picklists, paste the workbook rules into
MASTER_RULES below — the classifier prompt already interpolates it.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from . import config, llm
from .models import Lead

# Verdicts that STOP processing (drop, no copy, no enrichment).
STOP_PREFIX = "out_"
STOP_VERDICTS = {"undefined_insufficient_data"}

# Named full-stack AI voice/agent competitors -> instant out (addendum §2).
# Cited by name only in internal strategy, never in copy.
COMPETITOR_NAMES = {"bland", "bland.ai", "synthflow", "air.ai", "vapi", "retell", "retellai"}
COMPETITOR_DOMAINS = {"bland.ai", "synthflow.ai", "synthflow.com", "air.ai", "vapi.ai", "retellai.com"}

# Segment vocabulary the classifier may pick from (addendum §2 / §5).
SEGMENTS = [
    "MSP / reseller",
    "Vertical SaaS CRM",
    "Telecom / UCaaS reseller",
    "BPO / call center",
    "Healthcare",
    "Insurance",
    "Automotive",
    "Financial services / legal",
    "Home services",
    "undefined_emerging",
]

# Paste the exact workbook Rule 1-7 picklists here to harden the gate.
MASTER_RULES = ""

_SYSTEM = """You are the ICP classification gate for Vida (vida.io), a white-label AI \
voice/SMS/email/chat agent platform sold to platforms, resellers, and operators. \
You receive firmographic data about a company that just visited vida.io and must \
classify it against Vida's master ICP rules. Be strict and deterministic.

HARD EXCLUSIONS (any one of these => an out_ verdict, nothing else matters):
- Offshore-headquartered at the group-ownership level => "out_offshore".
- Full-stack AI voice / AI agent competitor (companies whose own product is an AI \
calling/agent platform, e.g. Bland, Synthflow, Vapi, Retell, Air.ai) => "out_competitor".
- Consumer-facing business (sells primarily to individual consumers, not to \
businesses/operators) => "out_consumer".

NOT an exclusion:
- Education (schools, colleges, universities, ed-tech) is NOT an instant out. \
Route it to segment "undefined_emerging" with verdict "undefined_emerging" \
(this is an active signal being tracked internally).

CLASSIFICATION:
- If the company is a fit and you can resolve a segment, verdict = "in".
- Choose 1 or 2 segments (max 2) from this list, most-likely first: {segments}.
- Attempt estimation from Industry / Employee Count / Revenue / Company Name \
before defaulting to "unclear". Only use verdict "undefined_insufficient_data" \
when there is genuinely nothing to classify on.
- classification_confidence is one of: high, medium, low.

{extra_rules}

Output ONLY a JSON object, no prose, no code fence:
{{"icp_verdict": "...", "segment": ["..."], "classification_confidence": "..."}}
Valid icp_verdict values: "in", "undefined_emerging", "undefined_insufficient_data", \
"out_offshore", "out_competitor", "out_consumer"."""


@dataclass
class Verdict:
    icp_verdict: str
    segment: list[str]
    classification_confidence: str

    @property
    def is_stop(self) -> bool:
        return self.icp_verdict.startswith(STOP_PREFIX) or self.icp_verdict in STOP_VERDICTS


def _deterministic_out(lead: Lead) -> Verdict | None:
    """Cheap exclusions we can decide without an LLM call."""
    name = (lead.company_name or "").lower()
    domain = (lead.domain or "").lower()
    if domain in COMPETITOR_DOMAINS or any(c == name for c in COMPETITOR_NAMES) or \
            any(name.startswith(c + " ") or f" {c} " in f" {name} " for c in COMPETITOR_NAMES):
        return Verdict("out_competitor", [], "high")
    return None


def _model() -> str:
    # Reuse the copy model choice if overridden, else a sensible current default.
    return config.COPY_MODEL_OVERRIDE or "claude-sonnet-4-6"


def classify(lead: Lead) -> Verdict:
    """Classify a lead. Falls back to a safe hold on any error."""
    det = _deterministic_out(lead)
    if det is not None:
        return det

    system = _SYSTEM.format(
        segments=", ".join(SEGMENTS),
        extra_rules=(f"ADDITIONAL MASTER-WORKBOOK RULES:\n{MASTER_RULES}" if MASTER_RULES else ""),
    )
    user = (
        f"Company Name: {lead.company_name}\n"
        f"Website / Domain: {lead.domain or lead.website}\n"
        f"Industry: {lead.industry}\n"
        f"Employee Count: {lead.employee_count}\n"
        f"Estimated Revenue: {lead.estimate_revenue}\n"
        f"Location: {lead.city}, {lead.state} {lead.zipcode}\n"
        f"Captured URL: {lead.captured_url}\n"
    )
    try:
        raw = llm.complete(
            messages=[{"role": "user", "content": user}],
            model=_model(), max_tokens=300, temperature=0.0, system=system,
        )
        data = _parse_json(raw)
        verdict = str(data.get("icp_verdict", "")).strip() or "undefined_insufficient_data"
        segment = data.get("segment") or []
        if isinstance(segment, str):
            segment = [segment]
        segment = [s for s in (str(x).strip() for x in segment) if s][:2]
        confidence = str(data.get("classification_confidence", "")).strip() or "low"
        return Verdict(verdict, segment, confidence)
    except Exception as e:  # never let a classifier hiccup crash the webhook worker
        # IMPORTANT: this is a distinct sentinel, NOT "undefined_insufficient_data".
        # A real "insufficient data" verdict means the model looked at the company
        # and couldn't classify it. This means the classifier call itself failed
        # (rate limit, exhausted API credits, timeout, bad response) — the company
        # was never actually evaluated. Conflating the two silently discards real,
        # classifiable leads and masks outages (e.g. a billing lapse) as ordinary
        # business. Pipeline.process() must branch on this before treating it as
        # any kind of ICP judgment.
        return Verdict("error_icp_gate", [], f"error:{str(e)[:300]}")


def _parse_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end != -1:
        text = text[start:end + 1]
    return json.loads(text)
