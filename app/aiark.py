"""AI Ark people-search client for company-only enrichment (addendum §4 Step 2).

Request shape is ported verbatim from the pipeline's aiark_search.py so it matches
what actually works against the API:
  base   = https://api.ai-ark.com/api/developer-portal/v1
  auth   = X-TOKEN header
  search = POST /people with account.domain + contact.experience.current.title +
           contact.seniority filters, paged.

Per-segment title map: the addendum (§4 Step 2) says the full per-segment title
map already exists in project knowledge from prior title-skill work and should be
reused verbatim. That artifact was not in the handoff zip, so TITLE_MAP below is
built from the addendum's own summary (End User = Ops/CX leadership; White
Label/reseller = Product/Partnerships/CEO; Channel reseller = Sales/Channel/BD).
Replace it with the verbatim map when available.
"""
from __future__ import annotations

from typing import Optional

from . import config

AIARK_BASE_URL = "https://api.ai-ark.com/api/developer-portal/v1"

# Buyer archetypes -> title lists (addendum §4 Step 2 summary).
_END_USER_TITLES = [
    "VP of Operations", "Director of Operations", "COO", "Head of Customer Experience",
    "VP Customer Support", "Director of Customer Service", "Head of CX", "Operations Manager",
]
_WHITE_LABEL_TITLES = [
    "CEO", "Founder", "Chief Product Officer", "VP Product", "Head of Product",
    "VP Partnerships", "Head of Partnerships", "Director of Product",
]
_CHANNEL_TITLES = [
    "VP Sales", "Chief Revenue Officer", "Head of Sales", "Head of Channel",
    "Director of Business Development", "VP Business Development", "Channel Manager",
]

# lead_with code (from url_mapping) -> buyer archetype titles.
LEAD_WITH_TITLES = {
    "partner_sip_native": _WHITE_LABEL_TITLES,
    "partner_hipaa_gateway": _WHITE_LABEL_TITLES,
    "end_user_appointment_confirmation": _END_USER_TITLES,
    "end_user_call_scale": _END_USER_TITLES,
    "end_user_never_miss": _END_USER_TITLES,
    "bottom_funnel_override": _END_USER_TITLES + _WHITE_LABEL_TITLES,
    "proof_forward": _END_USER_TITLES + _WHITE_LABEL_TITLES,
}
_DEFAULT_TITLES = _END_USER_TITLES + _WHITE_LABEL_TITLES + _CHANNEL_TITLES

SENIORITY = ["C-Level", "VP", "Director", "Head"]
SENIORITY_ENUM_MAP = {
    "c-level": "c_suite", "vp": "vp", "director": "director", "head": "head",
    "manager": "manager",
}


def titles_for(lead_with: str) -> list[str]:
    return LEAD_WITH_TITLES.get(lead_with, _DEFAULT_TITLES)


# ── Filter builders (ported from aiark_search.py) ────────────────────────────

def _all_any(include: Optional[list], use_all: bool = False) -> Optional[dict]:
    if not include:
        return None
    return {"all" if use_all else "any": {"include": include}}


def _search_match(include: Optional[list], mode: str = "SMART") -> Optional[dict]:
    if not include:
        return None
    return {"any": {"include": {"mode": mode, "content": include}}}


def _normalize_seniority(values: list[str]) -> list[str]:
    out = []
    for v in values or []:
        key = v.lower().strip()
        if key in SENIORITY_ENUM_MAP:
            out.append(SENIORITY_ENUM_MAP[key])
    return out


def _normalize_contact(item: dict) -> dict:
    profile = item.get("profile", {}) or {}
    first = profile.get("first_name", "") or item.get("first_name", "")
    last = profile.get("last_name", "") or item.get("last_name", "")
    headline = profile.get("headline", "") or ""
    bio = profile.get("summary", "") or ""

    job_title = ""
    for pg in item.get("position_groups", []) or []:
        positions = pg.get("profile_positions", []) or []
        if positions:
            job_title = positions[0].get("title", "")
            break
    if not job_title:
        job_title = headline

    link = item.get("link", {}) or {}
    company = item.get("company", {}) or {}
    csum = (company.get("summary", {}) or {}) if isinstance(company, dict) else {}
    clink = (company.get("link", {}) or {}) if isinstance(company, dict) else {}

    email_obj = item.get("email", "")
    email = email_obj if isinstance(email_obj, str) else ""
    if not email and isinstance(email_obj, dict):
        out = (email_obj.get("output") or [{}])
        email = (out[0].get("email", "") if out else "") or ""

    return {
        "id": item.get("id", ""),
        "first_name": first,
        "last_name": last,
        "full_name": (profile.get("full_name", "") or f"{first} {last}").strip(),
        "job_title": job_title,
        "headline": headline,
        "profile_bio": bio,
        "email": email,
        "linkedin_url": link.get("linkedin", "") or "",
        "company_name": csum.get("name", ""),
        "company_domain": clink.get("domain", clink.get("domain_ltd", "")),
        "company_description": csum.get("description", "") or csum.get("summary", "") or "",
        "industry": csum.get("industry", "") or item.get("industry", ""),
    }


def _request(path: str, payload: dict) -> dict:
    import httpx
    headers = {"X-TOKEN": config.AI_ARK_API_KEY, "Content-Type": "application/json"}
    with httpx.Client(timeout=60.0) as client:
        resp = client.post(f"{AIARK_BASE_URL}{path}", json=payload, headers=headers)
        if resp.status_code >= 400:
            raise RuntimeError(f"AI.ARK {resp.status_code}: {resp.text[:200]}")
        return resp.json()


def find_best_contact(domain: str, titles: list[str], limit: int = 10) -> Optional[dict]:
    """Search a single domain for the best-matching contact. Returns a normalized
    contact dict or None if the API returns nothing.

    Tier fallback mirrors the pipeline: tight (titles + seniority) -> broad
    (seniority only) so a company with the right people but odd titles isn't lost.
    """
    if not config.AI_ARK_API_KEY:
        raise RuntimeError("AI_ARK_API_KEY not set")
    if not domain:
        return None

    for use_titles in (titles, None):  # tight, then broad
        account = {"domain": _all_any([domain])}
        contact: dict = {}
        if use_titles:
            tt = _search_match(use_titles)
            if tt:
                contact["experience"] = {"current": {"title": tt}}
        sen = _normalize_seniority(SENIORITY)
        if sen:
            contact["seniority"] = _all_any(sen)

        payload: dict = {"page": 0, "size": min(limit, 100)}
        if account:
            payload["account"] = account
        if contact:
            payload["contact"] = contact

        data = _request("/people", payload)
        rows = data.get("content") or data.get("results") or data.get("people") or []
        contacts = [_normalize_contact(r) for r in rows if isinstance(r, dict)]
        contacts = [c for c in contacts if c.get("first_name") or c.get("full_name")]
        if contacts:
            # Prefer a contact whose title matches the requested list, else first.
            wanted = {t.lower() for t in (use_titles or [])}
            for c in contacts:
                if c.get("job_title", "").lower() in wanted:
                    return c
            return contacts[0]

    return None
