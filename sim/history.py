"""Golden Gate Freight's books from before FreightLine ran: months of resolved loads, as imported history.

Pure and deterministic. The same world, seed, end date, day count and load count always produce the same
cases, invoices, journals and source records, so rerunning the seed is a no-op. Nothing here calls a model
or an agent, and no agent work (task, dispatch, delegation, review) is ever produced: an imported case is
resolved history with its paperwork and its ledger, nothing more.

Every imported row is marked. Case and journal ids start with HIST- (`HIST-LD-0915-3FA2C1`,
`HIST-JE-LD-0915-3FA2C1-BILL`), invoices and source records belong to a HIST- case, and every source
record says `"imported": true` with the import's source tag. The dashboard keeps these rows visible
through "Start fresh", which only hides earlier live activity.

Dates are real wall-clock time (the last `days` days up to `end`, normally yesterday), never the
simulator's business clock. Accounts are the names the module, the agents (PR #49) and the dashboard
use; the codes are sim/world.py's chart of accounts:

    cash                    1000 Operating Cash
    accounts_receivable     1200 Accounts Receivable
    accounts_payable        2000 Accounts Payable
    freight_revenue         4000 Freight Revenue
    accessorial_revenue     4100 Accessorial Revenue
    freight_expense         5000 Purchased Transportation
    accessorial_expense     5100 Accessorial Expense
    bad_debt_expense        6900 Bad Debt / Short-pay Write-off
    opening_balance_equity  (equity; outside the sim's operating chart)

Per load: `Customer billing` (Dr AR / Cr freight revenue, plus accessorial revenue) when the shipper
invoice goes out, `Carrier settlement` (Dr purchased transportation / Cr AP) when the carrier invoice is
approved, `Matched source deposit` (Dr cash / Cr AR) when the shipper pays, `Carrier payment` (Dr AP /
Cr cash) when the carrier is paid, and `Short-pay write-off` (Dr bad debt / Cr AR) when a deduction is
written off. One opening entry funds cash so the balance never goes negative.
"""
from __future__ import annotations

import hashlib
import json
import math
import random
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta

from ingest import mirror, source_export
from sim import generator

PREFIX = "HIST-"
MANIFEST_ID = "dataset:history"
VERSION = "history-v1"
DEFAULT_SOURCE = "golden-gate-books"
DEFAULT_SEED = 7
DEFAULT_DAYS = 90
REQUESTER = "import:history"
ACCOUNT_CODES = {
    "cash": "1000", "accounts_receivable": "1200", "accounts_payable": "2000", "freight_revenue": "4000",
    "accessorial_revenue": "4100", "freight_expense": "5000", "accessorial_expense": "5100",
    "bad_debt_expense": "6900", "opening_balance_equity": "",
}
# The dashboard's account aliases (web/lib/ops.ts ACCOUNT_ALIASES), so dry-run totals match the tiles.
KPI_ALIASES = {
    "cash": "cash", "bank": "cash", "accounts_receivable": "ar", "ar": "ar", "receivables": "ar",
    "accounts_payable": "ap", "ap": "ap", "payables": "ap", "freight_revenue": "revenue", "revenue": "revenue",
    "sales": "revenue", "accessorial_revenue": "revenue", "freight_expense": "expense",
    "carrier_expense": "expense", "expense": "expense", "cost_of_revenue": "expense",
    "accessorial_expense": "expense", "purchased_transportation": "expense",
}
LOADS_PER_BUSINESS_DAY = (5, 8)
# Business hours in UTC (7:00-16:30 Pacific), so no event lands after the end day's last minute.
DAY_OPEN, LAST_EVENT = time(14, 0), time(23, 30)
SHIPPER_TERMS_DAYS = {"Net 15": (14, 30), "Net 30": (25, 45), "Net 45": (38, 58)}


def stamp(at: datetime) -> str:
    """The module's timestamp form (`Date.toISOString()`), so stored values equal what was sent."""
    return at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def record_stamp(at: datetime) -> str:
    """Source-record timestamps in ingest's export form."""
    return at.astimezone(UTC).isoformat()


def cents(value: float) -> int:
    return round(value * 100)


def usd(value_cents: int) -> float:
    return round(value_cents / 100, 2)


