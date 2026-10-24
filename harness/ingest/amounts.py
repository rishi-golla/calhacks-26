"""Exact money reading from document text. Models are good at finding *which* text is personal data and bad at
turning "$1,769.44" into 1769.44 (benchmarked: Olmo-3-7B shifted decimals or gave up on 132/132 invoices), so the
amounts the cross-check compares come from here: Decimal parsing, no guessing. When a document's figures are
missing or contradict each other the reader returns None and the pipeline flags the document for review."""
import re
from decimal import Decimal, InvalidOperation

_MONEY = r"\(?-?\$\s?-?\d[\d,]*(?:\.\d+)?\)?"   # also "$-63.50"
MONEY = re.compile(_MONEY)
TOTAL = re.compile(rf"(?i)\b(?:invoice\s+)?total(?:\s+(?:due|amount))?\s*[:\-]?\s*({_MONEY})")
PAYMENT = re.compile(rf"(?i)\b(?:payment\s+of|amount\s+paid\s*[:\-]?|paid\s*[:\-])\s*({_MONEY})")
HEADER = re.compile(r"(?i)\bdescription\b.*\bamount\b")


def money(text: str | None) -> float | None:
    """'$3,925.00' -> 3925.0, '($12.50)' -> -12.5. None if it isn't a well-formed amount."""
    if not isinstance(text, str):
        return None
    t = text.strip()
    negative = (t.startswith("(") and t.endswith(")")) or "-" in t
    t = t.strip("()").replace("$", "").replace(",", "").replace("-", "").strip()
    try:
        value = Decimal(t)
    except InvalidOperation:
        return None
    return float(-value if negative else value)


def read_invoice(text: str) -> dict:
    """{"invoice_amount": float|None, "line_items": [{"desc", "amount"}], "issues": [...]}"""
    issues: list[str] = []
    totals = {money(m) for m in TOTAL.findall(text)} - {None}
    total = totals.pop() if len(totals) == 1 else None
    if len(totals) > 0:   # more than one distinct stated total
        issues.append("document states conflicting totals")
    elif total is None:
        issues.append("no total found in document")
    lines = text.splitlines()
    start = next((i + 1 for i, line in enumerate(lines) if HEADER.search(line)), None)
    items = []
    for line in lines[start:] if start is not None else []:
        if TOTAL.search(line):
            break
        amounts = MONEY.findall(line)
        desc = re.split(r"\s+\d|\s+\$|\s+\(", line.strip(), maxsplit=1)[0].strip()
        if amounts and desc and desc[0].isalpha():
            items.append({"desc": desc, "amount": money(amounts[-1])})
    return {"invoice_amount": total, "line_items": items, "issues": issues}


def read_payment(text: str) -> dict:
    found = {money(m) for m in PAYMENT.findall(text)} - {None}
    if len(found) == 1:
        return {"amount": found.pop(), "issues": []}
    return {"amount": None, "issues": ["conflicting payment amounts" if found else "no payment amount found"]}


READERS = {"carrier_invoice": read_invoice, "shipper_payment": read_payment}
