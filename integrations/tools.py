"""Integration tools called by agents (PRD 7.6). Return {"error": ...} instead of raising.
Real by default: public FMCSA/SEC records, plus optional keyed providers. MOCK_INTEGRATIONS=1 is for offline tests."""
import os
import re

import httpx

from integrations import directory, fmcsa

FREE_MAIL = {"gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "aol.com", "icloud.com", "live.com",
             "msn.com", "comcast.net", "att.net", "sbcglobal.net", "verizon.net", "ymail.com", "me.com"}
APIFY_SCRAPER = "https://api.apify.com/v2/acts/vdrmota~contact-info-scraper/run-sync-get-dataset-items"
ROLE_DESK = {"dispatch": "Dispatch / Billing (FMCSA registered contact)",
             "accounts_payable": "Accounts Payable (corporate main line)"}


def _mock() -> bool:
    return os.getenv("MOCK_INTEGRATIONS", "0") == "1"


def fmcsa_lookup(usdot: str | None = None, mc: str | None = None) -> dict:
    if _mock():
        return {"legal_name": "MOCK CARRIER LLC", "authority_status": "ACTIVE", "phone": "+15555550100",
                "source": "mock"}
    if not usdot:
        return {"error": "usdot required"}
    try:
        c = directory.by_usdot(usdot) or fmcsa.lookup(usdot)
    except httpx.HTTPError as e:
        return {"error": f"fmcsa: {e}"}
    if not c:
        return {"error": f"USDOT {usdot} not found"}
    return {k: c.get(k) for k in ("legal_name", "name", "usdot", "mc", "authority_status", "phone", "email",
                                  "address", "source", "source_url")}


def website_for(company: dict) -> str | None:
    if company.get("website"):
        return company["website"]
    dom = (company.get("email") or "").partition("@")[2]
    return f"https://{dom}" if dom and dom not in FREE_MAIL else None


def enrich_contact(company_id: str, role: str) -> dict:
    """Waterfall: public registration (FMCSA/SEC) -> website scrape (Apify, if APIFY_TOKEN). Every value keeps its
    source; values that disagree with the registration are kept as alternates and flagged as a conflict."""
    if _mock():
        return {"name": "Mock Dispatcher", "title": role, "phone": "+15555550100", "email": "mock@example.com",
                "source": "mock", "confidence": 0.5, "alternates": []}
    c = directory.get(company_id)
    if not c:
        return {"error": f"{company_id} not in directory"}
    out = {"company": c["name"], "name": ROLE_DESK.get(role, role), "title": role, "phone": c.get("phone"),
           "email": c.get("email"), "source": c["source"], "source_url": c.get("source_url"),
           "confidence": 0.9 if c["source"] == "fmcsa_census" else 0.7, "alternates": [], "conflict": False}
    if (site := website_for(c)) and os.getenv("APIFY_TOKEN"):
        sc = scrape_company(site)
        for kind in ("phones", "emails"):
            for v in sc.get(kind, []):
                if v not in (out["phone"], out["email"]):
                    out["alternates"].append({"type": kind[:-1], "value": v, "source": "apify_website"})
        out["conflict"] = bool(out["alternates"])
    return out


def scrape_company(url_or_name: str) -> dict:
    if _mock():
        return {"website": "https://example.com", "phones": [], "emails": [], "source": "mock"}
    token = os.getenv("APIFY_TOKEN")
    if not token:
        return {"error": "APIFY_TOKEN not set"}
    if not re.match(r"https?://", url_or_name):
        return {"error": "website URL required"}
    try:
        r = httpx.post(APIFY_SCRAPER, params={"token": token}, timeout=120,
                       json={"startUrls": [{"url": url_or_name}], "maxRequestsPerStartUrl": 5, "maxDepth": 1})
        r.raise_for_status()
        items = r.json()
    except httpx.HTTPError as e:
        return {"error": f"apify: {e}"}
    phones = sorted({p for it in items for p in it.get("phones", [])})
    emails = sorted({e.lower() for it in items for e in it.get("emails", [])})
    return {"website": url_or_name, "phones": phones, "emails": emails, "source": "apify_website"}


def crs_business_credit(name: str, state: str) -> dict:
    return {"score": 72, "liens": 0, "judgments": 0, "source": "mock"} if _mock() else \
        {"error": "CRS not connected (CRS_API_KEY)"}


def crs_ofac(name: str) -> dict:
    return {"hit": False, "matches": [], "source": "mock"} if _mock() else {"error": "CRS not connected"}


def nessie_transfer(from_acct: str, to_acct: str, amount: float, memo: str) -> dict:
    return {"transfer_id": "mock-xfer", "status": "pending"} if _mock() else {"error": "Nessie not connected"}


def score_anomaly(invoice: dict) -> dict:
    return {"score": 0.9 if invoice.get("receipt_attached") is False else 0.1, "top_features": ["receipt"]}
