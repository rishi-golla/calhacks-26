"""Real shippers from SEC EDGAR (public, no key): manufacturers, wholesalers and retailers with their registered
business address and main phone. SEC asks for a descriptive User-Agent with a contact address."""
import os
import random
import time

import httpx

from integrations.normalize import e164

TICKERS = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS = "https://data.sec.gov/submissions/CIK{:010d}.json"
FILINGS = "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={}"
# SIC ranges that ship physical freight: manufacturing, wholesale trade, retail trade.
SHIPPER_SIC = ((2000, 3999), (5000, 5199), (5200, 5999))
NOT_FREIGHT_SIC = ((2833, 2836),)  # pharma/biotech R&D filers rarely tender truckloads


def _client() -> httpx.Client:
    ua = os.getenv("SEC_USER_AGENT", "Golden Gate Freight (Cal Hacks research) ops@goldengatefreight.example")
    return httpx.Client(headers={"User-Agent": ua}, timeout=30)


def is_shipper(sic: str | int | None) -> bool:
    try:
        n = int(sic or 0)
    except ValueError:
        return False
    return any(lo <= n <= hi for lo, hi in SHIPPER_SIC) and not any(lo <= n <= hi for lo, hi in NOT_FREIGHT_SIC)


def normalize(sub: dict, ticker: str | None = None) -> dict:
    cik = int(sub["cik"])
    a = (sub.get("addresses") or {}).get("business") or {}
    return {
        "id": f"S-{cik}", "kind": "shipper", "cik": cik, "ticker": ticker or (sub.get("tickers") or [None])[0],
        "name": sub.get("name"), "sic": sub.get("sic"), "industry": sub.get("sicDescription"),
        "phone": e164(sub.get("phone")), "website": sub.get("website") or None,
        "address": ", ".join(x for x in (a.get("street1"), a.get("city"), a.get("stateOrCountry"),
                                         a.get("zipCode")) if x),
        "city": (a.get("city") or "").title(), "state": a.get("stateOrCountry"),
        "source": "sec_edgar", "source_url": FILINGS.format(cik),
    }


def company(cik: int, client: httpx.Client | None = None) -> dict:
    c = client or _client()
    r = c.get(SUBMISSIONS.format(int(cik)))
    r.raise_for_status()
    return r.json()


def sample_shippers(n: int = 12, states: set[str] | None = None, seed: int | None = None,
                    max_requests: int = 1500) -> list[dict]:
    """Walk a shuffled ticker list and keep US shippers (optionally in `states`) with a registered phone."""
    out: list[dict] = []
    with _client() as c:
        tick = c.get(TICKERS)
        tick.raise_for_status()
        rows = list(tick.json().values())
        random.Random(seed).shuffle(rows)
        seen: set[int] = set()
        for row in rows[:max_requests]:
            cik = int(row["cik_str"])
            if cik in seen:
                continue
            seen.add(cik)
            try:
                sub = company(cik, c)
            except httpx.HTTPError:
                continue
            finally:
                time.sleep(0.12)  # SEC fair-access limit is 10 requests/second
            s = normalize(sub, row.get("ticker"))
            if is_shipper(s["sic"]) and s["phone"] and s["state"] and (not states or s["state"] in states):
                out.append(s)
                if len(out) >= n:
                    break
    return out
