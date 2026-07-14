"""EmailBison client — verify the campaign, then stage leads PAUSED with custom
fields (addendum §4 Step 5). Flow ported from the pipeline's emailbison_push.py:

  auth   = Authorization: Bearer <key>, header X-Workspace-Id
  base   = https://personal.buzzlead.io  (workspace 29)
  verify = GET  /api/campaigns?search=...        (confirm exact name/ID)
  ensure = POST /api/custom-variables            (register missing var names)
  upsert = POST /api/leads/create-or-update/multiple  -> lead ids
  attach = POST /api/campaigns/{id}/leads/attach-leads

"Paused" is achieved by attaching to the campaign WITHOUT starting it — attaching
loads leads but does not launch a send. This client never calls a start endpoint,
so leads sit staged until a human activates the campaign in EmailBison.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Optional

from . import config

_MAX_RETRIES = 6
_BACKOFF_BASE = 1.0
_BACKOFF_MAX = 30.0

UPSERT_BATCH = 100
ATTACH_BATCH = 500


def _request(method: str, path: str, payload: Optional[dict] = None,
             query: Optional[dict] = None) -> dict:
    url = f"{config.EMAILBISON_BASE_URL}{path}"
    if query:
        from urllib.parse import urlencode
        url += f"?{urlencode(query)}"
    headers = {
        "Authorization": f"Bearer {config.EMAILBISON_API_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-Workspace-Id": str(config.EMAILBISON_WORKSPACE_ID),
    }
    data = json.dumps(payload).encode("utf-8") if payload else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    attempts = 0
    while True:
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                body = resp.read().decode("utf-8")
                return json.loads(body) if body else {}
        except urllib.error.HTTPError as e:
            retryable = e.code == 429 or (e.code in (500, 502, 503, 504) and method == "GET")
            if retryable and attempts < _MAX_RETRIES:
                retry_after = e.headers.get("Retry-After") if getattr(e, "headers", None) else None
                try:
                    delay = float(retry_after) if retry_after else _BACKOFF_BASE * (2 ** attempts)
                except (TypeError, ValueError):
                    delay = _BACKOFF_BASE * (2 ** attempts)
                time.sleep(min(delay, _BACKOFF_MAX))
                attempts += 1
                continue
            err = e.read().decode("utf-8", errors="replace")[:300]
            raise RuntimeError(f"EmailBison {e.code} on {method} {path}: {err}")


def list_campaigns(search: Optional[str] = None) -> list[dict]:
    query = {"search": search} if search else None
    return (_request("GET", "/api/campaigns", query=query) or {}).get("data", [])


def resolve_campaign(name: str) -> dict:
    """Find the campaign by exact (case-insensitive) name. Never creates one —
    pushing into the wrong/new campaign pollutes another segment's metrics
    (addendum §8), so a missing campaign is a hard error the operator resolves."""
    target = (name or "").strip().lower()
    for c in list_campaigns(search=name):
        if (c.get("name") or "").strip().lower() == target:
            return {"id": c["id"], "name": c["name"], "status": c.get("status")}
    raise RuntimeError(
        f"EmailBison campaign not found by exact name: {name!r} in workspace "
        f"{config.EMAILBISON_WORKSPACE_ID}. Run list_campaigns() to see options."
    )


def _existing_custom_vars() -> set[str]:
    try:
        resp = _request("GET", "/api/custom-variables")
    except Exception:
        return set()
    items = resp.get("data") if isinstance(resp, dict) else resp
    names = set()
    for it in (items or []):
        if isinstance(it, dict):
            n = (it.get("name") or it.get("variable") or "").strip().lower()
            if n:
                names.add(n)
    return names


def ensure_custom_variables(names: set[str]) -> None:
    wanted = {(n or "").strip().lower() for n in names if (n or "").strip()}
    if not wanted:
        return
    existing = _existing_custom_vars()
    for name in sorted(wanted - existing):
        try:
            _request("POST", "/api/custom-variables", payload={"name": name})
        except Exception as e:
            if "already been taken" not in str(e).lower() and "already exists" not in str(e).lower():
                raise


def build_lead(lead) -> dict:
    """Map a processed Lead to the EmailBison upsert shape. Email bodies go into
    the sequence merge fields (PERSONALIZATION 1/2, COMPANY NAME CLEANED — the
    convention the workspace sequences use); the five RB2B custom fields ride
    alongside for variant-level reporting (addendum §4 Step 5)."""
    custom_variables = [
        {"name": "personalization 1", "value": lead.email_1 or ""},
        {"name": "personalization 2", "value": lead.email_2 or ""},
        {"name": "company name cleaned", "value": _clean_company(lead.company_name)},
        {"name": "rb2b_variant", "value": lead.variant or ""},
        {"name": "captured_url", "value": lead.captured_url or ""},
        {"name": "intent_tier", "value": lead.intent_tier or ""},
        {"name": "research_brief", "value": lead.research_brief or ""},
        {"name": "icp_verdict", "value": lead.icp_verdict or ""},
    ]
    return {
        "email": lead.business_email,
        "first_name": lead.first_name or "",
        "last_name": lead.last_name or "",
        "company": lead.company_name or "",
        "title": lead.title or "",
        "custom_variables": [cv for cv in custom_variables if cv["value"] != ""],
    }


def _clean_company(name: str) -> str:
    cleaned = (name or "").strip()
    for suf in (", Inc.", " Inc.", ", LLC", " LLC", ", Ltd.", " Ltd.", ", Corp.", " Corp.",
                ", Co.", " Co.", " Incorporated", " Corporation", " Limited", " Company"):
        if cleaned.lower().endswith(suf.lower()):
            return cleaned[: -len(suf)].rstrip(" ,.")
    return cleaned


def stage_leads(leads: list, campaign_id: int) -> dict:
    """Upsert leads then attach to the (paused) campaign. Returns a summary with
    the EmailBison lead ids so the caller can persist them."""
    if not leads:
        return {"upserted": 0, "attached": 0, "lead_ids": [], "errors": []}

    eb_leads = [build_lead(l) for l in leads if l.business_email]
    skipped = len(leads) - len(eb_leads)

    all_names: set[str] = set()
    for l in eb_leads:
        for cv in l["custom_variables"]:
            all_names.add(cv["name"])
    ensure_custom_variables(all_names)

    lead_ids: list = []
    errors: list = []
    for i in range(0, len(eb_leads), UPSERT_BATCH):
        batch = eb_leads[i:i + UPSERT_BATCH]
        try:
            result = _request(
                "POST", "/api/leads/create-or-update/multiple",
                payload={"existing_lead_behavior": "patch", "leads": batch},
            )
            lead_ids.extend([it["id"] for it in (result.get("data") or []) if "id" in it])
        except Exception as e:
            errors.append(f"upsert: {str(e)[:200]}")

    attached = 0
    for i in range(0, len(lead_ids), ATTACH_BATCH):
        id_batch = lead_ids[i:i + ATTACH_BATCH]
        try:
            _request(
                "POST", f"/api/campaigns/{campaign_id}/leads/attach-leads",
                payload={"lead_ids": id_batch, "allow_parallel_sending": False},
            )
            attached += len(id_batch)
        except Exception as e:
            errors.append(f"attach: {str(e)[:200]}")

    return {"upserted": len(lead_ids), "attached": attached, "lead_ids": lead_ids,
            "skipped_no_email": skipped, "errors": errors}
