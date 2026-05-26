"""
Detects operational facts in Aircall transcripts missing from SmartMoving CRM records.

Usage:
    python analyze_call.py <aircall_json> <smartmoving_json> [--dry-run] [--verbose]

Env:
    ANTHROPIC_API_KEY — required
"""

import argparse
import json
import os
import sys
from enum import Enum
from typing import Literal, NamedTuple

import anthropic
import instructor
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

MODEL = "claude-haiku-4-5-20251001"
MAX_TOKENS = 1024  # enough for ~10 findings with summaries
MAX_RETRIES = 2

# Outbound calls below this duration are voicemails or no-answers — no customer
# voice to analyze. Skipped to avoid wasted API calls and false positives from
# agent-only monologues.
MIN_OUTBOUND_DURATION_SECONDS = 30

# Inventory items at or above this weight are surfaced explicitly in the CRM
# summary so the model doesn't re-flag heavy items already on record.
HEAVY_ITEM_THRESHOLD_LBS = 200

# Present on every SmartMoving quote — excluded from the CRM summary to surface
# only non-standard fees that are genuinely informative to the model.
STANDARD_CHARGES = {
    "Moving Labor",
    "Travel Fee",
    "Materials & Packing Supplies",
    "Full Value Protection",
    "Double Drive Time (DDT)",
    "Overtime Rate (after 8 hours)",
    "Heavy Items Fee",
    "Estimated Move Day Total",
}


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


class Category(str, Enum):
    HEAVY_ITEMS = "HEAVY_ITEMS"
    ACCESS = "ACCESS"
    TIMING = "TIMING"
    ADDRESS_CHANGE = "ADDRESS_CHANGE"
    SPECIAL_HANDLING = "SPECIAL_HANDLING"
    DISASSEMBLY = "DISASSEMBLY"
    PACKING = "PACKING"
    PETS_CHILDREN = "PETS_CHILDREN"
    INSURANCE = "INSURANCE"
    PAYMENT = "PAYMENT"
    BUILDING_MGMT = "BUILDING_MGMT"
    COMMUNICATION_PREFS = "COMMUNICATION_PREFS"


class Finding(BaseModel):
    category: Category = Field(description="The category of the operational gap.")
    summary: str = Field(
        description="One-sentence description of what is missing from the CRM."
    )
    quote: str = Field(
        description="Exact verbatim quote from the transcript that supports the finding."
    )
    confidence: Literal["high", "medium", "low"] = Field(
        description="'high' if directly stated, 'medium' if implied, 'low' if uncertain."
    )


class AnalysisResult(BaseModel):
    findings: list[Finding] = Field(
        description="Operational gaps found. Empty if everything in the transcript is already in the CRM."
    )


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You are a moving-operations auditor. Read a call transcript and identify operational \
facts mentioned by the customer or agent that are NOT already captured in the CRM record.

Categories:
- HEAVY_ITEMS: pianos, safes, pool tables, large appliances, fitness equipment, fish tanks
- ACCESS: service entrances, parking restrictions, gate codes, elevator rules
- TIMING: arrival window changes, elevator time slots, time-sensitive constraints
- ADDRESS_CHANGE: any origin or destination different from the CRM
- SPECIAL_HANDLING: fragile items, artwork, antiques, temperature-sensitive goods
- DISASSEMBLY: furniture or equipment needing disassembly or reassembly
- PACKING: crew packing or unpacking requests
- PETS_CHILDREN: pets or young children present and unattended during active crew operations, requiring crew awareness or affecting safe movement through the space. DO NOT include if they won't be home by the time of the move.
- INSURANCE: valuation coverage questions or high-value item references
- PAYMENT: payment method, processing fees, deposit requirements
- BUILDING_MGMT: COI requirements, elevator deposits, building management contacts
- COMMUNICATION_PREFS: alternate contacts, language needs, preferred contact times

