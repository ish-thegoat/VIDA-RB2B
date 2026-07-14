"""SQLite-backed state: dedupe window, drop log, staged leads, digest buffer.

Everything the addendum asks to be "queryable, not just console" lands here
(addendum build note §7: log every dropped hit; run-commands: staged leads need
to be reportable). One small file DB, WAL mode, safe for the single-process
webhook receiver.

Railway note: the container filesystem is ephemeral. Point DB_PATH at a mounted
volume to persist the dedupe window and drop log across deploys.
"""
from __future__ import annotations

import csv
import json
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone
from typing import Any, Optional

from . import config

_LOCK = threading.Lock()
_CONN: Optional[sqlite3.Connection] = None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _conn() -> sqlite3.Connection:
    global _CONN
    if _CONN is not None:
        return _CONN
    os.makedirs(os.path.dirname(config.DB_PATH), exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS seen (
            dedupe_key TEXT NOT NULL,
            seen_ts    INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_seen_key ON seen(dedupe_key);

        CREATE TABLE IF NOT EXISTS drops (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            ts           TEXT NOT NULL,
            reason       TEXT NOT NULL,
            company_name TEXT,
            domain       TEXT,
            captured_url TEXT,
            icp_verdict  TEXT,
            segment      TEXT,
            detail       TEXT,
            payload      TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_drops_reason ON drops(reason);

        CREATE TABLE IF NOT EXISTS staged (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            ts             TEXT NOT NULL,
            company_name   TEXT,
            domain         TEXT,
            contact_name   TEXT,
            contact_email  TEXT,
            segment        TEXT,
            captured_url   TEXT,
            intent_tier    TEXT,
            variant        TEXT,
            icp_verdict    TEXT,
            eb_lead_ids    TEXT,
            digest_sent    INTEGER NOT NULL DEFAULT 0
        );
        """
    )
    conn.commit()
    _CONN = conn
    return conn


# ── Dedupe ───────────────────────────────────────────────────────────────────

def is_duplicate(dedupe_key: str, window_hours: Optional[int] = None) -> bool:
    """True if this key was seen within the rolling window. Does not record."""
    window = (window_hours if window_hours is not None else config.DEDUPE_WINDOW_HOURS)
    cutoff = int(time.time()) - window * 3600
    with _LOCK:
        conn = _conn()
        row = conn.execute(
            "SELECT 1 FROM seen WHERE dedupe_key = ? AND seen_ts > ? LIMIT 1",
            (dedupe_key, cutoff),
        ).fetchone()
        return row is not None


def record_seen(dedupe_key: str) -> None:
    with _LOCK:
        conn = _conn()
        conn.execute(
            "INSERT INTO seen (dedupe_key, seen_ts) VALUES (?, ?)",
            (dedupe_key, int(time.time())),
        )
        # Opportunistic cleanup so the table doesn't grow forever.
        cutoff = int(time.time()) - max(config.DEDUPE_WINDOW_HOURS, 48) * 3600
        conn.execute("DELETE FROM seen WHERE seen_ts <= ?", (cutoff,))
        conn.commit()


# ── Drops (queryable) ────────────────────────────────────────────────────────

def log_drop(reason: str, lead=None, detail: str = "", payload: Optional[dict] = None) -> None:
    """Record a dropped/held hit. reason ∈ {out_*, aiark_no_match, low_intent_hold,
    duplicate, test_event, error}. Always queryable via the drops table."""
    company = getattr(lead, "company_name", "") if lead is not None else ""
    domain = getattr(lead, "domain", "") if lead is not None else ""
    captured = getattr(lead, "captured_url", "") if lead is not None else ""
    verdict = getattr(lead, "icp_verdict", "") if lead is not None else ""
    segment = getattr(lead, "segment", "") if lead is not None else ""
    if isinstance(segment, list):
        segment = ", ".join(segment)
    body = payload if payload is not None else (getattr(lead, "raw", {}) if lead is not None else {})
    with _LOCK:
        conn = _conn()
        conn.execute(
            "INSERT INTO drops (ts, reason, company_name, domain, captured_url, "
            "icp_verdict, segment, detail, payload) VALUES (?,?,?,?,?,?,?,?,?)",
            (_now_iso(), reason, company, domain, captured, verdict, segment,
             detail, json.dumps(body, default=str)),
        )
        conn.commit()


# ── Staged leads + digest buffer ─────────────────────────────────────────────

def record_staged(lead, eb_lead_ids: Any = None) -> int:
    contact_name = " ".join(x for x in [getattr(lead, "first_name", ""),
                                        getattr(lead, "last_name", "")] if x).strip()
    with _LOCK:
        conn = _conn()
        cur = conn.execute(
            "INSERT INTO staged (ts, company_name, domain, contact_name, contact_email, "
            "segment, captured_url, intent_tier, variant, icp_verdict, eb_lead_ids) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (_now_iso(), lead.company_name, lead.domain, contact_name,
             lead.business_email, ", ".join(lead.segment) if lead.segment else "",
             lead.captured_url, lead.intent_tier, lead.variant, lead.icp_verdict,
             json.dumps(eb_lead_ids or [])),
        )
        conn.commit()
        return cur.lastrowid


def pending_digest_rows() -> list[sqlite3.Row]:
    with _LOCK:
        conn = _conn()
        return conn.execute(
            "SELECT * FROM staged WHERE digest_sent = 0 ORDER BY id ASC"
        ).fetchall()


def mark_digest_sent(ids: list[int]) -> None:
    if not ids:
        return
    with _LOCK:
        conn = _conn()
        conn.executemany("UPDATE staged SET digest_sent = 1 WHERE id = ?", [(i,) for i in ids])
        conn.commit()


# ── Manual-review queue (AI Ark no-match) ────────────────────────────────────

def append_manual_review(lead) -> None:
    """Company passed the ICP gate but AI Ark found no contact. Per the
    self-disqualify principle (addendum §4 Step 2) we do NOT drop it — a human
    decides. Written to the CSV path from config."""
    path = config.MANUAL_REVIEW_CSV
    os.makedirs(os.path.dirname(path), exist_ok=True)
    exists = os.path.exists(path)
    with _LOCK, open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if not exists:
            writer.writerow([
                "ts", "company_name", "domain", "website", "industry",
                "employee_count", "captured_url", "segment", "icp_verdict",
                "intent_tier", "linkedin_url",
            ])
        writer.writerow([
            _now_iso(), lead.company_name, lead.domain, lead.website, lead.industry,
            lead.employee_count, lead.captured_url,
            ", ".join(lead.segment) if lead.segment else "", lead.icp_verdict,
            lead.intent_tier, lead.linkedin_url,
        ])


# ── Simple counters for /health and reporting ────────────────────────────────

def counts() -> dict:
    with _LOCK:
        conn = _conn()
        staged = conn.execute("SELECT COUNT(*) AS n FROM staged").fetchone()["n"]
        drops = conn.execute("SELECT COUNT(*) AS n FROM drops").fetchone()["n"]
        pending = conn.execute(
            "SELECT COUNT(*) AS n FROM staged WHERE digest_sent = 0"
        ).fetchone()["n"]
    return {"staged": staged, "drops": drops, "pending_digest": pending}
