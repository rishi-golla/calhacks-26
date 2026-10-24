"""Build the live world: real carriers (FMCSA) and real shippers (SEC EDGAR) for the fictional brokerage
Golden Gate Freight, geocoded facilities, and contracted lanes priced off real mileage with accessorial schedules.
Cached in data/world.json; `python -m sim.world --refresh` rebuilds."""
import argparse
import random
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import httpx

from integrations import directory, edgar, fmcsa
from sim import geo

CARRIER_STATES: list[str] = []  # empty = nationwide
US_STATES = frozenset(geo.STATE_CENTROIDS)
DIESEL_USD = 3.85  # $/gal used by the fuel-surcharge table (DOE-style: (price - 1.25) / 6 mpg per mile)
GL_ACCOUNTS = [
    {"account": "1000", "name": "Operating Cash"}, {"account": "1200", "name": "Accounts Receivable"},
    {"account": "1250", "name": "Unbilled Receivables"}, {"account": "2000", "name": "Accounts Payable"},
    {"account": "2050", "name": "Accrued Purchased Transportation"}, {"account": "4000", "name": "Freight Revenue"},
    {"account": "4100", "name": "Accessorial Revenue"}, {"account": "5000", "name": "Purchased Transportation"},
    {"account": "5100", "name": "Accessorial Expense"}, {"account": "6900", "name": "Bad Debt / Short-pay Write-off"},
]


def commodity(sic: str | int | None, rnd: random.Random) -> dict:
    n = int(sic or 0)
    food = 2000 <= n <= 2099 or 5140 <= n <= 5149 or n == 5812 or 5400 <= n <= 5499
    name = ("Packaged food, palletized" if food else "Chemicals (non-hazmat), drums" if 2800 <= n <= 2899
            else "Plastic & rubber goods" if 3000 <= n <= 3099 else "Steel & aluminum coil" if 3300 <= n <= 3399
            else "Industrial machinery parts" if 3500 <= n <= 3599 else "Electronic components" if 3600 <= n <= 3699
            else "Auto parts" if 3700 <= n <= 3799 else "Wholesale general merchandise" if 5000 <= n <= 5199
            else "Retail store replenishment" if 5200 <= n <= 5999 else "Manufactured goods, palletized")
    return {"commodity": name, "equipment": "53' Reefer" if food else "53' Dry Van",
            "nmfc_class": rnd.choice([55, 65, 70, 85]), "temp_f": 34 if food else None}


def fsc_per_mile(diesel: float = DIESEL_USD) -> float:
    return round(max(0.0, (diesel - 1.25) / 6), 3)


def price_lane(miles: int, rnd: random.Random) -> dict:
    rpm = round(rnd.uniform(2.05, 3.10) if miles > 250 else rnd.uniform(3.4, 5.2), 2)
    linehaul = float(max(650, round(miles * rpm / 25) * 25))
    fsc = round(miles * fsc_per_mile(), 2)
    margin = rnd.uniform(1.12, 1.22)
    return {"miles": miles, "carrier_rpm": rpm, "linehaul": linehaul, "fsc": fsc,
            "contract_rate": round(linehaul + fsc, 2),
            "shipper_linehaul": float(round(linehaul * margin / 25) * 25), "shipper_fsc": fsc}


def _geocode_all(companies: list[dict]) -> None:
    with httpx.Client(timeout=20) as c, ThreadPoolExecutor(6) as ex:
        for co, g in zip(companies, ex.map(lambda x: geo.geocode(x.get("address"), x.get("state"), c), companies),
                         strict=True):
            co.update(g)


def build(n_carriers: int = 300, n_shippers: int = 60, seed: int | None = None) -> dict:
    rnd = random.Random(seed)
    carriers = fmcsa.search_carriers(CARRIER_STATES, limit=n_carriers, offset=rnd.randrange(0, 1500, 25))
    shippers = edgar.sample_shippers(n_shippers, states=US_STATES, seed=seed)
    _geocode_all(carriers + shippers)
    for s in shippers:
        s["directory"] = {"company": s["name"], "name": "Accounts Payable (corporate main line)",
                          "phone": s["phone"], "email": None, "source": s["source"], "source_url": s["source_url"]}
        s["terms"] = rnd.choice(["Net 30", "Net 30", "Net 45", "Net 15"])
    for c in carriers:
        c["terms"] = rnd.choice(["Net 30", "Net 30", "QuickPay 2% / 2 days"])
        c["factor"] = None  # no Notice of Assignment on file; remit-to must match the carrier
    lanes = []
    for s in shippers:
        consignees = [x for x in shippers if x is not s] or [s]
        near = sorted(carriers, key=lambda c: geo.great_circle_mi(s, c))[:8]
        for c in rnd.sample(near, k=min(3, len(near))):
            dest = rnd.choice(consignees)
            miles = geo.road_miles(s, dest)
            lanes.append({
                "id": f"LN-{s['cik']}-{dest['cik']}-{c['usdot']}", "shipper_id": s["id"], "carrier_id": c["id"],
                "consignee_id": dest["id"], "origin": f"{s['city']}, {s['state']}",
                "destination": f"{dest['city']}, {dest['state']}", **price_lane(miles, rnd),
                **commodity(s.get("sic"), rnd), "free_time_min": 120,
                "detention_rate_hr": 75.0 if rnd.random() < 0.5 else 50.0,
                "accessorials": {"lumper": "reimbursed at cost with receipt", "layover": 250.0, "tonu": 150.0,
                                 "stop_off": 75.0, "driver_assist": 100.0},
                "transit_hours": round(geo.transit_hours(miles), 1)})
    world = {"built_at": datetime.now(UTC).isoformat(), "brokerage": "Golden Gate Freight (fictional)",
             "brokerage_mc": "MC-000000 (fictional)", "diesel_usd": DIESEL_USD, "fsc_per_mile": fsc_per_mile(),
             "sources": {"carriers": fmcsa.URL, "shippers": edgar.TICKERS, "geocoder": geo.CENSUS},
             "gl_accounts": GL_ACCOUNTS, "carriers": carriers, "shippers": shippers, "lanes": lanes}
    directory.save(world)
    return world


def ensure(**kw) -> dict:
    w = directory.load()
    return w if w.get("lanes") and "miles" in w["lanes"][0] else build(**kw)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--carriers", type=int, default=300)
    ap.add_argument("--shippers", type=int, default=60)
    ap.add_argument("--seed", type=int)
    ap.add_argument("--refresh", action="store_true")
    a = ap.parse_args()
    w = build(a.carriers, a.shippers, a.seed) if a.refresh else ensure(n_carriers=a.carriers,
                                                                     n_shippers=a.shippers, seed=a.seed)
    geo_hits = sum(x.get("geo_source") == "census_geocoder" for x in w["carriers"] + w["shippers"])
    print(f"{len(w['carriers'])} carriers, {len(w['shippers'])} shippers ({geo_hits} geocoded), "
          f"{len(w['lanes'])} lanes -> {directory.path()}")