def journal(entry_id: str, case_id: str, at: datetime, memo: str, debits: list[tuple[str, int]],
            credits: list[tuple[str, int]]) -> dict:
    lines = [{"account": a, "debit": usd(c), "credit": 0} for a, c in debits if c] + \
            [{"account": a, "debit": 0, "credit": usd(c)} for a, c in credits if c]
    if sum(cents(x["debit"]) for x in lines) != sum(cents(x["credit"]) for x in lines):
        raise ValueError(f"{entry_id} is not balanced")
    return {"id": entry_id, "case_id": case_id, "period": stamp(at)[:7], "posted_at": stamp(at), "memo": memo,
            "lines": lines}


@dataclass
class Bundle:
    """Everything one load contributes, kept together so a batch never splits a case from its rows."""
    load_id: str
    companies: list[str]
    cases: list[dict] = field(default_factory=list)
    invoices: list[dict] = field(default_factory=list)
    ledger_entries: list[dict] = field(default_factory=list)
    source_records: list[dict] = field(default_factory=list)
    documents: list[tuple[str, str, dict]] = field(default_factory=list)


@dataclass
class History:
    params: dict
    companies: dict[str, dict]
    opening: dict
    bundles: list[Bundle]
    manifest: dict
    end_at: datetime

    @property
    def cases(self) -> list[dict]:
        return [c for b in self.bundles for c in b.cases]

    @property
    def invoices(self) -> list[dict]:
        return [i for b in self.bundles for i in b.invoices]

    @property
    def ledger_entries(self) -> list[dict]:
        return [self.opening, *(e for b in self.bundles for e in b.ledger_entries)]

    @property
    def source_records(self) -> list[dict]:
        return [*(r for b in self.bundles for r in b.source_records), self.manifest]

    @property
    def documents(self) -> list[tuple[str, str, dict]]:
        return [d for b in self.bundles for d in b.documents]


def default_end(today: date | None = None) -> date:
    return (today or datetime.now(UTC).date()) - timedelta(days=1)


def loads_per_day(days: list[date], rng: random.Random, total: int | None) -> list[int]:
    """5-8 loads a business day, the odd weekend load. A fixed `total` is spread with the same weights."""
    weights = [rng.uniform(0.8, 1.2) if d.weekday() < 5 else (0.15 if rng.random() < 0.3 else 0.0) for d in days]
    if total is None:
        low, high = LOADS_PER_BUSINESS_DAY
        return [rng.randint(low, high) if d.weekday() < 5 else int(w > 0) for d, w in zip(days, weights, strict=True)]
    scale = sum(weights)
    counts = [int(total * w / scale) for w in weights]
    for i in sorted(range(len(days)), key=lambda i: -(total * weights[i] / scale - counts[i]))[: total - sum(counts)]:
        counts[i] += 1
    return counts


def _at(day: date, rng: random.Random, start: time, end: time) -> datetime:
    lo = datetime.combine(day, start, UTC)
    span = (datetime.combine(day, end, UTC) - lo).total_seconds() / 60
    return lo + timedelta(minutes=int(rng.uniform(0, span)))


def _minute(at: datetime) -> datetime:
    return at.replace(second=0, microsecond=0)


def _later(at: datetime, rng: random.Random, low_h: float, high_h: float) -> datetime:
    """`at` plus a random delay of `low_h` to `high_h` hours, to the minute."""
    return _minute(at + timedelta(hours=rng.uniform(low_h, high_h)))


def _business_time(at: datetime, rng: random.Random) -> datetime:
    """Payments clear during banking hours on a weekday."""
    day = at.date()
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return _at(day, rng, time(16, 0), time(22, 0))


def _world_record(world: dict, company_id: str) -> dict:
    return next(x for x in world["carriers"] + world["shippers"] if x["id"] == company_id)


