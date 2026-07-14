# Vida · RB2B Website Visitor Intent — Webhook Receiver

A standalone FastAPI service that replaces the manual RB2B → Clay → hand-triage
loop. RB2B fires a webhook per identified vida.io visitor; this service classifies
the company against Vida's ICP, enriches company-only hits, maps the exact page
visited to a segment / case study / intent tier, writes bespoke Email 1 + Email 2
copy, and stages the lead **paused** in EmailBison for approval — then posts a
batched digest to Slack.

Built to `PROJECT-ADDENDUM.md` §4 and §7. This does **not** run through
`run_pipeline.py full`; it is a real-time receiver.

## Flow

```
POST /webhooks/rb2b?token=…  ──(validate + 200 in <15s)──►  in-process queue
                                                                │
   background worker:                                           ▼
   parse RB2B payload
   → test-event filter (Company Name == "RB2B" → skip)
   → dedupe (LinkedIn|Company + Captured URL, 24h window)
   → ICP gate (out_* → drop, no copy, no enrichment spend)
   → company-only? → AI Ark people-search (no match → manual-review CSV)
   → Captured URL → segment / case study / intent tier / variant  (addendum §5)
   → copy gen: Hot → research_emails_v1.md, Warm → v2, Cold → hold (no copy)
   → stage PAUSED in EmailBison ws 29 w/ custom fields
   → record staged → Slack digest (batched, every 15 min)
```

## Routes

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/webhooks/rb2b?token=<secret>` | RB2B target. Rejects a bad/missing token with 401. Validates JSON, enqueues, returns `200 {"status":"accepted"}` immediately. All real work runs in the background worker so RB2B never times out. |
| `GET` | `/health` | `200` with queue depth + staged/drop counts. Railway healthcheck. |

## Environment variables

Set these in Railway (exact names). See `.env.example`.

**Secrets (you supply):** `ANTHROPIC_API_KEY`, `AI_ARK_API_KEY`, `EMAILBISON_API_KEY`,
`SLACK_BOT_TOKEN`, `RB2B_WEBHOOK_TOKEN`.

**EmailBison target:** `EMAILBISON_WORKSPACE_ID` (=29), `EMAILBISON_BASE_URL`
(=`https://personal.buzzlead.io`), `EMAILBISON_CAMPAIGN_NAME`
(=`Vida - RB2B Website Visitors`).

**Slack / tuning:** `SLACK_CHANNEL` (=`#vida-buzzlead-private-channel`),
`SLACK_DIGEST_INTERVAL_SECONDS` (=900), `COPY_MODEL` (optional override),
`DEDUPE_WINDOW_HOURS` (=24), `DB_PATH` / `DATA_DIR` (point at a volume to persist).

## Custom fields written to EmailBison

Each staged lead carries `rb2b_variant`, `captured_url`, `intent_tier`,
`research_brief`, `icp_verdict` (for variant-level reporting) plus the sequence
merge fields `personalization 1` (Email 1), `personalization 2` (Email 2), and
`company name cleaned`. Missing custom variables are auto-registered in the
workspace before upsert.

**"Paused" means:** the service upserts leads and *attaches* them to the campaign
but never *starts* it. Leads sit staged until a human activates the campaign in
EmailBison. Confirm the campaign itself is paused there before turning on live
traffic.

## Local dry-run (do this before going live)

Runs the full chain — including real copy generation — but touches **nothing**
external (no EmailBison, no Slack, no paid AI Ark enrichment):

```bash
pip install -r requirements.txt
DRY_RUN=1 ANTHROPIC_API_KEY=sk-... python scripts/dry_run.py
# or your own rows:
DRY_RUN=1 ANTHROPIC_API_KEY=sk-... python scripts/dry_run.py my_rows.json
```

Review the printed Email 1 / Email 2 against
`copy-previews/01-rb2b-website-visitor-intent-drafts.md` before wiring the push.

## Smoke test once deployed

```bash
curl -sS -X POST \
  "https://<your-railway-domain>/webhooks/rb2b?token=$RB2B_WEBHOOK_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"Company Name":"Meridian BPO Solutions","First Name":"Dana","Last Name":"Ruiz","Title":"VP of Operations","Business Email":"dana.ruiz@meridianbpo.com","Website":"https://meridianbpo.com","Industry":"BPO / Call Center","Employee Count":"200-500","City":"Dallas","State":"TX","Zipcode":"75201","Seen At":"2026-07-14T15:00:00Z","LinkedIn URL":"https://linkedin.com/in/dana-ruiz-bpo","Captured URL":"https://vida.io/pricing","Tags":"Hot Pages, ICP"}'
# → {"status":"accepted"}   (processing happens in the background worker)

curl -sS https://<your-railway-domain>/health
```

Point RB2B's dashboard webhook at
`https://<your-railway-domain>/webhooks/rb2b?token=<RB2B_WEBHOOK_TOKEN>` only
after the dry-run and smoke test pass review.

## Two flagged assumptions (see addendum §8)

1. **ICP rules source.** `vida_master_workbook_reference.docx` (the authoritative
   Rule 1-7 picklists) was not in the handoff. `app/icp_gate.py` encodes the
   addendum's *summary* of those rules plus an LLM classifier; paste the verbatim
   workbook rules into `MASTER_RULES` there to harden it.
2. **AI Ark per-segment title map.** The verbatim title map from prior title-skill
   work wasn't in the handoff either; `app/aiark.py` `TITLE_MAP`/`LEAD_WITH_TITLES`
   is built from the addendum's buyer-archetype summary. Swap in the verbatim map
   when available.

## Model note

The approved prompt files pin a model id per JSON block; the service honors it. If
that id is ever retired, set `COPY_MODEL` to a current Sonnet id — the copy text
(system prompts, guardrails) is untouched.
