"""Charge calculations use recorded terms; no fixed free-time or fee assumptions."""
from datetime import datetime
from decimal import Decimal, InvalidOperation

from common.agent_support import money, number
from common.source_repository import SourceUnavailable


def duration(value, field):
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise SourceUnavailable(f"invalid {field}") from exc
    if isinstance(value, bool) or not result.is_finite() or result < 0:
        raise SourceUnavailable(f"invalid {field}")
    return result


def gps_dwell_minutes(subject: dict) -> int | None:
    if subject.get("gps_dwell_min") is not None:
        return int(duration(subject["gps_dwell_min"], "GPS dwell minutes"))
    gps = subject.get("gps", {})
    if not gps.get("arrived") or not gps.get("departed"):
        return None
    try:
        arrival, departure = (datetime.fromisoformat(gps[key]) for key in ("arrived", "departed"))
        minutes = (departure - arrival).total_seconds() / 60
    except (TypeError, ValueError):
        return None
    if minutes < 0:
        raise SourceUnavailable("departure precedes arrival")
    return int(minutes)


def audit_charges(source: dict) -> dict:
    dwell = gps_dwell_minutes(source)
    free = duration(source["free_time_min"], "free-time minutes") if source.get("free_time_min") is not None else None
    claimed = duration(source.get("detention_hours_claimed", 0), "claimed detention hours")
    suspicious = dwell is not None and free is not None and claimed > 0 and dwell <= free
    unsupported = []
    unsupported_amount = Decimal(0)
    if suspicious:
        unsupported.append("detention_within_free_time")
    for item in source.get("line_items", []):
        kind = str(item.get("kind", "")).lower()
        if (kind in {"lumper", "layover", "detention", "tonu", "extra_stop"}
                and (item.get("approved") is False or item.get("receipt_required") and not item.get("receipt_ref")
                     or kind == "detention" and suspicious)):
            unsupported.append(f"unsupported_{kind}")
            unsupported_amount += money(item.get("amount"))
    delta = money(source.get("invoice_amount")) - money(source.get("contract_rate"))
    return {"gps_dwell_min": dwell, "free_time_min": number(free) if free is not None else None,
            "detention_hours_claimed": number(claimed),
            "unsupported_charges": sorted(set(unsupported)), "delta": number(delta),
            "unsupported_amount": number(unsupported_amount),
            "needs_verification": suspicious or bool(unsupported),
            "insufficient_timing_evidence": bool(claimed) and (dwell is None or free is None)}
