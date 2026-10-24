"""Bounded facts for a voice conversation, rebuilt from the trusted source boundary.

This is a field allowlist, not a dump of an invoice, directory or model payload.
It deliberately contains no contact destinations or financial account details.
"""
from __future__ import annotations

import json
import math
import re
from datetime import datetime
from decimal import Decimal
from urllib.parse import urlsplit

from common.source_repository import SourceRepository, SourceUnavailable

MAX_CONTEXT_BYTES = 16_000
_EMAIL = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")
_PHONE = re.compile(r"(?<!\w)(?:\+[1-9][0-9 ()-]{7,}[0-9]|\(?[0-9]{3}\)?[ .-][0-9]{3}[ .-][0-9]{4}|[0-9]{10,})(?!\w)")
_SECRET = re.compile(r"\b(?:sk|pk)-[A-Za-z0-9_-]{8,}\b")
_DOCUMENTS = {"rate_confirmation", "pod", "bill_of_lading", "invoice", "detention_log",
              "detention_receipt", "lumper_receipt"}
_CHARGES = {"linehaul", "detention", "lumper", "layover", "tonu", "extra_stop", "fuel"}


def _text(value, limit=120):
    if not isinstance(value, str):
        return None
    bounded = " ".join(value[:max(limit * 4, 512)].split())
    return _SECRET.sub("[redacted]", _PHONE.sub("[redacted]", _EMAIL.sub("[redacted]", bounded)))[:limit]


def _number(value, maximum=1_000_000_000):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return value if 0 <= value <= maximum else None


def _money(value):
    value = _number(value)
    return value if value is not None and Decimal(str(value)) * 100 % 1 == 0 else None


def _timestamp(value):
    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.isoformat() if parsed.tzinfo is not None else None


def _reference(value):
    text = _text(value, 160)
    if not text or "[redacted]" in text:
        return None
    try:
        parsed = urlsplit(text)
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            return None
    except ValueError:
        return None
    return text


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _items(value, maximum=8):
    return value[:maximum] if isinstance(value, list) else []


def _detention(source):
    terms = _mapping(source.get("detention"))
    result = {"type": "detention" if terms or source.get("claim_type") == "detention" else _text(source.get("claim_type")),
        "claimed_amount": _money(terms.get("claimed_amount")),
        "claimed_hours": _number(terms.get("claimed_hours", source.get("detention_hours_claimed")), 1000),
        "free_minutes": _number(terms.get("free_minutes", source.get("free_time_min")), 10_080),
        "rate_per_hour": _money(terms.get("rate_per_hour")),
        "free_time_starts_at": _text(terms.get("free_time_starts_at")),
        "arrival_at": _timestamp(terms.get("arrival_at")),
        "appointment_at": _timestamp(terms.get("appointment_at")),
        "released_at": _timestamp(terms.get("released_at")),
        "timezone": _text(terms.get("timezone")), "billable_minutes": None,
        "supported_amount": None, "difference_from_claim": None,
        "calculation_status": "insufficient_source_terms", "payment_authorized": False}
    if (result["free_time_starts_at"] == "later_of_arrival_or_appointment"
            and all(result[key] is not None for key in ("arrival_at", "appointment_at", "released_at",
                                                       "free_minutes", "rate_per_hour"))):
        start = max(datetime.fromisoformat(result["arrival_at"]), datetime.fromisoformat(result["appointment_at"]))
        released = datetime.fromisoformat(result["released_at"])
        if 0 <= (released - start).total_seconds() <= 7 * 24 * 60 * 60:
            minutes = max(Decimal(str((released - start).total_seconds())) / 60
                          - Decimal(str(result["free_minutes"])), Decimal(0))
            amount = (minutes * Decimal(str(result["rate_per_hour"])) / 60).quantize(Decimal("0.01"))
            result.update(billable_minutes=float(minutes), supported_amount=float(amount),
                          calculation_status="provisional_source_calculation")
            if result["claimed_amount"] is not None:
                result["difference_from_claim"] = float(Decimal(str(result["claimed_amount"])) - amount)
    result["note"] = ("Calculated support is provisional, not an approved charge or payment. "
                      "Confirm the supplied times, agreed terms and required signed detention documentation.")
    return result


