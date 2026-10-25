"""Intake memory on the ASUS box (local SQLite, harness/data/memory.sqlite). Two things it remembers:

1. Invoice fingerprints for 14 days (PRD P-007: same carrier, load and amount within 14 days is a duplicate):
   carrier + invoice number, carrier + BOL + amount (catches renumbered re-sends), carrier + load + amount.
2. Each carrier's identity, checked against the verified directory (W4's source export, SOURCE_DATA_PATH): legal name
   vs the invoice payee (factoring notice or diversion), USDOT vs the invoice, registered email domain vs the sender
   (lookalike transp0rt.com vs transport.com is called out). Against the directory these checks are exact on the
   recorded sim run. Carriers missing from the directory fall back to first-seen values from this memory, which a
   fraudster can't overwrite by sending a new email; a verified change is recorded deliberately (`set_identity`).

Only non-sensitive values are stored: IDs, amounts, company domains and names. Personal data stays in the vault."""
import json
import os
import re
import sqlite3
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

from harness.ingest.vault import DATA_DIR

WINDOW_S = 14 * 24 * 3600


def _norm(value) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(value).upper()) if value not in (None, "") else ""


def _cents(value) -> str:
    return str(round(float(value) * 100)) if isinstance(value, (int, float)) else ""


def event_time(event: dict, payload: dict) -> float:
    """Business time for the 14-day window: simulated time when present (the sim fast-forwards), else wall time."""
    for v in (event.get("sim_ts"), payload.get("invoice_date"), payload.get("date"), event.get("ts")):
        if isinstance(v, str):
            try:
                return datetime.fromisoformat(v).astimezone(UTC).timestamp()
            except ValueError:
                continue
    return time.time()


def carrier_of(payload: dict) -> str:
    carrier = payload.get("carrier")
    return payload.get("carrier_id") or (carrier.get("id") if isinstance(carrier, dict) else "") or ""


def domain_of(email: str | None) -> str:
    m = re.search(r"@([A-Za-z0-9.-]+\.[A-Za-z]{2,})", email or "")
    return m.group(1).lower() if m else ""


def _distance(a: str, b: str) -> int:
    """Levenshtein distance (short strings only)."""
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def fingerprints(payload: dict) -> list[tuple[str, str]]:
    """(rule, fingerprint) pairs for a carrier invoice; empty when there is too little to compare."""
    carrier = _norm(carrier_of(payload))
    if not carrier:
        return []
    out = []
    if _norm(payload.get("invoice_number")):
        out.append(("same invoice number", f"inv|{carrier}|{_norm(payload['invoice_number'])}"))
    amount = _cents(payload.get("invoice_amount"))
    if amount and _norm(payload.get("bol")):
        out.append(("same BOL and amount", f"bol|{carrier}|{_norm(payload['bol'])}|{amount}"))
    if amount and _norm(payload.get("load_id")):
        out.append(("same load and amount", f"load|{carrier}|{_norm(payload['load_id'])}|{amount}"))
    return out


class Directory:
    """Verified carrier directory from the source export; reloaded when the file changes."""

    def __init__(self, path: str | None = None) -> None:
        self.path = path or os.getenv("SOURCE_DATA_PATH") or ""
        self._mtime = -1.0
        self._companies: dict = {}

    def get(self, carrier_id: str) -> dict | None:
        try:
            mtime = os.path.getmtime(self.path) if self.path else -1.0
        except OSError:
            return None
        if mtime != self._mtime:
            try:
                self._companies = json.loads(Path(self.path).read_text()).get("companies", {})
            except (OSError, ValueError):
                self._companies = {}
            self._mtime = mtime
        company = self._companies.get(carrier_id)
        return company if isinstance(company, dict) else None


