"""Copy generation — runs the approved prompt files as-is.

Each research_emails_v*.md holds three fenced ```json blocks that are complete
Anthropic request bodies (Research -> Email 1 -> Email 2), chained via {{merge
tags}} (addendum §6 / §7). We do NOT rewrite the system prompts — they are
approved copy strategy. We only:
  1. read the file for the resolved variant,
  2. fill merge tags from the lead + prior block outputs,
  3. call the Anthropic Messages API with the block's own model/temperature/max_tokens.

Model note: the blocks pin a model id. We honor it unless COPY_MODEL is set in the
env (escape hatch if the pinned id is ever retired) — see README.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass

from . import config, llm, research
from .models import Lead
from .url_mapping import page_label

_PROMPT_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "prompts")
_JSON_BLOCK = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL)
_MERGE = re.compile(r"\{\{\s*([^}]+?)\s*\}\}")


@dataclass
class CopyResult:
    research_output: str
    email_1: str
    email_2: str


def _load_blocks(prompt_file: str) -> list[dict]:
    path = os.path.join(_PROMPT_DIR, prompt_file)
    with open(path, encoding="utf-8") as f:
        text = f.read()
    blocks = [json.loads(m.group(1)) for m in _JSON_BLOCK.finditer(text)]
    if len(blocks) != 3:
        raise RuntimeError(f"{prompt_file}: expected 3 JSON blocks, found {len(blocks)}")
    return blocks


def _fill(text: str, merge: dict[str, str]) -> str:
    """Replace {{Tag}} with merge[Tag]; unknown tags become '' (never leave a
    literal merge tag in copy)."""
    return _MERGE.sub(lambda m: merge.get(m.group(1).strip(), ""), text)


def _run_block(block: dict, merge: dict[str, str]) -> str:
    messages = []
    for msg in block.get("messages", []):
        messages.append({"role": msg.get("role", "user"), "content": _fill(msg.get("content", ""), merge)})
    system, user_messages = llm.split_system(messages)
    model = config.COPY_MODEL_OVERRIDE or block.get("model") or "claude-sonnet-4-6"
    return llm.complete(
        messages=user_messages,
        model=model,
        max_tokens=int(block.get("max_tokens", 1000)),
        temperature=float(block.get("temperature", 0.5)),
        system=system,
    )


def _base_merge(lead: Lead) -> dict[str, str]:
    """Merge values available from the lead (+ enrichment)."""
    return {
        "Company Name": lead.company_name,
        "Domain": lead.domain,
        "Industry": lead.industry,
        "First Name": lead.first_name,
        "Last Name": lead.last_name,
        "Job Title": lead.title,
        "Captured Page": page_label(lead.captured_path),
        "LinkedIn Company Description": lead.raw.get("_company_description", ""),
        "Company Description": lead.raw.get("_company_description", ""),
        "Recent News": lead.raw.get("_recent_news", ""),
    }


def generate(lead: Lead) -> CopyResult:
    """Run the email chain, grounded in REAL research.

    We replace the prompt file's data-less research block with app.research
    (live website fetch + web search), then run the two email blocks against that
    real brief. Falls back to the file's research block only if research returns
    nothing, so copy generation never hard-fails.
    """
    blocks = _load_blocks(lead.variant_prompt)
    merge = _base_merge(lead)

    brief = research.deep_research(lead)
    if not brief:
        brief = _run_block(blocks[0], merge)  # fallback: file's research block
    merge["Research Output"] = brief

    email_1 = _run_block(blocks[1], merge)
    merge["Email 1 Output"] = email_1

    email_2 = _run_block(blocks[2], merge)

    return CopyResult(research_output=brief, email_1=email_1, email_2=email_2)