async def build_voice_context(source: dict, repository: SourceRepository | None = None, *,
                              purpose: str = "verification") -> dict:
    """Return only safe conversation facts from an already loaded authoritative source."""
    if not isinstance(source, dict) or purpose not in {"verification", "collections", "approvals", "cash_brief"}:
        raise ValueError("a source object and supported call purpose are required")
    parties = {}
    supplied_parties = _mapping(source.get("parties"))
    for role in ("broker", "carrier", "shipper", "receiver"):
        party = _mapping(supplied_parties.get(role))
        identifier = party.get("id") or source.get(f"{role}_id")
        name = party.get("name")
        if not name and isinstance(identifier, str) and len(identifier) <= 120 and repository is not None:
            try:
                name = (await repository.get_company(identifier)).get("name")
            except SourceUnavailable:
                pass
        parties[role] = {"id": _text(identifier), "name": _text(name)}
    route = _mapping(source.get("route"))
    stops = []
    for item in _items(source.get("stops"), 4):
        stop = _mapping(item)
        stops.append({**{key: _text(stop.get(key)) for key in ("kind", "facility", "city", "state", "timezone")},
                      **{key: _timestamp(stop.get(key)) for key in ("appointment_at", "arrival_at", "released_at")}})
    cargo = _mapping(source.get("cargo"))
    documents = []
    for item in _items(source.get("documents")):
        document = _mapping(item)
        if document.get("kind") not in _DOCUMENTS:
            continue
        if document.get("load_id") and document["load_id"] != source.get("load_id"):
            continue
        reference = _reference(document.get("ref"))
        present = document.get("present") if isinstance(document.get("present"), bool) else bool(reference)
        signed = document.get("signed") if isinstance(document.get("signed"), bool) else None
        status = "missing" if not present else "unsigned" if signed is False else "present"
        documents.append({"kind": document["kind"], "reference": reference,
                          "status": status, "present": present, "signed": signed})
    items = []
    for item in _items(source.get("line_items"), 12):
        item = _mapping(item)
        amount = _money(item.get("amount"))
        if item.get("kind") in _CHARGES and amount is not None:
            items.append({"kind": item["kind"], "amount": amount,
                          "approved": item.get("approved") if isinstance(item.get("approved"), bool) else None})
    amount, paid = _money(source.get("invoice_amount")), _money(source.get("paid_amount"))
    linehaul = [item for item in items if item["kind"] == "linehaul"]
    raw_items = source.get("line_items")
    items_complete = isinstance(raw_items, list) and len(raw_items) == len(items) and bool(items)
    scenario = _mapping(source.get("scenario"))
    dispatcher = _mapping(_mapping(source.get("contacts")).get("carrier_dispatcher"))
    context = {
        "version": 1, "synthetic": any(source.get(key) is True for key in ("synthetic", "simulated", "source_simulated")),
        "purpose": purpose, "case_id": _text(source.get("case_id")), "load_id": _text(source.get("load_id")),
        "load_reference": _text(source.get("load_reference") or source.get("load_id"), 80),
        "invoice_id": _text(source.get("invoice_id")),
        "invoice_reference": _text(source.get("invoice_reference") or source.get("invoice_id"), 80),
        "bill_of_lading_reference": _text(source.get("bill_of_lading_reference"), 80),
        "scenario": {"kind": _text(scenario.get("kind")), "summary": _text(scenario.get("summary"), 480),
                     "fictional_notice": _text(scenario.get("fictional_notice"), 300)},
        "parties": parties,
        "route": {side: {key: _text(_mapping(route.get(side)).get(key)) for key in ("city", "state", "facility")}
                  for side in ("origin", "destination")},
        "stops": stops,
        "cargo": {"commodity": _text(cargo.get("commodity")), "equipment": _text(cargo.get("equipment")),
                  "pallets": _number(cargo.get("pallets"), 1000), "weight_lb": _number(cargo.get("weight_lb"), 200_000)},
        "invoice": {"currency": "USD", "amount": amount, "paid_amount": paid,
                    "payment_status": "unknown" if amount is None or paid is None else
                    "unpaid" if paid == 0 else "partially_paid" if paid < amount else "paid",
                    "linehaul_amount": (float(sum(Decimal(str(item["amount"])) for item in linehaul))
                                         if items_complete and linehaul else _money(source.get("contract_rate"))),
                    "claimed_accessorial_amount": float(sum(Decimal(str(item["amount"])) for item in items
                                                            if item["kind"] != "linehaul")) if items_complete else None,
                    "line_items": items, "line_items_complete": items_complete},
        "claim": _detention(source), "documents": documents,
        "contacts": {"carrier_dispatcher": {"name": _text(dispatcher.get("name")), "role": _text(dispatcher.get("role"))},
                     "email": None, "submission_channels": [],
                     "note": "No email, SMS, or document-upload intake is configured for this call. "
                             "Do not invent an address or promise to send/receive documents; record that follow-up is needed."},
        "evidence_refs": list(dict.fromkeys(ref for value in _items(source.get("evidence_refs"), 12)
                                             if (ref := _reference(value)))),
    }
    if len(json.dumps(context, allow_nan=False).encode()) > MAX_CONTEXT_BYTES:
        raise SourceUnavailable("voice source context exceeds the safe size limit")
    return context


def verification_questions(context: dict, task_type: str) -> list[str]:
    """Ask about supplied facts, without inventing receipt destinations or promising payment."""
    if task_type == "verify_bank_change":
        return [("Did your authorized representative request a payment-detail change? "
                 "Do not share bank or account details on this call.")]
    claim = context.get("claim", {})
    if claim.get("type") != "detention" or claim.get("claimed_amount") is None:
        return ["Please describe the disputed load and the additional charge.",
                ("What supporting receipt and arrival/release timestamps are available? "
                 "We will record any missing evidence for follow-up.")]
    load = context.get("load_reference") or "the assigned load"
    invoice = context.get("invoice_reference") or "the supplied invoice"
    questions = [(f"For load {load}, invoice {invoice} claims ${claim['claimed_amount']:.2f} in detention. "
                  "What arrival, appointment and release times support that charge?")]
    if claim.get("supported_amount") is not None:
        questions.append(f"The supplied terms allow {claim['free_minutes']:g} free minutes from the later of arrival "
            f"or appointment, then ${claim['rate_per_hour']:.2f} per hour. Those times give "
            f"{claim['billable_minutes']:g} billable minutes, provisionally ${claim['supported_amount']:.2f}. "
            "Was there additional delay or a different agreed term we should review?")
    signed_support = any(item.get("kind") == "detention_receipt" and item.get("present") is True
                         and item.get("signed") is True for item in context.get("documents", []))
    support_question = ("Please confirm the supplied signed detention receipt matches these check-in and check-out times."
                        if signed_support else "Do you have a signed detention receipt or signed check-in and check-out record? "
                        "The supplied detention support is missing or unsigned.")
    questions.extend([support_question,
                      "What facts should we record for the broker's review? This call does not approve a charge or payment."])
    return questions
