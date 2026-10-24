"""W4: continuous, real-time synthetic operations for Golden Gate Freight -> Redis `events`.

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
import math
import os
import pathlib
import random
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import redis.asyncio as redis

from sim import bank, clock, docs, events, geo, mail, retention
from sim.world import WORLD_SCHEMA, ensure, offline_world

SCENARIOS = {"clean": 0.40, "decoy_detention_supported_small": 0.06, "decoy_quickpay": 0.06,
             "detention_unsupported": 0.04, "detention_supported": 0.04, "shipper_short_pay": 0.04,
             "bank_change": 0.04, "duplicate_invoice": 0.04, "duplicate_renumbered": 0.04,
             "rate_mismatch": 0.04, "fsc_error": 0.04, "math_error": 0.03, "lumper_no_receipt": 0.03,
             "factoring_unverified": 0.03, "pod_unsigned": 0.03, "double_brokering": 0.02,
             "misapplied_payment": 0.02}
# Scenarios that end in a parked call/email waiting on a human playing the counterparty.
NEEDS_HUMAN = {"detention_unsupported", "shipper_short_pay", "bank_change", "factoring_unverified"}
STREAM = "events"
FIRST = ["Maria", "James", "Luis", "Ashley", "Darnell", "Priya", "Tom", "Keisha", "Raj", "Elena", "Mike", "Grace"]
LAST = ["Garcia", "Nguyen", "Johnson", "Patel", "Kim", "Lopez", "Brown", "Singh", "Martinez", "Chen", "Davis"]


def data_dir() -> pathlib.Path:
    return pathlib.Path(os.getenv("SIM_DATA_DIR", "data"))


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
            "phone", "email", "lat", "lon", "terms", "directory", "source", "source_url")
    return {k: c[k] for k in keep if k in c}


def new_load(world: dict, rnd: random.Random, quick: bool = False) -> dict:
    lanes = world["lanes"]
    if quick:
        lanes = sorted(lanes, key=lambda lane: lane["transit_hours"])[:max(1, (len(lanes) + 2) // 3)]
    lane = rnd.choice(lanes)
    load_id = _id("LD")
    n = int(load_id[-6:], 16) % 900_000
    other_carriers = [c for c in world["carriers"] if c["id"] != lane["carrier_id"]]
    return {"load_id": load_id, "case_id": f"CASE-{load_id}", "lane": lane,
            "carrier": directory_entry(world, lane["carrier_id"]), "shipper": directory_entry(world, lane["shipper_id"]),
            "alternate_carrier": directory_entry(world, rnd.choice(other_carriers)["id"]) if other_carriers else None,
            "consignee": directory_entry(world, lane["consignee_id"]),
            "contract_rate": lane["contract_rate"], "free_time_min": lane["free_time_min"],
            "shipper_rate": lane["shipper_linehaul"] + lane["shipper_fsc"], "fsc_per_mile": world["fsc_per_mile"],
            "pro": f"{100000 + n}", "bol": f"BOL{rnd.randint(10**7, 10**8 - 1)}",
            "po": f"PO-{rnd.randint(400000, 999999)}", "seal": f"SL{rnd.randint(100000, 999999)}",
            "trailer": f"{rnd.randint(1000, 9999)}", "weight_lb": rnd.randrange(18000, 44000, 250),
            "pallets": rnd.randint(10, 26), "driver": _person(rnd)}


@dataclass
class Step:
    at: datetime  # business time the step happens
    make: Callable[[], list[dict]]  # writes its documents/mail and returns the events, called when the step fires


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


def plan(load: dict, scenario: str, rnd: random.Random) -> dict:
    """Decide every planted value up front so the answer key and documents agree."""
    lane, free = load["lane"], load["free_time_min"]
    receipt: bool | None = None
    if scenario == "detention_unsupported":
        dwell, claimed_h, receipt = rnd.randint(35, 80), 2, False
    elif scenario in ("detention_supported", "decoy_detention_supported_small"):
        dwell = free + 10 if scenario == "decoy_detention_supported_small" else rnd.randint(200, 290)
        claimed_h, receipt = math.ceil((dwell - free) / 60), True
    else:
        dwell, claimed_h = rnd.randint(25, 110), 0
    fsc = lane["fsc"]
    lines = [{"desc": "Linehaul", "amount": lane["linehaul"]},
             {"desc": "Fuel surcharge", "qty": lane["miles"], "rate": load["fsc_per_mile"], "amount": fsc}]
    bump = lambda pct, choices: max(rnd.choice(choices), round(lane["contract_rate"] * pct / 5) * 5)
    planted: dict = {}
    if claimed_h:
        amt = claimed_h * lane["detention_rate_hr"]
        lines.append({"desc": f"Detention {claimed_h}h", "qty": claimed_h, "rate": lane["detention_rate_hr"],
                      "amount": amt})
        planted = {"field": "detention", "claimed_h": claimed_h, "gps_dwell_min": dwell, "free_time_min": free}
    if scenario == "decoy_detention_supported_small":
        planted = {"field": "supported_detention", "claimed_h": claimed_h, "gps_dwell_min": dwell}
    if scenario == "decoy_quickpay":
        load["carrier"]["terms"] = "QuickPay 2% / 2 days"
        discount = round(lane["contract_rate"] * 0.02, 2)
        lines.append({"desc": "QuickPay discount 2%", "amount": -discount})
        planted = {"field": "quickpay_discount", "discount": discount}
    if scenario == "rate_mismatch":
        extra = bump(0.06, [150, 200, 250, 350])
        lines[0]["amount"] = lane["linehaul"] + extra
        planted = {"field": "linehaul", "billed": lines[0]["amount"], "contract": lane["linehaul"], "delta": extra}
    elif scenario == "fsc_error":
        wrong = round(lane["miles"] * rnd.uniform(1.25, 1.45) * load["fsc_per_mile"], 2)
        wrong = max(wrong, round(fsc + lane["contract_rate"] * 0.05, 2))
        lines[1]["amount"], lines[1]["qty"] = wrong, round(wrong / load["fsc_per_mile"])
        planted = {"field": "fuel_surcharge", "billed": wrong, "contract": fsc, "billed_miles": lines[1]["qty"],
                   "lane_miles": lane["miles"]}
    elif scenario == "lumper_no_receipt":
        amt = float(max(rnd.randrange(125, 400, 5), round(lane["contract_rate"] * 0.05)))
        lines.append({"desc": "Lumper (reimbursable)", "amount": amt})
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


def record_answer(load: dict, scenario: str, planted: dict | None = None) -> None:
    p = data_dir() / "answer_key.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a") as f:
        f.write(json.dumps({"ts": _now().isoformat(), "case_id": load["case_id"], "load_id": load["load_id"],
                            "carrier": load["carrier"]["name"], "shipper": load["shipper"]["name"],
                            "scenario": scenario, "planted": planted or {},
                            "expected": expected(load, scenario)}) + "\n")


def lifecycle(load: dict, scenario: str, rnd: random.Random, overrides: dict | None = None,
              t0: datetime | None = None) -> list[Step]:
    """Business-time schedule for one load. Documents are written when their step fires, never ahead of time."""
    t0 = t0 or _now()
    lid, case, c, s, k = load["load_id"], load["case_id"], load["carrier"], load["shipper"], load["consignee"]
    lane, free, rate = load["lane"], load["free_time_min"], load["contract_rate"]
    pl = plan(load, scenario, rnd)
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
    seq = rnd.randint(1, 8999)
    doc_carrier = (load.get("alternate_carrier") or c) if scenario == "double_brokering" else c
    inv_no = docs.invoice_number(doc_carrier, seq)
    inv_id = f"INV-{doc_carrier['usdot']}-{inv_no}"
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

    def add(at: datetime, fn: Callable[[], list[dict]]) -> None:
        steps.append(Step(at, fn))

    def tender() -> list[dict]:
        refs["rc"] = docs.write(f"RC-{lid}", "rate_confirmation", {
            **tms, "date": _ts(t0), "carrier": c, "shipper": s, "consignee": k, "pickup_appt": _ts(pickup),
            "delivery_appt": _ts(delivery_appt), "linehaul": lane["linehaul"], "fsc": lane["fsc"],
            "fsc_per_mile": load["fsc_per_mile"], "total": rate, "accessorials": lane["accessorials"],
            "free_time_min": free, "detention_rate_hr": lane["detention_rate_hr"], "terms": c.get("terms", "Net 30"),
            "temp_f": lane.get("temp_f")})
        return [_event("load.tendered", load, {**tms, "carrier": c, "shipper": s, "consignee": k,
                                                "contract_rate": rate, "free_time_min": free,
                                                "operator_overrides": overrides or {},
                                                "pickup_appt": pickup.isoformat(),
                                                "delivery_appt": delivery_appt.isoformat()}, t0, refs=[refs["rc"]])]
    add(t0, tender)
    dispatch_at = t0 + timedelta(minutes=rnd.randint(20, 90))
    add(dispatch_at, lambda: [_event("load.dispatched", load, {
        **base, "status": "dispatched", "driver": load["driver"], "truck": f"T{rnd.randint(100, 999)}",
        "trailer": load["trailer"]}, dispatch_at)])

    def ping(at: datetime, frac: float, milestone: str, dwell: int | None = None) -> Callable[[], list[dict]]:
        pt = geo.along(s, k, frac) if "lat" in s and "lat" in k else {}
        return lambda: [_event("gps.ping", load, {**base, **pt, "milestone": milestone, "speed_mph":
                               0 if frac in (0, 1) else rnd.randint(52, 66), "dwell_min": dwell}, at)]
    add(pickup, ping(pickup, 0, "pickup.arrived"))

    def loaded_step() -> list[dict]:
        refs["bol"] = docs.write(f"BOL-{lid}", "bill_of_lading", {
            **tms, "ship_date": _ts(loaded), "seal": load["seal"], "trailer": load["trailer"], "carrier": c,
            "shipper": s, "consignee": k, "nmfc_class": lane["nmfc_class"], "loader": _person(rnd),
            "loaded_at": _ts(loaded), "driver": load["driver"]})
        return [_event("load.status", load, {**base, "status": "loaded", "seal": load["seal"]}, loaded,
                       refs=[refs["bol"]]), *ping(loaded, 0, "pickup.departed")()]
    add(loaded, loaded_step)
    for frac in (0.25, 0.5, 0.75):
        at = loaded + (arrive - loaded) * frac
        add(at, ping(at, frac, "in_transit"))
    add(arrive, ping(arrive, 1, "delivery.arrived"))

    def delivered() -> list[dict]:
        signed = scenario != "pod_unsigned"
        refs["pod"] = docs.write(f"POD-{lid}", "proof_of_delivery", {
            **tms, "carrier": doc_carrier, "consignee": k, "arrived": _ts(arrive), "departed": _ts(depart),
            "pallets_received": load["pallets"], "receiver": _person(rnd) if signed else None})
        out = [refs["pod"]]
        if scenario in ("detention_supported", "decoy_detention_supported_small"):
            refs["det"] = docs.write(f"DET-{lid}", "detention_log", {
                "load_id": lid, "bol": load["bol"], "consignee": k, "appt": _ts(delivery_appt),
                "arrived": _ts(arrive), "departed": _ts(depart), "dwell_min": pl["dwell"], "clerk": _person(rnd)})
            out.append(refs["det"])
        return [_event("load.delivered", load, {**base, "dwell_min": pl["dwell"]}, depart),
                *ping(depart, 1, "delivery.departed", pl["dwell"])(),
                _event("pod.received", load, {**base, "pod_ref": refs["pod"], "signed": signed}, depart, refs=out)]
    add(depart, delivered)

    def invoice_payload(number: str, iid: str, at: datetime) -> tuple[dict, list[str]]:
        doc = docs.write(iid, "carrier_invoice", {
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
        out, ev_refs = invoice_payload(inv_no, inv_id, billed_at)
        refs["inv_evidence"] = json.dumps(ev_refs)
        refs["invoice_email"] = json.dumps(_email_payload(out["mail"]))
        return [_event("email.received", load, _email_payload(out["mail"]), billed_at, refs=ev_refs[:1])]
    add(billed_at, billed)
    if scenario == "duplicate_invoice":
        at = billed_at + timedelta(days=rnd.uniform(1, 2))
        add(at, lambda: [_event("email.received", load, json.loads(refs["invoice_email"]), at, "P2",
                                json.loads(refs["inv_evidence"]))])
    if scenario == "duplicate_renumbered":
        at = billed_at + timedelta(days=rnd.uniform(2, 4))
        no2 = docs.invoice_number(c, seq + rnd.randint(3, 40))

        def renumbered() -> list[dict]:
            out, ev_refs = invoice_payload(no2, f"INV-{c['usdot']}-{no2}", at)
            return [_event("email.received", load, _email_payload(out["mail"]), at, refs=ev_refs[:1])]
        add(at, renumbered)
    if scenario in ("bank_change", "factoring_unverified"):
        at = billed_at + timedelta(hours=rnd.uniform(2, 8))

        def bank_change() -> list[dict]:
            real = c.get("email") or f"billing@{c['usdot']}.carrier.example"
            sender = mail.lookalike(real) if scenario == "bank_change" else real
            new_account_last4 = f"{rnd.randint(0, 9999):04d}"
            m = mail.write(sender, mail.AP_INBOX, f"URGENT: updated remittance details - {c['name']}",
                           "Hi AP team,\n\nWe have changed banks. Effective immediately please send all payments, "
                           f"including invoice {inv_no}, to the new account below.\nRouting ****{remit['routing_last4']}"
                           f" / Account ****{new_account_last4}\n\nThank you,\nAccounting", at,
                           reply_to=sender)
            return [_event("email.received", load, _email_payload(m), at)]
        add(at, bank_change)

    sh_lines = [{"desc": f"Linehaul {lane['origin']} -> {lane['destination']} ({lane['miles']} mi)",
                 "amount": lane["shipper_linehaul"]}, {"desc": "Fuel surcharge", "amount": lane["shipper_fsc"]}]
    if scenario == "detention_supported":
        sh_lines.append({"desc": f"Detention {pl['claimed_h']}h", "amount": round(pl["claimed_h"] *
                                                                                 lane["detention_rate_hr"] * 1.25, 2)})
    sh_total = round(sum(li["amount"] for li in sh_lines), 2)
    sinv = f"SINV-{lid}"

    def shipper_invoice() -> list[dict]:
        refs["sinv"] = docs.write(sinv, "shipper_invoice", {
            **base, "invoice_id": sinv, "date": _ts(sinv_at), "terms": s.get("terms", "Net 30"),
            "due": _ts(sinv_at + timedelta(days=30)), "shipper": s, "line_items": sh_lines, "total": sh_total})
        return [_event("shipper_invoice.issued", load, {**base, "invoice_id": sinv, "shipper": s,
                                                        "amount": sh_total}, sinv_at, refs=[refs["sinv"]])]
    add(sinv_at, shipper_invoice)

    def paid() -> list[dict]:
        short = rnd.choice([150, 225, 300, 450]) if scenario == "shipper_short_pay" else 0
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
    add(paid_at, paid)
    steps.sort(key=lambda st: st.at)
    load["planted"] = pl["planted"]
    return steps


async def publish(r: redis.Redis | None, ev: dict) -> None:
    if r is not None:
        await r.xadd(STREAM, {"json": json.dumps(ev)}, maxlen=20_000, approximate=True)
    events.append_event(ev)


async def stream_load(r: redis.Redis, load: dict, scenario: str, rnd: random.Random,
                      overrides: dict | None = None, backdate: timedelta | None = None,
                      source: str = "sim") -> None:
    load["_event_source"] = source
    clk = await clock.load(r, heartbeat=True)
    t0 = clk.now() - backdate if backdate is not None else clk.now()
    steps = lifecycle(load, scenario, rnd, overrides, t0)
    record_answer(load, scenario, load.get("planted"))
    for st in steps:
        wait = clk.wall_delay(st.at - clk.now())
        if wait > 0:
            await asyncio.sleep(wait)
        for ev in st.make():
            ev["ts"] = events.iso_z()
            await publish(r, ev)


async def run_load(r: redis.Redis, world: dict, rnd: random.Random, scenario: str,
                   backdate: timedelta | None = None, source: str = "sim") -> dict:
    load = new_load(world, rnd)
    await stream_load(r, load, scenario, rnd, backdate=backdate, source=source)
    return load


async def inject(scenario: str, overrides: dict | None = None, r: redis.Redis | None = None):
    owns_redis = r is None
    client = r or redis.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"))
    world = ensure()
    rnd = random.Random()
    load = new_load(world, rnd, quick=True)
    load["_event_source"] = "demo"
    clk = await clock.load(client)
    await publish(client, _event("scenario.injected", load, {
        "scenario": scenario, "load_id": load["load_id"], "case_id": load["case_id"],
    }, clk.now(), priority="P1"))

    async def run() -> None:
        try:
            await stream_load(client, load, scenario, rnd, overrides, source="demo")
        finally:
            if owns_redis:
                await client.aclose()

    return asyncio.create_task(run())


async def seed_hero(world: dict) -> None:
    marker = data_dir() / ".hero_seeded"
    if marker.exists():
        return
    from sim.world import ensure
    world = ensure()
    carrier = directory_entry(world, "C-311")
    shipper = directory_entry(world, "S-07")
    consignee = {"id": "RENO-01", "name": "Reno Cold Storage", "address": "Reno, NV"}
    lane = {"id": "LN-HERO", "carrier_id": "C-311", "shipper_id": "S-07", "origin": "Stockton, CA",
            "destination": "Reno, NV", "miles": 133, "linehaul": 1850.0, "fsc": 0.0, "contract_rate": 1850.0,
            "free_time_min": 120, "detention_rate_hr": 150.0, "transit_hours": 1.0, "equipment": "Reefer",
            "commodity": "Produce", "nmfc_class": 70,
            "accessorials": {"lumper": "receipt required", "layover": 250.0, "tonu": 150.0,
                             "stop_off": 75.0, "driver_assist": 100.0}}
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
        invoice_ref = docs.write(invoice, "carrier_invoice", {**common, "invoice_number": invoice, "date": _ts(departed),
            "terms": "Net 30", "due": _ts(departed + timedelta(days=30)), "carrier": carrier,
            "remit_to": {"name": carrier["name"], "address": "", "routing_last4": "0000", "account_last4": "0000"},
            "line_items": [{"desc": "Linehaul", "amount": 1850.0},
                           {"desc": "Detention 2h", "qty": 2, "rate": 150.0, "amount": 300.0}],
            "total": 2150.0, "origin": lane["origin"], "destination": lane["destination"]})
        refs.append(invoice_ref)
        at = departed + timedelta(hours=2)
        message = mail.write(carrier["email"], "ap@goldengatefreight.example", f"Invoice {invoice}",
                             "Please see attached invoice and signed POD.", at,
                             attachments=[invoice, f"POD-{load_id}"])
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
            _event("pod.received", load, {**common, "pod_ref": refs[2], "signed": True}, departed, refs=refs[2:3]),
            _event("email.received", load, _email_payload(message), at),
        ]
        for event in events_to_publish:
            await publish(None, event)
        record_answer(load, "detention_unsupported", {"field": "detention", "claimed_h": 2,
                      "gps_dwell_min": 47, "free_time_min": 120})
    bank_change_at = _now()
    msg = mail.write("accounts@rapid-ridge.example", "ap@goldengatefreight.example",
                     "Remittance details - Rapid Ridge Transport",
                     "We changed banks. Invoice INV-RR-9400, new account details: Account ****9400.",
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
        record_answer(load, scenario, load.get("planted"))
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
