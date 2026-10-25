"""Settlement Approvals asks a human before releasing split holds (P-003 per invoice)."""
import pytest

from agents.controller.main import ControllerAgent
from common.envelope import DelegationMeta, TaskEnvelope
from common.runtime import TaskParked
from common.runtime_store import RuntimeStore


@pytest.fixture(autouse=True)
def fixture_environment(monkeypatch):
    monkeypatch.setenv("FREIGHTOPS_MODE", "fixture")
    monkeypatch.setenv("SPACETIME_DISABLED", "1")
    monkeypatch.delenv("SOURCE_DATA_PATH", raising=False)
    monkeypatch.delenv("HUMAN_APPROVER_ID", raising=False)


def release_source(holds):
    return {"case_id": "CASE-REL", "invoice_id": "INV-REL", "load_id": "LOAD-REL", "carrier_id": "C-REL",
            "invoice_amount": 9400, "contract_rate": 9400, "evidence_refs": ["source:invoice"],
            "requested_action": "release_hold", "hold_id": "HOLD-A", "release_verified": True, "holds": holds}


async def release(source):
    store = RuntimeStore("sqlite+aiosqlite:///:memory:")
    env = TaskEnvelope(task_type="review_hold", case_id=source["case_id"], subject=source)
    try:
        return await ControllerAgent(store=store).handle(
            env, DelegationMeta(case_id=env.case_id, from_agent="carrier_ap"), "task-release", None)
    finally:
        await store.close()


async def test_split_holds_wait_for_a_human_before_release():
    holds = [{"id": "HOLD-A", "case_id": "CASE-REL", "invoice_id": "INV-REL", "amount": 4700, "released_amount": 0},
             {"id": "HOLD-B", "case_id": "CASE-REL", "invoice_id": "INV-REL", "amount": 4700, "released_amount": 0}]
    with pytest.raises(TaskParked):
        await release(release_source(holds))


async def test_released_holds_on_the_invoice_still_count():
    holds = [{"id": "HOLD-A", "case_id": "CASE-REL", "invoice_id": "INV-REL", "amount": 4000, "released_amount": 0},
             {"id": "HOLD-0", "case_id": "CASE-REL", "invoice_id": "INV-REL", "amount": 4000,
              "released_amount": 4000, "status": "released"},
             {"id": "HOLD-X", "case_id": "CASE-REL", "invoice_id": "INV-OTHER", "amount": 9000, "released_amount": 0}]
    with pytest.raises(TaskParked):
        await release(release_source(holds))


async def test_small_invoice_total_releases_without_a_human():
    holds = [{"id": "HOLD-A", "case_id": "CASE-REL", "invoice_id": "INV-REL", "amount": 300, "released_amount": 0},
             {"id": "HOLD-X", "case_id": "CASE-REL", "invoice_id": "INV-OTHER", "amount": 9000, "released_amount": 0}]
    result = await release(release_source(holds))
    assert result.outcome == "approve"
    assert result.findings["release"]["proposed_payload"]["amount"] == 300