def generate(world: dict, *, seed: int = DEFAULT_SEED, end: date | None = None, days: int = DEFAULT_DAYS,
             loads: int | None = None, source: str = DEFAULT_SOURCE) -> History:
    if not world.get("lanes"):
        raise ValueError("the world has no lanes; build data/world.json first (make world)")
    if days < 14:
        raise ValueError("history needs at least 14 days")
    end = end or default_end()
    end_at = datetime.combine(end, LAST_EVENT, UTC)
    first = end - timedelta(days=days - 1)
    calendar = [first + timedelta(days=i) for i in range(days)]
    rng = random.Random(f"{VERSION}:{seed}")
    counts = loads_per_day(calendar, rng, loads)
    fsc_rate = round(float(world["fsc_per_mile"]), 3)
    brokerage = world.get("brokerage")

    # Exceptions are drawn up front from loads old enough to have played out by `end`.
    plans: list[dict] = []
    used: set[str] = set()
    for day, n in zip(calendar, counts, strict=True):
        for _ in range(n):
            while (load_id := f"LD-{day:%m%d}-{rng.getrandbits(24):06X}") in used:
                pass
            used.add(load_id)
            lane = dict(rng.choice(world["lanes"]))
            plans.append({"day": day, "load_id": load_id, "lane": lane, "scenario": "clean", "slow": False})
    total = len(plans)
    index_by_age = {i: (end - p["day"]).days for i, p in enumerate(plans)}

    def pick(count: int, min_age: int, max_age: int = 10_000) -> list[int]:
        pool = [i for i, age in index_by_age.items() if min_age <= age <= max_age and plans[i]["scenario"] == "clean"
                and not plans[i]["slow"]]
        return sorted(rng.sample(pool, min(count, len(pool))))

    for i in pick(2 if total < 450 else 3, 40):
        plans[i]["scenario"] = "detention_unsupported"      # accessorial dispute, carrier short-paid
    for i in pick(max(3, round(total * 0.012)), 75):
        plans[i]["scenario"] = "shipper_short_pay"          # deduction collected on, then written off
    for i in pick(max(2, round(total * 0.004)), 61, 85):
        plans[i]["slow"] = True                             # still unpaid past 60 days
    for i in pick(max(3, round(total * 0.01)), 33, 58):
        plans[i]["slow"] = True                             # still unpaid past 30 days
    for i in pick(round(total * 0.03), 3):
        plans[i]["scenario"] = "detention_supported"        # passed through to the shipper

    companies: dict[str, dict] = {}
    bundles: list[Bundle] = []
    for plan in plans:
        # A load whose paperwork would arrive after `end` is still live work, not history: it is left out
        # rather than squeezed into the window, so the books do not close everything at the last minute.
        if bundle := _load(world, plan, seed, fsc_rate, brokerage, source, end_at, companies):
            bundles.append(bundle)

    opening = _opening(bundles, first, end_at)
    history = History(params={"seed": seed, "end": end.isoformat(), "days": days, "loads": loads,
                              "source": source, "version": VERSION},
                      companies=companies, opening=opening, bundles=bundles, manifest={}, end_at=end_at)
    history.manifest = _manifest(history, world)
    return history