class Memory:
    def __init__(self, path=None, directory: Directory | None = None) -> None:
        self.directory = directory or Directory()
        path = path or DATA_DIR / "memory.sqlite"
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.Lock()
        self._db.executescript(
            "CREATE TABLE IF NOT EXISTS fingerprint(fp TEXT, event_id TEXT, case_id TEXT, at REAL,"
            " PRIMARY KEY(fp, event_id));"
            "CREATE TABLE IF NOT EXISTS identity(carrier_id TEXT, attr TEXT, value TEXT, first_event TEXT, at REAL,"
            " PRIMARY KEY(carrier_id, attr));")
        self._db.commit()

    # ---- duplicates (P-007) ----
    def find_duplicate(self, event_id: str, payload: dict, at: float) -> dict | None:
        """The earliest other invoice within 14 days that shares a fingerprint, or None."""
        for rule, fp in fingerprints(payload):
            row = self._db.execute(
                "SELECT event_id, case_id, at FROM fingerprint WHERE fp=? AND event_id<>? AND abs(at-?)<=?"
                " ORDER BY at LIMIT 1", (fp, event_id, at, WINDOW_S)).fetchone()
            if row:
                return {"event_id": row[0], "case_id": row[1], "rule": rule,
                        "days_apart": round(abs(at - row[2]) / 86400, 1)}
        return None

    def remember_invoice(self, event_id: str, case_id: str, payload: dict, at: float) -> None:
        with self._lock:
            self._db.executemany("INSERT OR IGNORE INTO fingerprint VALUES(?,?,?,?)",
                                 [(fp, event_id, case_id, at) for _rule, fp in fingerprints(payload)])
            self._db.commit()

    # ---- carrier identity ----
    def identity_signals(self, event_type: str, payload: dict) -> list[str]:
        carrier = carrier_of(payload)
        if not carrier:
            return []
        notes = []
        verified = self.directory.get(carrier)
        if verified:
            source = "directory"
            names = {_norm(verified.get("legal_name")), _norm(verified.get("name"))} - {""}
            known = {"sender_domain": domain_of(verified.get("email")), "usdot": _norm(verified.get("usdot")),
                     "payee": next(iter(names), "")}
        else:
            source = "first seen"
            names = set()
            known = dict(self._db.execute("SELECT attr, value FROM identity WHERE carrier_id=?", (carrier,)).fetchall())
        for attr, value in self._observed(payload).items():
            base = known.get(attr)
            if not base or not value or value == base or (attr == "payee" and value in names):
                continue
            if attr == "sender_domain":
                kind = "lookalike" if _distance(value, base) <= 2 else "different"
                notes.append(f"identity: {kind} sender domain for {carrier} ({value}; {source}: {base})")
            elif attr == "payee":
                notes.append(f"identity: payee for {carrier} is '{value}' ({source}: '{base}'): factoring notice "
                             "or payment diversion?")
            elif attr == "usdot":
                notes.append(f"identity: USDOT on invoice for {carrier} is {value} ({source}: {base})")
        if event_type == "bank_change.requested" and payload.get("sender_matches_registered") is False:
            notes.append(f"identity: bank-change request for {carrier} not from the registered contact")
        return notes

    def remember_identity(self, event_type: str, event_id: str, payload: dict, at: float) -> None:
        """Record first-seen values only, and never from a bank-change request (that is the claim being tested)."""
        carrier = carrier_of(payload)
        if not carrier or event_type == "bank_change.requested":
            return
        with self._lock:
            self._db.executemany("INSERT OR IGNORE INTO identity VALUES(?,?,?,?,?)",
                                 [(carrier, a, v, event_id, at) for a, v in self._observed(payload).items() if v])
            self._db.commit()

    def set_identity(self, carrier_id: str, attr: str, value: str, source_ref: str) -> None:
        """Deliberate update after out-of-band verification (e.g. a verified bank or payee change)."""
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO identity VALUES(?,?,?,?,?)",
                             (carrier_id, attr, value, source_ref, time.time()))
            self._db.commit()

    @staticmethod
    def _observed(payload: dict) -> dict[str, str]:
        remit = payload.get("remit_to")
        return {"sender_domain": domain_of(payload.get("from") or payload.get("source_email")),
                "payee": _norm(remit.get("name")) if isinstance(remit, dict) else "",
                "usdot": _norm(payload.get("invoice_carrier_usdot"))}
