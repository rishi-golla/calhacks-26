"""W4: continuous, real-time event stream for Golden Gate Freight -> Redis `events`.

Every load runs its lifecycle in wall-clock time against real counterparties from data/world.json:
tender -> pickup/delivery GPS -> POD -> carrier invoice -> shipper billing -> shipper payment.
Exceptions are planted at configurable rates; the expected outcome of each is appended to data/answer_key.jsonl
so agent decisions can be scored. Source documents (rate con, POD, invoice) are written to data/docs/."""
import asyncio
import json
import math
import os
import pathlib
import random
import uuid
from datetime import UTC, datetime, timedelta

import redis.asyncio as redis

from common import store
from sim.world import ensure

SCENARIOS = {"clean": 0.55, "detention_unsupported": 0.12, "detention_supported": 0.08,
             "shipper_short_pay": 0.10, "bank_change": 0.05, "duplicate_invoice": 0.10}
# Scenarios that end in a parked call/email waiting on a human playing the counterparty.
NEEDS_HUMAN = {"detention_unsupported", "shipper_short_pay", "bank_change"}
STREAM = "events"


def data_dir() -> pathlib.Path:
    return pathlib.Path(os.getenv("SIM_DATA_DIR", "data"))


def _now() -> datetime:
    return datetime.now(UTC)


def _id(prefix: str) -> str:
    return f"{prefix}-{_now():%m%d}-{uuid.uuid4().hex[:4].upper()}"


def pick(rnd: random.Random, weights: dict[str, float] = SCENARIOS) -> str:
    return rnd.choices(list(weights), weights=list(weights.values()))[0]


def new_load(world: dict, rnd: random.Random) -> dict:
    lane = rnd.choice(world["lanes"])
    carrier = directory_entry(world, lane["carrier_id"])
    shipper = directory_entry(world, lane["shipper_id"])
    load_id = _id("LD")
    return {"load_id": load_id, "case_id": f"CASE-{load_id}", "lane": lane, "carrier": carrier, "shipper": shipper,
            "contract_rate": lane["contract_rate"], "free_time_min": lane["free_time_min"],
            "shipper_rate": round(lane["contract_rate"] * rnd.uniform(1.14, 1.24) / 25) * 25}


def directory_entry(world: dict, cid: str) -> dict:
    c = next(x for x in world["carriers"] + world["shippers"] if x["id"] == cid)
    keep = ("id", "name", "legal_name", "usdot", "mc", "cik", "ticker", "industry", "city", "state", "directory",
            "source", "source_url")
    return {k: c[k] for k in keep if k in c}


def write_doc(ref: str, doc: dict) -> str:
    d = data_dir() / "docs"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{ref}.json").write_text(json.dumps(doc, indent=1))
    return f"doc:{ref}"


def _event(etype: str, load: dict, payload: dict, priority: str = "P3", refs: list[str] | None = None) -> dict:
    ev = {"event_id": f"EVT-{uuid.uuid4().hex[:10]}", "type": etype, "ts": _now().isoformat(),
          "case_hint": load["case_id"], "priority": priority, "source": "sim", "payload": payload}
    if refs:
        ev["evidence_refs"] = refs
    return ev


