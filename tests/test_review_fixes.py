import pytest
from fastapi.testclient import TestClient

import router.main as router
from common import a2a_client, parking, store


@pytest.fixture(autouse=True)
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("OPS_DB", str(tmp_path / "ops.db"))


def test_sheet_values_decoded_and_cell_history():
    store.commit_change_set(case_id="C1", agent="carrier_ap", task_id="t1", action="a",
                            cells=[{"sheet": "ap", "row": "I1", "col": "status", "after": "approved"},
                                   {"sheet": "ap", "row": "I1", "col": "amt", "after": 1850.0}])
    store.commit_change_set(case_id="C1", agent="carrier_ap", task_id="t1", action="b",
                            cells=[{"sheet": "ap", "row": "I1", "col": "status", "after": "paid"}])
    vals = {c["col"]: c["value"] for c in store.sheet("ap")}
    assert vals == {"status": "paid", "amt": 1850.0}
    assert [h["after"] for h in store.cell_history("ap", "I1", "status")] == ["approved", "paid"]
    assert store.verify_chain()


async def test_failed_resume_keeps_key_retriable(monkeypatch):
    store.park("call_x", "call", "voice_desk", "t1", "ctx", "C1", {})

    async def down(*a, **k):
        raise ConnectionError("agent restarting")
    monkeypatch.setattr(a2a_client, "resume_task", down)
    assert await parking.resume_parked("call_x", {"status": "completed"}, kind="call") is None
    assert [p["key"] for p in store.list_parked("call")] == ["call_x"]

    async def up(*a, **k):
        return True
    monkeypatch.setattr(a2a_client, "resume_task", up)
    assert await parking.resume_parked("call_x", {}, kind="approval") is None  # wrong kind
    assert await parking.resume_parked("call_x", {}, kind="call")
    assert await parking.resume_parked("call_x", {}, kind="call") is None  # duplicate


EV = {"event_id": "E1", "type": "carrier_invoice.received",
      "payload": {"case_id": "C1", "invoice_id": "I1", "invoice_amount": 2150, "contract_rate": 1850}}


async def test_router_retries_after_dispatch_failure(monkeypatch):
    calls = []

    async def fail(*a, **k):
        raise ConnectionError("down")

    class T:
        id = "task-1"

    async def ok(*a, **k):
        calls.append(a)
        return T()
    monkeypatch.setattr(a2a_client, "start_task", fail)
    with pytest.raises(ConnectionError):
        await router.dispatch(EV)
    monkeypatch.setattr(a2a_client, "start_task", ok)
    assert await router.dispatch(EV) == "task-1"
    assert await router.dispatch(EV) is None
    assert len(calls) == 1


@pytest.mark.parametrize("bad", [-5, "2150", True, 10_000_000])
def test_router_rejects_invalid_amounts(bad):
    assert router.plan({**EV, "payload": {**EV["payload"], "invoice_amount": bad}}) is None


def test_gateway_requires_tokens(monkeypatch):
    from gateway.app import app
    c = TestClient(app)
    monkeypatch.delenv("GATEWAY_TOKEN", raising=False)
    assert c.get("/parked").status_code == 503
    monkeypatch.setenv("GATEWAY_TOKEN", "tok")
    assert c.get("/parked").status_code == 401
    assert c.get("/parked", headers={"x-gateway-token": "tok"}).status_code == 200
    assert c.post("/approvals/appr_1", json={"decision": "approve", "approver": "x"}).status_code == 401
    monkeypatch.setenv("CARTESIA_WEBHOOK_SECRET", "s")
    assert c.post("/voice/tools/get_payment_status", json={}).status_code == 401
    s = {"x-webhook-secret": "s"}
    assert c.post("/voice/tools/get_case_summary", headers=s, json={"call_id": "nope"}).status_code == 403
    store.park("call_1", "call", "voice_desk", "t", "C9", "C9", {})
    store.commit_change_set(case_id="C9", agent="carrier_ap", task_id="t", action="a",
                            cells=[{"sheet": "ap_subledger", "row": "INV9", "col": "status", "after": "on_hold"}])
    assert c.post("/voice/tools/get_payment_status", headers=s,
                  json={"call_id": "call_1", "invoice_id": "INV9"}).json()["status"] == {"status": "on_hold"}
    assert c.post("/voice/tools/get_payment_status", headers=s,
                  json={"call_id": "call_1", "invoice_id": "OTHER"}).status_code == 403
    assert c.post("/voice/webhook", headers={"x-webhook-secret": "s"}, json={"call_id": "nope"}).json() == {"ok": False}


async def test_router_concurrent_deliveries_dispatch_once(monkeypatch):
    import asyncio
    calls = []

    class T:
        id = "task-1"

    async def slow(*a, **k):
        calls.append(a)
        await asyncio.sleep(0.05)
        return T()
    monkeypatch.setattr(a2a_client, "start_task", slow)
    res = await asyncio.gather(router.dispatch(EV), router.dispatch(EV))
    assert sorted(map(str, res)) == ["None", "task-1"]
    assert len(calls) == 1


def test_transcript_denial_vs_missing_receipt():
    from common.transcripts import CONFIRM, DENY
    said = "Yes, the driver waited two hours. We have no receipt though."
    assert not DENY.search(said) and CONFIRM.search(said)
    assert DENY.search("Driver was in and out, we withdraw the detention.")
    assert DENY.search("No, the driver didn't wait over two hours.")
