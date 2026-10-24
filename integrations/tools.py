"""Integration tools called by agents (PRD 7.6). Return {"error": ...} instead of raising.
MOCK_INTEGRATIONS=1 returns canned data so other workstreams aren't blocked."""
import os

MOCK = os.getenv("MOCK_INTEGRATIONS", "1") == "1"


def fmcsa_lookup(usdot: str | None = None, mc: str | None = None) -> dict:
    if MOCK:
        return {"legal_name": "MOCK CARRIER LLC", "authority_status": "ACTIVE", "phone": "+15555550100",
                "source": "mock"}
    return {"error": "not implemented (Apify FMCSA actor)"}


def enrich_contact(company_id: str, role: str) -> dict:
    if MOCK:
        return {"name": "Mock Dispatcher", "title": role, "phone": "+15555550100", "email": "mock@example.com",
                "source": "mock", "confidence": 0.5, "alternates": []}
    return {"error": "not implemented (waterfall: fmcsa -> own key -> context.dev -> website)"}


def scrape_company(url_or_name: str) -> dict:
    return {"error": "not implemented (Apify contact-info-scraper / context.dev)"} if not MOCK else \
        {"website": "https://example.com", "phones": [], "source": "mock"}


def crs_business_credit(name: str, state: str) -> dict:
    return {"score": 72, "liens": 0, "judgments": 0, "source": "mock"} if MOCK else {"error": "not implemented"}


def crs_ofac(name: str) -> dict:
    return {"hit": False, "matches": [], "source": "mock"} if MOCK else {"error": "not implemented"}


def nessie_transfer(from_acct: str, to_acct: str, amount: float, memo: str) -> dict:
    return {"transfer_id": "mock-xfer", "status": "pending"} if MOCK else {"error": "not implemented"}


def score_anomaly(invoice: dict) -> dict:
    return {"score": 0.9 if invoice.get("receipt_attached") is False else 0.1, "top_features": ["receipt"]}
