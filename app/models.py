"""RB2B payload parsing + normalized lead shape.

RB2B's webhook payload is fixed (addendum §3): field names and structure cannot
be changed on their end, so we map this exact shape. Company-only hits arrive
with null First Name / Last Name / Title / Business Email.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import urlparse

# The 17 fields RB2B sends. Kept verbatim as the contract.
RB2B_FIELDS = [
    "LinkedIn URL", "First Name", "Last Name", "Title", "Company Name",
    "Business Email", "Website", "Industry", "Employee Count", "Estimate Revenue",
    "City", "State", "Zipcode", "Seen At", "Referrer", "Captured URL", "Tags",
]


def _s(value: Any) -> str:
    """Coerce to a trimmed string. Downstream systems (EmailBison) reject nulls
    for string fields, so we normalize null -> '' everywhere (addendum §3)."""
    if value is None:
        return ""
    return str(value).strip()


def domain_from(website: str, business_email: str = "") -> str:
    """Resolve a bare registrable domain from Website, falling back to email."""
    candidate = _s(website)
    if candidate:
        if "://" not in candidate:
            candidate = "http://" + candidate
        host = urlparse(candidate).netloc or urlparse(candidate).path
        host = host.split("/")[0].strip().lower()
        if host.startswith("www."):
            host = host[4:]
        if host:
            return host
    email = _s(business_email)
    if "@" in email:
        return email.split("@", 1)[1].strip().lower()
    return ""


def path_from(url: str) -> str:
    """Return the lowercased path of a URL, e.g. '/solutions/msps'."""
    raw = _s(url)
    if not raw:
        return ""
    if "://" not in raw:
        raw = "http://" + raw
    path = urlparse(raw).path or "/"
    return path.rstrip("/").lower() or "/"


@dataclass
class Lead:
    """Normalized view of an RB2B hit plus everything the pipeline adds to it."""

    # Raw RB2B fields (null -> "")
    linkedin_url: str = ""
    first_name: str = ""
    last_name: str = ""
    title: str = ""
    company_name: str = ""
    business_email: str = ""
    website: str = ""
    industry: str = ""
    employee_count: str = ""
    estimate_revenue: str = ""
    city: str = ""
    state: str = ""
    zipcode: str = ""
    seen_at: str = ""
    referrer: str = ""
    captured_url: str = ""
    tags: str = ""

    # Derived
    domain: str = ""
    captured_path: str = ""
    raw: dict = field(default_factory=dict)

    # Filled by the pipeline
    icp_verdict: str = ""
    segment: list[str] = field(default_factory=list)
    classification_confidence: str = ""
    lead_with: str = ""
    case_study: str = ""
    intent_tier: str = ""
    variant: str = ""          # "A" or "B"
    variant_prompt: str = ""   # "research_emails_v1.md" / v2
    research_brief: str = ""
    email_1: str = ""
    email_2: str = ""
    enriched: bool = False

    @property
    def is_company_only(self) -> bool:
        """RB2B could not resolve a named person (addendum §3, Step 2)."""
        return not self.first_name or not self.business_email

    @property
    def dedupe_key(self) -> str:
        """(LinkedIn URL or Company Name) + Captured URL — addendum §3."""
        anchor = self.linkedin_url or self.company_name
        return f"{anchor.lower()}|{self.captured_url.lower()}"

    def has_tag(self, needle: str) -> bool:
        return needle.lower() in self.tags.lower()


def parse_rb2b(payload: dict) -> Lead:
    """Map a raw RB2B webhook body into a Lead. Tolerant of missing keys."""
    g = lambda k: _s(payload.get(k))  # noqa: E731
    lead = Lead(
        linkedin_url=g("LinkedIn URL"),
        first_name=g("First Name"),
        last_name=g("Last Name"),
        title=g("Title"),
        company_name=g("Company Name"),
        business_email=g("Business Email"),
        website=g("Website"),
        industry=g("Industry"),
        employee_count=g("Employee Count"),
        estimate_revenue=g("Estimate Revenue"),
        city=g("City"),
        state=g("State"),
        zipcode=g("Zipcode"),
        seen_at=g("Seen At"),
        referrer=g("Referrer"),
        captured_url=g("Captured URL"),
        tags=g("Tags"),
        raw=dict(payload) if isinstance(payload, dict) else {},
    )
    lead.domain = domain_from(lead.website, lead.business_email)
    lead.captured_path = path_from(lead.captured_url)
    return lead


_EMP_RANGE = re.compile(r"(\d[\d,]*)")


def employee_bounds(employee_count: str) -> tuple[Optional[int], Optional[int]]:
    """Parse '200-500' / '1-10' / '5000+' into (min, max) ints (None if open)."""
    nums = [int(n.replace(",", "")) for n in _EMP_RANGE.findall(employee_count or "")]
    if not nums:
        return None, None
    if len(nums) == 1:
        return nums[0], None
    return nums[0], nums[1]
