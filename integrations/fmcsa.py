"""Real carriers from FMCSA's public Company Census File on data.transportation.gov (Socrata, no key needed).
Registered phone/email/address are public MCS-150 records, so they are the most trusted verification contact."""
import os

import httpx

from integrations.normalize import e164, email

URL = "https://data.transportation.gov/resource/az4n-8mr2.json"
FIELDS = ("dot_number,legal_name,dba_name,phone,email_address,phy_street,phy_city,phy_state,phy_zip,"
          "power_units,total_drivers,docket1prefix,docket1,mcs150_date")
SNAPSHOT = ("https://safer.fmcsa.dot.gov/query.asp?searchtype=ANY&query_type=queryCarrierSnapshot"
            "&query_param=USDOT&query_string={}")
# Active, interstate, for-hire (MC docket), general freight, with a registered phone and email.
BASE_WHERE = ("status_code='A' AND carrier_operation='A' AND docket1prefix='MC' AND crgo_genfreight='X' "
              "AND email_address IS NOT NULL AND phone IS NOT NULL")


def _get(params: dict) -> list[dict]:
    headers = {"X-App-Token": t} if (t := os.getenv("SOCRATA_APP_TOKEN")) else {}
    r = httpx.get(URL, params=params, headers=headers, timeout=30)
    r.raise_for_status()
    return r.json()


def normalize(row: dict) -> dict:
    dot = row["dot_number"]
    return {
        "id": f"C-{dot}", "kind": "carrier", "usdot": dot,
        "mc": f"MC-{row['docket1']}" if row.get("docket1") else None,
        "name": row.get("dba_name") or row.get("legal_name"), "legal_name": row.get("legal_name"),
        "phone": e164(row.get("phone")), "email": email(row.get("email_address")),
        "address": ", ".join(x for x in (row.get("phy_street"), row.get("phy_city"), row.get("phy_state"),
                                         row.get("phy_zip")) if x),
        "city": (row.get("phy_city") or "").title(), "state": row.get("phy_state"),
        "power_units": int(row.get("power_units") or 0), "drivers": int(row.get("total_drivers") or 0),
        "mcs150_date": row.get("mcs150_date"), "authority_status": "ACTIVE",
        "source": "fmcsa_census", "source_url": SNAPSHOT.format(dot),
    }


def search_carriers(states: list[str], min_units: int = 10, max_units: int = 300, limit: int = 25,
                    offset: int = 0, updated_since: str = "20250101") -> list[dict]:
    st = ",".join(f"'{s.upper()}'" for s in states if s.isalpha() and len(s) == 2)
    where = (f"{BASE_WHERE} AND power_units::number BETWEEN {int(min_units)} AND {int(max_units)} "
             f"AND mcs150_date > '{int(updated_since)}'" + (f" AND phy_state in({st})" if st else ""))
    rows = _get({"$select": FIELDS, "$where": where, "$limit": int(limit), "$offset": int(offset),
                 "$order": "dot_number"})
    return [normalize(r) for r in rows]


def lookup(usdot: str) -> dict | None:
    if not str(usdot).isdigit():
        return None
    rows = _get({"$select": FIELDS + ",status_code", "$where": f"dot_number='{usdot}'", "$limit": 1})
    if not rows:
        return None
    c = normalize(rows[0])
    c["authority_status"] = "ACTIVE" if rows[0].get("status_code") == "A" else "INACTIVE"
    return c
