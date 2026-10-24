"""Regex redaction (runs before any model sees the text): bank account and routing numbers, cards, SSN, EIN,
emails, phones. Names and addresses are found by model A afterwards and tokenized with the same vault."""
import re
from typing import Any

from harness.ingest.vault import TOKEN_RE, Vault


def _luhn(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if alt:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
        alt = not alt
    return total % 10 == 0


def _aba(digits: str) -> bool:
    d = [int(c) for c in digits]
    return len(d) == 9 and (3 * (d[0] + d[3] + d[6]) + 7 * (d[1] + d[4] + d[7]) + d[2] + d[5] + d[8]) % 10 == 0


# (kind, pattern, group holding the sensitive value, validator). Order matters: earlier kinds win.
PATTERNS: list[tuple[str, re.Pattern, int, Any]] = [
    ("EMAIL", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"), 0, None),
    # 4-4-4-(1..7) or Amex 4-6-5 groupings only, so runs of amounts in prose don't look like a card.
    ("CARD", re.compile(r"(?<![\d-])(?:\d{4}[ -]?\d{4}[ -]?\d{4}[ -]?\d{1,7}|\d{4}[ -]?\d{6}[ -]?\d{5})(?![\d-])"),
     0,
     lambda s: 13 <= len(re.sub(r"\D", "", s)) <= 19 and _luhn(re.sub(r"\D", "", s))),
    ("SSN", re.compile(r"(?<![\d-])(?!000|666|9\d\d)\d{3}-(?!00)\d{2}-(?!0000)\d{4}(?![\d-])"), 0, None),
    ("SSN", re.compile(r"(?i)\b(?:ssn|social security(?: number| no\.?)?)\s*[:#]?\s*(\d{9})\b"), 1, None),
    ("EIN", re.compile(r"(?<![\d-])\d{2}-\d{7}(?![\d-])"), 0, None),
    ("EIN", re.compile(r"(?i)\b(?:ein|tax id|tin|fein)\s*(?:no\.?|number|#)?\s*[:#]?\s*(\d{9})\b"), 1, None),
    ("ROUTING", re.compile(r"(?i)\b(?:routing|aba|rtn|transit)\s*(?:no\.?|number|#)?\s*[:#]?\s*(\d{9})\b"), 1,
     lambda s: _aba(s)),
    ("ACCOUNT", re.compile(r"(?i)\b(?:account|acct|a/c)\.?\s*(?:no\.?|number|#)?\s*[:#]?\s*(\d[\d -]{3,20}\d)\b"),
     1, None),
    # Boundaries reject an adjacent digit or separator+digit, so "Call 415-555-0199." still matches.
    ("PHONE", re.compile(r"(?<!\d)(?<!\d[.-])(?:\+?1[\s.-]?)?(?:\(\d{3}\)\s?|\d{3}[\s.-])\d{3}[\s.-]\d{4}"
                         r"(?!\d|[.-]\d)"),
     0, None),
    ("PHONE", re.compile(r"(?<!\d)\+1\d{10}(?!\d)"), 0, None),
]

# Structured payload keys whose whole value is sensitive regardless of format.
SENSITIVE_KEYS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"(?i)routing|aba"), "ROUTING"),
    (re.compile(r"(?i)iban|account|acct"), "ACCOUNT"),  # also bare `account` / `new_account`
    (re.compile(r"(?i)\bssn\b|social"), "SSN"),
    (re.compile(r"(?i)\bein\b|tax_?id|\btin\b"), "EIN"),
    (re.compile(r"(?i)card_?(no|num|number)|\bpan\b"), "CARD"),
    (re.compile(r"(?i)e_?mail"), "EMAIL"),
    (re.compile(r"(?i)phone|mobile|\bfax\b|\btel\b"), "PHONE"),
]


def redact_text(text: str, vault: Vault) -> tuple[str, list[str]]:
    """Returns (redacted_text, tokens_created)."""
    found: list[str] = []
    for kind, pat, group, valid in PATTERNS:
        def repl(m: re.Match, kind=kind, group=group, valid=valid) -> str:
            value = m.group(group)
            if TOKEN_RE.search(m.group(0)) or (valid and not valid(value)):
                return m.group(0)
            tok = vault.tokenize(kind, value)
            found.append(tok)
            start, end = m.start(group) - m.start(0), m.end(group) - m.start(0)
            whole = m.group(0)
            return whole[:start] + tok + whole[end:]
        text = pat.sub(repl, text)
    return text, found


def _key_kind(key: str) -> str | None:
    return next((kind for pat, kind in SENSITIVE_KEYS if pat.search(key)), None)


def redact_obj(obj: Any, vault: Vault, key: str = "") -> tuple[Any, list[str]]:
    """Recursively redact a JSON payload: strings by pattern, sensitive keys by name."""
    if isinstance(obj, dict):
        out, toks = {}, []
        for k, v in obj.items():
            out[k], t = redact_obj(v, vault, k)
            toks += t
        return out, toks
    if isinstance(obj, list):
        out_l, toks = [], []
        for v in obj:
            r, t = redact_obj(v, vault, key)
            out_l.append(r)
            toks += t
        return out_l, toks
    kind = _key_kind(key) if key else None
    if kind and isinstance(obj, (str, int)) and not isinstance(obj, bool) and str(obj).strip():
        tok = vault.tokenize(kind, str(obj))
        return tok, [tok]
    if isinstance(obj, str):
        return redact_text(obj, vault)
    return obj, []


def replace_spans(obj: Any, spans: dict[str, str]) -> Any:
    """Replace literal substrings (e.g. names/addresses found by model A) with their tokens everywhere."""
    if not spans:
        return obj
    if isinstance(obj, dict):
        return {k: replace_spans(v, spans) for k, v in obj.items()}
    if isinstance(obj, list):
        return [replace_spans(v, spans) for v in obj]
    if isinstance(obj, str):
        for text in sorted(spans, key=len, reverse=True):  # longest first: "Marcus Bell" before "Marcus"
            obj = re.sub(rf"(?<!\w){re.escape(text)}(?!\w)", lambda _m, t=spans[text]: t, obj)
    return obj


def leftovers(obj: Any) -> list[str]:
    """Deterministic backstop: pattern hits that survived redaction (should be empty)."""
    if isinstance(obj, dict):
        return [x for v in obj.values() for x in leftovers(v)]
    if isinstance(obj, list):
        return [x for v in obj for x in leftovers(v)]
    if not isinstance(obj, str):
        return []
    hits = []
    for kind, pat, group, valid in PATTERNS:
        for m in pat.finditer(obj):
            if not TOKEN_RE.search(m.group(0)) and (not valid or valid(m.group(group))):
                hits.append(f"{kind}:{m.group(group)[:4]}…")
    return hits
