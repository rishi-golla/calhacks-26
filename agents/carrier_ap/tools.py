"""Deterministic carrier-settlement checks against a source bundle."""
from datetime import datetime
from decimal import Decimal

from agents.documents.tools import check_packet
from common.agent_support import money, number


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
