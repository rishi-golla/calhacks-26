"""W4: continuous, real-time synthetic operations for Golden Gate Freight -> Redis `raw`.

Every load plays out on the shared business clock (sim/clock.py) against real counterparties and real mileage
from data/world.json, producing the full paper trail a brokerage receives:
  TMS     load.tendered, load.dispatched, load.status (loaded), load.delivered, gps.ping
  docs    rate con, BOL, signed POD, carrier invoice, shipper invoice, lumper receipt, detention log (PDF + JSON)
  email   raw .eml: carrier invoice with attachments, bank-change requests, shipper remittance advice
  bank    BAI2-style operating-account lines for shipper receipts
Raw email and bank arrivals are published for ingest.main to read and turn into case-opening events. Exceptions are
planted at configurable rates; the expected outcome of each (down to the field) is appended to
data/answer_key.jsonl so agent decisions can be scored."""
import asyncio
import json
import logging
import math
import os
import pathlib
import random
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import redis.asyncio as redis

from sim import bank, clock, docs, events, geo, hero, mail, retention
from sim.world import WORLD_SCHEMA, ensure, offline_world

SCENARIOS = {"clean": 0.60, "decoy_detention_supported_small": 0.06, "decoy_quickpay": 0.06,
             "clean_decoy": 0.03, "detention_unsupported": 0.0208333333333333,
             "detention_supported": 0.0208333333333333, "shipper_short_pay": 0.0208333333333333,
             "bank_change": 0.0208333333333333, "duplicate_invoice": 0.0208333333333333,
             "duplicate_renumbered": 0.0208333333333333, "rate_mismatch": 0.0208333333333333,
             "fsc_error": 0.0208333333333333, "math_error": 0.015625, "lumper_no_receipt": 0.015625,
             "factoring_unverified": 0.015625, "pod_unsigned": 0.015625,
             "double_brokering": 0.0104166666666667, "misapplied_payment": 0.0104166666666667}
assert math.isclose(sum(SCENARIOS.values()), 1.0, rel_tol=0.0, abs_tol=1e-9)
# Scenarios that end in a parked call/email waiting on a human playing the counterparty.
NEEDS_HUMAN = {"detention_unsupported", "shipper_short_pay", "bank_change", "factoring_unverified"}
RAW_STREAM = os.getenv("RAW_STREAM", "raw")
FIRST = ["Maria", "James", "Luis", "Ashley", "Darnell", "Priya", "Tom", "Keisha", "Raj", "Elena", "Mike", "Grace"]
LAST = ["Garcia", "Nguyen", "Johnson", "Patel", "Kim", "Lopez", "Brown", "Singh", "Martinez", "Chen", "Davis"]
_INVOICE_REGISTRIES: dict[pathlib.Path, dict[str, str]] = {}
# Injected loads stream in the background; holding the task keeps it from being garbage-collected mid-stream.
_BACKGROUND: set[asyncio.Task] = set()
log = logging.getLogger("sim")


def data_dir() -> pathlib.Path:
    return pathlib.Path(os.getenv("SIM_DATA_DIR", "data"))


def _load_rng(load: dict) -> random.Random:
    return random.Random(f"{os.getenv('SIM_SEED', '7')}:{load['load_id']}")


def _reserve_invoice_number(carrier: dict, rnd: random.Random, load_id: str,
                            force_new: bool = False) -> str:
    registry_path = data_dir() / "invoice_registry.json"
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    registry = _INVOICE_REGISTRIES.get(registry_path)
    if registry is None:
        registry = json.loads(registry_path.read_text()) if registry_path.exists() else {}
        for path in (data_dir() / "docs").glob("*.json"):
            try:
                doc = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            if doc.get("type") == "carrier_invoice" and doc.get("invoice_number"):
                registry[doc["invoice_number"]] = doc.get("load_id", "existing")
        _INVOICE_REGISTRIES[registry_path] = registry
    if not force_new:
        existing = next((number for number, owner in registry.items() if owner == load_id), None)
        if existing:
            # Make the draw the first reservation made, so replaying lifecycle() for a load replays its schedule.
            rnd.randrange(1, 1_000_000_000)
            return existing
    while True:
        number = docs.invoice_number(carrier, rnd.randrange(1, 1_000_000_000))
        if number not in registry:
            registry[number] = load_id
            temporary = registry_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(registry, sort_keys=True))
            temporary.replace(registry_path)
            return number


def _now() -> datetime:
    return datetime.now(UTC)


def _id(prefix: str) -> str:
    return f"{prefix}-{_now():%m%d}-{uuid.uuid4().hex[:8].upper()}"


def pick(rnd: random.Random, weights: dict[str, float] = SCENARIOS) -> str:
    return rnd.choices(list(weights), weights=list(weights.values()))[0]


def _person(rnd: random.Random) -> str:
    return f"{rnd.choice(FIRST)} {rnd.choice(LAST)}"


def _ts(t: datetime) -> str:
    return f"{t:%Y-%m-%d %H:%M}"


def directory_entry(world: dict, cid: str) -> dict:
    c = next(x for x in world["carriers"] + world["shippers"] if x["id"] == cid)
    keep = ("id", "name", "legal_name", "usdot", "mc", "cik", "ticker", "industry", "address", "city", "state",
            "phone", "email", "website", "lat", "lon", "terms", "directory", "source", "source_url")
    return {k: c[k] for k in keep if k in c}


