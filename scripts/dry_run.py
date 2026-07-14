#!/usr/bin/env python3
"""Replay sample RB2B payloads through the FULL pipeline in dry-run mode.

Dry-run means: parse -> dedupe -> ICP gate -> URL mapping -> copy generation run
for real (so you can eyeball the copy), but NOTHING touches EmailBison, Slack, or
paid AI Ark enrichment (addendum §7 / run-commands Step 3). Review the output here
before wiring the live push.

Usage:
  # built-in samples (from copy-previews/01-...):
  DRY_RUN=1 ANTHROPIC_API_KEY=... python scripts/dry_run.py

  # your own rows from a JSON file (a list of RB2B payload objects):
  DRY_RUN=1 ANTHROPIC_API_KEY=... python scripts/dry_run.py path/to/rows.json

Copy generation needs ANTHROPIC_API_KEY. Everything else can be unset.
"""
import json
import os
import sys

# Force dry-run regardless of how it was invoked.
os.environ["DRY_RUN"] = "1"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.pipeline import process  # noqa: E402

# Sample payloads modeled on copy-previews/01-rb2b-website-visitor-intent-drafts.md
# plus a few edge cases (company-only, Cold hold, competitor, test event).
SAMPLES = [
    {  # Variant A — Hot page, Tags Hot Pages
        "Company Name": "Meridian BPO Solutions", "First Name": "Dana", "Last Name": "Ruiz",
        "Title": "VP of Operations", "Business Email": "dana.ruiz@meridianbpo.com",
        "Website": "https://meridianbpo.com", "Industry": "BPO / Call Center",
        "Employee Count": "200-500", "Estimate Revenue": "$40M rev",
        "City": "Dallas", "State": "TX", "Zipcode": "75201", "Seen At": "2026-07-14T15:00:00Z",
        "LinkedIn URL": "https://linkedin.com/in/dana-ruiz-bpo",
        "Captured URL": "https://vida.io/pricing", "Tags": "Hot Pages, ICP",
    },
    {  # Variant B — solution page, healthcare, company-only (should route to enrich)
        "Company Name": "Coastal Health Partners", "First Name": None, "Last Name": None,
        "Title": None, "Business Email": None,
        "Website": "https://coastalhealthpartners.com", "Industry": "Healthcare",
        "Employee Count": "50-200", "Estimate Revenue": "$18M rev",
        "City": "Charleston", "State": "SC", "Zipcode": "29401", "Seen At": "2026-07-14T15:05:00Z",
        "LinkedIn URL": "", "Captured URL": "https://vida.io/solutions/healthcare", "Tags": "ICP",
    },
    {  # Variant B — MSP solutions page, named contact
        "Company Name": "NorthArc Managed IT", "First Name": "Priya", "Last Name": "Shah",
        "Title": "Head of Partnerships", "Business Email": "priya@northarcit.com",
        "Website": "https://northarcit.com", "Industry": "IT Services / MSP",
        "Employee Count": "20-50", "Estimate Revenue": "$8M rev",
        "City": "Denver", "State": "CO", "Zipcode": "80202", "Seen At": "2026-07-14T15:10:00Z",
        "LinkedIn URL": "https://linkedin.com/in/priya-shah-msp",
        "Captured URL": "https://vida.io/solutions/msps", "Tags": "ICP",
    },
    {  # Cold — homepage, should HOLD with no copy
        "Company Name": "Generic Retail Co", "First Name": "Sam", "Last Name": "Lee",
        "Title": "Marketing Manager", "Business Email": "sam@genericretail.com",
        "Website": "https://genericretail.com", "Industry": "Retail",
        "Employee Count": "10-50", "City": "Austin", "State": "TX", "Zipcode": "73301",
        "Seen At": "2026-07-14T15:15:00Z", "LinkedIn URL": "",
        "Captured URL": "https://vida.io/", "Tags": "",
    },
    {  # Competitor — should be dropped out_competitor before any copy
        "Company Name": "Synthflow", "First Name": "Alex", "Last Name": "Kim",
        "Title": "Growth", "Business Email": "alex@synthflow.ai",
        "Website": "https://synthflow.ai", "Industry": "Software",
        "Employee Count": "10-50", "City": "New York", "State": "NY", "Zipcode": "10001",
        "Seen At": "2026-07-14T15:20:00Z", "LinkedIn URL": "",
        "Captured URL": "https://vida.io/pricing", "Tags": "Hot Pages",
    },
    {  # RB2B test event — should be skipped as test_event
        "Company Name": "RB2B", "Captured URL": "https://rb2b.com/pricing",
        "City": "Anywhere", "State": "NA", "Zipcode": "00000", "Seen At": "2026-07-14T15:25:00Z",
    },
]


def _print(result: dict) -> None:
    print("=" * 78)
    print(f"{result.get('company_name') or '(none)':<32} status={result['status']}")
    print(f"  verdict={result.get('icp_verdict')!r} segment={result.get('segment')} "
          f"tier={result.get('intent_tier')!r} variant={result.get('variant')!r}")
    if result.get("signals"):
        print(f"  signals={result['signals']}")
    print(f"  captured_url={result.get('captured_url')}")
    if result.get("email_1"):
        print("\n  --- EMAIL 1 ---")
        print("  " + result["email_1"].replace("\n", "\n  "))
    if result.get("email_2"):
        print("\n  --- EMAIL 2 ---")
        print("  " + result["email_2"].replace("\n", "\n  "))
    if result.get("research_brief"):
        print("\n  --- RESEARCH BRIEF ---")
        print("  " + result["research_brief"].replace("\n", "\n  "))
    if result.get("preview"):
        print("\n  --- EMAILBISON PAYLOAD (would upsert, NOT sent) ---")
        print("  " + json.dumps(result["preview"], indent=2).replace("\n", "\n  "))
    if result.get("error"):
        print(f"\n  ERROR: {result['error']}")
    print()


def main() -> None:
    if len(sys.argv) > 1:
        with open(sys.argv[1], encoding="utf-8") as f:
            payloads = json.load(f)
        if isinstance(payloads, dict):
            payloads = [payloads]
    else:
        payloads = SAMPLES

    if not os.getenv("ANTHROPIC_API_KEY"):
        print("WARNING: ANTHROPIC_API_KEY not set — ICP gate + copy generation will "
              "error for in-ICP leads (drops/holds still work).\n")

    summary: dict[str, int] = {}
    for payload in payloads:
        result = process(payload, dry_run=True)
        summary[result["status"]] = summary.get(result["status"], 0) + 1
        _print(result)

    print("=" * 78)
    print("SUMMARY:", json.dumps(summary))
    print("Nothing above was pushed to EmailBison or Slack (dry-run).")


if __name__ == "__main__":
    main()