Rules:
1. Only flag facts that are in the transcript AND missing or contradicted in the CRM.
2. Every finding must include a verbatim quote from a single continuous utterance — do not concatenate separate turns.
3. The finding summary must only describe what is directly and literally stated in the quote — do not infer or add context not present in the quote itself.
4. Prefer returning nothing over flagging uncertain gaps.
5. Do not flag concerns that were explicitly resolved by the agent during the call.
6. Flag each operational fact exactly once under the single most specific category — do not create separate findings for the same item under different categories.
7. Do not use a quote as evidence for a finding if that same quote already explains a different finding already flagged.
8. Return only valid JSON. No commentary, no markdown.\
"""


# ---------------------------------------------------------------------------
# Input parsing
# ---------------------------------------------------------------------------


def load_json(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        sys.exit(f"Error: file not found — {path}")
    except json.JSONDecodeError as e:
        sys.exit(f"Error: invalid JSON in {path} — {e}")


def is_voicemail(call: dict) -> bool:
    return (
        call.get("direction") == "outbound"
        and call.get("duration", 0) < MIN_OUTBOUND_DURATION_SECONDS
    )


def get_utterances(call: dict) -> list[dict]:
    try:
        return call["transcription"]["content"]["utterances"]
    except (KeyError, TypeError):
        return []


def build_speaker_map(call: dict) -> dict[str, str]:
    """Map speaker tokens ('agent', 'external') to real names ('Nicole', 'John')."""
    try:
        channels = call["transcription"]["content"]["channels"]
        return {ch["speaker"]: ch["name"] for ch in channels}
    except (KeyError, TypeError):
        return {}


class CallMetadata(NamedTuple):
    direction: str
    duration: int
    agent: str
    customer: str


def _safe_get(d: dict, *keys, default=None):
    """Walk nested dict keys, returning default if any step is missing or None."""
    for key in keys:
        if not isinstance(d, dict):
            return default
        d = d.get(key)
        if d is None:
            return default
    return d


def extract_call_metadata(call: dict) -> CallMetadata:
    """Pull the few fields used in the user prompt header."""
    first = _safe_get(call, "contact", "first_name", default="")
    last = _safe_get(call, "contact", "last_name", default="")
    customer = f"{first} {last}".strip() or "Customer"
    return CallMetadata(
        direction=_safe_get(call, "direction", default="inbound"),
        duration=_safe_get(call, "duration", default=0),
        agent=_safe_get(call, "user", "name", default="Agent"),
        customer=customer,
    )


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


def format_transcript(utterances: list[dict], speaker_map: dict[str, str]) -> str:
    lines = []
    for u in utterances:
        name = speaker_map.get(u["speaker"], u["speaker"].capitalize())
        text = u["text"].strip()
        if text:
            lines.append(f"{name}: {text}")
    return "\n".join(lines)


def _format_stop(stop: dict) -> str:
    addr = stop.get("addressFullAddress", "unknown")
    unit = f", {stop['addressUnit']}" if stop.get("addressUnit") else ""
    prop = stop.get("propertyTypeName", "")

    access = []
    if stop.get("stairs"):
        access.append(f"{stop['stairs']} stairs")
    access.append("elevator" if stop.get("hasElevator") else "no elevator")
    if parking := stop.get("parkingDescription"):
        access.append(f"parking: {parking}")

    line = f"{stop.get('type', 'Stop')}: {addr}{unit} ({prop}, {', '.join(access)})"
    if notes := (stop.get("notes") or "").strip():
        line += f" | {notes}"
    return line


def _format_notes(notes: dict) -> list[str]:
    labels = [
        ("internalNotes", "Notes"),
        ("crewNotes", "Crew notes"),
        ("dispatcherNotes", "Dispatch"),
    ]
    return [
        f"{label}: {val.strip()}"
        for key, label in labels
        if (val := notes.get(key) or "").strip()
    ]


def _format_inventory(inventory: dict) -> list[str]:
    items = inventory.get("items", []) if isinstance(inventory, dict) else []
    if not items:
        return ["Inventory: none recorded"]

    lines = [
        "Inventory: " + ", ".join(f"{i.get('quantity', 1)}x {i['name']}" for i in items)
    ]
    heavy = [
        i for i in items if i.get("estimatedWeightLbs", 0) >= HEAVY_ITEM_THRESHOLD_LBS
    ]
    if heavy:
        lines.append(
            "Heavy items on record: "
            + ", ".join(f"{i['name']} (~{i['estimatedWeightLbs']} lbs)" for i in heavy)
        )
    return lines


def _format_charges(charges: list[dict]) -> str | None:
    special = [
        c
        for c in charges
        if c.get("name") not in STANDARD_CHARGES and c.get("totalCost", 0) > 0
    ]
    if not special:
        return None
    return "Non-standard charges: " + ", ".join(
        f"{c['name']} (${c['totalCost']:.0f})" for c in special
    )


def format_crm(opportunity: dict) -> str:
    """Flatten the SmartMoving opportunity into a concise operational summary."""
    lines = []

    customer = opportunity.get("customer", {})
    name = f"{customer.get('firstName', '')} {customer.get('lastName', '')}".strip()
    if name:
        lines.append(f"Customer: {name}")
    lines.append(f"Status: {opportunity.get('statusName', 'unknown')}")
    if size := opportunity.get("moveSize"):
        lines.append(f"Move size: {size}")

    job = (opportunity.get("jobs") or [{}])[0]
    if window := job.get("arrivalWindow"):
        lines.append(f"Arrival window: {window}")
    lines.append(
        f"Crew: {job.get('crewSize', '?')} movers, {job.get('truckCount', '?')} truck(s)"
    )

    lines.extend(_format_stop(s) for s in job.get("stops", []))
    lines.extend(_format_notes(job.get("notes", {})))
    lines.extend(_format_inventory(job.get("inventory", {})))
    if charges_line := _format_charges(job.get("charges", [])):
        lines.append(charges_line)

    return "\n".join(lines)


def build_user_prompt(transcript: str, crm_summary: str, metadata: CallMetadata) -> str:
    return (
        f"CALL: {metadata.direction}, {metadata.duration}s | "
        f"AGENT: {metadata.agent} | CUSTOMER: {metadata.customer}\n\n"
        f"## CRM Record\n{crm_summary}\n\n"
        f"## Transcript\n{transcript}\n\n"
        'Identify operational gaps. Return a JSON object with a "findings" array.'
    )


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------


def _log_token_usage(response: object) -> None:
    """Log token usage to stderr after each API call."""
    u = response.usage
    print(f"[tokens] input={u.input_tokens}, output={u.output_tokens}", file=sys.stderr)


def analyze(
    aircall_path: str,
    smartmoving_path: str,
    dry_run: bool = False,
    verbose: bool = False,
) -> dict:
    call = load_json(aircall_path)
    opportunity = load_json(smartmoving_path)

    if is_voicemail(call):
        print(
            f"[SKIP] Outbound voicemail ({call.get('duration', 0)}s).", file=sys.stderr
        )
        return {"findings": []}

    utterances = get_utterances(call)
    if not utterances:
        print("[SKIP] No transcript available on this call.", file=sys.stderr)
        return {"findings": []}

    transcript = format_transcript(utterances, build_speaker_map(call))
    crm_summary = format_crm(opportunity)
    user_prompt = build_user_prompt(
        transcript, crm_summary, extract_call_metadata(call)
    )

    if dry_run:
        print("=== SYSTEM ===\n", SYSTEM_PROMPT, file=sys.stderr)
        print("\n=== USER ===\n", user_prompt, file=sys.stderr)
        return {"findings": []}

    client = instructor.from_anthropic(
        anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    )
    client.on("completion:response", _log_token_usage)

    result: AnalysisResult = client.messages.create(
        model=MODEL,
        temperature=0,
        max_tokens=MAX_TOKENS,
        max_retries=MAX_RETRIES,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}],
        response_model=AnalysisResult,
    )

    output = result.model_dump()

    if verbose:
        print(json.dumps(output, indent=2), file=sys.stderr)

    return output


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description="Detect CRM gaps from call transcripts."
    )
    parser.add_argument("aircall_json")
    parser.add_argument("smartmoving_json")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    if not args.dry_run and not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit(
            "Error: ANTHROPIC_API_KEY not set.\n  export ANTHROPIC_API_KEY=sk-ant-..."
        )

    output = analyze(
        args.aircall_json, args.smartmoving_json, args.dry_run, args.verbose
    )
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