def new_load(world: dict, rnd: random.Random, quick: bool = False) -> dict:
    load_id = _id("LD")
    rnd = _load_rng({"load_id": load_id})
    lanes = world["lanes"]
    if quick:
        lanes = sorted(lanes, key=lambda lane: lane["transit_hours"])[:max(1, (len(lanes) + 2) // 3)]
    lane = dict(rnd.choice(lanes))
    fsc_rate = round(world["fsc_per_mile"], 3)
    lane["fsc"] = round(lane["miles"] * fsc_rate, 2)
    lane["contract_rate"] = round(lane["linehaul"] + lane["fsc"], 2)
    lane["shipper_fsc"] = lane["fsc"]
    return build_load(world, load_id, lane, fsc_rate, rnd)


def build_load(world: dict, load_id: str, lane: dict, fsc_rate: float, rnd: random.Random,
               consignee: dict | None = None) -> dict:
    """The load record lifecycle() plays out, on a priced lane. `consignee` stands in for one outside the world."""
    n = int(load_id[-6:], 16) % 900_000
    other_carriers = [c for c in world["carriers"] if c["id"] != lane["carrier_id"]]
    return {"load_id": load_id, "case_id": f"CASE-{load_id}", "lane": lane,
            "carrier": directory_entry(world, lane["carrier_id"]), "shipper": directory_entry(world, lane["shipper_id"]),
            "alternate_carrier": directory_entry(world, rnd.choice(other_carriers)["id"]) if other_carriers else None,
            "consignee": consignee or directory_entry(world, lane["consignee_id"]),
            "contract_rate": lane["contract_rate"], "free_time_min": lane["free_time_min"],
            "shipper_rate": lane["shipper_linehaul"] + lane["shipper_fsc"], "fsc_per_mile": fsc_rate,
            "pro": f"{100000 + n}", "bol": f"BOL{rnd.randint(10**7, 10**8 - 1)}",
            "po": f"PO-{rnd.randint(400000, 999999)}", "seal": f"SL{rnd.randint(100000, 999999)}",
            "trailer": f"{rnd.randint(1000, 9999)}", "weight_lb": rnd.randrange(18000, 44000, 250),
            "pallets": rnd.randint(10, 26), "driver": _person(rnd)}


@dataclass
class Step:
    at: datetime  # business time the step happens
    name: str  # what happens: "tender", "dispatch", ..., "carrier_invoice", "bank_change", "paid"
    make: Callable[[], list[dict]]  # writes its documents/mail and returns the events, called when the step fires


# Values a caller may fix instead of drawing them (the demo presets); everything else stays random.
PINS = frozenset({"dwell", "short_amount"})


def _check_pins(pins: dict | None) -> dict:
    pins = dict(pins or {})
    if unknown := pins.keys() - PINS:
        raise ValueError(f"unknown pins: {sorted(unknown)}")
    for key, value in pins.items():
        if isinstance(value, bool) or not isinstance(value, int | float) or value < 0:
            raise ValueError(f"pin {key} must be a non-negative number")
    return pins


def _event(etype: str, load: dict, payload: dict, at: datetime, priority: str = "P3",
           refs: list[str] | None = None) -> dict:
    ev = {"event_id": f"EVT-{uuid.uuid4().hex[:10]}", "type": etype, "ts": events.iso_z(),
          "sim_ts": events.iso_z(at), "case_hint": payload.get("case_id", load["case_id"]),
          "priority": priority, "source": load.get("_event_source", "sim"), "payload": payload}
    if refs:
        ev["evidence_refs"] = refs
    return ev


def _email_payload(message: dict) -> dict:
    return {key: message[key] for key in ("mail_id", "from", "to", "subject", "sent_at", "attachments")}


def plan(load: dict, scenario: str, rnd: random.Random, pins: dict | None = None) -> dict:
    """Decide every planted value up front so the answer key and documents agree. A pinned value replaces the
    draw but the draw still happens, so every other value comes out the same as without the pin."""
    pins = _check_pins(pins)
    lane, free = load["lane"], load["free_time_min"]
    receipt: bool | None = None
    if scenario == "detention_unsupported":
        dwell, claimed_h, receipt = rnd.randint(35, 80), 2, False
    elif scenario in ("detention_supported", "decoy_detention_supported_small"):
        dwell = free + 10 if scenario == "decoy_detention_supported_small" else rnd.randint(200, 290)
        claimed_h, receipt = math.ceil((dwell - free) / 60), True
    else:
        dwell, claimed_h = rnd.randint(25, 110), 0
    dwell = pins.get("dwell", dwell)
    fsc_rate = round(load["fsc_per_mile"], 3)
    fsc = round(lane["miles"] * fsc_rate, 2)
    lines = [{"desc": "Linehaul", "amount": lane["linehaul"]},
             {"desc": "Fuel surcharge", "qty": lane["miles"], "rate": fsc_rate, "amount": fsc}]
    bump = lambda pct, choices: max(rnd.choice(choices), round(lane["contract_rate"] * pct / 5) * 5)
    planted: dict = {}
    if claimed_h:
        amt = round(claimed_h * lane["detention_rate_hr"], 2)
        lines.append({"desc": f"Detention {claimed_h}h", "qty": claimed_h, "rate": lane["detention_rate_hr"],
                      "amount": amt})
        planted = {"field": "detention", "claimed_h": claimed_h, "gps_dwell_min": dwell, "free_time_min": free}
    if scenario == "decoy_detention_supported_small":
        planted = {"field": "supported_detention", "claimed_h": claimed_h, "gps_dwell_min": dwell}
    if scenario == "decoy_quickpay":
        load["carrier"]["terms"] = "QuickPay 2% / 2 days"
        discount = round(lane["contract_rate"] * 0.02, 2)
        lines.append({"desc": "QuickPay discount 2%", "qty": 1, "rate": -discount, "amount": -discount})
        planted = {"field": "quickpay_discount", "discount": discount}
    if scenario == "rate_mismatch":
        extra = bump(0.06, [150, 200, 250, 350])
        lines[0]["amount"] = lane["linehaul"] + extra
        planted = {"field": "linehaul", "billed": lines[0]["amount"], "contract": lane["linehaul"], "delta": extra}
    elif scenario == "fsc_error":
        wrong_qty = lane["miles"] + math.ceil(lane["contract_rate"] * 0.05 / fsc_rate)
        wrong = round(wrong_qty * fsc_rate, 2)
        lines[1]["amount"], lines[1]["qty"] = wrong, wrong_qty
        planted = {"field": "fuel_surcharge", "billed": wrong, "contract": fsc, "billed_miles": lines[1]["qty"],
                   "lane_miles": lane["miles"]}
    elif scenario == "lumper_no_receipt":
        amt = float(max(rnd.randrange(125, 400, 5), round(lane["contract_rate"] * 0.05)))
        lines.append({"desc": "Lumper (reimbursable)", "qty": 1, "rate": amt, "amount": amt})
        planted = {"field": "lumper", "billed": amt, "receipt_attached": False}
        receipt = False
    total = round(sum(li["amount"] for li in lines), 2)
    stated = total
    if scenario == "math_error":
        err = max(rnd.choice([90.0, 100.0, 900.0]), round(lane["contract_rate"] * 0.05, 2))
        stated = round(total + err, 2)
        planted = {"field": "total", "stated_total": stated, "sum_of_lines": total, "delta": err}
    if scenario == "pod_unsigned":
        planted = {"field": "pod_signature", "pod_signed": False}
    if scenario == "double_brokering":
        planted = {"field": "carrier_identity", "tendered_carrier": load["carrier"]["id"],
                   "hauler": (load.get("alternate_carrier") or load["carrier"])["id"]}
    if scenario == "misapplied_payment":
        planted = {"field": "bank_reference", "expected": f"SINV-{load['load_id']}",
                   "billed": round(sum(x["amount"] for x in lines), 2)}
    if scenario == "duplicate_renumbered":
        planted = {"field": "duplicate", "same_bol": load["bol"], "same_load": load["load_id"]}
    if scenario in ("bank_change", "factoring_unverified"):
        planted = {"field": "remit_to", "channel": "lookalike-domain email" if scenario == "bank_change"
                   else "invoice remit-to (no NOA on file)"}
    return {"dwell": dwell, "claimed_h": claimed_h, "receipt": receipt, "lines": lines, "stated_total": stated,
            "planted": planted}


def expected(load: dict, scenario: str) -> dict:
    return {
        "clean": {"carrier_ap": "approve", "shipper_ar": "reconciled"},
        "clean_decoy": {"carrier_ap": "approve", "shipper_ar": "reconciled"},
        "decoy_detention_supported_small": {"carrier_ap": "approve_with_receipt"},
        "decoy_quickpay": {"carrier_ap": "approve_quickpay"},
        "detention_unsupported": {"carrier_ap": "short_pay_detention_after_verification", "risk": "suspicious"},
        "detention_supported": {"carrier_ap": "approve_with_human_review", "risk": "clear"},
        "shipper_short_pay": {"shipper_ar": "short_pay_collections_call"},
        "bank_change": {"risk": "block_until_verified"},
        "duplicate_invoice": {"router": "duplicate_blocked", "carrier_ap": "approve_once"},
        "duplicate_renumbered": {"carrier_ap": "hold_duplicate_same_bol"},
        "rate_mismatch": {"carrier_ap": "hold_linehaul_over_contract"},
        "fsc_error": {"carrier_ap": "hold_fuel_surcharge_over_contract"},
        "math_error": {"carrier_ap": "hold_total_not_equal_lines"},
        "lumper_no_receipt": {"carrier_ap": "hold_request_lumper_receipt"},
        "factoring_unverified": {"risk": "block_until_noa_verified"},
        "pod_unsigned": {"carrier_ap": "hold_missing_signed_pod"},
        "double_brokering": {"carrier_ap": "hold_carrier_identity"},
        "misapplied_payment": {"shipper_ar": "misapplied_payment"},
    }[scenario]


def record_answer(load: dict, scenario: str, planted: dict | None = None,
                  ts: datetime | None = None) -> None:
    p = data_dir() / "answer_key.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a") as f:
        f.write(json.dumps({"ts": (ts or _now()).isoformat(), "case_id": load["case_id"], "load_id": load["load_id"],
                            "carrier": load["carrier"]["name"], "shipper": load["shipper"]["name"],
                            "scenario": scenario, "planted": planted or {},
                            "expected": expected(load, scenario)}) + "\n")


def lifecycle(load: dict, scenario: str, rnd: random.Random, overrides: dict | None = None,
              t0: datetime | None = None, pins: dict | None = None) -> list[Step]:
    """Business-time schedule for one load. Documents are written when their step fires, never ahead of time.
    `overrides` are only recorded on the tender; `pins` fix planted values (see PINS)."""
    pins = _check_pins(pins)
    rnd = _load_rng(load)
    t0 = t0 or _now()
    lid, case, c, s, k = load["load_id"], load["case_id"], load["carrier"], load["shipper"], load["consignee"]
    lane, free, rate = load["lane"], load["free_time_min"], load["contract_rate"]
    pl = plan(load, scenario, rnd, pins)
    base = {"case_id": case, "load_id": lid, "lane": f"{lane['origin']} -> {lane['destination']}",
            "pro": load["pro"], "bol": load["bol"], "po": load["po"]}
    tms = {**base, "equipment": lane["equipment"], "commodity": lane["commodity"], "weight_lb": load["weight_lb"],
           "pallets": load["pallets"], "miles": lane["miles"]}
    pickup = (t0 + timedelta(hours=rnd.uniform(4, 9))).replace(minute=0, second=0, microsecond=0)
    loaded = pickup + timedelta(minutes=rnd.randint(40, 110))
    arrive = loaded + timedelta(hours=lane["transit_hours"])
    depart = arrive + timedelta(minutes=pl["dwell"])
    delivery_appt = arrive.replace(minute=0, second=0, microsecond=0)
    billed_at = depart + timedelta(hours=rnd.uniform(2, 20))
    sinv_at = depart + timedelta(hours=rnd.uniform(1, 6))
    paid_at = sinv_at + timedelta(days=rnd.uniform(1, 4))
    doc_carrier = (load.get("alternate_carrier") or c) if scenario == "double_brokering" else c
    inv_no = _reserve_invoice_number(doc_carrier, rnd, lid)
    lines, stated = pl["lines"], pl["stated_total"]
    remit = {"name": c["name"], "address": c.get("address"), "routing_last4": f"{rnd.randint(0, 9999):04d}",
             "account_last4": f"{rnd.randint(0, 9999):04d}"}
    if scenario == "double_brokering":
        remit = {"name": doc_carrier["name"], "address": doc_carrier.get("address"),
                 "routing_last4": "0000", "account_last4": "0000"}
    if scenario == "factoring_unverified":
        remit = {"name": f"{rnd.choice(['Apex', 'Summit', 'Triumph', 'Keystone'])} Freight Capital LLC",
                 "address": "PO Box 4410, Dallas, TX 75201", "routing_last4": f"{rnd.randint(0, 9999):04d}",
                 "account_last4": f"{rnd.randint(0, 9999):04d}"}
    refs: dict[str, str] = {}
    steps: list[Step] = []

    def add(at: datetime, name: str, fn: Callable[[], list[dict]]) -> None:
        steps.append(Step(at, name, fn))

    def tender() -> list[dict]:
        refs["rc"] = docs.write(f"RC-{lid}", "rate_confirmation", {
            **tms, "date": _ts(t0), "carrier": c, "shipper": s, "consignee": k, "pickup_appt": _ts(pickup),
            "delivery_appt": _ts(delivery_appt), "linehaul": lane["linehaul"], "fsc": lane["fsc"],
            "fsc_per_mile": load["fsc_per_mile"], "total": rate, "accessorials": lane["accessorials"],
            "free_time_min": free, "detention_rate_hr": lane["detention_rate_hr"], "terms": c.get("terms", "Net 30"),
            "temp_f": lane.get("temp_f")})
        # A demo load's routing context rides on the tender so ingest can stamp it on the case-opening event.
        demo = {"demo": load["demo"]} if "demo" in load else {}
        return [_event("load.tendered", load, {**tms, "carrier": c, "shipper": s, "consignee": k,
                                                "contract_rate": rate, "free_time_min": free,
                                                "operator_overrides": overrides or {},
                                                "pickup_appt": pickup.isoformat(),
                                                "delivery_appt": delivery_appt.isoformat(), **demo},
                       t0, refs=[refs["rc"]])]
    add(t0, "tender", tender)
    dispatch_at = t0 + timedelta(minutes=rnd.randint(20, 90))
    add(dispatch_at, "dispatch", lambda: [_event("load.dispatched", load, {
        **base, "status": "dispatched", "driver": load["driver"], "truck": f"T{rnd.randint(100, 999)}",
        "trailer": load["trailer"]}, dispatch_at)])

    def ping(at: datetime, frac: float, milestone: str, dwell: int | None = None) -> Callable[[], list[dict]]:
        pt = geo.along(s, k, frac) if "lat" in s and "lat" in k else {}
        return lambda: [_event("gps.ping", load, {**base, **pt, "milestone": milestone, "speed_mph":
                               0 if frac in (0, 1) else rnd.randint(52, 66), "dwell_min": dwell}, at)]
    add(pickup, "pickup", ping(pickup, 0, "pickup.arrived"))

    def loaded_step() -> list[dict]:
        refs["bol"] = docs.write(f"BOL-{lid}", "bill_of_lading", {
            **tms, "ship_date": _ts(loaded), "seal": load["seal"], "trailer": load["trailer"], "carrier": c,
            "shipper": s, "consignee": k, "nmfc_class": lane["nmfc_class"], "loader": _person(rnd),
            "loaded_at": _ts(loaded), "driver": load["driver"]})
        return [_event("load.status", load, {**base, "status": "loaded", "seal": load["seal"]}, loaded,
                       refs=[refs["bol"]]), *ping(loaded, 0, "pickup.departed")()]
    add(loaded, "loaded", loaded_step)
    for frac in (0.25, 0.5, 0.75):
        at = loaded + (arrive - loaded) * frac
        add(at, "in_transit", ping(at, frac, "in_transit"))
    add(arrive, "arrived", ping(arrive, 1, "delivery.arrived"))

    def delivered() -> list[dict]:
        signed = scenario != "pod_unsigned"
        receiver = _person(rnd) if signed else None
        refs["pod"] = docs.write(f"POD-{lid}", "proof_of_delivery", {
            **tms, "carrier": doc_carrier, "consignee": k, "arrived": _ts(arrive), "departed": _ts(depart),
            "pallets_received": load["pallets"], "receiver": receiver})
        out = [refs["pod"]]
        if scenario in ("detention_supported", "decoy_detention_supported_small"):
            refs["det"] = docs.write(f"DET-{lid}", "detention_log", {
                "load_id": lid, "bol": load["bol"], "consignee": k, "appt": _ts(delivery_appt),
                "arrived": _ts(arrive), "departed": _ts(depart), "dwell_min": pl["dwell"], "clerk": _person(rnd)})
            out.append(refs["det"])
        return [_event("load.delivered", load, {**base, "dwell_min": pl["dwell"]}, depart),
                *ping(depart, 1, "delivery.departed", pl["dwell"])(),
                _event("pod.received", load, {
                    **base, "pod_ref": refs["pod"], "signed": signed, "carrier_id": doc_carrier.get("id"),
                    "contract_rate": load["contract_rate"], "delivered_at": _ts(depart),
                    "receiver": receiver, "signed_by": receiver, "exceptions": [],
                }, depart, refs=out)]
    add(depart, "delivered", delivered)

    def invoice_payload(number: str, doc_ref: str, at: datetime) -> tuple[dict, list[str]]:
        doc = docs.write(doc_ref, "carrier_invoice", {
            **base, "invoice_number": number, "date": _ts(at), "terms": c.get("terms", "Net 30"),
            "due": _ts(at + timedelta(days=30)), "carrier": doc_carrier, "remit_to": remit, "line_items": lines,
            "total": stated, "origin": lane["origin"], "destination": lane["destination"]})
        attach = [doc] + [refs[x] for x in ("pod", "det") if x in refs]
        sender = c.get("email") or f"billing@{c['usdot']}.carrier.example"
        m = mail.write(sender, mail.AP_INBOX, f"Invoice {number} - Load {lid} / BOL {load['bol']}",
                       f"Hello,\n\nPlease find attached invoice {number} for load {lid} ({base['lane']}) and the "
                       f"signed POD.\nTotal due: ${stated:,.2f}. Remit to {remit['name']}.\n\nThanks,\nBilling\n"
                       f"{c['name']} | {c.get('phone') or ''}", at, attach)
        evidence = [doc, refs.get("pod"), refs["rc"], refs.get("bol"), refs.get("det"), f"mail:{m['mail_id']}"]
        return {"mail": m}, [x for x in evidence if x]

    def billed() -> list[dict]:
        out, ev_refs = invoice_payload(inv_no, f"INV-{lid}-1", billed_at)
        refs["inv_evidence"] = json.dumps(ev_refs)
        refs["invoice_email"] = json.dumps(_email_payload(out["mail"]))
        return [_event("email.received", load, _email_payload(out["mail"]), billed_at, refs=ev_refs[:1])]
    add(billed_at, "carrier_invoice", billed)
    if scenario == "duplicate_invoice":
        at = billed_at + timedelta(days=rnd.uniform(1, 2))
        add(at, "duplicate_invoice", lambda: [_event("email.received", load, json.loads(refs["invoice_email"]), at, "P2",
                                json.loads(refs["inv_evidence"]))])
    if scenario == "duplicate_renumbered":
        at = billed_at + timedelta(days=rnd.uniform(2, 4))
        no2 = _reserve_invoice_number(c, rnd, lid, force_new=True)

        def renumbered() -> list[dict]:
            out, ev_refs = invoice_payload(no2, f"INV-{lid}-2", at)
            return [_event("email.received", load, _email_payload(out["mail"]), at, refs=ev_refs[:1])]
        add(at, "renumbered_invoice", renumbered)
    if scenario in ("bank_change", "factoring_unverified"):
        at = billed_at + timedelta(hours=rnd.uniform(2, 8))

        def bank_change() -> list[dict]:
            real = c.get("email") or f"billing@{c['usdot']}.carrier.example"
            sender = mail.lookalike(real) if scenario == "bank_change" else real
            routing_number = bank.random_routing_number(rnd)
            account_number = bank.random_account_number(rnd)
            m = mail.write(sender, mail.AP_INBOX, f"URGENT: updated remittance details - {c['name']}",
                           "Hi AP team,\n\nWe have changed banks. Effective immediately please send all payments, "
                           f"including invoice {inv_no}, to the new account below. The next payment amount is "
                           f"${stated:,.2f}.\nRouting number: {routing_number}\nAccount number: {account_number}"
                           "\n\nThank you,\nAccounting", at,
                           reply_to=sender)
            return [_event("email.received", load, _email_payload(m), at)]
        add(at, "bank_change", bank_change)

    sh_lines = [{"desc": f"Linehaul {lane['origin']} -> {lane['destination']} ({lane['miles']} mi)",
                 "qty": 1, "rate": lane["shipper_linehaul"], "amount": lane["shipper_linehaul"]},
                {"desc": "Fuel surcharge", "qty": lane["miles"], "rate": load["fsc_per_mile"],
                 "amount": lane["shipper_fsc"]}]
    if scenario == "detention_supported":
        detention_rate = lane["detention_rate_hr"] * 1.25
        sh_lines.append({"desc": f"Detention {pl['claimed_h']}h", "qty": pl["claimed_h"],
                         "rate": detention_rate, "amount": round(pl["claimed_h"] * detention_rate, 2)})
    sh_total = round(sum(li["amount"] for li in sh_lines), 2)
    sinv = f"SINV-{lid}"

    def shipper_invoice() -> list[dict]:
        refs["sinv"] = docs.write(sinv, "shipper_invoice", {
            **base, "invoice_id": sinv, "date": _ts(sinv_at), "terms": s.get("terms", "Net 30"),
            "due": _ts(sinv_at + timedelta(days=30)), "shipper": s, "line_items": sh_lines, "total": sh_total})
        return [_event("shipper_invoice.issued", load, {**base, "invoice_id": sinv, "shipper": s,
                                                        "amount": sh_total}, sinv_at, refs=[refs["sinv"]])]
    add(sinv_at, "shipper_invoice", shipper_invoice)

    def paid() -> list[dict]:
        short = rnd.choice([150, 225, 300, 450]) if scenario == "shipper_short_pay" else 0
        if short:
            short = pins.get("short_amount", short)
        amount = round(sh_total - short, 2)
        bank_reference = f"SINV-MISSING-{lid}" if scenario == "misapplied_payment" else sinv
        txn = bank.post("ach_credit", amount, s["name"], bank_reference, paid_at)
        m = mail.write(f"remittance@{(s.get('ticker') or 'shipper').lower()}.remit.example", mail.AR_INBOX,
                       f"Remittance advice - {s['name']} - {bank_reference}",
                       f"Payment of ${amount:,.2f} sent via ACH for invoice {bank_reference} (PO {load['po']})."
                       + (f"\nDeduction ${short:,.2f}: rate dispute." if short else ""), paid_at)
        return [_event("email.received", load, _email_payload(m), paid_at),
                _event("bank.event", load, {"file": txn["file"], "account": txn["account"],
                                            "record": txn["record"]}, paid_at)]
    add(paid_at, "paid", paid)
    steps.sort(key=lambda st: st.at)
    load["planted"] = pl["planted"]
    return steps


def raw_maxlen() -> int:
    """Cap on Redis `raw` (approximate MAXLEN). Entries past it are trimmed whether or not ingest has read them, so
    keep it well above the longest backlog ingest may build up."""
    value = os.getenv("RAW_STREAM_MAXLEN", "200000")
    try:
        cap = int(value)
    except ValueError:
        cap = 0
    if cap <= 0:
        raise ValueError(f"RAW_STREAM_MAXLEN must be a positive integer, got {value!r}")
    return cap


RAW_STREAM_WARN_FRACTION = 0.8
RAW_STREAM_CHECK_S = 5.0
INGEST_GROUP = "ingest"  # ingest.main.GROUP, the consumer group reading `raw`
_last_length_check = 0.0


def _text(value) -> str:
    return value.decode() if isinstance(value, bytes) else str(value or "")


def _stream_id(value) -> tuple[int, int]:
    millis, _, seq = _text(value).partition("-")
    return int(millis or 0), int(seq or 0)


async def unread_backlog(r: redis.Redis) -> tuple[int | None, bool]:
    """(entries ingest has not read yet, whether unread entries were already trimmed). The count is the group's
    `lag` (Redis 7+). Without it, the group's last-delivered ID is compared with the oldest retained entry: if it
    is older, everything retained is unread and the gap has been trimmed; otherwise the backlog is unknown."""
    groups = await r.xinfo_groups(RAW_STREAM)
    group = next((g for g in groups if _text(g.get("name")) == INGEST_GROUP), None)
    if group is None:
        return int(await r.xlen(RAW_STREAM)), False  # ingest has not joined yet: nothing has been read
    if group.get("lag") is not None:
        return int(group["lag"]), False
    info = await r.xinfo_stream(RAW_STREAM)
    first = info.get("first-entry")
    if first and _stream_id(group.get("last-delivered-id")) < _stream_id(first[0]):
        return int(info.get("length") or 0), _text(group.get("last-delivered-id")) != "0-0"
    return None, False


async def _warn_near_cap(r: redis.Redis, cap: int) -> None:
    """At most every RAW_STREAM_CHECK_S, warn when ingest's unread backlog nears the cap; never fails a publish."""
    global _last_length_check
    now = time.monotonic()
    if now - _last_length_check < RAW_STREAM_CHECK_S:
        return
    _last_length_check = now
    try:
        backlog, trimmed = await unread_backlog(r)
    except Exception:  # noqa: BLE001 -- the check is advisory; publishing must not depend on it
        return
    if trimmed:
        log.warning("Redis %s trimmed entries ingest had not read (RAW_STREAM_MAXLEN=%d); arrivals were lost",
                    RAW_STREAM, cap)
    elif backlog is not None and backlog >= cap * RAW_STREAM_WARN_FRACTION:
        log.warning("ingest is %d entries behind on Redis %s, %.0f%% of RAW_STREAM_MAXLEN=%d; unread arrivals past "
                    "the cap are trimmed", backlog, RAW_STREAM, 100 * backlog / cap, cap)


async def publish(r: redis.Redis | None, ev: dict) -> None:
    if r is not None:
        cap = raw_maxlen()
        await r.xadd(RAW_STREAM, {"json": json.dumps(ev)}, maxlen=cap, approximate=True)
        await _warn_near_cap(r, cap)
    events.append_event(ev)


def fast_forward(load: dict, scenario: str, rnd: random.Random, now: datetime, trigger: str,
                 overrides: dict | None = None, pins: dict | None = None) -> tuple[datetime, list[Step]]:
    """Backdate t0 so the `trigger` step has just happened at `now`: every step up to it fires at once and the rest
    keep real pacing. Pickup is rounded down to the hour, so backdating by the trigger's offset can still leave it
    up to an hour ahead; one more hour always crosses that boundary."""
    t0 = now
    for attempt in range(3):
        steps = lifecycle(load, scenario, rnd, overrides, t0, pins)
        at = next((st.at for st in steps if st.name == trigger), None)
        if at is None:
            raise ValueError(f"{scenario} has no {trigger!r} step")
        if at <= now:
            return t0, steps
        t0 -= at - now if attempt == 0 else timedelta(hours=1)
    raise RuntimeError(f"could not fast-forward {load['load_id']} to {trigger}")


async def stream_load(r: redis.Redis, load: dict, scenario: str, rnd: random.Random,
                      overrides: dict | None = None, backdate: timedelta | None = None,
                      source: str = "sim", fast_forward_to: str | None = None, pins: dict | None = None) -> None:
    load["_event_source"] = source
    raw_maxlen()  # fail before the answer key records a load that could never be published
    clk = await clock.load(r, heartbeat=True)
    if fast_forward_to:
        t0, steps = fast_forward(load, scenario, rnd, clk.now(), fast_forward_to, overrides, pins)
    else:
        t0 = clk.now() - backdate if backdate is not None else clk.now()
        steps = lifecycle(load, scenario, rnd, overrides, t0, pins)
    record_answer(load, scenario, load.get("planted"), t0)
    for n, st in enumerate(steps, 1):
        wait = clk.wall_delay(st.at - clk.now())
        if wait > 0:
            await asyncio.sleep(wait)
        for ev in st.make():
            ev["ts"] = events.iso_z()
            await publish(r, ev)
        if st.name == fast_forward_to:
            log.info("fast_forward %s: emitted %d steps through %s", load["load_id"], n, st.name)


async def run_load(r: redis.Redis, world: dict, rnd: random.Random, scenario: str,
                   backdate: timedelta | None = None, source: str = "sim") -> dict:
    load = new_load(world, rnd)
    await stream_load(r, load, scenario, rnd, backdate=backdate, source=source)
    return load


async def launch(load: dict, scenario: str, r: redis.Redis | None, injection: dict, priority: str = "P1",
                 overrides: dict | None = None, pins: dict | None = None,
                 fast_forward_to: str | None = None) -> asyncio.Task:
    """Announce an operator-injected load (`scenario.injected`) and stream it in the background. Returns the
    streaming task once the announcement is in `raw`."""
    raw_maxlen()  # fail before announcing a load that could never stream
    owns_redis = r is None
    client = r or redis.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"))
    load["_event_source"] = "demo"
    try:
        clk = await clock.load(client)
        await publish(client, _event("scenario.injected", load, {**injection, "load_id": load["load_id"]},
                                     clk.now(), priority=priority))
    except BaseException:
        if owns_redis:
            await client.aclose()
        raise

    async def run() -> None:
        try:
            await stream_load(client, load, scenario, random.Random(), overrides, source="demo",
                              fast_forward_to=fast_forward_to, pins=pins)
        except Exception:
            log.exception("injected %s stopped streaming", load["load_id"])  # nobody awaits this task
            raise
        finally:
            if owns_redis:
                await client.aclose()

    task = asyncio.create_task(run())
    _BACKGROUND.add(task)
    task.add_done_callback(_BACKGROUND.discard)
    return task


async def inject(scenario: str, overrides: dict | None = None, r: redis.Redis | None = None,
                 fast_forward_to: str | None = None) -> asyncio.Task:
    """A random quick lane, no demo context (sim/demo.py starts the hero presets)."""
    load = new_load(ensure(), random.Random(), quick=True)
    return await launch(load, scenario, r, {"injection_id": f"INJ-{uuid.uuid4().hex}"}, overrides=overrides,
                        fast_forward_to=fast_forward_to)


async def seed_hero(world: dict) -> None:
    marker = data_dir() / ".hero_seeded"
    if marker.exists():
        return
    from sim.world import ensure
    world = ensure()
    carrier = directory_entry(world, hero.HERO_CARRIER)
    shipper = directory_entry(world, hero.HERO_SHIPPER)
    consignee = dict(hero.HERO_CONSIGNEE)
    lane = hero.hero_lane()
    for load_id, offset in (("LOAD-4821", 0), ("LOAD-4877", 14)):
        base_time = (_now() + timedelta(days=offset)).replace(hour=13, minute=0, second=0, microsecond=0)
        case_id, bol, po = f"CASE-{load_id[5:]}", f"BOL-{load_id}", f"PO-{load_id}"
        loaded, arrived, departed = (base_time + timedelta(minutes=55), base_time + timedelta(hours=1, minutes=2),
                                     base_time + timedelta(hours=1, minutes=49))
        load = {"load_id": load_id, "case_id": case_id, "carrier": carrier, "shipper": shipper,
                "consignee": consignee, "lane": lane, "contract_rate": 1850.0, "free_time_min": 120,
                "bol": bol, "po": po, "pro": f"PRO-{load_id}", "fsc_per_mile": 0.0, "pallets": 20,
                "weight_lb": 18000, "seal": "S4821", "trailer": "R-311", "driver": "Alex Rivera"}
        common = {"load_id": load_id, "case_id": case_id, "bol": bol, "po": po, "pro": load["pro"],
                  "lane": "Stockton, CA -> Reno, NV", "equipment": lane["equipment"],
                  "commodity": lane["commodity"], "weight_lb": load["weight_lb"],
                  "pallets": load["pallets"], "miles": lane["miles"]}
        refs = [
            docs.write(f"RC-{load_id}", "rate_confirmation", {**common, "date": _ts(base_time), "carrier": carrier,
                "shipper": shipper, "consignee": consignee, "pickup_appt": _ts(loaded), "delivery_appt": _ts(arrived),
                "linehaul": 1850.0, "fsc": 0.0, "fsc_per_mile": 0.0, "total": 1850.0, "accessorials": lane["accessorials"],
                "free_time_min": 120, "detention_rate_hr": 150.0, "terms": "Net 30"}),
            docs.write(f"BOL-{load_id}", "bill_of_lading", {**common, "ship_date": _ts(loaded), "seal": load["seal"],
                "trailer": load["trailer"], "carrier": carrier, "shipper": shipper, "consignee": consignee,
                "commodity": "Produce", "nmfc_class": 70, "pallets": 20, "weight_lb": 18000, "loader": "Pat Lee",
                "loaded_at": _ts(loaded), "driver": load["driver"]}),
            docs.write(f"POD-{load_id}", "proof_of_delivery", {**common, "carrier": carrier, "consignee": consignee,
                "arrived": _ts(arrived), "departed": _ts(departed), "pallets_received": 20, "receiver": "R. Gomez"}),
        ]
        invoice = f"INV-3141592-H{load_id[-4:]}"
        invoice_ref = docs.write(f"INV-{load_id}-1", "carrier_invoice",
            {**common, "invoice_number": invoice, "date": _ts(departed),
            "terms": "Net 30", "due": _ts(departed + timedelta(days=30)), "carrier": carrier,
            "remit_to": {"name": carrier["name"], "address": "", "routing_last4": "0000", "account_last4": "0000"},
            "line_items": [{"desc": "Linehaul", "amount": 1850.0},
                           {"desc": "Detention 2h", "qty": 2, "rate": 150.0, "amount": 300.0}],
            "total": 2150.0, "origin": lane["origin"], "destination": lane["destination"]})
        refs.append(invoice_ref)
        at = departed + timedelta(hours=2)
        sinv = f"SINV-{load_id}"
        sinv_ref = docs.write(sinv, "shipper_invoice",
            {**common, "invoice_id": sinv, "date": _ts(at), "terms": "Net 30",
             "due": _ts(at + timedelta(days=30)), "shipper": shipper,
             "line_items": [{"desc": "Linehaul", "qty": 1, "rate": 1850.0, "amount": 1850.0},
                            {"desc": "Fuel surcharge", "qty": 133, "rate": 0.0, "amount": 0.0}],
             "total": 1850.0})
        refs.append(sinv_ref)
        message = mail.write(carrier["email"], "ap@goldengatefreight.example", f"Invoice {invoice}",
                             "Please see attached invoice and signed POD.", at,
                             attachments=[f"INV-{load_id}-1", f"POD-{load_id}"])
        paid_at = at + timedelta(days=1)
        remittance = mail.write(shipper["email"], "ar@goldengatefreight.example",
                                f"Remittance advice - {shipper['name']} - {sinv}",
                                f"Payment of $1,850.00 sent via ACH for invoice {sinv} (PO {po}).",
                                paid_at)
        payment = bank.post("ach_credit", 1850.0, shipper["name"], sinv, paid_at)
        tms = {**common, "carrier": carrier, "shipper": shipper, "consignee": consignee, "contract_rate": 1850.0,
               "free_time_min": 120, "operator_overrides": {}, "pickup_appt": _ts(loaded),
               "delivery_appt": _ts(arrived)}
        events_to_publish = [
            _event("load.tendered", load, tms, base_time, refs=[refs[0]]),
            _event("load.dispatched", load, {**common, "status": "dispatched"}, base_time + timedelta(minutes=20)),
            _event("load.status", load, {**common, "status": "loaded"}, loaded, refs=[refs[1]]),
            _event("gps.ping", load, {**common, "milestone": "delivery.arrived", "dwell_min": None}, arrived),
            _event("load.delivered", load, {**common, "dwell_min": 47}, departed),
            _event("gps.ping", load, {**common, "milestone": "delivery.departed", "dwell_min": 47}, departed),
            _event("pod.received", load, {
                **common, "pod_ref": refs[2], "signed": True, "carrier_id": carrier["id"],
                "contract_rate": 1850.0, "delivered_at": _ts(departed),
                "receiver": "R. Gomez", "signed_by": "R. Gomez", "exceptions": [],
            }, departed, refs=refs[2:3]),
            _event("shipper_invoice.issued", load,
                   {**common, "invoice_id": sinv, "shipper": shipper, "amount": 1850.0},
                   at, refs=[sinv_ref]),
            _event("email.received", load, _email_payload(message), at),
            _event("email.received", load, _email_payload(remittance), paid_at),
            _event("bank.event", load, {"file": payment["file"], "account": payment["account"],
                                        "record": payment["record"]}, paid_at),
        ]
        for event in events_to_publish:
            await publish(None, event)
        record_answer(load, "detention_unsupported", {"field": "detention", "claimed_h": 2,
                      "gps_dwell_min": 47, "free_time_min": 120})
    bank_change_at = _now()
    hero_rng = _load_rng({"load_id": "LOAD-C-77-BANK"})
    routing_number = bank.random_routing_number(hero_rng)
    account_number = bank.random_account_number(hero_rng)
    msg = mail.write("accounts@rapid-ridge.example", "ap@goldengatefreight.example",
                     "Remittance details - Rapid Ridge Transport",
                     "We changed banks effective immediately. Invoice INV-RR-9400, next payment amount: "
                     "$9,400.00.\nRouting number: " + routing_number + "\nAccount number: " + account_number,
                     bank_change_at)
    await publish(None, _event("email.received", {"case_id": "CASE-C-77-BANK"},
                                _email_payload(msg), bank_change_at))
    txn = bank.post("ach_debit", 9400, "Rapid Ridge Transport", "INV-RR-9400",
                    bank_change_at + timedelta(days=1))
    await publish(None, _event("bank.event", {"case_id": "CASE-C-77-BANK"},
                 {"file": txn["file"], "account": txn["account"], "record": txn["record"]},
                 bank_change_at + timedelta(days=1)))
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("seeded")


async def heartbeat(r: redis.Redis) -> None:
    while True:
        await clock.load(r, heartbeat=True)
        await asyncio.sleep(5)


async def retention_loop() -> None:
    while True:
        retention.prune(data_dir())
        await asyncio.sleep(60)


async def main() -> None:
    raw_maxlen()  # fail fast on a bad RAW_STREAM_MAXLEN
    world = ensure()
    await seed_hero(world)
    r = redis.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"))
    rnd = random.Random()
    interval = float(os.getenv("SIM_LOAD_INTERVAL_S", "8"))
    max_open = int(os.getenv("SIM_MAX_OPEN_HUMAN_ITEMS", "3"))
    warm_start_loads = int(os.getenv("SIM_WARM_START_LOADS", "12"))
    history_days = int(os.getenv("SIM_HISTORY_DAYS", "90"))
    history_loads = int(os.getenv("SIM_HISTORY_LOADS", "120"))
    for _ in range(history_loads):
        scenario = pick(rnd)
        backdate = timedelta(days=rnd.uniform(2, max(2, history_days)))
        await run_load(r, world, rnd, scenario, backdate=backdate)
    running: set[asyncio.Task] = {asyncio.create_task(heartbeat(r)), asyncio.create_task(retention_loop())}
    human_running: set[asyncio.Task] = set()
    print(f"sim: {len(world['carriers'])} carriers, {len(world['shippers'])} shippers; new load every ~{interval}s; "
          f"1 wall second = {clock.ratio() / 60:.0f} business minutes")
    warm_human = 0
    for _ in range(warm_start_loads):
        sc = pick(rnd)
        if sc in NEEDS_HUMAN:
            if warm_human >= max_open:
                sc = "clean"
            else:
                warm_human += 1
        t = asyncio.create_task(run_load(r, world, rnd, sc, timedelta(hours=rnd.uniform(0, 96))))
        running.add(t)
        t.add_done_callback(running.discard)
        if sc in NEEDS_HUMAN:
            human_running.add(t)
            t.add_done_callback(human_running.discard)
        print(f"sim: warm-started {sc}")
    while True:
        sc = pick(rnd)
        if sc in NEEDS_HUMAN and len(human_running) >= max_open:
            sc = "clean"  # don't bury the operator in parked calls
        t = asyncio.create_task(run_load(r, world, rnd, sc))
        running.add(t)
        t.add_done_callback(running.discard)
        if sc in NEEDS_HUMAN:
            human_running.add(t)
            t.add_done_callback(human_running.discard)
        print(f"sim: started {sc}")
        await asyncio.sleep(interval * rnd.uniform(0.6, 1.4))


async def offline(loads: int, root: pathlib.Path, seed: int = 7) -> None:
    root.mkdir(parents=True, exist_ok=True)
    os.environ["SIM_DATA_DIR"] = str(root)
    os.environ["WORLD_FILE"] = str(root / "world.json")
    os.environ["SIM_SEED"] = str(seed)
    from integrations import directory
    if not (root / "world.json").exists() or directory.load().get("schema") != WORLD_SCHEMA:
        directory.save(offline_world())
    world = ensure()
    await seed_hero(world)
    rnd = random.Random(seed)
    now = _now()
    history_days = int(os.getenv("SIM_HISTORY_DAYS", "90"))
    for _ in range(loads):
        scenario = pick(rnd)
        load = new_load(world, rnd)
        start = now - timedelta(days=rnd.uniform(3, max(3, history_days)))
        steps = lifecycle(load, scenario, rnd, t0=start)
        record_answer(load, scenario, load.get("planted"), start)
        for step in steps:
            for event in step.make():
                await publish(None, event)
    retention.prune(root)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--loads", type=int, default=40)
    parser.add_argument("--data", type=pathlib.Path, default=pathlib.Path("data"))
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()
    asyncio.run(offline(args.loads, args.data, args.seed) if args.offline else main())