def _load(world: dict, plan: dict, seed: int, fsc_rate: float, brokerage: str | None, source: str,
          end_at: datetime, companies: dict[str, dict]) -> Bundle | None:
    load_id, day, lane, scenario = plan["load_id"], plan["day"], plan["lane"], plan["scenario"]
    rnd = random.Random(f"{VERSION}:{seed}:{load_id}")
    lane["fsc"] = round(lane["miles"] * fsc_rate, 2)
    lane["contract_rate"] = round(lane["linehaul"] + lane["fsc"], 2)
    lane["shipper_fsc"] = lane["fsc"]
    load = generator.build_load(world, load_id, lane, fsc_rate, rnd)
    carrier, shipper, consignee = load["carrier"], load["shipper"], load["consignee"]
    quickpay = str(carrier.get("terms") or "").startswith("QuickPay")
    planned = generator.plan(load, "decoy_quickpay" if quickpay and scenario == "clean" else scenario, rnd)
    case_id = f"{PREFIX}{load_id}"
    for company_id, kind in ((carrier["id"], "carrier"), (shipper["id"], "shipper"), (consignee["id"], "shipper")):
        if company_id not in companies:
            companies[company_id] = mirror.company_payload(source_export._company(_world_record(world, company_id)), kind)

    # Physical timeline, anchored on the delivery day.
    depart = _at(day, rnd, time(15, 0), time(21, 0))
    dwell = planned["dwell"]
    arrive = depart - timedelta(minutes=dwell)
    loaded = _minute(arrive - timedelta(hours=lane["transit_hours"]))
    pickup = _minute(loaded - timedelta(minutes=rnd.randint(40, 110)))
    tendered = _minute(pickup - timedelta(hours=rnd.uniform(4, 9)))
    billed_at = _later(depart, rnd, 1, 20)          # carrier invoice arrives: the case opens
    sinv_at = _later(depart, rnd, 4, 44)            # POD checked, shipper invoiced: revenue is billed
    disputed = scenario == "detention_unsupported"
    resolved_at = _later(billed_at, rnd, 50, 140) if disputed else (
        _later(billed_at, rnd, 4, 30) if scenario == "detention_supported" else _later(billed_at, rnd, 1.5, 26))
    if max(billed_at, sinv_at, resolved_at) > end_at:
        return None

    # Carrier side: the invoice as billed, and what the brokerage owes after review.
    lines, billed_total = planned["lines"], planned["stated_total"]
    owed = cents(load["contract_rate"]) if disputed else cents(billed_total)
    accessorial_cost = sum(cents(x["amount"]) for x in lines if x["desc"].startswith("Detention")) if not disputed else 0
    number = generator.docs.invoice_number(carrier, rnd.randrange(1, 1_000_000_000))
    invoice_id = f"INV-{carrier['usdot']}-{number}"
    pay_at = _business_time(resolved_at + timedelta(days=rnd.uniform(1, 2)), rnd) if quickpay else \
        _business_time(billed_at + timedelta(days=rnd.uniform(28, 32)), rnd)
    pay_at = max(pay_at, resolved_at + timedelta(minutes=30))
    carrier_paid = pay_at <= end_at

    # Shipper side: what was billed, what arrived and when.
    sh_lines = [{"desc": f"Linehaul {lane['origin']} -> {lane['destination']} ({lane['miles']} mi)", "qty": 1,
                 "rate": lane["shipper_linehaul"], "amount": lane["shipper_linehaul"]},
                {"desc": "Fuel surcharge", "qty": lane["miles"], "rate": fsc_rate, "amount": lane["shipper_fsc"]}]
    if scenario == "detention_supported":
        rate = lane["detention_rate_hr"] * 1.25
        sh_lines.append({"desc": f"Detention {planned['claimed_h']}h", "qty": planned["claimed_h"], "rate": rate,
                         "amount": round(planned["claimed_h"] * rate, 2)})
    sh_total = sum(cents(x["amount"]) for x in sh_lines)
    accessorial_revenue = sum(cents(x["amount"]) for x in sh_lines if x["desc"].startswith("Detention"))
    sinv = f"SINV-{load_id}"
    low, high = SHIPPER_TERMS_DAYS.get(str(shipper.get("terms") or "Net 30"), SHIPPER_TERMS_DAYS["Net 30"])
    receipt_at = _business_time(sinv_at + timedelta(days=rnd.uniform(low, high)), rnd)
    short = cents(rnd.choice([150, 225, 300, 450])) if scenario == "shipper_short_pay" else 0
    received = sh_total - short
    shipper_paid = receipt_at <= end_at and not plan["slow"]
    deposit_id = f"BNK-{hashlib.sha256(f'{seed}:{load_id}:deposit'.encode()).hexdigest()[:10].upper()}"
    write_off_at = _later(receipt_at, rnd, 24 * 10, 24 * 25) if short else None
    written_off = write_off_at is not None and shipper_paid and write_off_at <= end_at

    # Paperwork: refs follow the simulator's naming, so the case file and evidence viewer find them.
    tms = {"case_id": case_id, "load_id": load_id, "lane": f"{lane['origin']} -> {lane['destination']}",
           "pro": load["pro"], "bol": load["bol"], "po": load["po"], "equipment": lane["equipment"],
           "commodity": lane["commodity"], "weight_lb": load["weight_lb"], "pallets": load["pallets"],
           "miles": lane["miles"]}
    ts = generator._ts
    documents = [
        (f"RC-{load_id}", "rate_confirmation", {
            **tms, "date": ts(tendered), "carrier": carrier, "shipper": shipper, "consignee": consignee,
            "pickup_appt": ts(pickup), "delivery_appt": ts(arrive.replace(minute=0)), "linehaul": lane["linehaul"],
            "fsc": lane["fsc"], "fsc_per_mile": fsc_rate, "total": load["contract_rate"],
            "accessorials": lane["accessorials"], "free_time_min": load["free_time_min"],
            "detention_rate_hr": lane["detention_rate_hr"], "terms": carrier.get("terms", "Net 30"),
            "temp_f": lane.get("temp_f")}),
        (f"BOL-{load_id}", "bill_of_lading", {
            **tms, "ship_date": ts(loaded), "seal": load["seal"], "trailer": load["trailer"], "carrier": carrier,
            "shipper": shipper, "consignee": consignee, "nmfc_class": lane["nmfc_class"],
            "loader": generator._person(rnd), "loaded_at": ts(loaded), "driver": load["driver"]}),
        (f"POD-{load_id}", "proof_of_delivery", {
            **tms, "carrier": carrier, "consignee": consignee, "arrived": ts(arrive), "departed": ts(depart),
            "pallets_received": load["pallets"], "receiver": generator._person(rnd)}),
    ]
    if scenario == "detention_supported":
        documents.append((f"DET-{load_id}", "detention_log", {
            "load_id": load_id, "bol": load["bol"], "consignee": consignee, "appt": ts(arrive.replace(minute=0)),
            "arrived": ts(arrive), "departed": ts(depart), "dwell_min": dwell, "clerk": generator._person(rnd)}))
    remit = {"name": carrier["name"], "address": carrier.get("address"),
             "routing_last4": f"{rnd.randint(0, 9999):04d}", "account_last4": f"{rnd.randint(0, 9999):04d}"}
    documents.append((f"INV-{load_id}-1", "carrier_invoice", {
        **tms, "invoice_number": number, "date": ts(billed_at), "terms": carrier.get("terms", "Net 30"),
        "due": ts(billed_at + timedelta(days=30)), "carrier": carrier, "remit_to": remit, "line_items": lines,
        "total": billed_total, "origin": lane["origin"], "destination": lane["destination"]}))
    documents.append((sinv, "shipper_invoice", {
        **tms, "invoice_id": sinv, "date": ts(sinv_at), "terms": shipper.get("terms", "Net 30"),
        "due": ts(sinv_at + timedelta(days=30)), "shipper": shipper, "line_items": sh_lines, "total": usd(sh_total)}))
    kinds = {"proof_of_delivery": "pod"}
    listed = [{"kind": kinds.get(kind, kind), "load_id": load_id, "ref": f"doc:{ref}",
               **({"signed": True} if kind == "proof_of_delivery" else {})}
              for ref, kind, _ in documents if kind != "shipper_invoice"]

    # The source record, in ingest's export shape (ingest/source_export.py), marked as imported.
    deposits = [{"amount": usd(received), "id": deposit_id, "invoice_id": sinv, "posted_at": record_stamp(receipt_at),
                 "shipper_id": shipper["id"]}] if shipper_paid else []
    line_items = source_export._line_items(lines, listed)
    claim = next((x["kind"] for x in line_items if x["kind"] in ("detention", "lumper")), None)
    facts = {
        "load_id": load_id, "lane": tms["lane"], "pro": load["pro"], "bol": load["bol"], "po": load["po"],
        "brokerage": brokerage, "carrier": source_export._party(carrier, "usdot", "mc"),
        "shipper": source_export._party(shipper, "cik"), "carrier_id": carrier["id"], "shipper_id": shipper["id"],
        "contract_rate": load["contract_rate"], "free_time_min": load["free_time_min"], "gps_dwell_min": dwell,
        "gps": {"delivery_dwell_min": dwell, "free_time_min": load["free_time_min"]},
        "required_documents": source_export.REQUIRED_DOCUMENTS, "documents": listed,
        "invoice_id": invoice_id, "invoice_number": number, "carrier_identity_mismatch": False,
        "resubmission_refs": [], "invoice_amount": billed_total, "line_items": line_items,
        "issued_at": record_stamp(billed_at), "period": stamp(billed_at)[:7], "claim_type": claim,
        "detention_hours_claimed": planned["claimed_h"], "receipt_attached": planned["receipt"],
        "pod_signed": True, "remit_to": {"name": carrier["name"]}, "invoice_carrier_usdot": carrier["usdot"],
        "ingest_flags": [], "previous_invoices": [], "shipper_invoice_id": sinv,
        "shipper_invoice_amount": usd(sh_total), "deposits": deposits,
        "deduction_reason": "rate dispute" if short and shipper_paid else None,
        "deduction_written_off": usd(short) if written_off else None,
        "imported": True, "import_source": source,
        "settlement": {"approved_amount": usd(owed), "carrier_paid_at": record_stamp(pay_at) if carrier_paid else None,
                       "disputed_amount": usd(cents(billed_total) - owed) if disputed else 0},
    }
    doc_refs = [d["ref"] for d in listed]
    record = source_export._finish(case_id, facts, doc_refs)
    bundle = Bundle(load_id=load_id, companies=[carrier["id"], shipper["id"], consignee["id"]], documents=documents)
    bundle.cases.append({"case_id": case_id, "load_id": load_id, "title": mirror.title(case_id, record),
                         "priority": mirror.priority(case_id, record), "exposure_usd": mirror.exposure(case_id, record),
                         "opened_at": stamp(billed_at), "resolved_at": stamp(resolved_at)})
    bundle.invoices.append({"id": invoice_id, "case_id": case_id, "carrier_id": carrier["id"], "load_id": load_id,
                            "amount": billed_total, "contract_amount": load["contract_rate"],
                            "paid_amount": usd(owed) if carrier_paid else 0, "issued_at": stamp(billed_at),
                            "status": "paid" if carrier_paid else "open"})
    bundle.source_records.append({**mirror.source_record(f"case:{case_id}", record), "updated_at": stamp(resolved_at)})

    ar_case = case_id
    if written_off:
        # The deduction gets its own receivable case, as ingest opens CASE-<load>-AR for a short pay.
        ar_case = f"{case_id}-AR"
        ar_record = source_export._finish(ar_case, {**facts, "period": stamp(sinv_at)[:7]}, [f"doc:{sinv}", *doc_refs])
        bundle.cases.append({"case_id": ar_case, "load_id": load_id, "title": mirror.title(ar_case, ar_record),
                             "priority": mirror.priority(ar_case, ar_record),
                             "exposure_usd": mirror.exposure(ar_case, ar_record),
                             "opened_at": stamp(receipt_at), "resolved_at": stamp(write_off_at)})
        bundle.source_records.append({**mirror.source_record(f"case:{ar_case}", ar_record),
                                      "updated_at": stamp(write_off_at)})

    je = f"{PREFIX}JE-{load_id}"
    entries = bundle.ledger_entries
    entries.append(journal(f"{je}-BILL", case_id, sinv_at, f"Customer billing: {sinv}",
                           [("accounts_receivable", sh_total)],
                           [("freight_revenue", sh_total - accessorial_revenue), ("accessorial_revenue", accessorial_revenue)]))
    entries.append(journal(f"{je}-COST", case_id, resolved_at, f"Carrier settlement: {invoice_id}",
                           [("freight_expense", owed - accessorial_cost), ("accessorial_expense", accessorial_cost)],
                           [("accounts_payable", owed)]))
    if shipper_paid:
        entries.append(journal(f"{je}-RCPT", ar_case, receipt_at, f"Matched source deposit: {deposit_id}",
                               [("cash", received)], [("accounts_receivable", received)]))
    if carrier_paid:
        entries.append(journal(f"{je}-PAY", case_id, pay_at, f"Carrier payment: {invoice_id}",
                               [("accounts_payable", owed)], [("cash", owed)]))
    if written_off:
        entries.append(journal(f"{je}-WO", ar_case, write_off_at, f"Short-pay write-off: {sinv}",
                               [("bad_debt_expense", short)], [("accounts_receivable", short)]))
    return bundle


