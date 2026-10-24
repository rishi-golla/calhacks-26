"""Extraction schemas per document type (model A) and the event-type -> document-type map."""
from typing import Any

S: dict[str, Any] = {"type": ["string", "null"]}
N: dict[str, Any] = {"type": ["number", "null"]}
B: dict[str, Any] = {"type": ["boolean", "null"]}
AMOUNTS = {"type": "array", "items": {"type": "object", "properties": {"desc": S, "amount": N},
                                      "required": ["desc", "amount"]}}
PII = {"type": "array", "description": "personal names of people and street addresses, copied verbatim",
       "items": {"type": "object", "properties": {"kind": {"type": "string", "enum": ["name", "address"]},
                                                  "text": {"type": "string"}},
                 "required": ["kind", "text"]}}


def _obj(**props: Any) -> dict:
    props["pii"] = PII
    return {"type": "object", "properties": props, "required": list(props)}


SCHEMAS: dict[str, dict] = {
    "carrier_invoice": _obj(invoice_id=S, load_id=S, carrier_id=S, carrier_name=S, invoice_date=S,
                            invoice_amount=N, contract_rate=N, line_items=AMOUNTS,
                            detention_hours_claimed=N, receipt_attached=B),
    "pod": _obj(load_id=S, carrier_id=S, delivered_at=S, receiver=S, signed_by=S, exceptions=S),
    "shipper_payment": _obj(payment_id=S, shipper_id=S, shipper_name=S, amount=N, remittance_ref=S,
                            invoices_applied={"type": "array", "items": {
                                "type": "object", "properties": {"invoice_id": S, "amount": N},
                                "required": ["invoice_id", "amount"]}}),
    "bank_event": _obj(amount=N, direction={"type": ["string", "null"], "enum": ["credit", "debit", None]},
                       counterparty=S, reference=S, posted_at=S),
    "bank_change": _obj(carrier_id=S, carrier_name=S, requested_by=S, channel=S, effective_date=S,
                        new_account=S, new_routing=S, next_payment_amount=N, urgency_language=B),
    "voice_dispute": _obj(load_id=S, carrier_id=S, invoice_id=S, claim=S, amount_disputed=N),
    "email": _obj(sender=S, subject=S, summary=S, load_id=S, invoice_id=S, carrier_id=S, shipper_id=S,
                  amounts=AMOUNTS, intent=S),
}

DOC_TYPE: dict[str, str] = {
    "carrier_invoice.received": "carrier_invoice",
    "pod.received": "pod",
    "shipper_payment.received": "shipper_payment",
    "bank.event": "bank_event",
    "bank_change.requested": "bank_change",
    "voice.inbound.dispute": "voice_dispute",
    "email.received": "email",
}

# Amount fields cross-checked between the structured payload and what model A read from the document.
AMOUNT_FIELDS = ("invoice_amount", "contract_rate", "amount", "amount_disputed", "next_payment_amount")
