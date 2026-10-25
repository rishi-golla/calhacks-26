"""Intake context memory (harness/ingest/context.py)."""
import uuid

import httpx
import pytest

from harness.ingest.context import ES_URL, ContextStore, tag


@pytest.mark.parametrize(("note", "expected"), [
    ("identity: payee for C-1 is 'X' (directory: 'Y'): factoring notice or payment diversion?", "identity: payee"),
    ("duplicate of EVT-1 (same invoice number, 1.0 days apart; P-007): not sent again", "duplicate"),
    ("input: POD is not signed", "input: POD"),
    ("line items sum 2150.0 != invoice_amount 2250.0", "math: line items"),
    ("verifier pii: EMAIL", "pii leftover"),
])
def test_tags(note, expected):
    assert tag(note) == expected


def test_history_warning_only_for_identity_or_duplicate_flags():
    ctx = {"carrier": {"carrier_id": "C-1", "flags_90d": {"identity: payee": 2, "math: line items": 5}}}
    assert ContextStore.signals(ctx) == ["history: C-1 had 2 identity/duplicate flags in 90 days (identity: payee x2)"]
    assert ContextStore.signals({"carrier": {"carrier_id": "C-1", "flags_90d": {"input: POD": 1}}}) == []
    assert ContextStore.signals(None) == []


async def test_disabled_store_is_a_no_op():
    store = ContextStore("")
    assert await store.build("EVT-1", "C-1", "text", 0) is None
    await store.remember({"event_id": "EVT-1"})   # must not raise
    await store.aclose()


@pytest.fixture
async def live_store():
    store = ContextStore(index=f"w1-test-{uuid.uuid4().hex[:8]}")
    try:
        (await store._client.get(ES_URL, timeout=1)).raise_for_status()
    except httpx.HTTPError:
        await store.aclose()
        pytest.skip("local elasticsearch not running")
    yield store
    await store._client.delete(f"{store.url}/{store.index}")
    await store.aclose()


async def test_context_surfaces_carrier_history_and_similar_documents(live_store):
    day = 86400
    docs = [
        ({"event_id": "EVT-A", "type": "carrier_invoice.received"}, {"case_id": "CASE-1", "invoice_amount": 1000.0},
         ["identity: payee for C-9 is 'APEX CAPITAL' (directory: 'NINE TRUCKING')"],
         "CARRIER INVOICE linehaul detention lumper TOTAL DUE 1000.00 remit to Apex Capital", 0),
        ({"event_id": "EVT-B", "type": "carrier_invoice.received"}, {"case_id": "CASE-2", "invoice_amount": 3000.0},
         [], "CARRIER INVOICE linehaul fuel surcharge TOTAL DUE 3000.00", 1 * day),
        ({"event_id": "EVT-OLD", "type": "carrier_invoice.received"}, {"case_id": "CASE-0", "invoice_amount": 50.0},
         ["duplicate of EVT-Z"], "old", -200 * day),   # outside the 90-day window
    ]
    for ev, payload, notes, text, at in docs:
        await live_store.remember(ContextStore.record(ev, payload, carrier_id="C-9", at=1_800_000_000 + at,
                                                      status="accepted", notes=notes, text=text))
    await live_store._client.post(f"{live_store.url}/{live_store.index}/_refresh")
    ctx = await live_store.build("EVT-NEW", "C-9", "CARRIER INVOICE linehaul detention lumper remit to Apex Capital",
                                 1_800_000_000 + 2 * day)
    carrier = ctx["carrier"]
    assert carrier["documents_90d"] == 2 and carrier["invoices_90d"] == 2 and carrier["avg_invoice"] == 2000.0
    assert carrier["flags_90d"] == {"identity: payee": 1} and ctx["warning"].startswith("history: C-9 had 1")
    assert ctx["similar"][0]["event_id"] == "EVT-A"
