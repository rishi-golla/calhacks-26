"""Seed a labeled 30-day ledger history for dashboard demos, through the real reducers.

Journal requests are filed by the AR/AP service identities and posted by the controller
identity (P-005 two-person rule), then `posted_at` is back-dated with the admin SQL
credential so the cash chart has a history. All ids carry a DEMO-LEDGER prefix and every
memo says "[demo ledger]"; nothing here talks to an external system.
"""
from __future__ import annotations

import argparse
import random
from datetime import UTC, datetime, timedelta

import requests

from scripts.spacetime_admin import LocalAdmin

CASE = "CASE-W2-LEDGER"
DAYS = 30
VERSION = "demo-ledger-v1"


def seed(admin: LocalAdmin, server: str, database: str) -> None:
    sql_url = f"{server.rstrip('/')}/v1/database/{database}/sql"
    headers = {"Authorization": f"Bearer {admin.identity('admin')['token']}", "Content-Type": "text/plain"}

    def write(actor: str, reducer: str, key: str, **payload):
        admin.call(actor, reducer, {"write_id": f"{VERSION}:{reducer}:{key}", **payload})

    def backdate(entry_id: str, at: datetime) -> None:
        stamp = at.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"
        r = requests.post(sql_url, headers=headers, timeout=10,
                          data=f"UPDATE ledger_entry SET posted_at = '{stamp}' WHERE id = '{entry_id}'")
        r.raise_for_status()

    today = datetime.now(UTC).replace(hour=15, minute=0, second=0, microsecond=0)
    start = today - timedelta(days=DAYS)
    for month in sorted({(start + timedelta(days=i)).strftime("%Y-%m") for i in range(DAYS + 1)}):
        write("admin", "set_period", f"period-{month}", period=month, status="open")

    write("ingest", "open_case", "case", case_id=CASE, load_id="LOAD-W2-LEDGER", priority="P3",
          title="[W2 fixture] Ledger history · demo seed", exposure_usd=0)

    rng = random.Random(2026)
    n = 0

    def post(key: str, requester: str, at: datetime, memo: str, lines: list[dict]) -> None:
        nonlocal n
        n += 1
        period = at.strftime("%Y-%m")
        write(requester, "request_journal_entry", key, id=f"JR-DEMO-LEDGER-{key}", case_id=CASE,
              period=period, memo=f"[demo ledger] {memo}", lines=lines)
        write("controller", "post_journal_entry", key, id=f"JE-DEMO-LEDGER-{key}",
              request_id=f"JR-DEMO-LEDGER-{key}")
        backdate(f"JE-DEMO-LEDGER-{key}", at)

    # Opening position 30 days ago: cash, receivables and payables carried in.
    post("open-cash", "shipper_ar", start, "opening cash balance",
         [{"account": "cash", "debit": 362_000, "credit": 0}, {"account": "opening_balance_equity", "debit": 0, "credit": 362_000}])
    post("open-ar", "shipper_ar", start, "opening receivables",
         [{"account": "accounts_receivable", "debit": 388_000, "credit": 0}, {"account": "opening_balance_equity", "debit": 0, "credit": 388_000}])
    post("open-ap", "carrier_ap", start, "opening payables",
         [{"account": "opening_balance_equity", "debit": 204_000, "credit": 0}, {"account": "accounts_payable", "debit": 0, "credit": 204_000}])

    for i in range(1, DAYS + 1):
        day = start + timedelta(days=i)
        weekend = day.weekday() >= 5
        scale = 0.35 if weekend else 1.0
        revenue = round(rng.uniform(48_000, 66_000) * scale, 2)
        expense = round(revenue * rng.uniform(0.80, 0.86), 2)
        collected = round(rng.uniform(44_000, 64_000) * scale, 2)
        paid = round(rng.uniform(36_000, 52_000) * scale, 2)
        d = day.strftime("%Y%m%d")
        post(f"rev-{d}", "shipper_ar", day.replace(hour=9), "freight revenue billed",
             [{"account": "accounts_receivable", "debit": revenue, "credit": 0}, {"account": "freight_revenue", "debit": 0, "credit": revenue}])
        post(f"exp-{d}", "carrier_ap", day.replace(hour=10), "carrier invoices accrued",
             [{"account": "freight_expense", "debit": expense, "credit": 0}, {"account": "accounts_payable", "debit": 0, "credit": expense}])
        post(f"col-{d}", "shipper_ar", day.replace(hour=13), "customer payments received",
             [{"account": "cash", "debit": collected, "credit": 0}, {"account": "accounts_receivable", "debit": 0, "credit": collected}])
        post(f"pay-{d}", "carrier_ap", day.replace(hour=16), "carrier payments released",
             [{"account": "accounts_payable", "debit": paid, "credit": 0}, {"account": "cash", "debit": 0, "credit": paid}])

    for w, (amount, label, conf) in enumerate([
        (41_500, "Collections due · week 1", 0.9), (-312_000, "Carrier payment run", 0.95),
        (58_200, "Collections due · week 3", 0.85), (74_900, "Quarter-end customer remittances", 0.8),
        (-28_400, "Fuel surcharge true-up", 0.7), (12_600, "Detention recoveries", 0.6)], start=1):
        write("cfo", "upsert_forecast", f"forecast-{w}", id=f"FORECAST-DEMO-LEDGER-{w}", case_id=CASE,
              period=(today + timedelta(weeks=w)).strftime("%Y-%m"), category=f"[demo ledger] {label}",
              amount=amount, confidence=conf, assumptions_json={"demo_ledger": True, "week": w})
    print(f"Posted {n} labeled journal entries over {DAYS} days plus 6 forecast buckets on {CASE}.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", default="http://127.0.0.1:3000")
    parser.add_argument("--database", default="freightops")
    args = parser.parse_args()
    admin = LocalAdmin(args.server, args.database)
    try:
        seed(admin, args.server, args.database)
    finally:
        admin.client.close()


if __name__ == "__main__":
    main()