def _opening(bundles: list[Bundle], first: date, end_at: datetime) -> dict:
    """Opening cash a day before the window, sized so the running balance never dips below $150k."""
    running, low = 0, 0
    for entry in sorted((e for b in bundles for e in b.ledger_entries), key=lambda e: (e["posted_at"], e["id"])):
        running += sum(cents(x["debit"]) - cents(x["credit"]) for x in entry["lines"] if x["account"] == "cash")
        low = min(low, running)
    amount = max(250_000_00, math.ceil((150_000_00 - low) / 25_000_00) * 25_000_00)
    at = datetime.combine(first - timedelta(days=1), time(15, 0), UTC)
    return journal(f"{PREFIX}JE-OPENING", "", at, "Opening balance", [("cash", amount)],
                   [("opening_balance_equity", amount)])


def kpis(entries: list[dict], now: datetime) -> dict:
    """The Finance tiles over these journals, computed as web/lib/ops.ts buildFinance does (no epoch)."""
    totals = {"cash": 0, "ar": 0, "ap": 0, "revenue": 0, "expense": 0}
    revenue_mtd = revenue_30d = expense_30d = 0
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    window = now - timedelta(days=30)
    low_cash: int | None = None
    running = 0
    for entry in sorted(entries, key=lambda e: e["posted_at"]):
        at = datetime.fromisoformat(entry["posted_at"])
        if at > now:
            continue
        for line in entry["lines"]:
            account = KPI_ALIASES.get(str(line["account"]).lower())
            if not account:
                continue
            delta = cents(line["debit"]) - cents(line["credit"])
            delta = delta if account in ("cash", "ar", "expense") else -delta
            totals[account] += delta
            if account == "cash":
                running += delta
            if account == "revenue" and at >= window:
                revenue_30d += delta
            if account == "expense" and at >= window:
                expense_30d += delta
            if account == "revenue" and at >= month_start:
                revenue_mtd += delta
        low_cash = running if low_cash is None else min(low_cash, running)
    return {"cash": usd(totals["cash"]), "ar": usd(totals["ar"]), "ap": usd(totals["ap"]),
            "revenue_mtd": usd(revenue_mtd), "revenue_30d": usd(revenue_30d), "expense_30d": usd(expense_30d),
            "gross_margin_pct": round((revenue_30d - expense_30d) / revenue_30d * 100, 1) if revenue_30d else 0.0,
            "lowest_cash": usd(low_cash or 0)}


