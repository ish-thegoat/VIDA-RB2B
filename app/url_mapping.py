"""Captured URL -> segment / lead-with code / case study / intent tier / variant.

This is the core personalization logic and the reason the project exists
(addendum §5 + §6). The table below is a direct transcription of the addendum §5
mapping table — it is the single source of truth. Do not invent URLs outside it:
an unmatched Captured URL is treated as low-intent/explore and held (no copy).

Variant assignment is deterministic by intent tier (addendum §6):
  Hot  -> Slot A -> research_emails_v1.md ("Hot Page / Direct Ask")
  Warm -> Slot B -> research_emails_v2.md ("Solution Page / Segment Mirror")
  Cold -> no copy generated (hold)
"""
from __future__ import annotations

from dataclasses import dataclass

HOT = "Hot"
WARM = "Warm"
WARM_HOT = "Warm-Hot"
COLD = "Cold"

VARIANT_A = "A"  # research_emails_v1.md
VARIANT_B = "B"  # research_emails_v2.md

PROMPT_V1 = "research_emails_v1.md"
PROMPT_V2 = "research_emails_v2.md"


@dataclass
class Mapping:
    segment_signal: str
    lead_with: str
    case_study: str
    intent_tier: str
    matched: bool = True  # False => no page-level signal (hold)


# Bottom-funnel pages: whatever segment the ICP gate resolved, tier is Hot.
_HOT_PATHS = ("/pricing", "/demo", "/book-a-call", "/get-started")

# Ordered longest/most-specific first so /solutions/call-centers wins over /solutions.
_SOLUTION_TABLE: list[tuple[tuple[str, ...], Mapping]] = [
    (("/solutions/msps", "/solutions/agencies"),
     Mapping("MSP / reseller", "partner_sip_native", "JobNimbus", WARM)),
    (("/solutions/healthcare",),
     # reseller OR end-user is resolved by the ICP gate; default lead-with here is
     # the end-user appointment angle, overridden in the pipeline if reseller.
     Mapping("Healthcare (reseller or end-user)", "end_user_appointment_confirmation", "MeetingsTech", WARM)),
    (("/solutions/bpos", "/solutions/call-centers"),
     Mapping("BPO / enterprise call center", "end_user_call_scale", "Rob Graham Enterprises", WARM)),
    (("/solutions/insurance",),
     Mapping("Insurance", "end_user_appointment_confirmation", "MeetingsTech", WARM)),
    (("/solutions/saas", "/solutions/home-services"),
     Mapping("Vertical SaaS CRM", "partner_sip_native", "JobNimbus", WARM)),
    (("/solutions/automotive",),
     Mapping("Automotive", "end_user_never_miss", "SmartMoving, Squire", WARM)),
    (("/solutions/financial-services", "/solutions/legal"),
     Mapping("Financial services / legal", "end_user_appointment_confirmation", "MeetingsTech", WARM)),
]

_PROOF_PATHS = ("/case-studies", "/customers")
_INTEGRATION_PATHS = ("/integrations",)

# Pages people hit when actively evaluating (not just browsing). Used for the
# high-intent signal (surfaced/prioritized), independent of variant selection.
_HIGH_INTENT_PATHS = _HOT_PATHS + _INTEGRATION_PATHS + _PROOF_PATHS


def is_high_intent_path(captured_path: str) -> bool:
    return _startswith_any((captured_path or "/").lower(), _HIGH_INTENT_PATHS)


_PAGE_LABELS = {
    "/pricing": "pricing page", "/demo": "demo page", "/get-started": "get-started page",
    "/book-a-call": "book-a-call page", "/integrations": "integrations page",
    "/case-studies": "case studies", "/customers": "customers page",
}


def page_label(captured_path: str) -> str:
    """Human phrase for the page visited, for use in the email opener."""
    path = (captured_path or "/").lower()
    if path in ("", "/"):
        return "your site"
    for prefix, label in _PAGE_LABELS.items():
        if path == prefix or path.startswith(prefix):
            return label
    if path.startswith("/solutions/"):
        seg = path.rsplit("/", 1)[-1].replace("-", " ")
        return f"{seg} solutions page"
    if path.startswith("/blog"):
        return "blog"
    return "your site"


def _startswith_any(path: str, prefixes: tuple[str, ...]) -> bool:
    return any(path == p or path.startswith(p + "/") or path.startswith(p) for p in prefixes)


def map_captured_url(captured_path: str) -> Mapping:
    """Resolve the §5 mapping for a captured URL path. Never raises."""
    path = (captured_path or "/").lower()

    if _startswith_any(path, _HOT_PATHS):
        # Segment stays whatever the ICP gate resolved; bottom-funnel override -> Hot.
        return Mapping("(ICP-resolved segment)", "bottom_funnel_override", "(ICP segment best-fit)", HOT)

    for prefixes, mapping in _SOLUTION_TABLE:
        if _startswith_any(path, prefixes):
            return Mapping(mapping.segment_signal, mapping.lead_with, mapping.case_study, mapping.intent_tier)

    if _startswith_any(path, _PROOF_PATHS):
        return Mapping("Proof-seeking", "proof_forward", "(matched to ICP industry)", WARM_HOT)

    if _startswith_any(path, _INTEGRATION_PATHS):
        # Evaluating fit with existing stack — SIP-native / no-migration angle,
        # segment resolved by the ICP gate. High-intent, Variant B (segment mirror).
        return Mapping("(ICP-resolved segment)", "partner_sip_native", "(matched to ICP industry)", WARM_HOT)

    # Homepage, blog, or anything not in the table -> no page-level signal -> hold.
    return Mapping("No page-level signal", "undefined_explore", "none", COLD, matched=False)


def apply_tags_upgrade(mapping: Mapping, has_hot_pages_tag: bool) -> Mapping:
    """Tags containing 'Hot Pages' upgrade a Warm hit to Hot (addendum §5).

    Only Warm / Warm-Hot upgrade; a Cold (no-signal) hit stays held even if tagged
    — a homepage visit tagged Hot Pages is contradictory, so we don't fabricate a
    hot lead from it.
    """
    if has_hot_pages_tag and mapping.intent_tier in (WARM, WARM_HOT):
        return Mapping(mapping.segment_signal, mapping.lead_with, mapping.case_study, HOT)
    return mapping


def variant_for_tier(intent_tier: str) -> tuple[str, str]:
    """Return (variant_slot, prompt_filename) for a tier. ('', '') for Cold/hold."""
    if intent_tier == HOT:
        return VARIANT_A, PROMPT_V1
    if intent_tier in (WARM, WARM_HOT):
        return VARIANT_B, PROMPT_V2
    return "", ""  # Cold -> no copy
