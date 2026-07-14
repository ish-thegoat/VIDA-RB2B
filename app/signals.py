"""Intent signals watched per hit (operator request). These are surfaced on the
staged lead — as an `rb2b_signals` EmailBison custom field and as badges in the
Slack digest — so a reviewer can spot the genuinely hot leads in a noisy feed.
They do NOT rewrite the copy variant (a return visit to a solutions page should
not get pricing-page copy); variant stays driven by the page mapping + tags.

Signals:
  - high-intent page  : pricing / integrations / case-studies / demo / get-started
                        (pages people hit when evaluating, not browsing)
  - return visitor    : seen on >1 distinct day within the last 7 days (research,
                        not window-shopping)
  - buying group      : >1 distinct visitor from the same company domain in 7 days
                        (a buying group forming internally)
  - RB2B ICP-tagged   : RB2B's own ICP tag (traits configured in RB2B's dashboard —
                        company size / industry / seniority — tagged automatically)
  - RB2B Hot-Pages tag: RB2B's Hot Pages tag
"""
from __future__ import annotations

from . import store
from .models import Lead
from .url_mapping import is_high_intent_path

RETURN_WINDOW_DAYS = 7
CLUSTER_WINDOW_DAYS = 7


def compute(lead: Lead) -> list[str]:
    sigs: list[str] = []

    if is_high_intent_path(lead.captured_path):
        sigs.append("high-intent page")

    days = store.distinct_visit_days(lead.return_anchor, RETURN_WINDOW_DAYS)
    if days >= 2:
        sigs.append(f"return visitor ({days} days/7d)")

    if lead.domain:
        visitors = store.distinct_domain_visitors(lead.domain, CLUSTER_WINDOW_DAYS)
        if visitors >= 2:
            sigs.append(f"buying group ({visitors} visitors from {lead.domain})")

    if lead.has_tag("ICP"):
        sigs.append("RB2B ICP-tagged")
    if lead.has_tag("Hot Pages"):
        sigs.append("RB2B Hot-Pages tag")

    return sigs
