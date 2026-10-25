"""Deterministic carrier-settlement checks against a source bundle."""
import re
from datetime import datetime
from decimal import Decimal

from agents.documents.tools import check_packet
from common.agent_support import money, number
from common.policy import CONTRACT_TOLERANCE
from common.workings import document_ref, refs_for, working


def duplicate_invoices(source: dict) -> list[str]:
    """Match prior source invoices by carrier/load/amount inside the 14-day window."""
    previous = source.get("previous_invoices", [])
    if not previous:
        return []
    issued = datetime.fromisoformat(source["issued_at"])
    found = []
    for invoice in previous:
        if (invoice.get("id") and invoice["id"] != source.get("invoice_id")
                and invoice.get("carrier_id") == source.get("carrier_id")
                and invoice.get("load_id") == source.get("load_id")
                and money(invoice.get("amount")) == money(source.get("invoice_amount"))):
            age = (issued - datetime.fromisoformat(invoice["issued_at"])).total_seconds()
            if 0 <= age <= 14 * 24 * 60 * 60:
                found.append(invoice["id"])
    return found


def three_way_match(subject: dict) -> dict:
    invoice = money(subject.get("invoice_amount"))
    contract = money(subject.get("contract_rate"))
    delta = invoice - contract
    above_tolerance = invoice > contract * Decimal("1.02")
    missing = check_packet(subject)["missing"]
    line_items = subject.get("line_items", [])
    # Discounts and credits (QuickPay) are negative lines; the invoice total itself stays non-negative.
    total = sum((money(item.get("amount"), signed=True) for item in line_items), Decimal(0)) if line_items else invoice
    totals_match = total == invoice
    return {"matched": not above_tolerance and not missing and totals_match,
            "invoice_amount": number(invoice), "contract_rate": number(contract), "delta": number(delta),
            "above_tolerance": above_tolerance, "missing_documents": missing,
            "line_items_total": number(total), "totals_match": totals_match,
            "policy_ids": ["P-001"] if above_tolerance else [],
            "reason": "invoice exceeds the agreed rate tolerance" if above_tolerance else "source matching"}


def input_name(raw: str, index: int) -> str:
    """A formula identifier (letters, digits, underscores) so the dashboard can substitute and recompute."""
    name = re.sub(r"[^a-z0-9_]+", "_", str(raw).lower()).strip("_")
    if not name or name[0].isdigit():
        name = f"line_{index}"
    return name


def line_inputs(line_items: list[dict]) -> dict[str, float]:
    """One named input per invoice line; repeated kinds get a position suffix."""
    inputs: dict[str, float] = {}
    for index, item in enumerate(line_items, start=1):
        name = input_name(item.get("kind") or item.get("desc") or f"line_{index}", index)
        if name in inputs:
            name = f"{name}_{index}"
        inputs[name] = number(money(item.get("amount"), signed=True))
    return inputs


def deposit_inputs(deposits: list[dict]) -> dict[str, float]:
    """Deposit amounts under formula-safe names; the deposit IDs belong in the label, not the formula."""
    return {f"deposit_{index}": number(money(deposit.get("amount")))
            for index, deposit in enumerate(deposits, start=1)}


def match_workings(source: dict, match: dict) -> list[dict]:
    """The arithmetic behind a three-way match, each step tied to the document it reads."""
    invoice, contract = money(source.get("invoice_amount")), money(source.get("contract_rate"))
    ceiling = contract * (Decimal(1) + Decimal(str(CONTRACT_TOLERANCE)))
    both = refs_for(source, "invoice_amount", "contract_rate")
    items = [
        working("Invoice vs rate confirmation", "invoice_amount - contract_rate",
                {"invoice_amount": invoice, "contract_rate": contract}, invoice - contract,
                evidence_refs=both, basis="three-way match"),
        working("Tolerance ceiling", "contract_rate * (1 + tolerance)",
                {"contract_rate": contract, "tolerance": CONTRACT_TOLERANCE}, ceiling.quantize(Decimal("0.01")),
                evidence_refs=refs_for(source, "contract_rate"), basis="P-001"),
        working("Within tolerance", "invoice_amount <= ceiling",
                {"invoice_amount": invoice, "ceiling": ceiling.quantize(Decimal("0.01"))},
                not match["above_tolerance"], unit="bool", evidence_refs=both, basis="P-001"),
    ]
    line_items = source.get("line_items", [])
    if line_items:
        inputs = line_inputs(line_items)
        invoice_ref = [ref for ref in [document_ref(source, "carrier_invoice")] if ref]
        items.append(working("Invoice lines add up", " + ".join(inputs), inputs, match["line_items_total"],
                             evidence_refs=invoice_ref, basis="P-010"))
        items.append(working("Lines equal stated total", "line_items_total == invoice_amount",
                             {"line_items_total": match["line_items_total"], "invoice_amount": invoice},
                             match["totals_match"], unit="bool", evidence_refs=invoice_ref, basis="P-010"))
    packet = check_packet(source)
    items.append(working("Payment packet", "required_documents - received_documents",
                         {"required_documents": source.get("required_documents", ["rate_confirmation", "pod"]),
                          "received_documents": sorted({d.get("kind") for d in packet["documents"]})},
                         packet["missing"], unit="text",
                         evidence_refs=[d["ref"] for d in packet["documents"] if d.get("ref")],
                         basis="pay only against a rate confirmation and a signed POD"))
    return items
