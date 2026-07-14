"""FastAPI entrypoint for the Vida RB2B webhook receiver.

Routes:
  POST /webhooks/rb2b?token=<secret>  — validate token + payload, enqueue, 200 fast
  GET  /health                        — 200 for uptime monitoring

RB2B times out ~15s and disables the integration on repeated failures, so the
route does zero real work: it validates, drops the payload on an in-process
asyncio queue, and returns immediately. A background worker drains the queue and
runs the full pipeline (ICP gate, AI Ark, copy, EmailBison) off the request path.
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import FastAPI, Query, Request, Response
from fastapi.responses import JSONResponse

from . import config, slack, store
from .pipeline import process

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("rb2b.main")

app = FastAPI(title="Vida RB2B Webhook Receiver", version="1.0.0")

_QUEUE: asyncio.Queue = asyncio.Queue(maxsize=10_000)
_WORKERS: list[asyncio.Task] = []
_DIGEST_TASK: asyncio.Task | None = None

# Lightweight request stats so we can tell "nothing arrived" from "arrived but
# rejected". Survives until redeploy (in-memory, like the queue).
_STATS = {"accepted": 0, "auth_failures": 0, "bad_json": 0,
          "last_hit_ts": None, "last_auth_failure_ts": None}


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


async def _worker(worker_id: int) -> None:
    log.info("worker %d started", worker_id)
    while True:
        payload = await _QUEUE.get()
        try:
            # Pipeline is sync + I/O-bound (HTTP to Anthropic/AI Ark/EmailBison);
            # run it in a thread so the event loop stays free for new webhooks.
            result = await asyncio.to_thread(process, payload)
            log.info("processed: %s / %s", result.get("status"), result.get("company_name"))
        except Exception:  # a worker must never die on one bad hit
            log.exception("worker %d: unhandled error processing payload", worker_id)
        finally:
            _QUEUE.task_done()


async def _digest_loop() -> None:
    interval = max(30, config.SLACK_DIGEST_INTERVAL_SECONDS)
    log.info("digest loop started (every %ds -> %s)", interval, config.SLACK_CHANNEL)
    while True:
        await asyncio.sleep(interval)
        try:
            sent = await asyncio.to_thread(slack.flush_digest)
            if sent:
                log.info("slack digest: %d staged lead(s) sent", sent)
        except Exception:
            log.exception("digest flush failed")


@app.on_event("startup")
async def _startup() -> None:
    store.counts()  # touch the DB so schema is created at boot
    _WORKERS.append(asyncio.create_task(_worker(1)))
    global _DIGEST_TASK
    _DIGEST_TASK = asyncio.create_task(_digest_loop())
    missing = config.missing_required(for_push=True)
    if missing:
        log.warning("Missing env vars (real pushes will fail until set): %s", ", ".join(missing))


@app.get("/health")
async def health() -> JSONResponse:
    return JSONResponse({"status": "ok", "queue_depth": _QUEUE.qsize(),
                         "requests": dict(_STATS), **store.counts()})


@app.get("/debug/recent")
async def debug_recent(token: str = Query(default="")) -> JSONResponse:
    """Token-gated operational view: recent staged leads + drops + resolved
    campaign status. Behind the webhook token so lead metadata isn't public."""
    if not config.RB2B_WEBHOOK_TOKEN or token != config.RB2B_WEBHOOK_TOKEN:
        return JSONResponse({"error": "invalid token"}, status_code=401)
    from . import emailbison
    campaign = None
    try:
        campaign = await asyncio.to_thread(emailbison.resolve_campaign)
    except Exception as e:
        campaign = {"error": str(e)[:200]}
    return JSONResponse({
        "counts": store.counts(),
        "campaign": campaign,
        "staged": store.recent_staged(5),
        "drops": store.recent_drops(5),
    })


@app.post("/webhooks/rb2b")
async def rb2b_webhook(request: Request, token: str = Query(default="")) -> Response:
    src = request.client.host if request.client else "?"
    _STATS["last_hit_ts"] = _now_iso()

    # 1) auth: single self-contained URL, token in query param (addendum §3).
    if not config.RB2B_WEBHOOK_TOKEN or token != config.RB2B_WEBHOOK_TOKEN:
        _STATS["auth_failures"] += 1
        _STATS["last_auth_failure_ts"] = _now_iso()
        log.warning("401 rejected webhook from %s (token_present=%s, token_matches=%s)",
                    src, bool(token), token == config.RB2B_WEBHOOK_TOKEN)
        return JSONResponse({"error": "invalid token"}, status_code=401)

    # 2) validate: must be a JSON object.
    try:
        payload = await request.json()
    except Exception:
        _STATS["bad_json"] += 1
        log.warning("400 bad JSON on webhook from %s", src)
        return JSONResponse({"error": "invalid JSON"}, status_code=400)
    if not isinstance(payload, dict):
        return JSONResponse({"error": "expected a JSON object"}, status_code=400)

    # 3) enqueue + return 200 immediately (all real work happens in the worker).
    try:
        _QUEUE.put_nowait(payload)
    except asyncio.QueueFull:
        # Shed load rather than block RB2B; the drop is visible in logs.
        log.error("queue full, shedding payload for %s", payload.get("Company Name"))
        return JSONResponse({"status": "queued_full"}, status_code=200)

    _STATS["accepted"] += 1
    log.info("accepted webhook from %s (company=%r)", src, payload.get("Company Name"))
    return JSONResponse({"status": "accepted"}, status_code=200)
