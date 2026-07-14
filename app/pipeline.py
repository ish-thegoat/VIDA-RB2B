"""The processing pipeline (addendum §4). Runs entirely off the request path, in
the background worker. Order is fixed:

  parse -> test-event filter -> dedupe(24h) -> ICP gate -> [company-only? AI Ark]
    -> Captured-URL mapping -> copy gen (Hot=v1 / Warm=v2, Cold=skip)
    -> stage PAUSED in EmailBison + persist -> (Slack digest flushes on a timer)

Every terminal drop is logged to the queryable drops table. Nothing here raises
to the caller — the worker must never die on one bad hit.
"""
from __future__ import annotations

import logging

from . import aiark, config, copy_gen, emailbison, icp_gate, store
from .models import Lead, parse_rb2b
from .url_mapping import apply_tags_upgrade, map_captured_url, variant_for_tier

log = logging.getLogger("rb2b.pipeline")

_CAMPAIGN_CACHE: dict = {}


def _resolve_campaign() -> dict:
    if "id" not in _CAMPAIGN_CACHE:
        camp = emailbison.resolve_campaign()
        _CAMPAIGN_CACHE.update(camp)
        log.info("Resolved EmailBison campaign %r -> id=%s status=%s",
                 camp["name"], camp["id"], camp.get("status"))
    return _CAMPAIGN_CACHE


def _result(status: str, lead: Lead, **extra) -> dict:
    out = {
        "status": status,
        "company_name": lead.company_name,
        "domain": lead.domain,
        "captured_url": lead.captured_url,
        "icp_verdict": lead.icp_verdict,
        "segment": lead.segment,
        "intent_tier": lead.intent_tier,
        "variant": lead.variant,
    }
    out.update(extra)
    return out


def process(payload: dict, dry_run: bool | None = None) -> dict:
    """Process one RB2B payload end to end. Returns a summary dict (also used by
    the dry-run replay script). dry_run defaults to config.DRY_RUN."""
    dry = config.DRY_RUN if dry_run is None else dry_run
    lead = parse_rb2b(payload)

    # ── Test-event filter (run-commands Step 2): never push RB2B's test payload.
    if lead.company_name.strip().lower() == config.RB2B_TEST_COMPANY_NAME.lower():
        store.log_drop("test_event", lead, detail="RB2B test payload")
        return _result("test_event", lead)

    # ── Dedupe (addendum §3): repeat visit inside the rolling window.
    if store.is_duplicate(lead.dedupe_key):
        store.log_drop("duplicate", lead, detail=f"seen within {config.DEDUPE_WINDOW_HOURS}h")
        return _result("duplicate", lead)
    store.record_seen(lead.dedupe_key)

    # ── ICP gate (addendum §4 Step 1): before any copy or enrichment spend.
    verdict = icp_gate.classify(lead)
    lead.icp_verdict = verdict.icp_verdict
    lead.segment = verdict.segment
    lead.classification_confidence = verdict.classification_confidence
    if verdict.is_stop:
        store.log_drop(verdict.icp_verdict, lead, detail="ICP gate stop")
        return _result("dropped_icp", lead)

    # ── Captured-URL mapping (addendum §5). Cold/no-signal => hold, no copy.
    mapping = map_captured_url(lead.captured_path)
    mapping = apply_tags_upgrade(mapping, lead.has_tag("Hot Pages"))
    lead.lead_with = mapping.lead_with
    lead.case_study = mapping.case_study
    lead.intent_tier = mapping.intent_tier
    if not lead.segment and mapping.matched:
        lead.segment = [mapping.segment_signal]

    variant, prompt_file = variant_for_tier(lead.intent_tier)
    if not variant:  # Cold / low-intent hold — logged, no copy generated (§4 Step 4)
        store.log_drop("low_intent_hold", lead, detail=f"path={lead.captured_path}")
        return _result("low_intent_hold", lead)
    lead.variant = variant
    lead.variant_prompt = prompt_file

    # ── Company-only enrichment (addendum §4 Step 2): only after passing gate.
    if lead.is_company_only:
        enriched = _enrich(lead, dry)
        if enriched == "no_match":
            store.append_manual_review(lead)
            store.log_drop("aiark_no_match", lead, detail="AI Ark found no contact")
            return _result("manual_review", lead)

    # ── Copy generation (approved prompt files).
    try:
        copy = copy_gen.generate(lead)
    except Exception as e:
        store.log_drop("error", lead, detail=f"copy_gen: {str(e)[:200]}")
        return _result("error_copy", lead, error=str(e)[:200])
    lead.research_brief = copy.research_output
    lead.email_1 = copy.email_1
    lead.email_2 = copy.email_2

    # ── Stage PAUSED in EmailBison + persist (addendum §4 Step 5).
    if dry:
        return _result("dry_run_ready", lead, preview=emailbison.build_lead(lead),
                       email_1=lead.email_1, email_2=lead.email_2,
                       research_brief=lead.research_brief)

    try:
        camp = _resolve_campaign()
        # Safety gate: never push into a campaign that is actively sending — that
        # would send an un-approved email. Leads must land PAUSED (addendum §4 Step 5).
        if emailbison.is_sending(camp.get("status")):
            store.log_drop("error", lead,
                           detail=f"campaign {camp['id']} status={camp.get('status')!r} is sending; refused")
            return _result("error_campaign_active", lead,
                           error=f"campaign {camp['id']} is {camp.get('status')} (not paused)")
        push = emailbison.stage_leads([lead], camp["id"])
    except Exception as e:
        store.log_drop("error", lead, detail=f"emailbison: {str(e)[:200]}")
        return _result("error_push", lead, error=str(e)[:200])

    store.record_staged(lead, push.get("lead_ids"), campaign=camp)
    if push.get("errors"):
        log.warning("EmailBison push had errors for %s: %s", lead.company_name, push["errors"])
    return _result("staged", lead, emailbison=push)


def _enrich(lead: Lead, dry: bool) -> str:
    """Fill lead contact fields via AI Ark. Returns 'ok', 'no_match', or 'skipped'."""
    if dry:
        # Dry-run: do not spend enrichment credits. Proceed with whatever fields
        # are present so copy can still be previewed.
        lead.raw["_enrichment"] = "skipped (dry_run)"
        return "skipped"
    try:
        contact = aiark.find_best_contact(lead.domain, aiark.titles_for(lead.lead_with))
    except Exception as e:
        log.warning("AI Ark error for %s: %s", lead.domain, e)
        return "no_match"
    if not contact:
        return "no_match"
    lead.first_name = contact.get("first_name") or lead.first_name
    lead.last_name = contact.get("last_name") or lead.last_name
    lead.title = contact.get("job_title") or lead.title
    lead.business_email = contact.get("email") or lead.business_email
    if not lead.linkedin_url:
        lead.linkedin_url = contact.get("linkedin_url", "")
    if contact.get("company_description"):
        lead.raw["_company_description"] = contact["company_description"]
    lead.enriched = True
    # If AI Ark returned a contact but no deliverable email, treat as no-match so
    # it goes to human review rather than a push that will fail on empty email.
    if not lead.business_email:
        return "no_match"
    return "ok"