def _manifest(history: History, world: dict) -> dict:
    world_digest = hashlib.sha256(json.dumps(world.get("lanes", []), sort_keys=True).encode()).hexdigest()
    manifest = {
        "kind": "imported_history", **history.params, "brokerage": world.get("brokerage"),
        "world_lanes_digest": world_digest, "imported": True, "id_prefix": PREFIX,
        "counts": {"cases": len(history.cases), "invoices": len(history.invoices),
                   "ledger_entries": len(history.ledger_entries), "companies": len(history.companies),
                   "documents": len(history.documents)},
        "totals_at_end": kpis(history.ledger_entries, history.end_at),
        "accounts": ACCOUNT_CODES,
    }
    record_json = json.dumps(manifest, sort_keys=True, separators=(",", ":"))
    return {"id": MANIFEST_ID, "kind": "dataset", "record_json": record_json,
            "digest": hashlib.sha256(record_json.encode()).hexdigest()}


def manifest_digest(history: History) -> str:
    return history.manifest["digest"]


def batches(history: History, max_bytes: int = 200_000) -> list[dict]:
    """import_history_batch payloads, oldest loads first, each under `max_bytes` of JSON. A load's rows never
    split across batches; a company travels with the first load that names it; the opening entry rides in the
    first batch and the manifest in the last. Write ids depend on the history and the batch size only, so a
    rerun with the same parameters replays the same ids."""
    digest = manifest_digest(history)[:16]
    keys = ("companies", "cases", "invoices", "ledger_entries", "source_records")
    out: list[dict] = []
    seen: set[str] = set()

    def merge(*parts: dict) -> dict:
        return {"source": history.params["source"], **{key: [row for part in parts for row in part.get(key, [])]
                                                       for key in keys}}

    def size(batch: dict) -> int:
        return len(json.dumps(batch, separators=(",", ":")))

    current = merge({"ledger_entries": [history.opening]})
    for bundle in history.bundles:
        part = {key: list(getattr(bundle, key)) for key in keys[1:]}
        part["companies"] = [history.companies[c] for c in dict.fromkeys(bundle.companies) if c not in seen]
        seen.update(c["id"] for c in part["companies"])
        candidate = merge(current, part)
        if current["cases"] and size(candidate) > max_bytes:
            out.append(current)
            candidate = merge(part)
        if size(candidate) > max_bytes:
            raise ValueError(f"load {bundle.load_id} alone exceeds {max_bytes} bytes; raise --max-batch-bytes")
        current = candidate
    last = merge(current, {"source_records": [history.manifest]})
    if size(last) > max_bytes:
        out.append(current)
        last = merge({"source_records": [history.manifest]})
    out.append(last)
    for index, batch in enumerate(out):
        batch["write_id"] = f"history:{digest}:{max_bytes}:{index:03d}"
        for key in [k for k, v in batch.items() if isinstance(v, list) and not v]:
            del batch[key]
    return out
