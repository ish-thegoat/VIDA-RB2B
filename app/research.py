"""Real prospect research — so copy is grounded in specific, verifiable facts
instead of generic segment pain.

Two sources, both available with the keys already on Railway:
  1. Live website fetch (keyless httpx GET of the company site).
  2. Claude's server-side web_search tool (uses ANTHROPIC_API_KEY) for recent news,
     LinkedIn-surfaced context, and anything not on the homepage.

Output is a labeled brief the email prompts consume as {{Research Output}}, with a
REAL_OBSERVATION line the copy leads with. Deeper LinkedIn scraping (full profile /
post history) would need one of the scraping providers (Firecrawl / BrightData /
Apify) added to the env; web search covers most of it without that.
"""
from __future__ import annotations

import logging
import re

from . import config, llm

log = logging.getLogger("rb2b.research")

_SCRIPT = re.compile(r"<(script|style|noscript)[^>]*>.*?</\1>", re.S | re.I)
_TAGS = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def fetch_website(url: str, max_chars: int = 4000) -> str:
    """Best-effort plain-text of a company homepage. Returns '' on any failure."""
    if not url:
        return ""
    if "://" not in url:
        url = "https://" + url
    try:
        import httpx
        with httpx.Client(timeout=12.0, follow_redirects=True,
                          headers={"User-Agent": "Mozilla/5.0 (compatible; VidaResearch/1.0)"}) as c:
            r = c.get(url)
            if r.status_code >= 400 or "text/html" not in r.headers.get("content-type", "text/html"):
                return ""
            html = r.text
    except Exception as e:
        log.info("website fetch failed for %s: %s", url, e)
        return ""
    text = _WS.sub(" ", _TAGS.sub(" ", _SCRIPT.sub(" ", html))).strip()
    return text[:max_chars]


_SYSTEM = """You are a B2B sales researcher for Vida (an AI voice/SMS/chat agent \
platform). Someone from a company just visited a specific page on vida.io. Produce \
a tight brief a rep will use to write a genuinely personal cold email — grounded in \
real, verifiable facts, not generic filler.

Use the website excerpt provided AND web search. State only facts you can support \
from a source. Do NOT invent. If you cannot find something specific, write \
"none found" for that line rather than guessing.

Output exactly these labeled lines and nothing else:
COMPANY_SUMMARY: [one plain sentence on what this company actually does]
REAL_OBSERVATION: [ONE specific, verifiable, non-obvious detail about THIS company \
or person — a recent hire, expansion, funding, product launch, named client, \
location, award, or a concrete detail from their site/LinkedIn. Specific enough it \
could only be them. If nothing real, write "none found".]
PAGE_CONTEXT: [why someone in this role at this company would look at the exact \
page they visited]
LIKELY_TRIGGER: [the most plausible money/capacity reason they're evaluating an AI \
agent platform right now]"""


def _model() -> str:
    return config.COPY_MODEL_OVERRIDE or "claude-sonnet-4-6"


def deep_research(lead) -> str:
    """Return a labeled research brief for the lead. Never raises."""
    site = fetch_website(lead.website or lead.domain)
    user = (
        f"Company: {lead.company_name}\n"
        f"Domain: {lead.domain}\n"
        f"Industry: {lead.industry}\n"
        f"Employee count: {lead.employee_count}\n"
        f"Contact: {(lead.first_name + ' ' + lead.last_name).strip() or '(company-only)'}, "
        f"{lead.title or '(title unknown)'}\n"
        f"LinkedIn: {lead.linkedin_url or '(none)'}\n"
        f"Page visited on vida.io: {lead.captured_path}\n\n"
        f"Website excerpt:\n{site or '(none retrieved)'}"
    )
    try:
        return llm.complete_with_search(
            messages=[{"role": "user", "content": user}],
            model=_model(), system=_SYSTEM, max_tokens=900,
        )
    except Exception as e:
        log.warning("web-search research failed (%s); falling back to site-only", e)
        try:
            return llm.complete(
                messages=[{"role": "user", "content": user}],
                model=_model(), system=_SYSTEM, max_tokens=700, temperature=0.2,
            )
        except Exception as e2:
            log.warning("site-only research also failed: %s", e2)
            return ""