def lifecycle(load: dict, scenario: str, rnd: random.Random, overrides: dict | None = None) -> list[tuple[float, dict]]:
    """(delay_before_seconds, event) steps for one load. Delays are scaled by SIM_SPEED at publish time."""
    lid, case, c, s = load["load_id"], load["case_id"], load["carrier"], load["shipper"]
    lane, rate, free = load["lane"], load["contract_rate"], load["free_time_min"]
    base = {"case_id": case, "load_id": lid, "lane": f"{lane['origin']} -> {lane['destination']}"}
    steps: list[tuple[float, dict]] = []
    ratecon = write_doc(f"RC-{lid}", {"type": "rate_confirmation", "broker": "Golden Gate Freight", **base,
                                      "carrier": c, "shipper": s, "linehaul": rate, "free_time_min": free,
                                      "detention_rate_hr": lane["detention_rate_hr"]})
    steps.append((0, _event("load.tendered", load, {**base, "carrier": c, "shipper": s, "contract_rate": rate},
                            refs=[ratecon])))
    if scenario == "detention_unsupported":
        dwell, claimed_h, receipt = rnd.randint(35, 80), 2, False
    elif scenario == "detention_supported":
        dwell = rnd.randint(200, 270)
        claimed_h, receipt = math.ceil((dwell - free) / 60), True
    else:
        dwell, claimed_h, receipt = rnd.randint(25, 110), 0, None
    arrived = _now() + timedelta(seconds=20)
    departed = arrived + timedelta(minutes=dwell)
    gps = {"arrived": f"{arrived:%H:%M}", "departed": f"{departed:%H:%M}"}
    for delay, stop, kind in ((6, lane["origin"], "pickup.arrived"), (5, lane["origin"], "pickup.departed"),
                              (8, lane["destination"], "delivery.arrived"),
                              (5, lane["destination"], "delivery.departed")):
        steps.append((delay, _event("gps.ping", load, {**base, "stop": stop, "milestone": kind,
                                                       "dwell_min": dwell if kind == "delivery.departed" else None})))
    pod = write_doc(f"POD-{lid}", {"type": "proof_of_delivery", **base, "consignee_signed": True, "gps": gps,
                                   "receipt_attached": receipt})
    steps.append((3, _event("pod.received", load, {**base, "pod_ref": pod}, refs=[pod])))

    lines = [{"desc": "Linehaul", "amount": rate}]
    if claimed_h:
        lines.append({"desc": f"Detention {claimed_h}h", "amount": claimed_h * lane["detention_rate_hr"]})
    inv_id = f"INV-{c['usdot']}-{lid[-4:]}"
    total = round(sum(li["amount"] for li in lines), 2)
    inv = write_doc(inv_id, {"type": "carrier_invoice", "invoice_id": inv_id, "remit_to": c, "bill_to":
                             "Golden Gate Freight", **base, "line_items": lines, "total": total})
    invoice = {**base, "invoice_id": inv_id, "carrier": c, "shipper": s, "contract_rate": rate,
               "invoice_amount": total, "line_items": lines, "detention_hours_claimed": claimed_h,
               "free_time_min": free, "gps": gps, "gps_dwell_min": dwell, "receipt_attached": receipt,
               **(overrides or {})}
    refs = [inv, pod, ratecon]
    steps.append((4, _event("carrier_invoice.received", load, invoice, "P1" if claimed_h else "P2", refs)))
    if scenario == "duplicate_invoice":
        steps.append((6, _event("carrier_invoice.received", load, invoice, "P2", refs)))

    if scenario == "bank_change":
        steps.append((5, _event("bank_change.requested", load, {
            **base, "case_id": f"{case}-BANK", "carrier": c, "requested_via": "email",
            "new_account_last4": f"{rnd.randint(0, 9999):04d}", "invoice_id": inv_id}, "P0")))

    billed = load["shipper_rate"] + (lines[1]["amount"] if receipt else 0)
    paid = billed - rnd.choice([150, 225, 300, 450]) if scenario == "shipper_short_pay" else billed
    sinv = f"SINV-{lid}"
    steps.append((10, _event("shipper_payment.received", load, {
        **base, "case_id": f"{case}-AR", "invoice_id": sinv, "shipper": s, "invoice_amount": float(billed),
        "paid_amount": float(paid), "chase": paid < billed, "remittance": "ACH"}, "P2")))
    return steps


def expected(load: dict, scenario: str) -> dict:
    return {
        "clean": {"carrier_ap": "approve", "shipper_ar": "reconciled"},
        "detention_unsupported": {"carrier_ap": "short_pay_detention_after_verification", "risk": "suspicious"},
        "detention_supported": {"carrier_ap": "approve_with_human_review", "risk": "clear"},
        "shipper_short_pay": {"shipper_ar": "short_pay_collections_call"},
        "bank_change": {"risk": "block_until_verified"},
        "duplicate_invoice": {"router": "duplicate_blocked", "carrier_ap": "approve_once"},
    }[scenario]


def record_answer(load: dict, scenario: str) -> None:
    p = data_dir() / "answer_key.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a") as f:
        f.write(json.dumps({"ts": _now().isoformat(), "case_id": load["case_id"], "load_id": load["load_id"],
                            "carrier": load["carrier"]["name"], "shipper": load["shipper"]["name"],
                            "scenario": scenario, "expected": expected(load, scenario)}) + "\n")


async def publish(r: redis.Redis, ev: dict) -> None:
    await r.xadd(STREAM, {"json": json.dumps(ev)}, maxlen=20_000, approximate=True)


async def stream_load(r: redis.Redis, load: dict, scenario: str, rnd: random.Random, speed: float = 1.0,
                      overrides: dict | None = None) -> None:
    record_answer(load, scenario)
    for delay, ev in lifecycle(load, scenario, rnd, overrides):
        await asyncio.sleep(delay / speed)
        ev["ts"] = _now().isoformat()
        await publish(r, ev)


async def run_load(r: redis.Redis, world: dict, rnd: random.Random, scenario: str, speed: float = 1.0) -> dict:
    load = new_load(world, rnd)
    await stream_load(r, load, scenario, rnd, speed)
    return load


def open_human_items() -> int:
    return len(store.list_parked())


async def main() -> None:
    world = ensure()
    r = redis.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"))
    rnd = random.Random()
    speed = float(os.getenv("SIM_SPEED", "1"))
    interval = float(os.getenv("SIM_LOAD_INTERVAL_S", "20"))
    max_open = int(os.getenv("SIM_MAX_OPEN_HUMAN_ITEMS", "3"))
    running: set[asyncio.Task] = set()
    print(f"sim: {len(world['carriers'])} carriers, {len(world['shippers'])} shippers; new load every ~{interval}s")
    while True:
        sc = pick(rnd)
        if sc in NEEDS_HUMAN and open_human_items() >= max_open:
            sc = "clean"  # don't bury the operator in parked calls
        t = asyncio.create_task(run_load(r, world, rnd, sc, speed))
        running.add(t)
        t.add_done_callback(running.discard)
        print(f"sim: started {sc}")
        await asyncio.sleep(interval * rnd.uniform(0.6, 1.4))


if __name__ == "__main__":
    asyncio.run(main())
