"""Build the live world: real carriers (FMCSA) and real shippers (SEC EDGAR) for the fictional brokerage
Golden Gate Freight, plus contracted lanes. Cached in data/world.json; `python -m sim.world --refresh` rebuilds."""
import argparse
import random
from datetime import UTC, datetime

from integrations import directory, edgar, fmcsa

CARRIER_STATES = ["CA", "NV", "OR", "WA", "AZ", "UT", "ID"]
US_STATES = frozenset(["AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY"])


def build(n_carriers: int = 30, n_shippers: int = 12, seed: int | None = None) -> dict:
    rnd = random.Random(seed)
    carriers = fmcsa.search_carriers(CARRIER_STATES, limit=n_carriers, offset=rnd.randrange(0, 1500, 25))
    shippers = edgar.sample_shippers(n_shippers, states=US_STATES, seed=seed)
    for s in shippers:
        s["directory"] = {"company": s["name"], "name": "Accounts Payable (corporate main line)",
                          "phone": s["phone"], "email": None, "source": s["source"], "source_url": s["source_url"]}
    lanes = []
    for s in shippers:
        for c in rnd.sample(carriers, k=min(3, len(carriers))):
            lanes.append({"id": f"LN-{s['cik']}-{c['usdot']}", "shipper_id": s["id"], "carrier_id": c["id"],
                          "origin": f"{s['city']}, {s['state']}", "destination": f"{c['city']}, {c['state']}",
                          "contract_rate": float(rnd.randrange(900, 4200, 25)), "free_time_min": 120,
                          "detention_rate_hr": 75.0 if rnd.random() < 0.5 else 150.0})
    world = {"built_at": datetime.now(UTC).isoformat(), "brokerage": "Golden Gate Freight (fictional)",
             "sources": {"carriers": fmcsa.URL, "shippers": edgar.TICKERS},
             "carriers": carriers, "shippers": shippers, "lanes": lanes}
    directory.save(world)
    return world


def ensure(**kw) -> dict:
    w = directory.load()
    return w if w.get("lanes") else build(**kw)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--carriers", type=int, default=30)
    ap.add_argument("--shippers", type=int, default=12)
    ap.add_argument("--seed", type=int)
    ap.add_argument("--refresh", action="store_true")
    a = ap.parse_args()
    w = build(a.carriers, a.shippers, a.seed) if a.refresh else ensure(n_carriers=a.carriers,
                                                                     n_shippers=a.shippers, seed=a.seed)
    print(f"{len(w['carriers'])} carriers, {len(w['shippers'])} shippers, {len(w['lanes'])} lanes "
          f"-> {directory.path()}")
