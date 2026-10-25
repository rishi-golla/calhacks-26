"""Shown work: every number an agent or the Validator reports carries the formula that produced it.

A working is a small, serializable record a human can re-run by hand::

    {"label": "Delta vs contract", "formula": "invoice_amount - contract_rate",
     "inputs": {"invoice_amount": 2150.0, "contract_rate": 2000.0}, "result": 150.0,
     "unit": "usd", "evidence_refs": ["doc:INV-1", "doc:RC-1"], "basis": "P-001"}

Agents put a list under ``findings["workings"]``; the Validator attaches one to each check as
``work``. Values are plain JSON (floats for money, already exact-cent) so the dashboard can
render them without knowing the domain. Nothing here is an authorization or a decision.
"""
from __future__ import annotations

import re
from decimal import Decimal
from typing import Any

UNITS = {"usd", "min", "h", "mi", "ratio", "bool", "count", "text"}
# Which source document answers for which input, by the exported document kind.
DOCUMENT_KIND_FOR_INPUT = {
    "contract_rate": "rate_confirmation", "linehaul": "rate_confirmation", "fsc": "rate_confirmation",
    "free_time_min": "rate_confirmation", "detention_rate_hr": "rate_confirmation",
    "invoice_amount": "carrier_invoice", "line_items_total": "carrier_invoice", "paid_amount": "carrier_invoice",
    "shipper_invoice_amount": "shipper_invoice",
    "detention_hours_claimed": "carrier_invoice", "stated_total": "carrier_invoice",
    "gps_dwell_min": "pod", "pod_signed": "pod", "receipt_ref": "lumper_receipt",
}



def input_name(raw: Any, index: int) -> str:
    """A formula identifier (letters, digits, underscores) so the dashboard can substitute and recompute."""
    name = re.sub(r"[^a-z0-9_]+", "_", str(raw).lower()).strip("_")
    if not name or name[0].isdigit():
        name = f"line_{index}"
    return name


def unique_name(name: str, taken: dict | set, index: int) -> str:
    """`name`, or `name_<index>`, or `name_<index>_2`... until it collides with nothing already shown."""
    candidate, attempt = name, 1
    while candidate in taken:
        attempt += 1
        candidate = f"{name}_{index}" if attempt == 2 else f"{name}_{index}_{attempt - 1}"
    return candidate


def plain(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {str(k): plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    return value


def working(label: str, formula: str, inputs: dict[str, Any], result: Any, *, unit: str = "usd",
            evidence_refs: list[str] | None = None, basis: str = "") -> dict:
    if unit not in UNITS:
        raise ValueError(f"unknown working unit {unit}")
    return {"label": label, "formula": formula, "inputs": plain(inputs), "result": plain(result), "unit": unit,
            "evidence_refs": list(dict.fromkeys(evidence_refs or [])), "basis": basis}


def document_ref(source: dict, kind: str) -> str | None:
    """The evidence ref of a source document by kind, or the invoice ref when the export lists it only as evidence."""
    for document in source.get("documents", []):
        if isinstance(document, dict) and document.get("kind") == kind and document.get("ref"):
            return document["ref"]
    prefix = {"carrier_invoice": "doc:INV-", "shipper_invoice": "doc:SINV-"}.get(kind)
    if prefix:
        for ref in source.get("evidence_refs", []):
            if isinstance(ref, str) and ref.startswith(prefix):
                return ref
    return None


def refs_for(source: dict, *inputs: str) -> list[str]:
    """Evidence refs that support the named inputs; unknown inputs contribute nothing rather than a guess."""
    found = []
    for name in inputs:
        kind = DOCUMENT_KIND_FOR_INPUT.get(name)
        ref = document_ref(source, kind) if kind else None
        if ref and ref not in found:
            found.append(ref)
    return found
