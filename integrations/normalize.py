"""Shared normalization for public-record counterparty data."""
import re


def e164(raw: str | None) -> str | None:
    """US/CA 10-digit numbers -> +1XXXXXXXXXX; anything else is returned digits-only with a + prefix."""
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 10:
        return f"+1{digits}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    return f"+{digits}" if digits else None


def email(raw: str | None) -> str | None:
    e = (raw or "").strip().lower()
    return e if re.fullmatch(r"[^@\s]+@[^@\s]+\.[a-z]{2,}", e) else None
