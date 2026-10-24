"""PRD hero counterparties and lanes, shared by seed_hero() and the demo presets (sim/demo.py).

Sierra Haul (C-311) hauls Bay Produce (S-07) Stockton -> Reno on a $1,850 rate con: 120 min free time,
$150/h detention, no fuel surcharge. Bay Produce is billed $2,200 linehaul for it, a $350 (19%) margin inside
the 12-22% band sim.world.price_lane draws for real lanes, and deliberately not the $2,150 the carrier invoices
so the two totals are never confused on a call. Rapid Ridge (C-77) runs the same shipper's long haul to
Elizabeth, NJ at $9,400, the PRD's bank-change payment."""

HERO_CARRIER = "C-311"
BANK_CHANGE_CARRIER = "C-77"
HERO_SHIPPER = "S-07"
HERO_CONSIGNEE = {"id": "RENO-01", "name": "Reno Cold Storage", "address": "Reno, NV"}
HERO_LINEHAUL = 1850.0
HERO_SHIPPER_LINEHAUL = 2200.0
BANK_CHANGE_LINEHAUL = 9400.0
LONG_HAUL = {"destination": "Elizabeth, NJ", "miles": 2870, "transit_hours": 46.0,
             "consignee": {"id": "ELIZ-01", "name": "Port Elizabeth Cold Storage", "address": "Elizabeth, NJ"}}
ACCESSORIALS = {"lumper": "receipt required", "layover": 250.0, "tonu": 150.0, "stop_off": 75.0,
                "driver_assist": 100.0}


def shipper_linehaul(linehaul: float) -> float:
    """The hero lane's margin, rounded to $25 like sim.world.price_lane."""
    return float(round(linehaul * HERO_SHIPPER_LINEHAUL / HERO_LINEHAUL / 25) * 25)


def hero_lane(carrier_id: str = HERO_CARRIER, shipper_id: str = HERO_SHIPPER,
              linehaul: float = HERO_LINEHAUL, destination: str = "Reno, NV", miles: int = 133,
              transit_hours: float = 1.0, consignee_id: str = HERO_CONSIGNEE["id"]) -> dict:
    """A complete lane dict: everything sim.generator.lifecycle() and plan() read."""
    suffix = "" if (carrier_id, shipper_id, destination) == (HERO_CARRIER, HERO_SHIPPER, "Reno, NV") \
        else f"-{carrier_id}-{destination.split(',')[0].upper()}"
    return {"id": f"LN-HERO{suffix}", "carrier_id": carrier_id, "shipper_id": shipper_id,
            "consignee_id": consignee_id, "origin": "Stockton, CA", "destination": destination, "miles": miles,
            "linehaul": linehaul, "fsc": 0.0, "contract_rate": linehaul,
            "shipper_linehaul": shipper_linehaul(linehaul), "shipper_fsc": 0.0,
            "free_time_min": 120, "detention_rate_hr": 150.0, "transit_hours": transit_hours,
            "equipment": "Reefer", "commodity": "Produce", "nmfc_class": 70, "accessorials": dict(ACCESSORIALS)}


def bank_change_lane() -> tuple[dict, dict]:
    """Rapid Ridge's long haul and its consignee."""
    lane = hero_lane(BANK_CHANGE_CARRIER, HERO_SHIPPER, BANK_CHANGE_LINEHAUL, LONG_HAUL["destination"],
                     LONG_HAUL["miles"], LONG_HAUL["transit_hours"], LONG_HAUL["consignee"]["id"])
    return lane, dict(LONG_HAUL["consignee"])
