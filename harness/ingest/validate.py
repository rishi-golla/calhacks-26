"""Input validation for Redis `events` payloads (synthetic or real; never assumed correct).

Tolerant by design: values that can be fixed without guessing are normalized (numbers sent as text, dates in other
formats, booleans as strings), suspicious values are flagged for review, and only an unusable event is rejected
(the caller dead-letters it). Field names follow harness/DATA_CONTRACT.md and the W4 event shapes."""
import re
from datetime import UTC, datetime
from typing import Any

from harness.ingest.amounts import money

NUM, DATE, BOOL, TEXT, LIST = "number", "date", "bool", "text", "list"

# Per event type: field -> kind. "required" fields are needed for the work to make sense; missing ones are flagged,
# not rejected (a person can still resolve the item).
SPEC: dict[str, dict[str, Any]] = {
    "carrier_invoice.received": {
        "required": {"load_id": TEXT, "invoice_amount": NUM},
        "optional": {"contract_rate": NUM, "detention_hours_claimed": NUM, "free_time_min": NUM, "gps_dwell_min": NUM,
                     "invoice_date": DATE, "date": DATE, "receipt_attached": BOOL, "pod_signed": BOOL,
                     "line_items": LIST, "invoice_number": TEXT, "carrier_id": TEXT, "bol": TEXT}},
    "shipper_payment.received": {
        "required": {"amount": NUM},
        "optional": {"invoice_amount": NUM, "paid_amount": NUM, "posted_at": DATE, "invoices_applied": LIST,
                     "invoice_id": TEXT, "shipper_id": TEXT}},
    "bank_change.requested": {
        "required": {"carrier_id": TEXT, "new_account": TEXT, "new_routing": TEXT},
        "optional": {"next_payment_amount": NUM, "sender_matches_registered": BOOL, "effective_date": DATE,
                     "date": DATE}},
    "pod.received": {
        "required": {"load_id": TEXT},
        "optional": {"signed": BOOL, "delivered_at": DATE, "contract_rate": NUM, "exceptions": LIST}},
    "bank.event": {
        "required": {"amount": NUM},
        "optional": {"posted_at": DATE, "direction": TEXT}},
    "email.received": {"required": {"text": TEXT}, "optional": {"from": TEXT, "subject": TEXT, "date": DATE}},
}
ID_PATTERNS = {"case_id": re.compile(r"^CASE-[A-Za-z0-9-]+$"), "load_id": re.compile(r"^(LOAD|LD)-[A-Za-z0-9-]+$"),
               "carrier_id": re.compile(r"^C-[A-Za-z0-9-]+$"), "shipper_id": re.compile(r"^S-[A-Za-z0-9-]+$")}
MAX_AMOUNT = 1_000_000.0
DATE_FORMATS = ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%m/%d/%Y", "%m/%d/%Y %H:%M")


def parse_date(value: Any) -> str | None:
    """Normalize to ISO 8601 UTC; None if it isn't a recognizable date."""
    if not isinstance(value, str) or not value.strip():
        return None
    v = value.strip().replace("Z", "+00:00")
    for fmt in DATE_FORMATS:
        try:
            d = datetime.strptime(v, fmt)
        except ValueError:
            continue
        d = d if d.tzinfo else d.replace(tzinfo=UTC)
        return d.astimezone(UTC).isoformat().replace("+00:00", "Z")
    return None


def _coerce(kind: str, value: Any) -> tuple[Any, str | None]:
    """(normalized value, problem). The value is unchanged when it can't be fixed safely."""
    if value is None:
        return None, None
    if kind == NUM:
        if isinstance(value, bool):
            return value, "is a boolean, expected a number"
        if isinstance(value, (int, float)):
            return float(value), None
        parsed = money(value if "$" in str(value) else f"${value}") if isinstance(value, str) else None
        return (parsed, None) if parsed is not None else (value, f"'{str(value)[:20]}' is not a number")
    if kind == DATE:
        iso = parse_date(value)
        return (iso, None) if iso else (value, f"'{str(value)[:25]}' is not a date")
    if kind == BOOL:
        if isinstance(value, bool):
            return value, None
        if isinstance(value, str) and value.strip().lower() in ("true", "yes", "y", "1", "false", "no", "n", "0"):
            return value.strip().lower() in ("true", "yes", "y", "1"), None
        return value, f"'{str(value)[:20]}' is not true/false"
    if kind == LIST:
        return (value, None) if isinstance(value, list) else (value, "is not a list")
    return (value, None) if isinstance(value, str) else (str(value), None)


def validate(event_type: str, payload: dict) -> tuple[dict, list[str], str | None]:
    """Returns (normalized payload, issues to flag for review, fatal reason or None)."""
    if not isinstance(payload, dict):
        return payload, [], "payload must be an object"
    spec = SPEC.get(event_type)
    out = dict(payload)
    issues: list[str] = []
    if spec is None:
        return out, issues, None   # unknown or untyped (gps.ping, load.*): nothing to check
    for name, kind in {**spec["required"], **spec["optional"]}.items():
        value = out.get(name)
        if value in (None, "", []):
            if name in spec["required"]:
                issues.append(f"input: missing {name}")
            continue
        fixed, problem = _coerce(kind, value)
        out[name] = fixed
        if problem:
            issues.append(f"input: {name} {problem}")
        elif kind == NUM and isinstance(fixed, float):
            if fixed < 0 and name not in ("amount",):
                issues.append(f"input: {name} is negative ({fixed})")
            elif abs(fixed) > MAX_AMOUNT and name not in ("free_time_min", "gps_dwell_min"):
                issues.append(f"input: {name} implausibly large ({fixed})")
    for name, pattern in ID_PATTERNS.items():
        value = out.get(name)
        if isinstance(value, str) and value and not pattern.match(value):
            issues.append(f"input: {name} '{value[:24]}' has an unexpected format")
    issues += _consistency(event_type, out)
    return out, issues, None


def _consistency(event_type: str, p: dict) -> list[str]:
    """Cheap cross-field checks that need no other system. Business judgment stays with the agents."""
    issues = []
    inv, rate = p.get("invoice_amount"), p.get("contract_rate")
    if event_type == "carrier_invoice.received" and isinstance(inv, float) and isinstance(rate, float) and rate > 0 \
            and inv > rate * 1.5:
        issues.append(f"input: invoice {inv:.2f} is more than 1.5x the contract rate {rate:.2f}")
    hours = p.get("detention_hours_claimed")
    if isinstance(hours, float) and hours > 72:
        issues.append(f"input: detention_hours_claimed implausible ({hours})")
    if event_type == "pod.received" and p.get("signed") is False:
        issues.append("input: POD is not signed")
    return issues
